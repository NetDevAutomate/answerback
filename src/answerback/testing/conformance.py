"""Opt-in pytest support for an Answerback provider package.

A consumer supplies :class:`ProviderConformance` to the
``answerback_provider_conformance`` fixture from one test. The checks use only public,
shipped Answerback APIs and the provider's own source tree; they never import Answerback's
repository test suite.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import concurrent.futures
import inspect
import json
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.adapters.contracts import Submission, TaskReceipt
from answerback_bridge.config import SCHEMAS, Config
from answerback_bridge.plugin import PLUGIN_API, ProviderPlugin, StoreHooks, check_api_version
from answerback_bridge.provider_config import ProviderConfig
from answerback_bridge.tasks import TaskStore

_UNKNOWN_PROVIDER = "answerback_conformance_unknown"
_SUBJECT = "answerback-conformance-subject"

# Submits one `Submission` through the provider's own adapter against `store` (with the
# provider's fakes for its transport) and returns the receipt `submit` returned.
SubjectProbe = Callable[[TaskStore, Submission], Coroutine[Any, Any, TaskReceipt]]


@dataclass(frozen=True, slots=True)
class StoreHookReconcileRefusal:
    """One provider-shaped reconciliation call that store hooks must refuse."""

    task: dict[str, Any]
    match: str
    result: str = "conformance result"
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoreHooksConformance:
    """Provider-owned examples that exercise the store-hook policy.

    ``project_cases`` contains ``(metadata, expected_project)`` pairs,
    ``reconcilable_cases`` contains ``(task, expected_claim)`` pairs, and
    ``reconcile_refusals`` exercises provider-owned rejection behavior. These examples keep
    provider-shaped receipt policy in the provider package while the shared checker owns the
    protocol and assertion logic.
    """

    project_cases: tuple[tuple[Mapping[str, Any], str | None], ...] = ()
    reconcilable_cases: tuple[tuple[dict[str, Any], bool], ...] = ()
    reconcile_refusals: tuple[StoreHookReconcileRefusal, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderConformance:
    """One provider package's public conformance declaration.

    Surface sets are exact: use an empty set to declare that the provider intentionally
    contributes nothing to that surface. When ``config_schema`` is present, ``config`` must be
    a non-default valid nested block; it is round-tripped beside an unknown provider block. A
    provider with no settings passes ``None``. ``source_root`` is the directory against which
    each receipt reason rule's ``sources`` paths are resolved.
    """

    factory: Callable[[], ProviderPlugin]
    source_root: Path
    config: Mapping[str, Any] | None
    rpc: frozenset[str] = field(default_factory=frozenset)
    cli: frozenset[str] = field(default_factory=frozenset)
    mcp: frozenset[str] = field(default_factory=frozenset)
    events: frozenset[str] = field(default_factory=frozenset)
    store_hooks: StoreHooksConformance | None = None
    # A provider whose adapter reserves receipts declares one; the `subject` case checks
    # that the receipt holds the submission's subject. None skips that case.
    subject_probe: SubjectProbe | None = None


def _assert_names(label: str, actual: set[str], expected: frozenset[str]) -> None:
    assert actual == expected, f"{label} drifted: expected {sorted(expected)}, got {sorted(actual)}"


def _plugin_shape(plugin: ProviderPlugin) -> str:
    check_api_version(plugin)
    definition = plugin.definition
    registration = definition.registration
    assert isinstance(registration, str) and registration, "provider registration must be nonempty"
    assert definition.name, f"provider {registration} definition name must be nonempty"
    assert isinstance(plugin.capabilities, frozenset), (
        f"provider {registration} capabilities must be a frozenset"
    )
    assert all(isinstance(item, str) and item for item in plugin.capabilities), (
        f"provider {registration} capabilities must contain nonempty strings"
    )
    schema = plugin.config_schema
    if schema is not None:
        assert isinstance(schema, type) and issubclass(schema, ProviderConfig), (
            f"provider {registration} config_schema must be a ProviderConfig subclass or None"
        )
        assert schema.provider == registration, (
            f"provider {registration} config schema declares provider {schema.provider!r}"
        )
        schema_fields = {item.name for item in fields(schema)}
        for dotted in definition.routing_fields:
            provider, _, name = dotted.partition(".")
            assert provider == registration and name in schema_fields, (
                f"provider {registration} routing field {dotted!r} names no config field"
            )
        digest_fields = [dotted for dotted, _key in definition.digest_keys]
        assert len(digest_fields) == len(set(digest_fields)), (
            f"provider {registration} repeats a routing digest field"
        )
        if definition.registration_binary_field is not None:
            assert definition.registration_binary_field in schema_fields, (
                f"provider {registration} registration binary field "
                f"{definition.registration_binary_field!r} names no config field"
            )
        for dotted, child in definition.ignored_child_fields:
            provider, _, name = dotted.partition(".")
            assert provider == registration and name in schema_fields and child, (
                f"provider {registration} ignored child {dotted!r}/{child!r} is invalid"
            )
        for dotted, _default in definition.legacy_neutral_defaults:
            provider, _, name = dotted.partition(".")
            assert provider == registration and name in schema_fields, (
                f"provider {registration} legacy default {dotted!r} names no config field"
            )
    elif definition.routing_fields or definition.digest_keys or definition.legacy_neutral_defaults:
        raise AssertionError(
            f"provider {registration} has no config schema but declares config-backed policy"
        )
    registrar = plugin.registrar()
    if registrar is not None:
        assert isinstance(registrar.name, str) and registrar.name, (
            f"provider {registration} registrar name must be nonempty"
        )
        for method in (
            "install_path",
            "migration_path",
            "install",
            "migrate",
            "install_locks",
            "validate_install",
            "apply_install",
            "migration_locks",
            "validate_migration",
            "apply_migration",
            "rollback_migration",
            "skill_destination",
            "skill_native",
            "supports_skill",
            "skill_skip_note",
            "install_skill",
        ):
            assert callable(getattr(registrar, method, None)), (
                f"provider {registration} registrar must export {method}()"
            )
    status_order = getattr(plugin, "status_order", None)
    assert status_order is None or (
        isinstance(status_order, int) and not isinstance(status_order, bool) and status_order >= 0
    ), f"provider {registration} status_order must be a non-negative int or None"
    assert plugin.api_version == PLUGIN_API
    return registration


def _surface_names(plugin: ProviderPlugin, declaration: ProviderConformance) -> None:
    rpc = plugin.rpc()
    assert isinstance(rpc, Mapping), "provider rpc() must return a mapping"
    assert all(callable(handler) for handler in rpc.values()), "every RPC handler must be callable"
    _assert_names("RPC surface", set(rpc), declaration.rpc)

    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    bound: set[str] = set()

    def bind(command: argparse.ArgumentParser, handler: Any) -> None:
        assert callable(handler), f"CLI handler for {command.prog!r} must be callable"
        bound.add(command.prog.rpartition(" ")[2])

    plugin.cli(subparsers, bind)
    choices = set(subparsers.choices)
    assert bound == choices, "every provider CLI parser must bind exactly one handler"
    _assert_names("CLI surface", choices, declaration.cli)

    async def unused_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"MCP tool {method!r} ran during declaration inspection: {params!r}")

    tool_names: set[str] = set()
    for tool in plugin.mcp_tools():
        function = tool.build(unused_call)
        name = getattr(function, "__name__", None)
        description = getattr(function, "__doc__", None)
        assert isinstance(name, str) and name, "MCP tool must have a function name"
        assert inspect.iscoroutinefunction(function), f"MCP tool {name!r} must be async"
        assert description, f"MCP tool {name!r} must have a description docstring"
        assert name not in tool_names, f"duplicate MCP tool {name!r}"
        tool_names.add(name)
    _assert_names("MCP surface", tool_names, declaration.mcp)

    event_names: set[str] = set()
    for source in plugin.event_sources():
        assert isinstance(source.provider, str) and source.provider, (
            "event source provider must be a nonempty string"
        )
        assert callable(source.tail), f"event source {source.provider!r} must define tail()"
        assert source.provider not in event_names, f"duplicate event source {source.provider!r}"
        event_names.add(source.provider)
    _assert_names("event surface", event_names, declaration.events)


def _store_hooks(plugin: ProviderPlugin, declaration: ProviderConformance, directory: Path) -> None:
    hooks: StoreHooks | None = plugin.store_hooks()
    cases = declaration.store_hooks
    if cases is None:
        assert hooks is None, "provider unexpectedly declares store hooks"
        return
    assert hooks is not None, "provider must declare store hooks"
    for method in ("is_reconcilable", "reconcile", "project_of"):
        assert callable(getattr(hooks, method, None)), f"store hooks must define {method}()"
    for metadata, expected in cases.project_cases:
        assert hooks.project_of(metadata) == expected
    for task, expected in cases.reconcilable_cases:
        assert hooks.is_reconcilable(task) is expected
    if cases.reconcile_refusals:
        store = TaskStore(directory / f"{plugin.definition.registration}-hooks.sqlite")
        for case in cases.reconcile_refusals:
            with pytest.raises(ValueError, match=case.match):
                hooks.reconcile(
                    store,
                    case.task,
                    result=case.result,
                    evidence=case.evidence,
                )


class _LogCapture(logging.Handler):
    """Capture rendered warning messages without depending on pytest's own fixtures."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _load_with_unknown_warning(path: Path) -> Config:
    logger = logging.getLogger("answerback_bridge.config")
    capture = _LogCapture()
    logger.addHandler(capture)
    try:
        config = Config.load(path)
    finally:
        logger.removeHandler(capture)
    assert any(
        _UNKNOWN_PROVIDER in message and "disabled" in message for message in capture.messages
    ), "loading an unknown provider must log that it is disabled while preserving its settings"
    return config


