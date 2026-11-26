"""Inspect existing bridge configurations and archive stopped, owned directories."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .process import BridgeError
from .tasks import STATES, TERMINAL
from .transport import RemoteBridge, TransportUnavailable

ARCHIVES = "herdr-bridge-archives"
MAX_ROOT_ENTRIES = 1_024
MAX_CONTENT_ENTRIES = 10_000
MAX_CONTENT_DEPTH = 16


def _recognized(path: Path) -> bool:
    return path.name != ARCHIVES and (
        path.name in {"answerback-bridge", "herdr-bridge", "codex-herdr-bridge"}
        or path.name.startswith(("answerback-bridge-", "herdr-bridge-"))
    )


def _path_safety(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Configuration paths must be absolute without parent traversal")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"Refusing a symlink path: {component}")


def _owned(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"Refusing a symlink: {path}")
    if info.st_uid != os.getuid():
        raise ValueError(f"Path must be owned by the current user: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Expected a configuration directory: {path}")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular configuration file: {path}")
    return info


def _present(path: Path) -> bool:
    """Check the entry itself, including broken symlinks, without following it."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _directory_safety(directory: Path) -> None:
    _owned(directory, directory=True)
    if directory == Path.home() or directory == Path(directory.anchor):
        raise ValueError("Refusing to archive a home or root directory")
    if _present(directory / ".git"):
        raise ValueError("Refusing to archive a project directory")


