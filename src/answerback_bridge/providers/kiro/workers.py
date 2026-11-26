"""Kiro ACP worker ownership, launch, and receipt policy."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...active_budget import ActiveBudget
from ...config import public_task
from ...pressure import Match, match_lines
from ...process import BridgeError, CommandCleanupError, module_launch_argv
from ...worker_runtime import (
    THROTTLED_TWICE as THROTTLED_TWICE,
)
from ...worker_runtime import (
    ACPWorkerRuntime,
    Job,
    Throttled,
    native_stopped_fact,
)
from ...worker_runtime import (
    PendingPermission as PendingPermission,
)
from .acp import KiroACP
from .agent import AGENT_NAME, install_worker_agent

# A finished Kiro turn whose whole answer is this short and names the limiter is the
# limiter's own message; a longer answer that mentions it is a worker discussing it.
KIRO_LIMITER_ANSWER_CHARS = 1000


def _throttle_match(
    text: str | None,
    stop_reason: str,
    *,
    brief: str | None = None,
    account_hint: str = "default",
) -> Match | None:
    """The rate-limit signature in a Kiro turn's answer, if there is one. Any signature
    counts when the turn did not end normally; a normal end counts only for Kiro's own
    limiter message as (nearly) the whole answer. Lines of the brief never count."""
    if not isinstance(text, str) or not text.strip():
        return None
    ended = stop_reason == "end_turn"
    if ended and len(text.strip()) > KIRO_LIMITER_ANSWER_CHARS:
        return None
    found = match_lines(
        text,
        exclude=brief,
        harness="kiro",
        source="acp_text",
        account_hint=account_hint,
        only=frozenset({"kiro.rate_limit"}) if ended else None,
    )
    return found[0] if found else None


def worker_profile() -> dict[str, Any]:
    """Keep unrelated global MCP connections out of the required startup path."""
    return {
        "id": AGENT_NAME,
        "description": "Coding worker with explicitly scoped bridge MCP access",
        "prompt": (
            "Complete the assigned task within its stated scope. Follow the project's "
            "instructions and normal tool permission decisions. Report the result and "
            "validation evidence; never treat discovery or submission as completion."
        ),
        "tools": ["read", "write", "shell", "@answerback_bridge"],
        "includeMcpJson": False,
        "includePowers": False,
    }


class Workers(ACPWorkerRuntime):
    provider_name = "Kiro"

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
        cwd = self.config.kiro_workspaces.get(alias)
        if cwd is None:
            raise ValueError("Unknown Kiro alias; configure a named workspace first")
        if expected_cwd is not None and cwd != expected_cwd:
            raise ValueError("Kiro workspace changed since discovery; select the peer again")
        if not await asyncio.to_thread(Path(cwd).is_dir):
            raise ValueError("Kiro working directory is unavailable")
        # Removal can run while directory validation is offloaded. Admission and
        # receipt reservation must observe the same binding without another await.
        if self.closing:
            raise BridgeError("Bridge is stopping")
        if self.config.kiro_workspaces.get(alias) != cwd:
            raise ValueError("Kiro workspace changed; select a registered workspace again")
        if admission_guard is not None:
            admission_guard()
        task, created = self.store.reserve(
            f"kiro:{alias}:{cwd}",
            message,
            request_id,
            {"kind": "kiro", "alias": alias, "cwd": cwd, "completion": "acp_prompt_response"},
        )
        if created:
            job = Job()
            self.jobs[task["id"]] = job
            job.task = asyncio.create_task(self._run(task["id"], alias, cwd, message, job))
        return public_task(task)

    async def _run(
        self, task_id: str, alias: str, cwd: str, message: str, job: Job, attempt: int = 0
    ) -> None:
        try:
            async with self.slots:
                await self.wait_for_provider(task_id, "kiro")
                job.budget = ActiveBudget(self.config.kiro_active_budget)
                await job.budget.run(self._execute(task_id, alias, cwd, message, job))
        except asyncio.CancelledError:
            self.store.update(
                task_id, "uncertain", detail="Bridge interrupted Kiro; no automatic replay"
            )
            raise
        except Throttled as throttled:
            # Out of the slot and with the adapter closed; the relaunch resumes the saved
            # worker session, so the worker continues at the same workspace tip.
            await self.settle_throttled(
                task_id,
                job,
                throttled,
                attempt=attempt,
                interrupted="Bridge interrupted Kiro; no automatic replay",
                relaunch=lambda: self._run(task_id, alias, cwd, message, job, attempt + 1),
            )
        except TimeoutError as error:
            budget = job.budget
            if budget is not None and budget.expired:
                self.store.metadata(task_id, {"native_stopped": native_stopped_fact(job.adapter)})
                limit = f"Kiro task exceeded the {budget.seconds:g}-second active execution budget"
                if job.prompt_sent:
                    status = "uncertain"
                    detail = f"{limit} after its prompt was sent; no automatic replay"
                else:
                    status, detail = "failed", f"{limit} before its prompt was sent"
                self.store.update(task_id, status, detail=detail)
            else:
                self.store.update(task_id, "uncertain", detail=f"{type(error).__name__}: {error}")
        except Exception as error:
            # Adapters may raise SDK/protocol errors; do not leak unhandled task exceptions.
            budget = job.budget
            if budget is not None and budget.expired:
                self.store.metadata(
                    task_id,
                    {
                        "native_stopped": (
                            False
                            if isinstance(error, CommandCleanupError)
                            else native_stopped_fact(job.adapter)
                        )
                    },
                )
                limit = f"Kiro task exceeded the {budget.seconds:g}-second active execution budget"
                if job.prompt_sent:
                    status = "uncertain"
                    detail = f"{limit} after its prompt was sent; no automatic replay"
                else:
                    status, detail = "failed", f"{limit} before its prompt was sent"
                self.store.update(task_id, status, detail=detail)
            else:
                self.store.update(task_id, "uncertain", detail=f"{type(error).__name__}: {error}")
        finally:
            for pending in job.pending.values():
                if not pending.future.done():
                    pending.future.cancel()
            self.jobs.pop(task_id, None)

    def throttle_match(
        self, message: str, result: Any, error: BaseException | None
    ) -> "Match | None":
        # Kiro errors are reduced to their class (`acp_client._cause_text`): only the
        # turn's text can carry the limiter's message.
        if result is None or self.pressure is None:
            return None
        return _throttle_match(
            result.text,
            result.stop_reason,
            brief=message,
            account_hint=self.pressure.settings.account_hint("kiro"),
        )

    def launch_options(self, profile: dict[str, Any]) -> dict[str, Any]:
        """The `KiroACP` keyword arguments the configured engine and trust settings select.

        v3 is byte-identical to the historical launch. v2 names the on-disk agent that
        `install_worker_agent` writes (the v2 engine ignores inline `customAgents`) and
        carries `-a` or `--trust-tools` only when the owner configured one of them.
        """
        engine = self.config.kiro_acp_engine
        return {
            "agent_engine": engine,
            "trust_all_tools": self.config.kiro_trust_all_tools,
            "trust_tools": list(self.config.kiro_trust_tools),
            "agent": profile["id"] if engine == "v2" else None,
        }

    async def _execute(self, task_id: str, alias: str, cwd: str, message: str, job: Job) -> None:
        update, permission = self.callbacks(task_id, job)
        profile = worker_profile()
        if self.config.kiro_acp_engine == "v2":
            # Kiro reads the agent from the HOME the launch forwards (`KiroACP.environment`).
            await asyncio.to_thread(install_worker_agent, profile)
        async with KiroACP(
            self.config.kiro_binary,
            cwd,
            update,
            permission,
            **self.launch_options(profile),
        ) as adapter:
            job.adapter = adapter
            previous = self.store.worker_session(alias)
            if previous and previous["cwd"] != cwd:
                raise BridgeError("Kiro workspace changed; register a new alias")
            reuse_session = previous is not None and previous["profile"] == profile["id"]
            # `kiro_trust` says what this launch auto-approved: "ask" (a person answers
            # every tool use), "all" (`-a`) or the `--trust-tools` names.
            metadata = {"worker_profile": profile["id"], "kiro_trust": adapter.trust_mode}
            if previous and not reuse_session:
                # Loading cannot change Kiro's saved mode. Retain the old identity
                # and replace its binding only after the scoped session is ready.
                metadata["previous_session_id"] = previous["session_id"]
            self.store.metadata(task_id, metadata)
            launch = module_launch_argv(self.config_path, "mcp")
            session_id = await adapter.open_session(
                session_id=previous["session_id"] if reuse_session else None,
                mcp_servers=[
                    {
                        "name": "answerback_bridge",
                        "command": launch[0],
                        "args": launch[1:],
                        "env": [],
                        "_meta": {"kiro": {"waitForReady": True, "timeout": 60000}},
                    }
                ],
                # The tools the worker needs to delegate onward. Its own result is the
                # prompt turn's text (`adapter.prompt` below), not a `task_complete` call,
                # so that tool is not required here.
                required_mcp_tools={"answerback_bridge": {"peer_submit", "task_wait"}},
                agent_profile=profile,
            )
            job.session_id = session_id
            self.store.save_worker_session(alias, session_id, cwd, profile=profile["id"])
            # v3 reports every MCP server with its tools, which the bridge verifies; v2
            # reports only that the server initialized (`unverified_mcp_tools` names it).
            readiness = {
                "mcp_readiness": "server_initialized"
                if adapter.mcp_unverified_servers
                else "tools_verified"
            }
            if adapter.mcp_unverified_servers:
                readiness["unverified_mcp_tools"] = sorted(adapter.mcp_unverified_servers)
            self.store.metadata(
                task_id, {"session_id": session_id, "protocol": "acp-v1", **readiness}
            )
            self.store.update(task_id, "running")
            job.prompt_sent = True
            self.mark_resumed(task_id, job)
            result = await adapter.prompt(session_id, self.prompt_text("kiro", message))
            if self.pressure is not None and (
                match := _throttle_match(
                    result.text,
                    result.stop_reason,
                    brief=message,
                    account_hint=self.pressure.settings.account_hint("kiro"),
                )
            ):
                self.pressure.signal(match)
            if result.stop_reason != "cancelled" and (
                throttled := self.throttled(job, message, result)
            ):
                raise throttled
            if result.stop_reason == "end_turn":
                self.store.update(task_id, "completed", result=result.text)
            elif result.stop_reason == "cancelled":
                self.store.update(task_id, "cancelled", detail="Kiro confirmed cancellation")
            else:
                self.store.update(task_id, "failed", detail=f"Kiro stopped: {result.stop_reason}")
