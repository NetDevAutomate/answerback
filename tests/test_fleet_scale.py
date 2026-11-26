"""fleet: the pressure scale agrees with the limits of placement/assess.py."""

from dataclasses import replace

import pytest

from answerback_bridge.fleet import scale
from answerback_bridge.placement.assess import Thresholds, assess
from answerback_bridge.placement.probe import Reading

T = Thresholds()
MAC = Reading(
    "node",
    100,
    os="Darwin",
    cores=10,
    load1=1,
    load5=1,
    load15=1,
    mem_total_kb=1_000_000,
    pressure_level=1,
    compressor_kb=0,
    swap_used_kb=0,
    largest_process_kb=0,
)
LINUX = Reading(
    "node",
    100,
    os="Linux",
    cores=10,
    load1=1,
    load5=1,
    load15=1,
    mem_total_kb=1_000_000,
    mem_available_kb=900_000,
    largest_process_kb=0,
)


def _signal(reading: Reading, name: str, **agents: int) -> tuple[str, float]:
    result = assess(reading, now=reading.taken_at, **agents)
    (signal,) = [s for s in result.signals if s.name == name]
    point = scale.point(signal.name, signal.state, reading, result.load_per_core, **agents)
    assert point is not None
    return signal.state, point.fraction


# (signal, reading at the busy limit, reading at the full limit or None)
LIMITS = [
    ("cpu", replace(MAC, load1=T.cpu_busy * 10), replace(MAC, load1=T.cpu_full * 10)),
    (
        "compressor",
        replace(MAC, compressor_kb=int(T.compressor_busy * 1_000_000)),
        replace(MAC, compressor_kb=int(T.compressor_full * 1_000_000)),
    ),
    ("swap", replace(MAC, swap_used_kb=int(T.swap_busy * 1_000_000)), None),
    (
        "largest_process",
        replace(MAC, largest_process_kb=int(T.largest_process_busy * 1_000_000)),
        replace(MAC, largest_process_kb=int(T.largest_process_full * 1_000_000)),
    ),
    ("pressure", replace(MAC, pressure_level=2), replace(MAC, pressure_level=4)),
    ("stall", replace(LINUX, psi_some_avg10=T.stall_busy), None),
]


@pytest.mark.parametrize(("name", "busy", "full"), LIMITS)
def test_scale_value_at_busy_limit_is_busy_and_070_at_full_limit_full_and_100(
    name: str, busy: Reading, full: Reading | None
) -> None:
    assert _signal(busy, name) == ("busy", pytest.approx(0.70))
    if full is not None:
        assert _signal(full, name) == ("full", pytest.approx(1.00))


def test_scale_linux_available_limits_are_exclusive_and_map_to_070_and_100() -> None:
    # assess.py: available < 15% is busy, < 7% is full. The first value past each limit
    # assesses busy or full and maps to 0.70 or 1.00 within a micro-step.
    busy = replace(LINUX, mem_available_kb=int(T.available_busy * 1_000_000) - 1)
    full = replace(LINUX, mem_available_kb=int(T.available_full * 1_000_000) - 1)
    assert _signal(busy, "available") == ("busy", pytest.approx(0.70, abs=1e-5))
    assert _signal(full, "available") == ("full", pytest.approx(1.00, abs=1e-5))


def test_scale_agents_full_at_ceiling_is_100() -> None:
    assert _signal(MAC, "agents", active_agents=6, agent_ceiling=6) == ("full", 1.0)


def test_scale_is_linear_between_limits_and_keeps_its_slope_beyond_full() -> None:
    mid = replace(MAC, compressor_kb=320_000)  # 32%: between 25% and 40%
    assert _signal(mid, "compressor") == ("busy", pytest.approx(0.84))
    beyond = replace(MAC, compressor_kb=520_000)  # 52%: 0.30 per 15 points past 40%
    assert _signal(beyond, "compressor") == ("full", pytest.approx(1.24))


def test_scale_never_above_2_and_a_signal_that_cannot_be_full_stops_at_099() -> None:
    assert _signal(replace(MAC, load1=100), "cpu")[1] == 2.0
    assert _signal(replace(MAC, swap_used_kb=990_000), "swap") == ("busy", 0.99)
    assert _signal(replace(LINUX, psi_some_avg10=99), "stall") == ("busy", 0.99)


def test_scale_signal_names_are_cpu_memory_or_agents() -> None:
    assert scale.published("cpu") == "cpu"
    assert scale.published("agents") == "agents"
    for name in ("pressure", "compressor", "swap", "available", "stall", "largest_process"):
        assert scale.published(name) == "memory"


def test_scale_texts_are_8_characters_at_most() -> None:
    point = scale.point("pressure", "full", replace(MAC, pressure_level=4), 0.1)
    assert point is not None and point.text == "level 4"
    point = scale.point("compressor", "busy", replace(MAC, compressor_kb=320_000), 0.1)
    assert point is not None and point.text == "32%"
    point = scale.point("cpu", "full", MAC, 1.1612)
    assert point is not None and point.text == "1.16"
    point = scale.point("agents", "full", MAC, 0.1, active_agents=6, agent_ceiling=6)
    assert point is not None and point.text == "6/6"
    point = scale.point("agents", "full", MAC, 0.1, active_agents=10**9, agent_ceiling=6)
    assert point is not None and len(point.text) <= 8
    assert all(len(p.sentence) <= 80 for p in [point])


def test_a_busy_signal_that_can_be_full_keeps_its_value_on_the_scale_below_100() -> None:
    assert _signal(replace(MAC, load1=9.95), "cpu") == ("busy", 0.995)
    for load in (9.99, 9.995, 9.9999, 9.99999):
        state, value = _signal(replace(MAC, load1=load), "cpu")
        assert state == "busy" and value <= 0.999, (load, value)
    compressor = replace(MAC, compressor_kb=399_999)
    assert _signal(compressor, "compressor") == ("busy", 0.999)
