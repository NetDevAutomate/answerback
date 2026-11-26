"""Bounded metadata pages from retained receipts, independent of the lifecycle cache."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .producers import TERMINAL, receipt_run, run_state

MAX_PAGE = 100
RECEIPT_COLUMNS = (
    "id, request_key, target, status, created, updated, "
    "CASE WHEN typeof(message)='text' THEN substr(message,1,4000) END AS head, "
    "CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.kind') END AS kind, "
    "CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.peer.peer_id') END AS peer_id, "
    "length(result) AS result_chars"
)


def validate_page(offset: int, limit: int) -> None:
    if type(offset) is not int or not 0 <= offset <= 1_000_000_000:
        raise ValueError("Run offset must be 0–1000000000")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("Run page size must be 1–100")


def receipt_exists(path: Path, ident: str) -> bool:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.5)) as db:
        db.execute("PRAGMA query_only=ON")
        return db.execute("SELECT 1 FROM tasks WHERE id=?", (ident,)).fetchone() is not None


def read_runs(path: Path, offset: int = 0, limit: int = MAX_PAGE) -> dict[str, Any]:
    """One transaction binds the count and page. No writes, result or detail bodies.

    Order by immutable creation time and ID so status updates cannot move rows between
    pages. New receipts or retention can change offsets between requests; refresh from
    page one for a new traversal. No cross-request frozen snapshot is claimed.
    """
    validate_page(offset, limit)
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        total = db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        rows = db.execute(
            f"SELECT {RECEIPT_COLUMNS} FROM tasks ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    runs = {}
    for row in rows:
        run = receipt_run(dict(row))
        run.step = None if run.status in TERMINAL else run.status
        runs[run.run] = run_state(run)
    return {
        "runs": runs,
        "total": total,
        "offset": offset,
        "nextOffset": offset + len(runs) if offset + len(runs) < total else None,
        "source": "receipts",
    }
