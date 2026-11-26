"""Entry-point discovery is the one composition path for built-ins and extensions."""

from __future__ import annotations

import gc
import importlib.metadata
import json
import logging
import os
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import answerback_bridge.config as configuration
from answerback_bridge.adapters.catalog import ProviderDefinition
from answerback_bridge.config import Config
from answerback_bridge.plugin import (
    PLUGIN_API,
    PluginLoadError,
    PluginVersionMismatch,
    ProviderPlugin,
    SurfaceCollision,
    load_plugins,
)
from answerback_bridge.provider_config import ProviderConfig

PROVIDER_GROUP = "answerback.providers"
MODULE_GROUP = "answerback.modules"


@dataclass
class FixtureConfig(ProviderConfig):
    provider: ClassVar[str] = "fixture"
    endpoint: str = "local"
    binary: str = ""


class FixturePlugin(ProviderPlugin):
    capabilities = frozenset({"read"})

    def __init__(self, name: str = "fixture", version: Any = PLUGIN_API) -> None:
        self.api_version = version
        self.definition = ProviderDefinition(
            name, name, registration_binary_field=None if name != "fixture" else "binary"
        )
        # The block is the `fixture` provider's; a fixture under another name has none
        # (two providers sharing one block's flat keys would be refused as a collision).
        self.config_schema = FixtureConfig if name == "fixture" else None

    def build(self, ctx: Any) -> Any:
        raise AssertionError("loading must not build an adapter")


class FakeEntryPoint:
    group = PROVIDER_GROUP

    def __init__(
        self,
        name: str,
        value: str,
        factory: Any,
        *,
        distribution: str = "third-party",
    ) -> None:
        self.name = name
        self.value = value
        self._factory = factory
        self.dist = SimpleNamespace(metadata={"Name": distribution})

    def load(self) -> Any:
        return self._factory


def _entry_points(monkeypatch: pytest.MonkeyPatch, *entries: FakeEntryPoint) -> None:
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda **kwargs: [
            entry for entry in entries if entry.group == kwargs.get("group", entry.group)
        ],
    )


@pytest.fixture
def installed_answerback_entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    """The editable install exposes the same standard metadata a wheel exposes."""
    distribution = importlib.metadata.distribution("answerback")
    return tuple(distribution.entry_points)


def test_editable_metadata_declares_every_builtin_and_the_bus_module(
    installed_answerback_entry_points: tuple[importlib.metadata.EntryPoint, ...],
) -> None:
    providers = {
        entry.name: entry.value
        for entry in installed_answerback_entry_points
        if entry.group == PROVIDER_GROUP
    }
    assert providers == {
        "claude_code": "answerback_bridge.providers.native:claude_code_plugin",
        "codex": "answerback_bridge.providers.codex:codex_plugin",
        "codex_cli": "answerback_bridge.providers.native:codex_cli_plugin",
        "crew": "answerback_bridge.providers.crew:crew_plugin",
        "grok": "answerback_bridge.providers.native:grok_plugin",
        "hc": "answerback_bridge.providers.herdr:hc_plugin",
        "herdr": "answerback_bridge.providers.herdr:herdr_plugin",
        "kiro": "answerback_bridge.providers.kiro:kiro_plugin",
        "rex": "answerback_bridge.providers.rex:rex_plugin",
    }
    modules = {
        entry.name: entry.value
        for entry in installed_answerback_entry_points
        if entry.group == MODULE_GROUP
    }
    assert modules == {"bus": "answerback_bridge.bus.plugin:bus_plugin"}
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["entry-points"][MODULE_GROUP] == modules


def test_missing_provider_entry_point_metadata_requests_resync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_kwargs: [])

    with pytest.raises(
        PluginLoadError,
        match=r"installed metadata has no answerback\.providers entry points.*uv sync.*reinstall",
    ):
        configuration._load_plugin_config_tables()


def test_third_party_only_provider_metadata_requests_resync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint("fixture", "fixture_plugin:factory", FixturePlugin),
    )

    with pytest.raises(
        PluginLoadError,
        match=r"installed metadata has no answerback\.providers entry points.*uv sync.*reinstall",
    ):
        configuration._load_plugin_config_tables()


