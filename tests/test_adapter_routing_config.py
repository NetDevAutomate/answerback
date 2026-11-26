from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from answerback_bridge.provider_config import ProviderConfig
from codex_herdr_bridge.adapters import catalog
from codex_herdr_bridge.bus.config import routing_digest
from codex_herdr_bridge.config import Config


def test_registered_configuration_fields_participate_without_bus_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider's routing fields are its block's fields (T-198): loading its schema the
    way step 8 will, and naming them in its catalog row, is all the digest needs."""
    from codex_herdr_bridge import config as configuration

    @dataclass
    class FixtureConfig(ProviderConfig):
        provider: ClassVar[str] = "fixture"
        endpoint: str = "first"

    entry = catalog.ProviderDefinition("fixture", "fixture", routing_fields=("fixture.endpoint",))
    monkeypatch.setattr(catalog, "DEFINITIONS", (*catalog.DEFINITIONS, entry))
    monkeypatch.setattr(configuration, "PROVIDERS", (*configuration.PROVIDERS, "fixture"))
    monkeypatch.setattr(
        configuration, "SCHEMAS", {**configuration.SCHEMAS, "fixture": FixtureConfig}
    )
    monkeypatch.setattr(
        configuration,
        "FLAT_KEYS",
        {**configuration.FLAT_KEYS, "fixture_endpoint": ("fixture", "endpoint")},
    )
    config = Config(provider_selection=[])
    before = routing_digest(config)
    config.providers["fixture"].endpoint = "second"
    assert routing_digest(config) != before


def test_new_provider_defaults_to_disabled_for_legacy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_herdr_bridge import config as configuration

    monkeypatch.setattr(configuration, "PROVIDERS", (*configuration.PROVIDERS, "fixture"))
    assert "fixture" not in Config().registered_providers()


def test_a_nested_only_block_field_participates_in_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-257: the digest reads routing fields from the blocks by dotted name, so a field
    with no flat key (every new field once 0.17 stops reading flat keys) still counts, and
    is hashed under its dotted name."""
    from codex_herdr_bridge import config as configuration

    @dataclass
    class FixtureConfig(ProviderConfig):
        provider: ClassVar[str] = "fixture"
        endpoint: str = "first"

    entry = catalog.ProviderDefinition("fixture", "fixture", routing_fields=("fixture.endpoint",))
    monkeypatch.setattr(catalog, "DEFINITIONS", (*catalog.DEFINITIONS, entry))
    monkeypatch.setattr(configuration, "PROVIDERS", (*configuration.PROVIDERS, "fixture"))
    monkeypatch.setattr(
        configuration, "SCHEMAS", {**configuration.SCHEMAS, "fixture": FixtureConfig}
    )
    assert "fixture_endpoint" not in configuration.FLAT_KEYS
    config = Config(provider_selection=[])
    before = routing_digest(config)
    config.providers["fixture"].endpoint = "second"
    assert routing_digest(config) != before
    assert catalog.digest_key("fixture.endpoint") == "fixture.endpoint"


def test_published_digest_keys_are_the_flat_names_of_the_routing_fields() -> None:
    """Every built-in routing field keeps its pre-0.16 flat name as its digest key, so no
    published digest moves; the frozen table agrees with the schemas' flat names."""
    from codex_herdr_bridge import config as configuration

    routing = [field for entry in catalog.definitions() for field in entry.routing_fields]
    assert set(catalog.published_digest_keys()) == set(routing)
    for field in routing:
        provider, _, name = field.partition(".")
        schema = configuration.SCHEMAS[provider]
        assert catalog.digest_key(field) == schema.flat_key(name)
        assert configuration.FLAT_KEYS[schema.flat_key(name)] == (provider, name)


@pytest.mark.parametrize("field", ["fixture_endpoint", "other.endpoint", "fixture.", ".x"])
def test_routing_fields_must_name_a_field_of_their_own_block(field: str) -> None:
    with pytest.raises(ValueError, match="routing field"):
        catalog.ProviderDefinition("fixture", "fixture", routing_fields=(field,))


@pytest.mark.parametrize("field", ["fixture.missing", "fixture.fixture_endpoint"])
def test_startup_rejects_a_routing_field_its_block_does_not_have(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """A well-formed dotted name must also name a real block field: building the config
    tables at startup fails closed, naming provider, field and block class, rather than
    ``routing_digest`` dying later with an ``AttributeError``. The legacy flat spelling under
    the dotted path (``fixture.fixture_endpoint``) names no field either."""
    from answerback_bridge import config as configuration
    from answerback_bridge import plugin as plugins
    from answerback_bridge.adapters import catalog as definitions
    from answerback_bridge.plugin import PLUGIN_API, ProviderPlugin

    @dataclass
    class FixtureConfig(ProviderConfig):
        provider: ClassVar[str] = "fixture"
        endpoint: str = "first"

    entry = definitions.ProviderDefinition("fixture", "fixture", routing_fields=(field,))

    class FixturePlugin(ProviderPlugin):
        api_version = PLUGIN_API
        capabilities = frozenset({"read"})
        config_schema = FixtureConfig
        definition = entry

        def build(self, ctx: Any) -> Any:
            raise AssertionError("loading must not build an adapter")

    load = plugins.load_plugins
    monkeypatch.setattr(
        plugins, "load_plugins", lambda *args, **kwargs: [*load(*args, **kwargs), FixturePlugin()]
    )
    monkeypatch.setattr(definitions, "DEFINITIONS", (*definitions.DEFINITIONS, entry))
    # The loader rewrites these tables in place; give it copies the monkeypatch restores.
    monkeypatch.setattr(configuration, "PROVIDERS", configuration.PROVIDERS)
    monkeypatch.setattr(configuration, "SCHEMAS", dict(configuration.SCHEMAS))
    monkeypatch.setattr(configuration, "FLAT_KEYS", dict(configuration.FLAT_KEYS))

    message = (
        rf"Provider fixture routing field '{field}' is not a field of its config block "
        r"FixtureConfig \(fields: endpoint\)"
    )
    with pytest.raises(RuntimeError, match=message):
        configuration._load_plugin_config_tables()
        # Without the startup check the tables load and the digest fails late.
        routing_digest(Config(provider_selection=[]))
    assert "fixture" not in configuration.SCHEMAS
