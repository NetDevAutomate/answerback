#!/usr/bin/env python3
"""answerback-fleet: survey machines and code harnesses, then install Answerback skills on them.

One file, standard library only, Python 3.9+, so the same code runs on the coordinator and on
every LAN node. The coordinator only transports: it sends this file (and, for an install, the
skill folder) to a node and that node's own interpreter decides what is safe to write there.

Every command reads. Only ``apply --yes`` and ``ensure --yes`` write. A look by ``ensure`` also
starts programs: ``answerback --version`` and ``answerback status``, ``hc machine ls``, queries to
``systemctl --user`` (what is enabled and active), the bridge's two installers as previews, once
the command of each entry the installer reports as registered, and for each chosen node a call
through ``hc`` that runs this file there as a look. The list is in references/fleet.md ("What a
look starts"). An entry that the installer refused (it differs from what the installer would
write) is not tried.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

VERSION = "0.4.0"
SKILL_NAME = "answerback-fleet"
USAGE_SKILL = "answerback-collaboration"
MANIFEST = ".answerback-fleet.json"
SHARED = ".agents/skills"
BEGIN, END = "@@answerback-fleet-json-begin", "@@answerback-fleet-json-end"
MAX_MACHINES = 16
NODE_COMMANDS = ("probe", "node-install", "node-ensure")


# --------------------------------------------------------------------------- harnesses


@dataclasses.dataclass(frozen=True)
class Harness:
    """Where one code harness keeps skills, and what the bridge calls it."""

    id: str
    label: str
    binaries: tuple[str, ...]
    user_dir: str | None  # relative to the home directory
    project_dir: str | None  # relative to the project root
    reads_shared_user: bool  # reads ~/.agents/skills without a link
    reads_shared_project: bool  # reads <project>/.agents/skills without a link
    bridge_client: str | None  # name for `answerback clients|skill install --client`
    native_install: tuple[str, ...] = ()  # the harness's own skill installer, source path appended
    agent_dir: str | None = None  # user-scope custom agents (information only)


HARNESSES: tuple[Harness, ...] = (
    Harness(
        "claude",
        "Claude Code",
        ("claude",),
        ".claude/skills",
        ".claude/skills",
        False,
        False,
        "claude",
        agent_dir=".claude/agents",
    ),
    Harness(
        "codex",
        "Codex",
        ("codex",),
        ".codex/skills",
        ".agents/skills",
        True,
        True,
        "codex",
        agent_dir=".codex/agents",
    ),
    Harness(
        "kiro",
        "Kiro",
        ("kiro-cli",),
        ".kiro/skills",
        ".kiro/skills",
        False,
        False,
        "kiro",
        agent_dir=".kiro/agents",
    ),
    Harness(
        "grok",
        "Grok Build",
        ("grok",),
        ".grok/skills",
        ".grok/skills",
        False,
        False,
        "grok",
        agent_dir=".grok/agents",
    ),
    Harness(
        "opencode",
        "OpenCode",
        ("opencode",),
        ".config/opencode/skills",
        ".opencode/skills",
        True,
        True,
        None,
        agent_dir=".config/opencode/agents",
    ),
    Harness(
        "cursor",
        "Cursor Agent",
        ("cursor-agent",),
        ".cursor/skills",
        ".cursor/skills",
        True,
        True,
        None,
        agent_dir=".cursor/agents",
    ),
)
HARNESS_BY_ID = {h.id: h for h in HARNESSES}


class FleetError(Exception):
    """A refusal or a failed step, with a message fit to show the user."""


# --------------------------------------------------------------------------- running things

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def run(argv: list[str], timeout: float = 30, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a program without a shell. A missing program or a timeout is a failed result, not a crash."""
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]}: timed out after {timeout:g}s")


