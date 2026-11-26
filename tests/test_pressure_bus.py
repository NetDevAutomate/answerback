"""T-227 gate: a held bus command is accepted `queued` and published after `not_before`."""

import asyncio
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from pressure_fakes import WALL, FakePressure
from test_bus_api import configured
from test_bus_store import submit

from answerback_bridge.bus.api import BusAPI
from answerback_bridge.bus.store import BusStore
from answerback_bridge.config import Config
from answerback_bridge.runtime import Service
from answerback_bridge.service import Bridge
from answerback_bridge.transport import RemoteBridge


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def commands(store: BusStore) -> list[str]:
    return [row["command_id"] for row in store.pending_outbox(kind="command")]


def test_a_held_command_is_queued_and_published_only_after_not_before(tmp_path: Path) -> None:
    clock = Clock()
    store = BusStore(tmp_path / "ledger.sqlite", clock=clock)
    held = submit(store, "held", not_before=clock.now + 60)
    later = submit(store, "later")
    assert held["delivery"] == "queued"
    assert held["not_before"] == 160.0
    assert later["not_before"] is None
    assert store.get(held["command_id"])["not_before"] == 160.0
    # The later, unheld command is not blocked behind the held one.
    assert commands(store) == [later["command_id"]]
    clock.now = 159.9
    assert commands(store) == [later["command_id"]]
    # From `not_before` on, the held command is published in its original order.
    clock.now = 160.0
    assert commands(store) == [held["command_id"], later["command_id"]]
    accepted = next(
        event
        for event in store.events()["events"]
        if event["command_id"] == held["command_id"] and event["type"] == "command.accepted"
    )
    assert accepted["payload"] == {"delivery": "queued", "not_before": 160.0}
    plain = next(
        event
        for event in store.events()["events"]
        if event["command_id"] == later["command_id"] and event["type"] == "command.accepted"
    )
    assert plain["payload"] == {"delivery": "queued"}


def test_a_repeat_returns_the_first_hold_and_cancel_removes_it(tmp_path: Path) -> None:
    clock = Clock()
    store = BusStore(tmp_path / "ledger.sqlite", clock=clock)
    first = submit(store, "held", not_before=clock.now + 60)
    assert submit(store, "held", not_before=clock.now + 600) == first
    assert store.cancel(first["command_id"])["delivery"] == "cancelled"
    assert store.get(first["command_id"])["not_before"] is None
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM outbox_holds").fetchone()[0] == 0


def test_the_outbox_keeps_its_six_columns(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "ledger.sqlite", clock=Clock())
    submit(store, "held", not_before=500.0)
    with sqlite3.connect(store.path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(outbox)")]
        holds = [row[1] for row in db.execute("PRAGMA table_info(outbox_holds)")]
    assert columns == ["id", "kind", "command_id", "payload", "created", "published_at"]
    assert holds == ["id", "not_before", "reason"]


def test_an_existing_ledger_gains_the_companion_table_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    first = submit(BusStore(path, clock=Clock()), "old")
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER outbox_delete_holds")
        db.execute("DROP TABLE outbox_holds")
    reopened = BusStore(path, clock=Clock())
    assert reopened.get(first["command_id"])["not_before"] is None
    assert commands(reopened) == [first["command_id"]]


@asynccontextmanager
async def serving(path: Path) -> AsyncIterator[Service]:
    service = Service(path)
    service.path.parent.mkdir(mode=0o700, exist_ok=True)
    service.bridge = Bridge(Config.load(path), path)
    server = await asyncio.start_unix_server(service.handle, path=str(service.path))
    try:
        yield service
    finally:
        server.close()
        await server.wait_closed()
        await service.bridge.close()


async def test_bus_submit_under_pressure_returns_not_before_and_never_rejects() -> None:
    with tempfile.TemporaryDirectory(prefix="ab-bus-", dir="/tmp") as folder:
        root = await asyncio.to_thread(Path(folder).resolve)
        path = configured(root)
        async with serving(path) as service:
            service.bridge.pressure = FakePressure({("kiro", "default"): (2, 120.0)})
            command = await RemoteBridge(path).call(
                "bus_submit",
                {
                    "peer_id": "kiro:worker",
                    "project": str(root),
                    "message": "Review",
                    "request_id": "held-one",
                },
            )
            assert command["delivery"] == "queued"
            assert command["not_before"] == WALL + 120.0
            # A Bedrock peer is not held by Kiro pressure.
            service.bridge.pressure = FakePressure({("bedrock", "default"): (1, 60.0)})
            free = service.bridge.bus_api.submit(
                peer_id="kiro:worker", project=str(root), message="Other", request_id="free"
            )
            assert free["not_before"] is None


def test_hold_for_terminal_hosts_waits_behind_the_latest_pause(tmp_path: Path) -> None:
    store = FakePressure({("kiro", "default"): (1, 60.0), ("bedrock", "default"): (2, 240.0)})
    api = BusAPI(tmp_path / "config.json", pressure=lambda: store)
    hc = api.hold("hc:t5")
    assert hc is not None and hc.provider == "bedrock"
    kiro = api.hold("kiro:worker")
    assert kiro is not None and kiro.provider == "kiro"
    assert api.hold("fixtureprov:thread") is None
    assert BusAPI(tmp_path / "config.json").hold("kiro:worker") is None


def test_maintenance_still_knows_every_table_of_the_ledger(tmp_path: Path) -> None:
    from answerback_bridge.maintenance_policy import COLUMNS, KNOWN

    store = BusStore(tmp_path / "ledger.sqlite", clock=Clock())
    submit(store, "held", not_before=500.0)
    with sqlite3.connect(store.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        holds = {row[1] for row in db.execute("PRAGMA table_info(outbox_holds)")}
    # A table maintenance does not know marks the whole bus store unsupported.
    assert tables <= KNOWN["bus"]
    assert holds == COLUMNS["outbox_holds"]
