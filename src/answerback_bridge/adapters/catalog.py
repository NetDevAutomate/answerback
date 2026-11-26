"""Registration and persisted-task policy rows; no native imports or side effects.

Every provider, built-in or installed, carries one ``ProviderDefinition`` on its plugin.
``definitions()`` is the table of the loaded plugins' rows, built on first use, so the
consumers that decide by receipt kind (task recovery, manual completion, bus admission,
maintenance, debug leases) see exactly the providers this installation composes.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    registration: str
    name: str
    recovery_states: tuple[str, ...] = ()
    worker_reports: bool = False
    verified_delivery_first: bool = False
    # Dotted ``<registration>.<field>`` names of this provider's block fields that the
    # routing digest covers; ``digest_key`` says what name each is hashed under.
    routing_fields: tuple[str, ...] = ()
    ignored_child_fields: tuple[tuple[str, str], ...] = ()
    # New optional blocks may declare defaults absent from old configs and digests.
    # The entire block must match, and the provider must remain unselected.
    legacy_neutral_defaults: tuple[tuple[str, object], ...] = ()
    # The name a routing field is hashed under when a published digest (task receipts,
    # queued bus commands, admission guards) already covers it: its pre-0.16 flat name.
    # Frozen on the row rather than derived from the flat-key table 0.17 stops reading. A
    # field absent here is hashed under its dotted name, which cannot collide with a flat one.
    digest_keys: tuple[tuple[str, str], ...] = ()
    # The provider block field whose value names the executable used at registration. The
    # default preserves `<registration>_binary`; None marks transports such as HTTP that
    # register configuration only and have no executable to probe.
    registration_binary_field: str | None = "binary"
    # Where the row sits in every catalog-derived table (``config.PROVIDERS``, the
    # ``providers`` listing, a freshly written ``provider_selection``, the adapter registry).
    # Rows sort by this value, then by registration name; a row without one follows every
    # row that has one. The kernel's rows carry 10, 20, ... so a provider another package
    # installs can declare the slot it held before it left the kernel.
    order: int | None = None

    def __post_init__(self) -> None:
        for field in self.routing_fields:
            provider, dot, name = field.partition(".")
            if not dot or provider != self.registration or not name or "." in name:
                raise ValueError(
                    f"{self.registration} routing field {field!r} must be "
                    f"'{self.registration}.<field>'"
                )
        for field, _key in self.digest_keys:
            if field not in self.routing_fields:
                raise ValueError(
                    f"{self.registration} digest key for {field!r} names no routing field"
                )
        if self.registration_binary_field is not None and (
            not isinstance(self.registration_binary_field, str)
            or not self.registration_binary_field
        ):
            raise TypeError(
                f"{self.registration} registration_binary_field must be a nonempty string or None"
            )
        if self.order is not None and (
            isinstance(self.order, bool) or not isinstance(self.order, int)
        ):
            raise TypeError(f"{self.registration} order must be an int or None")

    def order_key(self) -> tuple[bool, int, str]:
        """The sort key of this row in every catalog-derived table."""
        return (self.order is None, self.order or 0, self.registration)

    def digest_key(self, field: str) -> str:
        """The name a dotted routing field of this provider is hashed under."""
        return dict(self.digest_keys).get(field, field)

    def has_legacy_defaults(self, values: Mapping[str, object], *, enabled: bool) -> bool:
        """Only a complete, unselected, exact-default block is safely omitted."""
        neutral = dict(self.legacy_neutral_defaults)
        return (
            bool(neutral)
            and set(self.routing_fields) == neutral.keys()
            and not enabled
            and all(
                key in values and type(values[key]) is type(value) and values[key] == value
                for key, value in neutral.items()
            )
        )


# The loaded plugins' rows, filled by ``definitions()`` on first use. Tests replace it.
DEFINITIONS: tuple[ProviderDefinition, ...] = ()


def ordered(entries: Iterable[ProviderDefinition]) -> tuple[ProviderDefinition, ...]:
    """Rows in catalog order: by ``order``, then registration name; unordered rows last."""
    return tuple(sorted(entries, key=ProviderDefinition.order_key))


def definitions() -> tuple[ProviderDefinition, ...]:
    """The catalog row of every loaded provider plugin, in catalog order.

    Loading happens on first use, not at import: this module must stay importable by every
    plugin without composing the installation, and ``plugin.py`` imports it.
    """
    global DEFINITIONS
    if not DEFINITIONS:
        from ..plugin import PROVIDER_GROUP, load_plugins

        plugins = load_plugins(PROVIDER_GROUP, require_builtin=True)
        DEFINITIONS = ordered(plugin.definition for plugin in plugins)
    return DEFINITIONS


def published_digest_keys() -> dict[str, str]:
    """Every loaded provider's routing field that hashes under a published (flat) name."""
    return {
        field: key for entry in definitions() for field, key in entry.digest_keys if field != key
    }


def digest_key(field: str) -> str:
    """The name a dotted routing field is hashed under in the routing digest."""
    return published_digest_keys().get(field, field)


def definition(name: str) -> ProviderDefinition | None:
    return next(
        (entry for entry in definitions() if name in (entry.name, entry.registration)), None
    )
