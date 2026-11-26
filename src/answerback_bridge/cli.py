"""Command interface for humans, Herdr agents, and the portable installer."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from . import __version__
from .adapters.builtins import builtin_plugins
from .cli_kit import binary_argument, bounded_file
from .config import (
    CLIENT_NAMES,
    PROVIDERS,
    WORKSPACE_PROVIDERS,
    Config,
    client_names,
    default_config_path,
)
from .exit_codes import EXIT_BLOCKED, EXIT_FAILURE, EXIT_WAIT_TIMED_OUT
from .install import SERVER_NAME, install_mcp, owns, uninstall_mcp
from .plugin import CliHandler, ModulePlugin, ProviderPlugin, by_name, load_modules
from .process import BridgeError, module_launch_argv, run
from .providers.herdr.herdr import Herdr
from .transport import RemoteBridge, TransportUnavailable


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


class BridgeParser(argparse.ArgumentParser):
    """Accept the config option at every command depth without resetting it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.add_argument(
            "--config",
            type=Path,
            default=argparse.SUPPRESS,
            # Resolved fresh on every parser construction so --help always
            # names the path this invocation will actually use, including
            # when an existing legacy config changes that resolution.
            help=f"Bridge config file (resolves to {default_config_path()} on this machine)",
        )


def _bind(command: argparse.ArgumentParser, handler: CliHandler) -> None:
    """How a plugin's handler travels to `dispatch`: as a default of its own subcommand."""
    command.set_defaults(plugin_handler=handler)


