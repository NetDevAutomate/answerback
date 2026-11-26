"""Config.load reports an unreadable or incompatible config as one clear ValueError.

A dataclass raises a raw TypeError for an unknown key, `json.loads` a JSONDecodeError
and a list where a mapping belongs an AttributeError inside `__post_init__`. None of
those is caught by the callers' `except (BridgeError, OSError, ValueError)`, so
`serve` used to die with a traceback and launchd restarted it every ten seconds.
Every load failure must be a ValueError that names the config path and, where there
is one, the failing field.
"""

import errno
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.config import Config


def test_load_rejects_an_unknown_key_with_a_clear_message_naming_path_and_field(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"herdr_session": "fixture", "some_removed_field": "x"}))
    with pytest.raises(ValueError, match="incompatible with this version") as info:
        Config.load(path)
    message = str(info.value)
    assert str(path) in message
    assert "some_removed_field" in message
    # Never the dataclass's raw TypeError: serve()/main() only expect ValueError.
    assert not isinstance(info.value, TypeError)


def test_load_rejects_malformed_json_as_a_value_error_naming_the_path(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not valid json")
    with pytest.raises(ValueError, match="not valid JSON") as info:
        Config.load(path)
    assert str(path) in str(info.value)
    assert not isinstance(info.value, json.JSONDecodeError)


def test_load_rejects_a_list_where_a_mapping_belongs_and_names_the_field(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"desktop_threads": []}))
    with pytest.raises(ValueError, match="desktop_threads") as info:
        Config.load(path)
    assert str(path) in str(info.value)
    assert "mapping" in str(info.value)


def test_load_rejects_a_mapping_where_a_list_belongs_and_names_the_field(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"client_registrations": {"kiro": True}}))
    with pytest.raises(ValueError, match="client_registrations") as info:
        Config.load(path)
    assert "list" in str(info.value)


def test_load_rejects_a_top_level_array_naming_the_path(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="JSON object") as info:
        Config.load(path)
    assert str(path) in str(info.value)


def test_load_prefixes_a_validation_failure_with_the_path(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"crew_enabled": "yes"}))
    with pytest.raises(ValueError, match="Provider enablement flags must be booleans") as info:
        Config.load(path)
    assert str(path) in str(info.value)