def test_plugins_return_in_registration_name_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint("z-entry", "z_plugin:factory", lambda: FixturePlugin("zulu")),
        FakeEntryPoint("a-entry", "a_plugin:factory", lambda: FixturePlugin("alpha")),
    )

    assert [plugin.definition.registration for plugin in load_plugins()] == ["alpha", "zulu"]


def test_loaded_provider_without_config_schema_is_not_reported_as_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(configuration, "PROVIDERS", (*configuration.PROVIDERS, "fixture"))
    config = Config(provider_selection=["fixture"])

    assert config.disabled_providers() == []
    config.select_provider("fixture", False)
    assert config.provider_selection == []


def test_api_version_mismatch_is_skipped_with_a_named_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint("old", "old_plugin:factory", lambda: FixturePlugin("old", 0)),
        FakeEntryPoint("fixture", "fixture_plugin:factory", FixturePlugin),
    )

    assert [plugin.definition.registration for plugin in load_plugins()] == ["fixture"]
    assert "old" in caplog.text
    assert "api_version 0" in caplog.text


@pytest.mark.parametrize("version", [True, 1.0])
def test_run6_a_third_party_version_equal_to_one_but_not_int_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, version: Any
) -> None:
    """red_at_base (run-6 hardening h1): True == 1 and 1.0 == 1 passed the version check."""
    _entry_points(
        monkeypatch,
        FakeEntryPoint("odd", "odd_plugin:factory", lambda: FixturePlugin("odd", version)),
        FakeEntryPoint("fixture", "fixture_plugin:factory", FixturePlugin),
    )

    assert [plugin.definition.registration for plugin in load_plugins()] == ["fixture"]
    assert f"api_version {version!r}" in caplog.text


class _RaisingVersion(FixturePlugin):
    def __init__(self) -> None:
        super().__init__("raising")

    @property
    def api_version(self) -> int:  # type: ignore[override]
        raise RuntimeError("version property exploded")

    @api_version.setter
    def api_version(self, value: Any) -> None:
        pass


class _RaisingDefinition(FixturePlugin):
    def __init__(self) -> None:
        super().__init__("raising")

    @property
    def definition(self) -> Any:  # type: ignore[override]
        raise LookupError("definition property exploded")

    @definition.setter
    def definition(self, value: Any) -> None:
        pass


class _BrokenFlatKeysConfig(ProviderConfig):
    provider: ClassVar[str] = "nodata"

    @classmethod
    def flat_keys(cls) -> dict[str, str]:
        return None  # type: ignore[return-value]


class _BrokenSchema(FixturePlugin):
    def __init__(self) -> None:
        super().__init__("nodata")
        self.config_schema = _BrokenFlatKeysConfig


@pytest.mark.parametrize(
    ("factory", "text"),
    [
        (_RaisingVersion, "version property exploded"),
        (_RaisingDefinition, "definition property exploded"),
        (_BrokenSchema, "AttributeError"),
    ],
    ids=["api_version", "definition", "schema"],
)
def test_run6_a_third_party_that_raises_while_validated_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    factory: Any,
    text: str,
) -> None:
    """red_at_base (run-6 hardening h1): such an exception stopped startup."""
    _entry_points(
        monkeypatch,
        FakeEntryPoint("bad", "bad_plugin:factory", factory, distribution="bad-dist"),
        FakeEntryPoint("fixture", "fixture_plugin:factory", FixturePlugin),
    )

    assert [plugin.definition.registration for plugin in load_plugins()] == ["fixture"]
    assert "bad_plugin:factory" in caplog.text and text in caplog.text


