"""Explicit, preview-first installation into selected local agent clients."""

import asyncio
import json
import os
import plistlib
import re
import shlex  # noqa: F401 - legacy registration API used by migration tests
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from . import installer_kit
from .installer_kit import LEGACY_SERVER_NAMES, SERVER_NAME, SETUP_SKILL_NAME, Snapshot
from .plugin import (
    ClientInstall,
    ClientMigration,
    ClientRegistrar,
    ClientRegistrationError,
    ProviderPlugin,
    _load_plugin_records,
)
from .process import module_launch_argv  # noqa: F401 - compatibility alias for migration tests

CLIENTS: tuple[str, ...] = ()

# Preserve the kernel's private implementation names while their public provider API lives
# in installer_kit. Kernel call sites remain unchanged; registrars import the public names.
_absolute = installer_kit.absolute
_safe_path = installer_kit.safe_path
_safe_parent = installer_kit.safe_parent
_read = installer_kit.read
_json_object = installer_kit.json_object
_route_matches = installer_kit.route_matches
_owned_route = installer_kit.owned_route
_add_safe_path_flag = installer_kit.add_safe_path_flag
_launch_record = installer_kit.launch_record
_snapshot = installer_kit.snapshot
_tree_matches = installer_kit.tree_matches
_private_copy = installer_kit.private_copy
_private_tree = installer_kit.private_tree
run = installer_kit.run

# Internal kernel-only support for the public helpers above remains implemented once.
_CONSOLE_SCRIPTS = installer_kit._CONSOLE_SCRIPTS
_script_directory = installer_kit._script_directory
_display = installer_kit._display
_backup_directory = installer_kit._backup_directory
_safe_path_state = installer_kit._safe_path_state


@dataclass
class _FilePlan:
    path: Path
    original: bytes | None
    updated: bytes


_Refusal = ClientRegistrationError


def _clients(clients: list[str]) -> list[str]:
    selected = list(dict.fromkeys(item for item in clients))
    if "all" in selected:
        selected = list(CLIENTS) + [item for item in selected if item not in (*CLIENTS, "all")]
    if not selected:
        raise ValueError("Select at least one client")
    invalid = [item for item in selected if item not in CLIENTS]
    if invalid:
        choices = ", ".join(CLIENTS)
        raise ValueError(f"Unsupported client; choose {choices}, or all")
    return selected


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    _safe_path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _safe_path(path)
    # Retain both historical installer identities. Old binaries cannot know a new lock.
    locks = [
        path.with_name(path.name + suffix) for suffix in (".herdr-bridge.lock", ".codex-herdr.lock")
    ]
    acquired: list[Path] = []
    try:
        for lock in locks:
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise _Refusal("Configuration is locked by another installer") from None
            os.close(descriptor)
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.unlink()


def _write(plan: _FilePlan) -> None:
    if _read(plan.path) != plan.original:
        raise _Refusal("Configuration changed during installation; review and retry")
    descriptor, staged = tempfile.mkstemp(prefix=".herdr-bridge-", dir=plan.path.parent)
    staged_path = Path(staged)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(plan.updated)
            stream.flush()
            os.fsync(stream.fileno())
        if _read(plan.path) != plan.original:
            raise _Refusal("Configuration changed during installation; review and retry")
        if plan.original is not None:
            backup_fd, _ = tempfile.mkstemp(
                prefix=plan.path.name + ".herdr-bridge-backup-", dir=plan.path.parent
            )
            with os.fdopen(backup_fd, "wb") as stream:
                stream.write(plan.original)
                stream.flush()
                os.fsync(stream.fileno())
        if _read(plan.path) != plan.original:
            raise _Refusal("Configuration changed during installation; review and retry")
        os.replace(staged_path, plan.path)
        if _read(plan.path) != plan.updated:
            raise _Refusal("Configuration read-back differs after installation")
    finally:
        staged_path.unlink(missing_ok=True)


def _rollback_files(plans: list[_FilePlan]) -> None:
    for plan in reversed(plans):
        if _read(plan.path) != plan.updated:
            raise _Refusal("Concurrent edits prevent rollback; recover from the private backup")
        if plan.original is None:
            plan.path.unlink()
        else:
            _write(_FilePlan(plan.path, plan.updated, plan.original))


def _executable(executable: str | None) -> str:
    if executable is not None:
        return str(_absolute(Path(executable)))
    directory = _script_directory()
    for name in _CONSOLE_SCRIPTS:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise _Refusal("answerback executable is missing from this environment; install the tool first")


def _path_arithmetic(path: Path, home: Path) -> Path:
    """Make a path absolute and expand only the supplied home, without filesystem access."""
    value = os.fspath(path)
    if value == "~":
        path = home
    elif value.startswith("~/"):
        path = home / value[2:]
    return Path(os.path.abspath(path))


def _config_path(client: str, home: Path, real_home: bool) -> Path:
    if client == "codex":
        configured = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
        codex = _path_arithmetic(configured if real_home else home / ".codex", home)
        return codex / "config.toml"
    return {
        "claude": home / ".claude.json",
        "grok": home / ".grok" / "config.toml",
        "kiro": home / ".kiro" / "settings" / "mcp.json",
        "crew": home / ".kiro" / "settings" / "mcp.json",
    }[client]


# The names CPython installs: python, python3, python3.13, python3.13t, pythonw, python.exe.


_SAFE_PATH_NOTE = (
    "-P was added to a Python launch of the bridge module. This is a change to the launch "
    "line, not a rename. With -P, Python no longer looks for the package in the client's "
    "working directory first, so a launch that could find the package only there will "
    "stop working."
)


