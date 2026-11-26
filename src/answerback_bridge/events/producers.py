"""Projectors: what the bridge already records, restated as events and board state.

Everything here reads. Receipts, peers and files are inputs; none of these functions can
submit, acknowledge, answer or change anything the dispatch path owns.
"""

import hashlib
import json
import os
import sqlite3
import stat
import weakref
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import model
from .hub import EMPTY_METRICS

TERMINAL = frozenset({"completed", "failed", "abandoned", "cancelled"})
WAITING = frozenset({"pending", "queued"})
ATTENTION = frozenset({"failed"})  # Ended, and someone should look. Shown at any age.
MAX_RUNS = 5000
TAIL_ACTIVITY = "answerback.tail"
MAX_PEERS = 200
MAX_PEER_ID = 1024  # Includes the longest supported provider address without shortening it.
MAX_DECISIONS = 200
MAX_WORKTREES = 64
MAX_QUEUE_NAMES = 50
MAX_PROJECTS = 8
# Identity budgets are UTF-8 bytes. Printable text expands by at most 3x in ASCII JSON.
MAX_DECISION_ID_BYTES = 256
MAX_QUEUE_NAME_BYTES = 255  # Full filename, including .json; also directory bucket names.
MAX_QUEUE_BUCKETS = 16  # In addition to the root pending list.
MAX_PROJECT_PATH_BYTES = 512
MAX_WORKTREE_PATH_BYTES = 4096
GIT_POINTER_BYTES = 4096
BOARD_OMISSIONS = {
    "decisionIdentifiers": "decision identifiers",
    "decisionDuplicates": "duplicate decision identifiers",
    "queueFileNames": "queue file names",
    "queueBucketNames": "queue bucket names",
    "queueBuckets": "queue buckets",
    "worktreePaths": "worktree paths",
    "projectPaths": "project paths",
}


def _board_identity(value: str, limit: int, kind: str, omissions: dict[str, int] | None) -> bool:
    """Admit an exact printable file identity, counting refusals without retaining text."""
    if value.isprintable() and len(value.encode("utf-8")) <= limit:
        return True
    if omissions is not None:
        omissions[kind] = omissions.get(kind, 0) + 1
    return False


def bounded_identity(value: str, limit: int = MAX_PEER_ID) -> str:
    """Keep a printable identity exact, or leave it empty so it cannot join another row."""
    return value if value == model.bounded(value, limit) else ""


@dataclass
class Run:
    run: str
    thread: str
    peer_id: str
    status: str
    provider: str
    request: str
    created: float
    updated: float
    step: str | None = None
    subagent: str | None = None
    invocations: int = 0
    closed: bool = False


