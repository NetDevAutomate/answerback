"""Machine facts for placement: the `placement` block of the routing policy file.

A person records the facts; nothing here measures, probes or writes. `routing.policy`
reads the same file and ignores this block, and this module ignores every other key
except the names under `harnesses`. A key or a value this reader does not understand
is an error: a ceiling that is silently ignored limits nothing.

The root of the policy file must be an object; any other root is refused. A root
without the key `placement` gives the defaults and no machine. The block must be an
object too. It has these keys, all optional, and none of the four may be null; any other
key is refused at each of the three levels (the block, a machine, a harness of a
machine). "Whole number" means a JSON integer: not true or false, and not 2.0. "Number"
means a JSON integer or float, not true or false, finite, and not too large for a float.

- `machines`: an object of at most 16 machines, counting `local` whether it is written
  or not; absent means no machine. A key is a machine name: 1 to 63 of the ASCII letters,
  digits, `.`, `_` and `-`, the first a letter or digit. A machine is an object of:
  `project`, an absolute POSIX path (it starts with `/`) of printable characters, not
  `/` itself and with no `..` part;
  `max_total`, a whole number from 1 to 10000; `tools`, a list of nonempty strings;
  `harnesses`, an object whose keys are among the keys of the policy file's own
  top-level `harnesses` (when that is absent or not an object, no key is taken). A
  harness there
  is an object of: `ready`, required, true or false; `max_workers`, a whole number from
  1 to 10000; `why`, a nonempty string. The upper bound 10000 of the two ceilings
  (`MAX_CEILING`) is the writer's number: no machine of this fleet comes near it.
  `project`, `max_total`, `max_workers` and `why` may be null, which reads as absent: no
  project folder, no ceiling, no reason. Absent `tools` and `harnesses` read as none.
- `order`: a list of machine names, each once, each a key of `machines`; absent means
  empty.
- `thresholds`: an object of overrides; absent means none. Its keys are the fields of
  `assess.Thresholds` and no other: `cpu_busy`, `cpu_full`, `compressor_busy`,
  `compressor_full`, `swap_busy`, `available_busy`, `available_full`, `stall_busy`,
  `largest_process_busy`, `largest_process_full` and `max_age`. A key that is not
  written keeps its default, and the defaults are written in one place only, the class
  `assess.Thresholds`. Each override is a number above 0. The seven keys other than
  `cpu_busy`, `cpu_full`, `stall_busy` and `max_age` are shares of memory and at most 1.
  `max_age` is at most 600. With the overrides applied to the defaults, `cpu_busy` is
  below `cpu_full`, `compressor_busy` below `compressor_full`, `largest_process_busy`
  below `largest_process_full`, and `available_full` below `available_busy`.
- `worker_cost`: a number from 0, default 1.0: the load one recent placement adds. With
  0 a recent placement adds no load and still counts in the order of `choose`.

A key that is not text, which JSON cannot give and a Python caller can, is refused by
its type at every level of the block (the block, `thresholds`, `machines`, a machine, its
`harnesses`, a harness) and is never made text. Every refusal by `parse_facts` and
`load_facts` is a `FactsError`, for a value built of plain dicts, lists, strings, numbers,
true, false and null, whatever plain value a dict has as a key. Where a message lists
names from the file it shows at most eight of them, and every name from the file in a
message is cut at 64 characters.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .assess import Thresholds
from .probe import LOCAL, validate_machines

BLOCK = "placement"
MAX_POLICY_BYTES = 256 * 1024
MAX_JSON_DEPTH = 64
MAX_MACHINES = 16
MAX_CEILING = 10_000
_BLOCK_KEYS = frozenset({"order", "thresholds", "worker_cost", "machines"})
_MACHINE_KEYS = frozenset({"project", "max_total", "tools", "harnesses"})
_HARNESS_KEYS = frozenset({"ready", "max_workers", "why"})
_THRESHOLDS = tuple(item.name for item in fields(Thresholds))
MAX_AGE_LIMIT = 600.0
# (lower, upper): the first must be below the second.
_ORDERED = (
    ("cpu_busy", "cpu_full"),
    ("compressor_busy", "compressor_full"),
    ("largest_process_busy", "largest_process_full"),
    ("available_full", "available_busy"),
)
# Shares of the machine's memory: above 1 the signal could never fire.
_SHARES = frozenset(_THRESHOLDS) - {"cpu_busy", "cpu_full", "stall_busy", "max_age"}
_EMPTY: Mapping[str, Any] = MappingProxyType({})
_SHOWN_NAMES = 8
_SHOWN_CHARS = 64


class FactsError(ValueError):
    """The placement block is malformed or holds something this reader does not apply."""


class FactsUnreadable(FactsError):
    """The policy file could not be read, or is too large to read."""


def _check_json_depth(raw: bytes) -> None:
    """Refuse JSON nesting beyond the fixed bound without recursion.

    This lexical pass does not validate JSON. It only counts object and array openers
    outside strings; `json.loads` remains the syntax parser. `raw` has already passed
    the file-size bound, so this loop cannot read or allocate beyond that input.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise FactsError(
                    f"Routing policy is too deeply nested (maximum JSON depth {MAX_JSON_DEPTH})"
                )
        elif byte in (0x5D, 0x7D) and depth:  # ] }
            depth -= 1


