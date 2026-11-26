"""Frozen 0.9 routing identities survive the addition of unused providers."""

import json
import sys
import tempfile
from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import Mock

import pytest
from composition import pick

from answerback_bridge.adapters import catalog
from answerback_bridge.bus.api import BusAPI
from answerback_bridge.bus.config import BusConfig, routing_digest
from answerback_bridge.bus.store import BusStore
from answerback_bridge.config import FLAT_KEYS, Config
from answerback_bridge.provider_config import ProviderConfig

# Computed by the 0.9.0 interpreter (`src/codex_herdr_bridge` at fb3df7a8, the 0.9.0 release
# commit) for this fixture; the internal composition, whose plugin hashes its block at the
# defaults 0.9.0 had, still produces them. The public composition composes no such block,
# so it hashes without those fields: its digests, computed at the T-269 tip, are pinned
# beside. These are published configuration digests, not credentials.
LEGACY_DIGEST = pick(
    "0bbbdc3b8d4efb14eae9ef8af0fecbd0346d14680402411594e29cd779325f48",  # pragma: allowlist secret
    "711f4b033c799265817ed3d8e62273caaed523133494921fff57b72acc2ecb94",  # pragma: allowlist secret
)
LEGACY_SELECTION_DIGEST = pick(
    "85ef03ac4067396bd24345061b0c667a3cd2ea63aa502806cbc6575becd0ae80",  # pragma: allowlist secret
    "e9115686c49ae5cdf8d094ce38b95afece18dd687d9b2bdc9a9b7530113b5aa2",  # pragma: allowlist secret
)
EMPTY_SELECTION_DIGEST = pick(
    "3f6e78d3f7c75c6c6393a24261d4157b16fb29f733ac2c5d10a735294c1d5400",  # pragma: allowlist secret
    "1aa685248fae0ce9d40f6f74dfc0b6bc7e6f7df4a6eaf0a1799993e7021c48e3",  # pragma: allowlist secret
)
LEGACY_VALUES = {
    "herdr_session": "fixture",
    "herdr_binary": "/fixture/bin/herdr",
    "codex_binary": "/fixture/bin/codex",
    "desktop_threads": {"review": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
    "codex_home": "/fixture/codex",
    "kiro_binary": "/fixture/bin/kiro",
    "kiro_workspaces": {"worker": "/fixture/project"},
    "crew_binary": "/fixture/bin/crew",
    "crew_enabled": False,
    "crew_port": 9876,
    "hc_enabled": False,
    "hc_binary": "/fixture/bin/hc",
    "hc_session": "fixture",
    "codex_enabled": True,
    "provider_selection": ["codex", "kiro"],
    "client_registrations": ["codex"],
    "heartbeat_interval": 10,
    "heartbeat_misses": 3,
}


def legacy_document(**changes: Any) -> Config:
    """The 0.9 flat document read by the loader; nested values are fresh for every call."""
    with tempfile.TemporaryDirectory(prefix="ab-legacy-", dir="/tmp") as folder:
        root = Path(folder)
        root.chmod(0o700)
        path = root / "config.json"
        path.write_text(json.dumps({**LEGACY_VALUES, **changes}))
        path.chmod(0o600)
        return Config.load(path)


@pytest.fixture
def legacy_config() -> Config:
    return legacy_document()


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        (["codex", "kiro"], LEGACY_DIGEST),
        (None, LEGACY_SELECTION_DIGEST),
        ([], EMPTY_SELECTION_DIGEST),
    ],
)
def test_unused_rex_preserves_frozen_09_digest(
    legacy_config: Config, selection: list[str] | None, expected: str
) -> None:
    assert routing_digest(replace(legacy_config, provider_selection=selection)) == expected


@pytest.mark.parametrize("selection", [None, [], ["codex", "kiro"]])
def test_unrelated_config_save_preserves_legacy_document(
    tmp_path: Path, selection: list[str] | None
) -> None:
    """An unrelated save of a 0.9 (flat) document keeps every value and its digest; since
    0.16 (T-198) it writes them under `providers.<name>` and no flat key (CHANGELOG)."""
    original = {**LEGACY_VALUES, "provider_selection": selection}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(original))
    config = Config.load(path)
    before = routing_digest(config)
    config.heartbeat_interval = 11
    config.save(path)
    written = json.loads(path.read_text())
    assert set(written) == {
        "provider_selection",
        "client_registrations",
        "heartbeat_interval",
        "heartbeat_misses",
        "providers",
    }
    assert written["heartbeat_interval"] == 11
    for key, value in original.items():
        if key in FLAT_KEYS:
            provider, name = FLAT_KEYS[key]
            assert written["providers"][provider][name] == value, key
        elif key != "heartbeat_interval":
            assert written[key] == value, key
    assert routing_digest(Config.load(path)) == before