def receipt_peer(row: dict[str, Any]) -> str:
    """The peer a receipt was sent to, as the scanner names it."""
    for key in ("peer_id", "target"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def receipt_run(row: dict[str, Any]) -> Run:
    """Only metadata shared by the live projection and the retained-receipt page."""
    return Run(
        run=row["id"],
        thread=model.thread_id(row.get("head"), row["request_key"]),
        peer_id=receipt_peer(row),
        status=row["status"],
        provider=row.get("kind") or "",
        request=row["request_key"],
        created=row["created"],
        updated=row["updated"],
    )


def run_state(run: Run) -> dict[str, Any]:
    peer = bounded_identity(run.peer_id)
    subagent = bounded_identity(run.subagent, 1280) if run.subagent else None
    return {
        "threadId": model.bounded(run.thread, 128),
        "peerId": peer,
        **({"peerIdOmitted": True} if run.peer_id and not peer else {}),
        "provider": model.bounded(run.provider, 32),
        "requestId": model.bounded(run.request, 128),
        "status": model.bounded(run.status, 32),
        "attention": run.status in ATTENTION,
        "step": model.bounded(run.step, 32) if run.step is not None else None,
        "subagentRunId": subagent or None,
        **({"subagentRunIdOmitted": True} if run.subagent and not subagent else {}),
        "createdMs": int(run.created * 1000),
        "updatedMs": int(run.updated * 1000),
    }


class RunTracker:
    """Receipt rows in, ordered lifecycle events out. One run per receipt.

    `limit` bounds the live working set, including unfinished runs. Evicted lifecycle
    metadata spills to a temporary index; history pages come from the receipt store.
    """

    def __init__(self, *, limit: int = MAX_RUNS) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("Run limit must be positive")
        self.runs: dict[str, Run] = {}
        self.limit = limit
        self.omitted = 0
        # Eviction must not erase lifecycle identity. SQLite's empty filename creates a
        # private temporary database, deleted on close; its page cache is bounded. Only
        # projected Run metadata is stored, never receipt bodies. This is observer state,
        # entirely separate from the authoritative, read-only receipt store.
        # The collector is the sole writer, but an embedder may construct it before
        # moving its sequential tick loop to a worker thread.
        self._archive = sqlite3.connect("", check_same_thread=False, uri=True)
        self._archive.execute("PRAGMA cache_size=-1024")
        self._archive.execute("CREATE TABLE runs(id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        weakref.finalize(self, self._archive.close)

    def _restore(self, ident: str) -> Run | None:
        row = self._archive.execute("SELECT data FROM runs WHERE id=?", (ident,)).fetchone()
        if row is None:
            return None
        run = Run(**json.loads(row[0]))
        self._archive.execute("DELETE FROM runs WHERE id=?", (ident,))
        self.runs[ident] = run
        return run

    def _count_omitted(self) -> None:
        self.omitted = self._archive.execute("SELECT count(*) FROM runs").fetchone()[0]
        self._archive.commit()

    def observe(self, rows: list[dict[str, Any]], *, backfill: bool = False) -> list[model.Event]:
        """`backfill` is the first read of what the store already holds: a receipt that had
        ended before this bridge started enters the board state, and announces nothing."""
        events: list[model.Event] = []
        for row in sorted(rows, key=lambda item: (item["updated"], item["id"])):
            status = row["status"]
            known = self.runs.get(row["id"]) or self._restore(row["id"])
            if known is None and backfill and status in TERMINAL:
                known = receipt_run(row)
                known.closed = True
                self.runs[known.run] = known
                continue
            if known is None:
                known = receipt_run(row)
                self.runs[known.run] = known
                events.append(model.run_started(known.thread, known.run, peer_id=known.peer_id))
                if status in TERMINAL:
                    events.extend(self._close(known, row))
                else:
                    events.append(self._open_step(known, status))
                continue
            known.updated = row["updated"]
            if known.closed or status == known.status:
                # A closed run stays closed on the stream: nothing may follow its terminal
                # event. A later correction of the receipt still reaches the board state.
                known.status = status
                continue
            known.status = status
            if status in TERMINAL:
                events.extend(self._close(known, row))
            else:
                events.append(self._close_step(known))
                events.append(self._open_step(known, status))
        self._bound()
        self._count_omitted()
        return [event for event in events if event]

    def _bound(self) -> None:
        excess = len(self.runs) - self.limit
        if excess <= 0:
            return
        # Open, then failed, then other receipts win the bounded working set. Every
        # omitted receipt remains reachable through the store-backed history pages.
        evictable = sorted(
            self.runs.values(),
            key=lambda run: (not run.closed, run.status in ATTENTION, run.updated, run.run),
        )
        for run in evictable[:excess]:
            self._archive.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?)",
                (run.run, json.dumps(asdict(run))),
            )
            del self.runs[run.run]

    def retain(self, receipts: set[str]) -> None:
        """Forget every run whose receipt the store no longer holds. Retention owns deletion."""
        for run in [run for run in self.runs if run not in receipts]:
            del self.runs[run]
        self._archive.execute("CREATE TEMP TABLE IF NOT EXISTS retained(id TEXT PRIMARY KEY)")
        self._archive.execute("DELETE FROM retained")
        self._archive.executemany("INSERT INTO retained VALUES (?)", ((key,) for key in receipts))
        self._archive.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM retained)")
        self._refill()

    def retain_store(self, path: Path) -> None:
        """Reconcile against an arbitrarily large store without loading all its IDs.

        The attached receipt database is opened in mode=ro; only the temporary observer
        index can be written. The collector calls this on its single writer lane.
        """
        self._archive.execute("ATTACH DATABASE ? AS receipts", (f"{path.as_uri()}?mode=ro",))
        try:
            self._archive.execute("CREATE TEMP TABLE IF NOT EXISTS cached(id TEXT PRIMARY KEY)")
            self._archive.execute("DELETE FROM cached")
            self._archive.executemany(
                "INSERT INTO cached VALUES (?)", ((ident,) for ident in self.runs)
            )
            retained = {
                row[0]
                for row in self._archive.execute(
                    "SELECT cached.id FROM cached JOIN receipts.tasks ON cached.id=tasks.id"
                )
            }
            self.runs = {ident: run for ident, run in self.runs.items() if ident in retained}
            self._archive.execute(
                "DELETE FROM runs WHERE NOT EXISTS "
                "(SELECT 1 FROM receipts.tasks WHERE tasks.id=runs.id)"
            )
            self._refill()
        finally:
            self._archive.commit()
            self._archive.execute("DETACH DATABASE receipts")

    def _refill(self) -> None:
        # Retention may leave only receipts outside the polling overlap. Refill the
        # working set from their already-observed metadata, without emitting lifecycle.
        refill = self._archive.execute(
            "SELECT id FROM runs ORDER BY json_extract(data,'$.closed'), "
            "(json_extract(data,'$.status')='failed') DESC, "
            "json_extract(data,'$.updated') DESC, id DESC LIMIT ?",
            (max(0, self.limit - len(self.runs)),),
        ).fetchall()
        for (ident,) in refill:
            self._restore(ident)
        self._count_omitted()

    def _open_step(self, run: Run, name: str) -> model.Event:
        run.step = name
        return model.step_started(name, thread=run.thread, run=run.run)

    def _close_step(self, run: Run) -> model.Event:
        name, run.step = run.step, None
        return model.step_finished(name, thread=run.thread, run=run.run) if name else {}

    def _close(self, run: Run, row: dict[str, Any]) -> list[model.Event]:
        status = row["status"]
        ids: dict[str, Any] = {"thread": run.thread, "run": run.run, "peer_id": run.peer_id}
        events = [self._close_step(run)]
        # Fixed messages only: a receipt's detail is free text, and may quote the brief or
        # the worker's output. Why a run failed is read from the receipt, not the stream.
        if status == "completed":
            if run.subagent:
                events.append(model.subagent_finished(run.subagent, **ids))
            result = {"status": status, "resultChars": row.get("result_chars")}
            events.append(
                model.run_finished(run.thread, run.run, result=result, peer_id=run.peer_id)
            )
        elif status == "failed":
            if run.subagent:
                events.append(model.subagent_error(run.subagent, "Run failed", "failed", **ids))
            events.append(model.run_error("Task failed", "failed", **ids))
        else:
            if run.subagent:
                events.append(model.subagent_error(run.subagent, f"Run {status}", status, **ids))
            events.append(
                model.run_finished(
                    run.thread, run.run, outcome=dict(model.CANCELLED), peer_id=run.peer_id
                )
            )
        run.closed, run.subagent = True, None
        return events

    def attach(self, peers: dict[str, dict[str, Any]], partial: set[str]) -> list[model.Event]:
        """Open a subagent when a run's worker is seen; fail it when the worker is gone."""
        events: list[model.Event] = []
        for run in self.runs.values():
            if run.closed or not run.peer_id:
                continue
            peer = peers.get(run.peer_id)
            ids: dict[str, Any] = {"thread": run.thread, "run": run.run, "peer_id": run.peer_id}
            if run.subagent is None and peer is not None:
                if run.status in WAITING and peer.get("state") != "working":
                    continue
                run.invocations += 1
                suffix = f"#{run.invocations}" if run.invocations > 1 else ""
                # An invocation id is never reused, so a worker that returns gets a new one.
                run.subagent = f"{run.peer_id}#{run.run}{suffix}"
                kind = peer.get("agentKind") or peer.get("provider") or "agent"
                events.append(
                    model.subagent_started(
                        run.subagent,
                        peer.get("name") or run.peer_id,
                        description=f"{kind} in {peer.get('project') or 'an unknown project'}",
                        **ids,
                    )
                )
            elif run.subagent and peer is None and run.peer_id.split(":")[0] not in partial:
                events.append(
                    model.subagent_error(
                        run.subagent, "Worker peer is no longer listed", "peer_gone", **ids
                    )
                )
                run.subagent = None
        return events

    def section(self) -> dict[str, Any]:
        return {run.run: run_state(run) for run in self.runs.values()}

    def open_by_peer(self) -> dict[str, str]:
        return {run.peer_id: run.run for run in self.runs.values() if not run.closed}