def _results_only(run_: Run) -> Run:
    """The runner of `ensure`: a program that cannot be started at all (an OSError) is a failed
    command with the error as its text, so that what follows it is still observed and reported."""
    if getattr(run_, "results_only", False):
        return run_

    def results_only(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        try:
            return run_(argv, *args, **kwargs)
        except OSError as error:
            return subprocess.CompletedProcess(argv, 126, "", f"{argv[0]}: {error}")

    results_only.results_only = True  # type: ignore[attr-defined]
    return results_only


def which(name: str) -> str | None:
    """Find a program the way a login shell would, since harnesses often live in ~/.local/bin."""
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    for extra in (
        home / ".local/bin",
        home / ".local/share/mise/shims",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        home / ".grok/bin",
        home / ".cargo/bin",
    ):
        candidate = extra / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# --------------------------------------------------------------------------- skill content


def tree_hash(root: Path) -> str | None:
    """One hash for a skill folder: every file's path and bytes, the manifest and caches left out."""
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == MANIFEST or "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        digest.update(rel.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def pack(root: Path) -> str:
    """The skill folder as base64 text of a tar.gz, safe to send through a text pipe."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            if rel == MANIFEST or "__pycache__" in path.parts or path.name == ".DS_Store":
                continue
            archive.add(str(path), arcname=rel, recursive=False)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def unpack(payload: str, dest: Path) -> None:
    """Extract a packed skill. Refuses any member that is not a plain file or folder inside dest."""
    dest.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload)), mode="r:gz") as archive:
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if not (member.isfile() or member.isdir()) or dest.resolve() not in (target, *target.parents):
                raise FleetError(f"refused archive member: {member.name}")
        for member in archive.getmembers():
            target = dest / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
            target.chmod(0o755 if member.mode & 0o100 else 0o644)


def default_source() -> Path:
    """The skill folder this script sits in: <skill>/scripts/fleet.py."""
    return Path(__file__).resolve().parent.parent


def read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((path / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("skill") == SKILL_NAME else None


# --------------------------------------------------------------------------- survey (one machine)


def dir_state(path: Path) -> dict[str, Any]:
    """What is at a skills directory: nothing, a folder, or a link (and where it really is)."""
    if path.is_symlink():
        try:
            real = str(path.resolve(strict=True))
        except (OSError, RuntimeError):
            return {"kind": "broken-link", "target": os.readlink(path)}
        return {"kind": "link", "target": os.readlink(path), "real": real}
    if path.is_dir():
        return {"kind": "dir", "real": str(path.resolve())}
    return {"kind": "file" if path.exists() else "missing"}


def skill_state(path: Path) -> dict[str, Any]:
    """What is at one skill's destination."""
    if path.is_symlink():
        target = os.readlink(path)
        ok = path.exists()
        return {
            "kind": "link" if ok else "broken-link",
            "target": target,
            "hash": tree_hash(path) if ok else None,
        }
    if path.is_dir():
        manifest = read_manifest(path)
        return {
            "kind": "dir",
            "managed": manifest is not None,
            "hash": tree_hash(path),
            "version": (manifest or {}).get("version"),
        }
    return {"kind": "file" if path.exists() else "missing"}


def bridge_state(run_: Run = run) -> dict[str, Any]:
    """Is the Answerback CLI here, which version, and is its bridge service running."""
    binary = which("answerback")
    if not binary:
        return {"installed": False}
    version = run_([binary, "--version"], timeout=15)
    status = run_([binary, "status"], timeout=20)
    state: dict[str, Any] = {"installed": True, "path": binary, "version": version.stdout.strip() or None}
    try:
        parsed = json.loads(status.stdout)
        if not isinstance(parsed, dict):
            raise ValueError("the status is not a mapping")
        state.update(running=bool(parsed.get("running")), state=parsed.get("state"))
    except ValueError:
        state.update(running=None, state="unknown")
    return state


def probe(home: Path | None = None, run_: Run = run) -> dict[str, Any]:
    """Everything the planner needs to know about this machine. Reads only."""
    home = home or Path.home()
    harnesses: dict[str, Any] = {}
    for harness in HARNESSES:
        binary = next((found for found in (which(b) for b in harness.binaries) if found), None)
        entry: dict[str, Any] = {"installed": binary is not None, "binary": binary}
        if harness.user_dir:
            base = home / harness.user_dir
            entry["skills_dir"] = dir_state(base)
            entry["skills"] = {name: skill_state(base / name) for name in (SKILL_NAME, USAGE_SKILL)}
        if harness.agent_dir:
            entry["agents_dir"] = (home / harness.agent_dir).is_dir()
        harnesses[harness.id] = entry
    shared = home / SHARED
    return {
        "fleet_version": VERSION,
        "os": os.uname().sysname,
        "host": os.uname().nodename,
        "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
        "home": str(home),
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "tools": {name: which(name) for name in ("uv", "git", "hc")},
        "shared": {
            "skills_dir": dir_state(shared),
            "skills": {name: skill_state(shared / name) for name in (SKILL_NAME, USAGE_SKILL)},
        },
        "harnesses": harnesses,
        "bridge": bridge_state(run_),
    }


# --------------------------------------------------------------------------- plan (one machine)


@dataclasses.dataclass
class Action:
    """One step of an install on one machine. `op` says what apply will do with it."""

    op: str  # copy | link | native | bridge | skip | refuse | unchanged
    harness: str
    path: str = ""
    target: str = ""
    note: str = ""
    argv: list[str] = dataclasses.field(default_factory=list)
    detail: Any = None

    @property
    def writes(self) -> bool:
        return self.op in ("copy", "link", "native") or (self.op == "bridge" and "--apply" in self.argv)


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _place(dest: Path, *, link_to: Path | None, expect: str, force: bool, harness: str) -> Action:
    """Decide what to do at one destination. A real file or folder we did not write is never replaced."""
    state = skill_state(dest)
    kind = state["kind"]
    if link_to is not None:
        # Measured from where the entry really lands: a skills directory can itself be a link.
        wanted = os.path.relpath(link_to.parent.resolve() / link_to.name, dest.parent.resolve())
        if kind == "missing":
            return Action("link", harness, str(dest), wanted)
        if kind in ("link", "broken-link"):
            same = kind == "link" and dest.resolve() == link_to.resolve()
            if same:
                return Action("unchanged", harness, str(dest), state["target"])
            if force:
                return Action("link", harness, str(dest), wanted, note=f"replaces link to {state['target']}")
            return Action(
                "refuse",
                harness,
                str(dest),
                note=f"a link to {state['target']} is there; --force replaces it",
            )
        if kind == "dir" and dest.resolve() == link_to.resolve():
            # A folder cannot be a link to itself: the script runs from the entry it would link to.
            return Action("unchanged", harness, str(dest), note="the skill runs from this entry")
        return Action("refuse", harness, str(dest), note="a real file or folder is there; move it away first")
    if kind == "missing":
        return Action("copy", harness, str(dest))
    if kind == "dir" and state.get("managed"):
        if state["hash"] == expect:
            return Action("unchanged", harness, str(dest))
        return Action(
            "copy", harness, str(dest), note=f"updates the copy this tool wrote ({state.get('version')})"
        )
    if kind in ("link", "broken-link"):
        if force:
            return Action("copy", harness, str(dest), note=f"replaces link to {state['target']}")
        return Action(
            "refuse", harness, str(dest), note=f"a link to {state['target']} is there; --force replaces it"
        )
    return Action(
        "refuse", harness, str(dest), note="a folder this tool did not write is there; move it away first"
    )


def plan(
    *,
    source: Path,
    scope: str = "user",
    mode: str = "canonical",
    link_source: bool = False,
    harnesses: Iterable[str] | None = None,
    project: Path | None = None,
    home: Path | None = None,
    force: bool = False,
    bridge: str = "preview",
    min_bridge: str | None = None,
    run_: Run = run,
    native: bool = True,
) -> dict[str, Any]:
    """What an install would do on this machine. Reads only.

    link_source: the shared entry is a link to `source` (the repo stays the one copy). Otherwise it
    is a copy that carries a manifest, which is how a node without the repo gets the skill.
    native: a harness's own installer may be given `source`. A node passes False: its source is a
    temporary folder that is gone after the run.
    """
    home = home or Path.home()
    expect = tree_hash(source)
    if expect is None or not (source / "SKILL.md").is_file():
        raise FleetError(f"no skill at {source}")
    if scope == "project":
        if project is None or not project.is_dir():
            raise FleetError("project scope needs --project with an existing folder")
        if link_source:
            raise FleetError(
                "project scope always copies: a link out of the project breaks on every other checkout"
            )
        base = project
    else:
        base = home
    facts = probe(home, run_)
    wanted = list(harnesses) if harnesses else [h for h, e in facts["harnesses"].items() if e["installed"]]
    unknown = [h for h in wanted if h not in HARNESS_BY_ID]
    if unknown:
        raise FleetError(f"unknown harness: {', '.join(unknown)}")
    actions: list[Action] = []
    shared_dest = base / SHARED / SKILL_NAME
    primary = source if link_source else None  # what the first real entry points at

    def first(dest: Path, harness: str) -> Action:
        return _place(dest, link_to=primary, expect=expect, force=force, harness=harness)

    shared_needed = mode == "canonical" and any(HARNESS_BY_ID[h].native_install == () for h in wanted)
    if shared_needed:
        if link_source and _inside(shared_dest.parent, source):
            raise FleetError("the shared directory is inside the skill's own folder")
        actions.append(first(shared_dest, "shared"))
    shared_ok = shared_needed and actions[0].op != "refuse"

    for hid in wanted:
        harness = HARNESS_BY_ID[hid]
        entry = facts["harnesses"][hid]
        if not entry["installed"]:
            actions.append(Action("skip", hid, note=f"{harness.label} is not installed here"))
            continue
        if harness.native_install:
            if scope == "project":
                actions.append(Action("skip", hid, note=f"{harness.label} has no project scope"))
            elif not native:
                note = f"{harness.label}'s own installer runs on the coordinator only: install it there"
                actions.append(Action("skip", hid, note=note))
            else:
                argv = [entry["binary"], *harness.native_install[1:], str(source)]
                actions.append(Action("native", hid, argv=argv, note=f"{harness.label}'s own installer"))
            continue
        rel = harness.user_dir if scope == "user" else harness.project_dir
        assert rel is not None
        directory = base / rel
        dest = directory / SKILL_NAME
        reads_shared = harness.reads_shared_user if scope == "user" else harness.reads_shared_project
        if mode == "harness":
            actions.append(first(dest, hid))
            continue
        if not shared_ok:
            actions.append(Action("refuse", hid, str(dest), note="the shared entry was refused"))
        elif directory.exists() and directory.resolve() == shared_dest.parent.resolve():
            actions.append(Action("skip", hid, str(dest), note=f"{rel} is the shared directory"))
        elif reads_shared:
            actions.append(Action("skip", hid, note=f"{harness.label} reads {SHARED} itself"))
        else:
            action = _place(dest, link_to=shared_dest, expect=expect, force=force, harness=hid)
            state = dir_state(directory)
            if state["kind"] == "link" and action.op == "link":
                action.note = (
                    action.note + "; " if action.note else ""
                ) + f"{rel} is a link to {state['target']}"
            actions.append(action)

    if scope == "project":
        for action in actions:
            if action.op in ("copy", "link") and _git_ignored(project, Path(action.path), run_):
                action.note = (
                    action.note + "; " if action.note else ""
                ) + "ignored by git: will not travel with the repo"
    if scope == "user" and bridge != "none":
        actions.extend(_bridge_actions(facts, wanted, bridge == "apply", run_, min_bridge))
    return {
        "machine": facts["host"],
        "scope": scope,
        "mode": mode,
        "source": str(source),
        "hash": expect,
        "source_kind": "link" if link_source else "copy",
        "facts": facts,
        "actions": [dataclasses.asdict(a) for a in actions],
        "refused": sum(a.op == "refuse" for a in actions),
        "writes": sum(a.writes for a in actions),
    }


def _git_ignored(project: Path | None, path: Path, run_: Run) -> bool:
    git = which("git")
    if not git or project is None:
        return False
    return run_([git, "-C", str(project), "check-ignore", "-q", str(path)], timeout=10).returncode == 0


def version_tuple(text: str | None) -> tuple[int, ...]:
    """'0.14.0+internal' -> (0, 14, 0). Anything unreadable sorts lowest."""
    numbers = re.match(r"\s*(\d+(?:\.\d+)*)", text or "")
    return tuple(int(part) for part in numbers.group(1).split(".")) if numbers else ()


def _bridge_actions(
    facts: dict[str, Any], wanted: list[str], apply_: bool, run_: Run, min_bridge: str | None = None
) -> list[Action]:
    """The bridge's own installers register its MCP connection and usage skill. We call them, never copy."""
    bridge = facts["bridge"]
    if not bridge.get("installed"):
        return [Action("skip", "bridge", note="answerback is not installed here; install the bridge first")]
    if apply_ and min_bridge and version_tuple(bridge.get("version")) < version_tuple(min_bridge):
        # An older installer may lay things out differently. Fail closed; the owner upgrades the bridge.
        note = f"bridge {bridge.get('version')} is older than {min_bridge}: its installers are not run"
        return [Action("refuse", "bridge", note=note)]
    clients = [
        HARNESS_BY_ID[h].bridge_client
        for h in wanted
        if HARNESS_BY_ID[h].bridge_client and facts["harnesses"][h]["installed"]
    ]
    if not clients:
        return []
    flags = [flag for client in clients for flag in ("--client", client)]
    note = "preview only" if not apply_ else "writes"
    if not bridge.get("running"):
        note += "; the bridge service is not running on this machine"
    actions = []
    for kind in ("clients", "skill"):
        argv = [bridge["path"], kind, "install", *flags]
        preview = _bridge_summary(run_(argv, timeout=60).stdout)  # the installers preview without --apply
        actions.append(
            Action("bridge", "bridge", argv=argv + (["--apply"] if apply_ else []), note=note, detail=preview)
        )
    return actions


# --------------------------------------------------------------------------- apply (one machine)


def _left_behind(staging: Path) -> str:
    """Remove a staging folder; say so when it is still there afterwards."""
    shutil.rmtree(staging, ignore_errors=True)
    return f"the staging folder {staging} could not be removed" if _is_there(staging) != "no" else ""


def _is_there(path: Path) -> str:
    """Ask the file system whether something is at path: `yes`, `no`, or the error it gave.

    `os.path.lexists` says False for every error; "it could not be read" is not "it is not there".
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "no"
    except OSError as error:
        return str(error) or type(error).__name__
    return "yes"


def _put_back(dest: Path, staging: Path) -> tuple[str, str]:
    """After a failure or a stop in `_write_copy`: put the previous entry back, then tidy.

    Returns what became of the previous entry ("" when it is not in the staging folder) and what
    `_left_behind` says. Where the previous entry is, is asked of the file system and not kept in
    a variable, so it is right wherever the stop came. `old` and `new` both in the staging folder
    mean that the previous entry was moved aside and the new copy is not in its place: it is put
    back by one rename. The staging folder is removed only when the file system says that `old`
    is not in it, or that `old` is and `new` is not (the new copy is in its place). When the file
    system does not answer for one of the two, nothing is moved and nothing is removed.
    A second stop while the previous entry is put back, or while the two questions are asked,
    goes on as it is; a line on standard error names `old` when it is still in the staging folder
    or the file system does not say.
    """
    old = staging / "old"

    def stopped_again() -> None:
        # Stopped a second time: nothing more is moved or removed.
        if _is_there(old) != "no":
            said = f"fleet: stopped while putting back {dest}; the previous copy is kept at {old}"
            print(said, file=sys.stderr)

    try:
        aside, waiting = _is_there(old), _is_there(staging / "new")
    except BaseException:
        stopped_again()
        raise
    if aside == "no" or (aside, waiting) == ("yes", "no"):
        return "", _left_behind(staging)
    if (aside, waiting) != ("yes", "yes"):
        error = waiting if aside == "yes" else aside
        unread = f"; it could not be read where the previous one is ({error})"
        return f"{unread}; nothing was removed, look at {old}", ""
    try:
        os.replace(old, dest)
    except OSError as again:
        kept = f", and the previous one could not be put back ({again})"
        return f"{kept}; the previous copy is kept at {old}", ""
    except BaseException:
        stopped_again()
        raise
    return "; the previous one is back in place", _left_behind(staging)


def _write_copy(dest: Path, source: Path, expect: str) -> str:
    """Stage beside the destination, check the bytes, then move into place in one step.

    The previous entry, a folder or a link, is moved aside into the staging folder by one rename.
    Until the new copy is in its place it is in one of two places: the destination, or `old` in the
    staging folder. A staging folder is removed only when the file system says that the previous
    entry is not in it or that the new copy is in its place. When a step fails or the run is
    stopped, the previous entry is put back by one rename; when that fails, or the file system
    does not say where things are, it stays in the staging folder and its path is named.
    A failure leaves as a `FleetError` that says where the previous entry is and names a staging
    folder that is still there. Anything else that stops the run goes on as it is, with one line
    on standard error when the previous entry was moved or something is left. A stop while the
    staging folder is removed after the new copy is in its place goes on as it is too, with one
    line that names the staging folder when it is still there. After a second stop while the
    previous entry is put back, a line is written only when `old` is still in the staging folder
    or the file system does not say; a staging folder that holds only the new copy may stay
    without a word. Only a failure to make the staging folder leaves as the `OSError` it is:
    nothing was touched then.
    Returns the result of the step: `written`, and a staging folder that could not be removed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.", dir=str(dest.parent)))
    old, staged = staging / "old", staging / "new"
    try:
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", MANIFEST))
        if tree_hash(staged) != expect:
            raise FleetError(f"staged copy for {dest} does not match the source")
        (staged / MANIFEST).write_text(json.dumps({"skill": SKILL_NAME, "version": VERSION}) + "\n")
        if os.path.lexists(dest):
            if not dest.is_symlink() and read_manifest(dest) is None:
                raise FleetError(f"{dest} changed since the plan; nothing was replaced")
            os.replace(dest, old)  # a link is renamed as a link, so its text is kept
        os.replace(staged, dest)
    except BaseException as stop:
        previous, left = _put_back(dest, staging)
        if left:
            left = f"; {left}"
        if isinstance(stop, FleetError):
            if previous or left:
                raise FleetError(f"{stop}{previous}{left}") from stop
            raise
        if isinstance(stop, OSError):
            if not previous:
                # Neither is said when the file system does not answer for the destination.
                there = _is_there(dest)
                said = {"yes": "; nothing was replaced", "no": "; nothing was there before"}
                previous = said.get(there, "")
            failed = f"the new copy could not be put at {dest} ({stop})"
            raise FleetError(f"{failed}{previous}{left}") from stop
        if previous or left:
            print(f"fleet: stopped while updating {dest}{previous}{left}", file=sys.stderr)
        raise
    try:
        left = _left_behind(staging)
    except BaseException:
        # The new copy is in its place; stopped while tidying. Nothing more is removed.
        if _is_there(staging) != "no":
            still = f"the staging folder {staging} is still there"
            print(f"fleet: stopped after updating {dest}; {still}", file=sys.stderr)
        raise
    return f"written; {left}" if left else "written"


def apply_plan(planned: dict[str, Any], *, run_: Run = run) -> dict[str, Any]:
    """Carry out a plan made on this machine. Stops before writing anything if a step was refused."""
    if planned["refused"]:
        raise FleetError("the plan has refused steps; resolve them, nothing was written")
    source, expect = Path(planned["source"]), planned["hash"]
    if tree_hash(source) != expect:
        raise FleetError("the skill changed since the plan; plan again")
    done: list[dict[str, Any]] = []
    for raw in planned["actions"]:
        action = Action(**raw)
        result = dict(raw)
        if action.op == "copy":
            result["result"] = _write_copy(Path(action.path), source, expect)
        elif action.op == "link":
            dest = Path(action.path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink():
                dest.unlink()
            elif dest.exists():
                raise FleetError(f"{dest} changed since the plan; nothing was replaced")
            os.symlink(action.target, dest)
            result["result"] = "linked"
        elif action.op == "bridge" and "--apply" not in action.argv:
            result["result"] = "previewed"
        elif action.op in ("native", "bridge"):
            finished = run_(action.argv, timeout=120)
            result["result"] = "ok" if finished.returncode == 0 else f"failed ({finished.returncode})"
            result["output"] = (
                _bridge_summary(finished.stdout) if action.op == "bridge" else finished.stdout[-400:]
            )
            if finished.returncode != 0:
                result["error"] = finished.stderr.strip()[-400:]
        else:
            result["result"] = action.op
        done.append(result)
    return {"machine": planned["machine"], "actions": done, "verify": verify(planned)}


def plan_remove(
    *,
    scope: str = "user",
    project: Path | None = None,
    home: Path | None = None,
    source: Path | None = None,
) -> dict[str, Any]:
    """What an uninstall would take away here: links to this skill and copies carrying our manifest."""
    home = home or Path.home()
    if scope == "project" and (project is None or not project.is_dir()):
        raise FleetError("project scope needs --project with an existing folder")
    base = project if scope == "project" and project else home
    shared = base / SHARED / SKILL_NAME
    ours = {shared.resolve()} | ({source.resolve()} if source else set())
    actions: list[Action] = []
    seen: set[str] = set()
    places = [(h.id, h.user_dir if scope == "user" else h.project_dir) for h in HARNESSES]
    for hid, rel in [*places, ("shared", SHARED)]:  # links first, the entry they point at last
        if rel is None:
            if scope == "user" and which(HARNESS_BY_ID[hid].binaries[0]):
                actions.append(Action("skip", hid, note="remove it with the harness's own command"))
            continue
        dest = base / rel / SKILL_NAME
        key = str(dest.parent.resolve() / dest.name)
        if key in seen:
            continue
        seen.add(key)
        if dest.is_symlink():
            real = dest.resolve()
            if real in ours or read_manifest(real) is not None or not dest.exists():
                actions.append(Action("remove", hid, str(dest), os.readlink(dest)))
            else:
                actions.append(
                    Action("skip", hid, str(dest), note=f"a link to {os.readlink(dest)}; not ours")
                )
        elif dest.is_dir():
            if read_manifest(dest) is not None:
                actions.append(Action("remove", hid, str(dest), note="the copy this tool wrote"))
            else:
                actions.append(Action("skip", hid, str(dest), note="a folder this tool did not write"))
    return {
        "machine": os.uname().nodename,
        "scope": scope,
        "mode": "remove",
        "source_kind": "uninstall",
        "actions": [dataclasses.asdict(a) for a in actions],
        "refused": 0,
        "writes": sum(a.op == "remove" for a in actions),
    }


def apply_remove(planned: dict[str, Any]) -> dict[str, Any]:
    """Take away what plan_remove listed, checking each entry again first."""
    done = []
    for raw in planned["actions"]:
        result = dict(raw)
        if raw["op"] == "remove":
            dest = Path(raw["path"])
            if dest.is_symlink():
                dest.unlink()
                result["result"] = "removed"
            elif dest.is_dir() and read_manifest(dest) is not None:
                shutil.rmtree(dest)
                result["result"] = "removed"
            else:
                result["result"] = "left: changed since the plan"
        else:
            result["result"] = raw["op"]
        done.append(result)
    return {"machine": planned["machine"], "actions": done, "verify": []}


def _bridge_summary(stdout: str) -> Any:
    """The bridge installers print long JSON; keep the verdict and each target's status."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout.strip()[-400:]
    targets = [
        {
            k: t.get(k)
            for k in ("skill", "client", "status", "would_change", "error", "note")
            if t.get(k) is not None
        }
        for t in data.get("targets") or data.get("clients") or []
        if isinstance(t, dict)
    ]
    return {"success": data.get("success"), "applied": data.get("applied"), "targets": targets}


def verify(planned: dict[str, Any]) -> list[dict[str, Any]]:
    """Read every destination back and compare it with the skill that was planned."""
    checks = []
    for raw in planned["actions"]:
        if raw["op"] not in ("copy", "link", "unchanged") or not raw["path"]:
            continue
        actual = tree_hash(Path(raw["path"]))
        checks.append({"harness": raw["harness"], "path": raw["path"], "ok": actual == planned["hash"]})
    return checks


# --------------------------------------------------------------------------- fleet (coordinator)


def machines(run_: Run = run) -> dict[str, Any]:
    """LAN machines the terminal host knows. No host, or none listed, means local-only."""
    hc = which("hc")
    if not hc:
        return {"mode": "local-only", "reason": "hc is not installed", "machines": []}
    listed = run_([hc, "machine", "ls", "--json"], timeout=20)
    try:
        found = json.loads(listed.stdout).get("machines", [])
    except (ValueError, AttributeError):
        return {"mode": "local-only", "reason": "hc machine ls gave no list", "machines": []}
    nodes = [
        {"name": m.get("name"), "host": m.get("host"), "state": m.get("state")}
        for m in found
        if isinstance(m, dict) and m.get("name")
    ]
    if not nodes:
        return {"mode": "local-only", "reason": "no other machine is registered", "machines": []}
    looked = nodes[: MAX_MACHINES - 1]
    return {"mode": "fleet", "machines": looked, "beyond": [m["name"] for m in nodes[len(looked) :]]}


def on_node(
    machine: str, args: list[str], *, payload: str | None = None, timeout: int = 90, run_: Run = run
) -> dict[str, Any]:
    """Run this very file on a node with that node's python3 and return the JSON it prints.

    The file travels on stdin, so a preview leaves nothing behind on the node. Its output is another
    machine's text: only the JSON between our two markers is read, and only as data.
    """
    hc = which("hc")
    if not hc:
        raise FleetError("hc is not installed")
    script = Path(__file__).read_text()
    envelope = json.dumps({"script": script, "argv": [*args, "--json"], "payload": payload})
    loader = (
        "import json,sys;e=json.load(sys.stdin);sys.argv=['fleet.py']+e['argv'];"
        "g={'__name__':'__main__','__file__':'<fleet>','FLEET_PAYLOAD':e['payload']};"
        "exec(compile(e['script'],'fleet.py','exec'),g)"
    )
    finished = run_(
        [
            hc,
            "exec",
            "--on",
            machine,
            "--timeout",
            str(timeout),
            "-i",
            "--",
            "sh",
            "-lc",
            f'exec python3 -c "{loader}"',
        ],
        timeout=timeout + 15,
        stdin=envelope,
    )
    text = finished.stdout
    if BEGIN not in text or END not in text:
        tail = (finished.stderr or text).strip()[-300:]
        raise FleetError(f"{machine}: no answer ({finished.returncode}): {tail}")
    return json.loads(text.split(BEGIN, 1)[1].split(END, 1)[0])


def capacity(names: list[str], run_: Run = run) -> dict[str, Any]:
    """Ask the bridge for each machine's capacity state. Unknown is a state: place nothing there."""
    binary = which("answerback")
    if not binary:
        return {}
    argv = [binary, "capacity", "--json", "--timeout", "15"]
    for name in names:
        argv += ["--machine", name]
    finished = run_(argv, timeout=20 * (len(names) + 1))
    try:
        rows = json.loads(finished.stdout).get("machines", [])
    except (ValueError, AttributeError):
        return {}
    return {
        row.get("machine"): {
            "state": row.get("state"),
            "load_per_core": row.get("load_per_core"),
            "reasons": row.get("reasons", []),
        }
        for row in rows
        if isinstance(row, dict)
    }


BEYOND = f"more than {MAX_MACHINES - 1} machines are registered; this one was not looked at"


def select(fleet: dict[str, Any], names: list[str] | None) -> tuple[bool, list[str], list[dict[str, str]]]:
    """Which machines a command covers: (local?, nodes, left out with the reason)."""
    known = {m["name"]: m for m in fleet["machines"]}
    beyond = [str(name) for name in fleet.get("beyond", [])]
    if not names:
        return True, [], []
    local = "local" in names
    picked = [*known, *beyond] if "all" in names else [n for n in names if n != "local"]
    if "all" in names:
        local = True
    nodes, left = [], []
    for name in picked:
        if name in beyond and name not in known:
            left.append({"machine": name, "reason": BEYOND})
        elif name not in known:
            left.append({"machine": name, "reason": "not registered with hc"})
        elif known[name]["state"] != "connected":
            left.append({"machine": name, "reason": f"state is {known[name]['state']}"})
        else:
            nodes.append(name)
    return local, nodes, left


def survey(names: list[str] | None = None, run_: Run = run) -> dict[str, Any]:
    """Every machine, its harnesses, its bridge and its capacity."""
    fleet = machines(run_)
    _, nodes, left = select(fleet, names or ["all"])
    rows: dict[str, Any] = {"local": probe(run_=run_)}
    for name in nodes:
        try:
            rows[name] = on_node(name, ["probe"], run_=run_)
        except (FleetError, ValueError) as error:
            left.append({"machine": name, "reason": str(error)})
    load = capacity(nodes, run_)
    for name, row in rows.items():
        row["capacity"] = load.get(name, {"state": "unknown"})
    return {
        "mode": fleet["mode"],
        "reason": fleet.get("reason"),
        "machines": rows,
        "left_out": left,
        "drift": drift(rows),
    }


def drift(rows: dict[str, Any]) -> list[str]:
    """Differences between machines that make one skill behave differently across the fleet."""
    notes = []
    versions = {
        name: row["bridge"].get("version") for name, row in rows.items() if row["bridge"].get("installed")
    }
    if len(set(versions.values())) > 1:
        notes.append("bridge versions differ: " + ", ".join(f"{n} {v}" for n, v in sorted(versions.items())))
    hashes = {
        name: (row["shared"]["skills"][SKILL_NAME].get("hash") or "")[:12]
        for name, row in rows.items()
        if row["shared"]["skills"][SKILL_NAME].get("hash")
    }
    if len(set(hashes.values())) > 1:
        notes.append(f"{SKILL_NAME} differs: " + ", ".join(f"{n} {h}" for n, h in sorted(hashes.items())))
    for name, row in rows.items():
        if not row["bridge"].get("installed"):
            notes.append(f"{name}: answerback is not installed")
        elif not row["bridge"].get("running"):
            notes.append(f"{name}: the bridge service is not running")
        for hid, entry in row["harnesses"].items():
            state = entry.get("skills_dir", {})
            if state.get("kind") == "link" and state.get("real") != row["shared"]["skills_dir"].get("real"):
                notes.append(
                    f"{name}: {HARNESS_BY_ID[hid].user_dir} is a link to {state['target']}, not to {SHARED}"
                )
    return notes


def pool(surveyed: dict[str, Any]) -> list[dict[str, Any]]:
    """Machines that may take work now. A measurement for the usage skill's placement rule, not a store."""
    rows = []
    for name, row in surveyed["machines"].items():
        harnesses = sorted(h for h, e in row["harnesses"].items() if e["installed"])
        state = row["capacity"].get("state", "unknown")
        reasons = []
        if state != "ok":
            reasons.append(f"capacity {state}")
        if not harnesses:
            reasons.append("no harness")
        if not row["bridge"].get("installed"):
            reasons.append("no answerback")
        rows.append(
            {
                "machine": name,
                "eligible": not reasons,
                "coordinator": name == "local",
                "capacity": state,
                "load_per_core": row["capacity"].get("load_per_core"),
                "harnesses": harnesses,
                "why_not": reasons,
            }
        )
    return rows


# --------------------------------------------------------------------------- ensure (bridge on a machine)

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": SKILL_NAME, "version": VERSION},
    },
}


