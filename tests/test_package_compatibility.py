"""Namespace and artifact contracts for the Answerback package migration."""

import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "src"


def _python(code: str, path: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(path)},
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("first", ["answerback_bridge", "codex_herdr_bridge"])
def test_namespaces_share_root_nested_modules_classes_and_state(first: str, tmp_path: Path) -> None:
    result = _python(
        f"""
import importlib, sys
first = {first!r}
second = 'codex_herdr_bridge' if first == 'answerback_bridge' else 'answerback_bridge'
for suffix in ['', '.tasks', '.process', '.errors', '.bus', '.bus.cli', '.bus.store',
               '.bus.model', '.adapters', '.adapters.contracts', '.adapters.registry',
               '.testing', '.testing.peers']:
    left = importlib.import_module(first + suffix)
    right = importlib.import_module(second + suffix)
    assert left is right, suffix
    assert left.__name__ == 'answerback_bridge' + suffix, suffix
    assert left.__spec__.name == 'answerback_bridge' + suffix, suffix
    left._compatibility_sentinel = object()
    assert right._compatibility_sentinel is left._compatibility_sentinel, suffix
from answerback_bridge.process import BridgeError as NewError
from codex_herdr_bridge.process import BridgeError as OldError
from answerback_bridge.tasks import TaskStore as NewStore
from codex_herdr_bridge.tasks import TaskStore as OldStore
assert NewError is OldError
assert NewStore is OldStore
try:
    raise OldError('same exception')
except NewError:
    pass
assert all(module.__name__.startswith('answerback_bridge')
           for name, module in sys.modules.items() if name.startswith('codex_herdr_bridge'))
""",
        SOURCE,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "module,args,expected",
    [
        ("answerback_bridge", ["--version"], "0.16.2"),
        ("codex_herdr_bridge", ["--version"], "0.16.2"),
        ("answerback_bridge.bus.cli", ["--help"], "submit"),
        ("codex_herdr_bridge.bus.cli", ["--help"], "submit"),
    ],
)
def test_module_entry_points(module: str, args: list[str], expected: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module, *args],
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert not result.stderr


def test_project_entry_points_use_canonical_package() -> None:
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "answerback"
    assert project["version"] == "0.16.2"
    assert project["scripts"] == {
        "answerback": "answerback_bridge.cli:main",
        "answerback-bridge": "answerback_bridge.cli:main",
        "herdr-bridge": "answerback_bridge.cli:main",
        "codex-herdr": "answerback_bridge.cli:main",
        "agent-bus": "answerback_bridge.bus.cli:main",
    }


def test_legacy_discovery_and_historical_class_references(tmp_path: Path) -> None:
    result = _python(
        """
import importlib, importlib.util, pickle
from pathlib import Path
import answerback_bridge
import codex_herdr_bridge
assert importlib.util.find_spec('codex_herdr_bridge.missing_module') is None
has_nats = importlib.util.find_spec('nats') is not None
needs_extra = []
root = Path(answerback_bridge.__file__).parent
for path in root.rglob('*.py'):
    relative = path.relative_to(root).with_suffix('')
    if path.name == '__main__.py':
        continue
    parts = list(relative.parts)
    if parts[-1] == '__init__':
        parts.pop()
    suffix = '.' + '.'.join(parts) if parts else ''
    try:
        canonical = importlib.import_module('answerback_bridge' + suffix)
    except ModuleNotFoundError as error:
        # nats-py is the optional `bus` extra: without it only the NATS-backed modules
        # are unloadable under either name; every other module still aliases.
        if has_nats or (error.name or '').partition('.')[0] != 'nats':
            raise
        needs_extra.append(suffix)
        continue
    legacy = importlib.import_module('codex_herdr_bridge' + suffix)
    assert canonical is legacy, suffix
expected = [] if has_nats else ['.actors.transport', '.bus.broker', '.bus.jetstream']
assert sorted(needs_extra) == expected, needs_extra
from answerback_bridge.process import BridgeError
assert pickle.loads(b'ccodex_herdr_bridge.process\\nBridgeError\\n.') is BridgeError
""",
        SOURCE,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr


def test_source_assets_stay_readable_at_legacy_location() -> None:
    filenames = ["dashboard.html", "dashboard.js", "dashboard.css", "favicon.svg", "favicon.ico"]
    for filename in filenames:
        canonical = SOURCE / "answerback_bridge/bus/assets" / filename
        legacy = SOURCE / "codex_herdr_bridge/bus/assets" / filename
        assert canonical.read_bytes() == legacy.read_bytes()


def test_wheel_has_both_namespaces_resources_and_working_console_entries(tmp_path: Path) -> None:
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist"), str(PROJECT)],
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = (tmp_path / "dist").glob("*.whl")
    extracted = tmp_path / "wheel"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(extracted)
    result = _python(
        """
import importlib
from importlib.metadata import distribution
from importlib.resources import files
canonical = importlib.import_module('answerback_bridge.bus.store')
legacy = importlib.import_module('codex_herdr_bridge.bus.store')
assert canonical is legacy
for package in ['answerback_bridge', 'codex_herdr_bridge']:
    root = files(package)
    assert root.joinpath('bus/assets/favicon.ico').read_bytes().startswith(b'\\x00\\x00\\x01\\x00')
    assert root.joinpath('data/skills/herdr-collaboration/SKILL.md').is_file()
    assert root.joinpath('data/skills/herdr-collaboration/references/bridge.md').is_file()
    assert root.joinpath('data/skills/answerback-collaboration/references/bridge.md').is_file()
from answerback_bridge.registration import _skill_source
assert str(_skill_source(None)).startswith(str(files('answerback_bridge')))
entry_points = distribution('answerback').entry_points
expected = {'answerback', 'answerback-bridge', 'herdr-bridge', 'codex-herdr', 'agent-bus'}
assert {entry.name for entry in entry_points if entry.group == 'console_scripts'} == expected
""",
        extracted,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    for command in ["answerback", "answerback-bridge", "herdr-bridge", "codex-herdr", "agent-bus"]:
        result = _python(
            f"""
import sys
from importlib.metadata import distribution
entry = next(e for e in distribution('answerback').entry_points if e.name == {command!r})
sys.argv = [{command!r}, '--help']
entry.load()()
""",
            extracted,
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert ("submit" if command == "agent-bus" else "start") in result.stdout
        assert not result.stderr
