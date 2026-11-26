"""Read-only ps(1) fallback. CPU is a delta of cumulative times, per foreground tree."""

import asyncio
import importlib
import math
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

MAX_PS_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Process:
    parent: int
    seconds: float
    rss: int
    started: str


def process_table(text: str) -> dict[int, Process]:
    """macOS/Linux ps time, rss (KiB), and lstart in the C locale. Reject partial rows."""
    if len(text) > MAX_PS_BYTES:
        raise ValueError("Process table exceeded its bound")
    result: dict[int, Process] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        pid, parent, cpu, rss, started = line.split(maxsplit=4)
        if not re.fullmatch(r"(?:(\d+)-)?\d+:\d+(?::\d+)?(?:\.\d+)?", cpu):
            raise ValueError("Unsupported cumulative CPU time")
        days, _, rest = cpu.partition("-")
        seconds = float(days) * 86400 if rest else 0.0
        parts = (rest or days).split(":")
        subtotal = 0.0
        for part in parts:
            subtotal = subtotal * 60 + float(part)
        seconds += subtotal
        if (
            int(pid) <= 0
            or int(parent) < 0
            or int(rss) < 0
            or not math.isfinite(seconds)
            or len(started.split()) != 5
            or int(pid) in result
        ):
            raise ValueError("Unsupported process metadata")
        result[int(pid)] = Process(int(parent), seconds, int(rss) * 1024, started)
    return result


async def read_ps() -> str:
    def read() -> str:
        # No shell and no timeout= (subprocess.run's timeout kills the child). The async
        # sampler bounds waiting, keeps a single outstanding read, and never signals it.
        completed = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,time=,rss=,lstart="],
            capture_output=True,
            check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        if len(completed.stdout) > MAX_PS_BYTES:
            raise ValueError("Process table exceeded its bound")
        return completed.stdout.decode("ascii", errors="strict")

    return await asyncio.to_thread(read)


def optional_psutil() -> Any:
    """Auto-detect the optional extra without changing the default ps sampler."""
    try:
        return importlib.import_module("psutil")
    except ImportError:
        return None


async def read_psutil(module: Any) -> dict[int, Process]:
    """Read only process metadata; CPU uses the same delta semantics as ps."""

    def read() -> dict[int, Process]:
        result = {}
        for process in module.process_iter(
            attrs=["pid", "ppid", "cpu_times", "memory_info", "create_time"]
        ):
            info = process.info
            cpu, memory = info["cpu_times"], info["memory_info"]
            if any(
                info[key] is None for key in ("ppid", "cpu_times", "memory_info", "create_time")
            ):
                continue  # Permission-denied metadata is unknown.
            result[info["pid"]] = Process(
                info["ppid"], cpu.user + cpu.system, memory.rss, str(info["create_time"])
            )
            if len(result) > 100_000:
                raise ValueError("Process table exceeded its bound")
        return result

    return await asyncio.to_thread(read)


ProcessRead = Callable[[], Awaitable[str | dict[int, Process]]]


class ProcessSampler:
    def __init__(
        self,
        read: ProcessRead = read_ps,
        *,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 5,
        max_gap: float = 45,
    ) -> None:
        self.read = read
        self.clock = clock
        self.timeout = timeout
        self.max_gap = max_gap
        self.previous: dict[int, Process] = {}
        self.at: float | None = None
        self.pending: asyncio.Task[str | dict[int, Process]] | None = None

    async def sample(self, roots: set[int]) -> dict[int, dict[str, Any]]:
        if not roots:
            self.previous, self.at = {}, None
            return {}
        if self.pending is not None:
            # A previous timed-out read must finish before another can start. Its old
            # output is discarded; late output cannot masquerade as a fresh snapshot.
            if not self.pending.done():
                return {}
            try:
                self.pending.result()
            except Exception:
                pass
            self.pending = None

        async def read() -> str | dict[int, Process]:
            return await self.read()

        self.pending = asyncio.create_task(read())
        try:
            text = await asyncio.wait_for(asyncio.shield(self.pending), self.timeout)
        except TimeoutError:
            self.previous, self.at = {}, None
            return {}
        finally:
            if self.pending.done():
                self.pending = None
        table, now = process_table(text) if isinstance(text, str) else text, self.clock()
        elapsed = now - self.at if self.at is not None else 0
        children: dict[int, list[int]] = {}
        for pid, process in table.items():
            children.setdefault(process.parent, []).append(pid)
        result: dict[int, dict[str, Any]] = {}
        for root in roots:
            if root not in table:
                continue
            selected: set[int] = set()
            pending = [root]
            while pending:
                pid = pending.pop()
                if pid not in selected:
                    selected.add(pid)
                    pending.extend(children.get(pid, []))
            comparable = [
                pid
                for pid in selected
                if pid in self.previous
                and table[pid].started == self.previous[pid].started
                and table[pid].seconds >= self.previous[pid].seconds
            ]
            cpu = (
                100 * sum(table[p].seconds - self.previous[p].seconds for p in comparable) / elapsed
                if comparable and 0 < elapsed <= self.max_gap
                else None
            )
            result[root] = {
                "cpuPercent": round(cpu, 2) if cpu is not None else None,
                "rssBytes": sum(table[p].rss for p in selected),
                "children": len(selected) - 1,
            }
        self.previous, self.at = table, now
        return result


def crew_tokens(stats: Any) -> int | None:
    """Best effort over explicit gateway turn_stats; never infer tokens from text."""
    if isinstance(stats, list):
        values = [crew_tokens(row) for row in stats]
        if not values or any(v is None for v in values):
            return None
        return sum(v for v in values if v is not None)
    if not isinstance(stats, dict):
        return None
    total = stats.get("total_tokens")
    if type(total) is int and total >= 0:
        return total
    incoming, outgoing = stats.get("input_tokens"), stats.get("output_tokens")
    if type(incoming) is int and type(outgoing) is int and incoming >= 0 and outgoing >= 0:
        return incoming + outgoing
    return None