def handshake(command: list[str], timeout: float = 20) -> tuple[bool, str]:
    """Start a registered MCP entry the way a harness would and ask it to initialize.

    This is the check that a registration works: the entry's program starts, reaches its bridge and
    answers. A matching config file proves none of that.
    """
    try:
        child = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except OSError as error:
        return False, f"cannot start: {error}"
    try:
        out, err = child.communicate(json.dumps(INIT) + "\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        out, err = child.communicate()
    for line in out.splitlines():
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get("id") == 1:
            if "result" in answer:
                result = answer["result"]
                info = result.get("serverInfo") if isinstance(result, dict) else None
                if not isinstance(info, dict):
                    return False, "the answer to initialize has no result with serverInfo"
                return True, f"{info.get('name', 'server')} {info.get('version', '')}".strip()
            error = answer.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            return False, str(message or "initialize refused")[:200]
    return False, (err.strip() or out.strip() or "no answer")[-200:]


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _status(binary: str, run_: Run) -> dict[str, Any]:
    return _json(run_([binary, "status"], timeout=20).stdout)


def _failure(finished: subprocess.CompletedProcess[str]) -> str:
    return str(_json(finished.stdout).get("error") or finished.stderr.strip() or finished.stdout.strip())[
        -300:
    ]


STANDALONE_UNIT = "answerback-bridge-standalone"
FLEET_UNIT = "answerback-bridge-fleet.service"


def _units() -> Path:
    return Path.home() / ".config/systemd/user"


def _systemctl(args: list[str], run_: Run) -> subprocess.CompletedProcess[str]:
    return run_(["systemctl", "--user", *args], timeout=30)


STAGED_TRIES = 8


def _staged_name(attempt: int) -> str:
    """The name of a staged file for the try with this number: the one source of such names."""
    return f".fleet-{os.getpid()}-{os.urandom(8).hex()}"


def _staged_file(parent: str, dir_fd: int | None = None) -> tuple[int, str]:
    """Create a staged file that is new: never one that is there, never through a link (O_EXCL,
    O_NOFOLLOW), mode 0600 from its first moment. In `parent`, or with `dir_fd` in that open
    folder. A name that is there already is left as it is and the next name is taken."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for attempt in range(STAGED_TRIES):
        name = os.path.join(parent, _staged_name(attempt))
        try:
            return os.open(name, flags, 0o600, dir_fd=dir_fd), name
        except FileExistsError:
            continue
    raise OSError(f"no free name for a staged file after {STAGED_TRIES} tries")


def _private_write(dest: Path, data: bytes, dir_fd: int | None = None) -> None:
    """Write a file that is this user's alone, without following what is at the destination.

    The bytes go to a new staged file (`_staged_file`) beside the destination and one rename puts it
    in place, so a link, a second name or a staged file that someone else left there is never
    written through. A folder at the destination is an error. With `dir_fd`, the file is created
    and renamed inside that open folder under the last name of `dest`: a folder that is exchanged
    on the path afterwards changes nothing.
    """
    name = os.fspath(dest) if dir_fd is None else dest.name
    try:
        try:
            if os.lstat(name, dir_fd=dir_fd).st_mode & 0o170000 == 0o040000:
                raise FleetError(f"{dest} is a folder; nothing was written")
        except FileNotFoundError:
            pass
        handle, staged = _staged_file(os.fspath(dest.parent) if dir_fd is None else "", dir_fd)
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            os.replace(staged, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(staged, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    except OSError as error:
        raise FleetError(f"cannot write {dest}: {error}") from error


def _config_path(config: Path) -> str:
    """The configuration's path the way the bridge names it (supervision.py: expanduser, resolve)."""
    return os.path.realpath(os.path.expanduser(os.fspath(config)))


def _unit_names(config: Path) -> list[str]:
    """The names the bridge gives its unit for this configuration: today's, then the earlier one."""
    digest = hashlib.sha256(_config_path(config).encode("utf-8")).hexdigest()[:12]
    return [f"{prefix}.{digest}.service" for prefix in ("local.answerback-bridge", "local.herdr-bridge")]


def _marker(name: str, config: Path) -> str:
    return f"# answerback-fleet: repaired copy of {name} for {_config_path(config)}"


def _fleet_unit(config: Path) -> str:
    """Whose the file under our unit's name is: `absent`, `ours` (written by this tool for this
    configuration, by its first line), or `foreign` (anything else, also what cannot be read)."""
    target = _units() / FLEET_UNIT
    if _is_there(target) == "no":
        return "absent"
    try:
        with open(target, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().rstrip("\n")
    except OSError:
        return "foreign"
    return "ours" if first in [_marker(name, config) for name in _unit_names(config)] else "foreign"


def _not_ours(config: Path) -> str:
    target = _units() / FLEET_UNIT
    return f"{target} was not written by this tool for this configuration; nothing was changed"


def supervised(run_: Run = run, config: Path | None = None) -> str | bool | None:
    """What restarts this configuration's bridge, as observed, in this order: "fleet" (the unit this
    tool wrote for it is enabled and active), "native" (the bridge's own unit for it is enabled and
    active), "foreign" (a file under our unit's name that this tool did not write for this
    configuration, and that unit is enabled or active: it may hold the bridge), False (a user manager
    answers and none of these), or None where it cannot be told (not Linux, no systemctl, no
    configuration, or no user manager that answers)."""
    if not sys.platform.startswith("linux") or not which("systemctl") or config is None:
        return None
    if _systemctl(["show-environment"], run_).returncode != 0:
        return None

    def holds(unit: str) -> bool:
        enabled = _systemctl(["is-enabled", unit], run_)
        if enabled.returncode != 0 or enabled.stdout.strip() != "enabled":
            return False
        active = _systemctl(["is-active", unit], run_)
        return active.returncode == 0 and active.stdout.strip() == "active"

    owner = _fleet_unit(config)
    if owner == "ours" and holds(FLEET_UNIT):
        return "fleet"
    if any(holds(name) for name in _unit_names(config)):
        return "native"
    if owner == "foreign":
        for question, answer in (("is-enabled", "enabled"), ("is-active", "active")):
            if _systemctl([question, FLEET_UNIT], run_).stdout.strip() == answer:
                return "foreign"
    return False


def _words(command: str) -> list[str] | None:
    """The words of an ExecStart= the way the bridge writes it (supervision.py, systemd_definition):
    JSON texts with every % and $ doubled, one space between. None when it is not written so."""
    words: list[str] = []
    decoder, at = json.JSONDecoder(), 0
    command = command.strip()
    while at < len(command):
        try:
            word, end = decoder.raw_decode(command, at)
        except ValueError:
            return None
        if not isinstance(word, str) or (end < len(command) and command[end] != " "):
            return None
        words.append(word.replace("%%", "%").replace("$$", "$"))
        at = end
        while at < len(command) and command[at] == " ":
            at += 1
    return words


def _working_directory(line: str) -> str:
    """The corrected line for a quoted WorkingDirectory=, the way the bridge writes it since T-146.

    The quoted value is the JSON text of the folder with every % doubled. It is decoded and `/.` is
    appended; nothing is doubled again. Raises ValueError with the reason when it cannot be repaired.
    """
    try:
        value = json.loads(line.split("=", 1)[1])
    except ValueError:
        raise ValueError("its WorkingDirectory= cannot be decoded") from None
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("its WorkingDirectory= is not an absolute path")
    if "\n" in value or "\r" in value:
        raise ValueError("its WorkingDirectory= holds a line break")
    if ".." in value.split("/"):
        raise ValueError("its WorkingDirectory= has a .. component")
    return "WorkingDirectory=" + value.rstrip("/") + "/."


def repair_unit(config_dir: Path, run_: Run = run) -> tuple[str, str] | None:
    """Install the bridge's own generated unit under our name, with its one bad line corrected.

    Bridge releases up to 0.14.0 write WorkingDirectory= in quotes, which systemd refuses ("bad unit
    file setting"). Everything else in the generated unit is kept, so the service is the one the
    bridge meant: same command, same environment, restart on failure. Only the unit the bridge wrote
    for this configuration is taken (by its name and by its command), and only a file this tool wrote
    for this configuration is replaced. Returns None, or the state (`refused`, `fail`) and its text.
    """
    config = config_dir / "config.json"
    owner = _fleet_unit(config)
    if owner == "foreign":
        return "refused", _not_ours(config)
    names = _unit_names(config)
    places = [folder / name for folder in (_units(), config_dir / "runtime") for name in names]
    source = next((path for path in places if _is_there(path) != "no"), None)
    if source is None:
        return "fail", "the bridge has written no unit for this configuration to repair"
    try:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, ValueError) as error:
        return "fail", f"the unit {source} cannot be read ({error})"
    commands = [ln[len("ExecStart=") :] for ln in text.splitlines() if ln.startswith("ExecStart=")]
    words = _words(commands[0]) if len(commands) == 1 else None
    if words is not None and "--" in words:  # rule R2-7: the bridge reads no option after it
        return "fail", f"the unit {source} cannot be repaired (its command holds the word `--`)"
    flags = [index for index, word in enumerate(words or []) if word.startswith("--config")]
    if (
        words is None
        or len(flags) != 1
        or words[flags[0]] != "--config"
        or words[flags[0] + 1 : flags[0] + 2] != [_config_path(config)]
    ):
        return "fail", f"the unit {source} is not for this configuration"
    lines = [_marker(source.name, config)]
    try:
        for line in text.splitlines():
            if line.startswith("WorkingDirectory=") and line.endswith('"'):
                line = _working_directory(line)
            lines.append(line)
    except ValueError as error:
        return "fail", f"the unit {source} cannot be repaired ({error})"
    heads = [index for index, line in enumerate(lines) if line.strip() == "[Install]"]
    if len(heads) > 1:
        return "fail", f"the unit {source} cannot be repaired (it has more than one [Install] section)"
    if not heads:
        lines += ["", "[Install]", "WantedBy=default.target"]
    else:
        end = next((i for i in range(heads[0] + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        wanted = [ln for ln in lines[heads[0] + 1 : end] if ln.replace(" ", "") == "WantedBy=default.target"]
        if not wanted:
            lines.insert(heads[0] + 1, "WantedBy=default.target")
    target = _units() / FLEET_UNIT
    try:
        _units().mkdir(parents=True, exist_ok=True)
        _private_write(target, ("\n".join(lines) + "\n").encode("utf-8"))
    except (OSError, FleetError) as error:
        return "fail", f"the repaired unit could not be written ({error})"
    for action in (["daemon-reload"], ["enable", "--now", FLEET_UNIT]):
        done = _systemctl(action, run_)
        if done.returncode != 0:
            why = (done.stderr.strip() or done.stdout.strip() or "it failed")[-200:]
            return "fail", f"systemctl --user {' '.join(action)}: {why}"
    return None


FINISHED = ("completed", "failed", "abandoned", "cancelled")  # src/answerback_bridge/tasks.py


def _unfinished(status: dict[str, Any]) -> str:
    """What a stop or a restart of a running bridge costs, from `tasks` of its status."""
    tasks = status.get("tasks")
    if isinstance(tasks, dict) and all(type(count) is int for count in tasks.values()):
        what = f"{sum(count for name, count in tasks.items() if name not in FINISHED)} unfinished task(s)"
    else:
        what = "an unknown number of unfinished tasks"
    return f"; {what} in its store; work that the bridge owns may be interrupted and need reconciling"


def _observed(finished: subprocess.CompletedProcess[str]) -> tuple[str, dict[str, Any], str]:
    try:
        status = json.loads(finished.stdout)
    except ValueError:
        return "unknown", {}, "answerback status gave no answer that can be read"
    if not isinstance(status, dict):
        return "unknown", {}, "answerback status did not answer with a mapping"
    if status.get("running") and status.get("accepting"):
        return "up", status, ""
    if status.get("running"):
        return "unknown", status, DRAINS
    if status.get("running") is False:
        return "down", status, ""
    return "unknown", status, "answerback status does not say whether it runs"


def _observe(binary: str, run_: Run, wait: int = 1) -> tuple[str, dict[str, Any], str]:
    """What the bridge's status says, in three answers: `up` (read: running and accepting), `down`
    (read: not running), `unknown` (anything else) with the reason. Also the status that was read.
    With `wait`, it is asked once a second until it is `up`, that many times at most."""
    seen: tuple[str, dict[str, Any], str] = ("unknown", {}, "it was not asked")
    for attempt in range(wait):
        seen = _observed(run_([binary, "status"], timeout=20))
        if seen[0] == "up":
            break
        if attempt + 1 < wait:
            time.sleep(1)
    return seen


def _told(state: str, reason: str) -> str:
    """The words for a bridge that was not observed `up`."""
    return "now stopped" if state == "down" else f"whether it runs cannot be told ({reason})"


WAS_DISABLED_UNREAD = (
    f"; {FLEET_UNIT} was enabled before and was disabled by this run; "
    "whether it is enabled again cannot be told"
)
WAS_DISABLED = f"; {FLEET_UNIT} was enabled before, was disabled by this run and is not enabled again"
UNDER_OURS = "running under the repaired user unit"
UNDER_OWN = "running under the bridge's own supervisor"
RESTARTS = "restarts on failure and at the next login; at boot only with lingering"
NOT_TOLD = "running; what restarts it cannot be told here"
DRAINS = "it runs but does not accept tasks"  # what `answerback stop` leaves when it gives up


def _start(
    binary: str,
    config: Path,
    running: bool,
    kept: str | bool | None,
    upgraded: bool,
    standalone: bool,
    run_: Run,
    before: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Start the bridge so that it stays up: the bridge's own supervisor first, then our repair.

    `kept` is what supervised() observed before: "fleet", "native", False, or None where no user
    manager answers; `before` is the status that was read before. The result of every command that
    is sent here is read, and the bridge is observed (`_observe`) after each stop, start, restart,
    `disable --now` and `enable --now` that is sent here, with two exceptions: `repair_unit` sends
    `daemon-reload` and `enable --now` one after the other, and the bridge is observed after the
    two; where the bridge does not run and our unit is there, `disable --now` and `answerback
    start` are sent one after the other, and the bridge is observed after the two. "runs" is
    written only after `up`, "now stopped" only after `down`. What is taken down is brought back
    the way it was when the new way fails; a bridge that ran and is not `up` at the end is `fail`
    (the caller's last look puts `previously running, ` and what it sees in front where the
    detail does not begin with them). Every result after a stop, a restart or a `disable --now`
    of a bridge whose status said that it runs carries a count of unfinished tasks: of the status
    read right before the first of them, and read again right before each later stop of the path
    (rule R2-6); what earlier commands of the path said stays beside it.
    """
    hint = " (--standalone works around a failing supervisor)"
    manager = kept is not None
    ours = _units() / FLEET_UNIT
    reason = ""

    def see(wait: int = 1) -> str:
        nonlocal reason
        state, _, reason = _observe(binary, run_, wait)
        return state

    def failed(finished: subprocess.CompletedProcess[str], what: str) -> str:
        return _failure(finished) or f"{what} failed"

    def own(verb: str) -> tuple[str | None, str]:
        """The bridge's own supervisor: the error (None when the command worked, the bridge is up
        and, where a user manager answers, its own unit is enabled and active), and what was
        observed after it. A command that reports failure is an error also when the bridge is up."""
        finished = run_([binary, verb], timeout=90)
        state = see()
        if finished.returncode != 0:
            return failed(finished, f"answerback {verb}"), state
        if state != "up":
            return f"answerback {verb} reported success, but the bridge is not running and accepting", state
        if manager and supervised(run_, config) != "native":
            return f"answerback {verb} started the bridge, but its own unit is not enabled and active", state
        return None, state

    def detached() -> tuple[str | None, str]:
        finished = run_([*_detached(), binary, "start", "--standalone"], timeout=90)
        state = see(wait=5)
        if finished.returncode != 0:
            return failed(finished, "the detached start"), state
        if state != "up":
            return "the detached start reported success, but the bridge is not running and accepting", state
        return None, state

    def repaired() -> tuple[str, str] | None:
        """Rule S5: the repaired unit. None when the bridge is up under it."""
        problem = repair_unit(config.parent, run_)
        if problem is not None:
            return problem
        if see(wait=20) == "up" and supervised(run_, config) == "fleet":
            return None
        return "fail", "the repaired unit did not come up"

    def settle(state: str) -> str:
        """Rule R2-1a: a bridge that drains is asked again, once a second, 30 times at most."""
        for _ in range(30):
            if state != "unknown" or reason != DRAINS:
                break
            time.sleep(1)
            state = see()
        return state

    def stop_outside(back: str = "") -> tuple[str, str] | None:
        """Stop a bridge that the bridge's own start left up outside its unit (rule R2-1). None
        when it is down and the step can go on; otherwise the result that ends the step. `back`
        is the way back when nothing can be observed after the stop: "detached", "now" (our unit
        is enabled and started again), "later" (enabled again, not started) or "" (nothing)."""
        nonlocal cost, counted
        # What this stop costs is read right before it (rule R2-6): the count of an earlier stop
        # is replaced, and what earlier commands said stays behind it.
        fresh = _unfinished(_observe(binary, run_)[1])
        cost, counted = fresh + cost[len(counted) :], fresh
        finished = run_([binary, "stop"], timeout=60)
        state = see() if finished.returncode == 0 else settle(see())
        why = failed(finished, "answerback stop") if finished.returncode != 0 else "it still answers"
        off = WAS_DISABLED if back in ("now", "later") else ""
        if state == "up":
            return (
                "fail",
                f"the bridge runs outside its unit and could not be stopped ({why}); "
                f"nothing restarts it{cost}{off}",
            )
        said = f"the stop of the bridge outside its unit reported failure ({why})"
        if state == "down" and finished.returncode == 0:
            return None
        if state == "down":
            for name in _unit_names(config) if manager else []:
                if _systemctl(["is-enabled", name], run_).stdout.strip() == "enabled":
                    return (
                        "fail",
                        f"{said}; the bridge is down and its own unit {name} is still enabled; "
                        f"nothing was repaired or enabled{cost}{off}",
                    )
            cost += f"; {said}, and the bridge was down after it"
            return None
        # not observed (rule R2-1e): one attempt at the way back, and what is seen then
        if finished.returncode == 0:
            said = "the stop of the bridge outside its unit reported success"
        said = f"{said}; after it: {_told(state, reason)}"
        if back == "detached":
            problem, state = detached()
            tried = "a detached start was tried" + (f" ({problem})" if problem else "")
        elif back:
            again = _systemctl(["enable", *(["--now"] if back == "now" else []), FLEET_UNIT], run_)
            state = see(wait=20) if back == "now" and again.returncode == 0 else see()
            if again.returncode == 0:
                off = ""
                tried = f"{FLEET_UNIT} was enabled again" + ("" if back == "now" else " without starting it")
            else:
                tried = f"{FLEET_UNIT} could not be enabled again ({failed(again, 'the enable')})"
        else:
            tried, state = "nothing was started", see()
        if state == "up":
            return "fail", f"{said}; {tried}; at the end: {where()}{cost}{off}"
        if running:
            return "fail", gone(state, f"{said}; {tried}")[1] + off
        return "fail", f"{said}; {tried}; at the end: {_told(state, reason)}{cost}{off}"

    def where() -> str:
        """Where a bridge that is up runs, as supervised() says now."""
        now = supervised(run_, config) if manager else None
        if now == "fleet":
            return UNDER_OURS
        if now == "native":
            return UNDER_OWN
        return "running, but nothing restarts it" if now is False else NOT_TOLD

    def gone(state: str, error: str) -> tuple[str, str]:
        if state == "down":
            return "fail", f"previously running, now stopped ({error}){cost}"
        return "fail", f"previously running, {_told(state, reason)}; {error}{cost}"

    def away() -> str:
        """Take our unit away once the bridge's own supervisor holds the bridge; what failed."""
        problem = _remove_ours(ours, run_)
        return f"; {problem}" if problem else ""

    under_ours = f"{UNDER_OURS}: {RESTARTS}"
    under_own = UNDER_OWN
    # read again right before a stop: what the stop costs
    again = _observe(binary, run_)[1]
    cost = _unfinished(again) if running else ""
    counted = cost  # the count at the front of `cost`; what later commands said comes after it

    if not running:
        was_enabled = False
        off = ""  # our unit was enabled, this run disabled it, and nothing enabled it again
        if manager and _fleet_unit(config) == "ours":  # give the bridge's own supervisor another chance
            was_enabled = _systemctl(["is-enabled", FLEET_UNIT], run_).stdout.strip() == "enabled"
            if again.get("running"):  # it runs and does not accept work: the disable may stop it
                cost = counted = _unfinished(again)
            finished = _systemctl(["disable", "--now", FLEET_UNIT], run_)
            if finished.returncode != 0:
                return (
                    "fail",
                    f"could not disable {FLEET_UNIT} ({_failure(finished)}); nothing else was done{cost}",
                )
        error, state = own("start")
        if error is None:
            note = away() if manager and _fleet_unit(config) == "ours" else ""
            return "done", (under_own if manager else NOT_TOLD) + note + cost
        if standalone and manager:
            if state == "up":  # up, but outside its unit: our unit must not be started beside it
                ended = stop_outside("later" if was_enabled else "")
                if ended is not None:
                    return ended
            problem = repaired()
            if problem is None:
                return "done", f"{under_ours}; the bridge's own supervisor failed ({error}){cost}"
            if was_enabled:
                # Only an answer `disabled` says that the unit is disabled; no answer says nothing.
                asked = _systemctl(["is-enabled", FLEET_UNIT], run_)
                said = asked.stdout.strip()
                if said == "disabled":
                    off = WAS_DISABLED
                elif said != "enabled":
                    off = f"{WAS_DISABLED_UNREAD} ({failed(asked, 'the question')})"
            return problem[0], f"{problem[1]}; the bridge's own supervisor failed ({error}){cost}{off}"
        if standalone:
            if state == "up":  # rule R2-8: a start that reports failure is a failure
                return "fail", f"{NOT_TOLD} (answerback start reported failure: {error})"
            again, state = detached()
            if again is None:
                return "done", "running (standalone: nothing restarts it)"
            return "fail", f"{again}; the bridge's own supervisor failed ({error})"
        if was_enabled and state != "up":
            finished = _systemctl(["enable", "--now", FLEET_UNIT], run_)
            if finished.returncode != 0:
                error = (
                    f"{error}; {FLEET_UNIT} was enabled before and could not be enabled again "
                    f"({failed(finished, 'the enable')})"
                )
                state = see()
            else:
                state = see(wait=20)
                if state == "up" and supervised(run_, config) == "fleet":
                    return "done", f"{UNDER_OURS}; the bridge's own supervisor failed ({error}){cost}"
        elif was_enabled:  # up after a start that failed: our unit is not started beside it
            off = WAS_DISABLED
        if state == "up":
            return "fail", f"{where()} ({error}){hint}{cost}{off}"
        if state == "unknown":
            error = f"{error}; {_told(state, reason)}"
        return "fail", error + hint + cost

    if kept is False:  # runs with nothing to restart it: take it down to start it under a supervisor
        finished = run_([binary, "stop"], timeout=60)
        state = see() if finished.returncode == 0 else settle(see())
        if finished.returncode != 0 and state != "down":
            stop_error = failed(finished, "answerback stop")
            if state == "up":
                return (
                    "fail",
                    f"could not stop the running bridge ({stop_error}); it still runs as before{cost}",
                )
            # Not observed: one attempt to bring it back the way it ran before, and what is seen then.
            told = _told(state, reason)
            again, state = detached()
            tried = f"could not stop the running bridge ({stop_error}); a detached start was tried"
            if state == "up":
                return (
                    "fail",
                    f"after the stop: {told}; {tried}, and the bridge now runs, with nothing to restart "
                    f"it{cost}",
                )
            return gone(state, tried + (f" ({again})" if again else ""))
        if finished.returncode == 0 and state == "up":
            return (
                "fail",
                f"could not stop the running bridge (it still answers); it still runs as before{cost}",
            )
        error, state = own("start")
        if error is None:
            return "done", f"{under_own}{cost}"
        if standalone:
            if state == "up":  # started, but outside its unit: our unit could not start beside it
                ended = stop_outside("detached")
                if ended is not None:
                    return ended
            problem = repaired()
            if problem is None:
                return "done", f"{under_ours}; the bridge's own supervisor failed ({error}){cost}"
            error = f"{problem[1]}; the bridge's own supervisor failed ({error})"
            state = see()
        if state != "up":
            again, state = detached()
            if state != "up":
                return gone(state, f"{error}; the detached start: {again}")
        if not manager or supervised(run_, config) is False:
            back = "it runs again as before, with nothing to restart it"
        else:
            back = "it runs again; " + where()
        return (
            "fail",
            f"could not be put under a supervisor ({error}); {back}{cost}" + ("" if standalone else hint),
        )

    if kept == "fleet":  # after an upgrade: give the bridge's own supervisor another chance
        finished = _systemctl(["disable", "--now", FLEET_UNIT], run_)
        state = see()
        if finished.returncode != 0:
            what = f"could not disable {FLEET_UNIT} ({failed(finished, 'the disable')})"
            if state == "up":
                return "fail", f"{what}; the bridge still runs as before{cost}"
            # down, or not observed: the way back is to enable it again, once
            told = _told(state, reason)
            back = _systemctl(["enable", "--now", FLEET_UNIT], run_)
            state = see(wait=20) if back.returncode == 0 else see()
            if back.returncode == 0 and state == "up" and supervised(run_, config) == "fleet":
                return "fail", f"{what}; it was enabled again and the bridge runs under it{cost}"
            tried = (
                "it was enabled again"
                if back.returncode == 0
                else (f"it could not be enabled again ({failed(back, 'the enable')})")
            )
            if state == "up":
                return "fail", f"{what}; after it: {told}; {tried}; {where()}{cost}"
            return gone(state, f"{what}; {tried}")
        error, state = own("start")
        if error is None:
            return "done", f"{under_own}{away()}{cost}"
        if state == "up":  # up, but outside its unit: our unit must not be started beside it
            ended = stop_outside("now")
            if ended is not None:
                return ended
        finished = _systemctl(["enable", "--now", FLEET_UNIT], run_)
        state = see(wait=20) if finished.returncode == 0 else see()
        if finished.returncode == 0 and state == "up" and supervised(run_, config) == "fleet":
            return "done", f"{under_ours}, as before; the bridge's own supervisor failed ({error}){cost}"
        if finished.returncode != 0:
            error = (
                f"{error}; {FLEET_UNIT} was enabled before and could not be enabled again "
                f"({failed(finished, 'the enable')})"
            )
        return gone(state, error) if state != "up" else ("fail", f"{where()} ({error}){cost}")

    # under the bridge's own supervisor, or where supervision cannot be told: after an upgrade
    finished = run_([binary, "restart"], timeout=90)
    state, now, reason = _observe(binary, run_)
    if finished.returncode != 0 and state == "up":
        version = now.get("version")
        at = f", at {version}" if upgraded and isinstance(version, str) and version else ""
        return (
            "fail",
            f"answerback restart failed ({failed(finished, 'answerback restart')}); the bridge that ran "
            f"before still runs{at}{cost}",
        )
    error: str | None = None
    if state == "up":
        instance = (before or {}).get("instance_id")
        if instance is not None and instance == now.get("instance_id"):
            return (
                "fail",
                f"the restart changed nothing: the bridge that runs is the one from before{cost}",
            )
        if manager and supervised(run_, config) != "native":
            error = "answerback restart started the bridge, but its own unit is not enabled and active"
    else:
        error, state = own("start")
    if error is None:
        return "done", (under_own if manager else NOT_TOLD) + cost
    if state == "up":
        if manager and supervised(run_, config) is False:
            return (
                "fail",
                "running, but its own unit is no longer enabled and active: nothing restarts it "
                f"({error}){cost}",
            )
        return "fail", f"{where()} ({error}){cost}"
    if standalone and not manager:
        again, state = detached()
        if again is None:
            return "done", f"running (standalone: nothing restarts it){cost}"
        error = f"{error}; the detached start: {again}"
    return gone(state, error)


def _remove_ours(ours: Path, run_: Run) -> str:
    """Take our repaired unit away once the bridge's own supervisor holds the bridge. Returns what
    failed and why, or an empty text."""
    try:
        ours.unlink()
    except OSError as error:
        return f"{ours} could not be removed ({error})"
    finished = _systemctl(["daemon-reload"], run_)
    if finished.returncode != 0:
        return f"{ours} was removed; daemon-reload failed after it ({_failure(finished) or 'it failed'})"
    return ""


def _detached() -> list[str]:
    """A prefix that starts a program outside the caller's process tree.

    A standalone bridge started through the terminal host dies when that call ends: the host ends
    every descendant of the call, and a new session or a systemd scope is still a descendant. A
    transient user service is started by the user's systemd, so it is not. Type=forking keeps the
    unit active for as long as the detached bridge lives; nothing restarts it.
    """
    if sys.platform.startswith("linux") and which("systemd-run"):
        unit = f"--unit={STANDALONE_UNIT}"
        return ["systemd-run", "--user", unit, "--collect", "--quiet", "-p", "Type=forking"]
    return ["setsid"] if which("setsid") else []


def _link_usage_skill(linked: list[Harness], write: bool) -> tuple[str, str]:
    """Give harnesses with a linked skills directory the usage skill: one link, nothing else there.

    The file system is asked (`_is_there`, `os.path.samefile`), not two computed texts compared.
    Every harness is looked at before the first link is made: one refusal and no link is made.
    """
    shared = Path.home() / SHARED / USAGE_SKILL
    if not (shared / "SKILL.md").is_file():
        return ("change" if not write else "fail"), f"{SHARED}/{USAGE_SKILL} is not there yet"
    todo: list[Path] = []
    for harness in linked:
        assert harness.user_dir
        dest = Path.home() / harness.user_dir / USAGE_SKILL
        there = _is_there(dest)
        if there == "no":
            todo.append(dest)
            continue
        try:
            same = there == "yes" and os.path.samefile(dest, shared)
        except FileNotFoundError:
            same = False  # a link to nowhere: something else
        except OSError as error:
            there = str(error) or type(error).__name__
        if there != "yes":
            return "refused", f"the file system does not say what is at {dest} ({there}); nothing was written"
        if not same:
            return "refused", f"{dest} is something else; nothing was replaced"
    names = " ".join(h.id for h in linked)
    text = f"{names}: one link in the linked skills directory to {SHARED}/{USAGE_SKILL}"
    if not todo:
        return "ok", text
    if not write:
        return "change", text
    for dest in todo:
        made = os.path.relpath(shared.parent.resolve() / shared.name, dest.parent.resolve())
        try:
            os.symlink(made, dest)
        except OSError as error:
            return "fail", f"cannot make the link {dest}: {error}"
        # A name is never taken away again here: looking at it and removing it are two steps, and
        # nothing makes them one (rule R2-4). The next run finds what is there and checks it again.
        try:
            why = "" if (dest / "SKILL.md").is_file() else "no SKILL.md is behind it"
            why = why or ("" if os.path.samefile(dest, shared) else "it leads to another folder")
        except OSError as error:
            why = str(error) or type(error).__name__
        if why and not os.path.islink(dest):
            return (
                "fail",
                f"the link did not open as the skill; what is at {dest} now is not the link that "
                "was made, and was left",
            )
        if why:
            return (
                "fail",
                f"{dest} is linked and does not open as the skill ({why}); the link was left in place",
            )
    return "done", text


WHEEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.whl$")
PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _wheel_bytes(wheel: Any) -> bytes:
    """The wheel of the coordinator's message, after every field was checked. Raises ValueError
    with what is wrong; nothing is written before this has passed."""
    if not isinstance(wheel, dict):
        raise ValueError("it is not a mapping")
    for key in ("name", "release", "sha256", "b64", "constraints"):
        if not isinstance(wheel.get(key), str):
            raise ValueError(f"`{key}` is not text")
    extras = wheel.get("extras")
    if not isinstance(extras, list) or not all(isinstance(extra, str) for extra in extras):
        raise ValueError("`extras` is not a list of texts")
    if not WHEEL_NAME.match(wheel["name"]):
        raise ValueError("`name` is not the plain name of a wheel")
    if not PLAIN_NAME.match(wheel["release"]):
        raise ValueError("`release` is not a plain name")
    if not all(PLAIN_NAME.match(extra) for extra in extras):
        raise ValueError("an extra is not a plain name")
    try:
        data = base64.b64decode(wheel["b64"].encode("ascii"), validate=True)
    except ValueError:  # binascii.Error and UnicodeEncodeError are both ValueError
        raise ValueError("`b64` is not base64") from None
    if hashlib.sha256(data).hexdigest() != wheel["sha256"]:
        raise ValueError("the bytes do not have the hash that came with them")
    return data


def _own_folders(release: str) -> tuple[int, Path]:
    """The folder of one release under this user's state folder, opened: its descriptor and its
    path. Each of `answerback-bridge`, `releases` and `<release>` is opened inside the one before
    it without following a link (a link or anything else in its place is an error) and is made with
    mode 0700 when it is missing. What is written afterwards goes through the descriptor, so a
    folder that is exchanged on the path changes nothing. The caller closes the descriptor."""
    folder = Path.home() / ".local/state"
    at = -1
    try:
        folder.mkdir(parents=True, exist_ok=True)
        at = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        for name in ("answerback-bridge", "releases", release):
            folder = folder / name
            try:
                os.mkdir(name, 0o700, dir_fd=at)
            except FileExistsError:
                pass
            try:
                inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=at)
            except OSError as error:
                if os.path.islink(folder) or not os.path.isdir(folder):
                    raise FleetError(f"{folder} is not a real folder; nothing was written") from error
                raise
            os.close(at)
            at = inner
    except BaseException as error:
        if at >= 0:
            os.close(at)
        if isinstance(error, OSError):
            raise FleetError(f"cannot use {folder}: {error}") from error
        raise
    return at, folder


# Every status a row of the bridge's two installers can have (src/answerback_bridge/registration.py).
INSTALLER_STATUSES = (
    "planned",
    "unchanged",
    "installed",
    "migrated",
    "blocked",
    "refused",
    "kept",
    "skipped",
)


def _installer_rows(
    finished: subprocess.CompletedProcess[str], key: str, wanted: list[str], phase: str = "look"
) -> list[dict[str, Any]]:
    """The rows of an installer's answer, when the answer is whole. Raises ValueError with what is
    missing: not a mapping, no list of rows, a row that is no mapping, has a status the installer
    cannot produce or that only an apply gives when `phase` is a look (ONLY_APPLY), lacks the
    change flag the installer writes or has one that does not fit its status (CHANGE_FLAG), no
    row for a client that was asked for (for the skill installer: one of
    the two skills the bridge ships is not in `sources`, or has no row for a client), or a non-zero
    exit code with no refused row.

    The skill installer of an older build of the bridge ships the usage skill alone and does not
    name it: its answer has `source`, one path, no `sources`, and no row has a `skill`. Such an
    answer is told by that shape and read as an answer about the usage skill: each row comes back
    with that skill's name and with OLDER_BUILD set. Every other rule holds for it as well."""
    try:
        answer = json.loads(finished.stdout)
    except ValueError:
        raise ValueError(
            "it is not JSON" + (f": {_failure(finished)[:120]}" if finished.returncode else "")
        ) from None
    if not isinstance(answer, dict):
        raise ValueError("it is not a mapping")
    rows = answer.get(key)
    if not isinstance(rows, list):
        raise ValueError(f"`{key}` is not a list")
    older = (
        key == "targets"
        and isinstance(answer.get("source"), str)
        and "sources" not in answer
        and not any(isinstance(row, dict) and "skill" in row for row in rows)
    )
    if older:
        rows = [
            {**row, "skill": USAGE_SKILL, OLDER_BUILD: True} if isinstance(row, dict) else row for row in rows
        ]
    shipped = (USAGE_SKILL,) if older else (USAGE_SKILL, SKILL_NAME)
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("client"), str):
            raise ValueError("a row names no client")
        if row.get("status") not in INSTALLER_STATUSES:
            raise ValueError(f"the row for {row['client']} has the status {row.get('status')!r}")
        if key == "targets" and not isinstance(row.get("skill"), str):
            raise ValueError(f"the row for {row['client']} names no skill")
        if phase == "look" and row["status"] in ONLY_APPLY:
            raise ValueError(
                f"the installer's look answered {row['status']} for {_row_name(row)}, which only an "
                "apply gives"
            )
        flag, fits = row.get("would_change"), CHANGE_FLAG.get(row["status"])
        if type(flag) is not bool:
            raise ValueError(f"the row for {_row_name(row)} has no change flag")
        if fits is not None and flag is not fits:
            raise ValueError(
                f"the row for {_row_name(row)} says {row['status']} with would_change "
                f"{str(flag).lower()} in the installer's {phase}"
            )
    if key == "targets":
        have = {(row["skill"], row["client"]) for row in rows}
        sources = answer.get("sources")
        for skill in shipped:  # what the bridge ships, not what happened to arrive
            if not older and (not isinstance(sources, dict) or skill not in sources):
                raise ValueError(f"names no {skill}")
            if wanted and not any((skill, client) in have for client in wanted):
                raise ValueError(f"names no {skill}")
        missing = [
            f"{skill} for {client}" for skill in shipped for client in wanted if (skill, client) not in have
        ]
    else:
        seen = {row["client"] for row in rows}
        missing = [client for client in wanted if client not in seen]
    if missing:
        raise ValueError("no row for " + ", ".join(missing))
    if finished.returncode != 0 and not any(row["status"] == "refused" for row in rows):
        raise ValueError(f"it ended with exit code {finished.returncode}: {_failure(finished)[:120]}")
    return rows


# The change flag the bridge's installers write beside each status at the time of the plan
# (src/answerback_bridge/registration.py:913-921, 1356-1366, 1750-1806, 1846-1852). An apply changes
# the status and leaves the flag: `installed` and `migrated` come from a `planned` row and keep its
# true flag; `blocked` and `refused` can have either. `installed` and `migrated` cannot be in a look.
CHANGE_FLAG = {
    "planned": True,
    "unchanged": False,
    "kept": False,
    "skipped": False,
    "installed": True,
    "migrated": True,
}
ONLY_APPLY = ("installed", "migrated")
# Set in each row of a skill installer's answer in the older form (see `_installer_rows`), and what
# the look says then: at the end of the `skill` line, and on the `install` line where the installed
# version is the wanted one. Neither is a state; neither makes a machine need attention.
OLDER_BUILD = "older_build"
OLDER_INSTALLER = (
    "; the installed bridge's installer is an older build: it does not name skills and does not "
    "ship the setup skill"
)
OLDER_BRIDGE = (
    "; the installed bridge is an older build than a source of the same version (its skill "
    "installer answers in the older form); only a reinstall from the source changes that, which is "
    "the owner's step"
)
STILL_TO_DO = ("planned", "blocked")


def _unusable(error: ValueError) -> str:
    text = str(error)
    if text.startswith("names no "):
        return f"the installer's answer {text}"
    if text.startswith("the installer's look answered "):
        return text
    return f"the installer's answer is not usable ({text})"


def _row_name(row: dict[str, Any]) -> str:
    return f"{row['skill']} for {row['client']}" if "skill" in row else str(row["client"])


def _rows_text(rows: list[dict[str, Any]], word: str, chosen: Callable[[dict[str, Any]], bool]) -> str:
    names = sorted({_row_name(row) for row in rows if chosen(row)})
    return f"{word}: {', '.join(names)}" if names else ""


def install_bridge(wheel: Any, run_: Run = run) -> str | None:
    """Install the coordinator's release wheel as a uv tool. Returns an error text, or None."""
    try:
        data = _wheel_bytes(wheel)
    except ValueError as error:
        return f"the coordinator's wheel message is not usable ({error})"
    uv = which("uv")
    if not uv:
        return "uv is not installed"
    try:
        at, folder = _own_folders(wheel["release"])
    except FleetError as error:
        return str(error)
    try:
        target = folder / wheel["name"]
        _private_write(target, data, dir_fd=at)
        wrote = [(target, data)]
        argv = [uv, "tool", "install", "--force", "--quiet"]
        if wheel["constraints"]:
            wrote.append((folder / "constraints.txt", wheel["constraints"].encode("utf-8")))
            _private_write(wrote[1][0], wrote[1][1], dir_fd=at)
            argv += ["--constraints", str(folder / "constraints.txt")]
    except FleetError as error:
        return str(error)
    finally:
        os.close(at)
    # The installer reads by path. Right before it is started, the paths it gets must lead to the
    # bytes that were written (rule R2-5). An exchange after this look is not defended against.
    for path, written in wrote:
        try:
            same = hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(written).digest()
        except OSError:
            same = False
        if not same:
            return f"the release folder changed while it was written ({path.name}); nothing was installed"
    extras = f"[{','.join(wheel['extras'])}]" if wheel["extras"] else ""
    finished = run_([*argv, f"answerback{extras} @ {target.as_uri()}"], timeout=300)
    return None if finished.returncode == 0 else (finished.stderr.strip() or finished.stdout.strip())[-300:]


def ensure_here(
    *,
    write: bool,
    want: str | None = None,
    wheel: dict[str, Any] | None = None,
    standalone: bool = False,
    replace: bool = False,
    run_: Run = run,
    handshake_: Callable[[list[str]], tuple[bool, str]] = handshake,
) -> dict[str, Any]:
    """Bring this machine's bridge to: installed, configured, started, registered, answering.

    Every step first looks, and acts only when something is missing and `write` is set, so a second
    run changes nothing. A step that fails stops the steps that depend on it. States: ok, change (a
    preview of what apply would do), done, refused, fail, skip.
    """
    run_ = _results_only(run_)
    steps: list[dict[str, str]] = []

    def step(name: str, state: str, detail: str = "") -> str:
        steps.append({"step": name, "state": state, "detail": detail})
        return state

    def finish() -> dict[str, Any]:
        bad = [s for s in steps if s["state"] in ("fail", "refused")]
        pending = [s for s in steps if s["state"] == "change"]
        return {
            "machine": os.uname().nodename,
            "steps": steps,
            "ok": not bad and not pending,
            "failed": len(bad),
        }

    # 1. the bridge program, at the coordinator's version
    bridge = bridge_state(run_)
    have = bridge.get("version")
    upgraded = False
    if bridge.get("installed") and (not want or version_tuple(have) >= version_tuple(want)):
        newer = bool(want) and version_tuple(have) > version_tuple(want)
        # never an older program over a newer one; the detail then names both versions
        step(
            "install",
            "ok",
            f"answerback {have}" + (f" (newer than the coordinator's {want})" if newer else ""),
        )
    else:
        what = f"install answerback {want}" + (f" (has {have})" if have else "")
        if not want:  # no bridge here and no version to ask for: said in words, not as `None`
            step(
                "install",
                "change" if not write else "fail",
                "install answerback: no release wheel is known here",
            )
        elif not write:
            cannot = "; the installed program is replaced; ensure cannot put the old one back"
            step("install", "change", what + (cannot if bridge.get("installed") else ""))
        elif not wheel:
            step("install", "fail", f"{what}: the coordinator has no release wheel to send")
        else:
            error = install_bridge(wheel, run_)
            bridge = bridge_state(run_)  # the version is read again: `done` is what runs now
            now = bridge.get("version")
            if error or not bridge.get("installed"):
                step("install", "fail", error or "answerback is still not found after the install")
            elif version_tuple(now) < version_tuple(want):
                step("install", "fail", f"installed, but answerback reports {now}, not {want}")
            else:
                upgraded = bool(have)
                step(
                    "install",
                    "done",
                    f"answerback {bridge.get('version')}" + (f" (was {have})" if have else ""),
                )
    if steps[0]["state"] == "fail" or not bridge.get("installed"):
        needs = (
            f"needs the bridge at {want}"
            if steps[0]["state"] == "fail" and want
            else "needs the bridge program"
        )
        for name in ("configure", "start", "register", "skill", "handshake"):
            step(name, "skip", needs)
        return finish()
    binary = bridge["path"]

    # 2. a configuration with the core provider
    status = _status(binary, run_)
    socket = status.get("socket")
    config = Path(socket).parent.parent / "config.json" if isinstance(socket, str) and socket else None
    there = _is_there(config) if config is not None else "no"
    if config is None:
        step("configure", "fail", "answerback status names no socket; cannot find the configuration")
    elif there != "no":  # a configuration that is there is read, and never edited
        try:
            with open(config, encoding="utf-8") as handle:
                settings = json.loads(handle.read())
            if not isinstance(settings, dict):
                raise ValueError("it is not a mapping")
        except (OSError, ValueError) as error:
            step("configure", "fail", f"{config} cannot be read as a configuration ({error})")
        else:
            if settings.get("hc_enabled"):
                step("configure", "ok", str(config))
            else:
                step(
                    "configure",
                    "refused",
                    f"{config} does not enable the core provider; "
                    "ensure does not edit a configuration that is there",
                )
    elif not write:
        step("configure", "change", "answerback configure --runtime hc")
    else:
        finished = run_([binary, "configure", "--runtime", "hc"], timeout=60)
        good = finished.returncode == 0 and config.is_file()
        step("configure", "done" if good else "fail", str(config) if good else _failure(finished))

    # 3. the service, and something that starts it again after a failure or at the next login
    running = bool(status.get("running") and status.get("accepting"))
    kept = supervised(run_, config)  # None where this cannot be told (no user manager answers)
    planned = steps[0]["state"] == "change" and bool(bridge.get("installed"))  # an upgrade --yes would do
    fresh = steps[-1]["state"] in ("change", "done")  # the configuration is written in this run
    if steps[-1]["state"] in ("fail", "refused"):
        step("start", "skip", "needs a configuration with the core provider")
    elif kept == "foreign":
        detail = (
            f"a unit {FLEET_UNIT} that this tool did not write for this configuration is enabled or "
            "active and may hold the bridge; nothing was stopped or changed"
        )
        if steps[0]["state"] == "done" and running:
            detail += (
                "; the program was replaced in this run; the bridge that runs is the old one until it "
                "is restarted"
            )
        step("start", "refused", detail)
    elif running and not upgraded and not planned and not fresh and kept is not False:
        step(
            "start",
            "ok",
            "running and accepting"
            + (
                ", under the repaired user unit"
                if kept == "fleet"
                else ", under the bridge's own supervisor"
                if kept == "native"
                else "; what restarts it cannot be told here"
            ),
        )
    elif not write:
        # What runs decides, also when the configure step plans a change (rule I1).
        if running and kept is False:
            what = "running, but nothing starts it again after a failure or at the next login"
            step("start", "change", what + ": stop it and start it under a supervisor" + _unfinished(status))
        elif running:
            why = "after the upgrade" if planned else "with the configuration that is written"
            step("start", "change", f"restart {why}" + _unfinished(status))
        else:
            step("start", "change", "answerback start")
    else:
        assert config is not None
        state, detail = _start(binary, config, running, kept, upgraded, standalone, run_, before=status)
        seen, now, reason = _observe(binary, run_)
        if state == "done" and seen != "up":
            # only what was observed is said: `did not stay up` needs a status that says so
            told = _told(seen, reason)
            was = f"previously running, {told}" if running else "did not stay up" if seen == "down" else told
            state, detail = "fail", f"{was} ({detail})"
        elif state == "done" and upgraded:
            # `done` after an upgrade is the bridge that runs, at the version that was installed
            installed, runs = bridge.get("version"), now.get("version")
            if not isinstance(runs, str) or not runs:
                detail += "; the running bridge does not say its version"
            elif version_tuple(runs) < version_tuple(installed):
                state = "fail"
                detail = (
                    f"the bridge that runs reports {runs}, not {installed}: it was not replaced "
                    f"(the start had said: {detail})"
                )
        elif state != "done" and running:
            # the last look decides the words of a failure too (rule R2-2)
            # the words about now are made again at every last look; what the step said stays
            begins = detail.startswith("previously running, ")
            now_is = f"previously running, {_told(seen, reason)}"
            if seen != "up" and not detail.startswith(now_is):
                detail = f"{now_is} (the start step had said: {detail})"
            elif seen == "up" and begins:
                detail = f"the bridge answers at the last look (the start step had said: {detail})"
        running = seen == "up"
        step("start", state, detail)

    # 4. and 5. the bridge's own installers: MCP connection and usage skill
    facts = probe(run_=run_)
    clients = [
        h.bridge_client for h in HARNESSES if h.bridge_client and facts["harnesses"][h.id]["installed"]
    ]
    # A harness whose skills directory is a link to another shared folder: the bridge's installer
    # refuses it, and one refusal blocks every client. Leave it out and link the one skill there.
    shared_real = facts["shared"]["skills_dir"].get("real")
    linked = [
        h
        for h in HARNESSES
        if h.bridge_client in clients
        and h.user_dir
        and facts["harnesses"][h.id]["skills_dir"].get("kind") == "link"
        and facts["harnesses"][h.id]["skills_dir"].get("real") != shared_real
    ]
    entries: dict[str, list[str]] = {}
    differs: set[str] = set()  # a different entry is registered; we can only try the one the bridge wants
    for name, kind, key in (("register", "clients", "clients"), ("skill", "skill", "targets")):
        wanted = [c for c in clients if kind == "clients" or c not in {h.bridge_client for h in linked}]
        if not wanted:
            step(name, "skip", "no harness with a bridge client here")
            continue
        flags = [flag for client in wanted for flag in ("--client", client)]
        base = [binary, kind, "install", *flags] + (["--replace"] if replace else [])
        try:
            rows = _installer_rows(run_(base, timeout=90), key, wanted)
        except ValueError as error:
            step(name, "fail", _unusable(error)[:300])
            continue
        # An older build's skill installer: said on this line and once on the `install` line; it
        # changes no state and is no reason to install, stop, start or write anything.
        older = OLDER_INSTALLER if any(row.get(OLDER_BUILD) for row in rows) else ""
        if older and want and version_tuple(bridge.get("version")) == version_tuple(want):
            steps[0]["detail"] += OLDER_BRIDGE
        refused = [f"{_row_name(r)}: {r.get('error')}" for r in rows if r["status"] == "refused"]
        todo = sorted({_row_name(r) for r in rows if r["status"] in STILL_TO_DO})
        left_alone = "".join(
            "; " + text
            for text in (
                _rows_text(rows, "kept", lambda r: r["status"] == "kept"),
                _rows_text(rows, "skipped", lambda r: r["status"] == "skipped"),
            )
            if text
        )
        if refused:
            hint = "" if replace else " (--replace backs up and replaces)"
            step(name, "refused", "; ".join(refused)[:300] + hint + older)
        elif not todo:
            same = _rows_text(rows, "unchanged", lambda r: r["status"] == "unchanged")
            step(name, "ok", (same + left_alone).lstrip("; ") + older)
        elif not write:
            what = "back up and replace what is there, and install" if replace else "install"
            step(name, "change", f"{what}: " + ", ".join(todo) + left_alone + older)
        else:
            applied = run_([*base, "--apply"], timeout=120)
            # The answer of the apply counts, whatever a look afterwards says (rule G3).
            state, why = "done", ""
            try:
                whole = subprocess.CompletedProcess(applied.args, 0, applied.stdout, applied.stderr)
                answered = _installer_rows(whole, key, wanted, "apply")
                no = [f"{_row_name(r)}: {r.get('error')}" for r in answered if r["status"] == "refused"]
                blocked = _rows_text(answered, "blocked", lambda r: r["status"] == "blocked")
                if no:
                    state, why = "refused", "refused by the apply: " + "; ".join(no)
                elif blocked:
                    state, why = "fail", blocked
                elif applied.returncode != 0:
                    state = "fail"
                    why = f"the apply ended with exit code {applied.returncode}: {_failure(applied)[:120]}"
            except ValueError as error:
                state, why = "fail", _unusable(error)
            after: list[dict[str, Any]] = []
            for attempt in range(3):  # a harness's own installer can take a moment to show its result
                seen = ""
                try:
                    after = _installer_rows(run_(base, timeout=90), key, wanted)
                    left = sorted(
                        {
                            _row_name(r)
                            for r in after
                            if r["status"] in STILL_TO_DO or r["status"] == "refused"
                        }
                    )
                    seen = f"still to do after the install: {', '.join(left)}" if left else ""
                except ValueError as error:
                    after, seen = [], _unusable(error)
                if not seen or attempt == 2 or why:
                    break
                time.sleep(2)
            if seen and not why:
                state, why = "fail", seen
            step(name, state, (why[:300] if why else ", ".join(todo) + left_alone) + older)
            rows = after
        if kind == "clients":
            for row in rows:
                entry = row.get("entry")
                command = entry.get("command") if isinstance(entry, dict) else None
                more = entry.get("args", []) if isinstance(entry, dict) else []
                usable = isinstance(command, str) and isinstance(more, list)
                if usable and row.get("status") not in STILL_TO_DO:
                    entries[str(row.get("client"))] = [str(command), *[str(word) for word in more]]
                if row.get("status") == "refused":
                    differs.add(str(row.get("client")))

    if linked:
        shared_skill = Path.home() / SHARED / USAGE_SKILL
        nobody = all(c in {h.bridge_client for h in linked} for c in clients)
        if nobody and not (shared_skill / "SKILL.md").is_file():
            step(
                "skill-link",
                "fail",
                f"{shared_skill} is not there, and the bridge's installer has no harness to install it "
                "for here",
            )
        else:
            state, detail = _link_usage_skill(linked, write)
            step("skill-link", state, detail)

    # 6. does a registered entry really answer
    if not entries:
        step("handshake", "skip", "no registered entry to try")
    elif not running:
        step("handshake", "skip", "the bridge is not running")
    else:
        results = {client: handshake_(command) for client, command in sorted(entries.items())}
        bad = [f"{client}: {text}" for client, (good, text) in results.items() if not good]
        tried = [c for c in results if c not in differs]
        parts = bad or (["answered: " + " ".join(tried)] if tried else [])
        if differs and not bad:
            names = " ".join(sorted(differs))
            parts.append(f"the bridge answers for {names}, whose registered entry differs and was not tried")
        text = "; ".join(parts)
        step("handshake", "fail" if bad else "ok", text[:300])
    return finish()


def coordinator_wheel(run_: Run = run) -> dict[str, Any] | None:
    """The release wheel this machine's bridge was installed from, ready to send to a node."""
    uv = which("uv")
    tools = run_([uv, "tool", "dir"], timeout=15).stdout.strip() if uv else ""
    receipt = Path(tools or Path.home() / ".local/share/uv/tools") / "answerback/uv-receipt.toml"
    try:
        text = receipt.read_text()
    except OSError:
        return None
    line = next((ln for ln in text.splitlines() if ln.startswith("requirements")), "")
    found = re.search(r'path = "([^"]+\.whl)"', line)
    if not found or not Path(found.group(1)).is_file():
        return None
    wheel = Path(found.group(1))
    try:
        data = wheel.read_bytes()
        with zipfile.ZipFile(wheel) as archive:
            names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
            lines = archive.read(names[0]).decode("utf-8", "replace").splitlines() if names else []
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    version = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("Version:")), "")
    extras = re.search(r"extras = \[([^\]]*)\]", line)
    constraints = wheel.parent / "constraints-live-env.txt"
    return {
        "version": version,  # the wheel's own Version:, for the coordinator; it is not sent
        "name": wheel.name,
        "release": wheel.parent.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "b64": base64.b64encode(data).decode("ascii"),
        "extras": re.findall(r'"([^"]+)"', extras.group(1)) if extras else [],
        "constraints": constraints.read_text() if constraints.is_file() else "",
    }


