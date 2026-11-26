"""The plugin API: composition through ``ProviderPlugin`` changes nothing a user sees."""

import ast
import asyncio
import importlib.metadata
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from composition import PLUGIN_PROVIDERS, contributed
from mcp import Client
from test_adapter_extension import FixtureAdapter

import answerback_bridge.adapters.builtins as builtins_module
import answerback_bridge.plugin as plugin_module
from answerback_bridge.adapters import catalog
from answerback_bridge.adapters.builtins import (
    BuiltinPlugin,
    BuiltinSource,
    Owners,
    _adapter_slots,
    builtin_plugins,
)
from answerback_bridge.adapters.catalog import ProviderDefinition
from answerback_bridge.adapters.contracts import ProviderAdapter
from answerback_bridge.adapters.factory import build_adapters
from answerback_bridge.config import SCHEMAS, Config
from answerback_bridge.mcp_server import create_server
from answerback_bridge.native_workers import NativeWorkers
from answerback_bridge.plugin import (
    PLUGIN_API,
    PROVIDER_GROUP,
    KernelContext,
    OwnedEventSource,
    PluginOwners,
    PluginVersionMismatch,
    ProviderPlugin,
    ProviderPluginSpec,
    SurfaceCollision,
    agent_programs,
    builtin_plugin,
    check_api_version,
    kernel_context,
    owned_verbs,
    provider_plugin,
    receipt_reasons,
)
from answerback_bridge.process import module_launch_argv
from answerback_bridge.providers.herdr.core import Core
from answerback_bridge.receipt_snapshot import ReasonRule
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import TaskStore

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "answerback_bridge"
KERNEL_ADAPTER_ORDER = (
    "kiro",
    "codex",
    "herdr",
    "crew",
    "hc",
    "rex",
    "codex_cli",
    "claude_code",
    "grok",
)
DEFINITIONS = {
    plugin.definition.registration: plugin.definition
    for plugin in builtin_plugins()
    if plugin.definition.registration in KERNEL_ADAPTER_ORDER
}


def test_provider_construction_building_blocks_are_public_plugin_apis() -> None:
    assert BuiltinPlugin is ProviderPluginSpec
    assert BuiltinSource is OwnedEventSource
    assert Owners is PluginOwners
    assert provider_plugin.__module__ == "answerback_bridge.plugin"
    assert builtin_plugin.__module__ == "answerback_bridge.plugin"
    assert owned_verbs.__module__ == "answerback_bridge.plugin"


def test_builtin_policy_is_owned_by_provider_packages() -> None:
    for name in ("DEFINITIONS", "CAPABILITIES", "AGENT_PROGRAMS", "STORE_HOOKS"):
        assert not hasattr(builtins_module, name)
    owners = {
        "hc": "herdr",
        "herdr": "herdr",
        "codex_cli": "native",
        "claude_code": "native",
        "grok": "native",
    }
    for plugin in builtin_plugins():
        registration = plugin.definition.registration
        if registration in PLUGIN_PROVIDERS:
            continue  # an installed plugin's policy lives in its own package
        package = owners.get(registration, registration)
        prefix = f"answerback_bridge.providers.{package}"
        assert plugin.config_schema is not None
        assert plugin.config_schema.__module__ == f"{prefix}.config"
        assert plugin.owner_factory is not None
        assert plugin.owner_factory.__module__ == f"{prefix}.plugin"
        if plugin.commands is not None:
            assert plugin.commands.__module__ == f"{prefix}.cli"
        assert all(spec.build.__module__ == f"{prefix}.tools" for spec in plugin.tools)


