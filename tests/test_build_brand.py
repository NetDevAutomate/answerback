"""scripts/build_brand.py writes the favicons into the served folder and its legacy copy.

The script itself is never run here: it needs a browser. Its module is loaded, its
folders are pointed at tmp_path, and the parts that need no browser are called.
"""

import runpy
from pathlib import Path
from typing import Any

import pytest

PROJECT = Path(__file__).parents[1]
MARK = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect/><path d="M1 1"/></svg>\n'


def load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], Path, Path]:
    module = runpy.run_path(str(PROJECT / "scripts/build_brand.py"))
    assets, served, legacy = tmp_path / "assets", tmp_path / "served", tmp_path / "legacy"
    for folder in (assets, served, legacy):
        folder.mkdir()
    for name in ("mark.svg", "wordmark.svg", "tagline.svg"):
        (assets / name).write_text(MARK)
    scope = module["svgs"].__globals__
    monkeypatch.setitem(scope, "ASSETS", assets)
    monkeypatch.setitem(scope, "WEB", served)
    monkeypatch.setitem(scope, "LEGACY_WEB", legacy)
    monkeypatch.setitem(scope, "WEB_FOLDERS", (served, legacy))
    return module, served, legacy


def test_web_folders_are_the_served_assets_and_their_legacy_copy() -> None:
    """red_at_pretip: the script named only the legacy copy as its web folder."""
    module = runpy.run_path(str(PROJECT / "scripts/build_brand.py"))
    assert module["WEB"] == PROJECT / "src/answerback_bridge/bus/assets"
    assert module["LEGACY_WEB"] == PROJECT / "src/codex_herdr_bridge/bus/assets"
    assert module["WEB_FOLDERS"] == (module["WEB"], module["LEGACY_WEB"])
    assert module["WEB"].is_dir() and module["LEGACY_WEB"].is_dir()


def test_favicon_svg_is_written_to_both_folders_with_equal_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: only one folder received favicon.svg."""
    module, served, legacy = load(tmp_path, monkeypatch)

    files = module["svgs"]()
    module["write_svgs"](files)

    assert (served / "favicon.svg").read_text() == MARK
    assert (legacy / "favicon.svg").read_bytes() == (served / "favicon.svg").read_bytes()
    assert all(tmp_path in path.parents for path in files), "nothing is written outside tmp_path"


def test_favicon_ico_is_written_to_both_folders_with_equal_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: only one folder received favicon.ico."""
    module, served, legacy = load(tmp_path, monkeypatch)
    icon = b"\x00\x00\x01\x00not a real icon"

    module["write_icon"](icon)

    assert (served / "favicon.ico").read_bytes() == icon
    assert (legacy / "favicon.ico").read_bytes() == icon
    assert (tmp_path / "assets" / "favicon.ico").read_bytes() == icon