def ensure_fleet(args: argparse.Namespace, run_: Run = run) -> dict[str, Any]:
    """Run ensure on this machine and on each node, from the coordinator. No login to a node is needed."""
    run_ = _results_only(run_)
    fleet = machines(run_)
    local, nodes, left = select(fleet, args.machine or ["all"])
    # Each machine once, in the order it was named: one acting call per machine in one run, and so
    # a result that is recorded for a machine is never replaced by a later one.
    nodes = list(dict.fromkeys(nodes))
    want = bridge_state(run_).get("version")
    out: dict[str, Any] = {"mode": fleet["mode"], "want": want, "machines": {}, "left_out": left}
    if local:
        out["machines"]["local"] = ensure_here(
            write=args.yes, standalone=args.standalone, replace=args.replace, run_=run_
        )
    if not nodes:
        return out
    if not want:
        raise FleetError("this machine has no bridge to take the version from")
    load = capacity(nodes, run_) if args.yes else {}
    node_args = ["node-ensure", "--want", want]
    node_args += ["--standalone"] if args.standalone else []
    node_args += ["--replace"] if args.replace else []
    wheel: dict[str, Any] | None = None
    for name in nodes:
        state = load.get(name, {}).get("state", "unknown")
        if args.yes and state not in ("ok", "busy") and not args.any_capacity:
            out["left_out"].append({"machine": name, "reason": f"capacity {state}; --any-capacity overrides"})
            continue
        try:
            look = _node_answer(name, on_node(name, node_args, run_=run_))
            needs = any(s["step"] == "install" and s["state"] == "change" for s in look["steps"])
            if args.yes and needs and wheel is None:
                wheel = coordinator_wheel(run_) or {}
        except (FleetError, ValueError, OSError) as error:
            # Nothing that changes this machine has been sent: only here may it be left out.
            out["left_out"].append({"machine": name, "reason": str(error)})
            continue
        problem = _wheel_problem(wheel or {}, want) if args.yes and needs else None
        if problem:  # nothing is sent that could change the node
            look = _without_wheel(look, problem, want)
        elif args.yes and not look["ok"]:
            sent = {key: value for key, value in (wheel or {}).items() if key != "version"}
            look = _act(name, node_args, json.dumps(sent) if needs else None, run_)
        out["machines"][name] = look
    return out


