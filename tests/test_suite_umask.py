"""The suite sets its own umask: what a test creates is never group- or other-writable.

`tests/conftest.py` sets umask 022 in its hook `pytest_configure`, before pytest imports
any test module and before any fixture runs. Without it the caller's umask is a hidden
input of the suite: under umask 002 (the default for a Linux account with a private
group) a folder made with `Path.mkdir()` has mode 0775 and a file made with
`Path.write_text()` has mode 0664. The configuration path check of `Config.load` and
`Config.save` refuses such a path, as it must, so every test that puts a configuration
under `tmp_path` failed there before it reached what it tests.

The umask is set in the hook and not at the top level of the file, because a statement
at the top level runs for every program that loads the file. The install smoke script
loads `tests/test_runtime.py`, which imports `conftest`; it must keep the umask of its
shell, or it would hide a product fault that only an account with umask 002 meets.
`test_a_plain_load_of_conftest_leaves_the_umask_as_it_was` pins that.

This module reads the umask once at its own top level, which is when pytest imports it:
`test_umask_was_in_force_when_pytest_imported_this_module` pins that the hook ran before.

These tests pin the effect, not the value 022: any umask that clears the group-write and
the other-write bit is as good here. A test that runs the command `serve` in the test
process sets umask 077 for the rest of that test only. The autouse fixture
`umask_restored` of `tests/conftest.py` puts the umask of before the test back after it,
so the next test runs under the suite's umask again.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

WRITABLE_BY_ANOTHER_USER = stat.S_IWGRP | stat.S_IWOTH


def current_umask() -> int:
    """Read the umask in force; `os.umask` can only read by setting, so set it back."""
    current = os.umask(0o077)
    os.umask(current)
    return current


# Read when pytest imports this module, which is during collection: after the hook
# `pytest_configure`, before any fixture and before any test of the session.
UMASK_AT_IMPORT = current_umask()

# What a program does that only loads tests/conftest.py, as the install smoke script does
# through tests/test_runtime.py. The first argument is the folder of the tests.
PLAIN_LOAD = """
import os
import sys

os.umask(0o002)
sys.path.insert(0, sys.argv[1])
import conftest

assert conftest.__file__ == os.path.join(sys.argv[1], "conftest.py"), conftest.__file__
current = os.umask(0)
os.umask(current)
print(f"{current:03o}")
"""


def test_umask_was_in_force_when_pytest_imported_this_module() -> None:
    assert UMASK_AT_IMPORT & WRITABLE_BY_ANOTHER_USER == WRITABLE_BY_ANOTHER_USER


def test_a_plain_load_of_conftest_leaves_the_umask_as_it_was() -> None:
    tests = Path(__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, "-c", PLAIN_LOAD, str(tests)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.stdout.strip() == "002"


def test_umask_in_force_clears_group_write_and_other_write() -> None:
    assert current_umask() & WRITABLE_BY_ANOTHER_USER == WRITABLE_BY_ANOTHER_USER


def test_reading_the_umask_leaves_it_as_it_was() -> None:
    before = current_umask()

    assert current_umask() == before


def test_folder_made_with_the_default_mode_is_not_writable_by_another_user(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "owned"
    folder.mkdir()

    assert stat.S_IMODE(folder.stat().st_mode) & WRITABLE_BY_ANOTHER_USER == 0


def test_file_made_with_the_default_mode_is_not_writable_by_another_user(
    tmp_path: Path,
) -> None:
    file = tmp_path / "config.json"
    file.write_text("{}\n")

    assert stat.S_IMODE(file.stat().st_mode) & WRITABLE_BY_ANOTHER_USER == 0