def parser(
    plugins: Sequence[ProviderPlugin] | None = None,
    modules: Sequence[ModulePlugin] | None = None,
) -> argparse.ArgumentParser:
    """The command tree: kernel commands plus installed module and provider contributions."""
    clients = tuple(dict.fromkeys((*CLIENT_NAMES, *client_names())))
    result = BridgeParser(prog="answerback", description="Local coordination for coding agents")
    result.add_argument("--version", action="version", version=__version__)
    result.set_defaults(config=default_config_path())
    # The bus parent is an ordinary ArgumentParser; argparse supports both classes.
    commands = cast(argparse.ArgumentParser, result).add_subparsers(dest="command", required=True)
    from .maintenance_cli import add_parser as add_maintenance_parser

    add_maintenance_parser(commands)
    for module in load_modules() if modules is None else modules:
        module.cli(commands, _bind)
    from .routing.cli import add_parser as add_routing_parser

    add_routing_parser(commands)
    from .placement.cli import add_parser as add_capacity_parser

    add_capacity_parser(commands)
    from .placement.rebalance_cli import add_parser as add_rebalance_parser

    add_rebalance_parser(commands)
    from .fleet.cli import add_parser as add_fleet_parser

    add_fleet_parser(commands)
    from .events.cli import add_parser as add_events_parser

    add_events_parser(commands)
    from .events.debug_cli import add_parser as add_debug_parser

    add_debug_parser(commands)
    from .pressure.cli import add_parser as add_pressure_parser

    add_pressure_parser(commands)
    completion = commands.add_parser("completion", help="Print tab-completion setup for your shell")
    completion.add_argument("--shell", choices=("zsh", "bash", "fish"), required=True)
    mcp = commands.add_parser("mcp", help="Run the local STDIO MCP server")
    mcp.add_argument("--client", dest="mcp_client", choices=clients)
    mcp.add_argument(
        "--actor-binding-file",
        type=Path,
        help="Expose only the four message tools for one privately enrolled actor",
    )
    commands.add_parser("doctor", help="Check configured provider connections")
    for name, purpose in (
        ("clients", "Register MCP connections in coding clients"),
        ("skill", "Install the collaboration skill and the setup skill"),
    ):
        group = commands.add_parser(name, help=purpose)
        actions = group.add_subparsers(dest="install_action", required=True)
        if name == "clients":
            migration = actions.add_parser("migrate", help="Preview owned naming migration")
            migration.add_argument(
                "--client",
                action="append",
                choices=tuple(client for client in clients if client != "kiro-crew"),
                required=True,
            )
            migration.add_argument("--apply", action="store_true")
            actions.add_parser("list", help="List registered clients and heartbeat availability")
            policy = actions.add_parser("policy", help="Set heartbeat interval and missed limit")
            policy.add_argument("--interval", type=int, required=True)
            policy.add_argument("--misses", type=int, required=True)
        action = actions.add_parser("install", help="Preview installation; --apply writes it")
        selection = action.add_mutually_exclusive_group(required=True)
        selection.add_argument(
            "--all", action="store_true", help="All supported local client targets"
        )
        selection.add_argument(
            "--client",
            action="append",
            choices=tuple(client for client in clients if client != "kiro-crew"),
        )
        action.add_argument("--apply", action="store_true")
        action.add_argument(
            "--replace", action="store_true", help="Back up and replace conflicting entries"
        )
        if name == "clients":
            action.add_argument("--name", default="answerback_bridge")
        else:
            action.add_argument(
                "--source",
                type=Path,
                help="Folder to install as the collaboration skill; the setup skill is then "
                "left out. Default: both packaged skills",
            )
    providers = commands.add_parser("providers", help="Opt in to specific bridge providers")
    provider_actions = providers.add_subparsers(dest="provider_action", required=True)
    provider_actions.add_parser("list", help="List provider registrations without probing them")
    provider_register = provider_actions.add_parser("register", help="Register a provider")
    provider_register.add_argument("provider", choices=PROVIDERS)
    provider_register.add_argument("--binary")
    provider_register.add_argument("--session")
    provider_register.add_argument("--port", type=int)
    provider_unregister = provider_actions.add_parser(
        "unregister", help="Disable new provider work"
    )
    provider_unregister.add_argument("provider", choices=PROVIDERS)
    notify = commands.add_parser("notify", help="Queue an addressed client-inbox notification")
    notify.add_argument("client", choices=clients)
    notify.add_argument("--message", required=True)
    notify.add_argument("--topic", default="notification")
    notify.add_argument("--request-id", default=str(uuid.uuid4()))
    notice_status = commands.add_parser(
        "notification-status", help="Read a passive notice's client acknowledgement"
    )
    notice_status.add_argument("client", choices=clients)
    notice_status.add_argument("--request-id", required=True)
    for plugin in by_name(builtin_plugins() if plugins is None else plugins):
        plugin.cli(commands, _bind)
    commands.add_parser("peers", help="List peers across configured providers")
    harness = commands.add_parser("self-test", help="Verify bidirectional collaboration")
    harness.add_argument("--mode", choices=("simulated", "live"), default="simulated")
    harness.add_argument("--herdr-peer")
    harness.add_argument("--kiro-peer")
    harness.add_argument("--timeout", type=int, default=120)
    harness.add_argument("--report", type=Path)
    live = commands.add_parser(
        "live-test",
        help="Legacy Herdr TUI test; Herdr App uses the attended hc checklist in docs/LIVE-TEST.md",
    )
    live.add_argument(
        "--directory",
        type=Path,
        default=Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "herdr-bridge-test",
        help="Separate test state directory; does not use the normal --config",
    )
    live.add_argument("--timeout", type=int, default=300)
    live.add_argument(
        "--reset-test-env",
        action="store_true",
        help=(
            "Back up and reset the owned, stopped test bridge configuration, then exit "
            "without probes; retain sessions, workspaces and receipts"
        ),
    )
    live.add_argument(
        "--report-dir",
        type=Path,
        help=(
            "Where a refused or failed run's report is saved when its test directory was "
            "never a verified, owned environment; defaults to this bridge's runtime directory"
        ),
    )
    workspace = commands.add_parser("workspace", help="Manage named native CLI project workspaces")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    list_workspace = workspace_commands.add_parser(
        "list", help="List workspaces, missing paths and unfinished tasks"
    )
    add_workspace = workspace_commands.add_parser("add", help="Register a project directory")
    add_workspace.add_argument("alias")
    add_workspace.add_argument("--cwd", type=Path, required=True)
    add_workspace.add_argument(
        "--create", action="store_true", help="Create the directory if missing"
    )
    add_workspace.add_argument("--binary")
    add_workspace.add_argument("--home", type=Path, help="Native CLI configuration/auth home")
    add_workspace.add_argument(
        "--active-budget", type=int, help="Active worker execution seconds (60–14400)"
    )
    remove_workspace = workspace_commands.add_parser(
        "remove", help="Preview removal of a workspace alias"
    )
    remove_workspace.add_argument("alias")
    remove_workspace.add_argument("--apply", action="store_true")
    for workspace_command in (list_workspace, add_workspace, remove_workspace):
        workspace_command.add_argument("--provider", choices=WORKSPACE_PROVIDERS, default="kiro")
    configs = commands.add_parser("config", help="Find or archive old bridge configurations")
    config_commands = configs.add_subparsers(dest="config_command", required=True)
    inventory = config_commands.add_parser("list", help="Inventory known configuration directories")
    inventory.add_argument("--root", type=Path)
    archive = config_commands.add_parser(
        "archive", help="Preview archiving the selected --config directory"
    )
    archive.add_argument("--apply", action="store_true")
    find = commands.add_parser("find", help="Find peers by name or project keywords")
    find.add_argument("query", nargs="?", default="")
    find.add_argument(
        "--provider",
        choices=(*PROVIDERS, "desktop"),
        help="Omit for all providers; desktop is the legacy alias for Codex Desktop/IDE",
    )
    send = commands.add_parser("send", help="Submit to a unique peer name or project match")
    send.add_argument("selector")
    send.add_argument(
        "--provider",
        choices=(*PROVIDERS, "desktop"),
        help="Omit for all providers; desktop is the legacy alias for Codex Desktop/IDE",
    )
    body = send.add_mutually_exclusive_group(required=True)
    body.add_argument("--message")
    body.add_argument("--message-file", type=Path)
    send.add_argument("--request-id", default=str(uuid.uuid4()))
    send.add_argument(
        "--binding",
        help=(
            "Identity binding returned for this peer by find/peer_search (only for providers "
            "whose peers carry one); if the selector now resolves to a different occupant, "
            "nothing is sent and the command fails with the generic bridge error: search "
            "again for a current selection"
        ),
    )
    tasks = commands.add_parser("tasks", help="Find recent task receipts by peer keywords")
    tasks.add_argument("query", nargs="?", default="")
    tasks.add_argument("--limit", type=int, default=30)
    for name in ("permissions", "permission-answer", "cancel"):
        control = commands.add_parser(name)
        control.add_argument("task_id", nargs="?")
        control.add_argument("--peer")
        if name == "permission-answer":
            control.add_argument(
                "--permission-id",
                required=True,
                help="ID shown by `permissions` for the exact request reviewed",
            )
            choice = control.add_mutually_exclusive_group(required=True)
            choice.add_argument("--option")
            choice.add_argument("--decision", choices=("allow-once", "reject-once"))
    mcp_config = commands.add_parser("mcp-config", help="Print a STDIO MCP entry")
    mcp_config.add_argument("--client", dest="mcp_config_client", choices=clients)
    commands.add_parser("serve", help="Run the shared bridge in the foreground")
    for name in ("start", "restart"):
        lifecycle = commands.add_parser(name, help=f"{name.title()} the bridge service")
        lifecycle.add_argument(
            "--standalone", action="store_true", help="Detach without a native supervisor"
        )
    commands.add_parser("stop", help="Stop only the bridge service")
    status = commands.add_parser("status", help="Service state and last provider observations")
    status.add_argument("--json", action="store_true")
    logs = commands.add_parser("logs", help="Read body-free operational logs")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--json", action="store_true")
    logs.add_argument("--lines", type=int, default=50)
    reset = commands.add_parser("reset", help="Clear connection observations; retain all receipts")
    reset.add_argument("--connections", action="store_true", required=True)
    configure = commands.add_parser("configure", help="Create explicit local pairing configuration")
    configure.add_argument(
        "--runtime",
        choices=("hc", "tui", "rex"),
        help="Explicitly register core or TUI; omit to start with no providers",
    )
    configure.add_argument("--session", help="Herdr TUI session, required with --runtime tui")
    configure.add_argument("--hc", default="hc")
    configure.add_argument("--hc-session", help="Optional herdr-core session, not an App space")
    configure.add_argument("--rex", default="rex")
    configure.add_argument("--rex-server")
    configure.add_argument(
        "--no-codex", action="store_true", help="Disable optional Codex Desktop/IDE discovery"
    )
    configure.add_argument("--herdr", default="herdr")
    configure.add_argument("--codex", default="codex")
    configure.add_argument("--codex-home", type=Path, default=codex_home())
    pair = commands.add_parser("pair", help="Explicitly pair a Desktop chat")
    pair.add_argument("alias")
    pair.add_argument("thread_id")
    task = commands.add_parser("task")
    task.add_argument("task_id", nargs="?")
    task.add_argument("--peer")
    task_action = task.add_mutually_exclusive_group()
    task_action.add_argument("--wait", type=int, default=0, metavar="SECONDS")
    task_action.add_argument(
        "--reconcile",
        action="store_true",
        help="Recover one supported receipt from its provider's native evidence; never resend",
    )
    complete = commands.add_parser("complete", help="Record a completed Herdr task's result")
    complete.add_argument("task_id")
    body = complete.add_mutually_exclusive_group(required=True)
    body.add_argument("--result")
    body.add_argument("--result-file", type=Path)
    abandon = commands.add_parser(
        "abandon", help="Release a receipt after manual investigation; does NOT cancel execution"
    )
    abandon.add_argument("task_id")
    abandon.add_argument("--reason", required=True)
    install = commands.add_parser("install", help="Legacy Herdr TUI and Codex connection installer")
    install.add_argument("--source", type=Path, required=True)
    install.add_argument("--session", required=True)
    install.add_argument("--herdr", default="herdr")
    install.add_argument("--codex", default="codex")
    install.add_argument("--desktop", action="append", default=[], metavar="ALIAS=CHAT_ID")
    install.add_argument("--bridge-config", type=Path)
    install.add_argument("--codex-home", type=Path, default=codex_home())
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--codex-home", type=Path, help="Override the saved Codex home")
    uninstall.add_argument(
        "--remove-tool", action="store_true", help="Also run uv tool uninstall answerback"
    )
    herdr_app = commands.add_parser(
        "herdr-app", help="Install the Herdr App side panel (the fleet column) and the themes"
    )
    herdr_app_actions = herdr_app.add_subparsers(dest="herdr_app_action", required=True)
    for action_name, purpose in (
        ("install", "Preview the installation into the App's folder; --apply writes it"),
        ("status", "Per shipped file: installed, missing or differs from shipped"),
        ("uninstall", "Preview the removal of the files that are as shipped; --apply removes them"),
    ):
        action = herdr_app_actions.add_parser(action_name, help=purpose)
        action.add_argument(
            "--directory",
            type=Path,
            help="The App's configuration folder; default $HERDR_APP_CONFIG or ~/.config/herdr-app",
        )
        if action_name != "status":
            action.add_argument("--apply", action="store_true")
        if action_name == "install":
            action.add_argument(
                "--replace",
                action="store_true",
                help="Back up and replace a shipped file that differs here",
            )
    return result