def _translate_controls(document: Any, old: str) -> None:
    """Translate typed identifiers, preserving opaque permission/settings values."""
    tool_keys = {
        "disabled_tools",
        "enabled_tools",
        "autoApprove",
        "tools",
        "allowedTools",
        "disallowedTools",
    }
    server_keys = {
        "disabledMcpServers",
        "enabledMcpServers",
        "disabledMcpjsonServers",
        "enabledMcpjsonServers",
        "allowedMcpServers",
        "deniedMcpServers",
    }

    def translate(value: str) -> str:
        if value == old:
            return SERVER_NAME
        if value == f"mcp__{old}":
            return f"mcp__{SERVER_NAME}"
        for prefix, replacement in (
            (f"mcp__{old}__", f"mcp__{SERVER_NAME}__"),
            (f"@{old}", f"@{SERVER_NAME}"),
        ):
            if (
                value == prefix
                or value.startswith(prefix + "/")
                or (prefix.endswith("__") and value.startswith(prefix))
            ):
                return replacement + value[len(prefix) :]
        return value

    def identifiers(value: Any, *, server_policy: bool = False) -> Any:
        if isinstance(value, str):
            return translate(value)
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str):
                    value[index] = translate(item)
                elif server_policy and isinstance(item, dict):
                    # Structured policy entries carry unrelated metadata too.
                    name = item.get("serverName")
                    if isinstance(name, str):
                        item["serverName"] = translate(name)
        elif isinstance(value, dict):
            identifier_keys(value)
        return value

    def identifier_keys(value: dict[str, Any]) -> None:
        for key, item in list(value.items()):
            translated_key = translate(key)
            if translated_key != key:
                if translated_key in value and value[translated_key] != item:
                    raise _Refusal("Conflicting tool permissions require manual preservation")
                value[translated_key] = value.pop(key)

    if isinstance(document, dict):
        for key, value in list(document.items()):
            if key in ("env", "headers"):
                continue  # Connection values are opaque, even when a key resembles a control.
            if key == "permissions":
                if isinstance(value, dict):
                    identifier_keys(value)
                    for rule in ("allow", "ask", "deny"):
                        if rule in value:
                            value[rule] = identifiers(value[rule])
                # additionalDirectories, defaultMode and unknown fields are opaque.
            elif key in tool_keys:
                document[key] = identifiers(value)
            elif key in server_keys:
                document[key] = identifiers(value, server_policy=True)
            else:
                _translate_controls(value, old)
    elif isinstance(document, list):
        for value in document:
            _translate_controls(value, old)


def _prepare_migration(
    path: Path,
    config_path: Path,
    *,
    translate_controls: bool = True,
    launches: list[dict[str, Any]] | None = None,
) -> tuple[_FilePlan | None, str | None]:
    original = _read(path)
    if original is None:
        return None, None
    try:
        text = original.decode()
        document = (
            tomlkit.parse(text)
            if path.suffix == ".toml"
            else json.loads(text, object_pairs_hook=_json_object)
        )
    except (ValueError, UnicodeError):
        raise _Refusal("Invalid configuration document; no changes made") from None
    if not isinstance(document, dict):
        raise _Refusal("Configuration root is not an object")
    servers = document.get("mcp_servers" if path.suffix == ".toml" else "mcpServers", {})
    if not isinstance(servers, dict):
        raise _Refusal("MCP server collection is not an object")
    routes = [name for name, entry in servers.items() if _route_matches(entry, str(config_path))]
    if len(routes) > 1:
        raise _Refusal("Conflicting duplicate bridge routes; resolve them before migration")
    if not routes:
        return None, None
    old = routes[0]
    if old == SERVER_NAME:
        # Nothing to rename. An owned module launch still gains -P; an entry that is
        # not provably ours, or is already safe, stays byte for byte as it is.
        if not _owned_route(servers[old], config_path):
            return None, old
        if not _add_safe_path_flag(servers[old], str(path)):
            return None, old
        renamed_from, flag_added = None, True
    elif old in LEGACY_SERVER_NAMES:
        if not _owned_route(servers[old], config_path):
            raise _Refusal("Bridge route ownership is not proven")
        if SERVER_NAME in servers:
            raise _Refusal("Canonical MCP name conflicts with an existing registration")
        servers[SERVER_NAME] = servers.pop(old)
        renamed_from, flag_added = old, _add_safe_path_flag(servers[SERVER_NAME], str(path))
        if translate_controls:
            _translate_controls(document, old)
    else:
        return None, old
    if launches is not None:
        entry = servers[SERVER_NAME]
        launches.append(
            _launch_record(
                str(path),
                entry["command"],
                entry["args"],
                renamed_from=renamed_from,
                flag_added=flag_added,
            )
        )
    updated = (
        tomlkit.dumps(document)
        if path.suffix == ".toml"
        else json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    )
    return _FilePlan(path, original, updated.encode()), old


def _claude_managed_paths() -> list[Path]:
    if sys.platform == "darwin":
        root = Path("/Library/Application Support/ClaudeCode")
        paths = [Path("/Library/Managed Preferences/com.anthropic.claudecode.plist")]
    elif sys.platform == "win32":
        # Windows also supplies policy through the registry. File inspection
        # cannot establish the effective permissions on that platform.
        raise _Refusal(
            "Claude managed registry policy cannot be inspected safely; "
            "ask the administrator to migrate permissions before renaming"
        )
    else:
        root = Path("/etc/claude-code")
        paths = []
    paths.append(root / "managed-settings.json")
    directory = root / "managed-settings.d"
    if directory.is_symlink():
        raise _Refusal(f"Claude managed settings directory is symlinked: {directory}")
    if directory.exists():
        paths.extend(sorted(directory.glob("*.json")))
    return paths


def _control_document(path: Path) -> tuple[bytes | None, dict[str, Any]]:
    try:
        original = _read(path)
        document = (
            plistlib.loads(original)
            if original is not None and path.suffix == ".plist"
            else json.loads(original or b"{}", object_pairs_hook=_json_object)
        )
        if not isinstance(document, dict):
            raise _Refusal("Settings root is not an object")
    except (OSError, ValueError, UnicodeError, plistlib.InvalidFileException) as error:
        detail = str(error) if isinstance(error, _Refusal) else "unreadable or invalid settings"
        raise _Refusal(f"Cannot safely preserve permissions at {path}: {detail}") from None
    return original, document


