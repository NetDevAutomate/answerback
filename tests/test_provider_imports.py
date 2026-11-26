"""Temporary tripwire for kernel imports of provider implementation modules."""

import ast
import importlib
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "answerback_bridge"
KERNEL_IMPORTERS = tuple(
    path
    for path in SOURCE_ROOT.rglob("*.py")
    if "providers" not in path.relative_to(SOURCE_ROOT).parts
)

# Compatibility paths are still provider implementation edges. Resolve them before comparing
# the allowlist so a kernel import cannot hide an edge by dropping the ``providers.`` token.
IMPLEMENTATION_COMPATIBILITY = {
    "adapters.codex": "providers.codex.adapter",
    "adapters.crew": "providers.crew.adapter",
    "adapters.kiro": "providers.kiro.adapter",
    "adapters.native": "providers.native.adapter",
    "adapters.rex": "providers.rex.adapter",
    "codex": "providers.codex.history",
    "crew": "providers.crew.client",
    "desktop": "providers.codex.desktop",
    "kiro_agent": "providers.kiro.agent",
    "native_claude": "providers.native.claude",
    "native_codex": "providers.native.codex",
    "native_grok": "providers.native.grok",
    "native_protocol": "providers.native.protocol",
    "native_workers": "providers.native.workers",
    "rex": "providers.rex.client",
    "rex_mailbox": "providers.rex.mailbox",
    "rex_service": "providers.rex.service",
    "workers": "providers.kiro.workers",
}

IMPLEMENTATION_ALIAS_MARKER = "sys.modules[__name__] = import_module("


def _implementation_alias_paths() -> dict[str, Path]:
    return {
        ".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts): path
        for path in SOURCE_ROOT.rglob("*.py")
        if IMPLEMENTATION_ALIAS_MARKER in path.read_text()
    }


def _resolved_implementation_compatibility() -> dict[str, str]:
    return {
        compatibility: importlib.import_module(
            f"answerback_bridge.{compatibility}"
        ).__name__.removeprefix("answerback_bridge.")
        for compatibility in _implementation_alias_paths()
    }


DISCOVERED_IMPLEMENTATION_COMPATIBILITY = _resolved_implementation_compatibility()


def _dynamic_provider_string_edges() -> dict[str, list[str]]:
    alias_paths = set(_implementation_alias_paths().values())
    observed: dict[str, list[str]] = {}
    for path in KERNEL_IMPORTERS:
        if path in alias_paths:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        imports_dynamically = any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "import_module")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            )
            for node in ast.walk(tree)
        )
        modules = sorted(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("answerback_bridge.providers.")
            and not node.value.endswith(".config")
        )
        if imports_dynamically and modules:
            observed[str(path.relative_to(SOURCE_ROOT))] = modules
    return observed


# Accepted string-form compatibility/registration edges that the import AST tripwire below
# cannot resolve: KiroACP's lazy export and the built-in CLI/tool registration tables. Concrete
# provider config exports are public schema compatibility, not implementation edges.
DYNAMIC_PROVIDER_STRING_EDGES = {
    "acp_client.py": ["answerback_bridge.providers.kiro.acp"],
    "adapters/builtin_cli.py": [
        "answerback_bridge.providers.codex.cli",
        "answerback_bridge.providers.crew.cli",
        "answerback_bridge.providers.herdr.cli",
        "answerback_bridge.providers.herdr.cli",
        "answerback_bridge.providers.kiro.cli",
        "answerback_bridge.providers.rex.cli",
    ],
    "adapters/builtin_tools.py": [
        "answerback_bridge.providers.codex.tools",
        "answerback_bridge.providers.crew.tools",
        "answerback_bridge.providers.herdr.tools",
        "answerback_bridge.providers.herdr.tools",
        "answerback_bridge.providers.kiro.tools",
        "answerback_bridge.providers.rex.tools",
    ],
}

# Step 12 made built-in definitions and surfaces plugin-owned. It did not move service-object
# construction or PluginOwners out of the kernel; T-262 owns that handover. Until then, pin
# every remaining kernel -> provider implementation edge so this boundary cannot grow or be
# hidden behind an implementation compatibility alias. Values are canonical module:symbol
# names and deliberately carry no source line numbers.
PROVIDER_IMPORTS_AFTER_STEP_12: dict[str, list[str]] = {
    "adapters/factory.py": [
        "providers.codex.adapter:CodexDiscovery",
        "providers.crew.adapter:CrewDiscovery",
        "providers.herdr.adapters.core:CoreDiscovery",
        "providers.herdr.adapters.herdr:HerdrDiscovery",
        "providers.kiro.adapter:KiroDiscovery",
        "providers.native.adapter:NativeDiscovery",
        "providers.rex.adapter:RexDiscovery",
        "providers.rex.client:Rex",
    ],
    "cli.py": [
        "providers.herdr.herdr:Herdr",
        "providers.herdr:herdr_app",
        "providers.herdr:herdr_app",
        "providers.herdr:herdr_app",
        "providers.rex.client:Rex",
        "providers.rex.client:resolve_binary",
    ],
    "fleet/probes.py": ["providers.herdr.core:Core"],
    "placement/probe.py": ["providers.herdr.core:Core"],
    "plugin.py": [
        "providers.native.workers:NativeWorkers",
        "providers.native.workers:NativeWorkers",
    ],
    "service.py": [
        "providers.codex.adapter:CodexAdapter",
        "providers.codex.desktop:DesktopIndex",
        "providers.crew.adapter:CrewAdapter",
        "providers.crew.client:Crew",
        "providers.herdr.adapters.herdr:HerdrAdapter",
        "providers.herdr.core:Core",
        "providers.herdr.core_tasks:CoreTasks",
        "providers.herdr.herdr:Herdr",
        "providers.kiro.workers:Workers",
        "providers.native.claude:MAX_SETTINGS_BYTES",
        "providers.native.claude:SDK_VERSION",
        "providers.native.claude:_cli_version_error",
        "providers.native.claude:_cli_version_from_output",
        "providers.native.claude:_missing_sdk_error",
        "providers.native.claude:_provider_environment",
        "providers.native.claude:_sdk_version_error",
        "providers.native.workers:native_home",
        "providers.rex.service:RexService",
    ],
    "worker_runtime.py": ["providers.native.protocol:SessionControl"],
}


