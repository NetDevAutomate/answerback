"""Projectors: receipts, peers and board files restated as events and state."""

import errno
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from events_helpers import PEER, RUN, Script, collector, peer, receipt, types

from answerback_bridge.events import model
from answerback_bridge.events.collector import Collector, Settings, read_receipt_ids, read_receipts
from answerback_bridge.events.hub import EventHub
from answerback_bridge.events.patch import apply, diff
from answerback_bridge.events.producers import (
    RunTracker,
    parse_decision,
    peers_section,
    project_root,
    read_board,
    run_state,
    tail_content,
)
from answerback_bridge.tasks import TaskStore

NOW = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)
DECISION = """---
id: D-20261117-09
task: T-14
class: reserved            # delegable | reserved
status: open
opened_at: 2026-11-17T09:00:00Z
needed_by: {needed_by}
---

# Approve the public push of v0.11.0?

## Answer
answer:{answer}
answered_by:
"""


@pytest.mark.parametrize("ident", ["herdr:" + "x" * 2048, "herdr:peer\x1b[31m"])
def test_t74_inventory_omits_unbounded_or_nonprintable_peer_ids(ident: str) -> None:
    """red_at_base: rejected identities cannot enter through scans or partial carryover."""
    previous = {ident: {"provider": "herdr"}}
    section, _ = peers_section(
        {"peers": [peer(ident=ident), peer()], "errors": {"herdr": "TimeoutError"}},
        previous,
        {},
    )
    assert set(section) == {PEER}


def test_t74_long_valid_peer_ids_keep_exact_run_and_metrics_links() -> None:
    """pins_existing: supported Crew IDs are longer than the suggested 128-character cap."""
    ident = "crew:" + "c" * 512
    previous, _ = peers_section({"peers": [peer(ident=ident)]}, {}, {})
    previous[ident]["metrics"]["tokens"] = 74
    current, _ = peers_section({"peers": [peer(ident=ident)]}, previous, {ident: RUN})
    assert set(current) == {ident}
    assert current[ident]["metrics"]["tokens"] == 74
    assert current[ident]["runId"] == RUN


@pytest.mark.parametrize("length", [1024, 1025])
def test_t74r1_inventory_identity_boundary(length: int) -> None:
    """pins_existing: keep the exact public boundary, including partial carryover."""
    from answerback_bridge.events.producers import MAX_PEER_ID

    assert MAX_PEER_ID == 1024
    ident = "herdr:" + "x" * (length - 6)
    section, _ = peers_section(
        {"peers": [peer(ident=ident)], "errors": {"herdr": "TimeoutError"}},
        {ident: {"provider": "herdr"}},
        {},
    )
    assert (ident in section) is (length == MAX_PEER_ID)


async def test_t74r1_inventory_omissions_are_visible_and_clear_on_recovery() -> None:
    """red_at_review_tip: rejected scan identities surface through existing source errors."""
    from answerback_bridge.events.board import Board

    script = Script()
    script.scan["peers"] = [
        peer(),
        peer(ident="herdr:" + "x" * 1024),
        peer(ident="herdr:bad\x1b"),
    ]
    observer = collector(script)
    await observer.tick_slow()
    board = Board()
    for item in observer.hub.read()["events"]:
        board.apply(item["id"], item["event"])
    assert "peerIdentifiers: 2 peer identifiers omitted" in board.render()
    script.scan["peers"] = [peer()]
    await observer.tick_slow()
    assert "peerIdentifiers" not in observer.hub.state["bridge"]["sourceErrors"]


def test_t74r1_run_projection_bounds_receipt_text_without_identity_collisions() -> None:
    """red_at_review_tip: every receipt text field is bounded before entering runs state."""
    tracker = RunTracker()
    tracker.observe(
        [
            receipt(
                "s" * 65536,
                101,
                head="",
                request_key="q" * 65536,  # pragma: allowlist secret
                kind="p" * 65536,
            )
        ]
    )
    run = tracker.runs[RUN]
    run.peer_id = "hc:" + "x" * 1024
    run.subagent = "s" * 65536
    row = run_state(run)
    for field, maximum in (
        ("threadId", 128),
        ("provider", 32),
        ("requestId", 128),
        ("status", 32),
        ("step", 32),
        ("subagentRunId", 1280),
    ):
        assert row[field] is None or len(row[field]) <= maximum, field
    assert row["peerId"] == ""
    assert row["peerIdOmitted"] is True
    assert row["subagentRunId"] is None
    assert row["subagentRunIdOmitted"] is True
    assert run.peer_id == "hc:" + "x" * 1024  # Routing identity is unchanged.