def _prepare_claude_controls(
    home: Path,
    real_home: bool,
    plan: _FilePlan,
    old: str | None,
    config_path: Path,
    *,
    launches: list[dict[str, Any]] | None = None,
) -> tuple[list[_FilePlan], dict[Path, bytes | None]]:
    """Establish discovered route identities before translating their controls."""
    shown: list[dict[str, Any]] = [] if launches is None else launches
    registration = json.loads(plan.original or b"{}", object_pairs_hook=_json_object)
    updated_registration = json.loads(plan.updated, object_pairs_hook=_json_object)
    user = _absolute(Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))))
    if not real_home:
        user = home / ".claude"
    settings = {user / "settings.json"}
    projects = registration.get("projects", {})
    if not isinstance(projects, dict):
        raise _Refusal("Claude project inventory is invalid; review permission scopes manually")
    roots: set[Path] = set()
    for project, project_settings in projects.items():
        if not isinstance(project_settings, dict):
            raise _Refusal(f"Claude project inventory is invalid at {project}")
        root = Path(project).expanduser()
        if not root.is_absolute():
            raise _Refusal("Claude project inventory has a relative path; review it manually")
        roots.add(root)
    current = _absolute(Path.cwd())
    # Explicit homes isolate tests/callers from unrelated host projects.
    if real_home or current.is_relative_to(home):
        roots.update([current, *current.parents])
    for root in roots:
        settings.update((root / ".claude/settings.json", root / ".claude/settings.local.json"))
    plans: list[_FilePlan] = []
    guards: dict[Path, bytes | None] = {plan.path: plan.original}
    route_documents = [(plan.path, plan.original, updated_registration)]
    for root in sorted(roots):
        path = root / ".mcp.json"
        original, document = _control_document(path)
        guards[path] = original
        route_documents.append((path, original, document))
    collections: list[tuple[str, dict[str, Any]]] = []
    for path, _, document in route_documents:
        collections.append((str(path), document))
    for project, document in updated_registration.get("projects", {}).items():
        collections.append((f"{plan.path} project {project}", document))
    renamed: set[str] = set()
    if old is not None and old in LEGACY_SERVER_NAMES:
        renamed.add(old)
    # First inventory every route. A settings rule can apply across these scopes;
    # translating it is safe only when no remaining legacy identity loses it.
    for scope, document in collections:
        servers = document.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise _Refusal(f"Claude MCP server collection is invalid at {scope}")
        matching = [
            name for name, entry in servers.items() if _route_matches(entry, str(config_path))
        ]
        if len(matching) > 1:
            raise _Refusal(f"Conflicting duplicate Claude bridge routes at {scope}")
        renamed.update(name for name in matching if name in LEGACY_SERVER_NAMES)
    if not renamed:
        # Nothing to rename. An owned module launch still gains -P in every scope; a
        # route that is not provably ours stays as it is and refuses nothing.
        for scope, document in collections:
            entry = document.get("mcpServers", {}).get(SERVER_NAME)
            if (
                entry is not None
                and _owned_route(entry, config_path)
                and _add_safe_path_flag(entry, scope)
            ):
                shown.append(
                    _launch_record(
                        scope, entry["command"], entry["args"], renamed_from=None, flag_added=True
                    )
                )
        for path, original, document in route_documents:
            if document != json.loads(original or b"{}", object_pairs_hook=_json_object):
                updated = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
                plans.append(_FilePlan(path, original, updated))
        return plans, guards
    for scope, document in collections:
        servers = document.get("mcpServers", {})
        for name in (*renamed, SERVER_NAME):
            if name in servers and not _owned_route(servers[name], config_path):
                raise _Refusal(
                    f"Claude route {name} at {scope} has a different or unproven identity; "
                    "preserve its restrictions and resolve the scope before migration"
                )
        legacy = [name for name in renamed if name in servers]
        if legacy and SERVER_NAME in servers:
            raise _Refusal(f"Canonical Claude MCP name conflicts at {scope}")
    for scope, document in collections:
        servers = document.get("mcpServers", {})
        renamed_from = next((name for name in sorted(renamed) if name in servers), None)
        if renamed_from is not None:
            servers[SERVER_NAME] = servers.pop(renamed_from)
        if SERVER_NAME not in servers:
            continue
        # Every route of this batch passed the ownership check above, the ones that keep
        # their name too: the translation below rewrites those as well.
        entry = servers[SERVER_NAME]
        flag_added = _add_safe_path_flag(entry, scope)
        if all(launch["scope"] != _display(scope) for launch in shown):
            shown.append(
                _launch_record(
                    scope,
                    entry["command"],
                    entry["args"],
                    renamed_from=renamed_from,
                    flag_added=flag_added,
                )
            )
    for path, original, document in route_documents:
        for name in sorted(renamed):
            _translate_controls(document, name)
        if document != json.loads(original or b"{}", object_pairs_hook=_json_object):
            updated = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
            plans.append(_FilePlan(path, original, updated))
    for path in sorted(settings):
        original, document = _control_document(path)
        guards[path] = original
        before = json.dumps(document, sort_keys=True)
        for name in sorted(renamed):
            _translate_controls(document, name)
        if json.dumps(document, sort_keys=True) != before:
            plans.append(
                _FilePlan(path, original, (json.dumps(document, indent=2) + "\n").encode())
            )
    for path in _claude_managed_paths():
        original, document = _control_document(path)
        guards[path] = original
        before = json.dumps(document, sort_keys=True)
        for name in sorted(renamed):
            _translate_controls(document, name)
        if json.dumps(document, sort_keys=True) != before:
            raise _Refusal(
                f"Claude managed permissions at {path} still name a legacy bridge; "
                "ask the administrator to migrate them before renaming the server"
            )
    # Server-supplied policy caches are not authoritative writable settings.
    cached = registration.get("cachedOrgSettings")
    if isinstance(cached, dict):
        before = json.dumps(cached, sort_keys=True)
        for name in sorted(renamed):
            _translate_controls(cached, name)
        if json.dumps(cached, sort_keys=True) != before:
            raise _Refusal(
                "Claude cached organization permissions require administrator migration; "
                "local renaming cannot preserve remote policy"
            )
    return plans, guards


