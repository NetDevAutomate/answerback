"""Actor message ledger invariants; no model, task runtime or broker is impersonated."""

import importlib
import importlib.util
import json
import secrets
from dataclasses import replace
from uuid import uuid4

import pytest


def api():
    assert importlib.util.find_spec("answerback_bridge.actors") is not None, (
        "The separate actor ledger is not implemented"
    )
    return (
        importlib.import_module("answerback_bridge.actors.store").ActorStore,
        importlib.import_module("answerback_bridge.actors.model"),
    )


@pytest.fixture
def ledger(tmp_path):
    store_type, model = api()
    clock = [1000.0]
    store = store_type(tmp_path / "actor-ledger.sqlite", clock=lambda: clock[0])
    run = str(uuid4())
    store.create_run(run, str(tmp_path), ["sender", "recipient"], ttl_seconds=100)
    auths = []
    for role in ("sender", "recipient"):
        auth = model.ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
        store.stage_actor(run, auth.actor_id, role, "test-client", auth.credential)
        assert store.receive(auth)["messages"] == []
        store.activate_actor(run, auth.actor_id, 1, {"native_id": str(uuid4()), "source": "test"})
        auths.append(auth)
    store.seal_run(run, auth=auths[0])
    return store, model, run, auths, clock


def send(ledger, request_id="question", body=None):
    store, _, _, (sender, recipient), _ = ledger
    return store.send(
        sender, recipient.actor_id, request_id, "review_request", body or {"ask": "why"}
    )


def deliver(ledger, seq=1):
    store = ledger[0]
    row = store.pending_outbox()[0]
    return store.deliver(row["payload"], "ACTORS_TEST", seq)


def test_bootstrap_receive_is_empty_and_work_requires_complete_sealed_roster(tmp_path):
    store_type, model = api()
    store = store_type(tmp_path / "actors.sqlite")
    run, actor = str(uuid4()), str(uuid4())
    store.create_run(run, str(tmp_path), ["first", "second"])
    auth = model.ActorAuth(run, actor, 1, secrets.token_hex(32), str(uuid4()))
    store.stage_actor(run, actor, "first", "codex_cli", auth.credential)
    assert store.receive(auth)["run_state"] == "staging"
    with pytest.raises(ValueError, match="sealed"):
        store.send(auth, str(uuid4()), "too-early", "review_request", {})
    store.activate_actor(run, actor, 1, {"native_id": "native-one"})
    with pytest.raises(ValueError, match="role|active|complete"):
        store.seal_run(run, auth=auth)


def test_separate_database_has_absent_safe_probe_and_no_task_tables(tmp_path):
    store_type, _ = api()
    path = tmp_path / "missing" / "actors.sqlite"
    assert store_type.has_runs(path) is False
    assert not path.parent.exists()
    store = store_type(path)
    assert store_type.has_runs(path) is False
    store.create_run(str(uuid4()), str(tmp_path), ["one"])
    assert store_type.has_runs(path) is True
    with store.connection() as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tasks" not in names and "commands" not in names


def test_sealed_bindings_and_definitions_cannot_change(ledger):
    store, _, run, (sender, _), _ = ledger
    before = store.roster(run, auth=sender)
    with pytest.raises(ValueError, match="immutable|sealed|different"):
        store.activate_actor(run, sender.actor_id, 1, {"native_id": "different"})
    with pytest.raises(ValueError, match="sealed"):
        store.stage_actor(run, str(uuid4()), "sender", "other", secrets.token_hex(32))
    with pytest.raises(ValueError, match="different|immutable"):
        store.create_run(
            run, "/another/project", ["sender", "recipient"], ttl_seconds=100, auth=sender
        )
    assert store.roster(run, auth=sender) == before


def test_tokens_hashes_and_nonces_are_not_exposed_by_public_views(ledger):
    store, _, run, (sender, recipient), _ = ledger
    assert sender.credential not in repr(sender)
    sent = send(ledger)
    deliver(ledger)
    offered = store.receive(recipient)["messages"][0]
    assert offered["message_id"] == sent["message_id"]
    data = json.dumps(
        [
            store.status(run, auth=sender),
            store.roster(run, auth=sender),
            store.export(run, auth=sender),
        ]
    )
    for value in (sender.credential, recipient.credential, offered["receipt_nonce"]):
        assert value not in data
    assert "credential_digest" not in data and "credential_hash" not in data
    assert sender.credential.encode() not in store.path.read_bytes()