@pytest.mark.parametrize(("compatibility", "canonical"), IMPLEMENTATION_COMPATIBILITY.items())
def test_provider_implementation_compatibility_paths_alias_canonical_modules(
    compatibility: str, canonical: str
) -> None:
    old = importlib.import_module(f"answerback_bridge.{compatibility}")
    moved = importlib.import_module(f"answerback_bridge.{canonical}")

    assert old is moved
    assert old.__name__ == f"answerback_bridge.{canonical}"


def test_provider_implementation_compatibility_table_matches_source_aliases() -> None:
    assert DISCOVERED_IMPLEMENTATION_COMPATIBILITY == IMPLEMENTATION_COMPATIBILITY


def test_dynamic_provider_string_edges_match_accepted_sites() -> None:
    assert _dynamic_provider_string_edges() == DYNAMIC_PROVIDER_STRING_EDGES


def test_kiro_compatibility_exports_are_provider_owned_classes() -> None:
    compatibility_acp = importlib.import_module("answerback_bridge.acp_client")
    provider_acp = importlib.import_module("answerback_bridge.providers.kiro.acp")
    compatibility_workers = importlib.import_module("answerback_bridge.workers")
    provider_workers = importlib.import_module("answerback_bridge.providers.kiro.workers")

    assert compatibility_acp.KiroACP is provider_acp.KiroACP
    assert compatibility_workers.Workers is provider_workers.Workers
    assert provider_acp.KiroACP.__module__ == "answerback_bridge.providers.kiro.acp"
    assert provider_workers.Workers.__module__ == "answerback_bridge.providers.kiro.workers"


def _canonical_provider(module: str) -> str | None:
    relative = module.removeprefix("answerback_bridge.")
    if relative == "providers" or relative.startswith("providers."):
        return relative
    return DISCOVERED_IMPLEMENTATION_COMPATIBILITY.get(relative)


def _from_module(relative: Path, node: ast.ImportFrom) -> str:
    module = node.module or ""
    if node.level == 0:
        if module == "answerback_bridge":
            return ""
        if module.startswith("answerback_bridge."):
            return module.removeprefix("answerback_bridge.")
        return f"<absolute>.{module}"
    package = list(relative.parent.parts)
    keep = len(package) - (node.level - 1)
    if keep < 0:
        return module
    return ".".join([*package[:keep], *module.split(".")]) if module else ".".join(package[:keep])


def _provider_imports(path: Path, relative: Path | None = None) -> list[str]:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    importer = relative or path.relative_to(SOURCE_ROOT)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("answerback_bridge."):
                    continue
                if canonical := _canonical_provider(alias.name):
                    found.append(f"{canonical}:<module>")
        elif isinstance(node, ast.ImportFrom):
            module = _from_module(importer, node)
            if canonical := _canonical_provider(module):
                found.extend(f"{canonical}:{alias.name}" for alias in node.names)
                continue
            for alias in node.names:
                imported_module = ".".join(part for part in (module, alias.name) if part)
                if canonical := _canonical_provider(imported_module):
                    found.append(f"{canonical}:<module>")
    return sorted(found)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("from .providers import herdr", "providers:herdr"),
        ("from .providers.herdr.core import Core", "providers.herdr.core:Core"),
        (
            "import answerback_bridge.providers.herdr.core",
            "providers.herdr.core:<module>",
        ),
    ],
)
def test_provider_import_parser_recognizes_supported_ast_forms(
    tmp_path: Path, statement: str, expected: str
) -> None:
    path = tmp_path / "importer.py"
    path.write_text(statement)
    assert _provider_imports(path, Path("importer.py")) == [expected]


def test_provider_import_parser_resolves_implementation_compatibility_alias(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scratch.py"
    path.write_text("from .workers import Workers\n")
    assert _provider_imports(path, Path("scratch.py")) == ["providers.kiro.workers:Workers"]


def test_provider_import_parser_does_not_mistake_top_level_module_for_compatibility_alias(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scratch.py"
    path.write_text("from workers import Workers\nimport workers\n")
    assert _provider_imports(path, Path("scratch.py")) == []


def test_kernel_provider_imports_match_step_12_allowlist() -> None:
    observed = {
        str(path.relative_to(SOURCE_ROOT)): imports
        for path in KERNEL_IMPORTERS
        if (imports := _provider_imports(path))
    }
    assert observed == PROVIDER_IMPORTS_AFTER_STEP_12
