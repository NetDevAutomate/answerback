"""The on-disk `answerback_bridge` agent that Kiro's v2 engine selects for managed workers.

Kiro CLI 3.5.0's v2 engine ignores the inline `customAgents` metadata that the v3 route
uses and selects an agent only from a file under the worker's HOME:
`~/.kiro/agents/answerback_bridge.json`, named at launch by `--agent answerback_bridge`.
`install_worker_agent` writes that file with the same content `worker_profile()` gives the
v3 route, in the universal agent format both engines load, so the two routes scope a
worker identically: native read, write and shell tools plus the bridge MCP server, no
global MCP servers, no powers.

The write is idempotent and fails closed. It rewrites only when the content or the mode
differs. It never writes any other agent name. Every folder on the way (`.kiro`, `agents`)
is opened without following a link (`O_NOFOLLOW | O_DIRECTORY`) and must be owned by this
user; a symlink or a foreign owner anywhere refuses the step. The file itself is read
through its folder's descriptor, and replaced only by staging a new 0600 file and renaming
it over the entry: the existing inode is never written to or chmod'ed, so nothing it may
share is changed. An entry that is a symlink, a FIFO or any other non-regular file, a file
owned by another user, a file with a second hard link, or an oversized file is refused.
The entry is inspected again on a descriptor opened just before the rename (device and
inode, owner and link count), which refuses a file pre-planted since the first check; what
it cannot refuse is a same-user swap in the instant between that check and the rename
(`docs/NATIVE-CLI.md`, "The Kiro agent file").
"""

import errno
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from ...process import BridgeError

AGENT_NAME = "answerback_bridge"
AGENT_DIRECTORY = Path(".kiro") / "agents"
_FOLDER_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
# O_NONBLOCK so a FIFO put where the file was is refused as not a regular file instead of
# blocking the launch until something writes to it; a regular file ignores the flag.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_LINK_ERRORS = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR})
_SIZE_LIMIT = 1024 * 1024


class KiroAgentError(BridgeError):
    """A refusal to write the worker agent file; the message is for the person."""


def agent_document(profile: dict[str, Any]) -> dict[str, Any]:
    """`profile` (the v3 route's inline `worker_profile()`) in Kiro's on-disk agent format.

    The inline form names the agent by `id` and scopes it with `includeMcpJson` and
    `includePowers`; the file form names it by `name`, scopes it with `useLegacyMcpJson`
    and an empty `mcpServers`, and carries the universal format's `allowedTools` and
    `permissions` keys. The content the probe installed and Kiro 3.5.0 selected.
    """
    if profile.get("id") != AGENT_NAME or profile.get("name", AGENT_NAME) != AGENT_NAME:
        raise KiroAgentError(f"The worker agent file is only ever written as {AGENT_NAME!r}")
    document = {key: value for key, value in profile.items() if key not in ("id", "name")}
    document.pop("includeMcpJson", None)
    document.pop("includePowers", None)
    return {
        "name": AGENT_NAME,
        **document,
        "allowedTools": [],
        "useLegacyMcpJson": False,
        "mcpServers": {},
        "permissions": {"rules": []},
    }


def agent_path(home: Path | None = None) -> Path:
    """Where Kiro reads the worker agent under `home` (the HOME the worker is launched with)."""
    return (home if home is not None else Path.home()) / AGENT_DIRECTORY / f"{AGENT_NAME}.json"


