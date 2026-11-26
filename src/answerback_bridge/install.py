"""Surgical MCP registration with ownership checks and atomic file replacement."""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import tomlkit

from .config import atomic_write
from .installer_kit import LEGACY_SERVER_NAMES as LEGACY_NAMES
from .installer_kit import SERVER_NAME

# `SERVER_NAME` and `LEGACY_NAMES` have one home, `installer_kit`; re-exported here for the
# CLI and the tests that import them from this module.
# Written into the `env` of an entry that install_mcp creates. Entries made before the
# rename carry CODEX_HERDR_MANAGED_CONFIG; registration._owned_route reads both keys, and
# install_mcp leaves the `env` of an existing entry as it is.
OWNER_KEY = "ANSWERBACK_MANAGED_CONFIG"


@contextmanager
def config_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlinked Codex config file")
    lock_paths = [
        path.with_name(path.name + suffix) for suffix in (".herdr-bridge.lock", ".codex-herdr.lock")
    ]
    acquired: list[Path] = []
    try:
        for lock_path in lock_paths:
            try:
                descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise ValueError(f"Config is locked by another installer: {lock_path}") from None
            os.close(descriptor)
            acquired.append(lock_path)
        yield
    finally:
        for lock_path in reversed(acquired):
            lock_path.unlink()


def owns(entry: Any, bridge_config: str) -> bool:
    from .registration import _owned_route

    return _owned_route(entry, Path(bridge_config))


def replace_checked(path: Path, original: str, updated: str) -> None:
    if original == updated:
        return
    current = path.read_text() if path.exists() else ""
    if current != original:
        raise ValueError("Codex config changed during installation; retry after the other edit")
    if path.exists():
        # Create the backup at mode 0600 before any byte is written so a lower-privileged
        # local reader can never observe it at a wider, umask-derived mode (the window a
        # copy-then-chmod sequence would leave open). O_EXCL refuses an existing name.
        backup = path.with_name(path.name + f".answerback-backup-{time.time_ns()}")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(path.read_bytes())
    atomic_write(path, updated)


def install_mcp(path: Path, executable: str, bridge_config: str) -> None:
    with config_lock(path):
        original = path.read_text() if path.exists() else ""
        document = tomlkit.parse(original)
        if "mcp_servers" not in document:
            document["mcp_servers"] = tomlkit.table()
        servers = document["mcp_servers"]
        from .registration import _route_matches

        for name, entry in servers.items():
            if name in LEGACY_NAMES or (
                name != SERVER_NAME and _route_matches(entry, bridge_config)
            ):
                raise ValueError(
                    "MCP entry conflict: review answerback clients migrate before installing"
                )
        existing = servers.get(SERVER_NAME)
        if existing is not None and not owns(existing, bridge_config):
            raise ValueError(
                "MCP entry conflict: existing answerback_bridge is not owned by this install"
            )
        if existing is None:
            entry = tomlkit.table()
            entry["command"] = executable
            entry["args"] = ["--config", bridge_config, "mcp", "--client", "codex"]
            entry["tool_timeout_sec"] = 60
            entry["env"] = {OWNER_KEY: bridge_config}
            servers[SERVER_NAME] = entry
        else:
            existing["command"] = executable
            existing["args"] = ["--config", bridge_config, "mcp", "--client", "codex"]
        replace_checked(path, original, tomlkit.dumps(document))


def uninstall_mcp(path: Path, bridge_config: str) -> None:
    with config_lock(path):
        if not path.exists():
            return
        original = path.read_text()
        document = tomlkit.parse(original)
        servers = document.get("mcp_servers", {})
        names = [name for name in (SERVER_NAME, *LEGACY_NAMES) if name in servers]
        for name in names:
            if not owns(servers[name], bridge_config):
                raise ValueError("MCP entry conflict: refusing to remove another installation")
        for name in names:
            del servers[name]
        replace_checked(path, original, tomlkit.dumps(document))