def test_run6_a_builtin_that_raises_while_validated_still_stops_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pins_existing: only third parties are skipped."""
    _entry_points(
        monkeypatch,
        FakeEntryPoint(
            "bad", "answerback_bridge.x:bad", _RaisingVersion, distribution="answerback"
        ),
    )
    with pytest.raises(RuntimeError, match="version property exploded"):
        load_plugins()


def test_builtin_api_version_mismatch_is_a_programming_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint(
            "fixture",
            "answerback_bridge.adapters.builtins:fixture",
            lambda: FixturePlugin(version=0),
            distribution="answerback",
        ),
    )

    with pytest.raises(PluginVersionMismatch, match=r"fixture.*api_version 0"):
        load_plugins()


def test_two_entry_points_claiming_one_registration_name_fail_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint("first", "first_plugin:factory", FixturePlugin, distribution="first-dist"),
        FakeEntryPoint(
            "second", "second_plugin:factory", FixturePlugin, distribution="second-dist"
        ),
    )

    with pytest.raises(SurfaceCollision, match=r"fixture.*first.*second"):
        load_plugins()


def test_third_party_factory_failure_warns_and_other_plugins_still_load(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="answerback_bridge.plugin")

    def explode() -> ProviderPlugin:
        raise RuntimeError("factory exploded")

    _entry_points(
        monkeypatch,
        FakeEntryPoint("broken", "broken_plugin:factory", explode, distribution="broken-dist"),
        FakeEntryPoint("fixture", "fixture_plugin:factory", FixturePlugin),
    )

    assert [plugin.definition.registration for plugin in load_plugins()] == ["fixture"]
    warning_records = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warning_records) == 1
    assert "broken" in warning_records[0].getMessage()
    assert "broken_plugin:factory" in warning_records[0].getMessage()
    assert "factory exploded" in warning_records[0].getMessage()
    debug_records = [record for record in caplog.records if record.levelno == logging.DEBUG]
    assert len(debug_records) == 1
    assert debug_records[0].exc_info is not None


def test_builtin_factory_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> ProviderPlugin:
        raise RuntimeError("factory exploded")

    _entry_points(
        monkeypatch,
        FakeEntryPoint(
            "broken",
            "answerback_bridge.adapters.builtins:broken",
            explode,
            distribution="answerback",
        ),
    )

    with pytest.raises(PluginLoadError, match=r"broken.*builtins:broken"):
        load_plugins()


def _write_config_schema_distribution(
    root: Path,
    *,
    registration: str,
    schema_provider: str,
    flat_key: str | None = None,
    flat_aliases: Mapping[str, str] | None = None,
    distribution: str = "answerback-bad-config-test",
) -> None:
    """A distribution whose block has a ``value`` field (aliased to ``flat_key``) and one
    more ``str`` field per extra name in ``flat_aliases``, in that order."""
    if flat_aliases is None:
        flat_aliases = {"value": flat_key} if flat_key is not None else {}
    block_fields = "\n".join(
        f"    {name}: str = ''" for name in dict.fromkeys(("value", *flat_aliases))
    )
    module = distribution.replace("-", "_")
    (root / f"{module}.py").write_text(
        f"""
from dataclasses import dataclass
from typing import Any, ClassVar

from answerback_bridge.adapters.catalog import ProviderDefinition
from answerback_bridge.plugin import PLUGIN_API, ProviderPlugin
from answerback_bridge.provider_config import ProviderConfig


@dataclass
class ConfigBlock(ProviderConfig):
    provider: ClassVar[str] = {schema_provider!r}
    flat_aliases: ClassVar[dict[str, str]] = {dict(flat_aliases)!r}
{block_fields}


class Plugin(ProviderPlugin):
    api_version = PLUGIN_API
    definition = ProviderDefinition(
        {registration!r}, {registration!r}, registration_binary_field=None
    )
    capabilities = frozenset()
    config_schema = ConfigBlock

    def build(self, ctx: Any) -> Any:
        raise AssertionError("the probe builds no adapter")


def make_plugin() -> Plugin:
    return Plugin()
""".lstrip()
    )
    metadata = root / f"{module}-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.0\n"
    )
    (metadata / "entry_points.txt").write_text(
        f"[answerback.providers]\n{registration} = {module}:make_plugin\n"
    )


# Imports `Config` with the temporary distributions on the path, constructs one, and prints
# the loaded providers and what the probes of a shadowed and a mistyped name do.
_CONFIG_PROBE = """
import json
import logging
import sys

logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(message)s")
from answerback_bridge.config import PROVIDERS, SCHEMAS, Config

config = Config()
probes = {}
for name in sys.argv[1:]:
    try:
        probes[name] = repr(getattr(config, name))
    except AttributeError as error:
        probes[name] = f"AttributeError: {error}"
