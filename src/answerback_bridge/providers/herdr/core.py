"""Bounded access to Herdr Core's exact terminal addresses.

Core has no expected-generation send token. Identity checks before and after a
send narrow the race, but cannot make delivery atomic with terminal identity.

A terminal that reports `idle` is sent to with `hc send --if idle`, so Core itself
refuses the text if the agent has started working since the bridge looked. Core
accepts only `idle` or `working` for `--if`, so a send to a terminal that reports
`done` cannot carry a status condition. Such a terminal is therefore not sent to
unless `send_to_done` is on (the bridge's `hc_send_to_done`, off by default). With
it on, the terminal is sent to without a status condition: the window between the
bridge's check and Core typing the text is not closed by Core for `done` terminals.
The receipt's status word must then show `done` or `idle`, otherwise delivery is
reported as uncertain; that check comes after the typing and cannot undo it.

With `events` on, listing peers reads the `hc events --json` follower's view
(`core_events.py`) instead of `hc ls` plus one `hc get` per terminal, and falls
back to that polling path whenever the view cannot answer exactly. The view is
never a send token: `peer()` and `send()` always call `hc get` themselves.
"""

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ...addresses import _address, sendable_address, validate_machine_names
from ...addresses import address_machine as address_machine
from ...addresses import identity_observer as identity_observer
from ...plugin import loaded_agent_programs
from ...process import BridgeError, CommandError, CommandTimeout, Output, run
from .core_events import EventFollower, TerminalRecord

INVENTORY_SECONDS = 20
COMMAND_SECONDS = 10
SEND_SECONDS = 90
MAX_TERMINALS = 100
GET_CONCURRENCY = 4
# Core sends a heartbeat every 15 s, so a view older than this has lost its stream.
EVENTS_MAX_AGE = 45.0
# A terminal on another machine reports through a file in this directory, relative
# to that machine's home (Herdr core starts a relative `--on` path there and does
# not expand a tilde). The name is `<task UUID>-<32 hex token>.txt`.
RESULT_DIRECTORY = ".answerback/results"
_RESULT_PATH = re.compile(
    r"\.answerback/results/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"-[0-9a-f]{32}\.txt\Z"
)
# The result bound `TaskStore.complete_worker` enforces (tasks.py).
RESULT_MAX_CHARS = 200_000
_INTERPRETERS = {"node", "nodejs", "bun", "deno", "python", "python3"}
_WRAPPERS = _INTERPRETERS | {"uv", "aim"}
_STATES = {"idle", "done", "working", "blocked", "error"}
_DONE_RECEIPT_STATES = {"idle", "done"}


class CoreIdentityChanged(BridgeError):
    """A terminal no longer matches the identity selected by the caller."""


class CoreTypedWhileWorking(BridgeError):
    """A send to a `done` terminal landed in a turn the agent had begun on its own.

    The receipt's status was neither `done` nor `idle`, and the identity check
    after the send found the same terminal, agent and binding as before it. The
    text is in the right terminal; whether the agent acts on it is unknown until
    the worker reports with the task ID the text carries. `receipt` is the send
    receipt as `Core.send` would have returned it; `turn_before` and `turn_after`
    are the terminal's turn counter at the two identity checks (None when Core
    did not report one).
    """

    def __init__(
        self,
        message: str,
        *,
        receipt: dict[str, Any],
        turn_before: int | None,
        turn_after: int | None,
    ) -> None:
        super().__init__(message)
        self.receipt = receipt
        self.turn_before = turn_before
        self.turn_after = turn_after


class CoreNotReady(BridgeError):
    """The terminal is not an eligible agent for this operation."""


class CoreNotSent(BridgeError):
    """A failure before `hc send` ran: nothing was typed into any terminal.

    Its text is one of the fixed `NOT_SENT_*` sentences, so a receipt settled with
    it names the cause without carrying Core's own output.
    """


NOT_SENT_UNREAD = (
    "Core delivery not attempted: the terminal could not be read before the send; nothing was sent"
)
NOT_SENT_MACHINE = "Core delivery not attempted: the machine is not connected; nothing was sent"
NOT_SENT_MACHINES_UNREAD = (
    "Core delivery not attempted: the machine list could not be read; nothing was sent"
)
NOT_SENT_MACHINE_TWICE = (
    "Core delivery not attempted: the machine list names the machine more than once; "
    "nothing was sent"
)
NOT_SENT_MACHINE_BAD_ID = (
    "Core delivery not attempted: the machine list gives the machine an id that is not "
    "32 lowercase hex digits; nothing was sent"
)
NOT_SENT_MACHINE_CHANGED = (
    "Core delivery not attempted: the machine's core id is not the one pinned at its "
    "first send (machine_id_changed); nothing was sent"
)
_MACHINE_ID = re.compile(r"[0-9a-f]{32}")


