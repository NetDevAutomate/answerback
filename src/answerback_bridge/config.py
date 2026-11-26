"""Explicit, local pairing. No focus-based routing."""

import json
import logging
import os
import stat
import tempfile
from collections import deque
from collections.abc import Mapping
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Generic, TypeVar, overload

from . import receipt_version
from .adapters import catalog
from .digests import digest

# Re-exported: `acp_client.py` and the tests import these from here.
from .provider_config import KIRO_TOOL_NAME as KIRO_TOOL_NAME
from .provider_config import NATIVE_PROVIDERS, ProviderConfig
from .provider_config import validate_kiro_trust_tools as validate_kiro_trust_tools
from .retention import task_availability

_LOG = logging.getLogger(__name__)
_T = TypeVar("_T")

# Filled from the loaded plugins' catalog rows at the end of this module.
PROVIDERS: tuple[str, ...] = ()
# The MCP clients the kernel installs itself; a provider plugin's `ClientRegistrar` adds
# its own name, so `CLIENT_NAMES` is completed from the loaded plugins below.
KERNEL_CLIENT_NAMES = ("codex", "claude", "grok", "kiro", "crew", "kiro-crew")
CLIENT_NAMES: tuple[str, ...] = KERNEL_CLIENT_NAMES
WORKSPACE_PROVIDERS = ("kiro", *NATIVE_PROVIDERS)

# Populated from ``ProviderPlugin.config_schema`` at the end of this module, after Config
# exists and entry-point factories can safely import the kernel API. These dict objects stay
# stable so modules and tests that import them observe the populated tables.
SCHEMAS: dict[str, type[ProviderConfig]] = {}
FLAT_KEYS: dict[str, tuple[str, str]] = {}

# The first flat fields, in the order callers passed them positionally (`Config("session",
# herdr_binary, codex_binary, desktop_threads, codex_home)`); kept for this step only.
_LEGACY_POSITIONAL = (
    "herdr_session",
    "herdr_binary",
    "codex_binary",
    "desktop_threads",
    "codex_home",
)


# The one statement of what listing a machine delegates. The documents quote it
# verbatim (README.md, SECURITY.md, docs/RECIPIENTS.md, docs/HARNESSES.md).
REMOTE_TRUST_STATEMENT = (
    "Listing a machine in `hc_send_machines` delegates completion of tasks sent there "
    "to every program running under that machine's user account: the result file is "
    "proof that the brief reached that account, not that a particular agent wrote it. "
    "Before removing a machine from the list, let its open tasks finish or reconcile "
    "them (`answerback task <id> --wait <seconds>`); receipts whose machine is no "
    "longer listed are not read."
)


def client_names() -> tuple[str, ...]:
    """Every client name exposed by loaded plugin registrars and late registrations."""
    # `registration` imports plugin discovery, so only read its live registry when a
    # frontend builds its parser, after this configuration module finished composing.
    from .registration import CLIENTS

    return tuple(dict.fromkeys((*CLIENT_NAMES, *CLIENTS, "kiro-crew")))


def remote_trust_line(machines: list[str] | tuple[str, ...]) -> str | None:
    """One line naming each listed machine and the trust it carries; None if none."""
    if not machines:
        return None
    return f"hc_send_machines: {', '.join(machines)}. {REMOTE_TRUST_STATEMENT}"