print(json.dumps({"providers": list(PROVIDERS), "schemas": sorted(SCHEMAS), "probes": probes}))
"""


def _import_config_with_distribution(root: Path, *probes: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(root), env.get("PYTHONPATH", "")) if item
    )
    return subprocess.run(
        [sys.executable, "-c", _CONFIG_PROBE, *probes],
        capture_output=True,
        text=True,
        env=env,
    )


def _refusal(result: subprocess.CompletedProcess[str], registration: str) -> str:
    lines = [
        line
        for line in result.stderr.splitlines()
        if line.startswith(
            f"Skipping third-party plugin entry point {PROVIDER_GROUP}:{registration} ("
        )
    ]
    # Every composition pass (config tables, catalog, adapters) logs the same refusal.
    assert len(set(lines)) == 1, result.stderr
    return lines[0]


def test_flat_key_shadowing_a_kernel_config_name_refuses_the_plugin(tmp_path: Path) -> None:
    """A flat alias equal to a kernel field would be read by `__getattr__` before the blocks
    exist (`Config()` died with `'Config' object has no attribute 'providers'`); the plugin is
    refused with the alias and the plugin named, every other provider loads, `Config()`
    constructs, the kernel field is its own, and a typo still raises `AttributeError`."""
    _write_config_schema_distribution(
        tmp_path, registration="shadow", schema_provider="shadow", flat_key="heartbeat_interval"
    )

    result = _import_config_with_distribution(tmp_path, "heartbeat_interval", "kiro_binry")

    assert result.returncode == 0, result.stderr
    refusal = _refusal(result, "shadow")
    assert "shadow_plugin" not in refusal  # the entry point label names the module and dist
    assert "answerback-bad-config-test" in refusal
    assert (
        "flat key 'heartbeat_interval' (field 'value') shadows Config.heartbeat_interval" in refusal
    )
    loaded = json.loads(result.stdout)
    assert "shadow" not in loaded["providers"] and "shadow" not in loaded["schemas"]
    assert set(configuration.PROVIDERS) <= set(loaded["providers"])
    assert loaded["probes"]["heartbeat_interval"] == repr(Config().heartbeat_interval)
    assert loaded["probes"]["kiro_binry"] == (
        "AttributeError: 'Config' object has no attribute 'kiro_binry'"
    )


@pytest.mark.parametrize(
    ("flat_key", "shadowed"),
    [
        ("save", "a method"),
        ("provider_enabled", "a method"),
        ("providers", "a field set in __init__ (absent from dir)"),
        ("kiro_binary", "a kernel schema's flat descriptor"),
        ("__class__", "a dunder, which __getattr__ never routes"),
    ],
)
def test_every_kind_of_reserved_config_name_refuses_the_plugin(
    tmp_path: Path, flat_key: str, shadowed: str
) -> None:
    _write_config_schema_distribution(
        tmp_path, registration="collide", schema_provider="collide", flat_key=flat_key
    )

    result = _import_config_with_distribution(tmp_path)

    assert result.returncode == 0, result.stderr
    refusal = _refusal(result, "collide")
    assert f"flat key {flat_key!r} (field 'value') shadows Config.{flat_key}" in refusal, shadowed
    if flat_key == "kiro_binary":
        assert "the kiro block's 'binary'" in refusal
    assert "collide" not in json.loads(result.stdout)["providers"]


def test_reserved_config_names_come_from_the_class() -> None:
    reserved = configuration.reserved_flat_names()
    assert {"save", "provider_enabled", "heartbeat_interval", "providers", "__getattr__"} <= set(
        reserved
    )
    assert reserved["kiro_binary"] == ("kiro", "binary")
    assert reserved["save"] is None
    # The retired flat keys are read by the loader but are not attributes (base pin).
    assert not set(configuration.RETIRED_FLAT_KEYS) & set(reserved)


def test_second_plugin_reusing_a_flat_key_is_refused_and_the_first_kept(tmp_path: Path) -> None:
    """Load order is distribution name, then entry name: `answerback-first-test` claims
    `shared_value`, `answerback-second-test` is refused naming the owner."""
    _write_config_schema_distribution(
        tmp_path,
        registration="first",
        schema_provider="first",
        flat_key="shared_value",
        distribution="answerback-first-test",
    )
    _write_config_schema_distribution(
        tmp_path,
        registration="second",
        schema_provider="second",
        flat_key="shared_value",
        distribution="answerback-second-test",
    )

    result = _import_config_with_distribution(tmp_path, "shared_value")

    assert result.returncode == 0, result.stderr
    refusal = _refusal(result, "second")
    assert "flat key 'shared_value' (field 'value') is already claimed by provider first" in refusal
    assert "Skipping third-party plugin entry point answerback.providers:first" not in result.stderr
    loaded = json.loads(result.stdout)
    assert "first" in loaded["providers"] and "second" not in loaded["providers"]
    assert loaded["probes"]["shared_value"] == "''"


def test_refused_plugin_claims_no_flat_key(tmp_path: Path) -> None:
    """`answerback-rejected-test` passes `shared_key` and is refused on `heartbeat_interval`;
    the refusal is atomic, so `answerback-valid-test`, loaded later, still claims `shared_key`
    and `Config()` routes the key to its block. The refused plugin is reported once per
    composition pass, with the key that refused it, and is never named as an owner."""
    _write_config_schema_distribution(
        tmp_path,
        registration="rejected",
        schema_provider="rejected",
        flat_aliases={"value": "shared_key", "extra": "heartbeat_interval"},
        distribution="answerback-rejected-test",
    )
    _write_config_schema_distribution(
        tmp_path,
        registration="valid",
        schema_provider="valid",
        flat_key="shared_key",
        distribution="answerback-valid-test",
    )

    result = _import_config_with_distribution(tmp_path, "shared_key", "heartbeat_interval")

    assert result.returncode == 0, result.stderr
    refusal = _refusal(result, "rejected")
    assert (
        "flat key 'heartbeat_interval' (field 'extra') shadows Config.heartbeat_interval" in refusal
    )
    assert "already claimed by provider rejected" not in result.stderr
    assert "Skipping third-party plugin entry point answerback.providers:valid" not in result.stderr
    loaded = json.loads(result.stdout)
    assert "valid" in loaded["providers"] and "rejected" not in loaded["providers"]
    assert loaded["probes"]["shared_key"] == "''"
    assert loaded["probes"]["heartbeat_interval"] == repr(Config().heartbeat_interval)


def test_refused_plugin_owns_no_registration_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A later entry point declaring the registration a refused plugin declared is not a
    collision with it: the refused plugin owns nothing."""

    @dataclass
    class ShadowConfig(ProviderConfig):
        provider: ClassVar[str] = "fixture"
        flat_aliases: ClassVar[dict[str, str]] = {"endpoint": "heartbeat_interval"}
        endpoint: str = "local"

    refused = FixturePlugin()
    refused.config_schema = ShadowConfig
    kept = FixturePlugin()
    _entry_points(
        monkeypatch,
        FakeEntryPoint("fixture", "a_plugin:factory", lambda: refused, distribution="a-third"),
        FakeEntryPoint("fixture", "b_plugin:factory", lambda: kept, distribution="b-third"),
    )

    assert load_plugins() == [kept]


