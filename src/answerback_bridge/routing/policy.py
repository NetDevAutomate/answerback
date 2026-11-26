"""The routing policy file is the source of truth; this module only reads and applies it.

Classes and lifecycle limits come from the policy. Executable choices and launch arguments
are constrained by this package: repository content cannot define a new command to run.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from importlib.resources import files
from pathlib import Path
from typing import Any

MAX_POLICY_BYTES = 256 * 1024
MAX_MATCH_VISITS = 32768
_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
TIE_BREAKS = ("existing_idle_peer_same_project", "not_author_of_touched_files", "cheaper_seat")
TEARDOWN_AFTER = "handoff_merged_or_rejected"
# Every lifecycle key the loader accepts. A key outside this set is an error, so a limit
# added to the file cannot be silently ignored.
_LIFECYCLE_ENFORCED = frozenset(
    {
        "gate_classes",
        "keep_idle_min",
        "max_concurrent_gates",
        "max_total",
        "name_pattern",
        "never_kill",
        "prime_text",
        "prime_timeout_s",
        "request_id_pattern",
        "teardown_after",
        "warm_pool",
    }
)
# Text for people. The code keeps it or reports it as unenforced; it never acts on it.
_LIFECYCLE_PROSE = frozenset({"gate_note", "hung_action", "make_visible", "prime"})
_NAME_FIELDS = ("class", "harness", "yymmdd", "n")
_REQUEST_FIELDS = ("yyyymmdd", "task", "lane", "address")
_CORE_NAME = re.compile(r"[a-z0-9_-]{1,32}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


# The only way a token-needing harness may start: it resolves the token inside the new
# terminal and exec()s the binary, so the token is never in argv, a file or a log.
LAUNCH_SCRIPT = "harness_launch.sh"
# Reviewed foreground command forms. Expanding these requires a source review, not
# just an edit to status/routing.json. Preserve the deployed interactive flags.
_SCRIPT_ARGUMENTS = {
    "claude": frozenset({(), ("--dangerously-skip-permissions",)}),
    "codex": frozenset({(), ("--yolo",), ("--yolo", "--no-daemon")}),
    "grok": frozenset({(), ("--yolo",)}),
}
_DIRECT_COMMANDS = {
    "kiro": frozenset({("kiro-cli", "chat", "-a", "--v3", "--agent=default")}),
}


class PolicyError(ValueError):
    """The routing policy is missing, malformed, or asks for something unsupported."""


@dataclass(frozen=True)
class Harness:
    name: str
    provider: str
    agent_kind: str | None
    launch: tuple[str, ...]
    model: str
    max_workers: int
    seat_cost: float | None
    order: int

    @property
    def creatable(self) -> bool:
        """Only core (hc) harnesses with a launch command can be started from here."""
        return self.provider == "hc" and bool(self.launch) and self.agent_kind is not None


@dataclass(frozen=True)
class TaskClass:
    name: str
    primary: str
    fallback: tuple[str, ...]
    match: tuple[str, ...]


@dataclass(frozen=True)
class NeverKill:
    states: frozenset[str]
    receipts: frozenset[str]


@dataclass(frozen=True)
class Classification:
    name: str | None
    source: str
    matched: tuple[str, ...]
    scores: Mapping[str, int]
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class WorkerName:
    task_class: str
    harness: str
    day: str
    n: int


class _PhraseMatcher:
    """Aho-Corasick index shared by all classes, built once for a policy.

    Failure links avoid rescanning text for each phrase. Output links retain nested
    matches without copying suffix lists into every node. Dense match output has a
    separate work budget; exceeding it gives no classification, never partial scores.
    """

    def __init__(self, classes: Mapping[str, TaskClass]) -> None:
        self.phrases = tuple(dict.fromkeys(p for c in classes.values() for p in c.match))
        self.folded = tuple(p.casefold() for p in self.phrases)
        self.edges: list[dict[str, int]] = [{}]
        self.fail = [0]
        self.output: list[list[int]] = [[]]
        self.suffix = [0]
        for index, phrase in enumerate(self.folded):
            node = 0
            for char in phrase:
                if char not in self.edges[node]:
                    self.edges[node][char] = len(self.edges)
                    self.edges.append({})
                    self.fail.append(0)
                    self.output.append([])
                    self.suffix.append(0)
                node = self.edges[node][char]
            self.output[node].append(index)
        pending = deque(self.edges[0].values())
        while pending:
            parent = pending.popleft()
            for char, child in self.edges[parent].items():
                pending.append(child)
                fallback = self.fail[parent]
                while fallback and char not in self.edges[fallback]:
                    fallback = self.fail[fallback]
                target = self.edges[fallback].get(char, 0)
                self.fail[child] = target
                self.suffix[child] = target if self.output[target] else self.suffix[target]

    def counts(self, text: str) -> dict[str, int] | None:
        folded = text.casefold()
        counts: dict[str, int] = {}
        last_end: dict[int, int] = {}
        node = visits = 0
        for offset, char in enumerate(folded):
            while node and char not in self.edges[node]:
                node = self.fail[node]
            node = self.edges[node].get(char, 0)
            found = node
            end = offset + 1
            while found:
                for index in self.output[found]:
                    visits += 1
                    if visits > MAX_MATCH_VISITS:
                        return None
                    phrase = self.phrases[index]
                    start = end - len(self.folded[index])
                    if start < last_end.get(index, 0):
                        continue
                    if phrase[0].isalnum() and start and folded[start - 1] in _WORD_CHARS:
                        continue
                    if phrase[-1].isalnum() and end < len(folded) and folded[end] in _WORD_CHARS:
                        continue
                    counts[phrase] = counts.get(phrase, 0) + 1
                    last_end[index] = end
                found = self.suffix[found]
        return counts


@dataclass(frozen=True)
class Policy:
    project: str
    project_space: str | None
    harnesses: Mapping[str, Harness]
    classes: Mapping[str, TaskClass]
    tie_breaks: tuple[str, ...]
    name_pattern: str
    request_id_pattern: str
    prime_timeout_s: float
    keep_idle_min: float
    warm_pool: Mapping[str, int]
    max_total: int
    max_concurrent_gates: int | None  # None: the policy sets no gate cap
    gate_classes: frozenset[str]  # classes whose brief ends in a full gate run
    gate_note: str | None
    unenforced: tuple[str, ...]  # lifecycle text the file carries and nothing here acts on
    never_kill: NeverKill
    _matcher: _PhraseMatcher = dataclass_field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_matcher", _PhraseMatcher(self.classes))

    def worker_name(self, task_class: str, harness: str, day: str, n: int) -> str:
        name = (
            self.name_pattern.replace("{class}", task_class)
            .replace("{harness}", harness)
            .replace("{yymmdd}", day)
            .replace("{n}", str(n))
        )
        if not _CORE_NAME.fullmatch(name):
            raise PolicyError(f"Worker name {name!r} is not a core name (1-32 of a-z 0-9 - _)")
        return name

    def parse_worker_name(self, name: Any) -> WorkerName | None:
        """Recognise only names this policy's pattern can produce."""
        if not isinstance(name, str):
            return None
        pattern = re.escape(self.name_pattern)
        for field, expression in (
            ("class", _alternation(self.classes)),
            ("harness", _alternation(self.harnesses)),
            ("yymmdd", r"[0-9]{6}"),
            ("n", r"[1-9][0-9]{0,3}"),
        ):
            pattern = pattern.replace(re.escape("{" + field + "}"), f"(?P<{field}>{expression})")
        found = re.fullmatch(pattern, name)
        if found is None:
            return None
        return WorkerName(found["class"], found["harness"], found["yymmdd"], int(found["n"]))

    def request_id(self, day: str, task: str, lane: str, address: str) -> str:
        value = (
            self.request_id_pattern.replace("{yyyymmdd}", day)
            .replace("{task}", _slug(task))
            .replace("{lane}", _slug(lane))
            .replace("{address}", _slug(address))
        )
        if not _REQUEST_ID.fullmatch(value):
            raise PolicyError("Request id pattern yields an id the bridge would refuse")
        return value

    def request_ids(self, task: str, lane: str | None = None) -> re.Pattern[str]:
        """Match request ids for one task on any day and address; `lane` None is any lane."""
        pattern = re.escape(self.request_id_pattern)
        for field, expression in (
            ("yyyymmdd", r"[0-9]{8}"),
            ("task", re.escape(_slug(task))),
            ("lane", r"[A-Za-z0-9._-]+" if lane is None else re.escape(_slug(lane))),
            ("address", r"[A-Za-z0-9._:-]+"),
        ):
            pattern = pattern.replace(re.escape("{" + field + "}"), expression)
        return re.compile(pattern)


