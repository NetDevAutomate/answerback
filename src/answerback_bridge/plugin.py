"""Plugin API, version 1: how a provider or a module composes into the kernel.

``ProviderAdapter`` (``adapters/contracts.py``) is the dispatch half of a provider:
discover, prepare, submit, observe. ``ProviderPlugin`` here is the composition half:
what the kernel needs in order to build that adapter and to wire the provider's RPC
verbs, CLI subcommands, MCP tools and event sources into the surfaces it generates
(``service.METHODS``, ``cli.parser``, ``mcp_server.create_server``, the observer's tails),
plus its receipt-store policy (``StoreHooks``) and its MCP-client installer
(``ClientRegistrar``). ``KernelContext`` is everything a plugin may touch; a plugin
receives nothing else and never imports ``service``, ``cli`` or ``mcp_server``.

This module imports no provider, no bus and no herdr code (``tests/test_plugin.py`` pins
that), so a plugin package can import it without loading any other provider.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from .adapters.catalog import ProviderDefinition
from .adapters.contracts import ProviderAdapter
from .process import ChildProcess, Output, module_launch_argv
from .provider_config import ProviderConfig
from .receipt_snapshot import ReasonRule
from .tasks import Task, TaskStore

if TYPE_CHECKING:
    from .acp_client import ACPClient
    from .config import Config
    from .events.hub import EventHub
    from .native_workers import NativeWorkers
    from .transport import Operations

PLUGIN_API = 1
PROVIDER_GROUP = "answerback.providers"
MODULE_GROUP = "answerback.modules"
_LOG = logging.getLogger(__name__)
_T = TypeVar("_T")

# Entry-point discovery constructs every plugin factory. The composition roots (config,
# registration, catalog, and frontends) all ask for the same immutable definitions during a
# process start, so retain one validated list per group rather than constructing each factory
# again. Tests and embedders that replace entry-point discovery must call
# ``reset_plugin_cache`` before installing their synthetic discovery.
_PLUGIN_RECORD_CACHE: dict[str, tuple[Callable[..., Any], tuple[_LoadedPlugin, ...]]] = {}

# An RPC verb's handler. The kernel validates a call's parameters against
# ``inspect.signature(handler)`` before calling it, so the signature is the verb's parameter
# contract. A handler may carry a ``requires`` attribute naming a provider that must be
# registered before the verb runs; the kernel refuses the call otherwise (see ``Verb``).
RpcHandler = Callable[..., Any]
# A CLI subcommand's handler: the parsed arguments and the RPC client for this invocation.
CliHandler = Callable[[argparse.Namespace, "Operations"], Awaitable[dict[str, Any]]]
# What ``ProviderPlugin.cli`` is handed beside the subparsers: ``run(parser, handler)`` binds
# a handler to a subcommand the plugin added, so the plugin never learns how the kernel
# carries that binding through the parsed namespace.
CliRunner = Callable[[argparse.ArgumentParser, CliHandler], None]
# What a tool function calls to reach the service: ``await call(method, params)``.
ToolCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolFunction(Protocol):
    """An async tool body; its name, signature and docstring are the tool's name, parameter
    schema and description."""

    __name__: str

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One MCP tool a plugin contributes.

    ``build(call)`` returns the async function the server registers; that function's name,
    signature and docstring become the tool's name, parameter schema and description, so
    a tool is declared the way the kernel's own tools are. ``kind`` selects the standard
    annotations (``read``: read-only; ``write``: idempotent mutation; ``send``: puts text in
    front of another agent or stops one). ``public_errors`` asks the kernel to translate
    typed refusals and timeouts into tool errors with their public wording instead of
    letting the raw exception reach the MCP framework.
    """

    build: Callable[[ToolCall], ToolFunction]
    kind: Literal["read", "write", "send"]
    public_errors: bool = False


