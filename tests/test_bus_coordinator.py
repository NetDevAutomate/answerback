import json
from pathlib import Path

import pytest

from codex_herdr_bridge.adapters.contracts import Peer, TaskReceipt
from codex_herdr_bridge.bus.coordinator import Coordinator, PreparedTarget
from codex_herdr_bridge.bus.model import Command
from codex_herdr_bridge.bus.store import BusStore
from codex_herdr_bridge.tasks import TaskStore


class Crash(BaseException):
    pass


class Gateway:
    def __init__(self, root: Path) -> None:
        self.tasks = TaskStore(root / "tasks.sqlite")
        self.sends = 0
        self.crash = ""
        self.preflight_error = False

    async def prepare(self, command: Command) -> PreparedTarget:
        if self.preflight_error:
            raise ValueError("Project or routing changed")
        return PreparedTarget(
            "kiro:worker:/work",
            Peer.from_record({"peer_id": "kiro:worker", "provider": "kiro", "target": "worker"}),
        )

    async def send(self, target: PreparedTarget, command: Command) -> TaskReceipt:
        if self.crash == "before-reservation":
            raise Crash()
        task, created = self.tasks.reserve(
            target.key, command.message, command.adapter_request_id, {"kind": "kiro"}
        )
        if created:
            self.sends += 1
        if self.crash == "after-reservation":
            raise Crash()
        return TaskReceipt.from_record(self.tasks.update(task["id"], "running"))

    def lookup(self, command: Command) -> TaskReceipt | None:
        task = self.tasks.by_request(command.adapter_request_id)
        if task is not None and task["message"] != command.message:
            raise ValueError("Request belongs to different work")
        return TaskReceipt.from_record(task) if task else None

    def get(self, task_id: str) -> TaskReceipt:
        return TaskReceipt.from_record(self.tasks.get(task_id))


def admitted(store: BusStore) -> Command:
    row = store.submit(
        request_id="ping",
        peer_id="kiro:worker",
        project="/work",
        routing_digest="a" * 64,
        message="Ping",
    )
    return store.command(row["command_id"])


