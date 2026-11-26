"""The generated surfaces: a plugin's verbs, subcommands, tools and tails reach the service,
the CLI, the MCP server and the observer through the kernel's tables; a name declared twice
is a startup error; what a client sees is byte-identical to the hand-written tables."""

import argparse
import hashlib
import inspect
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from composition import contributed
from mcp import Client
from test_adapter_extension import FixtureAdapter
from test_plugin import METHODS_ON_MAIN, STATUS_ON_MAIN, TOOLS_ON_MAIN

import answerback_bridge.cli as cli
from answerback_bridge import mcp_server
from answerback_bridge.actors import cli as actors_cli
from answerback_bridge.adapters.builtins import Owners, builtin_plugins
from answerback_bridge.adapters.catalog import ProviderDefinition
from answerback_bridge.adapters.contracts import ProviderAdapter
from answerback_bridge.config import Config
from answerback_bridge.events.collector import bridge_sources
from answerback_bridge.mcp_server import create_server
from answerback_bridge.plugin import (
    PLUGIN_API,
    CliRunner,
    KernelContext,
    ProviderPlugin,
    SurfaceCollision,
    ToolCall,
    ToolFunction,
    ToolSpec,
    Verb,
    load_modules,
)
from answerback_bridge.service import Bridge

# Computed on main at 1ca80205: each tool's name, its annotation kind and its parameters
# with types and defaults, in `tools/list` order; the sha256 of the canonical JSON of every
# tool's name, description, annotations, input and output schema; and, in
# `fixtures/provider-subcommand-help.txt`, the text of `answerback <sub> --help` for every
# provider subcommand at 80 columns with the machine-dependent `--config` default pinned.
# The digest was recomputed for T-203 after `peer_submit` generalized binding guidance
# from a provider-name list to the provider-neutral binding contract. Signatures did not change.
# Recomputed for T-277: `peer_submit` and `task_list` gained the optional `subject` argument.
TOOL_SIGNATURES_ON_MAIN = [
    "bus_submit send peer_id:string project:string message:string request_id:string "
    "ttl_seconds:integer=3600",
    "bus_result read command_id:string",
    "bus_events read after:integer=0 limit:integer=50",
    "bus_cancel write command_id:string",
    "bus_status read",
    "hc_read read target:string lines:integer=100",
    "rex_read read session_id:string block_id:string max_bytes:integer=16384",
    "rex_enroll write name:string session_id:string block_id:string request_id:string",
    "rex_receive read peer_id:string wait_seconds:integer=0",
    "rex_ack write peer_id:string task_id:string",
    "rex_complete write peer_id:string task_id:string result:string",
    "rex_retire write peer_id:string",
    "registered_clients read",
    "notify_client write client:string message:string request_id:string "
    'topic:string="notification"',
    "notification_status read client:string request_id:string",
    "read_inbox read",
    "acknowledge_inbox write ids:array",
    "desktop_list read",
    'desktop_sessions read query:string="" cursor:null|string=null limit:integer=50',
    "desktop_read read alias:string",
    "desktop_submit send alias:string message:string request_id:string",
    "herdr_list_agents read",
    "herdr_read_agent read target:string lines:integer=100",
    "task_get read task_id:string",
    "task_wait read task_id:string seconds:integer=30",
    "task_reconcile write task_id:string",
    "task_complete write task_id:string result:string",
    'peer_search read query:string="" provider:null|string=null',
    "peer_submit send selector:string message:string request_id:string "
    "provider:null|string=null binding:null|string=null subject:null|string=null",
    "task_for_peer read selector:string provider:null|string=null",
    'task_list read query:string="" limit:integer=30 subject:null|string=null',
    "task_permissions read task_id:string",
    "task_cancel send task_id:string",
    'kiro_sessions read query:string="" cwd:null|string=null cursor:null|string=null',
    "crew_read read target:string",
]
TOOL_DIGEST_ON_MAIN = (
    "fdce38968ab901357fa6066cb8774c7b15bd99dde80599b21b4d27352eb80f59"  # pragma: allowlist secret
)
# The kernel's provider subcommands; the installed plugin's own sections, top-level help and
# MCP instructions are the plugin's fixtures (`composition.contributed`).
HELP_ON_MAIN = Path(__file__).parent / "fixtures" / "provider-subcommand-help.txt"
HELP_FIXTURES = [HELP_ON_MAIN, *contributed("SUBCOMMAND_HELP", ())]
TOP_LEVEL_HELP = contributed(
    "TOP_LEVEL_HELP", Path(__file__).parent / "fixtures" / "top-level-help-public.txt"
)
MCP_INSTRUCTIONS = contributed(
    "MCP_INSTRUCTIONS", Path(__file__).parent / "fixtures" / "mcp-instructions-public.txt"
)
PROVIDER_SUBCOMMANDS = [
    "rex-configure",
    "read-rex",
    "rex-enroll",
    "rex-receive",
    "rex-ack",
    "rex-complete",
    "rex-retire",
    "hc-configure",
    "read-hc",
    *contributed("CLI_SUBCOMMANDS", ()),
    "kiro-register",
    "desktop-sessions",
    "kiro-sessions",
    "crew-configure",
    "crew-create",
    "read-crew",
    "send-desktop",
    "send-herdr",
    "read-desktop",
    "read-herdr",
]
# The verbs each built-in plugin moved out of the kernel's tables, by plugin name.
PLUGIN_VERBS_ON_MAIN = {
    **contributed("VERBS", {}),
    "codex": {"desktop_list", "desktop_sessions", "desktop_read", "desktop_submit"},
    "crew": {"crew_configure", "crew_create", "crew_read"},
    "hc": {"hc_configure", "hc_read"},
    "herdr": {"herdr_list_agents", "herdr_read_agent"},
    "kiro": {"kiro_register", "kiro_submit", "kiro_sessions"},
    "rex": {
        "rex_configure",
        "rex_read",
        "rex_enroll",
        "rex_receive",
        "rex_ack",
        "rex_complete",
        "rex_retire",
    },
}
# `guarded` in `Bridge.call` on main: the verbs refused while their provider is unregistered.
GUARDED_ON_MAIN = {
    "desktop_read": "codex",
    "desktop_submit": "codex",
    "desktop_sessions": "codex",
    "herdr_list_agents": "herdr",
    "herdr_read_agent": "herdr",
    "hc_read": "hc",
    "crew_read": "crew",
    "crew_create": "crew",
    "kiro_sessions": "kiro",
    "kiro_submit": "kiro",
}
KIND = {(True, False): "read", (False, False): "write", (False, True): "send"}


