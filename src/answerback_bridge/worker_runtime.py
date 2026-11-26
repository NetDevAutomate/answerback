"""Provider-neutral ACP job, permission, cancellation, and throttle runtime."""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .active_budget import ActiveBudget
from .config import Config, public_task
from .native_protocol import SessionControl
from .pressure import Match, PressureStore
from .pressure.gate import clock_time, held
from .pressure.gate import notice as pressure_notice
from .process import BridgeError
from .tasks import TERMINAL, TaskStore

# Second throttled attempt of one receipt: the class only, never provider text.
THROTTLED_TWICE = "Provider throttled twice; no further relaunch"


@dataclass
class PendingPermission:
    request: dict[str, Any]
    future: asyncio.Future[str | None]
    ordinal: int
    requested_at: float


@dataclass
class Job:
    task: asyncio.Task[None] | None = None
    adapter: SessionControl | None = None
    session_id: str | None = None
    budget: ActiveBudget | None = None
    prompt_sent: bool = False
    pending: dict[str, PendingPermission] = field(default_factory=dict)
    permission_rounds: int = 0
    # Set when the receipt is on its one relaunch after a provider backoff (T-227).
    relaunched: bool = False


class Throttled(Exception):
    """An attempt ended with a provider-throttle signature; no terminal status is written."""

    def __init__(self, match: "Match") -> None:
        super().__init__(match.signature)
        self.match = match


def native_stopped_fact(adapter: SessionControl | None) -> bool | str:
    """Return cleanup's native-process outcome without inferring an unknown state."""
    stopped = getattr(adapter, "native_stopped", None)
    return stopped if type(stopped) is bool else "unknown"