def test_schema_aliasing_one_flat_key_twice_is_refused(tmp_path: Path) -> None:
    """Two fields of one block aliased to one flat key: `flat_keys()` would keep only the
    later field and the earlier would vanish from flat-file routing, so the schema is refused
    on the same path as a collision with another provider."""
    _write_config_schema_distribution(
        tmp_path,
        registration="twice",
        schema_provider="twice",
        flat_aliases={"value": "shared_key", "extra": "shared_key"},
    )

    result = _import_config_with_distribution(tmp_path, "shared_key")

    assert result.returncode == 0, result.stderr
    refusal = _refusal(result, "twice")
    assert (
        "Provider twice config fields 'value' and 'extra' both declare flat key 'shared_key'"
        in refusal
    )
    loaded = json.loads(result.stdout)
    assert "twice" not in loaded["providers"] and "twice" not in loaded["schemas"]
    assert loaded["probes"]["shared_key"] == (
        "AttributeError: 'Config' object has no attribute 'shared_key'"
    )


def test_flat_keys_refuse_a_duplicate_within_the_block() -> None:
    @dataclass
    class Twice(ProviderConfig):
        provider: ClassVar[str] = "twice"
        flat_aliases: ClassVar[dict[str, str]] = {"second": "twice_first"}
        first: str = ""
        second: str = ""

    with pytest.raises(
        ValueError, match="'first' and 'second' both declare flat key 'twice_first'"
    ):
        Twice.flat_keys()
    assert FixtureConfig.flat_keys() == {"fixture_endpoint": "endpoint", "fixture_binary": "binary"}