def _config_roundtrip(
    plugin: ProviderPlugin, declaration: ProviderConformance, directory: Path
) -> None:
    registration = plugin.definition.registration
    schema = plugin.config_schema
    assert schema is not None
    assert declaration.config is not None
    assert SCHEMAS.get(registration) is schema, (
        f"provider {registration} is not installed through an answerback.providers entry point"
    )
    unknown_block = {"token": "kept", "nested": {"enabled": True}}
    path = directory / f"{registration}-provider-conformance.json"
    path.write_text(
        json.dumps(
            {
                "provider_selection": [registration, _UNKNOWN_PROVIDER],
                "providers": {
                    registration: dict(declaration.config),
                    _UNKNOWN_PROVIDER: unknown_block,
                },
            }
        )
    )
    config = _load_with_unknown_warning(path)
    assert config.provider_enabled(registration)
    assert not config.provider_enabled(_UNKNOWN_PROVIDER)
    assert _UNKNOWN_PROVIDER in config.disabled_providers()
    assert config.unknown_providers[_UNKNOWN_PROVIDER] == unknown_block
    block = config.providers[registration]
    for name, expected in declaration.config.items():
        assert getattr(block, name) == expected, f"providers.{registration}.{name} did not load"

    config.save(path)
    saved = json.loads(path.read_text())
    assert registration in saved["providers"], "known provider config must remain nested"
    for name, expected in declaration.config.items():
        assert saved["providers"][registration][name] == expected
    assert saved["providers"][_UNKNOWN_PROVIDER] == unknown_block
    assert _UNKNOWN_PROVIDER in saved["provider_selection"]
    assert registration in saved["provider_selection"]
    reloaded = _load_with_unknown_warning(path)
    assert not reloaded.provider_enabled(_UNKNOWN_PROVIDER)
    assert _UNKNOWN_PROVIDER in reloaded.disabled_providers()
    assert reloaded.unknown_providers[_UNKNOWN_PROVIDER] == unknown_block
    for name, expected in declaration.config.items():
        assert getattr(reloaded.providers[registration], name) == expected, (
            f"providers.{registration}.{name} did not round-trip"
        )