@dataclass(frozen=True)
class HarnessFacts:
    """Whether a person recorded one harness as ready on one machine, and its ceiling."""

    ready: bool
    max_workers: int | None = None
    why: str | None = None


@dataclass(frozen=True)
class MachineFacts:
    """What the policy file records about one machine; an absent fact stays None or empty."""

    name: str
    project: str | None = None
    max_total: int | None = None
    tools: frozenset[str] = frozenset()
    harnesses: Mapping[str, HarnessFacts] = field(default_factory=lambda: _EMPTY)


@dataclass(frozen=True)
class Facts:
    """The whole block. Without a block: the defaults and no machine.

    The coordinator is not a fact of the file: it is the machine the probe names `local`.
    `parse_facts` gives read-only mappings of its own; a `Facts` or `MachineFacts` built
    directly keeps the mapping it is given, and nothing checks its values, except that
    `choose` refuses a `worker_cost` this reader would refuse.
    """

    order: tuple[str, ...] = ()
    thresholds: Thresholds = Thresholds()
    worker_cost: float = 1.0
    machines: Mapping[str, MachineFacts] = field(default_factory=lambda: _EMPTY)


def _short(name: str) -> str:
    return name if len(name) <= _SHOWN_CHARS else name[:_SHOWN_CHARS] + "..."


def _listed(names: list[str]) -> str:
    """At most eight names, each cut, and the count of the rest."""
    shown = ", ".join(repr(_short(name)) for name in names[:_SHOWN_NAMES])
    rest = len(names) - _SHOWN_NAMES
    return shown + (f" and {rest} more" if rest > 0 else "")


