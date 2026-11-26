"""The on-disk `answerback_bridge` agent the bridge writes for Kiro's v2 engine."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.kiro_agent import (
    AGENT_NAME,
    KiroAgentError,
    agent_document,
    agent_path,
    install_worker_agent,
)
from answerback_bridge.workers import worker_profile


def expected_bytes() -> bytes:
    return (json.dumps(agent_document(worker_profile()), indent=2) + "\n").encode()


def test_agent_document_mirrors_the_inline_profile_in_file_format() -> None:
    profile = worker_profile()
    document = agent_document(profile)
    assert document["name"] == AGENT_NAME
    assert "id" not in document
    assert document["tools"] == profile["tools"]
    assert document["prompt"] == profile["prompt"]
    assert document["description"] == profile["description"]
    # The inline scoping keys become the file format's: no legacy MCP file, no servers.
    assert "includeMcpJson" not in document and "includePowers" not in document
    assert document["useLegacyMcpJson"] is False
    assert document["mcpServers"] == {}
    assert document["allowedTools"] == []
    assert document["permissions"] == {"rules": []}


def test_agent_document_refuses_any_other_agent_name() -> None:
    other = {**worker_profile(), "id": "other"}
    with pytest.raises(KiroAgentError, match="only ever written as 'answerback_bridge'"):
        agent_document(other)


@pytest.mark.parametrize("name", ["other", "", None, "Answerback_Bridge"])
def test_agent_document_refuses_a_name_that_would_override_the_fixed_one(
    name: str | None,
) -> None:
    """The inline profile names the agent by `id`; a `name` key in it would land after the
    fixed `"name"` in the file document and rename the file's agent. Refused, so the
    fixed-name invariant holds for every caller, not only `worker_profile()`."""
    with pytest.raises(KiroAgentError, match="only ever written as 'answerback_bridge'"):
        agent_document({**worker_profile(), "id": AGENT_NAME, "name": name})


def test_agent_document_accepts_a_redundant_matching_name() -> None:
    document = agent_document({**worker_profile(), "name": AGENT_NAME})
    assert document["name"] == AGENT_NAME
    assert document == agent_document(worker_profile())


def test_agent_path_is_under_the_given_home(tmp_path: Path) -> None:
    assert agent_path(tmp_path) == tmp_path / ".kiro" / "agents" / "answerback_bridge.json"


def test_agent_path_defaults_to_the_process_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert agent_path() == tmp_path / ".kiro" / "agents" / "answerback_bridge.json"


def test_install_creates_missing_folders_and_a_0600_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert install_worker_agent(worker_profile(), home) is True
    target = agent_path(home)
    assert target.read_bytes() == expected_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / ".kiro").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / ".kiro" / "agents").stat().st_mode) == 0o700
    assert sorted(path.name for path in (home / ".kiro" / "agents").iterdir()) == [
        "answerback_bridge.json"
    ]


def test_install_is_idempotent_and_leaves_a_current_file_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install_worker_agent(worker_profile(), home)
    target = agent_path(home)
    before = target.stat()
    assert install_worker_agent(worker_profile(), home) is False
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_install_rewrites_a_file_whose_content_differs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".kiro" / "agents").mkdir(parents=True)
    target = agent_path(home)
    target.write_text('{"name": "answerback_bridge", "tools": []}\n')
    assert install_worker_agent(worker_profile(), home) is True
    assert target.read_bytes() == expected_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(path.name for path in target.parent.iterdir()) == ["answerback_bridge.json"]


def test_install_replaces_a_current_file_left_group_readable_without_touching_its_inode(
    tmp_path: Path,
) -> None:
    """A 0644 file with the right content is not current: it is replaced by a new 0600
    file through the rename. The existing inode is never chmod'ed or written to, so a
    hard link made to it in the instant after the link-count check would see no change;
    the old inode keeps its mode and content under any other name it has."""
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    target = agent_path(home)
    target.write_bytes(expected_bytes())
    target.chmod(0o644)
    before = target.stat()
    kept = os.open(target, os.O_RDONLY)
    try:
        assert install_worker_agent(worker_profile(), home) is True
        after = target.stat()
        assert stat.S_IMODE(after.st_mode) == 0o600
        assert after.st_ino != before.st_ino
        assert target.read_bytes() == expected_bytes()
        old = os.fstat(kept)
        assert (old.st_ino, stat.S_IMODE(old.st_mode)) == (before.st_ino, 0o644)
        assert old.st_nlink == 0
    finally:
        os.close(kept)
    assert sorted(path.name for path in agents.iterdir()) == ["answerback_bridge.json"]


def test_install_never_changes_the_mode_of_an_existing_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only chmod is on the staged file the bridge created itself; no existing file is
    ever chmod'ed, whatever its mode or content, so there is no inspect-then-chmod window
    for a hard link to slip into."""
    home = tmp_path / "home"
    (home / ".kiro" / "agents").mkdir(parents=True)
    target = agent_path(home)
    target.write_bytes(b"{}\n")
    target.chmod(0o644)
    existing = target.stat().st_ino
    chmodded: list[int] = []
    real_fchmod = os.fchmod

    def fchmod(fd: int, mode: int) -> None:
        chmodded.append(os.fstat(fd).st_ino)
        real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", fchmod)
    assert install_worker_agent(worker_profile(), home) is True
    assert existing not in chmodded
    assert chmodded == [target.stat().st_ino]