def _echo_tool(call: ToolCall) -> ToolFunction:
    async def fixture_echo(text: str = "") -> dict[str, Any]:
        """Echo text through the fixture provider."""
        return await call("fixture_echo", {"text": text})

    return fixture_echo


ECHO_TOOL = ToolSpec(_echo_tool, "read")


class FixtureSource:
    provider = "fixture"

    async def tail(self, target: str, peer: Mapping[str, Any], resources: Any) -> str | None:
        return f"FIXTURE_TAIL {target}"


class SecondAdapter(FixtureAdapter):
    name = registration = "second"


class SurfacePlugin(ProviderPlugin):
    """A foreign provider that contributes one verb, one subcommand, one tool, one source."""

    api_version = PLUGIN_API
    capabilities = frozenset({"discovery", "send"})
    config_schema = None

    def __init__(
        self,
        verb: str = "fixture_echo",
        tool: ToolSpec | None = ECHO_TOOL,
        source_provider: str = "fixture",
        name: str = "fixture",
    ) -> None:
        self.definition = ProviderDefinition(name, name, recovery_states=("running",))
        self.verb = verb
        self.tool = tool
        self.source_provider = source_provider
        self.calls: list[str] = []

    def build(self, ctx: KernelContext) -> ProviderAdapter:
        adapter = SecondAdapter if self.definition.name == "second" else FixtureAdapter
        return adapter(ctx.config, ctx.config_path, ctx.store)

    async def echo(self, text: str = "") -> dict[str, Any]:
        self.calls.append(text)
        return {"echo": text}

    def rpc(self) -> Mapping[str, Any]:
        return {self.verb: self.echo}

    def cli(self, commands: argparse._SubParsersAction, run: CliRunner) -> None:
        command = commands.add_parser("fixture-echo", help="Echo through the fixture provider")
        command.add_argument("text")

        async def handler(args: argparse.Namespace, bridge: Any) -> dict[str, Any]:
            return await bridge.call("fixture_echo", {"text": args.text})

        run(command, handler)

    def mcp_tools(self) -> Sequence[ToolSpec]:
        return () if self.tool is None else (self.tool,)

    def event_sources(self) -> Sequence[Any]:
        source = FixtureSource()
        source.provider = self.source_provider
        return (source,)


