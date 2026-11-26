"""Every folder the bridge creates is private, whatever the umask of its caller.

`private_directory(path)` makes `path` with mode 0700. `Path.mkdir(mode=..., parents=True)`
gives the mode to the last folder only: a missing folder above `path` got the default mode,
which the umask decides (0775 under umask 002, 0755 under 022). `Config.load` and
`Config.save` refuse a configuration below a folder that group members or others can write,
so under umask 002 the bridge refused a configuration folder that it had just made itself
(a first bus setup, live-test provisioning into a folder whose parent is missing).

These tests pin that each folder the call creates has mode 0700, that a folder which exists
before the call is left exactly as it is, and that what the call does to `path` itself stands
as before. The last test pins the same for `Config.save` alone.

The suite runs under its own umask (022, see tests/conftest.py), so each test sets the umask
itself: 002, 022 and 077. The autouse fixture `umask_restored` of tests/conftest.py puts the
umask of before the test back after it.
"""

import os
import stat
from pathlib import Path

import pytest

from answerback_bridge import transport
from answerback_bridge.config import Config
from answerback_bridge.process import BridgeError
from answerback_bridge.transport import private_directory

UMASKS = pytest.mark.parametrize("mask", [0o002, 0o022, 0o077], ids=["002", "022", "077"])


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@UMASKS
def test_every_missing_folder_above_the_path_is_created_private(tmp_path: Path, mask: int) -> None:
    path = tmp_path / "first" / "second" / "leaf"
    os.umask(mask)

    private_directory(path)

    assert mode(path) == 0o700
    assert mode(path.parent) == 0o700
    assert mode(path.parent.parent) == 0o700


@UMASKS
@pytest.mark.parametrize("existing", [0o755, 0o775], ids=["0755", "0775"])
def test_a_folder_that_exists_above_the_path_keeps_its_mode(
    tmp_path: Path, mask: int, existing: int
) -> None:
    above = tmp_path / "exists"
    above.mkdir()
    above.chmod(existing)
    path = above / "new" / "leaf"
    os.umask(mask)

    private_directory(path)

    assert mode(above) == existing
    assert mode(path.parent) == 0o700
    assert mode(path) == 0o700


@UMASKS
def test_a_path_that_exists_with_a_wider_mode_is_made_private(tmp_path: Path, mask: int) -> None:
    """What the call did to `path` itself before this change; it stands."""
    path = tmp_path / "leaf"
    path.mkdir()
    path.chmod(0o755)
    os.umask(mask)

    private_directory(path)

    assert mode(path) == 0o700


@UMASKS
def test_a_regular_file_at_the_path_is_refused_and_left_alone(tmp_path: Path, mask: int) -> None:
    """`Path.mkdir` refuses a file before the bridge's own check sees it; that stands."""
    path = tmp_path / "leaf"
    path.write_text("not a folder\n")
    path.chmod(0o644)
    os.umask(mask)

    with pytest.raises(FileExistsError):
        private_directory(path)

    assert path.read_text() == "not a folder\n"
    assert mode(path) == 0o644


@UMASKS
def test_a_link_to_a_folder_at_the_path_is_refused_by_the_bridge(tmp_path: Path, mask: int) -> None:
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    path = tmp_path / "leaf"
    path.symlink_to(target)
    os.umask(mask)

    with pytest.raises(
        BridgeError, match="^Bridge runtime must be a directory owned by the current user$"
    ):
        private_directory(path)

    assert path.is_symlink()
    assert mode(target) == 0o755


@UMASKS
def test_a_second_call_changes_nothing_and_does_not_fail(tmp_path: Path, mask: int) -> None:
    path = tmp_path / "first" / "leaf"
    os.umask(mask)
    private_directory(path)
    (path / "kept").write_text("kept\n")
    before = [(mode(folder), folder.lstat().st_ino) for folder in (path, path.parent)]

    private_directory(path)

    assert [(mode(folder), folder.lstat().st_ino) for folder in (path, path.parent)] == before
    assert before == [(0o700, before[0][1]), (0o700, before[1][1])]
    assert (path / "kept").read_text() == "kept\n"


@UMASKS
def test_a_folder_that_another_creator_made_in_between_is_accepted(
    tmp_path: Path, mask: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers at once must not fail each other; the other's folder is left as it is."""
    above = tmp_path / "raced"
    path = above / "leaf"
    real_mkdir = Path.mkdir
    raced: list[Path] = []

    def mkdir_after_another_creator(self: Path, *args: object, **kwargs: object) -> None:
        if self == above and not raced:
            raced.append(self)
            os.mkdir(self)
            os.chmod(self, 0o755)
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(transport.Path, "mkdir", mkdir_after_another_creator)
    os.umask(mask)

    private_directory(path)

    assert raced == [above]
    assert mode(above) == 0o755
    assert mode(path) == 0o700


@UMASKS
def test_config_save_creates_private_folders_and_a_private_file(tmp_path: Path, mask: int) -> None:
    """The save's own folders (the `create` branch of the path check), pinned per umask."""
    path = tmp_path / "new" / "bridge" / "config.json"
    os.umask(mask)

    Config(provider_selection=[]).save(path)

    assert Config.load(path).registered_providers() == []
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert mode(path.parent.parent) == 0o700
