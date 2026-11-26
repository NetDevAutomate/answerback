"""fleet: advice names only current ok workers without a recent launch, never the coordinator."""

from answerback_bridge.fleet import advice
from answerback_bridge.placement.assess import assess
from answerback_bridge.placement.probe import parse_reading

GOOD = (
    "os=Linux\ncores=10\nload={load} 2 3\nmemtotal_kb=1000\nmemavailable_kb={avail}\n"
    "largest_kb=100\nlargest_name=python\nprocs=12\nsuites={suites}\nprobe_end=1\n"
)


def _worker(
    name: str,
    *,
    load: float = 1,
    suites: int = 0,
    avail: int = 900,
    link: str = "connected",
    current: bool = True,
) -> advice.Candidate:
    reading = parse_reading(name, GOOD.format(load=load, suites=suites, avail=avail), 100)
    a = assess(reading, now=100)
    return advice.Candidate(name, link, current, a.state, reading.suites_running, a)


def test_advice_names_first_ok_worker_of_the_ranking_and_first_without_a_suite() -> None:
    result = advice.advise(
        [_worker("worker-b", load=4, suites=0), _worker("worker-a", load=1, suites=1)],
        order=["worker-a", "worker-b"],
        launched=set(),
        paused=False,
    )
    assert result["ranked_for_heavy_work"] == ["worker-a", "worker-b"]
    assert result["new_work"]["machine"] == "worker-a"
    assert result["new_suite"]["machine"] == "worker-b"
    assert result["new_work"]["why"] and result["new_suite"]["why"]


def test_advice_only_ok_workers_with_a_connected_link_and_a_current_reading() -> None:
    result = advice.advise(
        [
            _worker("worker-a", avail=100),  # busy
            _worker("worker-b", link="unreachable"),
            _worker("worker-c", current=False),
        ],
        order=[],
        launched=set(),
        paused=False,
    )
    assert result["ranked_for_heavy_work"] == []
    assert result["new_work"] == {"machine": None, "why": "no ok worker"}
    assert result["new_suite"]["machine"] is None


def test_advice_from_expired_readings_only_is_null_with_reason() -> None:
    result = advice.advise(
        [_worker("worker-a", current=False)], order=[], launched=set(), paused=False
    )
    assert result["new_work"] == {"machine": None, "why": "no current reading of a worker"}


def test_advice_never_names_the_coordinator() -> None:
    result = advice.advise([_worker("local")], order=[], launched=set(), paused=False)
    assert result["ranked_for_heavy_work"] == [] and result["new_work"]["machine"] is None


def test_advice_skips_a_machine_with_a_launch_in_the_last_60s() -> None:
    result = advice.advise(
        [_worker("worker-a"), _worker("worker-b", load=4)],
        order=["worker-a", "worker-b"],
        launched={"worker-a"},
        paused=False,
    )
    assert result["ranked_for_heavy_work"] == ["worker-b"]
    only = advice.advise([_worker("worker-a")], order=[], launched={"worker-a"}, paused=False)
    assert only["new_work"] == {
        "machine": None,
        "why": "a launch in the last minute on every ok worker",
    }


def test_advice_suite_only_where_none_runs_else_null_with_reason() -> None:
    result = advice.advise(
        [_worker("worker-a", suites=1), _worker("worker-b", suites=2)],
        order=[],
        launched=set(),
        paused=False,
    )
    assert result["new_work"]["machine"] == "worker-a"
    assert result["new_suite"] == {"machine": None, "why": "every ok machine runs a suite"}


def test_advice_one_machine_at_most_for_each_line() -> None:
    result = advice.advise(
        [_worker("worker-a"), _worker("worker-b")], order=[], launched=set(), paused=False
    )
    assert isinstance(result["new_work"]["machine"], str)
    assert isinstance(result["new_suite"]["machine"], str)


def test_advice_is_null_while_probes_are_paused() -> None:
    result = advice.advise([_worker("worker-a")], order=[], launched=set(), paused=True)
    assert result["new_work"] == {"machine": None, "why": "worker probes are paused"}
    assert result["new_suite"]["machine"] is None and result["ranked_for_heavy_work"] == []


def test_launch_is_a_new_address_after_the_baseline_and_is_kept_60s() -> None:
    first = advice.launches(None, frozenset({"t1", "worker-a/t2"}), (), now_wall=1000, now_mono=0)
    assert first == ()
    second = advice.launches(
        frozenset({"t1", "worker-a/t2"}),
        frozenset({"t1", "worker-a/t3", "worker-b/t9"}),
        first,
        now_wall=1010,
        now_mono=10,
    )
    assert {launch.machine for launch in second} == {"worker-a", "worker-b"}
    # The terminal ended; the launch is still remembered until 60 s have passed.
    third = advice.launches(
        frozenset({"t1", "worker-a/t3", "worker-b/t9"}),
        frozenset({"t1"}),
        second,
        now_wall=1060,
        now_mono=60,
    )
    assert advice.launched_on(third, 60) == {"worker-a", "worker-b"}
    assert advice.launched_on(third, 70.01) == set()
    assert advice.recent(third, 60) == [
        {"machine": "worker-a", "seen_at": 1010, "count": 1},
        {"machine": "worker-b", "seen_at": 1010, "count": 1},
    ]


def test_machine_of_an_address() -> None:
    assert advice.machine_of("t53") is None
    assert advice.machine_of("worker-a/t17") == "worker-a"
    assert advice.machine_of("bad address") == ""


def test_a_terminal_address_with_an_id_over_64_characters_is_not_an_address() -> None:
    assert advice.machine_of("worker-a/t" + "a" * 63) == "worker-a"
    assert advice.machine_of("worker-a/t" + "a" * 64) == ""