def _inspect_crew_override(
    home: Path, config_path: Path, old: str | None
) -> tuple[Path, bytes | None]:
    path = home / ".kiro/crew/mcp.json"
    original, document = _control_document(path)
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise _Refusal(f"Crew MCP server collection is invalid at {path}")
    if any(
        _route_matches(entry, str(config_path)) or name in (old, SERVER_NAME)
        for name, entry in servers.items()
    ):
        raise _Refusal(
            f"Crew override at {path} requires resolution in Crew's native MCP settings: "
            "preserve its controls and remove the override or rename the effective route "
            "before migrating the shared global entry; then reload the Crew chat"
        )
    return path, original


def _prepare(path: Path, name: str, entry: dict[str, Any], replace: bool) -> _FilePlan | None:
    original = _read(path)
    is_toml = path.suffix == ".toml"
    try:
        text = (original or b"").decode()
        document = (
            tomlkit.parse(text)
            if is_toml
            else json.loads(text or "{}", object_pairs_hook=_json_object)
        )
    except (ValueError, UnicodeError):
        raise _Refusal("Invalid configuration document; no changes made") from None
    if not isinstance(document, dict):
        raise _Refusal("Configuration root is not an object")
    key = "mcp_servers" if is_toml else "mcpServers"
    if key not in document:
        document[key] = tomlkit.table() if is_toml else {}
    servers = document[key]
    if not isinstance(servers, dict):
        raise _Refusal("MCP server collection is not an object")
    for other_name, other_entry in servers.items():
        if other_name != name and _route_matches(other_entry, entry["args"][1]):
            raise _Refusal(
                "The selected bridge configuration is already registered under another name"
            )
    current = servers.get(name)
    if name in servers:
        if isinstance(current, dict) and all(
            current.get(key) == value for key, value in entry.items()
        ):
            return None
        if not replace:
            raise _Refusal("MCP entry conflict; review the existing entry before using --replace")
        if not isinstance(current, dict) or any(key in current for key in ("url", "httpUrl")):
            raise _Refusal(
                "Existing MCP entry is not a compatible local command; replace it manually"
            )
        # Keep existing environment and tool restrictions when changing only the connection.
        current.update(entry)
    else:
        servers[name] = entry
    updated = (
        tomlkit.dumps(document)
        if is_toml
        else json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    )
    return _FilePlan(path, original, updated.encode())


@dataclass(frozen=True, slots=True)
class _KernelClientRegistrar(ClientRegistrar):
    """A configuration-file client that remains built into the kernel for this step."""

    name: str

    def install_path(self, home: Path, server_name: str, *, real_home: bool) -> Path:
        del server_name
        return _config_path(self.name, home, real_home)

    def migration_path(self, home: Path, *, real_home: bool) -> Path:
        return _config_path(self.name, home, real_home)

    def install(
        self,
        home: Path,
        *,
        config_path: Path,
        server_name: str,
        entry: dict[str, Any],
        replace: bool,
        real_home: bool,
    ) -> ClientInstall:
        del config_path
        path = _config_path(self.name, home, real_home)
        note = (
            "Uses Kiro's global entry; Crew inheritance and reload need verification."
            if self.name == "crew"
            else None
        )
        try:
            operation = _prepare(path, server_name, entry, replace)
        except _Refusal as error:
            if note is not None:
                error.client_note = note
            raise
        return ClientInstall(path, operation, note)

    def migrate(
        self,
        home: Path,
        *,
        config_path: Path,
        real_home: bool,
    ) -> ClientMigration:
        path = _config_path(self.name, home, real_home)
        launches: list[dict[str, Any]] = []
        plan, old = _prepare_migration(
            path,
            config_path,
            translate_controls=self.name != "claude",
            launches=launches,
        )
        change = plan is not None
        plans: list[_FilePlan] = [plan] if plan is not None else []
        guards: dict[Path, bytes | None] = {}
        if self.name == "claude":
            original = plan.original if plan else _read(path)
            primary = plan or _FilePlan(path, original, original or b"{}")
            extra_plans, extra_guards = _prepare_claude_controls(
                home,
                real_home,
                primary,
                old,
                config_path,
                launches=launches,
            )
            plans.extend(extra_plans)
            guards.update(extra_guards)
            change = change or bool(extra_plans)
        if self.name in ("kiro", "crew"):
            override, original = _inspect_crew_override(home, config_path, old)
            guards[override] = original
        return ClientMigration(
            path=path,
            change=change,
            previous_name=old,
            file_operations=tuple(plans),
            guarded_files=tuple(guards.items()),
            launches=tuple(launches),
        )

    def skill_destination(self, home: Path, skill_name: str, *, real_home: bool) -> Path:
        if self.name == "codex" and real_home:
            return _config_path(self.name, home, True).parent / "skills" / skill_name
        harness = "kiro" if self.name == "crew" else self.name
        return home / f".{harness}/skills" / skill_name

    def skill_native(self, home: Path) -> str | None:
        del home
        return None

    def supports_skill(self, skill_name: str) -> bool:
        del skill_name
        return True

    async def install_skill(self, plan: Any, home: Path, replace: bool) -> None:
        del home, replace
        _apply_skill_file(plan)


_REGISTRARS: dict[str, ClientRegistrar] = {}


def _refresh_clients() -> None:
    global CLIENTS
    CLIENTS = tuple(_REGISTRARS)


def register_client_registrar(registrar: ClientRegistrar) -> None:
    """Register one unique client hook in deterministic insertion order."""
    if not registrar.name or registrar.name in _REGISTRARS:
        raise ValueError(f"Client registrar is missing a unique name: {registrar.name!r}")
    _REGISTRARS[registrar.name] = registrar
    _refresh_clients()


