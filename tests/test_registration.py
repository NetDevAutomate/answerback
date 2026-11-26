"""Registration contracts exercised against temporary client homes."""

import errno
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from codex_herdr_bridge.registration import install_clients


async def test_preview_is_read_only_and_apply_preserves_codex_comments(tmp_path: Path) -> None:
    config = tmp_path / "bridge config" / "config.json"
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    original = '# Keep me\nmodel = "example"\n\n[mcp_servers.other]\ncommand = "other"\n'
    path.write_text(original)
    executable = "/tool with spaces/herdr-bridge"

    preview = await install_clients(config, ["codex"], home=tmp_path, executable=executable)
    assert preview["success"]
    assert preview["clients"][0]["would_change"]
    assert path.read_text() == original
    assert sorted(p.name for p in path.parent.iterdir()) == ["config.toml"]

    result = await install_clients(
        config, ["codex"], home=tmp_path, executable=executable, apply=True
    )
    assert result["success"]
    written = path.read_text()
    assert "# Keep me" in written
    document = tomlkit.parse(written)
    assert document["model"] == "example"
    assert document["mcp_servers"]["other"]["command"] == "other"
    assert document["mcp_servers"]["answerback_bridge"]["command"] == executable
    assert document["mcp_servers"]["answerback_bridge"]["args"] == [
        "--config",
        str(config),
        "mcp",
        "--client",
        "codex",
    ]
    backups = list(path.parent.glob("config.toml.herdr-bridge-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600

    again = await install_clients(
        config, ["codex"], home=tmp_path, executable=executable, apply=True
    )
    assert again["clients"][0]["status"] == "unchanged"
    assert path.read_text() == written
    assert len(list(path.parent.glob("config.toml.herdr-bridge-backup-*"))) == 1


async def test_json_clients_share_kiro_entry_and_preserve_other_settings(tmp_path: Path) -> None:
    import json

    claude = tmp_path / ".claude.json"
    claude.write_text(
        '{"preferences":{"theme":"dark"},"mcpServers":{"other":{"url":"https://example.test"}}}'
    )
    result = await install_clients(
        tmp_path / "bridge.json",
        ["claude", "grok", "kiro", "crew"],
        apply=True,
        home=tmp_path,
        executable="/local/herdr-bridge",
    )
    assert result["success"]
    document = json.loads(claude.read_text())
    assert document["preferences"] == {"theme": "dark"}
    assert document["mcpServers"]["other"] == {"url": "https://example.test"}
    assert document["mcpServers"]["answerback_bridge"]["args"][-3:] == [
        "mcp",
        "--client",
        "claude",
    ]
    kiro = tmp_path / ".kiro/settings/mcp.json"
    assert (
        json.loads(kiro.read_text())["mcpServers"]["answerback_bridge"]["command"]
        == "/local/herdr-bridge"
    )
    assert not list(kiro.parent.glob("*.herdr-bridge-backup-*"))
    crew = next(report for report in result["clients"] if report["client"] == "crew")
    assert "inheritance" in crew["note"]
    assert "verification" in crew["note"]
    assert tomlkit.parse((tmp_path / ".grok/config.toml").read_text())["mcp_servers"][
        "answerback_bridge"
    ]


async def test_invalid_json_preflight_prevents_all_file_changes(tmp_path: Path) -> None:
    claude = tmp_path / ".claude.json"
    claude.write_text('{"token":"secret-do-not-print", broken')
    result = await install_clients(
        tmp_path / "bridge.json",
        ["codex", "claude"],
        apply=True,
        home=tmp_path,
        executable="/local/herdr-bridge",
    )
    assert not result["success"]
    assert not (tmp_path / ".codex").exists()
    assert "secret-do-not-print" not in str(result)
    assert result["clients"][0]["status"] == "blocked"


async def test_existing_name_conflict_needs_replace_and_preserves_controls(tmp_path: Path) -> None:
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir()
    original = (
        '[mcp_servers.answerback_bridge]\ncommand = "/old/bridge"\n'
        'args = ["--config", "/old/config.json", "mcp"]\n'
        'disabled_tools = ["peer_submit"]\n'
    )
    path.write_text(original)
    refused = await install_clients(
        tmp_path / "new.json", ["codex"], home=tmp_path, executable="/new/bridge", apply=True
    )
    assert not refused["success"]
    assert path.read_text() == original
    replaced = await install_clients(
        tmp_path / "new.json",
        ["codex"],
        home=tmp_path,
        executable="/new/bridge",
        apply=True,
        replace=True,
    )
    assert replaced["success"]
    entry = tomlkit.parse(path.read_text())["mcp_servers"]["answerback_bridge"]
    assert entry["disabled_tools"] == ["peer_submit"]
    assert entry["args"] == [
        "--config",
        str(tmp_path / "new.json"),
        "mcp",
        "--client",
        "codex",
    ]


async def test_duplicate_route_under_another_name_is_refused(tmp_path: Path) -> None:
    import json

    config = tmp_path / "bridge.json"
    path = tmp_path / ".claude.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "old_bridge": {
                        "command": "/old/bridge",
                        "args": ["--config", str(config), "mcp"],
                    }
                }
            }
        )
    )
    original = path.read_bytes()
    result = await install_clients(
        config, ["claude"], home=tmp_path, executable="/new/bridge", apply=True, replace=True
    )
    assert not result["success"]
    assert path.read_bytes() == original


async def test_skill_install_copies_whole_bundle_and_links_selected_harnesses(
    tmp_path: Path,
) -> None:
    from codex_herdr_bridge.registration import install_skill

    source = tmp_path / "bundle/answerback-collaboration"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("# Collaboration\n")
    (source / "references/bridge.md").write_text("Bridge reference\n")
    home = tmp_path / "home"
    preview = await install_skill(["codex", "grok", "crew"], home=home, source=source)
    assert preview["success"]
    assert not home.exists()
    result = await install_skill(["codex", "grok", "crew"], home=home, source=source, apply=True)
    assert result["success"]
    shared = home / ".agents/skills/answerback-collaboration"
    assert not shared.is_symlink()
    assert (shared / "references/bridge.md").read_text() == "Bridge reference\n"
    for harness in ("codex", "grok", "kiro"):
        link = home / f".{harness}/skills/answerback-collaboration"
        assert link.is_symlink()
        assert link.resolve() == shared
    assert not (home / ".claude").exists()
    again = await install_skill(["codex", "grok", "crew"], home=home, source=source, apply=True)
    assert again["success"]
    assert all(report["status"] == "unchanged" for report in again["targets"])


async def test_existing_shared_link_to_source_is_preserved(tmp_path: Path) -> None:
    from codex_herdr_bridge.registration import install_skill

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# Skill")
    shared = tmp_path / "home/.agents/skills/answerback-collaboration"
    shared.parent.mkdir(parents=True)
    shared.symlink_to(source, target_is_directory=True)
    result = await install_skill(["claude"], home=tmp_path / "home", source=source, apply=True)
    assert result["success"]
    assert shared.is_symlink()
    assert shared.resolve() == source
    assert not list(shared.parent.glob("*.herdr-bridge-backup-*"))


async def test_skill_conflict_blocks_batch_until_explicit_replacement(tmp_path: Path) -> None:
    from codex_herdr_bridge.registration import install_skill

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# New skill")
    home = tmp_path / "home"
    target = home / ".grok/skills/answerback-collaboration"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# Old skill")
    refused = await install_skill(["codex", "grok"], home=home, source=source, apply=True)
    assert not refused["success"]
    assert not (home / ".agents").exists()
    assert (target / "SKILL.md").read_text() == "# Old skill"
    replaced = await install_skill(
        ["codex", "grok"], home=home, source=source, apply=True, replace=True
    )
    assert replaced["success"]
    assert target.is_symlink()
    backups = list(
        (home / ".local/state/answerback-bridge/backups").glob(
            "answerback-collaboration.herdr-bridge-backup-*/content/SKILL.md"
        )
    )
    assert len(backups) == 1
    assert backups[0].read_text() == "# Old skill"
    assert backups[0].stat().st_mode & 0o777 == 0o600


async def test_config_symlink_is_refused_without_touching_referent(tmp_path: Path) -> None:
    other = tmp_path / "other.toml"
    other.write_text('# private settings\nmodel = "existing"\n')
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.symlink_to(other)
    result = await install_clients(
        tmp_path / "bridge.json",
        ["codex"],
        home=tmp_path,
        executable="/bridge",
        apply=True,
        replace=True,
    )
    assert not result["success"]
    assert other.read_text() == '# private settings\nmodel = "existing"\n'
    assert config.is_symlink()