def with_plugins(*extra: ProviderPlugin):
    return lambda owners: (*builtin_plugins(owners), *extra)


def compose(tmp_path: Path, *extra: ProviderPlugin, selection: list[str] | None = None) -> Bridge:
    config = Config(provider_selection=[] if selection is None else selection)
    return Bridge(config, tmp_path / "config.json", plugins=with_plugins(*extra))


async def test_plugin_verb_is_dispatched_through_the_generated_table(tmp_path: Path) -> None:
    plugin = SurfacePlugin()
    bridge = compose(tmp_path, plugin)
    try:
        assert "fixture_echo" in bridge.METHODS
        assert bridge.METHODS == Bridge.METHODS | {"fixture_echo"}
        assert "fixture_echo" not in Bridge.METHODS, "the default composition is unchanged"
        assert await bridge.call("fixture_echo", {"text": "hi"}) == {"echo": "hi"}
        assert plugin.calls == ["hi"]
        with pytest.raises(TypeError):
            await bridge.call("fixture_echo", {"nope": 1})  # bound against the handler
        with pytest.raises(ValueError, match="Unknown bridge operation"):
            await bridge.call("fixture_missing", {})
    finally:
        await bridge.close()


async def test_plugin_subcommand_is_parsed_and_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = SurfacePlugin()
    root = cli.parser(plugins=[*builtin_plugins(), plugin])
    assert "fixture-echo" in root.format_help()
    args = root.parse_args(["--config", str(tmp_path / "config.json"), "fixture-echo", "hello"])
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        return {"echo": params["text"]}

    monkeypatch.setattr(cli.RemoteBridge, "call", call)
    assert await cli.dispatch(args) == {"echo": "hello"}
    assert calls == [("fixture_echo", {"text": "hello"})]
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["fixture-echo", "hello"])  # not a built-in


async def test_plugin_tool_is_served_after_the_pinned_list(tmp_path: Path) -> None:
    plugin = SurfacePlugin()
    bridge = compose(tmp_path, plugin)
    try:
        server = create_server(bridge, plugins=[*builtin_plugins(), plugin])
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            assert [tool.name for tool in tools] == [*TOOLS_ON_MAIN, "fixture_echo"]
            echo = tools[-1]
            assert echo.description == "Echo text through the fixture provider."
            assert echo.annotations is not None and echo.annotations.read_only_hint is True
            assert echo.input_schema["properties"] == {
                "text": {"default": "", "title": "Text", "type": "string"}
            }
            result = await client.call_tool("fixture_echo", {"text": "via mcp"})
        assert result.structured_content == {"echo": "via mcp"}
        assert plugin.calls == ["via mcp"]
    finally:
        await bridge.close()


async def test_plugin_event_source_answers_for_its_provider_only(tmp_path: Path) -> None:
    bridge = compose(tmp_path, SurfacePlugin())
    try:
        sources = bridge_sources(bridge)
        assert await sources.tail("fixture:one", {}) == "FIXTURE_TAIL one"
        assert await sources.tail("unknown:one", {}) is None
        # Built-in sources are gated on their provider being registered, as before.
        assert await sources.tail("hc:term", {}) is None
    finally:
        await bridge.close()