def unregister_client_registrar(name: str) -> None:
    """Remove a loaded registrar, primarily for plugin unload and isolated tests."""
    if name not in _REGISTRARS:
        raise ValueError(f"Unknown client registrar: {name}")
    del _REGISTRARS[name]
    _refresh_clients()


def register_client_plugin(plugin: ProviderPlugin) -> None:
    """Load the client hook contributed by a provider plugin, when it has one."""
    registrar = plugin.registrar()
    if registrar is not None:
        register_client_registrar(registrar)


def client_registrar(name: str) -> ClientRegistrar:
    """Return the loaded registrar for one supported client name."""
    try:
        return _REGISTRARS[name]
    except KeyError:
        raise ValueError(f"Unsupported client registrar: {name}") from None


def _registrar_failure(error: Exception, fallback: str) -> str:
    if isinstance(error, _Refusal):
        return str(error)
    return fallback


async def install_clients(
    config_path: Path,
    clients: list[str],
    name: str = SERVER_NAME,
    apply: bool = False,
    home: Path | None = None,
    executable: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Preview or register a bridge connection; never launch clients or agents."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Server name must contain only letters, digits, underscores, or hyphens")
    selected = _clients(clients)
    selected_home = _absolute(home if home is not None else Path.home())
    command = _executable(executable)
    reports: list[dict[str, Any]] = []
    plans: dict[Path, _FilePlan] = {}
    native: list[tuple[ClientRegistrar, Any, dict[str, Any]]] = []
    for client in selected:
        registrar = client_registrar(client)
        identity = "kiro-crew" if client in ("kiro", "crew") else client
        entry = {
            "command": command,
            "args": ["--config", str(_absolute(config_path)), "mcp", "--client", identity],
        }
        report: dict[str, Any] = {
            "client": client,
            "path": str(registrar.install_path(selected_home, name, real_home=home is None)),
            "entry": entry,
        }
        try:
            prepared = registrar.install(
                selected_home,
                config_path=_absolute(config_path),
                server_name=name,
                entry=entry,
                replace=replace,
                real_home=home is None,
            )
            if not isinstance(prepared, ClientInstall):
                raise TypeError("Registrar returned an invalid installation plan")
            report["path"] = str(prepared.path)
            if prepared.note:
                report["note"] = prepared.note
            plan = prepared.operation
            report.update(
                ready=True,
                would_change=plan is not None,
                status="planned" if plan is not None else "unchanged",
            )
            if isinstance(plan, _FilePlan):
                plans[prepared.path] = plan
            elif plan is not None:
                native.append((registrar, plan, report))
        except (OSError, _Refusal) as error:
            note = getattr(error, "client_note", None)
            if note:
                report["note"] = str(note)
            report.update(
                ready=False,
                would_change=False,
                status="refused",
                error=_registrar_failure(error, "Configuration could not be read"),
            )
        reports.append(report)
    success = all(report["ready"] for report in reports)
    if apply and success:
        stack = ExitStack()
        written: list[_FilePlan] = []
        try:
            for plan in plans.values():
                stack.enter_context(_locked(plan.path))
            for registrar, plan, _ in native:
                for lock in registrar.install_locks(plan):
                    stack.enter_context(_locked(lock))
                registrar.validate_install(plan)
            for plan in plans.values():
                if _read(plan.path) != plan.original:
                    raise _Refusal("Configuration changed during installation; review and retry")
            for plan in plans.values():
                _write(plan)
                written.append(plan)
                for report in reports:
                    if report["path"] == str(plan.path):
                        report["status"] = "installed"
            for registrar, plan, report in native:
                await registrar.apply_install(plan, name)
                report["status"] = "installed"
        except asyncio.CancelledError:
            _rollback_files(written)
            raise
        except (OSError, _Refusal) as error:
            _rollback_files(written)
            success = False
            for report in reports:
                if report["status"] in ("planned", "installed"):
                    report.update(
                        status="refused",
                        ready=False,
                        error=_registrar_failure(error, "Configuration write failed"),
                    )
        finally:
            stack.close()
    elif apply:
        for report in reports:
            if report["status"] == "planned":
                report["status"] = "blocked"
    return {"success": success, "applied": apply, "clients": reports}