def test_credentials_epoch_run_and_frontend_are_all_bound(ledger):
    store, _, _, (sender, _), _ = ledger
    for wrong in (
        replace(sender, credential=secrets.token_hex(32)),
        replace(sender, epoch=2),
        replace(sender, run_id=str(uuid4())),
        replace(sender, frontend_id=str(uuid4())),
    ):
        with pytest.raises(ValueError, match="credential|actor|epoch|frontend|run"):
            store.receive(wrong)


def test_broker_delivery_is_required_and_puback_does_not_deliver(ledger):
    store, _, _, (_, recipient), _ = ledger
    receipt = send(ledger)
    assert store.receive(recipient)["messages"] == []
    store.mark_published(receipt["message_id"], "ACTORS_TEST", 7)
    assert store.receive(recipient)["messages"] == []
    raw = store.export(ledger[2], auth=recipient)["messages"][0]["envelope"]
    encoded = ledger[1].ActorEnvelope(**raw).to_bytes()
    result = store.deliver(encoded, "ACTORS_TEST", 7)
    assert result == {
        "message_id": receipt["message_id"],
        "delivery": "delivered",
        "duplicate": False,
    }
    assert store.receive(recipient)["messages"][0]["message_id"] == receipt["message_id"]


def test_deliver_can_race_puback_and_redelivery_preserves_every_observation(ledger):
    store, _, run, (_, recipient), clock = ledger
    sent = send(ledger)
    row = store.pending_outbox()[0]
    store.deliver(row["payload"], "ACTORS_TEST", 9)
    store.deliver(row["payload"], "ACTORS_TEST", 9)
    store.mark_published(sent["message_id"], "ACTORS_TEST", 9)
    store.mark_published(sent["message_id"], "ACTORS_TEST", 9)
    item = store.receive(recipient)["messages"][0]
    first = store.acknowledge(recipient, item["message_id"], item["receipt_nonce"])
    clock[0] += 1
    second = store.acknowledge(recipient, item["message_id"], item["receipt_nonce"])
    assert first["acknowledged_at"] == second["acknowledged_at"]
    assert store.deliver(row["payload"], "ACTORS_TEST", 9)["duplicate"] is True
    assert store.receive(recipient)["messages"] == []
    exported = store.export(run, auth=recipient)
    assert len(exported["transport_observations"]) == 5
    assert (
        len([row for row in exported["transport_observations"] if row["kind"] == "published"]) == 2
    )


def test_ack_requires_recipient_prior_offer_and_correct_nonce(ledger):
    store, _, _, (sender, recipient), _ = ledger
    sent = send(ledger)
    deliver(ledger)
    with pytest.raises(ValueError, match="offer|nonce"):
        store.acknowledge(recipient, sent["message_id"], "not-offered")
    item = store.receive(recipient)["messages"][0]
    with pytest.raises(ValueError, match="recipient"):
        store.acknowledge(sender, item["message_id"], item["receipt_nonce"])
    with pytest.raises(ValueError, match="nonce"):
        store.acknowledge(recipient, item["message_id"], "incorrect")


def test_reply_derives_recipient_requires_ack_and_has_one_immutable_result(ledger):
    store, _, _, (sender, recipient), _ = ledger
    sent = send(ledger)
    deliver(ledger)
    with pytest.raises(ValueError, match="acknowledg"):
        store.reply(recipient, sent["message_id"], "reply", {"answer": "because"})
    item = store.receive(recipient)["messages"][0]
    store.acknowledge(recipient, item["message_id"], item["receipt_nonce"])
    result = store.reply(recipient, item["message_id"], "reply", {"answer": "because"})
    repeat = store.reply(recipient, item["message_id"], "reply", {"answer": "because"})
    assert result["message_id"] == repeat["message_id"]
    assert result["recipient_actor_id"] == sender.actor_id
    assert result["kind"] == "review_reply"
    for request_id, body in (
        ("different", {"answer": "because"}),
        ("reply", {"answer": "changed"}),
    ):
        with pytest.raises(ValueError, match="reply|different"):
            store.reply(recipient, item["message_id"], request_id, body)