def _run_probe(probe: SubjectProbe, store: TaskStore, submission: Submission) -> TaskReceipt:
    """Run the probe on its own loop, on a thread when the caller's loop is running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(probe(store, submission))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(probe(store, submission))).result()


def _subject(declaration: ProviderConformance, directory: Path) -> None:
    """A receipt holds exactly the submission's subject: S for S, none for None."""
    probe = declaration.subject_probe
    assert probe is not None
    registration = declaration.factory().definition.registration
    for label, subject in (("with", _SUBJECT), ("without", None)):
        store = TaskStore(directory / f"{registration}-subject-{label}" / "tasks.sqlite")
        submission = Submission("Answerback conformance", f"conformance-{label}", subject)
        receipt = _run_probe(probe, store, submission)
        stored = store.get(receipt.task_id)
        assert stored["request_key"] == submission.request_id, (
            f"provider {registration} returned a receipt for another request"
        )
        assert stored["subject"] == subject, (
            f"provider {registration} stored subject {stored['subject']!r} for a submission "
            f"{label} a subject; pass `subject=submission.subject` to `TaskStore.reserve`"
        )


def _string_constants(path: Path) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _receipt_reason_drift(plugin: ProviderPlugin, source_root: Path) -> None:
    registration = plugin.definition.registration
    root = source_root.resolve()
    assert root.is_dir(), f"provider {registration} source root does not exist: {root}"
    rules = tuple(plugin.receipt_reasons())
    order = tuple(plugin.receipt_reason_order())
    assert not order or len(order) == len(rules), (
        f"provider {registration} receipt reason order has {len(order)} positions for "
        f"{len(rules)} rules"
    )
    for rule in rules:
        assert rule.code and rule.parts, f"provider {registration} has an empty receipt reason"
        assert rule.sources, f"receipt reason {rule.code!r} must declare writer sources"
        for relative, counts in rule.sources.items():
            path = (root / relative).resolve()
            assert path.is_relative_to(root), (
                f"receipt reason source escapes source root: {relative}"
            )
            assert path.is_file(), f"receipt reason source does not exist: {relative}"
            assert len(counts) == len(rule.parts), (
                f"receipt reason {rule.code!r} has one count per fixed fragment"
            )
            constants = _string_constants(path)
            for fragment, expected in zip(rule.parts, counts, strict=True):
                if expected == 0:
                    continue
                found = (
                    sum(value.startswith(fragment) for value in constants)
                    if rule.begins
                    else constants.count(fragment)
                )
                assert found >= expected, (
                    f"{relative}: receipt reason {rule.code!r} expected at least {expected} "
                    f"occurrences of {fragment!r}, found {found}"
                )


