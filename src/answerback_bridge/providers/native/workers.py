"""Receipt owner for opt-in, bridge-owned native CLI sessions."""

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...active_budget import ActiveBudget
from ...config import NATIVE_PROVIDERS, Config, public_task
from ...pressure import (
    KNOWN_PROVIDERS,
    Match,
    PressureStore,
    harness_of_peer,
    match_lines,
    provider_of,
)
from ...pressure.signatures import BY_KEY, account_of, line_digest
from ...process import BridgeError, CommandCleanupError, module_launch_argv
from ...retention import matches_work
from ...subjects import admitted_subject
from ...tasks import TaskStore
from ...worker_runtime import ACPWorkerRuntime, Job, Throttled, native_stopped_fact
from .protocol import NativeTransport

ACTIVE_BUDGET_PREFIXES = {
    "codex_cli": "codex_cli task exceeded the ",
    "claude_code": "claude_code task exceeded the ",
    "grok": "grok task exceeded the ",
}


HARNESS = {"codex_cli": "codex", "claude_code": "claude", "grok": "grok"}
MAX_EVIDENCE_TEXTS = 16
# Jobs one provider holds at once (running, waiting for one of its two slots, or waiting
# on a permission answer); a request past this is refused before a receipt is reserved,
# as CoreTasks refuses past its 32.
MAX_QUEUED_JOBS = 32


def _throttle_match(
    evidence: dict[str, Any] | None,
    error: BaseException | None,
    *,
    harness: str,
    hints: dict[str, str] | None = None,
    brief: str | None = None,
) -> Match | None:
    """The rate-limit signature in a native turn's evidence or in the error that ended
    it, if there is one. Evidence fields read: Claude `api_error_status` and `errors`,
    Codex `error` (a string or an object with `message`). Only a `BridgeError`'s text is
    matched: that text is bridge-reviewed, other errors are known by class alone."""
    hints = hints or {}
    texts: list[tuple[str, str]] = []
    if isinstance(evidence, dict):
        if evidence.get("api_error_status") == 429:
            signature = BY_KEY["bedrock.http_429"]
            provider = account_of(signature, harness)
            return Match(
                provider,
                hints.get(provider, "default"),
                harness,
                "native_evidence",
                signature.key,
                line_digest("api_error_status=429"),
            )
        errors = evidence.get("errors")
        if isinstance(errors, list):
            texts.extend(
                ("native_evidence", item)
                for item in errors[:MAX_EVIDENCE_TEXTS]
                if isinstance(item, str)
            )
        failure = evidence.get("error")
        if isinstance(failure, dict):
            failure = failure.get("message")
        if isinstance(failure, str):
            texts.append(("native_evidence", failure))
    if isinstance(error, BridgeError):
        texts.append(("native_error", str(error)))
    for source, text in texts:
        found = match_lines(
            text[:8000], exclude=brief, harness=harness, source=source, account_hint=hints
        )
        if found:
            return found[0]
    return None


def transport_for(provider: str) -> Callable[..., NativeTransport]:
    # Optional SDKs load only when their explicitly selected provider runs.
    if provider == "codex_cli":
        from .codex import CodexCLI

        return CodexCLI
    if provider == "claude_code":
        from .claude import ClaudeCLI

        return ClaudeCLI
    if provider == "grok":
        from .grok import GrokCLI

        return GrokCLI
    raise ValueError("Unknown native CLI provider")


def native_home(config: Config, provider: str) -> str:
    variable, directory = {
        "codex_cli": ("CODEX_HOME", ".codex"),
        "claude_code": ("CLAUDE_CONFIG_DIR", ".claude"),
        "grok": ("GROK_HOME", ".grok"),
    }[provider]
    value = getattr(config, f"{provider}_home") or os.environ.get(variable)
    return str(Path(value or Path.home() / directory).expanduser().resolve())


