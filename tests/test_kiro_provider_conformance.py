"""Kiro opts in to the shipped third-party provider conformance contract."""

from pathlib import Path

import answerback_bridge
from answerback.testing.conformance import (
    ProviderConformance,
    ProviderConformanceRunner,
    StoreHookReconcileRefusal,
    StoreHooksConformance,
)
from answerback_bridge.providers.kiro import kiro_plugin


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
        )
    )