def peers_section(
    result: dict[str, Any], previous: dict[str, Any], open_runs: dict[str, str]
) -> tuple[dict[str, Any], set[str]]:
    """Scanner inventory as board state. A provider that failed keeps its last known peers."""
    partial: set[str] = set()
    for key in ("errors", "excluded", "cursors"):
        value = result.get(key)
        if isinstance(value, dict):
            partial.update(str(name) for name in value)
    section: dict[str, Any] = {
        ident: peer
        for ident, peer in previous.items()
        if ident.split(":")[0] in partial and bounded_identity(ident)
    }
    rows = result.get("peers")
    for row in (rows if isinstance(rows, list) else [])[:MAX_PEERS]:
        ident = row.get("peer_id") if isinstance(row, dict) else None
        if not isinstance(ident, str) or not ident:
            continue
        # IDs are join keys and routing addresses, not display labels. Shortening one
        # could merge peers or detach a receipt; omit it if it cannot be retained intact.
        if not bounded_identity(ident):
            continue
        before = previous.get(ident, {})
        section[ident] = {
            "name": model.bounded(row.get("name"), 120) or ident,
            "provider": model.bounded(row.get("provider"), 32),
            "project": model.bounded(row.get("project"), 400),
            "agentKind": model.bounded(row.get("agent_kind") or row.get("agent"), 64),
            "state": model.bounded(row.get("state"), 32) or "unknown",
            "sendReady": row.get("send_ready") if type(row.get("send_ready")) is bool else None,
            "reason": model.bounded(row.get("send_readiness_reason"), 64) or None,
            "runId": open_runs.get(ident),
            # Reserved for the T-17 tracer, which owns these values. Carried over so a
            # rescan never erases what it measured.
            "metrics": before.get("metrics") or dict(EMPTY_METRICS),
            **({"trace": before["trace"]} if "trace" in before else {}),
        }
    return section, partial


