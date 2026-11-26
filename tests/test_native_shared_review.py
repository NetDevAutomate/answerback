"""Independent receipt-layer checks using a transport without native processes."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.config import Config
from answerback_bridge.native_protocol import NativeResult
from answerback_bridge.native_workers import NativeWorkers
from answerback_bridge.tasks import TaskStore


class ScriptedTransport:
    def __init__(self, scenario: dict[str, Any], binary, cwd, update, permission, *, home=None):
        self.scenario = scenario
        self.update = update
        self.closed = False
        self.cancel_called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.scenario.get("teardown_error"):
            raise RuntimeError("fixture teardown failed")

    async def open_session(self, session_id=None, mcp_servers=None):
        self.scenario.setdefault("opens", []).append(session_id)
        return session_id or "owned-session"

    async def prompt(self, session_id, message, *, prompt_id):
        self.scenario.setdefault("prompts", []).append(prompt_id)
        if self.scenario.get("update_then_disconnect"):
            await self.update(
                {
                    "sessionUpdate": "turn/started",
                    "session_id": session_id,
                    "turn_id": "native-turn",
                }
            )
            raise ConnectionError("fixture lost final notification")
        if self.scenario.get("hold_until_released"):
            await self.scenario["release"].wait()
            return NativeResult(session_id, "cancelled", None, "native-turn")
        return NativeResult(
            session_id,
            "completed",
            None if self.scenario.get("missing_answer") else "confirmed answer",
            "native-turn",
            {"prompt_id": prompt_id},
        )

    async def cancel(self, session_id):
        self.cancel_called = True


def configured(tmp_path: Path, scenario: dict[str, Any]):
    # Hermetic: a Codex home of the test's own, so native_home() never falls back
    # to CODEX_HOME or the account's ~/.codex. The folder need not exist.
    config = Config(
        codex_cli_workspaces={"project": str(tmp_path)},
        codex_cli_home=str(tmp_path / "native-home"),
    )
    config.select_provider("codex_cli", True)
    path = tmp_path / "config.json"
    config.save(path)
    store = TaskStore(tmp_path / "tasks.sqlite")

    def factory(*args, **kwargs):
        return ScriptedTransport(scenario, *args, **kwargs)

    workers = NativeWorkers("codex_cli", config, path, store, transport_factory=factory)
    return config, store, workers


async def settle(workers: NativeWorkers, task_id: str):
    task = workers.jobs[task_id].task
    assert task is not None
    await task
    return workers.store.get(task_id)


def durable_turn(row: dict[str, Any]) -> str | None:
    return row["turn_id"] or row["metadata"].get("native_turn_id")


@pytest.mark.parametrize(
    "scenario",
    [
        {"missing_answer": True},
        {"teardown_error": True},
        {"update_then_disconnect": True},
    ],
)
async def test_uncertain_receipt_retains_already_observed_native_turn(tmp_path, scenario) -> None:
    _, store, workers = configured(tmp_path, scenario)
    receipt = await workers.submit("project", "review fixture", "review-1")
    result = await settle(workers, receipt["id"])
    assert result["status"] == "uncertain"
    assert durable_turn(result) == "native-turn"
    store.recover_pending()
    assert durable_turn(store.get(receipt["id"])) == "native-turn"


async def test_retry_of_finished_request_survives_native_profile_change(tmp_path) -> None:
    scenario: dict[str, Any] = {}
    config, _, workers = configured(tmp_path, scenario)
    receipt = await workers.submit("project", "review fixture", "review-1")
    assert (await settle(workers, receipt["id"]))["status"] == "completed"
    config.codex_cli_home = str(tmp_path / "different-native-home")
    repeated = await workers.submit("project", "review fixture", "review-1")
    assert repeated["id"] == receipt["id"]
    assert len(scenario["prompts"]) == 1
    with pytest.raises(ValueError, match="profile|configuration"):
        await workers.submit("project", "new work", "review-2")


async def _wait_running(store: TaskStore, task_id: str) -> None:
    async with asyncio.timeout(3):
        while True:
            if store.get(task_id)["status"] == "running":
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("provider", ["codex_cli", "claude_code", "grok"])
async def test_cancel_returns_clean_receipt_when_native_adapter_is_already_closed(
    tmp_path: Path, provider: str
) -> None:
    """Finding ACP-2: ACPWorkerRuntime.cancel() is the one method shared by
    Workers (Kiro), a plugin's workers and NativeWorkers, which runs codex_cli,
    claude_code and grok jobs through ClaudeCLI, CodexCLI and GrokCLI. This
    pins that the shared cancel() path -- reading job.adapter.closed before
    ever calling job.adapter.cancel() -- works identically regardless of
    which native provider the NativeWorkers instance is configured for.
    """
    scenario: dict[str, Any] = {"hold_until_released": True, "release": asyncio.Event()}
    # Hermetic: a provider home of the test's own, so native_home() never falls
    # back to CODEX_HOME/CLAUDE_CONFIG_DIR/GROK_HOME or the account's ~/.<provider>.
    home = tmp_path / "provider-home"
    home.mkdir()
    config = Config(
        **{f"{provider}_workspaces": {"project": str(tmp_path)}, f"{provider}_home": str(home)}
    )
    config.select_provider(provider, True)
    path = tmp_path / "config.json"
    config.save(path)
    store = TaskStore(tmp_path / "tasks.sqlite")

    def factory(*args: Any, **kwargs: Any) -> ScriptedTransport:
        return ScriptedTransport(scenario, *args, **kwargs)

    workers = NativeWorkers(provider, config, path, store, transport_factory=factory)
    assert workers.binding("project")[2] == str(home.resolve())
    try:
        receipt = await workers.submit("project", "review fixture", "closed-cancel")
        await _wait_running(store, receipt["id"])
        job = workers.jobs[receipt["id"]]
        job.adapter.closed = True
        result = await workers.cancel(receipt["id"])
        assert result["id"] == receipt["id"]
        assert result["status"] == "running"
        assert job.adapter.cancel_called is False
    finally:
        scenario["release"].set()
        await workers.close()
