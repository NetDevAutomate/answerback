"""Routing policy loading, classification and naming (T-18)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from routing_fakes import FIXTURE, PROJECT, policy

from answerback_bridge.routing.policy import PolicyError, classify, load_policy, parse_policy

# The maintainers' own policy file (`status/routing.json` of the combined repository, now in
# their private archive) with its project path, launcher path and space neutralised and its
# host, credential and ruling notes left out (T-271).
REPO_POLICY = Path(__file__).parent / "fixtures" / "repository-routing-policy.json"


def raw() -> dict:
    return json.loads(FIXTURE.read_text())


def test_policy_comes_from_the_file_not_from_code() -> None:
    loaded = policy()
    assert loaded.project == PROJECT
    assert loaded.harnesses["claude"].launch[-2:] == ("claude", "--dangerously-skip-permissions")
    assert loaded.harnesses["claude"].creatable
    assert not loaded.harnesses["crew"].creatable  # provider crew: created by the conductor
    assert loaded.classes["design-impl"].fallback == ("codex",)
    assert loaded.never_kill.states == {"working"}
    assert loaded.never_kill.receipts == {"pending", "submitted"}
    assert loaded.warm_pool == {"port-fix": 1}

    changed = raw()
    changed["harnesses"]["codex"]["max_workers"] = 5
    changed["lifecycle"]["never_kill"].append("state=blocked")
    reparsed = parse_policy(changed)
    assert reparsed.harnesses["codex"].max_workers == 5
    assert reparsed.never_kill.states == {"working", "blocked"}


def test_only_a_harness_marked_direct_skips_the_launch_script() -> None:
    loaded = policy()
    for name in ("claude", "codex", "grok"):
        assert Path(loaded.harnesses[name].launch[0]).name == "harness_launch.sh"
        assert loaded.harnesses[name].launch[1] == name
    assert loaded.harnesses["kiro"].launch[0] == "kiro-cli" and loaded.harnesses["kiro"].creatable
    # A harness this tool never starts (no launch) is not held to the rule.
    assert parse_policy(raw()).harnesses["crew"].launch == ()


def test_the_repository_policy_loads() -> None:
    loaded = load_policy(REPO_POLICY)
    assert loaded.classes and any(h.creatable for h in loaded.harnesses.values())


def test_the_owner_opened_sentence_is_optional_text() -> None:
    # Review finding 8: the loader used to demand a sentence it never read.
    silent = raw()
    silent["lifecycle"]["never_kill"] = [
        rule for rule in silent["lifecycle"]["never_kill"] if not rule.startswith("owner-opened")
    ]
    assert len(silent["lifecycle"]["never_kill"]) == len(raw()["lifecycle"]["never_kill"]) - 1
    assert parse_policy(silent).never_kill == parse_policy(raw()).never_kill


def test_a_primed_worker_policy_is_refused() -> None:
    """Fact 2026-11-17: a worker that answers an initial prompt is undeliverable (T-13)."""
    primed = raw()
    primed["lifecycle"]["prime_text"] = "You are a worker"
    with pytest.raises(PolicyError, match="T-13"):
        parse_policy(primed)
    prompted = raw()
    prompted["harnesses"]["claude"]["accepts_initial_prompt"] = True
    with pytest.raises(PolicyError, match="T-13"):
        parse_policy(prompted)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p["lifecycle"]["never_kill"].append("on tuesdays"), "never_kill"),
        (lambda p: p["tie_breaks"].append("alphabetical"), "tie_breaks"),
        (lambda p: p["lifecycle"].update(teardown_after="immediately"), "teardown_after"),
        (lambda p: p["lifecycle"].update(name_pattern="worker-{n}"), "name_pattern"),
        (lambda p: p["lifecycle"]["warm_pool"].update(nonsense=1), "warm_pool"),
        (lambda p: p.update(version=2), "version"),
        (lambda p: p.update(project="relative/path"), "absolute"),
        (lambda p: p["harnesses"]["claude"].update(max_workers=0), "max_workers"),
        # Review finding 7: a token-needing harness may only start through the script.
        (lambda p: p["harnesses"]["claude"].update(launch=["/usr/bin/claude"]), "harness_launch"),
        (lambda p: p["harnesses"]["codex"]["launch"].pop(0), "harness_launch"),
        (
            lambda p: p["harnesses"]["grok"].update(launch=["tools/harness_launch.sh", "grok"]),
            "absolute",
        ),
        (
            lambda p: p["harnesses"]["grok"].update(launch=["/work/tools/harness_launch.sh"]),
            "tool name",
        ),
        (lambda p: p["harnesses"]["kiro"].pop("launch_via"), "harnesses.kiro.launch"),
        (lambda p: p["harnesses"]["kiro"].update(launch_via="wrapper"), "launch_via"),
    ],
)
def test_unsupported_policy_is_an_error_not_a_guess(edit, message: str) -> None:
    broken = raw()
    edit(broken)
    with pytest.raises(PolicyError, match=message):
        parse_policy(broken)


def test_missing_or_invalid_policy_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="unreadable"):
        load_policy(tmp_path / "absent.json")
    bad = tmp_path / "routing.json"
    bad.write_text("{not json")
    with pytest.raises(PolicyError, match="not valid JSON"):
        load_policy(bad)


@pytest.mark.parametrize(
    ("brief", "expected"),
    [
        ("Add a new module with a schema and an endpoint.", "design-impl"),
        ("Refactor the adapter and fix the exit code.", "port-fix"),
        ("Read-only: research the repo and write a report.", "analysis"),
        ("Update STATUS.md and the changelog under status/handoff.", "ledger-docs"),
        ("Second opinion wanted: security read of the branch.", "review"),
    ],
)
def test_classify_by_keyword(brief: str, expected: str) -> None:
    found = classify(policy(), brief)
    assert found.name == expected
    assert found.source == "keyword"
    assert found.matched


def test_classify_matches_whole_words_only() -> None:
    """`port` must not fire inside report, support or import."""
    found = classify(policy(), "Write a report on import support.")
    assert found.name == "analysis"
    assert "port-fix" not in found.scores
    # A phrase that ends in punctuation still matches in the middle of a path.
    assert classify(policy(), "Edit status/tasks.json").name == "ledger-docs"


def test_classify_override_and_no_match_and_tie() -> None:
    loaded = policy()
    assert classify(loaded, "anything at all", "port-fix").source == "override"
    with pytest.raises(PolicyError, match="Unknown class"):
        classify(loaded, "anything", "made-up")
    nothing = classify(loaded, "Hello there.")
    assert nothing.name is None and nothing.candidates == ()
    tie = classify(loaded, "One schema, one refactor.")
    assert tie.name is None
    assert set(tie.candidates) == {"design-impl", "port-fix"}


def test_more_distinct_phrases_win_then_more_hits() -> None:
    loaded = policy()
    assert classify(loaded, "schema and protocol, then a refactor").name == "design-impl"
    assert classify(loaded, "schema; refactor, refactor again").name == "port-fix"


def test_worker_names_round_trip_and_reject_foreign_names() -> None:
    loaded = policy()
    name = loaded.worker_name("design-impl", "claude", "261117", 4)
    assert name == "design-impl-claude-261117-4"
    parsed = loaded.parse_worker_name(name)
    assert parsed is not None
    assert (parsed.task_class, parsed.harness, parsed.day, parsed.n) == (
        "design-impl",
        "claude",
        "261117",
        4,
    )
    for foreign in ("claude", "design-impl-claude", "x-design-impl-claude-261117-4", None, 7):
        assert loaded.parse_worker_name(foreign) is None
    with pytest.raises(PolicyError, match="core name"):
        loaded.worker_name("design-impl", "claude", "261117" * 4, 1)


def test_request_id_follows_the_pattern() -> None:
    loaded = policy()
    assert loaded.request_id("20261117", "T-18", "a", "t5r") == "ab-20261117-T-18-a-t5r"
    assert loaded.request_ids("T-18", "a").fullmatch("ab-20261101-T-18-a-t9")
    assert not loaded.request_ids("T-18", "a").fullmatch("ab-20261101-T-18-b-t9")
    assert loaded.request_ids("T-18").fullmatch("ab-20261101-T-18-b-t9")
    assert not loaded.request_ids("T-1").fullmatch("ab-20261101-T-18-b-t9")
    with pytest.raises(PolicyError):
        loaded.request_id("20261117", "///", "a", "t5r")


# --- review finding 2: the gate cap and unread lifecycle keys ---------------------------


def test_the_gate_cap_and_its_classes_are_read() -> None:
    capped = raw()
    capped["lifecycle"].update(
        max_concurrent_gates=1, gate_classes=["design-impl", "port-fix"], gate_note="one at a time"
    )
    loaded = parse_policy(capped)
    assert loaded.max_concurrent_gates == 1
    assert loaded.gate_classes == frozenset({"design-impl", "port-fix"})
    assert loaded.gate_note == "one at a time"


def test_a_policy_without_a_gate_cap_has_none() -> None:
    loaded = parse_policy(raw())
    assert loaded.max_concurrent_gates is None and loaded.gate_classes == frozenset()


@pytest.mark.parametrize(
    ("lifecycle", "message"),
    [
        ({"max_concurrent_gates": 1}, "gate_classes"),  # a cap with nothing to count
        ({"gate_classes": ["port-fix"]}, "max_concurrent_gates"),
        ({"max_concurrent_gates": 0, "gate_classes": ["port-fix"]}, "positive"),
        ({"max_concurrent_gates": True, "gate_classes": ["port-fix"]}, "positive"),
        ({"max_concurrent_gates": "1", "gate_classes": ["port-fix"]}, "positive"),
        ({"max_concurrent_gates": 1, "gate_classes": ["no-such-class"]}, "unknown class"),
        ({"max_concurrent_gates": 1, "gate_classes": []}, "gate_classes"),
        ({"gate_note": 7}, "gate_note"),
    ],
)
def test_a_gate_cap_that_cannot_be_enforced_is_refused(lifecycle: dict, message: str) -> None:
    broken = raw()
    broken["lifecycle"].update(lifecycle)
    with pytest.raises(PolicyError, match=message):
        parse_policy(broken)


def test_an_unknown_lifecycle_key_is_refused() -> None:
    # A limit the loader does not know must not fail open.
    broken = raw()
    broken["lifecycle"]["max_concurrent_suites"] = 1
    with pytest.raises(PolicyError, match="max_concurrent_suites"):
        parse_policy(broken)


def test_lifecycle_text_the_code_does_not_act_on_is_listed() -> None:
    noted = raw()
    noted["lifecycle"].update(hung_action="kill, re-route once", prime="none", make_visible="x")
    assert parse_policy(noted).unenforced == ("hung_action", "make_visible", "prime")
    assert parse_policy(raw()).unenforced == ()


def test_the_repository_policy_caps_gates() -> None:
    loaded = load_policy(REPO_POLICY)
    assert loaded.max_concurrent_gates == 1 and loaded.gate_classes
    assert "hung_action" in loaded.unenforced


# T-72 card 1: every test below is red_at_base unless its docstring says otherwise.


@pytest.mark.parametrize(
    ("harness", "changes"),
    [
        ("claude", {"launch": ["/elsewhere/harness_launch.sh", "claude"]}),
        ("claude", {"launch": ["/work/answerback/tools/harness_launch.sh", "unknown"]}),
        ("claude", {"launch": ["/work/answerback/tools/harness_launch.sh", "claude", "extra"]}),
        ("codex", {"launch": ["/work/answerback/tools/harness_launch.sh", "codex", "exec"]}),
        ("kiro", {"launch": ["unapproved-command"], "launch_via": "direct"}),
        ("kiro", {"launch": ["/elsewhere/kiro-cli"], "launch_via": "direct"}),
        ("kiro", {"launch": ["kiro-cli", "unreviewed-subcommand"]}),
    ],
)
def test_launch_refuses_unreviewed_commands_and_arguments(harness: str, changes: dict) -> None:
    """red_at_base: policy content cannot supply executable paths or arbitrary arguments."""
    changed = raw()
    changed["harnesses"][harness].update(changes)
    with pytest.raises(PolicyError, match="launch"):
        parse_policy(changed)


def test_legacy_launcher_is_normalized_to_the_installed_resource(tmp_path: Path) -> None:
    """red_at_base: even an edited project path cannot choose the executable."""
    from importlib.resources import files

    changed = raw()
    changed["project"] = str(tmp_path)
    for harness in changed["harnesses"].values():
        if harness.get("launch_via", "script") == "script" and harness.get("launch"):
            harness["launch"][0] = str(tmp_path / "tools" / "harness_launch.sh")
    loaded = parse_policy(changed)
    expected = Path(str(files("answerback_bridge").joinpath("data", "harness_launch.sh")))
    assert loaded.harnesses["claude"].launch[0] == str(expected.resolve())
    assert expected.is_file() and expected.stat().st_mode & 0o111
    source = Path(__file__).parents[1] / "tools/harness_launch.sh"
    assert expected.read_bytes() == source.read_bytes()


def test_relocated_repository_policy_keeps_reviewed_launch_flags(tmp_path: Path) -> None:
    """red_at_base: a deployed policy copy loads but only the installed launcher is emitted."""
    from importlib.resources import files

    changed = json.loads(REPO_POLICY.read_text())
    changed["project"] = str(tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/harness_launch.sh").write_text("untrusted repository content\n")
    for harness in changed["harnesses"].values():
        if harness.get("launch_via", "script") == "script" and harness.get("launch"):
            harness["launch"][0] = str(tmp_path / "tools/harness_launch.sh")
    (tmp_path / "status").mkdir()
    copied = tmp_path / "status/routing.json"
    copied.write_text(json.dumps(changed))
    loaded = load_policy(copied)
    expected = str(
        Path(str(files("answerback_bridge").joinpath("data", "harness_launch.sh"))).resolve()
    )
    assert loaded.harnesses["codex"].launch == (expected, "codex", "--yolo", "--no-daemon")
    assert loaded.harnesses["claude"].launch == (
        expected,
        "claude",
        "--dangerously-skip-permissions",
    )
    assert loaded.harnesses["grok"].launch == (expected, "grok", "--yolo")
    assert loaded.harnesses["kiro"].launch == ("kiro-cli", "chat", "-a", "--v3", "--agent=default")


# T-72 card 2


def test_classifier_does_not_rescan_text_for_each_phrase() -> None:
    """red_at_base: compare CPU cost with 2,500 absent phrases, excluding policy build."""
    from time import process_time

    changed = raw()
    changed["classes"] = {
        f"class-{i}": {"primary": "claude", "match": [f"absent-{i}-{j}" for j in range(50)]}
        for i in range(50)
    }
    changed["lifecycle"]["warm_pool"] = {}
    dense = parse_policy(changed)
    text = "ordinary filler " * 16384
    start = process_time()
    assert classify(dense, text).name is None
    elapsed = process_time() - start
    # A broad CPU budget avoids scheduler/load noise; BASE takes seconds on this input.
    assert elapsed < 0.75, f"classification took {elapsed:.3f} CPU seconds"


@pytest.mark.parametrize(
    "text", ["a a a a", "status/tasks.json", "Straße STRASSE", "éé é!", "... ..."]
)
def test_classifier_preserves_overlapping_phrases_and_word_edges(text: str) -> None:
    """pins_existing: an independent regex oracle pins overlap, casefold and scoring."""
    import re

    changed = raw()
    phrases = [
        "a",
        "a a",
        "a a a",
        "status/",
        "status/tasks",
        "Straße",
        "STRASSE",
        "é",
        ".",
        "..",
        "a",
    ]
    changed["classes"] = {"only": {"primary": "claude", "match": phrases}}
    changed["lifecycle"]["warm_pool"] = {}
    expected = []
    for phrase in dict.fromkeys(phrases):
        left = r"(?<![a-z0-9])" if phrase[0].isalnum() else ""
        right = r"(?![a-z0-9])" if phrase[-1].isalnum() else ""
        if re.findall(left + re.escape(phrase.casefold()) + right, text.casefold()):
            expected.append(phrase)
    found = classify(parse_policy(changed), text)
    assert found.matched == tuple(expected)
    assert found.scores == ({"only": len(expected)} if expected else {})


def test_classifier_counts_each_phrase_nonoverlapping_for_hit_ties() -> None:
    """pins_existing: different phrases overlap, occurrences of the same phrase do not."""
    changed = raw()
    changed["classes"] = {
        "first": {"primary": "claude", "match": ["a a", "a a"]},
        "second": {"primary": "claude", "match": ["b"]},
    }
    changed["lifecycle"]["warm_pool"] = {}
    found = classify(parse_policy(changed), "a a a; b b")
    assert found.name == "second" and found.scores == {"first": 1, "second": 1}


def test_classifier_refuses_to_choose_from_an_excessive_match_fanout() -> None:
    """red_at_base: dense overlapping patterns must not defeat the scan work bound."""
    changed = raw()
    changed["classes"] = {"dense": {"primary": "claude", "match": ["." * i for i in range(1, 129)]}}
    changed["lifecycle"]["warm_pool"] = {}
    found = classify(parse_policy(changed), "." * 4096)
    assert found.name is None and not found.scores and not found.matched
    assert classify(parse_policy(changed), "." * 4096, "dense").name == "dense"


def test_run6_oversized_policy_is_refused_after_a_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base (run-6 routing.policy.load_policy:unbounded-read-before-size-cap)."""
    import os

    from answerback_bridge.routing import policy as policy_module

    big = tmp_path / "routing.json"
    big.write_bytes(b" " * (2 * 1024 * 1024))
    read: list[int] = []
    real_read = os.read
    real_read_bytes = Path.read_bytes

    def counting_read(descriptor: int, size: int) -> bytes:
        data = real_read(descriptor, size)
        read.append(len(data))
        return data

    def counting_read_bytes(self: Path) -> bytes:
        data = real_read_bytes(self)
        read.append(len(data))
        return data

    monkeypatch.setattr(os, "read", counting_read)
    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    with pytest.raises(PolicyError, match="exceeds 256 KiB"):
        load_policy(big)
    assert sum(read) <= policy_module.MAX_POLICY_BYTES + 1


def test_run6_policy_symlink_and_fifo_are_refused(tmp_path: Path) -> None:
    """red_at_base: a symlink is not followed and a FIFO is not read until EOF."""
    import os

    target = tmp_path / "real.json"
    target.write_bytes(REPO_POLICY.read_bytes())
    link = tmp_path / "routing.json"
    link.symlink_to(target)
    with pytest.raises(PolicyError, match="regular non-symlink file|unreadable"):
        load_policy(link)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(PolicyError, match="regular non-symlink file"):
        load_policy(fifo)
    assert load_policy(target).classes == load_policy(REPO_POLICY).classes
