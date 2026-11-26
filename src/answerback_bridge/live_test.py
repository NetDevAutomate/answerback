"""Provision and reuse one explicitly owned environment for live collaboration tests."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from . import lifecycle
from .config import Config, atomic_write, default_config_path
from .harness import run_harness
from .process import BridgeError, herdr_env, run
from .transport import (
    check_config_owner,
    make_private_directories,
    private_directory,
    runtime_dir,
)

# OWNER is written into a new environment.json. _manifest() also accepts LEGACY_OWNERS:
# an environment that was made before the rename carries the old value and stays usable.
OWNER = "answerback-live-test"
LEGACY_OWNERS = ("herdr-bridge-live-test",)
AGENT = "test-orchestrator"
KIRO = "test-worker"
HERDR_CONFIG = "[experimental]\nallow_nested=true\n"


class LiveTestFailure(Exception):
    """A concise diagnostic which never contains raw agent or subprocess output."""


def _digest(config: Config) -> str:
    # Over the flat names: the digests recorded in existing environment manifests were
    # taken before the config split, and the same configuration must still match them.
    return hashlib.sha256(json.dumps(config.flat_values(), sort_keys=True).encode()).hexdigest()


def _field_digests(config: Config) -> dict[str, str]:
    """Retain field-level drift evidence without copying configuration values."""
    return {
        name: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        for name, value in config.flat_values().items()
    }


def _manifest(directory: Path) -> dict[str, Any]:
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.getuid():
        raise LiveTestFailure("The test directory must be a real directory owned by this user")
    path = directory / "environment.json"
    if path.is_symlink() or not path.is_file():
        raise LiveTestFailure("Existing directory has no matching environment ownership record")
    value = json.loads(path.read_text())
    expected = {
        "schema": 1,
        "root": str(directory),
        "workspace": str(directory / "workspace"),
        "agent": AGENT,
    }
    if (
        not isinstance(value, dict)
        or value.get("owner") not in (OWNER, *LEGACY_OWNERS)
        or any(value.get(k) != v for k, v in expected.items())
    ):
        raise LiveTestFailure("Environment ownership, directory, workspace or agent was changed")
    session = value.get("session", "")
    if not isinstance(session, str) or not (
        session == "bridge-tests" or session.startswith("bridge-tests-")
    ):
        raise LiveTestFailure("Only a dedicated bridge-tests named session may be reused")
    if value.get("server_launch") in ("uncertain", "collision"):
        raise LiveTestFailure("Server launch ownership is uncertain; inspect it before any retry")
    if bool(value.get("pane_id")) != bool(value.get("terminal_id")):
        raise LiveTestFailure("Saved pane identity is incomplete; inspect the environment record")
    for name in ("config.json", "herdr.toml", "workspace", "reports", "run.lock", "server.log"):
        if (directory / name).is_symlink():
            raise LiveTestFailure("Refusing a symlink inside the owned test environment")
    return value


def _lock(directory: Path) -> int:
    descriptor = os.open(directory / "run.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise LiveTestFailure("Another live test is already running for this directory") from None
    return descriptor


def _launch_server(config: Config, directory: Path) -> subprocess.Popen[bytes]:
    environment = herdr_env()
    environment["HERDR_CONFIG_PATH"] = str(directory / "herdr.toml")
    descriptor = os.open(
        directory / "server.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "ab") as output:
        info = os.fstat(output.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise LiveTestFailure("Server log must be a regular file owned by this user")
        os.fchmod(output.fileno(), 0o600)
        # Environment.claim() always gives the live-test config a named session.
        return subprocess.Popen(
            [config.herdr_binary, "--session", cast(str, config.herdr_session), "server"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            env=environment,
            cwd=str(directory / "workspace"),
            start_new_session=True,
            close_fds=True,
        )


def _socket_peer_pid(path: Path) -> int:
    """Read kernel credentials without sending a protocol request to the server."""
    if sys.platform == "darwin":
        level, option, layout = 0, 2, "i"  # SOL_LOCAL, LOCAL_PEERPID, pid_t
    elif sys.platform == "linux":
        level, option, layout = socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), "3i"
    else:
        raise LiveTestFailure("Server peer PID verification is unsupported on this platform")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(path))
            credentials = connection.getsockopt(level, option, struct.calcsize(layout))
            pid = struct.unpack(layout, credentials)[0]
    except (OSError, struct.error):
        raise LiveTestFailure(
            "Unable to verify the named server's operating-system peer identity"
        ) from None
    if pid <= 0:
        raise LiveTestFailure("Named server returned no valid operating-system peer identity")
    return pid


def _save_report(report: dict[str, Any], directory: Path | None, fallback_root: Path) -> None:
    """Save into ``directory``, or ``fallback_root`` when the environment was never claimed.

    ``fallback_root`` is a single, bounded location reused across every refused run,
    never a fresh scratch directory per call, so repeated refusals accumulate report
    files in one place instead of leaking a new, never-removed directory each time.
    """
    if directory is None:
        try:
            private_directory(fallback_root)
        except BridgeError as error:
            raise LiveTestFailure("Report fallback root must be a private directory") from error
        directory = fallback_root
    reports = directory / "reports"
    if reports.is_symlink():
        raise LiveTestFailure("Refusing a symlinked report destination")
    reports.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex + ".json"
    report["report_path"] = str(reports / filename)
    atomic_write(reports / filename, json.dumps(report, indent=2) + "\n")


class Environment:
    def __init__(self, directory: Path, deadline: float) -> None:
        self.directory = directory
        self.deadline = deadline
        self.manifest: dict[str, Any] = {}
        self.config: Config
        self.lock: int | None = None
        self.fresh = False
        self.spawned: subprocess.Popen[bytes] | None = None
        self.server_socket: Path | None = None

    def remaining(self, maximum: float = 28) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise LiveTestFailure("Live test deadline expired; resources and receipts are retained")
        return min(maximum, remaining)

    async def save(self, **fields: Any) -> None:
        self.manifest.update(fields)
        await asyncio.to_thread(
            atomic_write,
            self.directory / "environment.json",
            json.dumps(self.manifest, indent=2) + "\n",
        )

    async def verify_spawned(self, path: Path | None = None) -> None:
        if self.spawned is None:
            return
        path = path or self.server_socket
        if self.spawned.poll() is not None:
            await self.save(server_launch="uncertain")
            raise LiveTestFailure(
                "Launched Herdr process exited before ownership was verified; inspect server.log"
            )
        if path is None or not path.is_absolute():
            await self.save(server_launch="uncertain")
            raise LiveTestFailure("Named server did not report an absolute socket path")
        try:
            async with asyncio.timeout(self.remaining(3)):
                peer_pid = await asyncio.to_thread(_socket_peer_pid, path)
        except (LiveTestFailure, TimeoutError):
            await self.save(server_launch="uncertain")
            raise
        if peer_pid != self.spawned.pid:
            await self.save(server_launch="collision")
            raise LiveTestFailure(
                "Another process owns the named server socket; no test work was dispatched"
            )
        if self.spawned.poll() is not None:
            await self.save(server_launch="uncertain")
            raise LiveTestFailure("Launched Herdr process exited during readiness verification")
        self.server_socket = path

    async def command(self, *args: str) -> dict[str, Any]:
        if args[:2] in (("workspace", "create"), ("agent", "start")):
            await self.verify_spawned()
        environment = herdr_env()
        environment["HERDR_CONFIG_PATH"] = str(self.directory / "herdr.toml")
        # claim() always gives the live-test config a named session.
        output = await run(
            [self.config.herdr_binary, "--session", cast(str, self.config.herdr_session), *args],
            env=environment,
            max_seconds=self.remaining(),
        )
        value = json.loads(output.stdout)
        if not isinstance(value, dict) or "error" in value:
            raise LiveTestFailure("Herdr rejected the named-session operation; inspect its state")
        return value.get("result", value)

    async def sessions(self) -> list[dict[str, Any]]:
        result = await self.command("session", "list", "--json")
        sessions = result.get("sessions")
        if not isinstance(sessions, list):
            raise LiveTestFailure("Herdr returned an unsupported session inventory")
        selected = [
            session for session in sessions if session.get("name") == self.config.herdr_session
        ]
        if len(selected) > 1:
            raise LiveTestFailure("The saved Herdr session name is ambiguous")
        return selected

    async def reset_config(self, expected: Config) -> Path:
        """Back up and reset only an owned, stopped test bridge's configuration."""
        config_path = self.directory / "config.json"
        root = runtime_dir(config_path)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise LiveTestFailure("Test bridge runtime must be a directory owned by this user")
        check_config_owner(config_path)
        descriptor = os.open(root / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise LiveTestFailure("Test bridge lock must be a regular file owned by this user")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LiveTestFailure(
                    "Stop the test bridge with its explicit --config before --reset-test-env"
                ) from None
            private_directory(root)
            # The service takes this same lock before loading config, so a simultaneous
            # start cannot load the old config while it is being replaced.
            with tempfile.NamedTemporaryFile(
                dir=self.directory, prefix="config.before-reset-", suffix=".json", delete=False
            ) as backup:
                backup.write(await asyncio.to_thread(config_path.read_bytes))
                backup.flush()
                os.fsync(backup.fileno())
                backup_path = Path(backup.name)
            await asyncio.to_thread(expected.save, config_path)
            await self.save(
                config_digest=_digest(expected), config_field_digests=_field_digests(expected)
            )
            self.config = expected
            return backup_path
        finally:
            os.close(descriptor)

    async def claim(self, *, reset_test_env: bool = False) -> Path | None:
        if len(os.fsencode(self.directory / "runtime" / "bridge.sock")) > 100:
            raise LiveTestFailure(
                "Test directory is too long for the local bridge socket; use a shorter path"
            )
        exists = await asyncio.to_thread(self.directory.exists)
        if exists:
            self.manifest = await asyncio.to_thread(_manifest, self.directory)
            self.lock = _lock(self.directory)
            # Re-read under the lock so a previous run cannot leave us a stale identity.
            self.manifest = await asyncio.to_thread(_manifest, self.directory)
            self.config = await asyncio.to_thread(Config.load, self.directory / "config.json")
            expected = Config(
                self.manifest["session"],
                herdr_binary=self.config.herdr_binary,
                codex_binary=self.config.codex_binary,
                kiro_binary=self.config.kiro_binary,
                kiro_workspaces={KIRO: str(self.directory / "workspace")},
            )
            changed = {
                name
                for name in ("herdr_session", "kiro_workspaces", "desktop_threads", "crew_enabled")
                if getattr(self.config, name) != getattr(expected, name)
            }
            digest_changed = self.manifest.get("config_digest", _digest(self.config)) != _digest(
                self.config
            )
            if digest_changed:
                recorded = self.manifest.get("config_field_digests")
                # Older manifests have only one aggregate hash. Compare their config
                # with the owned template, but do not infer why the digest changed:
                # the template already copies the current executable paths.
                baseline = recorded if isinstance(recorded, dict) else _field_digests(expected)
                changed.update(
                    name
                    for name, digest in _field_digests(self.config).items()
                    if baseline.get(name) != digest
                )
            if (changed or digest_changed) and not reset_test_env:
                fields = ", ".join(sorted(changed)) or (
                    "config_digest (aggregate digest changed; per-field history unavailable)"
                )
                raise LiveTestFailure(
                    f"Saved bridge configuration at {self.directory / 'config.json'} "
                    f"no longer matches this test environment. Changed fields: {fields}. "
                    "After settling outstanding work, stop the test bridge with its explicit "
                    "--config, then run answerback live-test with the same --directory and "
                    "--reset-test-env to back up and reset only its configuration. "
                    "See docs/LIVE-TEST.md; Herdr App tabs use the attended hc checklist."
                )
            for binary in (
                self.config.herdr_binary,
                self.config.codex_binary,
                self.config.kiro_binary,
            ):
                if not Path(binary).is_absolute() or shutil.which(binary) is None:
                    raise LiveTestFailure(
                        "A saved test executable is unavailable or no longer absolute"
                    )
            data = await asyncio.to_thread((self.directory / "herdr.toml").read_text)
            if tomllib.loads(data) != {"experimental": {"allow_nested": True}}:
                raise LiveTestFailure("The dedicated Herdr configuration was changed")
            if not await asyncio.to_thread((self.directory / "workspace").is_dir):
                raise LiveTestFailure("The saved test workspace is missing")
            if (
                not self.manifest.get("pane_id")
                and self.manifest.get("workspace_creation") != "not_started"
            ):
                raise LiveTestFailure(
                    "Workspace creation is uncertain; inspect it before any retry"
                )
            if reset_test_env:
                return await self.reset_config(expected)
            return None
        if reset_test_env:
            raise LiveTestFailure("--reset-test-env requires an existing owned test environment")
        binaries = [shutil.which(name) for name in ("herdr", "codex", "kiro-cli")]
        if not all(binaries):
            raise LiveTestFailure("Herdr, Codex and Kiro CLI must be installed before provisioning")
        session = "bridge-tests-" + hashlib.sha256(str(self.directory).encode()).hexdigest()[:8]
        # all() above rules out a missing binary; ty does not narrow list elements.
        self.config = Config(
            session,
            herdr_binary=cast(str, binaries[0]),
            codex_binary=cast(str, binaries[1]),
            kiro_binary=cast(str, binaries[2]),
            kiro_workspaces={KIRO: str(self.directory / "workspace")},
        )
        if await self.sessions():
            raise LiveTestFailure(
                "Named Herdr session already exists without this environment's ownership"
            )
        await asyncio.to_thread(make_private_directories, self.directory, exist_ok=False)
        self.fresh = True
        self.manifest = {
            "schema": 1,
            "owner": OWNER,
            "session": session,
            "root": str(self.directory),
            "workspace": str(self.directory / "workspace"),
            "agent": AGENT,
            "workspace_creation": "not_started",
            "server_launch": "not_started",
            "agent_creation": "not_started",
            "config_digest": _digest(self.config),
            "config_field_digests": _field_digests(self.config),
        }
        await self.save()
        self.lock = _lock(self.directory)
        await asyncio.to_thread((self.directory / "workspace").mkdir, mode=0o700)
        await asyncio.to_thread(self.config.save, self.directory / "config.json")
        await asyncio.to_thread(atomic_write, self.directory / "herdr.toml", HERDR_CONFIG)
        return None

    async def server(self) -> list[dict[str, Any]]:
        selected = await self.sessions()
        if (self.fresh or self.manifest.get("server_launch") == "not_started") and selected:
            await self.save(server_launch="collision")
            raise LiveTestFailure(
                "A session collision appeared before startup; no server was launched"
            )
        if (
            self.manifest.get("session_dir")
            and selected
            and selected[0].get("session_dir") != self.manifest["session_dir"]
        ):
            raise LiveTestFailure("Saved native session directory changed; refusing to proceed")
        if selected and selected[0].get("connection_error"):
            raise LiveTestFailure("Saved server is unreachable; refusing a competing server launch")
        if not selected or not selected[0].get("running"):
            await self.save(server_launch="uncertain")
            self.spawned = await asyncio.to_thread(_launch_server, self.config, self.directory)
        for _ in range(50):
            selected = await self.sessions()
            if self.spawned is not None and self.spawned.poll() is not None:
                raise LiveTestFailure(
                    "Launched Herdr process exited; inspect server.log before retrying"
                )
            if selected and selected[0].get("running"):
                raw_socket = selected[0].get("socket_path")
                socket_path = Path(raw_socket) if isinstance(raw_socket, str) else None
                await self.verify_spawned(socket_path)
                result = await self.command("pane", "list")
                await self.verify_spawned(socket_path)
                if not isinstance(result.get("panes"), list):
                    raise LiveTestFailure("Herdr returned an unsupported pane inventory")
                observed = (
                    {"session_dir": selected[0]["session_dir"]}
                    if selected[0].get("session_dir")
                    else {}
                )
                if self.spawned is not None:
                    observed["server_pid"] = self.spawned.pid
                await self.save(server_launch="ready", **observed)
                return result["panes"]
            await asyncio.sleep(min(0.2, self.remaining()))
        raise LiveTestFailure("Named headless server did not become ready; inspect server.log")

    async def workspace(self, panes: list[dict[str, Any]]) -> None:
        pane_id = self.manifest.get("pane_id")
        if pane_id:
            selected = [pane for pane in panes if pane.get("pane_id") == pane_id]
            if len(selected) != 1 or selected[0].get("terminal_id") != self.manifest["terminal_id"]:
                raise LiveTestFailure(
                    "Saved pane or terminal was replaced or is missing; no recreation"
                )
            if selected[0].get("cwd") not in (None, self.manifest["workspace"]):
                raise LiveTestFailure("Saved pane is now in another working directory")
            return
        if self.manifest.get("workspace_creation") != "not_started":
            raise LiveTestFailure("Workspace creation is uncertain; no automatic retry")
        await self.save(workspace_creation="uncertain")
        result = await self.command(
            "workspace",
            "create",
            "--cwd",
            self.manifest["workspace"],
            "--label",
            "Bridge collaboration tests",
            "--no-focus",
        )
        pane = result.get("root_pane", {})
        if not pane.get("pane_id") or not pane.get("terminal_id"):
            raise LiveTestFailure(
                "Workspace response did not establish stable pane and terminal identities"
            )
        await self.save(
            pane_id=pane["pane_id"], terminal_id=pane["terminal_id"], workspace_creation="created"
        )

    def identity(self, agents: list[dict[str, Any]]) -> dict[str, Any] | None:
        selected = [
            agent
            for agent in agents
            if agent.get("name") == AGENT or agent.get("pane_id") == self.manifest["pane_id"]
        ]
        if not selected:
            return None
        if len(selected) != 1:
            raise LiveTestFailure("Dedicated test agent identity is ambiguous")
        agent = selected[0]
        expected = {
            "name": AGENT,
            "agent": "codex",
            "pane_id": self.manifest["pane_id"],
            "terminal_id": self.manifest["terminal_id"],
        }
        if any(agent.get(key) != value for key, value in expected.items()):
            raise LiveTestFailure("Saved test agent was replaced; refusing the new occupant")
        previous = self.manifest.get("agent_snapshot", {})
        if (
            previous.get("agent_session")
            and agent.get("agent_session") != previous["agent_session"]
        ):
            raise LiveTestFailure(
                "Saved agent session identity changed; manual inspection is required"
            )
        cwd = agent.get("foreground_cwd") or agent.get("cwd")
        if cwd != self.manifest["workspace"]:
            raise LiveTestFailure("Test agent is not in the dedicated workspace")
        return agent

    async def agents(self) -> list[dict[str, Any]]:
        result = await self.command("agent", "list")
        if not isinstance(result.get("agents"), list):
            raise LiveTestFailure("Herdr returned an unsupported agent inventory")
        return result["agents"]

    async def startup_observation(self) -> dict[str, Any] | None:
        observed: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(self.remaining(20)):
                for _ in range(100):
                    observed = self.identity(await self.agents())
                    if observed is not None:
                        await self.save(agent_snapshot=observed, agent_creation="observed")
                        if observed.get("agent_status") != "working":
                            return observed
                    await asyncio.sleep(0.2)
        except TimeoutError:
            pass
        return observed

    async def agent(self) -> dict[str, Any]:
        agent = self.identity(await self.agents())
        if agent is None:
            if (
                self.manifest.get("agent_snapshot")
                or self.manifest.get("agent_creation") != "not_started"
            ):
                raise LiveTestFailure(
                    "Saved test agent is absent or its launch is uncertain; no automatic retry"
                )
            await self.save(agent_creation="uncertain")
            socket = json.dumps(str(self.directory / "runtime" / "bridge.sock"))
            profile = (
                'permissions.herdr_test={extends=":workspace",network={enabled=true,domains={},'
                "dangerously_allow_all_unix_sockets=false,allow_local_binding=false,"
                f'unix_sockets={{{socket}="allow"}}}}}}'
            )
            try:
                await self.command(
                    "agent",
                    "start",
                    AGENT,
                    "--kind",
                    "codex",
                    "--pane",
                    self.manifest["pane_id"],
                    "--timeout",
                    str(min(20000, max(1, int(self.remaining() * 1000)))),
                    "--",
                    "--no-alt-screen",
                    "--no-daemon",
                    "--enable",
                    "network_proxy",
                    "-a",
                    "on-request",
                    "-C",
                    self.manifest["workspace"],
                    "-c",
                    'model_reasoning_effort="low"',
                    "-c",
                    profile,
                    "-c",
                    'default_permissions="herdr_test"',
                )
            except (BridgeError, TimeoutError):
                # A failed start can leave an agent loading or blocked. Observe, never resend.
                pass
            agent = await self.startup_observation()
            if agent is None:
                raise LiveTestFailure(
                    "Agent start did not establish identity; inspect startup or profile support. "
                    "The restricted profile was tested with Codex 0.167.0; no fallback was used."
                )
        await self.save(
            agent_snapshot=agent, agent_creation="observed", config_digest=_digest(self.config)
        )
        if (
            agent.get("agent_status") not in ("idle", "done")
            or agent.get("interactive_ready") is not True
        ):
            raise LiveTestFailure(
                f"Test agent is {agent.get('agent_status', 'unknown')}; no probe was sent"
            )
        return agent


async def run_live_test(
    directory: Path,
    timeout: int = 300,  # noqa: ASYNC109 -- Public CLI duration, bounded internally.
    report_dir: Path | None = None,
    *,
    reset_test_env: bool = False,
) -> dict[str, Any]:
    """Retain one owned test environment and save every outcome as a unique JSON report.

    A refused or failed run (one that never reaches an owned, verified ``directory``)
    still saves its report, but never inside that unverified ``directory``. It is saved
    under ``report_dir`` when given, or otherwise under this bridge's own configured
    runtime directory: one bounded location reused across calls, never a fresh
    temporary directory minted on every refusal.
    """
    report: dict[str, Any] = {
        "mode": "live-test",
        "success": False,
        "phases": [],
        "resources_retained": True,
        "outstanding_task_ids": [],
    }
    phase: dict[str, Any] = {"name": "preflight", "status": "running"}
    report["phases"].append(phase)
    environment: Environment | None = None
    report_directory: Path | None = None
    fallback_root = runtime_dir(default_config_path())
    try:
        if report_dir is not None:
            if await asyncio.to_thread(report_dir.is_symlink):
                raise LiveTestFailure("Refusing a symlinked report directory")
            fallback_root = await asyncio.to_thread(lambda: report_dir.expanduser().resolve())
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
            raise LiveTestFailure("Live test timeout must be an integer between 1 and 600 seconds")
        if await asyncio.to_thread(directory.is_symlink):
            raise LiveTestFailure("Refusing a symlinked test directory")
        resolved = await asyncio.to_thread(lambda: directory.expanduser().resolve())
        report["directory"] = str(resolved)
        environment = Environment(resolved, time.monotonic() + timeout)
        backup = await environment.claim(reset_test_env=reset_test_env)
        report_directory = resolved
        report.update(
            session=environment.config.herdr_session,
            agent=AGENT,
            workspace=environment.manifest["workspace"],
            config_path=str(resolved / "config.json"),
        )
        phase["status"] = "passed"
        if reset_test_env:
            report.update(success=True, reset_only=True, config_backup=str(backup))
            return report
        for name in ("server", "workspace", "agent", "bridge", "harness"):
            phase = {"name": name, "status": "running"}
            report["phases"].append(phase)
            if name == "server":
                panes = await environment.server()
            elif name == "workspace":
                await environment.workspace(panes)
            elif name == "agent":
                agent = await environment.agent()
                phase.update(
                    pane_id=agent["pane_id"],
                    terminal_id=agent["terminal_id"],
                    state=agent["agent_status"],
                )
            elif name == "bridge":
                async with asyncio.timeout(environment.remaining()):
                    await lifecycle.start(resolved / "config.json", standalone=True)
            else:
                result = await run_harness(
                    resolved / "config.json",
                    mode="live",
                    herdr_peer=f"herdr:{AGENT}",
                    kiro_peer=f"kiro:{KIRO}",
                    timeout=max(1, int(environment.remaining(600))),
                )
                report["harness"] = result
                report["outstanding_task_ids"] = result.get("outstanding_task_ids", [])
                if not result.get("success"):
                    raise LiveTestFailure(
                        "Collaboration probe did not pass; inspect its recorded phases and receipts"
                    )
                report["success"] = True
            phase["status"] = "passed"
    except asyncio.CancelledError:
        phase.update(status="failed", detail="Live test was interrupted; resources are retained")
        raise
    except LiveTestFailure as error:
        phase.update(status="failed", detail=str(error))
    except Exception as error:
        phase.update(
            status="failed",
            detail=(
                f"Setup operation failed ({type(error).__name__}); inspect this phase. "
                "Raw process output is omitted."
            ),
        )
    finally:
        try:
            if (
                report_directory is None
                and environment is not None
                and environment.lock is not None
            ):
                report_directory = environment.directory
            try:
                await asyncio.to_thread(_save_report, report, report_directory, fallback_root)
            except (OSError, LiveTestFailure):
                report.setdefault("limitations", []).append(
                    "Owned report destination unavailable; saved privately elsewhere."
                )
                await asyncio.to_thread(_save_report, report, None, fallback_root)
        finally:
            if environment is not None and environment.lock is not None:
                os.close(environment.lock)
    return report
