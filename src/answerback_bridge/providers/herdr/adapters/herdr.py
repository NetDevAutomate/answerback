"""Herdr provider discovery and execution implementation."""

import asyncio
import hashlib
import json
import shlex
from collections.abc import Callable
from typing import Any

from .... import codex
from ....adapters.base import StoredAdapter, validate_project
from ....adapters.contracts import DiscoveryPage, Guard, Peer, PreparedPeer, Submission, TaskReceipt
from ....config import Config, public_task
from ....plugin import KernelContext
from ....process import BridgeError, module_launch_argv
from ..core import validate_message


def _agent_kind(agent: dict[str, Any]) -> str:
    """Use the same native kind aliases for discovery and delivery checks."""
    return (
        agent.get("agent")
        or agent.get("display_agent")
        or agent.get("agent_kind")
        or agent.get("kind")
        or ""
    )


class HerdrDiscovery:
    name = "herdr"
    registration = "herdr"
    aliases = ()

    def __init__(self, config: Config, native: Callable[[], Any]) -> None:
        self.config, self._native = config, native

    @property
    def herdr(self) -> Any:
        return self._native()

    def enabled(self) -> bool:
        return self.config.provider_enabled(self.registration)

    async def resolve_exact(self, selector: str) -> Peer | None:
        return None

    async def discover(self) -> DiscoveryPage:
        peers: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        cursors: dict[str, str] = {}
        excluded: dict[str, str] = {}
        if self.config.provider_enabled("herdr"):
            try:
                result = await self.herdr.call("agent", "list")
                agents = result.get("agents")
                if not isinstance(agents, list):
                    raise BridgeError("Herdr agent list has an unsupported shape")
                for agent in agents:
                    target = agent.get("name") or agent.get("agent_name") or agent.get("pane_id")
                    if not isinstance(target, str) or not target:
                        continue
                    terminal_id = agent.get("terminal_id")
                    agent_session = agent.get("agent_session")
                    agent_kind = _agent_kind(agent)
                    # Lets a caller that selected this agent through peer_search
                    # carry its discovery-time identity into peer_submit; a later
                    # resolve naming a replacement terminal, session or agent is then
                    # refused instead of silently delivering to it.
                    binding = hashlib.sha256(
                        json.dumps(
                            {
                                "terminal_id": terminal_id,
                                "agent_session": agent_session,
                                "agent_kind": agent_kind,
                            },
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()
                    peers.append(
                        {
                            "peer_id": f"herdr:{target}",
                            "provider": "herdr",
                            "target": target,
                            "name": agent.get("name")
                            or agent.get("agent_name")
                            or agent.get("title")
                            or target,
                            "title": agent.get("title")
                            or agent.get("terminal_title_stripped")
                            or agent.get("terminal_title")
                            or "",
                            "project": agent.get("foreground_cwd")
                            or agent.get("cwd")
                            or agent.get("working_directory")
                            or "",
                            "agent_kind": agent_kind,
                            "terminal_id": terminal_id,
                            "agent_session": agent_session,
                            "terminal_title": agent.get("terminal_title") or "",
                            "terminal_title_stripped": agent.get("terminal_title_stripped") or "",
                            "state": agent.get("agent_status", "unknown"),
                            "kind": "interactive",
                            "capabilities": ["read", "send", "worker_report"],
                            "binding": binding,
                        }
                    )
            except (BridgeError, OSError, ValueError) as error:
                errors["herdr"] = str(error)
        if not self.config.provider_enabled("herdr"):
            errors["herdr"] = "TUI session not configured; use hc for Herdr App"
        return DiscoveryPage.from_record(
            {"peers": peers, "errors": errors, "cursors": cursors, "excluded": excluded}
        )


class HerdrAdapter(HerdrDiscovery, StoredAdapter):
    def __init__(self, ctx: KernelContext, native: Callable[[], Any]) -> None:
        StoredAdapter.__init__(self, ctx.config, ctx.config_path, ctx.store)
        HerdrDiscovery.__init__(self, ctx.config, native)

    async def prepare(self, peer: Peer) -> PreparedPeer:
        self.require_enabled()
        terminal = peer.as_record().get("terminal_id")
        if not terminal:
            raise ValueError("Herdr terminal identity is unavailable")
        return PreparedPeer(f"herdr:{self.config.herdr_session}:{terminal}", peer)

    async def submit(
        self, prepared: PreparedPeer, submission: Submission, *, guard: Guard
    ) -> TaskReceipt:
        self.require_enabled()
        guard()
        row = await self.submit_native(
            prepared.peer.target,
            submission.message,
            submission.request_id,
            prepared.peer.as_record(),
            admission_guard=guard,
        )
        return TaskReceipt.from_record(self.store.get(row["id"]))

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        meta = receipt.as_record().get("metadata", {})
        record = peer.as_record()
        terminal_id = record.get("terminal_id")
        return (
            receipt.provider == self.name
            and peer.target in (meta.get("target"), meta.get("slot"), meta.get("alias"))
            and isinstance(terminal_id, str)
            and bool(terminal_id)
            and meta.get("terminal_id") == terminal_id
            and "agent_kind" in meta
            and meta["agent_kind"] == record.get("agent_kind")
            and (
                meta.get("agent_session") is None
                or meta["agent_session"] == record.get("agent_session")
            )
        )

    async def submit_native(
        self,
        target: str,
        message: str,
        request_id: str,
        expected: dict[str, Any],
        *,
        admission_guard: Guard | None = None,
    ) -> dict[str, Any]:
        # The annotation forbids None, but a caller can still pass it at runtime;
        # refuse it here, before any lookup, reservation or send, so the identity
        # check below can never be skipped by handing the adapter nothing.
        if expected is None:
            raise ValueError("Selected Herdr identity is missing; search again")
        validate_message(message)
        if len(message) > 100_000:
            raise ValueError("Message must contain 1–100000 characters")
        await self.herdr.check()
        agent = await self.herdr.get(target)
        terminal_id = agent.get("terminal_id")
        agent_kind = _agent_kind(agent)
        if not terminal_id:
            raise ValueError("Herdr terminal identity is unavailable; search again")
        if (
            not expected.get("terminal_id")
            or expected["terminal_id"] != terminal_id
            or expected.get("agent_kind", "") != agent_kind
            or (
                expected.get("agent_session") is not None
                and expected["agent_session"] != agent.get("agent_session")
            )
        ):
            raise ValueError("Selected Herdr identity changed or is unavailable; search again")
        if expected.get("project"):
            await validate_project(
                agent.get("foreground_cwd")
                or agent.get("cwd")
                or agent.get("working_directory")
                or "",
                expected["project"],
            )
        status = agent.get("agent_status")
        if status not in ("idle", "done"):
            raise ValueError(f"Herdr agent is not ready: {status or 'unknown'}")
        if admission_guard is not None:
            admission_guard()
        task, created = self.store.reserve(
            f"herdr:{self.config.herdr_session}:{terminal_id}",
            message,
            request_id,
            {
                "kind": "herdr",
                "target": target,
                "terminal_id": terminal_id,
                "agent_kind": agent_kind,
                "agent_session": agent.get("agent_session"),
            },
        )
        if not created:
            return public_task(task)
        command = shlex.join(
            module_launch_argv(self.config_path, "complete", task["id"], "--result-file")
        )
        envelope = (
            f"{codex.task_marker(task['id'])}\n{message}\n\n"
            "When finished, write your result into a UTF-8 file, then record completion with:\n"
            f"{command} /absolute/path/to/your-result.txt\n"
            "Use an actual file path you created. Do not mark completion until the task is done. "
            "If you need human approval, leave this task pending and request it normally."
        )
        try:
            receipt = await self.herdr.call("agent", "prompt", target, envelope)
        except (BridgeError, OSError, ValueError) as error:
            return public_task(self.store.update(task["id"], "uncertain", detail=str(error)))
        except asyncio.CancelledError:
            self.store.update(task["id"], "uncertain", detail="Submission was interrupted")
            raise
        except Exception as error:
            return public_task(
                self.store.update(
                    task["id"],
                    "uncertain",
                    detail=f"Herdr prompt call failed ({type(error).__name__}); "
                    "delivery is uncertain. Inspect the target before taking action.",
                )
            )
        try:
            observed = receipt.get("agent", {}).get("terminal_id")
        except Exception as error:
            return public_task(
                self.store.invalidate_submission(
                    task["id"],
                    f"Herdr prompt acknowledgement could not be read ({type(error).__name__}); "
                    "delivery is uncertain. Inspect the target before taking action.",
                )
            )
        if observed != terminal_id:
            return public_task(
                self.store.invalidate_submission(
                    task["id"],
                    f"Delivery identity unverified: expected {terminal_id}, observed {observed}. "
                    "Submission may have occurred; inspect the target before taking action.",
                )
            )
        try:
            after = await self.herdr.get(target)
        except asyncio.CancelledError:
            self.store.invalidate_submission(
                task["id"],
                "Herdr identity verification was interrupted after send; delivery is uncertain. "
                "Inspect the target before taking action.",
            )
            raise
        except Exception as error:
            error_type = (
                ""
                if isinstance(error, (BridgeError, OSError, ValueError))
                else f" ({type(error).__name__})"
            )
            return public_task(
                self.store.invalidate_submission(
                    task["id"],
                    f"Herdr identity could not be verified after send{error_type}; "
                    "delivery is uncertain. "
                    "Inspect the target before taking action.",
                )
            )
        if (
            not isinstance(after, dict)
            or after.get("terminal_id") != terminal_id
            or _agent_kind(after) != agent_kind
            or (
                agent.get("agent_session") is not None
                and after.get("agent_session") != agent["agent_session"]
            )
        ):
            return public_task(
                self.store.invalidate_submission(
                    task["id"],
                    "Herdr identity changed after send; delivery is uncertain. "
                    "Inspect the target before taking action.",
                )
            )
        return public_task(self.store.update(task["id"], "submitted"))