@pytest.mark.parametrize("explicit_selection", [False, True])
def test_config_save_retains_intentional_new_provider_settings(
    tmp_path: Path, explicit_selection: bool
) -> None:
    config = Config(
        codex_enabled=False,
        provider_selection=["rex"] if explicit_selection else [],
        rex_binary="rex" if explicit_selection else "/custom/rex",
    )
    path = tmp_path / "config.json"
    config.save(path)
    persisted = json.loads(path.read_text())["providers"]["rex"]
    assert persisted["binary"] == config.rex_binary
    assert persisted["enabled"] is config.rex_enabled
    assert "server" in persisted and "host_id" in persisted
    assert Config.load(path) == config


def persisted_legacy_command(
    tmp_path: Path, routing_identity: str = LEGACY_DIGEST
) -> tuple[BusAPI, BusStore, dict]:
    path = tmp_path / "config.json"
    # Load an actual pre-Rex config file; do not serialize candidate defaults into it.
    path.write_text(json.dumps(LEGACY_VALUES))
    binary = tmp_path / "unused-server"
    binary.symlink_to(Path(sys.executable).resolve())
    bus = BusConfig.create(path, server_binary=str(binary))
    store = BusStore(bus.root / "ledger.sqlite")
    command = store.submit(
        peer_id="kiro:worker",
        project="/fixture/project",
        message="Review résumé e\u0301\n \t終",
        request_id="before-upgrade",
        routing_digest=routing_identity,
    )
    return BusAPI(path), store, command


def test_legacy_command_fixture_never_resolves_a_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compatibility fixture must not depend on the owner's real mise install."""
    from answerback_bridge.bus import config as bus_config

    def refuse_mise_lookup(command: str) -> str | None:
        if command == "mise":
            raise AssertionError("A config-only fixture must not look up mise")
        return None

    monkeypatch.setattr(bus_config.shutil, "which", refuse_mise_lookup)

    _, _, command = persisted_legacy_command(tmp_path)

    assert command["request_id"] == "before-upgrade"


def test_exact_retry_keeps_stored_command_and_outbox_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, store, original = persisted_legacy_command(tmp_path)
    native = Mock(side_effect=AssertionError("An offline retry must not dispatch native work"))
    monkeypatch.setattr("subprocess.Popen", native)

    with store.connection() as db:
        before = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("commands", "outbox", "events")
        }
    assert (
        api.submit(
            "kiro:worker", "/fixture/project", "Review résumé e\u0301\n \t終", "before-upgrade"
        )
        == original
    )
    with store.connection() as db:
        after = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("commands", "outbox", "events")
        }
    assert after == before
    assert len(after["commands"]) == 1
    assert len(after["outbox"]) == 2
    assert store.command(original["command_id"]).routing_digest == LEGACY_DIGEST
    native.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"rex_enabled": True},
        {"rex_binary": "/different/rex"},
        {"rex_server": "unix:///tmp/configured.sock"},
        {"rex_host_id": "configured-host"},
        {"provider_selection": ["codex", "kiro", "rex"]},
    ],
)
def test_new_provider_selection_or_configuration_rejects_old_retry(
    tmp_path: Path, changes: dict
) -> None:
    api, store, original = persisted_legacy_command(tmp_path)
    config = replace(Config.load(api.config_path), **changes)
    config.save(api.config_path)
    assert routing_digest(config) != LEGACY_DIGEST
    with pytest.raises(ValueError, match="different work"):
        api.submit(
            "kiro:worker", "/fixture/project", "Review résumé e\u0301\n \t終", "before-upgrade"
        )
    assert store.counts() == {"queued": 1}
    assert store.command(original["command_id"]).routing_digest == LEGACY_DIGEST


def test_metadata_only_changes_preserve_legacy_digest(legacy_config: Config) -> None:
    legacy_config.heartbeat_interval = 30
    legacy_config.heartbeat_misses = 5
    legacy_config.client_registrations = ["codex", "claude"]
    assert routing_digest(legacy_config) == LEGACY_DIGEST