def _alternation(names: Mapping[str, Any]) -> str:
    return "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    if not cleaned:
        raise PolicyError("Request id parts need at least one letter or digit")
    return cleaned


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{where} must be an object")
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise PolicyError(f"{where} must be a list of nonempty strings")
    return tuple(value)


def _positive(value: Any, where: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise PolicyError(f"{where} must be a {'non-negative' if allow_zero else 'positive'} int")
    return value


def _seconds(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise PolicyError(f"lifecycle.{where} must be a non-negative number")
    return float(value)


def _never_kill(rules: tuple[str, ...]) -> NeverKill:
    states: set[str] = set()
    receipts: set[str] = set()
    for rule in rules:
        state = re.fullmatch(r"state=([a-z|]+)", rule)
        receipt = re.fullmatch(r"receipt in ([a-z|]+)", rule)
        if state:
            states.update(state[1].split("|"))
        elif receipt:
            receipts.update(receipt[1].split("|"))
        elif rule.startswith("owner-opened"):
            # A sentence for people. The rule itself is not policy and cannot be turned
            # off by leaving it out: Pool._managed only ever accepts a terminal with a
            # matching registry row, or a pattern-named bare one in the project.
            continue
        else:
            raise PolicyError(f"Unsupported never_kill rule: {rule!r}")
    return NeverKill(frozenset(states), frozenset(receipts))


def _gates(
    lifecycle: Mapping[str, Any], classes: Mapping[str, TaskClass]
) -> tuple[int | None, frozenset[str], str | None]:
    """The gate cap, the classes it counts and the note that explains it.

    The cap and the class list come together or not at all: a cap with nothing to count,
    or a list with no cap, would look enforced and limit nothing.
    """
    note = lifecycle.get("gate_note")
    if note is not None and not isinstance(note, str):
        raise PolicyError("lifecycle.gate_note must be text")
    cap, listed = lifecycle.get("max_concurrent_gates"), lifecycle.get("gate_classes")
    if cap is None and listed is None:
        return None, frozenset(), note
    if cap is None:
        raise PolicyError("lifecycle.gate_classes needs lifecycle.max_concurrent_gates")
    cap = _positive(cap, "lifecycle.max_concurrent_gates")
    if listed is None or not (names := _strings(listed, "lifecycle.gate_classes")):
        raise PolicyError(
            "lifecycle.max_concurrent_gates needs lifecycle.gate_classes: the classes "
            "whose brief runs a full gate"
        )
    for name in names:
        if name not in classes:
            raise PolicyError(f"lifecycle.gate_classes names an unknown class: {name}")
    return cap, frozenset(names), note


def _pattern(value: Any, fields: tuple[str, ...], where: str) -> str:
    if not isinstance(value, str) or not all("{" + field + "}" in value for field in fields):
        raise PolicyError(f"{where} must contain " + ", ".join("{" + f + "}" for f in fields))
    return value


def _launch(
    name: str, kind: Any, via: str, launch: tuple[str, ...], project: str
) -> tuple[str, ...]:
    """Translate a reviewed policy launch into a package-controlled executable."""
    if via == "direct":
        if not isinstance(kind, str) or launch not in _DIRECT_COMMANDS.get(kind, ()):
            raise PolicyError(
                f"harnesses.{name}.launch_via 'direct' requires a reviewed command and "
                "arguments for its agent_kind"
            )
        return launch
    resource = files("answerback_bridge").joinpath("data", LAUNCH_SCRIPT)
    if not isinstance(resource, Path) or not resource.is_file():
        raise PolicyError("The packaged harness_launch.sh is unavailable")
    pinned = resource.resolve()
    # The legacy absolute project path is an input alias only: never read or execute
    # that repository file. T-35 can move the launcher without trusting policy paths.
    legacy = Path(project) / "tools" / LAUNCH_SCRIPT
    if (
        not Path(launch[0]).is_absolute()
        or Path(launch[0]) not in (legacy, pinned)
        or not isinstance(kind, str)
        or kind not in _SCRIPT_ARGUMENTS
        or len(launch) < 2
        or launch[1] != kind
        or launch[2:] not in _SCRIPT_ARGUMENTS[kind]
    ):
        raise PolicyError(
            f"harnesses.{name}.launch requires the absolute packaged harness_launch.sh "
            "(or its project/tools legacy path), the matching tool name and reviewed arguments"
        )
    return (str(pinned), *launch[1:])


def parse_policy(raw: Any) -> Policy:
    data = _mapping(raw, "Routing policy")
    if data.get("version") != 1:
        raise PolicyError("Routing policy version must be 1")
    project = data.get("project")
    if not isinstance(project, str) or not Path(project).is_absolute():
        raise PolicyError("project must be an absolute path")
    space = data.get("project_space")
    if space is not None and (not isinstance(space, str) or not space):
        raise PolicyError("project_space must be a nonempty string when present")
    lifecycle = _mapping(data.get("lifecycle"), "lifecycle")
    strange = sorted(set(lifecycle) - _LIFECYCLE_ENFORCED - _LIFECYCLE_PROSE)
    if strange:
        raise PolicyError(f"Unsupported lifecycle keys (nothing would enforce them): {strange}")
    if lifecycle.get("prime_text") is not None:
        raise PolicyError(
            "lifecycle.prime_text is set, but a primed worker reports done and is "
            "undeliverable (T-13); workers launch bare"
        )
    if lifecycle.get("teardown_after") != TEARDOWN_AFTER:
        raise PolicyError(f"lifecycle.teardown_after must be {TEARDOWN_AFTER!r}")
    harnesses: dict[str, Harness] = {}
    for order, (name, entry) in enumerate(_mapping(data.get("harnesses"), "harnesses").items()):
        entry = _mapping(entry, f"harnesses.{name}")
        if entry.get("accepts_initial_prompt"):
            raise PolicyError(
                f"harnesses.{name}.accepts_initial_prompt is true, but an initial prompt "
                "leaves the worker undeliverable (T-13)"
            )
        provider = entry.get("provider")
        if not isinstance(provider, str) or not provider:
            raise PolicyError(f"harnesses.{name}.provider is required")
        cost = entry.get("seat_cost")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int | float)):
            raise PolicyError(f"harnesses.{name}.seat_cost must be a number")
        kind = entry.get("agent_kind")
        launch = _strings(entry.get("launch", []), f"harnesses.{name}.launch")
        via = entry.get("launch_via", "script")
        if via not in ("script", "direct"):
            raise PolicyError(f"harnesses.{name}.launch_via must be 'script' or 'direct'")
        if provider == "hc" and launch:
            launch = _launch(name, kind, via, launch, project)
        harnesses[name] = Harness(
            name=name,
            provider=provider,
            agent_kind=kind if isinstance(kind, str) and kind else None,
            launch=launch,
            model=str(entry.get("model", "harness default")),
            max_workers=_positive(entry.get("max_workers"), f"harnesses.{name}.max_workers"),
            seat_cost=cost,
            order=order,
        )
    classes: dict[str, TaskClass] = {}
    for name, entry in _mapping(data.get("classes"), "classes").items():
        entry = _mapping(entry, f"classes.{name}")
        primary = entry.get("primary")
        if not isinstance(primary, str) or not primary:
            raise PolicyError(f"classes.{name}.primary is required")
        classes[name] = TaskClass(
            name=name,
            primary=primary,
            fallback=_strings(entry.get("fallback", []), f"classes.{name}.fallback"),
            match=_strings(entry.get("match", []), f"classes.{name}.match"),
        )
    tie_breaks = _strings(data.get("tie_breaks"), "tie_breaks")
    unknown = [rule for rule in tie_breaks if rule not in TIE_BREAKS]
    if unknown:
        raise PolicyError(f"Unsupported tie_breaks: {unknown}")
    warm_pool = _mapping(lifecycle.get("warm_pool", {}), "lifecycle.warm_pool")
    for name, count in warm_pool.items():
        if name not in classes:
            raise PolicyError(f"lifecycle.warm_pool names an unknown class: {name}")
        _positive(count, f"lifecycle.warm_pool.{name}", allow_zero=True)
    timeout = _seconds(lifecycle.get("prime_timeout_s"), "prime_timeout_s")
    keep = _seconds(lifecycle.get("keep_idle_min"), "keep_idle_min")
    gate_cap, gate_classes, gate_note = _gates(lifecycle, classes)
    return Policy(
        project=project,
        project_space=space,
        harnesses=harnesses,
        classes=classes,
        tie_breaks=tie_breaks,
        name_pattern=_pattern(lifecycle.get("name_pattern"), _NAME_FIELDS, "name_pattern"),
        request_id_pattern=_pattern(
            lifecycle.get("request_id_pattern"), _REQUEST_FIELDS, "request_id_pattern"
        ),
        prime_timeout_s=timeout,
        keep_idle_min=keep,
        warm_pool=dict(warm_pool),
        max_total=_positive(lifecycle.get("max_total"), "lifecycle.max_total"),
        never_kill=_never_kill(_strings(lifecycle.get("never_kill"), "lifecycle.never_kill")),
        max_concurrent_gates=gate_cap,
        gate_classes=gate_classes,
        gate_note=gate_note,
        unenforced=tuple(sorted((set(lifecycle) & _LIFECYCLE_PROSE) - {"gate_note"})),
    )


