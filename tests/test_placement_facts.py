"""Machine facts for placement (fabric row P2): the `placement` block of the policy file.

Labels: `red_at_pretip` fails at d98fba8 (no `placement.facts` module there);
`pins_existing` passes there on purpose.
"""

import copy
import errno
import json
import math
import os
import re
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.placement.assess import Thresholds
from answerback_bridge.routing.policy import load_policy, parse_policy

# The maintainers' own policy file (`status/routing.json` of the combined repository, now in
# their private archive) with its project path, launcher path and space neutralised and its
# host, credential and ruling notes left out (T-271).
REPO_POLICY = Path(__file__).parent / "fixtures" / "repository-routing-policy.json"

# The form of the block is new with this row (no file holds one yet): every key the reader
# takes, around the maintainers' policy file, which `policy()` reads from disk.
BLOCK: dict[str, Any] = {
    "order": ["node-b", "node-a"],
    "thresholds": {"cpu_busy": 0.6},
    "worker_cost": 2,
    "machines": {
        "local": {"project": "/Users/someone/project", "harnesses": {"kiro": {"ready": True}}},
        "node-a": {
            "project": "/home/someone/project",
            "max_total": 4,
            "tools": ["uv", "just"],
            "harnesses": {
                "codex": {"ready": True, "max_workers": 2},
                "claude": {"ready": False, "why": "folder trust not accepted"},
            },
        },
        "node-b": {},
    },
}


def policy(block: Any = ...) -> dict[str, Any]:
    """The maintainers' policy file, optionally with a placement block."""
    raw = json.loads(REPO_POLICY.read_text())
    assert "placement" not in raw
    if block is not ...:
        raw["placement"] = copy.deepcopy(block)
    return raw


def changed(path: str, value: Any) -> dict[str, Any]:
    """BLOCK with one value replaced; `path` is split at `/`, `...` as value removes the key."""
    block = copy.deepcopy(BLOCK)
    *parents, last = path.split("/")
    at = block
    for key in parents:
        at = at.setdefault(key, {})
    if value is ...:
        del at[last]
    else:
        at[last] = value
    return block


def test_pins_existing_policy_reader_ignores_the_block() -> None:
    # pins_existing: parse_policy reads named keys only, so P2 needs no change in policy.py.
    assert parse_policy(policy(BLOCK)) == parse_policy(policy())
    assert parse_policy(policy({"nonsense": [1]})) == load_policy(REPO_POLICY)


def test_red_at_pretip_no_block_gives_defaults_and_no_machine() -> None:
    from answerback_bridge.placement.facts import Facts, load_facts, parse_facts

    facts = parse_facts(policy())
    assert facts == Facts() == load_facts(REPO_POLICY)
    assert facts.order == () and dict(facts.machines) == {}
    assert facts.thresholds == Thresholds()
    assert facts.worker_cost == 1.0
    assert parse_facts(policy({})) == facts


def test_red_at_pretip_block_reads_to_facts() -> None:
    from answerback_bridge.placement.facts import HarnessFacts, MachineFacts, parse_facts

    facts = parse_facts(policy(BLOCK))
    assert facts.order == ("node-b", "node-a")
    assert facts.thresholds == Thresholds(cpu_busy=0.6)
    assert facts.worker_cost == 2.0 and type(facts.worker_cost) is float
    assert list(facts.machines) == ["local", "node-a", "node-b"]
    assert facts.machines["node-a"] == MachineFacts(
        name="node-a",
        project="/home/someone/project",
        max_total=4,
        tools=frozenset({"uv", "just"}),
        harnesses={
            "codex": HarnessFacts(ready=True, max_workers=2),
            "claude": HarnessFacts(ready=False, why="folder trust not accepted"),
        },
    )
    assert facts.machines["node-b"] == MachineFacts(name="node-b")
    assert facts.machines["local"].harnesses["kiro"] == HarnessFacts(ready=True)