def _open_folder(name: str, dir_fd: int | None, shown: Path, uid: int) -> int:
    """A no-follow descriptor on the folder `name` under `dir_fd` (the path `name` when
    `dir_fd` is None), owned by `uid`. A symlink, anything that is not a folder, or a
    foreign owner is a refusal naming `shown`."""
    try:
        fd = os.open(name, _FOLDER_FLAGS, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in _LINK_ERRORS:
            raise KiroAgentError(f"Refusing to go through a symlink: {shown}") from error
        raise
    try:
        info = os.fstat(fd)
        if info.st_uid != uid:
            raise KiroAgentError(f"Refusing a Kiro agent folder owned by another user: {shown}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_agents(home: Path, uid: int) -> int:
    """A no-follow descriptor on `home/.kiro/agents`, making a missing part (0o700)."""
    fd = _open_folder(str(home), None, home, uid)
    try:
        shown = home
        for part in AGENT_DIRECTORY.parts:
            shown = shown / part
            try:
                child = _open_folder(part, fd, shown, uid)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = _open_folder(part, fd, shown, uid)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_target(dir_fd: int, name: str, shown: Path) -> int | None:
    """A no-follow descriptor on the entry `name` under `dir_fd`; None when there is no
    such entry. A symlink there is a refusal, before anything is read or changed."""
    try:
        return os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in _LINK_ERRORS:
            raise KiroAgentError(f"Refusing to replace a symlink: {shown}") from error
        raise


def _inspect(descriptor: int, shown: Path, uid: int) -> os.stat_result:
    """The stat of the open `descriptor` once it is a regular file owned by `uid` with no
    other name. The checks are made on a descriptor, so they describe the file that was
    opened, not whatever the name resolves to later. A second hard link is refused
    because the entry then also lives under another name, and the bridge does not adopt a
    file it would share (`docs/NATIVE-CLI.md`, "The Kiro agent file")."""
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise KiroAgentError(f"Refusing to replace what is not a regular file: {shown}")
    if info.st_uid != uid:
        raise KiroAgentError(f"Refusing a Kiro agent file owned by another user: {shown}")
    if info.st_nlink != 1:
        raise KiroAgentError(f"Refusing a Kiro agent file that is hard-linked elsewhere: {shown}")
    return info


def _current(dir_fd: int, name: str, shown: Path, uid: int) -> tuple[bytes, os.stat_result] | None:
    """The bytes and stat of the regular file `name` under `dir_fd`, read without following
    a link; None when there is no such entry. Anything else is a refusal. The file is only
    read: its mode is reported, never changed, so a wrong mode is fixed by replacing the
    entry with a new 0600 file, not by touching an inode that may be shared."""
    descriptor = _open_target(dir_fd, name, shown)
    if descriptor is None:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = _inspect(stream.fileno(), shown, uid)
        data = stream.read(_SIZE_LIMIT + 1)
        if len(data) > _SIZE_LIMIT:
            raise KiroAgentError(f"Refusing to replace an oversized Kiro agent file: {shown}")
        return data, info


def _is_current(found: tuple[bytes, os.stat_result] | None, data: bytes) -> bool:
    """Whether `found` (what `_current` read) already is `data` at mode 0600."""
    return found is not None and found[0] == data and stat.S_IMODE(found[1].st_mode) == 0o600


def _replace(
    parent: int,
    staged: str,
    name: str,
    shown: Path,
    uid: int,
    data: bytes,
    seen: os.stat_result | None,
) -> bool:
    """Rename `staged` over `name`, both under `parent`, once the entry at `name` is still
    the file `_current` inspected (same device and inode, still regular, owned by `uid`,
    singly linked) or is still absent. The final check is made on a descriptor opened just
    before the rename, so a file put there since the first check is refused, not
    overwritten. One exception keeps two workers starting at once from failing each
    other: an entry that already reads as `data` at mode 0600 is left in place, and
    False says so."""
    descriptor = _open_target(parent, name, shown)
    try:
        if descriptor is None:
            if seen is not None:
                raise KiroAgentError(f"The Kiro agent file went away while being replaced: {shown}")
        else:
            found = _inspect(descriptor, shown, uid)
            if seen is None or (found.st_dev, found.st_ino) != (seen.st_dev, seen.st_ino):
                if _is_current((os.read(descriptor, _SIZE_LIMIT + 1), found), data):
                    return False
                raise KiroAgentError(f"The Kiro agent file changed while being replaced: {shown}")
        # The rename resolves `name` again: a same-user swap in the instant between the
        # check above and this call is not defended (docs/NATIVE-CLI.md, "The Kiro agent file").
        os.rename(staged, name, src_dir_fd=parent, dst_dir_fd=parent)
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)


def install_worker_agent(profile: dict[str, Any], home: Path | None = None) -> bool:
    """Write `home/.kiro/agents/answerback_bridge.json` from `profile` unless it already
    reads as that content at mode 0600. True when the file was written, False when it was
    current."""
    selected_home = home if home is not None else Path.home()
    target = agent_path(selected_home)
    data = (json.dumps(agent_document(profile), indent=2) + "\n").encode()
    uid = os.getuid()
    parent = _open_agents(selected_home, uid)
    try:
        current = _current(parent, target.name, target, uid)
        if _is_current(current, data):
            return False
        staged = f".{AGENT_NAME}-{secrets.token_hex(8)}"
        descriptor = os.open(staged, _NEW_FILE_FLAGS, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o600)
            replaced = _replace(
                parent,
                staged,
                target.name,
                target,
                uid,
                data,
                None if current is None else current[1],
            )
        finally:
            try:
                os.unlink(staged, dir_fd=parent)
            except FileNotFoundError:
                pass
        written = _current(parent, target.name, target, uid)
        if not _is_current(written, data):
            raise KiroAgentError(f"The Kiro agent file does not read back as written: {target}")
        return replaced
    finally:
        os.close(parent)