class EventSource(Protocol):
    """A provider's contribution to the observer loop: the text tail of one of its peers.

    ``provider`` is the ``peer_id`` prefix the source answers for. ``tail`` returns screen or
    chat text for one peer, or ``None`` when there is nothing to read; the kernel digests it
    and never puts it on the stream. ``resources`` is the pass's resource reader, whose
    per-peer details a source may reuse instead of reading the peer again.
    """

    @property
    def provider(self) -> str: ...

    async def tail(self, target: str, peer: Mapping[str, Any], resources: Any) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Verb:
    """An RPC handler whose body is a method of an object that may be replaced at run time.

    ``owner()`` is read on every call, and for the signature the kernel validates parameters
    against, so a client the service swapped out is seen at once. ``requires`` names a
    provider that must be registered before the verb runs.
    """

    owner: Callable[[], Any]
    attr: str
    requires: str | None = None

    @property
    def __signature__(self) -> inspect.Signature:
        return inspect.signature(getattr(self.owner(), self.attr))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return getattr(self.owner(), self.attr)(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class OwnedRpc:
    """A provider-owned callable bound to live service owners.

    Provider packages subclass this type so the callable object itself belongs to that
    package. The generic base preserves the handler signature and makes detached surface
    calls fail before an async handler can return an un-awaited coroutine.
    """

    owners: PluginOwners
    handler: RpcHandler
    requires: str | None = None

    @property
    def __signature__(self) -> inspect.Signature:
        return inspect.signature(self.handler)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.owners.service()
        return self.handler(*args, **kwargs)


class SurfaceCollision(ValueError):
    """Two plugins, or a plugin and the kernel, declared the same verb, tool or source."""


def merged(
    kind: str, kernel: Mapping[str, _T], plugins: Sequence[tuple[str, Mapping[str, _T]]]
) -> dict[str, _T]:
    """The kernel's entries plus every plugin's, in the order given; a repeated name raises.

    Callers pass the plugins sorted by name, so the result, and the first collision reported,
    do not depend on load order.
    """
    result = dict(kernel)
    owners = dict.fromkeys(kernel, "the kernel")
    for plugin, entries in plugins:
        for name, entry in entries.items():
            if name in result:
                raise SurfaceCollision(
                    f"{kind} {name!r} from plugin {plugin} is already declared by {owners[name]}"
                )
            result[name] = entry
            owners[name] = f"plugin {plugin}"
    return result


class StoreHooks(Protocol):
    """A provider's receipt-store policy; the kernel store holds no provider rule itself.

    Three questions the store cannot answer generically: which receipts of this provider
    may be corrected after the fact (``is_reconcilable``), how such a correction is
    verified and written (``reconcile``), and where the receipt's project lives in its
    metadata (``project_of``). ``TaskStore.hooks`` holds the hooks of the loaded plugins;
    ``TaskStore.reconcile`` hands a receipt to the one plugin whose hooks claim it and
    refuses when none does or when two do.

    ``reconcile`` is the store half of a correction; the provider's adapter (its
    ``TaskReconciler.reconcile``) gathers ``result`` and ``evidence`` from the provider's own
    records first and then calls ``TaskStore.reconcile``. Inside, the hooks re-check
    ``is_reconcilable`` on the ``task`` snapshot the adapter verified, check ``evidence``
    against the durable metadata of that snapshot, and write the correction under a
    compare-and-swap on the stored row, so a receipt that changed meanwhile is never
    overwritten. ``evidence`` is provider-shaped; the audit record the hooks write keeps
    what it needs. The defaults describe a provider with no store policy: it claims nothing
    and knows no project.
    """

    def is_reconcilable(self, task: Task) -> bool:
        return False

    def reconcile(
        self, store: TaskStore, task: Task, *, result: str, evidence: Mapping[str, Any]
    ) -> Task:
        raise ValueError("Receipt is not reconcilable")

    def project_of(self, metadata: Mapping[str, Any]) -> str | None:
        """The receipt's project as the metadata stores it, or None when it names none.

        The kernel validates the value (an absolute path) where it groups by project; the
        hooks only know the key.
        """
        return None


class ProjectPathHooks(StoreHooks):
    """Store hooks of a provider that reconciles nothing and keeps its project at one path.

    ``ProjectPathHooks("peer", "project")`` reads ``metadata["peer"]["project"]``; a missing
    or non-object step along the path gives None. The value is returned as stored, so the
    kernel's validation sees what the receipt holds.
    """

    def __init__(self, *path: str) -> None:
        if not path or not all(isinstance(key, str) and key for key in path):
            raise ValueError("ProjectPathHooks needs the metadata keys of the project")
        self.path = path

    def project_of(self, metadata: Mapping[str, Any]) -> str | None:
        value: Any = metadata
        for key in self.path:
            if not isinstance(value, Mapping):
                return None
            value = value.get(key)
        return value


class ClientRegistrationError(ValueError):
    """A bounded refusal whose text is safe to include in a client report."""

    client_note: str | None = None


@dataclass(frozen=True, slots=True)
class ClientInstall:
    """One registrar's prepared installation, still read-only until batch apply."""

    path: Path
    operation: Any = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ClientMigration:
    """One registrar's prepared migration and guarded batch state."""

    path: Path
    change: bool = False
    previous_name: str | None = None
    operation: Any = None
    note: str | None = None
    launches: tuple[Mapping[str, Any], ...] = ()
    file_operations: tuple[Any, ...] = ()
    guarded_files: tuple[tuple[Path, bytes | None], ...] = ()


class ClientRegistrar(Protocol):
    """Install one MCP client through a provider-neutral registry.

    ``install`` and ``migrate`` return opaque prepared operations. The kernel owns the
    multi-client transaction; provider registrars own native validation and application.
    Path methods provide report destinations before fallible preparation. Skill methods
    preserve the existing shared-copy transaction while allowing a provider to use its
    native tool.
    """

    name: str

    def install_path(self, home: Path, server_name: str, *, real_home: bool) -> Path:
        """Return the destination reported before installation is prepared.

        This is pure path arithmetic: it must not touch the filesystem or raise.
        """
        ...

    def migration_path(self, home: Path, *, real_home: bool) -> Path:
        """Return the destination reported before migration is prepared.

        This is pure path arithmetic: it must not touch the filesystem or raise.
        """
        ...

    def install(
        self,
        home: Path,
        *,
        config_path: Path,
        server_name: str,
        entry: dict[str, Any],
        replace: bool,
        real_home: bool,
    ) -> ClientInstall: ...

    def migrate(
        self,
        home: Path,
        *,
        config_path: Path,
        real_home: bool,
    ) -> ClientMigration: ...

    def install_locks(self, plan: Any) -> Sequence[Path]:
        return ()

    def validate_install(self, plan: Any) -> None:
        return None

    async def apply_install(self, plan: Any, server_name: str) -> None:
        return None

    def migration_locks(self, plan: Any) -> Sequence[Path]:
        return ()

    def validate_migration(self, plan: Any) -> None:
        return None

    async def apply_migration(self, plan: Any) -> Any:
        return None

    def rollback_migration(self, rollback: Any) -> None:
        return None

    def skill_destination(self, home: Path, skill_name: str, *, real_home: bool) -> Path: ...

    def skill_native(self, home: Path) -> str | None:
        return None

    def supports_skill(self, skill_name: str) -> bool:
        return True

    def skill_skip_note(self, skill_name: str) -> str | None:
        return None

    async def install_skill(self, plan: Any, home: Path, replace: bool) -> None:
        return None


class PluginVersionMismatch(ValueError):
    """The plugin was written against another ``PLUGIN_API``; the kernel does not load it."""


class PluginLoadError(RuntimeError):
    """A built-in entry point could not produce a compatible plugin object."""


@dataclass(frozen=True, slots=True)
class _LoadedPlugin:
    """One validated plugin and the ownership of its installed entry point."""

    plugin: Any
    built_in: bool
    label: str


def check_api_version(plugin: Any) -> None:
    """Refuse a plugin whose ``api_version`` is not this kernel's ``PLUGIN_API``.

    The version must be the int itself: ``True`` and ``1.0`` compare equal to 1 and are
    refused all the same.
    """
    version = getattr(plugin, "api_version", None)
    if type(version) is not int or version != PLUGIN_API:
        name = (
            getattr(getattr(plugin, "definition", None), "registration", None)
            or getattr(plugin, "name", None)
            or type(plugin).__name__
        )
        raise PluginVersionMismatch(
            f"Plugin {name} declares api_version {version!r}; this kernel is {PLUGIN_API}"
        )


def _distribution_name(entry: metadata.EntryPoint) -> str:
    distribution = getattr(entry, "dist", None)
    name = getattr(distribution, "metadata", {}).get("Name") if distribution else None
    return str(name or "unknown distribution")


def _is_builtin_entry_point(entry: metadata.EntryPoint) -> bool:
    """Whether packaging says this entry point belongs to this ``answerback`` wheel."""
    return _distribution_name(entry).lower().replace("_", "-") == "answerback"


def _entry_label(entry: metadata.EntryPoint) -> str:
    return f"{entry.group}:{entry.name} ({_distribution_name(entry)}; {entry.value})"


def _registration_name(plugin: Any, group: str) -> str:
    name = (
        getattr(getattr(plugin, "definition", None), "registration", None)
        if group == PROVIDER_GROUP
        else getattr(plugin, "name", None)
    )
    if not isinstance(name, str) or not name:
        kind = "provider registration" if group == PROVIDER_GROUP else "module name"
        raise PluginLoadError(f"Plugin is missing a nonempty {kind}")
    return name


def _skip_third_party(label: str, error: Exception) -> None:
    """Warn that a third-party entry point failed validation and is not loaded.

    A refusal the kernel raised names its reason; any other exception (a raising property,
    a broken schema) is named by its type as well, with the traceback at debug level.
    """
    if isinstance(error, (PluginLoadError, PluginVersionMismatch)):
        _LOG.warning("Skipping third-party plugin entry point %s: %s", label, error)
        return
    _LOG.warning(
        "Skipping third-party plugin entry point %s: validation raised %s: %s",
        label,
        type(error).__name__,
        error,
    )
    _LOG.debug("Third-party plugin entry point %s validation traceback", label, exc_info=True)


def _discover_plugin_records(
    group: str = PROVIDER_GROUP, *, require_builtin: bool = False
) -> list[_LoadedPlugin]:
    """Load and validate plugins while retaining their distribution ownership."""
    entries = sorted(
        metadata.entry_points(group=group),
        key=lambda entry: (_distribution_name(entry), entry.name, entry.value),
    )
    if require_builtin and not any(_is_builtin_entry_point(entry) for entry in entries):
        raise PluginLoadError(
            f"The installed metadata has no {group} entry points: "
            "run `uv sync` or reinstall the package"
        )
    loaded: list[tuple[str, _LoadedPlugin]] = []
    owners: dict[str, str] = {}
    claimed_flat_keys: dict[str, str] = {}
    for entry in entries:
        label = _entry_label(entry)
        built_in = _is_builtin_entry_point(entry)
        try:
            target = entry.load()
            plugin = target() if callable(target) else target
        except Exception as error:
            if built_in:
                raise PluginLoadError(
                    f"Built-in plugin entry point {label} failed with {type(error).__name__}"
                ) from error
            _LOG.warning(
                "Skipping third-party plugin entry point %s: factory raised %s: %s",
                label,
                type(error).__name__,
                error,
            )
            _LOG.debug(
                "Third-party plugin entry point %s factory traceback",
                label,
                exc_info=True,
            )
            continue
        try:
            check_api_version(plugin)
            name = _registration_name(plugin, group)
        except Exception as error:
            if built_in:
                raise
            _skip_third_party(label, error)
            continue
        if name in owners:
            raise SurfaceCollision(
                f"Plugin registration {name!r} is declared by entry points "
                f"{owners[name]} and {label}"
            )
        claims: dict[str, str] = {}
        if group == PROVIDER_GROUP:
            try:
                claims = _check_flat_keys(plugin, name, claimed_flat_keys)
            except Exception as error:
                if built_in:
                    raise
                _skip_third_party(label, error)
                continue
        # Committed only now, after every check passed: a refused plugin owns neither its
        # registration nor a flat key, so a later plugin declaring either still loads.
        owners[name] = label
        claimed_flat_keys.update(claims)
        loaded.append((name, _LoadedPlugin(plugin, built_in, label)))
    return [record for _name, record in sorted(loaded, key=lambda item: item[0])]


def _load_plugin_records(
    group: str = PROVIDER_GROUP, *, require_builtin: bool = False
) -> tuple[_LoadedPlugin, ...]:
    """Return one validated record set per entry-point group for this process."""
    source = metadata.entry_points
    cached_entry = _PLUGIN_RECORD_CACHE.get(group)
    cached = cached_entry[1] if cached_entry is not None and cached_entry[0] is source else None
    if cached is None:
        cached = tuple(_discover_plugin_records(group, require_builtin=require_builtin))
        _PLUGIN_RECORD_CACHE[group] = (source, cached)
    elif require_builtin and not any(record.built_in for record in cached):
        raise PluginLoadError(
            f"The installed metadata has no {group} entry points: "
            "run `uv sync` or reinstall the package"
        )
    return cached


def _check_flat_keys(plugin: Any, name: str, claimed: Mapping[str, str]) -> dict[str, str]:
    """The flat keys a provider's config block claims, or refuse the provider.

    ``Config.__getattr__`` routes a loaded plugin's flat keys to its block, and a key that
    shadows a kernel field, method, property or descriptor would be read before the blocks
    exist or hide the kernel's own value; a key another provider loaded earlier claims would
    route to the wrong block. Both refuse the plugin (a third party is skipped with a warning
    naming the key and its owner; a built-in is a programming error), so the first provider
    in load order keeps its key and ``Config`` always constructs. ``claimed`` is read, not
    written: the caller commits the returned keys once the whole plugin has passed, so a
    plugin refused on its second key never owns its first. A schema whose own fields alias
    one flat key twice is refused the same way. The reserved names come from the class
    itself (``config.reserved_flat_names``), imported here on first use because ``config``
    composes the plugins at import.
    """
    schema = getattr(plugin, "config_schema", None)
    claims: dict[str, str] = {}
    flat_keys: Mapping[str, str] = {}
    if isinstance(schema, type) and issubclass(schema, ProviderConfig):
        from .config import reserved_flat_names

        reserved = reserved_flat_names()
        try:
            flat_keys = schema.flat_keys()
        except ValueError as error:
            raise PluginLoadError(f"Provider {error}") from error
        for flat, attribute in flat_keys.items():
            routes = reserved.get(flat)
            if (flat in reserved or flat.startswith("__")) and routes != (name, attribute):
                owner = f", the {routes[0]} block's {routes[1]!r}" if routes else ""
                raise PluginLoadError(
                    f"Provider {name} config flat key {flat!r} (field {attribute!r}) shadows "
                    f"Config.{flat}{owner}"
                )
            if flat in claimed:
                raise PluginLoadError(
                    f"Provider {name} config flat key {flat!r} (field {attribute!r}) is "
                    f"already claimed by provider {claimed[flat]}"
                )
            claims[flat] = name
    binary_field = getattr(
        getattr(plugin, "definition", None), "registration_binary_field", "binary"
    )
    if binary_field is not None:
        flat_key = f"{name}_{binary_field}"
        if flat_key not in flat_keys:
            raise PluginLoadError(
                f"Provider {name} registration_binary_field {binary_field!r} requires "
                f"config flat key {flat_key!r}"
            )
    return claims


def load_plugins(group: str = PROVIDER_GROUP, *, require_builtin: bool = False) -> list[Any]:
    """Load one entry-point group once per process, then return a fresh list view.

    ``require_builtin`` still validates an uncached group before it is stored. The cached
    plugin objects are definition objects: built-ins are rebound to a Bridge's owners by
    ``builtin_plugins`` and third-party plugins receive their KernelContext later, so callers
    cannot retain service state through this cache.
    """
    return [
        record.plugin for record in _load_plugin_records(group, require_builtin=require_builtin)
    ]


def reset_plugin_cache() -> None:
    """Forget discovered plugin definitions for an isolated test or controlled reload."""
    _PLUGIN_RECORD_CACHE.clear()


@dataclass(frozen=True, slots=True)
class ProcessKit:
    """Owned-child process helpers (``process.run`` and ``process.spawn_owned``)."""

    run: Callable[..., Awaitable[Output]]
    spawn_owned: Callable[..., Awaitable[ChildProcess]]


@dataclass(frozen=True, slots=True)
class SharedKits:
    """Kernel kits a plugin may build on instead of importing another provider.

    ``acp`` is the ACP client (decision S-4: a kernel kit, not a module of its own);
    ``native_workers`` is the receipt owner for bridge-owned native CLI sessions.
    """

    acp: type[ACPClient]
    native_workers: type[NativeWorkers]
    process: ProcessKit


def shared_kits() -> SharedKits:
    """The kits of this installation. Imported on first composition, not on module import."""
    from .acp_client import ACPClient
    from .native_workers import NativeWorkers
    from .process import run, spawn_owned

    return SharedKits(
        acp=ACPClient, native_workers=NativeWorkers, process=ProcessKit(run, spawn_owned)
    )


@dataclass(frozen=True, slots=True)
class KernelContext:
    """What a plugin may touch; nothing else is handed to it.

    ``config`` is the one live ``Config`` object (see its docstring: it is mutated in place,
    never replaced, so a plugin keeps reading current values). ``locks`` are the service's
    task lock stripes, shared so an adapter and the service serialize on the same receipt.
    """

    config: Config
    config_path: Path
    store: TaskStore
    events: EventHub
    locks: Sequence[asyncio.Lock]
    launch_argv: Callable[..., list[str]]
    shared: SharedKits


def kernel_context(
    config: Config,
    config_path: Path,
    store: TaskStore,
    *,
    events: EventHub | None = None,
    locks: Sequence[asyncio.Lock] | None = None,
    launch_argv: Callable[..., list[str]] = module_launch_argv,
    shared: SharedKits | None = None,
) -> KernelContext:
    """Compose a context; the service passes its own hub and stripes, tests take defaults."""
    if events is None:
        from .events.hub import EventHub

        events = EventHub()
    return KernelContext(
        config=config,
        config_path=config_path,
        store=store,
        events=events,
        locks=locks if locks is not None else (asyncio.Lock(),),
        launch_argv=launch_argv,
        shared=shared if shared is not None else shared_kits(),
    )


class ProviderPlugin(Protocol):
    """One provider's composition: identity, policy, its adapter and its surfaces.

    ``build`` makes the adapter. ``rpc`` names the verbs the service dispatches
    (``{provider}_{verb}``), ``cli`` adds subcommands, ``mcp_tools`` declares tools and
    ``event_sources`` gives the observer loop its tails; the kernel merges each into the
    matching generated surface and refuses a name two contributors declare. A plugin that
    has nothing to contribute leaves the defaults (``None`` or empty) in place.
    """

    api_version: int
    definition: ProviderDefinition
    capabilities: frozenset[str]
    # The provider's config block (``provider_config.ProviderConfig`` subclass), stored
    # under ``providers.<name>`` and validated through the block itself. The kernel builds
    # its schema tables from the loaded plugins before configuration is constructed.
    # ``None`` means this provider has no settings of its own.
    config_schema: type[ProviderConfig] | None

    def build(self, ctx: KernelContext) -> ProviderAdapter: ...

    def rpc(self) -> Mapping[str, RpcHandler]:
        return {}

    def agent_programs(self) -> Mapping[str, str]:
        """Executable names this plugin recognizes, mapped to public agent kinds."""
        return {}

    def cli(self, commands: argparse._SubParsersAction, run: CliRunner) -> None:
        return None

    def mcp_tools(self) -> Sequence[ToolSpec]:
        return ()

    def store_hooks(self) -> StoreHooks | None:
        """The provider's receipt-store policy; ``build_adapters`` registers it on the store."""
        return None

    def registrar(self) -> ClientRegistrar | None:
        return None

    def event_sources(self) -> Sequence[EventSource]:
        return ()

    def status(self, config: Config) -> Mapping[str, Any] | None:
        """This provider's block of ``provider_status`` (``doctor``), read whenever the
        service resets its connections; ``None`` reports no block."""
        return None

    def receipt_reasons(self) -> Sequence[ReasonRule]:
        """The fixed receipt-detail sentences this provider writes, classified for the fleet
        snapshot (``receipt_snapshot.REASONS`` holds the kernel's)."""
        return ()

    def receipt_reason_order(self) -> Sequence[int]:
        """Historical positions of this provider's rules; empty appends extension rules."""
        return ()

    def mcp_instructions(self) -> str | None:
        """One sentence for the MCP server's instructions on how this provider's peers are
        addressed, placed after the kernel providers' sentences; ``None`` adds nothing."""
        return None

    async def start(self, ctx: KernelContext) -> None:
        return None

    async def close(self) -> None:
        return None


def _detached_owner() -> Any:
    raise RuntimeError(
        "The provider plugins were composed without a service: their surfaces can be "
        "declared, but no adapter can be built and no verb or event source can run"
    )


@dataclass(frozen=True, slots=True)
class PluginOwners:
    """Live service-owned objects used by package-provided plugin surfaces.

    Every field is a getter so a client replaced by a configuration change is read on the
    next call. ``detached`` supports frontends that compose surfaces without a live service.
    """

    service: Callable[[], Any]
    desktop: Callable[[], Any]
    workers: Callable[[], Any]
    core: Callable[[], Any]
    core_tasks: Callable[[], Any]
    core_lock: Callable[[], asyncio.Lock]
    herdr: Callable[[], Any]
    crew: Callable[[], Any]
    rex: Callable[[], Any]
    pressure: Callable[[], Any] = lambda: None

    @classmethod
    def of(cls, service: Any) -> PluginOwners:
        """Create getters over one service, each read when called."""
        return cls(
            service=lambda: service,
            desktop=lambda: service.desktop_index,
            workers=lambda: service.workers,
            core=lambda: service.core,
            core_tasks=lambda: service.core_tasks,
            core_lock=lambda: service.core_lock,
            herdr=lambda: service.herdr,
            crew=lambda: service.crew,
            rex=lambda: service.rex,
            pressure=lambda: service.pressure,
        )

    @classmethod
    def detached(cls) -> PluginOwners:
        """Create owners whose surfaces are declarable but cannot run."""
        return cls(*([_detached_owner] * len(cls.__dataclass_fields__)))


@dataclass(frozen=True, slots=True)
class OwnedEventSource:
    """An event source backed by a read operation on the live service owner."""

    provider: str
    owners: PluginOwners
    read: Callable[[Any, str, Any], Any]

    async def tail(self, target: str, peer: Mapping[str, Any], resources: Any) -> str | None:
        service = self.owners.service()
        if not service.config.provider_enabled(self.provider):
            return None
        return await self.read(service, target, resources)


@dataclass(frozen=True)
class ProviderPluginSpec(ProviderPlugin):
    """Concrete provider composition shared by kernel and separately packaged providers."""

    definition: ProviderDefinition
    capabilities: frozenset[str]
    constructor: Callable[[KernelContext], ProviderAdapter]
    hooks: Callable[[], StoreHooks | None] | None = None
    programs: Mapping[str, str] = field(default_factory=dict)
    verbs: Mapping[str, RpcHandler] = field(default_factory=dict)
    commands: Callable[[argparse._SubParsersAction, CliRunner], None] | None = None
    tools: tuple[ToolSpec, ...] = ()
    sources: tuple[EventSource, ...] = ()
    api_version: int = PLUGIN_API
    config_schema: type[ProviderConfig] | None = None
    client_registrar: ClientRegistrar | None = None
    status_block: Callable[[Config], Mapping[str, Any]] | None = None
    status_name: str | None = None
    status_order: int | None = None
    reasons: tuple[ReasonRule, ...] = ()
    reason_order: tuple[int, ...] = ()
    instructions: str | None = None
    owner_factory: Callable[[PluginOwners], ProviderPluginSpec] | None = field(
        default=None, repr=False, compare=False
    )

    def with_owners(self, owners: PluginOwners) -> ProviderPluginSpec:
        """Recompose this provider around one service's live owners."""
        if self.owner_factory is None:
            return self
        return self.owner_factory(owners)

    def build(self, ctx: KernelContext) -> ProviderAdapter:
        return self.constructor(ctx)

    def store_hooks(self) -> StoreHooks | None:
        return self.hooks() if self.hooks is not None else None

    def registrar(self) -> ClientRegistrar | None:
        return self.client_registrar

    def rpc(self) -> Mapping[str, RpcHandler]:
        return self.verbs

    def agent_programs(self) -> Mapping[str, str]:
        return self.programs

    def cli(self, commands: argparse._SubParsersAction, run: CliRunner) -> None:
        if self.commands is not None:
            self.commands(commands, run)

    def mcp_tools(self) -> Sequence[ToolSpec]:
        return self.tools

    def event_sources(self) -> Sequence[EventSource]:
        return self.sources

    def status(self, config: Config) -> Mapping[str, Any] | None:
        return self.status_block(config) if self.status_block is not None else None

    def receipt_reasons(self) -> Sequence[ReasonRule]:
        return self.reasons

    def receipt_reason_order(self) -> Sequence[int]:
        return self.reason_order

    def mcp_instructions(self) -> str | None:
        return self.instructions


def provider_plugin(
    registration: str,
    constructor: Callable[[KernelContext], ProviderAdapter],
    config_schema: type[ProviderConfig] | None,
    owner_factory: Callable[[PluginOwners], ProviderPluginSpec],
    *,
    definition: ProviderDefinition,
    capabilities: frozenset[str],
    verbs: Mapping[str, RpcHandler] | None = None,
    commands: Callable[[argparse._SubParsersAction, CliRunner], None] | None = None,
    tools: tuple[ToolSpec, ...] = (),
    sources: tuple[EventSource, ...] = (),
    client_registrar: ClientRegistrar | None = None,
    status_block: Callable[[Config], Mapping[str, Any]] | None = None,
    status_name: str | None = None,
    status_order: int | None = None,
    reasons: tuple[ReasonRule, ...] = (),
    reason_order: tuple[int, ...] = (),
    instructions: str | None = None,
    hooks: Callable[[], StoreHooks | None] | None = None,
    programs: Mapping[str, str] | None = None,
) -> ProviderPluginSpec:
    """Construct a rebindable provider plugin from its complete public definition."""
    if definition.registration != registration:
        raise ValueError(
            f"Provider registration {registration!r} does not match definition "
            f"{definition.registration!r}"
        )
    return ProviderPluginSpec(
        definition,
        capabilities,
        constructor,
        hooks,
        programs=programs if programs is not None else {},
        verbs=verbs if verbs is not None else {},
        commands=commands,
        tools=tools,
        sources=sources,
        config_schema=config_schema,
        client_registrar=client_registrar,
        status_block=status_block,
        status_name=status_name,
        status_order=status_order,
        reasons=reasons,
        reason_order=reason_order,
        instructions=instructions,
        owner_factory=owner_factory,
    )


def builtin_plugin(
    registration: str,
    constructor: Callable[[KernelContext], ProviderAdapter],
    config_schema: type[ProviderConfig] | None,
    owner_factory: Callable[[PluginOwners], ProviderPluginSpec],
    *,
    verbs: Mapping[str, RpcHandler] | None = None,
    commands: Callable[[argparse._SubParsersAction, CliRunner], None] | None = None,
    tools: tuple[ToolSpec, ...] = (),
    sources: tuple[EventSource, ...] = (),
    client_registrar: ClientRegistrar | None = None,
    status_block: Callable[[Config], Mapping[str, Any]] | None = None,
    status_name: str | None = None,
    status_order: int | None = None,
    reasons: tuple[ReasonRule, ...] = (),
    reason_order: tuple[int, ...] = (),
    instructions: str | None = None,
    definition: ProviderDefinition | None = None,
    capabilities: frozenset[str] | None = None,
    hooks: Callable[[], StoreHooks | None] | None = None,
    programs: Mapping[str, str] | None = None,
) -> ProviderPluginSpec:
    """Compatibility constructor requiring provider-owned policy arguments."""
    if definition is None or capabilities is None:
        raise ValueError(
            f"Built-in provider {registration!r} must supply its definition and capabilities"
        )
    return provider_plugin(
        registration,
        constructor,
        config_schema,
        owner_factory,
        definition=definition,
        capabilities=capabilities,
        verbs=verbs,
        commands=commands,
        tools=tools,
        sources=sources,
        client_registrar=client_registrar,
        status_block=status_block,
        status_name=status_name,
        status_order=status_order,
        reasons=reasons,
        reason_order=reason_order,
        instructions=instructions,
        hooks=hooks,
        programs=programs,
    )


def owned_verbs(
    owners: PluginOwners, provider: str, **attrs: tuple[str, bool]
) -> dict[str, RpcHandler]:
    """Build RPC verbs from live service methods and optional provider guards."""
    return {
        name: Verb(owners.service, attr, requires=provider if guarded else None)
        for name, (attr, guarded) in attrs.items()
    }


# Compatibility names for code that used the concrete built-in vocabulary before the
# construction API became provider-neutral.
Owners = PluginOwners
BuiltinSource = OwnedEventSource
BuiltinPlugin = ProviderPluginSpec
builtin_verbs = owned_verbs


def provider_status(
    plugins: Sequence[ProviderPlugin],
    config: Config,
    kernel: Sequence[tuple[int, str, Mapping[str, Any]]] = (),
) -> dict[str, Any]:
    """Provider and kernel status blocks in their stable user-visible order.

    Built-ins carry their historical slot on the plugin. Third-party providers without a
    slot follow the positioned blocks in registration-name order. Two blocks under one
    name (two plugins, or a plugin and a kernel block) raise ``SurfaceCollision``, as a
    repeated verb or tool does, instead of one silently replacing the other.
    """
    entries: list[tuple[tuple[bool, int, str], str, Mapping[str, Any]]] = [
        ((False, order, name), name, block) for order, name, block in kernel
    ]
    owners = {name: "the kernel" for _order, name, _block in kernel}
    for plugin in plugins:
        hook = getattr(plugin, "status", None)
        block = hook(config) if hook is not None else None
        if block is None:
            continue
        registration = plugin.definition.registration
        name = getattr(plugin, "status_name", None) or registration
        if name in owners:
            raise SurfaceCollision(
                f"status block {name!r} from plugin {registration} is already declared by "
                f"{owners[name]}"
            )
        owners[name] = f"plugin {registration}"
        order = getattr(plugin, "status_order", None)
        entries.append(((order is None, order or 0, registration), name, block))
    return {name: dict(block) for _order, name, block in sorted(entries)}


def mcp_instructions(plugins: Sequence[ProviderPlugin]) -> list[str]:
    """Every plugin's instruction sentence, in catalog order, without the empty ones."""
    notes: list[str] = []
    for plugin in sorted(plugins, key=lambda item: item.definition.order_key()):
        hook = getattr(plugin, "mcp_instructions", None)
        note = hook() if hook is not None else None
        if note:
            notes.append(note)
    return notes


def receipt_reasons(plugins: Sequence[ProviderPlugin]) -> tuple[ReasonRule, ...]:
    """Compose historical built-in positions, then append extension rules by plugin name."""
    from .receipt_snapshot import HISTORICAL_REASON_COUNT, REASON_ORDER, REASONS

    positioned: dict[int, tuple[str, ReasonRule]] = {
        position: ("the kernel", rule) for position, rule in zip(REASON_ORDER, REASONS, strict=True)
    }
    trailing: list[ReasonRule] = []
    for plugin in by_name(plugins):
        hook = getattr(plugin, "receipt_reasons", None)
        rules = tuple(hook() if hook is not None else ())
        order_hook = getattr(plugin, "receipt_reason_order", None)
        order = tuple(order_hook() if order_hook is not None else ())
        if not order:
            trailing.extend(rules)
            continue
        if len(order) != len(rules):
            raise ValueError(
                f"Plugin {plugin.definition.registration} declares {len(rules)} receipt "
                f"rules but {len(order)} historical positions"
            )
        for position, rule in zip(order, rules, strict=True):
            if not 0 <= position < HISTORICAL_REASON_COUNT:
                raise ValueError(
                    f"Plugin {plugin.definition.registration} declares historical receipt "
                    f"reason position {position} outside 0..{HISTORICAL_REASON_COUNT - 1}"
                )
            if position in positioned:
                owner = positioned[position][0]
                raise SurfaceCollision(
                    f"receipt reason position {position} from plugin "
                    f"{plugin.definition.registration} is already declared by {owner}"
                )
            positioned[position] = (f"plugin {plugin.definition.registration}", rule)
    return tuple(rule for _owner, rule in dict(sorted(positioned.items())).values()) + tuple(
        trailing
    )


def by_name(plugins: Sequence[ProviderPlugin]) -> list[ProviderPlugin]:
    """Plugins sorted by registration name: the order every generated surface uses."""
    return sorted(plugins, key=lambda plugin: plugin.definition.registration)


def agent_programs(plugins: Sequence[ProviderPlugin]) -> dict[str, str]:
    """Build Core's program-to-agent table from plugins in deterministic name order.

    The kernel keeps entries for recognized agents that have no loaded provider plugin.
    Every other row belongs to the plugin that answers for that program. A collision is a
    startup error rather than a load-order-dependent choice.
    """
    unowned = {
        "gemini": "gemini",
        "opencode": "opencode",
        "pi": "pi",
        "amp": "amp",
        "copilot": "copilot",
        "cursor": "cursor",
    }
    contributions = [
        (
            plugin.definition.registration,
            hook() if (hook := getattr(plugin, "agent_programs", None)) is not None else {},
        )
        for plugin in by_name(plugins)
    ]
    return merged("agent program", unowned, contributions)


def loaded_agent_programs() -> dict[str, str]:
    """Build the default Core agent table from the installed provider catalog."""
    return agent_programs(load_plugins(PROVIDER_GROUP))


class ModuleError(ValueError):
    """A module refusal with a stable code and reviewed public message."""

    kind = "module"
    code: str


class MaintenanceProtected(ValueError):
    """A module proved that one maintenance row must not be changed."""


class MaintenanceContributor(Protocol):
    """A module's interpretation of rows in the maintenance graph it owns.

    ``tables`` is the exact set of ``(store, table)`` pairs the contributor claims.
    ``inspect`` returns normalized facts the kernel needs, or raises when the row cannot be
    proved safe.  The kernel retains age, closure, selection, and mutation policy.
    """

    tables: frozenset[tuple[str, str]]

    def inspect(
        self,
        store: str,
        table: str,
        row: Mapping[str, Any],
        *,
        retention: Mapping[str, Any] | None = None,
        related: Mapping[str, Mapping[str, Any]] | None = None,
        planned_at: float,
    ) -> Mapping[str, Any]: ...


class ModulePlugin(Protocol):
    """A non-provider module (the bus, actors) that runs beside the service."""

    api_version: int
    name: str
    # The optional dependency group that must be installed for the module to run.
    extra: str | None
    # RPC verbs that hold a request open. The runtime dispatches these outside the
    # general short-operation admission slots, but enforces each declared capacity.
    long_poll_verbs: Mapping[str, int]
    # Optional public refusals for a long-poll verb that has reached its capacity.
    # The factory keeps a refusal module-owned and creates a fresh exception per request.
    long_poll_refusals: Mapping[str, Callable[[], ModuleError]]

    def rpc(self) -> Mapping[str, RpcHandler]:
        return {}

    def cli(self, commands: argparse._SubParsersAction, run: CliRunner) -> None:
        return None

    def mcp_tools(self) -> Sequence[ToolSpec]:
        return ()

    def maintenance(self) -> MaintenanceContributor | None:
        return None

    def status(self) -> Mapping[str, Any] | None:
        """Module-owned operational state included in the service status response."""
        return None

    async def run(self, ctx: KernelContext, stopping: asyncio.Event) -> None: ...


@dataclass(frozen=True, slots=True)
class MaintenanceContribution:
    """One contributor bound to its trusted entry-point module identity."""

    module_name: str
    built_in: bool
    contributor: MaintenanceContributor

    @property
    def tables(self) -> frozenset[tuple[str, str]]:
        return self.contributor.tables

    def inspect(
        self,
        store: str,
        table: str,
        row: Mapping[str, Any],
        *,
        retention: Mapping[str, Any] | None = None,
        related: Mapping[str, Mapping[str, Any]] | None = None,
        planned_at: float,
    ) -> Mapping[str, Any]:
        return self.contributor.inspect(
            store,
            table,
            row,
            retention=retention,
            related=related,
            planned_at=planned_at,
        )


def load_modules(owner: Any | None = None) -> tuple[ModulePlugin, ...]:
    """Load installed modules and bind package-owned modules to the live service.

    Third-party modules receive only ``KernelContext`` when their worker starts.  A built-in
    may expose ``with_owner`` to preserve an existing in-process subsystem while it migrates;
    distribution ownership, not the entry-point name, decides whether rebinding is allowed.
    """
    records = _load_plugin_records(MODULE_GROUP, require_builtin=True)
    modules: list[ModulePlugin] = []
    for record in records:
        plugin = record.plugin
        bind = getattr(plugin, "with_owner", None)
        if owner is not None and record.built_in and callable(bind):
            plugin = bind(owner)
        modules.append(plugin)
    return tuple(modules)


def load_maintenance_contributions() -> tuple[MaintenanceContribution, ...]:
    """Load module maintenance hooks with trusted distribution ownership metadata."""
    contributions: list[MaintenanceContribution] = []
    for record in _load_plugin_records(MODULE_GROUP, require_builtin=True):
        module = record.plugin
        hook = getattr(module, "maintenance", None)
        contributor = hook() if callable(hook) else None
        if contributor is not None:
            contributions.append(MaintenanceContribution(module.name, record.built_in, contributor))
    return tuple(contributions)


def module_verbs(
    modules: Sequence[ModulePlugin],
) -> list[tuple[str, Mapping[str, RpcHandler]]]:
    """Each installed module's verb table in validated module-name order."""
    return [(module.name, module.rpc()) for module in modules]


# One Service owns at most 32 live client connections. Modules may reserve long-poll
# capacity, but their declarations must never make that connection envelope unbounded.
MAX_LONG_POLL_CONNECTIONS = 32


def module_long_poll_verbs(modules: Sequence[ModulePlugin]) -> dict[str, int]:
    """Collect validated module-owned long-poll capacities by RPC verb."""
    result: dict[str, int] = {}
    for module in modules:
        declared = getattr(module, "long_poll_verbs", {})
        if not isinstance(declared, Mapping):
            raise PluginLoadError(
                f"Module {module.name!r} long_poll_verbs must be a verb-to-capacity mapping"
            )
        unknown = set(declared) - module.rpc().keys()
        if unknown:
            raise PluginLoadError(
                f"Module {module.name!r} declares unknown long-poll verbs {sorted(unknown)!r}"
            )
        for verb, capacity in declared.items():
            if (
                not isinstance(verb, str)
                or not isinstance(capacity, int)
                or isinstance(capacity, bool)
                or capacity < 1
            ):
                raise PluginLoadError(
                    f"Module {module.name!r} declares invalid long-poll capacity for {verb!r}"
                )
            if verb in result:
                raise PluginLoadError(f"Modules declare duplicate long-poll verb {verb!r}")
            if sum(result.values()) + capacity > MAX_LONG_POLL_CONNECTIONS:
                raise PluginLoadError(
                    f"Module {module.name!r} long-poll capacity exceeds connection cap "
                    f"{MAX_LONG_POLL_CONNECTIONS}"
                )
            result[verb] = capacity
    return result


def module_long_poll_refusals(
    modules: Sequence[ModulePlugin],
) -> dict[str, Callable[[], ModuleError]]:
    """Collect optional module-owned capacity refusals for declared long-poll verbs."""
    result: dict[str, Callable[[], ModuleError]] = {}
    for module in modules:
        declared = getattr(module, "long_poll_refusals", {})
        if not isinstance(declared, Mapping):
            raise PluginLoadError(
                f"Module {module.name!r} long_poll_refusals must be a verb-to-factory mapping"
            )
        unknown = set(declared) - set(getattr(module, "long_poll_verbs", {}))
        if unknown:
            raise PluginLoadError(
                f"Module {module.name!r} declares refusals for unknown long-poll verbs "
                f"{sorted(unknown)!r}"
            )
        for verb, factory in declared.items():
            if not callable(factory):
                raise PluginLoadError(
                    f"Module {module.name!r} declares an invalid long-poll refusal for {verb!r}"
                )
            if verb in result:
                raise PluginLoadError(f"Modules declare duplicate long-poll refusal {verb!r}")
            result[verb] = factory
    return result