def test_red_at_pretip_facts_cannot_be_changed() -> None:
    from answerback_bridge.placement.facts import parse_facts

    facts = parse_facts(policy(BLOCK))
    with pytest.raises(FrozenInstanceError):
        facts.worker_cost = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        facts.machines["node-a"].max_total = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        facts.machines["new"] = facts.machines["node-a"]  # type: ignore[index]
    with pytest.raises(TypeError):
        facts.machines["node-a"].harnesses["grok"] = None  # type: ignore[index]


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ([], "placement must be an object"),
        (changed("node_order", ["node-a"]), r"placement has unsupported keys.*node_order"),
        (changed("machines/node-a/ram_gb", 64), r"machines\.node-a has unsupported keys.*ram_gb"),
        (
            changed("machines/node-a/harnesses/codex/launch_extra", []),
            r"harnesses\.codex has unsupported keys.*launch_extra",
        ),
        (changed("machines", []), "placement.machines must be an object"),
        (changed("machines/node-a", "ready"), r"machines\.node-a must be an object"),
        (changed("machines/bad name", {}), "must be a machine name"),
        (changed("machines/-a", {}), "must be a machine name"),
        (changed("machines/" + "a" * 64, {}), "must be a machine name"),
        (changed("coordinator", "node-a"), r"placement has unsupported keys.*coordinator"),
        (changed("order", "node-a"), "placement.order must be a list of machine names"),
        (changed("order", ["node-a", 3]), "placement.order must be a list of machine names"),
        (changed("order", ["node-a", "node-a"]), "placement.order names a machine twice"),
        (
            changed("order", ["node-a", "other-host"]),
            r"order names machines without facts.*other-host",
        ),
        (changed("thresholds", [0.6]), "placement.thresholds must be an object"),
        (changed("thresholds/overflow_load", 1.5), r"names no threshold.*overflow_load"),
        (changed("thresholds/cpu_busy", "0.6"), r"thresholds\.cpu_busy must be a number"),
        (changed("thresholds/cpu_busy", True), r"thresholds\.cpu_busy must be a number"),
        (changed("thresholds/cpu_busy", -0.1), r"thresholds\.cpu_busy must be finite and not"),
        (changed("thresholds/cpu_full", math.inf), r"thresholds\.cpu_full must be finite"),
        (changed("thresholds/cpu_full", math.nan), r"thresholds\.cpu_full must be finite"),
        (changed("thresholds/cpu_busy", 1.2), "cpu_busy must be below cpu_full"),
        (changed("thresholds/cpu_busy", 1.0), "cpu_busy must be below cpu_full"),
        (changed("thresholds/compressor_full", 0.2), "compressor_busy must be below"),
        (changed("thresholds/compressor_full", 0.25), "compressor_busy must be below"),
        (changed("thresholds/largest_process_busy", 0.6), "largest_process_busy must be below"),
        (changed("thresholds/largest_process_busy", 0.5), "largest_process_busy must be below"),
        (changed("thresholds/available_full", 0.2), "available_full must be below"),
        (changed("thresholds/available_full", 0.15), "available_full must be below"),
        (changed("thresholds/cpu_busy", 0), r"thresholds\.cpu_busy must be above 0"),
        (changed("thresholds/available_full", 0), r"thresholds\.available_full must be above 0"),
        (changed("thresholds/stall_busy", 0), r"thresholds\.stall_busy must be above 0"),
        (changed("thresholds/max_age", 0), r"thresholds\.max_age must be above 0"),
        (changed("thresholds/max_age", 600.5), r"thresholds\.max_age must not be above 600"),
        (changed("thresholds/swap_busy", 1.01), "swap_busy is a share and must not be above 1"),
        (changed("thresholds/available_busy", 2), "available_busy is a share and must not be"),
        (changed("thresholds/compressor_full", 1.5), "compressor_full is a share and must not"),
        (changed("thresholds/largest_process_full", 3), "largest_process_full is a share and"),
        (changed("worker_cost", "1"), "placement.worker_cost must be a number"),
        (changed("worker_cost", -1), "placement.worker_cost must be finite and not negative"),
        (changed("worker_cost", True), "placement.worker_cost must be a number"),
        (changed("worker_cost", math.nan), "placement.worker_cost must be finite"),
        (changed("recent_seconds", 90), r"placement has unsupported keys.*recent_seconds"),
        (None, "placement must be an object"),
        ("local", "placement must be an object"),
        (changed("machines/node-a/project", "code/project"), "project must be an absolute path"),
        (changed("machines/node-a/project", "~/project"), "project must be an absolute path"),
        (changed("machines/node-a/project", 5), "project must be an absolute path"),
        (changed("machines/node-a/project", ""), "project must be an absolute path"),
        (changed("machines/node-a/project", "/work/a\x00b"), "of printable characters"),
        (changed("machines/node-a/project", "/work/a\nb"), "of printable characters"),
        (changed("machines/node-a/max_total", 0), "max_total must be a positive int"),
        (changed("machines/node-a/max_total", True), "max_total must be a positive int"),
        (changed("machines/node-a/max_total", 2.0), "max_total must be a positive int"),
        (changed("machines/node-a/tools", "uv"), "tools must be a list of nonempty strings"),
        (changed("machines/node-a/tools", ["uv", ""]), "tools must be a nonempty string"),
        (changed("machines/node-a/harnesses", ["codex"]), "harnesses must be an object"),
        (changed("machines/node-a/harnesses/aider", {"ready": True}), r"no harness.*aider"),
        (changed("machines/node-a/harnesses/codex", True), r"harnesses\.codex must be an object"),
        (changed("machines/node-a/harnesses/codex/ready", ...), "ready is required"),
        (changed("machines/node-a/harnesses/codex/ready", "yes"), "ready is required"),
        (changed("machines/node-a/harnesses/codex/ready", 1), "ready is required"),
        (changed("machines/node-a/harnesses/codex/max_workers", 0), "max_workers must be a pos"),
        (changed("machines/node-a/harnesses/claude/why", ""), "why must be a nonempty string"),
        # Review 1, M4 and M1.
        (changed("machines/node-a/harnesses/claude/why", 7), "why must be a nonempty string"),
        (changed("machines/node-a/tools", ["uv", 7]), "tools must be a nonempty string"),
        (changed("machines/node-a/tools", [["uv"]]), "tools must be a nonempty string"),
        (changed("worker_cost", 10**400), "placement.worker_cost must be finite"),
        (changed("thresholds/cpu_full", 10**400), r"thresholds\.cpu_full must be finite"),
        (changed("thresholds/max_age", -(10**400)), r"thresholds\.max_age must be finite"),
    ],
)
def test_red_at_pretip_refusals(block: Any, message: str) -> None:
    from answerback_bridge.placement.facts import FactsError, parse_facts

    with pytest.raises(FactsError, match=message):
        parse_facts(policy(block))