def executable_path(value: str) -> str:
    resolved = shutil.which(value)
    if not resolved:
        raise ValueError(f"Executable not found: {value}")
    return str(Path(resolved).absolute())


async def install(args: argparse.Namespace) -> dict[str, Any]:
    import tomlkit

    if os.name != "posix":
        raise ValueError("This installer currently supports macOS and Linux only")
    source = args.source.resolve()
    config_path = (args.bridge_config or args.config).expanduser().resolve()
    if (await RemoteBridge(config_path).status())["running"]:
        raise ValueError("Stop this bridge before upgrading its installed executable")
    threads = {}
    for pair in args.desktop:
        alias, separator, thread_id = pair.partition("=")
        if not separator:
            raise ValueError("--desktop must be ALIAS=CHAT_ID")
        threads[alias] = thread_id
    config = Config(
        args.session,
        executable_path(args.herdr),
        executable_path(args.codex),
        threads,
        str(args.codex_home.expanduser().resolve()),
        codex_enabled=True,
    )
    if config_path.exists():
        previous = Config.load(config_path)
        if previous.herdr_session != config.herdr_session:
            raise ValueError(
                "Existing bridge uses another session; choose a separate --bridge-config"
            )
        # Herdr and Codex come from this command's arguments; every other provider's block
        # is kept as the file had it, as are the blocks of providers no loaded plugin
        # claims (decision S-5), so an install never discards a setting it cannot read.
        for name, block in previous.providers.items():
            if name not in ("herdr", "codex"):
                config.providers[name] = replace(block)
        config.unknown_providers = dict(previous.unknown_providers)
        config.codex_enabled = previous.codex_enabled
        config.provider_selection = previous.provider_selection
        config.client_registrations = previous.client_registrations
        config.heartbeat_interval = previous.heartbeat_interval
        config.heartbeat_misses = previous.heartbeat_misses
        for alias, thread_id in previous.desktop_threads.items():
            if alias in threads and threads[alias] != thread_id:
                raise ValueError(
                    f"Existing pairing for {alias} differs; use the explicit pair command"
                )
            config.desktop_threads.setdefault(alias, thread_id)
    if config.provider_enabled("herdr"):
        await Herdr(config).check()
    if config.provider_enabled("codex"):
        help_text = await run([config.codex_binary, "queue", "--help"])
        if "--thread" not in help_text.stdout:
            raise ValueError("Codex must support queue --thread")
    mcp_path = args.codex_home.expanduser().resolve() / "config.toml"
    if mcp_path.is_symlink():
        raise ValueError("Refusing to replace a symlinked Codex config")
    if mcp_path.exists():
        existing = tomlkit.parse(mcp_path.read_text()).get("mcp_servers", {}).get(SERVER_NAME)
        if existing is not None and not owns(existing, str(config_path)):
            raise ValueError("MCP entry conflict; no installation changes made")
    uv = executable_path("uv")
    tool_root = Path((await run([uv, "tool", "dir"])).stdout.strip())
    if (tool_root / "herdr-bridge/uv-receipt.toml").exists():
        raise ValueError(
            "The herdr-bridge distribution is still installed. Follow "
            "docs/ANSWERBACK-UPGRADE.md: stop its coordinator, uninstall herdr-bridge, "
            "then install answerback. No installation changes made."
        )
    await run([uv, "tool", "install", "--force", str(source)], max_seconds=300)
    bin_dir = Path((await run([uv, "tool", "dir", "--bin"])).stdout.strip())
    executable = bin_dir / "answerback"
    if not executable.is_file():
        raise BridgeError("uv did not produce the expected answerback executable")
    config.save(config_path)
    install_mcp(mcp_path, str(executable), str(config_path))
    from . import lifecycle

    service = await lifecycle.start(config_path)
    return {
        "installed": True,
        "executable": str(executable),
        "bridge_config": str(config_path),
        "codex_config": str(mcp_path),
        "session": config.herdr_session,
        "desktop_aliases": list(config.desktop_threads),
        "fleet_writer": service["fleet_writer"],
        "next": "Run answerback doctor. Open a new Codex chat to load MCP tools.",
    }


