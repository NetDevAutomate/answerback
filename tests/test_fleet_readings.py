"""fleet: one machine's entry, expiry, failed probes, links and clock jumps."""

from dataclasses import replace

from answerback_bridge.fleet import readings
from answerback_bridge.placement.probe import Reading, parse_reading

GOOD = (
    "os=Linux\ncores=10\nload=1 2 3\nmemtotal_kb=1000\nmemavailable_kb=900\n"
    "swaptotal_kb=100\nswapfree_kb=90\nlargest_kb=100\nlargest_name=python\nprocs=12\n"
    "suites=1\nprobe_end=1\n"
)
WALL = 1_800_000_000.0


def _reading(text: str = GOOD, at: float = WALL) -> Reading:
    return parse_reading("worker-a", text, at)


def _entry(memo: readings.Memo, now: float, link: str = "connected") -> dict:
    return readings.entry("worker-a", memo, link=link, now_mono=now, active=0, ceiling=None)


def test_a_fresh_reading_is_published_with_its_state_and_stamp() -> None:
    memo = readings.record(readings.Memo(), _reading(), WALL, 0.0, active=None, ceiling=None)
    e = _entry(memo, 10)
    assert e["reading"]["state"] == "ok" and e["reading"]["was"] is None
    assert e["reading"]["taken_at"] == WALL and e["reading"]["error"] is None
    assert e["reading"]["loads"] == [1.0, 2.0, 3.0] and e["reading"]["cores"] == 10
    assert e["reading"]["load_per_core"] == 0.2 and e["reading"]["suites"] == 1
    assert e["reading"]["pressure"] is None and e["reading"]["decided_by"] == []
    assert e["reading"]["memory"]["state"] == "ok"
    assert "swap 10%" in e["reading"]["memory"]["text"]
    assert e["os"] == "linux" and e["role"] == "worker" and e["self"] is False
    assert e["link"] == {"state": "connected", "since_ms": None, "why": None}


def test_a_reading_older_than_60s_is_unknown_with_was_and_keeps_taken_at() -> None:
    memo = readings.record(readings.Memo(), _reading(), WALL, 0.0, active=None, ceiling=None)
    assert _entry(memo, 60.0)["reading"]["state"] == "ok"
    e = _entry(memo, 60.01)
    assert e["reading"]["state"] == "unknown" and e["reading"]["was"] == "ok"
    assert e["reading"]["taken_at"] == WALL and e["reading"]["error"] == "reading expired"
    assert not readings.is_current(memo, 60.01)


def test_a_failed_probe_does_not_make_the_last_reading_younger() -> None:
    memo = readings.record(readings.Memo(), _reading(), WALL, 0.0, active=None, ceiling=None)
    failed = readings.record(
        memo,
        Reading("worker-a", WALL + 30, error="timeout"),
        WALL + 30,
        30.0,
        active=None,
        ceiling=None,
    )
    assert failed.last == memo.last
    e = _entry(failed, 31)
    assert e["reading"]["taken_at"] == WALL
    assert (e["reading"]["state"], e["reading"]["was"]) == ("unknown", "ok")
    assert e["reading"]["error"] == "probe timed out"
    assert not readings.is_current(failed, 31)
    # A later good probe is current again with its own stamp.
    again = readings.record(
        failed, _reading(at=WALL + 45), WALL + 45, 45.0, active=None, ceiling=None
    )
    assert _entry(again, 46)["reading"]["taken_at"] == WALL + 45


def test_a_probe_answer_that_cannot_be_understood_makes_that_machine_unknown_with_an_error() -> (
    None
):
    memo = readings.record(
        readings.Memo(), _reading("garbage\x1b]0;x\x07"), WALL, 0.0, active=None, ceiling=None
    )
    e = _entry(memo, 1)
    assert (e["reading"]["state"], e["reading"]["was"], e["reading"]["taken_at"]) == (
        "unknown",
        None,
        None,
    )
    assert e["reading"]["error"] == "probe answer not understood"


def test_a_machine_never_measured_is_unknown_with_why() -> None:
    e = _entry(readings.Memo(error="no link"), 0, link="unreachable")
    assert e["reading"]["state"] == "unknown" and e["reading"]["was"] is None
    assert e["reading"]["taken_at"] is None and e["reading"]["error"] == "no link"
    assert e["link"]["state"] == "unreachable"


def test_a_clock_jump_expires_every_reading() -> None:
    memo = readings.record(readings.Memo(), _reading(), WALL, 0.0, active=None, ceiling=None)
    jumped = readings.expire(memo)
    e = _entry(jumped, 1)
    assert (e["reading"]["state"], e["reading"]["was"]) == ("unknown", "ok")
    assert e["reading"]["taken_at"] == WALL
    assert not readings.is_current(jumped, 1)


def test_busy_reading_publishes_decided_by_and_pressure_as_memory() -> None:
    text = GOOD.replace("memavailable_kb=900", "memavailable_kb=100")
    memo = readings.record(readings.Memo(), _reading(text), WALL, 0.0, active=None, ceiling=None)
    r = _entry(memo, 1)["reading"]
    assert r["state"] == "busy"
    assert [d["signal"] for d in r["decided_by"]] == ["memory"]
    assert all(len(d["text"]) <= 80 for d in r["decided_by"])
    assert r["pressure"]["signal"] == "memory" and 0.70 <= r["pressure"]["fraction"] < 1


def test_agents_signal_only_with_a_ceiling_and_active_is_the_live_count() -> None:
    memo = readings.record(readings.Memo(), _reading(), WALL, 0.0, active=6, ceiling=6)
    e = readings.entry("worker-a", memo, link="connected", now_mono=1, active=6, ceiling=6)
    assert e["reading"]["state"] == "full"
    assert e["reading"]["pressure"] == {"signal": "agents", "fraction": 1.0, "text": "6/6"}
    assert e["reading"]["agents"] == {"active": 6, "ceiling": 6, "lanes": None}
    memo = readings.record(readings.Memo(), _reading(), WALL, 0.0, active=6, ceiling=None)
    e = readings.entry("worker-a", memo, link="connected", now_mono=1, active=6, ceiling=None)
    assert e["reading"]["state"] == "ok"
    assert e["reading"]["agents"] == {"active": 6, "ceiling": None, "lanes": None}


def test_coordinator_entry_is_self_with_null_name() -> None:
    reading = replace(_reading(), machine="local")
    memo = readings.record(readings.Memo(), reading, WALL, 0.0, active=None, ceiling=None)
    e = readings.entry(None, memo, link="self", now_mono=1, active=3, ceiling=None)
    assert (e["name"], e["self"], e["role"], e["link"]["state"]) == (
        None,
        True,
        "coordinator",
        "self",
    )
    assert e["reading"]["agents"] == {"active": 3, "ceiling": None, "lanes": None}