# Computed on main at 7b51462e with a Bridge whose provider_selection is empty (tools and
# methods do not depend on the selection). Byte-identical output is the acceptance test.
TOOLS_ON_MAIN = [
    "bus_submit",
    "bus_result",
    "bus_events",
    "bus_cancel",
    "bus_status",
    "hc_read",
    "rex_read",
    "rex_enroll",
    "rex_receive",
    "rex_ack",
    "rex_complete",
    "rex_retire",
    "registered_clients",
    "notify_client",
    "notification_status",
    "read_inbox",
    "acknowledge_inbox",
    "desktop_list",
    "desktop_sessions",
    "desktop_read",
    "desktop_submit",
    "herdr_list_agents",
    "herdr_read_agent",
    "task_get",
    "task_wait",
    "task_reconcile",
    "task_complete",
    "peer_search",
    "peer_submit",
    "task_for_peer",
    "task_list",
    "task_permissions",
    "task_cancel",
    "kiro_sessions",
    "crew_read",
]
METHODS_ON_MAIN = [
    "actor_ack",
    "actor_receive",
    "actor_reply",
    "actor_send",
    *contributed("METHODS", ()),
    "bus_cancel",
    "bus_events",
    "bus_result",
    "bus_status",
    "bus_submit",
    "client_ack",
    "client_disconnect",
    "client_enroll",
    "client_heartbeat",
    "client_list",
    "client_notification_status",
    "client_notify",
    "client_policy",
    "client_poll",
    "client_register",
    "crew_configure",
    "crew_create",
    "crew_read",
    "debug_off",
    "debug_on",
    "debug_status",
    "desktop_list",
    "desktop_read",
    "desktop_sessions",
    "desktop_submit",
    "doctor",
    "events_read",
    "events_runs",
    "fleet_snapshot",
    "hc_configure",
    "hc_read",
    "herdr_list_agents",
    "herdr_read_agent",
    "kiro_register",
    "kiro_sessions",
    "kiro_submit",
    "pair",
    "peer_search",
    "peer_submit",
    "peers",
    "permission_answer",
    "pressure_clear",
    "pressure_status",
    "provider_list",
    "provider_register",
    "provider_unregister",
    "rex_ack",
    "rex_complete",
    "rex_configure",
    "rex_enroll",
    "rex_read",
    "rex_receive",
    "rex_retire",
    "task_abandon",
    "task_cancel",
    "task_complete",
    "task_for_peer",
    "task_get",
    "task_list",
    "task_permissions",
    "task_reconcile",
    "task_wait",
    "workspace_add",
    "workspace_list",
    "workspace_remove",
]
# The catalog order on main; the installed plugin's row keeps its slot from its own package
# (``ProviderDefinition.order``).
ALL_PROVIDERS = [
    "hc",
    "herdr",
    "codex",
    "kiro",
    "crew",
    *PLUGIN_PROVIDERS,
    "rex",
    "codex_cli",
    "claude_code",
    "grok",
]
# The installed plugin's status block (its `json.dumps` fragment), which sorts first.
PLUGIN_STATUS = contributed("STATUS_BLOCKS", {"unconfigured": "", "configured": ""})
# Insertion order of the user-visible provider status mapping on main.
STATUS_ORDER_ON_MAIN = [
    "codex_cli",
    "claude_code",
    "grok",
    "rex",
    "hc",
    "herdr",
    "codex",
    "claude_desktop",
    "kirocrew",
    "kiro_cli",
    *contributed("STATUS_ORDER", ()),
]
# `json.dumps(bridge.provider_status, sort_keys=True)` on main, selection empty and full.
STATUS_ON_MAIN = {
    (): (
        "{"
        + PLUGIN_STATUS["unconfigured"]
        + '"claude_code": {"capabilities": ["send", "result", "permissions", "cancel"], '
        '"session_ownership": "bridge", "state": "disabled"}, '
        '"claude_desktop": {"capabilities": [], "state": "unverified"}, '
        '"codex": {"cancel": false, "capabilities": ["discovery", "queue", "persisted_result"], '
        '"live_progress": false, "state": "disabled"}, '
        '"codex_cli": {"capabilities": ["send", "result", "permissions", "cancel"], '
        '"session_ownership": "bridge", "state": "disabled"}, '
        '"grok": {"capabilities": ["send", "result", "permissions", "cancel"], '
        '"session_ownership": "bridge", "state": "disabled"}, '
        '"hc": {"capabilities": ["discovery", "read", "send", "worker_report"], '
        '"state": "disabled"}, '
        '"herdr": {"capabilities": ["send", "read", "list", "result"], "state": "disabled"}, '
        '"kiro_cli": {"capabilities": ["send", "result", "permissions", "cancel"], '
        '"protocol": "acp-v1", "state": "unconfigured"}, '
        '"kirocrew": {"capabilities": ["discovery", "send", "read", "worker_report"], '
        '"state": "unconfigured"}, '
        '"rex": {"capabilities": ["discovery", "read", "enroll", "queue", "ack", "result"], '
        '"live_readiness": false, "state": "disabled"}}'
    ),
    tuple(ALL_PROVIDERS): (
        "{"
        + PLUGIN_STATUS["configured"]
        + '"claude_code": {"capabilities": ["send", "result", "permissions", "cancel"], '
        '"session_ownership": "bridge", "state": "configured"}, '
        '"claude_desktop": {"capabilities": [], "state": "unverified"}, '
        '"codex": {"cancel": false, "capabilities": ["discovery", "queue", "persisted_result"], '
        '"live_progress": false, "state": "unchecked"}, '
        '"codex_cli": {"capabilities": ["send", "result", "permissions", "cancel"], '
        '"session_ownership": "bridge", "state": "configured"}, '
        '"grok": {"capabilities": ["send", "result", "permissions", "cancel"], '
        '"session_ownership": "bridge", "state": "configured"}, '
        '"hc": {"capabilities": ["discovery", "read", "send", "worker_report"], '
        '"state": "configured"}, '
        '"herdr": {"capabilities": ["send", "read", "list", "result"], "state": "unchecked"}, '
        '"kiro_cli": {"capabilities": ["send", "result", "permissions", "cancel"], '
        '"protocol": "acp-v1", "state": "configured"}, '
        '"kirocrew": {"capabilities": ["discovery", "send", "read", "worker_report"], '
        '"state": "configured"}, '
        '"rex": {"capabilities": ["discovery", "read", "enroll", "queue", "ack", "result"], '
        '"live_readiness": false, "state": "configured"}}'
    ),
}
# The literal capability lists of `Bridge.reset_connections` (service.py) per plugin, keyed
# by the provider's registration name and the status block that reports it.
CAPABILITIES_ON_MAIN = {
    "kiro": ("kiro_cli", ["send", "result", "permissions", "cancel"]),
    "codex": ("codex", ["discovery", "queue", "persisted_result"]),
    "herdr": ("herdr", ["send", "read", "list", "result"]),
    "crew": ("kirocrew", ["discovery", "send", "read", "worker_report"]),
    "hc": ("hc", ["discovery", "read", "send", "worker_report"]),
    "rex": ("rex", ["discovery", "read", "enroll", "queue", "ack", "result"]),
    "codex_cli": ("codex_cli", ["send", "result", "permissions", "cancel"]),
    "claude_code": ("claude_code", ["send", "result", "permissions", "cancel"]),
    "grok": ("grok", ["send", "result", "permissions", "cancel"]),
    **contributed("CAPABILITIES", {}),
}
# The registry order `factory.build_adapters` produced on main, by adapter name.
ADAPTER_ORDER_ON_MAIN = [
    "kiro",
    "desktop",
    "herdr",
    "crew",
    "hc",
    *PLUGIN_PROVIDERS,
    "rex",
    "codex_cli",
    "claude_code",
    "grok",
]
# The former ``core._AGENTS`` literal. Values are public ``agent_kind`` identifiers;
# several deliberately differ from the plugin registration that recognizes the program.
AGENT_PROGRAMS_ON_MAIN = {
    "claude": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "kiro": "kiro",
    "kiro-cli": "kiro",
    "kirocrew": "kirocrew",
    **contributed("AGENT_PROGRAMS", {}),
    "grok": "grok",
    "grok-build": "grok",
    "gemini": "gemini",
    "opencode": "opencode",
    "pi": "pi",
    "amp": "amp",
    "copilot": "copilot",
    "cursor": "cursor",
}


