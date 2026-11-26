"""Shared operations exposed through CLI and MCP."""

import asyncio
import importlib
import inspect
import logging
import shutil
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .adapters.builtins import builtin_plugins
from .adapters.codex import CodexAdapter
from .adapters.contracts import Guard, Peer, Submission, TaskControls, TaskReceipt, TaskReconciler
from .adapters.crew import CrewAdapter
from .adapters.router import AdapterRouter
from .client_mailbox import ClientMailbox
from .client_registry import ClientRegistry
from .config import (
    Config,
    public_task,
    remote_trust_line,
    routing_digest,
)
from .desktop import DesktopIndex
from .discovery import Directory, SelectionRequired
from .events.collector import Settings as EventSettings
from .events.debug import DebugController, is_receipt_target, resolve_scope
from .events.hub import EventHub
from .native_claude import (
    MAX_SETTINGS_BYTES,
    SDK_VERSION,
    _cli_version_error,
    _cli_version_from_output,
    _missing_sdk_error,
    _provider_environment,
    _sdk_version_error,
)
from .native_workers import native_home
from .plugin import (
    ModulePlugin,
    PluginOwners,
    ProviderPlugin,
    RpcHandler,
    SurfaceCollision,
    agent_programs,
    by_name,
    kernel_context,
    load_modules,
    merged,
    module_long_poll_refusals,
    module_long_poll_verbs,
    module_verbs,
    provider_status,
    receipt_reasons,
)
from .pressure import (
    KNOWN_PROVIDERS,
    Pressure,
    PressureStore,
    publish_section,
    stream_listener,
)
from .pressure.gate import ResumeTimers, backoff_result, hold_for, in_flight, pressure_text
from .pressure.gate import request_id as pressure_request_id
from .process import BridgeError, run
from .provider_registry import ProviderRegistry
from .providers.herdr.adapters.herdr import HerdrAdapter
from .providers.herdr.core import Core
from .providers.herdr.core_tasks import CoreTasks
from .providers.herdr.herdr import Herdr
from .retention import matches_work, task_availability
from .rex_service import RexService
from .tasks import TERMINAL, TaskStore
from .transport import runtime_dir
from .workers import Workers
from .workspaces import Workspaces

_LOG = logging.getLogger(__name__)


def _plugin_verbs(plugins: Sequence[ProviderPlugin]) -> list[tuple[str, Mapping[str, RpcHandler]]]:
    """Each plugin's name and ``rpc()`` table, sorted by name so the merge is deterministic."""
    return [(plugin.definition.registration, plugin.rpc()) for plugin in by_name(plugins)]


def _methods(
    kernel: Iterable[str],
    plugins: Sequence[ProviderPlugin],
    modules: Sequence[ModulePlugin],
) -> frozenset[str]:
    providers = merged("verb", dict.fromkeys(kernel), _plugin_verbs(plugins))
    return frozenset(merged("verb", providers, module_verbs(modules)))


