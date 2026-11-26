"""Everything routing touches outside its own process, behind small interfaces.

Tests script these Protocols; the real classes shell out to `hc` (programs) and
`herdr-app` (the visible window) and read the status/ files. No test starts a terminal.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from ..process import BridgeError, CommandError, CommandTimeout, run

_LOCAL_ADDRESS = re.compile(r"t[0-9a-z]{1,16}")
_MAX_STATUS_BYTES = 4 * 1024 * 1024
MAX_NOTE_CHARS = 4096


class RoutingError(BridgeError):
    """Routing could not complete a step; the message says which and what was left."""


class LayoutError(RoutingError):
    """Herdr App refused or could not perform a layout command."""


class BridgeCalls(Protocol):
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class Terminals(Protocol):
    """Core (hc): programs and their terminals."""

    async def live(self) -> list[dict[str, Any]]: ...

    async def get(self, address: str) -> dict[str, Any] | None: ...

    async def run(self, *, name: str, cwd: str, argv: list[str]) -> str: ...

    async def kill(self, address: str) -> None: ...

    async def remove(self, address: str) -> None: ...


class Layout(Protocol):
    """Herdr App: where a terminal is shown."""

    async def tab_new(
        self, *, space: str, folder: str, name: str, argv: list[str]
    ) -> dict[str, Any]: ...

    async def keep(self, address: str) -> None: ...

    async def right_click(self, address: str) -> None:
        """Send right-clicks in this terminal's pane to the App menu, not the program."""
        ...

    async def tab_terminals(self, tab: str) -> list[str] | None: ...

    async def space_tabs(self, tab: str) -> list[str] | None:
        """Ids of every tab in the space that holds this tab; None when it is not shown."""
        ...

    async def tab_close(self, tab: str) -> None: ...


class Handoffs(Protocol):
    def state(self, task: str) -> str:
        """Return merged, rejected or open."""
        ...


class Ledger(Protocol):
    def queued(self) -> list[str]:
        """Text of every task still to do; routing classifies it to count queued work."""
        ...


def local_address(value: Any) -> str:
    if not isinstance(value, str) or not _LOCAL_ADDRESS.fullmatch(value):
        raise RoutingError("Routing works on local core terminals only (an address like t7)")
    return value


async def _json(argv: list[str], *, seconds: float, env: dict[str, str] | None = None) -> Any:
    try:
        output = await run(argv, max_seconds=seconds, env=env)
    except CommandTimeout:
        raise RoutingError(
            f"{argv[0]} {argv[1]} timed out; its effect is unknown, inspect before retrying"
        ) from None
    try:
        return json.loads(output.stdout)
    except ValueError:
        raise RoutingError(f"{argv[0]} {argv[1]} returned something that is not JSON") from None


class HcTerminals:
    def __init__(self, binary: str = "hc", session: str | None = None) -> None:
        self.binary = binary
        self.session = session

    def _env(self) -> dict[str, str]:
        # Same rule as the bridge's core client: never inherit the caller's terminal
        # identity, so a worker is not parented to (or mistaken for) whoever ran this.
        env = dict(os.environ)
        for key in ("HC_TERMINAL_ID", "HC_SOCKET", "HC_SESSION"):
            env.pop(key, None)
        if self.session is not None:
            env["HC_SESSION"] = self.session
        return env

    async def _call(self, *args: str, seconds: float = 15) -> dict[str, Any]:
        value = await _json([self.binary, *args], seconds=seconds, env=self._env())
        if not isinstance(value, dict):
            raise RoutingError(f"hc {args[0]} returned an unsupported response")
        return value

    async def live(self) -> list[dict[str, Any]]:
        try:
            rows = (await self._call("ls", "--json")).get("terminals")
        except (CommandError, OSError):
            raise RoutingError("hc ls failed; is core running?") from None
        if not isinstance(rows, list):
            raise RoutingError("hc ls returned no terminal list")
        return [
            row
            for row in rows
            if isinstance(row, dict) and _LOCAL_ADDRESS.fullmatch(str(row.get("address")))
        ]

    async def get(self, address: str) -> dict[str, Any] | None:
        try:
            row = await self._call("get", local_address(address), "--json")
        except CommandError:
            return None
        except OSError:
            raise RoutingError("hc get failed; is core installed?") from None
        if "error" in row or row.get("address") != address:
            return None
        return row

    async def run(self, *, name: str, cwd: str, argv: list[str]) -> str:
        try:
            row = await self._call(
                "run", "--bg", "--no-parent", "--json", "--name", name, "--cwd", cwd, *argv
            )
        except (CommandError, OSError):
            raise RoutingError(f"hc run failed for {name}; no worker was confirmed") from None
        if "error" in row:
            raise RoutingError(f"hc run refused {name}: {str(row['error'])[:200]}")
        return local_address(row.get("address") or row.get("terminal"))

    async def _end(self, verb: str, address: str) -> None:
        try:
            row = await self._call(verb, "--json", local_address(address))
        except (CommandError, OSError):
            raise RoutingError(f"hc {verb} {address} failed") from None
        if "error" in row:
            raise RoutingError(f"hc {verb} {address} refused: {str(row['error'])[:200]}")

    async def kill(self, address: str) -> None:
        await self._end("kill", address)

    async def remove(self, address: str) -> None:
        await self._end("rm", address)


