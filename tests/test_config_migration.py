"""The config split (T-198): flat keys read until 0.17, nested keys written from 0.16.

Pins both directions of decision S-6 (a flat file loads as before; `save` writes only
`providers.<name>` blocks plus the kernel fields), decision S-5 (a provider no loaded plugin
claims is disabled with a warning, never a refusal; `select_provider` on it still refuses),
the compatibility surface the adapters keep using (`config.kiro_binary`, `replace(config,
hc_enabled=True)`, `__dict__.update`), and the six redacted live configs under
`tests/fixtures/live-configs/` (host names and home paths neutralised, the kernel's fields
only): each reads every public field exactly as `main` read it before the split
(`expected/<host>.json`, produced by the pre-split loader with `snapshot_fields.py`). The
same configs with the installed plugin's block are the plugin's own fixtures and tests.
"""

import json
import logging
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

import pytest
from composition import PLUGIN_PROVIDERS, pick

from answerback_bridge.adapters.builtins import builtin_plugins
from answerback_bridge.config import (
    FLAT_KEYS,
    PROVIDERS,
    SCHEMAS,
    Config,
    routing_digest,
)
from answerback_bridge.provider_config import KiroConfig, ProviderConfig
from answerback_bridge.service import Bridge

FIXTURES = Path(__file__).parent / "fixtures" / "live-configs"
# The work Mac's config exactly as redacted (`<redacted>` paths, anonymised thread keys):
# unloadable on `main` and here, kept to pin WHICH refusal it gets; `work-mac.json` is the
# loadable repair of it.
ORIGINAL_WORKMAC = "work-mac-redacted-original.json"
LIVE_CONFIGS = sorted(
    path.name for path in FIXTURES.glob("*.json") if path.name != ORIGINAL_WORKMAC
)
# `routing_digest` of each live config as the pre-split `main` loader computed it (printed
# by `snapshot_fields.py` run against `src` at ab693a31): the internal composition, whose
# plugin hashes its block at the defaults the pre-split kernel had, still produces them. The
# public composition composes no such block and hashes without it: its digests, computed at
# the T-269 tip, are pinned beside. Published configuration digests, not credentials.
_LAN_MAC = pick(
    "224bc655e888c30f95c9985e19567b4dcdafb14ddd9d13630ff3d45babb0165b",  # pragma: allowlist secret
    "2256f8ec2dc92dea68edd03ae28e990d9a56d7eb484fd4b1b9731c277aac1112",  # pragma: allowlist secret
)
_LAN_LINUX = pick(
    "c569006350b01414f48149ae3e33ef2e165efcdda3fc2726dfb645f6f12841da",  # pragma: allowlist secret
    "0319b9950bdbcca8c6c054db971b00ff050c4e64ea50207a365e114ed2d08c0d",  # pragma: allowlist secret
)
_WORKMAC = pick(
    "f7a90ff38e98977786cc95c909f22e961f03fdea109bc5fbec382aedbfd27f7c",  # pragma: allowlist secret
    "96d56e2cb5458704487ced171338de59ce271b6cc41e29e7909dddbcc59cddd4",  # pragma: allowlist secret
)
LIVE_DIGESTS = {
    "lan-linux-a.json": _LAN_LINUX,
    "lan-linux-b.json": _LAN_LINUX,
    "lan-linux-c.json": _LAN_LINUX,
    "lan-mac-a.json": _LAN_MAC,
    "lan-mac-b.json": _LAN_MAC,
    "work-mac.json": _WORKMAC,
}
KERNEL = {"provider_selection", "client_registrations", "heartbeat_interval", "heartbeat_misses"}