async def test_duplicate_delivery_binds_one_task_and_completion_is_separate(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite")
    gateway = Gateway(tmp_path)
    coordinator = Coordinator(store, gateway)
    command = admitted(store)
    await coordinator.process(command.to_bytes())
    await coordinator.process(command.to_bytes())
    row = store.get(command.command_id)
    assert row["delivery"] == "bound"
    assert gateway.sends == 1
    assert gateway.get(row["task_id"]).status == "running"
    gateway.tasks.update(row["task_id"], "completed", result="PONG")
    coordinator.project_receipts()
    assert store.events()["events"][-1]["type"] == "task.completed"


@pytest.mark.parametrize("point", ["before-reservation", "after-reservation"])
async def test_crash_recovery_reconciles_without_resending(tmp_path: Path, point: str) -> None:
    store = BusStore(tmp_path / "bus.sqlite")
    gateway = Gateway(tmp_path)
    gateway.crash = point
    coordinator = Coordinator(store, gateway)
    command = admitted(store)
    with pytest.raises(Crash):
        await coordinator.process(command.to_bytes())
    assert store.get(command.command_id)["delivery"] == "dispatching"
    gateway.tasks.recover_pending()
    recovered = Coordinator(store, gateway)
    recovered.recover_claims()
    await recovered.process(command.to_bytes())
    if point == "before-reservation":
        assert store.get(command.command_id)["delivery"] == "uncertain"
        assert gateway.sends == 0
    else:
        assert store.get(command.command_id)["delivery"] == "bound"
        assert gateway.sends == 1
        assert gateway.get(store.get(command.command_id)["task_id"]).status == "uncertain"


async def test_changed_project_is_rejected_before_admission(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite")
    gateway = Gateway(tmp_path)
    gateway.preflight_error = True
    command = admitted(store)
    await Coordinator(store, gateway).process(command.to_bytes())
    assert store.get(command.command_id)["delivery"] == "rejected"
    assert gateway.sends == 0


async def test_cancelled_command_and_tampered_wire_never_submit(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite")
    gateway = Gateway(tmp_path)
    command = admitted(store)
    store.cancel(command.command_id)
    coordinator = Coordinator(store, gateway)
    await coordinator.process(command.to_bytes())
    with pytest.raises(ValueError):
        await coordinator.process(command.to_bytes().replace(b"Ping", b"Different work"))
    assert gateway.sends == 0


async def test_recovery_refuses_same_request_key_for_a_different_target(tmp_path: Path) -> None:
    store = BusStore(tmp_path / "bus.sqlite")
    gateway = Gateway(tmp_path)
    gateway.crash = "before-reservation"
    command = admitted(store)
    coordinator = Coordinator(store, gateway)
    with pytest.raises(Crash):
        await coordinator.process(command.to_bytes())
    gateway.tasks.reserve("kiro:other:/elsewhere", "Ping", command.adapter_request_id)
    coordinator.recover_claims()
    assert store.get(command.command_id)["delivery"] == "uncertain"
    assert store.get(command.command_id)["task_id"] is None


async def test_runtime_error_is_uncertain_without_claiming_coordinator_stopped(tmp_path: Path):
    store = BusStore(tmp_path / "bus.sqlite")
    gateway = Gateway(tmp_path)

    async def raises(*args):
        raise ValueError("Unclassified external failure")

    gateway.send = raises
    command = admitted(store)
    await Coordinator(store, gateway).process(command.to_bytes())
    result = store.get(command.command_id)
    assert result["delivery"] == "uncertain"
    assert "ValueError" in result["detail"]
    assert "Coordinator stopped" not in result["detail"]


async def test_coordinator_refreshes_quarantine_counts_after_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: retained failure counts stay visible when publication resumes."""
    import asyncio
    from types import SimpleNamespace

    from answerback_bridge.bus import coordinator
    from answerback_bridge.bus.config import BusConfig

    store = BusStore(tmp_path / "ledger.sqlite")
    admitted(store)
    row = store.pending_outbox(kind="event")[0]
    stopping = asyncio.Event()

    class Bus:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def fetch(self, **kwargs: object) -> None:
            return None

        async def flush_outbox(self, store: BusStore) -> dict[str, str]:
            stopping.set()
            for _ in range(20):
                store.record_outbox_failure(row["id"], "APIError")
            return {}

    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: Bus)
    owner = Coordinator(store, SimpleNamespace())
    await owner.run(BusConfig(tmp_path / "config.json", 4222, "/unused"), stopping)
    assert owner.state["outbox_errors"] == {}
    assert owner.state["outbox_quarantined"] == {"command": 0, "event": 1}


async def test_startup_recovery_error_is_recorded_before_reraising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_review_tip: failed claim recovery reports only its exception type."""
    import asyncio

    from answerback_bridge.bus import coordinator
    from answerback_bridge.bus.config import BusConfig

    store = BusStore(tmp_path / "ledger.sqlite")
    command = admitted(store)
    assert store.claim(command.command_id)
    gateway = Gateway(tmp_path)
    owner = Coordinator(store, gateway)
    owner.state.update(connected=True, last_error="previous failure")
    error = OSError("private fixture receipt detail")
    lookups = []

    def unreadable_receipt(command: Command) -> TaskReceipt | None:
        lookups.append(command.command_id)
        raise error

    class Bus:
        def __init__(self, config: BusConfig) -> None:
            raise AssertionError("Failed recovery must not open a transport")

    monkeypatch.setattr(gateway, "lookup", unreadable_receipt)
    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: Bus)
    with pytest.raises(OSError) as raised:
        await owner.run(BusConfig(tmp_path / "config.json", 4222, "/unused"), asyncio.Event())
    assert raised.value is error
    assert owner.state["connected"] is False
    assert owner.state["last_error"] == "OSError"
    assert lookups == [command.command_id]
    assert store.get(command.command_id)["delivery"] == "dispatching"
    assert gateway.sends == 0


async def test_offline_broker_reason_reaches_bridge_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge health explains an offline supervised broker, not a null error."""
    import asyncio
    from types import SimpleNamespace

    import nats.errors

    from answerback_bridge.bus import broker, coordinator
    from answerback_bridge.bus.config import BusConfig

    stopping = asyncio.Event()

    class OfflineBus:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def __aenter__(self) -> "OfflineBus":
            stopping.set()
            raise nats.errors.NoServersError()

        async def __aexit__(self, *args: object) -> None:
            pass

    class DownBroker:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def failure_reason(self) -> str:
            return "broker not running (launchd: exited 0, not restarted)"

    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: OfflineBus)
    monkeypatch.setattr(broker, "Broker", DownBroker)
    owner = Coordinator(BusStore(tmp_path / "ledger.sqlite"), SimpleNamespace())
    await owner.run(BusConfig(tmp_path / "config.json", 4222, "/invented/nats"), stopping)
    assert owner.state["connected"] is False
    assert owner.state["last_error"] == "broker not running (launchd: exited 0, not restarted)"


async def test_connected_nats_timeout_does_not_probe_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace

    import nats.errors

    from answerback_bridge.bus import broker, coordinator
    from answerback_bridge.bus.config import BusConfig

    stopping = asyncio.Event()

    class TimeoutBus:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def __aenter__(self) -> "TimeoutBus":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def fetch(self, **kwargs: object) -> None:
            stopping.set()
            raise nats.errors.TimeoutError()

    class UnexpectedBroker:
        def __init__(self, config: BusConfig) -> None:
            raise AssertionError("a connected NATS timeout must not inspect the supervisor")

    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: TimeoutBus)
    monkeypatch.setattr(broker, "Broker", UnexpectedBroker)
    owner = Coordinator(BusStore(tmp_path / "ledger.sqlite"), SimpleNamespace())
    await owner.run(BusConfig(tmp_path / "config.json", 4222, "/nats"), stopping)
    assert owner.state["connected"] is False
    assert owner.state["last_error"] == "TimeoutError"


async def test_coordinator_keeps_transport_error_when_broker_diagnosis_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace

    import nats.errors

    from answerback_bridge.bus import broker, coordinator
    from answerback_bridge.bus.config import BusConfig

    stopping = asyncio.Event()

    class OfflineBus:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def __aenter__(self) -> "OfflineBus":
            stopping.set()
            raise nats.errors.NoServersError()

        async def __aexit__(self, *args: object) -> None:
            pass

    class UnreadableBroker:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def failure_reason(self) -> str:
            raise RuntimeError("fixture supervisor detail")

    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: OfflineBus)
    monkeypatch.setattr(broker, "Broker", UnreadableBroker)
    owner = Coordinator(BusStore(tmp_path / "ledger.sqlite"), SimpleNamespace())
    await owner.run(BusConfig(tmp_path / "config.json", 4222, "/nats"), stopping)
    assert owner.state["connected"] is False
    assert owner.state["last_error"] == "NoServersError"


async def test_coordinator_with_real_broker_handles_non_object_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace

    import nats.errors

    from answerback_bridge.bus import coordinator
    from answerback_bridge.bus.broker import Broker
    from answerback_bridge.bus.config import BusConfig

    stopping = asyncio.Event()
    config = BusConfig(tmp_path / "config.json", 4222, "/nats")
    real_broker = Broker(config)
    real_broker.supervisor.root.mkdir(parents=True)
    real_broker.supervisor.marker.write_text("[]")

    class OfflineBus:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def __aenter__(self) -> "OfflineBus":
            stopping.set()
            raise nats.errors.NoServersError()

        async def __aexit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: OfflineBus)
    owner = Coordinator(BusStore(tmp_path / "ledger.sqlite"), SimpleNamespace())
    await owner.run(config, stopping)
    assert owner.state["last_error"] == "broker not running (supervisor status unavailable)"


async def test_coordinator_refreshes_broker_reason_at_most_every_30_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from answerback_bridge.bus import broker, coordinator
    from answerback_bridge.bus.config import BusConfig

    now = [0.0]
    attempts = 0
    diagnoses = 0

    class Stopping:
        def is_set(self) -> bool:
            return attempts >= 4

        async def wait(self) -> None:
            return None

    class FailingBus:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def __aenter__(self) -> "FailingBus":
            nonlocal attempts
            attempts += 1
            now[0] = (0.0, 10.0, 29.0, 30.0)[attempts - 1]
            if attempts == 1:
                return self
            raise OSError("fixture broker outage")

        async def __aexit__(self, *args: object) -> None:
            pass

        async def fetch(self, **kwargs: object) -> None:
            raise OSError("fixture broker outage")

    class DownBroker:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def failure_reason(self) -> str:
            nonlocal diagnoses
            diagnoses += 1
            return f"broker reason {diagnoses}"

    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: FailingBus)
    monkeypatch.setattr(coordinator.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(broker, "Broker", DownBroker)
    owner = Coordinator(BusStore(tmp_path / "ledger.sqlite"), SimpleNamespace())
    await owner.run(BusConfig(tmp_path / "config.json", 4222, "/nats"), Stopping())
    assert diagnoses == 2
    assert owner.state["last_error"] == "broker reason 2"


async def test_coordinator_does_not_reuse_a_delivery_error_as_the_broker_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-check finding X3: inside the 30 s window only a reason this branch computed is
    reused; a non-transport error text left by delivery is replaced by the transport error's
    type name, never carried for up to 30 s as if it were the broker's state."""
    from types import SimpleNamespace

    from answerback_bridge.bus import broker, coordinator
    from answerback_bridge.bus.config import BusConfig

    attempts = 0

    class Stopping:
        def is_set(self) -> bool:
            return attempts >= 2

        async def wait(self) -> None:
            return None

    class FailingBus:
        def __init__(self, config: BusConfig) -> None:
            pass

        async def __aenter__(self) -> "FailingBus":
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("fixture delivery fault")
            raise OSError("fixture broker outage")

        async def __aexit__(self, *args: object) -> None:
            pass

    class NeverAskedBroker:
        def __init__(self, config: BusConfig) -> None:
            raise AssertionError("the broker must not be diagnosed in this test")

    monkeypatch.setattr(coordinator, "jetstream_bus_class", lambda: FailingBus)
    monkeypatch.setattr(coordinator.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(broker, "Broker", NeverAskedBroker)
    owner = Coordinator(BusStore(tmp_path / "ledger.sqlite"), SimpleNamespace())
    owner._failure_reason_at = 0.0  # a reason was computed moments ago, in the window
    owner.state.update(connected=False, last_error="ValueError")
    await owner.run(BusConfig(tmp_path / "config.json", 4222, "/nats"), Stopping())
    assert owner.state["last_error"] == "OSError"


async def test_run6_preflight_refusal_detail_keeps_the_kind_and_drops_the_text(
    tmp_path: Path,
) -> None:
    """red_at_base (run-6 hardening h4): bus_result/bus_events returned the raw message."""
    store = BusStore(tmp_path / "bus.sqlite")
    gateway = Gateway(tmp_path)
    gateway.preflight_error = True
    command = admitted(store)
    await Coordinator(store, gateway).process(command.to_bytes())
    row = store.get(command.command_id)
    assert row["delivery"] == "rejected"
    assert row["detail"] == "Admission preflight refused (ValueError)"
    events = [json.dumps(event) for event in store.events()["events"]]
    assert events and not any("Project or routing changed" in event for event in events)