ACT_LIMIT = 600  # seconds for the one call that changes a node


def _act(name: str, node_args: list[str], payload: str | None, run_: Run) -> dict[str, Any]:
    """Send the acting call to a node once, then look once. The machine has a result whatever comes.

    The call is never sent a second time: a node that did not answer may be half-way through.
    """
    acted: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = []
    try:
        answer = on_node(name, [*node_args, "--yes"], payload=payload, timeout=ACT_LIMIT, run_=run_)
        acted = _node_answer(name, answer)
    except (FleetError, ValueError, OSError) as error:
        detail = (
            f"the acting call did not come back ({error}); this machine may be partly changed; "
            "nothing was sent again"
        )
        steps.append({"step": "act", "state": "fail", "detail": detail})
    try:
        # Look again over a new connection: what a step reported inside its own call is not
        # proof that it still holds once that call has ended.
        now = _node_answer(name, on_node(name, node_args, run_=run_))
    except (FleetError, ValueError, OSError) as error:
        steps += acted["steps"] if acted else []
        detail = f"the look after the acting call did not come back ({error})"
        steps.append({"step": "look", "state": "fail", "detail": detail})
    else:
        if acted:
            return _settled(acted, now)
        steps += now["steps"]
    bad = [s for s in steps if s["state"] in ("fail", "refused")]
    return {"machine": name, "ok": False, "failed": len(bad), "steps": steps}


