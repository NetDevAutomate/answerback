"""status/board/render_board.py renders the kanban page from a status tree, without a live core.

`hc ls --json` is replayed from a fixture file (`--hc-json`), so no core is needed; the git HEAD
of a tmp root is "unknown". Run: `uv run --no-sync pytest tests/test_board.py -q`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / "status" / "board"
LANES = ("blocked", "doing", "review", "todo", "done")

TASKS = {
    "tasks": [
        {
            "id": "T-01",
            "lane": "blocked",
            "stage": "waiting on owner",
            "owner": "kirocrew",
            "evidence": "status/handoff/T-01.md",
            "note": "Blocked on the token rotation. Second sentence with detail.",
            "updated": "2026-11-18",
        },
        {
            "id": "T-02",
            "lane": "doing",
            "stage": "implementation",
            "owner": "codex",
            "evidence": "branch task-T02 @ abc1234",
            "note": "Doing the port fix (e.g. the sampler) on example-host. Then the review.",
            "updated": "2026-11-18",
        },
        {
            "id": "T-03",
            "lane": "review",
            "stage": "review",
            "owner": "grok",
            "evidence": "status/handoff/T-03-review.md",
            "note": "Independent review in flight.",
            "updated": "2026-11-17",
        },
        {
            "id": "T-04",
            "lane": "todo",
            "stage": "queued",
            "owner": "orchestrator",
            "evidence": "none yet",
            "note": "Release 0.12.0 after T-02 lands! Tag, build, reinstall.",
            "updated": "2026-11-16",
        },
        {
            "id": "T-05",
            "lane": "done",
            "stage": "merged",
            "owner": "conductor",
            "evidence": "merge c4d2ebe",
            "note": 'Event stream merged <with> "quotes" & ampersands.',
            "updated": "2026-11-15",
        },
        {
            "id": "T-06",
            "lane": "done",
            "stage": "merged",
            "owner": "conductor",
            "evidence": "merge d045e22",
            "note": "Routing merged.",
            "updated": "2026-11-18",
        },
    ]
}

HC_LS = {
    "hidden_finished": 3,
    "machines": [{"name": "example-host", "state": "connected"}],
    "terminals": [
        {"address": "t53", "agent": "zsh", "state": "live", "turn": 0},
        {
            "address": "t59",
            "agent": "kiro-cli",
            "state": "live",
            "status": "done",
            "turn": 131,
        },
        {
            "address": "example-host/t1m",
            "agent": "codex",
            "name": "port-fix-codex-261118-mm6",
            "state": "live",
            "status": {"app": "codex", "state": "working"},
            "turn": 1,
        },
        {
            "address": "other-host/t16",
            "agent": "grok",
            "name": "review-grok-261118-mb2",
            "state": "live",
            "status": "idle",
            "turn": 0,
        },
        {
            "address": "other-host/t18",
            "agent": "codex",
            "name": "merge-codex-261118-mb3",
            "state": "live",
            "status": {"app": "codex", "state": "blocked"},
            "turn": 2,
        },
    ],
}

OVERNIGHT = """# Overnight run

| Time | Event |
|---|---|
""" + "\n".join(f"| 00:{i:02d} | heartbeat event {i} |" for i in range(12))

EXTERNAL_URL = re.compile(r"""(?:src|href)\s*=\s*["']?https?://""", re.I)


def _load_module():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("render_board", BOARD / "render_board.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_board"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def render_board():  # noqa: ANN201
    return _load_module()


@pytest.fixture
def status_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "status" / "handoff").mkdir(parents=True)
    (root / "status" / "decisions").mkdir()
    (root / "status" / "tasks.json").write_text(json.dumps(TASKS))
    (root / "status" / "handoff" / "REMOTE-LOG.md").write_text("## remote lanes\n\n- lane one\n")
    (root / "status" / "handoff" / "OVERNIGHT-2026-11-17.md").write_text("| 09:00 | old file |\n")
    (root / "status" / "handoff" / "OVERNIGHT-2026-11-18.md").write_text(OVERNIGHT)
    for name, status in (("D-01", "closed"), ("D-02", "closed")):
        (root / "status" / "decisions" / f"{name}.md").write_text(
            f"---\nid: {name}\nstatus: {status}\n---\n\n# Question\n"
        )
    # The early decision files have no `---` fences and may carry a trailing comment.
    (root / "status" / "decisions" / "D-03.md").write_text(
        "id: D-03\nclass: reserved\nstatus: open  # awaiting the owner\n\n# Question\n"
    )
    (root / "status" / "decisions" / "TEMPLATE.md").write_text("---\nstatus: template\n---\n")
    return root


@pytest.fixture
def hc_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "hc-ls.json"
    path.write_text(json.dumps(HC_LS))
    return path


@pytest.fixture
def page(
    render_board, status_root: Path, hc_fixture: Path, capsys: pytest.CaptureFixture[str]
) -> str:  # noqa: ANN001
    out = status_root / "status" / "board" / "index.html"
    rc = render_board.main(
        ["--root", str(status_root), "--out", str(out), "--hc-json", str(hc_fixture)]
    )
    assert rc == 0
    line = capsys.readouterr().out.strip().splitlines()
    assert len(line) == 1 and line[0].startswith(f"rendered {out} (6 tasks, 4 workers")
    return out.read_text()


def test_every_lane_column_and_task_id_is_rendered(page: str) -> None:
    for lane in LANES:
        assert re.search(rf'<section class="lane[^"]*" data-lane="{lane}"', page), lane
    for task in TASKS["tasks"]:
        assert f'data-id="{task["id"]}"' in page, task["id"]
    # Columns appear in board order and the header summary carries the counts.
    board = page.split('<main class="board">')[1].split("</main>")[0]
    positions = [board.index(f'data-lane="{lane}"') for lane in LANES]
    assert positions == sorted(positions)
    assert "1 blocked · 1 doing · 1 review · 1 todo · 2 done" in page


