"""T-226 item 6: the `hc machine ls` id of a machine is pinned at its first send.

A machine name is only a name: a different core registered under it (a rebuilt
host, a re-added machine) would receive work meant for the old one. The bridge
stores the permanent core id at the first verified send and refuses a later send
to that name whose id differs, as `machine_id_changed`. A person clears the pin by
deleting its row (docs/HERDR-INTEGRATION.md, "Machine identity pin").

The fake `hc` is the one of `test_core_result_file`.
"""

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from test_core_result_file import (
    MACHINE,
    REMOTE,
    FakeCore,
    binding_of,
    delivered,
    make_bridge,
    make_fake,
)

from answerback_bridge.errors import public_bridge_error
from answerback_bridge.maintenance_policy import COLUMNS, KNOWN
from answerback_bridge.providers.herdr import core as core_module
from answerback_bridge.providers.herdr.core_tasks import MACHINE_CHANGED
from answerback_bridge.service import Bridge

FIRST = (MACHINE.encode().hex() + "0" * 32)[:32]
OTHER = "f" * 32


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> AsyncIterator[Bridge]:
    bridge = make_bridge(fake, tmp_path)
    yield bridge
    await bridge.close()


async def test_first_verified_send_pins_the_machine_id(bridge: Bridge) -> None:
    assert bridge.store.machine_pin(MACHINE) is None
    task = await delivered(bridge, REMOTE, "Review", "pin-first")
    assert task["status"] == "submitted"
    assert task["metadata"]["machine_id"] == FIRST
    assert bridge.store.machine_pin(MACHINE) == FIRST


