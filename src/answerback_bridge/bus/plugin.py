"""The bus and its actor lane as one installed module.

Actors share the bus configuration, NATS connection, and coordinator lifecycle, so a
second module would only split one failure domain across two entry points.  This object
owns their RPC and CLI surfaces and the coordinator state reported by the kernel.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ..plugin import (
    PLUGIN_API,
    CliRunner,
    KernelContext,
    ModuleError,
    RpcHandler,
    ToolCall,
    ToolFunction,
    ToolSpec,
)
from ..process import BridgeError
from .extra import MISSING_BUS_EXTRA

if TYPE_CHECKING:
    from ..actors.model import ActorAuth
    from ..service import Bridge

# The module-wide bound on waiting actor_receive calls (the kernel's long-poll slot), and
# the share of it one actor of one run may hold, so one credentialed actor cannot make
# every other actor's receive fail with `capacity`.
ACTOR_RECEIVE_CAPACITY = 16
ACTOR_RECEIVE_PER_ACTOR = max(1, min(4, ACTOR_RECEIVE_CAPACITY // 4))


async def _tool_call(call: ToolCall, method: str, params: dict[str, Any]) -> dict[str, Any]:
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        return await call(method, params)
    except (BridgeError, ValueError) as error:
        if isinstance(error, BridgeError) and str(error) != MISSING_BUS_EXTRA:
            raise
        raise ToolError(str(error)) from None
    except FileNotFoundError:
        raise ToolError("Bus configuration is unavailable; inspect agent-bus configure") from None


def _submit_tool(call: ToolCall) -> ToolFunction:
    async def bus_submit(
        peer_id: str,
        project: str,
        message: str,
        request_id: str,
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Queue authorised work for an exact peer/project; stored is not completed."""
        return await _tool_call(
            call,
            "bus_submit",
            {
                "peer_id": peer_id,
                "project": project,
                "message": message,
                "request_id": request_id,
                "ttl_seconds": ttl_seconds,
            },
        )

    return bus_submit


def _result_tool(call: ToolCall) -> ToolFunction:
    async def bus_result(command_id: str) -> dict[str, Any]:
        """Read command delivery and its separately verified harness task/result."""
        return await _tool_call(call, "bus_result", {"command_id": command_id})

    return bus_result


def _events_tool(call: ToolCall) -> ToolFunction:
    async def bus_events(after: int = 0, limit: int = 50) -> dict[str, Any]:
        """Read bounded lifecycle events with references instead of prompt/result bodies."""
        return await _tool_call(call, "bus_events", {"after": after, "limit": limit})

    return bus_events


def _cancel_tool(call: ToolCall) -> ToolFunction:
    async def bus_cancel(command_id: str) -> dict[str, Any]:
        """Cancel a queued command only; admitted work uses the existing task cancellation rules."""
        return await _tool_call(call, "bus_cancel", {"command_id": command_id})

    return bus_cancel


def _status_tool(call: ToolCall) -> ToolFunction:
    async def bus_status() -> dict[str, Any]:
        """Read bus configuration, backlog and observed coordinator connection state."""
        return await _tool_call(call, "bus_status", {})

    return bus_status


def _actor_receive_capacity_refusal() -> ModuleError:
    from ..actors.model import ActorError

    return ActorError("capacity")