class Bridge:
    # The verbs the kernel dispatches itself. Every provider verb (`{provider}_{verb}`) comes
    # from its plugin's `rpc()`; `METHODS` is the union, and a name two contributors declare
    # is a startup error (`SurfaceCollision`).
    KERNEL_METHODS = frozenset(
        {
            "doctor",
            "peers",
            "pair",
            "task_get",
            "task_wait",
            "task_reconcile",
            "events_read",
            "events_runs",
            "debug_on",
            "debug_off",
            "debug_status",
            "task_complete",
            "task_abandon",
            "peer_search",
            "peer_submit",
            "task_list",
            "fleet_snapshot",
            "task_for_peer",
            "workspace_add",
            "workspace_list",
            "workspace_remove",
            "task_permissions",
            "permission_answer",
            "task_cancel",
            "provider_list",
            "provider_register",
            "provider_unregister",
            "client_list",
            "client_register",
            "client_enroll",
            "client_heartbeat",
            "client_disconnect",
            "client_policy",
            "client_poll",
            "client_ack",
            "client_notify",
            "client_notification_status",
            "pressure_status",
            "pressure_clear",
        }
    )
    # The default composition's verbs. An instance composed with other plugins carries its
    # own `METHODS`; the runtime reads the instance's.
    METHODS = _methods(KERNEL_METHODS, builtin_plugins(), load_modules())

    def __init__(
        self,
        config: Config,
        config_path: Path,
        plugins: Callable[[PluginOwners], Sequence[ProviderPlugin]] = builtin_plugins,
    ) -> None:
        """``plugins`` composes the providers from the clients this service owns; the
        built-ins by default, and a test adds its own beside them."""
        self.config = config
        self.config_path = config_path.resolve()
        self.store = TaskStore(self.config_path.parent / "tasks.sqlite")
        try:
            self.event_settings = EventSettings.load(self.config_path)
            self.event_settings_error: str | None = None
        except (OSError, ValueError) as error:
            # A bad optional events.json must not stop dispatch; the stream reports it.
            self.event_settings, self.event_settings_error = EventSettings(), type(error).__name__
        self.debug = DebugController(runtime_dir(self.config_path))
        self.events = EventHub(
            history=self.event_settings.history, receipt_path=self.store.path, debug=self.debug
        )
        self.clients = ClientRegistry(config)
        self.mailbox = ClientMailbox(self.config_path.parent / "client-mailbox.sqlite")
        self.pressure = PressureStore(
            self.config_path.parent / "pressure.sqlite", settings=self.event_settings.pressure
        )
        # Each signal is one `answerback.provider_throttled` event; the `pressure` state
        # section follows every change.
        self.pressure.subscribe(stream_listener(self.events, self.pressure))
        publish_section(self.events, self.pressure)
        self.pressure_notify_state: dict[str, Any] = {"errors": 0, "last_error": None}
        # The "dispatch resumed" notice is due at `until`, not at the level-0 decay.
        self.pressure_resume = ResumeTimers(self.pressure, self._pressure_resumed)
        self.pressure.subscribe(self._on_pressure)
        self.providers = ProviderRegistry(config, self.config_path, self.store)
        self.plugins = tuple(plugins(PluginOwners.of(self)))
        self.agent_programs = agent_programs(self.plugins)
        self.receipt_reasons = receipt_reasons(self.plugins)
        self.herdr = Herdr(config)
        self.core = Core(
            config.hc_binary,
            config.hc_session,
            send_to_done=config.hc_send_to_done,
            send_machines=config.hc_send_machines,
            events=config.hc_events,
            agents=self.agent_programs,
        )
        self._retired_cores: set[asyncio.Task[None]] = set()
        self.core_tasks = CoreTasks(
            self.store, self.config_path, self.config, pressure=self.pressure
        )
        self.core_lock = asyncio.Lock()
        self.provider_status: dict[str, Any] = {}
        self.reset_connections()
        self.history_slots = asyncio.Semaphore(2)
        # Fixed-size stripes bound memory even when clients submit arbitrary unknown task IDs.
        self.task_locks = [asyncio.Lock() for _ in range(64)]
        self.workers = Workers(config, self.config_path, self.store, pressure=self.pressure)
        self.workspaces = Workspaces(config, self.config_path, self.store)
        self._crew_client: Any = None
        self.desktop_index = DesktopIndex(config.codex_binary, config.codex_home)
        from .adapters.factory import build_adapters

        self.rex = RexService(self)
        self.kernel = kernel_context(
            config,
            self.config_path,
            self.store,
            events=self.events,
            locks=self.task_locks,
        )
        self.adapters = build_adapters(self.kernel, self.plugins)
        self.modules = load_modules(self)
        self.long_poll_verbs = module_long_poll_verbs(self.modules)
        self.long_poll_refusals = module_long_poll_refusals(self.modules)
        # Generate the verb table once so a collision fails startup, not the first call.
        self.METHODS = frozenset(self.operations())
        # Last, so a constructor that fails half way never leaves a follower child.
        if config.provider_enabled("hc"):
            self.core.start_events()

    def module(self, name: str) -> ModulePlugin:
        """Return one installed module by its validated registration name."""
        matches = [module for module in self.modules if module.name == name]
        if len(matches) != 1:
            raise RuntimeError(f"Installed module {name!r} is unavailable")
        return matches[0]

    def module_statuses(self) -> dict[str, dict[str, Any]]:
        """Collect status from each module's public hook, keyed by module name."""
        statuses: dict[str, dict[str, Any]] = {}
        for module in self.modules:
            hook = getattr(module, "status", None)
            status = hook() if callable(hook) else None
            if status is not None:
                statuses[module.name] = dict(status)
        return statuses

    @property
    def bus_state(self) -> dict[str, Any]:
        """Compatibility view of the bus module's public status."""
        return self.module_statuses()["bus"]

    @bus_state.setter
    def bus_state(self, value: dict[str, Any]) -> None:
        """Compatibility writer for callers that replace the coordinator state."""
        module = cast(Any, self.module("bus"))
        module.state = value

    @property
    def bus_api(self) -> Any:
        """Compatibility view; new dispatch reaches this API through the bus module."""
        return cast(Any, self.module("bus"))._bus_api()

    @property
    def provider_status(self) -> dict[str, Any]:
        """Provider blocks for status; `hc.events` is read live when `hc_events` is on."""
        status = self._provider_status
        events_state = getattr(getattr(self, "core", None), "events_state", None)
        events = events_state() if callable(events_state) else None
        if events is not None and isinstance(status.get("hc"), dict):
            status["hc"]["events"] = events
        return status

    @provider_status.setter
    def provider_status(self, value: dict[str, Any]) -> None:
        self._provider_status = value

    def _replace_core(self, core: Core) -> None:
        """Make `core` current, start its follower, and stop the old one's in the background."""
        # Both callers run on the daemon's loop. Without one this raises before
        # anything changes, so the old Core stays current and close() still stops it.
        loop = asyncio.get_running_loop()
        old, self.core = self.core, core
        start = getattr(core, "start_events", None)
        if callable(start) and self.config.provider_enabled("hc"):
            start()
        close = getattr(old, "close", None)
        if old is not core and callable(close):
            closing = loop.create_task(close())
            self._retired_cores.add(closing)
            closing.add_done_callback(self._retired_cores.discard)

    @property
    def router(self) -> AdapterRouter:
        return AdapterRouter(self.adapters, self.store)

    def admission_guard(self, provider: str) -> Guard:
        expected = routing_digest(self.config)

        def guard() -> None:
            if not self.adapters.get(provider).enabled():
                raise ValueError("Recipient provider is not registered")
            if routing_digest(self.config) != expected:
                raise ValueError("Routing configuration changed before admission")

        return guard

    def recover_pending(self) -> int:
        return self.store.recover_pending(
            {adapter.name: adapter.recovery_states for adapter in self.adapters}
        )

    def observation_ids(self, limit: int = 4) -> tuple[str, ...]:
        return tuple(task_id for adapter in self.adapters for task_id in adapter.poll_ids(limit))

    def reset_connections(self) -> None:
        # Connections are scoped to an operation. Invalidation affects observations only.
        self.provider_status = provider_status(
            self.plugins,
            self.config,
            kernel=((80, "claude_desktop", {"state": "unverified", "capabilities": []}),),
        )
        # A provider the saved config names but no loaded plugin claims (decision S-5): the
        # loader kept its settings and disabled it; status says so instead of hiding it.
        # Assigned, not defaulted: an unknown name that happens to spell a public status
        # alias above (`kiro_cli` for the `kiro` plugin) must still read as disabled.
        for name in self.config.disabled_providers():
            self.provider_status[name] = {
                "state": "disabled",
                "capabilities": [],
                "reason": "no loaded plugin claims this provider; its settings are kept",
            }

    async def desktop_list(self) -> dict[str, Any]:
        return await self._provider_compat("desktop_list")

    async def desktop_sessions(
        self, query: str = "", cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        return await self._provider_compat(
            "desktop_sessions", query=query, cursor=cursor, limit=limit
        )

    async def peers(self) -> dict[str, Any]:
        return {
            "desktop_threads": self.config.desktop_threads,
            "herdr_session": self.config.herdr_session,
            "herdr": await self.herdr.call("agent", "list")
            if self.config.provider_enabled("herdr")
            else {"agents": []},
            **(await self.peer_search()),
        }

    async def pair(self, alias: str, thread_id: str) -> dict[str, Any]:
        candidate = replace(
            self.config, desktop_threads={**self.config.desktop_threads, alias: thread_id}
        )
        candidate.save(self.config_path)
        self.config.desktop_threads = candidate.desktop_threads
        return {"paired": alias, "thread_id": candidate.thread(alias)}

    async def list_agents(self) -> dict[str, Any]:
        return await self._provider_compat("herdr_list_agents")

    def abandon(self, task_id: str, reason: str) -> dict[str, Any]:
        # The single service event loop cannot admit a worker between this
        # synchronous owner check and the store's compare-and-swap transition.
        expected = self.store.get(task_id)
        if any(
            adapter.controls is not None and adapter.controls.running(task_id)
            for adapter in self.adapters
        ):
            raise ValueError("Cancel the live worker before abandoning its receipt")
        return public_task(self.store.abandon(expected, reason))

    def operations(self) -> dict[str, RpcHandler]:
        """Verb name -> handler: the kernel's bound methods plus every plugin's ``rpc()``.

        Built per call, as the kernel table always was, so a method a test or a
        reconfiguration replaced on this object is the one dispatched.
        """
        kernel: dict[str, RpcHandler] = {
            "provider_list": self.providers.snapshot,
            "provider_register": self.provider_register,
            "provider_unregister": self.provider_unregister,
            "client_list": self.clients.snapshot,
            "client_register": self.client_register,
            "client_enroll": self.client_enroll,
            "client_heartbeat": self.clients.heartbeat,
            "client_disconnect": self.clients.disconnect,
            "client_policy": self.client_policy,
            "client_poll": self.client_poll,
            "client_ack": self.client_ack,
            "client_notify": self.client_notify,
            "client_notification_status": self.mailbox.status,
            "pressure_status": self.pressure_status,
            "pressure_clear": self.pressure_clear,
            "doctor": self.doctor,
            "peers": self.peers,
            "pair": self.pair,
            "task_get": self.get_task,
            "task_wait": self.wait_task,
            "task_reconcile": self.reconcile_task,
            "events_read": self.events_read,
            "events_runs": self.events_runs,
            "debug_on": self.debug_on,
            "debug_off": self.debug_off,
            "debug_status": self.debug_status,
            "task_complete": self.complete,
            "task_abandon": self.abandon,
            "peer_search": self.peer_search,
            "peer_submit": self.peer_submit,
            "task_list": self.task_list,
            "fleet_snapshot": self.fleet_snapshot,
            "task_for_peer": self.task_for_peer,
            "workspace_add": self.workspace_add,
            "workspace_list": self.workspace_list,
            "workspace_remove": self.workspace_remove,
            "task_permissions": self.task_permissions,
            "permission_answer": self.permission_answer,
            "task_cancel": self.task_cancel,
        }
        if set(kernel) != self.KERNEL_METHODS:
            raise SurfaceCollision("The kernel's verb table and KERNEL_METHODS disagree")
        providers = merged("verb", kernel, _plugin_verbs(self.plugins))
        return merged("verb", providers, module_verbs(self.modules))

    async def _provider_compat(self, method: str, **params: Any) -> dict[str, Any]:
        """Call one provider-owned verb for a retained direct-method compatibility path."""
        for _registration, verbs in _plugin_verbs(self.plugins):
            operation = verbs.get(method)
            if operation is not None:
                value = operation(**params)
                return await value if inspect.isawaitable(value) else value
        raise RuntimeError(f"Provider operation {method!r} is unavailable")

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        operation = self.operations().get(method)
        if operation is None:
            raise ValueError("Unknown bridge operation")
        required = getattr(operation, "requires", None)
        if required and not self.config.provider_enabled(required):
            raise ValueError(f"{required} provider is not registered")
        inspect.signature(operation).bind(**params)
        value = operation(**params)
        return await value if inspect.isawaitable(value) else value

    def worker_owner(self, task_id: str) -> TaskControls:
        kind = TaskReceipt.from_record(self.store.get(task_id)).provider
        return self.adapters.controls(kind)

    def _observed(self, scope: str) -> bool:
        """Evidence, not existence: in the current peer scan, or some receipt's evidence.

        The current peer scan (``events.state["peers"]``) is a capped, lazily
        refreshed projection kept for the status board -- never an authority on
        what exists (T-56 findings X1-X4, X7). It cannot support a refusal; it can
        only support this hint. ``is_receipt_target()`` covers a scope that
        `resolve_scope()` resolved from either column it reads -- a receipt's
        stored ``target``, or its nested ``metadata.peer.peer_id`` (a different,
        shorter string than the target, the normal Core ``hc`` path) -- so this
        is the one check `debug_on()` and `debug_status()` both share; a lease
        granted ``observed`` through a receipt stays ``observed`` on a later,
        independent `debug_status()` call too (rework round 1, finding R-2:
        before `is_receipt_target()` read both columns, only `debug_on()` had
        `resolve_scope()`'s own ``from_receipt`` flag to fall back on, and
        `debug_status()` did not, so the two disagreed about the same lease).
        """
        return scope in self.events.state["peers"] or is_receipt_target(scope, self.store.path)

    def debug_status(self) -> dict[str, Any]:
        """Expose lease state to the command client and both stream projections.

        Each non-``"all"`` scope gets a fresh ``observed`` flag (T-56 WP-32,
        decision D-20261120-07): computed here, every read, never stored in the
        lease record. ``"all"`` has no single peer to be evidence-backed about, so
        it carries no ``observed`` key.

        The flag is a hint of this answer only. The copy published to the event
        stream here is replaced at the collector's next poll by the controller's
        own status, which has no ``observed`` key, and the warning of
        `debug_on()` is never in the stream. A stream consumer cannot depend on
        the flag; the boards read the deadlines only (T-56 review round 7, N2).
        """
        status = self.debug.status(self.events.state["peers"])
        for entry in status["scopes"]:
            if entry["scope"] != "all":
                entry["observed"] = self._observed(entry["scope"])
        self.events.set_section("bridge", {**self.events.state["bridge"], "debug": status})
        return status

    def pressure_status(self) -> dict[str, Any]:
        """Observe-only: the pressure level and hold of every account, and the settings in
        force. Times are wall-clock seconds; `held` is computed for this answer."""
        now = self.pressure.clock()
        snapshot = self.pressure.snapshot()
        for row in snapshot.values():
            current = self.pressure.status(row["provider"], row["account_hint"])
            row["held"] = current.retry_after(now) > 0
            row["retry_after_s"] = round(current.retry_after(now), 3)
        return {
            "pressure": snapshot,
            "settings": self.pressure.settings.effective(),
            "providers": list(KNOWN_PROVIDERS),
            "signals": self.pressure.signals(),
            "error": self.pressure.error,
        }

    def pressure_clear(self, provider: str, account_hint: str = "default") -> dict[str, Any]:
        """An operator override: the account's level goes to 0 and its hold ends now."""
        self.pressure.clear(provider, account_hint)
        publish_section(self.events, self.pressure)
        return self.pressure_status()

    def debug_on(self, scope: str, seconds: float = 600) -> dict[str, Any]:
        """Enable timed tracing through the command socket, never the event stream.

        No state of the peer scan, and no content of the peer list, refuses a
        scope here (T-56 WP-32, decision D-20261120-07): the scan is too
        unreliable an existence check (findings X1-X4, X7). When the granted
        scope is not evidence-backed (``observed`` false: `_observed()`), the
        result also carries a warning; it is never a verdict that the peer is
        absent. Whether a scope is on another machine is read from the text of
        its address alone (`is_remote()`): a Core address that names a machine
        is refused, and a Rex terminal is not checked for its host. That
        exception is known; it is board row T-100, held for the owner, and it
        reads nothing remote.
        """
        if not self.event_settings.enabled:
            raise ValueError("Enable events in events.json and restart before enabling debug")
        resolved, from_receipt = resolve_scope(scope, self.store.path)
        self.debug.on(resolved, seconds)
        result = self.debug_status()
        if resolved != "all":
            observed = from_receipt or self._observed(resolved)
            for entry in result["scopes"]:
                if entry["scope"] == resolved:
                    entry["observed"] = observed
            if not observed:
                result["warning"] = (
                    f"{resolved} is not in this bridge's current list of discovered "
                    "peers. The list is a snapshot and can be incomplete, so this "
                    "does not show that the peer is absent. The lease takes effect "
                    "if a run of this peer appears."
                )
        return result

    def debug_off(self, scope: str | None = None) -> dict[str, Any]:
        """Disable a resolved peer, or every lease when scope is omitted."""
        resolved = resolve_scope(scope, self.store.path)[0] if scope is not None else None
        self.debug.off(resolved)
        return self.debug_status()

    async def events_read(
        self,
        after: str | None = None,
        limit: int = 200,
        wait: float = 0,
        run: str | None = None,
    ) -> dict[str, Any]:
        """Observe-only: one page of the AG-UI event stream after a cursor. Never a command."""
        if type(wait) not in (int, float) or not 0 <= wait <= 20:
            raise ValueError("Event wait must be 0–20 seconds")
        deadline = time.monotonic() + wait
        while True:
            page = await asyncio.to_thread(self.events.read, after, limit, run)
            if page["events"] or page.get("incomplete") or time.monotonic() >= deadline:
                return page
            await asyncio.sleep(0.2)

    async def events_runs(self, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Observe-only retained receipt metadata, one bounded page and count."""
        from .events.history import read_runs

        return await asyncio.to_thread(read_runs, self.store.path, offset, limit)

    def task_permissions(self, task_id: str) -> dict[str, Any]:
        return self.worker_owner(task_id).permissions(task_id)

    def permission_answer(self, task_id: str, permission_id: str, option_id: str) -> dict[str, Any]:
        return self.worker_owner(task_id).answer(task_id, permission_id, option_id)

    async def task_cancel(self, task_id: str) -> dict[str, Any]:
        return await self.worker_owner(task_id).cancel(task_id)

    def client_register(self, client: str, instance_id: str) -> dict[str, Any]:
        before = list(self.config.client_registrations)
        result = self.clients.register(client, instance_id)
        if self.config.client_registrations != before:
            self.config.save(self.config_path)
        return result

    def client_enroll(self, client: str) -> dict[str, Any]:
        before = list(self.config.client_registrations)
        result = self.clients.enroll(client)
        if self.config.client_registrations != before:
            self.config.save(self.config_path)
        return result

    def client_policy(self, interval: int, misses: int) -> dict[str, Any]:
        candidate = replace(self.config, heartbeat_interval=interval, heartbeat_misses=misses)
        candidate.save(self.config_path)
        self.config.heartbeat_interval, self.config.heartbeat_misses = interval, misses
        return self.clients.snapshot()

    def client_poll(self, client: str, instance_id: str) -> dict[str, Any]:
        self.clients.require_live(client, instance_id)
        return self.mailbox.pending(client)

    def client_ack(self, client: str, instance_id: str, ids: list[int]) -> dict[str, Any]:
        self.clients.require_live(client, instance_id)
        return self.mailbox.acknowledge(client, ids)

    def client_notify(
        self, client: str, message: str, request_id: str, topic: str = "notification"
    ) -> dict[str, Any]:
        if client not in self.config.client_registrations:
            raise ValueError("Client is not registered; connect its named MCP server first")
        return self.mailbox.notify(client, message, request_id, topic)

    def _on_pressure(self, transition: str, pressure: Pressure) -> None:
        """Tell every registered client when a provider account enters pressure, when its
        hold ends (`resume`, at `until`) and when it is cleared while still held.

        Runs inside the store's listener loop, so it never raises: a full or refusing
        inbox is recorded on the stream's service state and the store carries on.
        """
        if transition in ("enter", "raise"):
            self.pressure_resume.arm(pressure)
        elif transition == "exit" and self.pressure_resume.disarm(pressure):
            return  # The hold ended earlier and the owner heard it then.
        if transition not in ("enter", "exit", "resume"):
            return
        self._notify_pressure(transition, pressure)

    def _pressure_resumed(self, pressure: Pressure) -> None:
        self._on_pressure("resume", pressure)

    def _notify_pressure(self, transition: str, pressure: Pressure) -> None:
        try:
            text = pressure_text(transition, pressure, in_flight(self))
        except Exception as error:
            # The fleet is still being built (a signal during __init__) or is closing.
            text = pressure_text(transition, pressure, 0)
            self._pressure_notify_error(error)
        request_id = pressure_request_id(transition, pressure)
        for client in self.config.active_clients():
            try:
                self.mailbox.notify(client, text, request_id, topic="provider_pressure")
            except (ValueError, OSError) as error:
                self._pressure_notify_error(error)

    def _pressure_notify_error(self, error: Exception) -> None:
        # Class name only, never the notification text; no stream event (lane-detect's).
        self.pressure_notify_state["errors"] += 1
        self.pressure_notify_state["last_error"] = type(error).__name__
        _LOG.warning("provider pressure notification not queued: %s", type(error).__name__)

    def provider_register(
        self,
        provider: str,
        binary: str | None = None,
        session: str | None = None,
        port: int | None = None,
    ) -> dict[str, Any]:
        result = self.providers.register(provider, binary, session, port)
        if provider == "herdr":
            self.herdr = Herdr(self.config)
        elif provider == "hc":
            self._replace_core(
                Core(
                    self.config.hc_binary,
                    self.config.hc_session,
                    send_to_done=self.config.hc_send_to_done,
                    send_machines=self.config.hc_send_machines,
                    events=self.config.hc_events,
                    agents=self.agent_programs,
                )
            )
        elif provider == "crew":
            self._crew_client = None
        elif provider == "codex":
            self.desktop_index = DesktopIndex(self.config.codex_binary, self.config.codex_home)
        self.reset_connections()
        return result

    def provider_unregister(self, provider: str) -> dict[str, Any]:
        result = self.providers.unregister(provider)
        self.reset_connections()
        return result

    async def _claude_doctor(self) -> dict[str, Any]:
        """Preflight the selected Claude worker without opening a native session."""
        configured_binary = self.config.claude_code_binary
        found_binary = await asyncio.to_thread(shutil.which, configured_binary)
        if found_binary is not None and isinstance(found_binary, str):
            binary = str(await asyncio.to_thread(Path(found_binary).resolve))
        else:
            binary = None
        version: str | None = None
        cli_error: Exception | None = _cli_version_error(None)
        if binary is not None:
            try:
                version = (await run([binary, "--version"], max_seconds=10)).stdout.strip()
                parsed_version = _cli_version_from_output(version)
                cli_error = (
                    _cli_version_error(parsed_version)
                    if parsed_version is not None
                    else BridgeError(f"Claude CLI version output could not be parsed: {version!r}")
                )
            except (BridgeError, OSError, ValueError) as error:
                cli_error = error

        home = Path(native_home(self.config, "claude_code"))
        settings_path = home / "settings.json"
        try:
            settings_size: int | None = (await asyncio.to_thread(settings_path.stat)).st_size
        except OSError:
            settings_size = None
        try:
            await asyncio.to_thread(_provider_environment, home, {})
            settings_error: BridgeError | None = None
        except BridgeError as error:
            settings_error = error

        try:
            sdk = await asyncio.to_thread(importlib.import_module, "claude_agent_sdk")
        except ImportError:
            sdk_version: object = None
            sdk_error: BridgeError | None = _missing_sdk_error()
        else:
            sdk_version = getattr(sdk, "__version__", None)
            sdk_error = _sdk_version_error(sdk_version)

        refusal = next(
            (str(error) for error in (sdk_error, settings_error, cli_error) if error is not None),
            None,
        )
        return {
            "binary": binary,
            "version": version,
            "settings": {
                "path": str(settings_path),
                "size_bytes": settings_size,
                "limit_bytes": MAX_SETTINGS_BYTES,
            },
            "sdk": {"found": sdk_version, "pinned": SDK_VERSION},
            "refusal": refusal,
        }

    def remote_results(self) -> dict[str, Any]:
        """What listing machines in `hc_send_machines` means, for `doctor` and `status`.

        `remote_trust` names each listed machine with the trust it carries.
        `remote_unlisted_receipts` lists open receipts whose machine was removed from
        the list: their result files are no longer read. The bridge warns rather than
        refusing to start, so a delisted machine never blocks the other providers;
        the drain step is in docs/RECIPIENTS.md. Each key is present only when it has
        something to say.
        """
        report: dict[str, Any] = {}
        line = remote_trust_line(self.config.hc_send_machines)
        if line is not None:
            report["remote_trust"] = line
        stranded = self.store.unlisted_result_receipts(frozenset(self.config.hc_send_machines))
        if stranded:
            report["remote_unlisted_receipts"] = stranded
            report["remote_unlisted_warning"] = (
                "These open receipts belong to machines no longer in hc_send_machines, so "
                "their result files are not read. List the machine again and restart the "
                "bridge, then reconcile them (answerback task <id> --wait <seconds>), "
                "or abandon them."
            )
        return report

    async def doctor(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        if self.config.provider_enabled("herdr"):
            try:
                checks["herdr_version"] = await self.herdr.check()
                checks["agents"] = await self.herdr.call("agent", "list")
                self.provider_status["herdr"].update(state="connected", checked_at=time.time())
            except (BridgeError, OSError, ValueError) as error:
                checks["herdr_error"] = str(error)
                self.provider_status["herdr"].update(state="unavailable", checked_at=time.time())
        if self.config.provider_enabled("codex"):
            try:
                checks["codex_version"] = (
                    await run([self.config.codex_binary, "--version"])
                ).stdout.strip()
                queue_help = await run([self.config.codex_binary, "queue", "--help"])
                if "--thread" not in queue_help.stdout:
                    raise BridgeError("Codex CLI lacks the required queue command")
                self.provider_status["codex"].update(
                    state="cli_available", checked_at=time.time(), idle_wake="unverified"
                )
            except (BridgeError, OSError, ValueError) as error:
                checks["codex_error"] = str(error)
                self.provider_status["codex"].update(state="unavailable", checked_at=time.time())
        if self.config.provider_enabled("claude_code") and self.config.claude_code_workspaces:
            checks["claude"] = await self._claude_doctor()
            claude_state = "cli_available" if checks["claude"]["refusal"] is None else "unavailable"
            self.provider_status["claude_code"].update(state=claude_state, checked_at=time.time())
        if self.config.provider_enabled("hc"):
            try:
                found = await self.core.inventory()
                checks["hc_peers"] = len(found["peers"])
                checks["hc_discovery_complete"] = found["complete"]
                if not found["complete"]:
                    checks["hc_error"] = str(found["errors"])
                state = "connected" if found["complete"] else "partial"
                if "inventory" in found["errors"]:
                    state = "unavailable"
                self.provider_status["hc"].update(state=state, checked_at=time.time())
            except (BridgeError, OSError, ValueError, TimeoutError) as error:
                checks["hc_error"] = str(error)
                self.provider_status["hc"].update(state="unavailable", checked_at=time.time())
        if self.config.provider_enabled("kiro"):
            try:
                checks["kiro_version"] = (
                    await run([self.config.kiro_binary, "--version"])
                ).stdout.strip()
                help_text = await run([self.config.kiro_binary, "acp", "--help"])
                if "agent-engine" not in help_text.stdout:
                    raise BridgeError("Kiro lacks the required v3 ACP interface")
                self.provider_status["kiro_cli"].update(
                    state="cli_available", checked_at=time.time()
                )
            except (BridgeError, OSError, ValueError) as error:
                checks["kiro_error"] = str(error)
                self.provider_status["kiro_cli"].update(state="unavailable", checked_at=time.time())
        if self.config.provider_enabled("crew"):
            try:
                checks["crew_peer_count"] = len(await self.crew.peers())
                self.provider_status["kirocrew"].update(state="connected", checked_at=time.time())
            except (BridgeError, OSError, ValueError) as error:
                checks["crew_error"] = str(error)
                self.provider_status["kirocrew"].update(state="unavailable", checked_at=time.time())
            self._publish_crew_version()
        if self.config.provider_enabled("rex"):
            try:
                self.rex.enabled()
                checks["rex"] = await self.rex.adapter.probe()
                self.provider_status["rex"].update(state="connected", checked_at=time.time())
            except (BridgeError, OSError, ValueError, TimeoutError) as error:
                checks["rex_error"] = str(error)
                self.provider_status["rex"].update(state="unavailable", checked_at=time.time())
        checks["herdr_session"] = self.config.herdr_session
        checks["desktop_aliases"] = self.config.desktop_threads
        checks["note"] = (
            "Connectivity checks do not prove idle Desktop wake; run a reply-only probe."
        )
        checks.update(self.remote_results())
        checks["healthy"] = not any(key.endswith("_error") for key in checks) and (
            checks.get("claude", {}).get("refusal") is None
        )
        checks["providers"] = self.provider_status
        return checks

    async def desktop_read(self, alias: str) -> dict[str, Any]:
        return await self._provider_compat("desktop_read", alias=alias)

    async def desktop_submit(self, alias: str, message: str, request_id: str) -> dict[str, Any]:
        return await self._provider_compat(
            "desktop_submit", alias=alias, message=message, request_id=request_id
        )

    async def kiro_submit(self, alias: str, message: str, request_id: str) -> dict[str, Any]:
        return await self._provider_compat(
            "kiro_submit", alias=alias, message=message, request_id=request_id
        )

    async def _desktop_submit(
        self, thread_id: str, message: str, request_id: str, alias: str | None = None
    ) -> dict[str, Any]:
        return await cast(CodexAdapter, self.adapters.get("desktop")).submit_native(
            thread_id,
            message,
            request_id,
            alias,
            admission_guard=self.admission_guard("desktop"),
        )

    async def herdr_submit(
        self, target: str, message: str, request_id: str, *, expected: dict[str, Any]
    ) -> dict[str, Any]:
        # Internal only: not in METHODS/operations, so no RPC or MCP tool can
        # reach this. `expected` is required and keyword-only, and the adapter
        # refuses None before any lookup, reservation or send, so a caller
        # cannot skip the identity check by handing it nothing. External
        # callers reach Herdr delivery exclusively through peer_submit, which
        # resolves the fresh peer and hands it over as expected.
        return await cast(HerdrAdapter, self.adapters.get("herdr")).submit_native(
            target,
            message,
            request_id,
            expected,
            admission_guard=self.admission_guard("herdr"),
        )

    async def get_task(self, task_id: str, *, refresh: bool = True) -> dict[str, Any]:
        lock = self.task_locks[hash(task_id) % len(self.task_locks)]
        async with lock:
            return await self._get_task(task_id, refresh=refresh)

    async def _get_task(self, task_id: str, *, refresh: bool = True) -> dict[str, Any]:
        return public_task((await self.router.observe(task_id, refresh=refresh)).as_record())

    async def reconcile_task(self, task_id: str) -> dict[str, Any]:
        """Explicit correction of one receipt, through the plugin that owns it.

        The store hooks decide which receipts have a recovery path: exactly one loaded plugin
        may claim this receipt (two is a composition error, raised before any provider work).
        The receipt's adapter then verifies the correction against the provider's own records
        and calls ``TaskStore.reconcile``, which dispatches the write through that same
        registry-selected claimant. A provider with no adapter-side reconciler is refused as
        before.
        """
        task = self.store.get(task_id)
        self.store.hooks.claimant(task)
        receipt = TaskReceipt.from_record(task)
        adapter = self.adapters.get(receipt.provider)
        if not isinstance(adapter, TaskReconciler):
            raise ValueError(
                f"Provider {receipt.provider} does not support explicit reconciliation"
            )
        return await adapter.reconcile(task_id)

    async def wait_task(self, task_id: str, seconds: int = 30) -> dict[str, Any]:
        if not 1 <= seconds <= 30:
            raise ValueError("Wait duration must be between 1 and 30 seconds")
        try:
            async with asyncio.timeout(seconds):
                while True:
                    task = await self.get_task(task_id)
                    if task["status"] in TERMINAL:
                        return task
                    await asyncio.sleep(2)
        except TimeoutError:
            task = public_task(self.store.get(task_id))
            task["wait_timed_out"] = True
            return task

    def complete(self, task_id: str, result: str) -> dict[str, Any]:
        return public_task(self.store.complete_worker(task_id, result))

    @property
    def crew(self) -> Any:
        if not self.config.provider_enabled("crew"):
            raise ValueError("Kirocrew is not configured; run crew-configure first")
        if self._crew_client is None:
            from .crew import Crew

            client = Crew(
                self.config.crew_binary,
                self.config.crew_port,
                receipt_path=self.store.path,
            )
            # The client reads the Crew version at every login, and discovery, a read or
            # a send can trigger one: publish each reading, not only the setup commands'.
            client.on_version = self._publish_crew_version
            self._crew_client = client
            # A new client has read nothing yet: do not show its predecessor's version.
            self._publish_crew_version()
        return self._crew_client

    def _crew_version(self) -> dict[str, Any]:
        """What the current Crew client read at its latest login attempt.

        `version_status` is "tested", "untested" (served: nobody has exercised that
        release series with this client, so the response checks are the protection) or
        "unsupported" (refused). `version` and `version_status` are None before a first
        attempt, when the version command failed, and for a client that does not report
        them. `version_checked_at` is when the latest attempt started its version check,
        also when that command failed; it can be older than the provider's `checked_at`,
        because a login lasts up to 55 minutes.
        """
        client = self._crew_client
        version = getattr(client, "version", None)
        status = getattr(client, "version_status", None)
        checked = getattr(client, "version_checked_at", None)
        return {
            "version": version if isinstance(version, str) else None,
            "version_status": status if isinstance(status, str) else None,
            "version_checked_at": checked if type(checked) in (int, float) else None,
        }

    def _publish_crew_version(self) -> None:
        entry = self.provider_status.get("kirocrew")
        if isinstance(entry, dict):
            entry.update(self._crew_version())

    def directory(self) -> Directory:
        return Directory(self.config, None, None, registry=self.adapters)

    async def peer_search(self, query: str = "", provider: str | None = None) -> dict[str, Any]:
        return await self.directory().search(query, provider)

    async def peer_submit(
        self,
        selector: str,
        message: str,
        request_id: str,
        provider: str | None = None,
        binding: str | None = None,
    ) -> dict[str, Any]:
        expected_digest = routing_digest(self.config)
        prefix, separator, target = selector.partition(":")
        if separator and self.adapters.contains(prefix):
            adapter = self.adapters.get(prefix)
            if provider is not None and self.adapters.canonical(provider) != adapter.name:
                raise ValueError("Peer handle conflicts with the selected provider")
            previous = self.router.previous(
                adapter.name + ":" + target, Submission(message, request_id)
            )
            if previous is not None:
                return public_task(previous.as_record())
        try:
            peer = await self.directory().resolve(selector, provider)
        except SelectionRequired as error:
            return {"status": "needs_selection", "matches": error.matches, "errors": error.errors}
        # String discovery bindings must be carried back before new admission.
        # Rex instead binds an immutable enrollment ID to a stored identity object;
        # its resolver verifies that enrollment before returning the peer.
        expected_binding = peer.get("binding")
        if (
            binding is not None or isinstance(expected_binding, str)
        ) and expected_binding != binding:
            raise ValueError("Selected peer changed since discovery; search again")
        # Provider pressure on this peer's account: nothing is reserved or sent; the caller
        # waits `retry_after_s` and asks again with the same request_id. A hold never hides
        # a receipt that request already has: the repeat is answered with it.
        if (held := hold_for(self.pressure, peer)) is not None:
            repeated = await self._repeated(peer, message, request_id)
            if repeated is not None:
                return repeated
            return backoff_result(held, self.pressure)
        return await self.submit_resolved(
            peer, message, request_id, expected_routing_digest=expected_digest
        )

    async def _repeated(
        self, peer: dict[str, Any], message: str, request_id: str
    ) -> dict[str, Any] | None:
        """The public receipt `request_id` already has for this peer and message, else None.

        The same check admission makes (`TaskStore.reserve_in_transaction`): the stored
        receipt must name this peer's target key, provider and message, or the repeat
        describes different work and is refused as it would be unheld.
        """
        task = self.store.by_request(request_id)
        if task is None:
            return None
        prepared = await self.router.prepare(Peer.from_record(peer))
        if not matches_work(task, prepared.target_key, message, request_id, prepared.peer.provider):
            raise ValueError("request_id already describes different work")
        return public_task(task)

    async def submit_resolved(
        self,
        peer: dict[str, Any],
        message: str,
        request_id: str,
        *,
        expected_routing_digest: str | None = None,
    ) -> dict[str, Any]:
        expected_routing_digest = expected_routing_digest or routing_digest(self.config)

        def verify_routing() -> None:
            if expected_routing_digest is not None:
                if routing_digest(self.config) != expected_routing_digest:
                    raise ValueError("Routing changed before admission")

        verify_routing()
        prepared = await self.router.prepare(Peer.from_record(peer))
        receipt = await self.router.submit(
            prepared, Submission(message, request_id), before_admission=verify_routing
        )
        return public_task(receipt.as_record())

    async def fleet_snapshot(self) -> dict[str, Any]:
        """Read bounded receipt sections off the loop; runtime attaches live health."""
        import sqlite3
        from contextlib import closing

        from . import receipt_snapshot

        read_at = time.time()
        deadline = time.monotonic() + 1.5

        def read() -> dict[str, Any]:
            sections = self.store.snapshot_read(deadline=deadline)
            errors = sections.pop("errors")
            waiting = sections["needs_person"]
            rows = (sections["recent"] or []) + ([] if waiting is None else waiting[0])
            command_ids = {
                row["request_key"][4:] for row in rows if row["request_key"].startswith("bus:")
            }
            bus_keys: dict[str, str] = {}
            try:
                if command_ids:
                    try:
                        (self.config_path.parent / "bus/config.json").stat()
                    except FileNotFoundError:
                        command_ids.clear()
                if command_ids:
                    remaining = deadline - time.monotonic()
                    if remaining < 0.05:
                        raise TimeoutError
                    ledger_seconds = min(0.5, remaining)
                    uri = (self.config_path.parent / "bus/ledger.sqlite").as_uri() + "?mode=ro"
                    # BusStore construction migrates the ledger. Read named columns directly.
                    with closing(sqlite3.connect(uri, uri=True, timeout=ledger_seconds)) as db:
                        ledger_deadline = min(deadline, time.monotonic() + ledger_seconds)
                        db.set_progress_handler(
                            lambda: int(time.monotonic() >= ledger_deadline), 1000
                        )
                        db.execute("BEGIN")
                        remaining = ledger_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        db.execute(f"PRAGMA busy_timeout={int(remaining * 1000)}")
                        db.execute("PRAGMA schema_version").fetchone()
                        if time.monotonic() >= ledger_deadline:
                            raise TimeoutError
                        bus_keys = dict(
                            db.execute(
                                "SELECT id,request_key FROM commands WHERE id IN ("
                                + ",".join("?" for _ in command_ids)
                                + ")",
                                tuple(command_ids),
                            )
                        )
            except Exception:
                errors.append({"section": "titles", "error": "bus ledger read failed"})
            return receipt_snapshot.build(
                read_at=read_at,
                **sections,
                bus_keys=bus_keys,
                errors=errors,
                rules=self.receipt_reasons,
            )

        return await asyncio.to_thread(read)

    def task_list(self, query: str = "", limit: int = 30) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        words = query.casefold().split(maxsplit=32)
        if len(words) > 32:
            raise ValueError("query must contain at most 32 search terms")
        tasks = self.store.recent(100)
        selected = []
        for task in tasks:
            if words:
                haystack = (
                    task["target"]
                    + " "
                    + str(public_task(task)["metadata"])
                    + " "
                    + task["request_key"]
                ).casefold()
                if not all(word in haystack for word in words):
                    continue
            selected.append(task)
        summaries = []
        for task in selected[:limit]:
            summary = {
                key: task[key]
                for key in ("id", "request_key", "target", "status", "created", "updated")
            }
            summary.update(task_availability(task))
            summary["detail"] = (task["detail"] or "")[:500]
            summaries.append(summary)
        return {"tasks": summaries, "searched_latest": 100}

    async def task_for_peer(self, selector: str, provider: str | None = None) -> dict[str, Any]:
        try:
            peer = Peer.from_record(await self.directory().resolve(selector, provider))
        except SelectionRequired as error:
            return {"status": "needs_selection", "matches": error.matches, "errors": error.errors}
        adapter = self.adapters.for_peer(peer)
        for task in self.store.recent(100):
            if adapter.matches(peer, TaskReceipt.from_record(task)):
                return await self.get_task(task["id"])
        raise ValueError("No receipt for this peer among the latest 100 tasks")

    async def kiro_register(self, alias: str, cwd: str, binary: str = "kiro-cli") -> dict[str, Any]:
        return await self._provider_compat("kiro_register", alias=alias, cwd=cwd, binary=binary)

    async def workspace_add(
        self,
        alias: str,
        cwd: str,
        binary: str | None = None,
        create: bool = False,
        provider: str = "kiro",
        home: str | None = None,
        active_budget: int | None = None,
    ) -> dict[str, Any]:
        spaces = Workspaces(self.config, self.config_path, self.store, provider=provider)
        result = await spaces.add(
            alias, cwd, binary=binary, create=create, home=home, active_budget=active_budget
        )
        self.reset_connections()
        return result

    def workspace_list(self, provider: str = "kiro") -> dict[str, Any]:
        return Workspaces(self.config, self.config_path, self.store, provider=provider).list()

    def workspace_remove(
        self, alias: str, apply: bool = False, provider: str = "kiro"
    ) -> dict[str, Any]:
        result = Workspaces(self.config, self.config_path, self.store, provider=provider).remove(
            alias, apply=apply
        )
        if result["applied"]:
            self.reset_connections()
        return result

    async def kiro_sessions(
        self, query: str = "", cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._provider_compat("kiro_sessions", query=query, cwd=cwd, cursor=cursor)

    async def crew_configure(
        self, binary: str = "kirocrew", port: int | None = None
    ) -> dict[str, Any]:
        return await self._provider_compat("crew_configure", binary=binary, port=port)

    async def crew_create(self, title: str, project: str, request_id: str) -> dict[str, Any]:
        return await self._provider_compat(
            "crew_create", title=title, project=project, request_id=request_id
        )

    async def crew_read(self, target: str) -> dict[str, Any]:
        return await self._provider_compat("crew_read", target=target)

    async def crew_submit(
        self, target: str, message: str, request_id: str, *, expected: dict[str, Any]
    ) -> dict[str, Any]:
        # Internal only: not in METHODS/operations, so no RPC or MCP tool can
        # reach this. `expected` is required and keyword-only, and the adapter
        # refuses None before any lookup, reservation or send, so a caller
        # cannot skip the Crew identity check by handing it nothing; the live
        # peer is never substituted for a missing identity. Any other identity
        # is compared with the live chat before any reservation (one the caller
        # did not discover is refused with no receipt row) and again by the Crew
        # client before and after the send. External callers reach Crew delivery
        # exclusively through peer_submit (provider "crew"), which resolves the
        # fresh peer and hands it over as expected.
        return await cast(CrewAdapter, self.adapters.get("crew")).submit_native(
            target,
            message,
            request_id,
            expected,
            admission_guard=self.admission_guard("crew"),
        )

    async def hc_configure(self, binary: str = "hc", session: str | None = None) -> dict[str, Any]:
        return await self._provider_compat("hc_configure", binary=binary, session=session)

    async def hc_read(self, target: str, lines: int = 100) -> dict[str, Any]:
        return await self._provider_compat("hc_read", target=target, lines=lines)

    async def rex_configure(self, binary: str = "rex", server: str | None = None) -> dict[str, Any]:
        return await self._provider_compat("rex_configure", binary=binary, server=server)

    async def rex_read(
        self, session_id: str, block_id: str, max_bytes: int = 16384
    ) -> dict[str, Any]:
        return await self._provider_compat(
            "rex_read", session_id=session_id, block_id=block_id, max_bytes=max_bytes
        )

    async def rex_enroll(
        self, name: str, session_id: str, block_id: str, request_id: str
    ) -> dict[str, Any]:
        return await self._provider_compat(
            "rex_enroll",
            name=name,
            session_id=session_id,
            block_id=block_id,
            request_id=request_id,
        )

    async def rex_receive(self, peer_id: str, wait_seconds: int = 0) -> dict[str, Any]:
        return await self._provider_compat(
            "rex_receive", peer_id=peer_id, wait_seconds=wait_seconds
        )

    async def rex_ack(self, peer_id: str, task_id: str) -> dict[str, Any]:
        return await self._provider_compat("rex_ack", peer_id=peer_id, task_id=task_id)

    async def rex_complete(self, peer_id: str, task_id: str, result: str) -> dict[str, Any]:
        return await self._provider_compat(
            "rex_complete", peer_id=peer_id, task_id=task_id, result=result
        )

    async def rex_retire(self, peer_id: str) -> dict[str, Any]:
        return await self._provider_compat("rex_retire", peer_id=peer_id)

    async def close(self) -> None:
        self.pressure_resume.cancel_all()
        try:
            await asyncio.gather(*(adapter.close() for adapter in self.adapters))
        finally:
            try:
                close = getattr(self.core, "close", None)
                if callable(close):  # A stand-in Core (tests) may own no follower.
                    await close()
                await asyncio.gather(*self._retired_cores, return_exceptions=True)
            finally:
                await asyncio.to_thread(self.debug.close)
