from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from answerback_bridge.rex_mailbox import RexMailbox
from answerback_bridge.tasks import TaskStore


def binding(**changes: object) -> dict:
    return {
        "endpoint": "unix:///tmp/rex.sock",
        "server_instance_id": "server-1",
        "server_host_id": "host-1",
        "session_id": "session:11111111-1111-1111-1111-111111111111",
        "block_id": "block:22222222-2222-2222-2222-222222222222",
        "block_host_id": "host-1",
        "foreground_pid": 42,
        "foreground_name": "python",
        "foreground_path": None,
        "foreground_script": None,
        "foreground_cwd": "/project",
        **changes,
    }


@pytest.fixture
def mailbox(tmp_path: Path) -> RexMailbox:
    return RexMailbox(TaskStore(tmp_path / "tasks.sqlite"))


def enroll(mailbox: RexMailbox, request: str = "enroll-1", **changes: object) -> dict:
    return mailbox.enroll("reviewer", binding(**changes), request)


def test_migration_keeps_tasks_and_worker_sessions(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    old, _ = store.reserve("kiro:old", "old work", "old")
    store.save_worker_session("old", "session-old", "/old", profile="herdr_bridge")
    RexMailbox(store)
    assert store.get(old["id"]) == old
    assert store.worker_session("old")["profile"] == "herdr_bridge"


def test_enrollment_retry_conflict_retirement(mailbox: RexMailbox) -> None:
    peer = enroll(mailbox)
    assert enroll(mailbox) == peer
    with pytest.raises(ValueError):
        mailbox.enroll("other", binding(), "enroll-1")
    with pytest.raises(ValueError):
        enroll(mailbox, "enroll-2", server_instance_id="new-server", foreground_pid=43)
    retired = mailbox.retire(peer["peer_id"])
    assert retired["state"] == "retired"
    assert mailbox.retire(peer["peer_id"]) == retired
    assert enroll(mailbox) == retired
    assert mailbox.peers() == []
    assert enroll(mailbox, "enroll-2")["peer_id"] != peer["peer_id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"foreground_pid": True},
        {"foreground_pid": 0},
        {"foreground_cwd": "relative"},
        {"foreground_path": []},
        {"foreground_script": {}},
        {"server_host_id": "other"},
        {"endpoint": "https://remote"},
        {"foreground_name": "bad\nname"},
        {"session_id": ""},
        {"server_instance_id": 123},
    ],
)
def test_binding_validation(mailbox: RexMailbox, changes: dict) -> None:
    with pytest.raises(ValueError):
        enroll(mailbox, **changes)


def test_names_binding_bounds_and_canonical_copy(mailbox: RexMailbox) -> None:
    for name in ("", "bad\nname", "é" * 65):
        with pytest.raises(ValueError):
            mailbox.enroll(name, binding(), "name")
    with pytest.raises(ValueError):
        enroll(mailbox, foreground_name="x" * 17000)
    source = binding()
    peer = mailbox.enroll("reviewer", source, "copy")
    source["foreground_pid"] = 99
    assert mailbox.get(peer["peer_id"])["binding"]["foreground_pid"] == 42


