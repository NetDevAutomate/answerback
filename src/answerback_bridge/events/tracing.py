"""Detection only: sampled worker evidence becomes metrics and anomaly events.

No terminal writes, receipt mutations or lifecycle actions belong in this module.
"""

import getpass
import math
from dataclasses import dataclass, field
from typing import Any

from . import model
from .hub import EventHub
from .patch import diff, escape
from .producers import Run


@dataclass(frozen=True)
class TraceSettings:
    enabled: bool = True
    stuck_seconds: float = 600
    hung_seconds: float = 3600
    undelivered_seconds: float = 30
    orphan_seconds: float = 30
    cpu_percent: float = 80
    cpu_seconds: float = 300
    rss_bytes: int = 4 * 1024**3
    children: int = 20
    confirm_samples: int = 2
    recovery_samples: int = 2
    max_sample_gap_seconds: float = 45
    timeline_limit: int = 64
    tail_bytes: int = 8192
    source_timeout_seconds: float = 5
    probe_concurrency: int = 8
    human_aliases: tuple[str, ...] = field(default_factory=lambda: (getpass.getuser(),))

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("tracing.enabled must be a boolean")
        integers = {
            "confirm_samples",
            "recovery_samples",
            "timeline_limit",
            "tail_bytes",
            "rss_bytes",
            "children",
            "probe_concurrency",
        }
        for key, value in vars(self).items():
            if key in {"enabled", "human_aliases"}:
                continue
            if (
                type(value) not in (int, float)
                or value > 10**15
                or not math.isfinite(value)
                or value <= 0
                or (key in integers and type(value) is not int)
            ):
                raise ValueError(f"tracing.{key} must be a positive finite number")
        if not 2 <= self.confirm_samples <= 100 or not 1 <= self.recovery_samples <= 100:
            raise ValueError("tracing sample counts must be bounded (confirmation minimum 2)")
        if not 1 <= self.timeline_limit <= 1000 or not 256 <= self.tail_bytes <= 65536:
            raise ValueError("tracing timeline or tail bound is out of range")
        if not 1 <= self.probe_concurrency <= 32 or self.source_timeout_seconds > 60:
            raise ValueError("tracing probe concurrency or timeout is out of range")
        if (
            not isinstance(self.human_aliases, (list, tuple))
            or len(self.human_aliases) > 16
            or any(not isinstance(v, str) or not v for v in self.human_aliases)
        ):
            raise ValueError("tracing.human_aliases must be a list of human aliases")


@dataclass
class Guard:
    reason: str = ""
    samples: int = 0
    healthy: int = 0
    active: bool = False
    since: float = 0