def test_load_missing_file_keeps_its_existing_distinct_message(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "config.json"
    with pytest.raises(ValueError, match="No bridge config at"):
        Config.load(path)


def test_load_missing_component_stops_before_reading_an_unchecked_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: a missing component must end validation before any later read."""
    path = tmp_path / "missing" / "config.json"

    def unchecked_read(selected: Path, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("A missing path was read without ownership validation")

    monkeypatch.setattr(Path, "is_file", lambda _: True)
    monkeypatch.setattr(Path, "read_text", unchecked_read)
    with pytest.raises(ValueError, match="No bridge config at"):
        Config.load(path)


def test_load_still_accepts_a_compatible_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    Config(herdr_session="fixture-session").save(path)
    assert Config.load(path).herdr_session == "fixture-session"


def test_load_never_rewrites_a_rejected_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = '{"desktop_threads": [], "stale": 1}\n'
    path.write_text(original)
    with pytest.raises(ValueError):
        Config.load(path)
    assert path.read_text() == original


@pytest.fixture
def owned_config(tmp_path: Path) -> Path:
    path = tmp_path / "owned" / "bridge" / "config.json"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text('{"herdr_session": "fixture"}\n')
    return path


def config_io(operation: str, path: Path) -> None:
    if operation == "load":
        Config.load(path)
    else:
        Config(herdr_session="replacement").save(path)


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("location", ["file", "parent", "ancestor", "broken-file", "broken-parent"])
def test_config_io_refuses_foreign_symlinked_paths(
    owned_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    location: str,
) -> None:
    """red_at_base: neither entry point may trust another account's symlink."""
    original = owned_config.read_bytes()
    link = tmp_path / "link"
    target, suffix = {
        "file": (owned_config, ""),
        "parent": (owned_config.parent, "config.json"),
        "ancestor": (owned_config.parent.parent, "bridge/config.json"),
        "broken-file": (tmp_path / "missing.json", ""),
        "broken-parent": (tmp_path / "missing", "bridge/config.json"),
    }[location]
    link.symlink_to(target, target_is_directory=bool(suffix))
    mock_path_owner(monkeypatch, link, os.getuid() + 1)
    selected = link / suffix if suffix else link
    with pytest.raises(ValueError, match="foreign ownership|symlinked config path") as error:
        config_io(operation, selected)
    assert str(link) in str(error.value)
    assert link.is_symlink()
    assert owned_config.read_bytes() == original
    assert not (tmp_path / "missing").exists()


def mock_path_owner(
    monkeypatch: pytest.MonkeyPatch, selected: Path, uid: int, mode: int | None = None
) -> None:
    """Model another owner without changing any real file's ownership."""
    original = Path.lstat

    def observed(path: Path) -> os.stat_result:
        info = original(path)
        if path == selected:
            values = list(info)
            values[4] = uid
            if mode is not None:
                values[0] = stat.S_IFMT(info.st_mode) | mode
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", observed)


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("location", ["file", "parent", "ancestor"])
def test_config_io_refuses_foreign_owned_resolved_paths(
    owned_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    location: str,
) -> None:
    """red_at_base: resolving a link upstream must not bypass ownership checks."""
    link = tmp_path / "link"
    link.symlink_to(owned_config.parent, target_is_directory=True)
    selected = (link / "config.json").expanduser().resolve()
    component = {
        "file": selected,
        "parent": selected.parent,
        "ancestor": selected.parent.parent,
    }[location]
    original = owned_config.read_bytes()
    mock_path_owner(monkeypatch, component, os.getuid() + 1)
    with pytest.raises(ValueError, match="foreign ownership") as error:
        config_io(operation, selected)
    assert str(component) in str(error.value)
    assert owned_config.read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("location", ["file", "parent", "ancestor"])
@pytest.mark.parametrize("write_bit", [stat.S_IWGRP, stat.S_IWOTH])
def test_config_io_refuses_other_writable_paths(
    owned_config: Path, operation: str, location: str, write_bit: int
) -> None:
    """red_at_base: ownership alone does not prevent another user changing config."""
    component = {
        "file": owned_config,
        "parent": owned_config.parent,
        "ancestor": owned_config.parent.parent,
    }[location]
    component.chmod(stat.S_IMODE(component.stat().st_mode) | write_bit)
    original = owned_config.read_bytes()
    before_mode = component.stat().st_mode
    with pytest.raises(ValueError, match="writable by another user") as error:
        config_io(operation, owned_config)
    assert str(component) in str(error.value)
    assert component.stat().st_mode == before_mode
    assert owned_config.read_bytes() == original


@pytest.mark.parametrize("unsafe", ["symlink", "foreign", "writable"])
def test_config_save_refuses_unsafe_ancestor_before_creating_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    """red_at_base: rejection must precede mkdir or a write into an unsafe tree."""
    parent = tmp_path / "existing"
    parent.mkdir()
    selected = parent
    if unsafe == "symlink":
        selected = tmp_path / "link"
        selected.symlink_to(parent, target_is_directory=True)
        mock_path_owner(monkeypatch, selected, os.getuid() + 1)
    elif unsafe == "foreign":
        mock_path_owner(monkeypatch, parent, os.getuid() + 1)
    else:
        parent.chmod(0o777)
    with pytest.raises(ValueError):
        Config().save(selected / "new" / "bridge" / "config.json")
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("operation", ["load", "save"])
def test_config_io_refuses_parent_traversal(owned_config: Path, operation: str) -> None:
    """red_at_base: lexical validation must not erase a traversed component."""
    selected = owned_config.parent / ".." / owned_config.parent.name / owned_config.name
    original = owned_config.read_bytes()
    with pytest.raises(ValueError, match="parent traversal"):
        config_io(operation, selected)
    assert owned_config.read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save"])
def test_config_io_refuses_a_nonregular_file(tmp_path: Path, operation: str) -> None:
    """red_at_base: both entry points report a directory leaf as invalid config."""
    path = tmp_path / "config.json"
    path.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        config_io(operation, path)
    assert path.is_dir()
    assert list(path.iterdir()) == []


@pytest.mark.parametrize("mode", [0o755, 0o1777])
def test_config_io_accepts_protected_system_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """pins_existing: system prefixes and sticky root namespaces remain usable."""
    system = tmp_path / "system"
    system.mkdir()
    mock_path_owner(monkeypatch, system, 0, mode)
    path = system / "owned" / "config.json"
    Config(herdr_session="fixture").save(path)
    assert Config.load(path).herdr_session == "fixture"


def test_config_io_accepts_relative_private_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: an explicit relative CLI config path remains supported."""
    monkeypatch.chdir(tmp_path)
    path = Path("bridge/config.json")
    Config(herdr_session="fixture").save(path)
    assert Config.load(path).herdr_session == "fixture"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_config_save_creates_all_missing_directories_privately(tmp_path: Path) -> None:
    """red_at_base: each newly created config ancestor must start private."""
    path = tmp_path / "new" / "bridge" / "config.json"
    Config().save(path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o700, 0o777])
def test_first_save_rechecks_a_concurrently_created_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """Safe: pins_existing at BASE; unsafe: red_at_base; both red_at_review_tip."""
    path = tmp_path / "new" / "config.json"
    original = Path.mkdir

    def concurrent_mkdir(directory: Path, *args: Any, **kwargs: Any) -> None:
        race = directory == path.parent and not directory.exists()
        original(directory, *args, **kwargs)
        if race:
            directory.chmod(mode)
            if not kwargs.get("exist_ok", False):
                raise FileExistsError("Another process created the config directory")

    monkeypatch.setattr(Path, "mkdir", concurrent_mkdir)
    if mode == 0o700:
        Config(herdr_session="fixture").save(path)
        assert Config.load(path).herdr_session == "fixture"
    else:
        with pytest.raises(ValueError, match="writable by another user"):
            Config().save(path)
        assert not path.exists()
    assert stat.S_IMODE(path.parent.stat().st_mode) == mode


@pytest.mark.parametrize("prefix", ["/tmp", "/var/tmp"])
def test_config_io_accepts_lexical_system_temporary_paths(prefix: str) -> None:
    """pins_existing at BASE; red_at_review_tip on macOS: probes A1 and A3."""
    with tempfile.TemporaryDirectory(prefix="awb-config-", dir=prefix) as directory:
        path = Path(directory) / "bridge" / "config.json"
        Config(herdr_session="fixture").save(path)
        assert Config.load(path) == Config.load(path.resolve())
        assert Config.load(path).herdr_session == "fixture"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("owner", ["current", "root"])
@pytest.mark.parametrize("location", ["parent", "ancestor"])
@pytest.mark.parametrize("target_form", ["absolute", "relative", "relative-parent"])
def test_config_io_follows_trusted_directory_links(
    owned_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    owner: str,
    location: str,
    target_form: str,
) -> None:
    """pins_existing at BASE; red_at_review_tip: probe C1 and target spelling variants."""
    links = tmp_path / "links"
    links.mkdir(mode=0o700)
    link = links / "config-root"
    target = owned_config.parent if location == "parent" else owned_config.parent.parent
    suffix = Path("config.json") if location == "parent" else Path("bridge/config.json")
    if target_form == "relative":
        link = tmp_path / "config-root"
        target = target.relative_to(tmp_path)
    elif target_form == "relative-parent":
        target = Path("..") / target.relative_to(tmp_path)
    link.symlink_to(target, target_is_directory=True)
    if owner == "root":
        mock_path_owner(monkeypatch, link, 0)
    config_io(operation, link / suffix)
    assert Config.load(link / suffix) == Config.load((link / suffix).resolve())
    expected = "fixture" if operation == "load" else "replacement"
    assert Config.load(owned_config).herdr_session == expected
    assert link.is_symlink()


@pytest.mark.parametrize("owner", ["current", "root"])
def test_config_load_follows_trusted_file_link_but_save_refuses_it(
    owned_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    """Load: pins_existing at BASE, red_at_review_tip; save refusal: red_at_base."""
    link = tmp_path / "config.json"
    link.symlink_to(owned_config)
    if owner == "root":
        mock_path_owner(monkeypatch, link, 0)
    original = owned_config.read_bytes()
    assert Config.load(link).herdr_session == "fixture"
    with pytest.raises(ValueError, match="symlinked config path"):
        Config().save(link)
    assert link.is_symlink()
    assert owned_config.read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("owner", ["current", "root", "foreign"])
def test_config_io_refuses_links_in_sticky_system_directory(
    owned_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    owner: str,
) -> None:
    """red_at_base and MM2 tip: a link's owner does not prove who placed its name."""
    system = tmp_path / "system"
    system.mkdir(mode=0o700)
    link = system / "config-root"
    link.symlink_to(owned_config.parent, target_is_directory=True)
    mock_path_owner(monkeypatch, system, 0, 0o1777)
    if owner != "current":
        mock_path_owner(monkeypatch, link, 0 if owner == "root" else os.getuid() + 1)
    original = owned_config.read_bytes()
    with pytest.raises(
        ValueError, match="symlink in a directory writable by another user"
    ) as error:
        config_io(operation, link / "config.json")
    assert str(error.value) == (
        f"Config path has a symlink in a directory writable by another user: {link}"
    )
    assert link.is_symlink()
    assert owned_config.read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("sticky", [False, True])
@pytest.mark.parametrize("remove_original", [False, True])
def test_config_io_checks_directory_of_hard_linked_symlink(
    owned_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    sticky: bool,
    remove_original: bool,
) -> None:
    """Sticky: red_at_base and MM2 tip; private: pins_existing, whatever link count."""
    source = tmp_path / "my-link"
    source.symlink_to(owned_config.parent, target_is_directory=True)
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o700)
    link = destination / "config-root"
    try:
        os.link(source, link, follow_symlinks=False)
    except NotImplementedError as error:
        pytest.skip(f"Platform cannot hard-link a symlink: {error}")
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        pytest.skip(f"Platform refuses to hard-link a symlink: {error}")
    assert link.is_symlink()
    assert link.lstat().st_ino == source.lstat().st_ino
    assert link.lstat().st_uid == os.getuid()
    assert link.lstat().st_nlink == 2
    if remove_original:
        source.unlink()
    assert link.lstat().st_nlink == (1 if remove_original else 2)
    original = owned_config.read_bytes()
    if sticky:
        mock_path_owner(monkeypatch, destination, 0, 0o1777)
        with pytest.raises(
            ValueError, match="symlink in a directory writable by another user"
        ) as error:
            config_io(operation, link / "config.json")
        assert str(error.value) == (
            f"Config path has a symlink in a directory writable by another user: {link}"
        )
        assert owned_config.read_bytes() == original
    else:
        config_io(operation, link / "config.json")
        expected = "fixture" if operation == "load" else "replacement"
        assert Config.load(link / "config.json").herdr_session == expected
        assert Config.load(owned_config).herdr_session == expected
    assert link.is_symlink()


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("route", ["link-target", "parent-step"])
@pytest.mark.parametrize("mode", [0o1770, 0o1707])
def test_config_io_refuses_sticky_directory_link_reached_through_target(
    owned_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    route: str,
    mode: int,
) -> None:
    """red_at_base and MM2 tip: current's permissions must follow target and '..' steps."""
    system = tmp_path / "system"
    system.mkdir(mode=0o700)
    private = system / "private"
    private.mkdir(mode=0o700)
    link = system / "config-root"
    link.symlink_to(owned_config.parent, target_is_directory=True)
    entry = tmp_path / "entry"
    entry.symlink_to(system if route == "link-target" else private / "..", target_is_directory=True)
    mock_path_owner(monkeypatch, system, 0, mode)
    original = owned_config.read_bytes()
    with pytest.raises(
        ValueError, match="symlink in a directory writable by another user"
    ) as error:
        config_io(operation, entry / "config-root" / "config.json")
    assert str(error.value) == (
        f"Config path has a symlink in a directory writable by another user: {link}"
    )
    assert link.is_symlink()
    assert entry.is_symlink()
    assert owned_config.read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("location", ["link-directory", "target-parent", "target-detour"])