def default_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    candidates = [
        root / name / "config.json"
        for name in ("answerback-bridge", "herdr-bridge", "codex-herdr-bridge")
    ]
    return next((path for path in candidates if path.is_file()), candidates[0])


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text.encode())
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _check_config_path(path: Path, *, create: bool = False) -> Path:
    """Walk trusted links and return the checked physical path for config IO."""
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError(f"Config path must not contain parent traversal: {path}")
    uid = os.getuid()

    def check(
        component: Path, info: os.stat_result, *, directory: bool, config_parent: bool = False
    ) -> None:
        if directory and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Config path component must be a directory: {component}")
        if not directory and not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Config path must name a regular file: {component}")
        system_ancestor = directory and not config_parent and info.st_uid == 0
        if info.st_uid != uid and not system_ancestor:
            raise ValueError(f"Config path has foreign ownership: {component}")
        # A root-owned sticky namespace protects owned child directories from other
        # users' replacement; it is never accepted as the config directory itself.
        sticky_system = system_ancestor and bool(info.st_mode & stat.S_ISVTX)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not sticky_system:
            raise ValueError(f"Config path is writable by another user: {component}")

    # Target components carry their source link so a dangling link cannot create
    # its own target on save. Missing children beyond an existing target are fine.
    pending: deque[tuple[str, Path | None]] = deque((part, None) for part in path.parts)
    current = Path(path.anchor)
    hops = 0
    while pending:
        name, link = pending.popleft()
        if name == "..":
            # Only link targets can contain '..'; current has already been walked
            # without links, including any directory this step backs out of.
            current = current.parent
            continue
        component = current / name
        directory = bool(pending)
        if not directory:
            check(current, current.lstat(), directory=True, config_parent=True)
        try:
            info = component.lstat()
        except FileNotFoundError:
            if link is not None:
                raise ValueError(f"Config path has a dangling symlink: {link}") from None
            if not create:
                raise
            if not directory:
                return component  # Only an absent leaf in a checked parent may be written.
            try:
                component.mkdir(mode=0o700)
            except FileExistsError:
                pass  # Another creator won; verify the entry it left below.
            info = component.lstat()
        if stat.S_ISLNK(info.st_mode):
            # Re-read the containing directory even after a target's '..' step.
            # A hard-linked symlink can retain a trusted UID under an untrusted name.
            if current.lstat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError(
                    "Config path has a symlink in a directory writable by another user: "
                    f"{component}"
                )
            if create and not directory:
                raise ValueError(f"Refusing a symlinked config path component: {component}")
            if info.st_uid not in (uid, 0):
                raise ValueError(f"Config path has foreign ownership: {component}")
            hops += 1
            if hops > 16:
                raise ValueError(f"Config path has too many symlink hops: {path}")
            target = component.readlink()
            pending.extendleft((part, component) for part in reversed(target.parts))
            continue
        check(component, info, directory=directory)
        current = component
    # A file link can end in '.' or '..', which must not bypass the leaf type check.
    check(current, current.lstat(), directory=False)
    return current


_KERNEL_FIELDS = (
    "provider_selection",
    "client_registrations",
    "heartbeat_interval",
    "heartbeat_misses",
)

# The sequence the pre-split ``Config.__post_init__`` checked the providers in, kept so a
# file with faults in two providers is usually refused for the same first one 0.15 named:
# the natives and Rex before the kernel fields; Herdr, Codex, Kiro, Crew and Core after
# them. Not exact: 0.15 ran a few checks out of block order (the active budgets, Herdr's
# binary-for-session, Codex's binary-when-enabled, the enablement-flag types), and those now
# run inside their block, so a file faulty in one of those AND elsewhere may name the other
# fault first. Single-fault files read the same text as 0.15 in every case. A provider in
# neither tuple (one another package installs; 0.15 checked its first such provider in this
# slot) is checked after Rex and before the kernel fields, in schema order.
_VALIDATED_BEFORE_KERNEL = ("codex_cli", "claude_code", "grok", "rex")
_VALIDATED_AFTER_KERNEL = ("herdr", "codex", "kiro", "crew", "hc")

# Flat keys of providers that left the kernel package: read until 0.17 like every other
# flat key, so a pre-0.16 file loads when the provider's plugin is absent (its block then
# lands in `unknown_providers`, kept and disabled, decision S-5) and when it is present (its
# schema's `flat_keys()` claims the same names first).
RETIRED_FLAT_KEYS: dict[str, tuple[str, str]] = {
    "aki_binary": ("aki", "binary"),
    "aki_threads": ("aki", "threads"),
}