class HerdrApp:
    def __init__(self, binary: str = "herdr-app") -> None:
        self.binary = binary

    async def _call(self, *args: str) -> Any:
        try:
            value = await _json([self.binary, *args], seconds=20)
        except (CommandError, OSError):
            raise LayoutError(f"herdr-app {' '.join(args[:2])} failed; is the App open?") from None
        # A timeout or unreadable reply stays a RoutingError: the command may have run, so
        # the caller must not fall back to a second way of creating the same worker.
        if isinstance(value, dict) and "error" in value:
            raise LayoutError(f"herdr-app {' '.join(args[:2])}: {str(value['error'])[:200]}")
        return value

    async def tab_new(
        self, *, space: str, folder: str, name: str, argv: list[str]
    ) -> dict[str, Any]:
        # One JSON object keeps argv an array. No `task`: the worker starts bare (T-13).
        # focus false: the owner's shown space and selected tab stay where they are.
        result = await self._call(
            "do",
            "tab.new",
            json.dumps(
                {"space": space, "folder": folder, "name": name, "argv": argv, "focus": False}
            ),
        )
        if not isinstance(result, dict) or not isinstance(result.get("terminal"), str):
            raise LayoutError("herdr-app tab.new returned no terminal id")
        return result

    async def keep(self, address: str) -> None:
        await self._call("do", "terminal.keep", json.dumps({"terminal": address, "place": "tab"}))

    async def right_click(self, address: str) -> None:
        # Sent for every worker instead of relying on the App default: a pane left on
        # "program" lets pointer reports reach the harness prompt as typed text.
        await self._call(
            "do", "terminal.right_click", json.dumps({"terminal": address, "to": "menu"})
        )

    async def _find(self, tab: str) -> tuple[list[Any], dict[str, Any]] | None:
        """The tab's entry in `herdr-app ls`, with every tab of the space that holds it."""
        layout = await self._call("ls")
        spaces = layout.get("spaces") if isinstance(layout, dict) else None
        for space in spaces if isinstance(spaces, list) else []:
            tabs = space.get("tabs") if isinstance(space, dict) else None
            if not isinstance(tabs, list):
                continue
            for entry in tabs:
                if isinstance(entry, dict) and entry.get("id") == tab:
                    return tabs, entry
        return None

    async def tab_terminals(self, tab: str) -> list[str] | None:
        found = await self._find(tab)
        if found is None:
            return None
        return [
            str(terminal.get("terminal"))
            for terminal in found[1].get("terminals", [])
            if isinstance(terminal, dict)
        ]

    async def space_tabs(self, tab: str) -> list[str] | None:
        found = await self._find(tab)
        if found is None:
            return None
        return [str(entry.get("id")) for entry in found[0] if isinstance(entry, dict)]

    async def tab_close(self, tab: str) -> None:
        # `confirmed` skips the App's question, and the App closes a space with its last
        # tab. Callers check space_tabs first; see Pool._teardown.
        await self._call("do", "tab.close", json.dumps({"id": tab, "confirmed": True}))