class FixturePlugin(ProviderPlugin):
    """A foreign provider composed only through the plugin API."""

    api_version = PLUGIN_API
    definition = ProviderDefinition("fixture", "fixture", recovery_states=("running",))
    capabilities = frozenset({"discovery", "send"})
    config_schema = None

    def __init__(self) -> None:
        self.adapter: FixtureAdapter | None = None

    def build(self, ctx: KernelContext) -> ProviderAdapter:
        self.adapter = FixtureAdapter(ctx.config, ctx.config_path, ctx.store)
        return self.adapter


def unbound_owners() -> Owners:
    none: Any = lambda: None  # noqa: E731 - one getter shape for every owner
    lock = asyncio.Lock()
    return Owners(
        service=none,
        desktop=none,
        workers=none,
        core=none,
        core_tasks=none,
        core_lock=lambda: lock,
        herdr=none,
        crew=none,
        rex=none,
    )


def composed(path: Path) -> tuple[Bridge, FixturePlugin]:
    bridge = Bridge(Config(provider_selection=[]), path)
    plugin = FixturePlugin()
    bridge.adapters = build_adapters(bridge.kernel, [plugin])
    return bridge, plugin


async def test_fake_plugin_built_through_build_adapters_dispatches_and_recovers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    bridge, plugin = composed(path)
    try:
        assert plugin.adapter is not None
        found = await bridge.peer_search()
        assert found["peers"][0]["peer_id"] == "fixture:one"
        direct = await bridge.peer_submit("fixture:one", "Review", "direct")
        assert direct["result"] == "FIXTURE_RESULT"
        assert plugin.adapter.sends == 1
        again = await bridge.peer_submit("fixture:one", "Review", "direct")
        assert again["id"] == direct["id"] and plugin.adapter.sends == 1
        with pytest.raises(ValueError, match="support"):
            bridge.task_permissions(direct["id"])
    finally:
        await bridge.close()
    reopened, fresh = composed(path)
    try:
        assert fresh.adapter is not None and fresh.adapter.sends == 0
        task = await reopened.get_task(direct["id"])
        assert task["result"] == "FIXTURE_RESULT"
        assert (await reopened.task_for_peer("fixture:one"))["id"] == task["id"]
        row, _ = reopened.store.reserve("fixture:one", "Later", "recovery", {"kind": "fixture"})
        reopened.store.update(row["id"], "running")
        assert reopened.recover_pending() == 1
        assert reopened.store.get(row["id"])["status"] == "uncertain"
    finally:
        await reopened.close()


