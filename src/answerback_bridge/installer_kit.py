"""Public, provider-neutral primitives for safe client installation.

Provider registrars use this module instead of reaching into ``registration`` internals.
The functions preserve the kernel installer's refusal rules, path checks, snapshots,
private backups, launch reporting, and owned-child process boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .plugin import ClientRegistrationError
from .process import Output
from .process import run as _run_process

SERVER_NAME = "answerback_bridge"
LEGACY_SERVER_NAMES = ("herdr_bridge", "codex_herdr_bridge")
SETUP_SKILL_NAME = "answerback-fleet"
BRIDGE_MODULES = ("answerback_bridge", "codex_herdr_bridge")
Snapshot = tuple[tuple[str, str, str], ...] | None

__all__ = (
    "SERVER_NAME",
    "LEGACY_SERVER_NAMES",
    "Snapshot",
    "SETUP_SKILL_NAME",
    "BRIDGE_MODULES",
    "absolute",
    "safe_path",
    "safe_parent",
    "read",
    "json_object",
    "route_matches",
    "owned_route",
    "add_safe_path_flag",
    "launch_record",
    "snapshot",
    "tree_matches",
    "private_copy",
    "private_tree",
    "run",
)

_Refusal = ClientRegistrationError
_CONSOLE_SCRIPTS = ("answerback", "answerback-bridge", "herdr-bridge", "codex-herdr")
_INTERPRETER_NAME = re.compile(r"pythonw?(?:\d+(?:\.\d+)?)?[dt]{0,2}(?:\.exe)?", re.IGNORECASE)


def absolute(path: Path) -> Path:
    """Return an absolute expanded path without following its final filesystem entry."""
    return Path(os.path.abspath(path.expanduser()))


def safe_path(path: Path) -> None:
    """Refuse a symlinked path chain or a non-regular existing configuration file."""
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise _Refusal("Refusing to modify a symlinked configuration path")
    if path.exists() and not path.is_file():
        raise _Refusal("Configuration path is not a regular file")


def safe_parent(path: Path) -> None:
    """Refuse symlinked or non-directory installation parents before mutation."""
    for parent in path.parents:
        if parent.is_symlink():
            raise _Refusal("Refusing to modify a symlinked installation parent")
        if parent.exists() and not parent.is_dir():
            raise _Refusal("Installation parent is not a directory")


def read(path: Path) -> bytes | None:
    """Read at most 16 MiB with ``O_NOFOLLOW``; ``None`` when the file is absent."""
    safe_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        value = stream.read(16 * 1024 * 1024 + 1)
    if len(value) > 16 * 1024 * 1024:
        raise _Refusal("Configuration exceeds the 16 MiB installation limit")
    return value


def json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while refusing duplicate keys at the parse boundary."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _route_config_arguments(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return []
    args = entry.get("args")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return []
    if args.count("mcp") != 1:
        return []
    paths: list[str] = []
    for index, argument in enumerate(args):
        if argument == "--config" and index + 1 < len(args):
            paths.append(args[index + 1])
        elif argument.startswith("--config="):
            paths.append(argument.split("=", 1)[1])
    return paths


def route_matches(entry: Any, config_path: str) -> bool:
    """Match one unambiguous route to a resolved configuration path."""
    paths = _route_config_arguments(entry)
    matches = [
        path
        for path in paths
        if Path(path).expanduser().resolve() == Path(config_path).expanduser().resolve()
    ]
    if matches and len(paths) > 1:
        raise _Refusal("Ambiguous bridge configuration arguments; review the route manually")
    return len(paths) == 1 and bool(matches)


def _module_launch_prefix(args: list[str]) -> list[str] | None:
    for prefix in (
        ["-P", "-m", "answerback_bridge"],
        ["-P", "-m", "codex_herdr_bridge"],
        ["-m", "answerback_bridge"],
        ["-m", "codex_herdr_bridge"],
    ):
        if args[: len(prefix)] == prefix:
            return prefix
    return None


def owned_route(entry: Any, config_path: Path) -> bool:
    """Prove that a local stdio entry has the bridge's known route and launcher shape."""
    if not route_matches(entry, str(config_path)):
        return False
    if any(key in entry for key in ("url", "httpUrl")):
        return False
    if any(entry.get(key) not in (None, "stdio") for key in ("type", "transport")):
        return False
    args = entry["args"]
    config_argument = _route_config_arguments(entry)[0]
    if not Path(config_argument).is_absolute():
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    environment = entry.get("env", {})
    if isinstance(environment, dict) and any(
        isinstance(environment.get(key), str)
        and Path(environment[key]).expanduser().resolve() == config_path.resolve()
        for key in ("CODEX_HERDR_MANAGED_CONFIG", "ANSWERBACK_MANAGED_CONFIG")
    ):
        return True
    if Path(command).name in _CONSOLE_SCRIPTS:
        executable = (
            Path(command) if Path(command).is_absolute() else Path(shutil.which(command) or command)
        )
        try:
            with executable.open("rb") as stream:
                source = stream.read(64 * 1024 + 1)
        except OSError:
            return False
        return (
            len(source) <= 64 * 1024
            and re.search(
                rb"(?m)^from (?:answerback_bridge|codex_herdr_bridge)\.cli import main\s*$",
                source,
            )
            is not None
        )
    return (
        re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", Path(command).name) is not None
        and len(args) >= 2
        and _module_launch_prefix(args) is not None
    )