class Tracer:
    def __init__(self, settings: TraceSettings) -> None:
        self.settings = settings
        self.guards: dict[tuple[str, str, str], Guard] = {}
        self.baselines: dict[str, float] = {}
        self.submitted: dict[str, float] = {}
        self.last_hung: dict[str, float] = {}
        self.last_sample: dict[str, float] = {}
        self.hashes: dict[str, str | None] = {}
        self.timelines: dict[str, list[dict[str, Any]]] = {}

    def _guard(
        self,
        hub: EventHub,
        peer_id: str,
        run: Run | None,
        reason: str,
        key: str,
        value: float | int | None,
        condition: bool | None,
        now: float,
        *,
        continuous: bool,
        hold_seconds: float = 0,
    ) -> None:
        identity = (peer_id, run.run if run else "", key)
        guard = self.guards.setdefault(identity, Guard(reason=reason, since=now))
        if not continuous:
            guard.healthy = 0
        if not continuous or condition is not True:
            guard.samples = 0
            guard.since = now
        if condition is None:
            guard.healthy = 0  # Unknown is never evidence of recovery.
            return
        if condition:
            guard.healthy = 0
            if guard.samples == 0:
                guard.since = now
            guard.samples += 1
            if (
                guard.active
                or guard.samples < self.settings.confirm_samples
                or now - guard.since < hold_seconds
            ):
                return
            guard.active = True
            threshold = getattr(self.settings, key)
            config_key = f"tracing.{key}"
            event = model.custom(
                "answerback.anomaly",
                {
                    "reason": reason,
                    "peerId": peer_id,
                    "runId": run.run if run else None,
                    "threshold": threshold,
                    "configKey": config_key,
                    "observed": value,
                    "sinceMs": int(guard.since * 1000),
                    "message": (
                        f"{reason}: {peer_id}; observed {value}, threshold {threshold} "
                        f"({config_key}); confirmed by {self.settings.confirm_samples} samples "
                        "(tracing.confirm_samples)"
                        + (
                            f"; sustained {hold_seconds}s (tracing.cpu_seconds)"
                            if reason == "hot" and key == "cpu_percent"
                            else ""
                        )
                    ),
                },
            )
            event["metadata"] = {
                model.NAMESPACE: {
                    "peerId": peer_id,
                    **({"runId": run.run, "threadId": run.thread} if run else {}),
                }
            }
            hub.publish(event)
        else:
            guard.healthy += 1
            # Model text and decision-file content never clear a stuck guard; only run closure does.
            if reason != "stuck" and guard.healthy >= self.settings.recovery_samples:
                guard.active = False

    def observe(
        self,
        hub: EventHub,
        runs: dict[str, Run],
        tails: dict[str, str],
        fresh: set[str],
        now: float,
        *,
        receipts_fresh: bool = True,
    ) -> None:
        live_runs = {ident: run for ident, run in runs.items() if not run.closed}
        self.baselines = {k: v for k, v in self.baselines.items() if k in live_runs}
        self.submitted = {k: v for k, v in self.submitted.items() if k in live_runs}
        self.last_hung = {k: v for k, v in self.last_hung.items() if k in live_runs}
        for ident, run in live_runs.items():
            if run.status == "submitted":
                self.submitted.setdefault(ident, run.updated)
                self.baselines.setdefault(ident, run.updated)
        peers = hub.state["peers"]
        self.guards = {
            key: guard
            for key, guard in self.guards.items()
            if key[1] in live_runs or (not key[1] and key[0] in peers)
        }
        for run in live_runs.values():
            last = self.last_hung.get(run.run)
            if last is not None and now <= last:
                continue
            age = now - self.submitted.get(run.run, run.created)
            self._guard(
                hub,
                run.peer_id,
                run,
                "hung",
                "hung_seconds",
                age,
                run.status == "submitted" and age > self.settings.hung_seconds
                if receipts_fresh
                else None,
                now,
                continuous=last is not None and now - last <= self.settings.max_sample_gap_seconds,
            )
            self.last_hung[run.run] = now
        for mapping in (self.timelines, self.last_sample, self.hashes):
            for ident in set(mapping) - set(peers):
                del mapping[ident]
        for peer_id, peer in peers.items():
            last = self.last_sample.get(peer_id)
            if last is not None and now <= last:
                continue
            continuous = last is not None and now - last <= self.settings.max_sample_gap_seconds
            valid = peer_id in fresh
            state = peer["state"] if valid else "unknown"
            timeline = self.timelines.setdefault(peer_id, [])
            if not timeline or timeline[-1]["state"] != state:
                timeline.append({"state": state, "sinceMs": int(now * 1000)})
                del timeline[: -self.settings.timeline_limit]
            digest = tails.get(peer_id)
            stable = digest is not None and self.hashes.get(peer_id) in (None, digest)
            assigned = [run for run in live_runs.values() if run.peer_id == peer_id]
            for run in assigned:
                baseline = self.baselines.get(run.run, run.created)
                age = now - baseline
                condition = (
                    (
                        run.status == "submitted"
                        and state == "working"
                        and stable
                        and age > self.settings.stuck_seconds
                    )
                    if valid and digest is not None
                    else None
                )
                self._guard(
                    hub,
                    peer_id,
                    run,
                    "stuck",
                    "stuck_seconds",
                    age,
                    condition,
                    now,
                    continuous=continuous,
                )
                self._guard(
                    hub,
                    peer_id,
                    run,
                    "undelivered",
                    "undelivered_seconds",
                    now - run.created,
                    run.status in {"pending", "submitted"}
                    and state in {"idle", "done"}
                    and now - run.created > self.settings.undelivered_seconds
                    if valid
                    else None,
                    now,
                    continuous=continuous,
                )
            self._guard(
                hub,
                peer_id,
                None,
                "orphan",
                "orphan_seconds",
                now - timeline[-1]["sinceMs"] / 1000,
                state == "working" and not assigned if valid else None,
                now,
                continuous=continuous,
                hold_seconds=self.settings.orphan_seconds,
            )
            for metric, key, duration in (
                ("cpuPercent", "cpu_percent", self.settings.cpu_seconds),
                ("rssBytes", "rss_bytes", 0),
                ("children", "children", 0),
            ):
                value = peer["metrics"].get(metric)
                self._guard(
                    hub,
                    peer_id,
                    assigned[-1] if assigned else None,
                    "hot",
                    key,
                    value,
                    value > getattr(self.settings, key) if valid and value is not None else None,
                    now,
                    continuous=continuous,
                    hold_seconds=duration,
                )
            trace = {
                "state": state,
                "sinceMs": timeline[-1]["sinceMs"],
                "timeline": list(timeline),
                "activityHash": digest,
                "sampledMs": int(now * 1000),
                "fresh": valid,
                "anomalies": sorted(
                    {
                        guard.reason
                        for key, guard in self.guards.items()
                        if key[0] == peer_id and guard.active
                    }
                ),
            }
            path = f"/peers/{escape(peer_id)}/trace"
            hub.patch_state(
                diff(peer["trace"], trace, path)
                if "trace" in peer
                else [{"op": "add", "path": path, "value": trace}]
            )
            self.last_sample[peer_id] = now
            self.hashes[peer_id] = digest
