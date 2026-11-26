"""Provider/OS boundaries are fakes: these tests never inspect a real worker."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from events_helpers import PEER, peer
from test_core import terminal

import answerback_bridge
from answerback_bridge.adapters.builtins import Owners, builtin_plugins
from answerback_bridge.events.collector import bridge_sources
from answerback_bridge.events.resources import ProcessSampler, crew_tokens
from answerback_bridge.events.tracing import TraceSettings
from answerback_bridge.providers.herdr.core import Core


async def test_core_trace_pid_requires_same_binding_local_live_foreground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = terminal(status={"app": "claude", "state": "working"})

    async def command(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(stdout=json.dumps(row))

    monkeypatch.delattr(answerback_bridge, "core", raising=False)
    monkeypatch.setattr("answerback_bridge.providers.herdr.core.run", command)
    core = Core()
    selected = await core.peer("t1")
    assert "foreground_pid" not in selected
    assert await core.trace_pid("t1", selected["binding"]) == 101
    row["foreground"]["pid"] = 102
    assert await core.trace_pid("t1", selected["binding"]) is None
    row["address"] = "remote/t1"
    selected = await core.peer("remote/t1")
    assert await core.trace_pid("remote/t1", selected["binding"]) is None


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (None, None),
        ({}, None),
        ({"total_tokens": True}, None),
        ({"total_tokens": 12}, 12),
        ({"input_tokens": 20, "output_tokens": 5}, 25),
        ([{"total_tokens": 12}, {"total_tokens": 13}], 25),
        ([{"total_tokens": 12}, {}], None),
    ],
)
def test_optional_crew_tokens_accept_only_explicit_complete_counts(
    stats: Any, expected: Any
) -> None:
    assert crew_tokens(stats) == expected


async def test_live_source_uses_gateway_metadata_without_guessing_a_gateway_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[set[int]] = []
    details = {
        "messages": [{"content": "work"}],
        "turn_stats": {"input_tokens": 10, "output_tokens": 5},
        "process": {"pid": 40},
    }

    async def crew_read(target: str) -> dict[str, Any]:
        return details

    async def trace_pid(target: str, binding: str) -> int:
        return 10

    async def sample(self: ProcessSampler, roots: set[int]) -> dict[int, Any]:
        calls.append(roots)
        return {p: {"cpuPercent": 90, "rssBytes": 1024, "children": 1} for p in roots}

    monkeypatch.setattr(ProcessSampler, "sample", sample)
    bridge = SimpleNamespace(
        config=SimpleNamespace(provider_enabled=lambda p: True),
        core=SimpleNamespace(trace_pid=trace_pid),
        crew_read=crew_read,
        event_settings=SimpleNamespace(tracing=TraceSettings()),
    )
    bridge.plugins = builtin_plugins(Owners.of(bridge))
    sources = bridge_sources(bridge)
    metrics = await sources.resources([peer(binding="same"), peer(ident="crew:slot")])
    assert calls == [{10, 40}]
    assert metrics[PEER]["tokens"] is None
    assert metrics["crew:slot"]["tokens"] == 15
    assert await sources.tail("crew:slot", {}) == "work"
    details.pop("process")
    details.pop("turn_stats")
    metrics = await sources.resources([peer(ident="crew:slot")])
    assert calls[-1] == set()
    assert metrics["crew:slot"] == {
        "tokens": None,
        "cpuPercent": None,
        "rssBytes": None,
        "children": None,
    }


async def test_busy_child_drives_hot_event_through_collector() -> None:
    from events_helpers import Script
    from test_tracing import Clock, anomalies

    from answerback_bridge.events.collector import Collector
    from answerback_bridge.events.hub import EventHub

    clock = Clock()
    used = 0

    async def read() -> str:
        return (
            "10 1 00:00.00 100 Tue Nov 17 00:00:00 2026\n"
            f"11 10 {used // 60:02}:{used % 60:05.2f} 200 Tue Nov 17 00:00:00 2026\n"
        )

    sampler = ProcessSampler(read, clock=clock)
    script = Script()
    script.scan = {"peers": [peer("idle")]}
    sources = script.sources()

    async def resources(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {PEER: (await sampler.sample({10}))[10]}

    sources.resources = resources
    observer = Collector(EventHub(), sources, clock=clock)
    for index in range(22):
        await observer.tick()
        if index < 21:
            assert not anomalies(observer.hub)
        used += 15
        clock.value += 15
    (event,) = anomalies(observer.hub)
    assert event["reason"] == "hot" and event["observed"] == 100
    assert observer.hub.state["peers"][PEER]["metrics"]["children"] == 1


async def test_slow_ps_read_is_single_flight_late_data_is_discarded() -> None:
    import asyncio

    release = asyncio.Event()
    calls = 0

    async def read() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "10 1 00:00.00 100 Tue Nov 17 00:00:00 2026\n"

    sampler = ProcessSampler(read, timeout=0.01)
    assert await sampler.sample({10}) == {}
    assert await sampler.sample({10}) == {} and calls == 1
    release.set()
    await asyncio.sleep(0)
    result = await sampler.sample({10})
    assert calls == 2 and result[10]["cpuPercent"] is None


def test_bad_tracing_config_falls_back_without_stopping_bridge(tmp_path: Any) -> None:
    from answerback_bridge.config import Config
    from answerback_bridge.service import Bridge

    (tmp_path / "events.json").write_text('{"tracing":{"unknown_guard":1}}')
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    assert bridge.event_settings_error == "ValueError"
    assert bridge.event_settings.tracing.stuck_seconds == 600


def test_decision_reader_exposes_human_answer_metadata(tmp_path: Any) -> None:
    from datetime import UTC, datetime

    from answerback_bridge.events.producers import read_decisions

    folder = tmp_path / "status" / "decisions"
    folder.mkdir(parents=True)
    (folder / "D-1.md").write_text(
        "---\nid: D-1\ntask: T-17\nclass: reserved\nstatus: answered\n---\n"
        "# Resume?\n\n## Answer\nanswer: Resume\nanswered_by: owner\n"
        "answered_at: 2026-11-17T12:00:00Z\nrecommendation_only: false\n"
    )
    decision = read_decisions(tmp_path, datetime(2026, 11, 17, tzinfo=UTC))["D-1"]
    assert decision["answeredBy"] == "owner" and decision["recommendationOnly"] is False
    assert decision["answeredMs"] == 1794916800000


async def test_crew_optional_telemetry_never_exposes_arbitrary_gateway_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from answerback_bridge.crew import Crew

    async def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "key": "slot",
            "running": False,
            "messages": [],
            "queue": [],
            "total": 0,
            "has_more": False,
            "process": {"pid": 40, "environment": {"private": "not-for-the-stream"}},
            "turn_stats": {"total_tokens": 12, "private_provider_detail": "opaque"},
        }

    crew = Crew()
    monkeypatch.setattr(crew, "_request", request)
    detail = await crew.detail("slot")
    assert detail["process"] == {"pid": 40}
    assert detail["turn_stats"] == {"total_tokens": 12}