class ACPWorkerRuntime:
    """Shared ACP job control; each provider owns its submission contract."""

    provider_name: str
    # Rate-limit signals go here when the service sets it (T-227); None observes nothing.
    pressure: PressureStore | None = None

    def __init__(
        self,
        config: Config,
        config_path: Path,
        store: TaskStore,
        *,
        pressure: "PressureStore | None" = None,
    ) -> None:
        self.config = config
        self.config_path = config_path.resolve()
        self.store = store
        self.jobs: dict[str, Job] = {}
        self.slots = asyncio.Semaphore(2)
        self.closing = False
        self.pressure = pressure

    async def wait_for_provider(self, task_id: str, provider: str) -> None:
        """Hold a launch while `provider` is under pressure; the receipt shows `queued`.

        Called inside the slot and before any execution budget exists, so the wait
        spends no active time. Nothing runs yet, so nothing can be interrupted.
        """
        if self.pressure is None or (pressure := held(self.pressure, provider)) is None:
            return
        self.store.metadata(task_id, {"provider_backoff_until": pressure.until_wall})
        self.store.update(
            task_id,
            "queued",
            detail=(
                f"provider backoff until {clock_time(pressure.until_wall)} (level {pressure.level})"
            ),
        )
        await self.pressure.wait(provider, pressure.account_hint)

    def prompt_text(self, provider: str, message: str) -> str:
        """What is sent: the pressure notice, when held, then the receipt's own message."""
        if self.pressure is None:
            return message
        return (pressure_notice(self.pressure, provider) or "") + message

    async def settle_throttled(
        self,
        task_id: str,
        job: Job,
        throttled: Throttled,
        *,
        attempt: int,
        interrupted: str,
        relaunch: Callable[[], Awaitable[None]],
        second: str = "failed",
    ) -> None:
        """A throttled attempt writes no terminal status: wait out the pause and relaunch.

        Only once: a second throttled attempt ends `second` (`failed`, or `uncertain` for
        an error after the prompt was sent) with a detail naming the class only.
        """
        if await self.relaunch_after(
            task_id, job, throttled, attempt=attempt, interrupted=interrupted
        ):
            await relaunch()
        else:
            self.store.update(task_id, second, detail=THROTTLED_TWICE)

    async def relaunch_on_error(
        self,
        task_id: str,
        job: Job,
        message: str,
        error: BaseException,
        *,
        attempt: int,
        interrupted: str,
        relaunch: Callable[[], Awaitable[None]],
    ) -> bool:
        """The error seam: True when `error` was a throttle and has been settled here."""
        throttled = self.throttled(job, message, error=error)
        if throttled is None:
            return False
        await self.settle_throttled(
            task_id,
            job,
            throttled,
            attempt=attempt,
            interrupted=interrupted,
            relaunch=relaunch,
            second="uncertain" if job.prompt_sent else "failed",
        )
        return True

    async def relaunch_after(
        self, task_id: str, job: Job, throttled: Throttled, *, attempt: int, interrupted: str
    ) -> bool:
        """After a throttled attempt: wait out the pause once and return True to relaunch.

        False (a second throttle, or no store) leaves the caller's terminal path to run.
        A cancellation during the wait settles `uncertain`, like any interrupted worker.
        """
        if attempt or self.pressure is None:
            return False
        match = throttled.match
        until = self.pressure.status(match.provider, match.account_hint).until_wall
        self.store.metadata(
            task_id, {"resumed_after_backoff": False, "provider_backoff_until": until}
        )
        self.store.update(
            task_id, "queued", detail=f"provider backoff; one relaunch after {clock_time(until)}"
        )
        # The first attempt's adapter is closed: a cancel now takes the pre-prompt path.
        job.adapter, job.session_id, job.relaunched = None, None, True
        try:
            await self.pressure.wait(match.provider, match.account_hint)
        except asyncio.CancelledError:
            self.store.update(task_id, "uncertain", detail=interrupted)
            raise
        return True

    def throttled(
        self, job: Job, message: str, result: Any = None, error: BaseException | None = None
    ) -> Throttled | None:
        """A `Throttled` for a turn or error with a throttle signature, else None.

        Only with a pressure store (nothing to wait on otherwise), and never for this
        budget's own expiry.
        """
        if self.pressure is None:
            return None
        if job.budget is not None and job.budget.expired:
            return None
        match = self.throttle_match(message, result, error)
        return Throttled(match) if match is not None else None

    def throttle_match(
        self, message: str, result: Any, error: BaseException | None
    ) -> "Match | None":
        """This runtime's matcher over a finished turn or the error that ended it."""
        return None

    def mark_resumed(self, task_id: str, job: Job) -> None:
        if job.relaunched:
            self.store.metadata(task_id, {"resumed_after_backoff": True})

    def callbacks(self, task_id: str, job: Job) -> tuple[Any, Any]:
        async def update(event: dict[str, Any]) -> None:
            # Keep a bounded progress observation, never operational-log prompt/result bodies.
            kind = event.get("sessionUpdate", event.get("session_update", "unknown"))
            self.store.metadata(task_id, {"last_update": kind})

        async def permission(request: dict[str, Any]) -> str | None:
            if len(json.dumps(request)) > 32_000 or len(job.pending) >= 8:
                raise BridgeError("Permission request exceeds the supported bounds")
            permission_id = str(uuid.uuid4())
            pending = PendingPermission(
                request=request,
                future=asyncio.get_running_loop().create_future(),
                ordinal=job.permission_rounds + 1,
                requested_at=time.time(),
            )
            job.pending[permission_id] = pending
            if job.budget is not None:
                job.budget.pause()
            try:
                self.store.update(task_id, "blocked", detail="Awaiting a human permission decision")
            except Exception:
                # Unwind exactly what was set up above before this call raised; the
                # existing finally block below assumes it never ran otherwise.
                job.pending.pop(permission_id, None)
                if job.budget is not None:
                    job.budget.resume()
                raise
            job.permission_rounds = pending.ordinal
            try:
                return await pending.future
            finally:
                job.pending.pop(permission_id, None)
                if job.budget is not None:
                    job.budget.resume()
                if not job.pending and self.store.get(task_id)["status"] == "blocked":
                    self.store.update(task_id, "running")

        return update, permission

    def permissions(self, task_id: str) -> dict[str, Any]:
        self.store.get(task_id)
        job = self.jobs.get(task_id)
        return {
            "task_id": task_id,
            "permissions": [
                {"id": key, **pending.request}
                for key, pending in (job.pending.items() if job else [])
            ],
        }

    def _resolve_permission(
        self,
        task_id: str,
        permission_id: str,
        pending: PendingPermission,
        option_id: str,
    ) -> None:
        tool_call = pending.request.get("tool_call")
        self.store.record_permission_round(
            task_id,
            permission_id=permission_id,
            tool_call_id=tool_call.get("toolCallId") if isinstance(tool_call, dict) else None,
            option_id=option_id,
            requested_at=pending.requested_at,
            answered_at=time.time(),
            ordinal=pending.ordinal,
        )

    def answer(self, task_id: str, permission_id: str, option_id: str) -> dict[str, Any]:
        job = self.jobs.get(task_id)
        if job is None or permission_id not in job.pending:
            raise ValueError("Permission request is no longer active")
        pending = job.pending[permission_id]
        allowed = {
            option["optionId"]
            for option in pending.request["options"]
            if option["kind"] in ("allow_once", "reject_once")
        }
        if option_id not in allowed:
            raise ValueError("Select a supplied one-time permission option")
        if pending.future.done():
            raise ValueError("Permission request was already answered")
        self._resolve_permission(task_id, permission_id, pending, option_id)
        pending.future.set_result(option_id)
        return {"task_id": task_id, "permission_id": permission_id, "answered": True}

    async def cancel(self, task_id: str) -> dict[str, Any]:
        receipt = self.store.get(task_id)
        if receipt["status"] in TERMINAL:
            return public_task(receipt)
        job = self.jobs.get(task_id)
        if job is None:
            raise ValueError(
                f"No owned live {self.provider_name} task; cancellation cannot be verified"
            )
        if job.adapter is None or job.session_id is None:
            self.store.metadata(task_id, {"cancel_requested": True})
            if job.task is not None:
                job.task.cancel()
                await asyncio.gather(job.task, return_exceptions=True)
            self.jobs.pop(task_id, None)
            detail = (
                "Cancelled during provider backoff; the throttled attempt had ended"
                if job.relaunched
                else "Cancelled before prompt submission"
            )
            return public_task(self.store.update(task_id, "cancelled", detail=detail))
        if job.adapter.closed:
            # The owned adapter already reports itself closed, ordinarily because the
            # turn already finished and the adapter is mid-teardown. There is nothing
            # left to cancel; a genuinely live, open adapter is still asked below, and
            # any BridgeError that raises is a real transport fault that propagates.
            return public_task(self.store.get(task_id))
        for permission_id, pending in job.pending.items():
            if not pending.future.done():
                self._resolve_permission(task_id, permission_id, pending, "cancelled")
                pending.future.set_result(None)
        await job.adapter.cancel(job.session_id)
        self.store.metadata(task_id, {"cancel_requested": True})
        return public_task(self.store.get(task_id))

    def running(self, task_id: str) -> bool:
        return task_id in self.jobs

    async def close(self) -> None:
        self.closing = True
        tasks = [job.task for job in self.jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Tasks cancelled before their first coroutine step cannot run their own finally block.
        for task_id in list(self.jobs):
            self.store.update(task_id, "uncertain", detail="Bridge stopped before worker settled")
            self.jobs.pop(task_id, None)