def test_install_never_touches_other_agents(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    other = agents / "reviewer.json"
    other.write_text('{"name": "reviewer"}\n')
    install_worker_agent(worker_profile(), home)
    assert other.read_text() == '{"name": "reviewer"}\n'
    assert sorted(path.name for path in agents.iterdir()) == [
        "answerback_bridge.json",
        "reviewer.json",
    ]


@pytest.mark.parametrize("linked", ["home", ".kiro", "agents"])
def test_install_refuses_a_symlinked_folder_on_the_way(tmp_path: Path, linked: str) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    home = tmp_path / "home"
    if linked == "home":
        home.symlink_to(elsewhere)
    else:
        home.mkdir()
        if linked == ".kiro":
            (home / ".kiro").symlink_to(elsewhere)
        else:
            (home / ".kiro").mkdir()
            (home / ".kiro" / "agents").symlink_to(elsewhere)
    with pytest.raises(KiroAgentError, match="Refusing to go through a symlink"):
        install_worker_agent(worker_profile(), home)
    assert list(elsewhere.iterdir()) == []


def test_install_refuses_to_replace_a_symlinked_agent_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("keep\n")
    (agents / "answerback_bridge.json").symlink_to(victim)
    with pytest.raises(KiroAgentError, match="Refusing to replace a symlink"):
        install_worker_agent(worker_profile(), home)
    assert victim.read_text() == "keep\n"
    assert (agents / "answerback_bridge.json").is_symlink()
    assert sorted(path.name for path in agents.iterdir()) == ["answerback_bridge.json"]


def test_install_refuses_a_dangling_symlink_where_the_file_belongs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "answerback_bridge.json").symlink_to(tmp_path / "absent.json")
    with pytest.raises(KiroAgentError, match="Refusing to replace a symlink"):
        install_worker_agent(worker_profile(), home)
    assert not (tmp_path / "absent.json").exists()


@pytest.mark.parametrize("victim_content", [b"keep\n", None])
def test_install_refuses_a_hard_linked_agent_file(
    tmp_path: Path, victim_content: bytes | None
) -> None:
    """A second hard link means the entry also lives under another name; the bridge does
    not adopt a file it would share. Refused before any mutation: the victim keeps its
    mode, content and both names whether its content differs from the agent document (a
    replacement would have been due) or already matches it (only its mode is wrong)."""
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    victim = home / ".kiro" / "agents" / "other.json"
    victim.write_bytes(expected_bytes() if victim_content is None else victim_content)
    victim.chmod(0o644)
    os.link(victim, agents / "answerback_bridge.json")
    before = victim.stat()
    assert before.st_nlink == 2
    with pytest.raises(KiroAgentError, match="hard-linked elsewhere"):
        install_worker_agent(worker_profile(), home)
    after = victim.stat()
    assert stat.S_IMODE(after.st_mode) == 0o644
    assert (after.st_ino, after.st_nlink) == (before.st_ino, 2)
    assert victim.read_bytes() == (expected_bytes() if victim_content is None else victim_content)
    assert (agents / "answerback_bridge.json").stat().st_ino == before.st_ino
    assert sorted(path.name for path in agents.iterdir()) == [
        "answerback_bridge.json",
        "other.json",
    ]


def test_install_refuses_what_is_not_a_regular_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    os.mkfifo(agents / "answerback_bridge.json")
    with pytest.raises(KiroAgentError, match="not a regular file"):
        install_worker_agent(worker_profile(), home)


def test_install_refuses_a_file_where_a_folder_should_be(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".kiro").mkdir(parents=True)
    (home / ".kiro" / "agents").write_text("not a folder")
    with pytest.raises((KiroAgentError, NotADirectoryError)):
        install_worker_agent(worker_profile(), home)
    assert (home / ".kiro" / "agents").read_text() == "not a folder"


@pytest.mark.parametrize("foreign", ["home", ".kiro", "agents", "file"])
def test_install_refuses_folders_and_files_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: str
) -> None:
    """Ownership is compared with `os.getuid()`; a test cannot chown, so the comparison is
    driven from the other side: every path the test made is owned by the real user, and
    a `getuid` that reports another user makes exactly the checked path foreign."""
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    target = agent_path(home)
    if foreign == "file":
        target.write_bytes(expected_bytes())
    real_uid = os.getuid()
    checked: dict[str, int] = {"home": 0, ".kiro": 1, "agents": 2, "file": 3}
    seen = {"count": 0}
    real_fstat = os.fstat

    def fstat(fd: int) -> Any:
        info = real_fstat(fd)
        index = seen["count"]
        seen["count"] += 1
        if index == checked[foreign]:
            return os.stat_result((*info[:4], real_uid + 1, *info[5:]))
        return info

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(KiroAgentError, match="owned by another user"):
        install_worker_agent(worker_profile(), home)
    if foreign == "file":
        assert target.read_bytes() == expected_bytes()
    else:
        assert not target.exists()
    assert not [path for path in agents.iterdir() if path.name != "answerback_bridge.json"]


