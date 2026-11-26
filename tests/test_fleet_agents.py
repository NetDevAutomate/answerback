"""fleet: which live terminals count as active agents, and the labelled lanes."""

import pytest

from answerback_bridge.fleet import agents
from answerback_bridge.fleet.agents import Terminal


def _t(machine: str | None, status: str | None, agent: str | None) -> Terminal:
    return Terminal(machine, status, agent)


FIRST = {
    "worker-a/t1": _t("worker-a", "working", "claude-code"),
    "worker-a/t2": _t("worker-a", "blocked", "claude-code"),
    "worker-a/t3": _t("worker-a", "idle", "codex"),
    "worker-a/t4": _t("worker-a", "done", "kiro-cli"),
    "worker-a/t5": _t("worker-a", None, "zsh"),
    "worker-a/t6": _t("worker-a", None, "node"),
    "worker-a/t7": _t("worker-a", None, "sh"),
    "worker-a/t8": _t("worker-a", None, "sh"),
}
LABELLED = {
    "worker-a/t7": _t("worker-a", None, "sh"),
    "worker-a/t8": _t("worker-a", None, "sh"),
}


def test_active_counts_working_and_non_shell_programs_and_labelled_lanes_not_blocked() -> None:
    active, lanes = agents.count(FIRST, LABELLED)
    assert active == {"worker-a": 4}
    assert lanes == {"worker-a": 2}


def test_a_blocked_terminal_waits_for_a_person_and_is_not_active() -> None:
    active, _ = agents.count({"worker-a/t2": _t("worker-a", "blocked", "claude-code")}, {})
    assert active == {}


def test_a_labelled_terminal_that_is_idle_is_neither_active_nor_a_lane() -> None:
    labelled = {"worker-a/t3": _t("worker-a", "idle", "sh")}
    active, lanes = agents.count(labelled, labelled)
    assert active == {} and lanes == {}


def test_an_address_in_both_listings_counts_once() -> None:
    both = {"worker-a/t9": _t("worker-a", "working", "sh")}
    active, lanes = agents.count(both, both)
    assert active == {"worker-a": 1}
    assert lanes == {}
    only_labelled = {"worker-a/t7": _t("worker-a", None, "sh")}
    active, lanes = agents.count(only_labelled, only_labelled)
    assert active == {"worker-a": 1} and lanes == {"worker-a": 1}


def test_the_coordinators_terminals_follow_the_same_rule() -> None:
    first = {
        "t1": _t(None, "working", "claude-code"),
        "t2": _t(None, "blocked", "claude-code"),
        "t3": _t(None, None, "zsh"),
        "t4": _t(None, None, "sh"),
    }
    active, lanes = agents.count(first, {"t4": _t(None, None, "sh")})
    assert active == {None: 2} and lanes == {None: 1}


def test_without_the_labelled_listing_lanes_are_unknown_and_the_first_listing_counts() -> None:
    active, lanes = agents.count(FIRST, None)
    assert active == {"worker-a": 2}
    assert lanes is None


@pytest.mark.parametrize("agent", ["zsh", "bash", "sh", "fish", "", None])
def test_a_plain_shell_without_status_is_not_active(agent: str | None) -> None:
    assert agents.count({"t1": _t(None, None, agent)}, {}) == ({}, {})


@pytest.mark.parametrize("label", ["ab.lane=1", "k=v", "A_b-c.d=-x", "k" * 64 + "=" + "v" * 64])
def test_a_good_lane_label_is_accepted(label: str) -> None:
    assert agents.label_ok(label)


@pytest.mark.parametrize(
    "label",
    ["", "ab.lane", "=1", "k=", "-k=1", "--all=1", "k=1 --all", "k=v=w", "k" * 65 + "=1", "k=ä"],
)
def test_a_bad_lane_label_is_refused(label: str) -> None:
    assert not agents.label_ok(label)