@pytest.mark.parametrize(
    "changes",
    [
        {"hc_enabled": True},
        {"hc_binary": "/different/hc"},
        {"hc_session": "different"},
        {"herdr_binary": "/different/herdr"},
        {"herdr_session": "different"},
        {"codex_enabled": False},
        {"codex_binary": "/different/codex"},
        {"codex_home": "/different/codex-home"},
        {"desktop_threads": {"review": "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"}},
        {"kiro_binary": "/different/kiro"},
        {"kiro_workspaces": {"worker": "/different/project"}},
        {"crew_enabled": True},
        {"crew_binary": "/different/crew"},
        {"crew_port": 8765},
    ],
)
def test_old_routing_fields_still_participate_when_provider_is_unselected(
    legacy_config: Config, changes: dict
) -> None:
    config = replace(legacy_config, provider_selection=[])
    assert routing_digest(replace(config, **changes)) != routing_digest(config)


def test_rex_rebinding_changes_selected_routing_digest() -> None:
    config = Config(
        provider_selection=["rex"], rex_server="unix:///tmp/rex.sock", rex_host_id="first"
    )
    assert routing_digest(replace(config, rex_host_id="second")) != routing_digest(config)
    assert routing_digest(replace(config, rex_server="unix:///tmp/second.sock")) != routing_digest(
        config
    )


def test_arbitrary_stored_digest_does_not_gain_compatibility(tmp_path: Path) -> None:
    api, store, original = persisted_legacy_command(tmp_path, "0" * 64)
    with pytest.raises(ValueError, match="different work"):
        api.submit(
            "kiro:worker", "/fixture/project", "Review résumé e\u0301\n \t終", "before-upgrade"
        )
    assert store.counts() == {"queued": 1}
    assert store.command(original["command_id"]).routing_digest == "0" * 64


def _install_fixture_provider(
    monkeypatch: pytest.MonkeyPatch, schema: type[ProviderConfig], entry: catalog.ProviderDefinition
) -> None:
    """Load a provider the way step 8 will: its schema joins the kernel's table, its flat
    names the loader's, its catalog row the registry's."""
    from answerback_bridge import config as configuration

    monkeypatch.setattr(catalog, "DEFINITIONS", (*catalog.DEFINITIONS, entry))
    monkeypatch.setattr(configuration, "PROVIDERS", (*configuration.PROVIDERS, entry.registration))
    monkeypatch.setattr(
        configuration, "SCHEMAS", {**configuration.SCHEMAS, schema.provider: schema}
    )
    monkeypatch.setattr(
        configuration,
        "FLAT_KEYS",
        {
            **configuration.FLAT_KEYS,
            **{flat: (schema.provider, name) for flat, name in schema.flat_keys().items()},
        },
    )


def test_declared_neutral_defaults_apply_to_any_new_provider(
    legacy_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    @dataclass
    class FixtureConfig(ProviderConfig):
        provider: ClassVar[str] = "fixture"
        version: object = False
        endpoint: str = "fixture"

    assert routing_digest(legacy_config) == LEGACY_DIGEST
    entry = catalog.ProviderDefinition(
        "fixture",
        "fixture",
        routing_fields=("fixture.version", "fixture.endpoint"),
        legacy_neutral_defaults=(("fixture.version", False), ("fixture.endpoint", "fixture")),
    )
    _install_fixture_provider(monkeypatch, FixtureConfig, entry)
    config = legacy_document()
    assert isinstance(config.providers["fixture"], FixtureConfig)
    assert routing_digest(config) == LEGACY_DIGEST
    assert routing_digest(replace(config, fixture_endpoint="configured")) != LEGACY_DIGEST
    # bool and int compare equal in Python, but are distinct routing declarations.
    assert routing_digest(replace(config, fixture_version=0)) != LEGACY_DIGEST
    selected = replace(config, provider_selection=[*config.registered_providers(), "fixture"])
    included = routing_digest(selected)
    monkeypatch.setattr(catalog, "DEFINITIONS", catalog.DEFINITIONS[:-1])
    assert routing_digest(selected) != included


def test_incomplete_neutral_declaration_keeps_entire_provider_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class FixtureConfig(ProviderConfig):
        provider: ClassVar[str] = "fixture"
        endpoint: str = "fixture"
        version: int = 1

    before = routing_digest(Config(provider_selection=[]))
    entry = catalog.ProviderDefinition(
        "fixture",
        "fixture",
        routing_fields=("fixture.version", "fixture.endpoint"),
        legacy_neutral_defaults=(("fixture.endpoint", "fixture"),),
    )
    _install_fixture_provider(monkeypatch, FixtureConfig, entry)
    assert routing_digest(Config(provider_selection=[])) != before


def test_compatibility_import_uses_same_digest(legacy_config: Config) -> None:
    old_import = import_module("codex_herdr_bridge.bus.config").routing_digest

    assert old_import(legacy_config) == routing_digest(legacy_config) == LEGACY_DIGEST