async def test_verb_collision_with_the_kernel_or_another_plugin_fails_startup(
    tmp_path: Path,
) -> None:
    with pytest.raises(SurfaceCollision, match=r"verb 'peer_search' from plugin fixture.*kernel"):
        compose(tmp_path, SurfacePlugin(verb="peer_search"))
    # Plugins merge in name order, so the second declarer is named deterministically.
    with pytest.raises(SurfaceCollision, match=r"verb 'hc_read' from plugin hc.*plugin fixture"):
        compose(tmp_path, SurfacePlugin(verb="hc_read"))
    with pytest.raises(SurfaceCollision, match=r"'fixture_echo' from plugin second.*fixture"):
        compose(tmp_path, SurfacePlugin(), SurfacePlugin(name="second"))
    assert SurfaceCollision.__mro__[1] is ValueError


def test_kernel_verb_table_and_kernel_methods_must_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Bridge, "KERNEL_METHODS", Bridge.KERNEL_METHODS - {"peer_search"})
    with pytest.raises(SurfaceCollision, match="KERNEL_METHODS"):
        Bridge(Config(provider_selection=[]), tmp_path / "config.json")


async def test_tool_collision_fails_server_startup(tmp_path: Path) -> None:
    def clash(call: ToolCall) -> ToolFunction:
        async def task_get(task_id: str) -> dict[str, Any]:
            """Not the kernel's."""
            return await call("task_get", {"task_id": task_id})

        return task_get

    bridge = compose(tmp_path, SurfacePlugin(tool=ToolSpec(clash, "read")))
    try:
        with pytest.raises(SurfaceCollision, match=r"tool 'task_get' from plugin fixture.*kernel"):
            create_server(bridge, plugins=[*builtin_plugins(), *bridge.plugins[-1:]])
    finally:
        await bridge.close()


async def test_event_source_collision_fails_observer_startup(tmp_path: Path) -> None:
    bridge = compose(tmp_path, SurfacePlugin(source_provider="hc"))
    try:
        with pytest.raises(SurfaceCollision, match=r"event source 'hc' from plugin hc.*fixture"):
            bridge_sources(bridge)
    finally:
        await bridge.close()


async def test_guarded_verbs_refuse_an_unregistered_provider_before_binding(
    tmp_path: Path,
) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    try:
        operations = bridge.operations()
        assert {
            name: required
            for name, handler in operations.items()
            if (required := getattr(handler, "requires", None))
        } == GUARDED_ON_MAIN
        for verb, provider in GUARDED_ON_MAIN.items():
            with pytest.raises(ValueError, match=f"{provider} provider is not registered"):
                await bridge.call(verb, {"not": "a parameter"})
    finally:
        await bridge.close()


def test_verbs_follow_a_replaced_client() -> None:
    class Owner:
        def read(self, target: str, lines: int = 100) -> str:
            return f"first {target}"

    class Later:
        def read(self, target: str) -> str:
            return f"later {target}"

    current: Any = Owner()
    verb = Verb(lambda: current, "read", requires="herdr")
    import inspect

    assert list(inspect.signature(verb).parameters) == ["target", "lines"]
    assert verb("a") == "first a"
    current = Later()
    assert list(inspect.signature(verb).parameters) == ["target"]
    assert verb("b") == "later b"