def test_submit_retry_conflicts_and_concurrency(mailbox: RexMailbox) -> None:
    peer = enroll(mailbox)["peer_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: mailbox.submit(peer, "work", "work-1"), range(8)))
    assert all(task == receipts[0] for task in receipts)
    assert receipts[0]["status"] == "queued"
    assert "message" not in receipts[0]
    with pytest.raises(ValueError):
        mailbox.submit(peer, "changed", "work-1")
    with pytest.raises(ValueError):
        mailbox.submit(peer, "new", "work-2")
    other = enroll(mailbox, "enroll-other", block_id="block:33333333-3333-3333-3333-333333333333")[
        "peer_id"
    ]
    with pytest.raises(ValueError):
        mailbox.submit(other, "work", "work-1")
    with pytest.raises(ValueError):
        mailbox.retire(peer)


def test_inbox_ack_completion_and_restart(mailbox: RexMailbox) -> None:
    peer = enroll(mailbox)["peer_id"]
    task = mailbox.submit(peer, "work", "work-1")
    task_id = task["id"]
    assert mailbox.inbox(peer)["tasks"][0]["message"] == "work"
    assert mailbox.inbox(peer)["tasks"][0]["status"] == "queued"
    with pytest.raises(ValueError):
        mailbox.complete(peer, task_id, "done")
    with pytest.raises(ValueError):
        mailbox.store.complete_worker(task_id, "done")
    reopened = RexMailbox(TaskStore(mailbox.store.path))
    assert reopened.store.recover_pending() == 0
    assert reopened.inbox(peer)["tasks"][0]["id"] == task_id
    ack = reopened.acknowledge(peer, task_id)
    assert ack["status"] == "submitted"
    assert ack["metadata"]["acknowledged_by"] == peer
    assert reopened.acknowledge(peer, task_id) == ack
    restarted = RexMailbox(TaskStore(mailbox.store.path))
    assert restarted.store.recover_pending() == 0
    inbox = restarted.inbox(peer)
    assert inbox["tasks"] == []
    assert inbox["active_task"] == ack
    assert "message" not in inbox["active_task"]
    done = restarted.complete(peer, task_id, "done")
    assert done["status"] == "completed"
    assert restarted.complete(peer, task_id, "done") == done
    with pytest.raises(ValueError):
        restarted.complete(peer, task_id, "changed")
    with pytest.raises(ValueError):
        restarted.acknowledge(peer, task_id)
    restarted.retire(peer)
    assert restarted.submit(peer, "work", "work-1") == done
    assert restarted.complete(peer, task_id, "done") == done


def test_wrong_association_and_invalidation_preserves_history(mailbox: RexMailbox) -> None:
    peer = enroll(mailbox)["peer_id"]
    other = enroll(mailbox, "other", block_id="block:33333333-3333-3333-3333-333333333333")[
        "peer_id"
    ]
    task = mailbox.submit(peer, "work", "work-1")
    for call in (
        lambda: mailbox.acknowledge(other, task["id"]),
        lambda: mailbox.complete(other, task["id"], "done"),
    ):
        with pytest.raises(ValueError):
            call()
    mailbox.acknowledge(peer, task["id"])
    done = mailbox.complete(peer, task["id"], "done")
    next_task = mailbox.submit(peer, "next", "work-2")
    mailbox.invalidate(peer, "process changed")
    assert mailbox.get(peer)["state"] == "stale"
    assert mailbox.store.get(task["id"])["result"] == "done"
    assert mailbox.submit(peer, "work", "work-1") == done
    uncertain = mailbox.store.get(next_task["id"])
    assert uncertain["status"] == "uncertain"
    assert uncertain["metadata"]["identity_invalidated"] is True
    for call in (
        lambda: mailbox.acknowledge(peer, next_task["id"]),
        lambda: mailbox.complete(peer, next_task["id"], "done"),
        lambda: mailbox.submit(peer, "new", "work-3"),
        lambda: mailbox.retire(peer),
    ):
        with pytest.raises(ValueError):
            call()
    mailbox.store.update(next_task["id"], "abandoned")
    mailbox.retire(peer)
    mailbox.invalidate(peer, "late")
    assert mailbox.get(peer)["state"] == "retired"
    assert mailbox.store.get(next_task["id"])["status"] == "abandoned"


def test_terminal_and_result_bounds(mailbox: RexMailbox) -> None:
    peer = enroll(mailbox)["peer_id"]
    task = mailbox.submit(peer, "work", "work-1")
    mailbox.acknowledge(peer, task["id"])
    with pytest.raises(ValueError):
        mailbox.complete(peer, task["id"], "x" * 200001)
    mailbox.store.update(task["id"], "failed", detail="failed")
    with pytest.raises(ValueError):
        mailbox.complete(peer, task["id"], "done")
    mailbox.invalidate(peer, "changed")
    assert mailbox.store.get(task["id"])["status"] == "failed"


def test_nonretired_capacity(mailbox: RexMailbox) -> None:
    for number in range(128):
        enroll(
            mailbox, f"enroll-{number}", block_id=f"block:{number:08x}-1111-1111-1111-111111111111"
        )
    assert len(mailbox.peers()) == 128
    with pytest.raises(ValueError):
        enroll(mailbox, "over", block_id="block:ffffffff-1111-1111-1111-111111111111")


def test_concurrent_enrollment_retries_share_one_registration(mailbox: RexMailbox) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        peers = list(pool.map(lambda _: enroll(mailbox), range(8)))
    assert all(peer == peers[0] for peer in peers)
    assert mailbox.peers() == [peers[0]]


def test_stale_retry_unknown_task_and_message_bounds(mailbox: RexMailbox) -> None:
    peer = enroll(mailbox)["peer_id"]
    for message in (" ", "x" * 100001):
        with pytest.raises(ValueError, match="Message"):
            mailbox.submit(peer, message, "invalid")
    for call in (
        lambda: mailbox.acknowledge(peer, "missing"),
        lambda: mailbox.complete(peer, "missing", "done"),
    ):
        with pytest.raises(ValueError, match="Unknown task"):
            call()
    queued = mailbox.submit(peer, "work", "work-1")
    mailbox.invalidate(peer, "changed")
    retry = mailbox.submit(peer, "work", "work-1")
    assert retry["id"] == queued["id"]
    assert retry["status"] == "uncertain"
    assert retry["metadata"]["identity_invalidated"] is True
    with pytest.raises(ValueError, match="stale"):
        mailbox.inbox(peer)
