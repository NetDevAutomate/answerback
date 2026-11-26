"""fleet: every outside text is cut and cleaned; bad machine names are refused."""

import pytest

from answerback_bridge.fleet import bounds


def test_text_from_outside_is_cut_and_loses_control_characters() -> None:
    hostile = "ok\x1b[31m\x00red\u202e\u2028\ud800" + "x" * 500
    cleaned = bounds.clean(hostile, bounds.TEXT)
    assert cleaned is not None
    assert len(cleaned) == 200
    assert not any(ord(c) < 32 or ord(c) == 127 for c in cleaned)
    assert "\u202e" not in cleaned and "\u2028" not in cleaned and "\ud800" not in cleaned
    assert cleaned.startswith("ok[31mred")
    cleaned.encode("utf-8")


def test_bounds_are_64_for_names_120_for_titles_200_for_other_text() -> None:
    assert len(bounds.clean("n" * 99, bounds.NAME) or "") == 64
    assert len(bounds.clean("t" * 999, bounds.TITLE) or "") == 120
    assert len(bounds.clean("o" * 999, bounds.TEXT) or "") == 200


def test_clean_of_a_non_text_is_none() -> None:
    assert bounds.clean(None, bounds.TEXT) is None
    assert bounds.clean(12, bounds.TEXT) is None
    assert bounds.clean("\x00\x01", bounds.TEXT) is None


@pytest.mark.parametrize(
    "name", ["", "-a", "a b", "a/b", "local", "x" * 64, "ä", "a\n", None, 5, "a;rm"]
)
def test_a_bad_machine_name_is_refused(name: object) -> None:
    assert not bounds.machine_name_ok(name)


@pytest.mark.parametrize("name", ["worker-a", "worker_b.lan", "W1", "x" * 63])
def test_a_good_machine_name_is_accepted(name: str) -> None:
    assert bounds.machine_name_ok(name)
