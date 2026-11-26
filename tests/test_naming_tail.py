"""T-144 part C: a new installation writes only the new names; the old ones are still read.

Each test is labelled `red_at_pretip` (it fails at the commit this part started from) or
`pins_existing` (it passes there on purpose and pins behaviour that had no test).
"""

import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tomlkit

from answerback_bridge import live_test
from answerback_bridge.install import install_mcp, uninstall_mcp
from answerback_bridge.process import Output

OLD_OWNER_KEY = "CODEX_HERDR_MANAGED_CONFIG"
NEW_OWNER_KEY = "ANSWERBACK_MANAGED_CONFIG"


# --- C1: the ownership key of a new Codex entry ---
# Input forms: no file, a file with other entries, and an `answerback_bridge` entry whose
# `env` holds only the old key, only the new key, or (after a hand edit) both.


def _entry(path: Path) -> dict:
    return tomlkit.parse(path.read_text())["mcp_servers"]["answerback_bridge"].unwrap()


def _write_entry(path: Path, env: dict[str, str]) -> None:
    # The command is not a known console script, so ownership rests on the env key alone.
    document = tomlkit.document()
    document["mcp_servers"] = {
        "answerback_bridge": {
            "command": "/tool/v1",
            "args": ["--config", "/bridge/config.json", "mcp", "--client", "codex"],
            "tool_timeout_sec": 60,
            "env": env,
        }
    }
    path.write_text(tomlkit.dumps(document))


def test_new_codex_entry_carries_only_the_new_ownership_key(tmp_path: Path) -> None:
    """red_at_pretip: install_mcp wrote CODEX_HERDR_MANAGED_CONFIG into a new entry."""
    path = tmp_path / "config.toml"
    install_mcp(path, "/tool/v1", "/bridge/config.json")
    assert _entry(path)["env"] == {NEW_OWNER_KEY: "/bridge/config.json"}
    assert OLD_OWNER_KEY not in path.read_text()


def test_new_codex_entry_beside_other_entries_carries_only_the_new_key(tmp_path: Path) -> None:
    """red_at_pretip: the same rule when the file already holds another server."""
    path = tmp_path / "config.toml"
    path.write_text('[mcp_servers.other]\ncommand = "other"\n')
    install_mcp(path, "/tool/v1", "/bridge/config.json")
    assert _entry(path)["env"] == {NEW_OWNER_KEY: "/bridge/config.json"}
    assert OLD_OWNER_KEY not in path.read_text()


def test_new_codex_entry_is_owned_by_its_installation(tmp_path: Path) -> None:
    """pins_existing: the entry install_mcp creates is updated and removed by the same
    installation (at the pretip through the old key)."""
    path = tmp_path / "config.toml"
    install_mcp(path, "/tool/v1", "/bridge/config.json")
    env = _entry(path)["env"]
    install_mcp(path, "/tool/v2", "/bridge/config.json")
    assert _entry(path)["command"] == "/tool/v2"
    assert _entry(path)["env"] == env
    uninstall_mcp(path, "/bridge/config.json")
    assert "answerback_bridge" not in tomlkit.parse(path.read_text()).get("mcp_servers", {})


def test_entry_with_only_the_old_key_is_updated_and_its_env_is_left(tmp_path: Path) -> None:
    """pins_existing: an entry made before the rename is still this installation's."""
    path = tmp_path / "config.toml"
    _write_entry(path, {OLD_OWNER_KEY: "/bridge/config.json"})
    install_mcp(path, "/tool/v2", "/bridge/config.json")
    entry = _entry(path)
    assert entry["command"] == "/tool/v2"
    assert entry["args"] == ["--config", "/bridge/config.json", "mcp", "--client", "codex"]
    assert entry["env"] == {OLD_OWNER_KEY: "/bridge/config.json"}
    assert NEW_OWNER_KEY not in path.read_text()


def test_entry_with_only_the_old_key_is_removed_by_uninstall(tmp_path: Path) -> None:
    """pins_existing: uninstall_mcp removes an entry that carries only the old key."""
    path = tmp_path / "config.toml"
    _write_entry(path, {OLD_OWNER_KEY: "/bridge/config.json"})
    uninstall_mcp(path, "/bridge/config.json")
    assert "answerback_bridge" not in tomlkit.parse(path.read_text()).get("mcp_servers", {})