def test_builtin_rpc_implementations_and_compatibility_delegates_have_one_owner() -> None:
    modules = {
        "codex": "answerback_bridge.providers.codex.verbs",
        "crew": "answerback_bridge.providers.crew.verbs",
        "hc": "answerback_bridge.providers.herdr.verbs",
        "herdr": "answerback_bridge.providers.herdr.verbs",
        "kiro": "answerback_bridge.providers.kiro.verbs",
        "rex": "answerback_bridge.providers.rex.verbs",
    }
    compatibility = {
        "desktop_list": "desktop_list",
        "desktop_sessions": "desktop_sessions",
        "desktop_read": "desktop_read",
        "desktop_submit": "desktop_submit",
        "crew_configure": "crew_configure",
        "crew_create": "crew_create",
        "crew_read": "crew_read",
        "hc_configure": "hc_configure",
        "hc_read": "hc_read",
        "list_agents": "herdr_list_agents",
        "kiro_register": "kiro_register",
        "kiro_submit": "kiro_submit",
        "kiro_sessions": "kiro_sessions",
        "rex_configure": "rex_configure",
        "rex_read": "rex_read",
        "rex_enroll": "rex_enroll",
        "rex_receive": "rex_receive",
        "rex_ack": "rex_ack",
        "rex_complete": "rex_complete",
        "rex_retire": "rex_retire",
    }

    plugins = {plugin.definition.registration: plugin for plugin in builtin_plugins()}
    for registration, module in modules.items():
        for handler in plugins[registration].rpc().values():
            implementation = inspect.getmodule(handler)
            callback = inspect.getmodule(getattr(handler, "handler", handler))
            assert implementation is not None
            assert callback is not None
            assert implementation.__name__ == module
            assert callback.__name__ == module
    for method, verb in compatibility.items():
        delegate = getattr(Bridge, method)
        assert delegate.__code__.co_names == ("_provider_compat",)
        assert verb in delegate.__code__.co_consts


def test_every_builtin_declares_exactly_the_verbs_it_owned() -> None:
    plugins = {p.definition.registration: p for p in builtin_plugins()}
    assert {name: set(p.rpc()) for name, p in plugins.items() if p.rpc()} == PLUGIN_VERBS_ON_MAIN
    moved = set().union(*PLUGIN_VERBS_ON_MAIN.values())
    module_verbs = {name for module in load_modules() for name in module.rpc()}
    assert Bridge.KERNEL_METHODS.isdisjoint(moved | module_verbs)
    assert Bridge.METHODS == Bridge.KERNEL_METHODS | moved | module_verbs
    with_tools = {name for name, p in plugins.items() if p.mcp_tools()}
    assert with_tools == {"hc", "rex", "codex", "herdr", "kiro", "crew"}
    declared = {
        fn.__name__ for p in plugins.values() for s in p.mcp_tools() for fn in (s.build(None),)
    }
    assert declared == set(mcp_server.TOOL_ORDER) - {
        "bus_submit",
        "bus_result",
        "bus_events",
        "bus_cancel",
        "bus_status",
        "registered_clients",
        "notify_client",
        "notification_status",
        "read_inbox",
        "acknowledge_inbox",
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
    }
    module_tools = {
        fn.__name__
        for module in load_modules()
        for spec in module.mcp_tools()
        for fn in (spec.build(None),)
    }
    assert module_tools == {
        "bus_submit",
        "bus_result",
        "bus_events",
        "bus_cancel",
        "bus_status",
    }
    assert {name for name, p in plugins.items() if p.event_sources()} == {"hc", "crew"}
    assert {name for name, p in plugins.items() if p.commands is not None} == set(
        PLUGIN_VERBS_ON_MAIN
    )


def test_detached_builtins_declare_surfaces_but_cannot_run_them() -> None:
    plugins = builtin_plugins()
    hc = next(p for p in plugins if p.definition.registration == "hc")
    assert set(hc.rpc()) == {"hc_configure", "hc_read"}
    with pytest.raises(RuntimeError, match="composed without a service"):
        hc.rpc()["hc_read"]("t", 1)
    with pytest.raises(RuntimeError, match="composed without a service"):
        Owners.detached().core_lock()