def test_builtin_flat_key_collision_is_a_programming_error(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class ShadowConfig(ProviderConfig):
        provider: ClassVar[str] = "fixture"
        flat_aliases: ClassVar[dict[str, str]] = {"endpoint": "heartbeat_interval"}
        endpoint: str = "local"

    plugin = FixturePlugin()
    plugin.config_schema = ShadowConfig
    _entry_points(
        monkeypatch,
        FakeEntryPoint(
            "fixture",
            "answerback_bridge.adapters.builtins:fixture",
            lambda: plugin,
            distribution="answerback",
        ),
    )

    with pytest.raises(PluginLoadError, match=r"fixture.*'heartbeat_interval'.*shadows Config"):
        load_plugins()


def test_third_party_schema_provider_mismatch_is_fail_closed(tmp_path: Path) -> None:
    _write_config_schema_distribution(
        tmp_path,
        registration="badprov",
        schema_provider="other",
    )

    result = _import_config_with_distribution(tmp_path)

    assert result.returncode != 0
    assert "Plugin badprov config_schema declares provider 'other'" in result.stderr


def _write_third_party_distribution(root: Path) -> None:
    (root / "third_party_plugin.py").write_text(
        '''
import argparse
from dataclasses import dataclass
from typing import Any, ClassVar

from answerback_bridge.adapters.base import StoredAdapter
from answerback_bridge.adapters.catalog import ProviderDefinition
from answerback_bridge.plugin import PLUGIN_API, ToolSpec
from answerback_bridge.provider_config import ProviderConfig


@dataclass
class ThirdConfig(ProviderConfig):
    provider: ClassVar[str] = "third_party"
    endpoint: str = "local"


class ThirdAdapter(StoredAdapter):
    name = registration = "third_party"


class ThirdRegistrar:
    name = "third-client"


class ThirdPlugin:
    api_version = PLUGIN_API
    definition = ProviderDefinition("third_party", "third_party", registration_binary_field=None)
    capabilities = frozenset({"read"})
    config_schema = ThirdConfig

    def build(self, ctx: Any) -> ThirdAdapter:
        return ThirdAdapter(ctx.config, ctx.config_path, ctx.store)

    async def ping(self) -> dict[str, bool]:
        return {"pong": True}

    def rpc(self) -> dict[str, Any]:
        return {"third_ping": self.ping}

    def cli(self, commands: Any, run: Any) -> None:
        command = commands.add_parser("third-command", help="Third-party command")

        async def handler(args: Any, bridge: Any) -> dict[str, Any]:
            return await bridge.call("third_ping", {})

        run(command, handler)

    def mcp_tools(self) -> tuple[ToolSpec, ...]:
        def build(call: Any) -> Any:
            async def third_tool() -> dict[str, Any]:
                """Call the third-party provider."""
                return await call("third_ping", {})

            return third_tool

        return (ToolSpec(build, "read"),)

    def store_hooks(self) -> None:
        return None

    def registrar(self) -> ThirdRegistrar:
        return ThirdRegistrar()

    def event_sources(self) -> tuple[Any, ...]:
        return ()


def make_plugin() -> ThirdPlugin:
    return ThirdPlugin()
'''.lstrip()
    )
    metadata = root / "answerback_third_party_test-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: answerback-third-party-test\nVersion: 1.0\n"
    )
    (metadata / "entry_points.txt").write_text(
        "[answerback.providers]\nthird = third_party_plugin:make_plugin\n"
    )


