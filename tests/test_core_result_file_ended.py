"""Row A2: an ended terminal with no result, and how often the bridge reads.

The fake `hc` and its sample forms live in `test_core_result_file.py`. Labels as there:
the first docstring line of each test is `red_at_pretip` or `pins_existing`.
"""

from pathlib import Path
from typing import Any

import pytest
from test_core_result_file import (
    MACHINE,
    REMOTE,
    FakeCore,
    delivered,
    finished,
    make_bridge,
    make_fake,
    row,
)
from test_core_result_file_read import (
    Clock,
    install_clock,
    path_of,
    read_argv,
    remote_task,
    valid,
)

from answerback_bridge.process import BridgeError
from answerback_bridge.providers.herdr import core as core_module
from answerback_bridge.service import Bridge


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return install_clock(monkeypatch)


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> Any:
    bridge = make_bridge(fake, tmp_path)
    yield bridge
    await bridge.close()


def end_terminal(fake: FakeCore, how: str) -> None:
    terminals = fake.settings()["terminals"]
    if how == "finished":
        terminals[REMOTE] = finished()  # "get: that terminal after its program has ended"
    else:
        del terminals[REMOTE]  # "get: the same address after the terminal was removed"
    fake.update(terminals=terminals)


@pytest.mark.parametrize("how", ["finished", "removed"])
async def test_ended_terminal_with_no_file_becomes_uncertain_and_a_late_file_completes(
    bridge: Bridge, fake: FakeCore, clock: Clock, how: str
) -> None:
    """red_at_pretip: an ended terminal is not a missing file (plan section 4, rule 5)."""
    task = await remote_task(bridge)
    end_terminal(fake, how)
    after = await bridge.get_task(task["id"])
    assert after["status"] == "uncertain"
    assert "terminal_ended_no_result" in after["detail"]
    # Read, ask Herdr core about the terminal, read one more time.
    assert fake.reads() == [read_argv(task), read_argv(task)]
    assert fake.calls()[-2:] == [["get", REMOTE, "--json"], read_argv(task)]
    assert len(fake.sends()) == 1
    # Still readable: a child of the ended terminal may still be writing (fact 14).
    assert task["id"] in bridge.observation_ids()
    clock.now += 6
    gets = len(fake.gets())
    assert (await bridge.get_task(task["id"]))["status"] == "uncertain"
    assert len(fake.gets()) == gets  # an uncertain receipt does not ask about the terminal
    fake.put_file(path_of(task), valid(task, "Written by a child"))
    clock.now += 6
    done = await bridge.get_task(task["id"])
    assert done["status"] == "completed" and done["result"] == "Written by a child"
    assert len(fake.sends()) == 1


async def test_valid_file_on_the_second_read_completes(bridge: Bridge, fake: FakeCore) -> None:
    """red_at_pretip: the read after the terminal check can still find the file."""
    task = await remote_task(bridge)
    end_terminal(fake, "finished")
    fake.update(files_after_get={f"{MACHINE}:{path_of(task)}": valid(task, "Just in time")})
    done = await bridge.get_task(task["id"])
    assert done["status"] == "completed" and done["result"] == "Just in time"