async def test_kernel_context_hands_the_plugin_the_live_config_store_and_stripes(
    tmp_path: Path,
) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    try:
        ctx = bridge.kernel
        assert ctx.config is bridge.config
        assert ctx.config_path == bridge.config_path
        assert ctx.store is bridge.store
        assert ctx.events is bridge.events
        assert ctx.locks is bridge.task_locks
        assert ctx.launch_argv is module_launch_argv
        assert ctx.shared.native_workers is NativeWorkers
        codex = bridge.adapters.get("codex")
        assert codex.task_locks is bridge.task_locks
        with pytest.raises(FrozenInstanceError):
            ctx.config = Config()  # type: ignore[misc]
    finally:
        await bridge.close()


@pytest.mark.parametrize("version", [0, 2, None, "1", True, 1.0])
def test_other_api_versions_are_refused_by_name(tmp_path: Path, version: Any) -> None:
    plugin = FixturePlugin()
    plugin.api_version = version  # type: ignore[assignment]
    with pytest.raises(PluginVersionMismatch, match=r"fixture.*api_version"):
        check_api_version(plugin)
    ctx = kernel_context(Config(), tmp_path / "config.json", TaskStore(tmp_path / "t.sqlite"))
    with pytest.raises(PluginVersionMismatch):
        build_adapters(ctx, [plugin])
    assert plugin.adapter is None, "a refused plugin is never built"
    assert PluginVersionMismatch.__mro__[1] is ValueError


def test_plugin_hooks_default_to_nothing() -> None:
    plugin = FixturePlugin()
    assert plugin.rpc() == {}
    assert plugin.agent_programs() == {}
    assert plugin.mcp_tools() == ()
    assert plugin.store_hooks() is None
    assert plugin.registrar() is None
    assert plugin.event_sources() == ()
    assert plugin.receipt_reason_order() == ()
    assert plugin.cli(None, lambda *_: None) is None
    assert asyncio.run(plugin.close()) is None