FLAT_DOCUMENT: dict[str, Any] = {
    "herdr_session": "fixture",
    "herdr_binary": "/fixture/bin/herdr",
    "codex_binary": "/fixture/bin/codex",
    "desktop_threads": {"review": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
    "codex_home": "/fixture/codex",
    "codex_enabled": True,
    "kiro_binary": "/fixture/bin/kiro",
    "kiro_workspaces": {"worker": "/fixture/project"},
    "kiro_acp_engine": "v2",
    "kiro_trust_tools": ["read"],
    "kiro_active_budget": 900,
    "crew_binary": "/fixture/bin/crew",
    "crew_enabled": True,
    "crew_port": 9876,
    "hc_enabled": True,
    "hc_binary": "/fixture/bin/hc",
    "hc_send_machines": ["build-box"],
    "provider_selection": ["hc", "herdr", "codex", "kiro", "crew"],
    "client_registrations": ["codex", "kiro"],
    "heartbeat_interval": 12,
    "heartbeat_misses": 4,
    "rex_binary": "/fixture/bin/rex",
    "claude_code_workspaces": {"reviewer": "/fixture/project"},
}


def nested(document: dict[str, Any]) -> dict[str, Any]:
    """The same document in the shape `save` writes."""
    result: dict[str, Any] = {key: value for key, value in document.items() if key in KERNEL}
    blocks: dict[str, dict[str, Any]] = {}
    for key, value in document.items():
        if key in FLAT_KEYS:
            provider, name = FLAT_KEYS[key]
            blocks.setdefault(provider, {})[name] = value
    result["providers"] = blocks
    return result


def write(tmp_path: Path, document: dict[str, Any], name: str = "config.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document, indent=2) + "\n")
    path.chmod(0o600)
    return path


def live_config(tmp_path: Path, name: str) -> Path:
    """A checked-in live config copied to a path `Config.load` accepts.

    The loader refuses a config another user could write (`_check_config_path`), and a
    checkout's mode is the checkout's (a `775`/`664` worktree is common on shared Linux
    nodes); the copy, not the repository path, is what a test loads.
    """
    directory = tmp_path / "live"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    path.write_bytes((FIXTURES / name).read_bytes())
    path.chmod(0o600)
    return path


def flat_keys_in(saved: dict[str, Any]) -> set[str]:
    return set(saved) & set(FLAT_KEYS)


@pytest.fixture
def warnings(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.WARNING, logger="answerback_bridge.config")
    return caplog


# --- S-6: flat read, nested write ---------------------------------------------------------


def test_flat_and_nested_documents_load_to_the_same_config(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    flat = Config.load(write(tmp_path, FLAT_DOCUMENT, "flat.json"))
    nest = Config.load(write(tmp_path, nested(FLAT_DOCUMENT), "nested.json"))
    assert flat == nest
    assert routing_digest(flat) == routing_digest(nest)
    for key, value in FLAT_DOCUMENT.items():
        assert getattr(flat, key) == value, key
    assert warnings.records == []


def test_first_save_after_upgrade_writes_the_nested_shape_and_no_flat_key(tmp_path: Path) -> None:
    path = write(tmp_path, FLAT_DOCUMENT)
    config = Config.load(path)
    config.save(path)
    saved = json.loads(path.read_text())
    assert flat_keys_in(saved) == set()
    assert set(saved) == KERNEL | {"providers"}
    assert saved == {**nested(FLAT_DOCUMENT), "providers": saved["providers"]}
    for provider, block in nested(FLAT_DOCUMENT)["providers"].items():
        assert block.items() <= saved["providers"][provider].items(), provider
    # A written block is whole: fields the flat file left at their defaults are explicit now.
    assert saved["providers"]["kiro"]["trust_all_tools"] is False
    assert Config.load(path) == config


def test_unselected_default_blocks_are_not_written(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    assert json.loads(path.read_text()) == {
        "provider_selection": [],
        "client_registrations": [],
        "heartbeat_interval": 10,
        "heartbeat_misses": 3,
        "providers": {},
    }
    Config(provider_selection=["grok"]).save(path)
    assert set(json.loads(path.read_text())["providers"]) == {"grok"}
    Config(provider_selection=[], grok_home="/fixture/grok").save(path)
    assert set(json.loads(path.read_text())["providers"]) == {"grok"}


def test_nested_value_wins_over_its_flat_twin_with_one_warning(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    document = {
        **nested(FLAT_DOCUMENT),
        "kiro_binary": "/stale/kiro",
        "hc_send_machines": ["stale-box"],
        "herdr_session": "fixture",  # a flat key with no nested twin still fills its field
    }
    del document["providers"]["herdr"]
    path = write(tmp_path, document)
    config = Config.load(path)
    assert config.kiro_binary == "/fixture/bin/kiro"
    assert config.hc_send_machines == ["build-box"]
    assert config.herdr_session == "fixture"
    assert [record.levelno for record in warnings.records] == [logging.WARNING]
    message = warnings.records[0].getMessage()
    assert "hc_send_machines, kiro_binary" in message and str(path) in message
    assert "herdr_session" not in message


def test_absent_blocks_take_their_defaults(tmp_path: Path) -> None:
    assert Config.load(write(tmp_path, {})) == Config()
    assert Config.load(write(tmp_path, {"providers": {}}, "empty.json")) == Config()
    assert Config().providers == {name: SCHEMAS[name]() for name in PROVIDERS}


def test_legacy_desktop_pairing_infers_codex_in_either_shape(tmp_path: Path) -> None:
    threads = {"planner": "11111111-2222-3333-4444-555555555555"}
    flat = Config.load(write(tmp_path, {"desktop_threads": threads}, "flat.json"))
    nest = Config.load(
        write(tmp_path, {"providers": {"codex": {"desktop_threads": threads}}}, "nested.json")
    )
    assert flat == nest and flat.codex_enabled is True
    explicit = write(
        tmp_path,
        {"providers": {"codex": {"desktop_threads": threads, "enabled": False}}},
        "explicit.json",
    )
    assert Config.load(explicit).codex_enabled is False


@pytest.mark.parametrize(
    ("document", "named"),
    [
        ({"providers": []}, "'providers' must be a mapping"),
        ({"providers": {"kiro": []}}, "'providers.kiro' must be a mapping"),
        (
            {"providers": {"codex": {"desktop_threads": []}}},
            "'providers.codex.desktop_threads' must be a mapping",
        ),
        (
            {"providers": {"kiro": {"trust_tools": "read"}}},
            "'providers.kiro.trust_tools' must be a list",
        ),
        ({"providers": {"kiro": {"removed_field": 1}}}, "removed_field"),
        ({"unknown_providers": {}}, "unexpected key 'unknown_providers'"),
        ({"kiro_binary": "x", "providers": {"kiro": {"acp_engine": "v9"}}}, "'v2' or 'v3'"),
    ],
)
def test_nested_shape_errors_name_the_path_and_the_field(
    tmp_path: Path, document: dict[str, Any], named: str
) -> None:
    path = write(tmp_path, document)
    with pytest.raises(ValueError, match="incompatible with this version") as info:
        Config.load(path)
    assert str(path) in str(info.value) and named in str(info.value)
    assert not isinstance(info.value, TypeError)


# --- S-5: an unknown provider is disabled, never refused -----------------------------------


def test_unknown_provider_block_is_disabled_kept_and_listed(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    document = {
        **nested(FLAT_DOCUMENT),
        "provider_selection": ["hc", "kiro", "opencode"],
        "providers": {
            **nested(FLAT_DOCUMENT)["providers"],
            "opencode": {"binary": "/opt/opencode", "nested": {"keep": [1, 2]}},
            "pi": {},
        },
    }
    path = write(tmp_path, document)
    config = Config.load(path)
    assert config.registered_providers() == ["hc", "kiro"]
    assert config.provider_enabled("opencode") is False
    assert config.disabled_providers() == ["opencode", "pi"]
    assert config.unknown_providers["opencode"] == document["providers"]["opencode"]
    assert config.unknown_providers["pi"] == {}
    assert len(warnings.records) == 1
    assert "opencode, pi" in warnings.records[0].getMessage()
    # Saving back loses nothing of the provider this install cannot run.
    config.heartbeat_interval = 20
    config.save(path)
    saved = json.loads(path.read_text())
    assert saved["providers"]["opencode"] == document["providers"]["opencode"]
    assert saved["providers"]["pi"] == {}
    assert saved["provider_selection"] == ["hc", "kiro", "opencode"]
    assert Config.load(path) == config


def test_run6_an_unknown_block_holding_kernel_field_names_is_kept_not_shape_checked(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    """red_at_base (run-6 hardening h1): the unclaimed block was checked against Config."""
    block = {"providers": "not a mapping", "client_registrations": 5, "unknown_providers": []}
    path = write(tmp_path, {"providers": {"opencode": block}})
    config = Config.load(path)
    assert config.disabled_providers() == ["opencode"]
    assert config.unknown_providers["opencode"] == block
    assert "opencode" in warnings.records[0].getMessage()
    with pytest.raises(ValueError, match="field 'client_registrations' must be a list"):
        Config.load(write(tmp_path, {"client_registrations": 5}))


def test_unknown_name_in_selection_alone_is_disabled_with_a_warning(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    path = write(tmp_path, {"provider_selection": ["kiro", "fixtureprov-next"]})
    config = Config.load(path)
    assert config.registered_providers() == ["kiro"]
    assert config.disabled_providers() == ["fixtureprov-next"]
    assert [record.getMessage() for record in warnings.records] == [
        f"Bridge config at {path} names providers no loaded plugin claims; they are disabled "
        "and their settings are kept: fixtureprov-next"
    ]


def test_unknown_client_registration_is_kept_and_not_notified_with_a_warning(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    """S-5 applied to `client_registrations`: a registered MCP client no loaded plugin
    installs stays in the file (an install that has its plugin keeps the registration),
    `disabled_clients` names it, `active_clients` leaves it out, and `load` warns once about
    it after the providers warning. Pinned before T-269 by the public reading of the live
    configs, whose registered client is now the plugin's; the rule is the kernel's."""
    document = {
        "provider_selection": ["kiro", "fixtureprov"],
        "client_registrations": ["kiro", "fixtureprov"],
    }
    path = write(tmp_path, document)
    config = Config.load(path)
    assert config.registered_providers() == ["kiro"]
    assert config.disabled_providers() == ["fixtureprov"]
    assert config.active_clients() == ["kiro"]
    assert config.disabled_clients() == ["fixtureprov"]
    assert config.client_registrations == ["kiro", "fixtureprov"]
    assert [record.getMessage() for record in warnings.records] == [
        f"Bridge config at {path} names providers no loaded plugin claims; they are disabled "
        "and their settings are kept: fixtureprov",
        f"Bridge config at {path} registers MCP clients no loaded plugin installs; they are "
        "kept and not notified: fixtureprov",
    ]
    config.save(path)
    assert json.loads(path.read_text())["client_registrations"] == ["kiro", "fixtureprov"]


# A provider no loaded plugin claims, selected by the file; its name holds an underscore so
# the longest-prefix split is pinned too. A fixture name, never a real provider's.
UNCLAIMED = "fixture_prov"


def test_flat_keys_of_a_selected_unclaimed_provider_load_into_its_kept_block(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    """A pre-0.16 flat key `<provider>_<field>` of a provider the file selects and no loaded
    plugin claims is that provider's field (docs/ANSWERBACK-UPGRADE.md: a flat file loads
    unchanged, so one written where the provider's plugin was installed loads without it).
    The block lands in `unknown_providers`, kept and disabled with one warning (S-5), and
    `save` writes it nested under `providers.<name>` with no flat key. Both compositions."""
    assert UNCLAIMED not in SCHEMAS
    flat = {f"{UNCLAIMED}_binary": "/fixture/bin", f"{UNCLAIMED}_threads": {"t": {"a": "b"}}}
    path = write(tmp_path, {"provider_selection": ["kiro", UNCLAIMED], **flat})
    config = Config.load(path)
    assert [record.getMessage() for record in warnings.records] == [
        f"Bridge config at {path} names providers no loaded plugin claims; they are disabled "
        f"and their settings are kept: {UNCLAIMED}"
    ]
    assert config.unknown_providers[UNCLAIMED] == {
        "binary": "/fixture/bin",
        "threads": {"t": {"a": "b"}},
    }
    assert config.registered_providers() == ["kiro"]
    assert config.disabled_providers() == [UNCLAIMED]
    config.save(path)
    saved = json.loads(path.read_text())
    assert not set(flat) & set(saved)
    assert saved["providers"][UNCLAIMED] == config.unknown_providers[UNCLAIMED]
    assert Config.load(path).unknown_providers[UNCLAIMED] == config.unknown_providers[UNCLAIMED]


def test_an_installed_plugins_flat_keys_are_claimed_by_its_schema_first(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    """With a plugin installed its schema's `flat_keys()` claims `<plugin>_<field>`, so the
    same flat file fills the plugin's own block, never `unknown_providers`. The public
    composition has no plugin: every flat key is a kernel provider's."""
    if not PLUGIN_PROVIDERS:
        assert {provider for provider, _ in FLAT_KEYS.values()} <= set(SCHEMAS)
        return
    (plugin,) = PLUGIN_PROVIDERS
    claimed = {key: name for key, (provider, name) in FLAT_KEYS.items() if provider == plugin}
    assert claimed
    defaults = asdict(SCHEMAS[plugin]())
    flat = {key: defaults[name] for key, name in claimed.items()}
    config = Config.load(write(tmp_path, {"provider_selection": ["kiro", plugin], **flat}))
    assert warnings.records == []
    assert plugin not in config.unknown_providers
    assert config.registered_providers() == [name for name in PROVIDERS if name in {"kiro", plugin}]
    assert {name: getattr(config.providers[plugin], name) for name in claimed.values()} == {
        name: defaults[name] for name in claimed.values()
    }


def test_a_nested_value_shadows_an_unclaimed_providers_flat_key(
    tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    path = write(
        tmp_path,
        {
            "provider_selection": [UNCLAIMED],
            f"{UNCLAIMED}_binary": "/flat",
            f"{UNCLAIMED}_home": "/flat-home",
            "providers": {UNCLAIMED: {"binary": "/nested"}},
        },
    )
    config = Config.load(path)
    assert config.unknown_providers[UNCLAIMED] == {"binary": "/nested", "home": "/flat-home"}
    assert [record.getMessage() for record in warnings.records][0] == (
        f"Bridge config at {path} sets both flat and nested values for {UNCLAIMED}_binary; "
        "the nested values under 'providers' win and the flat keys are ignored"
    )


@pytest.mark.parametrize(
    "document",
    [
        # The prefix names no provider the file selects: a mistyped key, still refused.
        {f"{UNCLAIMED}_binary": "/x"},
        {"provider_selection": ["kiro"], f"{UNCLAIMED}_binary": "/x"},
        # A nested block alone does not make its provider's flat keys readable.
        {"providers": {UNCLAIMED: {}}, f"{UNCLAIMED}_binary": "/x"},
        # A loaded provider's prefix with a field its schema does not claim.
        {"provider_selection": ["kiro"], "kiro_binray": "/x"},
        # The bare prefix with no field name.
        {"provider_selection": [UNCLAIMED], f"{UNCLAIMED}_": "/x"},
    ],
)
def test_a_flat_key_of_no_selected_unclaimed_provider_is_still_refused(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    (key,) = [key for key in document if key not in {"provider_selection", "providers"}]
    with pytest.raises(ValueError, match="incompatible with this version") as info:
        Config.load(write(tmp_path, document))
    assert repr(key) in str(info.value)


def test_a_kernel_field_is_never_read_as_an_unclaimed_providers_field(tmp_path: Path) -> None:
    config = Config.load(
        write(tmp_path, {"provider_selection": ["heartbeat"], "heartbeat_interval": 7})
    )
    assert config.heartbeat_interval == 7
    assert config.unknown_providers == {}


def test_select_provider_keeps_unknown_names_but_refuses_to_select_one() -> None:
    config = Config(provider_selection=["kiro", "opencode"])
    config.select_provider("hc", True)
    assert config.provider_selection == ["hc", "kiro", "opencode"]
    config.select_provider("kiro", False)
    assert config.provider_selection == ["hc", "opencode"]
    with pytest.raises(ValueError, match="Unknown provider"):
        config.select_provider("opencode", True)
    with pytest.raises(ValueError, match="Unknown provider"):
        config.select_provider("opencode", False)


async def test_provider_status_lists_a_disabled_unknown_provider(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {"provider_selection": ["opencode"], "providers": {"opencode": {"binary": "x"}}},
    )
    bridge = Bridge(Config.load(path), path)
    try:
        assert bridge.provider_status["opencode"] == {
            "state": "disabled",
            "capabilities": [],
            "reason": "no loaded plugin claims this provider; its settings are kept",
        }
    finally:
        await bridge.close()
    plain = Bridge(Config(), path)
    try:
        assert "opencode" not in plain.provider_status
    finally:
        await plain.close()


async def test_provider_status_reports_an_unknown_name_that_spells_a_status_alias_as_disabled(
    tmp_path: Path,
) -> None:
    """`kiro_cli` is the status alias of the `kiro` plugin, not a registration name; a config
    naming it is a disabled unknown provider and status must say so, not show the alias's
    `unconfigured` record for the real plugin."""
    path = write(
        tmp_path,
        {"provider_selection": ["kiro_cli"], "providers": {"kiro_cli": {"binary": "x"}}},
    )
    config = Config.load(path)
    assert config.disabled_providers() == ["kiro_cli"]
    bridge = Bridge(config, path)
    try:
        assert bridge.provider_status["kiro_cli"] == {
            "state": "disabled",
            "capabilities": [],
            "reason": "no loaded plugin claims this provider; its settings are kept",
        }
    finally:
        await bridge.close()


def test_unknown_providers_refuses_a_loaded_providers_name(tmp_path: Path) -> None:
    """A known name in `unknown_providers` would replace that provider's real block on save
    with one its schema cannot load; refuse it at construction, naming the key."""
    with pytest.raises(ValueError, match="unknown_providers names the loaded provider 'kiro'"):
        Config(unknown_providers={"kiro": {"bad": 1}})
    config = Config(kiro_binary="/k", unknown_providers={"opencode": {"bad": 1}})
    config.save(tmp_path / "config.json")
    reloaded = Config.load(tmp_path / "config.json")
    assert reloaded.kiro_binary == "/k"
    assert reloaded.unknown_providers == {"opencode": {"bad": 1}}


def test_provider_selection_still_refuses_non_text_entries() -> None:
    with pytest.raises(ValueError, match="Invalid provider registration"):
        Config(provider_selection=["kiro", 3])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="Invalid provider registration"):
        Config(provider_selection="kiro")  # type: ignore[arg-type]


# --- The compatibility surface the adapters, service and CLI keep using --------------------


def test_flat_attributes_route_to_the_blocks_both_ways() -> None:
    config = Config(hc_enabled=True, kiro_workspaces={"w": "/fixture/project"})
    assert config.providers["hc"].enabled is True
    assert config.kiro_workspaces is config.providers["kiro"].workspaces
    config.hc_binary = "/new/hc"
    assert config.providers["hc"].binary == "/new/hc"
    config.providers["kiro"].binary = "/new/kiro"
    assert config.kiro_binary == "/new/kiro"
    config.desktop_threads["chat"] = "11111111-2222-3333-4444-555555555555"
    assert config.providers["codex"].desktop_threads == {
        "chat": "11111111-2222-3333-4444-555555555555"
    }
    assert set(FLAT_KEYS) == {
        schema.flat_key(entry.name) for schema in SCHEMAS.values() for entry in fields(schema)
    }


def test_replace_with_a_flat_keyword_edits_a_copy_not_the_live_config() -> None:
    live = Config(hc_binary="/live/hc")
    candidate = replace(live, hc_enabled=True)
    candidate.herdr_session = "new"
    candidate.kiro_workspaces["w"] = "/fixture/project"  # the dict is shared, as it always was
    assert (live.hc_enabled, live.herdr_session) == (False, None)
    assert (candidate.hc_enabled, candidate.hc_binary) == (True, "/live/hc")
    assert candidate.providers["hc"] is not live.providers["hc"]
    assert live.kiro_workspaces == {"w": "/fixture/project"}
    # ProviderRegistry._save applies a candidate in place; holders of `live` see it.
    live.__dict__.update(candidate.__dict__)
    assert (live.hc_enabled, live.herdr_session) == (True, "new")


def test_positional_construction_keeps_its_legacy_order() -> None:
    config = Config("sess", "/h", "/c", {"d": "11111111-2222-3333-4444-555555555555"}, "/home")
    assert (config.herdr_session, config.herdr_binary, config.codex_binary) == ("sess", "/h", "/c")
    assert (config.desktop_threads, config.codex_home) == (
        {"d": "11111111-2222-3333-4444-555555555555"},
        "/home",
    )
    with pytest.raises(TypeError, match="positional"):
        Config("a", "b", "c", {}, None, "f")
    with pytest.raises(TypeError, match="multiple values"):
        Config("a", herdr_session="b")
    with pytest.raises(TypeError, match="unexpected keyword argument 'nope'"):
        Config(nope=1)


def test_blocks_may_be_given_as_instances_or_mappings() -> None:
    block = KiroConfig(binary="/k", acp_engine="v2")
    from_instance = Config(providers={"kiro": block})
    from_mapping = Config(providers={"kiro": {"binary": "/k", "acp_engine": "v2"}})
    assert from_instance == from_mapping
    assert from_instance.providers["kiro"] is not block  # a copy, as `replace` gives
    assert Config(providers={"kiro": block}, kiro_binary="/edited").kiro_binary == "/edited"
    with pytest.raises(TypeError, match="providers.kiro must be a KiroConfig"):
        Config(providers={"kiro": SCHEMAS["hc"]()})


def test_every_builtin_plugin_declares_the_block_the_kernel_stores() -> None:
    for plugin in builtin_plugins():
        name = plugin.definition.registration
        assert plugin.config_schema is SCHEMAS[name]
        assert issubclass(plugin.config_schema, ProviderConfig)
        assert isinstance(Config().providers[name], plugin.config_schema)


def test_block_validation_runs_on_construction_and_on_save(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Kirocrew port"):
        Config(providers={"crew": {"port": 0}})
    config = Config()
    config.crew_port = 0
    with pytest.raises(ValueError, match="Kirocrew port"):
        config.save(tmp_path / "config.json")


@pytest.mark.parametrize(
    ("values", "first"),
    [
        # The natives and Rex are checked before the kernel fields and the rest (and an
        # installed plugin's block after Rex: its own tests pin that slot).
        ({"grok_home": "relative", "rex_binary": ""}, "grok home must be an absolute path"),
        ({"codex_cli_home": "relative", "claude_code_binary": ""}, "codex_cli home must be"),
        # The kernel fields come next ...
        ({"kiro_workspaces": {"w": "relative"}, "heartbeat_interval": 0}, "Heartbeat interval"),
        ({"herdr_session": "bad name", "client_registrations": [1]}, "Invalid client"),
        # ... then Herdr, Codex, Kiro, Crew and Core.
        ({"herdr_session": "bad name", "desktop_threads": {"UP": "x"}}, "Herdr session name"),
        ({"desktop_threads": {"UP": "x"}, "kiro_acp_engine": "v9"}, "Chat aliases must be"),
        ({"kiro_acp_engine": "v9", "crew_port": 0}, "Kiro ACP engine must be 'v2' or 'v3'"),
        ({"crew_port": 0, "hc_session": "bad name"}, "Kirocrew port must be between"),
    ],
)
def test_two_faults_are_refused_for_the_one_main_named_first(
    values: dict[str, Any], first: str
) -> None:
    """The blocks are validated in the order the monolithic `__post_init__` checked them on
    `main`, so an operator with a twice-wrong file reads the same first refusal for these
    pairs (every expected message here was produced on `main` from the same values). Not
    every pair: the checks 0.15 ran out of block order (active budgets, Herdr's
    binary-for-session, Codex's binary-when-enabled, enablement-flag types) now run inside
    their block, so a file faulty in one of those and elsewhere may be refused for the other
    fault first. Single-fault files read the same text in every case."""
    with pytest.raises(ValueError, match=first):
        Config(**values)


def test_original_redacted_workmac_gets_the_refusal_main_gave(tmp_path: Path) -> None:
    """The `<redacted>` Kiro paths of the redaction are the fault `main` named for the
    kernel's fields (the plugin's own tests pin the fault its block adds before it)."""
    path = live_config(tmp_path, ORIGINAL_WORKMAC)
    with pytest.raises(ValueError) as info:
        Config.load(path)
    assert str(info.value) == (
        f"Bridge config at {path} is incompatible with this version of Answerback: "
        "Kiro working directories must be absolute paths"
    )


# --- The six live configs ------------------------------------------------------------------


@pytest.mark.parametrize("name", LIVE_CONFIGS)
def test_live_config_reads_as_main_read_it(
    name: str, tmp_path: Path, warnings: pytest.LogCaptureFixture
) -> None:
    expected = json.loads((FIXTURES / "expected" / name).read_text())
    config = Config.load(live_config(tmp_path, name))
    assert warnings.records == []
    for field_name, value in expected["fields"].items():
        assert getattr(config, field_name) == value, field_name
    assert config.registered_providers() == expected["registered_providers"]
    assert routing_digest(config) == LIVE_DIGESTS[name]
    assert config.disabled_providers() == []
    assert config.disabled_clients() == []


@pytest.mark.parametrize("name", LIVE_CONFIGS)
def test_live_config_round_trips_through_the_nested_shape(name: str, tmp_path: Path) -> None:
    config = Config.load(live_config(tmp_path, name))
    path = tmp_path / "answerback-bridge" / "config.json"
    config.save(path)
    saved = json.loads(path.read_text())
    assert flat_keys_in(saved) == set()
    assert set(saved) == KERNEL | {"providers"}
    reloaded = Config.load(path)
    assert reloaded == config
    assert routing_digest(reloaded) == routing_digest(config)
    # The second save is a fixed point.
    reloaded.save(path)
    assert json.loads(path.read_text()) == saved


def test_live_config_fixtures_cover_every_known_host() -> None:
    assert LIVE_CONFIGS == [
        "lan-linux-a.json",
        "lan-linux-b.json",
        "lan-linux-c.json",
        "lan-mac-a.json",
        "lan-mac-b.json",
        "work-mac.json",
    ]
    assert sorted(path.name for path in (FIXTURES / "expected").glob("*.json")) == LIVE_CONFIGS
    assert sorted(LIVE_DIGESTS) == LIVE_CONFIGS