def test_red_at_pretip_at_most_sixteen_machines() -> None:
    from answerback_bridge.placement.facts import FactsError, parse_facts

    # The probe measures at most 16 machines, and `local` is always one of them.
    fifteen: dict[str, Any] = {"machines": {f"node-{n}": {} for n in range(15)}}
    assert len(parse_facts(policy(fifteen)).machines) == 15
    with_local = {"machines": {**fifteen["machines"], "local": {}}}
    assert len(parse_facts(policy(with_local)).machines) == 16
    fifteen["machines"]["node-15"] = {}
    with pytest.raises(FactsError, match="more than 16 machines, counting local"):
        parse_facts(policy(fifteen))


def test_red_at_pretip_file_errors(tmp_path: Path) -> None:
    from answerback_bridge.placement.facts import FactsError, load_facts, parse_facts

    with pytest.raises(FactsError, match="Routing policy must be an object"):
        parse_facts([])
    with pytest.raises(FactsError, match="unreadable"):
        load_facts(tmp_path / "absent.json")
    # Review 2, R2-5: a path the system refuses before it opens anything, and a folder.
    with pytest.raises(FactsError, match="unreadable"):
        load_facts(Path("\x00"))
    with pytest.raises(FactsError, match="unreadable"):
        load_facts(tmp_path)
    bad = tmp_path / "routing.json"
    bad.write_text("{not json")
    with pytest.raises(FactsError, match="not valid JSON"):
        load_facts(bad)
    bad.write_text(json.dumps(policy(changed("worker_cost", -1))))
    with pytest.raises(FactsError, match="worker_cost"):
        load_facts(bad)
    bad.write_text(json.dumps(policy(BLOCK)))
    assert load_facts(bad) == parse_facts(policy(BLOCK))
    bad.write_text(" " * (256 * 1024 + 1))
    with pytest.raises(FactsError, match="exceeds 256 KiB"):
        load_facts(bad)


