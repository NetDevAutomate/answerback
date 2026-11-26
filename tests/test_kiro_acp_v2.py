"""Kiro ACP v2: argv, agent selection and the opt-in live qualification.

Hermetic argv, validation and refusal tests always run. The two live tests spend model
quota and grant Kiro local tool access (`-a`, or `--trust-tools read`), so they
additionally require ANSWERBACK_LIVE_KIRO_V2=1: the client probe, and one assignment
through the real managed `Workers` route.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

from answerback_bridge.acp_client import KiroACP
from answerback_bridge.kiro_agent import AGENT_NAME, agent_document
from answerback_bridge.process import BridgeError
from answerback_bridge.workers import worker_profile

LIVE_MODEL = "claude-haiku-5.5"
KIRO_CLI = shutil.which("kiro-cli")
LIVE_ENABLED = os.environ.get("ANSWERBACK_LIVE_KIRO_V2") == "1"
SCOPE_SERVER_NAME = "excluded_scope_probe"
SCOPE_MARKER_NAME = "excluded-scope-server-started"
XDG_HOME_VARIABLES = (
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)


async def ignore_update(update: dict[str, Any]) -> None:
    pass


async def cancel_permission(request: dict[str, Any]) -> None:
    return None


def kiro_client(
    *,
    engine: Literal["v3", "v2"] = "v3",
    trust_all_tools: bool = False,
    trust_tools: list[str] | tuple[str, ...] = (),
    agent: str | None = None,
) -> KiroACP:
    return KiroACP(
        "kiro-cli",
        "/tmp",
        ignore_update,
        cancel_permission,
        agent_engine=engine,
        trust_all_tools=trust_all_tools,
        trust_tools=trust_tools,
        agent=agent,
    )


def test_kiro_constructor_rejects_unknown_engine() -> None:
    bad_engine: Any = "v1"
    with pytest.raises(ValueError, match="Kiro ACP engine must be 'v2' or 'v3'"):
        KiroACP(
            "kiro-cli",
            "/tmp",
            ignore_update,
            cancel_permission,
            agent_engine=bad_engine,
        )


def test_kiro_constructor_rejects_non_boolean_trust() -> None:
    bad_trust: Any = 1
    with pytest.raises(ValueError, match="trust-all-tools switch must be a boolean"):
        KiroACP(
            "kiro-cli",
            "/tmp",
            ignore_update,
            cancel_permission,
            agent_engine="v2",
            trust_all_tools=bad_trust,
        )


def test_kiro_constructor_rejects_v3_trust_as_v2_only() -> None:
    with pytest.raises(ValueError, match="v2-only"):
        KiroACP(
            "kiro-cli",
            "/tmp",
            ignore_update,
            cancel_permission,
            agent_engine="v3",
            trust_all_tools=True,
        )


def test_default_kiro_argv_preserves_v3_cli_auth() -> None:
    default_client = KiroACP(
        "kiro-cli",
        "/tmp",
        ignore_update,
        cancel_permission,
    )
    assert default_client.command() == [
        "kiro-cli",
        "acp",
        "--agent-engine",
        "v3",
        "--auth-method",
        "cli",
    ]


def test_v2_trust_all_argv_uses_v2_only_flag_without_v3_auth() -> None:
    assert kiro_client(engine="v2", trust_all_tools=True).command() == [
        "kiro-cli",
        "acp",
        "--agent-engine",
        "v2",
        "-a",
    ]


V2_AGENT = ["kiro-cli", "acp", "--agent-engine", "v2", "--agent", AGENT_NAME]


@pytest.mark.parametrize(
    ("options", "argv", "trust"),
    [
        ({}, V2_AGENT, "ask"),
        ({"trust_all_tools": True}, [*V2_AGENT, "-a"], "all"),
        ({"trust_tools": ["read"]}, [*V2_AGENT, "--trust-tools", "read"], ["read"]),
        (
            {"trust_tools": ("read", "@answerback_bridge/task_wait")},
            [*V2_AGENT, "--trust-tools", "read,@answerback_bridge/task_wait"],
            ["read", "@answerback_bridge/task_wait"],
        ),
    ],
)
def test_v2_agent_argv_for_every_trust_combination(
    options: dict[str, Any], argv: list[str], trust: str | list[str]
) -> None:
    client = kiro_client(engine="v2", agent=AGENT_NAME, **options)
    assert client.command() == argv
    assert client.trust_mode == trust


def test_v3_default_trust_mode_is_ask() -> None:
    assert kiro_client().trust_mode == "ask"


def test_kiro_constructor_rejects_trust_tools_outside_v2() -> None:
    with pytest.raises(ValueError, match="--trust-tools is a v2-only flag"):
        kiro_client(engine="v3", trust_tools=["read"])


def test_kiro_constructor_rejects_agent_outside_v2() -> None:
    with pytest.raises(ValueError, match="--agent is a v2-only flag"):
        kiro_client(engine="v3", agent=AGENT_NAME)


def test_kiro_constructor_rejects_both_trust_flags() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        kiro_client(engine="v2", trust_all_tools=True, trust_tools=["read"])


@pytest.mark.parametrize(
    "trust_tools",
    [
        [""],
        ["read", "read"],
        ["a b"],
        "read",
        [1],
        # A leading dash would let a "tool name" become another launch flag.
        ["-a"],
        ["--trust-tools"],
        ["read,write"],
        ["read", "-a"],
        ["read -a"],
    ],
)
def test_kiro_constructor_rejects_malformed_trust_tools(trust_tools: Any) -> None:
    with pytest.raises(ValueError, match="Kiro trust-tools"):
        kiro_client(engine="v2", trust_tools=trust_tools)


@pytest.mark.parametrize("agent", ["", "../other", "a b", 7, "-a", "--trust-tools", "read -a"])
def test_kiro_constructor_rejects_malformed_agent_names(agent: Any) -> None:
    with pytest.raises(ValueError, match="--agent must name an on-disk agent"):
        kiro_client(engine="v2", agent=agent)


@pytest.fixture
def fake_agent(tmp_path: Path) -> tuple[str, str]:
    """The suite's deterministic stdio ACP peer (tests/fixtures/acp_agent.py)."""
    fixture = Path(__file__).parent / "fixtures" / "acp_agent.py"
    binary = tmp_path / "fake-kiro"
    binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    binary.chmod(0o700)
    (tmp_path / "agent-settings.json").write_text(json.dumps({"reported_mode": "kiro_default"}))
    return str(binary), str(tmp_path)


