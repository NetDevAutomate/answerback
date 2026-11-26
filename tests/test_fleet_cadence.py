"""fleet: modes, intervals, the emergency pause, the write gate, ticks and clock jumps."""

from dataclasses import replace

import pytest

from answerback_bridge.fleet import cadence
from answerback_bridge.placement.assess import assess
from answerback_bridge.placement.probe import Reading

GIB = 1024 * 1024
MAC = Reading(
    "local",
    100,
    os="Darwin",
    cores=10,
    load1=1,
    load5=1,
    load15=1,
    mem_total_kb=32 * GIB,
    pressure_level=1,
    compressor_kb=0,
    swap_used_kb=0,
    largest_process_kb=0,
)
LINUX = Reading(
    "local",
    100,
    os="Linux",
    cores=10,
    load1=1,
    load5=1,
    load15=1,
    mem_total_kb=32 * GIB,
    mem_available_kb=24 * GIB,
    swap_total_kb=8 * GIB,
    swap_free_kb=8 * GIB,
    largest_process_kb=0,
)


def _choose(reading: Reading) -> cadence.Cadence:
    return cadence.choose(reading, assess(reading, now=reading.taken_at))


def test_cadence_15s_normal_30s_memory_busy_45s_memory_full() -> None:
    assert (_choose(MAC).mode, _choose(MAC).interval_s) == ("normal", 15)
    busy = _choose(replace(MAC, compressor_kb=int(0.30 * 32 * GIB)))
    assert (busy.mode, busy.interval_s, busy.pause) == ("memory_busy", 30, None)
    full = _choose(replace(MAC, compressor_kb=int(0.45 * 32 * GIB)))
    assert (full.mode, full.interval_s, full.pause) == ("memory_full", 45, None)


def test_cadence_council_case_compressor_52_level_2_swap_90_probes_every_45s() -> None:
    # The council's case: compressor at 52%, pressure level 2, swap at 90%.
    mac = replace(
        MAC, compressor_kb=int(0.52 * 32 * GIB), pressure_level=2, swap_used_kb=int(0.9 * 28 * GIB)
    )
    result = _choose(mac)
    assert (result.mode, result.interval_s, result.pause) == ("memory_full", 45, None)
    linux = replace(LINUX, mem_available_kb=int(0.05 * 32 * GIB), swap_free_kb=int(0.1 * 8 * GIB))
    result = _choose(linux)
    assert (result.mode, result.interval_s, result.pause) == ("memory_full", 45, None)


def test_cadence_pressure_level_4_on_macos_pauses_worker_probes() -> None:
    result = _choose(replace(MAC, pressure_level=4))
    assert (result.mode, result.interval_s) == ("paused", 45)
    assert result.pause is not None and result.pause.reason == "pressure_critical"


def test_cadence_swap_95_percent_on_linux_pauses_worker_probes() -> None:
    result = _choose(replace(LINUX, swap_free_kb=int(0.05 * 8 * GIB)))
    assert result.mode == "paused"
    assert result.pause is not None and result.pause.reason == "swap_exhausted"
    assert "95%" in result.pause.text


def test_cadence_swap_share_is_not_a_stop_on_macos_nor_a_zero_total_on_linux() -> None:
    assert _choose(replace(MAC, swap_used_kb=31 * GIB)).pause is None
    assert _choose(replace(LINUX, swap_total_kb=0, swap_free_kb=0)).pause is None


def test_cadence_high_cpu_load_alone_never_pauses() -> None:
    result = _choose(replace(MAC, load1=500, load5=500))
    assert (result.mode, result.interval_s, result.pause) == ("normal", 15, None)


def test_cadence_without_a_current_coordinator_reading_is_unmeasured_at_45s() -> None:
    result = cadence.choose(None, None)
    assert (result.mode, result.interval_s, result.pause) == ("unmeasured", 45, None)


@pytest.mark.parametrize(
    ("changed", "since", "write"),
    [
        (True, None, True),
        (False, None, True),
        (True, 4.99, False),
        (True, 5.0, True),
        (False, 29.9, False),
        (False, 30.0, True),
    ],
)
def test_write_gate_least_gap_5s_heartbeat_30s_unchanged_waits(
    changed: bool, since: float | None, write: bool
) -> None:
    assert cadence.should_write(changed, since) is write


def test_next_wake_is_the_earliest_of_tick_heartbeat_and_pending_change() -> None:
    assert cadence.next_wake(tick=15, last_write=0, pending=False) == 15
    assert cadence.next_wake(tick=45, last_write=0, pending=False) == 30
    assert cadence.next_wake(tick=45, last_write=0, pending=True) == 5
    assert cadence.next_wake(tick=45, last_write=None, pending=True) == 45


def test_a_tick_during_a_cycle_is_skipped_and_not_caught_up() -> None:
    assert cadence.next_tick(0, 15, 3) == (15, 0)
    assert cadence.next_tick(0, 15, 15) == (15, 0)
    assert cadence.next_tick(0, 15, 16) == (30, 1)
    assert cadence.next_tick(0, 15, 50) == (60, 3)


def test_clock_jump_of_more_than_5s_is_seen() -> None:
    assert cadence.clock_jump(None, None, 100, 50) is None
    assert cadence.clock_jump(100, 50, 115, 65) is None
    assert cadence.clock_jump(100, 50, 120, 65) is None
    assert cadence.clock_jump(100, 50, 120.1, 65) == pytest.approx(5.1)
    assert cadence.clock_jump(100, 50, 100, 65) == pytest.approx(-15)


def test_write_gate_holds_5s_between_attempts_also_after_a_failure() -> None:
    assert cadence.should_write(True, 40.0, 4.9) is False
    assert cadence.should_write(False, 40.0, 4.9) is False
    assert cadence.should_write(True, 40.0, 5.0) is True


def test_next_wake_after_a_failed_attempt_is_5s_later_or_the_tick() -> None:
    # Last good write at 0, failed attempt at 15: the retry is at 20, not at once.
    assert cadence.next_wake(tick=30, last_write=0, pending=True, last_attempt=15) == 20
    assert cadence.next_wake(tick=18, last_write=0, pending=True, last_attempt=15) == 18
    # The heartbeat is also held back by the gap after an attempt.
    assert cadence.next_wake(tick=60, last_write=0, pending=False, last_attempt=28) == 33
    assert cadence.next_wake(tick=60, last_write=None, pending=True, last_attempt=1) == 6