class _Flat(Generic[_T]):
    """A pre-0.16 flat attribute (``config.kiro_binary``) routed to its block's field
    (``config.providers["kiro"].binary``).

    Read-write, not read-only: the service, the provider registry, the workspace manager
    and the CLI still assign these (``config.hc_enabled = True``), and those files belong to
    later steps of the split. Assignment is as unchecked as a field assignment was;
    ``Config.save`` re-validates every block, as it re-ran ``__post_init__`` before.
    """

    def __init__(self, provider: str, name: str) -> None:
        self.provider, self.name = provider, name

    def __set_name__(self, owner: type, attribute: str) -> None:
        self.attribute = attribute

    @overload
    def __get__(self, instance: None, owner: type) -> "_Flat[_T]": ...

    @overload
    def __get__(self, instance: "Config", owner: type) -> _T: ...

    def __get__(self, instance: "Config | None", owner: type) -> "_Flat[_T] | _T":
        if instance is None:
            return self
        value: _T = getattr(instance.providers[self.provider], self.name)
        return value

    def __set__(self, instance: "Config", value: _T) -> None:
        setattr(instance.providers[self.provider], self.name, value)


@dataclass(init=False)
class Config:
    """Persisted bridge settings, shared by every adapter that holds one.

    The kernel fields are the dataclass fields; every provider's settings live in its
    block under ``providers[<name>]`` (``providers.kiro.binary`` in ``config.json``), an
    instance of the schema ``SCHEMAS`` names for it (each loaded plugin declares that
    class through ``config_schema``). The flat attributes below (``config.kiro_binary``)
    are the pre-0.16 names, routed to the blocks so the adapters, the service and the CLI
    read and write what they always did; the loader reads flat files until 0.17 and ``save``
    writes the nested shape only.

    ``providers`` holds a block for every loaded provider. ``unknown_providers`` holds the
    blocks of providers no loaded plugin claims (decision S-5): the provider is disabled,
    the block is saved back unchanged, and ``provider_selection`` keeps the name, so an
    install that lacks a plugin never discards the settings of one that has it.

    Adapters and worker runtimes are constructed with a reference to one
    ``Config`` instance and keep reading it live for as long as they run.
    Never reassign it with ``self.config = replace(...)`` anywhere in this
    codebase: that would silently hand the caller a second, disconnected
    object, while every adapter already holding a reference to the
    original keeps seeing the old values with no error raised.
    ``ProviderRegistry._save`` is the only sanctioned way to apply a
    persisted change; it mutates the existing instance in place
    (``self.config.__dict__.update(...)``) so every holder keeps seeing
    the same object.
    """

    # None preserves the selections encoded by pre-registry configuration files.
    provider_selection: list[str] | None = None
    client_registrations: list[str] = field(default_factory=list)
    heartbeat_interval: int = 10
    heartbeat_misses: int = 3
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    unknown_providers: dict[str, dict[str, Any]] = field(default_factory=dict)

    herdr_session = _Flat[str | None]("herdr", "session")
    herdr_binary = _Flat[str]("herdr", "binary")
    codex_binary = _Flat[str]("codex", "binary")
    desktop_threads = _Flat[dict[str, str]]("codex", "desktop_threads")
    codex_home = _Flat[str | None]("codex", "home")
    codex_enabled = _Flat[bool]("codex", "enabled")
    kiro_binary = _Flat[str]("kiro", "binary")
    kiro_workspaces = _Flat[dict[str, str]]("kiro", "workspaces")
    kiro_acp_engine = _Flat[str]("kiro", "acp_engine")
    kiro_trust_all_tools = _Flat[bool]("kiro", "trust_all_tools")
    kiro_trust_tools = _Flat[list[str]]("kiro", "trust_tools")
    kiro_active_budget = _Flat[int]("kiro", "active_budget")
    crew_binary = _Flat[str]("crew", "binary")
    crew_enabled = _Flat[bool]("crew", "enabled")
    crew_port = _Flat[int | None]("crew", "port")
    hc_enabled = _Flat[bool]("hc", "enabled")
    hc_binary = _Flat[str]("hc", "binary")
    hc_session = _Flat[str | None]("hc", "session")
    hc_send_to_done = _Flat[bool]("hc", "send_to_done")
    hc_send_machines = _Flat[list[str]]("hc", "send_machines")
    hc_events = _Flat[bool]("hc", "events")
    hc_placement_policy = _Flat[str | None]("hc", "placement_policy")
    rex_enabled = _Flat[bool]("rex", "enabled")
    rex_binary = _Flat[str]("rex", "binary")
    rex_server = _Flat[str | None]("rex", "server")
    rex_host_id = _Flat[str | None]("rex", "host_id")
    codex_cli_binary = _Flat[str]("codex_cli", "binary")
    codex_cli_home = _Flat[str | None]("codex_cli", "home")
    codex_cli_workspaces = _Flat[dict[str, str]]("codex_cli", "workspaces")
    codex_cli_active_budget = _Flat[int]("codex_cli", "active_budget")
    claude_code_binary = _Flat[str]("claude_code", "binary")
    claude_code_home = _Flat[str | None]("claude_code", "home")
    claude_code_workspaces = _Flat[dict[str, str]]("claude_code", "workspaces")
    claude_code_active_budget = _Flat[int]("claude_code", "active_budget")
    grok_binary = _Flat[str]("grok", "binary")
    grok_home = _Flat[str | None]("grok", "home")
    grok_workspaces = _Flat[dict[str, str]]("grok", "workspaces")
    grok_active_budget = _Flat[int]("grok", "active_budget")

    def __init__(self, *positional: Any, **values: Any) -> None:
        """Kernel fields, ``providers`` (blocks or their mappings) and, for this step, the
        flat names: a flat keyword edits the named block, as ``replace(config, kiro_binary=
        ...)`` has always meant; ``load`` decides flat-versus-nested for a file."""
        if len(positional) > len(_LEGACY_POSITIONAL):
            raise TypeError(
                f"Config.__init__() takes at most {len(_LEGACY_POSITIONAL)} positional arguments"
            )
        for name, value in zip(_LEGACY_POSITIONAL, positional, strict=False):
            if name in values:
                raise TypeError(f"Config.__init__() got multiple values for argument {name!r}")
            values[name] = value
        flat: dict[str, dict[str, Any]] = {}
        for name in list(values):
            if name in FLAT_KEYS:
                provider, attribute = FLAT_KEYS[name]
                flat.setdefault(provider, {})[attribute] = values.pop(name)
        given = values.pop("providers", None)
        unknown = values.pop("unknown_providers", None)
        for entry in fields(self):
            if entry.name in ("providers", "unknown_providers"):
                continue
            if entry.name in values:
                value = values.pop(entry.name)
            elif entry.default_factory is not MISSING:
                value = entry.default_factory()
            else:
                value = entry.default
            setattr(self, entry.name, value)
        if values:
            raise TypeError(
                f"Config.__init__() got an unexpected keyword argument {next(iter(values))!r}"
            )
        self.providers, self.unknown_providers = _blocks(given, flat)
        if unknown is not None:
            if not isinstance(unknown, Mapping):
                raise TypeError("unknown_providers must be a mapping of provider blocks")
            # `save` writes these beside the known blocks under `providers`; a known name
            # here would overwrite that provider's real block with one it cannot load.
            for name in unknown:
                if name in SCHEMAS:
                    raise ValueError(
                        f"unknown_providers names the loaded provider {name!r}; "
                        f"its settings belong in providers[{name!r}]"
                    )
            self.unknown_providers.update({name: dict(block) for name, block in unknown.items()})
        self.__post_init__()

    def __getattr__(self, name: str) -> Any:
        """A flat name a loaded plugin's schema declares (``FLAT_KEYS``) that has no
        descriptor above routes to its block, so ``config.<provider>_<field>`` reads the same
        for an installed provider as for a kernel one."""
        if name in FLAT_KEYS and not name.startswith("__"):
            provider, attribute = FLAT_KEYS[name]
            return getattr(self.providers[provider], attribute)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in FLAT_KEYS and not isinstance(vars(type(self)).get(name), _Flat):
            provider, attribute = FLAT_KEYS[name]
            setattr(self.providers[provider], attribute, value)
            return
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        """Validate the blocks and the kernel fields in the pre-split order (see
        ``_VALIDATED_BEFORE_KERNEL``); ``save`` runs this again before writing."""
        ordered = (*_VALIDATED_BEFORE_KERNEL, *_VALIDATED_AFTER_KERNEL)
        for name in (*_VALIDATED_BEFORE_KERNEL, *(n for n in self.providers if n not in ordered)):
            self._validate_block(name)
        if self.provider_selection is not None and (
            not isinstance(self.provider_selection, list)
            or any(not isinstance(name, str) for name in self.provider_selection)
        ):
            raise ValueError("Invalid provider registration")
        if not isinstance(self.client_registrations, list) or any(
            not isinstance(name, str) or not name for name in self.client_registrations
        ):
            raise ValueError("Invalid client registration")
        if type(self.heartbeat_interval) is not int or not 1 <= self.heartbeat_interval <= 3600:
            raise ValueError("Heartbeat interval must be 1–3600 seconds")
        if type(self.heartbeat_misses) is not int or not 1 <= self.heartbeat_misses <= 100:
            raise ValueError("Missed-heartbeat limit must be 1–100")
        for name in _VALIDATED_AFTER_KERNEL:
            self._validate_block(name)

    def _validate_block(self, name: str) -> None:
        # A listed provider may have no block when no plugin is loaded for it.
        block = self.providers.get(name)
        if block is not None:
            block.validate()

    def registered_providers(self) -> list[str]:
        if self.provider_selection is not None:
            return [name for name in PROVIDERS if name in self.provider_selection]
        # A provider without a block (none loaded for it) was never selected by a legacy file.
        return [
            name
            for name in PROVIDERS
            if (block := self.providers.get(name)) is not None and block.legacy_selected()
        ]

    def provider_enabled(self, provider: str) -> bool:
        return provider in self.registered_providers()

    def disabled_providers(self) -> list[str]:
        """Names this config carries that no loaded plugin claims (S-5): kept, never routed."""
        names = set(self.unknown_providers)
        names.update(name for name in self.provider_selection or () if name not in PROVIDERS)
        return sorted(names)

    def disabled_clients(self) -> list[str]:
        """Registered MCP clients no loaded registrar installs: kept in the file, never
        notified, so an install that lacks a client's plugin keeps the registration of one
        that has it (the S-5 rule applied to `client_registrations`)."""
        return sorted(name for name in self.client_registrations if name not in CLIENT_NAMES)

    def active_clients(self) -> list[str]:
        """The registered MCP clients this installation can reach, in registration order."""
        return [name for name in self.client_registrations if name in CLIENT_NAMES]

    def select_provider(self, provider: str, enabled: bool) -> None:
        """An operator's choice: a name no loaded plugin claims is refused here, unlike in a
        saved file, where it is kept and disabled."""
        if provider not in PROVIDERS:
            raise ValueError("Unknown provider")
        selected = set(self.registered_providers())
        if enabled:
            selected.add(provider)
        else:
            selected.discard(provider)
        kept = [name for name in self.provider_selection or () if name not in PROVIDERS]
        self.provider_selection = [name for name in PROVIDERS if name in selected] + kept

    def flat_values(self) -> dict[str, Any]:
        """Every field under its pre-0.16 flat name (``routing_digest`` reads the blocks)."""
        values: dict[str, Any] = {name: getattr(self, name) for name in _KERNEL_FIELDS}
        for flat, (provider, name) in FLAT_KEYS.items():
            values[flat] = getattr(self.providers[provider], name)
        return values

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            checked = _check_config_path(path)
        except FileNotFoundError:
            raise ValueError(
                f"No bridge config at {path}. Run the installer or configure command."
            ) from None
        try:
            raw = json.loads(checked.read_text())
        except json.JSONDecodeError as error:
            raise ValueError(f"Bridge config at {path} is not valid JSON: {error}") from error
        incompatible = f"Bridge config at {path} is incompatible with this version of Answerback"
        if not isinstance(raw, dict):
            raise ValueError(f"{incompatible}: expected a JSON object, got {type(raw).__name__}")

        def shaped(label: str, schema: type[Any] | None, name: str, value: Any) -> None:
            """Refuse a list where a field's mapping belongs (or the reverse) by name.

            ``schema`` is the block's class, or ``cls`` for a kernel key. A provider no
            loaded plugin claims has no schema: its block is kept as written (decision S-5),
            never checked against the kernel's own field names.
            """
            if schema is None:
                return
            entry = next((item for item in fields(schema) if item.name == name), None)
            factory: Any = entry.default_factory if entry is not None else None
            kind = {dict: ("mapping", dict), list: ("list", list)}.get(factory)
            if kind and not isinstance(value, kind[1]):
                raise ValueError(
                    f"{incompatible}: field {label!r} must be a {kind[0]}, "
                    f"got {type(value).__name__}"
                )

        nested = raw.get("providers", {})
        if not isinstance(nested, dict):
            raise ValueError(
                f"{incompatible}: field 'providers' must be a mapping, got {type(nested).__name__}"
            )
        blocks: dict[str, dict[str, Any]] = {}
        for provider, block in nested.items():
            if not isinstance(block, dict):
                raise ValueError(
                    f"{incompatible}: field 'providers.{provider}' must be a mapping, "
                    f"got {type(block).__name__}"
                )
            blocks[provider] = dict(block)
            for name, value in block.items():
                shaped(f"providers.{provider}.{name}", SCHEMAS.get(provider), name, value)
        values: dict[str, Any] = {}
        shadowed: list[str] = []
        for key, value in raw.items():
            if key == "providers":
                continue
            if key in FLAT_KEYS or key in RETIRED_FLAT_KEYS:
                # Read until 0.17 (CHANGELOG): a flat key fills its block's field unless the
                # file already carries the nested twin, which then wins.
                provider, name = FLAT_KEYS.get(key) or RETIRED_FLAT_KEYS[key]
                block = blocks.setdefault(provider, {})
                if name in block:
                    shadowed.append(key)
                    continue
                shaped(key, SCHEMAS.get(provider), name, value)
                block[name] = value
            elif key == "unknown_providers":
                raise ValueError(f"{incompatible}: unexpected key {key!r}")
            else:
                shaped(key, cls, key, value)
                values[key] = value
        if shadowed:
            _LOG.warning(
                "Bridge config at %s sets both flat and nested values for %s; the nested "
                "values under 'providers' win and the flat keys are ignored",
                path,
                ", ".join(sorted(shadowed)),
            )
        # Before 0.5.0, saved Desktop pairings carried the provider choice.
        codex = blocks.get("codex", {})
        if (
            "enabled" not in codex
            and "provider_selection" not in values
            and codex.get("desktop_threads")
        ):
            codex["enabled"] = True
        try:
            config = cls(**values, providers=blocks)
        except (TypeError, AttributeError, ValueError) as error:
            # A block raises a raw TypeError for an unknown or stale key (an incompatible
            # upgrade); validation raises for a wrong value. Report one ValueError so every
            # caller's `except (BridgeError, OSError, ValueError)` prints it instead of
            # crashing with a traceback.
            raise ValueError(f"{incompatible}: {error}") from error
        disabled = config.disabled_providers()
        if disabled:
            _LOG.warning(
                "Bridge config at %s names providers no loaded plugin claims; they are "
                "disabled and their settings are kept: %s",
                path,
                ", ".join(disabled),
            )
        clients = config.disabled_clients()
        if clients:
            _LOG.warning(
                "Bridge config at %s registers MCP clients no loaded plugin installs; they "
                "are kept and not notified: %s",
                path,
                ", ".join(clients),
            )
        return config

    def save(self, path: Path) -> None:
        """Write the kernel fields and ``providers.<name>`` blocks; never a flat key.

        A block is written whole or not at all: an unselected provider whose block still
        holds its defaults is left out, so a config never mentions a provider it does not
        use, and ``load`` gives an absent block its defaults.
        """
        self.__post_init__()
        values: dict[str, Any] = {name: getattr(self, name) for name in _KERNEL_FIELDS}
        values["providers"] = {
            **{
                name: asdict(block)
                for name, block in self.providers.items()
                if self.provider_enabled(name) or block != SCHEMAS[name]()
            },
            **self.unknown_providers,
        }
        checked = _check_config_path(path, create=True)
        atomic_write(checked, json.dumps(values, indent=2) + "\n")

    def thread(self, alias: str) -> str:
        try:
            return self.desktop_threads[alias]
        except KeyError:
            raise ValueError(
                f"Unknown Desktop alias {alias!r}; explicitly pair the chat first"
            ) from None