async def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "debug":
        params = {} if args.debug_action == "status" else {"scope": args.scope}
        if args.debug_action == "on":
            params["seconds"] = args.seconds
        return await RemoteBridge(args.config).call("debug_" + args.debug_action, params)
    if args.command == "pressure":
        from .pressure.cli import dispatch as pressure_dispatch

        return await pressure_dispatch(args, RemoteBridge(args.config))
    if args.command == "actors":
        from .actors.cli import dispatch as actor_dispatch

        return actor_dispatch(args)
    if args.command == "maintenance":
        from .maintenance_cli import dispatch as maintenance_dispatch

        return maintenance_dispatch(args)
    if args.command in ("route", "workers"):
        from .routing.cli import dispatch as routing_dispatch

        return await routing_dispatch(args)
    if args.command == "capacity":
        from .placement.cli import run_cli as run_capacity_cli

        await run_capacity_cli(args)
        return
    if args.command == "rebalance":
        from .placement.rebalance_cli import run_cli as run_rebalance_cli

        await run_rebalance_cli(args)
        return
    if args.command == "fleet":
        from .fleet.cli import run_cli as run_fleet_cli

        await run_fleet_cli(args)
        return
    from . import lifecycle

    if args.command in ("clients", "skill"):
        if args.command == "clients" and args.install_action == "migrate":
            from .registration import migrate_clients

            return await migrate_clients(args.config, args.client, apply=args.apply)
        if args.command == "clients" and args.install_action != "install":
            bridge = RemoteBridge(args.config)
            if args.install_action == "list":
                return await bridge.call("client_list", {})
            return await bridge.call(
                "client_policy", {"interval": args.interval, "misses": args.misses}
            )
        from .registration import install_clients, install_skill

        selected = ["all"] if args.all else args.client
        if args.command == "clients":
            result = await install_clients(
                args.config.expanduser().resolve(),
                selected,
                name=args.name,
                apply=args.apply,
                replace=args.replace,
            )
            if args.apply:
                bridge = RemoteBridge(args.config)
                for report in result["clients"]:
                    if report["status"] not in ("installed", "unchanged"):
                        continue
                    identity = (
                        "kiro-crew" if report["client"] in ("kiro", "crew") else report["client"]
                    )
                    try:
                        await bridge.call("client_enroll", {"client": identity})
                    except (BridgeError, OSError):
                        report["registration"] = "Registers when its MCP server starts"
                    else:
                        report["registration"] = "Configured; heartbeat begins at MCP startup"
            return result
        return await install_skill(
            selected, apply=args.apply, source=args.source, replace=args.replace
        )
    if args.command == "config":
        from .config_inventory import archive_config, list_configs

        if args.config_command == "list":
            return await list_configs(args.root)
        return await archive_config(args.config.expanduser().absolute(), apply=args.apply)
    if args.command == "live-test":
        from .live_test import run_live_test

        return await run_live_test(
            args.directory, args.timeout, args.report_dir, reset_test_env=args.reset_test_env
        )
    if args.command == "self-test":
        from .harness import run_harness

        if args.report is not None and args.report.exists():
            raise ValueError("Report already exists; choose a new output path")
        report = await run_harness(
            args.config, args.mode, args.herdr_peer, args.kiro_peer, args.timeout
        )
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps(report, indent=2) + "\n")
        return report
    if args.command == "mcp-config":
        identity_args = ["--client", args.mcp_config_client] if args.mcp_config_client else []
        launch = module_launch_argv(args.config.expanduser().resolve(), "mcp", *identity_args)
        return {"mcpServers": {"answerback_bridge": {"command": launch[0], "args": launch[1:]}}}
    if args.command == "status":
        status = await RemoteBridge(args.config).status()
        status["fleet_writer"] = await lifecycle.fleet_writer_status(args.config)
        return status
    if args.command in ("start", "restart"):
        if args.command == "restart":
            await lifecycle.stop(args.config)
        return await lifecycle.start(args.config, standalone=args.standalone)
    if args.command == "stop":
        return await lifecycle.stop(args.config)
    if args.command == "logs":
        return await lifecycle.logs(args.config, follow=args.follow, lines=args.lines)
    if args.command == "configure":
        if args.config.exists():
            raise ValueError("Configuration already exists; use pair or edit it while stopped")
        runtime = args.runtime or ("tui" if args.session else None)
        if runtime in ("hc", "rex") and args.session:
            raise ValueError(
                "--session is for the older TUI; use --hc-session only for a named core"
            )
        if runtime == "tui" and not args.session:
            raise ValueError("--runtime tui requires an explicit --session")
        config = Config(
            herdr_session=args.session if runtime == "tui" else None,
            herdr_binary=executable_path(args.herdr) if runtime == "tui" else args.herdr,
            codex_binary=args.codex,
            codex_enabled=False,
            provider_selection=[{"hc": "hc", "tui": "herdr", "rex": "rex"}[runtime]]
            if runtime
            else [],
            hc_enabled=runtime == "hc",
            hc_binary=executable_path(args.hc) if runtime == "hc" else args.hc,
            hc_session=args.hc_session,
            codex_home=str(args.codex_home.expanduser().resolve()),
        )
        if runtime == "rex":
            from .rex import Rex, resolve_binary

            config.rex_binary = resolve_binary(binary_argument(args.rex) or "rex")
            observed = await Rex(config.rex_binary, args.rex_server).probe()
            config.rex_enabled = True
            config.rex_server = observed["endpoint"]
            config.rex_host_id = observed["server_host_id"]
        config.save(args.config)
        return {"configured": str(args.config), "next": "answerback start"}
    if args.command == "herdr-app":
        from .providers.herdr import herdr_app

        directory = herdr_app.config_directory(given=args.directory)
        if args.herdr_app_action == "status":
            return herdr_app.inspect(directory)
        if args.herdr_app_action == "install":
            return herdr_app.install(directory, apply=args.apply, replace=args.replace)
        return herdr_app.uninstall(directory, apply=args.apply)
    if args.command == "install":
        result = await install(args)
        from .providers.herdr import herdr_app

        # The App's side panel and themes go in with the bridge; a folder of the App that is not
        # here is reported, nothing else. A file the person changed is kept and named.
        result["herdr_app"] = herdr_app.install_step(herdr_app.config_directory())
        return result
    if args.command == "uninstall":
        await lifecycle.stop(args.config)
        saved_home = Config.load(args.config).codex_home if args.config.exists() else None
        selected_home = args.codex_home or (Path(saved_home) if saved_home else codex_home())
        uninstall_mcp(
            selected_home.expanduser().resolve() / "config.toml", str(args.config.resolve())
        )
        # The App's panel goes before the tool: `--remove-tool` removes this very package from
        # disk, so the module must be imported, and its step run, while it is still here.
        from .providers.herdr import herdr_app

        herdr_app_report = herdr_app.uninstall_step(herdr_app.config_directory())
        if args.remove_tool:
            await run(["uv", "tool", "uninstall", "answerback"], max_seconds=60)
        return {
            "removed_mcp": True,
            "retained": "Bridge pairing, task receipts and config backups",
            "herdr_app": herdr_app_report,
        }
    bridge = RemoteBridge(args.config)
    handler: CliHandler | None = getattr(args, "plugin_handler", None)
    if handler is not None:
        return await handler(args, bridge)
    if args.command == "notification-status":
        return await bridge.call(
            "client_notification_status",
            {"client": args.client, "request_id": args.request_id},
        )
    if args.command == "notify":
        return await bridge.call(
            "client_notify",
            {
                "client": args.client,
                "message": args.message,
                "topic": args.topic,
                "request_id": args.request_id,
            },
        )
    if args.command == "providers":
        if args.provider_action == "list":
            return await bridge.call("provider_list", {})
        params: dict[str, Any] = {"provider": args.provider}
        if args.provider_action == "register":
            params.update(
                binary=binary_argument(args.binary) if args.binary else None,
                session=args.session,
                port=args.port,
            )
        return await bridge.call(f"provider_{args.provider_action}", params)
    if args.command == "workspace":
        selection = {} if args.provider == "kiro" else {"provider": args.provider}
        if args.workspace_command == "list":
            return await bridge.call("workspace_list", selection)
        if args.workspace_command == "remove":
            return await bridge.call(
                "workspace_remove", {"alias": args.alias, "apply": args.apply, **selection}
            )
        cwd = await asyncio.to_thread(lambda: str(Path(args.cwd).expanduser().resolve()))
        return await bridge.call(
            "workspace_add",
            {
                "alias": args.alias,
                "cwd": cwd,
                "create": args.create,
                "binary": binary_argument(args.binary),
                **selection,
                **({"home": str(args.home.expanduser().resolve())} if args.home else {}),
                **({"active_budget": args.active_budget} if args.active_budget is not None else {}),
            },
        )
    if args.command == "find":
        return await bridge.call("peer_search", {"query": args.query, "provider": args.provider})
    if args.command == "send":
        message = bounded_file(args.message_file, 100_000) if args.message_file else args.message
        return await bridge.call(
            "peer_submit",
            {
                "selector": args.selector,
                "message": message,
                "request_id": args.request_id,
                "provider": args.provider,
                "binding": args.binding,
            },
        )
    if args.command == "tasks":
        return await bridge.call("task_list", {"query": args.query, "limit": args.limit})
    if args.command in ("task", "permissions", "permission-answer", "cancel"):
        if bool(args.task_id) == bool(args.peer):
            raise ValueError("Specify either a task ID or --peer with a name/project selector")
        if args.peer:
            args.task_id = (await bridge.call("task_for_peer", {"selector": args.peer}))["id"]
    if args.command == "permissions":
        return await bridge.call("task_permissions", {"task_id": args.task_id})
    if args.command == "cancel":
        return await bridge.call("task_cancel", {"task_id": args.task_id})
    if args.command == "permission-answer":
        # The task is re-resolved from --peer above, so a human who could omit
        # --permission-id would approve whatever happens to be pending at answer time,
        # not the request they reviewed with `permissions`. The parser therefore requires
        # the id, and it must still be live in this task's current inbox: the reviewed
        # request may have ended (cancel, the deadline) and been replaced by another.
        inbox = await bridge.call("task_permissions", {"task_id": args.task_id})
        candidates = [item for item in inbox["permissions"] if item["id"] == args.permission_id]
        if not candidates:
            raise ValueError(
                "That permission is no longer pending on this task; run `permissions` "
                "again and answer the request you actually reviewed"
            )
        if len(candidates) != 1:
            raise ValueError("Select one pending permission with --permission-id")
        item = candidates[0]
        option = args.option
        if args.decision:
            choices = [
                choice
                for choice in item["options"]
                if choice["kind"] == args.decision.replace("-", "_")
            ]
            if len(choices) != 1:
                raise ValueError("Select one of the supplied permission options explicitly")
            option = choices[0]["optionId"]
        answer = await bridge.call(
            "permission_answer",
            {"task_id": args.task_id, "permission_id": item["id"], "option_id": option},
        )
        return {**answer, "approved_tool_call": item.get("tool_call")}
    if args.command == "reset":
        return await bridge.call("reset", {})
    if args.command == "pair":
        return await bridge.call("pair", {"alias": args.alias, "thread_id": args.thread_id})
    if args.command == "doctor":
        return await bridge.call("doctor", {})
    if args.command == "peers":
        return await bridge.call("peers", {})
    if args.command == "task":
        if args.reconcile:
            return await bridge.call("task_reconcile", {"task_id": args.task_id})
        if args.wait:
            return await bridge.call("task_wait", {"task_id": args.task_id, "seconds": args.wait})
        return await bridge.call("task_get", {"task_id": args.task_id})
    if args.command == "complete":
        text = bounded_file(args.result_file, 200_000) if args.result_file else args.result
        return await bridge.call("task_complete", {"task_id": args.task_id, "result": text})
    if args.command == "abandon":
        return await bridge.call("task_abandon", {"task_id": args.task_id, "reason": args.reason})
    raise ValueError("Unknown command")