def _config_contract(
    plugin: ProviderPlugin, declaration: ProviderConformance, directory: Path
) -> None:
    registration = plugin.definition.registration
    if plugin.config_schema is None:
        assert declaration.config is None, (
            f"provider {registration} has no config schema, so its config sample must be None"
        )
        return
    assert declaration.config, f"provider {registration} must supply a non-default config sample"
    _config_roundtrip(plugin, declaration, directory)


def check_provider_conformance(declaration: ProviderConformance, directory: Path) -> None:
    """Run the shipped provider contract checks in ``directory``.

    Pytest consumers normally use ``answerback_provider_conformance`` instead. This function
    is public for test frameworks that want to call the same contract directly.
    """

    plugin = declaration.factory()
    _plugin_shape(plugin)
    _config_contract(plugin, declaration, directory)
    _surface_names(plugin, declaration)
    _store_hooks(plugin, declaration, directory)
    _receipt_reason_drift(plugin, declaration.source_root)
    if declaration.subject_probe is not None:
        _subject(declaration, directory)


ProviderConformanceRunner = Callable[[ProviderConformance], None]
_CONFORMANCE_CASES = (
    "plugin-shape",
    "surfaces",
    "config",
    "store-hooks",
    "receipt-reasons",
    "subject",
)


def _check_conformance_case(case: str, declaration: ProviderConformance, directory: Path) -> None:
    plugin = declaration.factory()
    if case == "plugin-shape":
        _plugin_shape(plugin)
    elif case == "surfaces":
        _surface_names(plugin, declaration)
    elif case == "config":
        _config_contract(plugin, declaration, directory)
    elif case == "store-hooks":
        _store_hooks(plugin, declaration, directory)
    elif case == "receipt-reasons":
        _receipt_reason_drift(plugin, declaration.source_root)
    elif case == "subject":
        if declaration.subject_probe is None:
            pytest.skip("no subject_probe declared; a provider that reserves receipts needs one")
        _subject(declaration, directory)
    else:  # pragma: no cover - fixture parameters are a closed tuple
        raise AssertionError(f"Unknown provider conformance case {case!r}")


@pytest.fixture(params=_CONFORMANCE_CASES, ids=_CONFORMANCE_CASES)
def answerback_provider_conformance(
    tmp_path: Path, request: pytest.FixtureRequest
) -> ProviderConformanceRunner:
    """Run one parametrised provider contract case for a consumer declaration."""

    case = str(request.param)

    def run(declaration: ProviderConformance) -> None:
        _check_conformance_case(case, declaration, tmp_path)

    return run
