"""The writer's settings file: what ``fleet configure`` writes and ``serve --settings`` reads.

The file holds what the command line of ``serve`` would otherwise carry: the workers, their
ceilings, the published folder and the lane label. A supervisor can then start the writer
from the file alone, and no service definition carries a machine name. The file is no part
of the bridge configuration file and the bridge never reads it.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import bounds
from .agents import DEFAULT_LABEL, label_ok

SCHEMA = "answerback.fleet-writer/1"
FILE = "fleet-writer.json"
READ_LIMIT = 64 * 1024
CEILING_MIN, CEILING_MAX = 1, 99
_KEYS = frozenset({"schema", "machines", "ceilings", "dir", "lane_label"})


class SettingsError(ValueError):
    """The settings file cannot be used; the message says why."""


@dataclass(frozen=True)
class FileSettings:
    """What the file says. ``dir`` None: the default folder; ``machines`` empty: every machine."""

    machines: tuple[str, ...] = ()
    ceilings: dict[str, int] = field(default_factory=dict)
    dir: Path | None = None
    lane_label: str = DEFAULT_LABEL

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "machines": list(self.machines),
            "ceilings": dict(self.ceilings),
            "dir": None if self.dir is None else str(self.dir),
            "lane_label": self.lane_label,
        }


def default_path(config_path: Path) -> Path:
    """``fleet-writer.json`` beside the bridge configuration file.

    Not inside the published folder ``fleet/``: the publisher refuses a folder that holds a
    file it does not own, and the settings file is the user's, not the writer's.
    """
    return config_path.expanduser().resolve().parent / FILE


def inside(path: Path, folder: Path) -> bool:
    """True when ``path`` is ``folder`` or below it, links and ``..`` resolved.

    The publisher refuses a folder that holds a file it does not own, so a settings file at or
    under the published folder would stop the writer at its start. Both are resolved as far as
    they exist; a missing tail is kept as written.
    """
    base = folder.expanduser().resolve()
    expanded = path.expanduser()
    # Both where the path's entry sits (its parent resolved, its own name kept, so a link
    # inside the folder that points elsewhere still counts as inside) and where it leads.
    entry = expanded.parent.resolve() / expanded.name
    target = expanded.resolve()
    return any(candidate == base or base in candidate.parents for candidate in (entry, target))


def check(machines: Any, ceilings: Any, dir: Any, lane_label: Any) -> FileSettings:
    """The settings as one value, or ``SettingsError`` with the rule that was broken.

    The arguments are checked, not trusted: they come from a file or the command line.
    """
    if not isinstance(machines, list) or not all(bounds.machine_name_ok(m) for m in machines):
        raise SettingsError(
            "machines: a list of names that match ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$, not 'local'"
        )
    unique = tuple(dict.fromkeys(machines))
    if len(unique) > bounds.MACHINES - 1:
        raise SettingsError(
            f"machines: at most {bounds.MACHINES - 1} workers ({bounds.MACHINES} machines with "
            "the coordinator)"
        )
    if not isinstance(ceilings, dict) or not all(
        bounds.machine_name_ok(name)
        and isinstance(n, int)
        and not isinstance(n, bool)
        and CEILING_MIN <= n <= CEILING_MAX
        for name, n in ceilings.items()
    ):
        raise SettingsError(
            f"ceilings: an object of machine name to a whole number from {CEILING_MIN} to "
            f"{CEILING_MAX}"
        )
    if dir is not None and (not isinstance(dir, Path) or not dir.is_absolute()):
        raise SettingsError("dir: an absolute path, or null for the default folder")
    if not label_ok(lane_label):
        raise SettingsError(
            "lane_label: K=V, 1 to 64 letters, digits, '.', '_' or '-' each, the key starting "
            "with a letter or a digit"
        )
    return FileSettings(unique, dict(ceilings), dir, lane_label)


def read(path: Path) -> FileSettings:
    """The settings of ``path``; ``SettingsError`` names the file and the rule for a bad one.

    A symbolic link is followed: the user chose it. What it leads to must be a regular file,
    checked on the open descriptor before any byte is read, so that a FIFO or a device cannot
    hold the writer at its start: the open does not wait for a writer of a FIFO, and the read
    of such a file never starts.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise SettingsError(f"{path} cannot be read: {error.strerror or error}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SettingsError(f"{path} cannot be read: not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1  # the handle owns it now
            data = handle.read(READ_LIMIT + 1)
    except OSError as error:
        raise SettingsError(f"{path} cannot be read: {error.strerror or error}") from None
    finally:
        if fd != -1:
            os.close(fd)
    if len(data) > READ_LIMIT:
        raise SettingsError(f"{path} cannot be read: larger than {READ_LIMIT} bytes")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SettingsError(f"{path} cannot be read: not a JSON document in UTF-8") from None
    try:
        return _from_json(raw)
    except SettingsError as error:
        raise SettingsError(f"{path} cannot be used: {error}") from None


def _from_json(raw: Any) -> FileSettings:
    if not isinstance(raw, dict):
        raise SettingsError("the document is not an object")
    if raw.get("schema") != SCHEMA:
        raise SettingsError(f"schema is not {SCHEMA}")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise SettingsError(f"unknown field {unknown[0]}")
    machines = raw.get("machines", [])
    ceilings = raw.get("ceilings", {})
    folder = raw.get("dir")
    label = raw.get("lane_label", DEFAULT_LABEL)
    if folder is not None and not isinstance(folder, str):
        raise SettingsError("dir: an absolute path, or null for the default folder")
    return check(machines, ceilings, None if folder is None else Path(folder), label)


def write(path: Path, settings: FileSettings) -> None:
    """Write ``settings`` to ``path`` with mode 0600, whole or not at all.

    The folder is made with mode 0700 when it is missing. The text is staged beside the file
    and renamed into place, so a reader sees the old file or the new one, never a part.
    """
    _make_folder(path.parent)
    text = json.dumps(settings.as_json(), indent=2, ensure_ascii=False) + "\n"
    handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _make_folder(folder: Path) -> None:
    """Make ``folder`` with mode 0700, whatever the umask; a folder that exists keeps its mode.

    ``mkdir`` filters its mode through the umask, and an umask that takes an owner bit would
    leave a folder the writer cannot stage a file in. So the mode is set again on the folder
    just made, through a descriptor of that folder and no other.
    """
    try:
        folder.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        if folder.is_dir():
            return
        raise
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
