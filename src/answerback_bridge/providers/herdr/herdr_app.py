"""Install the Herdr App side panel (the Answerback fleet column) and the owner's themes.

The wheel ships them under ``answerback_bridge/data/herdr-app/`` with ``manifest.json``: one row
per file with its digest (``sha256:<hex>``), its kind (``mod`` or ``theme``), its licence and its
provenance. The helper files the App writes beside a mod (``APP_HELPERS``) are not shipped and are
never compared. The manifest also names the files of the owner's window that are not shipped
(``dependencies``) and the older panel ``mods/answerback`` that the column supersedes
(``superseded``: its file digests, not its files).

Every command here previews first and writes only with ``apply``. A file the user changed is never
replaced without ``replace``, and then only after a copy of it is made under
``~/.local/state/answerback-bridge/backups/``. Nothing is done when the App's folder is not there.
When that folder, or a folder on the way to it below the home, is a symlink, the whole step is
refused before anything is read. Every write and removal checks again, without following links,
that it stays in the folder that was inspected, and is then made through a descriptor of its own
folder opened without following a link (`O_NOFOLLOW | O_DIRECTORY`, compared by device and inode
with the folder that was inspected), so a folder swapped for a symlink after the check is refused,
not followed. The entry itself: a missing file is given its name with a hard link (never over
anything), a replaced file is compared with the one backed up immediately before the rename,
and a removed folder is renamed to a private name, compared before and after its walk and never
renamed back; each child of it is compared with the listing immediately before it is removed.
Whatever a failure leaves under a private or a staged name is named in the error and under
`retained` (`RetainedError`). The App hot-reloads a mod when one of its files is saved; each
file is replaced in one step.
"""

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from ...process import BridgeError

ENVIRONMENT_KEY = "HERDR_APP_CONFIG"
DEFAULT_DIRECTORY = Path(".config") / "herdr-app"
MANIFEST_SCHEMA = "answerback.herdr-app-assets/1"
# Helper files the App writes beside a mod and regenerates; never compared, never backed up.
APP_HELPERS = frozenset({"gpui-kit.d.ts", "jsconfig.json"})
BACKUP_ROOT = Path(".local/state/answerback-bridge/backups")
# A relative path of the manifest: one to three folders and a file name, each of letters, digits,
# '.', '_' and '-' that starts with a letter, a digit or '_' (so never '.' or '..'), the first
# folder one of the App's (`MANIFEST_FOLDERS`). Matched whole with `fullmatch`: `$` would let a
# final newline through.
_NAME = r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}"
_RELATIVE = re.compile(rf"(?:{_NAME}/){{1,3}}{_NAME}")
MANIFEST_FOLDERS = frozenset({"mods", "themes"})
_SIZE_LIMIT = 4 * 1024 * 1024


class HerdrAppError(BridgeError):
    """A refusal of this installer; the message is for the person."""


class RetainedError(HerdrAppError):
    """A failure that leaves an entry under a name the person did not choose: the private name
    a removed folder was given, or the staged name of a file that was published. `left` is its
    path; the report lists it under `retained` beside the message."""

    def __init__(self, message: str, left: Path) -> None:
        super().__init__(message)
        self.left = left


def digest(data: bytes) -> str:
    """A file's digest as the manifest writes it: ``sha256:`` and the hex of its SHA-256."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Asset:
    path: str
    data: bytes
    digest: str
    kind: str
    written_by: str
    previous: frozenset[str] = frozenset()


def config_directory(
    home: Path | None = None, environ: dict[str, str] | None = None, given: Path | None = None
) -> Path:
    """The App's configuration folder: `given`, else $HERDR_APP_CONFIG, else ~/.config/herdr-app."""
    if given is not None:
        return Path(os.path.abspath(given.expanduser()))
    env = os.environ if environ is None else environ
    value = env.get(ENVIRONMENT_KEY, "")
    if value:
        return Path(os.path.abspath(Path(value).expanduser()))
    return (home if home is not None else Path.home()) / DEFAULT_DIRECTORY


def _assets_root() -> Path:
    root = Path(str(files("answerback_bridge").joinpath("data", "herdr-app")))
    if not (root / "manifest.json").is_file():
        raise HerdrAppError("The Herdr App assets are missing from this installation of answerback")
    return root


def manifest_path(value: object) -> str | None:
    """`value` when it is a path the manifest may name, else None: relative, no `..`, no absolute
    path, no empty or `.` segment, under one of the App's folders (`mods/`, `themes/`)."""
    if not isinstance(value, str) or _RELATIVE.fullmatch(value) is None:
        return None
    return value if value.split("/", 1)[0] in MANIFEST_FOLDERS else None


