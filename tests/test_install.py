from pathlib import Path

import pytest
import tomlkit

from codex_herdr_bridge.install import install_mcp, uninstall_mcp


def test_install_twice_then_remove_preserves_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = (
        '# Keep this comment\nmodel = "existing-model"\n\n[mcp_servers.other]\ncommand = "other"\n'
    )
    path.write_text(original)
    install_mcp(path, "/path with spaces/codex-herdr", "/bridge/config.json")
    first = path.read_text()
    install_mcp(path, "/path with spaces/codex-herdr", "/bridge/config.json")
    assert path.read_text() == first
    assert tomlkit.parse(first)["mcp_servers"]["other"]["command"] == "other"
    assert "# Keep this comment" in first
    uninstall_mcp(path, "/bridge/config.json")
    remaining = tomlkit.parse(path.read_text())
    assert remaining["model"] == "existing-model"
    assert "codex_herdr_bridge" not in remaining["mcp_servers"]
    assert remaining["mcp_servers"]["other"]["command"] == "other"


def test_existing_unowned_mcp_entry_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '[mcp_servers.codex_herdr_bridge]\ncommand = "someone-else"\n'
    path.write_text(original)
    with pytest.raises(ValueError, match="conflict"):
        install_mcp(path, "/bridge/tool", "/bridge/config.json")
    assert path.read_text() == original


def test_reinstall_preserves_user_tool_restrictions(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    install_mcp(path, "/tool/v1", "/bridge/config.json")
    document = tomlkit.parse(path.read_text())
    document["mcp_servers"]["answerback_bridge"]["disabled_tools"] = ["desktop_submit"]
    path.write_text(tomlkit.dumps(document))
    install_mcp(path, "/tool/v2", "/bridge/config.json")
    entry = tomlkit.parse(path.read_text())["mcp_servers"]["answerback_bridge"]
    assert entry["command"] == "/tool/v2"
    assert entry["disabled_tools"] == ["desktop_submit"]


def test_uninstall_refuses_to_remove_another_installation(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    install_mcp(path, "/tool", "/bridge/config.json")
    original = path.read_text()
    with pytest.raises(ValueError, match="conflict"):
        uninstall_mcp(path, "/other/config.json")
    assert path.read_text() == original


async def test_uninstall_uses_the_codex_home_saved_at_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_herdr_bridge.cli import dispatch, parser
    from codex_herdr_bridge.config import Config

    config_path = tmp_path / "bridge" / "config.json"
    selected_home = tmp_path / "selected-codex"
    other_home = tmp_path / "other-codex"
    monkeypatch.setenv("CODEX_HOME", str(other_home))
    Config("test", codex_home=str(selected_home)).save(config_path)
    install_mcp(selected_home / "config.toml", "/tool", str(config_path))
    await dispatch(parser().parse_args(["--config", str(config_path), "uninstall"]))
    assert "codex_herdr_bridge" not in tomlkit.parse(
        (selected_home / "config.toml").read_text()
    ).get("mcp_servers", {})
    assert not (other_home / "config.toml").exists()


def _observed_backup_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> tuple[list[Path], list[int]]:
    """Run install/uninstall with the widest umask, a disabled chmod, and a probe that
    records the mode of every backup that exists while the source is being read."""
    import os
    import stat

    observed: list[int] = []
    real_read_bytes = Path.read_bytes

    def probe(self: Path) -> bytes:
        for candidate in tmp_path.glob("config.toml.answerback-backup-*"):
            observed.append(stat.S_IMODE(candidate.stat().st_mode))
        return real_read_bytes(self)

    # A later chmod must not be what makes the backup private.
    monkeypatch.setattr(Path, "chmod", lambda self, mode, **kwargs: None)
    monkeypatch.setattr(Path, "read_bytes", probe)
    original_umask = os.umask(0)
    try:
        path = tmp_path / "config.toml"
        path.write_text('[mcp_servers.other]\ncommand = "other"\n')
        source = path.read_bytes()
        install_mcp(path, "/tool", "/bridge/config.json")
        if operation == "uninstall":
            source = path.read_bytes()
            uninstall_mcp(path, "/bridge/config.json")
    finally:
        os.umask(original_umask)
    backups = sorted(tmp_path.glob("config.toml.answerback-backup-*"))
    assert backups, "install/uninstall should have backed up the existing config"
    assert backups[-1].read_bytes() == source
    return backups, observed


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_config_backup_is_private_from_creation_not_a_later_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import stat

    backups, observed = _observed_backup_modes(tmp_path, monkeypatch, operation)
    for backup in backups:
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert observed, "the write path never read the source while a backup existed"
    assert all(mode == 0o600 for mode in observed), observed