# Process exit codes (EXIT_* from .exit_codes; documented in docs/NATIVE-CLI.md "Exit
# codes" and the README). 0: a completed task or a successful command; 130: Ctrl-C.
_FAILURE_STATUSES = (
    "failed",
    "uncertain",
    "abandoned",
    "cancelled",
    "needs_selection",
    "provider_backoff",
)


def exit_code_for(args: argparse.Namespace, result: dict[str, Any]) -> int:
    """Map a `dispatch()` result to the process exit code; 0 means success.

    `task --wait` can return before any terminal status: `wait_task` sets
    `wait_timed_out` once its own deadline expires, and a task `blocked` on a pending
    permission is neither done nor failed. Both must exit non-zero, with codes a script
    can tell apart from a failure, or `$?` cannot distinguish "done" from "still
    waiting". A blocked task that also outlived the wait reports `blocked`: answering
    the permission, not waiting longer, is the caller's next step.
    """
    status = result.get("status")
    if (
        status in _FAILURE_STATUSES
        or result.get("healthy") is False
        or result.get("success") is False
        or (
            args.command == "config"
            and args.config_command == "archive"
            and args.apply
            and not result.get("archived")
        )
    ):
        return EXIT_FAILURE
    if status == "blocked":
        return EXIT_BLOCKED
    if result.get("wait_timed_out"):
        return EXIT_WAIT_TIMED_OUT
    return 0


