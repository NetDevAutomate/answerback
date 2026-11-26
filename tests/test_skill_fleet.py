"""Tests for the setup skill's fleet.py. Every test works in a temporary home.

The 33 tests of the skill's own repository (answerback_skill 33a804a, tests/test_fleet.py), then the
tests this repository adds. fleet.py is loaded from its path with compile and exec: an import would
write a __pycache__ beside it, and an installer copies and hashes the whole skill folder.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import errno
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
REAL_SKILL = REPO / "skills" / "answerback-fleet"
FLEET_MODULE = "answerback_fleet_script"


def _load_fleet() -> Any:
    """fleet.py as a module: it has __file__ and is in sys.modules before its code runs."""
    loaded = sys.modules.get(FLEET_MODULE)
    if loaded is not None:
        return loaded
    path = REAL_SKILL / "scripts" / "fleet.py"
    module = types.ModuleType(FLEET_MODULE)
    module.__file__ = str(path)
    sys.modules[FLEET_MODULE] = module
    exec(compile(path.read_text(), str(path), "exec", dont_inherit=True), module.__dict__)
    return module


fleet = _load_fleet()


def fake_run(answers: dict[str, tuple[int, str]] | None = None):
    """A stand-in for fleet.run: answers by the first matching 'word word' of the command."""
    calls: list[list[str]] = []

    def run_(argv: list[str], timeout: float = 30, stdin: str | None = None):
        calls.append(argv)
        line = " ".join(argv)
        for key, (code, out) in (answers or {}).items():
            if key in line:
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 127, "", "not found")

    run_.calls = calls  # type: ignore[attr-defined]
    return run_


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with Claude, Kiro and Codex installed and no bridge."""
    home = tmp_path / "home"
    home.mkdir()
    installed = {"claude", "kiro-cli", "codex"}
    monkeypatch.setattr(fleet, "which", lambda name: f"/bin/{name}" if name in installed else None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def skill(tmp_path: Path) -> Path:
    source = tmp_path / "repo" / fleet.SKILL_NAME
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: answerback-fleet\ndescription: test\n---\nbody\n")
    (source / "scripts" / "fleet.py").write_text("print('hi')\n")
    return source


def ops(planned: dict[str, Any]) -> dict[str, str]:
    return {a["harness"]: a["op"] for a in planned["actions"]}


def make_plan(home: Path, skill: Path, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("bridge", "none")
    return fleet.plan(source=skill, home=home, run_=fake_run(), **kwargs)


# ----------------------------------------------------------------------------- content


def test_tree_hash_ignores_manifest_and_caches(skill: Path) -> None:
    before = fleet.tree_hash(skill)
    (skill / fleet.MANIFEST).write_text("{}")
    (skill / "scripts" / "__pycache__").mkdir()
    (skill / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    assert fleet.tree_hash(skill) == before
    (skill / "SKILL.md").write_text("changed")
    assert fleet.tree_hash(skill) != before


def test_pack_and_unpack_round_trip(skill: Path, tmp_path: Path) -> None:
    dest = tmp_path / "out"
    fleet.unpack(fleet.pack(skill), dest)
    assert fleet.tree_hash(dest) == fleet.tree_hash(skill)


def test_unpack_refuses_a_member_outside_the_destination(tmp_path: Path) -> None:
    import base64
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(fleet.FleetError, match="refused archive member"):
        fleet.unpack(base64.b64encode(buffer.getvalue()).decode(), tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


# ----------------------------------------------------------------------------- plan


def test_canonical_links_only_harnesses_that_do_not_read_the_shared_directory(
    home: Path, skill: Path
) -> None:
    planned = make_plan(home, skill, link_source=True)
    assert ops(planned) == {"shared": "link", "claude": "link", "codex": "skip", "kiro": "link"}
    assert planned["refused"] == 0 and planned["writes"] == 3


def test_a_harness_directory_that_is_the_shared_directory_is_written_once(
    home: Path, skill: Path
) -> None:
    (home / ".agents/skills").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude/skills").symlink_to(home / ".agents/skills")
    planned = make_plan(home, skill, link_source=True)
    assert ops(planned)["claude"] == "skip"


def test_a_harness_directory_linked_elsewhere_gets_a_link_that_resolves(
    home: Path, skill: Path
) -> None:
    (home / "deep/other/skills").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude/skills").symlink_to(home / "deep/other/skills")
    planned = make_plan(home, skill)
    claude = next(a for a in planned["actions"] if a["harness"] == "claude")
    assert claude["op"] == "link" and "is a link to" in claude["note"]
    fleet.apply_plan(planned, run_=fake_run())
    assert (home / ".claude/skills" / fleet.SKILL_NAME / "SKILL.md").is_file()


def test_harness_mode_writes_each_harness_and_no_shared_entry(home: Path, skill: Path) -> None:
    planned = make_plan(home, skill, mode="harness")
    assert ops(planned) == {"claude": "copy", "codex": "copy", "kiro": "copy"}


def test_a_folder_this_tool_did_not_write_is_never_replaced(home: Path, skill: Path) -> None:
    theirs = home / ".agents/skills" / fleet.SKILL_NAME
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text("someone else's")
    planned = make_plan(home, skill, force=True)
    assert ops(planned)["shared"] == "refuse"
    assert ops(planned)["kiro"] == "refuse"  # nothing links to a refused shared entry
    with pytest.raises(fleet.FleetError, match="refused"):
        fleet.apply_plan(planned, run_=fake_run())
    assert (theirs / "SKILL.md").read_text() == "someone else's"


def test_a_link_pointing_elsewhere_needs_force(home: Path, skill: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (home / ".kiro/skills").mkdir(parents=True)
    (home / ".kiro/skills" / fleet.SKILL_NAME).symlink_to(other)
    assert ops(make_plan(home, skill))["kiro"] == "refuse"
    assert ops(make_plan(home, skill, force=True))["kiro"] == "link"


def test_unknown_harness_and_missing_skill_are_errors(
    home: Path, skill: Path, tmp_path: Path
) -> None:
    with pytest.raises(fleet.FleetError, match="unknown harness"):
        make_plan(home, skill, harnesses=["emacs"])
    with pytest.raises(fleet.FleetError, match="no skill"):
        make_plan(home, tmp_path / "nothing")


def test_a_harness_that_is_not_installed_is_skipped(home: Path, skill: Path) -> None:
    assert ops(make_plan(home, skill, harnesses=["grok", "kiro"]))["grok"] == "skip"


def test_project_scope_copies_into_the_project_and_refuses_links_out_of_it(
    home: Path, skill: Path
) -> None:
    project = home / "proj"
    project.mkdir()
    planned = make_plan(home, skill, scope="project", project=project)
    assert ops(planned) == {"shared": "copy", "claude": "link", "codex": "skip", "kiro": "link"}
    assert planned["actions"][0]["path"] == str(project / ".agents/skills" / fleet.SKILL_NAME)
    with pytest.raises(fleet.FleetError, match="always copies"):
        make_plan(home, skill, scope="project", project=project, link_source=True)
    with pytest.raises(fleet.FleetError, match="needs --project"):
        make_plan(home, skill, scope="project")


# ----------------------------------------------------------------------------- apply


def test_apply_writes_verifies_and_is_idempotent(home: Path, skill: Path) -> None:
    planned = make_plan(home, skill)
    applied = fleet.apply_plan(planned, run_=fake_run())
    assert all(check["ok"] for check in applied["verify"]) and len(applied["verify"]) == 3
    shared = home / ".agents/skills" / fleet.SKILL_NAME
    # T-134, ruling N1: the manifest holds the name and the version, and no hash.
    assert fleet.read_manifest(shared) == {"skill": fleet.SKILL_NAME, "version": fleet.VERSION}
    link = home / ".kiro/skills" / fleet.SKILL_NAME
    assert (
        link.is_symlink()
        and not os.path.isabs(os.readlink(link))
        and link.resolve() == shared.resolve()
    )
    again = make_plan(home, skill)
    assert set(ops(again).values()) == {"unchanged", "skip"} and again["writes"] == 0


def test_a_copy_this_tool_wrote_is_updated_when_the_skill_changes(home: Path, skill: Path) -> None:
    fleet.apply_plan(make_plan(home, skill), run_=fake_run())
    (skill / "SKILL.md").write_text("---\nname: answerback-fleet\ndescription: v2\n---\n")
    planned = make_plan(home, skill)
    assert ops(planned)["shared"] == "copy"
    fleet.apply_plan(planned, run_=fake_run())
    assert "v2" in (home / ".agents/skills" / fleet.SKILL_NAME / "SKILL.md").read_text()
    assert not list((home / ".agents/skills").glob(".answerback-fleet.*"))  # staging is cleaned up


def test_apply_refuses_when_the_skill_changed_after_the_plan(home: Path, skill: Path) -> None:
    planned = make_plan(home, skill)
    (skill / "SKILL.md").write_text("changed")
    with pytest.raises(fleet.FleetError, match="changed since the plan"):
        fleet.apply_plan(planned, run_=fake_run())
    assert not (home / ".agents").exists()


# ----------------------------------------------------------------------------- bridge


def bridge_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fleet, "which", lambda name: f"/bin/{name}" if name in {"claude", "answerback"} else None
    )


def test_bridge_preview_is_run_at_plan_time_and_never_applied_by_default(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_home(monkeypatch)
    preview = json.dumps(
        {"success": True, "applied": False, "targets": [{"client": "claude", "status": "planned"}]}
    )
    run_ = fake_run(
        {"--version": (0, "0.14.0\n"), "status": (0, '{"running": false}'), "install": (0, preview)}
    )
    planned = fleet.plan(source=skill, home=home, run_=run_)
    bridge = [a for a in planned["actions"] if a["op"] == "bridge"]
    assert [a["argv"][1] for a in bridge] == ["clients", "skill"]
    assert all("--apply" not in a["argv"] and "not running" in a["note"] for a in bridge)
    assert bridge[0]["detail"]["targets"] == [{"client": "claude", "status": "planned"}]
    before = len(run_.calls)
    applied = fleet.apply_plan(planned, run_=run_)
    assert len(run_.calls) == before  # a preview is not run again, and nothing is applied
    assert [a["result"] for a in applied["actions"] if a["op"] == "bridge"] == [
        "previewed",
        "previewed",
    ]


def test_bridge_apply_passes_apply_only_when_asked(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_home(monkeypatch)
    run_ = fake_run(
        {"--version": (0, "0.14.0\n"), "status": (0, '{"running": true}'), "install": (0, "{}")}
    )
    planned = fleet.plan(source=skill, home=home, bridge="apply", run_=run_)
    fleet.apply_plan(planned, run_=run_)
    assert next(c for c in run_.calls if "--apply" in c)[:3] == [
        "/bin/answerback",
        "clients",
        "install",
    ]
    assert len([c for c in run_.calls if "--apply" in c]) == 2


def test_no_bridge_is_reported_not_installed(home: Path, skill: Path) -> None:
    planned = fleet.plan(source=skill, home=home, run_=fake_run())
    assert (
        planned["actions"][-1]["op"] == "skip" and "not installed" in planned["actions"][-1]["note"]
    )


# ----------------------------------------------------------------------------- fleet


MACHINES = json.dumps(
    {
        "machines": [
            {"name": "nuc1", "host": "nuc1", "state": "connected"},
            {"name": "old", "state": "offline"},
        ]
    }
)


def test_no_terminal_host_means_local_only(home: Path) -> None:
    assert fleet.machines(fake_run())["mode"] == "local-only"


def test_machines_and_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet, "which", lambda name: f"/bin/{name}")
    found = fleet.machines(fake_run({"machine ls": (0, MACHINES)}))
    assert found["mode"] == "fleet"
    assert fleet.select(found, None) == (True, [], [])
    local, nodes, left = fleet.select(found, ["all"])
    assert (
        local and nodes == ["nuc1"] and left == [{"machine": "old", "reason": "state is offline"}]
    )
    assert fleet.select(found, ["ghost"]) == (
        False,
        [],
        [{"machine": "ghost", "reason": "not registered with hc"}],
    )
    assert fleet.machines(fake_run({"machine ls": (0, '{"machines": []}')}))["mode"] == "local-only"


def row(state: str, harnesses: list[str], bridge: dict[str, Any]) -> dict[str, Any]:
    return {
        "capacity": {"state": state},
        "bridge": bridge,
        "shared": {
            "skills_dir": {"kind": "dir", "real": "/h/.agents/skills"},
            "skills": {fleet.SKILL_NAME: {"kind": "missing"}},
        },
        "harnesses": {
            h.id: {
                "installed": h.id in harnesses,
                "skills_dir": {"kind": "dir", "real": f"/h/{h.user_dir}"},
            }
            for h in fleet.HARNESSES
        },
    }


def test_pool_admits_only_measured_ok_machines_with_a_harness_and_the_bridge() -> None:
    on = {"installed": True, "running": True, "version": "1"}
    surveyed = {
        "machines": {
            "local": row("full", ["claude"], on),
            "a": row("ok", ["claude", "kiro"], on),
            "b": row("unknown", ["claude"], on),
            "c": row("ok", [], on),
            "d": row("ok", ["claude"], {"installed": False}),
        }
    }
    by_name = {r["machine"]: r for r in fleet.pool(surveyed)}
    assert [n for n, r in by_name.items() if r["eligible"]] == ["a"]
    assert by_name["local"]["coordinator"] and by_name["b"]["why_not"] == ["capacity unknown"]
    assert by_name["d"]["why_not"] == ["no answerback"]


def test_drift_reports_versions_stopped_bridges_and_stray_links() -> None:
    rows = {
        "local": row("ok", ["claude"], {"installed": True, "running": True, "version": "0.14"}),
        "n": row("ok", ["claude"], {"installed": True, "running": False, "version": "0.11"}),
    }
    rows["n"]["harnesses"]["claude"]["skills_dir"] = {
        "kind": "link",
        "target": "/h/.ai/skills",
        "real": "/h/.ai/skills",
    }
    notes = fleet.drift(rows)
    assert any("versions differ" in n for n in notes)
    assert "n: the bridge service is not running" in notes
    assert any(".claude/skills is a link to /h/.ai/skills" in n for n in notes)


def test_on_node_reads_only_the_json_between_the_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet, "which", lambda name: f"/bin/{name}")
    noisy = f'mise WARN something\n{fleet.BEGIN}\n{{"ok": 1}}\n{fleet.END}\ntrailing\n'
    run_ = fake_run({"exec --on nuc1": (0, noisy)})
    assert fleet.on_node("nuc1", ["probe"], run_=run_) == {"ok": 1}
    assert run_.calls[0][:4] == ["/bin/hc", "exec", "--on", "nuc1"]
    with pytest.raises(fleet.FleetError, match="no answer"):
        fleet.on_node("nuc1", ["probe"], run_=fake_run({"exec --on": (1, "boom")}))


# ------------------------------------------------------------------ command line, shipped skill


def test_apply_without_yes_writes_nothing(
    home: Path, skill: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fleet.main(["apply", "--source", str(skill), "--bridge", "none"]) == 1
    assert "apply --yes" in capsys.readouterr().err
    assert not (home / ".agents").exists()


def test_the_script_runs_from_stdin_the_way_a_node_receives_it(tmp_path: Path) -> None:
    """on_node sends the file as text and the node exec()s it: no __file__ on disk, no import.

    The child gets an empty home and a path that names an empty folder, and it is asked for a
    removal plan. It looks for skills only in the home it is given, and it starts no program.
    Its search for a program also asks whether a file exists in `/opt/homebrew/bin` and
    `/usr/local/bin`; nothing asserted here depends on those two folders (ruling A04 as
    corrected after review 1, finding R1-06).
    """
    script = (REAL_SKILL / "scripts" / "fleet.py").read_text()
    argv = ["node-install", "--expect", "none", "--remove", "--json"]
    envelope = json.dumps({"script": script, "argv": argv, "payload": None})
    loader = (
        "import json,sys;e=json.load(sys.stdin);sys.argv=['fleet.py']+e['argv'];"
        "g={'__name__':'__main__','__file__':'<fleet>','FLEET_PAYLOAD':e['payload']};"
        "exec(compile(e['script'],'fleet.py','exec'),g)"
    )
    child_home, child_bin = tmp_path / "home", tmp_path / "bin"
    child_home.mkdir()
    child_bin.mkdir()
    done = subprocess.run(
        [sys.executable, "-c", loader],
        input=envelope,
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(child_home), "PATH": str(child_bin)},
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    body = json.loads(done.stdout.split(fleet.BEGIN, 1)[1].split(fleet.END, 1)[0])
    assert body["mode"] == "remove" and body["writes"] == 0 and body["refused"] == 0
    assert not [action for action in body["actions"] if action["op"] == "remove"]
    assert list(child_home.iterdir()) == [] and list(child_bin.iterdir()) == []


def test_the_shipped_skill_is_well_formed() -> None:
    text = (REAL_SKILL / "SKILL.md").read_text()
    front = text.split("---", 2)[1]
    assert f"name: {fleet.SKILL_NAME}" in front and "description:" in front
    for line in text.splitlines():
        for part in line.split("](")[1:]:
            target = part.split(")", 1)[0]
            if not target.startswith(("http", "#")):
                assert (REAL_SKILL / target.split("#")[0]).exists(), f"broken link: {target}"


# ----------------------------------------------------------------------------- council findings


def test_bridge_apply_is_refused_on_a_bridge_older_than_the_coordinators(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_home(monkeypatch)
    run_ = fake_run(
        {"--version": (0, "0.11.0+internal\n"), "status": (0, "{}"), "install": (0, "{}")}
    )
    planned = fleet.plan(
        source=skill, home=home, bridge="apply", min_bridge="0.14.0+internal", run_=run_
    )
    assert planned["refused"] == 1 and "older than 0.14.0" in planned["actions"][-1]["note"]
    assert not [c for c in run_.calls if "install" in c]  # the old installer is not even previewed
    with pytest.raises(fleet.FleetError):
        fleet.apply_plan(planned, run_=run_)
    assert not (
        home / ".agents"
    ).exists()  # fail closed: the skill's own files are not written either
    preview = fleet.plan(source=skill, home=home, min_bridge="0.14.0", run_=run_)
    assert preview["refused"] == 0  # a preview is always allowed


def test_version_tuple() -> None:
    assert fleet.version_tuple("0.14.0+internal") == (0, 14, 0)
    assert fleet.version_tuple("0.9") < fleet.version_tuple("0.11.0")
    assert fleet.version_tuple(None) == () < fleet.version_tuple("0.0.1")


def test_drift_reports_when_the_skill_differs_between_machines() -> None:
    on = {"installed": True, "running": True, "version": "1"}
    rows = {"local": row("ok", [], on), "n": row("ok", [], on), "m": row("ok", [], on)}
    rows["local"]["shared"]["skills"][fleet.SKILL_NAME] = {"kind": "link", "hash": "a" * 64}
    rows["n"]["shared"]["skills"][fleet.SKILL_NAME] = {"kind": "dir", "hash": "b" * 64}
    assert fleet.drift(rows) == [f"{fleet.SKILL_NAME} differs: local {'a' * 12}, n {'b' * 12}"]
    rows["n"]["shared"]["skills"][fleet.SKILL_NAME]["hash"] = "a" * 64
    assert fleet.drift(rows) == []


def test_remove_takes_only_what_this_tool_wrote(home: Path, skill: Path, tmp_path: Path) -> None:
    fleet.apply_plan(make_plan(home, skill), run_=fake_run())
    theirs = home / ".codex/skills" / fleet.SKILL_NAME
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text("someone else's")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / ".grok/skills").mkdir(parents=True)
    (home / ".grok/skills" / fleet.SKILL_NAME).symlink_to(elsewhere)
    planned = fleet.plan_remove(home=home)
    assert ops(planned) == {
        "claude": "remove",
        "codex": "skip",
        "kiro": "remove",
        "grok": "skip",
        "shared": "remove",
    }
    assert (
        home / ".agents/skills" / fleet.SKILL_NAME / "SKILL.md"
    ).exists()  # a plan removes nothing
    fleet.apply_remove(planned)
    assert not (home / ".agents/skills" / fleet.SKILL_NAME).exists()
    assert not (home / ".kiro/skills" / fleet.SKILL_NAME).is_symlink()
    assert (theirs / "SKILL.md").read_text() == "someone else's"
    assert (home / ".grok/skills" / fleet.SKILL_NAME).is_symlink()
    assert fleet.plan_remove(home=home)["writes"] == 0


def test_remove_of_a_local_link_install_leaves_the_repo_alone(home: Path, skill: Path) -> None:
    fleet.apply_plan(make_plan(home, skill, link_source=True), run_=fake_run())
    fleet.apply_remove(fleet.plan_remove(home=home, source=skill))
    assert not (home / ".agents/skills" / fleet.SKILL_NAME).is_symlink()
    assert (skill / "SKILL.md").is_file()


def test_a_refused_plan_is_returned_unapplied_by_the_command_line(
    home: Path, skill: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    theirs = home / ".agents/skills" / fleet.SKILL_NAME
    theirs.mkdir(parents=True)
    assert fleet.main(["apply", "--yes", "--source", str(skill), "--bridge", "none", "--copy"]) == 1
    assert "refuse" in capsys.readouterr().out and not (theirs / "SKILL.md").exists()


def test_bridge_summary_reads_both_installer_shapes() -> None:
    skill = json.dumps(
        {"success": True, "targets": [{"client": "claude", "status": "planned", "path": "/x"}]}
    )
    clients = json.dumps(
        {"success": False, "clients": [{"client": "codex", "status": "planned", "entry": {}}]}
    )
    assert fleet._bridge_summary(skill)["targets"] == [{"client": "claude", "status": "planned"}]
    assert fleet._bridge_summary(clients)["targets"] == [{"client": "codex", "status": "planned"}]
    assert fleet._bridge_summary("not json") == "not json"


# ------------------------------------------------------------------ this repository's tests


def test_fleet_py_parses_as_python_3_9() -> None:
    """pins_existing: a LAN machine may run the script with Python 3.9."""
    source = (REAL_SKILL / "scripts" / "fleet.py").read_text()
    ast.parse(source, feature_version=(3, 9))


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_the_manifest_in_the_repository_names_the_script_version() -> None:
    """red_at_pretip (N1, D11): the folder holds the manifest, with exactly this text."""
    text = (REAL_SKILL / fleet.MANIFEST).read_text()
    assert text == '{"skill": "answerback-fleet", "version": "0.4.0"}\n'
    assert json.loads(text) == {"skill": fleet.SKILL_NAME, "version": fleet.VERSION}


def test_a_copy_written_from_the_repository_folder_equals_it_file_for_file(tmp_path: Path) -> None:
    """red_at_pretip (N1): _write_copy makes the folder's own bytes, the manifest included."""
    dest = tmp_path / "skills" / fleet.SKILL_NAME
    fleet._write_copy(dest, REAL_SKILL, fleet.tree_hash(REAL_SKILL))
    assert _files(dest) == _files(REAL_SKILL)
    assert fleet.MANIFEST in _files(dest)


def test_tree_hash_of_the_repository_folder_is_the_same_without_the_manifest(
    tmp_path: Path,
) -> None:
    """pins_existing (N1): the manifest is not part of the content hash."""
    import shutil

    bare = tmp_path / "bare"
    shutil.copytree(REAL_SKILL, bare, ignore=shutil.ignore_patterns(fleet.MANIFEST))
    assert not (bare / fleet.MANIFEST).exists()
    assert fleet.tree_hash(bare) == fleet.tree_hash(REAL_SKILL)


def test_a_plan_made_from_the_shared_entry_leaves_it_and_links_to_it(
    home: Path, skill: Path
) -> None:
    """red_at_pretip (D5 as narrowed): the script runs from the shared entry, a real folder."""
    shared = home / ".agents/skills" / fleet.SKILL_NAME
    fleet._write_copy(shared, skill, fleet.tree_hash(skill))  # the form both installers write
    planned = make_plan(home, shared, link_source=True)
    assert planned["refused"] == 0
    assert ops(planned) == {
        "shared": "unchanged",
        "claude": "link",
        "codex": "skip",
        "kiro": "link",
    }
    assert planned["actions"][0]["note"] == "the skill runs from this entry"
    applied = fleet.apply_plan(planned, run_=fake_run())
    assert applied["verify"] and all(check["ok"] for check in applied["verify"])
    for name in (".claude/skills", ".kiro/skills"):
        link = home / name / fleet.SKILL_NAME
        assert link.is_symlink() and link.resolve() == shared.resolve()
    assert shared.is_dir() and not shared.is_symlink()


def test_an_equal_real_folder_at_a_harness_is_refused_where_a_link_is_wanted(
    home: Path, skill: Path
) -> None:
    """pins_existing (ruling A05): equal bytes do not make a detached folder a link."""
    import shutil

    shutil.copytree(skill, home / ".kiro/skills" / fleet.SKILL_NAME)
    planned = make_plan(home, skill)
    assert ops(planned)["shared"] == "copy" and ops(planned)["kiro"] == "refuse"
    linked = make_plan(home, skill, link_source=True)
    assert ops(linked)["shared"] == "link" and ops(linked)["kiro"] == "refuse"


def _native_installer_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """A home where only a harness with its own skill installer is found.

    No shipped `HARNESSES` row has a `native_install` (the one that did belongs to a provider
    plugin and left the public skill with it), so the row is added for the test.
    """
    harness = fleet.Harness(
        "nativeh",
        "Native Harness",
        ("nativeh",),
        None,
        None,
        False,
        False,
        "nativeh",
        native_install=("nativeh", "skill", "install", "--profile", "default"),
    )
    monkeypatch.setattr(fleet, "HARNESSES", (*fleet.HARNESSES, harness))
    monkeypatch.setattr(fleet, "HARNESS_BY_ID", {h.id: h for h in fleet.HARNESSES})
    monkeypatch.setattr(fleet, "which", lambda name: f"/bin/{name}" if name == "nativeh" else None)


def test_a_native_installer_harness_is_skipped_on_a_lan_machine(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip (D8, fleet.py's part): a node's source is temporary, so no native step."""
    _native_installer_home(monkeypatch)
    monkeypatch.setattr(fleet, "FLEET_PAYLOAD", fleet.pack(skill), raising=False)
    argv = [
        "node-install",
        "--expect",
        fleet.tree_hash(skill),
        "--bridge",
        "none",
        "--harness",
        "nativeh",
    ]
    planned = fleet._node_install(fleet.build_parser().parse_args(argv))
    (action,) = planned["actions"]
    assert action["op"] == "skip" and action["harness"] == "nativeh" and action["argv"] == []
    assert "coordinator only" in action["note"]
    assert planned["writes"] == 0


def test_a_native_installer_harness_runs_it_on_the_machine_where_the_command_runs(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (D8): locally the harness's own installer runs with the skill as source."""
    _native_installer_home(monkeypatch)
    (action,) = make_plan(home, skill, harnesses=["nativeh"])["actions"]
    assert action["op"] == "native"
    assert action["argv"] == [
        "/bin/nativeh",
        "skill",
        "install",
        "--profile",
        "default",
        str(skill),
    ]


def _stand_in_for_subprocess(monkeypatch: pytest.MonkeyPatch, apply_code: int) -> list[list[str]]:
    """fleet.run with no real program behind it: the bridge's answers are given here."""
    calls: list[list[str]] = []

    def stand_in(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "--apply" in argv:
            return subprocess.CompletedProcess(argv, apply_code, "{}", "refused")
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, "0.14.0\n", "")
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, '{"running": true}', "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(fleet.subprocess, "run", stand_in)
    return calls


def test_an_apply_whose_bridge_step_fails_ends_with_exit_code_1(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """red_at_pretip (ruling A06): a failed bridge installer is not an exit code 0."""
    bridge_home(monkeypatch)
    calls = _stand_in_for_subprocess(monkeypatch, apply_code=1)
    argv = ["apply", "--yes", "--source", str(skill), "--copy", "--bridge", "apply"]
    assert fleet.main(argv) == 1
    assert "failed (1)" in capsys.readouterr().out
    assert len([c for c in calls if "--apply" in c]) == 2
    assert all(c[0] == "/bin/answerback" for c in calls)


def test_an_apply_whose_bridge_steps_succeed_ends_with_exit_code_0(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """pins_existing (ruling A06): the other side of the exit code."""
    bridge_home(monkeypatch)
    _stand_in_for_subprocess(monkeypatch, apply_code=0)
    argv = ["apply", "--yes", "--source", str(skill), "--copy", "--bridge", "apply"]
    assert fleet.main(argv) == 0
    assert "=> ok" in capsys.readouterr().out


def test_bridge_summary_passes_the_skill_and_the_note_and_the_text_shows_the_skill() -> None:
    """red_at_pretip (D9): the form is a target of the bridge's `skill install` after T-134."""
    report = {
        "skill": "answerback-fleet",
        "client": "shared",
        "path": "/h/.agents/skills/answerback-fleet",
        "ready": True,
        "would_change": False,
        "status": "kept",
        "note": "Another copy or version of the setup skill is here; --replace installs ours.",
    }
    summary = fleet._bridge_summary(
        json.dumps({"success": True, "applied": False, "targets": [report]})
    )
    assert summary["targets"] == [
        {
            "skill": "answerback-fleet",
            "client": "shared",
            "status": "kept",
            "would_change": False,
            "note": report["note"],
        }
    ]
    action = fleet.Action(
        "bridge", "bridge", argv=["answerback", "skill", "install"], detail=summary
    )
    body = {
        "scope": "user",
        "mode": "canonical",
        "source_kind": "copy",
        "actions": [fleet.dataclasses.asdict(action)],
    }
    text = fleet._text("plan", {"machines": {"local": body}, "left_out": []})
    line = next(line for line in text.splitlines() if "kept" in line)
    assert "answerback-fleet" in line and "shared" in line and "--replace" in line


# ------------------------------------------------- R1-01: a new copy that cannot be put in place


def _fail_the_move_into_place(
    monkeypatch: pytest.MonkeyPatch, *, and_the_way_back: bool = False
) -> None:
    """Stand in for `os.replace`: the move of the staged new copy fails, other moves are real."""
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        name = Path(src).name
        if name == "new" or (and_the_way_back and name == "old"):
            raise OSError(f"stand-in: {name} cannot be moved")
        real(src, dst)

    monkeypatch.setattr(fleet.os, "replace", replace)


def _other_source(skill: Path, tmp_path: Path) -> Path:
    other = tmp_path / "other-source"
    shutil.copytree(skill, other)
    (other / "SKILL.md").write_text("---\nname: answerback-fleet\ndescription: other\n---\n")
    return other


def _beside(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.parent.iterdir() if p.name != dest.name)


def test_a_failed_move_into_place_puts_the_previous_copy_back(
    home: Path, skill: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: at 2d1dd99 the previous copy was deleted with the staging folder."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    before = _files(dest)
    assert fleet.MANIFEST in before
    other = _other_source(skill, tmp_path)
    _fail_the_move_into_place(monkeypatch)
    with pytest.raises(fleet.FleetError, match="the previous one is back in place"):
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    assert not dest.is_symlink()
    assert _files(dest) == before
    assert _beside(dest) == []


def test_a_failed_move_into_place_puts_the_previous_link_back(
    home: Path, skill: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: at 2d1dd99 the link was removed and nothing was put back."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    dest.parent.mkdir(parents=True)
    os.symlink("../../somewhere/else", dest)
    _fail_the_move_into_place(monkeypatch)
    with pytest.raises(fleet.FleetError, match="the previous one is back in place"):
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    assert dest.is_symlink()
    assert os.readlink(dest) == "../../somewhere/else"
    assert _beside(dest) == []


def test_a_failed_move_into_place_leaves_nothing_where_nothing_was(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: an error (an OSError at 2d1dd99, a FleetError now), and nothing is left."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    _fail_the_move_into_place(monkeypatch)
    with pytest.raises((fleet.FleetError, OSError)):
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    assert not dest.exists() and not dest.is_symlink()
    assert _beside(dest) == []


def test_a_previous_copy_that_cannot_be_put_back_stays_where_the_error_says(
    home: Path, skill: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: at 2d1dd99 the previous copy was deleted; now its files stay and are named."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    before = _files(dest)
    other = _other_source(skill, tmp_path)
    _fail_the_move_into_place(monkeypatch, and_the_way_back=True)
    with pytest.raises(fleet.FleetError) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    kept = [Path(word) for word in str(caught.value).split() if word.endswith("/old")]
    assert len(kept) == 1
    assert kept[0].parent.parent == dest.parent
    assert _files(kept[0]) == before
    assert not dest.exists()


# --------------------------------------------- fix round 2: R2-02, R2-03, R2-04, an interrupt


def _leave_the_staging_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for `shutil.rmtree`: a staging folder cannot be removed, other folders can."""
    real = shutil.rmtree

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        if not Path(path).name.startswith(f".{fleet.SKILL_NAME}."):
            real(path, *args, **kwargs)

    monkeypatch.setattr(fleet.shutil, "rmtree", rmtree)


def _damage_the_staged_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for `shutil.copytree`: the real copy, then one staged file gets other bytes."""
    real = shutil.copytree

    def copytree(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        made = real(src, dst, *args, **kwargs)
        if Path(dst).name == "new":
            (Path(dst) / "SKILL.md").write_text("damaged staged bytes\n")
        return made

    monkeypatch.setattr(fleet.shutil, "copytree", copytree)


def _staging_folders(dest: Path) -> list[Path]:
    prefix = f".{fleet.SKILL_NAME}."
    return [dest.parent / name for name in _beside(dest) if name.startswith(prefix)]


def test_a_staging_folder_that_stays_after_an_update_is_named_in_the_result(
    home: Path, skill: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip (R2-03): the update is done, and the result says what is left beside it."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    assert fleet._write_copy(dest, skill, fleet.tree_hash(skill)) == "written"
    other = _other_source(skill, tmp_path)
    _leave_the_staging_folder(monkeypatch)
    result = fleet._write_copy(dest, other, fleet.tree_hash(other))
    (left,) = _staging_folders(dest)
    assert result == f"written; the staging folder {left} could not be removed"
    assert (left / "old" / "SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()
    assert (dest / "SKILL.md").read_bytes() == (other / "SKILL.md").read_bytes()


def test_an_apply_that_leaves_a_staging_folder_says_so_and_ends_with_exit_code_0(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """red_at_pretip (R2-03): the text output shows the folder; the run still counts as done."""
    _leave_the_staging_folder(monkeypatch)
    argv = ["apply", "--yes", "--source", str(skill), "--copy", "--bridge", "none"]
    assert fleet.main(argv) == 0
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    (left,) = _staging_folders(dest)
    assert f"the staging folder {left} could not be removed" in capsys.readouterr().out
    assert (dest / "SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()


def test_an_error_names_a_staging_folder_that_is_still_there(
    home: Path, skill: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip (R2-03): the refusal for a damaged staged copy, with what stays beside it."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    before = _files(dest)
    _damage_the_staged_copy(monkeypatch)
    _leave_the_staging_folder(monkeypatch)
    with pytest.raises(fleet.FleetError) as caught:
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    (left,) = _staging_folders(dest)
    text = str(caught.value)
    assert "does not match the source" in text
    assert text.endswith(f"; the staging folder {left} could not be removed")
    assert _files(dest) == before


def test_an_interrupt_at_the_move_into_place_puts_the_previous_copy_back(
    home: Path, skill: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip (ruling R2, addition): at cb360ce the previous copy went with the staging."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    before = _files(dest)
    other = _other_source(skill, tmp_path)
    interrupt = KeyboardInterrupt("stand-in: stopped at the move")
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if Path(src).name == "new":
            raise interrupt
        real(src, dst)

    monkeypatch.setattr(fleet.os, "replace", replace)
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    assert caught.value is interrupt
    assert not dest.is_symlink() and _files(dest) == before
    assert _beside(dest) == []


def test_a_staged_copy_that_differs_from_the_source_is_refused_and_nothing_is_replaced(
    home: Path, skill: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing (R2-02): the check of the staged bytes, apart from the check of the source."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    before = _files(dest)
    other = _other_source(skill, tmp_path)
    _damage_the_staged_copy(monkeypatch)
    with pytest.raises(fleet.FleetError) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    assert str(caught.value) == f"staged copy for {dest} does not match the source"
    assert not dest.is_symlink() and _files(dest) == before
    assert _beside(dest) == []


def test_a_previous_link_that_cannot_be_put_back_is_kept_where_the_error_says(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip (R2-04 as it is since rule W): the link is moved aside by one rename, so it
    is not lost. Until 53cf72f it was removed and the error said `the link to <text> is gone`."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    dest.parent.mkdir(parents=True)
    os.symlink("../../somewhere/else", dest)
    _fail_the_move_into_place(monkeypatch, and_the_way_back=True)
    with pytest.raises(fleet.FleetError) as caught:
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    (left,) = _staging_folders(dest)
    text = str(caught.value)
    assert text.endswith(f"; the previous copy is kept at {left / 'old'}")
    assert "back in place" not in text and "is gone" not in text
    assert os.readlink(left / "old") == "../../somewhere/else"
    assert not dest.exists() and not dest.is_symlink()


# ------------------------------- fix round 3: rule W, the previous entry is in one of two places

PRIORS = ("missing", "managed", "link", "linkdir", "unmanaged", "file")
FAULTS = (
    "none",  # no fault
    "copy",  # the copy step writes one file and fails (OSError)
    "copy-stop",  # the copy step writes one file, then an interrupt
    "hash",  # the staged copy does not match
    "manifest",  # the manifest cannot be written (OSError)
    "aside",  # the previous entry cannot be moved aside (OSError)
    "aside-then-stop",  # the previous entry is moved aside, then an interrupt
    "promote",  # the new copy cannot be moved into place (OSError)
    "promote+back",  # ... and the previous entry cannot be put back (OSError)
    "promote+back-stop",  # ... and an interrupt comes before the previous entry is put back
    "promote+back-then-stop",  # ... and an interrupt comes right after it was put back
    "stop",  # an interrupt in place of the move into place
    "stop+back",  # ... and the previous entry cannot be put back (OSError)
    "stop+back-stop",  # ... and a second interrupt before the previous entry is put back
    "stop+back-then-stop",  # ... and a second interrupt right after it was put back
    "promote-then-stop",  # the new copy is moved into place, then an interrupt
    # Fix round 4 (C-01, C-03):
    "promote+unread-old",  # the move into place fails (OSError), and the file system answers
    # the next question about `old` in the staging folder with an error (EIO), not yes or no
    "promote+unread-new",  # the same for `new` in the staging folder
    "tidy-stop",  # everything worked; an interrupt while the staging folder is removed
    # The row after T-134 (its second hand-back, section 6, point 1):
    "promote+ask-stop",  # the move into place fails (OSError), and an interrupt comes while the
    # routine asks whether `old` is in the staging folder
    "stop+ask-stop",  # an interrupt in place of the move, and a second one at that question
)
ASK_STOP = ("promote+ask-stop", "stop+ask-stop")
UNREAD = ("promote+unread-old", "promote+unread-new")
MOVED_ASIDE = ("managed", "link", "linkdir")
SECOND_STOP = (
    "promote+back-stop",
    "promote+back-then-stop",
    "stop+back-stop",
    "stop+back-then-stop",
    *ASK_STOP,
)
ROWS = [
    pytest.param(prior, fault, cleanup, id=f"{prior}|{fault}|{cleanup}")
    for prior in PRIORS
    for fault in FAULTS
    for cleanup in ("works", "blocked")
]


def _entry(path: Path) -> Any:
    """What is at a path: a link's text, a folder's files and links, a file's bytes, or None."""
    if path.is_symlink():
        return ("link", os.readlink(path))
    if path.is_dir():
        below = sorted(p for p in path.rglob("*") if p.is_symlink() or p.is_file())
        return ("folder", {str(p.relative_to(path)): _entry(p) for p in below})
    return ("file", path.read_bytes()) if path.is_file() else None


def _faults(monkeypatch: pytest.MonkeyPatch, dest: Path, fault: str, cleanup: str) -> Any:
    """Stand-ins for the moment of a fault only; every move, copy and removal besides is real.

    `Path.unlink` and `os.symlink` are covered too: the routine used them for a link until 53cf72f.
    Returns the first interrupt, which must come out as the same object.
    """
    first, second = KeyboardInterrupt("stand-in: first"), KeyboardInterrupt("stand-in: second")
    real_replace, real_copy, real_write = os.replace, shutil.copytree, Path.write_text
    real_unlink, real_symlink = Path.unlink, os.symlink
    real_lstat, real_rmtree, unread = os.lstat, shutil.rmtree, []
    asking: list[str] = []
    first.second = second  # type: ignore[attr-defined]

    def copytree(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if fault in ("copy", "copy-stop"):
            Path(dst).mkdir()
            (Path(dst) / "partial").write_text("partial")
            raise OSError("stand-in: copy") if fault == "copy" else first
        return real_copy(src, dst, *args, **kwargs)

    def write_text(self: Path, *args: Any, **kwargs: Any) -> Any:
        if fault == "manifest" and self.name == fleet.MANIFEST and self.parent.name == "new":
            raise OSError("stand-in: manifest")
        return real_write(self, *args, **kwargs)

    def aside(move: Any) -> None:
        if fault == "aside":
            raise OSError("stand-in: aside")
        move()
        if fault == "aside-then-stop":
            raise first

    def back(move: Any) -> None:
        if fault in ("promote+back", "stop+back"):
            raise OSError("stand-in: put back")
        if fault in ("promote+back-stop", "stop+back-stop"):
            raise second
        move()
        if fault in ("promote+back-then-stop", "stop+back-then-stop"):
            raise second

    def replace(src: Any, dst: Any) -> None:
        name = Path(src).name
        if Path(src) == dest:
            aside(lambda: real_replace(src, dst))
        elif name == "old":
            back(lambda: real_replace(src, dst))
        elif name != "new":
            real_replace(src, dst)
        elif fault.startswith("promote+") or fault == "promote":
            if fault in UNREAD:
                unread.append(fault.rpartition("-")[2])
            if fault in ASK_STOP:
                asking.append("old")
            raise OSError("stand-in: move into place")
        elif fault.startswith("stop"):
            if fault in ASK_STOP:
                asking.append("old")
            raise first
        else:
            real_replace(src, dst)
            if fault == "promote-then-stop":
                raise first

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == dest:
            aside(lambda: real_unlink(self, *args, **kwargs))
        else:
            real_unlink(self, *args, **kwargs)

    def symlink(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(dst) == dest:
            back(lambda: real_symlink(src, dst, *args, **kwargs))
        else:
            real_symlink(src, dst, *args, **kwargs)

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if asking and asking[0] == os.path.basename(os.fspath(path)):
            asking.clear()  # the second interrupt comes once, at the first question
            raise second
        if unread and unread[0] == os.path.basename(os.fspath(path)):
            unread.clear()  # one question is not answered; every later one is
            raise OSError(errno.EIO, "stand-in: the file system does not answer", str(path))
        return real_lstat(path, *args, **kwargs)

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        staging = Path(path).name.startswith(f".{fleet.SKILL_NAME}.")
        in_place = not os.path.lexists(Path(path) / "new") and os.path.lexists(dest)
        if fault == "tidy-stop" and staging and in_place:
            raise first
        if not (cleanup == "blocked" and staging):
            real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(fleet.os, "replace", replace)
    monkeypatch.setattr(fleet.os, "symlink", symlink)
    monkeypatch.setattr(fleet.os, "lstat", lstat)
    monkeypatch.setattr(fleet.shutil, "copytree", copytree)
    monkeypatch.setattr(fleet.shutil, "rmtree", rmtree)
    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "unlink", unlink)
    return first


# Rows that are red at 53cf72f (74 of 192), by what is there before and the cleanup:
#   every state, cleanup works:    copy, manifest
#   every state, cleanup blocked:  copy, copy-stop, manifest
#   missing, blocked:              stop, stop+back, promote-then-stop
#   managed, works:                aside, aside-then-stop, promote+back-stop, stop+back,
#                                  stop+back-stop, promote-then-stop
#   managed, blocked:              the same, and stop
#   link and linkdir, works:       aside, aside-then-stop, promote+back, promote+back-stop,
#                                  stop+back, stop+back-stop
#   link and linkdir, blocked:     the same, and stop, promote-then-stop
# The exact rows are the failed ids of the red run of this test at 53cf72f.
# Rows that are red at 474e7f3 (20 of 228), the three places for a fault of fix round 4:
#   managed, link and linkdir, both cleanups:           promote+unread-old, promote+unread-new
#   missing, managed, link and linkdir, both cleanups:  tidy-stop
# The other 16 new rows (unmanaged and file, and missing with an unread answer) pass there.
@pytest.mark.parametrize(("prior", "fault", "cleanup"), ROWS)
def test_the_previous_entry_is_in_one_of_two_places_whatever_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    prior: str,
    fault: str,
    cleanup: str,
) -> None:
    """Rule W over what is there before, where the fault comes, and whether the cleanup works."""
    source, outside = tmp_path / "source", tmp_path / "external" / "real"
    source.mkdir()
    (source / "SKILL.md").write_text("new")
    dest = tmp_path / "installed" / fleet.SKILL_NAME
    dest.parent.mkdir()
    manifest = json.dumps({"skill": fleet.SKILL_NAME, "version": fleet.VERSION}) + "\n"
    if prior in ("managed", "unmanaged"):
        dest.mkdir()
        (dest / "SKILL.md").write_text("old")
        if prior == "managed":
            (dest / fleet.MANIFEST).write_text(manifest)
    elif prior == "link":
        dest.symlink_to("../../external/old")
    elif prior == "linkdir":
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("outside")
        dest.symlink_to("../external/real")
    elif prior == "file":
        dest.write_text("old file")
    before, outside_before = _entry(dest), _entry(outside)
    expect = "wrong" if fault == "hash" else fleet.tree_hash(source)
    first = _faults(monkeypatch, dest, fault, cleanup)
    result, raised = None, None
    try:
        result = fleet._write_copy(dest, source, expect)
    except BaseException as stop:  # noqa: BLE001 - every way out of the routine is judged
        raised = stop
    monkeypatch.undo()
    after = _entry(dest)
    beside = [dest.parent / name for name in _beside(dest)]
    words = f"{result} {raised} {capsys.readouterr().err}"
    new_copy = after == (
        "folder",
        {"SKILL.md": ("file", b"new"), fleet.MANIFEST: ("file", manifest.encode())},
    )
    kept = [folder for folder in beside if before is not None and _entry(folder / "old") == before]
    took_effect = fault in ("none", "promote-then-stop", "tidy-stop") and prior in MOVED_ASIDE
    # V1: the previous entry is never lost.
    if before is not None and after != before:
        assert len(kept) == 1 or (new_copy and took_effect)
    # V2: `written` means that the new copy is at the destination.
    if result is not None:
        assert result.startswith("written") and new_copy
    # V3: no folder is left without a word (not asked after a second interrupt).
    if fault not in SECOND_STOP:
        assert [str(folder) for folder in beside if str(folder) not in words] == []
    # V4: a previous entry that is kept in a staging folder is named with its path.
    if after != before and len(kept) == 1 and not new_copy:
        assert str(kept[0] / "old") in words
    # V5: one interrupt goes on as the same object.
    reached = prior in ("missing", *MOVED_ASIDE)
    if fault == "copy-stop" or (fault == "aside-then-stop" and prior in MOVED_ASIDE):
        assert raised is first
    if fault in ("stop", "stop+back", "promote-then-stop", "tidy-stop") and reached:
        assert raised is first
    # V8: a second interrupt while the routine asks goes on as the same object, nothing more is
    # moved or removed, and the line names `old` (V4 above asserts the path for these rows too).
    if fault in ASK_STOP and reached:
        assert raised is first.second  # type: ignore[attr-defined]
        assert after is None and len(beside) == 1 and _entry(beside[0] / "new") is not None
        line = f"fleet: stopped while putting back {dest}; the previous copy is kept at "
        assert (line in words) == (prior in MOVED_ASIDE)
    # V6: a fault that is an OSError leaves the routine as a FleetError.
    oserror = ("copy", "manifest", "aside", "promote", "promote+back", *UNREAD)
    if fault in oserror and raised is not None:
        assert isinstance(raised, fleet.FleetError)
    # V7: a folder that a previous link pointed to is never touched.
    assert _entry(outside) == outside_before


def test_neither_is_said_when_the_file_system_does_not_answer_for_the_destination(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit `own-dest-unread-wording` of T-134's second hand-back. pins_existing."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    real_replace, real_is_there = os.replace, fleet._is_there

    def replace(src: Any, dst: Any) -> None:
        if Path(src).name == "new":
            raise OSError("stand-in: move into place")
        real_replace(src, dst)

    monkeypatch.setattr(fleet.os, "replace", replace)
    monkeypatch.setattr(
        fleet,
        "_is_there",
        lambda path: "stand-in: no answer" if path == dest else real_is_there(path),
    )
    with pytest.raises(fleet.FleetError) as caught:
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    said = f"the new copy could not be put at {dest} (stand-in: move into place)"
    assert str(caught.value) == said


def test_a_second_stop_is_named_when_the_file_system_does_not_say_where_old_is(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The edit `own-second-stop-on-yes` of T-134's second hand-back: the line is written on
    every answer but a clear no. pins_existing."""
    dest, _ = _installed(home, skill, "folder")
    second = KeyboardInterrupt("stand-in: second")
    real_replace, real_is_there, stopped = os.replace, fleet._is_there, []

    def replace(src: Any, dst: Any) -> None:
        if Path(src).name == "new":
            raise OSError("stand-in: move into place")
        if Path(src).name == "old":
            stopped.append(True)
            raise second
        real_replace(src, dst)

    def is_there(path: Path) -> str:
        return "stand-in: no answer" if stopped and path.name == "old" else real_is_there(path)

    monkeypatch.setattr(fleet.os, "replace", replace)
    monkeypatch.setattr(fleet, "_is_there", is_there)
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    (left,) = _staging_folders(dest)
    assert caught.value is second
    line = f"fleet: stopped while putting back {dest}; the previous copy is kept at {left / 'old'}"
    assert line in capsys.readouterr().err


def test_a_staging_folder_is_asked_for_with_three_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-134's second hand-back, section 6, point 3: `Path.exists` says False for a folder that
    cannot be read; the folder is named unless the file system says it is gone. red_before."""
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(fleet.shutil, "rmtree", lambda path, ignore_errors=False: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert fleet._left_behind(staging) == f"the staging folder {staging} could not be removed"
    monkeypatch.setattr(fleet, "_is_there", lambda path: "Permission denied")
    assert fleet._left_behind(staging) == f"the staging folder {staging} could not be removed"
    monkeypatch.setattr(fleet, "_is_there", lambda path: "no")
    assert fleet._left_behind(staging) == ""


def _installed(home: Path, skill: Path, previous: str) -> tuple[Path, Any]:
    """A destination with a previous copy this tool wrote, or a previous link; and what it is."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    if previous == "folder":
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    else:
        dest.parent.mkdir(parents=True)
        os.symlink("../../somewhere/else", dest)
    return dest, _entry(dest)


def _stop_the_moves(
    monkeypatch: pytest.MonkeyPatch,
    dest: Path,
    *,
    aside: BaseException | None = None,
    into_place: BaseException | None = None,
    after_into_place: BaseException | None = None,
    back: BaseException | None = None,
) -> None:
    """Stand in for `os.replace`: raise at one move or more. `aside` and `after_into_place` are
    raised after the real rename, `into_place` and `back` in place of it."""
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        name = Path(src).name
        if name == "new" and into_place is not None:
            raise into_place
        if name == "old" and back is not None:
            raise back
        real(src, dst)
        if Path(src) == dest and aside is not None:
            raise aside
        if name == "new" and after_into_place is not None:
            raise after_into_place

    monkeypatch.setattr(fleet.os, "replace", replace)


@pytest.mark.parametrize("previous", ["folder", "link"])
def test_an_interrupt_while_the_previous_one_is_put_back_keeps_it_in_the_staging_folder(
    home: Path,
    skill: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    previous: str,
) -> None:
    """red_at_pretip (R3-02): at 53cf72f the previous folder went with the staging folder."""
    dest, before = _installed(home, skill, previous)
    other = _other_source(skill, tmp_path)
    interrupt = KeyboardInterrupt("stand-in: stopped while putting back")
    _stop_the_moves(monkeypatch, dest, into_place=OSError("stand-in"), back=interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    assert caught.value is interrupt
    (left,) = _staging_folders(dest)
    assert _entry(left / "old") == before
    if previous == "link":
        assert os.readlink(left / "old") == "../../somewhere/else"
    assert capsys.readouterr().err == (
        f"fleet: stopped while putting back {dest}; the previous copy is kept at {left / 'old'}\n"
    )


@pytest.mark.parametrize("previous", ["folder", "link"])
def test_an_interrupt_right_after_the_previous_one_was_moved_aside_puts_it_back(
    home: Path,
    skill: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    previous: str,
) -> None:
    """red_at_pretip (R3-02): the rename is done and then the interrupt comes."""
    dest, before = _installed(home, skill, previous)
    other = _other_source(skill, tmp_path)
    interrupt = KeyboardInterrupt("stand-in: stopped after the move aside")
    _stop_the_moves(monkeypatch, dest, aside=interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    assert caught.value is interrupt
    assert _entry(dest) == before
    assert _beside(dest) == []
    assert capsys.readouterr().err == (
        f"fleet: stopped while updating {dest}; the previous one is back in place\n"
    )


def test_an_interrupt_at_the_move_into_place_puts_the_previous_link_back(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """red_at_pretip (R3-03) by the line on standard error only: the link was put back at
    53cf72f too, and no test said so."""
    dest, _ = _installed(home, skill, "link")
    interrupt = KeyboardInterrupt("stand-in: stopped at the move")
    _stop_the_moves(monkeypatch, dest, into_place=interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    assert caught.value is interrupt
    assert dest.is_symlink() and os.readlink(dest) == "../../somewhere/else"
    assert _beside(dest) == []
    assert capsys.readouterr().err == (
        f"fleet: stopped while updating {dest}; the previous one is back in place\n"
    )


@pytest.mark.parametrize("previous", ["folder", "link"])
def test_an_interrupt_at_the_move_into_place_keeps_a_previous_one_that_cannot_be_put_back(
    home: Path,
    skill: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    previous: str,
) -> None:
    """red_at_pretip (R3-03, R3-05): the folder was kept and not named; the link was lost."""
    dest, before = _installed(home, skill, previous)
    other = _other_source(skill, tmp_path)
    interrupt = KeyboardInterrupt("stand-in: stopped at the move")
    _stop_the_moves(monkeypatch, dest, into_place=interrupt, back=OSError("stand-in: no way back"))
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    assert caught.value is interrupt
    (left,) = _staging_folders(dest)
    assert _entry(left / "old") == before
    assert not dest.exists() and not dest.is_symlink()
    assert capsys.readouterr().err == (
        f"fleet: stopped while updating {dest}, and the previous one could not be put back "
        f"(stand-in: no way back); the previous copy is kept at {left / 'old'}\n"
    )


@pytest.mark.parametrize("step", ["copy", "manifest", "aside"])
def test_a_plain_error_is_a_fleet_error_that_names_the_staging_folder_that_stays(
    home: Path, skill: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """red_at_pretip (R3-05): at 53cf72f the OSError came out as it was and named no folder."""
    dest, before = _installed(home, skill, "folder")
    other = _other_source(skill, tmp_path)
    _faults(monkeypatch, dest, step, "blocked")
    with pytest.raises(fleet.FleetError) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    (left,) = _staging_folders(dest)
    assert str(caught.value) == (
        f"the new copy could not be put at {dest} (stand-in: {step}); nothing was replaced"
        f"; the staging folder {left} could not be removed"
    )
    assert _entry(dest) == before


def test_an_update_whose_copy_step_fails_ends_with_exit_code_1_and_one_line(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """red_at_pretip (R3-05): at 53cf72f `main` let the OSError through as a traceback."""
    dest, before = _installed(home, skill, "folder")
    (skill / "SKILL.md").write_text("---\nname: answerback-fleet\ndescription: next\n---\n")
    _faults(monkeypatch, dest, "copy", "works")
    argv = ["apply", "--yes", "--source", str(skill), "--copy", "--bridge", "none"]
    assert fleet.main(argv) == 1
    assert capsys.readouterr().err == (
        f"fleet: the new copy could not be put at {dest} (stand-in: copy); nothing was replaced\n"
    )
    assert _entry(dest) == before
    assert _beside(dest) == []


def test_an_interrupt_at_the_move_into_place_names_a_staging_folder_that_stays(
    home: Path,
    skill: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """red_at_pretip (R3-05): at 53cf72f nothing was written to standard error."""
    dest, before = _installed(home, skill, "folder")
    other = _other_source(skill, tmp_path)
    interrupt = KeyboardInterrupt("stand-in: stopped at the move")
    _stop_the_moves(monkeypatch, dest, into_place=interrupt)
    _leave_the_staging_folder(monkeypatch)
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    assert caught.value is interrupt
    (left,) = _staging_folders(dest)
    assert _entry(dest) == before
    assert capsys.readouterr().err == (
        f"fleet: stopped while updating {dest}; the previous one is back in place"
        f"; the staging folder {left} could not be removed\n"
    )


def test_an_interrupt_right_after_the_move_into_place_leaves_the_new_copy(
    home: Path,
    skill: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """red_at_pretip: at 53cf72f the new copy stayed too, but the staging folder with the
    previous copy was left beside it without a word."""
    dest, _ = _installed(home, skill, "folder")
    other = _other_source(skill, tmp_path)
    interrupt = KeyboardInterrupt("stand-in: stopped after the move")
    _stop_the_moves(monkeypatch, dest, after_into_place=interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    assert caught.value is interrupt
    assert (dest / "SKILL.md").read_bytes() == (other / "SKILL.md").read_bytes()
    assert (dest / fleet.MANIFEST).is_file()
    assert _beside(dest) == []
    assert capsys.readouterr().err == ""


def test_a_failed_move_into_place_with_nothing_there_before_says_so(
    home: Path, skill: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: the text was the same at 53cf72f, and no test read it."""
    dest = home / ".agents/skills" / fleet.SKILL_NAME
    _fail_the_move_into_place(monkeypatch)
    with pytest.raises(fleet.FleetError) as caught:
        fleet._write_copy(dest, skill, fleet.tree_hash(skill))
    assert str(caught.value) == (
        f"the new copy could not be put at {dest} (stand-in: new cannot be moved)"
        "; nothing was there before"
    )
    assert _beside(dest) == []


# ---------------- fix round 4: C-01 (an answer that is not given is not a no), C-03 (tidying)


def _do_not_answer_for(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """Stand in for `os.lstat`: the first question about `name` in a staging folder gets EIO.

    Every other question, and every later one, is answered by the file system. Returns the list
    of the questions that were not answered.
    """
    real, unanswered = os.lstat, []

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        asked = Path(os.fspath(path))
        in_staging = asked.parent.name.startswith(f".{fleet.SKILL_NAME}.")
        if not unanswered and in_staging and asked.name == name:
            unanswered.append(str(asked))
            raise OSError(errno.EIO, "stand-in: the file system does not answer", str(asked))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(fleet.os, "lstat", lstat)
    return unanswered


@pytest.mark.parametrize("previous", ["folder", "link"])
@pytest.mark.parametrize("unread", ["old", "new"])
def test_a_failed_move_into_place_keeps_everything_when_the_file_system_does_not_answer(
    home: Path,
    skill: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous: str,
    unread: str,
) -> None:
    """red_at_pretip (C-01): at 474e7f3 the unanswered question was taken for "it is not there",
    the staging folder was removed with the previous entry and the new copy in it, and the error
    ended "nothing was there before"."""
    dest, before = _installed(home, skill, previous)
    other = _other_source(skill, tmp_path)
    _stop_the_moves(monkeypatch, dest, into_place=OSError("stand-in: new cannot be moved"))
    unanswered = _do_not_answer_for(monkeypatch, unread)
    with pytest.raises(fleet.FleetError) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    monkeypatch.undo()
    (left,) = _staging_folders(dest)
    assert unanswered == [str(left / unread)]
    assert _entry(left / "old") == before
    if previous == "link":
        assert os.readlink(left / "old") == "../../somewhere/else"
    assert (left / "new" / "SKILL.md").read_bytes() == (other / "SKILL.md").read_bytes()
    assert not os.path.lexists(dest)
    text = str(caught.value)
    assert text.startswith(
        f"the new copy could not be put at {dest} (stand-in: new cannot be moved)"
        "; it could not be read where the previous one is ("
    )
    assert text.endswith(f"); nothing was removed, look at {left / 'old'}")
    assert "nothing was there before" not in text and "nothing was replaced" not in text
    assert "back in place" not in text


@pytest.mark.parametrize("previous", ["folder", "link"])
def test_an_interrupt_while_the_staging_folder_is_removed_after_an_update_names_it(
    home: Path,
    skill: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    previous: str,
) -> None:
    """red_at_pretip (C-03): at 474e7f3 the same interrupt came out and the new copy was in its
    place, but the staging folder with the previous entry was left without a word."""
    dest, before = _installed(home, skill, previous)
    other = _other_source(skill, tmp_path)
    interrupt = KeyboardInterrupt("stand-in: stopped while tidying")

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        raise interrupt

    monkeypatch.setattr(fleet.shutil, "rmtree", rmtree)
    with pytest.raises(KeyboardInterrupt) as caught:
        fleet._write_copy(dest, other, fleet.tree_hash(other))
    monkeypatch.undo()
    assert caught.value is interrupt
    assert not dest.is_symlink()
    assert (dest / "SKILL.md").read_bytes() == (other / "SKILL.md").read_bytes()
    assert (dest / fleet.MANIFEST).is_file()
    (left,) = _staging_folders(dest)
    assert _entry(left / "old") == before
    assert capsys.readouterr().err == (
        f"fleet: stopped after updating {dest}; the staging folder {left} is still there\n"
    )


class FakeBridge:
    """A stateful stand-in for `answerback` on one machine, driven through fleet's run_."""

    def __init__(
        self,
        tmp: Path,
        *,
        installed: bool = True,
        version: str = "0.14.0",
        configured: bool = False,
        running: bool = False,
        registered: bool = False,
        skilled: bool = False,
    ) -> None:
        self.config = tmp / "cfg" / "config.json"
        self.installed, self.version, self.running = installed, version, running
        self.registered, self.skilled = registered, skilled
        self.start_error: str | None = None
        self.fleet_enabled = False
        self.native_enabled = False  # the bridge's own unit for this configuration
        self.manager = True  # a user manager answers
        self.other_units: set[str] = set()  # units of other configurations, enabled and active
        self.failing: set[str] = set()  # systemctl actions that fail
        self.stop_fails = self.stop_gone = self.start_lies = self.detached_fails = False
        self.status_good: int | None = None  # after this many answers, status says nothing usable
        self.blind: list[int] = []  # where in `calls` a status gave nothing usable
        self.tasks: Any = {"working": 1, "submitted": 2, "completed": 5, "failed": 1}
        self.conflict: str | None = None
        self.answer: dict[str, Any] = {}  # per installer: (exit code, text) instead of its rows
        self.row_status: dict[str, str] = {}  # per client: the status of its unchanged rows
        self.applied_status: dict[str, str] = {}  # per client: `migrated` instead of `installed`
        self.calls: list[list[str]] = []
        # The bridge that runs, apart from the program on disk: its instance and its version.
        self.instance = 1
        self.running_version: str | None = None  # taken from the program when it starts
        self.says_version = True
        self.inactive: set[str] = set()  # units that are enabled and yet not active
        self.enable_starts = True  # `enable --now` of our unit brings the bridge up
        self.disable_kills = False  # a `disable --now` that fails has taken the bridge down
        self.restart_error: str | None = None  # the restart fails and the old bridge stays
        self.restart_noop = self.restart_old = self.restart_drops_unit = False
        self.start_outside = False  # the bridge's own start leaves it up outside its unit
        self.later_stop_fails = self.stop_blinds = False
        # a later stop that reports failure: it took the bridge down; its own unit stays enabled;
        # no status can be read after it
        self.later_stop_gone = self.later_stop_keeps_unit = self.later_stop_blinds = False
        self.stop_drains = ""  # "first" or "later": that stop gives up while the bridge drains
        self.draining = 0  # so many reads of the status say running and not accepting
        self.start_fails_up = False  # the bridge's own start reports failure and leaves it up
        self.stops = 0
        self.raises: set[str] = set()  # verbs of the bridge whose program cannot be started
        if configured:
            self._configure()

    def _begin(self, old: bool = False) -> None:
        """A new process of the bridge: another instance, at the program's version."""
        self.running = True
        self.instance += 1
        if not old or self.running_version is None:
            self.running_version = self.version

    def _configure(self) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text('{"hc_enabled": true}')

    def which(self, name: str) -> str | None:
        if name == "answerback":
            return "/bin/answerback" if self.installed else None
        return f"/bin/{name}" if name in {"claude", "kiro-cli", "uv", "hc"} else None

    def _installer(self, argv: list[str], done: bool, key: str, applied: bool = False) -> str:
        """The installer's answer. `done`: nothing was to do when it was asked. `applied`: the
        answer of `--apply`, whose planned rows are `installed` (or `migrated`) with the flag of
        their plan (registration.py: the apply changes the status and leaves the flag)."""
        rows: list[dict[str, Any]] = []
        asked = [argv[i + 1] for i, word in enumerate(argv) if word == "--client"]
        skills = [fleet.USAGE_SKILL, fleet.SKILL_NAME] if key == "targets" else [None]
        for skill in skills:
            for client in asked:
                entry = {"command": "/bin/answerback", "args": ["mcp", "--client", client]}
                row: dict[str, Any] = {"client": client, "entry": entry}
                if skill:
                    row["skill"] = skill
                if self.conflict and "--replace" not in argv:
                    row.update(status="refused", error=self.conflict, would_change=False)
                elif applied and not done:
                    row.update(
                        status=self.applied_status.get(client, "installed"), would_change=True
                    )
                else:
                    status = self.row_status.get(client, "unchanged") if done else "planned"
                    row.update(status=status, would_change=not done)
                rows.append(row)
        answer: dict[str, Any] = {"success": True, key: rows}
        if (
            key == "targets"
        ):  # registration.py:1931: the skills that were looked for, name -> folder
            answer["sources"] = {skill: f"/pkg/skills/{skill}" for skill in skills}
        return json.dumps(answer)

    def __call__(self, argv: list[str], timeout: float = 30, stdin: str | None = None):
        self.calls.append(argv)
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if argv[0] == "systemctl":
            action = argv[2:]
            no = subprocess.CompletedProcess(argv, 1, "", f"Failed to {action[0]}: stand-in")
            if not self.manager or action[0] in self.failing:
                if self.manager and action[0] == "disable" and self.disable_kills:
                    self.running = False
                return no
            mine = {
                fleet.FLEET_UNIT: self.fleet_enabled,
                **dict.fromkeys(fleet._unit_names(self.config), self.native_enabled),
                **dict.fromkeys(self.other_units, True),
            }
            if action[:2] == ["enable", "--now"]:
                self.fleet_enabled = True
                if self.enable_starts and not self.running:
                    self._begin()
            elif action[0] == "enable":  # without --now: nothing is started
                self.fleet_enabled = True
            elif action[:2] == ["disable", "--now"]:
                self.running = self.running and not self.fleet_enabled
                self.fleet_enabled = False
            elif action[0] == "is-enabled":
                if action[1] in mine and not mine[action[1]]:  # as systemctl: the word, and exit 1
                    return subprocess.CompletedProcess(argv, 1, "disabled\n", "")
                return ok("enabled\n") if mine.get(action[1]) else no
            elif action[0] == "is-active":
                held = mine.get(action[1]) and (self.running or action[1] in self.other_units)
                return ok("active\n") if held and action[1] not in self.inactive else no
            return ok()
        verb = argv[argv.index("/bin/answerback") + 1 :] if "/bin/answerback" in argv else argv[1:]
        if verb[:1] and verb[0] in self.raises:
            raise PermissionError(f"stand-in: {verb[0]} cannot be started")
        if verb == ["--version"]:
            return ok(self.version + "\n")
        if verb == ["status"]:
            if self.status_good is not None:
                self.status_good -= 1
                if self.status_good < 0:
                    self.blind.append(len(self.calls) - 1)
                    return ok("no answer that can be read")
            sock = str(self.config.parent / "runtime" / "bridge.sock")
            drains = self.draining > 0
            self.draining -= drains
            now: dict[str, Any] = {
                "running": self.running,
                "accepting": self.running and not drains,
                "socket": sock,
            }
            if self.running:
                if self.running_version is None:  # it ran before the test began: the program then
                    self.running_version = self.version
                now["instance_id"] = f"instance-{self.instance}"
                if self.says_version:
                    now["version"] = self.running_version
            if drains and not self.draining:  # drained: the next read finds it down
                self.running = self.native_enabled = False
                self.running_version = None
            return ok(json.dumps({**now, "tasks": self.tasks}))
        if verb[:1] == ["configure"]:
            self._configure()
            return ok()
        if verb == ["stop"]:
            self.stops += 1
            later = self.stops > 1
            if self.stop_blinds or (later and self.later_stop_blinds):
                self.status_good = 0
            if self.stop_drains == ("later" if later else "first"):
                self.draining = 3
                return subprocess.CompletedProcess(
                    argv, 1, json.dumps({"error": "Bridge is still draining"}), ""
                )
            if self.stop_fails or (self.later_stop_fails and later):
                self.running = self.running and not (self.stop_fails and self.stop_gone)
                if later and self.later_stop_gone:
                    self.running, self.running_version = False, None
                    self.native_enabled = self.later_stop_keeps_unit
                return subprocess.CompletedProcess(
                    argv, 1, json.dumps({"error": "stop failed"}), ""
                )
            self.running = self.native_enabled = False  # stop removes the native supervision first
            self.running_version = None
            return ok()
        if verb[:1] in (["start"], ["restart"]):
            failing = self.detached_fails if "--standalone" in verb else self.start_error
            if verb == ["restart"]:
                # A restart is a stop and a start. One that fails here leaves the old bridge.
                failing = self.restart_error or failing
                if not failing and self.restart_noop:
                    return ok()
            if failing:
                return subprocess.CompletedProcess(argv, 1, json.dumps({"error": str(failing)}), "")
            if self.start_lies and "--standalone" not in verb:
                return ok()
            if self.start_fails_up and verb == ["start"]:
                if not self.running:
                    self._begin()
                self.native_enabled = False
                return subprocess.CompletedProcess(
                    argv, 1, json.dumps({"error": "start said no"}), ""
                )
            if verb == ["restart"] or not self.running:
                self._begin(old=self.restart_old)
            if self.restart_drops_unit or self.start_outside:
                self.native_enabled = False
            else:
                self.native_enabled = self.native_enabled or "--standalone" not in verb
            return ok()
        if verb[:2] in (["clients", "install"], ["skill", "install"]) and verb[0] in self.answer:
            code, text = self.answer[verb[0]]
            return subprocess.CompletedProcess(argv, code, text, "")
        if verb[:2] == ["clients", "install"]:
            was = self.registered
            if "--apply" in verb:
                self.registered = True
            return ok(self._installer(argv, was, "clients", "--apply" in verb))
        if verb[:2] == ["skill", "install"]:
            was = self.skilled
            if "--apply" in verb:
                self.skilled = True
            return ok(self._installer(argv, was, "targets", "--apply" in verb))
        return subprocess.CompletedProcess(argv, 127, "", "not found")

    def wrote(self) -> list[list[str]]:
        words = {"configure", "start", "restart", "--apply"}
        return [c for c in self.calls if words & set(c)]


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBridge:
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeBridge(tmp_path)
    monkeypatch.setattr(fleet, "which", fake.which)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return fake


def states(result: dict[str, Any]) -> dict[str, str]:
    return {s["step"]: s["state"] for s in result["steps"]}


def answers(command: list[str]) -> tuple[bool, str]:
    return True, "answerback 0.14.0"


def test_ensure_preview_of_a_fresh_machine_changes_nothing(bridge: FakeBridge) -> None:
    result = fleet.ensure_here(write=False, want="0.14.0", run_=bridge, handshake_=answers)
    assert states(result) == {
        "install": "ok",
        "configure": "change",
        "start": "change",
        "register": "change",
        "skill": "change",
        "handshake": "skip",
    }
    assert not result["ok"] and result["failed"] == 0 and bridge.wrote() == []


def test_ensure_brings_a_machine_up_and_a_second_run_changes_nothing(bridge: FakeBridge) -> None:
    first = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert states(first) == {
        "install": "ok",
        "configure": "done",
        "start": "done",
        "register": "done",
        "skill": "done",
        "handshake": "ok",
    }
    assert first["ok"]
    bridge.calls.clear()
    second = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert set(states(second).values()) == {"ok"} and bridge.wrote() == []


def test_ensure_reports_a_failed_start_and_does_not_fall_back_by_itself(bridge: FakeBridge) -> None:
    bridge.start_error = "Unit local.answerback-bridge.service has a bad unit file setting."
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    start = next(s for s in result["steps"] if s["step"] == "start")
    assert start["state"] == "fail" and "bad unit file" in start["detail"]
    assert states(result)["handshake"] == "skip" and not result["ok"]
    assert not [c for c in bridge.calls if "--standalone" in c]
    again = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=bridge, handshake_=answers
    )
    assert (
        states(again)["start"] == "done"
        and "standalone" in again["steps"][2]["detail"]
        and again["ok"]
    )


def test_ensure_reports_a_conflicting_entry_and_replaces_only_when_told(bridge: FakeBridge) -> None:
    bridge.conflict = "MCP entry conflict"
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    register = next(s for s in result["steps"] if s["step"] == "register")
    assert (
        register["state"] == "refused"
        and "--replace" in register["detail"]
        and not bridge.registered
    )
    replaced = fleet.ensure_here(
        write=True, want="0.14.0", replace=True, run_=bridge, handshake_=answers
    )
    assert states(replaced)["register"] == "done" and bridge.registered


def test_ensure_fails_when_a_registered_entry_does_not_answer(bridge: FakeBridge) -> None:
    result = fleet.ensure_here(
        write=True, want="0.14.0", run_=bridge, handshake_=lambda c: (False, "no answer")
    )
    assert (
        states(result)["handshake"] == "fail"
        and "claude: no answer" in result["steps"][-1]["detail"]
    )
    assert not result["ok"]


def test_ensure_installs_or_upgrades_only_with_a_wheel(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge.version = "0.11.0"
    assert states(fleet.ensure_here(write=False, want="0.14.0", run_=bridge))["install"] == "change"
    missing = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert (
        states(missing)["install"] == "fail" and "no release wheel" in missing["steps"][0]["detail"]
    )

    def install(wheel: dict[str, Any], run_: Any = None) -> None:
        bridge.version = "0.14.0"

    monkeypatch.setattr(fleet, "install_bridge", install)
    bridge.running = True
    bridge._configure()
    done = fleet.ensure_here(
        write=True, want="0.14.0", wheel={"name": "w"}, run_=bridge, handshake_=answers
    )
    assert done["steps"][0] == {
        "step": "install",
        "state": "done",
        "detail": "answerback 0.14.0 (was 0.11.0)",
    }
    assert ["/bin/answerback", "restart"] in bridge.calls  # an upgraded running bridge is restarted


def test_ensure_without_a_bridge_program_skips_the_rest(bridge: FakeBridge) -> None:
    bridge.installed = False
    result = fleet.ensure_here(write=False, want="0.14.0", run_=bridge)
    assert states(result)["install"] == "change" and set(list(states(result).values())[1:]) == {
        "skip"
    }


def test_handshake_talks_to_a_real_process(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(
        "import json,sys\n"
        "req=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'serverInfo':{'name':'fake','version':'1'}}}))\n"
    )
    assert fleet.handshake(["python3", str(server)]) == (True, "fake 1")
    silent = tmp_path / "silent.py"
    silent.write_text("import sys\nsys.stderr.write('bridge is stopped')\n")
    assert fleet.handshake(["python3", str(silent)]) == (False, "bridge is stopped")
    assert fleet.handshake(["/nonexistent/program"])[0] is False


def test_coordinator_wheel_reads_the_install_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "releases" / "0.14.0-x"
    release.mkdir(parents=True)
    wheel = release / "answerback-0.14.0-py3-none-any.whl"
    import zipfile

    with zipfile.ZipFile(wheel, "w") as archive:  # rule V3: the wheel's own Version: is read
        archive.writestr(
            "answerback-0.14.0.dist-info/METADATA", "Name: answerback\nVersion: 0.14.0\n"
        )
    (release / "constraints-live-env.txt").write_text("anyio==4.16.0\n")
    tools = tmp_path / "tools"
    (tools / "answerback").mkdir(parents=True)
    (tools / "answerback" / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "answerback", extras = ["bus"], path = "{wheel}" }}]\n'
    )
    monkeypatch.setattr(fleet, "which", lambda name: "/bin/uv")
    found = fleet.coordinator_wheel(fake_run({"tool dir": (0, f"{tools}\n")}))
    assert (
        found["name"] == wheel.name
        and found["release"] == "0.14.0-x"
        and found["extras"] == ["bus"]
    )
    assert found["constraints"] == "anyio==4.16.0\n" and len(found["sha256"]) == 64
    assert found["version"] == "0.14.0"
    (tools / "answerback" / "uv-receipt.toml").write_text(
        'requirements = [{ name = "answerback", directory = "/x" }]\n'
    )
    assert fleet.coordinator_wheel(fake_run({"tool dir": (0, f"{tools}\n")})) is None


def test_handshake_says_when_the_registered_entry_itself_was_not_tried(bridge: FakeBridge) -> None:
    bridge._configure()
    bridge.running = True
    bridge.conflict = "MCP entry conflict"
    result = fleet.ensure_here(write=False, want="0.14.0", run_=bridge, handshake_=answers)
    last = result["steps"][-1]
    assert (
        last["state"] == "ok"
        and "claude kiro" in last["detail"]
        and "was not tried" in last["detail"]
    )
    assert states(result)["register"] == "refused" and not result["ok"]


def test_a_standalone_start_leaves_the_callers_process_tree(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    which = bridge.which
    monkeypatch.setattr(
        fleet, "which", lambda n: "/bin/" + n if n in {"systemd-run", "setsid"} else which(n)
    )
    monkeypatch.setattr(fleet.sys, "platform", "linux")
    monkeypatch.setattr(fleet.time, "sleep", lambda s: None)
    bridge.start_error = (
        "bad unit file setting"  # and no systemctl: only the transient start is left
    )
    fleet.ensure_here(write=True, want="0.14.0", standalone=True, run_=bridge, handshake_=answers)
    start = next(c for c in bridge.calls if "--standalone" in c)
    assert start[:3] == ["systemd-run", "--user", f"--unit={fleet.STANDALONE_UNIT}"]
    assert start[-5:] == ["-p", "Type=forking", "/bin/answerback", "start", "--standalone"]
    monkeypatch.setattr(fleet.sys, "platform", "darwin")
    assert fleet._detached() == ["setsid"]


def test_settled_reports_a_step_that_did_not_hold() -> None:
    acted = {
        "steps": [
            {"step": "start", "state": "done", "detail": "running (standalone)"},
            {"step": "register", "state": "done", "detail": "claude"},
            {"step": "skill", "state": "refused", "detail": "claude: linked parent"},
        ]
    }
    now = {
        "steps": [
            {"step": "start", "state": "change", "detail": "answerback start"},
            {"step": "register", "state": "ok", "detail": "unchanged: claude"},
            {"step": "skill", "state": "refused", "detail": "claude: linked parent"},
        ]
    }
    result = fleet._settled(acted, now)
    by = {s["step"]: s for s in result["steps"]}
    assert by["start"]["state"] == "fail" and "did not hold" in by["start"]["detail"]
    assert by["register"] == {"step": "register", "state": "done", "detail": "claude"}
    assert not result["ok"] and result["failed"] == 2


@pytest.fixture
def linux(bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch) -> FakeBridge:
    """A Linux machine with a user manager, whose bridge writes the unit with the quoted path."""
    which = bridge.which
    monkeypatch.setattr(
        fleet, "which", lambda n: "/bin/" + n if n in {"systemctl", "systemd-run"} else which(n)
    )
    monkeypatch.setattr(fleet.sys, "platform", "linux")
    monkeypatch.setattr(fleet.time, "sleep", lambda s: None)
    bridge.start_error = "Unit has a bad unit file setting."
    units = fleet._units()
    units.mkdir(parents=True)
    # Rules U1 and U2: the unit has the name and the command the bridge gives this configuration.
    (units / unit_name(bridge.config)).write_text(unit_text(bridge.config))
    return bridge


def test_a_failing_supervisor_is_replaced_by_a_repaired_persistent_unit(linux: FakeBridge) -> None:
    refused = fleet.ensure_here(write=True, want="0.14.0", run_=linux, handshake_=answers)
    assert states(refused)["start"] == "fail" and "--standalone" in refused["steps"][2]["detail"]
    result = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=linux, handshake_=answers
    )
    start = result["steps"][2]
    assert start["state"] == "done" and "repaired user unit" in start["detail"] and result["ok"]
    unit = (fleet._units() / fleet.FLEET_UNIT).read_text()
    folder = os.path.realpath(linux.config.parent)  # the corrected line ends in `/.` (rulings)
    assert f"WorkingDirectory={folder}/.\n" in unit and '"/py" "-m" "answerback_bridge"' in unit
    assert (
        unit.rstrip().endswith("[Install]\nWantedBy=default.target")
        and "Restart=on-failure" in unit
    )
    assert ["systemctl", "--user", "enable", "--now", fleet.FLEET_UNIT] in linux.calls
    assert not [c for c in linux.calls if "--standalone" in c]  # no unsupervised start was needed
    linux.calls.clear()
    again = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=linux, handshake_=answers
    )
    assert again["steps"][2] == {
        "step": "start",
        "state": "ok",
        "detail": "running and accepting, under the repaired user unit",
    }
    assert linux.wrote() == []


def test_a_running_but_unsupervised_bridge_is_reported_and_then_made_persistent(
    linux: FakeBridge,
) -> None:
    linux._configure()
    linux.running = True  # e.g. started by hand, or by the transient unit
    look = fleet.ensure_here(write=False, want="0.14.0", run_=linux, handshake_=answers)
    assert (
        look["steps"][2]["state"] == "change"
        and "nothing starts it again" in look["steps"][2]["detail"]
    )
    fixed = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=linux, handshake_=answers
    )
    assert ["/bin/answerback", "stop"] in linux.calls and "repaired user unit" in fixed["steps"][2][
        "detail"
    ]


def test_the_repaired_unit_gives_way_once_the_bridges_own_supervisor_works(
    linux: FakeBridge,
) -> None:
    fleet.ensure_here(write=True, want="0.14.0", standalone=True, run_=linux, handshake_=answers)
    assert (fleet._units() / fleet.FLEET_UNIT).exists()
    linux.start_error = None  # a release with the fix
    linux.version = "0.13.0"
    wheel = {"name": "w"}
    import unittest.mock

    with unittest.mock.patch.object(
        fleet, "install_bridge", lambda w, run_=None: setattr(linux, "version", "0.14.0")
    ):
        result = fleet.ensure_here(
            write=True, want="0.14.0", wheel=wheel, run_=linux, handshake_=answers
        )
    start = result["steps"][2]  # rule P3: the detail also names the unfinished tasks
    assert start["state"] == "done"
    assert start["detail"].startswith("running under the bridge's own supervisor; 3 unfinished")
    assert not (fleet._units() / fleet.FLEET_UNIT).exists()
    assert ["systemctl", "--user", "disable", "--now", fleet.FLEET_UNIT] in linux.calls


def test_supervised_cannot_be_told_without_systemd(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fleet.sys, "platform", "darwin")
    assert fleet.supervised(bridge) is None


def test_a_linked_skills_directory_gets_one_link_and_no_longer_blocks_the_others(
    bridge: FakeBridge,
) -> None:
    home = Path.home()
    (home / ".ai/skills").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude/skills").symlink_to(home / ".ai/skills")
    look = fleet.ensure_here(write=False, want="0.14.0", run_=bridge, handshake_=answers)
    assert states(look)["skill-link"] == "change"
    skill_calls = [c for c in bridge.calls if c[1:3] == ["skill", "install"]]
    assert skill_calls and all(
        "claude" not in c for c in skill_calls
    )  # the installer is not asked for it
    assert any("claude" in c for c in bridge.calls if c[1:3] == ["clients", "install"])
    shared = home / ".agents/skills" / fleet.USAGE_SKILL
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("usage")
    done = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    link = home / ".ai/skills" / fleet.USAGE_SKILL
    assert (
        states(done)["skill-link"] == "done"
        and link.is_symlink()
        and (link / "SKILL.md").read_text() == "usage"
    )
    assert (
        states(fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers))[
            "skill-link"
        ]
        == "ok"
    )
    link.unlink()
    link.mkdir()
    blocked = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert states(blocked)["skill-link"] == "refused" and link.is_dir()


# --------------------------------------------------------------------------- ensure: rules U and F
#
# Rulings on ENSURE-ATTACK-1. The unit texts are the form the bridge writes
# (src/answerback_bridge/supervision.py, systemd_definition); every command is a stand-in.


def quoted(word: str) -> str:
    """One word of ExecStart= as the bridge writes it (supervision.py, systemd_definition)."""
    return json.dumps(word).replace("%", "%%").replace("$", "$$")


def unit_name(config: Path, prefix: str = "local.answerback-bridge") -> str:
    """The bridge's unit for a configuration (supervision.py, NativeSupervisor.__init__)."""
    import hashlib

    return f"{prefix}.{hashlib.sha256(os.path.realpath(config).encode()).hexdigest()[:12]}.service"


def unit_text(config: Path, working: str | None = None) -> str:
    words = ["/py", "-m", "answerback_bridge", "serve", "--config", os.path.realpath(config)]
    folder = json.dumps(working or os.path.dirname(os.path.realpath(config))).replace("%", "%%")
    return (
        "[Unit]\nDescription=AnswerBack collaboration bridge\n\n[Service]\nType=simple\n"
        f"ExecStart={' '.join(quoted(w) for w in words)}\nWorkingDirectory={folder}\n"
        "Restart=on-failure\nRestartSec=10\n"
    )


def marker(config: Path) -> str:
    return (
        f"# answerback-fleet: repaired copy of {unit_name(config)} for {os.path.realpath(config)}"
    )


def passing(argv: list[str], timeout: float = 30, stdin: str | None = None):
    return subprocess.CompletedProcess(argv, 0, "", "")


def wheel_message(data: bytes = b"new wheel bytes", **changes: Any) -> dict[str, Any]:
    import base64
    import hashlib

    message: dict[str, Any] = {
        "name": "answerback-0.14.0-py3-none-any.whl",
        "release": "r1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "b64": base64.b64encode(data).decode("ascii"),
        "extras": ["bus"],
        "constraints": "anyio==4.16.0\n",
    }
    message.update(changes)
    return message


def _state_folder(home: Path, depth: int) -> Path:
    return home.joinpath(".local/state", *("answerback-bridge", "releases", "r1")[: depth + 1])


def _make(path: Path, kind: str, outside: Path) -> None:
    """Put one of the table's states at path. `outside` is a folder beside the home."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "regular file":
        path.write_bytes(b"what was there")
    elif kind == "link to a file outside":
        path.symlink_to(outside / "file")
    elif kind == "link to nowhere":
        path.symlink_to(outside / "nothing")
    elif kind == "folder":
        path.mkdir()
    elif kind == "second name":
        os.link(outside / "file", path)
    elif kind == "real folder":
        path.mkdir()
    elif kind == "link to a folder outside":
        path.symlink_to(outside / "folder", target_is_directory=True)
    elif kind == "file":
        path.write_bytes(b"a file where a folder belongs")
    else:
        assert kind == "not there"


PRIVATE_WRITES = [
    *[
        (f"destination: {kind}", None, kind, kind != "folder")
        for kind in (
            "not there",
            "regular file",
            "link to a file outside",
            "link to nowhere",
            "folder",
            "second name",
        )
    ],
    *[
        (f"folder {depth}: {kind}", depth, kind, kind in ("not there", "real folder"))
        for depth in range(3)
        for kind in ("not there", "real folder", "link to a folder outside", "file")
    ],
]


@pytest.mark.parametrize(
    ("depth", "kind", "written"),
    [row[1:] for row in PRIVATE_WRITES],
    ids=[r[0] for r in PRIVATE_WRITES],
)
def test_a_private_write_changes_no_file_outside(
    bridge: FakeBridge, tmp_path: Path, depth: int | None, kind: str, written: bool
) -> None:
    """The table of a private write (rules F1 and F1a). red_before: the rows with a link, a
    second name, a folder or a file; pins_existing: the others."""
    home = Path.home()
    outside = tmp_path / "outside"
    (outside / "folder").mkdir(parents=True)
    (outside / "file").write_bytes(b"outside bytes")
    release = _state_folder(home, 2)
    target = release / "answerback-0.14.0-py3-none-any.whl"
    if depth is None:
        _make(target, kind, outside)
        before = os.lstat(target) if kind != "not there" else None
    else:
        _make(_state_folder(home, depth), kind, outside)
    uv_calls: list[list[str]] = []

    def uv(argv: list[str], timeout: float = 30, stdin: str | None = None):
        uv_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    problem = fleet.install_bridge(wheel_message(), run_=uv)
    assert (outside / "file").read_bytes() == b"outside bytes"
    assert list((outside / "folder").iterdir()) == []
    if written:
        assert problem is None and len(uv_calls) == 1
        for path, data in (
            (target, b"new wheel bytes"),
            (release / "constraints.txt", b"anyio==4.16.0\n"),
        ):
            found = os.lstat(path)
            assert path.read_bytes() == data and not path.is_symlink()
            assert found.st_mode & 0o7777 == 0o600 and found.st_nlink == 1
        assert sorted(p.name for p in release.iterdir()) == [target.name, "constraints.txt"]
        for step in range(3):
            assert os.lstat(_state_folder(home, step)).st_mode & 0o170000 == 0o040000
    else:
        assert problem and uv_calls == []
        if depth is None:
            after = os.lstat(target)
            assert before is not None and (after.st_ino, after.st_mode) == (
                before.st_ino,
                before.st_mode,
            )
            assert list(target.iterdir()) == []
        else:
            state = _state_folder(home, depth)
            assert state.is_symlink() or state.is_file()
            assert not [p for p in (home / ".local/state").rglob("*.whl")]


def test_a_folder_this_tool_makes_is_private(bridge: FakeBridge) -> None:
    """Rule F1a: a missing folder is made with mode 0700. pins_existing."""
    assert fleet.install_bridge(wheel_message(), run_=passing) is None
    for depth in range(3):
        assert os.lstat(_state_folder(Path.home(), depth)).st_mode & 0o7777 == 0o700


WHEEL_MESSAGES = {
    "not a mapping": ["a list"],
    "no name": wheel_message(name=None),
    "a name with a folder": wheel_message(name="../x.whl"),
    "a name that is no wheel": wheel_message(name="answerback.tar.gz"),
    "a release with a folder": wheel_message(release="../../x"),
    "a release that is two dots": wheel_message(release=".."),
    "extras that are text": wheel_message(extras="bus"),
    "an extra that is not plain": wheel_message(extras=["bus]; rm"]),
    "constraints that are no text": wheel_message(constraints=None),
    "a hash that is a number": wheel_message(sha256=7),
    "base64 that does not decode": wheel_message(b64="!!not base64!!"),
    "base64 with other text in it": wheel_message(b64="bmV3IHdoZWVsIGJ5dGVz\n$$"),
    "another hash": wheel_message(sha256="0" * 64),
}


@pytest.mark.parametrize("name", sorted(WHEEL_MESSAGES))
def test_a_wheel_message_is_checked_before_anything_is_written(
    bridge: FakeBridge, name: str
) -> None:
    """Rule F2. red_before (upstream raised, or wrote and then called uv)."""
    calls: list[list[str]] = []
    problem = fleet.install_bridge(
        WHEEL_MESSAGES[name], run_=lambda argv, **kw: calls.append(argv) or passing(argv)
    )
    assert problem and problem.startswith("the coordinator's wheel message is not usable (")
    assert calls == [] and not (Path.home() / ".local").exists()


def test_ensure_reports_a_wheel_message_that_is_not_usable(bridge: FakeBridge) -> None:
    """Rule F2 through ensure_here: the step is `fail`. red_before."""
    bridge.version = "0.13.0"
    result = fleet.ensure_here(
        write=True, want="0.14.0", wheel={"name": "w"}, run_=bridge, handshake_=answers
    )
    assert result["steps"][0]["state"] == "fail"
    assert "the coordinator's wheel message is not usable" in result["steps"][0]["detail"]


@pytest.fixture
def repairable(linux: FakeBridge) -> Path:
    """The configuration of the `linux` machine, whose unit is in the user manager's folder."""
    return linux.config


def test_the_repaired_unit_is_a_marked_private_copy_for_this_configuration(
    linux: FakeBridge, repairable: Path, tmp_path: Path
) -> None:
    """Rules U1, U3, F1 and the corrected line. Counterpart of the attack's
    test_repair_temp_symlink_and_wrong_configuration. red_before."""
    units = fleet._units()
    other = units / "local.answerback-bridge.ffffffffffff.service"
    other.write_text(unit_text(tmp_path / "elsewhere" / "config.json"))
    os.utime(other, (4102444800, 4102444800))  # the newest: its change time decides nothing
    victim = tmp_path / "outside-units.txt"
    victim.write_text("preserve this")
    (units / fleet.FLEET_UNIT).with_suffix(".tmp").symlink_to(victim)
    assert fleet.repair_unit(repairable.parent, run_=linux) is None
    target = units / fleet.FLEET_UNIT
    lines = target.read_text().splitlines()
    assert victim.read_text() == "preserve this" and not target.is_symlink()
    assert os.lstat(target).st_mode & 0o7777 == 0o600
    assert lines[0] == marker(repairable)
    assert f"WorkingDirectory={os.path.realpath(repairable.parent)}/." in lines
    assert quoted(os.path.realpath(repairable)) in target.read_text()
    assert "elsewhere" not in target.read_text()
    assert lines[-2:] == ["[Install]", "WantedBy=default.target"]
    assert lines.count("[Install]") == 1


def test_a_unit_of_another_configuration_is_not_repaired(
    linux: FakeBridge, repairable: Path
) -> None:
    """Rule U2: this configuration's name, another configuration's command. red_before."""
    unit = fleet._units() / unit_name(repairable)
    unit.write_text(
        unit_text(repairable).replace(os.path.realpath(repairable), "/another/config.json")
    )
    linux.calls.clear()
    assert fleet.repair_unit(repairable.parent, run_=linux) == (
        "fail",
        f"the unit {unit} is not for this configuration",
    )
    assert not (fleet._units() / fleet.FLEET_UNIT).exists() and linux.calls == []


def test_the_config_word_must_follow_the_config_flag(linux: FakeBridge, repairable: Path) -> None:
    """Rule U2: the path as another word of the command is not `--config <path>`. red_before."""
    unit = fleet._units() / unit_name(repairable)
    unit.write_text(
        unit_text(repairable).replace('"--config" ', '"--config" "/another.json" "--also" ')
    )
    assert fleet.repair_unit(repairable.parent, run_=linux) == (
        "fail",
        f"the unit {unit} is not for this configuration",
    )


def test_the_runtime_folder_and_the_earlier_name_are_looked_at(
    linux: FakeBridge, repairable: Path
) -> None:
    """Rule U1: the unit's two names, in the user manager's folder and in the runtime folder.
    pins_existing for the runtime folder, red_before for the earlier name."""
    (fleet._units() / unit_name(repairable)).unlink()
    assert fleet.repair_unit(repairable.parent, run_=linux) == (
        "fail",
        "the bridge has written no unit for this configuration to repair",
    )
    runtime = repairable.parent / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / unit_name(repairable, "local.herdr-bridge")).write_text(unit_text(repairable))
    assert fleet.repair_unit(repairable.parent, run_=linux) is None
    first = (fleet._units() / fleet.FLEET_UNIT).read_text().splitlines()[0]
    assert first == marker(repairable).replace("local.answerback-bridge", "local.herdr-bridge")


@pytest.mark.parametrize("content", ["[Unit]\nDescription=someone else's\n", ""])
def test_an_unmarked_fleet_unit_is_never_touched(
    linux: FakeBridge, repairable: Path, content: str
) -> None:
    """Rule U3, verdict W4: the same bytes afterwards, and no command names it. red_before."""
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text(content)
    linux.calls.clear()
    result = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=linux, handshake_=answers
    )
    start = result["steps"][2]
    assert (
        f"{target} was not written by this tool for this configuration; nothing was changed"
        in (start["detail"])
    )
    assert target.read_text() == content
    changing = [c for c in linux.calls if c[0] == "systemctl" and fleet.FLEET_UNIT in c]
    assert [c[2] for c in changing if c[2] not in ("is-enabled", "is-active")] == []
    assert fleet.supervised(linux, repairable) is not "fleet"  # noqa: F632


def test_a_unit_marked_for_another_configuration_is_not_ours(
    linux: FakeBridge, repairable: Path
) -> None:
    """Rule U3: the first line names another configuration. red_before."""
    target = fleet._units() / fleet.FLEET_UNIT
    content = (
        marker(repairable).replace(os.path.realpath(repairable), "/another/config.json") + "\n"
    )
    target.write_text(content)
    assert fleet.repair_unit(repairable.parent, run_=linux) == (
        "refused",
        f"{target} was not written by this tool for this configuration; nothing was changed",
    )
    assert target.read_text() == content


SUPERVISED = [
    ("our unit, enabled and active", {"fleet_enabled": True, "running": True}, True, "fleet"),
    ("our unit, enabled and not active", {"fleet_enabled": True, "running": False}, True, False),
    ("a fleet unit that is not ours", {"fleet_enabled": True, "running": True}, False, "foreign"),
    ("a fleet unit that is not ours and holds nothing", {"running": True}, False, False),
    (
        "a fleet unit that is not ours beside the bridge's unit",
        {"fleet_enabled": True, "native_enabled": True, "running": True},
        False,
        "native",
    ),
    (
        "the bridge's unit, enabled and active",
        {"native_enabled": True, "running": True},
        True,
        "native",
    ),
    ("the bridge's unit, enabled and not active", {"native_enabled": True}, True, False),
    ("neither", {"running": True}, True, False),
    (
        "no user manager answers",
        {"manager": False, "fleet_enabled": True, "running": True},
        True,
        None,
    ),
]


@pytest.mark.parametrize(
    ("facts", "ours", "expected"), [row[1:] for row in SUPERVISED], ids=[r[0] for r in SUPERVISED]
)
def test_supervised_says_what_was_observed(
    linux: FakeBridge, repairable: Path, facts: dict[str, Any], ours: bool, expected: Any
) -> None:
    """Rule U4. Counterpart of the attack's test_no_systemd_manager_is_not_unknown. red_before."""
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text((marker(repairable) if ours else "[Unit]") + "\n")
    for name, value in facts.items():
        setattr(linux, name, value)
    assert fleet.supervised(linux, repairable) == expected
    assert type(fleet.supervised(linux, repairable)) is type(expected)


def test_another_units_state_is_not_this_bridges(linux: FakeBridge, repairable: Path) -> None:
    """Rule U1: another configuration's unit, enabled and active, says nothing. red_before."""
    other = "local.answerback-bridge.ffffffffffff.service"
    (fleet._units() / other).write_text(unit_text(repairable))
    linux.other_units = {other}
    linux.running = True
    assert fleet.supervised(linux, repairable) is False


WORKING_DIRECTORIES = [
    ("/home/me/.config/answerback-bridge", "/home/me/.config/answerback-bridge/."),
    ("/home/me/percent%dir", "/home/me/percent%%dir/."),
    ("/", "/."),
    ("/home/me/with space ", "/home/me/with space /."),
    ('/home/me/quote"d', '/home/me/quote"d/.'),
    ("relative/folder", None),
    ("/home/me/../you", None),
    ("/home/me/line\nfeed", None),
    ("/home/me/carriage\rreturn", None),
]


@pytest.mark.parametrize(("folder", "expected"), WORKING_DIRECTORIES)
def test_the_corrected_line_is_the_decoded_value_with_a_dot(
    linux: FakeBridge, repairable: Path, folder: str, expected: str | None
) -> None:
    """The rulings' third correction of the card (D4): nothing is doubled again. red_before."""
    unit = fleet._units() / unit_name(repairable)
    unit.write_text(unit_text(repairable, working=folder))
    problem = fleet.repair_unit(repairable.parent, run_=linux)
    target = fleet._units() / fleet.FLEET_UNIT
    if expected is None:
        assert problem is not None and problem[0] == "fail" and "cannot be repaired" in problem[1]
        assert not target.exists()
    else:
        assert problem is None and f"WorkingDirectory={expected}" in target.read_text().splitlines()


def test_a_quoted_line_that_cannot_be_decoded_is_not_repaired(
    linux: FakeBridge, repairable: Path
) -> None:
    """Card D4 (a). red_before."""
    unit = fleet._units() / unit_name(repairable)
    unit.write_text(
        unit_text(repairable).replace('answerback-bridge"\n', 'answerback-bridge\\q"\n')
    )
    unit.write_text(unit.read_text().replace('WorkingDirectory="', 'WorkingDirectory="\\x'))
    problem = fleet.repair_unit(repairable.parent, run_=linux)
    assert problem is not None and problem[0] == "fail" and "cannot be repaired" in problem[1]


@pytest.mark.parametrize(
    ("install", "count"),
    [
        ("", 1),
        ("\n[Install]\n", 1),
        ("\n[Install]\nWantedBy=default.target\n", 1),
        ("\n[Install]\nAlso=x.socket\n", 1),
    ],
)
def test_the_repaired_unit_has_one_install_section_with_the_target(
    linux: FakeBridge, repairable: Path, install: str, count: int
) -> None:
    """Rule U5. Counterpart of the attack's test_missing_systemd_install_section_is_not_repaired.
    red_before for the empty section and the section without the line."""
    unit = fleet._units() / unit_name(repairable)
    unit.write_text(unit_text(repairable) + install)
    assert fleet.repair_unit(repairable.parent, run_=linux) is None
    lines = (fleet._units() / fleet.FLEET_UNIT).read_text().splitlines()
    assert lines.count("[Install]") == count and lines.count("WantedBy=default.target") == 1
    assert lines.index("WantedBy=default.target") > lines.index("[Install]")
    if "Also" in install:
        assert "Also=x.socket" in lines


def test_a_reload_that_fails_is_the_repairs_failure(linux: FakeBridge, repairable: Path) -> None:
    """Rule S1 for the repair: the result of daemon-reload is read. red_before."""
    linux.failing = {"daemon-reload"}
    problem = fleet.repair_unit(repairable.parent, run_=linux)
    assert problem is not None and problem[0] == "fail" and "daemon-reload" in problem[1]
    assert ["systemctl", "--user", "enable", "--now", fleet.FLEET_UNIT] not in linux.calls


def test_a_unit_that_cannot_be_read_is_the_repairs_failure(
    linux: FakeBridge, repairable: Path
) -> None:
    """Card D4 (c): an OSError while a unit is read is a `fail` with its text. red_before."""
    unit = fleet._units() / unit_name(repairable)
    unit.unlink()
    unit.mkdir()
    problem = fleet.repair_unit(repairable.parent, run_=linux)
    assert problem is not None and problem[0] == "fail" and str(unit) in problem[1]


# --------------------------------------------------------------------------- ensure: rules S and P

BEFORE = ["not running", "nothing restarts it", "the bridge's unit", "our unit", "cannot be told"]
START_FAULTS = [
    "none",
    "stop fails, still runs",
    "stop fails, gone",
    "own start fails",
    "own start reports success, not running",
    "repair refused: another configuration's unit",
    "repair refused: an unmarked fleet unit",
    "repair refused: a line that cannot be repaired",
    "enable fails",
    "daemon-reload fails",
    "the restore fails",
    "the status cannot be read",
    "stop fails, the status cannot be read",
    "restart fails, the old bridge stays",
    "a later stop fails",
    "a program cannot be started after the stop",
    "only the first line of our unit is changed",
    "the configuration of a running bridge is gone",
    "a later stop fails, the bridge is down",
    "a later stop fails, the bridge is down, its own unit still enabled",
    "a later stop fails, the status cannot be read",
    "the first stop gives up while the bridge drains",
    "a later stop gives up while the bridge drains",
    "own start reports failure, the bridge is up",
]
QUIET = (
    "none",
    "the status cannot be read",
    "stop fails, the status cannot be read",
    "restart fails, the old bridge stays",
    "a later stop fails",
    "a program cannot be started after the stop",
    "only the first line of our unit is changed",
    "the configuration of a running bridge is gone",
    "a later stop fails, the bridge is down",
    "a later stop fails, the bridge is down, its own unit still enabled",
    "a later stop fails, the status cannot be read",
    "the first stop gives up while the bridge drains",
    "a later stop gives up while the bridge drains",
    "own start reports failure, the bridge is up",
)
START_TABLE = [
    (before, upgrade, standalone, fault)
    for before in BEFORE
    for upgrade in (False, True)
    for standalone in (False, True)
    for fault in START_FAULTS
]


def _arrange(linux: FakeBridge, before: str, fault: str) -> None:
    config = linux.config
    linux._configure()
    linux.registered = linux.skilled = True
    linux.start_error = None
    linux.running = before != "not running"
    linux.native_enabled = before == "the bridge's unit"
    linux.manager = before != "cannot be told"
    target = fleet._units() / fleet.FLEET_UNIT
    if before == "our unit":
        linux.fleet_enabled = True
        target.write_text(marker(config) + "\n" + unit_text(config))
    unit = fleet._units() / unit_name(config)
    if fault in ("stop fails, still runs", "stop fails, gone"):
        linux.stop_fails, linux.stop_gone = True, fault.endswith("gone")
    elif fault == "own start reports success, not running":
        linux.start_lies = True
    elif fault not in QUIET:
        linux.start_error = "Unit has a bad unit file setting."
    if fault == "stop fails, the status cannot be read":
        linux.stop_fails = linux.stop_blinds = True
    elif fault == "restart fails, the old bridge stays":
        linux.restart_error = "restart refused"
    elif fault.startswith("a later stop"):
        linux.start_outside = True
        linux.stops = 0 if before == "nothing restarts it" else 1
        linux.later_stop_fails = "fails" in fault
        linux.later_stop_gone = "is down" in fault
        linux.later_stop_keeps_unit = fault.endswith("still enabled")
        linux.later_stop_blinds = fault.endswith("cannot be read")
        linux.stop_drains = "later" if fault.endswith("drains") else ""
    elif fault == "the first stop gives up while the bridge drains":
        linux.stop_drains = "first"
    elif fault == "own start reports failure, the bridge is up":
        linux.start_fails_up = True
    elif fault == "a program cannot be started after the stop":
        linux.raises = {"start", "restart"}
    elif fault == "only the first line of our unit is changed":
        target.write_text("# changed by its owner\n" + unit_text(config))
    elif fault == "the configuration of a running bridge is gone":
        config.unlink()
    if fault == "repair refused: another configuration's unit":
        unit.write_text(unit_text(config).replace(os.path.realpath(config), "/another/config.json"))
    elif fault == "repair refused: an unmarked fleet unit":
        target.write_text("[Unit]\nDescription=someone else's\n")
    elif fault == "repair refused: a line that cannot be repaired":
        unit.write_text(unit_text(config, working="relative/folder"))
    elif fault == "enable fails":
        linux.failing = {"enable"}
    elif fault == "daemon-reload fails":
        linux.failing = {"daemon-reload"}
    elif fault == "the restore fails":
        linux.failing, linux.detached_fails = {"enable"}, True


@pytest.mark.parametrize(
    ("before", "upgrade", "standalone", "fault"),
    START_TABLE,
    ids=[f"{b} | upgrade={u} | standalone={s} | {f}" for b, u, s, f in START_TABLE],
)
def test_the_table_of_start(
    linux: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    before: str,
    upgrade: bool,
    standalone: bool,
    fault: str,
) -> None:
    """The rulings' table of `start` (rules S1 to S6, and A to C of the first review): 5 befores,
    with and without an upgrade and `--standalone`, 18 faults; the verdicts W1 to W5 are asserted
    for every row.

    What a verdict does not show. W1: only the words, and only where the stand-in's bridge is
    down at the end; not that a real bridge is down. W2: what the stand-in's flags say (enabled,
    active, running, the running version); not that systemd would restart anything. W3: no
    command and no file change in a row that was `ok`; not what a second machine does. W4: the
    file's bytes, no command that names our unit, and that the bridge runs as it ran; not who
    owns a unit on a real machine. W5: the calls of the start step after the failed stop; not
    what the bridge does by itself afterwards."""
    _arrange(linux, before, fault)
    target = fleet._units() / fleet.FLEET_UNIT
    not_ours = (
        "repair refused: an unmarked fleet unit",
        "only the first line of our unit is changed",
    )
    foreign = target.read_bytes() if fault in not_ours else None
    held = foreign is not None and before == "our unit"  # enabled or active: it may hold the bridge
    if upgrade:
        linux.version = "0.13.0"
        monkeypatch.setattr(
            fleet, "install_bridge", lambda w, run_=None: setattr(linux, "version", "0.14.0")
        )
    ran = linux.running
    wheel = wheel_message()
    look = fleet.ensure_here(write=False, want="0.14.0", run_=linux, handshake_=answers)
    assert linux.wrote() == [] and linux.running == ran
    files = {p.name: p.read_bytes() for p in fleet._units().iterdir()}
    linux.calls.clear()
    linux.blind.clear()
    if fault == "the status cannot be read":
        linux.status_good = 2 if before == "not running" else 3
    result = fleet.ensure_here(
        write=True,
        want="0.14.0",
        wheel=wheel,
        standalone=standalone,
        run_=linux,
        handshake_=answers,
    )
    start = result["steps"][2]
    assert start["step"] == "start"
    # the step's own commands: from its first question to the user manager (supervised) to the
    # probe of the next step. Not a slice that ends at the version read after the install.
    asks = ["systemctl", "--user", "show-environment"]
    first = linux.calls.index(asks) if asks in linux.calls else len(linux.calls)
    upto = next(
        (
            i
            for i, c in enumerate(linux.calls)
            if i > first and ("clients" in c or "--version" in c)
        ),
        len(linux.calls),
    )
    calls = linux.calls[first:upto]
    # the status gave nothing usable while the step ran (the reads after it are the next steps')
    unread = any(first <= at < upto for at in linux.blind)
    lifecycle = {"stop", "disable", "restart", "start", "enable", "daemon-reload"}
    # W1: a bridge that ran and does not run afterwards
    if ran and not linux.running:
        assert start["state"] == "fail" and not result["ok"]
        if unread:
            assert start["detail"].startswith("previously running, whether it runs cannot be told")
        else:
            assert start["detail"].startswith("previously running, now stopped")
    if "it still runs" in start["detail"] or "runs again as before" in start["detail"]:
        assert linux.running and not unread
    if "now stopped" in start["detail"]:
        assert not linux.running and not unread
    if ran and [c for c in calls if {"stop", "restart", "disable"} & set(c)]:
        # rules A4 and R2-6: failures too; a status without a task map gives no older count
        assert "unfinished task" in start["detail"] and " in its store" in start["detail"]
        if not unread:
            assert "unfinished task(s) in its store" in start["detail"]
    # W2: `done` only for what was observed
    if start["state"] == "done":
        assert linux.running and not unread
        if upgrade:
            assert linux.running_version == "0.14.0"
        ours = target.exists() and target.read_text().startswith(marker(linux.config))
        if "repaired user unit" in start["detail"]:
            assert linux.manager and linux.fleet_enabled and ours
        elif "bridge's own supervisor" in start["detail"].split(";")[0]:
            assert linux.manager and linux.native_enabled and not linux.fleet_enabled
        else:
            assert "nothing restarts it" in start["detail"] or "cannot be told" in start["detail"]
            assert not linux.manager or not (linux.native_enabled or linux.fleet_enabled)
    # W3: a row that is `ok` in the look
    if look["steps"][2]["state"] == "ok":
        words = {"stop", "disable", "restart", "start", "enable", "daemon-reload"}
        assert start["state"] == "ok" and [c for c in calls if words & set(c)] == []
        assert {p.name: p.read_bytes() for p in fleet._units().iterdir()} == files
    # W4: a unit file that is not ours: the file, and what happened to the service
    if foreign is not None:
        assert target.read_bytes() == foreign
        assert [c for c in linux.calls if fleet.FLEET_UNIT in c and lifecycle & set(c)] == []
    if held and start["state"] != "skip":
        assert start["state"] == "refused" and look["steps"][2]["state"] == "refused"
        assert "nothing was stopped or changed" in start["detail"]
        assert [c for c in calls if lifecycle & set(c)] == [] and linux.running == ran
        assert linux.instance == 1
    other = fleet._units() / unit_name(linux.config)
    assert other.read_bytes() == files[other.name]
    # W5: after a failed stop with the bridge still running
    stop = ["/bin/answerback", "stop"]
    if stop in calls and linux.stop_fails and not linux.stop_gone and not unread:
        after = calls[calls.index(stop) + 1 :]
        assert [c for c in after if c != ["/bin/answerback", "status"]] == []
        assert start["state"] == "fail" and start["detail"].startswith(
            "could not stop the running bridge (stop failed); it still runs as before"
        )
    if fault == "a later stop fails" and linux.stops > 1 and stop in calls:
        last = len(calls) - 1 - calls[::-1].index(stop)
        assert [c for c in calls[last + 1 :] if lifecycle & set(c)] == []
        assert start["state"] == "fail" and start["detail"].startswith(
            "the bridge runs outside its unit and could not be stopped (stop failed); "
            "nothing restarts it"
        )
    # and no row ends as an unfinished preview or a traceback
    assert start["state"] in ("ok", "done", "fail", "refused") or (
        linux.blind and start["state"] == "skip"
    )


def test_a_restore_is_named_with_the_error_of_the_new_way(linux: FakeBridge) -> None:
    """Rule S2: what was taken down runs again as before, and the detail says so. red_before."""
    _arrange(linux, "nothing restarts it", "own start fails")
    result = fleet.ensure_here(write=True, want="0.14.0", run_=linux, handshake_=answers)
    start = result["steps"][2]
    assert linux.running and start["state"] == "fail"
    assert start["detail"].startswith(
        "could not be put under a supervisor (Unit has a bad unit file setting.); "
        "it runs again as before, with nothing to restart it"
    )
    assert [c for c in linux.calls if "--standalone" in c]


def test_our_unit_is_enabled_again_when_the_bridges_own_fails(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule S2, third transition: `done` under our unit, with the error of the bridge's own.
    red_before."""
    _arrange(linux, "our unit", "own start fails")
    linux.version = "0.13.0"
    monkeypatch.setattr(
        fleet, "install_bridge", lambda w, run_=None: setattr(linux, "version", "0.14.0")
    )
    result = fleet.ensure_here(
        write=True, want="0.14.0", wheel=wheel_message(), run_=linux, handshake_=answers
    )
    start = result["steps"][2]
    assert start["state"] == "done" and linux.fleet_enabled and linux.running
    assert "repaired user unit" in start["detail"] and "bad unit file setting" in start["detail"]
    assert (fleet._units() / fleet.FLEET_UNIT).exists()


def test_a_refused_repair_is_not_hidden_by_a_detached_start(linux: FakeBridge) -> None:
    """Rule S5. Counterpart of the attack's test_repair_refusal_is_hidden_by_standalone_success.
    pins_existing: it passes at `2bbd5a8` already; the refusal holds since rule U3."""
    _arrange(linux, "not running", "repair refused: an unmarked fleet unit")
    result = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=linux, handshake_=answers
    )
    assert result["steps"][2]["state"] == "refused" and not result["ok"]
    assert not [c for c in linux.calls if "--standalone" in c or fleet.STANDALONE_UNIT in c]


@pytest.mark.parametrize(
    ("tasks", "words"),
    [
        ({"working": 1, "submitted": 2, "completed": 5, "cancelled": 1}, "; 3 unfinished task(s)"),
        ({}, "; 0 unfinished task(s)"),
        (None, "; an unknown number of unfinished tasks"),
        ({"working": "one"}, "; an unknown number of unfinished tasks"),
        ({"working": True}, "; an unknown number of unfinished tasks"),
        ([3], "; an unknown number of unfinished tasks"),
    ],
)
def test_the_look_says_what_a_stop_costs(linux: FakeBridge, tasks: Any, words: str) -> None:
    """Rule P2. red_before."""
    _arrange(linux, "nothing restarts it", "none")
    linux.tasks = tasks
    look = fleet.ensure_here(write=False, want="0.14.0", run_=linux, handshake_=answers)
    detail = look["steps"][2]["detail"]
    assert look["steps"][2]["state"] == "change" and "nothing starts it again" in detail
    assert detail.endswith(
        words + " in its store; work that the bridge owns may be interrupted and need reconciling"
    )


@pytest.mark.parametrize("before", ["the bridge's unit", "our unit", "cannot be told"])
def test_the_look_plans_the_restart_of_an_upgrade(linux: FakeBridge, before: str) -> None:
    """Rules P1, P2 and S7. Counterpart of the attack's test_upgrade_preview_hides_restart.
    red_before."""
    _arrange(linux, before, "none")
    linux.version = "0.13.0"
    look = fleet.ensure_here(write=False, want="0.14.0", run_=linux, handshake_=answers)
    install, start = look["steps"][0], look["steps"][2]
    assert install["state"] == "change" and install["detail"].endswith(
        "the installed program is replaced; ensure cannot put the old one back"
    )
    assert start["state"] == "change" and start["detail"].startswith(
        "restart after the upgrade; 3 "
    )
    assert linux.wrote() == []


def test_a_first_install_promises_no_rollback_text(bridge: FakeBridge) -> None:
    """Rule S7 is for an upgrade only. pins_existing."""
    bridge.installed = False
    look = fleet.ensure_here(write=False, want="0.14.0", run_=bridge, handshake_=answers)
    assert look["steps"][0]["detail"] == "install answerback 0.14.0"


def test_the_count_is_read_again_before_the_stop(linux: FakeBridge) -> None:
    """Rule P3: the result names the count as it was right before the stop. red_before."""
    _arrange(linux, "nothing restarts it", "none")
    seen = {"status": 0}
    inner = linux.__call__

    def later(argv: list[str], timeout: float = 30, stdin: str | None = None):
        if argv[-1] == "status":
            seen["status"] += 1
            if seen["status"] == 2:  # the first is the step's look, the second is before the stop
                linux.tasks = {"working": 7}
        return inner(argv, timeout=timeout, stdin=stdin)

    result = fleet.ensure_here(write=True, want="0.14.0", run_=later, handshake_=answers)
    start = result["steps"][2]
    assert start["state"] == "done" and "7 unfinished task(s)" in start["detail"]
    order = [c[-1] for c in linux.calls if c[-1] in ("status", "stop")]
    assert order[order.index("stop") - 1] == "status"


# ------------------------------------------------------------------- ensure: rules V, R and L


def test_done_needs_the_wanted_version_and_a_failed_install_skips_the_rest(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rules V1 and V2. Counterpart of the attack's
    test_successful_install_not_required_to_have_requested_version and
    test_failed_install_still_runs_old_installers. red_before."""
    bridge.version = "0.13.0"
    bridge.running = True
    bridge._configure()
    monkeypatch.setattr(
        fleet, "install_bridge", lambda w, run_=None: None
    )  # installs nothing newer
    result = fleet.ensure_here(
        write=True, want="0.14.0", wheel=wheel_message(), run_=bridge, handshake_=answers
    )
    assert result["steps"][0] == {
        "step": "install",
        "state": "fail",
        "detail": "installed, but answerback reports 0.13.0, not 0.14.0",
    }
    assert [s["state"] for s in result["steps"][1:]] == ["skip"] * 5
    assert {s["detail"] for s in result["steps"][1:]} == {"needs the bridge at 0.14.0"}
    assert bridge.wrote() == [] and not [c for c in bridge.calls if "install" in c]
    missing = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert [s["state"] for s in missing["steps"]] == ["fail"] + ["skip"] * 5


def node_result(**steps: str) -> dict[str, Any]:
    rows = [
        {"step": name, "state": state, "detail": f"{name} text"} for name, state in steps.items()
    ]
    good = all(state in ("ok", "done", "skip") for state in steps.values())
    return {"machine": "n1", "ok": good, "failed": 0, "steps": rows}


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A coordinator with one node `n1`. `script` holds what each call to the node answers, in
    order (an exception is raised); `sent` is every call that went to the node."""
    world: dict[str, Any] = {
        "script": [],
        "sent": [],
        "wheel": {**wheel_message(), "version": "0.14.0"},
    }

    def on_node(machine: str, args: list[str], *, payload=None, timeout=90, run_=None):
        world["sent"].append(
            {"machine": machine, "args": args, "payload": payload, "timeout": timeout}
        )
        answer = world["script"].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    listed = {"mode": "fleet", "machines": [{"name": "n1", "host": "h", "state": "connected"}]}
    here = {"installed": True, "version": "0.14.0", "path": "/bin/answerback"}
    monkeypatch.setattr(fleet, "machines", lambda run_=None: listed)
    monkeypatch.setattr(fleet, "on_node", on_node)
    monkeypatch.setattr(fleet, "bridge_state", lambda run_=None: here)
    monkeypatch.setattr(
        fleet, "capacity", lambda nodes, run_=None: {n: {"state": "ok"} for n in nodes}
    )
    monkeypatch.setattr(fleet, "coordinator_wheel", lambda run_=None: world["wheel"])
    return world


def node_args(yes: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        machine=["n1"], yes=yes, standalone=False, replace=False, any_capacity=False
    )


@pytest.mark.parametrize(
    ("wheel", "words"),
    [
        ({"version": "0.13.0"}, "the coordinator's wheel is 0.13.0, its program reports 0.14.0"),
        (None, "the coordinator has no release wheel to send"),
    ],
)
def test_a_wheel_that_is_not_the_running_program_is_not_sent(
    node: dict[str, Any], wheel: Any, words: str
) -> None:
    """Rule V3. red_before."""
    node["wheel"] = wheel and {**wheel_message(), **wheel}
    node["script"] = [node_result(install="change", configure="change", start="change")]
    result = fleet.ensure_fleet(node_args(), run_=passing)
    assert len(node["sent"]) == 1 and "--yes" not in node["sent"][0]["args"]
    steps = result["machines"]["n1"]["steps"]
    assert steps[0]["state"] == "fail" and words in steps[0]["detail"]
    assert [s["state"] for s in steps[1:]] == ["skip", "skip"] and not result["machines"]["n1"][
        "ok"
    ]


def test_the_wheel_that_is_sent_is_the_checked_message(node: dict[str, Any]) -> None:
    """Rule V3 and F2: the node gets exactly the fields install_bridge checks. pins_existing."""
    look = node_result(install="change")
    node["script"] = [look, node_result(install="done"), node_result(install="ok")]
    fleet.ensure_fleet(node_args(), run_=passing)
    assert json.loads(node["sent"][1]["payload"]) == wheel_message()


INSTALLER_ANSWERS = {
    "no JSON": (0, "Traceback"),
    "a list": (0, "[]"),
    "rows that are no list": (0, '{"clients": {"claude": "ok"}}'),
    "no rows": (0, '{"clients": []}'),
    "a row short": (0, '{"clients": [{"client": "claude", "status": "unchanged"}]}'),
    "a row that is no mapping": (0, '{"clients": ["claude", "kiro"]}'),
    "a status the installer cannot give": (
        0,
        '{"clients": [{"client": "claude", "status": "fine"}, '
        '{"client": "kiro", "status": "fine"}]}',
    ),
    "a failure without a refused row": (
        1,
        '{"clients": [{"client": "claude", "status": "blocked"}, '
        '{"client": "kiro", "status": "unchanged"}]}',
    ),
}


@pytest.mark.parametrize("name", sorted(INSTALLER_ANSWERS))
def test_an_installers_answer_counts_only_when_it_is_whole(bridge: FakeBridge, name: str) -> None:
    """Rule R1. Counterpart of the attack's test_installer_failure_rows_accepted_as_ok.
    red_before."""
    bridge._configure()
    bridge.running = True
    bridge.answer["clients"] = INSTALLER_ANSWERS[name]
    for write in (False, True):
        result = fleet.ensure_here(write=write, want="0.14.0", run_=bridge, handshake_=answers)
        register = result["steps"][3]
        assert register["step"] == "register" and register["state"] == "fail", register
        assert register["detail"].startswith("the installer's answer is not usable (")
        assert not result["ok"]
    assert not [c for c in bridge.calls if "clients" in c and "--apply" in c]


def test_kept_and_skipped_rows_are_named_and_each_skill_too(bridge: FakeBridge) -> None:
    """Rules R1 and R2. red_before."""
    bridge._configure()
    bridge.running = bridge.registered = bridge.skilled = True
    bridge.row_status = {"claude": "kept", "kiro": "skipped"}
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    skill = result["steps"][4]
    assert skill["state"] == "ok" and result["ok"]
    for words in (
        f"kept: {fleet.USAGE_SKILL} for claude, {fleet.SKILL_NAME} for claude",
        f"skipped: {fleet.USAGE_SKILL} for kiro, {fleet.SKILL_NAME} for kiro",
    ):
        assert words in skill["detail"]
    assert skill["detail"].startswith("kept: ")
    bridge.row_status = {}
    bridge.skilled = False
    look = fleet.ensure_here(
        write=False, want="0.14.0", replace=True, run_=bridge, handshake_=answers
    )
    detail = look["steps"][4]["detail"]
    assert look["steps"][4]["state"] == "change"
    assert detail.startswith("back up and replace what is there, and install: ")
    assert f"{fleet.SKILL_NAME} for claude" in detail and f"{fleet.USAGE_SKILL} for kiro" in detail


def test_a_skill_row_short_is_a_failure(bridge: FakeBridge) -> None:
    """Rule R1 for the skill installer: a row for every client and every skill it names.
    red_before."""
    bridge._configure()
    bridge.running = bridge.registered = True
    rows = [
        {"skill": skill, "client": client, "status": "unchanged", "would_change": False}
        for skill, client in (
            (fleet.USAGE_SKILL, "claude"),
            (fleet.USAGE_SKILL, "kiro"),
            (fleet.SKILL_NAME, "claude"),
        )
    ]
    sources = {fleet.USAGE_SKILL: "/pkg/a", fleet.SKILL_NAME: "/pkg/b"}
    bridge.answer["skill"] = (0, json.dumps({"targets": rows, "sources": sources}))
    result = fleet.ensure_here(write=False, want="0.14.0", run_=bridge, handshake_=answers)
    assert result["steps"][4]["state"] == "fail"
    assert f"no row for {fleet.SKILL_NAME} for kiro" in result["steps"][4]["detail"]


def test_a_configuration_without_the_core_provider_is_refused_and_not_edited(
    bridge: FakeBridge,
) -> None:
    """Rule R3. Counterpart of the attack's test_disabled_core_configuration_is_ok. red_before."""
    bridge.config.parent.mkdir(parents=True)
    bridge.config.write_text('{"hc_enabled": false}')
    bridge.running = True
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert result["steps"][1] == {
        "step": "configure",
        "state": "refused",
        "detail": f"{bridge.config} does not enable the core provider; "
        "ensure does not edit a configuration that is there",
    }
    assert result["steps"][2]["state"] == "skip" and not result["ok"]
    assert bridge.config.read_text() == '{"hc_enabled": false}'
    assert not [c for c in bridge.calls if "configure" in c or "stop" in c or "start" in c]


@pytest.mark.parametrize("content", ["[1]", "not json", None])
def test_a_configuration_that_cannot_be_read_is_a_failure(bridge: FakeBridge, content: Any) -> None:
    """Rule R3. red_before."""
    bridge.config.parent.mkdir(parents=True)
    if content is None:
        bridge.config.mkdir()
    else:
        bridge.config.write_text(content)
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert result["steps"][1]["state"] == "fail"
    assert "cannot be read as a configuration" in result["steps"][1]["detail"]
    assert not [c for c in bridge.calls if "configure" in c]


NODE_ANSWERS = [
    ["a list"],
    {"machine": "n1", "ok": True},
    {"machine": 7, "ok": True, "steps": []},
    {"machine": "n1", "ok": "yes", "steps": []},
    {"machine": "n1", "ok": False, "steps": "none"},
    {"machine": "n1", "ok": False, "steps": [["install", "ok"]]},
    {"machine": "n1", "ok": False, "steps": [{"step": "install", "state": "ok"}]},
    {"machine": "n1", "ok": False, "steps": [{"step": "install", "state": "fine", "detail": ""}]},
]


@pytest.mark.parametrize("answer", NODE_ANSWERS)
def test_a_nodes_answer_is_checked_before_it_is_used(node: dict[str, Any], answer: Any) -> None:
    """Rule R4. Counterpart of the attack's test_bad_remote_shape_escapes_no_traceback_rule.
    red_before."""
    with pytest.raises(fleet.FleetError, match="n1: its answer is not a result of ensure"):
        fleet._node_answer("n1", answer)
    node["script"] = [answer]
    result = fleet.ensure_fleet(node_args(), run_=passing)  # no TypeError, KeyError, AttributeError
    assert len(node["sent"]) == 1 and "n1" not in result["machines"]
    assert "not a result of ensure" in result["left_out"][0]["reason"]


@pytest.mark.parametrize("result", ["[1]", "{}", '{"serverInfo": "text"}'])
def test_handshake_checks_the_shape_of_the_answer(tmp_path: Path, result: str) -> None:
    """Rule R4: `result` and `serverInfo` are mappings. red_before."""
    server = tmp_path / "server.py"
    server.write_text(
        "import json,sys\nreq=json.loads(sys.stdin.readline())\n"
        f"print(json.dumps({{'jsonrpc':'2.0','id':req['id'],'result':json.loads({result!r})}}))\n"
        "print(json.dumps({'jsonrpc':'2.0','id':1,'error':'text'}))\n"
    )
    assert fleet.handshake(["python3", str(server)]) == (
        False,
        "the answer to initialize has no result with serverInfo",
    )


@pytest.fixture
def linked(bridge: FakeBridge) -> Path:
    """A machine whose ~/.claude/skills is a link to another shared folder; the usage skill is in
    ~/.agents/skills. Returns where the one link belongs."""
    home = Path.home()
    (home / ".ai/skills").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude/skills").symlink_to(home / ".ai/skills")
    shared = home / ".agents/skills" / fleet.USAGE_SKILL
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("usage")
    bridge._configure()
    bridge.running = True
    return home / ".ai/skills" / fleet.USAGE_SKILL


def test_a_link_is_equal_when_the_file_system_says_it_is_the_same_folder(
    bridge: FakeBridge, linked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule L1. Counterpart of the attack's test_d3_case_insensitive_identity: the resolved texts
    differ and the folder is the same. red_before."""
    shared = Path.home() / ".agents/skills" / fleet.USAGE_SKILL
    linked.symlink_to(shared)
    real = Path.resolve

    def other_text(self: Path, strict: bool = False) -> Path:
        found = real(self, strict)
        return Path(str(found).upper()) if ".claude" in self.parts else found

    monkeypatch.setattr(Path, "resolve", other_text)
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert states(result)["skill-link"] == "ok" and linked.is_symlink()


def test_a_destination_the_file_system_does_not_answer_for_is_refused(
    bridge: FakeBridge, linked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule L2. red_before."""
    real = fleet._is_there
    monkeypatch.setattr(
        fleet,
        "_is_there",
        lambda path: "Permission denied" if path.name == linked.name else real(path),
    )
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    link = result["steps"][5]
    assert link["step"] == "skill-link" and link["state"] == "refused"
    assert "does not say" in link["detail"] and "Permission denied" in link["detail"]
    assert not os.path.lexists(linked)


def test_a_link_to_nowhere_is_something_else(bridge: FakeBridge, linked: Path) -> None:
    """Rule L1 (sequence d iv). pins_existing."""
    linked.symlink_to(Path.home() / "nowhere")
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    assert states(result)["skill-link"] == "refused" and os.readlink(linked).endswith("nowhere")


def test_one_refusal_and_no_link_is_made_for_any(bridge: FakeBridge, linked: Path) -> None:
    """Rule L3: every linked harness is looked at before the first link. red_before."""
    home = Path.home()
    claude = next(h for h in fleet.HARNESSES if h.id == "claude")
    other = dataclasses.replace(claude, id="other", user_dir=".other/skills")
    (home / ".other/skills").mkdir(parents=True)
    (home / ".other/skills" / fleet.USAGE_SKILL).mkdir()
    state, detail = fleet._link_usage_skill([claude, other], write=True)
    assert state == "refused" and "something else" in detail
    assert not os.path.lexists(linked)


def test_a_new_link_that_does_not_open_as_the_skill_fails_the_step_and_is_left(
    bridge: FakeBridge, linked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule R2-4 (was L1): the step fails, and the name is not removed. red_before."""
    real = os.symlink
    usage = Path.home() / ".agents/skills" / fleet.USAGE_SKILL / "SKILL.md"

    def emptied(target: str, dest: Path) -> None:  # the link is made, the skill behind it is gone
        real(target, dest)
        usage.unlink()

    monkeypatch.setattr(fleet.os, "symlink", emptied)
    result = fleet.ensure_here(write=True, want="0.14.0", run_=bridge, handshake_=answers)
    link = result["steps"][5]
    assert link["state"] == "fail" and (
        f"{fleet.USAGE_SKILL} is linked and does not open as the skill (" in link["detail"]
    )
    assert link["detail"].endswith("; the link was left in place") and linked.is_symlink()


@pytest.mark.parametrize("write", [False, True])
def test_a_machine_with_only_linked_harnesses_cannot_get_the_usage_skill(
    bridge: FakeBridge, linked: Path, monkeypatch: pytest.MonkeyPatch, write: bool
) -> None:
    """Rule L4. Counterpart of the attack's test_only_linked_harness_cannot_bootstrap_usage_skill.
    red_before."""
    shared = Path.home() / ".agents/skills" / fleet.USAGE_SKILL
    shutil.rmtree(shared)
    monkeypatch.setattr(fleet, "which", lambda n: None if n == "kiro-cli" else bridge.which(n))
    result = fleet.ensure_here(write=write, want="0.14.0", run_=bridge, handshake_=answers)
    by = {s["step"]: s for s in result["steps"]}
    assert by["skill"]["state"] == "skip"
    assert by["skill-link"] == {
        "step": "skill-link",
        "state": "fail",
        "detail": f"{shared} is not there, and the bridge's installer has no harness to install it "
        "for here",
    }
    assert not os.path.lexists(linked)


# ------------------------------------------------------------------------------ ensure: rule N

LOOK = {"install": "ok", "configure": "ok", "start": "change", "handshake": "skip"}
ACTED = {"install": "ok", "configure": "ok", "start": "done", "handshake": "ok"}
AFTER = {"install": "ok", "configure": "ok", "start": "ok", "handshake": "ok"}
LOST = fleet.FleetError("n1: timed out after 600s")
NOT_A_RESULT = {"machine": "n1", "ok": True, "steps": "all fine"}
NODE_PHASES = {
    # name: (what the node answers in order, calls sent, the steps' names and states)
    "the look fails": ([LOST], 1, None),
    "the look answers a non-result": ([NOT_A_RESULT], 1, None),
    "the acting call times out": (
        [node_result(**LOOK), LOST, node_result(**AFTER)],
        3,
        [
            ("act", "fail"),
            ("install", "ok"),
            ("configure", "ok"),
            ("start", "ok"),
            ("handshake", "ok"),
        ],
    ),
    "the acting call answers a non-result": (
        [node_result(**LOOK), NOT_A_RESULT, node_result(**LOOK)],
        3,
        [
            ("act", "fail"),
            ("install", "ok"),
            ("configure", "ok"),
            ("start", "change"),
            ("handshake", "skip"),
        ],
    ),
    "the second look fails": (
        [node_result(**LOOK), node_result(**ACTED), LOST],
        3,
        [
            ("install", "ok"),
            ("configure", "ok"),
            ("start", "done"),
            ("handshake", "ok"),
            ("look", "fail"),
        ],
    ),
    "the second look answers a non-result": (
        [node_result(**LOOK), node_result(**ACTED), NOT_A_RESULT],
        3,
        [
            ("install", "ok"),
            ("configure", "ok"),
            ("start", "done"),
            ("handshake", "ok"),
            ("look", "fail"),
        ],
    ),
    "neither comes back": (
        [node_result(**LOOK), LOST, LOST],
        3,
        [("act", "fail"), ("look", "fail")],
    ),
    "a done step is missing from the second look": (
        [node_result(**LOOK), node_result(**ACTED), node_result(install="ok", configure="ok")],
        3,
        [("install", "ok"), ("configure", "ok"), ("start", "fail")],
    ),
    "all holds": (
        [node_result(**LOOK), node_result(**ACTED), node_result(**AFTER)],
        3,
        [("install", "ok"), ("configure", "ok"), ("start", "done"), ("handshake", "ok")],
    ),
}


@pytest.mark.parametrize("phase", sorted(NODE_PHASES))
def test_the_table_of_node_phases(node: dict[str, Any], phase: str) -> None:
    """Rules N1 to N4. A machine is under `machines` once the acting call was sent, with the line
    `act` when that call did not come back; it is under `left_out` only before. Counterpart of the
    attack's test_remote_timeout_becomes_left_out_without_mutation_state."""
    script, calls, steps = NODE_PHASES[phase]
    node["script"] = [json.loads(json.dumps(a)) if isinstance(a, dict) else a for a in script]
    result = fleet.ensure_fleet(node_args(), run_=passing)
    acting = [call for call in node["sent"] if "--yes" in call["args"]]
    assert len(node["sent"]) == calls and len(acting) <= 1  # N2: never sent twice
    assert [call["timeout"] for call in acting] == [600] * len(acting)  # N4
    left = [row["machine"] for row in result["left_out"]]
    assert ("n1" in result["machines"]) == bool(acting) and ("n1" in left) == (not acting)  # N1
    if steps is None:
        return
    machine = result["machines"]["n1"]
    assert [(s["step"], s["state"]) for s in machine["steps"]] == steps
    assert machine["ok"] == (phase == "all holds")
    assert machine["failed"] == len([s for s in machine["steps"] if s["state"] == "fail"])
    by = {s["step"]: s["detail"] for s in machine["steps"]}
    if steps[0][0] == "act":
        assert by["act"].startswith("the acting call did not come back (")
        assert by["act"].endswith("); this machine may be partly changed; nothing was sent again")
    if phase.startswith("a done step"):
        assert by["start"] == "no longer reported after the call ended"  # N3


def test_a_look_never_sends_the_acting_call(node: dict[str, Any]) -> None:
    """Rule N1: without --yes one call per node. pins_existing."""
    node["script"] = [node_result(**LOOK)]
    result = fleet.ensure_fleet(node_args(yes=False), run_=passing)
    assert len(node["sent"]) == 1 and result["machines"]["n1"]["steps"][2]["state"] == "change"


def test_fifteen_machines_are_looked_at_and_the_rest_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule N5. Counterpart of the attack's test_sixteenth_remote_node_is_silently_omitted.
    red_before."""
    monkeypatch.setattr(fleet, "which", lambda name: f"/bin/{name}")
    listed = [{"name": f"m{i:02}", "host": "h", "state": "connected"} for i in range(18)]
    found = fleet.machines(fake_run({"machine ls": (0, json.dumps({"machines": listed}))}))
    assert len(found["machines"]) == 15 and found["beyond"] == ["m15", "m16", "m17"]
    reason = "more than 15 machines are registered; this one was not looked at"
    local, nodes, left = fleet.select(found, ["all"])
    assert local and len(nodes) == 15
    assert left == [{"machine": name, "reason": reason} for name in ("m15", "m16", "m17")]
    assert fleet.select(found, ["m16", "m01"]) == (
        False,
        ["m01"],
        [{"machine": "m16", "reason": reason}],
    )
    monkeypatch.setattr(fleet, "machines", lambda run_=None: found)
    monkeypatch.setattr(fleet, "probe", lambda run_=None: {})
    monkeypatch.setattr(fleet, "on_node", lambda name, args, **kw: {})
    monkeypatch.setattr(fleet, "capacity", lambda nodes, run_=None: {})
    monkeypatch.setattr(fleet, "drift", lambda rows: [])
    survey = fleet.survey(run_=passing)
    assert [r for r in survey["left_out"] if r["reason"] == reason] == left


# ------------------------------------------- tests that the single edits showed to be missing


def test_a_bridge_that_ran_and_is_gone_is_named_so(linux: FakeBridge) -> None:
    """Rule S3: the words `previously running, now stopped`, with the errors. The edit
    `s-stopped-as-done` survived without this test. pins_existing."""
    _arrange(linux, "nothing restarts it", "the restore fails")
    result = fleet.ensure_here(write=True, want="0.14.0", run_=linux, handshake_=answers)
    start = result["steps"][2]
    assert not linux.running and start["state"] == "fail"
    assert start["detail"].startswith(
        "previously running, now stopped (Unit has a bad unit file setting.; the detached start: "
    )


def test_done_is_what_the_status_says_after_the_start(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule S1: the status is read again after the step; `done` from the routine alone is not
    enough. The edit `s-result-not-read` survived without this test. pins_existing."""
    _arrange(linux, "not running", "none")
    monkeypatch.setattr(fleet, "_start", lambda *a, **k: ("done", "said to be up"))
    result = fleet.ensure_here(write=True, want="0.14.0", run_=linux, handshake_=answers)
    assert result["steps"][2] == {
        "step": "start",
        "state": "fail",
        "detail": "did not stay up (said to be up)",
    }


def test_a_private_write_has_no_name_that_can_be_guessed(tmp_path: Path) -> None:
    """Rule F1: a link that waits under a fixed temporary name is not written through. The edit
    `f-fixed-temp-name` survived without this test. pins_existing."""
    victim, dest = tmp_path / "victim", tmp_path / "folder" / "unit.service"
    victim.write_text("theirs")
    dest.parent.mkdir()
    for name in (dest.name + ".tmp", "." + dest.name + ".tmp", dest.name + "~"):
        (dest.parent / name).symlink_to(victim)
    fleet._private_write(dest, b"ours")
    assert dest.read_bytes() == b"ours" and not dest.is_symlink()
    assert victim.read_text() == "theirs"
    assert sorted(p.name for p in dest.parent.iterdir() if p.is_symlink()) == sorted(
        [dest.name + ".tmp", "." + dest.name + ".tmp", dest.name + "~"]
    )
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- the first review


def _upgrade(linux: FakeBridge, monkeypatch: pytest.MonkeyPatch) -> None:
    linux.version = "0.13.0"
    monkeypatch.setattr(
        fleet, "install_bridge", lambda w, run_=None: setattr(linux, "version", "0.14.0")
    )


def _ensure(linux: FakeBridge, **more: Any) -> dict[str, str]:
    result = fleet.ensure_here(
        write=True, want="0.14.0", wheel=wheel_message(), run_=linux, handshake_=answers, **more
    )
    assert result["steps"][2]["step"] == "start"
    return result["steps"][2]


COST = "; 3 unfinished task(s) in its store"


def test_a_restart_that_fails_is_a_failure_while_the_old_bridge_runs(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule B1 (E1-02): the result of the restart is read; no further command. red_before."""
    _arrange(linux, "the bridge's unit", "restart fails, the old bridge stays")
    _upgrade(linux, monkeypatch)
    start = _ensure(linux)
    assert start["state"] == "fail" and linux.running and linux.instance == 1
    assert start["detail"].startswith(
        "answerback restart failed (restart refused); "
        "the bridge that ran before still runs, at 0.13.0" + COST
    )
    assert ["/bin/answerback", "start"] not in linux.calls


def test_a_restart_that_changes_nothing_is_a_failure(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule B2: the instance that answers after a restart is not the one from before. red_before."""
    _arrange(linux, "cannot be told", "none")
    linux.restart_noop = True
    _upgrade(linux, monkeypatch)
    start = _ensure(linux)
    assert start["state"] == "fail" and start["detail"].startswith(
        "the restart changed nothing: the bridge that runs is the one from before" + COST
    )


@pytest.mark.parametrize(
    ("says", "state", "words"),
    [
        (True, "fail", "the bridge that runs reports 0.13.0, not 0.14.0: it was not replaced"),
        (False, "done", "the running bridge does not say its version"),
    ],
)
def test_done_after_an_upgrade_needs_the_running_bridge_at_the_version(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch, says: bool, state: str, words: str
) -> None:
    """Rule B3: the version is the running bridge's, not the program's. red_before."""
    _arrange(linux, "the bridge's unit", "none")
    _upgrade(linux, monkeypatch)
    linux.restart_old, linux.says_version = True, says
    start = _ensure(linux)
    assert start["state"] == state and words in start["detail"]


def test_a_stop_that_fails_with_no_status_claims_nothing(linux: FakeBridge) -> None:
    """Rule A3 (E1-01, O-4): one attempt to bring it back, and what was then observed.
    red_before."""
    _arrange(linux, "nothing restarts it", "stop fails, the status cannot be read")
    start = _ensure(linux)
    assert start["state"] == "fail" and "it still runs" not in start["detail"]
    assert start["detail"].startswith(
        "previously running, whether it runs cannot be told (answerback status gave no answer that "
        "can be read); could not stop the running bridge (stop failed); a detached start was tried"
    )
    assert len([c for c in linux.calls if "--standalone" in c]) == 1


def test_a_later_stop_that_fails_ends_the_step(linux: FakeBridge) -> None:
    """Rule B4 (E1-02): no repair, no reload, no enable after it. red_before."""
    _arrange(linux, "nothing restarts it", "a later stop fails")
    start = _ensure(linux, standalone=True)
    assert linux.stops == 2 and start["state"] == "fail"
    assert start["detail"].startswith(
        "the bridge runs outside its unit and could not be stopped (stop failed); "
        "nothing restarts it"
    )
    assert not [c for c in linux.calls if "enable" in c or "daemon-reload" in c]


def test_a_bridge_left_up_outside_its_unit_is_stopped_before_the_repair(linux: FakeBridge) -> None:
    """Rule C3 (O-3): not running, `--standalone`, a user manager answers. red_before."""
    _arrange(linux, "not running", "none")
    linux.start_outside = True
    start = _ensure(linux, standalone=True)
    stop = linux.calls.index(["/bin/answerback", "stop"])
    enable = linux.calls.index(["systemctl", "--user", "enable", "--now", fleet.FLEET_UNIT])
    assert stop < enable and start["state"] == "done" and linux.instance == 3
    assert start["detail"].startswith("running under the repaired user unit")


@pytest.mark.parametrize("fails", [False, True])
def test_our_unit_that_was_enabled_is_enabled_again_and_the_result_is_read(
    linux: FakeBridge, fails: bool
) -> None:
    """Rules C2 (O-1) and B5 (O-2): not running, our unit enabled, the bridge's own start fails.
    red_before."""
    _arrange(linux, "our unit", "own start fails")
    linux.running = False
    real = linux.__call__

    def run(argv: list[str], **kw: Any):
        if fails and argv[2:4] == ["enable", "--now"]:
            linux.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "Failed to enable: stand-in")
        return real(argv, **kw)

    result = fleet.ensure_here(write=True, want="0.14.0", run_=run, handshake_=answers)
    start = result["steps"][2]
    bad = "Unit has a bad unit file setting."
    if fails:
        assert start["state"] == "fail" and not linux.running
        assert (
            f"{fleet.FLEET_UNIT} was enabled before and could not be enabled again "
            "(Failed to enable: stand-in)"
        ) in start["detail"]
    else:
        assert start == {
            "step": "start",
            "state": "done",
            "detail": f"running under the repaired user unit; "
            f"the bridge's own supervisor failed ({bad})",
        }


def test_a_bridge_that_lost_its_own_unit_in_the_restart_is_not_done(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule C1 (O-6). red_before."""
    _arrange(linux, "the bridge's unit", "none")
    _upgrade(linux, monkeypatch)
    linux.restart_drops_unit = True
    start = _ensure(linux)
    assert linux.running and start["state"] == "fail"
    assert start["detail"].startswith(
        "running, but its own unit is no longer enabled and active: nothing restarts it ("
    )


def test_a_disable_that_fails_and_took_the_bridge_down_is_followed_by_one_enable(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule C4 (O-5). red_before."""
    _arrange(linux, "our unit", "none")
    _upgrade(linux, monkeypatch)
    linux.failing, linux.disable_kills = {"disable"}, True
    start = _ensure(linux)
    enables = [c for c in linux.calls if c[2:4] == ["enable", "--now"]]
    assert len(enables) == 1 and linux.running and start["state"] == "fail"
    assert start["detail"].startswith(
        f"could not disable {fleet.FLEET_UNIT} (Failed to disable: stand-in); it was enabled again "
        "and the bridge runs under it" + COST
    )
    assert ["/bin/answerback", "start"] not in linux.calls


def test_a_unit_of_ours_that_cannot_be_taken_away_is_named(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule B6: the step stays `done`, and the detail says what failed. red_before."""
    _arrange(linux, "our unit", "none")
    _upgrade(linux, monkeypatch)
    linux.failing = {"daemon-reload"}
    start = _ensure(linux)
    assert start["state"] == "done" and linux.native_enabled
    assert "daemon-reload failed after it (Failed to daemon-reload: stand-in)" in start["detail"]


def test_a_program_that_cannot_be_started_after_the_stop_is_a_result(linux: FakeBridge) -> None:
    """Rule D1 (E1-03): an OSError is a failed command; the way back is tried. red_before."""
    _arrange(linux, "nothing restarts it", "a program cannot be started after the stop")
    start = _ensure(linux)
    assert start["state"] == "fail" and not linux.running
    assert start["detail"].startswith("previously running, now stopped (")
    assert "stand-in: start cannot be started" in start["detail"] and COST in start["detail"]


def test_an_acting_call_that_cannot_be_started_keeps_its_lines(node: dict[str, Any]) -> None:
    """Rule D2 (E1-03): OSError at the acting call and at the look after it. red_before."""
    node["script"] = [node_result(**LOOK), OSError("output pipe"), OSError("no route")]
    result = fleet.ensure_fleet(node_args(), run_=passing)
    steps = result["machines"]["n1"]["steps"]
    assert [(s["step"], s["state"]) for s in steps] == [("act", "fail"), ("look", "fail")]
    assert "output pipe" in steps[0]["detail"] and "no route" in steps[1]["detail"]
    assert sum("--yes" in c["args"] for c in node["sent"]) == 1


@pytest.mark.parametrize("text", ["[]", "7", '"up"', "null"])
def test_a_status_that_is_not_a_mapping_cannot_be_read(bridge: FakeBridge, text: str) -> None:
    """Rule D3 (E1-03): no AttributeError, also in the inherited bridge_state. red_before."""

    def run(argv: list[str], **kw: Any):
        if argv == ["/bin/answerback", "status"]:
            return subprocess.CompletedProcess(argv, 0, text, "")
        return bridge(argv, **kw)

    assert fleet.bridge_state(run)["state"] == "unknown"
    result = fleet.ensure_here(write=False, run_=run)
    assert states(result)["configure"] == "fail"


@pytest.mark.parametrize(
    ("status", "seen"),
    [
        ({"running": True, "accepting": True}, ("up", "")),
        ({"running": False}, ("down", "")),
        ({"running": True, "accepting": False}, ("unknown", "it runs but does not accept tasks")),
        ({}, ("unknown", "answerback status does not say whether it runs")),
        ([], ("unknown", "answerback status did not answer with a mapping")),
        (None, ("unknown", "answerback status gave no answer that can be read")),
    ],
)
def test_what_is_observed_has_three_answers(status: Any, seen: tuple[str, str]) -> None:
    """Rule A1. red_before (the routine is new)."""
    text = "nothing" if status is None else json.dumps(status)
    state, _, reason = fleet._observe(
        "/bin/answerback", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, text, "")
    )
    assert (state, reason) == seen


def test_a_unit_of_our_name_for_another_configuration_is_nothing_to_a_bridge_under_its_own_unit(
    linux: FakeBridge,
) -> None:
    """Rule H1, the order of the answers: `native` comes before `foreign`. pins_existing."""
    _arrange(linux, "the bridge's unit", "none")
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text("# answerback-fleet: repaired copy of x.service for /other/config.json\n")
    linux.fleet_enabled = True  # the other configuration's repaired unit is enabled and active
    assert fleet.supervised(linux, linux.config) == "native"
    look = fleet.ensure_here(write=False, want="0.14.0", run_=linux, handshake_=answers)
    assert look["steps"][2]["state"] == "ok"
    linux.native_enabled = False
    assert fleet.supervised(linux, linux.config) == "foreign"


@pytest.mark.parametrize(
    "more",
    [["--config", "/other/config.json"], ["--config=/other/config.json"], ["--config-dir", "/x"]],
    ids=["a second --config", "a word --config=", "a word that begins with --config"],
)
def test_the_command_of_the_unit_to_repair_names_this_configuration_once(
    linux: FakeBridge, more: list[str]
) -> None:
    """Rule H2: exactly one `--config`, this configuration after it, no other word that begins
    with `--config`. red_before."""
    _arrange(linux, "nothing restarts it", "none")
    words = [
        "/py",
        "-m",
        "answerback_bridge",
        "--config",
        os.path.realpath(linux.config),
        *more,
        "serve",
    ]
    source = fleet._units() / unit_name(linux.config)
    source.write_text(
        unit_text(linux.config).replace(
            "ExecStart=" + unit_text(linux.config).split("ExecStart=")[1].split("\n")[0],
            "ExecStart=" + " ".join(quoted(w) for w in words),
        )
    )
    result = fleet.repair_unit(linux.config.parent, run_=linux)
    assert result == ("fail", f"the unit {source} is not for this configuration")
    assert not (fleet._units() / fleet.FLEET_UNIT).exists()


def test_the_command_of_the_unit_to_repair_is_decoded_word_by_word(linux: FakeBridge) -> None:
    """Rule H2: a path with `%`, `$` and a space is this configuration. pins_existing."""
    _arrange(linux, "nothing restarts it", "none")
    assert fleet._words('"/a b" "50%%" "$$x" "--config"') == ["/a b", "50%", "$x", "--config"]
    assert fleet._words('"/a" bare') is None and fleet._words('"/a" 3') is None


def _all_set(bridge: FakeBridge) -> None:
    bridge._configure()
    bridge.running = bridge.registered = bridge.skilled = True


def _look(bridge: FakeBridge, **more: Any) -> dict[str, Any]:
    return fleet.ensure_here(write=False, want="0.14.0", run_=bridge, handshake_=answers, **more)


@pytest.mark.parametrize(
    ("status", "flag", "said"),
    [
        ("planned", None, "has no change flag"),
        ("planned", False, "says planned with would_change false"),
        ("unchanged", True, "says unchanged with would_change true"),
        ("kept", True, "says kept with would_change true"),
        ("unchanged", "no", "has no change flag"),
    ],
)
def test_a_row_whose_change_flag_is_missing_or_does_not_fit_is_not_ok(
    bridge: FakeBridge, status: str, flag: Any, said: str
) -> None:
    """Rule G1: the installer's table of statuses is read; `ok` never comes from rows that plan a
    change or that are not whole. red_before."""
    _all_set(bridge)
    rows = [{"client": c, "status": "unchanged", "would_change": False} for c in ("claude", "kiro")]
    rows[1]["status"] = status
    if flag is None:
        del rows[1]["would_change"]
    else:
        rows[1]["would_change"] = flag
    bridge.answer["clients"] = (0, json.dumps({"clients": rows}))
    result = _look(bridge)
    assert not result["ok"] and result["steps"][3]["state"] == "fail"
    assert f"the row for kiro {said}" in result["steps"][3]["detail"]


@pytest.mark.parametrize("gone", ["rows", "sources"])
@pytest.mark.parametrize("skill", [fleet.USAGE_SKILL, fleet.SKILL_NAME])
def test_both_skills_the_bridge_ships_must_be_in_the_answer(
    bridge: FakeBridge, skill: str, gone: str
) -> None:
    """Rule G2: in `sources` and as rows for every wanted client. red_before."""
    _all_set(bridge)
    answer = json.loads(
        bridge._installer(["--client", "claude", "--client", "kiro"], True, "targets")
    )
    if gone == "rows":
        answer["targets"] = [row for row in answer["targets"] if row["skill"] != skill]
    else:
        del answer["sources"][skill]
    bridge.answer["skill"] = (0, json.dumps(answer))
    result = _look(bridge)
    assert not result["ok"] and result["steps"][4]["state"] == "fail"
    assert f"the installer's answer names no {skill}" in result["steps"][4]["detail"]


@pytest.mark.parametrize(
    ("status", "code", "state", "said"),
    [
        ("refused", 1, "refused", "refused by the apply: kiro: stand-in says no"),
        ("blocked", 1, "fail", "blocked: kiro"),
        ("blocked", 0, "fail", "blocked: kiro"),
        ("installed", 3, "fail", "the apply ended with exit code 3"),
    ],
)
def test_the_answer_of_an_apply_counts_whatever_a_look_says_afterwards(
    bridge: FakeBridge, status: str, code: int, state: str, said: str
) -> None:
    """Rule G3. red_before."""
    bridge._configure()
    bridge.running = bridge.skilled = True

    def run(argv: list[str], **more: Any):
        if argv[1:3] == ["clients", "install"] and "--apply" in argv:
            bridge.calls.append(argv)
            bridge.registered = True  # someone else put it right before the look that follows
            rows = [{"client": "claude", "status": "installed", "would_change": True}]
            rows.append({"client": "kiro", "status": status, "would_change": status != "refused"})
            if status == "refused":
                rows[1]["error"] = "stand-in says no"
            return subprocess.CompletedProcess(argv, code, json.dumps({"clients": rows}), "")
        return bridge(argv, **more)

    result = fleet.ensure_here(write=True, want="0.14.0", run_=run, handshake_=answers)
    assert not result["ok"] and result["steps"][3]["state"] == state
    assert said in result["steps"][3]["detail"]


@pytest.mark.parametrize("chosen", [["n1", "n1"], ["n1", "n1", "n1"]])
def test_a_machine_that_is_named_twice_gets_one_acting_call(
    node: dict[str, Any], chosen: list[str]
) -> None:
    """Rule F1: the machines are made unique before the first call, and a result that is recorded
    is not replaced by a later one. red_before."""
    node["script"] = [
        node_result(**LOOK),
        fleet.FleetError("connection lost"),
        node_result(**LOOK),
        node_result(**LOOK),
        node_result(**ACTED),
        node_result(**AFTER),
    ]
    args = node_args()
    args.machine = chosen
    result = fleet.ensure_fleet(args, run_=passing)
    assert sum("--yes" in call["args"] for call in node["sent"]) == 1
    assert len(node["sent"]) == 3
    steps = result["machines"]["n1"]["steps"]
    assert [s["state"] for s in steps if s["step"] == "act"] == ["fail"]
    assert not result["machines"]["n1"]["ok"]


def _names(monkeypatch: pytest.MonkeyPatch, *names: str, before: Any = None) -> list[int]:
    """Give the names of the staged files (rule E2: one small function is their only source)."""
    asked: list[int] = []

    def name(attempt: int) -> str:
        if before is not None and not asked:
            before()
        asked.append(attempt)
        return names[attempt] if attempt < len(names) else f".fleet-free-{attempt}"

    monkeypatch.setattr(fleet, "_staged_name", name)
    return asked


def test_a_release_folder_that_is_exchanged_after_it_was_opened_changes_nothing(
    bridge: FakeBridge, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule E1a: the wheel and the constraints file stay in the folder that was opened. Rule R2-5
    (C1-04): the paths the installer would get no longer lead to them, so the installer is not
    started. red_before (the refusal)."""
    release = Path.home() / ".local/state/answerback-bridge/releases/r1"
    release.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    name = wheel_message()["name"]
    (outside / name).write_bytes(b"outside original bytes")
    (outside / "constraints.txt").write_bytes(b"outside constraints")
    opened = release.with_name("r1-opened")

    def exchange() -> None:  # the folder is open: the first staged file is about to be created
        release.rename(opened)
        release.symlink_to(outside, target_is_directory=True)

    _names(monkeypatch, before=exchange)
    started: list[list[str]] = []
    said = fleet.install_bridge(
        wheel_message(), run_=lambda argv, **kw: started.append(argv) or passing(argv)
    )
    assert (
        said == f"the release folder changed while it was written ({name}); nothing was installed"
    )
    assert started == []
    assert (outside / name).read_bytes() == b"outside original bytes"
    assert (outside / "constraints.txt").read_bytes() == b"outside constraints"
    assert sorted(p.name for p in outside.iterdir()) == sorted(["constraints.txt", name])
    assert (opened / name).read_bytes() == b"new wheel bytes"
    assert (opened / "constraints.txt").read_bytes() == b"anyio==4.16.0\n"
    assert sorted(p.name for p in opened.iterdir()) == sorted(["constraints.txt", name])


@pytest.mark.parametrize("taken", ["a link to a file outside", "a plain file"])
@pytest.mark.parametrize("through", ["path", "open folder"])
def test_a_staged_name_that_is_there_already_is_not_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, taken: str, through: str
) -> None:
    """Rule E3: whatever name the source gives. red_before."""
    folder = tmp_path / "folder"
    folder.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    first = folder / ".fleet-first"
    if taken == "a plain file":
        first.write_bytes(b"someone's file")
    else:
        first.symlink_to(outside)
    asked = _names(monkeypatch, ".fleet-first", ".fleet-second")
    dest = folder / "dest.txt"
    if through == "path":
        fleet._private_write(dest, b"new bytes")
    else:
        at = os.open(folder, os.O_RDONLY)
        try:
            fleet._private_write(dest, b"new bytes", dir_fd=at)
        finally:
            os.close(at)
    assert outside.read_bytes() == b"outside" and asked == [0, 1]
    assert first.is_symlink() or first.read_bytes() == b"someone's file"
    assert dest.read_bytes() == b"new bytes" and dest.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in folder.iterdir()) == [".fleet-first", "dest.txt"]


def test_a_staged_file_gives_up_after_a_few_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule E2: a few tries, then an error. red_before."""
    (tmp_path / ".fleet-same").write_bytes(b"someone's file")
    monkeypatch.setattr(fleet, "_staged_name", lambda attempt: ".fleet-same")
    with pytest.raises(fleet.FleetError, match="no free name for a staged file"):
        fleet._private_write(tmp_path / "dest.txt", b"new bytes")
    assert (tmp_path / ".fleet-same").read_bytes() == b"someone's file"
    assert not (tmp_path / "dest.txt").exists()


def test_what_took_the_place_of_the_new_link_is_left_and_named(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule E4: the link is removed again only when it is still that link. red_before."""
    shared = Path.home() / fleet.SHARED / fleet.USAGE_SKILL
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("usage")
    parent = Path.home() / ".claude/skills"
    parent.mkdir(parents=True)
    dest = parent / fleet.USAGE_SKILL
    real = os.symlink

    def exchanged(target: Any, path: Any) -> None:
        real(target, path)
        Path(path).unlink()
        Path(path).write_bytes(b"the owner's file")

    monkeypatch.setattr(fleet.os, "symlink", exchanged)
    claude = next(h for h in fleet.HARNESSES if h.id == "claude")
    assert fleet._link_usage_skill([claude], write=True) == (
        "fail",
        f"the link did not open as the skill; what is at {dest} now is not the link that was made, "
        "and was left",
    )
    assert dest.read_bytes() == b"the owner's file"


def test_a_new_link_that_does_not_open_as_the_skill_is_left_and_named(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule R2-4 (C1-01): reading the link and removing the name are two steps, so the name is
    never removed; the next run finds the link and is `ok` once the skill opens. red_before."""
    shared = Path.home() / fleet.SHARED / fleet.USAGE_SKILL
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("usage")
    parent = Path.home() / ".claude/skills"
    parent.mkdir(parents=True)
    real = os.symlink

    def emptied(target: str, path: Path) -> None:  # the link is made, the skill behind it is gone
        real(target, path)
        (shared / "SKILL.md").unlink()

    monkeypatch.setattr(fleet.os, "symlink", emptied)
    claude = next(h for h in fleet.HARNESSES if h.id == "claude")
    dest = parent / fleet.USAGE_SKILL
    assert fleet._link_usage_skill([claude], write=True) == (
        "fail",
        f"{dest} is linked and does not open as the skill (no SKILL.md is behind it); "
        "the link was left in place",
    )
    assert dest.is_symlink() and [p.name for p in parent.iterdir()] == [fleet.USAGE_SKILL]
    monkeypatch.setattr(fleet.os, "symlink", real)
    (shared / "SKILL.md").write_text("usage")
    assert fleet._link_usage_skill([claude], write=True)[0] == "ok"
    source = inspect.getsource(fleet._link_usage_skill)
    assert "unlink" not in source and "rmdir" not in source


@pytest.mark.parametrize("status", ["completed", "failed", "abandoned", "cancelled"])
def test_a_task_in_a_terminal_status_is_not_unfinished(status: str) -> None:
    """Rule E5: one test per terminal status of src/answerback_bridge/tasks.py. `abandoned` is
    red against the review's edit; all four pin what is there (pins_existing)."""
    assert fleet._unfinished({"tasks": {status: 7, "running": 2}}).startswith(
        "; 2 unfinished task(s) in its store"
    )
    assert fleet._unfinished({"tasks": {status: 7}}).startswith("; 0 unfinished task(s)")


# ----- rulings I and K of review 1 -----


def test_the_look_shows_a_running_bridge_as_a_restart_when_its_configuration_is_gone(
    linux: FakeBridge,
) -> None:
    """Rule I1 (E1-11): the configure step plans a change, and the start step still names the
    restart and what it costs, not `answerback start`. red_before."""
    _arrange(linux, "the bridge's unit", "none")
    linux.config.unlink()
    linux.version = "0.13.0"
    calls = len(linux.calls)
    look = fleet.ensure_here(write=False, want="0.14.0", run_=linux, handshake_=answers)
    start = look["steps"][2]
    assert states(look)["configure"] == "change" and start["state"] == "change"
    assert start["detail"].startswith("restart after the upgrade" + COST)
    assert not [c for c in linux.calls[calls:] if c[1:2] in (["stop"], ["start"], ["restart"])]


def test_a_bridge_that_is_newer_than_the_wanted_one_is_ok_and_both_versions_are_named(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule K1: never an older program over a newer one; the detail names both. red_before
    (the words)."""
    bridge.version = "0.15.1"
    monkeypatch.setattr(fleet, "install_bridge", lambda *a, **k: pytest.fail("installed"))
    result = fleet.ensure_here(
        write=True, want="0.14.0", wheel=wheel_message(), run_=bridge, handshake_=answers
    )
    assert result["steps"][0] == {
        "step": "install",
        "state": "ok",
        "detail": "answerback 0.15.1 (newer than the coordinator's 0.14.0)",
    }


def test_the_same_version_is_named_alone(bridge: FakeBridge) -> None:
    """Rule K1, the other side. pins_existing."""
    result = fleet.ensure_here(write=False, want="0.14.0", run_=bridge, handshake_=answers)
    assert result["steps"][0]["detail"] == "answerback 0.14.0"


@pytest.mark.parametrize("write", [False, True])
def test_no_bridge_and_no_wheel_is_said_in_words(bridge: FakeBridge, write: bool) -> None:
    """Rule K2: not `install answerback None`. red_before."""
    bridge.installed = False
    result = fleet.ensure_here(write=write, want=None, run_=bridge, handshake_=answers)
    first = result["steps"][0]
    assert first["state"] == ("fail" if write else "change")
    assert first["detail"].startswith("install answerback: no release wheel is known here")
    assert "None" not in json.dumps(result)


def test_done_needs_a_status_that_was_read_after_the_start(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule A2: `done` needs `up`. The start says `done`, and the status that is read after it
    gives nothing usable: the step is `fail` and says that it cannot be told. red_before (the
    words: `2c1d372` says `did not stay up`, which was not observed)."""
    _arrange(linux, "not running", "none")
    after: list[int] = []
    monkeypatch.setattr(fleet, "_start", lambda *a, **k: after.append(1) or ("done", "started"))

    def blind(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        finished = linux(argv, **kw)
        if after and argv == ["/bin/answerback", "status"]:
            return subprocess.CompletedProcess(argv, 0, "no answer", "")
        return finished

    result = fleet.ensure_here(write=True, want="0.14.0", run_=blind, handshake_=answers)
    start = result["steps"][2]
    assert start["state"] == "fail" and not result["ok"]
    assert (
        start["detail"].startswith("whether it runs cannot be told (")
        and "started" in start["detail"]
    )
    assert "did not stay up" not in start["detail"]


# --------------------------------------------------------------------------- the closure check
#
# Rulings on ENSURE-CLOSURE-1 (R2-1 to R2-10). Every command is a stand-in.


@pytest.mark.parametrize("gone", ["the constraints differ", "the wheel cannot be read"])
def test_the_installer_is_started_only_while_the_paths_lead_to_what_was_written(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    """Rule R2-5 (C1-04): both files are read back through the paths right before the installer
    is started. red_before."""
    wheel = wheel_message()
    release = Path.home() / ".local/state/answerback-bridge/releases/r1"
    real = fleet._private_write

    def write(path: Path, data: bytes, dir_fd: int | None = None) -> None:
        real(path, data, dir_fd=dir_fd)
        if path.name == "constraints.txt" and gone == "the constraints differ":
            path.write_bytes(b"other==1\n")
        elif path.name == "constraints.txt":
            (release / wheel["name"]).unlink()

    monkeypatch.setattr(fleet, "_private_write", write)
    started: list[list[str]] = []
    said = fleet.install_bridge(
        wheel, run_=lambda argv, **kw: started.append(argv) or passing(argv)
    )
    which = "constraints.txt" if gone == "the constraints differ" else wheel["name"]
    assert (
        said == f"the release folder changed while it was written ({which}); nothing was installed"
    )
    assert started == []


@pytest.mark.parametrize(
    ("status", "words"),
    [
        (None, "previously running, now stopped"),
        (
            0,
            "previously running, whether it runs cannot be told (answerback status gave no answer "
            "that can be read)",
        ),
    ],
)
def test_the_last_look_names_a_bridge_that_ran_and_is_not_up(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch, status: int | None, words: str
) -> None:
    """Rule R2-2 (C1-02): a result that is not `done` is corrected by the last look too.
    red_before."""
    _arrange(linux, "nothing restarts it", "none")

    def start(*a: Any, **k: Any) -> tuple[str, str]:
        linux.running, linux.status_good = False, status
        return "fail", "could not stop the running bridge (x); it still runs as before"

    monkeypatch.setattr(fleet, "_start", start)
    assert _ensure(linux) == {
        "step": "start",
        "state": "fail",
        "detail": f"{words} (the start step had said: could not stop the running bridge (x); it "
        "still runs as before)",
    }


def test_the_last_look_takes_back_a_stop_that_the_start_step_said(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule R2-2: the bridge answers at the last look; the state stays `fail`. red_before."""
    _arrange(linux, "nothing restarts it", "none")
    said = "previously running, now stopped (the detached start: no)"
    monkeypatch.setattr(fleet, "_start", lambda *a, **k: ("fail", said))
    assert _ensure(linux) == {
        "step": "start",
        "state": "fail",
        "detail": f"the bridge answers at the last look (the start step had said: {said})",
    }


@pytest.mark.parametrize("status", ["installed", "migrated"])
@pytest.mark.parametrize("flag", [True, False])
def test_a_status_that_only_an_apply_gives_is_not_ok_in_a_look(
    bridge: FakeBridge, status: str, flag: bool
) -> None:
    """Rule R2-3 (C1-03): the installer's table by phase. red_before."""
    _all_set(bridge)
    rows = [{"client": c, "status": "unchanged", "would_change": False} for c in ("claude", "kiro")]
    rows[0].update(status=status, would_change=flag)
    bridge.answer["clients"] = (0, json.dumps({"clients": rows}))
    for result in (_look(bridge), fleet.ensure_here(write=True, run_=bridge, handshake_=answers)):
        assert not result["ok"] and result["steps"][3] == {
            "step": "register",
            "state": "fail",
            "detail": f"the installer's look answered {status} for claude, "
            "which only an apply gives",
        }
    assert not [c for c in bridge.calls if "--apply" in c]


@pytest.mark.parametrize("status", ["installed", "migrated"])
def test_an_applied_status_with_the_flag_false_is_not_done(bridge: FakeBridge, status: str) -> None:
    """Rule R2-3: the installer changes the status and leaves the flag of its plan. red_before."""
    bridge._configure()
    bridge.running = bridge.skilled = True

    def run(argv: list[str], **more: Any):
        if argv[1:3] == ["clients", "install"] and "--apply" in argv:
            bridge.calls.append(argv)
            bridge.registered = True
            rows = [{"client": "claude", "status": "installed", "would_change": True}]
            rows.append({"client": "kiro", "status": status, "would_change": False})
            return subprocess.CompletedProcess(argv, 0, json.dumps({"clients": rows}), "")
        return bridge(argv, **more)

    result = fleet.ensure_here(write=True, want="0.14.0", run_=run, handshake_=answers)
    assert not result["ok"] and result["steps"][3]["state"] == "fail"
    assert (
        f"the row for kiro says {status} with would_change false in the installer's apply"
        in result["steps"][3]["detail"]
    )


def test_the_stand_in_answers_an_apply_as_the_installer_does(bridge: FakeBridge) -> None:
    """Rule R2-3: after `--apply` the planned rows are `installed` (`migrated` for the migration)
    with the flag true; the look after it says `unchanged`, false. The step is `done`.
    red_before (the stand-in answered `unchanged`)."""
    bridge._configure()
    bridge.running = True
    bridge.applied_status = {"kiro": "migrated"}
    applied: list[Any] = []

    def run(argv: list[str], **more: Any):
        finished = bridge(argv, **more)
        if argv[1:3] == ["clients", "install"] and "--apply" in argv:
            applied.append(json.loads(finished.stdout))
        return finished

    result = fleet.ensure_here(write=True, want="0.14.0", run_=run, handshake_=answers)
    assert [s["state"] for s in result["steps"][3:5]] == ["done", "done"]
    assert [(r["client"], r["status"], r["would_change"]) for r in applied[0]["clients"]] == [
        ("claude", "installed", True),
        ("kiro", "migrated", True),
    ]
    after = json.loads(bridge(["/bin/answerback", "clients", "install", "--client", "kiro"]).stdout)
    assert [(r["status"], r["would_change"]) for r in after["clients"]] == [("unchanged", False)]


# --------------------------------------------------------------------------- R2-1, R2-6, R2-7, R2-8

DOWN_AFTER = (
    "; the stop of the bridge outside its unit reported failure (stop failed), and the bridge "
    "was down after it"
)
OFF = f"{fleet.FLEET_UNIT} was enabled before, was disabled by this run and is not enabled again"


def _after_last_stop(linux: FakeBridge) -> list[list[str]]:
    last = [i for i, c in enumerate(linux.calls) if c == ["/bin/answerback", "stop"]][-1]
    return linux.calls[last + 1 :]


def test_a_later_stop_that_fails_with_the_bridge_down_goes_on_and_says_so(
    linux: FakeBridge,
) -> None:
    """Rule R2-1d: the repair follows, and the detail ends with the stop's failure. red_before."""
    _arrange(linux, "nothing restarts it", "a later stop fails, the bridge is down")
    start = _ensure(linux, standalone=True)
    assert linux.stops == 2 and start["state"] == "done" and linux.fleet_enabled
    assert start["detail"].startswith("running under the repaired user unit")
    assert start["detail"].endswith(DOWN_AFTER)


def test_a_later_stop_that_fails_with_its_own_unit_still_enabled_ends_the_step(
    linux: FakeBridge,
) -> None:
    """Rule R2-1d: a unit of the bridge's own that is enabled would start a second bridge.
    red_before."""
    _arrange(
        linux,
        "nothing restarts it",
        "a later stop fails, the bridge is down, its own unit still enabled",
    )
    start = _ensure(linux, standalone=True)
    assert start["state"] == "fail" and not linux.running
    assert (
        "the stop of the bridge outside its unit reported failure (stop failed); the bridge is "
        f"down and its own unit {unit_name(linux.config)} is still enabled; nothing was repaired "
        "or enabled"
    ) in start["detail"]
    assert start["detail"].startswith("previously running, now stopped (the start step had said: ")
    after = _after_last_stop(linux)
    assert not [c for c in after if {"enable", "daemon-reload", "start"} & set(c)]


def test_a_later_stop_with_no_status_after_it_tries_the_detached_start_once(
    linux: FakeBridge,
) -> None:
    """Rule R2-1e, ran with nothing to restart it. red_before."""
    _arrange(linux, "nothing restarts it", "a later stop fails, the status cannot be read")
    start = _ensure(linux, standalone=True)
    after = _after_last_stop(linux)
    assert len([c for c in after if "--standalone" in c]) == 1
    assert not [c for c in after if "enable" in c or "daemon-reload" in c]
    assert start["state"] == "fail"
    assert start["detail"].startswith("previously running, whether it runs cannot be told")
    assert "a detached start was tried" in start["detail"] and "stop failed" in start["detail"]


def test_a_later_stop_with_no_status_after_it_enables_our_unit_again(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule R2-1e, ran under our unit. red_before."""
    _arrange(linux, "our unit", "a later stop fails, the status cannot be read")
    linux.version = linux.running_version = "0.13.0"
    monkeypatch.setattr(
        fleet, "install_bridge", lambda *a, **k: setattr(linux, "version", "0.14.0")
    )
    start = _ensure(linux)
    after = _after_last_stop(linux)
    enable = ["systemctl", "--user", "enable", "--now", fleet.FLEET_UNIT]
    assert [c for c in after if "enable" in c] == [enable] and linux.fleet_enabled
    assert start["state"] == "fail" and f"{fleet.FLEET_UNIT} was enabled again" in start["detail"]
    assert OFF not in start["detail"]


@pytest.mark.parametrize("enabled", [True, False])
def test_a_later_stop_with_no_status_starts_nothing_where_no_bridge_ran(
    linux: FakeBridge, enabled: bool
) -> None:
    """Rule R2-1e, did not run before: our unit is enabled again without `--now`. red_before."""
    _arrange(linux, "not running", "a later stop fails, the status cannot be read")
    if enabled:
        target = fleet._units() / fleet.FLEET_UNIT
        target.write_text(marker(linux.config) + "\n" + unit_text(linux.config))
        linux.fleet_enabled = True
    start = _ensure(linux, standalone=True)
    after = _after_last_stop(linux)
    assert not [c for c in after if "start" in c or "--now" in c or "daemon-reload" in c]
    wanted = [["systemctl", "--user", "enable", fleet.FLEET_UNIT]] if enabled else []
    assert [c for c in after if "enable" in c] == wanted and linux.fleet_enabled is enabled
    assert start["state"] == "fail" and "whether it runs cannot be told" in start["detail"]
    assert ("without starting it" if enabled else "nothing was started") in start["detail"]


@pytest.mark.parametrize("which", ["first", "later"])
def test_a_stop_that_gives_up_while_the_bridge_drains_is_asked_again(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """Rule R2-1a: once a second until it is down; what was seen last decides. red_before."""
    naps: list[float] = []
    monkeypatch.setattr(fleet.time, "sleep", naps.append)
    fault = "the first stop gives up while the bridge drains"
    _arrange(linux, "nothing restarts it", fault if which == "first" else "a later" + fault[9:])
    start = _ensure(linux, standalone=True)
    assert start["state"] == "done" and linux.running and naps.count(1) >= 2
    if which == "later":
        assert start["detail"].endswith(
            DOWN_AFTER.replace("stop failed", "Bridge is still draining")
        )
    else:
        assert "running under the bridge's own supervisor" in start["detail"]


def test_a_bridge_that_drains_for_ever_is_asked_thirty_times(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule R2-1a: 30 times at most, then the way back of rule R2-1e. red_before."""
    naps: list[float] = []
    monkeypatch.setattr(fleet.time, "sleep", naps.append)
    _arrange(linux, "nothing restarts it", "a later stop gives up while the bridge drains")

    def run(argv: list[str], **kw: Any) -> Any:
        done = linux(argv, **kw)
        if argv == ["/bin/answerback", "stop"] and linux.stops > 1:
            linux.draining = 10**6
        return done

    result = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=run, handshake_=answers
    )
    start = result["steps"][2]
    assert start["state"] == "fail" and "it runs but does not accept tasks" in start["detail"]
    after = _after_last_stop(linux)
    assert len([c for c in after if "--standalone" in c]) == 1


def test_our_unit_that_this_run_disabled_and_left_off_is_named(linux: FakeBridge) -> None:
    """Rule R2-1f, with rule R2-1b: the bridge still runs outside its unit. red_before."""
    _arrange(linux, "not running", "a later stop fails")
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text(marker(linux.config) + "\n" + unit_text(linux.config))
    linux.fleet_enabled = True
    start = _ensure(linux, standalone=True)
    assert start["state"] == "fail" and linux.running and not linux.fleet_enabled
    assert start["detail"].startswith(
        "the bridge runs outside its unit and could not be stopped (stop failed); "
        "nothing restarts it"
    )
    assert start["detail"].endswith(OFF)


def test_a_later_stop_of_a_bridge_that_this_step_started_carries_the_count(
    linux: FakeBridge,
) -> None:
    """Rule R2-6: not running at the start; the stop of the bridge outside its unit, and the
    `done` of the repair after it. red_before."""
    _arrange(linux, "not running", "none")
    linux.start_outside = True
    start = _ensure(linux, standalone=True)
    assert start["state"] == "done" and linux.stops == 1
    assert start["detail"].startswith("running under the repaired user unit")
    assert COST in start["detail"]


def test_a_status_without_a_task_map_gives_no_older_count(linux: FakeBridge) -> None:
    """Rule R2-6: the count is of the status read right before the stop. red_before."""
    _arrange(linux, "nothing restarts it", "none")
    reads: list[int] = []

    def run(argv: list[str], **kw: Any) -> Any:
        done = linux(argv, **kw)
        if argv == ["/bin/answerback", "status"]:
            reads.append(1)
            if (
                len(reads) > 2
            ):  # the two reads of the look keep their count; the one before the stop has none
                whole = json.loads(done.stdout)
                del whole["tasks"]
                done = subprocess.CompletedProcess(argv, 0, json.dumps(whole), "")
        return done

    result = fleet.ensure_here(write=True, want="0.14.0", run_=run, handshake_=answers)
    start = result["steps"][2]
    assert start["state"] == "done" and linux.stops == 1
    assert "an unknown number of unfinished tasks in its store" in start["detail"]
    assert "3 unfinished" not in start["detail"]


def test_a_unit_whose_command_holds_two_dashes_as_a_word_is_not_repaired(
    linux: FakeBridge,
) -> None:
    """Rule R2-7: the bridge's parser reads nothing after `--` as an option. red_before."""
    unit = fleet._units() / unit_name(linux.config)
    text = unit_text(linux.config)
    flag = '"--config"' if '"--config"' in text else "--config"
    unit.write_text(text.replace(flag, flag.replace("--config", "--") + " " + flag))
    assert "-- " in unit.read_text() or '"--" ' in unit.read_text()
    linux.calls.clear()
    problem = fleet.repair_unit(linux.config.parent, linux)
    assert problem is not None and problem[0] == "fail"
    assert "cannot be repaired" in problem[1] and "the word `--`" in problem[1]
    assert not (fleet._units() / fleet.FLEET_UNIT).exists()
    assert not [c for c in linux.calls if "enable" in c or "daemon-reload" in c]


def test_a_start_that_reports_failure_is_a_failure_with_standalone_too(
    linux: FakeBridge,
) -> None:
    """Rule R2-8: no user manager answers, the bridge is up after it. red_before."""
    _arrange(linux, "not running", "own start reports failure, the bridge is up")
    linux.manager = False
    start = _ensure(linux, standalone=True)
    assert start["state"] == "fail" and linux.running
    assert start["detail"] == (
        "running; what restarts it cannot be told here "
        "(answerback start reported failure: start said no)"
    )
    assert not [c for c in linux.calls if "--standalone" in c]


@pytest.mark.parametrize("third", ["a look that answers", "a look that cannot be started"])
def test_an_acting_call_that_does_not_come_back_is_a_result(
    node: dict[str, Any], third: str
) -> None:
    """Rule R2-10 (E1-03), with a third answer in the stand-in: no exception leaves, the acting
    call is sent once, its line and the line of the look are in the answer. pins_existing."""
    last = node_result(**LOOK) if third == "a look that answers" else OSError("stand-in: no route")
    node["script"] = [node_result(**LOOK), OSError("stand-in: output pipe EIO"), last]
    result = fleet.ensure_fleet(node_args(), run_=passing)
    assert len(node["sent"]) == 3
    assert ["--yes" in call["args"] for call in node["sent"]] == [False, True, False]
    machine = result["machines"]["n1"]
    assert not machine["ok"]
    told = json.dumps(machine)
    assert "output pipe EIO" in told
    print("R2-10 " + third + ": " + json.dumps(machine, sort_keys=True))


# ------------------------------------------------- ensure: board row T-155 (the follow-up of T-150)
#
# D: the skill installer of the released bridge answers in an older form: `source` is one path,
# there is no `sources`, and no row names a skill. The stand-in below has the shape of that answer;
# its paths are neutral. A, B and C: the words of three results whose state is `fail`.

OLDER_SKILL_LINE = (
    "the installed bridge's installer is an older build: it does not name skills and does not "
    "ship the setup skill"
)
OLDER_INSTALL_LINE = (
    "the installed bridge is an older build than a source of the same version (its skill "
    "installer answers in the older form); only a reinstall from the source changes that, which is "
    "the owner's step"
)


def _older(bridge: FakeBridge, change: Any = None, refuses: str = "") -> Any:
    """`bridge`, with the skill installer of the released bridge: one skill, no skill names, a
    row for the shared folder first. `change` edits the answer; `refuses` is a client it refuses."""

    def run(argv: list[str], **more: Any) -> Any:
        if argv[1:3] != ["skill", "install"]:
            return bridge(argv, **more)
        bridge.calls.append(argv)
        applied, was = "--apply" in argv, bridge.skilled
        bridge.skilled = was or applied
        rows: list[dict[str, Any]] = []
        for client in [
            "shared",
            *[argv[i + 1] for i, word in enumerate(argv) if word == "--client"],
        ]:
            row: dict[str, Any] = {
                "client": client,
                "path": f"/somewhere/{client}/skills/{fleet.USAGE_SKILL}",
                "ready": was or applied,
                "would_change": not was,
                "status": "unchanged" if was else "installed" if applied else "planned",
            }
            if client == refuses:
                row.update(ready=False, would_change=False, status="refused", error="stand-in no")
            rows.append(row)
        answer: Any = {
            "success": not refuses,
            "applied": applied,
            "source": f"/pkg/data/skills/{fleet.USAGE_SKILL}",
            "targets": rows,
        }
        if change is not None:
            answer = change(answer)
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return subprocess.CompletedProcess(argv, 0, text, "")

    return run


def _usage(*clients: str) -> str:
    return ", ".join(f"{fleet.USAGE_SKILL} for {client}" for client in clients)


def test_an_answer_in_the_older_form_is_read_as_one_about_the_usage_skill_alone(
    bridge: FakeBridge,
) -> None:
    """T-155, D1: told by its shape; every row is a row of the usage skill, none of the setup
    skill is asked for. red_before."""
    _all_set(bridge)
    argv = ["/bin/answerback", "skill", "install", "--client", "claude", "--client", "kiro"]
    rows = fleet._installer_rows(_older(bridge)(argv), "targets", ["claude", "kiro"])
    assert [(row["skill"], row["client"]) for row in rows] == [
        (fleet.USAGE_SKILL, client) for client in ("shared", "claude", "kiro")
    ]
    skill = fleet.ensure_here(write=False, want="0.14.0", run_=_older(bridge), handshake_=answers)[
        "steps"
    ][4]
    assert skill["step"] == "skill" and skill["state"] == "ok"
    assert skill["detail"].startswith("unchanged: " + _usage("claude", "kiro", "shared"))
    assert fleet.SKILL_NAME not in skill["detail"]


@pytest.mark.parametrize(
    ("skilled", "refuses", "state", "begins"),
    [
        (True, "", "ok", "unchanged: " + _usage("claude", "kiro", "shared")),
        (False, "", "change", "install: " + _usage("claude", "kiro", "shared")),
        (True, "claude", "refused", f"{fleet.USAGE_SKILL} for claude: stand-in no"),
    ],
)
def test_the_skill_line_reports_the_rows_and_ends_with_the_older_build(
    bridge: FakeBridge, skilled: bool, refuses: str, state: str, begins: str
) -> None:
    """T-155, D2: the states are today's states for the usage skill; the clause ends the line.
    red_before."""
    _all_set(bridge)
    bridge.skilled = skilled
    result = fleet.ensure_here(
        write=False, want="0.14.0", run_=_older(bridge, refuses=refuses), handshake_=answers
    )
    skill = result["steps"][4]
    assert skill["state"] == state and skill["detail"].startswith(begins)
    assert skill["detail"].endswith("; " + OLDER_SKILL_LINE)
    assert skill["detail"].count(OLDER_SKILL_LINE) == 1


def test_the_install_line_says_once_that_the_installed_bridge_is_an_older_build(
    bridge: FakeBridge,
) -> None:
    """T-155, D3: where the versions are equal and the installer answered in the older form; not
    for an installer that names its skills, and not where the versions differ. red_before."""
    _all_set(bridge)
    result = fleet.ensure_here(write=False, want="0.14.0", run_=_older(bridge), handshake_=answers)
    assert result["steps"][0] == {
        "step": "install",
        "state": "ok",
        "detail": "answerback 0.14.0; " + OLDER_INSTALL_LINE,
    }
    assert json.dumps(result).count(OLDER_INSTALL_LINE) == 1
    assert OLDER_INSTALL_LINE not in json.dumps(_look(bridge))  # the installer of the source
    bridge.version = "0.15.0"
    newer = fleet.ensure_here(write=False, want="0.14.0", run_=_older(bridge), handshake_=answers)
    assert newer["steps"][0]["detail"] == "answerback 0.15.0 (newer than the coordinator's 0.14.0)"
    assert newer["steps"][4]["detail"].endswith(OLDER_SKILL_LINE)


def test_a_healthy_machine_with_the_released_bridge_reads_ok(bridge: FakeBridge) -> None:
    """T-155, D4: neither sentence makes a machine need attention. red_before."""
    _all_set(bridge)
    result = fleet.ensure_here(write=False, want="0.14.0", run_=_older(bridge), handshake_=answers)
    assert result["ok"] and result["failed"] == 0
    assert set(states(result).values()) == {"ok"}
    assert OLDER_SKILL_LINE in result["steps"][4]["detail"]
    assert bridge.wrote() == []


def _with(**changes: Any) -> Any:
    return lambda answer: {**answer, **changes}


def _rows(edit: Any) -> Any:
    def change(answer: dict[str, Any]) -> dict[str, Any]:
        edit(answer["targets"])
        return answer

    return change


BROKEN_OLDER_ANSWERS = {
    "not JSON": (
        lambda answer: "Traceback",
        "the installer's answer is not usable (it is not JSON)",
    ),
    "not a mapping": (
        lambda answer: answer["targets"],
        "the installer's answer is not usable (it is not a mapping)",
    ),
    "no list": (
        _with(targets={"shared": "unchanged"}),
        "the installer's answer is not usable (`targets` is not a list)",
    ),
    "a row that names no client": (
        _rows(lambda rows: rows[1].pop("client")),
        "the installer's answer is not usable (a row names no client)",
    ),
    "a skill that is present and not a string": (
        _rows(lambda rows: [row.update(skill=None) for row in rows]),
        "the installer's answer is not usable (the row for shared names no skill)",
    ),
    "some rows with a skill and some without": (
        _rows(lambda rows: rows[0].update(skill=fleet.USAGE_SKILL)),
        "the installer's answer is not usable (the row for claude names no skill)",
    ),
    "a status the installer cannot produce": (
        _rows(lambda rows: rows[2].update(status="fine")),
        "the installer's answer is not usable (the row for kiro has the status 'fine')",
    ),
    "a source that is no path": (
        _with(source=None),
        "the installer's answer is not usable (the row for shared names no skill)",
    ),
    "a source and a mapping of sources": (
        _with(sources={fleet.USAGE_SKILL: "/pkg/a"}),
        "the installer's answer is not usable (the row for shared names no skill)",
    ),
}


@pytest.mark.parametrize("broken", list(BROKEN_OLDER_ANSWERS))
def test_an_answer_that_is_broken_in_another_way_still_fails_with_the_words_it_had(
    bridge: FakeBridge, broken: str
) -> None:
    """T-155, D5: the older form is one shape and nothing wider. pins_existing for seven of the
    nine, which fail with these words before the change too. red_before for the row that names
    no client and for the status: before, the first row's missing skill was named instead."""
    _all_set(bridge)
    change, said = BROKEN_OLDER_ANSWERS[broken]
    result = fleet.ensure_here(
        write=False, want="0.14.0", run_=_older(bridge, change), handshake_=answers
    )
    assert not result["ok"]
    assert result["steps"][4] == {"step": "skill", "state": "fail", "detail": said}
    assert "older build" not in json.dumps(result)


def test_with_yes_the_older_form_is_read_the_same_and_is_no_reason_to_act(
    bridge: FakeBridge,
) -> None:
    """T-155, D6: nothing is installed, upgraded, stopped, started or written. red_before."""
    _all_set(bridge)
    look = fleet.ensure_here(write=False, want="0.14.0", run_=_older(bridge), handshake_=answers)
    seen = len(bridge.calls)
    acted = fleet.ensure_here(
        write=True, want="0.14.0", wheel=wheel_message(), run_=_older(bridge), handshake_=answers
    )
    assert acted == look and acted["ok"]
    words = {"configure", "start", "restart", "stop", "--apply", "install", "enable", "disable"}
    sent = [c for c in bridge.calls[seen:] if words & set(c) and "install" not in c[1:3]]
    assert sent == [] and not [
        c for c in bridge.calls[seen:] if "--apply" in c or c[0] != "/bin/answerback"
    ]


def test_an_apply_of_the_older_installer_and_the_look_after_it_are_read_in_the_older_form(
    bridge: FakeBridge,
) -> None:
    """T-155, D6: the apply path (`_installer_rows(..., "apply")`) and the look after it, for a
    usage skill that the older installer has to install. red_before."""
    _all_set(bridge)
    bridge.skilled = False
    result = fleet.ensure_here(write=True, want="0.14.0", run_=_older(bridge), handshake_=answers)
    skill = result["steps"][4]
    assert result["ok"] and skill["state"] == "done" and bridge.skilled
    assert skill["detail"] == _usage("claude", "kiro", "shared") + "; " + OLDER_SKILL_LINE
    calls = [c for c in bridge.calls if c[1:3] == ["skill", "install"]]
    assert ["--apply" in c for c in calls] == [False, True, False]
    assert result["steps"][0]["detail"].count(OLDER_INSTALL_LINE) == 1
    argv = [*calls[1]]
    bridge.skilled = False
    rows = fleet._installer_rows(_older(bridge)(argv), "targets", ["claude", "kiro"], "apply")
    assert {row["status"] for row in rows} == {"installed"}
    assert all(row["skill"] == fleet.USAGE_SKILL for row in rows)


def test_the_last_look_makes_the_words_about_now_again_after_a_stop_the_step_named(
    linux: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-155, A (the check's C1-02): the step ended with the bridge down; a start from elsewhere
    brings up a bridge that runs and does not accept work yet. The state stays `fail`.
    red_before."""
    _arrange(linux, "nothing restarts it", "own start fails")
    linux.detached_fails = True
    real = fleet._start

    def start(*a: Any, **k: Any) -> tuple[str, str]:
        ended = real(*a, **k)
        assert ended[0] == "fail" and ended[1].startswith("previously running, now stopped")
        linux._begin()  # started from elsewhere, between the step and the last look
        linux.draining = 100
        return ended

    monkeypatch.setattr(fleet, "_start", start)
    row = _ensure(linux)
    assert linux.running and row["state"] == "fail"
    assert row["detail"].startswith(
        "previously running, whether it runs cannot be told (it runs but does not accept tasks) "
        "(the start step had said: previously running, now stopped"
    )


def test_a_later_stop_names_the_count_that_is_read_right_before_it(linux: FakeBridge) -> None:
    """T-155, B (the check's C1-07): seven tasks arrive between the first stop and the later one.
    red_before."""
    _arrange(linux, "nothing restarts it", "a later stop fails")
    linux.tasks = {"completed": 10, "queued": 0}

    def run(argv: list[str], **kw: Any) -> Any:
        done = linux(argv, **kw)
        if argv == ["/bin/answerback", "start"]:
            linux.tasks = {"completed": 10, "queued": 7}
        return done

    result = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=run, handshake_=answers
    )
    row = result["steps"][2]
    assert linux.stops == 2 and linux.running and row["state"] == "fail"
    assert "7 unfinished task(s) in its store" in row["detail"]
    assert "0 unfinished" not in row["detail"]


def test_a_later_stop_keeps_what_an_earlier_command_said_beside_the_new_count(
    linux: FakeBridge,
) -> None:
    """T-155, B: the later stop fails and leaves the bridge down, the repair follows and the
    step is `done`: the new count, and after it what that stop said. red_before."""
    _arrange(linux, "nothing restarts it", "a later stop fails, the bridge is down")
    linux.tasks = {"completed": 10, "queued": 0}

    def run(argv: list[str], **kw: Any) -> Any:
        done = linux(argv, **kw)
        if argv == ["/bin/answerback", "start"]:
            linux.tasks = {"completed": 10, "queued": 7}
        return done

    result = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=run, handshake_=answers
    )
    row = result["steps"][2]
    assert row["state"] == "done" and linux.stops == 2
    assert (
        "7 unfinished task(s) in its store" in row["detail"] and "0 unfinished" not in row["detail"]
    )
    assert row["detail"].endswith(DOWN_AFTER)


def test_a_unit_that_this_run_disabled_is_named_where_the_start_fails_with_the_bridge_up(
    linux: FakeBridge,
) -> None:
    """T-155, C (the check's C2-01): our unit was enabled and idle; `answerback start` reports
    failure and leaves the bridge up. red_before."""
    _arrange(linux, "not running", "own start reports failure, the bridge is up")
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text(marker(linux.config) + "\n" + unit_text(linux.config))
    linux.fleet_enabled = True
    row = _ensure(linux)
    assert linux.running and not linux.fleet_enabled and row["state"] == "fail"
    assert row["detail"].startswith("running, but nothing restarts it (start said no)")
    assert row["detail"].endswith(OFF)


def test_a_unit_that_was_not_enabled_before_is_not_said_to_be_disabled_by_this_run(
    linux: FakeBridge,
) -> None:
    """T-155, C: the sentence is about a unit that was enabled. pins_existing."""
    _arrange(linux, "not running", "own start reports failure, the bridge is up")
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text(marker(linux.config) + "\n" + unit_text(linux.config))
    row = _ensure(linux)
    assert row["state"] == "fail" and "nothing restarts it" in row["detail"]
    assert "disabled by this run" not in row["detail"]


def test_a_unit_that_this_run_disabled_is_named_where_the_repair_after_it_fails(
    linux: FakeBridge,
) -> None:
    """T-155, C, another exit that leaves the unit disabled: with `--standalone`, the start
    fails with the bridge down and the repaired unit cannot be enabled. red_before."""
    _arrange(linux, "not running", "enable fails")
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text(marker(linux.config) + "\n" + unit_text(linux.config))
    linux.fleet_enabled = True
    row = _ensure(linux, standalone=True)
    assert not linux.running and not linux.fleet_enabled and row["state"] == "fail"
    assert row["detail"].startswith("systemctl --user enable --now")
    assert row["detail"].endswith(OFF)


def test_an_unread_answer_about_the_unit_is_not_said_to_be_a_disabled_unit(
    linux: FakeBridge,
) -> None:
    """T-155, C, the review's B1: the repair enables the unit, the bridge does not come up, and
    the question whether the unit is enabled gets no answer. The line stays `fail` and says that
    it cannot be told; it does not say `is not enabled again`. red_before."""
    _arrange(linux, "not running", "enable fails")
    linux.failing.clear()
    linux.enable_starts = False  # the enable works; the bridge does not come up
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text(marker(linux.config) + "\n" + unit_text(linux.config))
    linux.fleet_enabled = True
    asked = ["systemctl", "--user", "is-enabled", fleet.FLEET_UNIT]
    enabled_again = False

    def run(argv: list[str], **more: Any) -> subprocess.CompletedProcess[str]:
        nonlocal enabled_again
        if enabled_again and argv == asked:
            return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus: stand-in")
        finished = linux(argv, **more)
        enabled_again = enabled_again or argv[2:4] == ["enable", "--now"]
        return finished

    result = fleet.ensure_here(
        write=True, want="0.14.0", standalone=True, run_=run, handshake_=answers
    )
    row = result["steps"][2]
    assert enabled_again and linux.fleet_enabled and row["state"] == "fail"
    assert "is not enabled again" not in row["detail"]
    assert row["detail"].endswith(
        "was disabled by this run; whether it is enabled again cannot be told "
        "(Failed to connect to bus: stand-in)"
    )


def test_the_disable_of_our_unit_names_the_count_where_the_bridge_ran_and_did_not_accept(
    linux: FakeBridge,
) -> None:
    """T-155, B, the path whose first command is the `disable --now` of our unit: the status
    read before it says running and not accepting, so the disable may stop a bridge. red_before."""
    _arrange(linux, "not running", "own start fails")
    target = fleet._units() / fleet.FLEET_UNIT
    target.write_text(marker(linux.config) + "\n" + unit_text(linux.config))
    linux.fleet_enabled = linux.running = True
    linux.failing = {"enable"}
    linux.draining = 1000
    row = _ensure(linux)
    assert ["systemctl", "--user", "disable", "--now", fleet.FLEET_UNIT] in linux.calls
    assert row["state"] == "fail" and COST in row["detail"]