async def test_local_send_pins_nothing(bridge: Bridge) -> None:
    await delivered(bridge, "t1", "Review", "pin-local")
    with sqlite3.connect(bridge.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM hc_machine_pins").fetchone() == (0,)


async def test_changed_id_is_refused_before_a_receipt(bridge: Bridge, fake: FakeCore) -> None:
    await delivered(bridge, REMOTE, "Review", "pin-1")
    fake.update(machine_ids={MACHINE: OTHER})
    sends = len(fake.sends())
    with pytest.raises(ValueError) as refused:
        await bridge.peer_submit(
            f"hc:{REMOTE}", "Review again", "pin-2", binding=binding_of(bridge, REMOTE)
        )
    assert str(refused.value) == MACHINE_CHANGED
    assert bridge.store.by_request("pin-2") is None
    assert len(fake.sends()) == sends
    assert bridge.store.machine_pin(MACHINE) == FIRST
    public = public_bridge_error(refused.value)
    assert public is not None and public.code == "machine_id_changed"


async def test_id_changed_after_reservation_fails_the_receipt_unsent(
    bridge: Bridge, fake: FakeCore
) -> None:
    """The id admitted at reservation must still be the id at the send."""
    fake.update(machine_ls_ok=1, machine_ids={MACHINE: OTHER})
    task = await delivered(bridge, REMOTE, "Review", "pin-swap")
    assert task["status"] == "failed"
    assert task["detail"] == core_module.NOT_SENT_MACHINE_CHANGED
    assert fake.sends() == []
    # Nothing was sent, so nothing is pinned.
    assert bridge.store.machine_pin(MACHINE) is None


async def test_id_flip_between_the_last_presend_read_and_hc_send_is_not_verified(
    bridge: Bridge, fake: FakeCore
) -> None:
    """Review F1, probe `id_flip_after_presend_check`.

    The name changes core after the last id read before `hc send` and before the
    send itself. `hc send` names only the machine, so the text was typed into the
    new core: the receipt must not read as verified and nothing may be pinned.
    """
    fake.update(on_send={"machine_ids": {MACHINE: OTHER}})
    task = await delivered(bridge, REMOTE, "Review", "pin-flip-at-send")
    assert len(fake.sends()) == 1
    assert task["status"] == "uncertain"
    assert task["metadata"].get("identity_invalidated") is True
    assert "machine id changed during send" in task["detail"]
    assert bridge.store.machine_pin(MACHINE) is None


async def test_id_change_after_the_last_presend_read_is_detected_not_prevented(
    bridge: Bridge, fake: FakeCore
) -> None:
    """Review round 2 F1-R, probe `probe_after_last_presend_read`: a documented residual.

    The id changes the moment the final pre-send `machine ls` has returned. Core has
    no send qualified by machine id, so the bridge cannot stop `hc send` typing into
    the new core (docs/HERDR-INTEGRATION.md, "Machine identity pin"). What it must
    do is detect it after the send: `uncertain`, `identity_invalidated`, no pin.
    """
    reads = 0
    machine_id = bridge.core.machine_id

    async def flips_after_the_presend_read(machine: str) -> str:
        nonlocal reads
        ident = await machine_id(machine)
        reads += 1
        if reads == 2:  # 1: submission, 2: the last read before `hc send`.
            fake.update(machine_ids={MACHINE: OTHER})
        return ident

    bridge.core.machine_id = flips_after_the_presend_read  # type: ignore[method-assign]
    task = await delivered(bridge, REMOTE, "Review", "pin-after-last-read")
    calls = fake.calls()
    sent = [i for i, args in enumerate(calls) if args[0] == "send"]
    assert len(sent) == 1
    assert calls[sent[0] - 1] == ["machine", "ls", "--json"]
    assert task["status"] == "uncertain"
    assert task["metadata"].get("identity_invalidated") is True
    assert bridge.store.machine_pin(MACHINE) is None


async def test_id_flip_after_hc_send_is_not_verified(bridge: Bridge, fake: FakeCore) -> None:
    """Review F1: the read after `hc send` must still see the admitted id."""
    # Reads 1 (submission) and 2 (last step before `hc send`) see the first id.
    fake.update(machine_ls_ok=2, machine_ids={MACHINE: OTHER})
    task = await delivered(bridge, REMOTE, "Review", "pin-flip-after-send")
    assert len(fake.sends()) == 1
    assert task["status"] == "uncertain"
    assert task["metadata"].get("identity_invalidated") is True
    assert bridge.store.machine_pin(MACHINE) is None


async def test_unreadable_id_after_hc_send_is_uncertain_not_failed(
    bridge: Bridge, fake: FakeCore
) -> None:
    """A read failure after the text was typed proves nothing was NOT sent."""
    fake.update(machine_ls_ok=2, machine_ls_error=True)
    task = await delivered(bridge, REMOTE, "Review", "pin-unread-after-send")
    assert len(fake.sends()) == 1
    assert task["status"] == "uncertain"
    assert bridge.store.machine_pin(MACHINE) is None


async def test_id_read_is_the_last_call_before_hc_send(bridge: Bridge, fake: FakeCore) -> None:
    """Review F1: no other await sits between the id check and `hc send`."""
    await delivered(bridge, REMOTE, "Review", "pin-order")
    calls = fake.calls()
    sent = [i for i, args in enumerate(calls) if args[0] == "send"]
    assert len(sent) == 1
    assert calls[sent[0] - 1] == ["machine", "ls", "--json"]
    assert calls[-1] == ["machine", "ls", "--json"]
    assert bridge.store.machine_pin(MACHINE) == FIRST


async def test_a_send_that_fails_before_typing_pins_nothing(bridge: Bridge, fake: FakeCore) -> None:
    fake.update(vanish={REMOTE: 2})
    task = await delivered(bridge, REMOTE, "Review", "pin-gone")
    assert task["status"] == "failed"
    assert bridge.store.machine_pin(MACHINE) is None


async def test_deleting_the_pin_row_lets_the_new_id_be_pinned(
    bridge: Bridge, fake: FakeCore
) -> None:
    await delivered(bridge, REMOTE, "Review", "pin-a")
    fake.update(machine_ids={MACHINE: OTHER})
    # The documented clearing step: delete the machine's row from the bridge store.
    with sqlite3.connect(bridge.store.path) as db:
        db.execute("DELETE FROM hc_machine_pins WHERE machine=?", (MACHINE,))
    # The terminal from the first send still has its receipt open; settle it so the
    # target admits a second one.
    first = bridge.store.by_request("pin-a")
    assert first is not None
    bridge.store.update(first["id"], "failed", detail="test settled")
    task = await delivered(bridge, REMOTE, "Review", "pin-b")
    assert task["status"] == "submitted"
    assert bridge.store.machine_pin(MACHINE) == OTHER


def test_pin_is_never_replaced_by_the_store(tmp_path: Path, fake: FakeCore) -> None:
    bridge = make_bridge(fake, tmp_path)
    assert bridge.store.pin_machine(MACHINE, FIRST) == FIRST
    assert bridge.store.pin_machine(MACHINE, OTHER) == FIRST
    assert bridge.store.machine_pin(MACHINE) == FIRST


def test_maintenance_knows_the_pin_table(tmp_path: Path, fake: FakeCore) -> None:
    """An unknown table would make maintenance treat the whole store as unsupported."""
    bridge = make_bridge(fake, tmp_path)
    assert "hc_machine_pins" in KNOWN["tasks"]
    with sqlite3.connect(bridge.store.path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(hc_machine_pins)")}
    assert columns == COLUMNS["hc_machine_pins"]