async def test_v2_session_reporting_kiro_default_is_refused_naming_agent_flag(
    fake_agent: tuple[str, str],
) -> None:
    """A v2 peer that answers `kiro_default` never gets a prompt; the refusal names --agent."""
    client = KiroACP(
        *fake_agent,
        ignore_update,
        cancel_permission,
        agent_engine="v2",
        agent=AGENT_NAME,
        trust_tools=["read"],
    )
    async with client:
        with pytest.raises(BridgeError) as refusal:
            await client.open_session(agent_profile=worker_profile())
    message = str(refusal.value)
    assert "did not select required agent profile 'answerback_bridge'" in message
    assert "Kiro reported 'kiro_default'" in message
    assert "--agent answerback_bridge" in message
    requests = [
        json.loads(line)
        for line in (Path(fake_agent[1]) / "agent-requests.jsonl").read_text().splitlines()
    ]
    assert not any(request["method"] == "session/prompt" for request in requests)
    assert json.loads((Path(fake_agent[1]) / "agent-argv.json").read_text()) == [
        "acp",
        "--agent-engine",
        "v2",
        "--agent",
        AGENT_NAME,
        "--trust-tools",
        "read",
    ]


REQUIRED_MCP_TOOLS = {"answerback_bridge": {"peer_submit", "task_wait"}}
MCP_DEFINITIONS = [
    {
        "name": "answerback_bridge",
        "command": "/bin/true",
        "args": [],
        "env": [],
        "_meta": {"kiro": {"waitForReady": True, "timeout": 60000}},
    }
]