STATES = ("ok", "change", "done", "refused", "fail", "skip")


def _node_answer(name: str, answer: Any) -> dict[str, Any]:
    """A node's answer to ensure, checked before anything of it is used. Raises FleetError."""

    def wrong(what: str) -> FleetError:
        return FleetError(f"{name}: its answer is not a result of ensure ({what})")

    if not isinstance(answer, dict):
        raise wrong("it is not a mapping")
    if not isinstance(answer.get("machine"), str):
        raise wrong("`machine` is not text")
    if not isinstance(answer.get("ok"), bool):
        raise wrong("`ok` is not true or false")
    steps = answer.get("steps")
    if not isinstance(steps, list):
        raise wrong("`steps` is not a list")
    for row in steps:
        if not isinstance(row, dict):
            raise wrong("a step is not a mapping")
        if not all(isinstance(row.get(key), str) for key in ("step", "state", "detail")):
            raise wrong("a step lacks the text of `step`, `state` or `detail`")
        if row["state"] not in STATES:
            raise wrong(f"the step {row['step']} has the state {row['state']!r}")
    return answer


def _wheel_problem(wheel: dict[str, Any], want: str) -> str | None:
    """Why the coordinator's wheel is not sent: there is none, or it is not the program that runs."""
    if not wheel:
        return f"install answerback {want}: the coordinator has no release wheel to send"
    if wheel.get("version") != want:
        have = wheel.get("version") or "of no readable version"
        return f"the coordinator's wheel is {have}, its program reports {want}"
    return None