def test_config_io_refuses_writable_directories_on_either_side_of_link(
    owned_config: Path, tmp_path: Path, operation: str, location: str
) -> None:
    """red_at_base: target expansion cannot erase an unsafe traversed directory."""
    links = tmp_path / "links"
    links.mkdir()
    link = links / "config-root"
    target = owned_config.parent
    if location == "link-directory":
        unsafe = links
    elif location == "target-parent":
        unsafe = target
    else:
        unsafe = tmp_path / "detour"
        unsafe.mkdir()
        target = Path("..") / "detour" / ".." / "owned" / "bridge"
    unsafe.chmod(0o775)
    link.symlink_to(target, target_is_directory=True)
    original = owned_config.read_bytes()
    with pytest.raises(ValueError, match="writable by another user") as error:
        config_io(operation, link / "config.json")
    assert str(unsafe) in str(error.value)
    assert owned_config.read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("location", ["file", "parent", "ancestor"])
def test_config_io_checks_foreign_ownership_after_following_a_link(
    owned_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    location: str,
) -> None:
    """red_at_base: trusted links do not grant trust to their targets."""
    link = tmp_path / "config-root"
    link.symlink_to(owned_config.parent, target_is_directory=True)
    component = {
        "file": owned_config,
        "parent": owned_config.parent,
        "ancestor": owned_config.parent.parent,
    }[location]
    mock_path_owner(monkeypatch, component, os.getuid() + 1)
    original = owned_config.read_bytes()
    with pytest.raises(ValueError, match="foreign ownership") as error:
        config_io(operation, link / "config.json")
    assert str(component) in str(error.value)
    assert owned_config.read_bytes() == original


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("kind", ["loop", "long-chain", "dangling"])
def test_config_io_bounds_link_expansion(tmp_path: Path, operation: str, kind: str) -> None:
    """red_at_base: bound expansion and distinguish dangling links from excessive hops."""
    link = tmp_path / "config-root"
    if kind == "loop":
        link.symlink_to("config-root", target_is_directory=True)
    elif kind == "long-chain":
        target = tmp_path / "target"
        target.mkdir()
        (target / "config.json").write_text("{}")
        for index in range(32):
            previous = tmp_path / f"link-{index}"
            previous.symlink_to(target, target_is_directory=True)
            target = previous
        link.symlink_to(target, target_is_directory=True)
    else:
        link.symlink_to("missing", target_is_directory=True)
    message = "dangling symlink" if kind == "dangling" else "too many symlink hops"
    with pytest.raises(ValueError, match=message):
        config_io(operation, link / "config.json")
    assert not (tmp_path / "missing").exists()
    assert link.is_symlink()