def configure_fake(agent: tuple[str, str], **settings: Any) -> None:
    (Path(agent[1]) / "agent-settings.json").write_text(json.dumps(settings))


@pytest.mark.parametrize("timing", ["before", "after"])
async def test_v2_server_initialized_satisfies_readiness_and_is_recorded_unverified(
    fake_agent: tuple[str, str], timing: str
) -> None:
    """The v2 engine reports `_kiro.dev/mcp/server_initialized` per server, with no tool
    list (seen live on Kiro CLI 3.5.0); the bridge accepts the named required server and
    records that its tools were not verified."""
    configure_fake(
        fake_agent,
        reported_mode=AGENT_NAME,
        mcp_open_plans=[{timing: [{"server_initialized": "answerback_bridge"}]}],
    )
    client = KiroACP(
        *fake_agent, ignore_update, cancel_permission, agent_engine="v2", agent=AGENT_NAME
    )
    async with client:
        session = await client.open_session(
            None,
            MCP_DEFINITIONS,
            required_mcp_tools=REQUIRED_MCP_TOOLS,
            agent_profile=worker_profile(),
        )
        assert session == "new-session"
        assert client.mcp_unverified_servers == frozenset({"answerback_bridge"})


async def test_v2_server_initialized_for_another_server_does_not_satisfy_readiness(
    fake_agent: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge import acp_client

    monkeypatch.setattr(acp_client, "MCP_READINESS_TIMEOUT", 0.1)
    configure_fake(
        fake_agent,
        reported_mode=AGENT_NAME,
        mcp_open_plans=[{"before": [{"server_initialized": "other_server"}]}],
    )
    client = KiroACP(
        *fake_agent, ignore_update, cancel_permission, agent_engine="v2", agent=AGENT_NAME
    )
    async with client:
        with pytest.raises(BridgeError, match="MCP readiness.*timed out"):
            await client.open_session(
                None,
                MCP_DEFINITIONS,
                required_mcp_tools=REQUIRED_MCP_TOOLS,
                agent_profile=worker_profile(),
            )


async def test_v3_ignores_the_v2_server_initialized_notification(
    fake_agent: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v3 keeps its verified-tools contract: the v2 notification is not readiness there."""
    from answerback_bridge import acp_client

    monkeypatch.setattr(acp_client, "MCP_READINESS_TIMEOUT", 0.1)
    configure_fake(
        fake_agent,
        mcp_open_plans=[{"before": [{"server_initialized": "answerback_bridge"}]}],
    )
    async with KiroACP(*fake_agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="MCP readiness.*timed out"):
            await client.open_session(None, MCP_DEFINITIONS, required_mcp_tools=REQUIRED_MCP_TOOLS)
        assert client.mcp_unverified_servers == frozenset()


async def test_v2_full_status_snapshot_still_verifies_tools(fake_agent: tuple[str, str]) -> None:
    server = {
        "name": "answerback_bridge",
        "status": "connected",
        "tools": [{"name": name, "disabled": False} for name in ("peer_submit", "task_wait")],
    }
    configure_fake(
        fake_agent, reported_mode=AGENT_NAME, mcp_open_plans=[{"before": [{"servers": [server]}]}]
    )
    client = KiroACP(
        *fake_agent, ignore_update, cancel_permission, agent_engine="v2", agent=AGENT_NAME
    )
    async with client:
        await client.open_session(
            None,
            MCP_DEFINITIONS,
            required_mcp_tools=REQUIRED_MCP_TOOLS,
            agent_profile=worker_profile(),
        )
        assert client.mcp_unverified_servers == frozenset()


async def test_v3_refusal_does_not_mention_the_v2_only_agent_flag(
    fake_agent: tuple[str, str],
) -> None:
    async with KiroACP(*fake_agent, ignore_update, cancel_permission) as client:
        with pytest.raises(BridgeError, match="required agent profile") as refusal:
            await client.open_session(agent_profile=worker_profile())
    assert "--agent" not in str(refusal.value)


class RecordingKiroACP(KiroACP):
    """Capture bounded incoming ACP frames and configure an isolated live launch."""

    def __init__(
        self,
        *args: Any,
        home: Path | None = None,
        kas_bundle: Path | None = None,
        agent_name: str | None = None,
        trusted_tools: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> None:
        # The probes name the agent and trusted tools here; the `Workers` route passes
        # the real client's `agent=` and `trust_tools=` keywords through `kwargs`.
        if agent_name is not None:
            kwargs["agent"] = agent_name
        if trusted_tools:
            kwargs["trust_tools"] = trusted_tools
        super().__init__(*args, **kwargs)
        self.frames: list[dict[str, Any]] = []
        self._home = home
        self._kas_bundle = kas_bundle

    def command(self) -> list[str]:
        command = super().command()
        if self._agent_engine == "v2":
            command.extend(("--model", LIVE_MODEL))
        return command

    def environment(self) -> dict[str, str]:
        environment = super().environment()
        if self._home is not None:
            environment["HOME"] = str(self._home)
            environment["KIRO_HOME"] = str(self._home / ".kiro")
            data_directory = self._home / "Library" / "Application Support" / "kiro-cli"
            environment["KIRO_DATA_DIR"] = str(data_directory)
            environment["KIRO_RUNTIME_DIR"] = str(data_directory / "run")
            environment["KIRO_TEST_DB_PATH"] = str(data_directory / "data.sqlite3")
            for name in XDG_HOME_VARIABLES:
                environment.pop(name, None)
        if self._kas_bundle is not None:
            environment["KAS_BUNDLE_PATH"] = str(self._kas_bundle)
        return environment

    def validate_frame(self, data: bytes) -> None:
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            return
        if isinstance(message, dict):
            self.frames.append(message)


def test_recording_client_unsets_real_xdg_homes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in XDG_HOME_VARIABLES:
        monkeypatch.setenv(name, f"/real/{name.lower()}")

    environment = RecordingKiroACP(
        "kiro-cli",
        "/tmp",
        ignore_update,
        cancel_permission,
        home=tmp_path / "home",
    ).environment()

    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["KIRO_HOME"] == str(tmp_path / "home" / ".kiro")
    assert environment["KIRO_DATA_DIR"] == str(
        tmp_path / "home" / "Library" / "Application Support" / "kiro-cli"
    )
    assert environment["KIRO_RUNTIME_DIR"] == str(
        tmp_path / "home" / "Library" / "Application Support" / "kiro-cli" / "run"
    )
    assert environment["KIRO_TEST_DB_PATH"] == str(
        tmp_path / "home" / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"
    )
    assert all(name not in environment for name in XDG_HOME_VARIABLES)


def isolated_kiro_home(
    tmp_path: Path,
    *,
    source_home: Path | None = None,
) -> tuple[Path, Path]:
    """Recreate schema, seed migration versions, and copy only real ``auth_kv`` rows."""
    source_home = source_home or Path.home()
    real_data = source_home / "Library" / "Application Support" / "kiro-cli"
    database = real_data / "data.sqlite3"
    source_kas_bundle = real_data / "run" / "chat-cli-3.5.0"
    if not database.is_file() or not source_kas_bundle.is_file():
        pytest.skip("Kiro CLI 3.5.0 live state is unavailable for an isolated HOME")

    home = tmp_path / "home"
    agent_directory = home / ".kiro" / "agents"
    data_directory = home / "Library" / "Application Support" / "kiro-cli"
    run_directory = data_directory / "run"
    agent_directory.mkdir(parents=True)
    run_directory.mkdir(parents=True)
    kas_bundle = run_directory / source_kas_bundle.name
    shutil.copy2(source_kas_bundle, kas_bundle)

    isolated_database = data_directory / database.name
    try:
        descriptor = os.open(
            isolated_database,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        os.close(descriptor)
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as source:
            schema = source.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND type IN ('table', 'index') "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
            ).fetchall()
            migration_version = source.execute("SELECT MAX(version) FROM migrations").fetchone()[0]
            auth_rows = source.execute("SELECT key, value FROM auth_kv").fetchall()
        with sqlite3.connect(isolated_database) as destination:
            for (statement,) in schema:
                destination.execute(statement)
            destination.executemany(
                "INSERT INTO migrations (id, version, migration_time) VALUES (?, ?, 0)",
                [(version + 1, version) for version in range(migration_version + 1)],
            )
            destination.executemany(
                "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
                auth_rows,
            )
        isolated_database.chmod(0o600)
    except BaseException:
        isolated_database.unlink(missing_ok=True)
        raise

    (agent_directory / f"{AGENT_NAME}.json").write_text(
        json.dumps(agent_document(worker_profile()), indent=2) + "\n"
    )

    scope_marker = home / SCOPE_MARKER_NAME
    scope_server = home / "scope_probe_mcp.py"
    scope_server.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "from mcp.server import MCPServer\n"
        "server = MCPServer('excluded_scope_probe')\n"
        "@server.tool()\n"
        "def forbidden_scope_probe() -> str:\n"
        "    return 'this global MCP server must be excluded'\n"
        "Path(sys.argv[1]).write_text('started\\n')\n"
        "server.run(transport='stdio')\n"
    )
    settings_directory = home / ".kiro" / "settings"
    settings_directory.mkdir()
    (settings_directory / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    SCOPE_SERVER_NAME: {
                        "command": sys.executable,
                        "args": [str(scope_server), str(scope_marker)],
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )
    return home, kas_bundle


def real_kiro_mtimes(home: Path | None = None) -> dict[Path, int]:
    """Snapshot mtimes of the named real Kiro state roots without following links."""
    home = home or Path.home()
    roots = [
        home / ".kiro",
        home / "Library" / "Application Support" / "kiro-cli",
        *(home / ".config").glob("kiro*"),
    ]
    mtimes: dict[Path, int] = {}
    for path in roots:
        try:
            mtimes[path] = path.lstat().st_mtime_ns
        except FileNotFoundError:
            continue
    return mtimes


def test_isolated_home_copies_only_authentication_rows(tmp_path: Path) -> None:
    source_home = tmp_path / "source"
    real_data = source_home / "Library" / "Application Support" / "kiro-cli"
    real_data.mkdir(parents=True)
    (real_data / "run").mkdir()
    (real_data / "run" / "chat-cli-3.5.0").write_text("fixture")
    source_database = real_data / "data.sqlite3"
    with sqlite3.connect(source_database) as database:
        database.execute(
            "CREATE TABLE migrations "
            "(id INTEGER PRIMARY KEY, version INTEGER, migration_time INTEGER)"
        )
        database.execute("INSERT INTO migrations VALUES (11, 10, 123456)")
        database.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO auth_kv VALUES ('token', 'fixture-secret')")
        database.execute("CREATE TABLE conversations_v2 (body TEXT)")
        database.execute("INSERT INTO conversations_v2 VALUES ('private-history')")

    home, kas_bundle = isolated_kiro_home(tmp_path / "isolated", source_home=source_home)
    copied_database = home / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"

    assert kas_bundle.parent == home / "Library" / "Application Support" / "kiro-cli" / "run"
    assert kas_bundle.read_text() == "fixture"
    assert copied_database.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(copied_database) as database:
        assert database.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("auth_kv",), ("conversations_v2",), ("migrations",)]
        assert database.execute("SELECT key, value FROM auth_kv").fetchall() == [
            ("token", "fixture-secret")
        ]
        assert database.execute("SELECT * FROM conversations_v2").fetchall() == []
        assert database.execute(
            "SELECT id, version, migration_time FROM migrations ORDER BY id"
        ).fetchall() == [(version + 1, version, 0) for version in range(11)]


@pytest.fixture
def isolated_kiro_installation(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Remove copied ``auth_kv`` authentication rows and assert real state is unchanged."""
    real_home = Path.home()
    before = real_kiro_mtimes(real_home)
    home = tmp_path / "home"
    try:
        yield isolated_kiro_home(tmp_path, source_home=real_home)
    finally:
        after = real_kiro_mtimes(real_home)
        shutil.rmtree(home, ignore_errors=True)
        assert after == before


def selected_mode(frames: list[dict[str, Any]]) -> str | None:
    """Return the latest session/new mode from captured response frames."""
    for frame in reversed(frames):
        result = frame.get("result")
        if not isinstance(result, dict) or "sessionId" not in result:
            continue
        modes = result.get("modes")
        if isinstance(modes, dict):
            mode = modes.get("currentModeId")
            return mode if isinstance(mode, str) else None
    return None


@pytest.mark.skipif(KIRO_CLI is None, reason="kiro-cli is not installed")
@pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="spends model quota and trusts all Kiro tools; set ANSWERBACK_LIVE_KIRO_V2=1",
)
async def test_live_v2_agent_selection_and_trust_scopes(
    tmp_path: Path,
    isolated_kiro_installation: tuple[Path, Path],
) -> None:
    """Exercise the bridge client against Kiro; never run in ordinary CI."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    home, kas_bundle = isolated_kiro_installation
    scope_marker = home / SCOPE_MARKER_NAME
    note = scratch / "note.txt"
    output = scratch / "out.txt"
    first_word = "amber-t191-first"
    second_word = "cobalt-t191-second"
    written_text = "written-by-kiro-v2"
    note.write_text(first_word + "\n")
    output.unlink(missing_ok=True)

    v2_permissions: list[dict[str, Any]] = []
    v2_updates: list[dict[str, Any]] = []
    update_received = asyncio.Event()

    async def record_update(update: dict[str, Any]) -> None:
        v2_updates.append(update)
        update_received.set()

    async def unexpected_permission(request: dict[str, Any]) -> None:
        v2_permissions.append(request)
        return None

    profile_probe = RecordingKiroACP(
        KIRO_CLI or "kiro-cli",
        str(scratch),
        record_update,
        unexpected_permission,
        home=home,
        kas_bundle=kas_bundle,
        agent_engine="v2",
        trust_all_tools=True,
    )
    async with profile_probe:
        with pytest.raises(BridgeError, match="did not select required agent profile"):
            await profile_probe.open_session(agent_profile=worker_profile())
    (scratch / "acp-v2-profile-frames.json").write_text(
        json.dumps(profile_probe.frames, indent=2) + "\n"
    )
    assert scope_marker.is_file()
    scope_marker.unlink()

    v2 = RecordingKiroACP(
        KIRO_CLI or "kiro-cli",
        str(scratch),
        record_update,
        unexpected_permission,
        home=home,
        kas_bundle=kas_bundle,
        agent_engine="v2",
        trust_all_tools=True,
    )
    async with v2:
        first_session = await v2.open_session()
        first = await v2.prompt(first_session, "Read note.txt and reply with its word only.")
        assert first.stop_reason == "end_turn"
        assert first_word in first.text.lower()

        note.write_text(second_word + "\n")
        second_session = await v2.open_session()
        assert second_session != first_session
        second = await v2.prompt(second_session, "Read note.txt and reply with its word only.")
        assert second.stop_reason == "end_turn"
        assert second_word in second.text.lower()

        write_session = await v2.open_session()
        written = await v2.prompt(
            write_session,
            f"Write exactly {written_text!r} followed by a newline to out.txt, then reply done.",
        )
        assert written.stop_reason == "end_turn"
        assert output.read_text() == written_text + "\n"

        shell_session = await v2.open_session()
        shelled = await v2.prompt(
            shell_session,
            "Use the shell pwd command and reply with the exact absolute path only.",
        )
        assert shelled.stop_reason == "end_turn"
        assert str(scratch) in shelled.text

        cancel_session = await v2.open_session()
        update_received.clear()
        pending = asyncio.create_task(
            v2.prompt(
                cancel_session,
                "Use the shell command `sleep 30`, wait for it to finish, then reply done.",
            )
        )
        await asyncio.wait_for(update_received.wait(), 20)
        assert not pending.done()
        await v2.cancel(cancel_session)
        cancelled = await asyncio.wait_for(pending, 10)
        assert cancelled.stop_reason == "cancelled"

    (scratch / "acp-v2-frames.json").write_text(json.dumps(v2.frames, indent=2) + "\n")
    (scratch / "acp-v2-updates.json").write_text(json.dumps(v2_updates, indent=2) + "\n")
    assert v2_permissions == []
    assert scope_marker.is_file()
    scope_marker.unlink()

    agent_all_permissions: list[dict[str, Any]] = []

    async def unexpected_agent_all_permission(request: dict[str, Any]) -> None:
        agent_all_permissions.append(request)
        return None

    agent_all = RecordingKiroACP(
        KIRO_CLI or "kiro-cli",
        str(scratch),
        record_update,
        unexpected_agent_all_permission,
        home=home,
        kas_bundle=kas_bundle,
        agent_name=AGENT_NAME,
        agent_engine="v2",
        trust_all_tools=True,
    )
    async with agent_all:
        agent_all_session = await agent_all.open_session(agent_profile=worker_profile())
        assert selected_mode(agent_all.frames) == AGENT_NAME
        assert not scope_marker.exists()
        agent_all_read = await agent_all.prompt(
            agent_all_session,
            "Read note.txt and reply with its word only.",
        )
        assert agent_all_read.stop_reason == "end_turn"
        assert second_word in agent_all_read.text.lower()

        agent_all_output = scratch / "agent-all.txt"
        agent_all_write = await agent_all.prompt(
            agent_all_session,
            (
                f"Write exactly {written_text!r} followed by a newline to "
                "agent-all.txt, then reply done."
            ),
        )
        assert agent_all_write.stop_reason == "end_turn"
        assert agent_all_output.read_text() == written_text + "\n"

        agent_all_shell = await agent_all.prompt(
            agent_all_session,
            "Use the shell pwd command and reply with the exact absolute path only.",
        )
        assert agent_all_shell.stop_reason == "end_turn"
        assert str(scratch) in agent_all_shell.text
    assert agent_all_permissions == []
    assert not scope_marker.exists()
    (scratch / "acp-v2-agent-trust-all-frames.json").write_text(
        json.dumps(agent_all.frames, indent=2) + "\n"
    )

    agent_read_permissions: list[dict[str, Any]] = []

    async def unexpected_agent_read_permission(request: dict[str, Any]) -> None:
        agent_read_permissions.append(request)
        return None

    agent_read = RecordingKiroACP(
        KIRO_CLI or "kiro-cli",
        str(scratch),
        record_update,
        unexpected_agent_read_permission,
        home=home,
        kas_bundle=kas_bundle,
        agent_name=AGENT_NAME,
        trusted_tools=("read",),
        agent_engine="v2",
    )
    async with agent_read:
        agent_read_session = await agent_read.open_session(agent_profile=worker_profile())
        assert selected_mode(agent_read.frames) == AGENT_NAME
        trusted_read = await agent_read.prompt(
            agent_read_session,
            "Read note.txt and reply with its word only.",
        )
        assert trusted_read.stop_reason == "end_turn"
        assert second_word in trusted_read.text.lower()
    assert agent_read_permissions == []
    (scratch / "acp-v2-agent-trust-read-frames.json").write_text(
        json.dumps(agent_read.frames, indent=2) + "\n"
    )

    async def assert_permission_requested(
        prompt: str,
        evidence_name: str,
        expected_title: str,
    ) -> None:
        permission_received = asyncio.Event()
        permissions: list[dict[str, Any]] = []

        async def hold_narrow_permission(request: dict[str, Any]) -> None:
            permissions.append(request)
            permission_received.set()
            await asyncio.Future()

        narrowed = RecordingKiroACP(
            KIRO_CLI or "kiro-cli",
            str(scratch),
            record_update,
            hold_narrow_permission,
            home=home,
            kas_bundle=kas_bundle,
            agent_name=AGENT_NAME,
            trusted_tools=("read",),
            agent_engine="v2",
        )
        async with narrowed:
            session = await narrowed.open_session(agent_profile=worker_profile())
            assert selected_mode(narrowed.frames) == AGENT_NAME
            pending = asyncio.create_task(narrowed.prompt(session, prompt))
            await asyncio.wait_for(permission_received.wait(), 25)
            assert not pending.done()
            await narrowed.cancel(session)
            assert (await asyncio.wait_for(pending, 10)).stop_reason == "cancelled"
        assert permissions
        assert expected_title in permissions[0]["tool_call"]["title"].lower()
        (scratch / evidence_name).write_text(json.dumps(narrowed.frames, indent=2) + "\n")

    await assert_permission_requested(
        "Use the write tool to write 'must-not-be-written' to narrowed-write.txt.",
        "acp-v2-agent-trust-read-write-frames.json",
        "creating",
    )
    assert not (scratch / "narrowed-write.txt").exists()
    await assert_permission_requested(
        "Use the shell command `pwd`, then reply with its output.",
        "acp-v2-agent-trust-read-shell-frames.json",
        "running",
    )


@pytest.mark.skipif(KIRO_CLI is None, reason="kiro-cli is not installed")
@pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="spends model quota and trusts Kiro's read tool; set ANSWERBACK_LIVE_KIRO_V2=1",
)
async def test_live_v2_workers_route_completes_a_read_with_trust_tools_read(
    tmp_path: Path,
    isolated_kiro_installation: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One assignment through the real managed route (`Workers.submit` to the receipt):
    v2 with `kiro_trust_tools=["read"]` writes the agent file, launches with `--agent`
    and `--trust-tools read`, and a read task completes with no permission callback.
    Isolation is the probe's: temp HOME, auth_kv-only DB copy, XDG unset, real roots
    compared by mtime at teardown."""
    from answerback_bridge.config import Config
    from answerback_bridge.kiro_agent import agent_path
    from answerback_bridge.tasks import TaskStore
    from answerback_bridge.workers import Workers

    home, kas_bundle = isolated_kiro_installation
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    word = "teal-t205-route"
    (scratch / "note.txt").write_text(word + "\n")
    # The fixture pre-installs the agent for the client probes; the route must write it.
    agent_path(home).unlink()
    # `Workers` installs the agent under, and `KiroACP.environment` forwards, this HOME.
    monkeypatch.setenv("HOME", str(home))

    launched: list[RecordingKiroACP] = []

    class IsolatedKiroACP(RecordingKiroACP):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, home=home, kas_bundle=kas_bundle, **kwargs)
            launched.append(self)

    monkeypatch.setattr("answerback_bridge.workers.KiroACP", IsolatedKiroACP)
    config = Config(
        kiro_binary=KIRO_CLI or "kiro-cli",
        kiro_workspaces={"probe": str(scratch)},
        kiro_acp_engine="v2",
        kiro_trust_tools=["read"],
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)
    store = TaskStore(tmp_path / "tasks.sqlite")
    workers = Workers(config, config_path, store)
    try:
        receipt = await workers.submit(
            "probe",
            "Read note.txt and reply with its word only.",
            "t205-live-route",
            expected_cwd=str(scratch),
        )
        settled = {"completed", "blocked", "failed", "uncertain", "cancelled"}
        for _ in range(900):  # 180 s; a permission request settles as `blocked`
            finished = store.get(receipt["id"])
            if finished["status"] in settled:
                break
            await asyncio.sleep(0.2)
        permissions = workers.permissions(receipt["id"])["permissions"]
    finally:
        await workers.close()

    (scratch / "acp-v2-workers-route-frames.json").write_text(
        json.dumps([client.frames for client in launched], indent=2) + "\n"
    )
    assert permissions == []
    assert finished["status"] == "completed", finished.get("detail")
    assert word in finished["result"].lower()
    assert finished["metadata"]["kiro_trust"] == ["read"]
    assert finished["metadata"]["worker_profile"] == AGENT_NAME
    assert len(launched) == 1
    assert launched[0].command()[1:] == [
        "acp",
        "--agent-engine",
        "v2",
        "--agent",
        AGENT_NAME,
        "--trust-tools",
        "read",
        "--model",
        LIVE_MODEL,
    ]
    assert selected_mode(launched[0].frames) == AGENT_NAME
    installed = agent_path(home)
    assert installed.stat().st_mode & 0o777 == 0o600
    assert json.loads(installed.read_text()) == agent_document(worker_profile())
