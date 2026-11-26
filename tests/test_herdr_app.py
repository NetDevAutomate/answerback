"""T-181: the Herdr App side panel (the fleet column) and the themes, shipped and installed."""

import errno
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_fleet_lifetime import no_descriptor_left

from answerback_bridge import cli
from answerback_bridge.providers.herdr import herdr_app

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "answerback_bridge" / "data" / "herdr-app"
OLDER_PANEL = ROOT / "integrations" / "herdr-app" / "answerback"
FLEET = "mods/answerback-fleet"


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """An App folder as the App leaves it before the panel is installed. Names are invented."""
    folder = tmp_path / "app-config"
    (folder / "mods").mkdir(parents=True)
    (folder / "themes").mkdir()
    return folder


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


def _states(report: dict) -> dict[str, str]:
    return {row["path"]: row["state"] for row in report["files"]}


def _actions(report: dict) -> dict[str, str]:
    return {row["path"]: row["action"] for row in report["files"]}


def _shipped(path: str) -> bytes:
    return (ASSETS / path).read_bytes()


# ---------------------------------------------------------------- the shipped files


def test_manifest_rows_match_the_files_and_the_older_panel() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "herdr_app_manifest.py"), "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_shipped_files_are_verified_and_name_who_wrote_them() -> None:
    assets = {asset.path: asset for asset in herdr_app.shipped()}
    assert sorted(assets) == [
        f"{FLEET}/fixtures.js",
        f"{FLEET}/fleet.js",
        f"{FLEET}/main.js",
        f"{FLEET}/mod.json",
        "themes/catppuccin-frappe.json",
        "themes/catppuccin-latte.json",
        "themes/catppuccin-macchiato.json",
        "themes/catppuccin-mocha.json",
        "themes/rex-macchiato.json",
        "themes/zed-tokyo-night.json",
    ]
    # The App's own helpers beside a mod are not shipped: the App writes them.
    assert not any(path.rsplit("/", 1)[-1] in herdr_app.APP_HELPERS for path in assets)
    assert {asset.written_by for asset in assets.values()} == {"answerback"}
    # Only the two files T-181 changed carry an earlier shipped version.
    assert {path for path, asset in assets.items() if asset.previous} == {
        f"{FLEET}/fleet.js",
        f"{FLEET}/fixtures.js",
    }
    assert json.loads(_shipped(f"{FLEET}/mod.json")) == {"place": "right", "size": 320}
    for asset in assets.values():
        if asset.kind == "theme":
            theme = json.loads(asset.data)
            assert set(theme) >= {"background", "foreground", "selection", "ansi"}
            assert len(theme["ansi"]) == 16


def test_every_shipped_file_has_a_licence_and_a_provenance() -> None:
    document = json.loads((ASSETS / "manifest.json").read_text())
    for row in document["files"]:
        assert row.get("licence") and row.get("provenance") and row.get("written_by"), row["path"]
    # The App's own themes and the clones of its mods are named, not shipped.
    named = {row["path"] for row in document["dependencies"]}
    assert {"themes/atom-one-dark.json", "themes/win98.json"} <= named


def test_a_shipped_file_that_does_not_match_its_row_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "assets"
    shutil.copytree(ASSETS, copy)
    (copy / FLEET / "main.js").write_bytes(b"// changed\n")
    monkeypatch.setattr(herdr_app, "_assets_root", lambda: copy)
    with pytest.raises(herdr_app.HerdrAppError, match="does not match its manifest"):
        herdr_app.shipped()


def test_a_manifest_path_that_leaves_the_assets_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "assets"
    shutil.copytree(ASSETS, copy)
    document = json.loads((copy / "manifest.json").read_text())
    document["files"][0]["path"] = "../outside.js"
    (copy / "manifest.json").write_text(json.dumps(document))
    monkeypatch.setattr(herdr_app, "_assets_root", lambda: copy)
    with pytest.raises(herdr_app.HerdrAppError, match="names a path it may not"):
        herdr_app.shipped()


@pytest.mark.parametrize(
    "value",
    [
        "../victim",
        "mods/../../victim",
        "mods/../themes/x",
        "/etc/herdr",
        "",
        ".",
        "mods",
        "mods//x",
        "mods/./x",
        "mods\\x",
        "mods/x\x00",
        "mods/x\n",
        "mods/x\r\n",
        "\nmods/x",
        "etc/x",
        7,
    ],
)
def test_a_manifest_path_outside_the_apps_folders_is_not_one(value: object) -> None:
    """R1 (ABM-T181F): one rule for every manifest path: relative, under mods/ or themes/.

    X3 (ABM-T181F2): matched whole. red at 05b1fd0 for `mods/x\\n`: `match` with `$` let a final
    newline through, so the installer and the shared check tool both accepted a name outside the
    stated alphabet.
    """
    assert herdr_app.manifest_path(value) is None
    assert herdr_app.manifest_path("mods/answerback") == "mods/answerback"
    assert herdr_app.manifest_path("themes/x.json") == "themes/x.json"
    assert herdr_app.manifest_path(f"{FLEET}/main.js") == f"{FLEET}/main.js"
    # The shared check tool applies the same rule, so a manifest with such a path fails its check.
    sys.path.insert(0, str(ROOT / "tools"))
    import herdr_app_manifest

    document = {"files": [{"path": value}], "superseded": {"path": value}}
    assert len(herdr_app_manifest.unsafe_paths(document)) == 2


def _manifest_with_superseded_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    copy = tmp_path / "assets"
    shutil.copytree(ASSETS, copy)
    document = json.loads((copy / "manifest.json").read_text())
    document["superseded"]["path"] = path
    document["superseded"]["files"] = {}
    (copy / "manifest.json").write_text(json.dumps(document))
    monkeypatch.setattr(herdr_app, "_assets_root", lambda: copy)


