"""The bridge's skill installer and the setup skill's fleet.py on one temporary home (T-134, N1).

Both installers are the real functions: `install_skill` with no source, which takes this checkout's
`skills/` folders, and fleet.py loaded from its path as tests/test_skill_fleet.py loads it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_skill_fleet import REAL_SKILL, _load_fleet

from answerback_bridge.registration import install_skill

fleet = _load_fleet()
COLLABORATION = "answerback-collaboration"


def _no_program(argv: list[str], timeout: float = 30, stdin: str | None = None) -> Any:
    """Stand-in for fleet.run: no program is found, so no bridge and no harness is asked."""
    return subprocess.CompletedProcess(argv, 127, "", "not found")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home in which fleet.py finds Claude and Kiro and nothing else."""
    home = tmp_path / "home"
    home.mkdir()
    found = {"claude", "kiro-cli"}
    monkeypatch.setattr(fleet, "which", lambda name: f"/bin/{name}" if name in found else None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _setup(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["client"]: r for r in result["targets"] if r["skill"] == fleet.SKILL_NAME}


def _ops(planned: dict[str, Any]) -> dict[str, str]:
    actions = [action for action in planned["actions"] if action["harness"] != "bridge"]
    return {action["harness"]: action["op"] for action in actions}


def _content(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _other_skill(folder: Path, body: str) -> Path:
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: answerback-fleet\ndescription: t\n---\n{body}\n")
    (folder / "scripts/fleet.py").write_text("print('other')\n")
    return folder


async def test_fleet_plans_no_write_for_the_copy_the_bridge_installed(home: Path) -> None:
    """red_at_pretip: the script runs from the shared copy that `install_skill` wrote."""
    result = await install_skill(["claude", "kiro"], home=home, apply=True)
    assert result["success"]
    shared = home / ".agents/skills" / fleet.SKILL_NAME
    assert fleet.read_manifest(shared) == {"skill": fleet.SKILL_NAME, "version": fleet.VERSION}
    planned = fleet.plan(source=shared, home=home, link_source=True, run_=_no_program)
    assert planned["refused"] == 0 and planned["writes"] == 0
    assert _ops(planned) == {"shared": "unchanged", "claude": "unchanged", "kiro": "unchanged"}
    assert all(check["ok"] for check in fleet.verify(planned))


async def test_the_bridge_says_unchanged_for_a_copy_fleet_wrote_from_the_repository(
    home: Path,
) -> None:
    """red_at_pretip (N1): the same content gives the same folder, whoever wrote it."""
    planned = fleet.plan(source=REAL_SKILL, home=home, link_source=False, run_=_no_program)
    assert _ops(planned)["shared"] == "copy"
    fleet.apply_plan(planned, run_=_no_program)
    shared = home / ".agents/skills" / fleet.SKILL_NAME
    before = _content(shared)
    assert before == _content(REAL_SKILL)
    result = await install_skill(["claude", "kiro"], home=home, apply=True)
    assert result["success"]
    assert {client: report["status"] for client, report in _setup(result).items()} == {
        "shared": "unchanged",
        "claude": "unchanged",
        "kiro": "unchanged",
    }
    assert _content(shared) == before
    assert (home / ".agents/skills" / COLLABORATION / "SKILL.md").is_file()


async def test_a_copy_of_other_content_is_kept_then_replaced_then_updated_by_fleet(
    home: Path, tmp_path: Path
) -> None:
    """red_at_pretip (D7, N1): `kept`, then `--replace`, then fleet.py may update the copy."""
    older = _other_skill(tmp_path / "older" / fleet.SKILL_NAME, "older")
    fleet.apply_plan(
        fleet.plan(source=older, home=home, link_source=False, run_=_no_program), run_=_no_program
    )
    shared = home / ".agents/skills" / fleet.SKILL_NAME
    before = _content(shared)
    kept = await install_skill(["claude"], home=home, apply=True)
    assert kept["success"]
    assert _setup(kept)["shared"]["status"] == "kept"
    assert _content(shared) == before
    assert (home / ".agents/skills" / COLLABORATION / "SKILL.md").is_file()
    replaced = await install_skill(["claude"], home=home, apply=True, replace=True)
    assert replaced["success"]
    assert _setup(replaced)["shared"]["status"] == "installed"
    assert _content(shared) == _content(REAL_SKILL)
    equal = fleet.plan(source=REAL_SKILL, home=home, link_source=False, run_=_no_program)
    assert equal["refused"] == 0 and _ops(equal)["shared"] == "unchanged"
    newer = tmp_path / "newer" / fleet.SKILL_NAME
    shutil.copytree(REAL_SKILL, newer)
    (newer / "SKILL.md").write_text((newer / "SKILL.md").read_text() + "\nA newer line.\n")
    update = fleet.plan(source=newer, home=home, link_source=False, run_=_no_program)
    assert update["refused"] == 0 and _ops(update)["shared"] == "copy"
    fleet.apply_plan(update, run_=_no_program)
    assert (shared / "SKILL.md").read_text().endswith("A newer line.\n")
    assert json.loads((shared / fleet.MANIFEST).read_text())["version"] == fleet.VERSION


async def test_fleet_removes_the_setup_skill_and_leaves_the_collaboration_skill(
    home: Path,
) -> None:
    """red_at_pretip (N1, A08): `--remove` takes a copy with the manifest, whoever wrote it."""
    result = await install_skill(["claude", "kiro"], home=home, apply=True)
    assert result["success"]
    collaboration = home / ".agents/skills" / COLLABORATION
    before = _content(collaboration)
    planned = fleet.plan_remove(home=home)
    assert sorted(a["harness"] for a in planned["actions"] if a["op"] == "remove") == [
        "claude",
        "kiro",
        "shared",
    ]
    fleet.apply_remove(planned)
    for folder in (".agents/skills", ".claude/skills", ".kiro/skills"):
        entry = home / folder / fleet.SKILL_NAME
        assert not entry.exists() and not entry.is_symlink()
        assert (home / folder / COLLABORATION / "SKILL.md").is_file()
    assert _content(collaboration) == before
    assert (home / ".claude/skills" / COLLABORATION).is_symlink()
