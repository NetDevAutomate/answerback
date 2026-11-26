import importlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


def test_rex_source_import_alias_keeps_class_identity():
    for suffix in ("rex", "rex_mailbox", "rex_service", "adapters.rex"):
        canonical = importlib.import_module("answerback_bridge." + suffix)
        legacy = importlib.import_module("codex_herdr_bridge." + suffix)
        assert canonical is legacy
    canonical = importlib.import_module("answerback_bridge.rex")
    legacy = importlib.import_module("codex_herdr_bridge.rex")
    assert canonical.RexIdentityChanged is legacy.RexIdentityChanged


@pytest.fixture(scope="module")
def rex_wheel(tmp_path_factory):
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the Rex package contract"
    root = Path(__file__).resolve().parents[1]
    out = tmp_path_factory.mktemp("rex-wheel")
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out), str(root)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    (wheel,) = out.glob("*.whl")
    return wheel


def test_rex_wheel_ships_canonical_modules_and_reference(rex_wheel):
    with zipfile.ZipFile(rex_wheel) as archive:
        assert {
            "answerback_bridge/rex.py",
            "answerback_bridge/rex_mailbox.py",
            "answerback_bridge/rex_service.py",
            "answerback_bridge/adapters/rex.py",
            "answerback_bridge/data/skills/answerback-collaboration/references/rex.md",
        } <= set(archive.namelist())


def test_rex_wheel_import_alias_uses_one_module(rex_wheel):
    # Read directly from the real wheel: no installation or service operation.
    code = """
import importlib
import sys
sys.path.insert(0, sys.argv[1])
for suffix in ("rex", "rex_mailbox", "rex_service", "adapters.rex"):
    canonical = importlib.import_module("answerback_bridge." + suffix)
    legacy = importlib.import_module("codex_herdr_bridge." + suffix)
    assert canonical is legacy
    assert canonical.__file__.startswith(sys.argv[1]), canonical.__file__
"""
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(rex_wheel)],
        check=True,
        capture_output=True,
        timeout=20,
    )