def test_temporary_distribution_composes_every_surface_and_config_block(tmp_path: Path) -> None:
    """Use real dist-info so discovery, imports and editable metadata run end to end."""
    _write_third_party_distribution(tmp_path)
    script = """
import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import Client

from answerback_bridge.adapters.builtins import builtin_plugins
from answerback_bridge.cli import parser
from answerback_bridge.config import Config, SCHEMAS
from answerback_bridge.mcp_server import create_server
from answerback_bridge.registration import CLIENTS
from answerback_bridge.service import Bridge

path = Path(sys.argv[1])
plugins = builtin_plugins()
third = next(plugin for plugin in plugins if plugin.definition.registration == "third_party")
assert "third_ping" in third.rpc()
assert third.registrar().name == "third-client"
assert SCHEMAS["third_party"].provider == "third_party"
assert "third-client" in CLIENTS
config = Config(provider_selection=["third_party"])
assert config.providers["third_party"].endpoint == "local"
root = parser()
subparsers = next(
    action for action in root._actions if isinstance(action, argparse._SubParsersAction)
)
assert "third-command" in subparsers.choices
bridge = Bridge(config, path)
assert "third_ping" in bridge.METHODS
assert "third_party" in {adapter.registration for adapter in bridge.adapters}

async def verify() -> list[str]:
    try:
        async with Client(create_server(bridge)) as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
        assert "third_tool" in tools
        return tools
    finally:
        await bridge.close()

tools = asyncio.run(verify())
print(json.dumps({"plugins": [p.definition.registration for p in plugins], "tools": tools}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(tmp_path), env.get("PYTHONPATH", "")) if item
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "config.json")],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    # A row without a catalog ``order`` follows every kernel adapter, whatever the entry
    # point is called, so the listing is stable in both compositions.
    assert loaded["plugins"][-1] == "third_party"
    assert "third_tool" in loaded["tools"]


@pytest.fixture(autouse=True)
def reset_plugin_discovery_cache() -> None:
    """Keep synthetic entry-point discovery isolated from the process-wide cache."""
    from answerback_bridge.plugin import reset_plugin_cache

    reset_plugin_cache()
    yield
    reset_plugin_cache()


def test_plugin_loader_caches_entry_points_and_factories_for_one_bridge_start() -> None:
    """Import-time config composition, registrars, and Bridge share one discovered plugin set."""
    script = """
import importlib.metadata
import json

original = importlib.metadata.entry_points
calls = {}
class Entry:
    def __init__(self, entry): self.entry = entry
    def __getattr__(self, name): return getattr(self.entry, name)
    def load(self):
        label = f'{self.entry.group}:{self.entry.name}'
        calls[label] = calls.get(label, 0) + 1
        return self.entry.load()
def entries(*args, **kwargs):
    return [Entry(item) for item in original(*args, **kwargs)]
importlib.metadata.entry_points = entries
from answerback_bridge.config import Config
from answerback_bridge.service import Bridge
from pathlib import Path
bridge = Bridge(Config(provider_selection=[]), Path('/tmp/answerback-cache-test/config.json'))
print(json.dumps(calls, sort_keys=True))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout).values()) == {1}


def test_cached_third_party_plugins_still_require_a_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint("fixture", "fixture_plugin:factory", FixturePlugin),
    )

    assert [plugin.definition.registration for plugin in load_plugins()] == ["fixture"]
    with pytest.raises(
        PluginLoadError,
        match=r"installed metadata has no answerback\.providers entry points.*uv sync.*reinstall",
    ):
        load_plugins(require_builtin=True)


def test_plugin_loader_replaces_a_collected_discovery_function() -> None:
    """A cache entry retains its discovery callable, not its recyclable object id."""
    original = importlib.metadata.entry_points
    try:
        for _attempt in range(20):

            def first(**kwargs: object) -> list[FakeEntryPoint]:
                plugin = FixturePlugin("first")
                plugin.definition = ProviderDefinition(
                    "first", "first", registration_binary_field=None
                )
                return [FakeEntryPoint("first", "first_plugin:factory", lambda: plugin)]

            importlib.metadata.entry_points = first
            assert [plugin.definition.registration for plugin in load_plugins()] == ["first"]
            importlib.metadata.entry_points = original
            del first
            gc.collect()

            def second(**kwargs: object) -> list[FakeEntryPoint]:
                plugin = FixturePlugin("second")
                plugin.definition = ProviderDefinition(
                    "second", "second", registration_binary_field=None
                )
                return [FakeEntryPoint("second", "second_plugin:factory", lambda: plugin)]

            importlib.metadata.entry_points = second
            assert [plugin.definition.registration for plugin in load_plugins()] == ["second"]
    finally:
        importlib.metadata.entry_points = original


