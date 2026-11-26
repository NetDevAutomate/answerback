"""Bounded, read-only machine measurements through local sh or Herdr Core."""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any, Literal

from ..process import BridgeError, spawn_owned, stop_child
from ..providers.herdr.core import Core

PROBE_SCRIPT = 'echo "os=$(uname -s)"; echo "cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null)"; if [ -r /proc/loadavg ]; then echo "load=$(cut -d" " -f1-3 /proc/loadavg)"; else echo "load=$(sysctl -n vm.loadavg | tr -d "{}")"; fi; ps -Ao rss=,comm= 2>/dev/null | awk \'{r=$1; $1=""; n=split($0,p,"/"); name=p[n]; sub(/^ +/,"",name); c++; if (r+0>max) {max=r+0; big=name} if (name=="claude"||name=="codex"||name=="kiro-cli"||name=="grok") h++} END {print "procs=" c; print "largest_kb=" max; print "largest_name=" big; print "harness_procs=" h+0}\'; echo "suites=$(ps -Ao args= 2>/dev/null | grep -c "[b]in/pytest")"; if [ "$(uname -s)" = Darwin ]; then echo "mem_total_kb=$(( $(sysctl -n hw.memsize) / 1024 ))"; echo "pressure=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)"; echo "mem_level=$(sysctl -n kern.memorystatus_level 2>/dev/null)"; echo "swap=$(sysctl -n vm.swapusage 2>/dev/null)"; vm_stat 2>/dev/null | awk \'/page size of/ {print "page_bytes=" $8} /occupied by compressor/ {gsub(/\\./,"",$5); print "compressor_pages=" $5}\'; else awk \'/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {gsub(":","",$1); print tolower($1) "_kb=" $2}\' /proc/meminfo; if [ -r /proc/pressure/memory ]; then awk \'/^some/ {split($2,a,"="); print "psi_some_avg10=" a[2]}\' /proc/pressure/memory; fi; fi; echo "probe_end=1"\n'  # noqa: E501

READ_KEYS = frozenset(
    "os cores load procs largest_kb largest_name harness_procs suites mem_total_kb pressure "
    "mem_level swap page_bytes compressor_pages memtotal_kb memavailable_kb swaptotal_kb "
    "swapfree_kb psi_some_avg10 probe_end".split()
)
DARWIN_MEMORY_KEYS = frozenset(
    "mem_total_kb pressure mem_level swap page_bytes compressor_pages".split()
)
LINUX_MEMORY_KEYS = frozenset(
    "memtotal_kb memavailable_kb swaptotal_kb swapfree_kb psi_some_avg10".split()
)

LOCAL = "local"
MAX_BYTES = 64 * 1024
MAX_LINES = 200
MAX_VALUE = 400
MAX_KB = 2**50
_DECIMAL = r"[0-9]+(?:[.,][0-9]+)?"
_MACHINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
Runner = Callable[[list[str]], Awaitable[tuple[int, str | bytes]]]


@dataclass(frozen=True)
class Reading:
    """A measured snapshot; absent or refused fields remain unknown."""

    machine: str
    taken_at: float
    os: str | None = None
    cores: int | None = None
    load1: float | None = None
    load5: float | None = None
    load15: float | None = None
    mem_total_kb: int | None = None
    pressure_level: int | None = None
    mem_level: int | None = None
    compressor_kb: int | None = None
    swap_used_kb: int | None = None
    mem_available_kb: int | None = None
    swap_total_kb: int | None = None
    swap_free_kb: int | None = None
    psi_some_avg10: float | None = None
    largest_process_kb: int | None = None
    largest_process_name: str | None = None
    process_count: int | None = None
    harness_processes: int | None = None
    suites_running: int | None = None
    error: Literal["timeout", "unreachable", "unparsed"] | None = None

    def to_json(self) -> dict[str, Any]:
        """Return every field in stable declaration order."""
        return asdict(self)


def _integer(value: str | None, maximum: int = MAX_KB, minimum: int = 0) -> int | None:
    if value is None or re.fullmatch(r"[0-9]+", value) is None:
        return None
    number = int(value)
    return number if minimum <= number <= maximum else None