def test_retry_cannot_change_recipient_kind_or_body(ledger):
    store, _, _, (sender, recipient), _ = ledger
    original = send(ledger)
    assert send(ledger)["message_id"] == original["message_id"]
    for destination, kind, body in (
        (sender.actor_id, "review_request", {"ask": "why"}),
        (recipient.actor_id, "correction", {"ask": "why"}),
        (recipient.actor_id, "review_request", {"ask": "changed"}),
    ):
        with pytest.raises(ValueError):
            store.send(sender, destination, "question", kind, body)


def test_unknown_or_changed_broker_payload_is_rejected_without_delivery(ledger):
    store, model, _, (_, recipient), _ = ledger
    send(ledger)
    row = store.pending_outbox()[0]
    value = model.ActorEnvelope.from_bytes(row["payload"])
    for changed in (
        replace(value, body={"tampered": True}),
        replace(value, message_id=str(uuid4())),
    ):
        with pytest.raises(ValueError, match="payload|Unknown|unknown|stored"):
            store.deliver(changed.to_bytes(), "ACTORS_TEST", 1)
    assert store.receive(recipient)["messages"] == []


@pytest.mark.parametrize("action", ["expire", "revoke", "finish"])
def test_new_delivery_after_expiry_revocation_or_finish_is_terminal_rejection(ledger, action):
    store, _, run, (_, recipient), clock = ledger
    send(ledger)
    raw = store.pending_outbox()[0]["payload"]
    if action == "expire":
        clock[0] += 101
    elif action == "revoke":
        store.revoke_actor(run, recipient.actor_id, 1, "retired")
    else:
        store.finish_run(run, "closed", auth=recipient)
    assert store.deliver(raw, "ACTORS_TEST", 2)["delivery"] == (
        "expired" if action == "expire" else "rejected"
    )
    with pytest.raises(ValueError, match="expired|active|finished|revoked|closed"):
        store.receive(recipient)


def test_already_delivered_fact_survives_later_expiry_without_redelivery(ledger):
    store, _, _, (_, recipient), clock = ledger
    send(ledger)
    raw = store.pending_outbox()[0]["payload"]
    store.deliver(raw, "ACTORS_TEST", 3)
    clock[0] += 101
    result = store.deliver(raw, "ACTORS_TEST", 3)
    assert result["delivery"] == "delivered" and result["duplicate"] is True
    with pytest.raises(ValueError, match="expired"):
        store.receive(recipient)


@pytest.mark.parametrize("limit", [0, 9, True, -1])
def test_receive_bound_is_eight_and_boolean_is_not_an_integer_limit(ledger, limit):
    with pytest.raises(ValueError, match="limit"):
        ledger[0].receive(ledger[3][0], limit=limit)


def test_body_and_message_count_bounds_are_enforced(ledger):
    store, _, _, (sender, recipient), _ = ledger
    with pytest.raises(ValueError, match="body|bound"):
        store.send(sender, recipient.actor_id, "big", "review_request", {"text": "日" * 6000})
    # Both actors send: one sender alone stops at its share (MAX_MESSAGES_PER_SENDER).
    for i in range(256):
        if i % 2:
            store.send(recipient, sender.actor_id, f"message-{i}", "review_request", {"a": 1})
        else:
            send(ledger, f"message-{i}")
    with pytest.raises(ValueError, match="capacity|256|bound"):
        store.send(recipient, sender.actor_id, "overflow", "review_request", {"a": 1})


def test_model_rejects_duplicate_json_fields_and_bad_scalar_types(ledger):
    _, model, _, _, _ = ledger
    send(ledger)
    raw = ledger[0].pending_outbox()[0]["payload"]
    duplicated = raw[:-1] + b',"schema_version":1}'
    with pytest.raises(ValueError, match="duplicate"):
        model.ActorEnvelope.from_bytes(duplicated)
    envelope = model.ActorEnvelope.from_bytes(raw)
    for field, value in (
        ("sender_epoch", True),
        ("created_at", float("nan")),
        ("schema_version", 2),
    ):
        with pytest.raises(ValueError):
            replace(envelope, **{field: value})