def test_a_traversal_in_the_superseded_path_is_refused_before_the_backup_folder_is_touched(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 (ABM-T181F): `superseded.path` is validated like every other manifest path.

    red at 350c792: an empty folder named by `../victim` read as the unchanged older panel, was
    copied under the backup folder's `content/..` and removed with `rmtree`.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    _manifest_with_superseded_path(tmp_path, monkeypatch, "../victim")
    with pytest.raises(herdr_app.HerdrAppError, match="names a path it may not"):
        herdr_app.install(app, apply=True, home=home)
    assert victim.is_dir()
    assert not (home / ".local").exists()
    with pytest.raises(herdr_app.HerdrAppError, match="names a path it may not"):
        herdr_app.inspect(app)
    # The check tool says the same of such a manifest.
    sys.path.insert(0, str(ROOT / "tools"))
    import herdr_app_manifest

    document = json.loads((tmp_path / "assets" / "manifest.json").read_text())
    assert herdr_app_manifest.unsafe_paths(document) == [
        "path the installer refuses: superseded.path '../victim'"
    ]


def test_a_backup_or_removal_outside_the_apps_folder_is_refused(app: Path, tmp_path: Path) -> None:
    """R1 (ABM-T181F): containment is enforced again where a copy or a removal happens."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("x")
    backup = tmp_path / "backup"
    backup.mkdir()
    for target in (app / ".." / "outside", outside, app / "mods" / ".." / ".." / "outside"):
        with pytest.raises(herdr_app.HerdrAppError, match="outside the Herdr App folder"):
            herdr_app._back_up(target, app, herdr_app.Backup(backup, (0, 0)), None)
    assert list(backup.iterdir()) == []
    linked = app / "mods" / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(herdr_app.HerdrAppError, match="symlink"):
        herdr_app._contained(linked / "file", app)
    assert herdr_app._contained(app / "mods" / "x" / "main.js", app) == Path("mods/x/main.js")


# ---------------------------------------------------------------- where the App's folder is


def test_config_directory_prefers_the_given_folder_then_the_environment(tmp_path: Path) -> None:
    given = tmp_path / "given"
    env = {"HERDR_APP_CONFIG": str(tmp_path / "from-env")}
    assert herdr_app.config_directory(home=tmp_path, environ=env, given=given) == given
    assert herdr_app.config_directory(home=tmp_path, environ=env) == tmp_path / "from-env"
    assert herdr_app.config_directory(home=tmp_path, environ={}) == (
        tmp_path / ".config" / "herdr-app"
    )
    assert herdr_app.config_directory(home=tmp_path, environ={"HERDR_APP_CONFIG": ""}) == (
        tmp_path / ".config" / "herdr-app"
    )


def test_nothing_is_done_without_the_app_folder(tmp_path: Path, home: Path) -> None:
    missing = tmp_path / "not-here"
    report = herdr_app.install(missing, apply=True, home=home)
    assert report["present"] is False
    assert "not here" in report["note"]
    assert report["success"] is True
    assert not missing.exists()
    assert not (home / ".local").exists()
    assert herdr_app.uninstall(missing, apply=True)["present"] is False
    assert not missing.exists()


# ---------------------------------------------------------------- install


def test_preview_writes_nothing_and_apply_installs_every_file(app: Path, home: Path) -> None:
    preview = herdr_app.install(app, home=home)
    assert preview["applied"] is False
    assert set(_actions(preview).values()) == {"install"}
    assert not (app / FLEET).exists()

    applied = herdr_app.install(app, apply=True, home=home)
    assert applied["success"] is True
    assert set(_actions(applied).values()) == {"installed"}
    for asset in herdr_app.shipped():
        assert (app / asset.path).read_bytes() == asset.data
    assert applied["backup"] is None
    status = herdr_app.inspect(app)
    assert set(_states(status).values()) == {"installed"}


def test_a_second_install_changes_nothing(app: Path, home: Path) -> None:
    herdr_app.install(app, apply=True, home=home)
    before = {p: p.stat().st_mtime_ns for p in app.rglob("*") if p.is_file()}
    again = herdr_app.install(app, apply=True, home=home)
    assert set(_actions(again).values()) == {"unchanged"}
    assert {p: p.stat().st_mtime_ns for p in app.rglob("*") if p.is_file()} == before


def test_a_file_the_person_changed_is_kept_and_named(app: Path, home: Path) -> None:
    herdr_app.install(app, apply=True, home=home)
    edited = app / FLEET / "main.js"
    edited.write_bytes(b"// the person's own edit\n")
    assert _states(herdr_app.inspect(app))[f"{FLEET}/main.js"] == "differs"
    report = herdr_app.install(app, apply=True, home=home)
    assert _actions(report)[f"{FLEET}/main.js"] == "keep"
    assert edited.read_bytes() == b"// the person's own edit\n"
    assert any("main.js: differs from the shipped file and is kept" in n for n in report["notes"])
    assert report["backup"] is None


def test_replace_backs_up_the_changed_file_before_it_installs_the_shipped_one(
    app: Path, home: Path
) -> None:
    herdr_app.install(app, apply=True, home=home)
    (app / FLEET / "main.js").write_bytes(b"// the person's own edit\n")
    report = herdr_app.install(app, apply=True, replace=True, home=home)
    assert _actions(report)[f"{FLEET}/main.js"] == "replaced"
    assert (app / FLEET / "main.js").read_bytes() == _shipped(f"{FLEET}/main.js")
    backup = Path(report["backup"])
    assert backup.parent == home / ".local/state/answerback-bridge/backups"
    assert (backup / "content" / FLEET / "main.js").read_bytes() == b"// the person's own edit\n"
    assert (backup / "content" / FLEET / "main.js").stat().st_mode & 0o777 == 0o600
    assert json.loads((backup / "recovery.json").read_text())["directory"] == str(app)


def test_an_earlier_shipped_version_is_updated_with_a_backup(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = b"// the panel as an earlier release shipped it\n"
    copy = tmp_path / "assets"
    shutil.copytree(ASSETS, copy)
    document = json.loads((copy / "manifest.json").read_text())
    for row in document["files"]:
        if row["path"] == f"{FLEET}/main.js":
            row["previous_digests"] = [herdr_app.digest(earlier)]
    (copy / "manifest.json").write_text(json.dumps(document))
    monkeypatch.setattr(herdr_app, "_assets_root", lambda: copy)
    (app / FLEET).mkdir()
    (app / FLEET / "main.js").write_bytes(earlier)
    assert _states(herdr_app.inspect(app))[f"{FLEET}/main.js"] == "outdated"
    report = herdr_app.install(app, apply=True, home=home)
    assert _actions(report)[f"{FLEET}/main.js"] == "updated"
    assert (app / FLEET / "main.js").read_bytes() == _shipped(f"{FLEET}/main.js")
    assert (Path(report["backup"]) / "content" / FLEET / "main.js").read_bytes() == earlier


def test_the_apps_own_helpers_are_left_alone(app: Path, home: Path) -> None:
    (app / FLEET).mkdir()
    (app / FLEET / "jsconfig.json").write_text('{"compilerOptions": {}}\n')
    report = herdr_app.install(app, apply=True, replace=True, home=home)
    assert f"{FLEET}/jsconfig.json" not in _actions(report)
    assert (app / FLEET / "jsconfig.json").read_text() == '{"compilerOptions": {}}\n'


def test_a_symlink_on_the_way_is_refused_and_nothing_is_written(
    app: Path, home: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (app / FLEET).symlink_to(elsewhere, target_is_directory=True)
    report = herdr_app.install(app, apply=True, home=home)
    assert report["success"] is False
    assert _actions(report)[f"{FLEET}/main.js"] == "refuse"
    assert list(elsewhere.iterdir()) == []
    assert not (app / "themes" / "rex-macchiato.json").exists()


def _snapshot(folder: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def _tree(folder: Path) -> list[Path]:
    """Every name below `folder`, files, folders and links, so an added one shows."""
    return sorted(p.relative_to(folder) for p in folder.rglob("*"))


def test_a_symlinked_app_folder_is_refused_and_nothing_is_written_through_it(
    app: Path, home: Path, tmp_path: Path
) -> None:
    """R2 (ABM-T181F): the config folder itself being a symlink refuses the whole step.

    red at 350c792: `_write` stopped at the first existing parent and wrote through the link.
    """
    link = tmp_path / "app-link"
    link.symlink_to(app, target_is_directory=True)
    before = _snapshot(app)
    with pytest.raises(herdr_app.HerdrAppError, match="is a symlink"):
        herdr_app.install(link, apply=True, home=home)
    with pytest.raises(herdr_app.HerdrAppError, match="is a symlink"):
        herdr_app.inspect(link, home=home)
    assert _snapshot(app) == before
    assert not (app / FLEET).exists()
    assert not (home / ".local").exists()
    assert herdr_app.install_step(link)["success"] is False


def test_a_symlinked_app_folder_is_refused_and_nothing_is_removed_through_it(
    app: Path, home: Path, tmp_path: Path
) -> None:
    """R2 (ABM-T181F): uninstall through a symlinked config folder unlinks nothing.

    red at 350c792: `_linked_between` stopped at the folder before testing it.
    """
    herdr_app.install(app, apply=True, home=home)
    _older_panel(app)
    before = _snapshot(app)
    link = tmp_path / "app-link"
    link.symlink_to(app, target_is_directory=True)
    with pytest.raises(herdr_app.HerdrAppError, match="is a symlink"):
        herdr_app.uninstall(link, apply=True, home=home)
    assert _snapshot(app) == before
    assert herdr_app.uninstall_step(link)["success"] is False
    # The install's removal of the older panel does not go through the link either.
    with pytest.raises(herdr_app.HerdrAppError, match="is a symlink"):
        herdr_app.install(link, apply=True, home=home)
    assert _snapshot(app) == before


def test_a_symlinked_folder_below_the_home_on_the_way_is_refused(
    home: Path, tmp_path: Path
) -> None:
    """R2 (ABM-T181F): `~/.config` being a symlink refuses `~/.config/herdr-app`."""
    real = tmp_path / "real-config"
    (real / "herdr-app" / "mods").mkdir(parents=True)
    (home / ".config").symlink_to(real, target_is_directory=True)
    directory = home / ".config" / "herdr-app"
    with pytest.raises(herdr_app.HerdrAppError, match="is a symlink"):
        herdr_app.install(directory, apply=True, home=home)
    assert not (real / "herdr-app" / FLEET).exists()
    # A folder elsewhere is checked itself only; the home is never checked.
    assert herdr_app.install(tmp_path / "not-here", home=home)["present"] is False


def test_a_mutation_checks_the_folder_it_was_inspected_in_without_following_links(
    app: Path, home: Path, tmp_path: Path
) -> None:
    """R2 (ABM-T181F): the final no-follow check compares the folder's identity."""
    anchor = herdr_app._root_check(app, home)
    assert anchor is not None
    other = tmp_path / "other"
    other.mkdir()
    swapped = (os.lstat(other).st_dev, os.lstat(other).st_ino)
    with pytest.raises(herdr_app.HerdrAppError, match="changed during the step"):
        herdr_app._write(app / FLEET / "main.js", b"x", app, swapped, None)
    assert not (app / FLEET).exists()
    assert herdr_app._root_check(tmp_path / "not-here", home) is None


def _swap_for_link(path: Path, outside: Path) -> None:
    """Put a symlink to `outside` where `path` is; the folder that was there is moved aside."""
    if path.exists() or path.is_symlink():
        path.rename(path.with_name(path.name + ".moved"))
    path.symlink_to(outside, target_is_directory=True)


def _race_after_the_check(
    monkeypatch: pytest.MonkeyPatch, swapped: Path, outside: Path
) -> list[Path]:
    """Patch the seam between the check and the mutation: the first check a mutation makes
    (`_contained` with the inspected folder's identity) runs for real, then `swapped` is replaced
    by a symlink to `outside`. Later checks return what the real one returned, as when the swap
    lands after the last of them; only the mutation itself can still notice it."""
    real = herdr_app._contained
    seen: dict[tuple[Path, Path], Path] = {}
    swaps: list[Path] = []

    def raced(target: Path, directory: Path, anchor: herdr_app.Anchor | None = None) -> Path:
        if anchor is None:
            return real(target, directory, anchor)
        key = (target, directory)
        if key not in seen:
            seen[key] = real(target, directory, anchor)
            if not swaps:
                _swap_for_link(swapped, outside)
                swaps.append(swapped)
        return seen[key]

    monkeypatch.setattr(herdr_app, "_contained", raced)
    return swaps


def _outside_with_decoys(tmp_path: Path, installed: bool) -> tuple[Path, dict[Path, bytes]]:
    """A folder outside the App's, laid out like it, so a followed link finds something to hit:
    at its top (for a swapped mod folder or `mods`) and as a whole App folder (a swapped root),
    the panel in it as shipped when `installed` (what an uninstall would remove)."""
    outside = tmp_path / "outside"
    (outside / "answerback").mkdir(parents=True)
    (outside / "answerback" / "logic.js").write_text("// not the App's\n")
    (outside / FLEET).mkdir(parents=True)
    (outside / "mods" / "answerback").mkdir()
    (outside / "mods" / "answerback" / "logic.js").write_text("// not the App's\n")
    for name in ("main.js", "fleet.js", "fixtures.js", "mod.json"):
        (outside / name).write_text(f"// the owner's {name}, not the App's\n")
        if installed:
            (outside / FLEET / name).write_bytes(_shipped(f"{FLEET}/{name}"))
    return outside, _snapshot(outside)


@pytest.mark.parametrize(
    ("step", "swapped"),
    [
        ("install", FLEET),
        ("replace", FLEET),
        ("uninstall", FLEET),
        ("older-panel", "mods"),
        ("install", "."),
        ("uninstall", "."),
    ],
)
def test_a_folder_swapped_for_a_symlink_after_the_check_is_not_followed(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, swapped: str
) -> None:
    """X1 (ABM-T181F2): every mutation names a child of a no-follow descriptor, not a path.

    red at 05b1fd0 for each case: the check named a path and so did the mutation after it, so
    `mkstemp`/`os.replace`, `unlink` and `rmtree` went through the swapped link into `outside`.
    """
    if step in ("replace", "uninstall", "older-panel"):
        # Installed first, so the only mutation the step makes is the one under test.
        herdr_app.install(app, apply=True, home=home)
    if step == "replace":
        (app / FLEET / "main.js").write_bytes(b"// the person's own edit\n")
    if step == "older-panel":
        _older_panel(app)
    outside, before = _outside_with_decoys(tmp_path, installed=step == "uninstall")
    tree = _tree(outside)
    target = app if swapped == "." else app / swapped
    swaps = _race_after_the_check(monkeypatch, target, outside)
    if step == "uninstall":
        report = herdr_app.uninstall(app, apply=True, home=home)
    else:
        report = herdr_app.install(app, apply=True, replace=step == "replace", home=home)
    assert swaps == [target]
    assert _snapshot(outside) == before
    assert _tree(outside) == tree
    assert report["success"] is False
    assert "symlink" in report["error"] or "changed during the step" in report["error"]


def _edited_install(app: Path, home: Path) -> Path:
    """The panel installed, then main.js edited, so `--replace` needs a backup first."""
    herdr_app.install(app, apply=True, home=home)
    edited = app / FLEET / "main.js"
    edited.write_bytes(b"// the person's own edit\n")
    return edited


def test_a_symlinked_home_refuses_the_step_and_names_the_home(home: Path, tmp_path: Path) -> None:
    """X2 (ABM-T181F2): the home folder is part of the walk when it is the boundary.

    red at 05b1fd0: the walk stopped below the home, so a home that is a link was followed.
    """
    (home / ".config" / "herdr-app" / "mods").mkdir(parents=True)
    link = tmp_path / "home-link"
    link.symlink_to(home, target_is_directory=True)
    directory = link / ".config" / "herdr-app"
    with pytest.raises(herdr_app.HerdrAppError, match="home folder is a symlink: .*home-link$"):
        herdr_app.install(directory, apply=True, home=link)
    with pytest.raises(herdr_app.HerdrAppError, match="home folder is a symlink"):
        herdr_app.uninstall(directory, apply=True, home=link)
    assert not (home / ".config" / "herdr-app" / FLEET).exists()


def test_a_symlinked_home_refuses_the_backup_of_a_folder_outside_it(
    app: Path, home: Path, tmp_path: Path
) -> None:
    """X2 (ABM-T181F2): an App folder outside the home keeps its own anchor, but the backup tree
    is always below the home, so a home that is a link refuses the backup and so the replace.

    red at 05b1fd0: `mkdir(parents=True)` made the backup tree through the link.
    """
    edited = _edited_install(app, home)
    link = tmp_path / "home-link"
    link.symlink_to(home, target_is_directory=True)
    report = herdr_app.install(app, apply=True, replace=True, home=link)
    assert report["success"] is False
    assert report["error"] == f"Refusing: the home folder is a symlink: {link}"
    assert not (home / ".local").exists()
    assert edited.read_bytes() == b"// the person's own edit\n"


@pytest.mark.parametrize(
    "linked",
    [
        ".local",
        ".local/state/answerback-bridge",
        ".local/state/answerback-bridge/backups",
    ],
)
def test_a_symlinked_folder_of_the_backup_tree_is_refused(
    app: Path, home: Path, tmp_path: Path, linked: str
) -> None:
    """X2 (ABM-T181F2): the backup root, its new folder, `content/` and `recovery.json` are made
    and chmod'ed through no-follow descriptors from the home down.

    red at 05b1fd0: `mkdir(exist_ok=True)`, `chmod(0o700)` and `mkdtemp` followed the link, so
    the backup, its chmod and `recovery.json` landed in `outside`.
    """
    edited = _edited_install(app, home)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    (home / linked).parent.mkdir(parents=True, exist_ok=True)
    (home / linked).symlink_to(outside, target_is_directory=True)
    report = herdr_app.install(app, apply=True, replace=True, home=home)
    assert report["success"] is False
    assert "symlink" in report["error"]
    assert _tree(outside) == []
    assert outside.stat().st_mode & 0o777 == 0o755
    assert edited.read_bytes() == b"// the person's own edit\n"


@pytest.mark.parametrize("swap", ["symlink", "folder"])
def test_a_content_folder_swapped_after_the_backup_is_made_is_not_followed(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    """X2 (ABM-T181F2): the copy into `content/` compares its identity and opens it no-follow.

    red at 05b1fd0 for both: `_contained(copy, backup / "content")` left out `content` itself,
    and the copy's `mkdir`/`copyfile` went through the link into `outside` or into the folder put
    in its place. The `folder` case is red at the X1 commit too, which opened `content` no-follow
    but did not compare its identity.
    """
    edited = _edited_install(app, home)
    outside = tmp_path / "outside"
    outside.mkdir()
    real = herdr_app._backup_folder

    def swapped(*args: Any, **kwargs: Any) -> Any:
        made = real(*args, **kwargs)
        folder = Path(getattr(made, "folder", made))
        content = folder / "content"
        content.mkdir(exist_ok=True)
        if swap == "symlink":
            _swap_for_link(content, outside)
        else:
            content.rename(folder / "content.moved")
            content.mkdir(mode=0o700)
        return made

    monkeypatch.setattr(herdr_app, "_backup_folder", swapped)
    report = herdr_app.install(app, apply=True, replace=True, home=home)
    assert report["success"] is False
    assert "symlink" in report["error"] or "changed during the step" in report["error"]
    assert _tree(outside) == []
    assert _tree(Path(report["backup"]) / "content") == []
    assert edited.read_bytes() == b"// the person's own edit\n"


def test_a_fifo_where_a_file_was_is_refused_and_does_not_block_the_backup(
    app: Path, home: Path
) -> None:
    """ABM-T181F2: the descriptor reads open with O_NONBLOCK, so a FIFO is refused, not waited on.

    red at 2eb12b0: `_copy_file_at` opened the FIFO for reading and blocked until a writer came
    (the test's alarm fails it after 5 s); the path code before X1 refused it in `copyfile`.
    """
    _edited_install(app, home)
    fifo = app / FLEET / "main.js"
    fifo.unlink()
    os.mkfifo(fifo)
    signal.signal(signal.SIGALRM, _blocked)
    signal.alarm(5)
    try:
        report = herdr_app.install(app, apply=True, replace=True, home=home)
    finally:
        signal.alarm(0)
    assert report["success"] is False
    assert "not a regular file" in report["error"]
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)


def _blocked(signum: int, frame: object) -> None:
    # pytest.fail, not an OSError: the installer turns an OSError into its own report.
    pytest.fail("the step blocked on a FIFO")


def test_the_backup_tree_is_private(app: Path, home: Path) -> None:
    """X2 (ABM-T181F2): the descriptors make the same modes the paths did."""
    _edited_install(app, home)
    report = herdr_app.install(app, apply=True, replace=True, home=home)
    assert report["success"] is True
    backup = Path(report["backup"])
    root = home / ".local/state/answerback-bridge/backups"
    assert backup.parent == root
    for folder in (root, backup, backup / "content", backup / "content" / FLEET):
        assert folder.stat().st_mode & 0o777 == 0o700, folder
    assert (backup / "recovery.json").stat().st_mode & 0o777 == 0o600
    assert (backup / "content" / FLEET / "main.js").stat().st_mode & 0o777 == 0o600


def test_dependencies_are_reported_present_or_missing(app: Path, home: Path) -> None:
    (app / "themes" / "atom-one-dark.json").write_text("{}\n")
    (app / "mods" / "catppuccin-tabs").mkdir()
    report = herdr_app.install(app, home=home)
    states = {row["path"]: row["state"] for row in report["dependencies"]}
    assert states["themes/atom-one-dark.json"] == "present"
    assert states["themes/win98.json"] == "missing"
    assert states["mods/catppuccin-tabs"] == "present"
    assert states["mods/catppuccin-files"] == "missing"
    assert any(
        n.startswith("themes/win98.json: not shipped by answerback") for n in report["notes"]
    )


# ---------------------------------------------------------------- the older panel


def _older_panel(app: Path) -> Path:
    folder = app / "mods" / "answerback"
    shutil.copytree(OLDER_PANEL, folder)
    (folder / "gpui-kit.d.ts").write_text("// the App's own helper\n")
    return folder


def test_an_unchanged_older_panel_is_removed_with_a_backup_and_a_line(
    app: Path, home: Path
) -> None:
    folder = _older_panel(app)
    preview = herdr_app.install(app, home=home)
    assert preview["superseded"] == {
        **preview["superseded"],
        "state": "unchanged",
        "action": "remove",
    }
    assert folder.is_dir()
    report = herdr_app.install(app, apply=True, home=home)
    assert report["superseded"]["action"] == "removed"
    assert not folder.exists()
    copy = Path(report["backup"]) / "content" / "mods" / "answerback"
    assert (copy / "logic.js").read_bytes() == (OLDER_PANEL / "logic.js").read_bytes()
    assert any(n.startswith("mods/answerback: the older panel") for n in report["notes"])


def test_an_edited_older_panel_is_left_alone(app: Path, home: Path) -> None:
    folder = _older_panel(app)
    (folder / "main.js").write_text("// the person's own edit\n")
    report = herdr_app.install(app, apply=True, home=home)
    assert report["superseded"]["state"] == "edited"
    assert report["superseded"]["action"] == "none"
    assert (folder / "main.js").read_text() == "// the person's own edit\n"
    assert any("with changes of yours" in n for n in report["notes"])


def test_an_older_panel_that_a_layout_places_is_kept_and_named(app: Path, home: Path) -> None:
    folder = _older_panel(app)
    (app / "layouts").mkdir()
    (app / "layouts" / "mine.json").write_text(
        '{"type": "row", "children": [{"type": "block", "content": {"mod": "answerback"}}]}\n'
    )
    report = herdr_app.install(app, apply=True, home=home)
    assert report["superseded"]["layouts"] == ["mine.json"]
    assert report["superseded"]["action"] == "none"
    assert folder.is_dir()
    assert any("a layout places it (mine.json)" in n for n in report["notes"])


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("escaped-key", '{"content": {"m\\u006fd": "answerback"}}'),
        ("escaped-value", '{"content": {"mod": "answer\\u0062ack"}}'),
        (
            "header",
            '{"type": "stack", "header": {"mod": "answerback"}, "children": []}',
        ),
        (
            "floating",
            '{"type": "row", "children": [], "floating": [{"content": {"mod": "answerback"}}]}',
        ),
        ("malformed", '{"type": "row", "children": [{"content": {"mod": '),
        ("not-utf8", "\udcff"),
        # Valid JSON that places nothing, one byte past the read bound: it is not read whole,
        # so it counts as placing the panel.
        ("oversize", '{"pad": "' + "x" * (herdr_app._SIZE_LIMIT + 1 - 11) + '"}'),
        # The placement 10 000 lists deep, past the default recursion limit: the walk has no
        # call-stack bound, and a parser that gives up on the depth counts as placing it too.
        ("deep", "[" * 10_000 + '{"mod": "answerback"}' + "]" * 10_000),
    ],
)
def test_a_layout_is_read_as_json_and_any_spelling_or_failure_keeps_the_older_panel(
    app: Path, home: Path, name: str, text: str
) -> None:
    """R4 (ABM-T181F): the layouts are parsed and walked, not searched for one spelling.

    red at 350c792 for the escaped key, the escaped value and the malformed file: the regex did
    not match, and the unchanged older panel was removed. `oversize` and `deep` were added in
    ABM-T181F2 (the re-check found them absent); they pass at 05b1fd0 and fail when the read
    bound is dropped or the walk is made recursive without catching the depth.
    """
    folder = _older_panel(app)
    (app / "layouts").mkdir()
    layout = app / "layouts" / f"{name}.json"
    if name == "not-utf8":
        layout.write_bytes(b"\xff\xfe{")
    else:
        layout.write_text(text)
    if name == "oversize":
        assert layout.stat().st_size == herdr_app._SIZE_LIMIT + 1
        assert not herdr_app._places(json.loads(text), "answerback")
    report = herdr_app.install(app, apply=True, home=home)
    assert report["superseded"]["layouts"] == [f"{name}.json"]
    assert report["superseded"]["action"] == "none"
    assert folder.is_dir()
    assert (folder / "logic.js").exists()