@contextmanager
def _open_beneath(root: Path, relative: Path, *, directory: bool = False) -> Iterator[int]:
    """Open beneath a trusted root without following any untrusted path component."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise OSError("Expected a path beneath the project")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(root, flags | os.O_DIRECTORY)
    try:
        for index, part in enumerate(relative.parts):
            is_dir = directory or index < len(relative.parts) - 1
            child = os.open(part, flags | (os.O_DIRECTORY if is_dir else 0), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_bytes(root: Path, relative: Path) -> bytes | None:
    """Read at most the status cap from one regular, descriptor-checked file."""
    try:
        with _open_beneath(root, relative) as descriptor:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATUS_BYTES:
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(_MAX_STATUS_BYTES + 1)
            return data if len(data) <= _MAX_STATUS_BYTES else None
    except (OSError, ValueError):
        return None


def _read_json(path: Path, *, root: Path | None = None) -> Any:
    try:
        root = path.parent if root is None else root
        raw = _read_bytes(root, path.relative_to(root))
        if raw is None:
            return None
        value = json.loads(raw)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if isinstance(item, dict | list):
                if depth >= 64:
                    return None
                children = item.values() if isinstance(item, dict) else item
                pending.extend((child, depth + 1) for child in children)
        return value
    except (ValueError, RecursionError):
        return None


def _names_task(candidate: Any, task: str) -> bool:
    return isinstance(candidate, str) and (candidate == task or candidate.startswith(task + "-"))


# Mirror the settled-record contract in status/apply_queue.py without importing or
# executing repository code. A well-formed record is evidence of consistency, not
# authentication of the reviewer or the same-user process that wrote it.
_REQUIRED_SETTLEMENT = (
    "task",
    "root",
    "branch",
    "handoff",
    "reviewer",
    "reviewer_verdict",
    "review_evidence",
    "expected_shape",
    "requested_at",
    "state",
)
_PLAN_FIELDS = ("plan_file", "plan_sha256", "planned_shape")
_VERDICTS = frozenset({"PASS", "PASS_WITH_FINDINGS", "FAIL"})
_PLAN_SHAPE = re.compile(r"^\d+ to add, \d+ to change, \d+ to destroy$")
_PLAN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PATH_PROSE = re.compile(r"[\s;(]")


def _text_fields(entry: dict[str, Any], names: tuple[str, ...]) -> bool:
    return all(isinstance(entry.get(key), str) and entry[key].strip() for key in names)


def _relative_file(project: Path, value: str) -> bytes | None:
    if _PATH_PROSE.search(value):
        return None
    return _read_bytes(project, Path(value))


def _plan_valid(entry: dict[str, Any], *, required: bool) -> bool:
    """Mirror validate's scalar checks; reading/hashing saved plans belongs to verify."""
    if required and not _text_fields(entry, _PLAN_FIELDS):
        return False
    if any(key in entry and not isinstance(entry[key], str) for key in _PLAN_FIELDS):
        return False
    plan_file = entry.get("plan_file", "")
    if plan_file and (
        _PATH_PROSE.search(plan_file)
        or Path(plan_file).is_absolute()
        or ".." in Path(plan_file).parts
    ):
        return False
    digest = entry.get("plan_sha256", "")
    shape = entry.get("planned_shape", "")
    return (not digest or bool(_PLAN_DIGEST.fullmatch(digest))) and (
        not shape or bool(_PLAN_SHAPE.fullmatch(shape))
    )


def _settled(project: Path, entry: Any, state: str) -> bool:
    """Check lifecycle, typed fields and local evidence before releasing a worker."""
    if not isinstance(entry, dict):
        return False
    if not _text_fields(entry, _REQUIRED_SETTLEMENT):
        return False
    if entry["reviewer_verdict"] not in _VERDICTS:
        return False
    if state == "merged":
        if (
            entry["state"] != "applied"
            or entry["reviewer_verdict"] == "FAIL"
            or not _text_fields(entry, ("apply_result",))
        ):
            return False
    elif entry["state"] != "rejected" or not _text_fields(entry, ("reason",)):
        return False
    if _relative_file(project, entry["handoff"]) is None:
        return False
    merge_only = entry["root"] == "merge-only"
    if not merge_only:
        tf_root = Path(entry["root"])
        if tf_root.is_absolute() or ".." in tf_root.parts:
            return False
        try:
            with _open_beneath(project, Path("terraform") / tf_root, directory=True):
                pass
        except (OSError, ValueError):
            return False
    if not _plan_valid(entry, required=state == "merged" and not merge_only):
        return False
    follow_ups = entry.get("follow_up_plans", [])
    return isinstance(follow_ups, list) and all(
        isinstance(item, dict)
        and _text_fields(item, ("apply_result",))
        and _plan_valid(item, required=True)
        for item in follow_ups
    )


class QueueHandoffs:
    """Only consistent queue-contract records settle a hand-off."""

    def __init__(self, project: Path) -> None:
        self.project = project.resolve()
        self.queue = self.project / "status" / "apply-queue"

    def state(self, task: str) -> str:
        settled: set[str] = set()
        for folder, state in (("done", "merged"), ("rejected", "rejected")):
            relative = Path("status") / "apply-queue" / folder
            try:
                with _open_beneath(self.project, relative, directory=True) as descriptor:
                    names = sorted(
                        name for name in os.listdir(descriptor) if name.endswith(".json")
                    )
            except (OSError, ValueError):
                continue
            for name in names:
                entry = _read_json(self.queue / folder / name, root=self.project)
                named = entry.get("task") if isinstance(entry, dict) else None
                if _names_task(named, task) and _settled(self.project, entry, state):
                    settled.add(state)
        return settled.pop() if len(settled) == 1 else "open"


class TasksLedger:
    def __init__(self, project: Path) -> None:
        self.project = project.resolve()
        self.path = self.project / "status" / "tasks.json"

    def queued(self) -> list[str]:
        data = _read_json(self.path, root=self.project)
        rows = data.get("tasks") if isinstance(data, dict) else data
        return [
            str(row.get("note", ""))[:MAX_NOTE_CHARS]
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and row.get("lane") == "todo"
        ]
