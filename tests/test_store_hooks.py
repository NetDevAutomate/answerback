"""Store hooks: a plugin's receipt-store policy reaches the kernel only through the registry."""

import asyncio
import json
import re
import time
import tokenize
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from composition import PLUGIN_PROVIDERS, contributed
from test_adapter_extension import FixtureAdapter

from answerback_bridge import codex
from answerback_bridge.adapters.builtins import builtin_plugins, builtin_store_hooks
from answerback_bridge.adapters.catalog import ProviderDefinition, definition, definitions
from answerback_bridge.adapters.codex import CodexAdapter, DesktopStoreHooks
from answerback_bridge.adapters.contracts import ProviderAdapter
from answerback_bridge.adapters.factory import build_adapters
from answerback_bridge.config import Config
from answerback_bridge.maintenance import Maintenance, RetentionSelection
from answerback_bridge.plugin import (
    PLUGIN_API,
    KernelContext,
    ProjectPathHooks,
    ProviderPlugin,
    StoreHooks,
    kernel_context,
)
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import StoreHookConflict, StoreHookRegistry, Task, TaskStore

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "answerback_bridge"
# The provider word the kernel store still carries; the public-tree gate denies the rest.
PROVIDER_WORD = re.compile(r"\b(desktop)\b", re.IGNORECASE)
# The exact source lines of `tasks.py` that still name a provider, in file order. Both are
# pre-existing kernel compatibility text rather than receipt-store policy.
# One entry per provider TOKEN (source line, token text), in file order: a line that names a
# provider twice is two occurrences, so a second token on an allowlisted line is caught too.
TASKS_LEGACY_LINES = [
    # Compatibility query for the Codex history reader (`reconcilable_ids`); not a rule.
    ('return self.observation_ids("desktop", "last_history_attempt", limit)', '"desktop"'),
    # Worker-completion refusal text (`complete_from_worker`); the errors catalogue pins it.
    (
        '"Desktop results require a verified turn; manual completion refused"',
        '"Desktop results require a verified turn; manual completion refused"',
    ),
]


class FixtureHooks(StoreHooks):
    """Claims failed fixture receipts whose result says so; corrects them on one proof."""

    def __init__(self, kind: str = "fixture", *, claim_all_failed: bool = False) -> None:
        self.kind = kind
        self.claim_all_failed = claim_all_failed
        self.writes = 0

    def is_reconcilable(self, task: Task) -> bool:
        return (
            task["status"] == "failed"
            and (self.claim_all_failed or task["metadata"].get("kind") == self.kind)
            and task["result"] == "fixture lost the answer"
            and not task["metadata"].get("identity_invalidated")
        )

    def reconcile(
        self, store: TaskStore, task: Task, *, result: str, evidence: Mapping[str, Any]
    ) -> Task:
        if not self.is_reconcilable(task):
            raise ValueError("Reconciliation requires a failed fixture receipt")
        if evidence.get("proof") != task["metadata"].get("proof"):
            raise ValueError("Reconciliation requires the receipt's durable fixture proof")
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if row is None or store.decode(row, db) != task:
                raise ValueError("Receipt changed during reconciliation; inspect it again")
            metadata = dict(task["metadata"])
            now = time.time()
            metadata["fixture_reconciliation"] = {
                "previous_status": task["status"],
                "previous_result": task["result"],
                "evidence": dict(evidence),
                "reconciled_at": now,
            }
            db.execute(
                "UPDATE tasks SET status='completed', result=?, detail=NULL, metadata=?, "
                "updated=? WHERE id=?",
                (result, json.dumps(metadata), now, task["id"]),
            )
        self.writes += 1
        return store.get(task["id"])

    def project_of(self, metadata: Mapping[str, Any]) -> str | None:
        return metadata.get("workspace")


class RecordingDesktopHooks(DesktopStoreHooks):
    """Counts writes selected from the store registry."""

    def __init__(self) -> None:
        self.writes: list[tuple[Task, str, Mapping[str, Any]]] = []

    def reconcile(
        self, store: TaskStore, task: Task, *, result: str, evidence: Mapping[str, Any]
    ) -> Task:
        self.writes.append((task, result, evidence))
        return super().reconcile(store, task, result=result, evidence=evidence)