def main() -> None:
    import argcomplete

    command_parser = parser()
    # Must run before parsing or dispatch so Tab cannot start a service or send work.
    argcomplete.autocomplete(command_parser)
    args = command_parser.parse_args()
    if args.command == "maintenance" and args.maintenance_action is None:
        print(args.maintenance_help, end="")
        return
    plugin_main = getattr(args, "plugin_main", None)
    if callable(plugin_main):
        plugin_main(args)
        return
    if args.command == "events":
        from .events.cli import run_cli as run_events_cli

        run_events_cli(args)
        return
    if args.command == "completion":
        print(
            argcomplete.shellcode(
                ["answerback", "answerback-bridge", "herdr-bridge", "codex-herdr"], shell=args.shell
            ),
            end="",
        )
        return
    try:
        if args.command == "serve":
            from .runtime import Service, record_startup_failure, run_service

            os.umask(0o077)
            try:
                exit_code = run_service(Service(args.config))
                if exit_code:
                    raise SystemExit(exit_code)
            except (BridgeError, OSError, ValueError) as error:
                # launchd (KeepAlive.SuccessfulExit=False) and systemd (Restart=on-failure)
                # restart only after a non-zero exit. Once startup has failed repeatedly
                # inside the window (an incompatible config after an upgrade), exit 0 so
                # the supervisor stops the loop instead of retrying every ten seconds.
                if record_startup_failure(args.config):
                    print(
                        json.dumps(
                            {
                                "error": str(error),
                                "crash_loop_guard": (
                                    "Repeated serve startup failures; exiting 0 so the "
                                    "launchd/systemd restart loop stops. Fix the error "
                                    "above, then run 'answerback start' to resume."
                                ),
                            }
                        ),
                        file=sys.stderr,
                    )
                    return
                raise
            return
        if args.command == "mcp":
            if args.actor_binding_file is not None:
                from .actors.api import load_binding
                from .actors.frontend import create_actor_server

                create_actor_server(
                    RemoteBridge(args.config), load_binding(args.actor_binding_file)
                ).run(transport="stdio")
                return
            from .mcp_server import create_server

            create_server(RemoteBridge(args.config), client_name=args.mcp_client).run(
                transport="stdio"
            )
            return
        result = asyncio.run(dispatch(args))
        if args.command == "pressure" and not args.json:
            from .pressure.cli import render

            print(render(result))
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
        code = exit_code_for(args, result)
        if code:
            raise SystemExit(code)
    except (BridgeError, OSError, ValueError) as error:
        from .maintenance import MaintenanceError

        payload = {"error": str(error)}
        if isinstance(error, MaintenanceError):
            payload["code"] = error.code
        # The path travels beside the message, never inside it (str(error)
        # is a catalogue literal matched by exact string; see errors.py and
        # TransportUnavailable's own docstring).
        if isinstance(error, TransportUnavailable) and error.config_path is not None:
            payload["config_path"] = str(error.config_path)
        print(json.dumps(payload), file=sys.stderr)
        raise SystemExit(EXIT_FAILURE) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