def _blocks(
    given: Any, flat: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, ProviderConfig], dict[str, dict[str, Any]]]:
    """One block per loaded provider from ``given`` (a block, its mapping, or absent) with the
    flat edits applied; the blocks of providers no plugin claims, kept as mappings."""
    if given is None:
        given = {}
    if not isinstance(given, Mapping):
        raise TypeError("providers must be a mapping of provider blocks")
    blocks: dict[str, ProviderConfig] = {}
    for provider, schema in SCHEMAS.items():
        block = given.get(provider)
        edits = flat.get(provider, {})
        if block is None:
            blocks[provider] = schema(**edits)
        elif isinstance(block, ProviderConfig):
            if type(block) is not schema:
                raise TypeError(f"providers.{provider} must be a {schema.__name__}")
            # A copy, as `dataclasses.replace` always gave: editing the candidate leaves
            # the live config alone until the registry applies it.
            blocks[provider] = replace(block, **edits)
        elif isinstance(block, Mapping):
            blocks[provider] = schema(**{**block, **edits})
        else:
            raise TypeError(f"providers.{provider} must be a mapping")
    unknown: dict[str, dict[str, Any]] = {}
    for name, block in given.items():
        if name in SCHEMAS:
            continue
        if not isinstance(block, Mapping):
            raise TypeError(f"providers.{name} must be a mapping")
        unknown[name] = dict(block)
    return blocks, unknown