def _mentions_bridge_module(args: list[str]) -> bool:
    return any(
        arg in BRIDGE_MODULES
        or (
            arg.startswith("-")
            and not arg.startswith("--")
            and arg.endswith(tuple("m" + module for module in BRIDGE_MODULES))
        )
        for arg in args
    )


def _unread_option_state(safe: bool, rest: list[str]) -> str | None:
    if not _mentions_bridge_module(rest):
        return None
    return "present" if safe else "unknown"


def _safe_path_state(command: Any, args: Any) -> str | None:
    if not isinstance(command, str) or not isinstance(args, list):
        return None
    if not all(isinstance(arg, str) for arg in args):
        return None
    if _INTERPRETER_NAME.fullmatch(Path(command).name) is None:
        return None
    safe = False
    index = 0
    while index < len(args):
        argument = args[index]
        if not argument.startswith("-") or argument in ("-", "--"):
            return None
        if argument.startswith("--"):
            return _unread_option_state(safe, args[index:])
        index += 1
        position = 1
        while position < len(argument):
            letter = argument[position]
            position += 1
            if letter == "P":
                safe = True
            elif letter in "XWmc":
                value = argument[position:]
                if not value:
                    if index == len(args):
                        return None
                    value = args[index]
                    index += 1
                if letter == "c":
                    return None
                if letter == "m":
                    if value not in BRIDGE_MODULES:
                        return None
                    return "present" if safe else "absent"
                break
            elif letter not in "bBdEiIOqRsStuvx":
                return _unread_option_state(safe, args[index - 1 :])
    return None


def add_safe_path_flag(entry: Any, scope: str) -> bool:
    """Insert ``-P`` only when an owned interpreter route is proven to lack it."""
    if not isinstance(entry, dict):
        return False
    state = _safe_path_state(entry.get("command"), entry.get("args"))
    if state == "unknown":
        raise _Refusal(
            f"Bridge module launch at {scope} has interpreter options that "
            "cannot be checked for -P; put -P first in the arguments of that launch, "
            "directly after the interpreter, then migrate again"
        )
    if state != "absent":
        return False
    entry["args"].insert(0, "-P")
    return True


def _display(text: str) -> str:
    def shown(character: str) -> str:
        if character == "\\":
            return "\\\\"
        if character.isprintable():
            return character
        code = ord(character)
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"

    return "".join(map(shown, text))


def _script_directory() -> Path:
    launcher = absolute(Path(sys.argv[0]))
    if launcher.name in _CONSOLE_SCRIPTS:
        return launcher.parent
    return absolute(Path(sys.executable)).parent


def _installation_directory() -> Path:
    launcher = absolute(Path(sys.argv[0]))
    try:
        if launcher.name in _CONSOLE_SCRIPTS:
            return launcher.resolve().parent
        return _script_directory().resolve()
    except (OSError, RuntimeError):
        return _script_directory()