@pytest.mark.parametrize("selection", list(STATUS_ON_MAIN))
async def test_tools_methods_help_and_status_are_byte_identical_to_main(
    tmp_path: Path, selection: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = Bridge(Config(provider_selection=list(selection)), tmp_path / "config.json")
    try:
        async with Client(create_server(bridge)) as client:
            tools = (await client.list_tools()).tools
        signatures = []
        for tool in tools:
            annotations = tool.annotations
            assert annotations is not None
            assert annotations.idempotent_hint is True and annotations.open_world_hint is False
            kind = KIND[(annotations.read_only_hint, annotations.destructive_hint)]
            required = set(tool.input_schema.get("required", []))
            params = []
            for name, schema in tool.input_schema["properties"].items():
                kind_ = schema.get("type") or "|".join(sorted(s["type"] for s in schema["anyOf"]))
                text = f"{name}:{kind_}"
                if name not in required:
                    text += "=" + json.dumps(schema.get("default"))
                params.append(text)
            signatures.append(" ".join([tool.name, kind, *params]))
        assert signatures == TOOL_SIGNATURES_ON_MAIN
        canonical = json.dumps(
            [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "annotations": tool.annotations.model_dump() if tool.annotations else None,
                    "inputSchema": tool.input_schema,
                    "outputSchema": tool.output_schema,
                }
                for tool in tools
            ],
            sort_keys=True,
        )
        assert hashlib.sha256(canonical.encode()).hexdigest() == TOOL_DIGEST_ON_MAIN
        assert sorted(bridge.METHODS) == sorted(Bridge.METHODS) == METHODS_ON_MAIN
        assert json.dumps(bridge.provider_status, sort_keys=True) == STATUS_ON_MAIN[selection]
    finally:
        await bridge.close()
    monkeypatch.setattr(
        cli, "default_config_path", lambda: Path("/home/user/.config/answerback/config.json")
    )
    monkeypatch.setenv("COLUMNS", "80")
    root = cli.parser()
    subparsers = next(a for a in root._actions if getattr(a, "dest", None) == "command")
    rendered = "".join(
        f"=== answerback {name} --help\n" + subparsers.choices[name].format_help()
        for name in PROVIDER_SUBCOMMANDS
    )
    # The kernel fixture holds the kernel's provider subcommands and the plugin's fixture its
    # own; every section is expected, in PROVIDER_SUBCOMMANDS order.
    sections = {
        section.split(" --help\n", 1)[0]: "=== answerback " + section
        for fixture in HELP_FIXTURES
        for section in fixture.read_text().split("=== answerback ")[1:]
    }
    assert sorted(sections) == sorted(PROVIDER_SUBCOMMANDS)
    assert rendered == "".join(sections[name] for name in PROVIDER_SUBCOMMANDS)


def test_top_level_cli_help_is_byte_identical_to_composition_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "default_config_path", lambda: Path("/home/user/.config/answerback/config.json")
    )
    monkeypatch.setenv("COLUMNS", "80")
    assert cli.parser().format_help() == TOP_LEVEL_HELP.read_text()


async def test_mcp_instructions_are_byte_identical_to_composition_fixture(
    tmp_path: Path,
) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    try:
        expected = MCP_INSTRUCTIONS.read_text().removesuffix("\n")
        assert create_server(bridge).instructions == expected
    finally:
        await bridge.close()


async def test_local_actor_commands_bypass_remote_config_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    args = cli.parser().parse_args(
        [
            "--config",
            str(config_path),
            "actors",
            "create",
            "--project",
            str(tmp_path),
            "--role",
            "author",
        ]
    )
    expected = {"run_id": "local"}
    monkeypatch.setattr(actors_cli, "dispatch", lambda actual: expected)

    def remote_bridge_must_not_be_constructed(*_args: object) -> None:
        raise AssertionError("local actors commands must not contact the bridge")

    monkeypatch.setattr(cli, "RemoteBridge", remote_bridge_must_not_be_constructed)
    assert await cli.dispatch(args) == expected


def test_cli_and_mcp_frontends_import_no_provider_transport() -> None:
    """Composing the built-ins for their surfaces must not load every provider's client."""
    code = (
        "import sys\n"
        "import answerback_bridge.cli, answerback_bridge.mcp_server\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('answerback_bridge.'))\n"
        "print('\\n'.join(loaded))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.split())
    heavy = {
        "answerback_bridge.acp_client",
        "answerback_bridge.providers.kiro.adapter",
        "answerback_bridge.providers.codex.adapter",
        "answerback_bridge.providers.herdr.adapters.core",
        "answerback_bridge.providers.rex.adapter",
        "answerback_bridge.providers.rex.service",
        "answerback_bridge.workers",
        "answerback_bridge.providers.kiro.workers",
        "answerback_bridge.providers.codex.desktop",
    }
    assert loaded & heavy == set(), sorted(loaded & heavy)
    assert "answerback_bridge.adapters.builtins" in loaded