@pytest.mark.parametrize("field", ["peer_id", "target"])
def test_t74_receipt_identity_is_bounded_at_the_hub(field: str) -> None:
    """red_at_base: receipt metadata bypasses the inventory projector."""
    row = receipt("submitted", 101, peer_id=None)
    row[field] = "herdr:" + "x" * 65536
    hub = EventHub()
    for event in RunTracker().observe([row]):
        hub.publish(event)
    events = [item["event"] for item in hub.read(run=RUN)["events"]]
    assert events[0]["type"] == "RUN_STARTED"
    assert events[0]["metadata"]["answerback"]["oversized"] is True
    assert all(len(json.dumps(event).encode()) <= 65536 for _, event in hub.ring)
    assert all(model.run_id_of(event) == RUN for event in events)


def ledger(root: Path, *, needed_by: str = "2026-11-17T16:00:00Z", answer: str = "") -> Path:
    decisions = root / "status" / "decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / "D-20261117-09.md").write_text(DECISION.format(needed_by=needed_by, answer=answer))
    (decisions / "TEMPLATE.md").write_text("---\nid: D-YYYYMMDD-NN\n---\n")
    queue = root / "status" / "apply-queue"
    (queue / "done").mkdir(parents=True, exist_ok=True)
    (queue / "T-16-events.json").write_text("{}")
    (queue / "done" / "T-04-release-prep.json").write_text("{}")
    (queue / "done" / "T-04-release-prep.just-check.log").write_text("ok")
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("81fc3840aa11bb22cc33dd44ee55ff6677889900\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "a68b2e8000000000000000000000000000000000 refs/heads/task-T16-events\n"
    )
    worktree = root.parent / "wt-t16"
    worktree.mkdir(exist_ok=True)
    (worktree / "status").mkdir(exist_ok=True)
    (git / "worktrees" / "wt-t16").mkdir(parents=True, exist_ok=True)
    (git / "worktrees" / "wt-t16" / "HEAD").write_text("ref: refs/heads/task-T16-events\n")
    (git / "worktrees" / "wt-t16" / "gitdir").write_text(f"{worktree}/.git\n")
    (worktree / ".git").write_text(f"gitdir: {git}/worktrees/wt-t16\n")
    return worktree


@pytest.mark.parametrize("symlinked", [False, True])
def test_t191_project_root_rejects_unbounded_or_symlinked_git_pointers(
    tmp_path: Path, symlinked: bool
) -> None:
    """red_at_76db6d7a: project_root accepts both attacker-controlled marker forms."""
    root = tmp_path / "repo"
    root.mkdir()
    pointer = tmp_path / "large-git-pointer"
    pointer.write_bytes(b"gitdir: /scratch/not-worktree/" + b"x" * 4096)
    marker = root / ".git"
    if symlinked:
        marker.symlink_to(pointer)
    else:
        marker.write_bytes(pointer.read_bytes())

    assert project_root(str(root)) is None