def _without_wheel(look: dict[str, Any], problem: str, want: str) -> dict[str, Any]:
    """The look at a node that needs a wheel the coordinator cannot send: nothing was sent to it."""
    for row in look["steps"]:
        if row["step"] == "install":
            row.update(state="fail", detail=problem)
        else:
            row.update(state="skip", detail=f"needs the bridge at {want}")
    look.update(ok=False, failed=1)
    return look


def _settled(acted: dict[str, Any], now: dict[str, Any]) -> dict[str, Any]:
    """The state after the call that acted has ended, keeping `done` where it still holds."""
    did = {s["step"]: s for s in acted["steps"]}
    for current in now["steps"]:
        before = did.get(current["step"], {})
        if before.get("state") == "done":
            if current["state"] == "ok":
                current.update(state="done", detail=before["detail"])
            else:
                current.update(
                    state="fail", detail=f"did not hold after the call ended ({current['detail']})"
                )
        elif before.get("state") in ("fail", "refused") and current["state"] == "change":
            current.update(state=before["state"], detail=before["detail"])
    reported = {s["step"] for s in now["steps"]}
    for step, before in did.items():
        if before["state"] == "done" and step not in reported:
            detail = "no longer reported after the call ended"
            now["steps"].append({"step": step, "state": "fail", "detail": detail})
    bad = [s for s in now["steps"] if s["state"] in ("fail", "refused")]
    now.update(ok=not bad and not any(s["state"] == "change" for s in now["steps"]), failed=len(bad))
    return now