async def test_config_changed_during_backup_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('model = "original"\n')
    original_fsync = os.fsync
    calls = 0

    def change_after_backup(descriptor: int) -> None:
        nonlocal calls
        original_fsync(descriptor)
        calls += 1
        if calls == 2:
            config.write_text('model = "concurrent editor"\n')

    monkeypatch.setattr("codex_herdr_bridge.registration.os.fsync", change_after_backup)
    result = await install_clients(
        tmp_path / "bridge.json", ["codex"], home=tmp_path, executable="/bridge", apply=True
    )
    assert not result["success"]
    assert config.read_text() == 'model = "concurrent editor"\n'


async def test_codex_home_is_respected_without_affecting_explicit_test_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(configured))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = await install_clients(
        tmp_path / "bridge.json", ["codex"], executable="/bridge", apply=True
    )
    assert result["success"]
    assert (configured / "config.toml").exists()
    assert not (tmp_path / ".codex").exists()
    isolated = tmp_path / "isolated"
    result = await install_clients(
        tmp_path / "bridge.json", ["codex"], home=isolated, executable="/bridge", apply=True
    )
    assert result["success"]
    assert (isolated / ".codex/config.toml").exists()


@pytest.mark.parametrize(
    "contents", ['{"mcpServers":[],"token":"secret"}', '{"mcpServers":{},"mcpServers":{}}']
)
async def test_ambiguous_json_is_refused(tmp_path: Path, contents: str) -> None:
    path = tmp_path / ".claude.json"
    path.write_text(contents)
    result = await install_clients(
        tmp_path / "bridge.json", ["claude"], home=tmp_path, executable="/bridge", apply=True
    )
    assert not result["success"]
    assert path.read_text() == contents
    assert "secret" not in str(result)


@pytest.mark.parametrize("name", ["", "a.b", "../escape", "name\nwith-control"])
async def test_invalid_server_names_do_not_write(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="Server name"):
        await install_clients(
            tmp_path / "bridge.json",
            ["codex"],
            name=name,
            home=tmp_path,
            executable="/bridge",
            apply=True,
        )
    assert not (tmp_path / ".codex").exists()


async def test_skill_replaces_link_itself_without_writing_external_tree(tmp_path: Path) -> None:
    from codex_herdr_bridge.registration import install_skill

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# New")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "SKILL.md").write_text("# Unrelated")
    home = tmp_path / "home"
    target = home / ".claude/skills/answerback-collaboration"
    target.parent.mkdir(parents=True)
    target.symlink_to(unrelated)
    result = await install_skill(["claude"], home=home, source=source, apply=True, replace=True)
    assert result["success"]
    assert (unrelated / "SKILL.md").read_text() == "# Unrelated"
    assert target.resolve() == home / ".agents/skills/answerback-collaboration"
    backups = list(
        (home / ".local/state/answerback-bridge/backups").glob(
            "answerback-collaboration.herdr-bridge-backup-*/content"
        )
    )
    assert len(backups) == 1
    assert backups[0].is_symlink()
    assert backups[0].resolve() == unrelated


async def test_existing_installer_lock_preserves_configuration(tmp_path: Path) -> None:
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('model = "current"\n')
    lock = path.with_name("config.toml.herdr-bridge.lock")
    lock.write_text("other installer")
    result = await install_clients(
        tmp_path / "bridge.json", ["codex"], home=tmp_path, executable="/bridge", apply=True
    )
    assert not result["success"]
    assert "locked" in result["clients"][0]["error"]
    assert path.read_text() == 'model = "current"\n'
    assert lock.read_text() == "other installer"


async def test_symlinked_client_parent_is_refused_for_config_and_skill(tmp_path: Path) -> None:
    from codex_herdr_bridge.registration import install_skill

    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").symlink_to(outside)
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# Skill")
    config_result = await install_clients(
        tmp_path / "bridge.json", ["codex"], home=home, executable="/bridge", apply=True
    )
    skill_result = await install_skill(
        ["codex"], home=home, source=source, apply=True, replace=True
    )
    assert not config_result["success"]
    assert not skill_result["success"]
    assert not (outside / "config.toml").exists()
    assert not (outside / "skills").exists()
    assert not (home / ".agents").exists()


@pytest.mark.parametrize("invocation", ["herdr-bridge", "codex-herdr", "module"])
async def test_client_cli_uses_its_own_launcher_despite_shadowed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invocation: str
) -> None:
    import json
    import sys

    from codex_herdr_bridge.cli import dispatch, parser

    installed = tmp_path / "installed/bin"
    installed.mkdir(parents=True)
    bridge = installed / "herdr-bridge"
    bridge.write_text("#!/bin/sh\n")
    bridge.chmod(0o700)
    alias = installed / "codex-herdr"
    alias.symlink_to(bridge)
    shadow = tmp_path / "project/.venv/bin"
    shadow.mkdir(parents=True)
    (shadow / "herdr-bridge").write_text("#!/bin/sh\n")
    (shadow / "herdr-bridge").chmod(0o700)
    monkeypatch.setenv("PATH", str(shadow))
    monkeypatch.setattr(sys, "executable", str(installed / "python"))
    launcher = (
        tmp_path / "package/__main__.py" if invocation == "module" else installed / invocation
    )
    monkeypatch.setattr(sys, "argv", [str(launcher)])
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    args = parser().parse_args(
        [
            "--config",
            str(tmp_path / "config.json"),
            "clients",
            "install",
            "--client",
            "claude",
            "--apply",
        ]
    )
    result = await dispatch(args)
    assert result["success"]
    entry = json.loads((home / ".claude.json").read_text())["mcpServers"]["answerback_bridge"]
    assert entry["command"] == str(bridge)


async def test_client_default_launcher_does_not_fall_back_to_another_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setattr(sys, "argv", [str(tmp_path / "caller.py")])
    monkeypatch.setattr(sys, "executable", str(tmp_path / "missing/python"))
    monkeypatch.setattr(
        "codex_herdr_bridge.registration.shutil.which", lambda name: "/other/herdr-bridge"
    )
    with pytest.raises(ValueError, match="executable"):
        await install_clients(tmp_path / "config.json", ["claude"], home=tmp_path, apply=True)
    assert not (tmp_path / ".claude.json").exists()


@pytest.mark.parametrize("existing", [False, True])
async def test_skill_install_supports_a_harness_root_linked_to_shared_skills(
    tmp_path: Path, existing: bool
) -> None:
    from codex_herdr_bridge.registration import install_skill

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# Current skill")
    home = tmp_path / "home"
    shared_root = home / ".agents/skills"
    shared_root.mkdir(parents=True)
    shared = shared_root / "answerback-collaboration"
    if existing:
        shared.symlink_to(source, target_is_directory=True)
    claude_root = home / ".claude/skills"
    claude_root.parent.mkdir()
    claude_root.symlink_to(shared_root, target_is_directory=True)

    preview = await install_skill(["claude"], home=home, source=source)
    assert preview["success"]
    assert shared.exists() == existing
    result = await install_skill(["claude"], home=home, source=source, apply=True)
    assert result["success"]
    assert claude_root.is_symlink()
    assert (claude_root / "answerback-collaboration/SKILL.md").read_text() == "# Current skill"
    assert shared.resolve() == (source if existing else shared)
    assert all(
        row["status"] == ("unchanged" if existing else "installed") for row in result["targets"]
    )
    again = await install_skill(["claude"], home=home, source=source, apply=True)
    assert again["success"]
    assert all(row["status"] == "unchanged" for row in again["targets"])


async def test_skill_update_deduplicates_an_aliased_harness_root(tmp_path: Path) -> None:
    from codex_herdr_bridge.registration import install_skill

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# New skill")
    home = tmp_path / "home"
    shared = home / ".agents/skills/answerback-collaboration"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("# Old skill")
    claude_root = home / ".claude/skills"
    claude_root.parent.mkdir()
    claude_root.symlink_to(shared.parent, target_is_directory=True)

    result = await install_skill(["claude"], home=home, source=source, replace=True, apply=True)
    assert result["success"]
    assert (shared / "SKILL.md").read_text() == "# New skill"
    assert all(row["status"] == "installed" for row in result["targets"])
    backups = list(
        (home / ".local/state/answerback-bridge/backups").glob(
            "answerback-collaboration.herdr-bridge-backup-*/content/SKILL.md"
        )
    )
    assert len(backups) == 1
    assert backups[0].read_text() == "# Old skill"