def routing_digest(config: Config) -> str:
    """Digest of the routing-relevant settings. Each catalog row names its fields by dotted
    block path and they are read from the blocks; each is hashed under ``catalog.digest_key``,
    the flat name for every field a published digest already covers, so those bytes stay."""
    selected: dict[str, Any] = {"provider_selection": config.provider_selection}
    for entry in catalog.definitions():
        # A provider without a block (no config schema) declares no routing fields.
        routing = {
            key: getattr(config.providers[entry.registration], key.partition(".")[2])
            for key in entry.routing_fields
        }
        if entry.has_legacy_defaults(routing, enabled=config.provider_enabled(entry.registration)):
            continue
        for container, ignored in entry.ignored_child_fields:
            routing[container] = {
                ident: {key: value for key, value in record.items() if key != ignored}
                for ident, record in routing[container].items()
            }
        selected.update((entry.digest_key(key), value) for key, value in routing.items())
    return digest(selected)


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    # A Core result file's name carries its token, so neither is shown to a caller.
    # The two version keys are shown as `bridge_versions`, never as stored values.
    private = (
        "provisional_result",
        "provisional_provenance",
        "result_token",
        "result_path",
        receipt_version.CREATED,
        receipt_version.ENDED,
    )
    public = {key: value for key, value in task.items() if key not in ("message", "_retention")}
    public["metadata"] = {
        key: value for key, value in task.get("metadata", {}).items() if key not in private
    }
    public["bridge_versions"] = receipt_version.read(task)
    if task.get("_retention") is not None:
        public.update(task_availability(task))
    return public