def _validated(document: dict[str, Any]) -> dict[str, Any]:
    """The manifest once every path in it (`files[].path`, `superseded.path`, each expanded
    `dependencies[].path`) is one the installer may touch; the one place that checks them."""
    for row in document.get("files", []):
        if not isinstance(row, dict) or manifest_path(row.get("path")) is None:
            raise HerdrAppError("The Herdr App asset manifest names a path it may not")
    superseded = document.get("superseded")
    if superseded is not None:
        if not isinstance(superseded, dict) or manifest_path(superseded.get("path")) is None:
            raise HerdrAppError("The Herdr App asset manifest names a path it may not")
    for row in document.get("dependencies", []):
        pattern = row.get("path") if isinstance(row, dict) else None
        if not isinstance(pattern, str) or any(
            manifest_path(path) is None for path in _expand(pattern)
        ):
            raise HerdrAppError("The Herdr App asset manifest names a path it may not")
    return document


def manifest() -> dict[str, Any]:
    root = _assets_root()
    try:
        document = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError) as error:
        raise HerdrAppError("The Herdr App asset manifest cannot be read") from error
    if not isinstance(document, dict) or document.get("schema") != MANIFEST_SCHEMA:
        raise HerdrAppError("The Herdr App asset manifest has an unknown shape")
    return _validated(document)


def shipped() -> tuple[Asset, ...]:
    """The shipped files, each verified against its manifest row before anything is planned."""
    root = _assets_root()
    assets: list[Asset] = []
    for row in manifest().get("files", []):
        path = str(row["path"])
        source = root / path
        try:
            data = source.read_bytes()
        except OSError as error:
            raise HerdrAppError(f"A shipped Herdr App file is missing: {path}") from error
        found = digest(data)
        if found != row.get("digest") or len(data) != row.get("bytes") or len(data) > _SIZE_LIMIT:
            raise HerdrAppError(f"A shipped Herdr App file does not match its manifest: {path}")
        previous = row.get("previous_digests", [])
        assets.append(
            Asset(
                path,
                data,
                found,
                str(row.get("kind", "")),
                str(row.get("written_by", "")),
                frozenset(str(value) for value in previous if isinstance(value, str)),
            )
        )
    if not assets:
        raise HerdrAppError("The Herdr App asset manifest lists no files")
    return tuple(assets)


def _expand(pattern: str) -> list[str]:
    """One level of {a,b} expansion, as the manifest's dependency rows use it."""
    match = re.search(r"\{([^{}]*)\}", pattern)
    if match is None:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    return [head + part + tail for part in match.group(1).split(",")]


