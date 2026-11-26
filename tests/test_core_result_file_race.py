"""T-187 on a remote terminal: a `done` send that races the agent's own turn.

A worker on an `hc_send_machines` terminal is told not to call `task_complete`;
its result file is what completes the receipt. These tests drive the whole path
through the fake `hc` of `test_core_result_file.py`: the send, the after-send
identity check, and the read of the result file. The first docstring line of
each test is `red_at_pretip`: it fails at `f18cb1cd`, `main` before T-187.
"""

from pathlib import Path
from typing import Any

import pytest
from test_core_result_file import (
    MACHINE,
    REMOTE,
    FakeCore,
    delivered,
    make_fake,
    row,
)
from test_core_result_file_read import Clock, install_clock, path_of, valid

from answerback_bridge.config import Config
from answerback_bridge.service import Bridge

# The receipt of a send that went into a turn the agent had started on its own.
RACED = {"turn": 4, "started": False, "status": "working"}


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return install_clock(monkeypatch)


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    fake = make_fake(tmp_path)
    terminals = fake.settings()["terminals"]
    terminals[REMOTE] = row(REMOTE, status={"app": "claude", "state": "done"})
    fake.update(terminals=terminals, send=RACED)
    return fake


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> Any:
    path = tmp_path / "config.json"
    Config(
        hc_enabled=True,
        codex_enabled=False,
        hc_binary=str(fake.binary),
        hc_send_machines=[MACHINE],
        hc_send_to_done=True,
    ).save(path)
    bridge = Bridge(Config.load(path), path)
    yield bridge
    await bridge.close()


async def test_raced_done_send_to_a_remote_terminal_completes_from_its_result_file(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: the same agent after the send makes the result file readable.

    At `f18cb1cd` the receipt was `uncertain` without `delivery_verified`, so the
    adapter never read the file and the terminal stayed reserved.
    """
    fake.update(after_send={"status": {"app": "claude", "state": "working"}, "turn": 4})
    task = await delivered(bridge, REMOTE, "Review the parser", "remote-race")
    assert task["status"] == "uncertain"
    assert task["result"] is None
    meta = task["metadata"]
    assert meta["typed_while_working"] is True
    assert meta["delivery_verified"] is True
    assert (meta["turn_before"], meta["turn_after"]) == (3, 4)
    assert meta["delivery"]["status"] == "working"
    assert meta["completion"] == "result_file"
    assert "identity_invalidated" not in meta
    (send,) = fake.sends()
    assert send[:4] == ["send", "--json", "--", REMOTE]  # a done send has no `--if`
    assert "do not call task_complete" in send[-1]
    # No file yet: the receipt is read and stays as it is.
    assert (await bridge.get_task(task["id"]))["status"] == "uncertain"
    assert len(fake.reads()) == 1
    # The worker writes its framed result file; the next read completes the receipt.
    fake.put_file(path_of(task), valid(task, "Parser reviewed."))
    clock.now += 60
    done = await bridge.get_task(task["id"])
    assert done["status"] == "completed"
    assert done["result"] == "Parser reviewed."
    # T-189: the settled receipt carries the account-level label of its result.
    assert done["metadata"]["result_authentication"] == "trusted_os_account"
    assert done["metadata"]["result_machine"] == meta["result_machine"]
    # The terminal is free again: a new assignment to it is reserved.
    again, created = bridge.store.reserve(task["target"], "Next", "after-race", {"kind": "hc"})
    assert created and again["id"] != task["id"]


async def test_raced_done_send_into_a_replaced_agent_never_reads_the_result_file(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: a changed binding after the send invalidates the submission.

    A result file written for that receipt is not read and does not complete it. At
    `f18cb1cd` the raced receipt was recorded before the after-send check ran, so the
    submission was not invalidated.
    """
    fake.update(
        after_send={
            "foreground": {"pid": 999, "argv": ["claude"], "cwd": "/home/user/code/project"},
            "status": {"app": "claude", "state": "working"},
            "turn": 4,
        }
    )
    task = await delivered(bridge, REMOTE, "Review the parser", "remote-replaced")
    assert task["status"] == "uncertain"
    meta = task["metadata"]
    assert meta["identity_invalidated"] is True
    assert "delivery_verified" not in meta
    assert "typed_while_working" not in meta
    fake.put_file(path_of(task), valid(task, "Parser reviewed."))
    clock.now += 60
    after = await bridge.get_task(task["id"])
    assert after["status"] == "uncertain"
    assert after["result"] is None
    assert fake.reads() == []
    with pytest.raises(ValueError, match="invalidated"):
        await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert fake.reads() == []
    assert bridge.store.get(task["id"])["status"] == "uncertain"
    # The terminal stays reserved until a person inspects the receipt.
    with pytest.raises(ValueError, match="already has task"):
        bridge.store.reserve(task["target"], "Next", "after-replaced", {"kind": "hc"})