@pytest.mark.parametrize("kind", ["absent", "folder", "not json"])
def test_red_at_pretip_survivors_a_file_message_is_one_escaped_line(
    tmp_path: Path, kind: str
) -> None:
    # Review 3's surviving mutants: the properties of the message, not its wording.
    from answerback_bridge.placement.facts import FactsError, load_facts

    path = tmp_path / ("a\nb\x1bc" + "k" * 200)
    reason = ""
    if kind == "absent":
        reason = os.strerror(errno.ENOENT)
    elif kind == "folder":
        path.mkdir()
        reason = os.strerror(errno.EISDIR)
    else:
        path.write_text("{not json")
    with pytest.raises(FactsError) as caught:
        load_facts(path)
    message = str(caught.value)
    assert not [c for c in message if c < " " or c == "\x7f"]
    assert repr(path) in message and "a\\nb\\x1bc" in message
    # T-145 (pins_existing): the message is judged without the path it shows, so a word in
    # a folder's name (a scratch folder called `unreadable`) cannot answer for the message.
    assert message.count(repr(path)) == 1
    assert ("unreadable" in message.replace(repr(path), "")) == bool(reason)
    assert message.endswith(f" ({reason})" if reason else repr(path))


def test_red_at_pretip_review1_messages_stay_short() -> None:
    # Review 1, M2: a message repeats at most eight names, each cut at 64 characters.
    from answerback_bridge.placement.facts import FactsError, parse_facts

    long = "k" * 100_000
    many = {f"key-{n:04d}": 1 for n in range(1000)}
    declared = policy()
    declared["harnesses"][long] = declared["harnesses"]["codex"]
    cases: list[tuple[dict[str, Any], str]] = [
        (policy(changed(long, 1)), "placement has unsupported keys"),
        (policy({**BLOCK, **many}), r"placement has unsupported keys.*key-0007' and 992 more"),
        (policy(changed(f"machines/node-a/{long}", 1)), r"machines\.node-a has unsupported keys"),
        (policy(changed(f"machines/node-a/harnesses/codex/{long}", 1)), "codex has unsupported"),
        (policy(changed(f"machines/node-a/harnesses/{long}", {})), "names no harness"),
        (policy(changed(f"thresholds/{long}", 1)), "names no threshold"),
        (policy(changed("machines/node-b", {"harnesses": {long: {}}})), "names no harness"),
        ({**declared, "placement": changed(f"machines/node-a/harnesses/{long}", {})}, "ready is"),
        ({**declared, "placement": changed(f"machines/node-a/harnesses/{long}", 1)}, "an object"),
    ]
    for raw, message in cases:
        with pytest.raises(FactsError, match=message) as caught:
            parse_facts(raw)
        assert len(str(caught.value)) < 1000, message
        assert "k" * 65 not in str(caught.value)
        assert "k" * 64 + "..." in str(caught.value) or "and 992 more" in str(caught.value)
    names = [f"stranger-{n:04d}" for n in range(1000)]
    with pytest.raises(FactsError, match="order names machines without facts") as caught:
        parse_facts(policy(changed("order", [long, *names])))
    assert len(str(caught.value)) < 1000 and str(caught.value).endswith("and 993 more")
    # Review 2, R2-7, and the closing round, R3-3: a key that JSON cannot give is refused
    # by its type and never made text, at every level of the block where a key is read.
    keys = [(5, "int"), (10**5000, "int"), (2.5, "float"), (None, "NoneType"), ((1, 2), "tuple")]
    for key, kind in keys:
        for raw, where in [
            (policy({**BLOCK, key: 1, "x": 1}), "placement"),
            (policy(changed("thresholds", {key: 1, "x": 1})), r"placement\.thresholds"),
            (policy(changed("machines", {key: {}})), r"placement\.machines"),
            (policy(changed("machines/node-a", {key: 1, "x": 1})), r"machines\.node-a"),
            (policy(changed("machines/node-a/harnesses", {key: {}})), r"node-a\.harnesses"),
            (
                policy(changed("machines/node-a/harnesses/codex", {"ready": True, key: 1})),
                r"harnesses\.codex",
            ),
        ]:
            with pytest.raises(FactsError, match=f"{where} has a key that is not text: {kind}$"):
                parse_facts(raw)