# T-134, ruling A01: a client link that goes around a shared entry that is being replaced.
# The forms are the ones the installers write: `fleet.py` links the shared entry and, in harness
# mode, a client entry straight to a checkout; `install_skill` links a client entry to the
# shared path.


def _a01_home(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    old = tmp_path / "old-checkout/answerback-collaboration"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("# Old skill")
    source = tmp_path / "bundle/answerback-collaboration"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# New skill")
    home = tmp_path / "home"
    shared = home / ".agents/skills/answerback-collaboration"
    client = home / ".claude/skills/answerback-collaboration"
    shared.parent.mkdir(parents=True)
    client.parent.mkdir(parents=True)
    return old, source, home, shared, client


async def test_a_client_link_that_bypasses_a_replaced_shared_entry_is_replaced(
    tmp_path: Path,
) -> None:
    """red_at_pretip: both entries are separate links to the same old folder."""
    from codex_herdr_bridge.registration import install_skill

    old, source, home, shared, client = _a01_home(tmp_path)
    shared.symlink_to(old, target_is_directory=True)
    client.symlink_to(old, target_is_directory=True)
    result = await install_skill(["claude"], home=home, source=source, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["installed", "installed"]
    assert not shared.is_symlink() and (shared / "SKILL.md").read_text() == "# New skill"
    assert client.is_symlink() and client.resolve() == shared
    assert (client / "SKILL.md").read_text() == "# New skill"
    backups = list(
        (home / ".local/state/answerback-bridge/backups").glob(
            "answerback-collaboration.herdr-bridge-backup-*/content"
        )
    )
    assert len(backups) == 2 and all(backup.is_symlink() for backup in backups)
    assert (old / "SKILL.md").read_text() == "# Old skill"


async def test_a_client_link_to_the_shared_entry_follows_its_replacement(tmp_path: Path) -> None:
    """pins_existing: a link that points at the shared entry itself is left alone."""
    from codex_herdr_bridge.registration import install_skill

    old, source, home, shared, client = _a01_home(tmp_path)
    shared.symlink_to(old, target_is_directory=True)
    client.symlink_to(shared, target_is_directory=True)
    result = await install_skill(["claude"], home=home, source=source, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["installed", "unchanged"]
    assert client.is_symlink() and client.resolve() == shared
    assert (client / "SKILL.md").read_text() == "# New skill"


async def test_a_client_link_by_another_way_is_unchanged_when_shared_is_not_replaced(
    tmp_path: Path,
) -> None:
    """pins_existing: without a replacement of the shared entry nothing changes."""
    from codex_herdr_bridge.registration import install_skill

    _, source, home, shared, client = _a01_home(tmp_path)
    shared.symlink_to(source, target_is_directory=True)
    via = tmp_path / "via"
    via.symlink_to(source, target_is_directory=True)
    client.symlink_to(via, target_is_directory=True)
    for replace in (False, True):
        result = await install_skill(
            ["claude"], home=home, source=source, apply=True, replace=replace
        )
        assert result["success"]
        assert [report["status"] for report in result["targets"]] == ["unchanged", "unchanged"]
        assert os.readlink(client) == str(via)
    assert not (home / ".local/state/answerback-bridge/backups").exists()


# T-134, D6 to D9: `install_skill` with no source installs the collaboration skill and the setup
# skill. The setup skill's source is this checkout's `skills/answerback-fleet`, the folder the
# wheel packages; the other folders are forms that `fleet.py` or a person leaves in a home.

FLEET_SKILL = Path(__file__).resolve().parents[1] / "skills/answerback-fleet"
SETUP_FILES = [
    ".answerback-fleet.json",
    "SKILL.md",
    "references/agents.md",
    "references/fleet.md",
    "references/harnesses.md",
    "scripts/fleet.py",
]
SETUP_CONFLICT = "Skill destination conflict; review it before using --replace"


def _setup_reports(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [report for report in result["targets"] if report.get("skill") == "answerback-fleet"]


def _files(folder: Path) -> list[str]:
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())


def _state(entry: Path) -> Any:
    """What is at an entry, so that a test can say it was not touched."""
    if entry.is_symlink():
        return ("link", os.readlink(entry))
    if entry.is_dir():
        return [(name, (entry / name).read_bytes()) for name in _files(entry)]
    return entry.read_bytes()


def _other_setup_skill(folder: Path) -> Path:
    """A usable setup skill with other content: the two files D7 asks for."""
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: answerback-fleet\ndescription: old\n---\nold\n")
    (folder / "scripts/fleet.py").write_text("print('old')\n")
    return folder


def _kept_link(tmp_path: Path, shared: Path) -> None:
    # fleet.py at user scope on a coordinator: the shared entry is a link to a checkout.
    other = _other_setup_skill(tmp_path / "checkout/skills/answerback-fleet")
    shared.symlink_to(other, target_is_directory=True)


def _kept_old_copy(tmp_path: Path, shared: Path) -> None:
    # The copy that fleet.py 0.1.0 wrote on the LAN machines: a manifest with a hash field.
    _other_setup_skill(shared)
    (shared / ".answerback-fleet.json").write_text(
        json.dumps({"skill": "answerback-fleet", "version": "0.1.0", "hash": "0" * 64}) + "\n"
    )


def _kept_bare_copy(tmp_path: Path, shared: Path) -> None:
    _other_setup_skill(shared)


def _kept_stray_file(tmp_path: Path, shared: Path) -> None:
    shutil.copytree(FLEET_SKILL, shared)
    (shared / "scripts/notes.txt").write_text("a stray file\n")


def _conflict_dangling(tmp_path: Path, shared: Path) -> None:
    shared.symlink_to(tmp_path / "gone", target_is_directory=True)


def _conflict_other_link(tmp_path: Path, shared: Path) -> None:
    other = tmp_path / "something-else"
    other.mkdir()
    (other / "README.md").write_text("not a skill\n")
    shared.symlink_to(other, target_is_directory=True)


def _conflict_manifest_only(tmp_path: Path, shared: Path) -> None:
    shared.mkdir()
    (shared / ".answerback-fleet.json").write_text(
        json.dumps({"skill": "answerback-fleet", "version": "0.3.0"}) + "\n"
    )


def _conflict_file(tmp_path: Path, shared: Path) -> None:
    shared.write_text("a file\n")


def _setup_backups(home: Path) -> list[Path]:
    return list(
        (home / ".local/state/answerback-bridge/backups").glob(
            "answerback-fleet.herdr-bridge-backup-*/content"
        )
    )


async def test_skill_install_without_a_source_installs_both_skills(tmp_path: Path) -> None:
    """red_at_pretip (D6): the collaboration skill and then the setup skill."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    preview = await install_skill(["claude", "codex"], home=home)
    assert preview["success"] and not preview["applied"]
    assert [(report["skill"], report["client"]) for report in preview["targets"]] == [
        ("answerback-collaboration", "shared"),
        ("answerback-collaboration", "claude"),
        ("answerback-collaboration", "codex"),
        ("answerback-fleet", "shared"),
        ("answerback-fleet", "claude"),
        ("answerback-fleet", "codex"),
    ]
    assert all(report["status"] == "planned" for report in preview["targets"])
    assert not home.exists()
    result = await install_skill(["claude", "codex"], home=home, apply=True)
    assert result["success"]
    assert all(report["status"] == "installed" for report in result["targets"])
    assert (home / ".agents/skills/answerback-collaboration/SKILL.md").is_file()
    shared = home / ".agents/skills/answerback-fleet"
    assert not shared.is_symlink() and _files(shared) == SETUP_FILES
    assert (shared / "scripts/fleet.py").stat().st_mode & 0o100
    assert _state(shared) == _state(FLEET_SKILL)
    for harness in ("claude", "codex"):
        link = home / f".{harness}/skills/answerback-fleet"
        assert link.is_symlink() and link.resolve() == shared.resolve()
    again = await install_skill(["claude", "codex"], home=home, apply=True)
    assert again["success"] and len(again["targets"]) == 6
    assert all(report["status"] == "unchanged" for report in again["targets"])


async def test_skill_install_with_a_source_leaves_the_setup_skill_out(tmp_path: Path) -> None:
    """pins_existing (D6): an explicit source is the collaboration skill and nothing else."""
    from codex_herdr_bridge.registration import install_skill

    source = tmp_path / "bundle/answerback-collaboration"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# Collaboration\n")
    home = tmp_path / "home"
    result = await install_skill(["claude"], home=home, source=source, apply=True)
    assert result["success"]
    assert [report["client"] for report in result["targets"]] == ["shared", "claude"]
    assert not any("answerback-fleet" in report["path"] for report in result["targets"])
    assert not (home / ".agents/skills/answerback-fleet").exists()
    assert not (home / ".claude/skills/answerback-fleet").exists()


async def test_skill_install_result_names_the_skill_and_the_sources(tmp_path: Path) -> None:
    """red_at_pretip (D9): `skill` in every report, `sources`, and the order of the reports."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    legacy = home / ".agents/skills/herdr-collaboration"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# The full skill under its former name\n")
    result = await install_skill(["claude"], home=home, replace=True)
    assert result["success"]
    assert [(report["skill"], report["client"]) for report in result["targets"]] == [
        ("answerback-collaboration", "shared"),
        ("answerback-collaboration", "claude"),
        ("herdr-collaboration", "shared"),
        ("answerback-fleet", "shared"),
        ("answerback-fleet", "claude"),
    ]
    repo = FLEET_SKILL.parent
    assert result["sources"] == {
        "answerback-collaboration": str(repo / "answerback-collaboration"),
        "answerback-fleet": str(FLEET_SKILL),
    }
    assert result["source"] == result["sources"]["answerback-collaboration"]
    source = tmp_path / "bundle/answerback-collaboration"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# Collaboration\n")
    given = await install_skill(["claude"], home=tmp_path / "other-home", source=source)
    assert given["source"] == str(source)
    assert given["sources"] == {"answerback-collaboration": str(source)}
    assert {report["skill"] for report in given["targets"]} == {"answerback-collaboration"}


@pytest.mark.parametrize("arrange", [_kept_link, _kept_old_copy, _kept_bare_copy, _kept_stray_file])
async def test_a_usable_setup_skill_that_is_not_equal_is_kept(tmp_path: Path, arrange: Any) -> None:
    """red_at_pretip (D7 as replaced): it does not block the collaboration skill."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    shared = home / ".agents/skills/answerback-fleet"
    shared.parent.mkdir(parents=True)
    arrange(tmp_path, shared)
    before = _state(shared)
    result = await install_skill(["claude"], home=home, apply=True)
    assert result["success"]
    report = _setup_reports(result)[0]
    assert (report["client"], report["status"]) == ("shared", "kept")
    assert report["ready"] and not report["would_change"]
    assert "--replace" in report["note"]
    assert _state(shared) == before
    assert not _setup_backups(home)
    assert (home / ".agents/skills/answerback-collaboration/SKILL.md").is_file()
    link = home / ".claude/skills/answerback-fleet"
    assert link.is_symlink() and link.resolve() == shared.resolve()
    again = await install_skill(["claude"], home=home, apply=True)
    assert [report["status"] for report in _setup_reports(again)] == ["kept", "unchanged"]


@pytest.mark.parametrize("arrange", [_kept_link, _kept_old_copy, _kept_bare_copy, _kept_stray_file])
async def test_a_kept_setup_skill_is_backed_up_and_replaced_with_replace(
    tmp_path: Path, arrange: Any
) -> None:
    """red_at_pretip (D7 as replaced): `--replace` installs the packaged copy."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    shared = home / ".agents/skills/answerback-fleet"
    shared.parent.mkdir(parents=True)
    arrange(tmp_path, shared)
    before = _state(shared)
    result = await install_skill(["claude"], home=home, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in _setup_reports(result)] == ["installed", "installed"]
    assert not shared.is_symlink() and _state(shared) == _state(FLEET_SKILL)
    backups = _setup_backups(home)
    assert len(backups) == 1 and _state(backups[0]) == before


@pytest.mark.parametrize(
    "arrange",
    [_conflict_dangling, _conflict_other_link, _conflict_manifest_only, _conflict_file],
)
async def test_something_that_is_not_a_setup_skill_is_a_conflict(
    tmp_path: Path, arrange: Any
) -> None:
    """red_at_pretip (D7 as replaced): refused, and nothing at all is installed."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    shared = home / ".agents/skills/answerback-fleet"
    shared.parent.mkdir(parents=True)
    arrange(tmp_path, shared)
    before = _state(shared)
    result = await install_skill(["claude"], home=home, apply=True)
    assert not result["success"]
    report = _setup_reports(result)[0]
    assert (report["status"], report["error"]) == ("refused", SETUP_CONFLICT)
    assert not report["ready"]
    assert _state(shared) == before
    assert not (home / ".agents/skills/answerback-collaboration").exists()
    assert not (home / ".claude").exists()
    assert not (home / ".local").exists()


@pytest.mark.parametrize(
    "arrange",
    [_conflict_dangling, _conflict_other_link, _conflict_manifest_only, _conflict_file],
)
async def test_a_setup_skill_conflict_is_backed_up_and_replaced_with_replace(
    tmp_path: Path, arrange: Any
) -> None:
    """red_at_pretip (D7 as replaced): as for every other destination."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    shared = home / ".agents/skills/answerback-fleet"
    shared.parent.mkdir(parents=True)
    arrange(tmp_path, shared)
    result = await install_skill(["claude"], home=home, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in _setup_reports(result)] == ["installed", "installed"]
    assert not shared.is_symlink() and _state(shared) == _state(FLEET_SKILL)
    assert len(_setup_backups(home)) == 1
    assert (home / ".agents/skills/answerback-collaboration/SKILL.md").is_file()


async def test_both_skills_are_planned_before_the_first_write(tmp_path: Path) -> None:
    """red_at_pretip (D6): a conflict at a client's setup entry leaves everything uninstalled."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    entry = home / ".claude/skills/answerback-fleet"
    entry.parent.mkdir(parents=True)
    entry.write_text("a file\n")
    result = await install_skill(["claude"], home=home, apply=True)
    assert not result["success"]
    statuses = {
        (report["skill"], report["client"]): report["status"] for report in result["targets"]
    }
    assert statuses == {
        ("answerback-collaboration", "shared"): "blocked",
        ("answerback-collaboration", "claude"): "blocked",
        ("answerback-fleet", "shared"): "blocked",
        ("answerback-fleet", "claude"): "refused",
    }
    assert not (home / ".agents").exists()
    assert not (home / ".claude/skills/answerback-collaboration").exists()
    assert entry.read_text() == "a file\n"


async def test_a_client_directory_linked_to_the_shared_one_gets_the_setup_skill_once(
    tmp_path: Path,
) -> None:
    """red_at_pretip (D6): the rule for a linked skills directory holds for the setup skill."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    (home / ".agents/skills").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude/skills").symlink_to(home / ".agents/skills", target_is_directory=True)
    result = await install_skill(["claude"], home=home, apply=True)
    assert result["success"]
    shared = home / ".agents/skills/answerback-fleet"
    reports = _setup_reports(result)
    assert [report["status"] for report in reports] == ["installed", "installed"]
    assert reports[1]["installation_path"] == str(shared)
    assert (home / ".claude/skills").is_symlink()
    assert not shared.is_symlink() and _files(shared) == SETUP_FILES
    assert not (home / ".local").exists()


# ------------------------------------------------------- fix round 1: R1-02, the client link


async def test_a_client_link_through_a_linked_folder_and_dot_dot_is_replaced(
    tmp_path: Path,
) -> None:
    """red_at_pretip (R1-02): `alias/..` is where the file system says, not where the text says."""
    from codex_herdr_bridge.registration import install_skill

    old, source, home, shared, client = _a01_home(tmp_path)
    elsewhere = tmp_path / "alternate/.agents/skills/answerback-collaboration"
    elsewhere.parent.mkdir(parents=True)
    (tmp_path / "alternate/child").mkdir()
    elsewhere.symlink_to(old, target_is_directory=True)
    (home / "alias").symlink_to(tmp_path / "alternate/child", target_is_directory=True)
    shared.symlink_to(old, target_is_directory=True)
    # As text this is the shared entry; on disk `alias/..` is the folder `alternate`.
    client.symlink_to("../../alias/../.agents/skills/answerback-collaboration")
    assert client.resolve() == shared.resolve() == old
    result = await install_skill(["claude"], home=home, source=source, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["installed", "installed"]
    assert not shared.is_symlink() and (shared / "SKILL.md").read_text() == "# New skill"
    assert client.is_symlink() and client.resolve() == shared
    assert (client / "SKILL.md").read_text() == "# New skill"
    backups = list(
        (home / ".local/state/answerback-bridge/backups").glob(
            "answerback-collaboration.herdr-bridge-backup-*/content"
        )
    )
    texts = sorted(os.readlink(backup) for backup in backups)
    assert texts == sorted(["../../alias/../.agents/skills/answerback-collaboration", str(old)])
    assert (old / "SKILL.md").read_text() == "# Old skill"


async def test_a_client_link_that_leaves_the_shared_entry_during_the_run_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (A01): the check after the writes, with a stand-in and no other fault."""
    from codex_herdr_bridge import registration

    home = tmp_path / "home"
    assert (await registration.install_skill(["claude"], home=home, apply=True))["success"]
    setup_shared = home / ".agents/skills/answerback-fleet"
    setup_client = home / ".claude/skills/answerback-fleet"
    client = home / ".claude/skills/answerback-collaboration"
    shutil.rmtree(setup_shared)
    setup_client.unlink()
    elsewhere = tmp_path / "elsewhere/answerback-collaboration"
    elsewhere.mkdir(parents=True)
    (elsewhere / "SKILL.md").write_text("# Another skill")
    real = registration._apply_skill_file
    written: list[Path] = []

    def writer(plan: Any) -> None:
        real(plan)
        written.append(plan.target)
        if plan.target == setup_client:  # the last write of this run
            client.unlink()
            client.symlink_to(elsewhere, target_is_directory=True)

    monkeypatch.setattr("codex_herdr_bridge.registration._apply_skill_file", writer)
    result = await registration.install_skill(["claude"], home=home, apply=True)
    assert written == [setup_shared, setup_client]
    assert not result["success"]
    statuses = {
        (report["skill"], report["client"]): report["status"] for report in result["targets"]
    }
    assert statuses == {
        ("answerback-collaboration", "shared"): "unchanged",
        ("answerback-collaboration", "claude"): "refused",
        ("answerback-fleet", "shared"): "installed",
        ("answerback-fleet", "claude"): "installed",
    }
    report = result["targets"][1]
    assert not report["ready"]
    assert report["error"] == "Skill link does not lead to the shared skill; review and retry"


# ------------------------------------------- fix round 1: R1-03, what counts as a setup skill

WHOLE_SKILL_MD = "---\nname: answerback-fleet\ndescription: old\n---\nold\n"


def _unusable_open_front_matter(tmp_path: Path, entry: Path) -> None:
    _other_setup_skill(entry)
    (entry / "SKILL.md").write_text("---\nname: answerback-fleet\nold\n")


def _unusable_other_name(tmp_path: Path, entry: Path) -> None:
    _other_setup_skill(entry)
    (entry / "SKILL.md").write_text("---\nname: another-skill\ndescription: old\n---\nold\n")


def _unusable_no_script(tmp_path: Path, entry: Path) -> None:
    entry.mkdir()
    (entry / "SKILL.md").write_text(WHOLE_SKILL_MD)


def _unusable_linked_skill_md(tmp_path: Path, entry: Path) -> None:
    _other_setup_skill(entry)
    (tmp_path / "real-SKILL.md").write_text(WHOLE_SKILL_MD)
    (entry / "SKILL.md").unlink()
    (entry / "SKILL.md").symlink_to(tmp_path / "real-SKILL.md")


def _unusable_linked_script(tmp_path: Path, entry: Path) -> None:
    _other_setup_skill(entry)
    (tmp_path / "real-fleet.py").write_text("print('old')\n")
    (entry / "scripts/fleet.py").unlink()
    (entry / "scripts/fleet.py").symlink_to(tmp_path / "real-fleet.py")


UNUSABLE = [
    pytest.param(_unusable_open_front_matter, ".agents", id="open-front-matter"),
    pytest.param(_unusable_open_front_matter, ".claude", id="open-front-matter-at-a-client"),
    pytest.param(_unusable_other_name, ".agents", id="other-name"),
    pytest.param(_unusable_no_script, ".agents", id="no-script"),
    pytest.param(_unusable_linked_skill_md, ".agents", id="linked-skill-md"),
    pytest.param(_unusable_linked_script, ".agents", id="linked-script"),
    # pins_existing (R2-05): the same four at a client entry.
    pytest.param(_unusable_other_name, ".claude", id="other-name-at-a-client"),
    pytest.param(_unusable_no_script, ".claude", id="no-script-at-a-client"),
    pytest.param(_unusable_linked_skill_md, ".claude", id="linked-skill-md-at-a-client"),
    pytest.param(_unusable_linked_script, ".claude", id="linked-script-at-a-client"),
]


@pytest.mark.parametrize(("arrange", "place"), UNUSABLE)
async def test_a_folder_that_is_not_a_whole_setup_skill_is_a_conflict(
    tmp_path: Path, arrange: Any, place: str
) -> None:
    """red_at_pretip for the two open-front-matter cases (R1-03); the others pins_existing."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    entry = home / place / "skills/answerback-fleet"
    entry.parent.mkdir(parents=True)
    arrange(tmp_path, entry)
    before = _state(entry)
    result = await install_skill(["claude"], home=home, apply=True)
    assert not result["success"]
    client = "shared" if place == ".agents" else "claude"
    report = next(r for r in _setup_reports(result) if r["client"] == client)
    assert (report["status"], report["error"]) == ("refused", SETUP_CONFLICT)
    assert not report["ready"]
    assert not entry.is_symlink() and _state(entry) == before
    assert not (home / ".agents/skills/answerback-collaboration").exists()
    assert not (home / ".claude/skills/answerback-collaboration").exists()
    assert not (home / ".local").exists()
    others = [home / ".agents/skills/answerback-fleet", home / ".claude/skills/answerback-fleet"]
    assert [path for path in others if path != entry and (path.exists() or path.is_symlink())] == []


@pytest.mark.parametrize(("arrange", "place"), UNUSABLE)
async def test_a_folder_that_is_not_a_whole_setup_skill_is_replaced_with_replace(
    tmp_path: Path, arrange: Any, place: str
) -> None:
    """pins_existing: `--replace` backs the folder up and installs the packaged skill."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    entry = home / place / "skills/answerback-fleet"
    entry.parent.mkdir(parents=True)
    arrange(tmp_path, entry)
    before = _state(entry)
    result = await install_skill(["claude"], home=home, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in _setup_reports(result)] == ["installed", "installed"]
    shared = home / ".agents/skills/answerback-fleet"
    client = home / ".claude/skills/answerback-fleet"
    assert not shared.is_symlink() and _state(shared) == _state(FLEET_SKILL)
    assert client.is_symlink() and client.resolve() == shared
    backups = _setup_backups(home)
    assert len(backups) == 1 and _state(backups[0]) == before
    assert (home / ".agents/skills/answerback-collaboration/SKILL.md").is_file()


def _kept_client_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    shared = home / ".agents/skills/answerback-fleet"
    client = home / ".claude/skills/answerback-fleet"
    shared.parent.mkdir(parents=True)
    shutil.copytree(FLEET_SKILL, shared)
    client.parent.mkdir(parents=True)
    _other_setup_skill(client)
    return home, shared, client


async def test_a_usable_setup_skill_at_a_client_entry_is_kept(tmp_path: Path) -> None:
    """pins_existing (D7): `kept` holds for a client's own folder, not only for the shared entry."""
    from codex_herdr_bridge.registration import install_skill

    home, shared, client = _kept_client_home(tmp_path)
    before = _state(client)
    result = await install_skill(["claude"], home=home, apply=True)
    assert result["success"]
    reports = _setup_reports(result)
    assert [(r["client"], r["status"]) for r in reports] == [
        ("shared", "unchanged"),
        ("claude", "kept"),
    ]
    assert reports[1]["ready"] and not reports[1]["would_change"]
    assert "--replace" in reports[1]["note"]
    assert not client.is_symlink() and _state(client) == before
    assert _state(shared) == _state(FLEET_SKILL)
    assert not _setup_backups(home)


async def test_a_kept_setup_skill_at_a_client_entry_is_replaced_by_a_link(tmp_path: Path) -> None:
    """pins_existing (D7): with `--replace` the folder is backed up and a link takes its place."""
    from codex_herdr_bridge.registration import install_skill

    home, shared, client = _kept_client_home(tmp_path)
    before = _state(client)
    result = await install_skill(["claude"], home=home, apply=True, replace=True)
    assert result["success"]
    assert [(r["client"], r["status"]) for r in _setup_reports(result)] == [
        ("shared", "unchanged"),
        ("claude", "installed"),
    ]
    assert client.is_symlink() and client.resolve() == shared
    assert not shared.is_symlink() and _state(shared) == _state(FLEET_SKILL)
    backups = _setup_backups(home)
    assert len(backups) == 1 and _state(backups[0]) == before


# ------------------------------------------ fix round 1: installer rows that had no test


async def test_an_equal_setup_skill_is_unchanged_with_replace(tmp_path: Path) -> None:
    """pins_existing: `--replace` backs up and rewrites nothing that is already equal."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    assert (await install_skill(["claude"], home=home, apply=True))["success"]
    before = _state(home / ".agents/skills/answerback-fleet")
    result = await install_skill(["claude"], home=home, apply=True, replace=True)
    assert result["success"]
    assert [(r["client"], r["status"]) for r in _setup_reports(result)] == [
        ("shared", "unchanged"),
        ("claude", "unchanged"),
    ]
    assert not any(report["would_change"] for report in result["targets"])
    assert _state(home / ".agents/skills/answerback-fleet") == before
    assert not _setup_backups(home)
    assert not (home / ".local/state/answerback-bridge/backups").exists()


async def test_a_missing_packaged_setup_skill_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (sequence j.2): the package holds one skill and there is no checkout."""
    import importlib.resources

    from codex_herdr_bridge import registration

    package = tmp_path / "installed/answerback_bridge"
    shutil.copytree(
        FLEET_SKILL.parent / "answerback-collaboration",
        package / "data/skills/answerback-collaboration",
    )
    monkeypatch.setattr(importlib.resources, "files", lambda name: package)
    monkeypatch.setattr(registration, "__file__", str(package / "registration.py"))
    home = tmp_path / "home"
    with pytest.raises(registration._Refusal) as refused:
        await registration.install_skill(["claude"], home=home, apply=True)
    assert str(refused.value) == "The setup skill bundle is missing SKILL.md"
    assert not home.exists()


async def test_a_write_that_fails_in_the_middle_leaves_the_finished_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (sequence j.4): no rollback across skills; the rest is refused."""
    from codex_herdr_bridge import registration

    home = tmp_path / "home"
    setup_shared = home / ".agents/skills/answerback-fleet"
    real = registration._apply_skill_file

    def writer(plan: Any) -> None:
        if plan.target == setup_shared:
            raise OSError("stand-in: the disk is full")
        real(plan)

    monkeypatch.setattr("codex_herdr_bridge.registration._apply_skill_file", writer)
    result = await registration.install_skill(["claude"], home=home, apply=True)
    assert not result["success"]
    assert [(r["skill"], r["client"], r["status"]) for r in result["targets"]] == [
        ("answerback-collaboration", "shared", "installed"),
        ("answerback-collaboration", "claude", "installed"),
        ("answerback-fleet", "shared", "refused"),
        ("answerback-fleet", "claude", "refused"),
    ]
    assert (home / ".agents/skills/answerback-collaboration/SKILL.md").is_file()
    assert (home / ".claude/skills/answerback-collaboration").resolve() == (
        home / ".agents/skills/answerback-collaboration"
    )
    for entry in (setup_shared, home / ".claude/skills/answerback-fleet"):
        assert not entry.exists() and not entry.is_symlink()


# ------------------------------- fix round 2: R2-01, the file system decides about a client link

LINK_NAME = "answerback-collaboration"
THROUGH = f"../../missing/../.agents/skills/{LINK_NAME}"
LINK_REFUSAL = "Skill link does not lead to the shared skill; review and retry"


def _row_relative(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    return f"../../.agents/skills/{LINK_NAME}"


def _row_absolute(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    return str(shared)


def _row_alias_dot_dot(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    (tmp_path / "alternate/child").mkdir(parents=True)
    (tmp_path / f"alternate/.agents/skills/{LINK_NAME}").mkdir(parents=True)
    (home / "alias").symlink_to(tmp_path / "alternate/child", target_is_directory=True)
    return f"../../alias/../.agents/skills/{LINK_NAME}"


def _row_missing_dot_dot(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    return THROUGH


def _row_file_dot_dot(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    (home / "missing").write_text("an ordinary file")
    return THROUGH


def _row_shared_absent(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.parent.mkdir(parents=True)
    return f"../../.agents/skills/{LINK_NAME}"


def _row_agents_absent(tmp_path: Path, home: Path, shared: Path) -> str:
    return f"../../.agents/skills/{LINK_NAME}"


def _row_straight_to_old(tmp_path: Path, home: Path, shared: Path) -> str:
    (tmp_path / "old").mkdir()
    shared.parent.mkdir(parents=True)
    shared.symlink_to(tmp_path / "old", target_is_directory=True)
    return str(tmp_path / "old")


def _row_itself(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    return LINK_NAME


def _row_home_through_a_link(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    (tmp_path / "way-home").symlink_to(home, target_is_directory=True)
    return str(tmp_path / "way-home/.agents/skills" / LINK_NAME)


def _row_ends_in_dot_dot(tmp_path: Path, home: Path, shared: Path) -> str:
    (shared / "inside").mkdir(parents=True)
    return f"../../.agents/skills/{LINK_NAME}/inside/.."


def _row_link_to_a_link(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    (shared.parent / "second").symlink_to(LINK_NAME, target_is_directory=True)
    return "../../.agents/skills/second"


LINK_ROWS = [
    pytest.param(_row_relative, True, True, id="relative"),
    pytest.param(_row_absolute, True, True, id="absolute"),
    pytest.param(_row_alias_dot_dot, False, True, id="alias-dot-dot"),
    pytest.param(_row_missing_dot_dot, False, False, id="missing-dot-dot"),
    pytest.param(_row_file_dot_dot, False, False, id="file-dot-dot"),
    pytest.param(_row_shared_absent, True, False, id="shared-absent"),
    pytest.param(_row_agents_absent, True, False, id="agents-absent"),
    pytest.param(_row_straight_to_old, False, True, id="straight-to-old"),
    pytest.param(_row_itself, False, False, id="itself"),
    pytest.param(_row_home_through_a_link, True, True, id="home-through-a-link"),
    pytest.param(_row_ends_in_dot_dot, False, True, id="ends-in-dot-dot"),
    pytest.param(_row_link_to_a_link, False, True, id="link-to-a-link"),
]


@pytest.mark.parametrize(("arrange", "names", "opens"), LINK_ROWS)
def test_whether_a_client_link_names_the_shared_entry(
    tmp_path: Path, arrange: Any, names: bool, opens: bool
) -> None:
    """red_at_pretip for missing-dot-dot and file-dot-dot (R2-01); the other rows pins_existing."""
    from codex_herdr_bridge.registration import _links_to

    home = tmp_path / "home"
    shared = home / ".agents/skills" / LINK_NAME
    client = home / ".claude/skills" / LINK_NAME
    client.parent.mkdir(parents=True)
    client.symlink_to(arrange(tmp_path, home, shared))
    try:
        os.stat(client)
        opened = True
    except OSError:
        opened = False
    assert (_links_to(client, shared), opened) == (names, opens)


def _link_backups(home: Path) -> list[str]:
    found = (home / ".local/state/answerback-bridge/backups").glob(
        f"{LINK_NAME}.herdr-bridge-backup-*/content"
    )
    return sorted(os.readlink(backup) for backup in found if backup.is_symlink())


def _in_the_way(home: Path, kind: str) -> None:
    if kind == "file":
        (home / "missing").write_text("an ordinary file")


@pytest.mark.parametrize("kind", ["missing", "file"])
async def test_a_client_link_that_cannot_be_followed_is_replaced_with_the_shared_entry(
    tmp_path: Path, kind: str
) -> None:
    """red_at_pretip (R2-01): through a missing folder, or a file, and `..`; nothing opens it."""
    from codex_herdr_bridge.registration import install_skill

    old, source, home, shared, client = _a01_home(tmp_path)
    _in_the_way(home, kind)
    shared.symlink_to(old, target_is_directory=True)
    client.symlink_to(THROUGH)
    assert not client.exists()
    result = await install_skill(["claude"], home=home, source=source, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["installed", "installed"]
    assert (client / "SKILL.md").read_text() == "# New skill"
    assert client.is_symlink() and client.samefile(shared)
    assert _link_backups(home) == sorted([THROUGH, str(old)])
    assert (old / "SKILL.md").read_text() == "# Old skill"


@pytest.mark.parametrize("kind", ["missing", "file"])
async def test_a_client_link_that_cannot_be_followed_is_a_conflict_beside_an_equal_shared_entry(
    tmp_path: Path, kind: str
) -> None:
    """red_at_pretip (R2-01): nothing is replaced at the shared entry; preview and apply agree."""
    from codex_herdr_bridge.registration import install_skill

    _, source, home, shared, client = _a01_home(tmp_path)
    _in_the_way(home, kind)
    shutil.copytree(source, shared)
    client.symlink_to(THROUGH)
    for apply in (False, True):
        result = await install_skill(["claude"], home=home, source=source, apply=apply)
        assert not result["success"]
        first, second = result["targets"]
        assert (first["status"], second["status"]) == ("unchanged", "refused")
        assert second["error"] == SETUP_CONFLICT and not second["ready"]
        assert os.readlink(client) == THROUGH and not client.exists()
        assert not (home / ".local").exists()
    result = await install_skill(["claude"], home=home, source=source, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["unchanged", "installed"]
    assert (client / "SKILL.md").read_text() == "# New skill"
    assert client.samefile(shared)
    assert _link_backups(home) == [THROUGH]


async def test_a_setup_skill_link_that_cannot_be_followed_is_a_conflict_not_kept(
    tmp_path: Path,
) -> None:
    """red_at_pretip (R2-01): `kept` is for a setup skill that can be opened."""
    from codex_herdr_bridge.registration import install_skill

    home = tmp_path / "home"
    assert (await install_skill(["claude"], home=home, apply=True))["success"]
    client = home / ".claude/skills/answerback-fleet"
    text = "../../missing/../.agents/skills/answerback-fleet"
    client.unlink()
    client.symlink_to(text)
    for apply in (False, True):
        result = await install_skill(["claude"], home=home, apply=apply)
        assert not result["success"]
        report = _setup_reports(result)[1]
        assert (report["status"], report["error"]) == ("refused", SETUP_CONFLICT)
        assert os.readlink(client) == text
    result = await install_skill(["claude"], home=home, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in _setup_reports(result)] == ["unchanged", "installed"]
    assert (client / "SKILL.md").is_file()


@pytest.mark.parametrize("agents", [True, False], ids=["leaf-missing", "agents-missing"])
async def test_a_client_link_to_a_shared_entry_that_is_not_there_yet_is_unchanged(
    tmp_path: Path, agents: bool
) -> None:
    """pins_existing (R2-01, point 1): the shared entry is made in the run, the link stays."""
    from codex_herdr_bridge.registration import install_skill

    _, source, home, shared, client = _a01_home(tmp_path)
    if not agents:
        shutil.rmtree(home / ".agents")
    text = f"../../.agents/skills/{LINK_NAME}"
    client.symlink_to(text)
    result = await install_skill(["claude"], home=home, source=source, apply=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["installed", "unchanged"]
    assert os.readlink(client) == text
    assert (client / "SKILL.md").read_text() == "# New skill"
    assert not (home / ".local").exists()


async def test_a_client_link_through_a_linked_folder_stays_when_the_shared_entry_stays(
    tmp_path: Path,
) -> None:
    """pins_existing (R2-01, point 2): it opens and is one folder with the shared entry."""
    from codex_herdr_bridge.registration import install_skill

    old, _, home, shared, client = _a01_home(tmp_path)
    elsewhere = tmp_path / f"alternate/.agents/skills/{LINK_NAME}"
    elsewhere.parent.mkdir(parents=True)
    (tmp_path / "alternate/child").mkdir()
    elsewhere.symlink_to(old, target_is_directory=True)
    (home / "alias").symlink_to(tmp_path / "alternate/child", target_is_directory=True)
    shared.symlink_to(old, target_is_directory=True)
    text = f"../../alias/../.agents/skills/{LINK_NAME}"
    client.symlink_to(text)
    result = await install_skill(["claude"], home=home, source=old, apply=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["unchanged", "unchanged"]
    assert os.readlink(client) == text and os.readlink(shared) == str(old)
    assert not (home / ".local").exists()


def _shared_goes(shared: Path) -> None:
    shutil.rmtree(shared)


def _shared_loops(shared: Path) -> None:
    shutil.rmtree(shared)
    shared.symlink_to(shared.name)


@pytest.mark.parametrize("damage", [_shared_goes, _shared_loops], ids=["gone", "a-loop"])
async def test_a_shared_entry_that_cannot_be_opened_after_the_writes_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: Any
) -> None:
    """red_at_pretip (R2-01, point 4): the check after the writes opens the client entry."""
    from codex_herdr_bridge import registration

    _, source, home, shared, client = _a01_home(tmp_path)
    real = registration._apply_skill_file

    def writer(plan: Any) -> None:
        real(plan)
        if plan.target == client:  # the last write of this run
            damage(shared)

    monkeypatch.setattr("codex_herdr_bridge.registration._apply_skill_file", writer)
    result = await registration.install_skill(["claude"], home=home, source=source, apply=True)
    assert not result["success"]
    first, second = result["targets"]
    assert (first["status"], second["status"]) == ("installed", "refused")
    assert second["error"] == LINK_REFUSAL and not second["ready"]


def test_two_folder_paths_are_one_folder_only_without_dot_dot_in_what_is_missing(
    tmp_path: Path,
) -> None:
    """pins_existing (R2-01, "same folder"): nothing may come back out of a missing name."""
    from codex_herdr_bridge.registration import _same_folder

    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "way-home").symlink_to(home, target_is_directory=True)
    assert _same_folder(home / "not/yet", tmp_path / "way-home/not/yet")
    assert not _same_folder(home / "not/yet", tmp_path / "way-home/not/other")
    assert not _same_folder(home / "not/../yet", tmp_path / "way-home/not/../yet")
    assert not _same_folder(home / "not/yet", tmp_path / "not/yet")


# ------------------- fix round 3: R3-04 and R3-01, naming the shared entry is not opening it


def _longest_chain(folder: Path, end: Path) -> Path:
    """Links to `end`, each to the one before: the last one that the file system still opens."""
    head = end
    for count in range(200):
        link = folder / f"alias-{count}"
        link.symlink_to(head)
        if not link.is_dir():
            link.unlink()
            break
        head = link
    assert head != end and head.is_dir()
    return head


def _row_more_links_than_followed(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    return str(_longest_chain(tmp_path, shared.parent) / LINK_NAME)


def _row_shared_is_a_link_to_nowhere(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.parent.mkdir(parents=True)
    shared.symlink_to("nowhere")
    return f"../../.agents/skills/{LINK_NAME}"


def _row_shared_is_an_ordinary_file(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.parent.mkdir(parents=True)
    shared.write_text("an ordinary file")
    return f"../../.agents/skills/{LINK_NAME}"


def _row_shared_is_an_empty_folder(tmp_path: Path, home: Path, shared: Path) -> str:
    shared.mkdir(parents=True)
    return f"../../.agents/skills/{LINK_NAME}"


# The four new rows of the ruling's table: names, opens, equal (shared entry kept and replaced).
NEW_LINK_ROWS = [
    pytest.param(_row_more_links_than_followed, True, False, False, id="more-links-than-followed"),
    pytest.param(_row_shared_is_a_link_to_nowhere, True, False, False, id="shared-link-to-nowhere"),
    pytest.param(_row_shared_is_an_ordinary_file, True, True, True, id="shared-ordinary-file"),
    pytest.param(_row_shared_is_an_empty_folder, True, True, True, id="shared-empty-folder"),
]
LINK_ROWS.extend(pytest.param(*row.values[:3], id=row.id) for row in NEW_LINK_ROWS)


@pytest.mark.parametrize(("arrange", "names", "opens", "equal"), NEW_LINK_ROWS)
def test_a_client_link_that_names_the_shared_entry_is_equal_only_when_it_opens(
    tmp_path: Path, arrange: Any, names: bool, opens: bool, equal: bool
) -> None:
    """red_at_pretip (R3-04) for more-links-than-followed and shared-link-to-nowhere, which were
    equal at 53cf72f; the other two rows pins_existing."""
    from codex_herdr_bridge.registration import _equivalent_skill, _links_to, _snapshot

    home, source = tmp_path / "home", tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# New skill")
    shared = home / ".agents/skills" / LINK_NAME
    client = home / ".claude/skills" / LINK_NAME
    client.parent.mkdir(parents=True)
    client.symlink_to(arrange(tmp_path, home, shared))
    assert _links_to(client, shared) is names
    assert client.exists() is opens
    expected = _snapshot(source)
    kept = _equivalent_skill(client, source, expected, shared, False)
    replaced = _equivalent_skill(client, source, expected, shared, True)
    assert (kept, replaced) == (equal, equal)


def _one_link_too_many(tmp_path: Path, shared: Path, client: Path) -> str:
    """A client link that names the shared entry through one link more than are followed."""
    from codex_herdr_bridge.registration import _links_to

    head = _longest_chain(tmp_path, shared.parent)
    text = str(head / LINK_NAME)
    client.symlink_to(text)
    assert _links_to(client, shared)
    with pytest.raises(OSError) as caught:
        os.stat(client)
    assert caught.value.errno == errno.ELOOP
    assert Path(text).parent.is_dir() and Path(text).is_dir()
    return text


async def test_a_client_link_through_more_links_than_are_followed_is_a_conflict(
    tmp_path: Path,
) -> None:
    """red_at_pretip (R3-04): it names the shared entry and nothing opens it; `unchanged` and a
    successful run at 53cf72f."""
    from codex_herdr_bridge.registration import install_skill

    _, source, home, shared, client = _a01_home(tmp_path)
    shutil.copytree(source, shared)
    text = _one_link_too_many(tmp_path, shared, client)
    for apply in (False, True):
        result = await install_skill(["claude"], home=home, source=source, apply=apply)
        assert not result["success"]
        first, second = result["targets"]
        assert (first["status"], second["status"]) == ("unchanged", "refused")
        assert second["error"] == SETUP_CONFLICT and not second["ready"]
        assert os.readlink(client) == text
        assert not (home / ".local").exists()
    result = await install_skill(["claude"], home=home, source=source, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["unchanged", "installed"]
    assert _link_backups(home) == [text]
    assert client.is_symlink() and client.samefile(shared)
    assert (client / "SKILL.md").read_text() == "# New skill"


async def test_a_client_link_to_a_shared_entry_that_is_a_link_to_nowhere_is_made_again(
    tmp_path: Path,
) -> None:
    """red_at_pretip (R3-04) by the status: the client report is `installed`, the link is backed
    up and made again. At 53cf72f it was `unchanged`; the client entry opened afterwards then too,
    because the shared entry is replaced in the same run."""
    from codex_herdr_bridge.registration import install_skill

    _, source, home, shared, client = _a01_home(tmp_path)
    shared.symlink_to("nowhere")
    text = f"../../.agents/skills/{LINK_NAME}"
    client.symlink_to(text)
    result = await install_skill(["claude"], home=home, source=source, apply=True, replace=True)
    assert result["success"]
    assert [report["status"] for report in result["targets"]] == ["installed", "installed"]
    assert _link_backups(home) == sorted(["nowhere", text])
    assert (client / "SKILL.md").read_text() == "# New skill"
    assert client.samefile(shared)


def _shared_becomes_a_file(shared: Path) -> None:
    shutil.rmtree(shared)
    shared.write_text("an ordinary file")


def _shared_becomes_empty(shared: Path) -> None:
    shutil.rmtree(shared)
    shared.mkdir()


@pytest.mark.parametrize(
    "damage", [_shared_becomes_a_file, _shared_becomes_empty], ids=["a-file", "an-empty-folder"]
)
async def test_a_shared_entry_that_is_no_skill_after_the_writes_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: Any
) -> None:
    """red_at_pretip (R3-01): the client link opens and is one file with the shared entry, and
    what it opens is no skill. A successful run at 53cf72f."""
    from codex_herdr_bridge import registration

    _, source, home, shared, client = _a01_home(tmp_path)
    real = registration._apply_skill_file

    def writer(plan: Any) -> None:
        real(plan)
        if plan.target == client:  # the last write of this run
            damage(shared)

    monkeypatch.setattr("codex_herdr_bridge.registration._apply_skill_file", writer)
    result = await registration.install_skill(["claude"], home=home, source=source, apply=True)
    assert not result["success"]
    second = result["targets"][1]
    assert (second["status"], second["ready"]) == ("refused", False)
    assert second["error"] == LINK_REFUSAL


@pytest.mark.parametrize("damage", [_shared_goes, _shared_loops], ids=["gone", "a-loop"])
async def test_a_client_link_to_the_source_is_refused_when_the_shared_entry_does_not_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: Any
) -> None:
    """red_at_pretip (R3-01): the client link does not go through the shared entry, so only a
    look at the shared entry itself sees the damage. A successful run at 53cf72f.

    The status of the shared entry's own report is not read here: rule P wants `refused`, and
    the test of fix round 2 for a shared entry that is gone or a loop, which must stay as it is,
    wants `installed`. See the hand-back notes.
    """
    from codex_herdr_bridge import registration

    _, source, home, shared, client = _a01_home(tmp_path)
    client.symlink_to(source, target_is_directory=True)
    real = registration._apply_skill_file

    def writer(plan: Any) -> None:
        real(plan)
        if plan.target == shared:  # right after its own write
            damage(shared)

    monkeypatch.setattr("codex_herdr_bridge.registration._apply_skill_file", writer)
    result = await registration.install_skill(["claude"], home=home, source=source, apply=True)
    assert not result["success"]
    second = result["targets"][1]
    assert (second["status"], second["ready"]) == ("refused", False)
    assert second["error"] == LINK_REFUSAL
    assert os.readlink(client) == str(source)


async def test_a_client_link_to_a_source_that_is_no_skill_after_the_writes_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip (R3-01, point 2): the shared entry opens as a skill, the client entry is
    one file with the source and does not. A successful run at 53cf72f."""
    from codex_herdr_bridge import registration

    _, source, home, shared, client = _a01_home(tmp_path)
    client.symlink_to(source, target_is_directory=True)
    real = registration._apply_skill_file

    def writer(plan: Any) -> None:
        real(plan)
        (source / "SKILL.md").unlink()

    monkeypatch.setattr("codex_herdr_bridge.registration._apply_skill_file", writer)
    result = await registration.install_skill(["claude"], home=home, source=source, apply=True)
    assert not result["success"]
    first, second = result["targets"]
    assert (shared / "SKILL.md").read_text() == "# New skill"
    assert (first["status"], second["status"]) == ("installed", "refused")
    assert second["error"] == LINK_REFUSAL


# ------------------------------- fix round 4: C-02, two states of the check after the writes


async def test_an_unchanged_shared_entry_that_is_gone_after_the_writes_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (C-02; passes at 474e7f3): the shared entry is equal before the run, so its
    report is `unchanged` and nothing is written to it; the last write of the other skill removes
    it. The client link goes straight to the source and still opens. The edit that looks only at
    an `installed` shared entry (`c-shared-installed-only`) is noticed here."""
    from codex_herdr_bridge import registration

    name, setup = registration.SKILL_NAME, registration.SETUP_SKILL_NAME
    source = registration._skill_source(None)
    home = tmp_path / "home"
    shared = home / ".agents" / "skills" / name
    client = home / ".claude" / "skills" / name
    client.parent.mkdir(parents=True)
    shutil.copytree(source, shared)
    client.symlink_to(source, target_is_directory=True)
    real = registration._apply_skill_file

    def writer(plan: Any) -> None:
        real(plan)
        if plan.target == client.with_name(setup):  # the last write of this run
            shutil.rmtree(shared)

    monkeypatch.setattr("codex_herdr_bridge.registration._apply_skill_file", writer)
    result = await registration.install_skill(["claude"], home=home, apply=True)
    assert not shared.exists() and not shared.is_symlink()
    assert (client / "SKILL.md").is_file()
    assert not result["success"]
    reports = {(r["skill"], r["client"]): r for r in result["targets"]}
    assert reports[(name, "shared")]["status"] == "unchanged"
    refused = reports[(name, "claude")]
    assert (refused["status"], refused["ready"]) == ("refused", False)
    assert refused["error"] == LINK_REFUSAL
    assert reports[(setup, "claude")]["status"] == "installed"


async def test_a_client_link_to_a_source_whose_skill_md_is_a_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (C-02; passes at 474e7f3): after the shared copy is made, the source's
    `SKILL.md` is a folder. The client link goes straight to the source: something named
    `SKILL.md` is there, and it is no file. The edit that asks `exists()` and not `is_file()`
    (`c-skill-directory`) is noticed here."""
    from codex_herdr_bridge import registration

    _, source, home, shared, client = _a01_home(tmp_path)
    client.symlink_to(source, target_is_directory=True)
    real = registration._apply_skill_file

    def writer(plan: Any) -> None:
        real(plan)
        if plan.target == shared:  # right after the shared copy is made
            (source / "SKILL.md").unlink()
            (source / "SKILL.md").mkdir()

    monkeypatch.setattr("codex_herdr_bridge.registration._apply_skill_file", writer)
    result = await registration.install_skill(["claude"], home=home, source=source, apply=True)
    assert (client / "SKILL.md").is_dir()
    assert (shared / "SKILL.md").is_file()
    assert not result["success"]
    first, second = result["targets"]
    assert (first["status"], second["status"]) == ("installed", "refused")
    assert (second["ready"], second["error"]) == (False, LINK_REFUSAL)