def _tree_safety(directory: Path) -> None:
    """Bound archive validation independently of the immediate config inventory."""
    _directory_safety(directory)
    inspected = 0

    def visit(parent: Path, depth: int) -> None:
        nonlocal inspected
        # Unlike os.walk, scandir does not collect an entire wide directory before
        # the entry limit is enforced. Traversal holds at most depth + 1 iterators.
        with os.scandir(parent) as entries:
            for entry in entries:
                if inspected >= MAX_CONTENT_ENTRIES:
                    raise ValueError(
                        f"Configuration contents exceed the {MAX_CONTENT_ENTRIES}-entry "
                        "safety limit; archive refused"
                    )
                inspected += 1
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError(f"Refusing a symlink in configuration contents: {path}")
                if info.st_uid != os.getuid():
                    raise ValueError(f"Configuration contents must be owned by this user: {path}")
                if stat.S_ISDIR(info.st_mode):
                    if depth >= MAX_CONTENT_DEPTH:
                        raise ValueError(
                            f"Configuration contents exceed the {MAX_CONTENT_DEPTH}-directory "
                            f"depth safety limit at {path}; archive refused"
                        )
                    visit(path, depth + 1)
                elif not (stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
                    raise ValueError(f"Refusing unsafe configuration contents: {path}")

    visit(directory, 0)


def _lock(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"Lock must be a regular file owned by this user: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _lock_reason(path: Path) -> str | None:
    try:
        descriptor = _lock(path)
    except BlockingIOError:
        return f"Lock is held by an active service or live test: {path}"
    except (OSError, ValueError) as error:
        return f"Unable to verify lock: {error}"
    else:
        os.close(descriptor)
        return None


def _receipts(path: Path) -> int:
    # Immutable reads avoid creating sidecars but also skip recovery. Either
    # journal form can make the main database an incomplete receipt history.
    for suffix, kind in (("-wal", "WAL"), ("-journal", "rollback journal")):
        if _present(path.with_name(path.name + suffix)):
            raise ValueError(f"Receipt database has a {kind}; inspect/recover it before archiving")
    if not _present(path):
        return 0
    _owned(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True, timeout=0.2)
    try:
        connection.execute("PRAGMA query_only=ON")
        counts = connection.execute("SELECT status, count(*) FROM tasks GROUP BY status").fetchall()
        if any(status not in STATES for status, _ in counts):
            raise ValueError("Receipt database contains an unknown task status")
        return sum(count for status, count in counts if status not in TERMINAL)
    finally:
        connection.close()


def _actor_receipts(path: Path) -> int:
    """Retain unfinished actor runs and unconsumed messages without opening writable state."""
    for suffix in ("-wal", "-journal"):
        if _present(path.with_name(path.name + suffix)):
            raise ValueError("Actor database has journal state; recover it before archiving")
    if not _present(path):
        return 0
    _path_safety(path)
    _owned(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True, timeout=0.2)
    try:
        connection.execute("PRAGMA query_only=ON")
        states = connection.execute(
            "SELECT state,count(*) FROM actor_runs GROUP BY state"
        ).fetchall()
        deliveries = connection.execute(
            "SELECT delivery,count(*) FROM actor_messages GROUP BY delivery"
        ).fetchall()
        if any(state not in {"staging", "sealed", "finished"} for state, _ in states) or any(
            state not in {"queued", "published", "delivered", "expired", "rejected"}
            for state, _ in deliveries
        ):
            raise ValueError("Actor database contains an unknown lifecycle state")
        pending = connection.execute(
            "SELECT count(*) FROM actor_messages WHERE delivery NOT IN ('expired','rejected') "
            "AND (delivered_at IS NULL OR acknowledged_at IS NULL)"
        ).fetchone()[0]
        return sum(count for state, count in states if state != "finished") + pending
    finally:
        connection.close()


def _snapshot(config_path: Path, *, locks_held: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {
        "config_path": str(config_path),
        "session": None,
        "config_valid": False,
        "workspace_aliases": {"desktop": [], "kiro": []},
        "modified_at": None,
        "bridge_status": {"running": None, "state": "unknown"},
        "unresolved_receipts": None,
        "unresolved_actor_items": None,
        "archive_eligible": False,
        "content_check": "not_run",
        "reasons": [],
        "errors": [],
    }
    reasons = row["reasons"]
    try:
        _path_safety(config_path)
        if config_path.name != "config.json" or not _recognized(config_path.parent):
            raise ValueError("Select config.json in a recognized bridge configuration directory")
        _directory_safety(config_path.parent)
        row["modified_at"] = _owned(config_path).st_mtime
        config = Config.load(config_path)
        row["config_valid"] = True
        row["session"] = config.herdr_session
        row["workspace_aliases"] = {
            "desktop": sorted(config.desktop_threads),
            "kiro": sorted(config.kiro_workspaces),
        }
    except (OSError, ValueError, TypeError, AttributeError) as error:
        row["errors"].append(f"Configuration inspection failed: {error}")
        reasons.extend(row["errors"])
        return row
    if _present(config_path.parent / "environment.json"):
        reasons.append(
            "Retaining reusable live-test environment: its separate Herdr runtime and "
            "workspace require coordinated cleanup; generic config archival is unavailable"
        )
    runtime = config_path.parent / "runtime"
    try:
        _owned(runtime, directory=True)
        owner = runtime / "config-owner.json"
        _owned(owner)
        if json.loads(owner.read_text()) != {"config_path": str(config_path)}:
            raise ValueError("Configuration ownership does not bind this exact config.json path")
    except (OSError, ValueError, TypeError) as error:
        message = f"Configuration ownership cannot be verified: {error}"
        reasons.append(message)
        row["errors"].append(message)
    try:
        row["unresolved_receipts"] = _receipts(config_path.parent / "tasks.sqlite")
    except (OSError, sqlite3.Error, ValueError) as error:
        message = f"Unable to read task receipts: {error}"
        reasons.append(message)
        row["errors"].append(message)
    if row["unresolved_receipts"]:
        reasons.append(f"{row['unresolved_receipts']} unresolved task receipts must be reconciled")
    try:
        row["unresolved_actor_items"] = _actor_receipts(config_path.parent / "bus/actors.sqlite")
    except (OSError, sqlite3.Error, ValueError) as error:
        message = f"Unable to read actor receipts: {error}"
        reasons.append(message)
        row["errors"].append(message)
    if row["unresolved_actor_items"]:
        reasons.append(
            f"{row['unresolved_actor_items']} unfinished actor runs or messages must be resolved"
        )
    if not locks_held:
        for path in (runtime / "owner.lock", config_path.parent / "run.lock"):
            if path.name == "run.lock" and not path.exists():
                continue
            if reason := _lock_reason(path):
                reasons.append(reason)
    return row


async def _status(config_path: Path, row: dict[str, Any]) -> None:
    if not row["config_valid"] or any("ownership" in reason.lower() for reason in row["reasons"]):
        return
    path = config_path.parent / "runtime" / "bridge.sock"
    try:
        socket_info = await asyncio.to_thread(path.lstat)
    except FileNotFoundError:
        row["bridge_status"] = {"running": False, "state": "stopped"}
        if any(
            "Lock is held" in reason and reason.endswith(str(path.parent / "owner.lock"))
            for reason in row["reasons"]
        ):
            row["bridge_status"] = {"running": True, "state": "unresponsive_or_starting"}
        return
    except OSError as error:
        message = f"Bridge status cannot be inspected: {error}"
    else:
        if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.getuid():
            row["reasons"].append("Bridge status path is not a socket owned by this user")
            return
        try:
            async with asyncio.timeout(2):
                result = await RemoteBridge(config_path).call("status", {})
            if not isinstance(result, dict) or result.get("running") is not True:
                raise ValueError("Bridge returned an unsupported status response")
            row["bridge_status"] = {
                key: result[key]
                for key in ("running", "accepting", "pid", "started", "instance_id")
                if key in result
            }
            row["bridge_status"]["state"] = "running"
            row["reasons"].append("Bridge service is running; stop it before archiving")
            return
        except TransportUnavailable as error:
            if isinstance(error.__cause__, FileNotFoundError | ConnectionRefusedError):
                row["bridge_status"] = {"running": False, "state": "stopped"}
                return
            message = "Bridge status is unresponsive or uncertain; archive refused"
        except (
            OSError,
            BridgeError,
            ValueError,
            TimeoutError,
            TypeError,
            KeyError,
            AttributeError,
        ) as error:
            message = f"Bridge status could not be verified ({type(error).__name__})"
    row["bridge_status"] = {"running": None, "state": "unresponsive_or_unknown"}
    row["reasons"].append(message)
    row["errors"].append(message)


async def _inspect(
    config_path: Path, *, locks_held: bool = False, check_contents: bool = False
) -> dict[str, Any]:
    row = await asyncio.to_thread(_snapshot, config_path, locks_held=locks_held)
    await _status(config_path, row)
    stopped = row["bridge_status"].get("state") == "stopped"
    if check_contents and not row["reasons"] and stopped:
        try:
            await asyncio.to_thread(_tree_safety, config_path.parent)
        except (OSError, ValueError) as error:
            row["content_check"] = "failed"
            message = f"Configuration contents validation failed: {error}"
            row["reasons"].append(message)
            row["errors"].append(message)
        else:
            row["content_check"] = "passed"
    elif not check_contents:
        row["reasons"].append(
            "Contents safety check not run; archive preview or apply must validate contents"
        )
    row["archive_eligible"] = not row["reasons"] and stopped and row["content_check"] == "passed"
    return row


def _candidates(root: Path) -> tuple[list[Path], list[str]]:
    _path_safety(root)
    if not root.exists():
        return [], []
    if _recognized(root):
        return [root / "config.json"], []
    paths: list[Path] = []
    errors: list[str] = []
    try:
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ROOT_ENTRIES:
                    errors.append(
                        f"Configuration inventory reached the {MAX_ROOT_ENTRIES}-entry "
                        "root limit; remaining entries were not inspected"
                    )
                    break
                path = Path(entry.path)
                if _recognized(path):
                    paths.append(path / "config.json")
    except OSError as error:
        errors.append(f"Configuration inventory failed: {error}")
    return sorted(paths), errors


async def list_configs(root: Path | None = None) -> dict[str, Any]:
    """List immediate known config directories without creating or migrating files."""
    root = root or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    result: dict[str, Any] = {"root": str(root), "configs": [], "complete": True, "errors": []}
    try:
        paths, errors = await asyncio.to_thread(_candidates, root)
        result["errors"].extend(errors)
    except (OSError, ValueError) as error:
        result["complete"] = False
        result["errors"].append(f"Configuration inventory failed: {error}")
        return result
    for path in paths:
        row = await _inspect(path)
        result["configs"].append(row)
        result["errors"].extend(row["errors"])
    result["complete"] = not result["errors"]
    return result


def _destination(config_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return (
        config_path.parent.parent
        / ARCHIVES
        / (f"{config_path.parent.name}-{timestamp}-{uuid.uuid4().hex[:8]}")
    )


def _destination_safety(destination: Path) -> None:
    _path_safety(destination)
    if destination.exists():
        raise ValueError("Archive destination already exists; choose a new destination")
    if destination.parent.exists():
        _owned(destination.parent, directory=True)
    _owned(destination.parent.parent, directory=True)


def _move(source: Path, destination: Path) -> None:
    _destination_safety(destination)
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    _destination_safety(destination)
    source.rename(destination)


async def archive_config(config_path: Path, apply: bool = False) -> dict[str, Any]:
    """Plan by default; apply preserves the complete stopped configuration directory."""
    row = await _inspect(config_path, check_contents=True)
    destination = _destination(config_path)
    result: dict[str, Any] = {
        **row,
        "source": str(config_path.parent),
        "destination": str(destination),
        "apply": apply,
        "archived": False,
        "client_references": "Update client references to the old configuration path as needed.",
        "restore_required": (
            "Restore this directory to its original path before restarting; "
            "the unchanged ownership record binds that original config.json path."
        ),
    }
    if not row["archive_eligible"]:
        return result
    try:
        await asyncio.to_thread(_destination_safety, destination)
    except (OSError, ValueError) as error:
        result["reasons"].append(f"Unsafe archive destination: {error}")
        result["archive_eligible"] = False
        return result
    if not apply:
        return result
    descriptors: list[int] = []
    try:
        for path in (
            config_path.parent / "runtime" / "owner.lock",
            config_path.parent / "run.lock",
        ):
            if path.name == "run.lock" and not await asyncio.to_thread(path.exists):
                continue
            descriptors.append(await asyncio.to_thread(_lock, path))
        checked = await _inspect(config_path, locks_held=True, check_contents=True)
        result.update(checked)
        if checked["archive_eligible"]:
            await asyncio.to_thread(_move, config_path.parent, destination)
            result["archived"] = True
    except (OSError, ValueError) as error:
        result["reasons"].append(f"Archive refused while checking locks or moving: {error}")
        result["archive_eligible"] = False
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    return result