def _launcher_location(command: str) -> dict[str, Any]:
    if Path(command).name not in _CONSOLE_SCRIPTS:
        return {}
    launcher = (
        Path(command) if Path(command).is_absolute() else Path(shutil.which(command) or command)
    )
    directory = _installation_directory()
    scripts = f"the scripts of the installation that runs this command ({_display(str(directory))})"
    try:
        found = launcher.resolve(strict=True).parent
    except (OSError, RuntimeError):
        return {
            "launcher_in_this_installation": False,
            "launcher_note": (
                "The path of this launcher cannot be followed to an existing file (a part "
                "of it is missing, or its links do not end), so it is not shown to be among "
                f"{scripts}. Nothing is refused because of it. Read the command before you "
                "apply."
            ),
        }
    if found == directory:
        return {"launcher_in_this_installation": True}
    return {
        "launcher_in_this_installation": False,
        "launcher_note": (
            "With its links followed, this launcher is in a different directory from "
            f"{scripts}. That is a difference of location only: it is not a sign of an "
            "attack, and the same directory does not prove ownership either. Nothing is "
            "refused because of it. Read the command before you apply."
        ),
    }


def launch_record(
    scope: str,
    command: str,
    args: list[str],
    *,
    renamed_from: str | None,
    flag_added: bool,
    replaces: list[str] | None = None,
) -> dict[str, Any]:
    """Build an escaped, environment-free launch report for operator review."""
    record: dict[str, Any] = {"scope": _display(scope), "name": SERVER_NAME}
    if renamed_from is not None:
        record["renamed_from"] = renamed_from
    record["command"] = [_display(str(argument)) for argument in (command, *args)]
    if replaces is not None:
        record["replaces"] = [_display(str(argument)) for argument in replaces]
    record["safe_path_flag_added"] = flag_added
    record.update(_launcher_location(command))
    return record


def snapshot(path: Path) -> Snapshot:
    if not path.exists() and not path.is_symlink():
        return None
    rows: list[tuple[str, str, str]] = []

    def visit(item: Path, relative: str) -> None:
        if item.is_symlink():
            rows.append((relative, "symlink", os.readlink(item)))
        elif item.is_dir():
            rows.append((relative, "directory", ""))
            for child in sorted(item.iterdir()):
                visit(child, relative + "/" + child.name)
        elif item.is_file():
            rows.append((relative, "file", hashlib.sha256(item.read_bytes()).hexdigest()))
        else:
            raise _Refusal("Installation path contains an unsupported file type")

    visit(path, "")
    return tuple(rows)


def tree_matches(path: Path, expected: Snapshot) -> bool:
    """Return true only when parent checks and a fresh tree snapshot match exactly."""
    try:
        safe_parent(path)
        return snapshot(path) == expected
    except (OSError, _Refusal):
        return False


def _backup_directory(path: Path, home: Path, expected: Snapshot) -> Path:
    root = home / ".local/state/answerback-bridge/backups"
    safe_parent(root / "backup")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    backup = Path(tempfile.mkdtemp(prefix=path.name + ".herdr-bridge-backup-", dir=root))
    metadata = backup / "recovery.json"
    metadata.write_text(json.dumps({"schemaVersion": 1, "target": str(path), "snapshot": expected}))
    metadata.chmod(0o600)
    return backup


def private_copy(path: Path, expected: Snapshot, home: Path) -> Path:
    if snapshot(path) != expected:
        raise _Refusal("Installation changed during backup; review and retry")
    backup = _backup_directory(path, home, expected)
    content = backup / "content"
    if path.is_symlink():
        content.symlink_to(os.readlink(path))
    elif path.is_dir():
        shutil.copytree(path, content, symlinks=True)
    else:
        shutil.copyfile(path, content)
    for item in (
        [content, *content.rglob("*")]
        if content.is_dir() and not content.is_symlink()
        else [content]
    ):
        if not item.is_symlink():
            item.chmod(0o700 if item.is_dir() else 0o600)
    if snapshot(content) != expected or snapshot(path) != expected:
        raise _Refusal("Installation changed during backup; review and retry")
    return backup


def private_tree(path: Path) -> None:
    """Set directories to 0700 and files to 0600 while preserving owner execute."""
    if path.is_symlink():
        return
    if path.is_dir():
        path.chmod(0o700)
        for child in path.iterdir():
            private_tree(child)
    else:
        path.chmod(0o600 | (path.stat().st_mode & 0o100))


async def run(args: list[str], **kwargs: Any) -> Output:
    """Run one owned child without a shell (``process.run``)."""
    return await _run_process(args, **kwargs)
