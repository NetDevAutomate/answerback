"""Best-effort telemetry from the already configured hc and Crew read interfaces."""

import asyncio
from typing import Any

from .hub import EMPTY_METRICS
from .resources import ProcessSampler, crew_tokens, optional_psutil, read_ps, read_psutil
from .tracing import TraceSettings


class LiveResources:
    def __init__(self, bridge: Any, settings: TraceSettings) -> None:
        self.bridge = bridge
        self.settings = settings
        self.sampler = ProcessSampler(
            timeout=settings.source_timeout_seconds, max_gap=settings.max_sample_gap_seconds
        )
        self.crew_details: dict[str, dict[str, Any]] = {}
        self.debug_sampler: ProcessSampler | None = None

    async def __call__(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        self.crew_details.clear()
        semaphore = asyncio.Semaphore(self.settings.probe_concurrency)

        async def identify(row: dict[str, Any]) -> tuple[str, int | None, int | None]:
            ident = row["peer_id"]
            provider, _, target = ident.partition(":")
            pid, tokens = None, None
            try:
                async with semaphore, asyncio.timeout(self.settings.source_timeout_seconds):
                    if provider == "hc" and self.bridge.config.provider_enabled("hc"):
                        pid = await self.bridge.core.trace_pid(target, row.get("binding"))
                    elif provider == "crew" and self.bridge.config.provider_enabled("crew"):
                        detail = await self.bridge.crew_read(target)
                        self.crew_details[ident] = detail
                        tokens = crew_tokens(detail.get("turn_stats"))
                        # Only an explicitly chat-scoped process is attributable. Do not
                        # substitute the gateway parent's PID and charge every worker to it.
                        process = detail.get("process")
                        if isinstance(process, dict):
                            candidate = process.get("pid")
                            if type(candidate) is int and candidate > 0:
                                pid = candidate
            except Exception:
                pass  # Optional or unavailable metadata stays null, without warnings.
            return ident, pid, tokens

        identities = await asyncio.gather(*(identify(row) for row in rows))
        debug = getattr(self.bridge, "debug", None)
        debug_roots = {
            pid
            for ident, pid, _ in identities
            if pid and debug is not None and debug.until(ident) is not None
        }
        normal_roots = {
            pid
            for ident, pid, _ in identities
            if pid and (debug is None or debug.until(ident) is None)
        }
        if debug_roots and self.debug_sampler is None:
            module = optional_psutil()
            self.debug_sampler = ProcessSampler(
                (lambda: read_psutil(module)) if module is not None else read_ps,
                timeout=self.settings.source_timeout_seconds,
                max_gap=self.settings.max_sample_gap_seconds,
            )
        measured: dict[int, Any] = {}
        for sampler, roots in ((self.sampler, normal_roots), (self.debug_sampler, debug_roots)):
            if sampler is None or (not roots and (normal_roots or debug_roots)):
                continue
            try:
                measured.update(await sampler.sample(roots))
            except Exception:
                pass  # Failed/optional telemetry remains null.
        return {
            ident: {**EMPTY_METRICS, **measured.get(pid, {}), "tokens": tokens}
            for ident, pid, tokens in identities
        }