def _decimal(value: str | None, maximum: float = 100_000) -> float | None:
    if value is None or re.fullmatch(_DECIMAL, value) is None:
        return None
    number = float(value.replace(",", "."))
    return number if 0 <= number <= maximum else None


def clean_process_name(value: str) -> str:
    """Keep a short ASCII label that cannot introduce a line or key=value field."""
    return (
        value
        if len(value) <= 64 and "=" not in value and all(" " <= c <= "~" for c in value)
        else "?"
    )


def parse_reading(machine: str, text: str | bytes, taken_at: float) -> Reading:
    """Parse an untrusted, bounded prefix without raising for any input text."""
    if isinstance(text, str):
        prefix = text[:MAX_BYTES].encode("utf-8", errors="replace")
        truncated = len(text) > MAX_BYTES or len(prefix) > MAX_BYTES
        raw = prefix[:MAX_BYTES]
    elif isinstance(text, bytes):
        truncated = len(text) > MAX_BYTES
        raw = text[:MAX_BYTES]
    else:
        return Reading(machine, taken_at, error="unparsed")
    lines = raw.decode("utf-8", errors="replace").split("\n", MAX_LINES)
    line_count = len(lines) - (lines[-1] == "")
    while lines and lines[-1] in ("", "\r"):
        lines.pop()
    complete = (
        not truncated
        and line_count <= MAX_LINES
        and bool(lines)
        and lines[-1].removesuffix("\r") == "probe_end=1"
    )
    values: dict[str, str] = {}
    seen: set[str] = set()
    repeated_key = False
    misplaced_key = False
    for line in lines[:MAX_LINES]:
        key, separator, value = line.partition("=")
        if not separator or key not in READ_KEYS:
            continue
        misplaced_key |= key != "os" and "os" not in seen
        if key in seen:
            repeated_key = True
            continue
        seen.add(key)
        if key == "largest_name":
            # Remove only the transport's CR; other control characters remain invalid.
            values[key] = clean_process_name(value.removesuffix("\r"))
        elif len(value) <= MAX_VALUE:
            values[key] = value.strip()

    cores = _integer(values.get("cores"), 4096, 1)
    loads = values.get("load", "").split()
    load1, load5, load15 = (
        tuple(_decimal(value) for value in loads) if len(loads) == 3 else (None, None, None)
    )
    page_bytes = _integer(values.get("page_bytes"), minimum=1)
    pages = _integer(values.get("compressor_pages"))
    compressor = (
        pages * page_bytes // 1024 if pages is not None and page_bytes is not None else None
    )
    if compressor is not None and compressor > MAX_KB:
        compressor = None
    swap = None
    used = re.search(rf"(?:^|\s)used\s*=\s*({_DECIMAL})([KMGT])(?:\s|$)", values.get("swap", ""))
    if used:
        amount = _decimal(used[1], MAX_KB)
        if amount is not None:
            size = amount * 1024 ** "KMGT".index(used[2])
            swap = int(size) if size <= MAX_KB else None
    total_key = {"Darwin": "mem_total_kb", "Linux": "memtotal_kb"}.get(values.get("os", ""))
    total = _integer(
        values.get(total_key)
        if total_key
        else values.get("mem_total_kb", values.get("memtotal_kb")),
        minimum=1,
    )
    available = _integer(values.get("memavailable_kb"))
    invalid_memory = total is None or (available is not None and available > total)
    return Reading(
        machine=machine,
        taken_at=taken_at,
        os=values.get("os") if values.get("os") in ("Darwin", "Linux") else None,
        cores=cores,
        load1=load1,
        load5=load5,
        load15=load15,
        mem_total_kb=total,
        pressure_level=_integer(values.get("pressure")),
        mem_level=_integer(values.get("mem_level"), 100),
        compressor_kb=compressor,
        swap_used_kb=swap,
        mem_available_kb=available,
        swap_total_kb=_integer(values.get("swaptotal_kb")),
        swap_free_kb=_integer(values.get("swapfree_kb")),
        psi_some_avg10=_decimal(values.get("psi_some_avg10"), 100),
        largest_process_kb=_integer(values.get("largest_kb")),
        largest_process_name=values.get("largest_name"),
        process_count=_integer(values.get("procs")),
        harness_processes=_integer(values.get("harness_procs")),
        suites_running=_integer(values.get("suites")),
        error="unparsed"
        if not complete
        or repeated_key
        or misplaced_key
        or "os" not in seen
        or (values.get("os") == "Darwin" and bool(seen & LINUX_MEMORY_KEYS))
        or (values.get("os") == "Linux" and bool(seen & DARWIN_MEMORY_KEYS))
        or invalid_memory
        or cores is None
        or None in (load1, load5, load15)
        else None,
    )