async def test_unknown_machine_on_the_terminal_check_changes_nothing(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: a machine Herdr core cannot answer for proves nothing ended."""
    task = await remote_task(bridge)
    fake.update(unknown_on_get=True)  # "get: a terminal on a machine name that hc does not know"
    with pytest.raises(BridgeError):
        await bridge.get_task(task["id"])
    assert bridge.store.get(task["id"])["status"] == "submitted"
    assert bridge.store.get(task["id"])["detail"] is None


async def test_live_terminal_with_no_file_changes_nothing(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: a missing file beside a live terminal is only "not yet"."""
    task = await remote_task(bridge)
    gets = len(fake.gets())
    for _ in range(3):
        assert (await bridge.get_task(task["id"]))["status"] == "submitted"
        clock.now += 6
    assert len(fake.reads()) == 3
    # The terminal is asked about once per "not yet" read, never more often.
    assert fake.gets()[gets:] == [["get", REMOTE, "--json"]] * 3
    assert bridge.store.get(task["id"])["detail"] is None


# Rate.


async def test_two_observations_inside_five_seconds_make_one_read(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: `last_result_read` spaces reads by 5 seconds; a person skips that."""
    task = await remote_task(bridge)
    await bridge.get_task(task["id"])
    clock.now += 4.9
    await bridge.call("task_wait", {"task_id": task["id"], "seconds": 1})
    assert len(fake.reads()) == 1
    await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert len(fake.reads()) == 2
    clock.now += 5
    await bridge.get_task(task["id"])
    assert len(fake.reads()) == 3


async def test_poll_ids_returns_remote_receipts_only_and_at_most_limit(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: local receipts never push remote ones out of the 4 per cycle."""
    terminals = fake.settings()["terminals"]
    for index in range(2, 8):
        terminals[f"t{index}"] = row(f"t{index}")
    for index in range(16, 22):
        terminals[f"{MACHINE}/t{index}"] = row(f"{MACHINE}/t{index}")
    fake.update(terminals=terminals)
    local = [await delivered(bridge, f"t{i}", "Local work", f"local-{i}") for i in range(2, 8)]
    remote = [await remote_task(bridge, f"remote-{i}", f"{MACHINE}/t{i}") for i in range(16, 22)]
    adapter = bridge.adapters.get("hc")
    remote_ids = {task["id"] for task in remote}
    first = adapter.poll_ids(4)
    assert len(first) == 4 and set(first) <= remote_ids
    assert not {task["id"] for task in local} & set(bridge.observation_ids())
    for task_id in first:
        await bridge.get_task(task_id)
    clock.now += 1
    second = adapter.poll_ids(4)
    assert set(second) >= remote_ids - set(first)
    assert len(adapter.poll_ids(100)) == 6


# Fix round 1, F3: a refused result file does not hide an ended terminal.


def refused_file(task: dict[str, Any], kind: str) -> str:
    if kind == "empty":
        token = task["metadata"]["result_token"]
        return f"answerback-result {task['id']} {token}\nanswerback-end {token}\n"
    return valid(task, "x" * (core_module.RESULT_MAX_CHARS + 1))


@pytest.mark.parametrize("how", ["finished", "removed"])
@pytest.mark.parametrize("kind", ["empty", "oversized"])
async def test_refused_file_beside_an_ended_terminal_becomes_uncertain(
    bridge: Bridge, fake: FakeCore, how: str, kind: str
) -> None:
    """red_at_pretip: a refused body used to end the read before the terminal check."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), refused_file(task, kind))
    end_terminal(fake, how)
    after = await bridge.get_task(task["id"])
    assert after["status"] == "uncertain"
    assert after["detail"].startswith("terminal_ended_no_result")
    assert after["result"] is None
    assert fake.reads() == [read_argv(task), read_argv(task)]
    assert fake.calls()[-2:] == [["get", REMOTE, "--json"], read_argv(task)]
    assert len(fake.sends()) == 1
    # A person can still recover it with a corrected file.
    fake.put_file(path_of(task), valid(task, "Corrected"))
    done = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert done["status"] == "completed" and done["result"] == "Corrected"


async def test_refused_file_then_terminal_ends_is_still_noticed(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: after a refusal, polls look at the terminal but not at the file."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), refused_file(task, "empty"))
    first = await bridge.get_task(task["id"])
    assert first["status"] == "submitted" and "empty" in first["detail"]
    assert len(fake.reads()) == 1
    assert task["id"] in bridge.observation_ids()
    # A live terminal: one `hc get`, no file read, nothing changes.
    clock.now += 6
    gets = len(fake.gets())
    assert (await bridge.get_task(task["id"]))["status"] == "submitted"
    assert fake.gets()[gets:] == [["get", REMOTE, "--json"]]
    assert len(fake.reads()) == 1
    # Inside 5 seconds nothing is asked at all.
    clock.now += 1
    await bridge.get_task(task["id"])
    assert fake.gets()[gets:] == [["get", REMOTE, "--json"]]
    # The terminal ends: one verification read, then uncertain.
    end_terminal(fake, "finished")
    clock.now += 6
    after = await bridge.get_task(task["id"])
    assert after["status"] == "uncertain"
    assert after["detail"].startswith("terminal_ended_no_result")
    assert len(fake.reads()) == 2
    # Refused and uncertain: nothing left for the loop to do until a person reconciles.
    assert task["id"] not in bridge.observation_ids()
    clock.now += 60
    calls = len(fake.calls())
    await bridge.get_task(task["id"])
    assert len(fake.calls()) == calls


async def test_refused_file_with_a_valid_file_on_the_verification_read_completes(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: the read after the terminal check can still find a valid file."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), refused_file(task, "oversized"))
    end_terminal(fake, "removed")
    fake.update(files_after_get={f"{MACHINE}:{path_of(task)}": valid(task, "Rewritten")})
    done = await bridge.get_task(task["id"])
    assert done["status"] == "completed" and done["result"] == "Rewritten"


# Fix round 2, R2-F3: a verified delivery whose turn start was not seen.


async def start_unseen_task(bridge: Bridge, fake: FakeCore) -> dict[str, Any]:
    """A remote receipt after a send whose receipt says `started: false`.

    `Core.send` accepts that receipt and `TaskStore.confirm_delivery` then stores
    `delivery_verified` with status `uncertain` ("did not observe the turn start").
    """
    fake.update(send={"turn": 4, "started": False, "status": "working"})
    task = await remote_task(bridge, "start-unseen")
    assert task["status"] == "uncertain"
    assert task["metadata"]["delivery_verified"] is True
    assert task["detail"] == "Core accepted text but did not observe the turn start"
    return task


@pytest.mark.parametrize("how", ["finished", "removed"])
@pytest.mark.parametrize("kind", ["empty", "oversized"])
async def test_start_unseen_refused_file_beside_an_ended_terminal_becomes_ended(
    bridge: Bridge, fake: FakeCore, clock: Clock, how: str, kind: str
) -> None:
    """red_at_pretip: the terminal of a start-unseen receipt was never looked at."""
    task = await start_unseen_task(bridge, fake)
    fake.put_file(path_of(task), refused_file(task, kind))
    end_terminal(fake, how)
    after = await bridge.get_task(task["id"])
    assert after["status"] == "uncertain"
    assert after["detail"].startswith("terminal_ended_no_result")
    assert after["result"] is None
    assert fake.calls()[-3:] == [read_argv(task), ["get", REMOTE, "--json"], read_argv(task)]
    # Classified and refused: the loop leaves it alone; a person can still recover it.
    assert task["id"] not in bridge.observation_ids()
    clock.now += 60
    calls = len(fake.calls())
    await bridge.get_task(task["id"])
    assert len(fake.calls()) == calls
    fake.put_file(path_of(task), valid(task, "Corrected"))
    done = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert done["status"] == "completed" and done["result"] == "Corrected"
    assert len(fake.sends()) == 1


@pytest.mark.parametrize("later", ["still_refused", "valid_by_then"])
async def test_start_unseen_refused_file_then_terminal_ends_between_polls(
    bridge: Bridge, fake: FakeCore, clock: Clock, later: str
) -> None:
    """red_at_pretip: polls keep looking at the terminal, then read the file once."""
    task = await start_unseen_task(bridge, fake)
    fake.put_file(path_of(task), refused_file(task, "empty"))
    first = await bridge.get_task(task["id"])
    assert first["status"] == "uncertain" and "empty" in first["detail"]
    assert task["id"] in bridge.observation_ids()
    # A live terminal: one `hc get`, no file read, and nothing inside 5 seconds.
    clock.now += 6
    start = len(fake.calls())
    await bridge.get_task(task["id"])
    clock.now += 1
    await bridge.get_task(task["id"])
    assert fake.calls()[start:] == [["get", REMOTE, "--json"]]
    assert task["id"] in bridge.observation_ids()
    end_terminal(fake, "finished")
    if later == "valid_by_then":
        fake.put_file(path_of(task), valid(task, "Written before the end"))
    clock.now += 6
    start = len(fake.calls())
    after = await bridge.get_task(task["id"])
    assert fake.calls()[start:] == [["get", REMOTE, "--json"], read_argv(task)]
    if later == "valid_by_then":
        assert after["status"] == "completed" and after["result"] == "Written before the end"
    else:
        assert after["status"] == "uncertain"
        assert after["detail"].startswith("terminal_ended_no_result")
        assert task["id"] not in bridge.observation_ids()


@pytest.mark.parametrize("how", ["finished", "removed"])
async def test_start_unseen_missing_file_beside_an_ended_terminal_then_a_late_file(
    bridge: Bridge, fake: FakeCore, clock: Clock, how: str
) -> None:
    """red_at_pretip: a missing file and an ended terminal give the ended reason."""
    task = await start_unseen_task(bridge, fake)
    end_terminal(fake, how)
    after = await bridge.get_task(task["id"])
    assert after["status"] == "uncertain"
    assert after["detail"].startswith("terminal_ended_no_result")
    assert fake.calls()[-3:] == [read_argv(task), ["get", REMOTE, "--json"], read_argv(task)]
    # Still polled for the file, but the terminal is not asked about again.
    assert task["id"] in bridge.observation_ids()
    clock.now += 6
    start = len(fake.calls())
    await bridge.get_task(task["id"])
    assert fake.calls()[start:] == [read_argv(task)]
    fake.put_file(path_of(task), valid(task, "Written by a child"))
    clock.now += 6
    done = await bridge.get_task(task["id"])
    assert done["status"] == "completed" and done["result"] == "Written by a child"
    assert len(fake.sends()) == 1


async def test_adapter_eligibility_and_the_polling_query_agree(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: both places decide by classification, and give the same answer.

    Each receipt is built from the forms the bridge stores: `reserve` with the
    metadata `CoreTasks.submit` writes, `confirm_core_delivery` with a send receipt,
    and the metadata keys the reader itself sets.
    """
    store = bridge.store
    adapter = bridge.adapters.get("hc")
    ended = {"terminal_ended": True}
    refused = {"result_refused": True}
    cases: dict[str, tuple[dict[str, Any], bool | None, dict[str, Any], bool]] = {
        # name: (reservation metadata, started (None: no delivery), later metadata, polled)
        "submitted_missing": ({}, True, {}, True),
        "submitted_refused": ({}, True, refused, True),
        "start_unseen_missing": ({}, False, {}, True),
        "start_unseen_refused": ({}, False, refused, True),
        "ended_missing": ({}, True, ended, True),
        "ended_refused": ({}, True, ended | refused, False),
        "start_unseen_ended_refused": ({}, False, ended | refused, False),
        "start_unseen_ended_missing": ({}, False, ended, True),
        "not_delivered": ({}, None, {}, False),
        "invalidated": ({}, True, {"identity_invalidated": True}, False),
        "machine_not_listed": ({"result_machine": "other-host"}, True, {}, False),
        "no_token": ({"result_token": None}, True, {}, False),
        "local": ({"local": True}, True, {}, False),
    }
    ids: dict[str, str] = {}
    for index, (name, (changes, started, later, _)) in enumerate(cases.items()):
        local = changes.pop("local", False)
        target = f"t{70 + index}" if local else f"{MACHINE}/t{70 + index}"
        metadata: dict[str, Any] = {"kind": "hc", "target": target, "completion": "worker_report"}
        if not local:
            metadata |= {
                "completion": "result_file",
                "result_machine": MACHINE,
                "result_token": f"{index:032x}",
            }
        metadata |= changes
        if metadata.get("result_token") is None:
            metadata.pop("result_token", None)
        task, _ = store.reserve(f"hc:default:{target}", "Review", f"agree-{index}", metadata)
        if started is not None:
            store.confirm_core_delivery(
                task["id"], {"turn": 1, "started": started, "status": "working"}
            )
        if later:
            store.metadata(task["id"], later)
        if later.get("terminal_ended"):
            store.update(task["id"], "uncertain", detail="terminal_ended_no_result: fixture")
        ids[name] = task["id"]
    polled = set(adapter.poll_ids(100))
    for name, (_, _, _, expected) in cases.items():
        task = store.get(ids[name])
        eligible = adapter._unreadable(task, person=False) is None
        assert (ids[name] in polled, eligible) == (expected, expected), name
    assert fake.calls() == []