async def test_agent_programs_are_byte_identical_to_the_old_core_table(tmp_path: Path) -> None:
    observed = agent_programs(builtin_plugins(unbound_owners()))
    assert observed == AGENT_PROGRAMS_ON_MAIN
    assert json.dumps(observed, sort_keys=True) == json.dumps(
        AGENT_PROGRAMS_ON_MAIN, sort_keys=True
    )
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    try:
        assert bridge.core.agents == AGENT_PROGRAMS_ON_MAIN
    finally:
        await bridge.close()


def test_direct_core_gets_agent_programs_from_loaded_plugin_catalog() -> None:
    legacy_name = "_".join(("LEGACY", "AGENT", "PROGRAMS"))
    assert not hasattr(plugin_module, legacy_name)
    assert Core().agents == agent_programs(builtin_plugins(unbound_owners()))


async def test_bridge_replacements_keep_plugin_agent_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AgentPlugin(FixturePlugin):
        definition = ProviderDefinition("zz-fixture", "zz-fixture")

        def agent_programs(self) -> dict[str, str]:
            return {"zz-agent": "zz"}

    def plugins(owners: Owners) -> list[ProviderPlugin]:
        return [*builtin_plugins(owners), AgentPlugin()]

    async def inventory(core: Core) -> dict[str, Any]:
        return {"errors": {}}

    monkeypatch.setattr(Core, "inventory", inventory)
    monkeypatch.setattr(
        "answerback_bridge.providers.herdr.verbs.shutil.which", lambda binary: binary
    )
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json", plugins=plugins)
    try:
        assert bridge.core.agents["zz-agent"] == "zz"
        await bridge.hc_configure(binary="hc")
        assert bridge.core.agents["zz-agent"] == "zz"
        bridge.provider_register("hc", binary="hc")
        assert bridge.core.agents["zz-agent"] == "zz"
    finally:
        await bridge.close()


def test_agent_program_collision_is_refused_in_plugin_name_order() -> None:
    class Alpha(FixturePlugin):
        definition = ProviderDefinition("alpha", "alpha")

        def agent_programs(self) -> dict[str, str]:
            return {"shared-program": "alpha"}

    class Zulu(FixturePlugin):
        definition = ProviderDefinition("zulu", "zulu")

        def agent_programs(self) -> dict[str, str]:
            return {"shared-program": "zulu"}

    with pytest.raises(
        SurfaceCollision,
        match="agent program 'shared-program' from plugin zulu is already declared by plugin alpha",
    ):
        agent_programs([Zulu(), Alpha()])


def test_receipt_reason_historical_positions_are_reserved_for_builtins() -> None:
    class Intruder(FixturePlugin):
        definition = ProviderDefinition("intruder", "intruder")

        def receipt_reasons(self) -> tuple[ReasonRule, ...]:
            return (ReasonRule("intruder", ("intruder",), {}),)

        def receipt_reason_order(self) -> tuple[int, ...]:
            return (-1,)

    with pytest.raises(
        ValueError,
        match="Plugin intruder.*historical receipt reason position -1",
    ):
        receipt_reasons([Intruder()])


def test_every_builtin_plugin_carries_its_catalog_row() -> None:
    plugins = builtin_plugins(unbound_owners())
    names = [plugin.definition.registration for plugin in plugins]
    assert sorted(names) == sorted(entry.registration for entry in catalog.DEFINITIONS)
    for plugin in plugins:
        assert plugin.api_version == PLUGIN_API
        assert plugin.definition is catalog.definition(plugin.definition.registration)
        # Step 7: the block class the kernel stores under providers.<name> is the plugin's.
        assert plugin.config_schema is SCHEMAS[plugin.definition.registration]
        assert plugin.config_schema.provider == plugin.definition.registration
        assert isinstance(plugin.capabilities, frozenset)


def test_builtin_capabilities_equal_the_status_blocks_on_main(tmp_path: Path) -> None:
    plugins = {p.definition.registration: p for p in builtin_plugins(unbound_owners())}
    # Every installed provider carries its own capability set.
    assert set(plugins) == set(CAPABILITIES_ON_MAIN) == set(ALL_PROVIDERS)
    assert set(KERNEL_ADAPTER_ORDER) == set(ALL_PROVIDERS) - set(PLUGIN_PROVIDERS)
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    for name, (status_key, literal) in CAPABILITIES_ON_MAIN.items():
        assert plugins[name].capabilities == frozenset(literal), name
        assert len(literal) == len(frozenset(literal)), name
        assert bridge.provider_status[status_key]["capabilities"] == literal, name


