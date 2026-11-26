"""Kiro opts in to the shipped third-party provider conformance contract."""

from pathlib import Path
from typing import Any

import pytest

import answerback.testing.conformance as conformance
import answerback_bridge
from answerback.testing.conformance import (
    ProviderConformance,
    ProviderConformanceRunner,
    StoreHookReconcileRefusal,
    StoreHooksConformance,
)
from answerback_bridge.adapters.contracts import Peer, Submission, TaskReceipt
from answerback_bridge.config import Config
from answerback_bridge.plugin import kernel_context
from answerback_bridge.providers.kiro import kiro_plugin
from answerback_bridge.providers.kiro.adapter import KiroAdapter
from answerback_bridge.providers.kiro.workers import Workers
from answerback_bridge.tasks import TaskStore
from answerback_bridge.worker_runtime import Job


class ReservingWorkers(Workers):
    """The real Kiro admission and reservation, with no ACP process behind them."""

    async def _run(self, task_id: str, alias: str, cwd: str, message: str, job: Job) -> None:
        return None


async def kiro_subject_probe(
    store: TaskStore, submission: Submission, workers_type: type[Workers] = ReservingWorkers
) -> TaskReceipt:
    workspace = store.path.parent / "workspace"
    workspace.mkdir(exist_ok=True)
    config = Config("conformance", kiro_workspaces={"conformance": str(workspace)})
    config.select_provider("kiro", True)
    config_path = store.path.parent / "config.json"
    workers = workers_type(config, config_path, store)
    adapter = KiroAdapter(kernel_context(config, config_path, store), lambda: workers)
    peer = Peer.from_record(
        {
            "peer_id": "kiro:conformance",
            "provider": "kiro",
            "target": "conformance",
            "project": str(workspace),
        }
    )
    try:
        return await adapter.submit(await adapter.prepare(peer), submission, guard=lambda: None)
    finally:
        await workers.close()


def test_kiro_provider_conformance(
    answerback_provider_conformance: ProviderConformanceRunner,
) -> None:
    answerback_provider_conformance(
        ProviderConformance(
            factory=kiro_plugin,
            source_root=Path(answerback_bridge.__file__).resolve().parent,
            config={
                "binary": "kiro-conformance",
                "workspaces": {"conformance": "/tmp/answerback-conformance"},
                "active_budget": 120,
            },
            rpc=frozenset({"kiro_register", "kiro_submit", "kiro_sessions"}),
            cli=frozenset({"kiro-register", "kiro-sessions"}),
            mcp=frozenset({"kiro_sessions"}),
            events=frozenset(),
            store_hooks=StoreHooksConformance(
                project_cases=(
                    ({"cwd": "/tmp/answerback-conformance"}, "/tmp/answerback-conformance"),
                ),
                reconcilable_cases=(({"status": "uncertain", "metadata": {}}, False),),
                reconcile_refusals=(
                    StoreHookReconcileRefusal(
                        task={"status": "uncertain", "metadata": {}},
                        match="Receipt is not reconcilable",
                    ),
                ),
            ),
            subject_probe=kiro_subject_probe,
        )
    )


class DroppingWorkers(ReservingWorkers):
    """As a provider that does not pass the submission's subject to reserve."""

    async def submit(self, *args: Any, subject: str | None = None, **kwargs: Any) -> dict:
        return await super().submit(*args, **kwargs)


class InventingWorkers(ReservingWorkers):
    """As a provider that stores a subject the submission did not carry."""

    async def submit(self, *args: Any, subject: str | None = None, **kwargs: Any) -> dict:
        return await super().submit(*args, subject=subject or "invented", **kwargs)


def subject_declaration(workers_type: type[Workers]) -> ProviderConformance:
    async def probe(store: TaskStore, submission: Submission) -> TaskReceipt:
        return await kiro_subject_probe(store, submission, workers_type)

    return ProviderConformance(
        factory=kiro_plugin,
        source_root=Path(answerback_bridge.__file__).resolve().parent,
        config=None,
        subject_probe=probe,
    )


@pytest.mark.parametrize(
    ("workers_type", "label"), [(DroppingWorkers, "with"), (InventingWorkers, "without")]
)
def test_the_subject_case_refuses_a_provider_that_does_not_carry_the_subject(
    tmp_path: Path, workers_type: type[Workers], label: str
) -> None:
    with pytest.raises(AssertionError, match=f"for a submission {label} a subject"):
        conformance._subject(subject_declaration(workers_type), tmp_path)


async def test_the_subject_case_runs_from_a_running_loop(tmp_path: Path) -> None:
    conformance._subject(subject_declaration(ReservingWorkers), tmp_path)


def test_the_subject_case_skips_without_a_probe(tmp_path: Path) -> None:
    declaration = ProviderConformance(
        factory=kiro_plugin,
        source_root=Path(answerback_bridge.__file__).resolve().parent,
        config=None,
    )
    with pytest.raises(pytest.skip.Exception, match="no subject_probe declared"):
        conformance._check_conformance_case("subject", declaration, tmp_path)
