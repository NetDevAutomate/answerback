"""The kernel's public surface, and what the installed plugin adds to it and nothing more.

The PUBLIC composition (the default sync) must show exactly the public sets; the INTERNAL
one (the plugin installed) exactly the public sets plus the plugin's own names.
"""

import argparse
import importlib
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from composition import contributed

from answerback_bridge import adapters, providers
from answerback_bridge.cli import parser
from answerback_bridge.config import CLIENT_NAMES, FLAT_KEYS, PROVIDERS, SCHEMAS, Config, _Flat
from answerback_bridge.registration import CLIENTS

PUBLIC_PROVIDERS = {
    "hc",
    "herdr",
    "codex",
    "kiro",
    "crew",
    "rex",
    "codex_cli",
    "claude_code",
    "grok",
}
PUBLIC_CLIENTS = {"codex", "claude", "grok", "kiro", "crew"}
# Config fields by their saved name: the kernel's own, then `providers.<name>.<field>` for
# each provider's block (T-198). The flat pre-0.16 names are read-compatibility only and
# are pinned separately below.
KERNEL_CONFIG_FIELDS = {
    "provider_selection",
    "client_registrations",
    "heartbeat_interval",
    "heartbeat_misses",
}
PUBLIC_PROVIDER_FIELDS = {
    "herdr": {"session", "binary"},
    "codex": {"binary", "desktop_threads", "home", "enabled"},
    "kiro": {
        "binary",
        "workspaces",
        "acp_engine",
        "trust_all_tools",
        "trust_tools",
        "active_budget",
    },
    "crew": {"binary", "enabled", "port"},
    "hc": {
        "enabled",
        "binary",
        "session",
        "send_to_done",
        "send_machines",
        "events",
        "placement_policy",
    },
    "rex": {"enabled", "binary", "server", "host_id"},
    **{
        provider: {"binary", "home", "workspaces", "active_budget"}
        for provider in ("codex_cli", "claude_code", "grok")
    },
}
PUBLIC_CONFIG_FIELDS = KERNEL_CONFIG_FIELDS | {
    f"providers.{provider}.{name}"
    for provider, names in PUBLIC_PROVIDER_FIELDS.items()
    for name in names
}
# The flat names the loader still reads (until 0.17) and `Config` still exposes.
PUBLIC_FLAT_FIELDS = {
    "herdr_session",
    "herdr_binary",
    "codex_binary",
    "desktop_threads",
    "codex_home",
    "codex_enabled",
    "kiro_binary",
    "kiro_workspaces",
    "kiro_acp_engine",
    "kiro_trust_all_tools",
    "kiro_trust_tools",
    "kiro_active_budget",
    "crew_binary",
    "crew_enabled",
    "crew_port",
    "hc_enabled",
    "hc_binary",
    "hc_session",
    "hc_send_to_done",
    "hc_send_machines",
    "hc_events",
    "hc_placement_policy",
    "rex_enabled",
    "rex_binary",
    "rex_server",
    "rex_host_id",
    *(
        f"{provider}_{field}"
        for provider in ("codex_cli", "claude_code", "grok")
        for field in ("binary", "home", "workspaces", "active_budget")
    ),
}


# What this composition must show: the public sets, plus the installed plugin's names when
# one is (`PRIVATE_*` keeps the name the kernel suite has used for the internal build).
PRIVATE_PROVIDERS = PUBLIC_PROVIDERS | set(contributed("PROVIDERS", ()))
PRIVATE_CLIENTS = PUBLIC_CLIENTS | set(contributed("CLIENTS", ()))
PRIVATE_PROVIDER_FIELDS = {**PUBLIC_PROVIDER_FIELDS, **contributed("PROVIDER_FIELDS", {})}
PRIVATE_CONFIG_FIELDS = PUBLIC_CONFIG_FIELDS | contributed("CONFIG_FIELDS", set())
PRIVATE_FLAT_FIELDS = PUBLIC_FLAT_FIELDS | contributed("FLAT_FIELDS", set())


INSTALLER_KIT_PUBLIC_NAMES = {
    "SERVER_NAME",
    "LEGACY_SERVER_NAMES",
    "Snapshot",
    "SETUP_SKILL_NAME",
    "BRIDGE_MODULES",
    "absolute",
    "safe_path",
    "safe_parent",
    "read",
    "json_object",
    "route_matches",
    "owned_route",
    "add_safe_path_flag",
    "launch_record",
    "snapshot",
    "tree_matches",
    "private_copy",
    "private_tree",
    "run",
}


def test_installer_kit_has_a_deliberate_public_surface() -> None:
    kit = importlib.import_module("answerback_bridge.installer_kit")

    assert set(kit.__all__) == INSTALLER_KIT_PUBLIC_NAMES
    assert all(not name.startswith("_") for name in kit.__all__)