def test_reopen_preserves_offer_nonce_ack_reply_and_frontend(ledger):
    store, model, run, (_, recipient), clock = ledger
    original = send(ledger)
    deliver(ledger)
    offer = store.receive(recipient)["messages"][0]
    reopened = type(store)(store.path, clock=lambda: clock[0])
    assert reopened.receive(recipient)["messages"][0]["receipt_nonce"] == offer["receipt_nonce"]
    reopened.acknowledge(recipient, original["message_id"], offer["receipt_nonce"])
    reply = reopened.reply(recipient, original["message_id"], "response", {"answer": "yes"})
    again = type(store)(store.path, clock=lambda: clock[0])
    assert again.receive(recipient)["messages"] == []
    assert (
        again.reply(recipient, original["message_id"], "response", {"answer": "yes"})["message_id"]
        == reply["message_id"]
    )
    assert len(again.export(run, auth=recipient)["messages"]) == 2


def test_concurrent_retry_creates_one_message_and_outbox_record(ledger):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(pool.map(lambda _: send(ledger, "concurrent"), range(12)))
    assert len({row["message_id"] for row in receipts}) == 1
    assert len(ledger[0].pending_outbox()) == 1


def test_credential_cannot_be_reused_in_another_run_or_actor(ledger, tmp_path):
    store, _, _, (sender, _), _ = ledger
    other = str(uuid4())
    store.create_run(other, str(tmp_path), ["new"])
    with pytest.raises(ValueError, match="credential"):
        store.stage_actor(other, str(uuid4()), "new", "test-client", sender.credential)


def test_coordinator_only_assignment_kinds_are_enforced_atomically(tmp_path):
    store_type, model = api()
    store = store_type(tmp_path / "roles.sqlite")
    run = str(uuid4())
    store.create_run(run, str(tmp_path), ["coordinator", "worker"])
    actors = {}
    for role in ("coordinator", "worker"):
        auth = model.ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
        store.stage_actor(run, auth.actor_id, role, "test", auth.credential)
        store.activate_actor(run, auth.actor_id, 1, {"native_id": role})
        actors[role] = auth
    store.seal_run(run, auth=actors["coordinator"])
    coordinator, worker = actors["coordinator"], actors["worker"]
    for kind in ("assignment", "correction", "finish"):
        with pytest.raises(ValueError, match="role|coordinator"):
            store.send(worker, coordinator.actor_id, kind, kind, {})
        assert store.send(coordinator, worker.actor_id, kind, kind, {})["kind"] == kind
    with pytest.raises(ValueError, match="role|coordinator"):
        store.send(coordinator, worker.actor_id, "counted", "review_request", {})
    assert len(store.export(run, auth=coordinator)["messages"]) == 3
    question = store.send(worker, coordinator.actor_id, "review", "review_request", {})
    raw = next(
        row["payload"]
        for row in store.pending_outbox()
        if row["message_id"] == question["message_id"]
    )
    store.deliver(raw, "ACTORS_TEST", 1)
    offered = store.receive(coordinator)["messages"][0]
    store.acknowledge(coordinator, offered["message_id"], offered["receipt_nonce"])
    with pytest.raises(ValueError, match="role|coordinator"):
        store.reply(coordinator, offered["message_id"], "reply", {})


@pytest.mark.parametrize("field", ["kind", "request_id"])
def test_invalid_send_scalars_are_validation_errors(ledger, field):
    store, _, _, (sender, recipient), _ = ledger
    values = {
        "to_actor": recipient.actor_id,
        "request_id": "valid",
        "kind": "review_request",
        "body": {},
    }
    values[field] = []
    with pytest.raises(ValueError):
        store.send(sender, **values)


def test_non_ascii_ack_nonce_is_a_validation_error(ledger):
    store, _, _, (_, recipient), _ = ledger
    send(ledger)
    deliver(ledger)
    offered = store.receive(recipient)["messages"][0]
    with pytest.raises(ValueError, match="nonce"):
        store.acknowledge(recipient, offered["message_id"], "日")