def test_board_files_are_read_into_state(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    worktree = ledger(root)
    # A worker's directory inside a worktree resolves to the one ledger of its main checkout.
    assert project_root(str(worktree / "status")) == root
    board = read_board([str(worktree), str(root), "relative/path", str(tmp_path / "missing")], NOW)

    assert list(board["decisions"]) == ["D-20261117-09"]
    decision = board["decisions"]["D-20261117-09"]
    assert decision == {
        "task": "T-14",
        "class": "reserved",
        "status": "open",
        "title": "Approve the public push of v0.11.0?",
        "neededBy": "2026-11-17T16:00:00+00:00",
        "answered": False,
        "overdue": True,
        "project": str(root),
    }
    assert board["applyQueue"] == {
        str(root): {"pending": ["T-16-events"], "done": ["T-04-release-prep"]}
    }
    assert board["heads"] == {
        str(root): {
            str(root): {"branch": "main", "sha": "81fc3840aa11"},
            str(worktree): {"branch": "task-T16-events", "sha": "a68b2e800000"},
        }
    }


def board_project(root: Path) -> None:
    """Create only local ledger files; no git process or live source is involved."""
    (root / ".git").mkdir(parents=True)
    (root / "status" / "decisions").mkdir(parents=True)
    (root / "status" / "apply-queue").mkdir()
    (root / ".git" / "HEAD").write_text("a" * 40)


def test_t74r2_queue_keeps_existing_stem_sort_window(tmp_path: Path) -> None:
    """pins_existing: directory buckets select the last 50 stems, not full filenames."""
    board_project(tmp_path / "repo")
    root = tmp_path / "repo"
    bucket = root / "status" / "apply-queue" / "done"
    bucket.mkdir()
    stems = ["a", "a!", *(f"b-{index:02}" for index in range(49))]
    for stem in stems:
        (bucket / (stem + ".json")).write_text("{}")
    assert read_board([str(root)], NOW)["applyQueue"][str(root)]["done"] == sorted(stems)[-50:]


def board_decision(root: Path, name: str, ident: str) -> Path:
    path = root / "status" / "decisions" / name
    path.write_text(f"---\nid: {ident}\nclass: delegable\nstatus: open\n---\n# A decision\n")
    return path


def file_board_collector(roots: list[Path]) -> Collector:
    observer = collector(Script(), full=True)
    observer.settings = Settings(projects=tuple(str(root) for root in roots))

    async def files(paths: list[str]) -> dict[str, Any]:
        return read_board(paths, NOW)

    observer.sources.board = files
    return observer


@pytest.mark.parametrize("section", ["decisions", "applyQueue"])
async def test_t74r2_invalid_board_rows_do_not_freeze_other_projects(
    tmp_path: Path, section: str
) -> None:
    """red_at_review_tip: four real scans retain, add and remove valid neighbours."""
    from answerback_bridge.events.board import Board

    good, bad = tmp_path / "good", tmp_path / "bad"
    for root in (good, bad):
        board_project(root)
    if section == "decisions":
        old = board_decision(good, "D-1.md", "D-GOOD")
        board_decision(bad, "D-2.md", "D-OTHER")
        invalid = [board_decision(bad, "D-3.md", "\x01" * 12000)]
        error = "decisionIdentifiers: 1 decision identifiers omitted"
    else:
        old = good / "status" / "apply-queue" / "T-GOOD.json"
        old.write_text("{}")
        (bad / "status" / "apply-queue" / "T-OTHER.json").write_text("{}")
        invalid = []
        for index in range(50):
            path = bad / "status" / "apply-queue" / ("\x01" * 245 + f"{index:02}.json")
            path.write_text("{}")
            invalid.append(path)
        error = "queueFileNames: 50 queue file names omitted"
    observer = file_board_collector([good, bad])
    cursor = observer.hub.read()["cursor"]
    for tick in range(4):
        if tick == 2:
            if section == "decisions":
                board_decision(good, "D-4.md", "D-ADDED")
            else:
                (old.parent / "T-ADDED.json").write_text("{}")
        if tick == 3:
            old.unlink()
        await observer.tick_slow()
        page = observer.hub.read(cursor)
        cursor = page["cursor"]
        assert page["resync"] is False
        state = observer.hub.state[section]
        if section == "decisions":
            assert set(state) == (
                {"D-OTHER"}
                | ({"D-GOOD"} if tick < 3 else set())
                | ({"D-ADDED"} if tick >= 2 else set())
            )
        else:
            assert set(state[str(good)]["pending"]) == (
                ({"T-GOOD"} if tick < 3 else set()) | ({"T-ADDED"} if tick >= 2 else set())
            )
            assert state[str(bad)]["pending"] == ["T-OTHER"]
        board = Board()
        for item in observer.hub.read()["events"]:
            board.apply(item["id"], item["event"])
        assert error in board.render()
        assert sum(e.get("name") == "answerback.oversized" for _, e in observer.hub.ring) <= 1
    for path in invalid:
        path.unlink()
    await observer.tick_slow()
    assert error.split(":")[0] not in observer.hub.state["bridge"]["sourceErrors"]


def padded_path(prefix: Path, size: int) -> Path:
    """A printable path of exactly size UTF-8 bytes, including the temporary prefix."""
    value = str(prefix)
    while len(value.encode()) < size:
        if size - len(value.encode()) == 1:
            value += "x"
            break
        remaining = size - len(value.encode()) - 1
        length = min(180, remaining)
        value += "/" + "Ж" * (length // 2) + "x" * (length % 2)
    assert len(value.encode()) == size
    return Path(value)


@pytest.mark.parametrize("section", ["decisions", "applyQueue", "heads"])
async def test_t74r2_largest_file_board_entries_fit_bounded_deltas(
    tmp_path: Path, section: str
) -> None:
    """pins_existing for decisions; red_at_review_tip for initial queue/HEAD admission."""
    root = padded_path(tmp_path, 512)
    board_project(root)
    if section == "decisions":
        for index in range(200):
            ident = "Ж" * 126 + f"{index:04}"
            path = board_decision(root, f"D-{index:03}.md", ident)
            path.write_text(
                f"---\nid: {ident}\ntask: {'😀' * 32}\nclass: {'😀' * 16}\n"
                f"status: {'😀' * 16}\ntitle: {'😀' * 200}\n"
                f"answer: yes\nanswered_by: {'😀' * 128}\n"
                "answered_at: 2026-11-17T12:00:00.999999+00:00\n"
                "needed_by: 2026-11-17T12:00:00.999999+00:00\n---\n"
            )
        expected_count = 200
    elif section == "applyQueue":
        base = root / "status" / "apply-queue"
        for index in range(17):
            bucket = base / ("Ж" * 126 + f"{index:03}")
            bucket.mkdir()
            directory = os.open(bucket, os.O_RDONLY)
            try:
                for number in range(50):
                    # dir_fd supports the full name budgets even with macOS PATH_MAX.
                    fd = os.open(
                        "Ж" * 124 + f"{number:02}.json",
                        os.O_WRONLY | os.O_CREAT,
                        0o600,
                        dir_fd=directory,
                    )
                    os.close(fd)
            finally:
                os.close(directory)
        for number in range(50):
            (base / ("Ж" * 124 + f"{number:02}.json")).write_text("{}")
        expected_count = 17  # pending plus the first 16 directory buckets.
    else:
        # Gitdir files hold full path text without needing actual 4 KiB filesystem paths.
        for index in range(64):
            gitdir = root / ".git" / "worktrees" / f"wt-{index:02}"
            gitdir.mkdir(parents=True)
            location = str(padded_path(Path(f"/work-{index:02}"), 4096))
            (gitdir / "gitdir").write_text(location + "/.git\n")
            (gitdir / "HEAD").write_text("ref: refs/heads/" + "\x01" * 497)
        expected_count = 65
    wanted = read_board([str(root)], NOW)[section]
    count = len(wanted) if section == "decisions" else len(wanted[str(root)])
    assert count == expected_count
    if section == "applyQueue":
        assert all(len(names) == 50 for names in wanted[str(root)].values())
        assert all(
            len((name + ".json").encode()) == 255
            for names in wanted[str(root)].values()
            for name in names
        )
    if section == "heads":
        assert all(path == str(root) or len(path.encode()) == 4096 for path in wanted[str(root)])
    observer = file_board_collector([root])
    cursor = observer.hub.read()["cursor"]
    for _ in range(2):
        await observer.tick_slow()
        assert observer.hub.state[section] == wanted
        page = observer.hub.read(cursor)
        cursor = page["cursor"]
        assert page["resync"] is False
        assert observer.hub.omitted_updates == 0
        assert all(len(json.dumps(e).encode()) <= 65536 for _, e in observer.hub.ring)
    # A stream consumer can apply all first-sight parent/child operations in order.
    from answerback_bridge.events.board import Board

    board = Board()
    from answerback_bridge.events.hub import empty_state

    board.apply("initial", model.state_snapshot(empty_state()))
    for seq, event in observer.hub.ring:
        board.apply(observer.hub.ident(seq), event)
    assert board.state[section] == wanted


async def test_t74r2_board_omission_kinds_are_named_and_recover(tmp_path: Path) -> None:
    """red_at_review_tip: byte/printability refusals and bucket overflow reach the board."""
    from answerback_bridge.events.board import Board

    root = tmp_path / "repo"
    board_project(root)
    board_decision(root, "D-1.md", "Ж" * 128)  # Exactly 256 UTF-8 bytes.
    board_decision(root, "D-2.md", "Ж" * 128 + "x")
    queue = root / "status" / "apply-queue"
    (queue / "bad\x01").mkdir()
    (queue / "bad\x01.json").write_text("{}")
    for index in range(17):
        (queue / f"bucket-{index:02}").mkdir()
    gitdir = root / ".git" / "worktrees" / "bad"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("a" * 40)
    (gitdir / "gitdir").write_text("/tmp/bad\x01/.git\n")
    # A root over the bound is omitted and reported independently of worktree paths.
    long_root = padded_path(tmp_path / "long", 513)
    board_project(long_root)
    observer = file_board_collector([root, long_root])
    await observer.tick_slow()
    board = Board()
    for item in observer.hub.read()["events"]:
        board.apply(item["id"], item["event"])
    rendered = board.render()
    for key, noun in (
        ("decisionIdentifiers", "decision identifiers"),
        ("queueFileNames", "queue file names"),
        ("queueBucketNames", "queue bucket names"),
        ("queueBuckets", "queue buckets"),
        ("worktreePaths", "worktree paths"),
        ("projectPaths", "project paths"),
    ):
        assert f"{key}: 1 {noun} omitted" in rendered
    assert "Ж" * 128 in observer.hub.state["decisions"]
    assert str(long_root) not in observer.hub.state["heads"]
    observer.sources.board = Script().read_board
    await observer.tick_slow()
    assert not observer.hub.state["bridge"]["sourceErrors"]


@pytest.mark.parametrize(
    ("kind", "maximum"),
    [
        ("decisionIdentifiers", 256),
        ("decisionFilename", 256),
        ("queueFileNames", 255),
        ("queueBucketNames", 255),
        ("worktreePaths", 4096),
        ("projectPaths", 512),
    ],
)
@pytest.mark.parametrize("case", ["exact", "over", "control"])
def test_t74r2_file_identity_boundaries(tmp_path: Path, kind: str, maximum: int, case: str) -> None:
    """red_at_review_tip for refusals/exact long names; pins_existing for ordinary limits."""
    root = tmp_path / "repo"
    size = maximum + (case == "over")
    if kind == "projectPaths":
        root = padded_path(tmp_path, size)
        if case == "control":
            root = root.with_name(root.name + "\x01")
    board_project(root)
    if kind in ("worktreePaths", "projectPaths"):
        ident = str(root) if kind == "projectPaths" else str(padded_path(Path("/work"), size))
    else:
        ident = "Ж" * (size // 2) + "x" * (size % 2)
    if case == "control" and kind != "projectPaths":
        ident = ident[:-1] + "\x01"
    try:
        if kind == "decisionIdentifiers":
            board_decision(root, "D-1.md", ident)
        elif kind == "decisionFilename":
            ident = "D-" + ident[1:]
            (root / "status" / "decisions" / (ident + ".md")).write_text("---\n---\n")
        elif kind == "queueFileNames":
            ident = "Ж" * ((size - 5) // 2) + "x" * ((size - 5) % 2) + ".json"
            if case == "control":
                ident = ident[:1] + "\x01" + ident[2:]
            (root / "status" / "apply-queue" / ident).write_text("{}")
        elif kind == "queueBucketNames":
            (root / "status" / "apply-queue" / ident).mkdir()
        elif kind == "worktreePaths":
            gitdir = root / ".git" / "worktrees" / "wt"
            gitdir.mkdir(parents=True)
            (gitdir / "HEAD").write_text("a" * 40)
            (gitdir / "gitdir").write_text(ident + "/.git\n")
    except OSError as error:
        if error.errno == errno.ENAMETOOLONG:
            pytest.skip("filesystem rejects this identity before the reader can see it")
        raise
    board = read_board([str(root)], NOW)
    if kind in ("decisionIdentifiers", "decisionFilename"):
        present = ident in board["decisions"]
    elif kind == "queueFileNames":
        present = ident[:-5] in board["applyQueue"][str(root)]["pending"]
    elif kind == "queueBucketNames":
        present = ident in board["applyQueue"][str(root)]
    elif kind == "worktreePaths":
        present = ident in board["heads"][str(root)]
    else:
        present = ident in board["heads"]
    assert present is (case == "exact")
    counter = "decisionIdentifiers" if kind == "decisionFilename" else kind
    assert board.get("omissions", {}).get(counter, 0) == (case != "exact")


def test_board_reader_follows_no_symlink_and_survives_bad_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    ledger(root)
    secret = tmp_path / "secret.md"
    secret.write_text("---\nid: D-LEAK\ntask: PRIVATE\n---\n# private\n")
    os.symlink(secret, root / "status" / "decisions" / "D-20261117-10.md")
    (root / "status" / "decisions" / "D-20261117-11.md").write_bytes(b"\xff\xfe not front matter")
    (root / ".git" / "HEAD").write_text("ref: refs/../../etc/passwd\n")
    board = read_board([str(root)], NOW)
    assert "D-LEAK" not in board["decisions"] and "D-20261117-10" not in board["decisions"]
    assert board["decisions"]["D-20261117-11"]["status"] == ""
    assert str(root) not in board["heads"][str(root)]
    assert parse_decision("no front matter") == {}
    assert parse_decision("") == {}
    # Older decision files carry the same header without the fences.
    bare = (
        "id: D-20261115-01\ntask: T-04\nstatus: answered\nneeded_by:\n"
        "\n# Is it true?\n\nanswer: yes\n"
    )
    assert parse_decision(bare) == {
        "id": "D-20261115-01",
        "task": "T-04",
        "status": "answered",
        "needed_by": "",
        "title": "Is it true?",
        "answer": "yes",
    }
    assert parse_decision("---\nid: x\nnever closed") == {}


async def test_overdue_reserved_decision_is_an_interrupt_announced_once(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    ledger(root)
    script = Script()
    script.board = read_board([str(root)], NOW)
    observer = collector(script, full=True)
    cursor = observer.hub.read()["cursor"]
    await observer.tick()
    await observer.tick()

    events = [item["event"] for item in observer.hub.read(cursor)["events"]]
    runs = [event for event in events if event["type"] in ("RUN_STARTED", "RUN_FINISHED")]
    assert [event["type"] for event in runs] == ["RUN_STARTED", "RUN_FINISHED"]
    assert runs[0]["threadId"] == "T-14" and runs[0]["runId"] == "decision:D-20261117-09"
    assert runs[1]["outcome"] == {
        "type": "interrupt",
        "interrupts": [
            {
                "id": "D-20261117-09",
                "reason": "decision_overdue",
                "message": "Approve the public push of v0.11.0?",
                "metadata": {
                    "answerback": {"neededBy": "2026-11-17T16:00:00+00:00", "class": "reserved"}
                },
            }
        ],
    }
    assert observer.hub.state["decisions"]["D-20261117-09"]["overdue"] is True


async def test_t74_overdue_memory_tracks_only_the_visible_decisions() -> None:
    """red_at_base: repeated scans forget removed IDs but deduplicate visible ones."""
    script = Script()
    observer = collector(script, full=True)
    decision = {
        "task": "T-74",
        "class": "reserved",
        "overdue": True,
        "title": "A decision",
        "neededBy": "2026-11-17T10:00:00Z",
    }
    for generation in range(8):
        visible = {f"D-{generation}-{index}": dict(decision) for index in range(5)}
        script.board["decisions"] = visible
        await observer.tick_slow()
        assert observer.interrupted == set(visible)
        cursor = observer.hub.read()["cursor"]
        await observer.tick_slow()
        assert "RUN_STARTED" not in types(observer.hub.read(cursor))
    script.board["decisions"] = {}
    await observer.tick_slow()
    assert not observer.interrupted
    script.board["decisions"] = {"D-0-0": decision}
    cursor = observer.hub.read()["cursor"]
    await observer.tick_slow()
    assert "RUN_STARTED" in types(observer.hub.read(cursor))
    assert observer.interrupted == {"D-0-0"}


@pytest.mark.parametrize(
    ("needed_by", "answer", "klass"),
    [
        ("2026-11-18T16:00:00Z", "", "reserved"),  # not due yet
        ("2026-11-17T16:00:00Z", " approve", "reserved"),  # answered
        ("2026-11-17T16:00:00Z", "", "delegable"),  # past deadline = default, no interrupt
        ("", "", "reserved"),  # no deadline
    ],
)
async def test_other_decisions_do_not_interrupt(
    tmp_path: Path, needed_by: str, answer: str, klass: str
) -> None:
    root = tmp_path / "repo"
    ledger(root, needed_by=needed_by, answer=answer)
    path = root / "status" / "decisions" / "D-20261117-09.md"
    path.write_text(path.read_text().replace("class: reserved", f"class: {klass}"))
    script = Script()
    script.board = read_board([str(root)], NOW)
    observer = collector(script, full=True)
    await observer.tick()
    assert "RUN_FINISHED" not in types(observer.hub.read(observer.hub.ident(0)))


def test_peer_inventory_keeps_a_failed_providers_peers_and_tracer_metrics() -> None:
    metrics = {"tokens": 1200, "cpuPercent": 3.5, "rssBytes": 1, "children": 2}
    previous, _ = peers_section({"peers": [peer(), peer(ident="crew:chat-1")]}, {}, {})
    previous[PEER]["metrics"] = metrics
    section, partial = peers_section(
        {"peers": [peer("idle"), "junk", {"peer_id": 5}], "errors": {"crew": "timed out"}},
        previous,
        {PEER: RUN},
    )
    assert partial == {"crew"}
    assert section["crew:chat-1"] == previous["crew:chat-1"]
    assert section[PEER]["metrics"] == metrics and section[PEER]["runId"] == RUN
    assert (section[PEER]["state"], section[PEER]["sendReady"]) == ("idle", True)


def test_a_worker_that_vanishes_fails_its_subagent_and_gets_a_new_id_on_return() -> None:
    tracker = RunTracker()
    tracker.observe([receipt("submitted", 1.0)])
    present, _ = peers_section({"peers": [peer()]}, {}, {})
    assert [e["type"] for e in tracker.attach(present, set())] == ["SUBAGENT_STARTED"]
    assert tracker.attach(present, set()) == []
    # An incomplete scan of that provider proves nothing about the worker.
    assert tracker.attach({}, {"hc"}) == []
    gone = tracker.attach({}, set())
    assert [(e["type"], e["code"]) for e in gone] == [("SUBAGENT_ERROR", "peer_gone")]
    back = tracker.attach(present, set())
    assert back[0]["subagentRunId"] == f"{PEER}#{RUN}#2"
    # A queued run on an idle worker has not been picked up: no subagent yet.
    waiting = RunTracker()
    waiting.observe([receipt("pending", 1.0)])
    idle, _ = peers_section({"peers": [peer("idle")]}, {}, {})
    assert waiting.attach(idle, set()) == []


def test_runs_are_bounded_and_unfinished_ones_are_never_left_out() -> None:
    tracker = RunTracker(limit=4)
    for index in range(10):
        tracker.observe([receipt("completed", float(index), run=f"run-{index}")])
    tracker.observe([receipt("running", 99.0, run="open")])
    assert sorted(tracker.runs) == ["open", "run-7", "run-8", "run-9"]
    assert tracker.omitted == 7


def test_thread_id_comes_from_the_brief_then_the_request_id() -> None:
    assert model.thread_id("Assignment ... Task T-16. Routed", "anything") == "T-16"
    assert model.thread_id(None, "ab-20261117-T16-design-t61") == "T-16"
    assert model.thread_id("no task named", "ab-20261117-T-7-fix") == "T-07"
    assert model.thread_id("", "nightly-audit") == "nightly-audit"
    assert model.thread_id("", "CAT-16") == "CAT-16"


def test_tail_is_a_digest_and_sizes_with_no_text() -> None:
    text = "one\n\n" + "x" * 500 + "\n\x1b[31mred\x07\n"
    content = tail_content(PEER, RUN, text)
    assert set(content) == {"peerId", "runId", "sha256", "lineCount", "chars", "changedMs"}
    assert len(content["sha256"]) == 64 and content["peerId"] == PEER
    assert (content["lineCount"], content["chars"]) == (3, len(text))
    assert "red" not in str(content) and "xxx" not in str(content)
    # The change time moves only when the text does.
    earlier = {**content, "changedMs": 5}
    assert tail_content(PEER, RUN, text, previous=earlier)["changedMs"] == 5
    changed = tail_content(PEER, RUN, text + "more", previous=earlier)
    assert changed["sha256"] != content["sha256"] and changed["changedMs"] > 5


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"a": {"b": 1}}, {"a": {"b": 2, "c": [1, 2]}}),
        ({"a/b": {"~x": 1}, "gone": 1}, {"a/b": {"~x": {"y": None}}}),
        ({"list": [1, 2, 3]}, {"list": [1, 3]}),
        ({"n": 1}, {"n": True}),
        ({}, {"x": {}}),
        ({"x": 1}, {"x": 1}),
    ],
)
def test_diff_then_apply_round_trips(old: Any, new: Any) -> None:
    operations = diff(old, new)
    assert apply(old, operations) == new
    assert all(op["op"] in ("add", "remove", "replace") for op in operations)
    # Python calls 1 and True equal; JSON does not, so compare what goes on the wire.
    assert json.dumps(apply(old, operations)) == json.dumps(new)
    if json.dumps(old) == json.dumps(new):
        assert operations == []


def test_apply_rejects_paths_that_do_not_resolve() -> None:
    for operation in (
        {"op": "replace", "path": "/missing", "value": 1},
        {"op": "remove", "path": "/a/missing"},
        {"op": "add", "path": "/a/b/c", "value": 1},
        {"op": "move", "path": "/a", "from": "/b"},
        {"op": "remove", "path": ""},
        {"op": "add", "path": "no-slash", "value": 1},
        {"op": "replace", "path": "/list/9", "value": 1},
    ):
        with pytest.raises(ValueError):
            apply({"a": {}, "list": [1]}, [operation])
    assert apply({"list": [1]}, [{"op": "add", "path": "/list/-", "value": 2}]) == {"list": [1, 2]}
    assert apply({"a": 1}, [{"op": "replace", "path": "", "value": [1]}]) == [1]


def test_receipts_are_read_without_bodies_through_a_read_only_connection(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    assert read_receipts(store.path, None) == []
    assert read_receipt_ids(store.path) == set()
    task, _ = store.reserve(
        "hc:default:t61",
        "Task T-16. PROMPT_PRIVATE " + "x" * 6000,
        "ab-20261117-T16",
        {"kind": "hc", "peer": {"peer_id": PEER}, "completion": "worker_report"},
    )
    (row,) = read_receipts(store.path, None)
    assert (row["id"], row["status"], row["kind"], row["peer_id"]) == (
        task["id"],
        "pending",
        "hc",
        PEER,
    )
    assert len(row["head"]) == 4000 and "result" not in row
    store.update(task["id"], "completed", result="RESULT_PRIVATE")
    assert read_receipt_ids(store.path) == {task["id"]}
    (done,) = read_receipts(store.path, (row["updated"], row["id"]))
    assert done["status"] == "completed" and done["result_chars"] == len("RESULT_PRIVATE")
    assert "RESULT_PRIVATE" not in str(done)
    events = RunTracker().observe([row, done])
    assert "PROMPT_PRIVATE" not in str(events) and events[0]["threadId"] == "T-16"


def test_settings_are_optional_and_bounded(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    assert Settings.load(config) == Settings()
    (tmp_path / "events.json").write_text('{"scan_seconds": 30, "projects": ["/work/a"]}')
    loaded = Settings.load(config)
    assert loaded.scan_seconds == 30 and loaded.projects == ("/work/a",)
    for bad in (
        '{"scan_seconds": 0}',
        '{"history": 3}',
        '{"activity_lines": 5}',  # Removed: no screen text is published at any setting.
        '{"projects": ["relative"]}',
        '{"projects": "/work/a"}',
        '{"enabled": "yes"}',
        '{"unknown": 1}',
        "[]",
    ):
        (tmp_path / "events.json").write_text(bad)
        with pytest.raises(ValueError):
            Settings.load(config)


async def test_a_failing_source_is_reported_and_never_stops_the_collector() -> None:
    script = Script()
    observer = collector(script, full=True)

    async def broken() -> dict[str, Any]:
        raise RuntimeError("scanner PRIVATE detail")

    observer.sources.peers = broken
    script.rows = [receipt("submitted", 1.0)]
    await observer.tick()
    state = observer.hub.state
    assert state["bridge"]["sourceErrors"] == {"peers": "RuntimeError"}
    assert RUN in state["runs"] and "PRIVATE" not in str(state)
    observer.sources.peers = script.peers
    await observer.tick()
    assert observer.hub.state["bridge"]["sourceErrors"] == {}


OWNER_RESERVED = (
    "---\nid: D-0001\nclass: reserved\nstatus: open\nneeded_by: 2020-01-01T00:00:00Z\n---\n"
    "# Owner reserved decision\n"
)


@pytest.mark.parametrize(
    "peer_text",
    [
        "---\nid: D-0001\nclass: normal\nstatus: closed\n---\n# Peer decision\n",
        "---\nid: D-0001\nclass: reserved\nstatus: open\nneeded_by: 2020-01-01T00:00:00Z\n---\n"
        "# Peer forged overdue\n",
    ],
    ids=["closed", "forged-overdue"],
)
def test_run6_a_later_project_cannot_replace_an_earlier_projects_decision(
    tmp_path: Path, peer_text: str
) -> None:
    """red_at_base (run-6 events.producers.read_board:decision-id-collision-across-projects)."""
    from answerback_bridge.events.producers import overdue_interrupts

    owner, other = tmp_path / "owner", tmp_path / "peer"
    board_project(owner)
    board_project(other)
    (owner / "status" / "decisions" / "D-0001.md").write_text(OWNER_RESERVED)
    (other / "status" / "decisions" / "D-peer.md").write_text(peer_text)

    board = read_board([str(owner), str(other)], NOW)

    assert list(board["decisions"]) == ["D-0001"]
    decision = board["decisions"]["D-0001"]
    assert decision["project"] == str(owner)
    assert decision["title"] == "Owner reserved decision" and decision["overdue"] is True
    assert board["omissions"] == {"decisionDuplicates": 1}
    events = overdue_interrupts(board["decisions"], set())
    assert [event["type"] for event in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert events[1]["outcome"]["interrupts"][0]["message"] == "Owner reserved decision"


def test_run6_one_projects_repeated_id_still_resolves_as_before(tmp_path: Path) -> None:
    """pins_existing: within one project the later file of a repeated id is kept, uncounted."""
    root = tmp_path / "repo"
    board_project(root)
    board_decision(root, "D-1.md", "D-SAME")
    (root / "status" / "decisions" / "D-2.md").write_text(
        "---\nid: D-SAME\nclass: delegable\nstatus: closed\n---\n# Second\n"
    )
    board = read_board([str(root)], NOW)
    assert board["decisions"]["D-SAME"]["title"] == "Second"
    assert board["omissions"] == {}
