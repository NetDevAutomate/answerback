"""Golden identities and import boundaries for kernel digest helpers."""

import ast
from pathlib import Path
from typing import Any

import pytest
from composition import pick

from answerback_bridge.config import Config, routing_digest
from answerback_bridge.digests import (
    KEY,
    PEER,
    canonical,
    digest,
    namespace_for_config,
    unique_object,
)

NESTED_DIGEST = (
    "b4bcdf18cde0863a30d38e2a09ed6c969d42759752388fd7aec0ec7ac55346a8"  # pragma: allowlist secret
)
UNICODE_DIGEST = (
    "5de4c6403202abfe8f330c9339880a88e07e1903f77055f03b635494a78e6846"  # pragma: allowlist secret
)
FLOATS_DIGEST = (
    "b7f1d0e10f8ebd859e0ed49ff738990715c7ca753979cb54fb0e7daccd9b1895"  # pragma: allowlist secret
)
# A routing digest covers every loaded provider's block, so the public composition (no plugin
# block) and the internal one (the block at its defaults) hash a default config differently;
# the internal values are the pre-split ones, the public ones were computed at the step-11
# tip. Published configuration digests, not credentials.
EMPTY_CONFIG_DIGEST = pick(
    "b0aac882a83b6f17795dd9b04fa99f2a8748bf9d0b870c6c93d7b7849025e564",  # pragma: allowlist secret
    "b151a5f958d61c283a99b461d689e728fd9ebc57a1c0514b18c4ba7c2fdb0524",  # pragma: allowlist secret
)
PROVIDERS_CONFIG_DIGEST = pick(
    "aaffcce761d5d1bb329648be19cf9d2e0ebb13e6b41d8cfe43b4c826ac527ea6",  # pragma: allowlist secret
    "76ba554ee79670a708ccf7e3407e314735d039b91ecf96148a908070e75513ab",  # pragma: allowlist secret
)


@pytest.mark.parametrize(
    ("value", "expected_canonical", "expected_digest"),
    [
        (
            {"z": [3, {"b": False, "a": None}], "a": {"beta": "snow"}},
            '{"a":{"beta":"snow"},"z":[3,{"a":null,"b":false}]}',
            NESTED_DIGEST,
        ),
        (
            {"message": "Zażółć gęślą jaźń 🌉"},
            '{"message":"Zażółć gęślą jaźń 🌉"}',
            UNICODE_DIGEST,
        ),
        (
            {"negative_zero": -0.0, "small": 1.25e-7, "whole": 2.0},
            '{"negative_zero":-0.0,"small":1.25e-07,"whole":2.0}',
            FLOATS_DIGEST,
        ),
    ],
)
def test_canonical_and_digest_bytes_are_unchanged(
    value: Any, expected_canonical: str, expected_digest: str
) -> None:
    assert canonical(value) == expected_canonical
    assert digest(value) == expected_digest


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            Config(),
            EMPTY_CONFIG_DIGEST,
        ),
        (
            Config(
                provider_selection=["kiro", "claude_code"],
                kiro_workspaces={"worker": "/work/雪"},
                claude_code_workspaces={"reviewer": "/work/雪"},
            ),
            PROVIDERS_CONFIG_DIGEST,
        ),
    ],
)
def test_routing_digest_bytes_are_unchanged(config: Config, expected: str) -> None:
    assert routing_digest(config) == expected


def test_namespace_for_config_bytes_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = Path("/tmp/x.toml")
    # macOS resolves /tmp through /private; pin that base input on every test platform.
    monkeypatch.setattr(Path, "resolve", lambda _path: Path("/private/tmp/x.toml"))

    assert namespace_for_config(config_path) == "ab977eab5872ab"


def test_bus_modules_reexport_kernel_helpers() -> None:
    from answerback_bridge.bus import config as bus_config
    from answerback_bridge.bus import model as bus_model

    assert bus_model.canonical is canonical
    assert bus_model.digest is digest
    assert bus_model.unique_object is unique_object
    assert bus_model.PEER is PEER
    assert bus_model.KEY is KEY
    assert bus_config.namespace_for_config is namespace_for_config
    assert bus_config.routing_digest is routing_digest


def test_kernel_importers_keep_only_explicit_bus_concept_dependencies() -> None:
    source_root = Path(__file__).parents[1] / "src" / "answerback_bridge"
    expected = {
        "config.py": [],
        "digests.py": [],
        "tasks.py": [],
        "retention.py": [],
        "assignments.py": [],
        "maintenance.py": [],
        "service.py": [],
        "maintenance_policy.py": [],
    }

    for module, expected_imports in expected.items():
        tree = ast.parse((source_root / module).read_text(), filename=module)
        imports = _bus_imports(tree)
        assert imports == expected_imports, f"{module} imports unexpected bus modules: {imports}"


@pytest.mark.parametrize("level", [1, 2])
def test_bus_import_scanner_detects_relative_package_imports(level: int) -> None:
    source = f"from {'.' * level} import bus"

    assert _bus_imports(ast.parse(source)) == [("", ("bus",))]


def _bus_imports(tree: ast.AST) -> list[tuple[str, tuple[str, ...]]]:
    imports: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "answerback_bridge.bus" or alias.name.startswith(
                    "answerback_bridge.bus."
                ):
                    imports.append((alias.name, ()))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            absolute_bus = module == "answerback_bridge.bus" or module.startswith(
                "answerback_bridge.bus."
            )
            relative_bus = node.level > 0 and (
                module == "bus"
                or module.startswith("bus.")
                or (node.module is None and any(alias.name == "bus" for alias in node.names))
            )
            if absolute_bus or relative_bus:
                imports.append((module, tuple(alias.name for alias in node.names)))
    return imports