def test_local_expiry_before_first_broker_delivery_is_not_a_redelivery(ledger):
    store, _, _, _, clock = ledger
    send(ledger)
    raw = store.pending_outbox()[0]["payload"]
    clock[0] += 101
    assert store.pending_outbox() == []
    result = store.deliver(raw, "ACTORS_TEST", 1)
    assert result["delivery"] == "expired"
    assert result["duplicate"] is False
    assert store.deliver(raw, "ACTORS_TEST", 1)["duplicate"] is True


def test_active_run_limit_and_typed_public_error_messages(tmp_path):
    store_type, model = api()
    error = model.ActorError("binding_invalid")
    assert error.code == "binding_invalid"
    assert "binding" in str(error)
    for code in ("invalid_request", "unavailable", "capacity"):
        assert model.ActorError(code).code == code
    with pytest.raises(ValueError):
        model.ActorError("secret-arbitrary-code")
    store = store_type(tmp_path / "capacity.sqlite")
    runs = [str(uuid4()) for _ in range(8)]
    owners = [store.create_run(run, str(tmp_path), ["one"])["owner_token"] for run in runs]
    with pytest.raises(model.ActorError, match="capacity"):
        store.create_run(str(uuid4()), str(tmp_path), ["one"])
    store.finish_run(runs[0], owner_token=owners[0])
    assert store.create_run(str(uuid4()), str(tmp_path), ["one"])["state"] == "staging"


def test_first_authenticated_frontend_remains_bound_after_a_refused_operation(tmp_path):
    store_type, model = api()
    store = store_type(tmp_path / "frontend.sqlite")
    run = str(uuid4())
    store.create_run(run, str(tmp_path), ["sender", "recipient"])
    actors = []
    for role in ("sender", "recipient"):
        auth = model.ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
        store.stage_actor(run, auth.actor_id, role, "test", auth.credential)
        store.activate_actor(run, auth.actor_id, 1, {"native_id": role})
        actors.append(auth)
    store.seal_run(run, auth=actors[0])
    sender, recipient = actors
    with pytest.raises(ValueError, match="role"):
        store.send(sender, recipient.actor_id, "denied", "assignment", {})
    with pytest.raises(ValueError, match="frontend"):
        store.receive(replace(sender, frontend_id=str(uuid4())))


