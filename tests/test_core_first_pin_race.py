"""T-226 review round 2 F4: two first deliveries to one machine cannot both pin.

Probe `probe_concurrent_pin`: two first deliveries to `node-two/t15` and
`/t16` were admitted with different core ids (`a*32`, then `b*32`). Per-target
admission allows both, the delivery semaphore allows two at once, and
`pin_machine()` returned the first pin while `_deliver` confirmed the second.

The fix: a first delivery to a machine (no pin yet) holds a per-machine lock
across its final pin check, send and pin, so the second waits, sees the pin and
fails unsent. And `_deliver` compares the id `pin_machine()` returns with the
id it admitted: on a mismatch (a pin from another bridge process) the receipt is
`uncertain` with `identity_invalidated`, never confirmed.

A stub core stands in for `Core`: it holds a send open until released, so the
interleaving is scheduled by the test, not by subprocess timing.
"""

import asyncio
from pathlib import Path
from typing import Any

from answerback_bridge.providers.herdr.core import NOT_SENT_MACHINE_CHANGED
from answerback_bridge.providers.herdr.core_tasks import PIN_RACED, CoreTasks
from answerback_bridge.tasks import TaskStore

MACHINE = "node-two"
FIRST, SECOND = f"{MACHINE}/t15", f"{MACHINE}/t16"
A, B = "a" * 32, "b" * 32


class StubCore:
    """Answers `peer`, `machine_id` and `send` as `CoreTasks` uses them."""

    session = None

    def __init__(self, ident: str) -> None:
        self.ident = ident
        self.sends: list[tuple[str, str | None]] = []
        self.release: dict[str, asyncio.Event] = {}
        self.during_send: dict[str, Any] = {}

    async def peer(self, target: str) -> dict[str, Any]:
        return {"binding": target, "send_ready": True, "project": "/home/user/code"}

    async def machine_id(self, machine: str) -> str:
        return self.ident

    async def send(
        self, target: str, envelope: str, *, expected: Any, machine_id: str | None = None
    ) -> dict[str, Any]:
        self.sends.append((target, machine_id))
        action = self.during_send.get(target)
        if action is not None:
            action()
        gate = self.release.get(target)
        if gate is not None:
            await gate.wait()
        return {
            "target": target,
            "turn": 1,
            "started": True,
            "status": "working",
            "identity_guard": "observed_before_and_after",
            "identity_observer": f"core:{MACHINE}",
        }


async def settled(tasks: CoreTasks) -> None:
    async with asyncio.timeout(5):
        await asyncio.gather(*tasks.jobs.values())


async def test_concurrent_first_deliveries_pin_once_and_send_once(tmp_path: Path) -> None:
    """Probe `probe_concurrent_pin`: the second first delivery must not be typed."""
    store = TaskStore(tmp_path / "tasks.sqlite")
    tasks = CoreTasks(store, tmp_path / "config.json")
    core = StubCore(A)
    core.release[FIRST] = asyncio.Event()
    first = await tasks.submit(core, FIRST, "Review", "race-1")
    core.ident = B  # The name changes core between the two admissions.
    second = await tasks.submit(core, SECOND, "Review", "race-2")
    assert store.get(first["id"])["metadata"]["machine_id"] == A
    assert store.get(second["id"])["metadata"]["machine_id"] == B
    # Let both delivery jobs run as far as they can while the first send is open.
    for _ in range(20):
        await asyncio.sleep(0)
    assert core.sends == [(FIRST, A)]
    core.release[FIRST].set()
    await settled(tasks)
    assert store.get(first["id"])["status"] == "submitted"
    late = store.get(second["id"])
    assert late["status"] == "failed"
    assert late["detail"] == NOT_SENT_MACHINE_CHANGED
    assert core.sends == [(FIRST, A)]
    assert store.machine_pin(MACHINE) == A


async def test_a_pin_from_elsewhere_during_the_send_is_never_confirmed(tmp_path: Path) -> None:
    """Defence beyond the in-process lock: `pin_machine()`'s answer is checked.

    Another bridge process (out of scope for the lock) pins another id while this
    delivery's send is in flight. The text was typed, so the receipt is uncertain
    with its identity invalidated, not submitted.
    """
    store = TaskStore(tmp_path / "tasks.sqlite")
    tasks = CoreTasks(store, tmp_path / "config.json")
    core = StubCore(B)
    core.during_send[SECOND] = lambda: store.pin_machine(MACHINE, A)
    task = await tasks.submit(core, SECOND, "Review", "race-other-process")
    await settled(tasks)
    result = store.get(task["id"])
    assert core.sends == [(SECOND, B)]
    assert result["status"] == "uncertain"
    assert result["metadata"].get("identity_invalidated") is True
    assert result["detail"] == PIN_RACED
    assert store.machine_pin(MACHINE) == A


async def test_first_deliveries_to_different_machines_do_not_wait_on_each_other(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    tasks = CoreTasks(store, tmp_path / "config.json")
    core = StubCore(A)
    core.release[FIRST] = asyncio.Event()
    await tasks.submit(core, FIRST, "Review", "machines-1")
    await tasks.submit(core, "other-host/t2", "Review", "machines-2")
    for _ in range(20):
        await asyncio.sleep(0)
    assert sorted(target for target, _ in core.sends) == sorted([FIRST, "other-host/t2"])
    core.release[FIRST].set()
    await settled(tasks)


async def test_pinned_machine_deliveries_still_run_two_at_once(tmp_path: Path) -> None:
    """The lock is for a first delivery only; a pinned machine keeps two slots."""
    store = TaskStore(tmp_path / "tasks.sqlite")
    store.pin_machine(MACHINE, A)
    tasks = CoreTasks(store, tmp_path / "config.json")
    core = StubCore(A)
    core.release[FIRST] = asyncio.Event()
    await tasks.submit(core, FIRST, "Review", "pinned-1")
    await tasks.submit(core, SECOND, "Review", "pinned-2")
    for _ in range(20):
        await asyncio.sleep(0)
    assert sorted(target for target, _ in core.sends) == [FIRST, SECOND]
    core.release[FIRST].set()
    await settled(tasks)