def test_a_layout_that_places_another_mod_only_does_not_keep_the_older_panel(
    app: Path, home: Path
) -> None:
    folder = _older_panel(app)
    (app / "layouts").mkdir()
    (app / "layouts" / "other.json").write_text(
        '{"children": [{"content": {"mod": "answerback-fleet"}}, {"name": "answerback"}]}'
    )
    report = herdr_app.install(app, apply=True, home=home)
    assert report["superseded"]["layouts"] == []
    assert report["superseded"]["action"] == "removed"
    assert not folder.exists()


def test_the_install_step_reports_a_refusal_instead_of_raising(
    app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Path:
        raise herdr_app.HerdrAppError("The Herdr App assets are missing")

    monkeypatch.setattr(herdr_app, "_assets_root", broken)
    report = herdr_app.install_step(app)
    assert report == {
        "directory": str(app),
        "applied": True,
        "success": False,
        "error": "The Herdr App assets are missing",
    }
    assert herdr_app.uninstall_step(app)["success"] is False


# ---------------------------------------------------------------- uninstall


def test_uninstall_removes_only_the_files_it_owns_and_keeps_the_helpers_folder_and_themes(
    app: Path, home: Path
) -> None:
    """R5 (ABM-T181F): the App's helper files beside the mod, and the folder, are not ours.

    red at 350c792: both helper files were deleted, without a digest check or a backup, and the
    folder removed.
    """
    herdr_app.install(app, apply=True, home=home)
    (app / FLEET / "gpui-kit.d.ts").write_text("// the App's own helper, with an edit of mine\n")
    (app / FLEET / "jsconfig.json").write_text('{"compilerOptions": {"checkJs": true}}\n')
    preview = herdr_app.uninstall(app)
    assert (app / FLEET / "main.js").exists()
    assert _actions(preview)[f"{FLEET}/main.js"] == "remove"
    report = herdr_app.uninstall(app, apply=True)
    assert report["success"] is True
    for name in ("main.js", "fleet.js", "fixtures.js", "mod.json"):
        assert not (app / FLEET / name).exists()
    assert (app / FLEET).is_dir()
    assert (app / FLEET / "gpui-kit.d.ts").read_text() == (
        "// the App's own helper, with an edit of mine\n"
    )
    assert (app / FLEET / "jsconfig.json").read_text() == (
        '{"compilerOptions": {"checkJs": true}}\n'
    )
    assert report["retained"] == {FLEET: ["gpui-kit.d.ts", "jsconfig.json"]}
    assert any(
        n.startswith(f"{FLEET}: the folder is kept with 2 file(s) not shipped by answerback")
        and "gpui-kit.d.ts, jsconfig.json" in n
        for n in report["notes"]
    )
    assert (app / "themes" / "rex-macchiato.json").exists()
    assert _actions(report)["themes/rex-macchiato.json"] == "keep"
    assert any(n.startswith("themes: 6 shipped theme file(s) are kept") for n in report["notes"])


def test_uninstall_keeps_the_folder_when_nothing_is_left_in_it(app: Path, home: Path) -> None:
    herdr_app.install(app, apply=True, home=home)
    report = herdr_app.uninstall(app, apply=True)
    assert report["success"] is True
    assert (app / FLEET).is_dir() and list((app / FLEET).iterdir()) == []
    assert report["retained"] == {FLEET: []}
    assert f"{FLEET}: the folder is kept, empty; remove it by hand" in report["notes"]


def test_uninstall_keeps_a_changed_file_and_its_folder(app: Path, home: Path) -> None:
    herdr_app.install(app, apply=True, home=home)
    (app / FLEET / "fleet.js").write_text("// the person's own edit\n")
    report = herdr_app.uninstall(app, apply=True)
    assert (app / FLEET / "fleet.js").read_text() == "// the person's own edit\n"
    assert not (app / FLEET / "main.js").exists()
    assert report["retained"] == {FLEET: ["fleet.js"]}
    assert any(n.startswith(f"{FLEET}: the folder is kept with 1 file(s)") for n in report["notes"])


# ---------------------------------------------------------------- the instant of the entry (T-188)

HERDR_APP = herdr_app.__name__
PUT_HERE = b"// put here in the instant\n"


@pytest.fixture(autouse=True)
def descriptors() -> Any:
    """Y4 / Z6 (ABM-T188F): every test of this module leaves the process's descriptor table as it
    found it: each descriptor with the device and inode of its file, after a collection, as
    `tests/test_fleet_lifetime.py` does (a count alone misses a leaked descriptor that took the
    number of one closed meanwhile, and fails on an unrelated one opened meanwhile)."""
    with no_descriptor_left():
        yield


def _from_the_installer() -> bool:
    """True when the patched `os` function was called from the installer's module (two frames
    up from here: the wrapper is one), not from pathlib, shutil, pytest or the test itself."""
    return sys._getframe(2).f_globals.get("__name__") == HERDR_APP


class _Listing:
    """A listing of the installer's whose `with` block, once left, runs `swap` once: the seam
    immediately after the entries were read and each was looked at, before any is acted on.
    (`os.scandir` on macOS reads the folder when it is opened, on Linux when it is first
    iterated; a swap at the return of `os.scandir` falls before the listing on one of them.)"""

    def __init__(self, inner: Any, swap: Any) -> None:
        self._inner = inner
        self._swap = swap

    def __iter__(self) -> Any:
        return iter(self._inner)

    def __enter__(self) -> "_Listing":
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        result = self._inner.__exit__(*exc)
        if exc == (None, None, None):
            self._swap()
        return result

    def close(self) -> None:
        self._inner.close()


def _after_the_listing(monkeypatch: pytest.MonkeyPatch, swap: Any) -> list[int]:
    """The first `os.scandir` the installer makes returns a `_Listing` that runs `swap()` when
    its `with` block is left; `fired` holds the listed descriptor once that happened."""
    fired: list[int] = []
    real = os.scandir

    def patched(*args: Any, **kwargs: Any) -> Any:
        listing = real(*args, **kwargs)
        if not fired and _from_the_installer():
            fired.append(args[0])
            return _Listing(listing, swap)
        return listing

    monkeypatch.setattr(os, "scandir", patched)
    return fired


def _at_publication(monkeypatch: pytest.MonkeyPatch, name: str, swap: Any) -> list[str]:
    """The seam immediately before the publication of `name`: the first `os.rename` or `os.link`
    the installer makes with `name` as the new name runs `swap()` first, then the real one."""
    fired: list[str] = []

    def seam(function: Any) -> Any:
        def patched(*args: Any, **kwargs: Any) -> Any:
            if (
                not fired
                and _from_the_installer()
                and args[1:2] == (name,)
                and "dst_dir_fd" in kwargs
            ):
                fired.append(function.__name__)
                swap()
            return function(*args, **kwargs)

        return patched

    monkeypatch.setattr(os, "rename", seam(os.rename))
    monkeypatch.setattr(os, "link", seam(os.link))
    return fired


def _put(kind: str, target: Path, outside: Path) -> None:
    """Put `kind` at `target`, whatever is there moved aside: a regular file, a symlink to a file
    of `outside`, or a folder."""
    if target.exists() or target.is_symlink():
        target.rename(target.with_name(target.name + ".moved"))
    if kind == "file":
        target.write_bytes(PUT_HERE)
    elif kind == "symlink":
        target.symlink_to(outside / "main.js")
    else:
        target.mkdir()


def _is(kind: str, target: Path, outside: Path) -> bool:
    if kind == "file":
        return not target.is_symlink() and target.is_file() and target.read_bytes() == PUT_HERE
    if kind == "symlink":
        return target.is_symlink() and os.readlink(target) == str(outside / "main.js")
    return not target.is_symlink() and target.is_dir()


@pytest.mark.parametrize("kind", ["file", "symlink", "folder"])
def test_a_file_put_where_a_missing_one_is_being_installed_is_not_overwritten(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Y1 (ABM-T188): a missing file is published with `link`, which fails when anything is at
    the name, instead of a rename, which replaces it.

    red at 7b51462 for `file` (the file put there was overwritten, success True, no backup) and
    `symlink` (the link was replaced; the no-follow look before the rename had seen nothing).
    `folder` was safe before (`rename` onto a folder fails, reported as "could not be written"
    without the file's name) and now names the file that appeared.
    """
    outside, before = _outside_with_decoys(tmp_path, installed=False)
    target = app / FLEET / "main.js"
    fired = _at_publication(monkeypatch, "main.js", lambda: _put(kind, target, outside))
    report = herdr_app.install(app, apply=True, home=home)
    assert fired and fired[0] in ("link", "rename")
    assert report["success"] is False
    assert "main.js" in report["error"] and (
        "appeared during installation" in report["error"]
        or "could not be written" in report["error"]
    )
    assert _is(kind, target, outside)
    assert _snapshot(outside) == before
    assert not [p for p in (app / FLEET).iterdir() if p.name.startswith(".answerback-")]
    assert report["backup"] is None


def test_a_missing_file_is_published_with_a_hard_link_and_falls_back_without_them(
    app: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Y1 (ABM-T188): `os.link` with both folder descriptors is the no-clobber publication on
    macOS and Linux (`os.supports_dir_fd`); a filesystem that has no hard links (EPERM on Linux
    vfat, ENOTSUP elsewhere) falls back to the look-then-rename of T-181, which still installs."""
    assert os.link in os.supports_dir_fd and os.link in os.supports_follow_symlinks
    linked: list[str] = []
    real = os.link

    def counted(*args: Any, **kwargs: Any) -> Any:
        if _from_the_installer():
            linked.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "link", counted)
    report = herdr_app.install(app, apply=True, home=home)
    assert report["success"] is True
    assert sorted(linked) == sorted(a.path.rsplit("/", 1)[1] for a in herdr_app.shipped())
    assert (app / FLEET / "main.js").stat().st_nlink == 1

    def no_hard_links(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_hard_links)
    herdr_app.uninstall(app, apply=True, home=home)
    report = herdr_app.install(app, apply=True, home=home)
    assert report["success"] is True
    assert (app / FLEET / "main.js").read_bytes() == _shipped(f"{FLEET}/main.js")


@pytest.mark.parametrize("action", ["replace", "update"])
@pytest.mark.parametrize("kind", ["file", "symlink", "folder"])
def test_a_file_swapped_after_its_backup_is_not_replaced(
    app: Path,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    kind: str,
) -> None:
    """Y1 (ABM-T188): a replace or update renames over the entry only when, held open without
    following a link immediately before, it is the very file (device, inode) that was backed up.

    red at 7b51462 for `file`: the look before the rename saw a regular file and renamed over
    it, so the file put there was lost with no backup of it and the report said success. The
    `symlink` case was refused before and stays so; `folder` failed in `rename` before (reported
    as "could not be written") and is now compared and named.
    """
    outside, before = _outside_with_decoys(tmp_path, installed=False)
    target = app / FLEET / "main.js"
    herdr_app.install(app, apply=True, home=home)
    if action == "replace":
        target.write_bytes(b"// the person's own edit\n")
    else:
        earlier = b"// the panel as an earlier release shipped it\n"
        _with_an_earlier_version(tmp_path, monkeypatch, f"{FLEET}/main.js", earlier)
        target.write_bytes(earlier)
        assert _states(herdr_app.inspect(app, home=home))[f"{FLEET}/main.js"] == "outdated"
    real = herdr_app._back_up
    swaps: list[Path] = []

    def after_the_backup(path: Path, *args: Any, **kwargs: Any) -> Any:
        copied = real(path, *args, **kwargs)
        if path == target and not swaps:
            _put(kind, target, outside)
            swaps.append(target)
        return copied

    monkeypatch.setattr(herdr_app, "_back_up", after_the_backup)
    report = herdr_app.install(app, apply=True, replace=action == "replace", home=home)
    assert swaps == [target]
    assert report["success"] is False
    assert (
        "changed during installation" in report["error"]
        or "Refusing to go through a symlink" in report["error"]
    ), report["error"]
    assert _is(kind, target, outside)
    assert _snapshot(outside) == before
    assert not [p for p in (app / FLEET).iterdir() if p.name.startswith(".answerback-")]
    backed_up = Path(report["backup"]) / "content" / FLEET / target.name
    assert backed_up.read_bytes() == target.with_name(target.name + ".moved").read_bytes()


def _with_an_earlier_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, earlier: bytes
) -> None:
    """The shipped assets with `earlier` recorded as a previous version of `path`."""
    copy = tmp_path / "assets"
    shutil.copytree(ASSETS, copy)
    document = json.loads((copy / "manifest.json").read_text())
    for row in document["files"]:
        if row["path"] == path:
            row["previous_digests"] = [herdr_app.digest(earlier)]
    (copy / "manifest.json").write_text(json.dumps(document))
    monkeypatch.setattr(herdr_app, "_assets_root", lambda: copy)


@pytest.mark.parametrize("kind", ["folder", "symlink"])
def test_a_folder_swapped_during_the_removal_walk_is_not_removed(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Y2 (ABM-T188): the older panel is renamed to a private name in `mods/` and compared with
    the open descriptor before it is walked, so the walk and the final `rmdir` remove the folder
    that was inspected, and a folder put at `mods/answerback` meanwhile is left alone.

    red at 7b51462 for `folder`: `rmdir("answerback", dir_fd=mods)` removed the empty folder put
    at the name mid-walk, and the report said success. `symlink` failed in `rmdir` before
    (reported as "could not be written", the older panel emptied but not removed); now the
    link is left alone and the removal succeeds.
    """
    folder = _older_panel(app)
    outside, before = _outside_with_decoys(tmp_path, installed=False)
    swaps: list[Path] = []
    real = os.scandir

    def mid_walk(*args: Any, **kwargs: Any) -> Any:
        listing = real(*args, **kwargs)
        # The removal's walk is the one after the backup has copied the older panel whole.
        if _from_the_installer() and not swaps:
            copies = list((home / herdr_app.BACKUP_ROOT).glob("*/content/mods/answerback/logic.js"))
            if copies:
                _put(kind, folder, outside)
                swaps.append(folder)
        return listing

    monkeypatch.setattr(os, "scandir", mid_walk)
    report = herdr_app.install(app, apply=True, home=home)
    assert swaps == [folder]
    assert _is(kind, folder, outside)
    assert _snapshot(outside) == before
    assert report["success"] is True, report.get("error")
    assert report["superseded"]["action"] == "removed"
    assert not (folder.with_name("answerback.moved")).exists()
    assert not list((app / "mods").glob(".answerback-remove-*"))


def test_the_tombstone_swapped_after_the_walk_is_not_removed_and_is_named(
    app: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Y2 (ABM-T188): the private name is compared with the open descriptor again once the walk
    has emptied the folder, before the `rmdir`; a folder put at that name during the walk is
    kept, and the report says where what is left is."""
    folder = _older_panel(app)
    swaps: list[Path] = []
    real = os.stat

    def after_the_walk(*args: Any, **kwargs: Any) -> Any:
        if _from_the_installer() and not swaps and str(args[0]).startswith(".answerback-remove-"):
            tombstone = app / "mods" / args[0]
            if tombstone.is_dir() and not list(tombstone.iterdir()):
                tombstone.rename(tombstone.with_name("emptied.moved"))
                tombstone.mkdir()
                swaps.append(tombstone)
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "stat", after_the_walk)
    report = herdr_app.install(app, apply=True, home=home)
    assert len(swaps) == 1
    assert report["success"] is False
    assert report["error"] == (
        f"{folder} changed during removal; what is left of {folder} is at {swaps[0]}"
    )
    assert report["retained"] == {"mods": [swaps[0].name]}
    assert swaps[0].is_dir()
    assert (app / "mods" / "emptied.moved").is_dir()
    assert not folder.exists()


APPEARED = b"// appeared at the old name after the look\n"


def test_a_mismatched_tombstone_is_left_there_and_never_renamed_back(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z1 (ABM-T188F): when the rename to the private name moved something other than the held
    folder (a file put at `mods/answerback` in that instant), it stays at the private name,
    which the error and `retained` give; the old name is not written to again, so an entry
    appearing there is not replaced.

    red at 2d38863: the old name was looked at and, found free, the private name was renamed
    back over it; a file appearing at the old name between that look and the rename (the seam
    here is that look) was replaced by the file from the private name, and the report said
    "changed during removal; retry" with nothing under `retained`. The fixed code never looks,
    so that seam does not fire and the old name is left empty.
    """
    folder = _older_panel(app)
    outside, before = _outside_with_decoys(tmp_path, installed=False)
    swapped: list[Path] = []
    real_rename = os.rename

    def in_the_instant_of_the_rename(*args: Any, **kwargs: Any) -> Any:
        if _from_the_installer() and not swapped and str(args[1]).startswith(".answerback-remove-"):
            _put("file", folder, outside)
            swapped.append(folder)
        return real_rename(*args, **kwargs)

    appeared: list[Path] = []
    real_stat = os.stat

    def at_the_look_at_the_old_name(*args: Any, **kwargs: Any) -> Any:
        try:
            return real_stat(*args, **kwargs)
        except FileNotFoundError:
            if _from_the_installer() and swapped and not appeared and args[0] == "answerback":
                folder.write_bytes(APPEARED)
                appeared.append(folder)
            raise

    monkeypatch.setattr(os, "rename", in_the_instant_of_the_rename)
    monkeypatch.setattr(os, "stat", at_the_look_at_the_old_name)
    report = herdr_app.install(app, apply=True, home=home)
    assert swapped == [folder]
    tombstones = list((app / "mods").glob(".answerback-remove-*"))
    assert len(tombstones) == 1
    left = tombstones[0]
    assert report["success"] is False
    assert report["error"] == (
        f"{folder} changed during removal; what was at that name is now {left}"
    )
    assert report["retained"] == {"mods": [left.name]}
    assert not left.is_symlink() and left.is_file() and left.read_bytes() == PUT_HERE
    # Never renamed back: the file the rename moved is not at the old name. When the old name
    # was looked at (2d38863 only), the file that appeared there is as it was put.
    if appeared:
        assert folder.read_bytes() == APPEARED
    else:
        assert not folder.exists() and not folder.is_symlink()
    assert (folder.with_name("answerback.moved") / "gpui-kit.d.ts").is_file()
    assert _snapshot(outside) == before


def _nested(app: Path) -> Path:
    folder = app / "mods" / "nested"
    (folder / "sub" / "deeper").mkdir(parents=True)
    (folder / "sub" / "deeper" / "file.js").write_text("// deep\n")
    (folder / "sub" / "file.js").write_text("// sub\n")
    (folder / "top.js").write_text("// top\n")
    return folder


@pytest.mark.parametrize("kind", ["symlink", "folder"])
def test_a_child_folder_swapped_during_the_walk_stops_the_removal_and_names_what_is_left(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Y2 (ABM-T188), the walk itself: a child folder is opened without following a link and
    compared with the entry the listing saw; a symlink or another folder put at its name after
    the listing stops the removal, nothing through it is touched, and what is left is named by
    its private name."""
    anchor = herdr_app._root_check(app, home)
    folder = _nested(app)
    outside, before = _outside_with_decoys(tmp_path, installed=False)
    identity = (os.lstat(folder).st_dev, os.lstat(folder).st_ino)
    swaps: list[Path] = []

    def swap() -> None:
        tombstone = next((app / "mods").glob(".answerback-remove-*"))
        _put(kind, tombstone / "sub", outside)
        swaps.append(tombstone)

    fired = _after_the_listing(monkeypatch, swap)
    with pytest.raises(herdr_app.HerdrAppError) as caught:
        herdr_app._remove_tree(folder, app, anchor, identity)
    assert fired and len(swaps) == 1
    message = str(caught.value)
    assert "sub" in message and f"what is left of {folder} is at {swaps[0]}" in message
    assert ("symlink" in message) == (kind == "symlink")
    assert _is(kind, swaps[0] / "sub", outside)
    assert (swaps[0] / "sub.moved" / "deeper" / "file.js").read_text() == "// deep\n"
    assert _snapshot(outside) == before
    assert not folder.exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "folder"])