def _node_ensure(args: argparse.Namespace) -> dict[str, Any]:
    payload = globals().get("FLEET_PAYLOAD")
    wheel: Any = None
    if payload:
        try:
            wheel = json.loads(payload)
        except (TypeError, ValueError):
            wheel = "not JSON"  # install_bridge says that the message is not usable
    return ensure_here(
        write=args.yes,
        want=args.want,
        wheel=wheel,
        standalone=args.standalone,
        replace=args.replace,
    )


# --------------------------------------------------------------------------- command line


def _plan_args(args: argparse.Namespace) -> list[str]:
    out = ["--scope", args.scope, "--mode", args.mode, "--bridge", args.bridge]
    for harness in args.harness or []:
        out += ["--harness", harness]
    if args.force:
        out.append("--force")
    if args.min_bridge:
        out += ["--min-bridge", args.min_bridge]
    if args.remove:
        out.append("--remove")
    return out


def _carry_out(planned: dict[str, Any], write: bool) -> dict[str, Any]:
    """A plan, or the plan with what was done. A plan with a refused step is returned untouched."""
    if not write or planned["refused"]:
        return planned
    done = apply_remove(planned) if planned["mode"] == "remove" else apply_plan(planned)
    return {"plan": planned, "applied": done}


def _here_plan(
    args: argparse.Namespace, source: Path, link_source: bool, native: bool = True
) -> dict[str, Any]:
    return plan(
        source=source,
        scope=args.scope,
        mode=args.mode,
        link_source=link_source,
        harnesses=args.harness,
        project=Path(args.project).resolve() if args.project else None,
        force=args.force,
        bridge=args.bridge,
        min_bridge=args.min_bridge,
        native=native,
    )


def _node_install(args: argparse.Namespace) -> dict[str, Any]:
    """Runs on a node: unpack what the coordinator sent, plan against it, and write only if asked."""
    project = Path(args.project).resolve() if args.project else None
    if args.remove:
        return _carry_out(plan_remove(scope=args.scope, project=project), args.yes)
    payload = globals().get("FLEET_PAYLOAD")
    if not payload:
        raise FleetError("node-install needs the skill from the coordinator")
    staging = Path(tempfile.mkdtemp(prefix=f"{SKILL_NAME}."))
    try:
        source = staging / SKILL_NAME
        unpack(payload, source)
        if tree_hash(source) != args.expect:
            raise FleetError("the skill arrived damaged; nothing was written")
        return _carry_out(_here_plan(args, source, link_source=False, native=False), args.yes)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _fleet_install(args: argparse.Namespace, write: bool) -> dict[str, Any]:
    source = Path(args.source).resolve() if args.source else default_source()
    expect = tree_hash(source)
    if expect is None:
        raise FleetError(f"no skill at {source}")
    fleet = machines()
    local, nodes, left = select(fleet, args.machine)
    if args.scope == "project" and nodes:
        raise FleetError("project scope is local: commit the project's skill folders and let git carry them")
    out: dict[str, Any] = {"mode": fleet["mode"], "hash": expect, "machines": {}, "left_out": left}
    if args.bridge == "apply" and not args.min_bridge:
        args.min_bridge = bridge_state().get("version")  # nodes must not be behind the coordinator
    if local:
        if args.remove:
            project = Path(args.project).resolve() if args.project else None
            planned = plan_remove(scope=args.scope, project=project, source=source)
        else:
            planned = _here_plan(args, source, link_source=args.scope == "user" and not args.copy)
        out["machines"]["local"] = _carry_out(planned, write)
    if nodes:
        payload = None if args.remove else pack(source)
        load = capacity(nodes)
        for name in nodes:
            state = load.get(name, {}).get("state", "unknown")
            if write and state not in ("ok", "busy") and not args.any_capacity:
                out["left_out"].append(
                    {"machine": name, "reason": f"capacity {state}; --any-capacity overrides"}
                )
                continue
            node_args = ["node-install", "--expect", expect, *_plan_args(args)] + (["--yes"] if write else [])
            try:
                out["machines"][name] = on_node(name, node_args, payload=payload, timeout=180)
            except (FleetError, ValueError) as error:
                out["left_out"].append({"machine": name, "reason": str(error)})
    return out


def _text(command: str, data: Any) -> str:
    """A short table for people. --json gives the full facts."""
    lines: list[str] = []
    if command == "ensure":
        lines.append(f"bridge wanted: {data['want']}")
        for name, body in data["machines"].items():
            lines.append(f"== {name}: " + ("ok" if body["ok"] else "needs attention"))
            lines += [f"  {s['state']:<8} {s['step']:<10} {s['detail']}" for s in body["steps"]]
        lines += [f"left out: {item['machine']}: {item['reason']}" for item in data["left_out"]]
        return "\n".join(lines)
    if command in ("survey", "pool"):
        if command == "survey":
            lines.append(f"mode: {data['mode']}" + (f" ({data['reason']})" if data.get("reason") else ""))
            for name, row in data["machines"].items():
                have = " ".join(h for h, e in row["harnesses"].items() if e["installed"])
                bridge = row["bridge"]
                service = "running" if bridge.get("running") else "stopped"
                version = f"{bridge.get('version')} {service}" if bridge.get("installed") else "missing"
                lines.append(
                    f"{name:<14} {row['os']:<7} capacity={row['capacity'].get('state'):<8} "
                    f"bridge={version:<26} harnesses: {have}"
                )
            lines += [f"drift: {note}" for note in data["drift"]]
        else:
            for row in data:
                mark = "yes" if row["eligible"] else "no "
                why = "" if row["eligible"] else "  (" + ", ".join(row["why_not"]) + ")"
                role = " coordinator" if row["coordinator"] else ""
                lines.append(
                    f"{mark} {row['machine']:<14} {row['capacity']:<8} {' '.join(row['harnesses'])}{role}"
                    + why
                )
        for item in data.get("left_out", []) if isinstance(data, dict) else []:
            lines.append(f"left out: {item['machine']}: {item['reason']}")
        return "\n".join(lines)
    for name, body in data["machines"].items():
        planned = body.get("plan", body)
        results = [a.get("result") for a in body.get("applied", {}).get("actions", [])]
        lines.append(f"== {name}: {planned['scope']} scope, {planned['mode']} mode, {planned['source_kind']}")
        for index, action in enumerate(planned["actions"]):
            what = action["path"] or " ".join(action["argv"])
            arrow = f" -> {action['target']}" if action["target"] else ""
            note = f"  [{action['note']}]" if action["note"] else ""
            done = f"  => {results[index]}" if index < len(results) else ""
            lines.append(f"  {action['op']:<9} {action['harness']:<9} {what}{arrow}{note}{done}")
            for target in (
                (action["detail"] or {}).get("targets", []) if isinstance(action["detail"], dict) else []
            ):
                extra = f" ({target['error']})" if target.get("error") else ""
                extra += f"  [{target['note']}]" if target.get("note") else ""
                skill = f"{target['skill']:<24} " if target.get("skill") else ""
                lines.append(f"            {target.get('client'):<9} {skill}{target.get('status')}{extra}")
        for check in body.get("applied", {}).get("verify", []):
            if not check["ok"]:
                lines.append(f"  VERIFY FAILED {check['path']}")
    for item in data["left_out"]:
        lines.append(f"left out: {item['machine']}: {item['reason']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="print the full facts as JSON")

    def install(p: argparse.ArgumentParser) -> None:
        common(p)
        p.add_argument("--scope", choices=("user", "project"), default="user")
        p.add_argument(
            "--mode",
            choices=("canonical", "harness"),
            default="canonical",
            help="canonical: one entry in .agents/skills, harnesses link to it (default)",
        )
        p.add_argument(
            "--harness", action="append", choices=sorted(HARNESS_BY_ID), help="repeat; default: all found"
        )
        p.add_argument(
            "--bridge",
            choices=("none", "preview", "apply"),
            default="preview",
            help="the bridge's own installers for its MCP connection and usage skill",
        )
        p.add_argument("--force", action="store_true", help="replace a link that points elsewhere")
        p.add_argument("--min-bridge", help="oldest bridge whose installers --bridge apply may run")
        p.add_argument("--remove", action="store_true", help="uninstall: links to this skill and our copies")
        p.add_argument("--project", help="project root, for --scope project")

    for name, text in (
        ("survey", "machines, harnesses, bridge and capacity"),
        ("pool", "machines that may take work now"),
    ):
        p = sub.add_parser(name, help=text)
        common(p)
        p.add_argument(
            "--machine", action="append", help="local, a name from `hc machine ls`, or all (default)"
        )
    for name, text in (
        ("plan", "show what an install would do; writes nothing"),
        ("apply", "carry the plan out; needs --yes"),
    ):
        p = sub.add_parser(name, help=text)
        install(p)
        p.add_argument("--machine", action="append", help="local (default), a machine name, or all")
        p.add_argument("--source", help="skill folder (default: the one this script is in)")
        p.add_argument("--copy", action="store_true", help="copy locally instead of linking to the source")
        p.add_argument(
            "--any-capacity", action="store_true", help="also write to a node that is full or unmeasured"
        )
        if name == "apply":
            p.add_argument("--yes", action="store_true", help="really write")
    for name, text in (
        ("ensure", "bridge installed, configured, started, registered and answering, on every machine"),
        ("node-ensure", argparse.SUPPRESS),
    ):
        p = sub.add_parser(name, help=text)
        common(p)
        p.add_argument("--yes", action="store_true", help="really change what is missing")
        p.add_argument("--standalone", action="store_true", help="start without the native supervisor")
        p.add_argument("--replace", action="store_true", help="back up and replace conflicting entries")
        if name == "ensure":
            p.add_argument("--machine", action="append", help="local, a machine name, or all (default)")
            p.add_argument(
                "--any-capacity", action="store_true", help="also a node that is full or unmeasured"
            )
        else:
            p.add_argument("--want", help="the coordinator's bridge version")
    common(sub.add_parser("probe", help="this machine only (what a node answers)"))
    p = sub.add_parser("node-install", help=argparse.SUPPRESS)
    install(p)
    p.add_argument("--expect", required=True)
    p.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "probe":
            data: Any = probe()
        elif args.command == "node-install":
            data = _node_install(args)
        elif args.command == "node-ensure":
            data = _node_ensure(args)
        elif args.command == "ensure":
            data = ensure_fleet(args)
        elif args.command == "survey":
            data = survey(args.machine)
        elif args.command == "pool":
            data = pool(survey(args.machine))
        elif args.command == "plan":
            data = _fleet_install(args, write=False)
        else:
            if not args.yes:
                raise FleetError("apply writes; run plan first, then apply --yes")
            data = _fleet_install(args, write=True)
    except FleetError as error:
        print(f"fleet: {error}", file=sys.stderr)
        return 1
    if args.json:
        if args.command in NODE_COMMANDS:
            print(BEGIN)
        print(json.dumps(data, indent=None if args.command in NODE_COMMANDS else 2))
        if args.command in NODE_COMMANDS:
            print(END)
    else:
        print(json.dumps(data, indent=2) if args.command in NODE_COMMANDS else _text(args.command, data))
    if args.command == "ensure":
        return 0 if all(b["ok"] for b in data["machines"].values()) and not data["left_out"] else 1
    if args.command in ("plan", "apply"):
        bodies = [b.get("plan", b) for b in data["machines"].values()]
        failed = [
            a
            for b in data["machines"].values()
            for a in (b.get("applied") or {}).get("actions", [])
            if a.get("op") == "bridge" and str(a.get("result", "")).startswith("failed")
        ]  # a bridge installer that ended with another exit code than 0
        return 1 if any(b["refused"] for b in bodies) or data["left_out"] or failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