async def migrate_clients(
    config_path: Path,
    clients: list[str],
    *,
    apply: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    """Rename only proved owned defaults, preserving every connection and state path."""
    selected_home = _absolute(home if home is not None else Path.home())
    config_path = _resolved(config_path)
    reports: list[dict[str, Any]] = []
    plans: dict[Path, _FilePlan] = {}
    guards: dict[Path, bytes | None] = {}
    native: list[tuple[ClientRegistrar, Any]] = []
    for client in _clients(clients):
        registrar = client_registrar(client)
        report: dict[str, Any] = {
            "client": client,
            "path": str(registrar.migration_path(selected_home, real_home=home is None)),
        }
        try:
            prepared = registrar.migrate(
                selected_home,
                config_path=config_path,
                real_home=home is None,
            )
            if not isinstance(prepared, ClientMigration):
                raise TypeError("Registrar returned an invalid migration plan")
            report["path"] = str(prepared.path)
            plans.update((plan.path, plan) for plan in prepared.file_operations)
            guards.update(prepared.guarded_files)
            if prepared.operation is not None:
                native.append((registrar, prepared.operation))
            report.update(
                ready=True,
                would_change=prepared.change,
                status="planned" if prepared.change else "unchanged",
                previous_name=(
                    None if prepared.previous_name is None else _display(prepared.previous_name)
                ),
            )
            if prepared.change:
                report["name"] = SERVER_NAME
            if prepared.launches:
                report["launches"] = list(prepared.launches)
                if prepared.note is not None:
                    report["launch_note"] = prepared.note
                elif any(launch["safe_path_flag_added"] for launch in prepared.launches):
                    report["launch_note"] = _SAFE_PATH_NOTE
        except (OSError, _Refusal) as error:
            report.update(
                ready=False,
                would_change=False,
                status="refused",
                error=_display(_registrar_failure(error, "Configuration could not be read")),
            )
        reports.append(report)
    success = all(report["ready"] for report in reports)
    written: list[_FilePlan] = []
    native_rollbacks: list[tuple[ClientRegistrar, Any]] = []
    if apply and success:
        stack = ExitStack()
        try:
            for plan in plans.values():
                stack.enter_context(_locked(plan.path))
            for registrar, plan in native:
                for lock in registrar.migration_locks(plan):
                    stack.enter_context(_locked(lock))
            for plan in plans.values():
                if _read(plan.path) != plan.original:
                    raise _Refusal("Configuration changed during migration; review and retry")
            for path, original in guards.items():
                if _read(path) != original:
                    raise _Refusal(f"Permission or override scope changed during migration: {path}")
            for registrar, plan in native:
                rollback = await registrar.apply_migration(plan)
                native_rollbacks.append((registrar, rollback))
            for plan in plans.values():
                for registrar, native_plan in native:
                    registrar.validate_migration(native_plan)
                for path, original in guards.items():
                    if (
                        all(written_plan.path != path for written_plan in written)
                        and _read(path) != original
                    ):
                        raise _Refusal(
                            f"Permission or override scope changed during migration: {path}"
                        )
                _write(plan)
                written.append(plan)
            final_guards = {**guards, **{plan.path: plan.updated for plan in written}}
            for registrar, native_plan in native:
                registrar.validate_migration(native_plan)
            for path, expected in final_guards.items():
                if _read(path) != expected:
                    raise _Refusal(f"Permission or override scope changed during migration: {path}")
            for report in reports:
                if report["status"] == "planned":
                    report["status"] = "migrated"
        except (OSError, _Refusal, asyncio.CancelledError) as error:
            success = False
            recovery: list[str] = []
            try:
                _rollback_files(written)
            except (OSError, _Refusal) as rollback_error:
                recovery.append(str(rollback_error))
            for registrar, rollback in reversed(native_rollbacks):
                try:
                    registrar.rollback_migration(rollback)
                except (OSError, _Refusal) as rollback_error:
                    recovery.append(
                        str(rollback_error)
                        if isinstance(rollback_error, _Refusal)
                        else "Native registrar rollback requires manual recovery"
                    )
            if isinstance(error, asyncio.CancelledError):
                for detail in recovery:
                    error.add_note(detail)
                raise
            for report in reports:
                if report["status"] == "planned":
                    detail = _registrar_failure(error, "Migration write failed")
                    report.update(
                        ready=False,
                        status="refused",
                        error=_display("; ".join([detail, *recovery])),
                    )
        finally:
            stack.close()
    elif apply:
        for report in reports:
            if report["status"] == "planned":
                report["status"] = "blocked"
    return {"success": success, "applied": apply, "config": str(config_path), "clients": reports}


SKILL_NAME = "answerback-collaboration"


@dataclass
class _SkillPlan:
    target: Path
    original: Snapshot
    source: Path
    expected: Snapshot
    home: Path
    link: Path | None = None
    native: str | None = None


def _skill_source(source: Path | None, name: str = SKILL_NAME) -> Path:
    if source is not None:
        path = _absolute(source).resolve()
    else:
        from importlib.resources import files

        bundled = files("answerback_bridge").joinpath("data", "skills", name)
        path = Path(str(bundled))
        if not path.is_dir():
            path = Path(__file__).resolve().parents[2] / "skills" / name
    if not path.is_dir() or not (path / "SKILL.md").is_file():
        if name == SETUP_SKILL_NAME:
            raise _Refusal("The setup skill bundle is missing SKILL.md")
        raise _Refusal("The collaboration skill bundle is missing SKILL.md")
    snapshot = _snapshot(path)
    if snapshot is None or any(row[1] == "symlink" for row in snapshot):
        raise _Refusal("The source skill bundle must contain regular files and directories")
    return path


def _enterable(path: Path) -> tuple[Path | None, tuple[str, ...]]:
    """The longest first part of path that the file system enters as a folder, and the rest."""
    parts = path.parts
    for cut in range(len(parts), 0, -1):
        head = Path(*parts[:cut])
        try:
            if head.is_dir():
                return head, parts[cut:]
        except OSError:
            continue
    return None, parts


def _same_file(first: Path, second: Path) -> bool:
    """Whether both paths can be opened and are one file or folder for the file system."""
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return False


def _same_folder(first: Path, second: Path) -> bool:
    """Whether two folder paths, which may end in names that do not exist yet, are one folder.

    Nothing is computed from the text of a path: the file system says how far each path can be
    entered and whether those two folders are one. The rests must be equal names without `..`.
    """
    head, rest = _enterable(first)
    other, other_rest = _enterable(second)
    if head is None or other is None or rest != other_rest or ".." in rest:
        return False
    return _same_file(head, other)


def _links_to(target: Path, link: Path) -> bool:
    """Whether the link at target names the link path itself, not only a place it leads to.

    The last name of the link's text is kept as written; the folder before it is compared with
    the folder of link through the file system, so `alias/..` and `missing/..` mean what they
    mean to a program that opens the link.
    """
    try:
        named = Path(os.path.join(target.parent, os.readlink(target)))
    except OSError:
        return False
    return named.name == link.name and _same_folder(named.parent, link.parent)


def _equivalent_skill(
    target: Path, source: Path, expected: Snapshot, link: Path | None, strict: bool = False
) -> bool:
    if target.is_symlink():
        if link is not None and _links_to(target, link):
            # Naming the shared entry is not opening it. A link that names it is equal when
            # nothing is at the shared entry's path yet (the run makes it), or when the file
            # system opens the link and it is one file with the shared entry.
            return not os.path.lexists(link) or _same_file(target, link)
        if strict and link is not None:
            # The shared entry is replaced in this run: a link that reaches the old place by
            # another way would keep serving the old content.
            return False
        # Otherwise the link must open, and be one folder with the shared entry or the source.
        return (link is not None and _same_file(target, link)) or _same_file(target, source)
    return _snapshot(target) == expected


def _prepare_skill(
    target: Path,
    source: Path,
    expected: Snapshot,
    replace: bool,
    home: Path,
    link: Path | None = None,
    native: str | None = None,
    native_name: str | None = None,
    strict: bool = False,
) -> _SkillPlan | None:
    _safe_parent(target)
    original = _snapshot(target)
    if _equivalent_skill(target, source, expected, link, strict):
        return None
    if original is not None and not replace:
        raise _Refusal("Skill destination conflict; review it before using --replace")
    if original is not None:
        _safe_parent(home / ".local/state/answerback-bridge/backups/backup")
    if native is not None and target.is_symlink():
        provider = native_name.title() if native_name is not None else "Native"
        raise _Refusal(
            f"{provider}'s native installer cannot safely replace a symlinked skill destination"
        )
    return _SkillPlan(target, original, source, expected, home, link, native)


def _usable_setup_skill(target: Path) -> bool:
    """Whether a setup skill that an agent can use is at target, whatever its content."""
    try:
        folder = target.resolve()
        skill, script = folder / "SKILL.md", folder / "scripts" / "fleet.py"
        if not folder.is_dir() or skill.is_symlink() or script.is_symlink():
            return False
        if not skill.is_file() or not script.is_file():
            return False
        lines = skill.read_text(encoding="utf-8").splitlines()
    except (OSError, RuntimeError, UnicodeDecodeError):
        return False
    if not lines or lines[0].strip() != "---":
        return False
    named = False
    for line in lines[1:]:
        if line.strip() == "---":
            return named
        if line.rstrip() == f"name: {SETUP_SKILL_NAME}":
            named = True
    return False


def _opens_as_skill(path: Path) -> bool:
    """Whether the file system opens path as a folder that holds a regular `SKILL.md`."""
    try:
        return (path / "SKILL.md").is_file()
    except OSError:
        return False


def _skill_link_holds(
    path: Path, source: Path, expected: Snapshot, shared: Path, replaced: bool
) -> bool:
    """Check a client destination once more after the writes of a run."""
    if not _opens_as_skill(path):
        return False
    if _same_file(path, shared):
        return True
    if not path.is_symlink():
        return _snapshot(path) == expected
    return not replaced and _same_file(path, source)


def _apply_skill_file(plan: _SkillPlan) -> None:
    _safe_parent(plan.target)
    if _snapshot(plan.target) != plan.original or _snapshot(plan.source) != plan.expected:
        raise _Refusal("Skill contents changed during installation; review and retry")
    staging = Path(tempfile.mkdtemp(prefix=".herdr-bridge-skill-", dir=plan.target.parent))
    content = staging / "content"
    previous: Path | None = None
    try:
        if plan.link is not None:
            content.symlink_to(plan.link, target_is_directory=True)
        else:
            shutil.copytree(plan.source, content)
            _private_tree(content)
            if _snapshot(content) != plan.expected:
                raise _Refusal("The copied skill does not match its source")
        _safe_parent(plan.target)
        if _snapshot(plan.target) != plan.original or _snapshot(plan.source) != plan.expected:
            raise _Refusal("Skill contents changed during installation; review and retry")
        if plan.original is not None:
            backup = _backup_directory(plan.target, plan.home, plan.original)
            previous = backup / "content"
            os.replace(plan.target, previous)
            _private_tree(previous)
        try:
            os.replace(content, plan.target)
        except OSError:
            if previous is not None:
                os.replace(previous, plan.target)
            raise
        if not _equivalent_skill(plan.target, plan.source, plan.expected, plan.link):
            raise _Refusal("Installed skill could not be verified")
    finally:
        shutil.rmtree(staging)


async def install_skill(
    clients: list[str],
    apply: bool = False,
    home: Path | None = None,
    source: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Install bundled skills through the same registrar set as MCP installation."""
    selected = _clients(clients)
    selected_home = _absolute(home if home is not None else Path.home())
    registrars = {name: client_registrar(name) for name in selected}
    sources = {SKILL_NAME: _skill_source(source)}
    if source is None:
        sources[SETUP_SKILL_NAME] = _skill_source(None, SETUP_SKILL_NAME)
    source_path = sources[SKILL_NAME]

    def destinations(name: str) -> dict[str, Path]:
        found = {"shared": selected_home / ".agents/skills" / name}
        for client, registrar in registrars.items():
            found[client] = registrar.skill_destination(selected_home, name, real_home=home is None)
        return found

    paths = destinations(SKILL_NAME)
    shared = paths["shared"]
    reports: list[dict[str, Any]] = []
    plans: dict[Path, _SkillPlan] = {}
    owners: dict[Path, ClientRegistrar | None] = {}
    linked: list[tuple[dict[str, Any], Path, str]] = []

    def replaced(name: str) -> bool:
        entry = selected_home / ".agents/skills" / name
        return entry in plans and plans[entry].original is not None

    def plan_skill(name: str, found: dict[str, Path]) -> list[dict[str, Any]]:
        made: list[dict[str, Any]] = []
        setup = name == SETUP_SKILL_NAME
        entry = found["shared"]
        expected = _snapshot(sources[name])
        for client, path in found.items():
            registrar = registrars.get(client)
            report: dict[str, Any] = {"skill": name, "client": client, "path": str(path)}
            made.append(report)
            if registrar is not None and not registrar.supports_skill(name):
                report.update(
                    ready=True,
                    would_change=False,
                    status="skipped",
                    note=registrar.skill_skip_note(name),
                )
                continue
            if client == "crew":
                report["note"] = (
                    "Uses Kiro's skill directory; Crew skill inheritance needs verification."
                )
            try:
                native = registrar.skill_native(selected_home) if registrar is not None else None
                link = entry if registrar is not None and native is None else None
                target = path
                if link is not None and path.parent.is_symlink():
                    try:
                        if path.parent.resolve() == entry.parent:
                            target, link = entry, None
                            report["installation_path"] = str(entry)
                    except RuntimeError:
                        raise _Refusal("Skill directory link cannot be resolved") from None
                strict = replaced(name)
                if setup and not replace:
                    _safe_parent(target)
                    if (
                        target.is_dir()
                        and not _equivalent_skill(target, sources[name], expected, link, strict)
                        and _usable_setup_skill(target)
                    ):
                        report.update(
                            ready=True,
                            would_change=False,
                            status="kept",
                            note="Another copy or version of the setup skill is here; "
                            "--replace installs the packaged one.",
                        )
                        continue
                plan = _prepare_skill(
                    target,
                    sources[name],
                    expected,
                    replace,
                    selected_home,
                    link,
                    native,
                    registrar.name if native is not None and registrar is not None else None,
                    strict,
                )
                report.update(
                    ready=True,
                    would_change=plan is not None,
                    status="planned" if plan else "unchanged",
                )
                if plan is not None:
                    plans[target] = plan
                    owners[target] = registrar
                if link is not None:
                    linked.append((report, path, name))
            except (OSError, _Refusal) as error:
                report.update(
                    ready=False,
                    would_change=False,
                    status="refused",
                    error=(
                        str(error)
                        if isinstance(error, _Refusal)
                        else "Skill destination could not be read"
                    ),
                )
        return made

    reports.extend(plan_skill(SKILL_NAME, paths))
    legacy_source = _skill_source(None).with_name("herdr-collaboration")
    if not legacy_source.is_dir():
        legacy_source = _resolved(Path(__file__)).parents[2] / "skills/herdr-collaboration"
    legacy_expected = _snapshot(legacy_source)
    for client, path in list(paths.items()):
        registrar = registrars.get(client)
        legacy = path.with_name("herdr-collaboration")
        if _snapshot(legacy) is None:
            continue
        report = {
            "skill": "herdr-collaboration",
            "client": client,
            "path": str(legacy),
            "note": "Retire full legacy skill to manual alias",
        }
        try:
            if not replace and not _equivalent_skill(legacy, legacy_source, legacy_expected, None):
                raise _Refusal("Legacy collaboration skill conflict; use --replace to back it up")
            target = legacy
            native = registrar.skill_native(selected_home) if registrar is not None else None
            if native is None and registrar is not None and legacy.parent.is_symlink():
                if legacy.parent.resolve() == shared.parent:
                    target = shared.with_name("herdr-collaboration")
                    report["installation_path"] = str(target)
            plan = _prepare_skill(
                target,
                legacy_source,
                legacy_expected,
                replace,
                selected_home,
                native=native,
                native_name=(
                    registrar.name if native is not None and registrar is not None else None
                ),
            )
            if plan:
                plans[target] = plan
                owners[target] = registrar
            report.update(
                ready=True,
                would_change=plan is not None,
                status="planned" if plan else "unchanged",
            )
        except (OSError, _Refusal) as error:
            report.update(
                ready=False,
                would_change=False,
                status="refused",
                error=(
                    str(error) if isinstance(error, _Refusal) else "Legacy skill could not be read"
                ),
            )
        reports.append(report)
    if SETUP_SKILL_NAME in sources:
        reports.extend(plan_skill(SETUP_SKILL_NAME, destinations(SETUP_SKILL_NAME)))
    success = all(report["ready"] for report in reports)
    if apply and success:
        try:
            with ExitStack() as stack:
                for plan in plans.values():
                    stack.enter_context(
                        _locked(plan.target.with_name(plan.target.name + ".registration"))
                    )
                for plan in plans.values():
                    if (
                        _snapshot(plan.target) != plan.original
                        or _snapshot(plan.source) != plan.expected
                    ):
                        raise _Refusal(
                            "Skill contents changed during installation; review and retry"
                        )
                for target, plan in plans.items():
                    registrar = owners[target]
                    if registrar is None:
                        _apply_skill_file(plan)
                    else:
                        await registrar.install_skill(plan, selected_home, replace)
                    for report in reports:
                        if report.get("installation_path", report["path"]) == str(plan.target):
                            report["status"] = "installed"
                unopened = {
                    report["skill"]
                    for report in reports
                    if report["skill"] in sources
                    and report["client"] == "shared"
                    and report["status"] in ("installed", "unchanged")
                    and not _opens_as_skill(Path(report["path"]))
                }
                if unopened:
                    success = False
                for report, link_path, name in linked:
                    if report["status"] not in ("installed", "unchanged"):
                        continue
                    if name in unopened or not _skill_link_holds(
                        link_path,
                        sources[name],
                        _snapshot(sources[name]),
                        selected_home / ".agents/skills" / name,
                        replaced(name),
                    ):
                        success = False
                        report.update(
                            ready=False,
                            status="refused",
                            error="Skill link does not lead to the shared skill; review and retry",
                        )
        except (OSError, _Refusal) as error:
            success = False
            for report in reports:
                if report["status"] == "planned":
                    report.update(
                        ready=False,
                        status="refused",
                        error=(
                            str(error)
                            if isinstance(error, _Refusal)
                            else "Skill installation failed"
                        ),
                    )
    elif apply:
        for report in reports:
            if report["status"] == "planned":
                report["status"] = "blocked"
    return {
        "success": success,
        "applied": apply,
        "source": str(source_path),
        "sources": {name: str(path) for name, path in sources.items()},
        "targets": reports,
    }


def _load_default_registrars() -> None:
    for name in ("codex", "claude", "grok", "kiro", "crew"):
        register_client_registrar(_KernelClientRegistrar(name))
    for loaded in _load_plugin_records():
        registrar = loaded.plugin.registrar()
        if registrar is not None:
            register_client_registrar(registrar)


_load_default_registrars()