def _file_digest(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            data = stream.read(_SIZE_LIMIT + 1)
    except OSError:
        return None
    if len(data) > _SIZE_LIMIT:
        return None
    return digest(data)


Anchor = tuple[int, int]


def _root_check(directory: Path, home: Path) -> Anchor | None:
    """Refuse when the App's folder, or a folder on the way to it from `home` (the home folder
    itself included), is a symlink.

    Returns the folder's identity (device, inode) that every mutation compares against with a
    no-follow `lstat`, or None when the folder is not there. A folder outside `home` is checked
    itself only: the folders above it are the person's choice, and its identity is the anchor.
    """
    checked = [directory]
    if home in directory.parents:
        checked += [parent for parent in directory.parents if home in parent.parents]
        checked.append(home)
    for item in checked:
        if item.is_symlink():
            if item == home:
                raise HerdrAppError(f"Refusing: the home folder is a symlink: {item}")
            raise HerdrAppError(
                f"Refusing: the Herdr App folder or a folder on the way to it is a symlink: {item}"
            )
    try:
        info = os.lstat(directory)
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None


def _linked_between(directory: Path, target: Path) -> bool:
    """True when the target or a folder between it and the App's folder is a symlink."""
    for item in (target, *target.parents):
        if item == directory:
            return False
        if item.is_symlink():
            return True
    return False


def _file_state(target: Path, asset: Asset, directory: Path) -> str:
    """installed, outdated (an earlier shipped version), differs, missing or symlink."""
    if _linked_between(directory, target):
        return "symlink"
    if not target.exists():
        return "missing"
    found = _file_digest(target)
    if found == asset.digest:
        return "installed"
    return "outdated" if found in asset.previous else "differs"


def _superseded_state(folder: Path, known: dict[str, str]) -> str:
    """absent, unchanged (every file of the older panel is a known one), edited, or not-a-folder."""
    if folder.is_symlink():
        return "not-a-folder"
    if not folder.exists():
        return "absent"
    if not folder.is_dir():
        return "not-a-folder"
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return "edited"
    for child in children:
        if child.name in APP_HELPERS:
            continue
        if child.name not in known or _file_digest(child) != known[child.name]:
            return "edited"
    return "unchanged"


def _places(document: object, mod: str) -> bool:
    """True when some object in `document` has the key `mod` with the value `mod`'s name."""
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("mod") == mod:
                return True
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return False


def _layouts_naming(directory: Path, mod: str) -> list[str]:
    """The layout files of the App's folder that place `mod` in a block, read as JSON and walked
    whole (so any shape, and any JSON escaping of the key or the name, counts); one that cannot
    be read or parsed counts too: the panel is kept and the file is named."""
    found: list[str] = []
    folder = directory / "layouts"
    if not folder.is_dir():
        return found
    for layout in sorted(folder.glob("*.json")):
        try:
            with layout.open("rb") as stream:
                data = stream.read(_SIZE_LIMIT + 1)
            if len(data) > _SIZE_LIMIT:
                raise ValueError("too large to read")
            document = json.loads(data)
        except (OSError, ValueError, RecursionError):
            found.append(layout.name)
            continue
        if _places(document, mod):
            found.append(layout.name)
    return found


def inspect(directory: Path, *, home: Path | None = None) -> dict[str, Any]:
    """What is in the App's folder, per shipped file, dependency and the superseded panel."""
    _root_check(directory, home if home is not None else Path.home())
    assets = shipped()
    document = manifest()
    report: dict[str, Any] = {"directory": str(directory), "present": directory.is_dir()}
    if not report["present"]:
        report["note"] = (
            "The Herdr App's configuration folder is not here; nothing is installed or removed"
        )
        return report
    report["files"] = [
        {
            "path": asset.path,
            "kind": asset.kind,
            "written_by": asset.written_by,
            "state": _file_state(directory / asset.path, asset, directory),
        }
        for asset in assets
    ]
    dependencies: list[dict[str, Any]] = []
    for row in document.get("dependencies", []):
        for path in _expand(str(row.get("path", ""))):
            target = directory / path
            dependencies.append(
                {
                    "path": path,
                    "kind": row.get("kind"),
                    "state": "present" if target.exists() else "missing",
                    "provenance": row.get("provenance"),
                }
            )
    report["dependencies"] = dependencies
    superseded = document.get("superseded")
    if isinstance(superseded, dict) and isinstance(superseded.get("files"), dict):
        folder = directory / str(superseded["path"])
        _contained(folder, directory)
        report["superseded"] = {
            "path": superseded["path"],
            "state": _superseded_state(folder, dict(superseded["files"])),
            "layouts": _layouts_naming(directory, folder.name),
            "note": superseded.get("note"),
        }
    return report


def _actions(report: dict[str, Any], replace: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in report["files"]:
        state = row["state"]
        if state == "missing":
            action = "install"
        elif state == "outdated":
            action = "update"
        elif state == "differs":
            action = "replace" if replace else "keep"
        elif state == "symlink":
            action = "refuse"
        else:
            action = "unchanged"
        actions.append({**row, "action": action})
    return actions


@dataclass(frozen=True)
class Backup:
    """The backup folder of one step and the identity of its `content/`, which every copy into
    it compares against."""

    folder: Path
    content: Anchor


def _backup_folder(home: Path, directory: Path) -> Backup:
    """Make `~/.local/state/answerback-bridge/backups/herdr-app.herdr-bridge-backup-<random>/`
    with `content/` and `recovery.json` in it, every part through a no-follow descriptor of its
    parent: the home folder being a symlink, or any folder from it to the new one, refuses."""
    if home.is_symlink():
        raise HerdrAppError(f"Refusing: the home folder is a symlink: {home}")
    root = _open_below(home, BACKUP_ROOT, None, create=True)
    try:
        os.fchmod(root, 0o700)
        for _ in range(16):
            name = f"herdr-app.herdr-bridge-backup-{secrets.token_hex(6)}"
            try:
                os.mkdir(name, 0o700, dir_fd=root)
                break
            except FileExistsError:
                continue
        else:
            raise HerdrAppError(f"No new backup folder could be made in {home / BACKUP_ROOT}")
        backup = home / BACKUP_ROOT / name
        folder = _open_folder(name, root, backup)
    finally:
        os.close(root)
    try:
        os.fchmod(folder, 0o700)
        os.mkdir("content", 0o700, dir_fd=folder)
        content = _open_folder("content", folder, backup / "content")
        try:
            os.fchmod(content, 0o700)
            info = os.fstat(content)
        finally:
            os.close(content)
        record = os.open("recovery.json", _NEW_FILE_FLAGS, 0o600, dir_fd=folder)
        with os.fdopen(record, "w") as stream:
            stream.write(json.dumps({"schemaVersion": 1, "directory": str(directory)}) + "\n")
            os.fchmod(stream.fileno(), 0o600)
    finally:
        os.close(folder)
    return Backup(backup, (info.st_dev, info.st_ino))


def _contained(target: Path, directory: Path, anchor: Anchor | None = None) -> Path:
    """`target` relative to `directory`, once it is below it lexically (no `..`, no absolute
    segment), with no symlink between them (no-follow `lstat` of each folder on the way) and
    resolved (no symlink takes it out); with `anchor`, `directory` must still be the folder that
    was inspected. Else a refusal.

    This is the check. It names a path, so a folder swapped for a symlink after it would be
    followed by a mutation that names a path too; every mutation therefore goes through a
    descriptor from `_open_below`, never through the path again.
    """
    try:
        relative = target.relative_to(directory)
    except ValueError:
        relative = None
    if (
        relative is None
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise HerdrAppError(f"Refusing a path outside the Herdr App folder: {target}")
    for item in (target, *target.parents):
        if item == directory:
            break
        try:
            if stat.S_ISLNK(os.lstat(item).st_mode):
                raise HerdrAppError(f"Refusing to go through a symlink: {item}")
        except OSError:
            continue
    if anchor is not None:
        try:
            info = os.lstat(directory)
        except OSError as error:
            raise HerdrAppError(f"The Herdr App folder went away: {directory}") from error
        if (info.st_dev, info.st_ino) != anchor or not stat.S_ISDIR(info.st_mode):
            raise HerdrAppError(f"The Herdr App folder changed during the step: {directory}")
    resolved_root = os.path.realpath(directory)
    resolved = os.path.realpath(target)
    if os.path.commonpath([resolved_root, resolved]) != resolved_root:
        raise HerdrAppError(f"Refusing a path outside the Herdr App folder: {target}")
    return relative


# ---------------------------------------------------------------- mutations, by descriptor
#
# A pathname names whatever is at the path when the kernel resolves it, so a check on a path and
# a write to the same path leave a gap a symlink can be put into. Below, a folder is opened once
# with O_NOFOLLOW | O_DIRECTORY, its identity compared with the one that was inspected, and
# every further step (`mkdir`, `open`, `rename`, `unlink`, `rmdir`, `scandir`) names a child of
# that open descriptor (`dir_fd=`), so a link put in place after the open is not followed. The
# `*at` forms used here exist on every platform this package runs on (macOS, Linux); `os.replace`
# has no `dir_fd` form, and `os.rename` with both descriptors is the same `renameat` on POSIX.
#
# The entry itself: a rename replaces whatever is at the target name when the kernel runs it, so
# a check on the name and the rename leave an instant too (T-188). A file that was missing is
# published with `link` (`linkat`), which makes the name only if nothing is there and fails with
# EEXIST otherwise: no clobber, in one step. A file that is replaced is held open without
# following a link and compared by device and inode with the one that was backed up immediately
# before the rename, which narrows the instant to the one `renameat` itself leaves. A folder that
# is removed is first renamed to a private name in its own parent and compared, through the open
# descriptor, with the folder that was inspected, so the walk and the final `rmdir` name the
# folder that was opened and not whatever is at the old name by then. Nothing is renamed back to
# the old name, which may have been taken again: what a failure leaves under the private name,
# or under a staged name, stays there and is named (`RetainedError`).

_FOLDER_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
# O_NONBLOCK so a FIFO put where a file was is opened and refused as not a regular file, instead
# of blocking the step until something writes to it; a regular file ignores the flag.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# What `open` with O_NOFOLLOW | O_DIRECTORY says of an entry that is not a folder: ELOOP for a
# symlink (Linux; macOS says ENOTDIR for it too), ENOTDIR for a file. The entry is then looked at
# without following it to say which; EMLINK is what some kernels say for a link loop.
_NOT_A_FOLDER_ERRORS = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR})
# What `link` says on a filesystem that has no hard links (Linux: EPERM on vfat and the like;
# ENOTSUP or EOPNOTSUPP elsewhere). Then, and only then, a missing file is published by the
# check-then-rename of T-181, which leaves the instant open; EEXIST is never one of these.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP})
_TOMBSTONE = ".answerback-remove-"


def _open_folder(name: str, dir_fd: int | None, shown: Path) -> int:
    """A no-follow descriptor on the folder `name` under `dir_fd` (the path `name` when `dir_fd`
    is None). A symlink there is a refusal naming `shown` as one; anything else that is not a
    folder is a refusal naming it as not a folder."""
    try:
        return os.open(name, _FOLDER_FLAGS, dir_fd=dir_fd)
    except OSError as error:
        if error.errno not in _NOT_A_FOLDER_ERRORS:
            raise
        try:
            found = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            found = None
        if found is None or stat.S_ISLNK(found.st_mode):
            raise HerdrAppError(f"Refusing to go through a symlink: {shown}") from error
        raise HerdrAppError(f"Refusing: not a folder: {shown}") from error


def _open_below(
    directory: Path, relative: Path, anchor: Anchor | None, *, create: bool = False
) -> int:
    """A no-follow descriptor on the folder `directory/relative`, which the caller closes.

    `directory` is opened without following a link and, with `anchor`, must still be the folder
    that was inspected (`fstat` device and inode). Each part of `relative` is then opened from
    its parent's descriptor with `O_NOFOLLOW | O_DIRECTORY`, so a symlink put in its place after
    the check is refused, not followed. With `create`, a part that is not there is made (0o700)
    from the parent's descriptor.
    """
    fd = _open_folder(str(directory), None, directory)
    try:
        if anchor is not None:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != anchor:
                raise HerdrAppError(f"The Herdr App folder changed during the step: {directory}")
        shown = directory
        for part in relative.parts:
            shown = shown / part
            try:
                child = _open_folder(part, fd, shown)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = _open_folder(part, fd, shown)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _digest_at(dir_fd: int, name: str) -> str | None:
    """The digest of the regular file `name` under `dir_fd`, read without following a link."""
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        data = stream.read(_SIZE_LIMIT + 1)
    return None if len(data) > _SIZE_LIMIT else digest(data)


def _identity(info: os.stat_result) -> Anchor:
    return (info.st_dev, info.st_ino)


def _copy_file_at(source: int, destination: int, name: str, shown: Path) -> Anchor:
    """Copy the regular file `name` under `source` to a new 0o600 file `name` under
    `destination`; neither side follows a link. Returns the identity of the file copied."""
    with os.fdopen(os.open(name, _READ_FLAGS, dir_fd=source), "rb") as reader:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise HerdrAppError(f"Refusing to back up what is not a regular file: {shown}")
        writer = os.open(name, _NEW_FILE_FLAGS, 0o600, dir_fd=destination)
        with os.fdopen(writer, "wb") as stream:
            shutil.copyfileobj(reader, stream)
            os.fchmod(stream.fileno(), 0o600)
    return _identity(info)


def _copy_tree_at(source: int, destination: int, name: str, shown: Path) -> Anchor:
    """Copy the folder `name` under `source` to a new 0o700 folder `name` under `destination`:
    files as files (0o600), folders by recursion, symlinks as symlinks (never followed).
    Returns the identity of the folder copied."""
    folder = _open_folder(name, source, shown)
    try:
        identity = _identity(os.fstat(folder))
        os.mkdir(name, 0o700, dir_fd=destination)
        copy = _open_folder(name, destination, shown)
        try:
            os.fchmod(copy, 0o700)
            with os.scandir(folder) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
            for entry in children:
                if entry.is_symlink():
                    os.symlink(os.readlink(entry.name, dir_fd=folder), entry.name, dir_fd=copy)
                elif entry.is_dir(follow_symlinks=False):
                    _copy_tree_at(folder, copy, entry.name, shown / entry.name)
                elif entry.is_file(follow_symlinks=False):
                    _copy_file_at(folder, copy, entry.name, shown / entry.name)
                else:
                    raise HerdrAppError(
                        f"Refusing to back up what is not a file, a folder or a symlink: "
                        f"{shown / entry.name}"
                    )
        finally:
            os.close(copy)
    finally:
        os.close(folder)
    return identity


def _remove_tree_at(parent: int, name: str, shown: Path, expected: Anchor | None) -> None:
    """Remove the folder `name` under `parent` and everything in it.

    The folder is opened without following a link and, with `expected`, must be the folder that
    was inspected (device and inode). It is then renamed to a private name in `parent`
    (`.answerback-remove-<random>`) and that name compared with the open descriptor, so what
    is walked and finally removed is the folder that was opened, whatever is put at `name`
    meanwhile. Each child is named from its own folder's descriptor and compared with the entry
    the listing saw (`_empty_at`); a symlink is unlinked, never followed. A mismatch anywhere
    stops the removal and names what is left and where.

    Once the rename has been made nothing is renamed back: whatever is at the private name
    stays there, and every failure after that point (a mismatch, or an `OSError` from the walk,
    an `unlink`, a `stat` or the `rmdir`) is a `RetainedError` naming that private path, so the
    report says where the person's data is. The rename itself replaces an empty folder that
    happens to be at the private name already (a random one; `renameat2(RENAME_NOREPLACE)` is
    not in `os`), which the comparison after it cannot see: the held folder is what is there.
    """
    folder = _open_folder(name, parent, shown)
    try:
        held = _identity(os.fstat(folder))
        if expected is not None and held != expected:
            raise HerdrAppError(f"{shown} changed during removal; retry")
        tombstone = _TOMBSTONE + secrets.token_hex(8)
        os.rename(name, tombstone, src_dir_fd=parent, dst_dir_fd=parent)
    except BaseException:
        os.close(folder)
        raise
    left = shown.parent / tombstone
    try:
        try:
            moved = os.stat(tombstone, dir_fd=parent, follow_symlinks=False)
            if _identity(moved) != held:
                # Something else was at `name` in the instant of the rename. It is left at the
                # private name, never renamed back: the old name may have been taken again by
                # now, and a rename over that would lose it.
                raise RetainedError(
                    f"{shown} changed during removal; what was at that name is now {left}", left
                )
            _empty_at(folder, held[0], shown)
        finally:
            os.close(folder)
        if _identity(os.stat(tombstone, dir_fd=parent, follow_symlinks=False)) != held:
            raise HerdrAppError(f"{shown} changed during removal")
        os.rmdir(tombstone, dir_fd=parent)
    except RetainedError:
        raise
    except HerdrAppError as error:
        raise RetainedError(f"{error}; what is left of {shown} is at {left}", left) from error
    except OSError as error:
        raise RetainedError(
            f"The Herdr App folder could not be written ({error.strerror}); what is left of "
            f"{shown} is at {left}",
            left,
        ) from error


def _shape(info: os.stat_result) -> tuple[int, int, int]:
    """What tells one entry from another without following it: device, inode and type."""
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _empty_at(folder: int, device: int, shown: Path) -> None:
    """Remove everything in the open folder `folder`, each child named from it. Every child's
    identity (device, inode, type) is taken without following it while the folder is listed. A
    file or a symlink is compared with that immediately before it is unlinked (never followed);
    the instant between that comparison and the `unlink` is the one that stays open. A child
    folder is opened without following a link, compared with the entry the listing saw
    (`device` and its inode), emptied the same way, compared again and removed by name from
    `folder`. A child that changed stops the removal and is named."""
    with os.scandir(folder) as entries:
        children = [
            (entry.name, os.stat(entry.name, dir_fd=folder, follow_symlinks=False))
            for entry in entries
        ]
    for name, listed in children:
        child = shown / name
        if not stat.S_ISDIR(listed.st_mode):
            now = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if _shape(now) != _shape(listed):
                raise HerdrAppError(f"{child} changed during removal")
            os.unlink(name, dir_fd=folder)
            continue
        seen = (device, listed.st_ino)
        descriptor = _open_folder(name, folder, child)
        try:
            if _identity(os.fstat(descriptor)) != seen:
                raise HerdrAppError(f"{child} changed during removal")
            _empty_at(descriptor, device, child)
        finally:
            os.close(descriptor)
        if _identity(os.stat(name, dir_fd=folder, follow_symlinks=False)) != seen:
            raise HerdrAppError(f"{child} changed during removal")
        os.rmdir(name, dir_fd=folder)


def _back_up(target: Path, directory: Path, backup: Backup, anchor: Anchor | None) -> Anchor:
    """Copy `target` into the backup; returns the identity of the file or folder copied, which
    the write or removal that follows compares against."""
    relative = _contained(target, directory, anchor)
    content = backup.folder / "content"
    _contained(content / relative, content, backup.content)
    source = _open_below(directory, relative.parent, anchor)
    try:
        destination = _open_below(content, relative.parent, backup.content, create=True)
        try:
            info = os.stat(relative.name, dir_fd=source, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                return _copy_tree_at(source, destination, relative.name, target)
            return _copy_file_at(source, destination, relative.name, target)
        finally:
            os.close(destination)
    finally:
        os.close(source)


def _publish_new(parent: int, staged: str, name: str, shown: Path) -> None:
    """Give the staged file the name `name` only if nothing is there: `link` makes a second name
    for it or fails with EEXIST, in one step (the caller unlinks the staged name). A filesystem
    with no hard links falls back to a no-follow look at the name and a rename, which leaves the
    instant between the two open; that is the T-181 behaviour and the only case of it."""
    try:
        os.link(staged, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        return
    except FileExistsError as error:
        raise HerdrAppError(f"{shown} appeared during installation; retry") from error
    except OSError as error:
        if error.errno not in _NO_HARD_LINKS:
            raise
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(staged, name, src_dir_fd=parent, dst_dir_fd=parent)
        return
    raise HerdrAppError(f"{shown} appeared during installation; retry")


def _replace(parent: int, staged: str, name: str, shown: Path, expected: Anchor) -> None:
    """Rename the staged file over `name`, which must be the regular file `expected` (the one
    that was backed up) when it is held open, without following a link, immediately before. The
    instant between that look and the rename is the one `renameat` leaves open: nothing short
    of an exchanging rename (Linux `renameat2(RENAME_EXCHANGE)`, macOS `renamex_np(RENAME_SWAP)`,
    neither in `os`) can close it, so it is stated in the docs rather than hidden."""
    try:
        held = os.open(name, _READ_FLAGS, dir_fd=parent)
    except FileNotFoundError as error:
        raise HerdrAppError(f"{shown} changed during installation; retry") from error
    except OSError as error:
        if error.errno in _NOT_A_FOLDER_ERRORS:
            raise HerdrAppError(f"Refusing to replace a symlink: {shown}") from error
        raise
    try:
        before = os.fstat(held)
        if not stat.S_ISREG(before.st_mode) or _identity(before) != expected:
            raise HerdrAppError(f"{shown} changed during installation; retry")
        os.rename(staged, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        os.close(held)


def _verify_at(parent: int, name: str, data: bytes, shown: Path) -> None:
    """The regular file `name` under `parent` reads back as `data` and is mode 0600."""
    if _digest_at(parent, name) != digest(data):
        raise HerdrAppError(f"The installed file does not read back as written: {shown}")
    mode = stat.S_IMODE(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)
    if mode != 0o600:
        raise HerdrAppError(f"The installed file is mode {mode:04o}, not 0600: {shown}")


def _write(
    target: Path, data: bytes, directory: Path, anchor: Anchor | None, expected: Anchor | None
) -> None:
    """Write `data` to `target` in one step: to a staged name in its folder, then to its name.
    With `expected` None the file must be missing still (no clobber); else it must be the file
    `expected`, the one that was backed up.

    The staged name is unlinked afterwards: after `link` it is a second name of the installed
    file, after `rename` it is gone already. When that unlink fails after the file was
    published, the file is verified at its name (contents and mode) and the failure is a
    `RetainedError` saying so: installed, with a second name at the staged path. When it fails
    after the publication itself failed, the error names the staged file that is left.
    """
    relative = _contained(target, directory, anchor)
    parent = _open_below(directory, relative.parent, anchor, create=True)
    try:
        staged = f".answerback-{secrets.token_hex(8)}"
        left = target.parent / staged
        descriptor = os.open(staged, _NEW_FILE_FLAGS, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o600)
            if expected is None:
                _publish_new(parent, staged, relative.name, target)
            else:
                _replace(parent, staged, relative.name, target, expected)
        except BaseException as error:
            second = _unlink_staged(parent, staged)
            if second is not None and isinstance(error, (OSError, HerdrAppError)):
                said = (
                    str(error)
                    if isinstance(error, HerdrAppError)
                    else f"The Herdr App folder could not be written ({error.strerror})"
                )
                raise RetainedError(f"{said}; a staged file remains at {left}", left) from second
            raise
        second = _unlink_staged(parent, staged)
        try:
            _verify_at(parent, relative.name, data, target)
        except HerdrAppError as error:
            if second is None:
                raise
            raise RetainedError(f"{error}; a staged file remains at {left}", left) from error
        if second is not None:
            raise RetainedError(
                f"{target} installed; a second name remains at {left}", left
            ) from second
    finally:
        os.close(parent)


def _unlink_staged(parent: int, staged: str) -> OSError | None:
    """Unlink the staged name under `parent`; gone already is fine. Returns the error of a
    failure that leaves the name there instead of raising it, so the caller says what the
    failure leaves behind."""
    try:
        os.unlink(staged, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        try:
            os.stat(staged, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            pass
        return error
    return None


def _unlink(target: Path, directory: Path, anchor: Anchor | None) -> None:
    """Remove the regular file `target`, named from its folder's descriptor."""
    relative = _contained(target, directory, anchor)
    parent = _open_below(directory, relative.parent, anchor)
    try:
        info = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise HerdrAppError(f"{relative.as_posix()} changed during removal; retry")
        os.unlink(relative.name, dir_fd=parent)
    finally:
        os.close(parent)


def _remove_tree(folder: Path, directory: Path, anchor: Anchor | None, expected: Anchor) -> None:
    """Remove the folder `folder`, which must be the folder `expected` (the one that was backed
    up), and its contents, named from its parent's descriptor."""
    relative = _contained(folder, directory, anchor)
    parent = _open_below(directory, relative.parent, anchor)
    try:
        _remove_tree_at(parent, relative.name, folder, expected)
    finally:
        os.close(parent)


def install(
    directory: Path, *, apply: bool = False, replace: bool = False, home: Path | None = None
) -> dict[str, Any]:
    """Preview, or with `apply` do, the installation into the App's folder."""
    selected_home = home if home is not None else Path.home()
    anchor = _root_check(directory, selected_home)
    report = inspect(directory, home=selected_home)
    report.update(applied=apply, success=True, backup=None, notes=[])
    if not report["present"]:
        return report
    actions = _actions(report, replace)
    report["files"] = actions
    notes: list[str] = report["notes"]
    superseded = report.get("superseded")
    remove_older = (
        superseded is not None and superseded["state"] == "unchanged" and not superseded["layouts"]
    )
    if superseded is not None:
        superseded["action"] = "remove" if remove_older else "none"
        if superseded["state"] == "unchanged" and superseded["layouts"]:
            notes.append(
                f"{superseded['path']}: the older panel is kept, because a layout places it "
                f"({', '.join(superseded['layouts'])}); point that block at answerback-fleet, "
                "then run this again"
            )
        if superseded["state"] == "edited":
            notes.append(
                f"{superseded['path']}: the older panel is here with changes of yours; it is left "
                "as it is. Remove it by hand once the fleet column shows."
            )
    for row in actions:
        if row["action"] == "keep":
            notes.append(
                f"{row['path']}: differs from the shipped file and is kept; --replace backs it up "
                "and installs the shipped one"
            )
        elif row["action"] == "refuse":
            report["success"] = False
            notes.append(f"{row['path']}: is a symlink; nothing is written there")
    for row in report["dependencies"]:
        if row["state"] == "missing":
            notes.append(
                f"{row['path']}: not shipped by answerback and not here ({row['provenance']})"
            )
    if not apply or not report["success"]:
        return report
    backup: Backup | None = None
    assets = {asset.path: asset for asset in shipped()}
    try:
        needs_backup = remove_older or any(
            row["action"] in ("replace", "update") for row in actions
        )
        if needs_backup:
            backup = _backup_folder(selected_home, directory)
            report["backup"] = str(backup.folder)
        for row in actions:
            target = directory / row["path"]
            asset = assets[row["path"]]
            if row["action"] in ("replace", "update"):
                if _file_state(target, asset, directory) != row["state"]:
                    raise HerdrAppError(f"{row['path']} changed during installation; retry")
                assert backup is not None
                copied = _back_up(target, directory, backup, anchor)
                _write(target, asset.data, directory, anchor, copied)
                row["action"] = "replaced" if row["action"] == "replace" else "updated"
            elif row["action"] == "install":
                if target.exists() or target.is_symlink():
                    raise HerdrAppError(f"{row['path']} appeared during installation; retry")
                _write(target, asset.data, directory, anchor, None)
                row["action"] = "installed"
        if remove_older and superseded is not None:
            folder = directory / str(superseded["path"])
            known = dict(manifest()["superseded"]["files"])
            if _superseded_state(folder, known) != "unchanged" or _layouts_naming(
                directory, folder.name
            ):
                raise HerdrAppError(f"{superseded['path']} changed during installation; retry")
            assert backup is not None
            copied = _back_up(folder, directory, backup, anchor)
            _remove_tree(folder, directory, anchor, copied)
            superseded["action"] = "removed"
            notes.append(
                f"{superseded['path']}: the older panel, unchanged since it was installed, was "
                f"removed; a copy is at {backup.folder}/content/{superseded['path']}"
            )
    except (OSError, HerdrAppError) as error:
        report["success"] = False
        report["error"] = (
            str(error)
            if isinstance(error, HerdrAppError)
            else ("The Herdr App folder could not be written")
        )
        if isinstance(error, RetainedError):
            _retain(report, error.left, directory)
    return report


def _retain(report: dict[str, Any], left: Path, directory: Path) -> None:
    """List `left` under the report's `retained`, by its folder relative to the App's folder,
    the way uninstall lists what it keeps: `{"mods": [".answerback-remove-<random>"]}`."""
    relative = left.relative_to(directory)
    folder = relative.parent.as_posix()
    retained: dict[str, list[str]] = report.setdefault("retained", {})
    names = retained.setdefault(folder, [])
    if relative.name not in names:
        names.append(relative.name)


def install_step(directory: Path) -> dict[str, Any]:
    """The step `answerback install` runs: apply, and turn a refusal into the report's error.

    The bridge itself is installed by then, so a refusal here must not hide that.
    """
    try:
        return install(directory, apply=True)
    except HerdrAppError as error:
        return {"directory": str(directory), "applied": True, "success": False, "error": str(error)}


def uninstall_step(directory: Path) -> dict[str, Any]:
    """The step `answerback uninstall` runs, after the bridge's own entries are removed."""
    try:
        return uninstall(directory, apply=True)
    except HerdrAppError as error:
        return {"directory": str(directory), "applied": True, "success": False, "error": str(error)}


def uninstall(directory: Path, *, apply: bool = False, home: Path | None = None) -> dict[str, Any]:
    """Remove the panel's files that are as shipped; keep and name every file that differs.

    Themes are kept: a layout of the person's may name one, and the App falls back to its own
    theme without saying so when it is gone. They are listed with the action `keep`. The mod's
    folder and every file in it that the manifest does not own (the App's helper files among
    them) are kept too and listed under `retained`.
    """
    selected_home = home if home is not None else Path.home()
    anchor = _root_check(directory, selected_home)
    report = inspect(directory, home=selected_home)
    report.update(applied=apply, success=True, notes=[])
    if not report["present"]:
        return report
    notes: list[str] = report["notes"]
    themes = 0
    for row in report["files"]:
        state = row["state"]
        if row["kind"] == "theme":
            row["action"] = "keep"
            themes += state == "installed"
        elif state in ("installed", "outdated"):
            row["action"] = "remove"
        elif state == "differs":
            row["action"] = "keep"
            notes.append(f"{row['path']}: differs from the shipped file and is kept")
        else:
            row["action"] = "none"
    if themes:
        notes.append(
            f"themes: {themes} shipped theme file(s) are kept, as a layout may name them; "
            "delete them by hand if no layout does"
        )
    if not apply:
        return report
    assets = {asset.path: asset for asset in shipped()}
    try:
        for row in report["files"]:
            if row["action"] == "remove":
                target = directory / row["path"]
                asset = assets[row["path"]]
                if _file_state(target, asset, directory) != row["state"]:
                    raise HerdrAppError(f"{row['path']} changed during removal; retry")
                _unlink(target, directory, anchor)
                row["action"] = "removed"
        # Only files the manifest owns are removed. The folder stays, and so does whatever else
        # is in it: the App's helper files (never shipped, maybe edited) and files of the person's.
        folders = {row["path"].rsplit("/", 1)[0] for row in report["files"] if row["kind"] == "mod"}
        for folder_name in sorted(folders):
            folder = directory / folder_name
            if not folder.is_dir() or folder.is_symlink():
                continue
            left = sorted(child.name for child in folder.iterdir())
            report.setdefault("retained", {})[folder_name] = left
            if left:
                notes.append(
                    f"{folder_name}: the folder is kept with {len(left)} file(s) not shipped by "
                    f"answerback ({', '.join(left)}); remove it by hand if you want it gone"
                )
            else:
                notes.append(f"{folder_name}: the folder is kept, empty; remove it by hand")
    except (OSError, HerdrAppError) as error:
        report["success"] = False
        report["error"] = (
            str(error)
            if isinstance(error, HerdrAppError)
            else ("The Herdr App folder could not be changed")
        )
    return report
