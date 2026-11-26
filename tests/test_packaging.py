import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from codex_herdr_bridge.config import default_config_path


def test_default_config_uses_generic_name_but_keeps_existing_pairings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "answerback-bridge" / "config.json"
    legacy = tmp_path / "codex-herdr-bridge" / "config.json"
    legacy.parent.mkdir()
    legacy.write_text("{}")
    assert default_config_path() == legacy
    current = tmp_path / "herdr-bridge" / "config.json"
    current.parent.mkdir()
    current.write_text("{}")
    assert default_config_path() == current


def _build_sdist(out_dir: Path) -> tarfile.TarFile:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the source distribution")
    project = Path(__file__).resolve().parents[1]
    subprocess.run(
        [uv, "build", "--sdist", "--out-dir", str(out_dir), str(project)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    (archive,) = out_dir.glob("*.tar.gz")
    return tarfile.open(archive)


def test_sdist_ships_sources_and_licence_but_no_maintainer_files(tmp_path: Path) -> None:
    with _build_sdist(tmp_path) as sdist:
        members = sdist.getnames()
        root = members[0].split("/", 1)[0]
        names = {name.split("/", 1)[1] for name in members if "/" in name}
        metadata = sdist.extractfile(f"{root}/PKG-INFO")
        assert metadata is not None
        pkg_info = metadata.read().decode()
    required = {
        "LICENSE",
        "NOTICE",
        "README.md",
        "CHANGELOG.md",
        "VALIDATION.md",
        "install.sh",
        "pyproject.toml",
        "uv.lock",
        "src/answerback_bridge/cli.py",
        "src/codex_herdr_bridge/__init__.py",
        "src/codex_herdr_bridge/__main__.py",
        "src/codex_herdr_bridge/bus/cli.py",
        "skills/herdr-collaboration/SKILL.md",
        "skills/answerback-fleet/SKILL.md",
        "skills/answerback-fleet/scripts/fleet.py",
        "tests/test_packaging.py",
        "assets/logo.svg",
        "assets/social-preview.png",
        "src/codex_herdr_bridge/bus/assets/favicon.svg",
        "src/codex_herdr_bridge/bus/assets/favicon.ico",
        "src/answerback_bridge/bus/assets/favicon.svg",
        "src/answerback_bridge/bus/assets/favicon.ico",
    }
    assert required <= names
    maintainer_only = (".pre-commit", ".secrets", ".gitleaks", ".local/", ".venv/", "dist/")
    assert not {name for name in names if name.startswith(maintainer_only)}
    assert "License-Expression: Apache-2.0" in pkg_info
    assert "License-File: LICENSE" in pkg_info
    assert "Name: answerback" in pkg_info


def test_wheel_ships_exactly_the_setup_skill_files_and_an_executable_script(tmp_path: Path) -> None:
    """T-134: the wheel holds the setup skill's files and nothing else under its folder."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the wheel")
    project = Path(__file__).resolve().parents[1]
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(project)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    (wheel,) = tmp_path.glob("*.whl")
    prefix = "answerback_bridge/data/skills/answerback-fleet/"
    with zipfile.ZipFile(wheel) as archive:
        shipped = {
            info.filename[len(prefix) :]: info
            for info in archive.infolist()
            if info.filename.startswith(prefix) and not info.is_dir()
        }
    assert sorted(shipped) == [
        ".answerback-fleet.json",
        "SKILL.md",
        "references/agents.md",
        "references/fleet.md",
        "references/harnesses.md",
        "scripts/fleet.py",
    ]
    assert (shipped["scripts/fleet.py"].external_attr >> 16) & 0o100


def test_wheel_ships_the_herdr_app_assets_as_the_manifest_lists_them(tmp_path: Path) -> None:
    """T-181: the side panel and the themes are package data, byte for byte as the manifest says."""
    import hashlib
    import json

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the wheel")
    project = Path(__file__).resolve().parents[1]
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(project)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    (wheel,) = tmp_path.glob("*.whl")
    prefix = "answerback_bridge/data/herdr-app/"
    with zipfile.ZipFile(wheel) as archive:
        shipped = {
            info.filename[len(prefix) :]: archive.read(info)
            for info in archive.infolist()
            if info.filename.startswith(prefix) and not info.is_dir()
        }
    manifest = json.loads(shipped.pop("manifest.json"))
    assert {row["path"] for row in manifest["files"]} == set(shipped)
    for row in manifest["files"]:
        data = shipped[row["path"]]
        assert "sha256:" + hashlib.sha256(data).hexdigest() == row["digest"], row["path"]
        assert len(data) == row["bytes"], row["path"]
    assert "mods/answerback-fleet/main.js" in shipped
    assert "mods/answerback-fleet/gpui-kit.d.ts" not in shipped
    assert "themes/rex-macchiato.json" in shipped