def test_entry_with_only_the_new_key_is_updated_and_removed(tmp_path: Path) -> None:
    """pins_existing: the new key alone already proved ownership before this change."""
    path = tmp_path / "config.toml"
    _write_entry(path, {NEW_OWNER_KEY: "/bridge/config.json"})
    install_mcp(path, "/tool/v2", "/bridge/config.json")
    entry = _entry(path)
    assert entry["command"] == "/tool/v2"
    assert entry["env"] == {NEW_OWNER_KEY: "/bridge/config.json"}
    uninstall_mcp(path, "/bridge/config.json")
    assert "answerback_bridge" not in tomlkit.parse(path.read_text()).get("mcp_servers", {})


# --- C2: the backup of a Codex configuration file ---
# Input form: an existing config.toml that install_mcp or uninstall_mcp changes. Nothing in
# src/ reads a backup by its name, so there is no old name to keep reading.


def test_config_backup_has_the_new_name_and_no_old_one(tmp_path: Path) -> None:
    """red_at_pretip: the backup was named config.toml.codex-herdr-backup-<ns>."""
    path = tmp_path / "config.toml"
    original = '[mcp_servers.other]\ncommand = "other"\n'
    path.write_text(original)
    install_mcp(path, "/tool/v1", "/bridge/config.json")
    installed = path.read_text()
    uninstall_mcp(path, "/bridge/config.json")
    backups = sorted(tmp_path.glob("config.toml.answerback-backup-*"))
    assert [backup.read_text() for backup in backups] == [original, installed]
    for backup in backups:
        assert backup.name.removeprefix("config.toml.answerback-backup-").isdigit()
        assert backup.stat().st_mode & 0o777 == 0o600
    assert sorted(item.name for item in tmp_path.iterdir()) == sorted(
        ["config.toml", *(backup.name for backup in backups)]
    )


# --- C3: the owner field of a live-test environment ---
# Input forms of `owner` in environment.json: the new value, the old value (an environment
# made before this change), another string, a value that is not a string, and no field.

OLD_OWNER = "herdr-bridge-live-test"
NEW_OWNER = "answerback-live-test"