class _NotAgent(CoreNotReady):
    """A well-formed terminal describes a shell or another unsupported process."""


@dataclass(frozen=True)
class ResultText:
    """A result file as Core returned it; `utf8` is False when its bytes were not UTF-8."""

    text: str
    utf8: bool


class CoreMissing(BridgeError):
    """Core answered exit 3 with error kind `not_found`: no such file or terminal."""


def result_path(task_id: str, token: str) -> str:
    """The home-relative result file a remote receipt names, or raise ValueError."""
    path = f"{RESULT_DIRECTORY}/{task_id}-{token}.txt"
    if not _RESULT_PATH.fullmatch(path):
        raise ValueError("Core result path is not a bridge result file")
    return path


def _not_found(error: CommandError) -> bool:
    """Whether a failed `hc` call is Core's `not_found` answer (exit 3, JSON on stderr)."""
    if error.returncode != 3:
        return False
    detail = str(error).partition("): ")[2]
    try:
        value = json.loads(detail, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError):
        return False
    failure = value.get("error") if isinstance(value, dict) else None
    return isinstance(failure, dict) and failure.get("kind") == "not_found"


def _status_mismatch(error: CommandError) -> bool:
    """Whether `hc send --if` refused because the terminal status changed."""
    if error.returncode != 3:
        return False
    detail = str(error).partition("): ")[2]
    try:
        value = json.loads(detail, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError):
        return False
    failure = value.get("error") if isinstance(value, dict) else None
    return isinstance(failure, dict) and failure.get("kind") == "status_mismatch"


def _string(value: Any, *, limit: int = 4096) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(arg, str) for arg in value):
        raise BridgeError("Core returned incomplete process identity")
    return value


def _program(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).name.removesuffix(".exe")


def _interpreter(program: str) -> bool:
    return program in _INTERPRETERS or re.fullmatch(r"python3\.[0-9]{1,2}", program) is not None


def _foreground_agent(argv: list[str], agents: Mapping[str, str]) -> str | None:
    """Identify only known execution positions, never incidental argument text."""
    program = _program(argv[0])
    direct = agents.get(program)
    if direct is not None:
        return direct
    if program == "aim":
        if not 4 <= len(argv) <= 64 or argv[1:3] != ["sandbox", "--client"]:
            return None
        if any(len(arg) > 4096 for arg in argv):
            return None
        if any(arg == "--client" or arg.startswith("--client=") for arg in argv[4:]):
            return None
        return agents.get(argv[3])
    if not _interpreter(program) or len(argv) < 2:
        return None
    script = argv[1]
    if not _string(script) or script.startswith("-"):
        return None
    agent = agents.get(_program(script))
    if agent is not None:
        return agent
    if program in {"node", "nodejs", "bun"} and PurePosixPath(script).parts[-4:] == (
        "node_modules",
        "@anthropic-ai",
        "claude-code",
        "cli.js",
    ):
        return "claude"
    return None


def validate_message(text: str) -> None:
    """Validate the complete outbound text before reserving or starting delivery.

    The largest accepted text is 131,071 bytes of UTF-8, one byte under 128 KiB.
    `hc send` gets the text as one argument, and Linux with 4 KiB pages refuses
    one argument of 131,072 bytes: its limit of 32 pages counts the final NUL.
    """
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise ValueError("Core message must be nonempty text without NUL characters")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Core message must be valid UTF-8 text") from None
    if len(encoded) >= 128 * 1024:
        raise ValueError("Core message exceeds the 128 KiB bound")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _event_row(record: TerminalRecord) -> dict[str, Any] | None:
    """Rebuild the `hc get --json` fields `Core._peer` reads, or None to use `get`.

    A record cannot tell an absent field from its empty default, so any field the
    readiness decision reads that is empty or missing sends the caller to `get`,
    as does any shortened (`truncated`) record or one an unmodelled event type
    touched (`unverified`).
    """
    foreground = record.foreground
    status = record.status
    if (
        record.truncated
        or record.unverified
        or record.pid is None
        or record.created_ms is None
        or not record.argv
        or (foreground is not None and (not foreground.argv or not foreground.cwd))
        or (status is not None and status.app is None)
    ):
        return None
    return {
        "address": record.address,
        "state": record.state,
        "argv": list(record.argv),
        "pid": record.pid,
        "created_ms": record.created_ms,
        "turn": record.turn,
        "labels": dict(record.labels),
        "foreground": None
        if foreground is None
        else {
            "pid": foreground.pid,
            "name": foreground.name,
            "argv": list(foreground.argv),
            "cwd": foreground.cwd,
        },
        "status": None if status is None else {"app": status.app, "state": status.state},
    }