def test_done_column_is_collapsed_with_a_count(page: str) -> None:
    section = re.search(r'<section class="lane lane-done" data-lane="done" data-count="2">', page)
    assert section
    details = re.search(r"<details class=\"done-cards\"[^>]*>", page)
    assert details and " open" not in details.group(0)
    assert "<summary>show 2 done</summary>" in page
    assert '<h2>Done <span class="count">2</span></h2>' in page


def test_cards_show_first_sentence_full_note_and_evidence(page: str) -> None:
    assert '<p class="note">Blocked on the token rotation.</p>' in page
    # Abbreviations such as e.g. do not end the first sentence.
    assert '<p class="note">Doing the port fix (e.g. the sampler) on example-host.</p>' in page
    assert '<p class="note">Release 0.12.0 after T-02 lands!</p>' in page
    # The full note is on the card (hover) and in an expandable block; markup is escaped.
    escaped = "Event stream merged &lt;with&gt; &quot;quotes&quot; &amp; ampersands."
    assert f'title="{escaped}"' in page
    assert "<with>" not in page
    assert 'data-remember="note:T-01"><summary>full note</summary>' in page
    assert "Second sentence with detail." in page
    # A one-sentence note gets no redundant expander.
    assert 'data-remember="note:T-06"' not in page
    assert "evidence: <code>status/handoff/T-01.md</code>" in page
    assert "evidence: <code>merge d045e22</code>" in page
    for task in TASKS["tasks"]:
        assert f'<span class="stage">{task["stage"]}</span>' in page
        assert f"{task['owner']} · updated {task['updated']}" in page


def test_workers_strip_reads_both_status_shapes_and_colours_by_state(page: str) -> None:
    assert '<h2>Workers <span class="count">4</span>' in page
    assert re.search(
        r'<li class="worker state-working" data-address="example-host/t1m">'
        r'<span class="name">port-fix-codex-261118-mm6</span>'
        r'<span class="addr muted">example-host/t1m · codex · turn 1</span>'
        r'<span class="state">working</span>',
        page,
    )
    assert 'class="worker state-idle" data-address="other-host/t16"' in page
    assert 'class="worker state-blocked" data-address="other-host/t18"' in page
    assert 'class="worker state-done" data-address="t59"' in page
    assert '<span class="name">kiro-cli</span>' in page  # unnamed agent terminal, named by agent
    assert 'data-address="t53"' not in page  # a plain shell is not a worker
    # Named workers come before unnamed agent terminals.
    assert page.index('data-address="other-host/t16"') < page.index('data-address="t59"')
    for state in ("working", "idle", "blocked", "done"):
        assert f".worker.state-{state} {{ border-left-color:" in page


def test_heartbeat_shows_last_ten_rows_of_the_newest_overnight_log(page: str) -> None:
    assert "<h2>Last 10 heartbeat rows" in page
    assert "OVERNIGHT-2026-11-18.md" in page
    assert "old file" not in page
    rows = re.findall(r'<tr><td class="time">(\d\d:\d\d)</td><td>(.*?)</td></tr>', page)
    assert [t for t, _ in rows] == [f"00:{i:02d}" for i in range(2, 12)]
    assert rows[-1][1] == "heartbeat event 11"


def test_decisions_summary_counts_by_front_matter_status(page: str) -> None:
    assert '<h2>Decisions <span class="count">3</span>' in page
    assert '<li><span class="count">2</span> closed</li>' in page
    assert '<li><span class="count">1</span> open</li>' in page
    assert "template" not in page.split('class="strip decisions"')[1].split("</section>")[0]


def test_footer_refresh_and_no_external_urls(page: str) -> None:
    footer = re.search(r"<footer id=\"footer\">(.*?)</footer>", page, re.S)
    assert footer
    assert re.search(r"Rendered \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", footer.group(1))
    assert "main HEAD <code>unknown</code>" in footer.group(1)  # a tmp root has no git branch
    assert '<meta http-equiv="refresh" content="60">' in page
    assert 'id="countdown"' in page
    assert not EXTERNAL_URL.search(page)
    assert "<link" not in page and 'src="' not in page


def test_hc_unavailable_is_reported_not_fatal(
    render_board,  # noqa: ANN001
    status_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(render_board, "HC", str(tmp_path / "no-such-hc"))
    out = tmp_path / "board.html"
    assert render_board.main(["--root", str(status_root), "--out", str(out)]) == 0
    page = out.read_text()
    assert '<h2>Workers <span class="count">0</span>' in page
    assert "hc ls unavailable: hc not found on PATH" in page
    assert "hc: hc not found on PATH" in capsys.readouterr().out


def test_first_sentence_helper(render_board) -> None:  # noqa: ANN001
    first = render_board.first_sentence
    assert first("Answerback: coordination log. Orchestrator (2026-11-17) said so.") == (
        "Answerback: coordination log."
    )
    assert first("  spaced\n  out   text  ") == "spaced out text"
    assert first("") == ""
    assert first("x" * 300).endswith("…") and len(first("x" * 300)) == 200


@pytest.mark.skipif(shutil.which("zsh") is None, reason="watch_board.sh needs zsh")
def test_watch_board_once_renders_and_exits_zero(tmp_path: Path, hc_fixture: Path) -> None:
    out = tmp_path / "once.html"
    proc = subprocess.run(
        [
            "zsh",
            str(BOARD / "watch_board.sh"),
            "--once",
            "--out",
            str(out),
            "--hc-json",
            str(hc_fixture),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("board ") and f"rendered {out}" in lines[0]
    assert out.exists() and '<main class="board">' in out.read_text()