def test_a_child_file_swapped_after_the_listing_is_not_unlinked_and_is_named(
    app: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Z3 (ABM-T188F): the identity of a child that is not a folder (device, inode, type) is
    taken, without following it, while its folder is listed and compared immediately before
    its unlink; another file, a symlink or a folder put at its name after the listing stops the
    removal, is left where it is, and is named with the private name of what is left.

    red at 2d38863: a child that was not a folder was unlinked with no look at it; the file put
    at `top.js` was removed, the walk went on, and the removal succeeded.
    """
    anchor = herdr_app._root_check(app, home)
    folder = _nested(app)
    outside, before = _outside_with_decoys(tmp_path, installed=False)
    identity = (os.lstat(folder).st_dev, os.lstat(folder).st_ino)
    swaps: list[Path] = []

    def swap() -> None:
        tombstone = next((app / "mods").glob(".answerback-remove-*"))
        _put(kind, tombstone / "top.js", outside)
        swaps.append(tombstone)

    fired = _after_the_listing(monkeypatch, swap)
    with pytest.raises(herdr_app.HerdrAppError) as caught:
        herdr_app._remove_tree(folder, app, anchor, identity)
    assert fired and len(swaps) == 1
    message = str(caught.value)
    assert message == (
        f"{folder / 'top.js'} changed during removal; what is left of {folder} is at {swaps[0]}"
    )
    assert _is(kind, swaps[0] / "top.js", outside)
    assert (swaps[0] / "top.js.moved").read_text() == "// top\n"
    assert _snapshot(outside) == before
    assert not folder.exists()


def test_a_regular_file_where_a_folder_should_be_is_reported_as_not_a_folder(
    app: Path, home: Path
) -> None:
    """Y3 (ABM-T188): `open` with O_NOFOLLOW | O_DIRECTORY says ENOTDIR for a file (and, on
    macOS, for a symlink too), so the entry is looked at to say which.

    red at 7b51462: ENOTDIR was reported as "Refusing to go through a symlink".
    """
    (app / FLEET).write_text("a file where the mod's folder should be\n")
    report = herdr_app.install(app, apply=True, home=home)
    assert report["success"] is False
    assert report["error"] == f"Refusing: not a folder: {app / FLEET}"
    assert (app / FLEET).read_text() == "a file where the mod's folder should be\n"
    anchor = herdr_app._root_check(app, home)
    with pytest.raises(herdr_app.HerdrAppError, match="^Refusing: not a folder: "):
        herdr_app._open_below(app, Path(FLEET), anchor)
    (app / FLEET).unlink()
    (app / FLEET).symlink_to(app / "themes")
    with pytest.raises(herdr_app.HerdrAppError, match="^Refusing to go through a symlink: "):
        herdr_app._open_below(app, Path(FLEET), anchor)


def test_a_staged_name_left_after_link_is_reported_as_a_second_name_of_the_installed_file(
    app: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z5 (ABM-T188F): once `link` has given a file its name, the staged name is a second name
    of the same file; when its unlink fails the file is verified at its name (contents, mode
    0600) and the report says it is installed with a second name at the staged path, which
    `retained` lists.

    red at 2d38863: the report said "The Herdr App folder could not be written" and nothing
    named the staged file that was left.
    """
    failed: list[str] = []
    real = os.unlink

    def the_staged_unlink_fails(*args: Any, **kwargs: Any) -> Any:
        name = str(args[0])
        if (
            _from_the_installer()
            and not failed
            and name.startswith(".answerback-")
            and not name.startswith(".answerback-remove-")
        ):
            failed.append(name)
            raise OSError(errno.EIO, "injected failure at the staged unlink")
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "unlink", the_staged_unlink_fails)
    report = herdr_app.install(app, apply=True, home=home)
    assert len(failed) == 1
    staged = next(app.rglob(failed[0]))
    target = next(
        p
        for p in staged.parent.iterdir()
        if p.name != staged.name and p.stat().st_ino == staged.stat().st_ino
    )
    assert report["success"] is False
    assert report["error"] == f"{target} installed; a second name remains at {staged}"
    assert report["retained"] == {staged.parent.relative_to(app).as_posix(): [staged.name]}
    assert target.stat().st_nlink == 2
    assert target.read_bytes() == _shipped(target.relative_to(app).as_posix())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert report["backup"] is None