def test_builtin_registry_order_and_names_are_unchanged(tmp_path: Path) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    assert [adapter.name for adapter in bridge.adapters] == ADAPTER_ORDER_ON_MAIN
    assert {a.registration for a in bridge.adapters} == set(ALL_PROVIDERS)
    assert list(bridge.provider_status) == STATUS_ORDER_ON_MAIN


def test_catalog_order_is_the_rows_order_then_name_and_unordered_rows_come_last() -> None:
    """The kernel rows carry tens; a row declares its slot with the value between two of
    them, and a row without one follows every ordered row, by name, so a third-party plugin
    lands deterministically whatever its entry point is called."""
    kernel = [DEFINITIONS[name] for name in ("hc", "herdr", "codex", "kiro", "crew", "rex")]
    assert [entry.order for entry in kernel] == [10, 20, 30, 40, 50, 70]
    between = ProviderDefinition("between", "between", order=60)
    zulu = ProviderDefinition("zulu", "zulu")
    alpha = ProviderDefinition("alpha", "alpha")
    ordered = catalog.ordered([zulu, DEFINITIONS["rex"], alpha, between, *kernel[:5]])
    assert [entry.registration for entry in ordered] == [
        "hc",
        "herdr",
        "codex",
        "kiro",
        "crew",
        "between",
        "rex",
        "alpha",
        "zulu",
    ]
    assert [entry.registration for entry in catalog.definitions()] == ALL_PROVIDERS
    with pytest.raises(TypeError, match="order must be an int"):
        ProviderDefinition("bad", "bad", order="60")  # type: ignore[arg-type]


def test_adapter_slot_follows_the_last_kernel_adapter_the_catalog_orders_before_it() -> None:
    """The adapter registry keeps the historical built-in order; a row ordered
    between ``crew`` (50) and ``rex`` (70) in the catalog sits after ``hc``, the last of the
    five kernel adapters those rows name; a row without an order follows ``grok``."""
    between = ProviderDefinition("between", "between", order=60)
    first = ProviderDefinition("first", "first", order=5)
    zulu = ProviderDefinition("zulu", "zulu")
    alpha = ProviderDefinition("alpha", "alpha")
    slots = _adapter_slots([*DEFINITIONS.values(), zulu, between, alpha, first])
    registry = [name for name in sorted(slots, key=slots.__getitem__)]
    assert registry == [
        "first",
        "kiro",
        "codex",
        "herdr",
        "crew",
        "hc",
        "between",
        "rex",
        "codex_cli",
        "claude_code",
        "grok",
        "alpha",
        "zulu",
    ]


