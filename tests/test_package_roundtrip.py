"""Small tests for the opt-in installed upgrade/rollback gate."""

import importlib.util
import inspect
from pathlib import Path
from zipfile import ZipFile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_package_roundtrip.py"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("package_roundtrip_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_roundtrip_reuses_the_public_package_helpers(gate) -> None:
    expected = SCRIPT.with_name("package_smoke_helpers.py")
    assert Path(inspect.getsourcefile(gate.wheel_identity)) == expected
    assert Path(inspect.getsourcefile(gate.isolated_environment)) == expected


@pytest.mark.parametrize("version", ["0.10.0", "0.10.0+internal"])
def test_roundtrip_accepts_the_explicit_private_candidate_version(gate, version: str) -> None:
    gate.validate_package_pair(
        {"name": "answerback", "version": "0.9.0"},
        {"name": "answerback", "version": version},
    )


def test_roundtrip_still_rejects_an_unrelated_candidate(gate) -> None:
    with pytest.raises(ValueError, match="Candidate"):
        gate.validate_package_pair(
            {"name": "answerback", "version": "0.9.0"},
            {"name": "answerback", "version": "0.11.0"},
        )


def wheel(tmp_path: Path, *, name: str, version: str) -> Path:
    path = tmp_path / "test.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.5\nName: {name}\nVersion: {version}\n",
        )
    return path


def test_prior_distribution_identity_comes_from_metadata(gate, tmp_path: Path) -> None:
    prior = gate.wheel_identity(wheel(tmp_path, name="answerback", version="0.9.0"))
    assert prior["name"] == "answerback"
    assert prior["version"] == "0.9.0"
    assert len(prior["sha256"]) == 64


def test_no_metadata_fails_closed(gate, tmp_path: Path) -> None:
    path = tmp_path / "missing.whl"
    with ZipFile(path, "w"):
        pass
    with pytest.raises(ValueError, match="exactly one"):
        gate.wheel_identity(path)


def test_disposable_environment_removes_development_and_live_selectors(
    gate, tmp_path: Path
) -> None:
    env = gate.isolated_environment(
        tmp_path,
        {
            "PATH": "/usr/bin",
            "HOME": "/original",
            "PYTHONPATH": "/source",
            "VIRTUAL_ENV": "/development",
            "HERDR_BRIDGE_CONFIG": "/live/config.json",
            "ANSWERBACK_CONFIG": "/live/config.json",
            "HERDR_SOCKET_PATH": "/live/socket",
            "REX_SESSION_ID": "live-session",
        },
    )
    assert env["HOME"] == "/original"
    assert env["UV_TOOL_DIR"] == str(tmp_path / "tools")
    assert env["UV_TOOL_BIN_DIR"] == str(tmp_path / "bin")
    assert env["CODEX_HOME"] == str(tmp_path / "codex")
    assert not any(
        key in env
        for key in (
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "HERDR_BRIDGE_CONFIG",
            "ANSWERBACK_CONFIG",
            "HERDR_SOCKET_PATH",
            "REX_SESSION_ID",
        )
    )