class MuteHooks(StoreHooks):
    """A provider with no store policy: the protocol's defaults."""


class ReconcilingFixtureAdapter(FixtureAdapter):
    """The adapter half: verifies against the provider's records, writes through the store."""

    def __init__(self, ctx: KernelContext, kind: str) -> None:
        super().__init__(ctx.config, ctx.config_path, ctx.store)
        self.kind = kind
        self.verified: list[str] = []

    async def reconcile(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        if task["status"] == "completed" and task["metadata"].get("fixture_reconciliation"):
            return task
        self.verified.append(task_id)
        return self.store.reconcile(
            task, result="FIXTURE_RECOVERED", evidence={"proof": "witness-1"}
        )


class FixtureStorePlugin(ProviderPlugin):
    api_version = PLUGIN_API
    capabilities = frozenset({"discovery", "send"})
    config_schema = None

    def __init__(self, kind: str = "fixture", hooks: StoreHooks | None = None) -> None:
        self.definition = ProviderDefinition(kind, kind, recovery_states=("running",))
        self.hooks = hooks
        self.adapter: ReconcilingFixtureAdapter | None = None

    def build(self, ctx: KernelContext) -> ProviderAdapter:
        self.adapter = ReconcilingFixtureAdapter(ctx, self.definition.name)
        self.adapter.name = self.adapter.registration = self.definition.name  # type: ignore[misc]
        return self.adapter

    def store_hooks(self) -> StoreHooks | None:
        return self.hooks


def failed_fixture_task(store: TaskStore, kind: str = "fixture", **metadata: Any) -> Task:
    row, created = store.reserve(
        f"{kind}:one",
        "Recover me",
        f"recover-{kind}",
        {"kind": kind, "proof": "witness-1", **metadata},
    )
    assert created
    return store.update(row["id"], "failed", result="fixture lost the answer")


def composed(path: Path, *plugins: ProviderPlugin) -> Bridge:
    bridge = Bridge(Config(provider_selection=[]), path)
    bridge.adapters = build_adapters(bridge.kernel, list(plugins))
    return bridge


async def test_fake_plugin_hooks_claim_and_reconcile_through_the_bridge(tmp_path: Path) -> None:
    hooks = FixtureHooks()
    plugin = FixtureStorePlugin(hooks=hooks)
    bridge = composed(tmp_path / "config.json", plugin)
    try:
        assert bridge.store.hooks.get("fixture") is hooks
        task = failed_fixture_task(bridge.store)
        assert bridge.store.hooks.claimant(task) is hooks
        corrected = await bridge.reconcile_task(task["id"])
        assert corrected["status"] == "completed"
        assert corrected["result"] == "FIXTURE_RECOVERED"
        audit = corrected["metadata"]["fixture_reconciliation"]
        assert audit["previous_status"] == "failed"
        assert audit["previous_result"] == "fixture lost the answer"
        assert audit["evidence"] == {"proof": "witness-1"}
        assert hooks.writes == 1
        assert plugin.adapter is not None and plugin.adapter.verified == [task["id"]]
        # A corrected receipt is no longer claimed and the adapter returns it unchanged.
        assert bridge.store.hooks.claimant(bridge.store.get(task["id"])) is None
        assert await bridge.reconcile_task(task["id"]) == corrected
        assert hooks.writes == 1
    finally:
        await bridge.close()


class PlainFixturePlugin(FixtureStorePlugin):
    """Carries store hooks but an adapter without a reconciler."""

    def build(self, ctx: KernelContext) -> ProviderAdapter:
        adapter = FixtureAdapter(ctx.config, ctx.config_path, ctx.store)
        adapter.name = adapter.registration = self.definition.name  # type: ignore[misc]
        return adapter


async def test_a_receipt_no_hook_claims_is_refused(tmp_path: Path) -> None:
    plugin = FixtureStorePlugin(hooks=FixtureHooks())
    plain = PlainFixturePlugin("plain", hooks=FixtureHooks("plain"))
    bridge = composed(tmp_path / "config.json", plugin, plain)
    try:
        row, _ = bridge.store.reserve("fixture:two", "Settle me", "settled", {"kind": "fixture"})
        task = bridge.store.update(row["id"], "failed", result="operator decision")
        assert bridge.store.hooks.claimant(task) is None
        with pytest.raises(ValueError, match="not reconcilable"):
            await bridge.reconcile_task(task["id"])
        assert bridge.store.get(task["id"]) == task
        # A provider whose adapter has no reconciler is refused as before, hooks or not.
        other = failed_fixture_task(bridge.store, "plain")
        assert bridge.store.hooks.claimant(other) is plain.hooks
        with pytest.raises(ValueError, match="does not support explicit reconciliation"):
            await bridge.reconcile_task(other["id"])
        assert bridge.store.get(other["id"])["status"] == "failed"
    finally:
        await bridge.close()


def test_bare_store_reconciles_nothing_and_hooks_write_under_compare_and_swap(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    task = failed_fixture_task(store)
    assert store.hooks.kinds() == ()
    with pytest.raises(ValueError, match="not reconcilable"):
        store.reconcile(task, result="X", evidence={"proof": "witness-1"})
    hooks = FixtureHooks()
    store.hooks.register("fixture", hooks)
    with pytest.raises(ValueError, match="durable fixture proof"):
        store.reconcile(task, result="X", evidence={"proof": "forged"})
    stale = dict(task)
    with store.connection() as db:
        db.execute("UPDATE tasks SET detail='seen again' WHERE id=?", (task["id"],))
    assert store.get(task["id"]) != stale
    with pytest.raises(ValueError, match="changed during reconciliation"):
        store.reconcile(stale, result="X", evidence={"proof": "witness-1"})
    assert store.get(task["id"])["status"] == "failed"
    fresh = store.reconcile(store.get(task["id"]), result="X", evidence={"proof": "witness-1"})
    assert fresh["status"] == "completed" and hooks.writes == 1


async def test_two_claimants_are_a_composition_error_before_any_provider_work(
    tmp_path: Path,
) -> None:
    first = FixtureStorePlugin("fixture", hooks=FixtureHooks("fixture"))
    greedy = FixtureStorePlugin("greedy", hooks=FixtureHooks("greedy", claim_all_failed=True))
    bridge = composed(tmp_path / "config.json", first, greedy)
    try:
        task = failed_fixture_task(bridge.store)
        with pytest.raises(StoreHookConflict, match=r"fixture, greedy"):
            bridge.store.hooks.claimant(task)
        with pytest.raises(StoreHookConflict):
            bridge.store.hooks.is_reconcilable(task)
        with pytest.raises(StoreHookConflict):
            bridge.store.reconcile(task, result="X", evidence={"proof": "witness-1"})
        with pytest.raises(StoreHookConflict, match=task["id"]):
            await bridge.reconcile_task(task["id"])
        assert first.adapter is not None and first.adapter.verified == []
        assert bridge.store.get(task["id"])["status"] == "failed"
        # One claimant of a kind the greedy hooks do not reach keeps working.
        lone = failed_fixture_task(bridge.store, "greedy")
        assert bridge.store.hooks.claimant(lone) is greedy.hooks
    finally:
        await bridge.close()


async def test_registry_keys_hooks_by_kind_and_replaces_a_kind(tmp_path: Path) -> None:
    registry = StoreHookRegistry()
    one, two = FixtureHooks(), FixtureHooks()
    registry.register("fixture", one)
    registry.register("fixture", two)
    assert registry.kinds() == ("fixture",)
    assert registry.get("fixture") is two
    assert registry.get(None) is None and registry.get(7) is None
    for bad in ("", None, 3):
        with pytest.raises(ValueError, match="receipt kind"):
            registry.register(bad, one)  # type: ignore[arg-type]
    # Composing twice on one store (two Bridges, a rebuilt registry) is idempotent.
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    try:
        before = bridge.store.hooks.kinds()
        build_adapters(bridge.kernel, [FixtureStorePlugin(hooks=FixtureHooks())])
        build_adapters(bridge.kernel, [FixtureStorePlugin(hooks=FixtureHooks())])
        assert bridge.store.hooks.kinds() == (*before, "fixture")
    finally:
        await bridge.close()


def test_project_of_falls_back_to_unknown(tmp_path: Path) -> None:
    registry = StoreHookRegistry()
    registry.register("fixture", FixtureHooks())
    registry.register("nested", ProjectPathHooks("peer", "project"))
    registry.register("mute", MuteHooks())
    assert registry.project_of({"kind": "fixture", "workspace": "/w"}) == "/w"
    assert registry.project_of({"kind": "fixture"}) is None
    assert registry.project_of({"kind": "nested", "peer": {"project": "/p"}}) == "/p"
    assert registry.project_of({"kind": "nested", "peer": "/p"}) is None
    assert registry.project_of({"kind": "nested"}) is None
    # A provider with no project rule, a kind no plugin serves, and no kind at all.
    assert registry.project_of({"kind": "mute", "cwd": "/c"}) is None
    assert registry.project_of({"kind": "unknown", "cwd": "/c"}) is None
    assert registry.project_of({"cwd": "/c"}) is None
    assert registry.project_of({"kind": ["fixture"], "workspace": "/w"}) is None
    # The value is handed back as stored; the kernel validates it where it groups.
    assert registry.project_of({"kind": "fixture", "workspace": 7}) == 7
    with pytest.raises(ValueError, match="metadata keys"):
        ProjectPathHooks()
    assert MuteHooks().is_reconcilable({"status": "failed", "metadata": {}}) is False
    with pytest.raises(ValueError, match="not reconcilable"):
        MuteHooks().reconcile(TaskStore(tmp_path / "t.sqlite"), {}, result="X", evidence={})


def test_builtin_hooks_reproduce_the_project_grouping_the_kernel_had() -> None:
    """Keyed by receipt kind; the per-provider branches `maintenance` used to carry."""
    registry = builtin_store_hooks()
    # The kernel's own kinds, plus an installed plugin's.
    assert set(registry.kinds()) == {
        "kiro",
        "desktop",
        "crew",
        "hc",
        "rex",
        *contributed("STORE_KINDS", ()),
    }
    assert isinstance(registry.get("desktop"), DesktopStoreHooks)
    plugins = {plugin.definition.registration: plugin for plugin in builtin_plugins()}
    for registration, plugin in plugins.items():
        hooks = plugin.store_hooks()
        if hooks is None:
            continue
        row = definition(registration)
        assert row is not None
        if registration in PLUGIN_PROVIDERS:
            assert isinstance(registry.get(row.name), type(hooks))
        else:
            assert registry.get(row.name) is hooks
    cases = {
        ("kiro", "cwd"): "/k",
        **contributed("STORE_PROJECT_CASES", {}),
        ("hc", "peer.project"): "/h",
        ("crew", "peer.project"): "/c",
        ("rex", "binding.foreground_cwd"): "/r",
    }
    for (kind, path), expected in cases.items():
        metadata: dict[str, Any] = {"kind": kind}
        cursor = metadata
        *parents, leaf = path.split(".")
        for key in parents:
            cursor = cursor.setdefault(key, {})
        cursor[leaf] = expected
        assert registry.project_of(metadata) == expected, kind
        assert registry.project_of({"kind": kind}) is None, kind
    for kind in ("desktop", "codex_cli", "claude_code", "grok", "herdr"):
        assert registry.project_of({"kind": kind, "cwd": "/x", "peer": {"project": "/x"}}) is None
    assert {row.name for row in definitions()} >= set(registry.kinds())


async def test_direct_codex_adapter_writes_through_the_registry_claimant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct adapter construction must not bypass a replacement store policy."""
    store = TaskStore(tmp_path / "tasks.sqlite")
    hooks = RecordingDesktopHooks()
    store.hooks.register("desktop", hooks)
    config = Config(provider_selection=["codex"], codex_enabled=True)
    adapter = CodexAdapter(
        kernel_context(config, tmp_path / "config.json", store, locks=[asyncio.Lock()]),
        lambda: None,  # The reconciliation path reads persisted history directly.
    )
    task, _ = store.reserve(
        "desktop:chat",
        "Ping",
        "registry-write",
        {"kind": "desktop", "binding": "discovered", "thread_id": "chat"},
    )
    failed = store.update(task["id"], "failed", result="Codex turn interrupted", turn_id="turn-1")

    class Reader:
        def __init__(self, *_: object) -> None:
            pass

        async def __aenter__(self) -> "Reader":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def scan(self, thread_id: str, **_: object) -> Any:
            assert thread_id == "chat"
            return (
                [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "startedAt": failed["created"],
                        "completedAt": failed["created"] + 1,
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"{codex.task_marker(failed['id'])}\nPing",
                                    }
                                ],
                            },
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "Recovered answer",
                            },
                        ],
                    }
                ],
                None,
            )

    monkeypatch.setattr(codex, "HistoryReader", Reader)
    corrected = await adapter.reconcile(failed["id"])

    assert corrected["status"] == "completed"
    assert hooks.writes == [
        (
            failed,
            "Recovered answer",
            {"completed_at": failed["created"] + 1},
        )
    ]


async def test_bridge_registers_the_builtin_hooks_on_its_store(tmp_path: Path) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    try:
        assert set(bridge.store.hooks.kinds()) == set(builtin_store_hooks().kinds())
        assert isinstance(bridge.store.hooks.get("desktop"), DesktopStoreHooks)
        row, _ = bridge.store.reserve(
            "desktop:one", "Ping", "late-turn", {"kind": "desktop", "alias": "a", "thread_id": "t"}
        )
        failed = bridge.store.update(
            row["id"], "failed", result="Codex turn failed", turn_id="turn"
        )
        assert bridge.store.hooks.claimant(failed) is bridge.store.hooks.get("desktop")
    finally:
        await bridge.close()


def test_desktop_hooks_refuse_evidence_without_completed_at_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic store path reaches `DesktopStoreHooks` with arbitrary evidence.

    The Codex adapter always supplies the verified turn's `completed_at`; a caller of
    `TaskStore.reconcile` need not. A missing key is refused by the hooks themselves, before
    `BEGIN IMMEDIATE`, so the receipt row is untouched and no transaction is opened.
    """
    store = TaskStore(tmp_path / "tasks.sqlite")
    store.hooks = builtin_store_hooks()
    row, _ = store.reserve(
        "desktop:one", "Ping", "late-turn", {"kind": "desktop", "alias": "a", "thread_id": "t"}
    )
    failed = store.update(row["id"], "failed", result="Codex turn failed", turn_id="turn")
    assert store.hooks.claimant(failed) is store.hooks.get("desktop")
    opened: list[str] = []
    real_connection = store.connection

    def counted_connection() -> Any:
        opened.append("connection")
        return real_connection()

    monkeypatch.setattr(store, "connection", counted_connection)
    with pytest.raises(ValueError, match="requires the verified turn's completion time"):
        store.reconcile(failed, result="late answer", evidence={"turn_id": "turn"})
    assert opened == []
    monkeypatch.undo()
    assert store.get(failed["id"]) == failed
    # The same evidence with the key present is the one difference between refusal and write.
    fixed = store.reconcile(
        failed, result="late answer", evidence={"turn_id": "turn", "completed_at": 1234.5}
    )
    assert fixed["status"] == "completed" and fixed["result"] == "late answer"
    assert fixed["metadata"]["desktop_reconciliation"]["completed_at"] == 1234.5


def test_maintenance_counts_unknown_projects_through_the_hooks(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    rows = [
        ("kiro:a", {"kind": "kiro", "cwd": "/k"}),
        # A receipt of a provider no loaded plugin claims (S-7): its project is unknown.
        ("other:b", {"kind": "other", "cwd": "/a"}),
        ("hc:c", {"kind": "hc", "peer": {"project": "/h"}}),
        ("rex:d", {"kind": "rex", "binding": {"foreground_cwd": "/r"}}),
        ("desktop:e", {"kind": "desktop"}),
        ("kiro:f", {"kind": "kiro"}),
    ]
    for index, (target, metadata) in enumerate(rows):
        task, _ = store.reserve(target, "Ping", f"row-{index}", metadata)
        store.update(task["id"], "completed", result="done")
    with store.connection() as db:
        db.execute("UPDATE tasks SET metadata='not json' WHERE target='rex:d'")
        db.execute("UPDATE tasks SET metadata='[1]' WHERE target='hc:c'")
    (tmp_path / "bridge.json").write_text("{}")
    maintenance = Maintenance(tmp_path / "bridge.json")
    assert set(maintenance.hooks.kinds()) == set(builtin_store_hooks().kinds())
    stats = maintenance.stats(RetentionSelection())
    # desktop (no project rule), the unclaimed `other` kind, kiro without cwd, the two
    # unreadable metadata rows.
    assert stats["stores"]["tasks"]["tables"]["tasks"]["project_unknown_rows"] == 5
    # A caller may hand maintenance the hooks of the plugins it loaded.
    empty = Maintenance(tmp_path / "bridge.json", hooks=StoreHookRegistry())
    assert empty.stats()["stores"]["tasks"]["tables"]["tasks"]["project_unknown_rows"] == 6


def provider_mentions(path: Path, segment: str | None = None) -> list[tuple[int, str, str]]:
    """Whole-word provider names in names, strings and comments: (line, line text, token).

    One entry per matching token, in file order; two tokens on one line are two entries.
    """
    source = path.read_text()
    lines = source.splitlines()
    if segment is not None:
        start = source.index(segment)
        first = source[:start].count("\n")
        indent = len(lines[first]) - len(lines[first].lstrip())
        last = first + 1
        while last < len(lines) and (
            not lines[last].strip() or len(lines[last]) - len(lines[last].lstrip()) > indent
        ):
            last += 1
        block = "\n".join(lines[first:last])
        offset = first
    else:
        block, offset = source, 0
    found: list[tuple[int, str, str]] = []
    for token in tokenize.generate_tokens(StringIO(block).readline):
        if token.type in (
            tokenize.NAME,
            tokenize.STRING,
            tokenize.COMMENT,
        ) and PROVIDER_WORD.search(token.string):
            line = offset + token.start[0]
            found.append((line, lines[line - 1].strip(), token.string))
    return found


def test_kernel_store_and_reconcile_dispatch_name_no_provider(tmp_path: Path) -> None:
    """`tasks.py` holds generic dispatch; every line that still names a provider is listed."""
    remaining = provider_mentions(SOURCE_ROOT / "tasks.py")
    unexpected = [entry for entry in remaining if entry[1:] not in TASKS_LEGACY_LINES]
    assert unexpected == [], f"tasks.py names a provider: {unexpected}"
    # Occurrence-exact per token: the same line twice, one fewer, or a second provider
    # token on an allowlisted line is a change to the legacy surface.
    assert [entry[1:] for entry in remaining] == TASKS_LEGACY_LINES, "update the legacy list"
    service = SOURCE_ROOT / "service.py"
    assert provider_mentions(service, "    async def reconcile_task(") == []
    # The scanner sees names, strings and comments alike, and only whole words.
    probe = tmp_path / "probe.py"
    probe.write_text(
        'def f(desktop):  # the Desktop\n    x = desktop_index\n    return "a desktop", DESKTOP_X\n'
    )
    # Line 1 names a provider twice (parameter and comment); line 3 once (`DESKTOP_X` is not
    # a whole word, `desktop_index` on line 2 is not either).
    assert [line for line, *_ in provider_mentions(probe)] == [1, 1, 3]
    assert provider_mentions(probe, "def f(") == provider_mentions(probe)
