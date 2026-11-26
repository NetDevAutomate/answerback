"""new_feature: measured capacity states, ranking and advisory transitions."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from answerback_bridge.placement.assess import Thresholds, advise, assess, rank
from answerback_bridge.placement.probe import Reading, parse_reading

FIXTURES = Path(__file__).parent / "fixtures/placement"
BASE = Reading(
    "node",
    100,
    os="Darwin",
    cores=10,
    load1=1,
    load5=1,
    load15=1,
    mem_total_kb=1000,
    pressure_level=1,
    compressor_kb=0,
    swap_used_kb=0,
    largest_process_kb=0,
)


@pytest.mark.parametrize(
    ("fixture", "state", "reasons"),
    [
        ("darwin-idle.txt", "ok", set()),
        ("darwin-compressor-warning.txt", "full", {"pressure", "compressor"}),
        ("darwin-swap-residue.txt", "full", {"cpu", "swap"}),
        ("linux-a.txt", "ok", set()),
        ("linux-b.txt", "ok", set()),
    ],
)
def test_new_feature_measured_states(fixture: str, state: str, reasons: set[str]) -> None:
    reading = parse_reading("node", (FIXTURES / fixture).read_text(), 100)
    result = assess(reading, now=100)
    assert result.state == state
    assert {signal.name for signal in result.signals if signal.state != "ok"} == reasons
    assert result.to_json()["reasons"] == [
        signal.text for signal in result.signals if signal.state != "ok"
    ]
    if fixture == "darwin-compressor-warning.txt":
        assert any("43.35%" in reason for reason in result.reasons)
    if fixture == "darwin-swap-residue.txt":
        result = assess(replace(reading, load1=4.8, load5=4.8, load15=4.8), now=100)
        assert result.state == "busy"
        assert [signal.name for signal in result.signals if signal.state == "busy"] == ["swap"]


@pytest.mark.parametrize(
    ("signal", "changes", "state"),
    [
        ("cpu", {"load1": 6.99}, "ok"),
        ("cpu", {"load1": 7}, "busy"),
        ("cpu", {"load5": 9.99}, "busy"),
        ("cpu", {"load5": 10}, "full"),
        ("pressure", {"pressure_level": 1}, "ok"),
        ("pressure", {"pressure_level": 2}, "busy"),
        ("pressure", {"pressure_level": 4}, "full"),
        ("pressure", {"pressure_level": 3}, "busy"),
        ("compressor", {"compressor_kb": 249}, "ok"),
        ("compressor", {"compressor_kb": 250}, "busy"),
        ("compressor", {"compressor_kb": 399}, "busy"),
        ("compressor", {"compressor_kb": 400}, "full"),
        ("swap", {"swap_used_kb": 249}, "ok"),
        ("swap", {"swap_used_kb": 250}, "busy"),
        ("swap", {"swap_used_kb": 1000}, "busy"),
        ("available", {"os": "Linux", "mem_available_kb": 151}, "ok"),
        ("available", {"os": "Linux", "mem_available_kb": 150}, "ok"),
        ("available", {"os": "Linux", "mem_available_kb": 149}, "busy"),
        ("available", {"os": "Linux", "mem_available_kb": 70}, "busy"),
        ("available", {"os": "Linux", "mem_available_kb": 69}, "full"),
        ("stall", {"os": "Linux", "mem_available_kb": 900, "psi_some_avg10": 9.99}, "ok"),
        ("stall", {"os": "Linux", "mem_available_kb": 900, "psi_some_avg10": 10}, "busy"),
        ("stall", {"os": "Linux", "mem_available_kb": 900, "psi_some_avg10": 100}, "busy"),
        ("largest_process", {"largest_process_kb": 249}, "ok"),
        ("largest_process", {"largest_process_kb": 250}, "busy"),
        ("largest_process", {"largest_process_kb": 499}, "busy"),
        ("largest_process", {"largest_process_kb": 500}, "full"),
    ],
)
def test_new_feature_signal_boundaries(signal: str, changes: dict, state: str) -> None:
    result = assess(replace(BASE, **changes), now=100)
    assert next(item.state for item in result.signals if item.name == signal) == state
    assert result.state == state
    assert all(any(char.isdigit() for char in item.text) for item in result.signals)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"error": "timeout"}, "timeout"),
        ({"error": "unreachable"}, "unreachable"),
        ({"error": "unparsed"}, "unparsed"),
        ({"taken_at": 39}, "stale"),
        ({"cores": None}, "CPU"),
        ({"load1": None}, "CPU"),
        ({"load5": None}, "CPU"),
        ({"load15": None}, "CPU"),
        (
            {
                "pressure_level": None,
                "compressor_kb": None,
                "swap_used_kb": None,
                "largest_process_kb": None,
            },
            "memory",
        ),
        (
            {"mem_total_kb": 0, "pressure_level": None},
            "memory",
        ),
    ],
)
def test_new_feature_unknown_overrides_full(changes: dict, reason: str) -> None:
    result = (
        assess(replace(BASE, load1=20, **changes), now=100)
        if "load1" not in changes
        else (assess(replace(BASE, load5=20, **changes), now=100))
    )
    assert result.state == "unknown"
    assert any(reason in text for text in result.reasons)


@pytest.mark.parametrize(
    ("reading", "state"),
    [
        (
            Reading(
                "node", 100, os="Darwin", cores=2, load1=0, load5=0, load15=0, pressure_level=1
            ),
            "ok",
        ),
        (
            Reading("node", 100, os="Linux", cores=2, load1=0, load5=0, load15=0, psi_some_avg10=0),
            "unknown",
        ),
        (
            Reading(
                "node",
                100,
                os="Linux",
                cores=2,
                load1=0,
                load5=0,
                load15=0,
                mem_total_kb=1000,
                largest_process_kb=10,
            ),
            "unknown",
        ),
    ],
)
def test_new_feature_one_memory_signal_is_enough(reading: Reading, state: str) -> None:
    """red_at_pretip: only the pressure case measures platform free memory."""
    assert assess(reading, now=100).state == state


@pytest.mark.parametrize("os_name", ["Darwin", "Linux", "FreeBSD", None])
@pytest.mark.parametrize("largest_kb", [0, 500])
def test_red_at_pretip_free_memory_is_required(os_name: str | None, largest_kb: int) -> None:
    # Direct Reading callers can supply a total/process/CPU without platform free memory.
    reading = replace(
        BASE,
        os=os_name,
        pressure_level=None,
        compressor_kb=None,
        swap_used_kb=1000,
        psi_some_avg10=100,
        largest_process_kb=largest_kb,
    )
    result = assess(reading, now=100)
    assert result.state == "unknown"
    assert next(item.state for item in result.signals if item.name == "largest_process") == (
        "full" if largest_kb == 500 else "ok"
    )
    if os_name in ("Darwin", "Linux"):
        name = "swap" if os_name == "Darwin" else "stall"
        assert next(item.state for item in result.signals if item.name == name) == "busy"


@pytest.mark.parametrize(
    "changes",
    [
        {"pressure_level": None},
        {"compressor_kb": None},
        {"os": "Linux", "mem_available_kb": 900},
    ],
)
def test_pins_existing_one_platform_free_memory_signal_suffices(changes: dict) -> None:
    """pins_existing: optional signals may be missing from a usable Reading."""
    assert assess(replace(BASE, swap_used_kb=None, **changes), now=100).state == "ok"


@pytest.mark.parametrize("os_name", ["FreeBSD", None])
def test_red_at_pretip_unsupported_os_with_memory_fields_is_unknown(os_name: str | None) -> None:
    assert assess(replace(BASE, os=os_name, mem_available_kb=900), now=100).state == "unknown"


@pytest.mark.parametrize(
    ("active", "ceiling", "state"),
    [(2, 3, "ok"), (3, 3, "full"), (4, 3, "full"), (None, 3, "ok"), (3, None, "ok")],
)
def test_new_feature_agents(active: int | None, ceiling: int | None, state: str) -> None:
    result = assess(BASE, now=100, active_agents=active, agent_ceiling=ceiling)
    assert result.state == state
    assert ("agents" in [item.name for item in result.signals]) == (
        active is not None and ceiling is not None
    )


def test_new_feature_thresholds_and_freshness_are_explicit() -> None:
    thresholds = Thresholds(cpu_busy=0.2, cpu_full=0.5, max_age=120)
    reading = replace(BASE, load5=5, taken_at=0)
    assert assess(reading, now=120, thresholds=thresholds).state == "full"
    assert assess(reading, now=121, thresholds=thresholds).state == "unknown"
    assert assess(replace(BASE, taken_at=40), now=100).state == "ok"
    assert assess(replace(BASE, load15=1000), now=100).load_per_core == 0.1
    with pytest.raises(FrozenInstanceError):
        thresholds.cpu_busy = 1  # type: ignore[misc]


def test_new_feature_rank_bands_order_and_coordinator() -> None:
    readings = [
        replace(BASE, machine="local", load1=0, load5=0),
        replace(BASE, machine="z", load1=0.1, load5=0.1),
        replace(BASE, machine="a", load1=2.4, load5=2.4),
        replace(BASE, machine="preferred", load1=2, load5=2),
        replace(BASE, machine="band2", load1=2.5, load5=2.5),
        replace(BASE, machine="busy", load1=7),
        replace(BASE, machine="unknown", error="timeout"),
    ]
    results = [assess(reading, now=100) for reading in readings]
    assert rank(results) == ["a", "preferred", "z", "band2"]
    assert rank(results, order=("band2", "preferred", "z")) == ["preferred", "z", "a", "band2"]
    assert rank(results, heavy=False) == ["a", "local", "preferred", "z", "band2"]
    assert rank(results, coordinator="a") == ["local", "preferred", "z", "band2"]
    assert rank(results[-2:]) == []


def test_new_feature_advice_levels_and_targets() -> None:
    results = [
        assess(replace(BASE, machine="local", load1=10), now=100),
        assess(replace(BASE, machine="full", compressor_kb=500), now=100),
        assess(replace(BASE, machine="busy", load1=7), now=100),
        assess(replace(BASE, machine="unknown", error="timeout"), now=100),
        assess(replace(BASE, machine="target"), now=100),
    ]
    advice = advise(results)
    assert [(item.machine, item.level) for item in advice] == [
        ("local", 3),
        ("full", 2),
        ("busy", 1),
        ("unknown", 0),
    ]
    assert all(item.to_json()["targets"] == ["target"] for item in advice)
    assert "hand-over note" in advice[0].text and "reports" in advice[0].text
    assert "next stage" in advice[1].text and "boundary" in advice[1].text
    assert "nothing heavy" in advice[2].text and "finish" in advice[2].text
    assert "until it can be measured" in advice[3].text
    repeated = advise(results, previous=results)
    assert next(item.level for item in repeated if item.machine == "busy") == 2
    assert advise([results[-1]]) == []
    assert advise([results[0]])[0].to_json()["targets"] == []