def test_red_at_pretip_review1_the_file_is_read_bounded_and_nesting_is_refused(
    tmp_path: Path,
) -> None:
    # Review 1, M4: the bound holds before the bytes are in memory, not after.
    from answerback_bridge.placement.facts import (
        MAX_JSON_DEPTH,
        Facts,
        FactsError,
        load_facts,
        parse_facts,
    )

    asked: list[int | None] = []

    class Stream:
        def __enter__(self) -> "Stream":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int | None = None) -> bytes:
            asked.append(size)
            return json.dumps(policy(BLOCK)).encode()

    class Spy:
        def open(self, mode: str) -> Stream:
            assert mode == "rb"
            return Stream()

    assert load_facts(Spy()) == parse_facts(policy(BLOCK))  # type: ignore[arg-type]
    assert asked == [256 * 1024 + 1]
    # Valid JSON far below the size bound, nested deeper than the reader allows.
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 10_000 + "]" * 10_000)
    with pytest.raises(FactsError) as caught:
        load_facts(deep)
    assert str(caught.value) == (
        f"Routing policy is too deeply nested (maximum JSON depth {MAX_JSON_DEPTH})"
    )

    # The explicit bound counts arrays and objects, but brackets inside a JSON string
    # are data. The check is iterative, so neither case consumes Python's call stack.
    at_limit = tmp_path / "at-limit.json"
    at_limit.write_text(
        '{"ignored":' + "[" * (MAX_JSON_DEPTH - 1) + "0" + "]" * (MAX_JSON_DEPTH - 1) + "}"
    )
    assert load_facts(at_limit) == Facts()
    quoted = tmp_path / "quoted-brackets.json"
    quoted.write_text(json.dumps({"ignored": '[{\\"' * (MAX_JSON_DEPTH + 1)}))
    assert load_facts(quoted) == Facts()


def test_red_at_pretip_closing_ceilings_are_bounded_above(tmp_path: Path) -> None:
    # Closing round, R3-1: one stated upper bound on both ceilings, from a dict and from a file.
    from answerback_bridge.placement.facts import FactsError, load_facts, parse_facts

    file = tmp_path / "routing.json"
    refusal = "must be a positive int, at most 10000"
    for path in ("machines/node-a/max_total", "machines/node-a/harnesses/codex/max_workers"):
        at = policy(changed(path, 10_000))
        file.write_text(json.dumps(at))
        for facts in (parse_facts(at), load_facts(file)):
            node = facts.machines["node-a"]
            assert 10_000 in (node.max_total, node.harnesses["codex"].max_workers)
        for over in (10_001, 10**5000):
            with pytest.raises(FactsError, match=f"{path.rsplit('/', 1)[1]} {refusal}$"):
                parse_facts(policy(changed(path, over)))
        file.write_text(json.dumps(policy(changed(path, 10_001))))
        with pytest.raises(FactsError, match=f"{path.rsplit('/', 1)[1]} {refusal}$"):
            load_facts(file)


def test_red_at_pretip_closing_the_docstring_names_every_threshold() -> None:
    # Closing round, R3-5: the list in the docstring cannot drift from `assess.Thresholds`.
    from answerback_bridge.placement import facts

    text = facts.__doc__ or ""
    bullet = text[text.index("- `thresholds`:") : text.index("- `worker_cost`:")]
    # The one sentence that lists the keys, so that a later mention does not count.
    listed = bullet[bullet.index("and no other:") : bullet.index("A key that is not")]
    named = re.findall(r"`([a-z0-9_]+)`", listed)
    assert sorted(named) == sorted(item.name for item in fields(Thresholds))
    # The root shape the docstring states: an object, anything else is refused.
    for root in ([], None, "placement", 5, True):
        with pytest.raises(facts.FactsError, match="^Routing policy must be an object$"):
            facts.parse_facts(root)
    assert facts.parse_facts({}) == facts.Facts()