def test_only_one_first_frontend_can_win_concurrent_bootstrap(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store_type, model = api()
    store = store_type(tmp_path / "race.sqlite")
    run = str(uuid4())
    store.create_run(run, str(tmp_path), ["actor"])
    auth = model.ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
    store.stage_actor(run, auth.actor_id, "actor", "test", auth.credential)
    competitor = replace(auth, frontend_id=str(uuid4()))

    def attempt(value):
        try:
            store.receive(value)
            return value.frontend_id
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (auth, competitor)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store.roster(run, auth=auth)["actors"][0]["frontend_id"] == winners[0]


@pytest.mark.parametrize("operation", ["status", "roster", "export"])
def test_run_reads_require_proof(ledger, operation):
    """red_at_base: a known run ID alone cannot authorize an inspection."""
    store, model, run, _, _ = ledger
    send(ledger)
    with pytest.raises(model.ActorError, match="credential"):
        getattr(store, operation)(run)


@pytest.mark.parametrize("operation", ["create", "seal", "finish"])
def test_status_returning_callers_require_proof_without_mutating(ledger, tmp_path, operation):
    """red_at_base: lifecycle status returns cannot bypass inspection authentication."""
    store, model, run, _, _ = ledger
    with store.connection() as db:
        before = dict(store._run(db, run))
    with pytest.raises(model.ActorError, match="credential"):
        if operation == "create":
            store.create_run(run, str(tmp_path), ["sender", "recipient"], ttl_seconds=100)
        elif operation == "seal":
            store.seal_run(run)
        else:
            store.finish_run(run)
    with store.connection() as db:
        assert dict(store._run(db, run)) == before


@pytest.mark.parametrize("operation", ["status", "roster", "export"])
@pytest.mark.parametrize("invalid", ["credential", "epoch", "actor", "run", "revoked", "dict"])
def test_run_reads_refuse_invalid_participation(ledger, tmp_path, operation, invalid):
    """red_at_base: proof is typed, credentialed, current and scoped to this run."""
    store, model, run, (sender, _), _ = ledger
    proof = sender
    if invalid == "credential":
        proof = replace(sender, credential="0" * 64)
    elif invalid == "epoch":
        proof = replace(sender, epoch=2)
    elif invalid == "actor":
        proof = replace(sender, actor_id=str(uuid4()))
    elif invalid == "run":
        other = str(uuid4())
        store.create_run(other, str(tmp_path), ["outsider"])
        proof = model.ActorAuth(other, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
        store.stage_actor(other, proof.actor_id, "outsider", "test", proof.credential)
    elif invalid == "revoked":
        store.revoke_actor(run, sender.actor_id, sender.epoch)
    else:
        proof = {"run_id": run, "credential": sender.credential}
    with pytest.raises(model.ActorError):
        getattr(store, operation)(run, auth=proof)


@pytest.mark.parametrize("operation", ["status", "roster", "export"])
def test_participant_inspection_does_not_claim_or_replace_frontend(tmp_path, operation):
    """red_at_base: inspection checks participation without consuming the MCP binding."""
    store_type, model = api()
    store = store_type(tmp_path / "inspect.sqlite")
    run = str(uuid4())
    store.create_run(run, str(tmp_path), ["reader"])
    proof = model.ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
    store.stage_actor(run, proof.actor_id, "reader", "test", proof.credential)
    inspect = getattr(store, operation)
    inspect(run, auth=proof)
    with store.connection() as db:
        assert store._actor(db, run, proof.actor_id, 1)["frontend_id"] is None
    frontend = replace(proof, frontend_id=str(uuid4()))
    store.receive(frontend)
    inspect(run, auth=proof)
    with store.connection() as db:
        assert store._actor(db, run, proof.actor_id, 1)["frontend_id"] == frontend.frontend_id
    with pytest.raises(model.ActorError, match="frontend"):
        store.receive(proof)


def test_owner_token_is_once_only_persistent_and_not_in_views(tmp_path):
    """red_at_base: creation keeps its dict and grants one owner credential once."""
    store_type, model = api()
    store = store_type(tmp_path / "owner.sqlite")
    run = str(uuid4())
    created = store.create_run(run, str(tmp_path), ["reader"])
    assert created["state"] == "staging"
    token = created["owner_token"]
    assert len(token) == 64
    assert token.encode() not in store.path.read_bytes()
    reopened = store_type(store.path)
    views = [
        getattr(reopened, name)(run, owner_token=token) for name in ("status", "roster", "export")
    ]
    repeated = reopened.create_run(run, str(tmp_path), ["reader"], owner_token=token)
    assert "owner_token" not in repeated
    serialized = json.dumps([*views, repeated])
    assert token not in serialized
    assert "owner_token_digest" not in serialized
    with pytest.raises(model.ActorError, match="credential"):
        reopened.create_run(run, str(tmp_path), ["reader"])
    reopened.finish_run(run, owner_token=token)
    assert reopened.status(run, owner_token=token)["state"] == "finished"


@pytest.mark.parametrize("operation", ["status", "roster", "export"])
def test_owner_proof_rejects_wrong_run_malformed_and_ambiguous_credentials(tmp_path, operation):
    """red_at_base: owner authority is scoped to a run and cannot be empty or ambiguous."""
    store_type, model = api()
    store = store_type(tmp_path / "owners.sqlite")
    run, other = str(uuid4()), str(uuid4())
    token = store.create_run(run, str(tmp_path), ["reader"])["owner_token"]
    other_token = store.create_run(other, str(tmp_path), ["reader"])["owner_token"]
    auth = model.ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
    store.stage_actor(run, auth.actor_id, "reader", "test", auth.credential)
    for bad in (other_token, "0" * 64, "", "日", [], False):
        with pytest.raises(model.ActorError, match="credential"):
            getattr(store, operation)(run, owner_token=bad)
    with pytest.raises(model.ActorError, match="credential"):
        getattr(store, operation)(run, owner_token=token, auth=auth)


@pytest.mark.parametrize("state", ["staging", "sealed", "finished", "expired"])
def test_legacy_run_migration_keeps_participant_access(tmp_path, state):
    """red_at_base: old schemas gain no implicit owner, but retained actor proof works."""
    import sqlite3

    store_type, model = api()
    path = tmp_path / "legacy.sqlite"
    run = str(uuid4())
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE actor_runs (run_id TEXT PRIMARY KEY, project TEXT NOT NULL, "
            "roles TEXT NOT NULL, ttl_seconds INTEGER NOT NULL, state TEXT NOT NULL, "
            "roster_digest TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, "
            "sealed_at REAL, finished_at REAL, reason TEXT NOT NULL DEFAULT '')"
        )
        db.execute(
            "INSERT INTO actor_runs(run_id,project,roles,ttl_seconds,state,created_at,expires_at) "
            "VALUES(?,?,?,3600,'staging',1000,4600)",
            (run, str(tmp_path), '["reader"]'),
        )
    store = store_type(path, clock=lambda: 1000.0)
    auth = model.ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
    store.stage_actor(run, auth.actor_id, "reader", "test", auth.credential)
    store.activate_actor(run, auth.actor_id, 1, {"native_id": "legacy-reader"})
    with store.connection() as db:
        db.execute(
            "UPDATE actor_runs SET state=?, expires_at=? WHERE run_id=?",
            (state if state != "expired" else "sealed", 999 if state == "expired" else 4600, run),
        )
    reopened = store_type(path, clock=lambda: 1000.0)
    for operation in ("status", "roster", "export"):
        with pytest.raises(model.ActorError):
            getattr(reopened, operation)(run)
        with pytest.raises(model.ActorError):
            getattr(reopened, operation)(run, owner_token="0" * 64)
        getattr(reopened, operation)(run, auth=auth)
    assert reopened.status(run, auth=auth)["state"] == state
    with reopened.connection() as db:
        assert reopened._run(db, run)["owner_token_digest"] is None
    if state in {"finished", "expired"}:
        with pytest.raises(model.ActorError, match=state):
            reopened.receive(auth)


def test_revoked_credentials_leave_read_only_offline_evidence_recovery(ledger):
    """red_at_base: revocation denies API proof while the OS owner can recover the ledger."""
    import sqlite3

    store, model, run, auths, _ = ledger
    sent = send(ledger)
    for auth in auths:
        store.revoke_actor(run, auth.actor_id, auth.epoch)
        with pytest.raises(model.ActorError, match="binding"):
            store.export(run, auth=auth)
    with sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT payload FROM actor_messages WHERE run_id=?", (run,)).fetchall()
        assert json.loads(rows[0][0])["message_id"] == sent["message_id"]
        bindings = db.execute("SELECT native_binding FROM actors WHERE run_id=?", (run,)).fetchall()
        assert len(bindings) == 2 and all(json.loads(row[0])["native_id"] for row in bindings)


def test_owner_and_participant_export_preserve_the_same_evidence(tmp_path):
    """red_at_base: either proof returns the complete existing export shape."""
    store_type, model = api()
    store = store_type(tmp_path / "evidence.sqlite")
    run = str(uuid4())
    token = store.create_run(run, str(tmp_path), ["author", "reviewer"])["owner_token"]
    auths = []
    for role in ("author", "reviewer"):
        auth = model.ActorAuth(run, str(uuid4()), 1, secrets.token_hex(32), str(uuid4()))
        store.stage_actor(run, auth.actor_id, role, "test", auth.credential)
        store.activate_actor(run, auth.actor_id, 1, {"native_id": role})
        auths.append(auth)
    store.seal_run(run, owner_token=token)
    author, reviewer = auths
    sent = store.send(author, reviewer.actor_id, "one", "review_request", {"question": "fixture"})
    raw = store.pending_outbox()[0]["payload"]
    store.deliver(raw, "TEST", 1)
    offer = store.receive(reviewer)["messages"][0]
    store.acknowledge(reviewer, sent["message_id"], offer["receipt_nonce"])
    store.finish_run(run, owner_token=token)
    owner_view = store.export(run, owner_token=token)
    assert owner_view == store.export(run, auth=reviewer)
    assert owner_view["messages"][0]["envelope"]["body"] == {"question": "fixture"}
    assert {row["native_binding"]["native_id"] for row in owner_view["roster"]["actors"]} == {
        "author",
        "reviewer",
    }
    assert len(owner_view["transport_observations"]) == 1
    assert [row["kind"] for row in owner_view["receipts"]] == ["offered", "acknowledged"]