def tail_content(
    peer_id: str, run: str | None, text: str, *, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What a worker's screen or chat history says about its activity, and none of its text.

    A screen shows the brief the worker was sent and the result it is writing, so no line of
    it is published: only a digest that changes when the text does, two sizes, and when the
    digest last changed. `previous` is the content published before, if any.
    """
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    unchanged = previous is not None and previous.get("sha256") == digest
    return {
        "peerId": peer_id,
        "runId": run,
        "sha256": digest,
        "lineCount": sum(1 for line in text.splitlines() if line.strip()),
        "chars": len(text),
        "changedMs": previous["changedMs"] if previous and unchanged else model.now_ms(),
    }


def project_root(path: str) -> Path | None:
    """The main checkout that owns `path`, so every worktree maps to one ledger."""
    try:
        current = Path(path)
        if not current.is_absolute() or not current.is_dir():
            return None
        for candidate in (current, *current.parents[:6]):
            marker = candidate / ".git"
            try:
                mode = marker.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode):
                return candidate
            if not stat.S_ISREG(mode):
                return None
            pointer = _git_pointer(marker)
            if pointer is None:
                return None
            if pointer.startswith("gitdir:"):
                gitdir = Path(pointer[7:].strip())
                if gitdir.parent.name == "worktrees":
                    return gitdir.parent.parent.parent
            return candidate
    except OSError:
        return None
    return None


def _git_pointer(path: Path) -> str | None:
    """Read a regular `.git` pointer without following it or exceeding its budget."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | no_follow)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            raw = os.read(descriptor, GIT_POINTER_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    if len(raw) > GIT_POINTER_BYTES:
        return None
    return raw.decode("utf-8", "replace").strip()


def _small_text(path: Path, limit: int = 16384) -> str | None:
    """Regular, non-symlink files only: a board file never redirects the reader."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_decision(text: str) -> dict[str, str]:
    """Front matter, the first heading and the answer line. Older files omit the `---` fences
    and end their header at the first blank line; both forms are read."""
    lines = text.splitlines()
    fields: dict[str, str] = {}
    fenced = bool(lines) and lines[0].strip() == "---"
    end = len(lines)
    for index, line in enumerate(lines[1:] if fenced else lines, start=1 if fenced else 0):
        key, separator, value = line.partition(":")
        if line.strip() == "---" if fenced else not line.strip():
            end = index
            break
        if separator and key.strip().replace("_", "").isalnum():
            fields[key.strip()] = value.split(" #", 1)[0].strip()
        elif not fenced:
            return {}  # Not a header at all.
    else:
        if fenced:
            return {}  # An unclosed fence is not front matter.
    for line in lines[end + 1 :]:
        if line.startswith("# ") and "title" not in fields:
            fields["title"] = line[2:].strip()
        elif line.partition(":")[0] in {
            "answer",
            "answered_by",
            "answered_at",
            "recommendation_only",
        }:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    return fields


def read_decisions(
    root: Path, now: datetime, *, omissions: dict[str, int] | None = None
) -> dict[str, Any]:
    decisions: dict[str, Any] = {}
    try:
        names = sorted(
            name
            for name in os.listdir(root / "status" / "decisions")
            if name.startswith("D-") and name.endswith(".md")
        )
    except OSError:
        return decisions
    for name in names[-MAX_DECISIONS:]:
        text = _small_text(root / "status" / "decisions" / name)
        if text is None:
            continue
        fields = parse_decision(text)
        ident = fields.get("id") or name[:-3]
        if not _board_identity(ident, MAX_DECISION_ID_BYTES, "decisionIdentifiers", omissions):
            continue
        needed = parse_time(fields["needed_by"]) if fields.get("needed_by") else None
        answered = parse_time(fields["answered_at"]) if fields.get("answered_at") else None
        status = fields.get("status", "")
        decisions[ident] = {
            "task": model.bounded(fields.get("task"), 32),
            "class": model.bounded(fields.get("class"), 16),
            "status": model.bounded(status, 16),
            "title": model.bounded(fields.get("title"), 200),
            "neededBy": needed.isoformat() if needed else None,
            "answered": bool(fields.get("answer")),
            **(
                {
                    "answeredBy": model.bounded(fields.get("answered_by"), 128) or None,
                    "answeredMs": int(answered.timestamp() * 1000),
                    "recommendationOnly": fields.get("recommendation_only", "").lower() != "false",
                }
                if answered and fields.get("answer")
                else {}
            ),
            "overdue": bool(
                status == "open" and not fields.get("answer") and needed and needed < now
            ),
            "project": str(root),
        }
    return decisions


def read_apply_queue(
    root: Path, *, omissions: dict[str, int] | None = None
) -> dict[str, Any] | None:
    base = root / "status" / "apply-queue"
    queue: dict[str, Any] = {}
    try:
        entries = sorted(os.scandir(base), key=lambda entry: entry.name)
    except OSError:
        return None

    def admitted_names(names: list[str]) -> list[str]:
        return [
            name[:-5]
            for name in names
            if _board_identity(name, MAX_QUEUE_NAME_BYTES, "queueFileNames", omissions)
        ]

    pending = [e.name for e in entries if e.name.endswith(".json") and e.is_file()]
    queue["pending"] = admitted_names(pending)[:MAX_QUEUE_NAMES]
    buckets = 0
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
            continue
        if not _board_identity(entry.name, MAX_QUEUE_NAME_BYTES, "queueBucketNames", omissions):
            continue
        if buckets >= MAX_QUEUE_BUCKETS:
            if omissions is not None:
                omissions["queueBuckets"] = omissions.get("queueBuckets", 0) + 1
            continue
        buckets += 1
        try:
            names = sorted(name for name in os.listdir(entry.path) if name.endswith(".json"))
        except OSError:
            continue
        queue[entry.name] = sorted(admitted_names(names))[-MAX_QUEUE_NAMES:]
    return queue


def _resolve_head(gitdir: Path, common: Path) -> dict[str, Any] | None:
    head = _small_text(gitdir / "HEAD", 512)
    if head is None:
        return None
    head = head.strip()
    if not head.startswith("ref:"):
        return {"branch": None, "sha": head[:12]}
    ref = head[4:].strip()
    if ".." in ref or not ref.startswith("refs/"):
        return None
    sha = _small_text(common / ref, 128)
    if sha is None:
        packed = _small_text(common / "packed-refs", 1024 * 1024) or ""
        sha = next(
            (line.split(" ", 1)[0] for line in packed.splitlines() if line.endswith(" " + ref)),
            "",
        )
    return {"branch": ref.removeprefix("refs/heads/"), "sha": sha.strip()[:12] or None}


def read_heads(root: Path, *, omissions: dict[str, int] | None = None) -> dict[str, Any]:
    common = root / ".git"
    heads: dict[str, Any] = {}
    main = _resolve_head(common, common)
    if main is not None:
        heads[str(root)] = main
    try:
        names = sorted(os.listdir(common / "worktrees"))
    except OSError:
        names = []
    for name in names[:MAX_WORKTREES]:
        gitdir = common / "worktrees" / name
        # Include /.git, a possible CRLF and one overflow byte; never accept a cut path.
        location = _small_text(gitdir / "gitdir", MAX_WORKTREE_PATH_BYTES + 8)
        resolved = _resolve_head(gitdir, common)
        if location and resolved is not None:
            location = location.removesuffix("\n").removesuffix("\r")
            if not _board_identity(
                location, MAX_WORKTREE_PATH_BYTES + 5, "worktreePaths", omissions
            ):
                continue
            path = str(Path(location).parent)
            if _board_identity(path, MAX_WORKTREE_PATH_BYTES, "worktreePaths", omissions):
                heads[path] = resolved
    return heads


def read_board(paths: list[str], now: datetime | None = None) -> dict[str, Any]:
    """Decisions, apply queue and HEADs of every ledger project among `paths`. File reads only."""
    now = now or datetime.now(UTC)
    board: dict[str, Any] = {"decisions": {}, "applyQueue": {}, "heads": {}}
    omissions: dict[str, int] = {}
    roots: list[Path] = []
    for path in paths:
        root = project_root(path)
        if root is not None and root not in roots and (root / "status").is_dir():
            roots.append(root)
    for root in roots[:MAX_PROJECTS]:
        if not _board_identity(str(root), MAX_PROJECT_PATH_BYTES, "projectPaths", omissions):
            continue
        # Decision ids are one flat space across projects. The first project that holds an
        # id keeps it: another repository's front matter cannot replace that row or decide
        # whether its reserved decision is announced. A refused duplicate is counted.
        for ident, decision in read_decisions(root, now, omissions=omissions).items():
            if ident in board["decisions"]:
                omissions["decisionDuplicates"] = omissions.get("decisionDuplicates", 0) + 1
                continue
            board["decisions"][ident] = decision
        queue = read_apply_queue(root, omissions=omissions)
        if queue is not None:
            board["applyQueue"][str(root)] = queue
        board["heads"][str(root)] = read_heads(root, omissions=omissions)
    board["omissions"] = omissions
    return board


def overdue_interrupts(decisions: dict[str, Any], seen: set[str]) -> list[model.Event]:
    """A reserved decision past its deadline is a run paused on the owner, announced once."""
    events: list[model.Event] = []
    for ident, decision in decisions.items():
        if decision["class"] != "reserved" or not decision["overdue"] or ident in seen:
            continue
        seen.add(ident)
        thread, run = decision["task"] or ident, f"decision:{ident}"
        events.append(model.run_started(thread, run))
        events.append(
            model.run_finished(
                thread,
                run,
                outcome=model.interrupt_outcome(
                    ident,
                    "decision_overdue",
                    decision["title"] or f"Decision {ident} is waiting for the owner",
                    details={"neededBy": decision["neededBy"], "class": "reserved"},
                ),
            )
        )
    return events