def _machine_errors(machines: Any, errors: dict[str, str]) -> None:
    """Record each unavailable or malformed `hc ls` / `hc machine ls` machine entry."""
    if not isinstance(machines, list):
        errors["machines"] = "Machine inventory unavailable or unsupported"
        return
    for index, machine in enumerate(machines):
        if not isinstance(machine, dict) or not _string(machine.get("name")):
            errors[f"machine:{index}"] = "Unsupported machine inventory entry"
        elif machine.get("state") != "connected":
            errors[f"machine:{machine['name']}"] = "Machine is unavailable"


class Core:
    def __init__(
        self,
        binary: str = "hc",
        session: str | None = None,
        *,
        send_to_done: bool = False,
        send_machines: list[str] | tuple[str, ...] = (),
        events: bool = False,
        agents: Mapping[str, str] | None = None,
    ) -> None:
        """Bind one `hc` executable and optional session.

        `send_to_done` is off unless it is passed: a terminal that reports `done`
        is then not `send_ready` (reason `done`) and `send` refuses it before
        anything is typed. Passed as `True`, such a terminal receives work like an
        `idle` one; see the module docstring for the unguarded window that opens.
        The bridge passes its `hc_send_to_done` here, which is off by default too.
        `send_machines` names the remote machines whose terminals may receive work;
        every other `machine/tNN` address stays read-only.
        `events` lets listing peers read an owned `hc events` follower; it only
        runs after `start_events()` and stops for good at `close()`.
        `agents` is the kernel-composed executable-to-agent-kind table. Direct
        construction reads the installed provider catalog.
        """
        if not _string(binary):
            raise ValueError("Core executable is required")
        if session is not None and not _string(session, limit=128):
            raise ValueError("Core session must be a nonempty name")
        if type(send_to_done) is not bool:
            raise ValueError("Core send_to_done must be a boolean")
        if type(events) is not bool:
            raise ValueError("Core events must be a boolean")
        self.binary = binary
        self.session = session
        self.send_to_done = send_to_done
        self.send_machines = frozenset(validate_machine_names(send_machines))
        self.agents = dict(loaded_agent_programs() if agents is None else agents)
        self._ready_states = frozenset({"idle", "done"} if send_to_done else {"idle"})
        self.follower = EventFollower(binary, session=session) if events else None
        self._starting: asyncio.Task[None] | None = None
        self._closed = False

    def start_events(self) -> None:
        """Start the owned follower on the running loop; a no-op when off, closed or loopless."""
        if self.follower is None or self._closed or self._starting is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._starting = loop.create_task(self.follower.start())

    async def close(self) -> None:
        """Stop the follower for good, killing and reaping its child (idempotent)."""
        self._closed = True
        starting, self._starting = self._starting, None
        if starting is not None:
            # Cancelled before it ran, follower.start() never spawns anything.
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        if self.follower is not None:
            await self.follower.stop()

    def events_state(self) -> str | None:
        """`running` or `stale` for an owned follower; None when the switch is off."""
        if self.follower is None:
            return None
        return "stale" if self.follower.snapshot(EVENTS_MAX_AGE) is None else "running"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("HC_TERMINAL_ID", "HC_SOCKET", "HC_SESSION"):
            env.pop(key, None)
        if self.session is not None:
            env["HC_SESSION"] = self.session
        return env

    async def _call(
        self,
        *args: str,
        seconds: float = COMMAND_SECONDS,
        allow_error: bool = False,
        missing: bool = False,
        status_mismatch: bool = False,
    ) -> dict[str, Any]:
        """Run one `hc` command and parse its JSON object.

        With `missing`, Core's `not_found` answer becomes CoreMissing. With
        `status_mismatch`, that exact Core refusal becomes CoreNotReady. Every
        other failed command remains a plain BridgeError.
        """
        response = await self._run(
            *args, seconds=seconds, missing=missing, status_mismatch=status_mismatch
        )
        return self._object(response.stdout, allow_error=allow_error)

    async def _run(
        self,
        *args: str,
        seconds: float = COMMAND_SECONDS,
        missing: bool = False,
        status_mismatch: bool = False,
        keep_stdout_bytes: bool = False,
    ) -> Output:
        """Run one `hc` command; every failure becomes a fixed-text BridgeError."""
        options: dict[str, Any] = {"keep_stdout_bytes": True} if keep_stdout_bytes else {}
        try:
            return await run([self.binary, *args], max_seconds=seconds, env=self._env(), **options)
        except CommandTimeout:
            if not args or args[0] != "send":
                raise BridgeError("Core read timed out; retry the read") from None
            raise BridgeError(
                "Core command timed out; delivery may be uncertain. Do not resubmit blindly."
            ) from None
        except CommandError as error:
            message = f"Core command failed (exit {error.returncode})"
            if missing and _not_found(error):
                raise CoreMissing(message) from None
            if status_mismatch and _status_mismatch(error):
                raise CoreNotReady(
                    "Core refused: status changed before typing; nothing was sent"
                ) from None
            raise BridgeError(message) from None
        except (OSError, BridgeError):
            raise BridgeError("Core command unavailable or output exceeded its bound") from None

    @staticmethod
    def _object(stdout: str, *, allow_error: bool = False) -> dict[str, Any]:
        try:
            value = json.loads(stdout, object_pairs_hook=_object, parse_constant=_constant)
        except (ValueError, RecursionError):
            raise BridgeError("Core returned an invalid JSON response") from None
        if not isinstance(value, dict) or ("error" in value and not allow_error):
            raise BridgeError("Core returned an unsupported or rejected response")
        return value

    def _sendable_address(self, target: str) -> bool:
        """A local address, or a remote one whose machine is exactly allow-listed."""
        return sendable_address(target, self.send_machines)

    def _peer(self, row: dict[str, Any], target: str) -> dict[str, Any]:
        try:
            observed = _address(row.get("address"))
        except ValueError:
            raise BridgeError("Core returned an invalid terminal address") from None
        if observed != target:
            raise CoreIdentityChanged("Core returned a different terminal address")
        foreground = row.get("foreground")
        status = row.get("status")
        if status is not None and (
            not isinstance(status, dict)
            or not _string(status.get("app"), limit=128)
            or not _string(status.get("state"), limit=128)
        ):
            raise BridgeError("Core returned unsupported agent status")
        argv: list[str] | None
        foreground_pid: int | None
        if foreground is None:
            agent = self.agents.get(status["app"]) if status is not None else None
            if agent is None:
                raise BridgeError("Core returned no foreground identity or recognized agent report")
            argv = None
            foreground_pid = None
            current_cwd = None
            agent_identity_source = "reported_status"
        else:
            if not isinstance(foreground, dict):
                raise BridgeError("Core returned incomplete foreground identity")
            argv = _argv(foreground.get("argv"))
            program = _program(argv[0])
            agent = _foreground_agent(argv, self.agents)
            agent_identity_source = "foreground_argv"
            if (
                agent is None
                and (program in _WRAPPERS or _interpreter(program))
                and status is not None
            ):
                agent = self.agents.get(status["app"])
                agent_identity_source = "reported_status"
            if agent is None:
                raise _NotAgent("Core terminal does not contain a recognized foreground agent")
            foreground_pid = foreground.get("pid")
            if type(foreground_pid) is not int or foreground_pid <= 0:
                raise BridgeError("Core returned incomplete foreground identity")
            current_cwd = foreground.get("cwd")
            if current_cwd is not None and not _string(current_cwd):
                raise BridgeError("Core returned an invalid current directory")
        # Some reported-only terminals expose no launch argv. Retain that gap
        # in their binding; missing foreground details always prohibit sending.
        root_argv = None if foreground is None and "argv" not in row else _argv(row.get("argv"))
        if (
            type(row.get("pid")) is not int
            or row["pid"] <= 0
            or type(row.get("created_ms")) is not int
            or row["created_ms"] < 0
            or not _string(row.get("state"), limit=128)
        ):
            raise BridgeError("Core returned incomplete process identity")
        labels = row.get("labels", {})
        if not isinstance(labels, dict):
            raise BridgeError("Core returned invalid placement labels")
        placement = {
            key: labels[key]
            for key in ("herdr.space", "herdr.tab", "where")
            if _string(labels.get(key))
        }
        status_matches = status is not None and self.agents.get(status["app"]) == agent
        state = status["state"] if status_matches and status["state"] in _STATES else "unknown"
        if foreground is None:
            readiness_reason = "foreground_unknown"
        elif agent_identity_source != "foreground_argv":
            readiness_reason = "foreground_agent_unknown"
        elif not self._sendable_address(target):
            readiness_reason = "remote_read_only"
        elif row["state"] != "live":
            readiness_reason = "not_live"
        elif current_cwd is None:
            readiness_reason = "current_directory_unknown"
        elif status is None:
            readiness_reason = "unreported"
        elif not status_matches:
            readiness_reason = "status_mismatch"
        elif state not in self._ready_states:
            readiness_reason = state
        else:
            readiness_reason = "ready"
        ready = readiness_reason == "ready"
        # A terminal on another machine reports through a result file that this
        # bridge reads (`read_result`), a local one through `task_complete`. A
        # remote worker cannot reach this bridge's socket, so it never advertises
        # `worker_report`.
        machine = address_machine(target)
        report = "worker_report" if machine is None else "result_file"
        capabilities = ["read", "send", report] if ready else ["read"]
        identity = {
            "address": target,
            "argv": root_argv,
            "foreground_argv": argv,
            "pid": row["pid"],
            "foreground_pid": foreground_pid,
            "created_ms": row["created_ms"],
            "cwd": current_cwd,
            "agent": agent,
            "agent_identity_source": agent_identity_source,
            "session": self.session,
        }
        binding = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        # The turn counter is a reading, not identity: it is kept so that a send
        # can say which turn its text landed in, and left out of the binding.
        turn = row.get("turn")
        if type(turn) is not int or turn < 0:
            turn = None
        return {
            "peer_id": f"hc:{target}",
            "provider": "hc",
            "target": target,
            "name": placement.get("where") or placement.get("herdr.tab") or target,
            "project": current_cwd or "",
            "project_known": current_cwd is not None,
            "agent_kind": agent,
            "agent_identity_source": agent_identity_source,
            "foreground_known": foreground is not None,
            "kind": "interactive",
            "state": state,
            "terminal_state": row["state"],
            "turn": turn,
            "placement": placement,
            "capabilities": capabilities,
            "send_ready": ready,
            "send_readiness_reason": readiness_reason,
            "binding": binding,
            "session": self.session,
            # The `hc machine ls` name of a remote terminal; None for a local one.
            "machine": machine,
            # The bridge has no verified way to stop work typed into an hc terminal,
            # local or remote, so no hc peer offers cancellation.
            "cancel": False,
        }

    async def peer(self, target: str) -> dict[str, Any]:
        target = _address(target)
        return self._peer(await self._call("get", target, "--json"), target)

    async def trace_pid(self, target: str, binding: str | None) -> int | None:
        """Private observer input. A remote/stale/unknown foreground has no local PID."""
        target = _address(target)
        if "/" in target:
            return None
        row = await self._call("get", target, "--json")
        peer = self._peer(row, target)
        if (
            peer["binding"] != binding
            or peer["terminal_state"] != "live"
            or peer["agent_identity_source"] != "foreground_argv"
            or not peer["foreground_known"]
        ):
            return None
        return row["foreground"]["pid"]

    async def inventory(self) -> dict[str, Any]:
        """List every live terminal's peer, from the event view when it can answer."""
        records = None
        if self.follower is not None and not self._closed:
            records = self.follower.snapshot(EVENTS_MAX_AGE)
        if records is not None:
            return await self._inventory_from_events(records)
        peers: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        excluded = 0
        excluded_finished: int | None = None
        tasks: list[asyncio.Task[None]] = []

        async def collect(target: str, semaphore: asyncio.Semaphore) -> None:
            nonlocal excluded
            async with semaphore:
                try:
                    peers.append(await self.peer(target))
                except _NotAgent:
                    excluded += 1
                except (BridgeError, ValueError):
                    errors[target] = "Terminal details unavailable or unsupported"

        try:
            async with asyncio.timeout(INVENTORY_SECONDS):
                inventory = await self._call("ls", "--json")
                terminals = inventory.get("terminals")
                if not isinstance(terminals, list):
                    raise BridgeError("Core returned an unsupported terminal inventory")
                _machine_errors(inventory.get("machines"), errors)
                hidden = inventory.get("hidden_finished")
                if type(hidden) is int and hidden >= 0:
                    excluded_finished = hidden
                if len(terminals) > MAX_TERMINALS:
                    errors["terminals"] = (
                        f"Inventory limited to the first {MAX_TERMINALS} terminals"
                    )
                seen: set[str] = set()
                semaphore = asyncio.Semaphore(GET_CONCURRENCY)
                for index, row in enumerate(terminals[:MAX_TERMINALS]):
                    try:
                        target = _address(row.get("address") if isinstance(row, dict) else None)
                    except ValueError:
                        errors[f"terminal:{index}"] = "Unsupported terminal inventory entry"
                        continue
                    if target in seen:
                        errors[f"terminal:{index}"] = "Duplicate terminal address"
                        continue
                    seen.add(target)
                    tasks.append(asyncio.create_task(collect(target, semaphore)))
                await asyncio.gather(*tasks)
        except TimeoutError:
            errors["inventory"] = "Core inventory exceeded its 20 second deadline"
        except BridgeError as error:
            errors["inventory"] = str(error)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return {
            "peers": sorted(peers, key=lambda peer: peer["target"]),
            "complete": not errors,
            "errors": errors,
            "excluded_non_agents": excluded,
            "excluded_finished": excluded_finished,
            "inventory_scope": "live",
        }

    async def _inventory_from_events(self, records: Mapping[str, TerminalRecord]) -> dict[str, Any]:
        """The polling inventory's result, built from the follower's view.

        Machine availability is not part of the view, so it comes from one
        `hc machine ls --json`. A record the view cannot answer exactly is read
        with `hc get` instead, through the same `peer()` the polling path uses.
        """
        peers: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        excluded = 0
        live = [record for record in records.values() if record.state == "live"]
        excluded_finished = len(records) - len(live)
        if len(live) > MAX_TERMINALS:
            errors["terminals"] = f"Inventory limited to the first {MAX_TERMINALS} terminals"
        exact: list[str] = []
        for record in live[:MAX_TERMINALS]:
            row = _event_row(record)
            if row is None:
                exact.append(record.address)
                continue
            try:
                peers.append(self._peer(row, record.address))
            except _NotAgent:
                excluded += 1
            except (BridgeError, ValueError):
                errors[record.address] = "Terminal details unavailable or unsupported"
        tasks: list[asyncio.Task[None]] = []

        async def collect(target: str, semaphore: asyncio.Semaphore) -> None:
            nonlocal excluded
            async with semaphore:
                try:
                    peers.append(await self.peer(target))
                except _NotAgent:
                    excluded += 1
                except (BridgeError, ValueError):
                    errors[target] = "Terminal details unavailable or unsupported"

        try:
            async with asyncio.timeout(INVENTORY_SECONDS):
                try:
                    machines = (await self._call("machine", "ls", "--json")).get("machines")
                except BridgeError:
                    machines = None
                _machine_errors(machines, errors)
                semaphore = asyncio.Semaphore(GET_CONCURRENCY)
                tasks = [asyncio.create_task(collect(target, semaphore)) for target in exact]
                await asyncio.gather(*tasks)
        except TimeoutError:
            errors["inventory"] = "Core inventory exceeded its 20 second deadline"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return {
            "peers": sorted(peers, key=lambda peer: peer["target"]),
            "complete": not errors,
            "errors": errors,
            "excluded_non_agents": excluded,
            "excluded_finished": excluded_finished,
            "inventory_scope": "live",
        }

    async def read(self, target: str, lines: int = 100) -> dict[str, Any]:
        target = _address(target)
        if type(lines) is not int or not 1 <= lines <= 500:
            raise ValueError("lines must be an integer between 1 and 500")
        result = await self._call("read", "--lines", str(lines), "--json", target)
        if (
            type(result.get("complete")) is not bool
            or not _string(result.get("how"), limit=128)
            or not isinstance(result.get("text"), str)
        ):
            raise BridgeError("Core returned an unsupported read response")
        return {"target": target, **{key: result[key] for key in ("complete", "how", "text")}}

    async def read_result(self, machine: str, path: str) -> ResultText | None:
        """Read a remote result file, or None while Core has no such file.

        The path must be one that `result_path` builds, and the machine must be in
        `send_machines` now; anything else is refused before `hc` runs. Every other
        outcome than the file or `not_found` raises BridgeError. Core's output is
        decoded strictly: a file that is not valid UTF-8 comes back with
        `utf8=False` and a lossy text that is only fit for finding its marker lines.
        """
        if not isinstance(path, str) or not _RESULT_PATH.fullmatch(path):
            raise ValueError("Core result path is not a bridge result file")
        if not isinstance(machine, str) or machine not in self.send_machines:
            raise CoreNotReady("Core reads result files only on allow-listed machines")
        try:
            response = await self._run(
                "file",
                "read",
                "--on",
                machine,
                "--json",
                path,
                seconds=COMMAND_SECONDS,
                missing=True,
                keep_stdout_bytes=True,
            )
        except CoreMissing:
            return None
        if not isinstance(response.stdout_bytes, bytes):
            raise BridgeError("Core file read returned no raw output")
        try:
            stdout, utf8 = response.stdout_bytes.decode("utf-8"), True
        except UnicodeDecodeError:
            stdout, utf8 = response.stdout, False
        value = self._object(stdout)
        text = value.get("text")
        if not isinstance(text, str):
            raise BridgeError("Core returned an unsupported file read response")
        last, lines = value.get("last"), value.get("lines")
        if type(last) is int and type(lines) is int and last < lines:
            raise BridgeError("Core returned part of the result file")
        if utf8:
            # A JSON escape such as \ud800 decodes to a lone surrogate: not UTF-8 text.
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                utf8 = False
        return ResultText(text, utf8)

    async def machine_id(self, machine: str) -> str:
        """The permanent core id `hc machine ls` reports for a connected machine.

        Raises CoreNotSent, each case with its own sentence, when the list cannot be
        read, names the machine more than once, does not name it or not as
        `connected`, or gives it an id that is not 32 lowercase hex digits. Only
        machines in `send_machines` are asked about.
        """
        if not isinstance(machine, str) or machine not in self.send_machines:
            raise CoreNotReady("Core reads machine identity only on allow-listed machines")
        try:
            machines = (await self._call("machine", "ls", "--json")).get("machines")
        except BridgeError:
            raise CoreNotSent(NOT_SENT_MACHINES_UNREAD) from None
        if not isinstance(machines, list):
            raise CoreNotSent(NOT_SENT_MACHINES_UNREAD)
        found = [m for m in machines if isinstance(m, dict) and m.get("name") == machine]
        # Each refusal has its own sentence: a name listed twice or an id of the wrong
        # form is not fixed by retrying the read, unlike an unreadable list.
        if len(found) > 1:
            raise CoreNotSent(NOT_SENT_MACHINE_TWICE)
        if not found or found[0].get("state") != "connected":
            raise CoreNotSent(NOT_SENT_MACHINE)
        ident = found[0].get("id")
        if not isinstance(ident, str) or not _MACHINE_ID.fullmatch(ident):
            raise CoreNotSent(NOT_SENT_MACHINE_BAD_ID)
        return ident

    async def terminal_ended(self, target: str) -> bool:
        """Whether Core reports the terminal `finished`, or answers that it is gone.

        Any other answer, including an unknown or unreachable machine, raises
        BridgeError: it proves nothing about the terminal.
        """
        target = _address(target)
        try:
            row = await self._call("get", target, "--json", seconds=COMMAND_SECONDS, missing=True)
        except CoreMissing:
            return True
        if row.get("address") != target or not _string(row.get("state"), limit=128):
            raise BridgeError("Core returned an unsupported terminal state")
        return row["state"] == "finished"

    async def send(
        self,
        target: str,
        message: str,
        expected: dict[str, Any],
        *,
        machine_id: str | None = None,
    ) -> dict[str, Any]:
        """Type `message` into the selected terminal and verify its identity before and after.

        A terminal on another machine also needs `machine_id`, the core id admitted
        for its machine. That id is read again as the last step before `hc send`
        (CoreNotSent when it differs: nothing is typed) and once more after it
        (CoreIdentityChanged when it differs: delivery is uncertain), so a name
        that a different core took over is never recorded as verified.

        An `idle` terminal is sent to with `--if idle`. A `done` terminal (when
        `send_to_done` is on) is sent to without a status condition, so Core does not
        close the window between this check and the text arriving; a receipt whose
        status is not `done` or `idle` is reported as uncertain delivery: as
        CoreTypedWhileWorking when the identity check after the send finds the same
        agent, as CoreIdentityChanged when it does not. Any doubt
        after the native send starts means: delivery is uncertain, do not resubmit.
        """
        target = _address(target)
        validate_message(message)
        if (
            not isinstance(expected, dict)
            or expected.get("peer_id") != f"hc:{target}"
            or expected.get("provider") != "hc"
            or expected.get("target") != target
            or "session" not in expected
            or expected["session"] != self.session
            or not isinstance(expected.get("binding"), str)
            or re.fullmatch(r"[0-9a-f]{64}", expected["binding"]) is None
        ):
            raise CoreIdentityChanged("Core send requires the selected terminal identity")
        if not self._sendable_address(target):
            raise CoreNotReady(
                "Remote Core terminals support reading only unless their machine is allow-listed"
            )
        machine = address_machine(target)
        if machine is not None and (
            not isinstance(machine_id, str) or not _MACHINE_ID.fullmatch(machine_id)
        ):
            raise CoreNotReady("Core send to another machine requires its admitted core id")
        try:
            current = await self.peer(target)
        except CoreNotReady:
            raise CoreIdentityChanged("Core foreground agent changed before send") from None
        except CoreIdentityChanged:
            raise
        except BridgeError:
            # `hc send` has not run: an unreachable machine or an unreadable answer
            # here proves that nothing was typed.
            raise CoreNotSent(NOT_SENT_UNREAD) from None
        if any(
            expected.get(key) != current[key]
            for key in ("binding", "agent_kind", "project", "project_known")
        ):
            raise CoreIdentityChanged("Core terminal identity changed before send")
        if not current["send_ready"]:
            raise CoreNotReady(
                "Core terminal needs a recognized foreground command, known directory, "
                "and reported " + ("idle or done" if self.send_to_done else "idle")
            )
        # `hc send --if` accepts only idle or working; a `done` terminal is sent to
        # without a condition (never with --force, so Core still refuses a blocked
        # program) and its receipt status is checked below instead. `--` ends the
        # options, so a message beginning with dashes is always the text argument.
        guarded = current["state"] == "idle"
        condition = ("--if", "idle") if guarded else ()
        if machine is not None and await self.machine_id(machine) != machine_id:
            # The last await before `hc send`: the name must still belong to the
            # core admitted for it. CoreNotSent (from the read or from here) means
            # nothing was typed.
            raise CoreNotSent(NOT_SENT_MACHINE_CHANGED)
        try:
            receipt = await self._call(
                "send",
                *condition,
                "--json",
                "--",
                target,
                message,
                seconds=SEND_SECONDS,
                allow_error=True,
                status_mismatch=True,
            )
        except CoreNotReady:
            raise
        except BridgeError as error:
            raise BridgeError(f"{error}; delivery is uncertain. Do not resubmit blindly.") from None
        if any(key in receipt and receipt[key] != target for key in ("address", "target")):
            raise CoreIdentityChanged(
                "Core send receipt identifies a different terminal; delivery is uncertain. "
                "Do not resubmit blindly."
            )
        if (
            "error" in receipt
            or type(receipt.get("turn")) is not int
            or receipt["turn"] < 0
            or "started" not in receipt
            or (receipt["started"] is not None and type(receipt["started"]) is not bool)
            or "status" not in receipt
            or (receipt["status"] is not None and not isinstance(receipt["status"], str))
        ):
            raise BridgeError(
                "Core returned an unsupported send receipt; delivery is uncertain. "
                "Do not resubmit blindly."
            )
        # A `done` send has no `--if`, so its receipt can say the agent was already
        # in a turn of its own. The identity check below still runs: if the same
        # terminal, agent and binding are there, the text landed in that turn and
        # the worker's later report with the task ID settles the receipt
        # (CoreTypedWhileWorking). A changed identity is still a changed identity.
        typed_while_working = not guarded and receipt["status"] not in _DONE_RECEIPT_STATES
        try:
            after = await self.peer(target)
        except (CoreNotReady, CoreIdentityChanged):
            raise CoreIdentityChanged(
                "Core foreground agent changed after send; delivery is uncertain. "
                "Do not resubmit blindly."
            ) from None
        except BridgeError:
            raise BridgeError(
                "Core identity could not be verified after send; delivery is uncertain. "
                "Do not resubmit blindly."
            ) from None
        if after["binding"] != current["binding"]:
            raise CoreIdentityChanged(
                "Core terminal identity changed after send; delivery is uncertain. "
                "Do not resubmit blindly."
            )
        if machine is not None:
            # `hc send` names the machine, not its core id, so a different core that
            # took the name between the check above and the send would have been
            # typed into. Read the id once more; only the same id verifies.
            try:
                after_id = await self.machine_id(machine)
            except BridgeError:
                raise BridgeError(
                    "Core machine id could not be read after send; delivery is uncertain. "
                    "Do not resubmit blindly."
                ) from None
            if after_id != machine_id:
                raise CoreIdentityChanged(
                    "Core machine id changed during send; delivery is uncertain. "
                    "Do not resubmit blindly."
                )
        verified = {
            "target": target,
            **{key: receipt[key] for key in ("turn", "started", "status")},
            "identity_guard": "observed_before_and_after",
            # Which Core made the two identity reads: this machine's, or the remote
            # one's through the machine link. The guard's value stays the same word
            # for both, so consumers that match it do not split.
            "identity_observer": identity_observer(target),
        }
        if typed_while_working:
            raise CoreTypedWhileWorking(
                "Core typed into a terminal that was not done or idle; the same agent is "
                "still there, so delivery is uncertain until the worker reports. "
                "Do not resubmit blindly.",
                receipt=verified,
                turn_before=current["turn"],
                turn_after=after["turn"],
            )
        return verified