def _mapping(value: Any, where: str, allowed: frozenset[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FactsError(f"{where} must be an object")
    for key in value:
        if not isinstance(key, str):
            raise FactsError(f"{where} has a key that is not text: {type(key).__name__}")
    if allowed is not None and (strange := sorted(set(value) - allowed)):
        raise FactsError(
            f"{where} has unsupported keys (nothing would apply them): {_listed(strange)}"
        )
    return value


class NumberRefused(Exception):
    """`finite_from_zero` refused a value. Its text is fixed and holds no value.

    Not a TypeError, a ValueError or a FactsError. The two callers of the rule catch this
    class alone.
    """

    def __init__(self, not_a_number: bool) -> None:
        super().__init__("not a number" if not_a_number else "not finite, or negative")
        self.not_a_number = not_a_number


def finite_from_zero(value: Any) -> float:
    """The float of `value` when it is a finite number from 0; the one statement of that rule.

    The reader applies it to every number of the block, and `choose` to the `worker_cost`
    of the facts it is given. Raises NumberRefused, and only for a refusal: with
    `not_a_number` true for true, false and a value that is neither an int nor a float;
    with it false for not-a-number, an infinity, a negative number and an int too large
    for a float. That error holds no value. An error that the conversion of an instance
    of a subclass of int or float raises is not a refusal and passes through unchanged,
    except an OverflowError, which is read as a number too large for a float: a refusal.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NumberRefused(True)
    try:
        number = float(value)
    except OverflowError:
        number = math.inf
    if not math.isfinite(number) or number < 0:
        raise NumberRefused(False)
    return number


def _number(value: Any, where: str) -> float:
    try:
        return finite_from_zero(value)
    except NumberRefused as refused:
        reason = "must be a number" if refused.not_a_number else "must be finite and not negative"
        raise FactsError(f"{where} {reason}") from None


def _ceiling(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_CEILING:
        raise FactsError(f"{where} must be a positive int, at most {MAX_CEILING}")
    return value


def _name(value: Any, where: str) -> str:
    try:
        return validate_machines([value])[0]
    except ValueError:
        raise FactsError(
            f"{where} must be a machine name (1-63 of A-Z a-z 0-9 . _ -, starting with a "
            "letter or digit)"
        ) from None


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise FactsError(f"{where} must be a nonempty string")
    return value


def _thresholds(value: Any) -> Thresholds:
    overrides = _mapping(value, "placement.thresholds")
    if strange := sorted(set(overrides) - set(_THRESHOLDS)):
        raise FactsError(
            f"placement.thresholds names no threshold of the measurement: {_listed(strange)}; "
            f"it has: {', '.join(_THRESHOLDS)}"
        )
    result = replace(
        Thresholds(),
        **{name: _number(item, f"placement.thresholds.{name}") for name, item in overrides.items()},
    )
    for name in overrides:
        value = getattr(result, name)
        if value == 0:
            raise FactsError(f"placement.thresholds.{name} must be above 0")
        if name in _SHARES and value > 1:
            raise FactsError(f"placement.thresholds.{name} is a share and must not be above 1")
    if result.max_age > MAX_AGE_LIMIT:
        raise FactsError(f"placement.thresholds.max_age must not be above {MAX_AGE_LIMIT:g}")
    for lower, upper in _ORDERED:
        if getattr(result, lower) >= getattr(result, upper):
            raise FactsError(f"placement.thresholds: {lower} must be below {upper}")
    return result


def _harness(value: Any, where: str) -> HarnessFacts:
    entry = _mapping(value, where, _HARNESS_KEYS)
    if type(entry.get("ready")) is not bool:
        raise FactsError(f"{where}.ready is required and must be true or false")
    why = entry.get("why")
    return HarnessFacts(
        ready=entry["ready"],
        max_workers=_ceiling(entry.get("max_workers"), f"{where}.max_workers"),
        why=None if why is None else _text(why, f"{where}.why"),
    )


def _machine(name: str, value: Any, known_harnesses: frozenset[str]) -> MachineFacts:
    where = f"placement.machines.{name}"
    entry = _mapping(value, where, _MACHINE_KEYS)
    project = entry.get("project")
    if project is not None and (
        not isinstance(project, str)
        or not PurePosixPath(project).is_absolute()
        or not project.isprintable()
    ):
        raise FactsError(f"{where}.project must be an absolute path of printable characters")
    if project is not None and (
        len(PurePosixPath(project).parts) < 2 or ".." in PurePosixPath(project).parts
    ):
        # `/` would make a project root hold every absolute directory, and a `..`
        # part names a folder this reader cannot resolve on the other machine.
        raise FactsError(f"{where}.project must name a folder below `/` without `..` parts")
    tools = entry.get("tools", [])
    if not isinstance(tools, list):
        raise FactsError(f"{where}.tools must be a list of nonempty strings")
    harnesses = _mapping(entry.get("harnesses", {}), f"{where}.harnesses")
    if strange := sorted(set(harnesses) - known_harnesses):
        raise FactsError(
            f"{where}.harnesses names no harness of this policy file: {_listed(strange)}"
        )
    return MachineFacts(
        name=name,
        project=project,
        max_total=_ceiling(entry.get("max_total"), f"{where}.max_total"),
        tools=frozenset(_text(tool, f"{where}.tools") for tool in tools),
        harnesses=MappingProxyType(
            {
                key: _harness(item, f"{where}.harnesses.{_short(key)}")
                for key, item in harnesses.items()
            }
        ),
    )


def parse_facts(raw: Any) -> Facts:
    """Read the placement block of a parsed policy file; no block gives the defaults.

    Raises FactsError when `raw` is not an object or the block is not as the module's
    docstring states it.
    """
    if not isinstance(raw, dict):
        raise FactsError("Routing policy must be an object")
    data: dict[Any, Any] = raw
    if BLOCK not in data:
        return Facts()
    block = _mapping(data[BLOCK], BLOCK, _BLOCK_KEYS)
    declared = data.get("harnesses")
    known = frozenset(declared) if isinstance(declared, dict) else frozenset()
    entries = _mapping(block.get("machines", {}), "placement.machines")
    if len({LOCAL, *entries}) > MAX_MACHINES:
        raise FactsError(
            f"placement.machines holds more than {MAX_MACHINES} machines, counting {LOCAL}"
        )
    machines = {
        _name(name, "placement.machines key"): _machine(name, entry, known)
        for name, entry in entries.items()
    }
    order = block.get("order", [])
    if not isinstance(order, list) or not all(isinstance(name, str) for name in order):
        raise FactsError("placement.order must be a list of machine names")
    if len(set(order)) != len(order):
        raise FactsError("placement.order names a machine twice")
    if strange := [name for name in order if name not in machines]:
        raise FactsError(f"placement.order names machines without facts: {_listed(strange)}")
    return Facts(
        order=tuple(order),
        thresholds=_thresholds(block.get("thresholds", {})),
        worker_cost=_number(block.get("worker_cost", 1.0), "placement.worker_cost"),
        machines=MappingProxyType(machines),
    )


def load_facts(path: Path) -> Facts:
    """Read the placement block from a policy file on disk.

    Reads at most 256 KiB and one byte, and refuses a file of more than 256 KiB. Raises
    FactsError for a file that cannot be read, is too large, is nested beyond
    `MAX_JSON_DEPTH`, is not JSON, or holds a block `parse_facts` refuses.
    """
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_POLICY_BYTES + 1)
    except (OSError, ValueError) as error:
        why = error.strerror if isinstance(error, OSError) else str(error)
        raise FactsUnreadable(f"Routing policy unreadable: {path!r} ({why})") from None
    if len(raw) > MAX_POLICY_BYTES:
        raise FactsUnreadable("Routing policy exceeds 256 KiB")
    try:
        _check_json_depth(raw)
        return parse_facts(json.loads(raw))
    except (ValueError, RecursionError) as error:
        if isinstance(error, FactsError):
            raise
        raise FactsError(f"Routing policy is not valid JSON: {path!r}") from None