class NativeWorkers(ACPWorkerRuntime):
    def __init__(
        self,
        provider: str,
        config: Config,
        config_path: Path,
        store: TaskStore,
        *,
        transport_factory: Callable[..., NativeTransport] | None = None,
        pressure: "PressureStore | None" = None,
    ) -> None:
        if provider not in NATIVE_PROVIDERS:
            raise ValueError("Unknown native CLI provider")
        super().__init__(config, config_path, store, pressure=pressure)
        self.provider_name = provider
        self._factory = transport_factory

    @property
    def account_provider(self) -> str:
        """The rate-limited account this CLI spends: `bedrock` for every native CLI today."""
        return provider_of(self.provider_name)

    def throttle_match(
        self, message: str, result: Any, error: BaseException | None
    ) -> "Match | None":
        if self.pressure is None:
            return None
        settings = self.pressure.settings
        return _throttle_match(
            None if result is None else result.evidence,
            error,
            harness=harness_of_peer({"provider": self.provider_name}),
            hints={provider: settings.account_hint(provider) for provider in KNOWN_PROVIDERS},
            brief=message,
        )

    async def _relaunch(
        self,
        task_id: str,
        alias: str,
        message: str,
        binding: tuple[str, str, str, str],
        job: Job,
        attempt: int,
    ) -> None:
        # The relaunch is a new turn of the same session: keep the throttled turn's
        # identity as evidence and let the new one bind.
        turn = self.store.get(task_id)["metadata"].get("native_turn_id")
        self.store.metadata(task_id, {"native_turn_id": None, "throttled_turn_id": turn})
        await self._run(task_id, alias, message, binding, job, attempt + 1)

    def binding(self, alias: str) -> tuple[str, str, str, str]:
        provider = self.provider_name
        cwd = getattr(self.config, f"{provider}_workspaces").get(alias)
        if cwd is None:
            raise ValueError("Unknown native workspace; register a named project first")
        binary = getattr(self.config, f"{provider}_binary")
        home = native_home(self.config, provider)
        profile = hashlib.sha256(
            json.dumps(
                [provider, binary, home, cwd, str(self.config_path), "scoped-native-v1"]
            ).encode()
        ).hexdigest()
        return cwd, binary, home, profile

    def previous(self, alias: str, cwd: str, profile: str) -> str | None:
        saved = self.store.worker_session(f"{self.provider_name}:{alias}")
        if saved is None:
            return None
        if not isinstance(saved["session_id"], str) or not saved["session_id"]:
            raise ValueError("Saved native session ownership is invalid")
        owner = self.store.native_session_owner(self.provider_name, saved["session_id"])
        if (
            saved["cwd"] != cwd
            or saved["profile"] != profile
            or owner is None
            or (owner["alias"], owner["cwd"], owner["profile"]) != (alias, cwd, profile)
        ):
            raise ValueError("Native session ownership or configuration profile changed")
        return saved["session_id"]

    async def submit(
        self,
        alias: str,
        message: str,
        request_id: str,
        *,
        expected_cwd: str | None = None,
        admission_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if self.closing:
            raise BridgeError("Bridge is stopping")
        if not self.config.provider_enabled(self.provider_name):
            raise ValueError("Native CLI provider is not registered")
        binding = self.binding(alias)
        cwd, _, _, profile = binding
        if expected_cwd is not None and expected_cwd != cwd:
            raise ValueError("Native workspace changed after discovery")
        if not await asyncio.to_thread(Path(cwd).is_dir):
            raise ValueError("Native working directory is unavailable")
        if self.closing or self.binding(alias) != binding:
            raise ValueError("Native workspace configuration changed before admission")
        if admission_guard is not None:
            admission_guard()
        target = f"{self.provider_name}:{alias}:{cwd}"
        repeated = self.store.by_request(request_id)
        if repeated is not None:
            # The subject comes from the router's admission, as a reservation takes it.
            subject = admitted_subject(self.store.path, request_id, None)
            if not matches_work(
                repeated, target, message, request_id, self.provider_name, subject=subject
            ):
                raise ValueError("request_id already describes different work")
            return public_task(repeated)
        if len(self.jobs) >= MAX_QUEUED_JOBS:
            raise BridgeError("Native delivery queue is full; inspect existing tasks")
        self.previous(alias, cwd, profile)
        receipt, created = self.store.reserve(
            target,
            message,
            request_id,
            {
                "kind": self.provider_name,
                "alias": alias,
                "cwd": cwd,
                "profile": profile,
                "completion": "native_terminal_response",
            },
        )
        if created:
            job = Job()
            self.jobs[receipt["id"]] = job
            job.task = asyncio.create_task(self._run(receipt["id"], alias, message, binding, job))
        return public_task(receipt)

    def callbacks(self, task_id: str, job: Job) -> tuple[Any, Any]:
        observe, permission = super().callbacks(task_id, job)

        async def update(event: dict[str, Any]) -> None:
            turn_id = event.get("turn_id")
            if turn_id is not None:
                if job.session_id is None or event.get("session_id") != job.session_id:
                    raise BridgeError("Native progress has an unexpected session")
                self.store.bind_native_turn(task_id, job.session_id, turn_id)
            await observe(event)

        return update, permission

    async def _run(
        self,
        task_id: str,
        alias: str,
        message: str,
        binding: tuple[str, str, str, str],
        job: Job,
        attempt: int = 0,
    ) -> None:
        try:
            async with self.slots:
                await self.wait_for_provider(task_id, self.account_provider)
                if self.binding(alias) != binding or not self.config.provider_enabled(
                    self.provider_name
                ):
                    raise ValueError("Native routing changed before execution")
                cwd, binary, home, profile = binding
                previous = self.previous(alias, cwd, profile)
                factory = self._factory or transport_for(self.provider_name)
                update, permission = self.callbacks(task_id, job)
                active_budget = getattr(self.config, f"{self.provider_name}_active_budget")
                job.budget = ActiveBudget(active_budget)
                async with job.budget.limit():
                    async with factory(binary, cwd, update, permission, home=home) as adapter:
                        job.adapter = adapter
                        launch = module_launch_argv(self.config_path, "mcp")
                        session_id = await adapter.open_session(
                            previous,
                            [
                                {
                                    "name": "answerback_bridge",
                                    "command": launch[0],
                                    "args": launch[1:],
                                    "env": [],
                                }
                            ],
                        )
                        if (
                            not isinstance(session_id, str)
                            or not session_id
                            or len(session_id) > 512
                        ):
                            raise BridgeError("Native session identity is invalid")
                        if previous is not None and previous != session_id:
                            raise BridgeError("Native resume changed the owned session identity")
                        self.store.claim_native_session(
                            self.provider_name, session_id, alias, cwd, profile
                        )
                        job.session_id = session_id
                        self.store.metadata(task_id, {"session_id": session_id})
                        self.store.update(task_id, "running")
                        job.prompt_sent = True
                        self.mark_resumed(task_id, job)
                        result = await adapter.prompt(
                            session_id,
                            self.prompt_text(self.account_provider, message),
                            prompt_id=task_id,
                        )
                        if result.session_id != session_id:
                            raise BridgeError("Native terminal event has the wrong session")
                        if result.status not in ("completed", "failed", "cancelled"):
                            raise BridgeError("Native terminal event has an invalid status")
                        if result.turn_id is not None:
                            self.store.bind_native_turn(task_id, session_id, result.turn_id)
                        self.store.metadata(
                            task_id,
                            {"native_terminal": result.status, "native_evidence": result.evidence},
                        )
                        if self.pressure is not None and (
                            match := _throttle_match(
                                result.evidence,
                                None,
                                harness=HARNESS[self.provider_name],
                                hints=self.pressure.settings.hints(),
                                brief=message,
                            )
                        ):
                            self.pressure.signal(match)
                    # Native teardown must finish before releasing the task's reservation.
                    if result.status == "completed" and result.text is None:
                        raise BridgeError(
                            "Native turn completed but its final response is unavailable"
                        )
                    if result.text is not None and len(result.text) > 200_000:
                        raise BridgeError("Native response exceeds its supported bound")
                    if result.evidence.get("resumable", True):
                        self.store.save_worker_session(
                            f"{self.provider_name}:{alias}", session_id, cwd, profile=profile
                        )
                    # After the session is saved, so the relaunch resumes it. Only a failed
                    # turn: a completed one may have recovered from a throttle on its own.
                    if result.status == "failed" and (
                        throttled := self.throttled(job, message, result)
                    ):
                        raise throttled
                    self.store.update(
                        task_id,
                        result.status,
                        result=result.text,
                        turn_id=result.turn_id,
                        detail=f"{self.provider_name} confirmed {result.status}",
                    )
        except asyncio.CancelledError:
            self.store.update(
                task_id, "uncertain", detail="Native worker interrupted; no automatic replay"
            )
            raise
        except Throttled as throttled:
            await self.settle_throttled(
                task_id,
                job,
                throttled,
                attempt=attempt,
                interrupted="Native worker interrupted; no automatic replay",
                relaunch=lambda: self._relaunch(task_id, alias, message, binding, job, attempt),
            )
        except TimeoutError as error:
            budget = job.budget
            if budget is not None and budget.expired:
                self.store.metadata(task_id, {"native_stopped": native_stopped_fact(job.adapter)})
                limit = (
                    f"{ACTIVE_BUDGET_PREFIXES[self.provider_name]}"
                    f"{budget.seconds:g}-second active execution budget"
                )
                if job.prompt_sent:
                    status = "uncertain"
                    detail = f"{limit} after its prompt was sent; no automatic replay"
                else:
                    status, detail = "failed", f"{limit} before its prompt was sent"
                self.store.update(task_id, status, detail=detail)
            else:
                self.store.update(
                    task_id,
                    "uncertain" if job.prompt_sent else "failed",
                    detail=f"{type(error).__name__}: {error}",
                )
        except Exception as error:
            if self.pressure is not None and (
                match := _throttle_match(
                    None,
                    error,
                    harness=HARNESS[self.provider_name],
                    hints=self.pressure.settings.hints(),
                    brief=message,
                )
            ):
                self.pressure.signal(match)
            # The error seam: a throttle relaunches once instead of ending the receipt.
            if await self.relaunch_on_error(
                task_id,
                job,
                message,
                error,
                attempt=attempt,
                interrupted="Native worker interrupted; no automatic replay",
                relaunch=lambda: self._relaunch(task_id, alias, message, binding, job, attempt),
            ):
                return
            budget = job.budget
            if budget is not None and budget.expired:
                cleanup_failed = isinstance(error, CommandCleanupError) or (
                    self.provider_name == "claude_code"
                    and isinstance(error, BridgeError)
                    and "shutdown is unconfirmed" in str(error)
                )
                self.store.metadata(
                    task_id,
                    {
                        "native_stopped": (
                            False if cleanup_failed else native_stopped_fact(job.adapter)
                        )
                    },
                )
                limit = (
                    f"{ACTIVE_BUDGET_PREFIXES[self.provider_name]}"
                    f"{budget.seconds:g}-second active execution budget"
                )
                if job.prompt_sent:
                    status = "uncertain"
                    detail = f"{limit} after its prompt was sent; no automatic replay"
                else:
                    status, detail = "failed", f"{limit} before its prompt was sent"
                self.store.update(task_id, status, detail=detail)
            else:
                self.store.update(
                    task_id,
                    "uncertain" if job.prompt_sent else "failed",
                    detail=f"{type(error).__name__}: {error}",
                )
        finally:
            for pending in job.pending.values():
                if not pending.future.done():
                    pending.future.cancel()
            self.jobs.pop(task_id, None)