class BusModule:
    """The built-in bus module, rebound to the live service after entry-point loading."""

    api_version = PLUGIN_API
    name = "bus"
    extra = "bus"
    long_poll_verbs = {"actor_receive": ACTOR_RECEIVE_CAPACITY}
    long_poll_refusals = {"actor_receive": _actor_receive_capacity_refusal}
    actor_receive_per_actor = ACTOR_RECEIVE_PER_ACTOR

    def __init__(self, owner: Bridge | None = None) -> None:
        self.owner = owner
        self.state: dict[str, Any] = {"connected": False, "last_error": None}
        self.dependency_error: ImportError | None = None
        self.actor_slots = asyncio.Semaphore(4)
        # Waiting receives per (run_id, actor_id); touched only on the event loop.
        self.receive_waits: dict[tuple[str, str], int] = {}
        self.event: Callable[..., None] = lambda *_args, **_fields: None

    def with_owner(self, owner: Bridge) -> BusModule:
        return type(self)(owner)

    def _owner(self) -> Bridge:
        if self.owner is None:
            raise RuntimeError("The bus module was composed without a service")
        return self.owner

    def _bus_api(self) -> Any:
        from .api import BusAPI

        owner = self._owner()
        return BusAPI(owner.config_path, pressure=lambda: owner.pressure)

    def _actor_api(self) -> Any:
        from ..actors.api import ActorAPI

        return ActorAPI(self._owner().config_path)

    def bus_submit(
        self,
        peer_id: str,
        project: str,
        message: str,
        request_id: str,
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        return self._bus_api().submit(peer_id, project, message, request_id, ttl_seconds)

    def bus_result(self, command_id: str) -> dict[str, Any]:
        return self._bus_api().result(command_id)

    def bus_events(self, after: int = 0, limit: int = 50) -> dict[str, Any]:
        return self._bus_api().events(after, limit)

    def bus_cancel(self, command_id: str) -> dict[str, Any]:
        return self._bus_api().cancel(command_id)

    def bus_status(self) -> dict[str, Any]:
        return {**self._bus_api().status(), "coordinator": dict(self.state)}

    async def actor_send(
        self,
        auth: dict[str, Any],
        to_actor: str,
        request_id: str,
        kind: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        # The store is synchronous SQLite with a 5 s busy timeout: run it off the event
        # loop so actor_slots bounds the threads and the loop keeps serving other clients.
        async with self.actor_slots:
            return await asyncio.to_thread(
                self._actor_api().send, auth, to_actor, request_id, kind, body
            )

    @contextmanager
    def _receive_wait(self, auth: ActorAuth) -> Iterator[None]:
        """Hold one of this actor's waiting-receive slots, or refuse with `capacity`.

        Taken only after the first poll authenticated the caller, so a request with a
        borrowed run and actor id but no valid credential cannot occupy that actor's slots.
        """
        key = (auth.run_id, auth.actor_id)
        if self.receive_waits.get(key, 0) >= self.actor_receive_per_actor:
            from ..actors.model import ActorError

            raise ActorError("capacity")
        self.receive_waits[key] = self.receive_waits.get(key, 0) + 1
        try:
            yield
        finally:
            self.receive_waits[key] -= 1
            if not self.receive_waits[key]:
                del self.receive_waits[key]

    async def actor_receive(
        self, auth: dict[str, Any], limit: int = 8, wait_seconds: int = 0
    ) -> dict[str, Any]:
        return await self._actor_api().receive(auth, limit, wait_seconds, hold=self._receive_wait)

    async def actor_ack(
        self, auth: dict[str, Any], message_id: str, receipt_nonce: str
    ) -> dict[str, Any]:
        async with self.actor_slots:
            return await asyncio.to_thread(
                self._actor_api().acknowledge, auth, message_id, receipt_nonce
            )

    async def actor_reply(
        self, auth: dict[str, Any], reply_to: str, request_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        async with self.actor_slots:
            return await asyncio.to_thread(
                self._actor_api().reply, auth, reply_to, request_id, body
            )

    def rpc(self) -> Mapping[str, RpcHandler]:
        return {
            "bus_submit": self.bus_submit,
            "bus_result": self.bus_result,
            "bus_events": self.bus_events,
            "bus_cancel": self.bus_cancel,
            "bus_status": self.bus_status,
            "actor_send": self.actor_send,
            "actor_receive": self.actor_receive,
            "actor_ack": self.actor_ack,
            "actor_reply": self.actor_reply,
        }

    def cli(self, commands: argparse._SubParsersAction, run: CliRunner) -> None:
        from ..actors.cli import add_parser as add_actor_parser
        from .cli import parser as bus_parser
        from .cli import run_cli

        bus = commands.add_parser(
            "bus",
            help="Durable delivery and live monitoring",
            parents=[bus_parser(nested=True)],
            add_help=False,
            conflict_handler="resolve",
        )
        bus.set_defaults(plugin_main=run_cli)
        actors = add_actor_parser(commands)

        async def actor_handler(args: argparse.Namespace, _bridge: Any) -> dict[str, Any]:
            from ..actors.cli import dispatch

            return dispatch(args)

        run(actors, actor_handler)

    def mcp_tools(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(_submit_tool, "send"),
            ToolSpec(_result_tool, "read"),
            ToolSpec(_events_tool, "read"),
            ToolSpec(_cancel_tool, "write"),
            ToolSpec(_status_tool, "read"),
        )

    def maintenance(self) -> Any:
        from .maintenance import BusMaintenance

        return BusMaintenance()

    def status(self) -> Mapping[str, Any]:
        """The coordinator state the kernel exposes without reading bus internals."""
        return dict(self.state)

    async def run(self, ctx: KernelContext, stopping: asyncio.Event) -> None:
        from ..actors.api import ActorAPI
        from .config import BusConfig
        from .coordinator import Coordinator
        from .gateway import BridgeGateway

        owner = self._owner()
        while not stopping.is_set():
            try:
                config = BusConfig.load(ctx.config_path)
            except FileNotFoundError:
                pass
            except (ValueError, OSError) as error:
                self.state.update(connected=False, last_error=type(error).__name__)
            else:
                api = self._bus_api()
                coordinator = Coordinator(
                    api.store(),
                    BridgeGateway(owner),
                    actor_store=ActorAPI(ctx.config_path).available_store,
                )
                self.state = coordinator.state
                try:
                    await coordinator.run(config, stopping)
                except BridgeError as error:
                    self.event("bus_disabled", reason=str(error))
                    coordinator.state.update(connected=False, last_error=str(error))
                except ImportError as error:
                    self.dependency_error = error
                    self.state.update(connected=False, last_error=type(error).__name__)
                    raise
                finally:
                    self.state["connected"] = False
                return
            try:
                await asyncio.wait_for(stopping.wait(), 1)
            except TimeoutError:
                pass


def bus_plugin() -> BusModule:
    """The detached entry-point object; the service rebinds package-owned modules."""
    return BusModule()