def test_config_save_creates_missing_children_after_trusted_link(tmp_path: Path) -> None:
    """red_at_base: children beyond an existing target must be created privately."""
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "config-root"
    link.symlink_to(target, target_is_directory=True)
    Config(herdr_session="fixture").save(link / "new" / "bridge" / "config.json")
    assert Config.load(target / "new" / "bridge" / "config.json").herdr_session == "fixture"
    assert stat.S_IMODE((target / "new").stat().st_mode) == 0o700
    assert stat.S_IMODE((target / "new" / "bridge").stat().st_mode) == 0o700


@pytest.mark.parametrize("operation", ["load", "save"])
def test_config_directory_itself_cannot_be_a_root_owned_system_prefix(
    owned_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """red_at_base: the immediate directory never receives the root exemption."""
    # Model a non-root caller even when this suite runs under root in CI.
    if os.getuid() == 0:
        monkeypatch.setattr(os, "getuid", lambda: 1000)
    link = tmp_path / "config-root"
    link.symlink_to(owned_config.parent, target_is_directory=True)
    mock_path_owner(monkeypatch, owned_config.parent, 0, 0o755)
    with pytest.raises(ValueError, match="foreign ownership") as error:
        config_io(operation, link / "config.json")
    assert str(owned_config.parent) in str(error.value)


def test_config_load_follows_a_short_chain_and_rejects_a_directory_target(
    owned_config: Path, tmp_path: Path
) -> None:
    """red_at_base: preserve valid chains and give directory targets a type refusal."""
    inner = tmp_path / "inner"
    inner.symlink_to(owned_config.parent, target_is_directory=True)
    outer = tmp_path / "outer"
    outer.symlink_to("inner", target_is_directory=True)
    assert Config.load(outer / "config.json") == Config.load(owned_config)
    with pytest.raises(ValueError, match="regular file"):
        Config.load(outer)


def test_new_config_starts_with_no_registered_providers() -> None:
    """red_at_base: a new Config must not implicitly opt into Desktop access."""
    assert Config().registered_providers() == []


@pytest.mark.parametrize("directory", ["answerback-bridge", "herdr-bridge", "codex-herdr-bridge"])
@pytest.mark.parametrize("raw", [{}, {"herdr_session": "fixture"}, {"desktop_threads": {}}])
def test_load_without_codex_intent_keeps_desktop_disabled(
    tmp_path: Path, directory: str, raw: dict[str, Any]
) -> None:
    """red_at_base: a missing flag or a legacy directory name is not provider intent."""
    path = tmp_path / directory / "config.json"
    path.parent.mkdir()
    original = json.dumps(raw)
    path.write_text(original)
    config = Config.load(path)
    assert config.codex_enabled is False
    assert config.registered_providers() == (["herdr"] if "herdr_session" in raw else [])
    assert path.read_text() == original


@pytest.mark.parametrize("directory", ["answerback-bridge", "herdr-bridge", "codex-herdr-bridge"])
def test_load_legacy_desktop_pairing_preserves_provider(tmp_path: Path, directory: str) -> None:
    """pins_existing at BASE; red_at_review_tip: a saved pairing is Desktop intent."""
    path = tmp_path / directory / "config.json"
    path.parent.mkdir()
    threads = {"planner": "11111111-2222-3333-4444-555555555555"}
    original = json.dumps({"herdr_session": "fixture", "desktop_threads": threads})
    path.write_text(original)
    config = Config.load(path)
    assert config.codex_enabled is True
    assert config.registered_providers() == ["herdr", "codex"]
    assert config.desktop_threads == threads
    assert path.read_text() == original
    config.save(path)
    assert json.loads(path.read_text())["providers"]["codex"]["enabled"] is True
    assert Config.load(path).desktop_threads == threads


def test_legacy_pairing_requires_a_codex_executable(tmp_path: Path) -> None:
    """pins_existing at BASE; red_at_review_tip: inferred selection is validated too."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "desktop_threads": {"planner": "11111111-2222-3333-4444-555555555555"},
                "codex_binary": "",
            }
        )
    )
    with pytest.raises(ValueError, match="Codex executable is required"):
        Config.load(path)


@pytest.mark.parametrize(
    ("choice", "flag", "enabled"),
    [
        ({"codex_enabled": False}, False, False),
        ({"codex_enabled": True}, True, True),
        ({"provider_selection": []}, False, False),
        ({"provider_selection": ["codex"]}, False, True),
        ({"provider_selection": None}, False, False),
    ],
)
def test_explicit_choice_wins_over_legacy_pairing(
    tmp_path: Path, choice: dict[str, Any], flag: bool, enabled: bool
) -> None:
    """Flags: pins_existing at BASE; absent flag: red_at_base; all pin the review tip."""
    path = tmp_path / "config.json"
    threads = {"planner": "11111111-2222-3333-4444-555555555555"}
    path.write_text(json.dumps({"desktop_threads": threads, **choice}))
    config = Config.load(path)
    assert config.codex_enabled is flag
    assert config.provider_enabled("codex") is enabled
    config.save(path)
    assert Config.load(path).provider_enabled("codex") is enabled
    assert Config.load(path).desktop_threads == threads


@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_explicit_codex_flag_survives_load_and_save(tmp_path: Path, enabled: bool) -> None:
    """pins_existing: releases from 0.5.0 serialize the explicit boolean."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"herdr_session": "fixture", "codex_enabled": enabled}))
    config = Config.load(path)
    assert config.provider_enabled("codex") is enabled
    assert config.provider_selection is None
    config.save(path)
    # The flag stays explicit in a written block; an all-default unselected codex is omitted.
    written = json.loads(path.read_text())["providers"].get("codex", {"enabled": False})
    assert written["enabled"] is enabled
    assert Config.load(path).provider_enabled("codex") is enabled


@pytest.mark.parametrize(
    ("selected", "enabled", "expected"), [([], True, []), (["codex"], False, ["codex"])]
)
def test_explicit_provider_selection_retains_precedence(
    tmp_path: Path, selected: list[str], enabled: bool, expected: list[str]
) -> None:
    """pins_existing: the registry list remains authoritative over legacy flags."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider_selection": selected, "codex_enabled": enabled}))
    assert Config.load(path).registered_providers() == expected


@pytest.mark.parametrize("method", ["desktop_read", "desktop_submit", "desktop_sessions"])
async def test_config_without_intent_refuses_desktop_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """red_at_base: the real dispatcher refuses before touching a Desktop provider."""
    from answerback_bridge.service import Bridge

    path = tmp_path / "config.json"
    path.write_text("{}")
    bridge = Bridge(Config.load(path), path)

    async def unexpected_provider_call() -> None:
        raise AssertionError("An unselected Desktop provider must not be called")

    monkeypatch.setattr(bridge, method, unexpected_provider_call)
    try:
        with pytest.raises(ValueError, match="codex provider is not registered"):
            await bridge.call(method, {})
    finally:
        await bridge.close()


def test_kiro_acp_policy_defaults_preserve_v3_manual_approval(tmp_path: Path) -> None:
    config = Config()
    assert config.kiro_acp_engine == "v3"
    assert config.kiro_trust_all_tools is False
    assert config.kiro_trust_tools == []

    path = tmp_path / "config.json"
    config.save(path)
    saved = json.loads(path.read_text())
    # An unselected Kiro at its defaults is not written at all; a written block is whole.
    assert "kiro" not in saved["providers"]
    config.select_provider("kiro", True)
    config.save(path)
    kiro = json.loads(path.read_text())["providers"]["kiro"]
    assert (kiro["acp_engine"], kiro["trust_all_tools"], kiro["trust_tools"]) == ("v3", False, [])


def test_kiro_v2_trust_tools_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    Config(kiro_acp_engine="v2", kiro_trust_tools=["read", "@answerback_bridge/task_wait"]).save(
        path
    )
    saved = json.loads(path.read_text())
    assert saved["providers"]["kiro"]["trust_tools"] == ["read", "@answerback_bridge/task_wait"]
    restored = Config.load(path)
    assert restored.kiro_acp_engine == "v2"
    assert restored.kiro_trust_tools == ["read", "@answerback_bridge/task_wait"]
    assert restored.kiro_trust_all_tools is False


@pytest.mark.parametrize(
    "trust_tools",
    [
        [""],
        ["read", ""],
        [1],
        [None],
        ["read,write"],
        ["read write"],
        "read",
        {"read": True},
        # A leading dash would let a "tool name" become another launch flag.
        ["-a"],
        ["--trust-tools"],
        ["read", "-a"],
        ["read -a"],
    ],
)
def test_kiro_trust_tools_rejects_malformed_entries(trust_tools: Any) -> None:
    with pytest.raises(ValueError, match="Kiro trust-tools"):
        Config(kiro_acp_engine="v2", kiro_trust_tools=trust_tools)


def test_kiro_trust_tools_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="lists 'read' twice"):
        Config(kiro_acp_engine="v2", kiro_trust_tools=["read", "read"])


def test_kiro_trust_tools_is_v2_only() -> None:
    with pytest.raises(ValueError, match="--trust-tools is a v2-only flag"):
        Config(kiro_acp_engine="v3", kiro_trust_tools=["read"])


def test_kiro_trust_tools_excludes_trust_all_tools() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        Config(kiro_acp_engine="v2", kiro_trust_all_tools=True, kiro_trust_tools=["read"])


def test_kiro_trust_tools_saved_as_a_non_list_is_refused_on_load(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"kiro_acp_engine": "v2", "kiro_trust_tools": "read"}))
    with pytest.raises(ValueError, match="'kiro_trust_tools' must be a list"):
        Config.load(path)


@pytest.mark.parametrize("trust_all_tools", [False, True])
def test_kiro_v2_acp_policy_round_trips(tmp_path: Path, trust_all_tools: bool) -> None:
    path = tmp_path / "config.json"
    Config(kiro_acp_engine="v2", kiro_trust_all_tools=trust_all_tools).save(path)
    restored = Config.load(path)
    assert restored.kiro_acp_engine == "v2"
    assert restored.kiro_trust_all_tools is trust_all_tools


@pytest.mark.parametrize("engine", ["v1", "V2", "", 2, None])
def test_kiro_acp_policy_rejects_unknown_engines(engine: Any) -> None:
    with pytest.raises(ValueError, match="Kiro ACP engine must be 'v2' or 'v3'"):
        Config(kiro_acp_engine=engine)


@pytest.mark.parametrize("trust_all_tools", [0, 1, "yes", None])
def test_kiro_acp_policy_rejects_non_boolean_trust(trust_all_tools: Any) -> None:
    with pytest.raises(ValueError, match="Kiro trust-all-tools switch must be a boolean"):
        Config(kiro_acp_engine="v2", kiro_trust_all_tools=trust_all_tools)


def test_kiro_v3_rejects_v2_only_trust_all_tools_flag() -> None:
    with pytest.raises(ValueError, match="--trust-all-tools.*v2-only"):
        Config(kiro_acp_engine="v3", kiro_trust_all_tools=True)


def test_owned_worker_active_budgets_default_absent_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    defaults = Config()
    assert defaults.kiro_active_budget == 600
    assert defaults.codex_cli_active_budget == 600
    assert defaults.claude_code_active_budget == 600
    assert defaults.grok_active_budget == 600
    defaults.save(path)
    assert json.loads(path.read_text())["providers"] == {}

    Config(
        kiro_active_budget=601,
        codex_cli_active_budget=602,
        claude_code_active_budget=603,
        grok_active_budget=604,
    ).save(path)
    restored = Config.load(path)
    assert (
        restored.kiro_active_budget,
        restored.codex_cli_active_budget,
        restored.claude_code_active_budget,
        restored.grok_active_budget,
    ) == (601, 602, 603, 604)


@pytest.mark.parametrize("seconds", [59, 14_401, 60.0, "600", True])
def test_owned_worker_active_budget_refuses_outside_integer_range(seconds: Any) -> None:
    with pytest.raises(ValueError, match="active execution budget must be 60–14400 seconds"):
        Config(kiro_active_budget=seconds)