def _check_routing_fields(
    definitions: tuple[catalog.ProviderDefinition, ...],
    schemas: Mapping[str, type[ProviderConfig]],
) -> None:
    """Fail closed when a routing field names no field of its provider's block, so a bad
    catalog row stops startup instead of breaking ``routing_digest`` later."""
    for entry in definitions:
        if not entry.routing_fields:
            continue
        schema = schemas.get(entry.registration)
        if schema is None:
            raise RuntimeError(
                f"Provider {entry.registration} declares routing fields "
                f"{list(entry.routing_fields)} but loads no config schema"
            )
        names = {item.name for item in fields(schema)}
        for key in entry.routing_fields:
            if key.partition(".")[2] not in names:
                raise RuntimeError(
                    f"Provider {entry.registration} routing field {key!r} is not a field "
                    f"of its config block {schema.__name__} (fields: {', '.join(sorted(names))})"
                )


def reserved_flat_names() -> dict[str, tuple[str, str] | None]:
    """Every name ``Config`` answers itself, which no plugin flat key may shadow, and the
    block field each pre-0.16 flat descriptor (``_Flat``) routes to, or ``None``.

    Computed from the class, not listed by hand: the dataclass fields (also the ones without
    a default, which ``dir`` omits), every class attribute (methods, properties, class
    variables, dunders) and the descriptors. A descriptor is the kernel schema's own flat key,
    so that schema may claim it and any other plugin collides with it; the loader
    (``plugin._check_flat_keys``) refuses the plugin either way, and treats a ``__``-prefixed
    name as reserved because ``__getattr__`` never routes one.
    """
    attributes = vars(Config)
    return {
        name: (
            (descriptor.provider, descriptor.name)
            if isinstance(descriptor := attributes.get(name), _Flat)
            else None
        )
        for name in {*dir(Config), *(item.name for item in fields(Config))}
    }


