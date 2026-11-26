"""The fleet writer's children: ``hc ls --json`` and the placement probes (effects).

Every child is started through one runner that the caller passes in. The default runner
reads a bounded amount of output and always ends and reaps the child's process group.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial

from ..placement.probe import LOCAL, Reading, read
from ..process import spawn_owned, stop_child
from ..providers.herdr.core import Core
from . import advice, bounds
from .agents import Terminal

Runner = Callable[[list[str]], Awaitable[tuple[int, str | bytes]]]
OUTPUT_LIMIT = 1024 * 1024


async def run_bounded(
    argv: list[str], *, env: dict[str, str], limit: int = OUTPUT_LIMIT
) -> tuple[int, bytes]:
    """Run one owned child; read at most ``limit`` bytes; always end and reap it."""
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
        while len(output) < limit:
            chunk = await proc.stdout.read(min(65536, limit - len(output)))
            if not chunk:
                return await proc.wait(), bytes(output)
            output.extend(chunk)
        return 1, b""
    finally:
        # Keep reading while the child is ended: a full pipe would hide its end of file.
        drain = asyncio.create_task(_discard(proc.stdout))
        try:
            await stop_child(proc, cleanup_seconds=2)
        finally:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain


async def _discard(stream: asyncio.StreamReader | None) -> None:
    while stream is not None and await stream.read(65536):
        pass


def default_runner(hc_binary: str, hc_session: str | None) -> tuple[Runner, str]:
    """The real runner with the core's environment, and the ``hc`` binary to call."""
    core = Core(hc_binary, hc_session)
    return partial(run_bounded, env=core._env()), core.binary


class ListingError(Exception):
    """An answer of ``hc ls`` that cannot be used; its text is made here, on purpose."""


@dataclass
class Listing:
    """What one ``hc ls --json`` said."""

    machines: list[tuple[str, str]] = field(default_factory=list)
    addresses: frozenset[str] = frozenset()
    terminals: dict[str, Terminal] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)


def parse_listing(raw: str | bytes) -> Listing:
    """Parse ``hc ls --json``; raise ``ListingError`` when the answer cannot be used."""
    if isinstance(raw, bytes):
        if len(raw) > OUTPUT_LIMIT:
            raise ListingError("hc ls answer too large")
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ListingError("hc ls answer is not JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("machines"), list):
        raise ListingError("hc ls answer has no machines list")
    if not isinstance(data.get("terminals"), list):
        # A missing list of terminals is never taken as "no terminal".
        raise ListingError("hc ls answer has no terminals list")
    listing = Listing()
    seen: set[str] = set()
    for row in data["machines"]:
        name = row.get("name") if isinstance(row, dict) else None
        if not bounds.machine_name_ok(name):
            listing.refused.append(bounds.clean(str(name), bounds.NAME) or "?")
            continue
        assert isinstance(name, str)
        if name in seen:
            continue
        seen.add(name)
        state = bounds.clean(row.get("state"), bounds.NAME) or "unknown"
        listing.machines.append((name, state))
    for row in data["terminals"]:
        if not isinstance(row, dict) or row.get("state") != "live":
            continue
        address = row.get("address")
        machine = advice.machine_of(address)
        if machine == "" or not isinstance(address, str):
            continue
        status = row.get("status")
        agent = row.get("agent")
        listing.terminals[address] = Terminal(
            machine,
            bounds.clean(status, bounds.NAME) if isinstance(status, str) else None,
            bounds.clean(agent, bounds.NAME) if isinstance(agent, str) else None,
        )
    listing.addresses = frozenset(listing.terminals)
    return listing


async def list_core(
    runner: Runner,
    binary: str,
    *,
    timeout: float,  # noqa: ASYNC109
    label: str | None = None,
) -> Listing:
    """Run ``hc ls --json`` once within ``timeout`` seconds; ``label`` adds ``--label K=V``."""
    argv = [binary, "ls", "--json"] if label is None else [binary, "ls", "--json", "--label", label]
    async with asyncio.timeout(timeout):
        status, output = await runner(argv)
    if status:
        raise ListingError(f"hc ls exited with {status}")
    return parse_listing(output)


async def measure(
    name: str,
    *,
    runner: Runner,
    sent_at: float,
    timeout: float,  # noqa: ASYNC109
    hc_binary: str,
    hc_session: str | None,
) -> Reading:
    """One machine through the placement code's route; ``LOCAL`` is the coordinator."""
    readings = await read(
        [name],
        runner=runner,
        timeout=timeout,
        clock=lambda: sent_at,
        hc_binary=hc_binary,
        hc_session=hc_session,
    )
    return readings.get(name, Reading(name, sent_at, error="unparsed"))


__all__ = ["LOCAL", "Listing", "ListingError", "Runner", "default_runner", "list_core", "measure"]