def test_install_refuses_an_oversized_existing_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    target = agent_path(home)
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(KiroAgentError, match="oversized"):
        install_worker_agent(worker_profile(), home)
    assert target.stat().st_size == 1024 * 1024 + 1


def test_install_leaves_no_staged_file_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)

    def failing_rename(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("rename refused")

    monkeypatch.setattr(os, "rename", failing_rename)
    with pytest.raises(PermissionError):
        install_worker_agent(worker_profile(), home)
    assert list(agents.iterdir()) == []


def test_install_refuses_a_foreign_owner_found_at_the_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ownership check is repeated on the descriptor held open across the rename;
    a final stat that reports another user refuses the replacement. The stat is the
    fifth `os.fstat` call: home, `.kiro`, `agents`, the first look at the file, then
    this one."""
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    target = agent_path(home)
    target.write_bytes(b"{}\n")
    real_uid = os.getuid()
    seen = {"count": 0}
    real_fstat = os.fstat

    def fstat(fd: int) -> Any:
        info = real_fstat(fd)
        index = seen["count"]
        seen["count"] += 1
        if index == 4:
            return os.stat_result((*info[:4], real_uid + 1, *info[5:]))
        return info

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(KiroAgentError, match="owned by another user"):
        install_worker_agent(worker_profile(), home)
    assert target.read_bytes() == b"{}\n"
    assert sorted(path.name for path in agents.iterdir()) == ["answerback_bridge.json"]


def test_install_refuses_a_file_swapped_in_between_the_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different inode at the agent name when the rename is due means the entry is no
    longer the file that was inspected; it is refused, not overwritten. The swap happens
    during the staged file's fsync, which sits between the two checks."""
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    target = agent_path(home)
    target.write_bytes(b"{}\n")
    swapped = agents / "swapped.json"
    swapped.write_bytes(b"swapped in\n")
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        real_fsync(fd)
        os.replace(swapped, target)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(KiroAgentError, match="changed while being replaced"):
        install_worker_agent(worker_profile(), home)
    assert target.read_bytes() == b"swapped in\n"
    assert sorted(path.name for path in agents.iterdir()) == ["answerback_bridge.json"]


def test_docs_state_what_the_agent_file_write_does_not_defend_against() -> None:
    """`os.rename` resolves the entry's name again after the last descriptor check, so a
    same-user swap in that instant is overwritten, not refused. The docs must say so
    rather than claim the final check pins the rename target, and must keep the list of
    what is refused; the source comment at the rename names the same window."""
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "NATIVE-CLI.md").read_text()
    body = text.split("## The Kiro agent file", 1)[1].split("## Exit codes", 1)[0]
    section = " ".join(body.split())
    assert "What is refused, before anything is written:" in section
    for refused in ("symlink", "another user", "second hard link", "FIFO", "over 1 MiB"):
        assert refused in section
    assert "What this does not defend against:" in section
    window = "swapping the entry in the very instant between that last check and the rename"
    assert window in section
    assert "inside the account trust boundary" in section
    assert "never written to or chmod'ed" in section
    assert "cannot be swapped" not in section
    assert "held open across the rename" not in section
    source = (root / "src" / "answerback_bridge" / "providers" / "kiro" / "agent.py").read_text()
    assert "cannot be swapped" not in source
    assert "held open across the rename" not in source
    assert "a same-user swap in the instant between the" in source


def test_install_refuses_a_file_that_appeared_between_the_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    target = agent_path(home)
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        real_fsync(fd)
        target.write_bytes(b"appeared\n")

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(KiroAgentError, match="changed while being replaced"):
        install_worker_agent(worker_profile(), home)
    assert target.read_bytes() == b"appeared\n"
    assert sorted(path.name for path in agents.iterdir()) == ["answerback_bridge.json"]


def test_install_accepts_the_same_content_written_by_another_worker_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers starting at once both find the file stale; the second must not fail
    because the first got there. An entry that already reads as the agent document at
    mode 0600 (what the other worker writes) is left alone and reported as not written."""
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    target = agent_path(home)
    target.write_bytes(b"{}\n")
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        real_fsync(fd)
        other = agents / "other-worker.json"
        other.write_bytes(expected_bytes())
        other.chmod(0o600)
        os.replace(other, target)

    monkeypatch.setattr(os, "fsync", fsync)
    assert install_worker_agent(worker_profile(), home) is False
    assert target.read_bytes() == expected_bytes()
    assert sorted(path.name for path in agents.iterdir()) == ["answerback_bridge.json"]