def _load_plugin_config_tables() -> None:
    """Build provider and flat-key tables from the installed provider entry points."""
    from .plugin import PROVIDER_GROUP, load_plugins

    plugins = load_plugins(PROVIDER_GROUP, require_builtin=True)
    by_registration = {plugin.definition.registration: plugin for plugin in plugins}
    ordered = tuple(entry.registration for entry in catalog.definitions())
    schemas: dict[str, type[ProviderConfig]] = {}
    flat_keys: dict[str, tuple[str, str]] = {}
    flat_owners: dict[str, str] = {}
    for registration in ordered:
        schema = by_registration[registration].config_schema
        if schema is None:
            continue
        if not isinstance(schema, type) or not issubclass(schema, ProviderConfig):
            raise RuntimeError(
                f"Plugin {registration} config_schema must be a ProviderConfig subclass"
            )
        if schema.provider != registration:
            raise RuntimeError(
                f"Plugin {registration} config_schema declares provider {schema.provider!r}"
            )
        schemas[registration] = schema
        for flat, name in schema.flat_keys().items():
            # Unreachable through `load_plugins`, which refuses the later claimant
            # (`plugin._check_flat_keys`); kept as the table's own invariant.
            if flat in flat_keys:
                raise RuntimeError(
                    "Provider schemas "
                    f"{flat_owners[flat]} and {registration} claim flat key {flat!r}"
                )
            flat_keys[flat] = (registration, name)
            flat_owners[flat] = registration
    _check_routing_fields(catalog.definitions(), schemas)

    global PROVIDERS, CLIENT_NAMES
    PROVIDERS = ordered
    CLIENT_NAMES = (
        *KERNEL_CLIENT_NAMES,
        *(
            registrar.name
            for registration in sorted(by_registration)
            if (registrar := by_registration[registration].registrar()) is not None
            and registrar.name not in KERNEL_CLIENT_NAMES
        ),
    )
    SCHEMAS.clear()
    SCHEMAS.update(schemas)
    FLAT_KEYS.clear()
    FLAT_KEYS.update(flat_keys)
    for attribute, descriptor in vars(Config).items():
        if isinstance(descriptor, _Flat) and FLAT_KEYS.get(attribute) != (
            descriptor.provider,
            descriptor.name,
        ):
            raise RuntimeError(
                f"Config.{attribute} does not match the loaded schema of {descriptor.provider}"
            )


_load_plugin_config_tables()
