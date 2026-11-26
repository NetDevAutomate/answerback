"""Independent ledger review: transaction failures and concurrent evidence reads."""

import secrets
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

import pytest

from answerback_bridge.actors.model import ActorAuth
from answerback_bridge.actors.store import ActorStore


@pytest.fixture
def actors(tmp_path):
    store = ActorStore(tmp_path / "actors.sqlite", clock=lambda: 1000.0)
    run = str(uuid4())
    store.create_run(run, str(tmp_path), ["author", "reviewer"])
    auths = []
    for role in ("author", "reviewer"):
        auth = ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
        store.stage_actor(run, auth.actor_id, role, "fixture", auth.credential)
        store.activate_actor(run, auth.actor_id, 1, {"native_id": str(uuid4())})
        auths.append(auth)
    store.seal_run(run, auth=auths[0])
    return store, run, *auths


def request(actors):
    store, _, author, reviewer = actors
    sent = store.send(author, reviewer.actor_id, "review", "review_request", {"question": "Why?"})
    raw = store.pending_outbox()[0]["payload"]
    store.deliver(raw, "ACTORS_REVIEW", 1)
    offer = store.receive(reviewer)["messages"][0]
    store.acknowledge(reviewer, sent["message_id"], offer["receipt_nonce"])
    return sent


def test_failed_outbox_insert_rolls_back_message_but_keeps_frontend_pin(actors):
    store, run, author, reviewer = actors
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_outbox BEFORE INSERT ON actor_outbox "
            "BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        store.send(author, reviewer.actor_id, "fail", "review_request", {})
    exported = store.export(run, auth=reviewer)
    assert exported["messages"] == []
    assert store.pending_outbox() == []
    actor = next(row for row in exported["roster"]["actors"] if row["actor_id"] == author.actor_id)
    assert actor["frontend_id"] == author.frontend_id


def test_failed_reply_receipt_rolls_back_reply_outbox_and_original_projection(actors):
    store, run, _, reviewer = actors
    sent = request(actors)
    before = store.export(run, auth=reviewer)
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_reply_receipt BEFORE INSERT ON actor_receipts "
            "WHEN NEW.kind='replied' BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        store.reply(reviewer, sent["message_id"], "answer", {"answer": "Because"})
    assert store.export(run, auth=reviewer) == before
    assert len(store.pending_outbox()) == 1


def test_export_uses_one_snapshot_while_a_reply_is_committed(actors, monkeypatch):
    store, run, author, reviewer = actors
    sent = request(actors)
    with store.connection() as db:
        # WAL permits the concurrent writer even when the exporter correctly
        # holds a read transaction. This affects only this disposable fixture.
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    original_connection = store.connection
    inserted = False
    failures = []

    def concurrent_writer(statement):
        nonlocal inserted
        if inserted or "SELECT o.* FROM actor_transport_observations" not in statement:
            return
        inserted = True
        try:
            reply = store.reply(reviewer, sent["message_id"], "answer", {"answer": "Because"})
            raw = next(
                row["payload"]
                for row in store.pending_outbox()
                if row["message_id"] == reply["message_id"]
            )
            store.deliver(raw, "ACTORS_REVIEW", 2)
            store.receive(author)
        except Exception as error:
            # sqlite trace callbacks otherwise swallow raised exceptions.
            failures.append(error)

    @contextmanager
    def interleaved_connection():
        with original_connection() as db:
            db.set_trace_callback(concurrent_writer)
            yield db

    monkeypatch.setattr(store, "connection", interleaved_connection)
    exported = store.export(run, auth=reviewer)
    assert inserted and not failures
    message_ids = {row["message_id"] for row in exported["messages"]}
    assert sum(exported["run"]["messages"].values()) == len(message_ids), (
        "Export combined message rows from before a reply with counters from after it"
    )
    for collection in ("transport_observations", "receipts"):
        assert {row["message_id"] for row in exported[collection]} <= message_ids, (
            f"Export {collection} refers to a message absent from the exported snapshot"
        )