def validate_machines(machines: Iterable[str]) -> list[str]:
    """Validate the entire request before any process starts; preserve first order."""
    names: dict[str, None] = {}
    for machine in machines:
        if not isinstance(machine, str) or _MACHINE.fullmatch(machine) is None:
            raise ValueError(
                "Machine names must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ "
                "(1-63 ASCII characters; local is reserved for this machine)"
            )
        names[machine] = None
        if len(names) > 16:
            raise ValueError("At most 16 distinct machines may be measured, including local")
    return list(names)


async def run_probe(argv: list[str], *, env: dict[str, str]) -> tuple[int, bytes]:
    """Read at most 64 KiB from an owned child and always kill/reap its group."""
    proc = await spawn_owned(
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=8192,
        start_new_session=os.name == "posix",
    )
    try:
        assert proc.stdout is not None
        output = bytearray()
        while len(output) < MAX_BYTES:
            chunk = await proc.stdout.read(min(8192, MAX_BYTES - len(output)))
            if not chunk:
                return await proc.wait(), bytes(output)
            output.extend(chunk)
        # A capped response cannot establish a complete snapshot. Stop even a
        # never-ending producer instead of draining arbitrary amounts of output.
        return 1, b""
    finally:
        await stop_child(proc, cleanup_seconds=2)


async def read(
    machines: Iterable[str],
    *,
    runner: Runner | None = None,
    timeout: float = 10.0,  # noqa: ASYNC109 — one deadline per machine, including child cleanup
    clock: Callable[[], float] = time.time,
    hc_binary: str = "hc",
    hc_session: str | None = None,
) -> dict[str, Reading]:
    """Measure up to 16 machines concurrently, each with its own time bound.

    Optional Core configuration uses the same binary and environment resolution
    as existing Core callers. An injected runner receives only the exact argv.
    """
    names = validate_machines(machines)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Probe timeout must be finite and greater than zero")
    core = Core(hc_binary, hc_session)
    execute = runner if runner is not None else partial(run_probe, env=core._env())

    async def sample(machine: str) -> Reading:
        taken_at = clock()
        argv = (
            ["/bin/sh", "-c", PROBE_SCRIPT]
            if machine == LOCAL
            else [
                core.binary,
                "exec",
                "--on",
                machine,
                "--timeout",
                str(math.ceil(timeout)),
                "sh",
                "-c",
                PROBE_SCRIPT,
            ]
        )
        try:
            async with asyncio.timeout(timeout):
                status, output = await execute(argv)
        except TimeoutError:
            return Reading(machine, taken_at, error="timeout")
        except (OSError, BridgeError):
            return Reading(machine, taken_at, error="unreachable")
        if status:
            return Reading(machine, taken_at, error="unreachable")
        return parse_reading(machine, output, taken_at)

    readings = await asyncio.gather(*(sample(name) for name in names))
    return {reading.machine: reading for reading in readings}


class ReadingCache:
    """Small in-process cache; callers supply time when looking up a reading."""

    def __init__(self, max_age: float = 60.0) -> None:
        self.max_age = max_age
        self._readings: dict[str, Reading] = {}

    def put(self, reading: Reading) -> None:
        """Replace the reading for its machine."""
        self._readings[reading.machine] = reading

    def get(self, machine: str, now: float) -> Reading | None:
        """Return only readings no older than max_age."""
        reading = self._readings.get(machine)
        return reading if reading is not None and now - reading.taken_at <= self.max_age else None