def load_policy(path: Path) -> Policy:
    """Read the policy through one descriptor: a regular file, at most 256 KiB and one byte.

    A symlink is refused where the platform has O_NOFOLLOW, and a FIFO or device is refused
    before it is read, so a hostile path cannot make the caller buffer an unbounded read.
    """
    not_regular = PolicyError(f"Routing policy must be a regular non-symlink file: {path}")
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise not_regular from None
        raise PolicyError(f"Routing policy unreadable: {path} ({error.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise not_regular
        raw = os.read(descriptor, MAX_POLICY_BYTES + 1)
    except OSError as error:
        raise PolicyError(f"Routing policy unreadable: {path} ({error.strerror})") from None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_POLICY_BYTES:
        raise PolicyError("Routing policy exceeds 256 KiB")
    try:
        return parse_policy(json.loads(raw))
    except (ValueError, RecursionError) as error:
        if isinstance(error, PolicyError):
            raise
        raise PolicyError(f"Routing policy is not valid JSON: {path}") from None


def classify(policy: Policy, text: str, override: str | None = None) -> Classification:
    """Keyword classification from the policy's match lists. No model is consulted."""
    if override is not None:
        if override not in policy.classes:
            raise PolicyError(
                f"Unknown class {override!r}; the policy has: {', '.join(policy.classes)}"
            )
        return Classification(override, "override", (), {}, (override,))
    counts = policy._matcher.counts(text)
    if counts is None:
        return Classification(None, "keyword", (), {}, ())
    scores: dict[str, int] = {}
    hits: dict[str, int] = {}
    matched: dict[str, tuple[str, ...]] = {}
    for name, task_class in policy.classes.items():
        found = tuple(phrase for phrase in dict.fromkeys(task_class.match) if counts.get(phrase))
        if found:
            scores[name] = len(found)
            hits[name] = sum(counts[phrase] for phrase in found)
            matched[name] = found
    if not scores:
        return Classification(None, "keyword", (), {}, ())
    best = max((scores[name], hits[name]) for name in scores)
    top = tuple(name for name in scores if (scores[name], hits[name]) == best)
    if len(top) > 1:
        return Classification(None, "keyword", (), scores, top)
    return Classification(top[0], "keyword", matched[top[0]], scores, top)