def test_plugins_sharing_a_kernel_gap_keep_the_catalog_order_in_the_adapter_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two installed rows between ``crew`` (50) and ``rex`` (70) share the slot after ``hc``;
    their order there is the catalog's (``order`` then name), not their registration or
    entry-point names, and an unordered row still follows every ordered one, by name.
    Checked on the slots and on the loaded result (``load_plugins`` returns name order)."""
    declared = ["zeta", "alpha", "beta"]  # zeta(51) before alpha(59); beta has no order
    rows = {
        "zeta": ProviderDefinition("zeta", "zeta", order=51, registration_binary_field=None),
        "alpha": ProviderDefinition("alpha", "alpha", order=59, registration_binary_field=None),
        "beta": ProviderDefinition("beta", "beta", registration_binary_field=None),
    }
    slots = _adapter_slots([*DEFINITIONS.values(), *rows.values()])
    assert slots["zeta"][:2] == slots["alpha"][:2] == (KERNEL_ADAPTER_ORDER.index("hc"), 1)
    assert slots["zeta"] < slots["alpha"]
    assert [entry.registration for entry in catalog.ordered(rows.values())] == declared

    class Installed(ProviderPlugin):
        api_version = PLUGIN_API
        capabilities = frozenset({"read"})
        config_schema = None

        def __init__(self, definition: ProviderDefinition) -> None:
            self.definition = definition

        def build(self, ctx: KernelContext) -> ProviderAdapter:
            raise AssertionError("ordering must not build an adapter")

    installed = list(importlib.metadata.entry_points(group=PROVIDER_GROUP))
    fakes = [
        SimpleNamespace(
            group=PROVIDER_GROUP,
            name=f"{index}-entry",  # entry names in the reverse of the declared order
            value=f"{name}_plugin:factory",
            dist=SimpleNamespace(metadata={"Name": "third-party"}),
            load=lambda name=name: lambda: Installed(rows[name]),
        )
        for index, name in enumerate(reversed(declared))
    ]
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_kwargs: [*installed, *fakes])

    plugins = builtin_plugins(unbound_owners())
    adapter_order = [plugin.definition.registration for plugin in plugins]
    assert [
        plugin.definition.name for plugin in plugins if plugin.definition.registration not in rows
    ] == ADAPTER_ORDER_ON_MAIN
    catalog_order = [
        entry.registration
        for entry in catalog.ordered(plugin.definition for plugin in plugins)
        if entry.registration in rows
    ]
    assert [name for name in adapter_order if name in rows] == catalog_order == declared
    assert adapter_order[adapter_order.index("hc") + 1 :][:2] == ["zeta", "alpha"]
    assert adapter_order[-1] == "beta"


@pytest.mark.parametrize("selection", list(STATUS_ON_MAIN))
async def test_tools_methods_and_status_are_byte_identical_to_main(
    tmp_path: Path, selection: tuple[str, ...]
) -> None:
    bridge = Bridge(Config(provider_selection=list(selection)), tmp_path / "config.json")
    try:
        async with Client(create_server(bridge)) as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
        assert tools == TOOLS_ON_MAIN
        assert sorted(Bridge.METHODS) == METHODS_ON_MAIN
        assert json.dumps(bridge.provider_status, sort_keys=True) == STATUS_ON_MAIN[selection]
    finally:
        await bridge.close()


def test_plugin_module_imports_no_provider_bus_or_herdr_code() -> None:
    """The kernel's plugin API must load without any provider; arrows point inward only."""
    tree = ast.parse((SOURCE_ROOT / "plugin.py").read_text(), filename="plugin.py")
    forbidden_modules = {"bus", "service", "cli", "mcp_server", "runtime"}
    forbidden_prefixes = ("core", "herdr", "bus.", "adapters.")
    allowed_adapters = {"adapters.catalog", "adapters.contracts"}
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("answerback_bridge."):
                    offending.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            module = node.module or ""
            if node.module is None:
                offending.extend(alias.name for alias in node.names)
            elif module in allowed_adapters:
                continue
            elif module in forbidden_modules or module.startswith(forbidden_prefixes):
                offending.append(module)
    assert offending == [], f"plugin.py imports module code: {offending}"


def test_run6_two_status_blocks_under_one_name_are_a_collision() -> None:
    """red_at_base (run-6 hardening h1): a shared status_name overwrote the other block."""
    from types import SimpleNamespace

    from answerback_bridge.plugin import SurfaceCollision, provider_status

    def status_plugin(registration: str, status_name: str | None) -> Any:
        return SimpleNamespace(
            definition=SimpleNamespace(registration=registration),
            status_name=status_name,
            status_order=None,
            status=lambda config: {"state": registration},
        )

    first, second = status_plugin("alpha", "shared"), status_plugin("beta", "shared")
    with pytest.raises(SurfaceCollision, match=r"'shared' from plugin beta.*plugin alpha"):
        provider_status([first, second], Config())
    kernel = ((80, "claude_desktop", {"state": "unverified"}),)
    with pytest.raises(SurfaceCollision, match=r"'claude_desktop'.*the kernel"):
        provider_status([status_plugin("gamma", "claude_desktop")], Config(), kernel)
    assert provider_status([first, status_plugin("beta", None)], Config(), kernel) == {
        "claude_desktop": {"state": "unverified"},
        "shared": {"state": "alpha"},
        "beta": {"state": "beta"},
    }