def saved_field_names(saved: dict[str, Any]) -> set[str]:
    """The names a saved config carries, nested ones as `providers.<name>.<field>`."""
    names = {key for key in saved if key != "providers"}
    for provider, block in saved.get("providers", {}).items():
        names.update(f"providers.{provider}.{name}" for name in block)
    return names


def test_public_provider_and_client_catalogues() -> None:
    assert set(PROVIDERS) == PRIVATE_PROVIDERS
    assert set(CLIENT_NAMES) == PRIVATE_CLIENTS | {"kiro-crew"}
    assert set(CLIENTS) == PRIVATE_CLIENTS


def test_public_configuration_has_no_extension_fields(tmp_path: Path) -> None:
    assert {item.name for item in fields(Config)} == KERNEL_CONFIG_FIELDS | {
        "providers",
        "unknown_providers",
    }
    assert {
        f"providers.{provider}.{item.name}"
        for provider, schema in SCHEMAS.items()
        for item in fields(schema)
    } == PRIVATE_CONFIG_FIELDS - KERNEL_CONFIG_FIELDS
    assert set(FLAT_KEYS) == PRIVATE_FLAT_FIELDS
    # The kernel declares a descriptor per flat name of its own providers; a plugin's flat
    # names reach their block through `Config.__getattr__` instead.
    assert {name for name, value in vars(Config).items() if isinstance(value, _Flat)} == (
        PUBLIC_FLAT_FIELDS
    )
    path = tmp_path / "config.json"
    config = Config(provider_selection=list(PRIVATE_PROVIDERS))
    config.save(path)
    saved = json.loads(path.read_text())
    assert saved_field_names(saved) == PRIVATE_CONFIG_FIELDS
    assert not (set(saved) & PRIVATE_FLAT_FIELDS)
    assert Config.load(path) == config


def test_unknown_configuration_is_refused_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = '{"provider_selection": [], "extension_only": {"enabled": true}}\n'
    path.write_text(original)
    with pytest.raises(ValueError, match="extension_only"):
        Config.load(path)
    assert path.read_text() == original


def test_cli_exposes_only_public_session_registration() -> None:
    subcommands = next(
        action for action in parser()._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert {name for name in subcommands.choices if name.endswith("-register")} == {
        "kiro-register",
        *contributed("REGISTER_SUBCOMMANDS", ()),
    }


def test_distributed_adapter_modules_match_the_public_surface() -> None:
    root = Path(adapters.__file__).parent
    assert {path.stem for path in root.glob("*.py")} == {
        "__init__",
        "base",
        "builtin_cli",
        "builtin_tools",
        "builtins",
        "catalog",
        "codex",
        "contracts",
        "crew",
        "factory",
        "kiro",
        "native",
        "registry",
        "rex",
        "router",
    }


def test_distributed_provider_modules_match_the_public_surface() -> None:
    root = Path(providers.__file__).parent
    assert {path.relative_to(root).as_posix() for path in root.rglob("*.py")} == {
        "__init__.py",
        "codex/__init__.py",
        "codex/adapter.py",
        "codex/cli.py",
        "codex/config.py",
        "codex/desktop.py",
        "codex/history.py",
        "codex/plugin.py",
        "codex/reasons.py",
        "codex/tools.py",
        "codex/verbs.py",
        "crew/__init__.py",
        "crew/adapter.py",
        "crew/client.py",
        "crew/cli.py",
        "crew/config.py",
        "crew/plugin.py",
        "crew/reasons.py",
        "crew/tools.py",
        "crew/verbs.py",
        "herdr/__init__.py",
        "herdr/adapters/__init__.py",
        "herdr/adapters/core.py",
        "herdr/adapters/herdr.py",
        "herdr/cli.py",
        "herdr/config.py",
        "herdr/core.py",
        "herdr/core_events.py",
        "herdr/core_rpc.py",
        "herdr/core_tasks.py",
        "herdr/herdr.py",
        "herdr/herdr_app.py",
        "herdr/plugin.py",
        "herdr/reasons.py",
        "herdr/tools.py",
        "herdr/verbs.py",
        "kiro/__init__.py",
        "kiro/acp.py",
        "kiro/adapter.py",
        "kiro/agent.py",
        "kiro/cli.py",
        "kiro/config.py",
        "kiro/plugin.py",
        "kiro/reasons.py",
        "kiro/tools.py",
        "kiro/verbs.py",
        "kiro/workers.py",
        "native/__init__.py",
        "native/adapter.py",
        "native/claude.py",
        "native/codex.py",
        "native/config.py",
        "native/grok.py",
        "native/plugin.py",
        "native/protocol.py",
        "native/reasons.py",
        "native/workers.py",
        "rex/__init__.py",
        "rex/adapter.py",
        "rex/client.py",
        "rex/cli.py",
        "rex/config.py",
        "rex/mailbox.py",
        "rex/plugin.py",
        "rex/reasons.py",
        "rex/service.py",
        "rex/tools.py",
        "rex/verbs.py",
    }