@pytest.fixture
def short_path() -> Iterator[Path]:
    # The bridge socket path under the environment must stay below the socket-name limit.
    with tempfile.TemporaryDirectory(prefix="nt-live-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def _environment(directory: Path, **changes: Any) -> dict[str, Any]:
    """Write the smallest environment.json that _manifest() accepts, with `changes`."""
    directory.mkdir()
    manifest: dict[str, Any] = {
        "schema": 1,
        "owner": NEW_OWNER,
        "session": "bridge-tests",
        "root": str(directory),
        "workspace": str(directory / "workspace"),
        "agent": "test-orchestrator",
    }
    manifest.update(changes)
    (directory / "environment.json").write_text(json.dumps(manifest))
    return manifest


async def test_new_live_test_environment_has_the_new_owner(
    short_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: claim() wrote the owner herdr-bridge-live-test into a new manifest."""
    directory = short_path / "new"

    async def no_sessions(args: list[str], **_: Any) -> Output:
        assert args[3:] == ["session", "list", "--json"]
        return Output(json.dumps({"result": {"sessions": []}}), "")

    monkeypatch.setattr(live_test, "run", no_sessions)
    monkeypatch.setattr(live_test.shutil, "which", lambda name: "/tools/" + name)
    environment = live_test.Environment(directory, time.monotonic() + 30)
    try:
        await environment.claim()
        saved = json.loads((directory / "environment.json").read_text())
        assert saved["owner"] == NEW_OWNER
        assert OLD_OWNER not in (directory / "environment.json").read_text()
        # The environment that was just made is accepted when it is used again.
        assert live_test._manifest(directory)["owner"] == NEW_OWNER
    finally:
        if environment.lock is not None:
            os.close(environment.lock)


def test_manifest_accepts_the_new_owner(short_path: Path) -> None:
    """red_at_pretip: _manifest() required the old owner and refused the new one."""
    directory = short_path / "live"
    manifest = _environment(directory)
    assert live_test._manifest(directory) == manifest


def test_manifest_still_accepts_the_old_owner(short_path: Path) -> None:
    """pins_existing: an environment made before this change is still accepted."""
    directory = short_path / "live"
    manifest = _environment(directory, owner=OLD_OWNER)
    assert live_test._manifest(directory) == manifest


@pytest.mark.parametrize(
    "owner",
    [
        "someone-else",
        "",
        "Answerback-Live-Test",
        "answerback-live-test ",
        "herdr-bridge",
        None,
        1,
        True,
        [NEW_OWNER],
        [OLD_OWNER, NEW_OWNER],
        {"owner": NEW_OWNER},
    ],
)
def test_manifest_refuses_every_other_owner(short_path: Path, owner: Any) -> None:
    """pins_existing: only the two owner values are accepted; a value that is not a string
    is refused with the same error, not with a TypeError."""
    directory = short_path / "live"
    _environment(directory, owner=owner)
    with pytest.raises(live_test.LiveTestFailure, match="Environment ownership"):
        live_test._manifest(directory)


def test_manifest_refuses_a_missing_owner(short_path: Path) -> None:
    """pins_existing: a manifest with no owner field is refused."""
    directory = short_path / "live"
    manifest = _environment(directory)
    del manifest["owner"]
    (directory / "environment.json").write_text(json.dumps(manifest))
    with pytest.raises(live_test.LiveTestFailure, match="Environment ownership"):
        live_test._manifest(directory)


@pytest.mark.parametrize("owner", [OLD_OWNER, NEW_OWNER])
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema": 2}, "Environment ownership"),
        ({"root": "/somewhere/else"}, "Environment ownership"),
        ({"workspace": "/somewhere/else"}, "Environment ownership"),
        ({"agent": "another-agent"}, "Environment ownership"),
        ({"session": "unrelated"}, "dedicated bridge-tests named session"),
        ({"server_launch": "uncertain"}, "Server launch ownership is uncertain"),
        ({"pane_id": "w1:p1"}, "Saved pane identity is incomplete"),
    ],
)
def test_manifest_keeps_its_other_checks_for_both_owners(
    short_path: Path, owner: str, changes: dict[str, Any], message: str
) -> None:
    """pins_existing for the old owner. For the new owner the first four cases pass at
    the pretip too (the owner itself was refused with that message) and the last three
    are red_at_pretip: the later checks were never reached."""
    directory = short_path / "live"
    _environment(directory, owner=owner, **changes)
    with pytest.raises(live_test.LiveTestFailure, match=message):
        live_test._manifest(directory)


# --- Fix round 2 (review X4): an `answerback_bridge` entry of another installation ---
# Input forms: the arguments name another configuration file (with the new or the old
# ownership key), or they name this one and nothing else proves the entry: no ownership
# key, or a key that names another file, and a command that is no console script of the
# project.

THIS_CONFIG = "/bridge/config.json"
OTHER_CONFIG = "/other/config.json"


@pytest.mark.parametrize(
    ("config_argument", "env"),
    [
        pytest.param(OTHER_CONFIG, {NEW_OWNER_KEY: OTHER_CONFIG}, id="other-file-new-key"),
        pytest.param(OTHER_CONFIG, {OLD_OWNER_KEY: OTHER_CONFIG}, id="other-file-old-key"),
        pytest.param(THIS_CONFIG, None, id="this-file-no-key"),
        pytest.param(THIS_CONFIG, {NEW_OWNER_KEY: OTHER_CONFIG}, id="this-file-key-of-another"),
    ],
)
def test_existing_entry_of_another_installation_is_refused_and_left(
    tmp_path: Path, config_argument: str, env: dict[str, str] | None
) -> None:
    """pins_existing: no test failed when install_mcp overwrote such an entry (review X4)."""
    path = tmp_path / "config.toml"
    entry: dict[str, Any] = {
        "command": "/tool/foreign",
        "args": ["--config", config_argument, "mcp", "--client", "codex"],
        "tool_timeout_sec": 60,
    }
    if env is not None:
        entry["env"] = env
    document = tomlkit.document()
    document["mcp_servers"] = {"answerback_bridge": entry}
    path.write_text(tomlkit.dumps(document))
    original = path.read_bytes()

    with pytest.raises(ValueError, match="not owned by this install"):
        install_mcp(path, "/tool/ours", THIS_CONFIG)

    assert path.read_bytes() == original
    # No backup and no lock file either.
    assert [item.name for item in tmp_path.iterdir()] == ["config.toml"]


# --- Fix round 2 (review X6): a backup name that exists ---


def test_second_change_that_needs_the_same_backup_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: no test failed when the backup was opened without O_EXCL (review X6)."""
    # Only the name `time` in install.py is replaced; the module is as it was for other code.
    monkeypatch.setattr("answerback_bridge.install.time", SimpleNamespace(time_ns=lambda: 123))
    path = tmp_path / "config.toml"
    original = b'[mcp_servers.other]\ncommand = "other"\n'
    path.write_bytes(original)
    install_mcp(path, "/tool/v1", THIS_CONFIG)
    backup = tmp_path / "config.toml.answerback-backup-123"
    assert backup.read_bytes() == original
    installed = path.read_bytes()
    assert installed != original

    with pytest.raises(FileExistsError):
        uninstall_mcp(path, THIS_CONFIG)

    assert backup.read_bytes() == original
    assert path.read_bytes() == installed
    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.toml", backup.name]