def test_reset_plugin_cache_does_not_leak_synthetic_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from answerback_bridge.plugin import reset_plugin_cache

    _entry_points(
        monkeypatch,
        FakeEntryPoint("first", "first_plugin:factory", lambda: FixturePlugin("first")),
    )
    assert [plugin.definition.registration for plugin in load_plugins()] == ["first"]

    reset_plugin_cache()
    _entry_points(
        monkeypatch,
        FakeEntryPoint("second", "second_plugin:factory", lambda: FixturePlugin("second")),
    )
    assert [plugin.definition.registration for plugin in load_plugins()] == ["second"]


def test_external_registrar_client_name_reaches_config_and_cli_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import answerback_bridge.cli as bridge_cli

    installed_entries = (
        *importlib.metadata.entry_points(group=PROVIDER_GROUP),
        *importlib.metadata.entry_points(group=MODULE_GROUP),
    )

    class FixtureRegistrar:
        name = "fixture-client"

    monkeypatch.setattr(FixturePlugin, "registrar", lambda self: FixtureRegistrar())
    _entry_points(
        monkeypatch,
        *(entry for entry in installed_entries if entry.group in (PROVIDER_GROUP, MODULE_GROUP)),
        FakeEntryPoint("fixture", "fixture_plugin:factory", FixturePlugin),
    )
    original = (
        configuration.PROVIDERS,
        configuration.CLIENT_NAMES,
        dict(configuration.SCHEMAS),
        dict(configuration.FLAT_KEYS),
    )
    try:
        configuration._load_plugin_config_tables()
        assert "fixture-client" in configuration.CLIENT_NAMES
        root = bridge_cli.parser()
        clients = next(action for action in root._actions if action.dest == "command").choices[
            "mcp"
        ]
        client_option = next(action for action in clients._actions if action.dest == "mcp_client")
        assert "fixture-client" in client_option.choices

        from answerback_bridge.registration import (
            register_client_registrar,
            unregister_client_registrar,
        )

        late = type("LateRegistrar", (), {"name": "late-client"})()
        register_client_registrar(late)
        try:
            root = bridge_cli.parser()
            clients = next(action for action in root._actions if action.dest == "command").choices[
                "mcp"
            ]
            client_option = next(
                action for action in clients._actions if action.dest == "mcp_client"
            )
            assert "late-client" in client_option.choices
        finally:
            unregister_client_registrar("late-client")
    finally:
        configuration.PROVIDERS, configuration.CLIENT_NAMES = original[:2]
        configuration.SCHEMAS.clear()
        configuration.SCHEMAS.update(original[2])
        configuration.FLAT_KEYS.clear()
        configuration.FLAT_KEYS.update(original[3])


def test_provider_with_missing_registration_binary_field_is_refused(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    missing_field = FixturePlugin()
    missing_field.definition = ProviderDefinition(
        "fixture", "fixture", registration_binary_field="nope"
    )
    schema_less = FixturePlugin("schema_less")
    schema_less.definition = ProviderDefinition("schema_less", "schema_less")
    _entry_points(
        monkeypatch,
        FakeEntryPoint("fixture", "fixture_plugin:factory", lambda: missing_field),
        FakeEntryPoint("schema_less", "schema_less_plugin:factory", lambda: schema_less),
    )

    assert load_plugins() == []
    assert "fixture_nope" in caplog.text
    assert "schema_less_binary" in caplog.text


def test_schema_less_provider_without_a_binary_field_can_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = FixturePlugin("schema_less")
    plugin.definition = ProviderDefinition(
        "schema_less", "schema_less", registration_binary_field=None
    )
    _entry_points(
        monkeypatch,
        FakeEntryPoint("schema_less", "schema_less_plugin:factory", lambda: plugin),
    )

    assert load_plugins() == [plugin]


def test_provider_without_a_binary_field_can_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge import provider_registry
    from answerback_bridge.provider_registry import ProviderRegistry
    from answerback_bridge.tasks import TaskStore

    definition = ProviderDefinition("http", "http", registration_binary_field=None)
    monkeypatch.setattr(configuration, "PROVIDERS", ("http",))
    monkeypatch.setattr(provider_registry, "PROVIDERS", ("http",))
    monkeypatch.setattr(provider_registry, "definition", lambda name: definition)
    config = Config(provider_selection=[])
    registry = ProviderRegistry(
        config, tmp_path / "config.json", TaskStore(tmp_path / "tasks.sqlite")
    )

    assert registry.register("http") == {"providers": [{"provider": "http", "registered": True}]}