def test_a_staged_file_left_by_a_failed_publication_is_named(
    app: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z5 (ABM-T188F), the other side: when the publication failed and the staged file cannot
    be unlinked either, the error keeps the publication's message and names the staged file."""

    def refused(*args: Any, **kwargs: Any) -> None:
        raise herdr_app.HerdrAppError("the publication refused")

    real = os.unlink

    def the_staged_unlink_fails(*args: Any, **kwargs: Any) -> Any:
        if _from_the_installer() and str(args[0]).startswith(".answerback-"):
            raise OSError(errno.EIO, "injected failure at the staged unlink")
        return real(*args, **kwargs)

    monkeypatch.setattr(herdr_app, "_publish_new", refused)
    monkeypatch.setattr(os, "unlink", the_staged_unlink_fails)
    report = herdr_app.install(app, apply=True, home=home)
    staged = list(app.rglob(".answerback-[0-9a-f]*"))
    assert len(staged) == 1
    assert report["success"] is False
    assert report["error"] == f"the publication refused; a staged file remains at {staged[0]}"
    assert report["retained"] == {staged[0].parent.relative_to(app).as_posix(): [staged[0].name]}
    assert staged[0].stat().st_nlink == 1
    assert not any(p for p in (app / FLEET).iterdir() if not p.name.startswith(".answerback-"))


_INJECTED = (
    "open",
    "stat",
    "fstat",
    "mkdir",
    "rename",
    "link",
    "unlink",
    "rmdir",
    "scandir",
    "fchmod",
    "fsync",
)


def _full_step(tmp_path: Path, run: int) -> tuple[Path, Path]:
    """An App folder that makes every mutation do work: an edited main.js (replace, with a
    backup; an update is the same path), themes missing (install) and the older panel (backup
    and removal)."""
    app = tmp_path / f"run-{run}" / "app-config"
    home = tmp_path / f"run-{run}" / "home"
    (app / "mods").mkdir(parents=True)
    (app / "themes").mkdir()
    home.mkdir()
    herdr_app.install(app, apply=True, home=home)
    (app / FLEET / "main.js").write_bytes(b"// the person's own edit\n")
    for theme in app.glob("themes/*.json"):
        theme.unlink()
    _older_panel(app)
    return app, home


def _what_a_failed_step_left_is_named(app: Path, report: dict, tombstoned: bool) -> None:
    """Z2 / Z5 (ABM-T188F): every entry a failed step leaves under a name the person did not
    choose is named in the error and listed under `retained`: the private name of the older
    panel once it was renamed to it (whatever failed after), and a staged file, which after a
    `link` is a second name of the installed file (then verified: contents and mode)."""
    tombstones = list((app / "mods").glob(".answerback-remove-*"))
    retained: dict[str, list[str]] = {}
    if tombstoned:
        assert len(tombstones) == 1, "the older panel was renamed; its private name is gone"
        assert str(tombstones[0]) in report["error"], report["error"]
        retained["mods"] = [tombstones[0].name]
    else:
        assert tombstones == []
    staged = list(app.rglob(".answerback-[0-9a-f]*"))
    assert len(staged) <= 1, staged
    for left in staged:
        assert str(left) in report["error"], report["error"]
        retained[left.parent.relative_to(app).as_posix()] = [left.name]
        if "a second name remains at" in report["error"]:
            target = next(
                p
                for p in left.parent.iterdir()
                if p.name != left.name and p.stat().st_ino == left.stat().st_ino
            )
            assert report["error"] == f"{target} installed; a second name remains at {left}"
            assert target.read_bytes() == _shipped(target.relative_to(app).as_posix())
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        else:
            assert "a staged file remains at" in report["error"]
    assert report.get("retained", {}) == retained


@pytest.mark.parametrize("name", _INJECTED)
def test_an_injected_failure_after_every_open_stage_leaves_no_descriptor_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Y4 (ABM-T188): with every call of one `os` function the installer makes counted in a
    clean run, the step is run once per call with that call failing (EIO), after every
    descriptor opened before it; each run ends in a report, not an exception, the descriptor
    table is as it was (Z6: by device and inode, after a collection), and what the failure
    left under a private or staged name is named (Z2, Z5). The one call whose failure is not a
    failure of the step: the unlink of a staged name that is gone already (renamed over the
    file it replaced); the step is complete and says so."""
    real = getattr(os, name)
    real_rename = os.rename
    calls: list[int] = []
    failing: list[int] = []
    tombstoned: list[str] = []
    gone: list[str] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        if _from_the_installer():
            calls.append(len(calls) + 1)
            if failing and calls[-1] == failing[0]:
                if name == "unlink":
                    try:
                        os.stat(args[0], dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)
                    except FileNotFoundError:
                        gone.append(args[0])
                raise OSError(errno.EIO, f"injected failure at {name} #{failing[0]}")
            result = real(*args, **kwargs)
            if name == "rename" and str(args[1]).startswith(".answerback-remove-"):
                tombstoned.append(args[1])
            return result
        return real(*args, **kwargs)

    def tracked_rename(*args: Any, **kwargs: Any) -> Any:
        result = real_rename(*args, **kwargs)
        if _from_the_installer() and str(args[1]).startswith(".answerback-remove-"):
            tombstoned.append(args[1])
        return result

    def arm() -> None:
        monkeypatch.setattr(os, name, counted)
        if name != "rename":
            monkeypatch.setattr(os, "rename", tracked_rename)

    def disarm() -> None:
        monkeypatch.setattr(os, name, real)
        monkeypatch.setattr(os, "rename", real_rename)

    app, home = _full_step(tmp_path, 0)
    arm()
    report = herdr_app.install(app, apply=True, replace=True, home=home)
    disarm()
    assert report["success"] is True, report.get("error")
    assert report["superseded"]["action"] == "removed"
    assert len(tombstoned) == 1
    total = len(calls)
    assert total > 0, f"the step never calls os.{name}"
    for k in range(1, total + 1):
        app, home = _full_step(tmp_path, k)
        calls.clear()
        tombstoned.clear()
        gone.clear()
        failing[:] = [k]
        arm()
        try:
            with no_descriptor_left():
                report = herdr_app.install(app, apply=True, replace=True, home=home)
        finally:
            disarm()
        if gone:
            assert report["success"] is True, (name, k, report.get("error"))
            assert not list(app.rglob(".answerback-*")), (name, k)
            continue
        assert report["success"] is False, (name, k)
        _what_a_failed_step_left_is_named(app, report, bool(tombstoned))


# ---------------------------------------------------------------- the command line


def _run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    monkey_argv = ["answerback", *argv]
    old = sys.argv
    sys.argv = monkey_argv
    try:
        try:
            cli.main()
            code = 0
        except SystemExit as stop:
            code = int(stop.code or 0)
    finally:
        sys.argv = old
    return code, json.loads(capsys.readouterr().out)


def test_the_command_previews_applies_and_reports(
    app: Path, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(home))
    code, preview = _run_cli(["herdr-app", "install", "--directory", str(app)], capsys)
    assert code == 0 and preview["applied"] is False
    assert not (app / FLEET).exists()
    code, applied = _run_cli(["herdr-app", "install", "--directory", str(app), "--apply"], capsys)
    assert code == 0 and applied["success"] is True
    monkeypatch.setenv("HERDR_APP_CONFIG", str(app))
    code, status = _run_cli(["herdr-app", "status"], capsys)
    assert code == 0 and status["directory"] == str(app)
    assert set(_states(status).values()) == {"installed"}
    code, removed = _run_cli(["herdr-app", "uninstall", "--apply"], capsys)
    assert code == 0 and not (app / FLEET / "main.js").exists()


def test_a_refused_install_exits_non_zero(
    app: Path,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "elsewhere").mkdir()
    (app / FLEET).symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    code, report = _run_cli(["herdr-app", "install", "--directory", str(app), "--apply"], capsys)
    assert code != 0 and report["success"] is False


# ---------------------------------------------------------------- the mod's own tests


@pytest.mark.parametrize("name", ["footer.test.mjs", "receipt-detail.test.mjs"])
def test_the_mod_node_tests_pass(name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the Herdr App mod's tests")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "herdr-app" / name)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
