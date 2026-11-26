import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from answerback_bridge.process import CommandCleanupError
from codex_herdr_bridge.config import Config, public_task
from codex_herdr_bridge.tasks import TaskStore


async def wait_status(store: TaskStore, task_id: str, expected: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            task = store.get(task_id)
            if task["status"] == expected:
                return task
            await asyncio.sleep(0.01)


async def wait_permission_round(
    manager: Any, task_id: str, raw_input: dict[str, Any]
) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            permissions = manager.permissions(task_id)["permissions"]
            for permission in permissions:
                if permission["tool_call"].get("rawInput") == raw_input:
                    return permission
            await asyncio.sleep(0.01)


class FakeACP:
    sends = 0
    permissions: bool = False
    never_completes: bool = False
    permission_rounds: bool = False
    permission_answers: list[tuple[str, dict[str, Any], str | None]] = []
    session_definitions: list[dict[str, Any]] = []
    launch_definitions: list[dict[str, Any]] = []
    unverified_servers: tuple[str, ...] = ()
    exit_failure: Exception | None = None
    block_open: bool = False

    def __init__(
        self,
        binary: str,
        cwd: str,
        on_update: Any,
        on_permission: Any,
        *,
        agent_engine: str = "v3",
        trust_all_tools: bool = False,
        trust_tools: list[str] | tuple[str, ...] = (),
        agent: str | None = None,
    ) -> None:
        self.on_update = on_update
        self.on_permission = on_permission
        self.cancelled = asyncio.Event()
        self.capabilities = {"loadSession": True}
        self.closed = False
        self.native_stopped: bool | None = None
        self.mcp_unverified_servers: frozenset[str] = frozenset(FakeACP.unverified_servers)
        # The same summary the real client records on the receipt as `kiro_trust`.
        self.trust_mode: str | list[str] = (
            "all" if trust_all_tools else list(trust_tools) if trust_tools else "ask"
        )
        FakeACP.launch_definitions.append(
            {
                "binary": binary,
                "cwd": cwd,
                "agent_engine": agent_engine,
                "trust_all_tools": trust_all_tools,
                "trust_tools": list(trust_tools),
                "agent": agent,
            }
        )

    async def __aenter__(self) -> "FakeACP":
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.exit_failure is not None:
            raise self.exit_failure
        self.native_stopped = True

    async def open_session(
        self,
        session_id: str | None = None,
        mcp_servers: Any = None,
        required_mcp_tools: Any = None,
        agent_profile: Any = None,
    ) -> str:
        if self.block_open:
            await asyncio.Future()
        FakeACP.session_definitions.append(
            {
                "session_id": session_id,
                "servers": mcp_servers,
                "required": required_mcp_tools,
                "profile": agent_profile,
            }
        )
        return session_id or "kiro-session-1"

    async def prompt(self, session_id: str, message: str) -> Any:
        FakeACP.sends += 1
        if self.permission_rounds:
            for raw_input, locations in (
                ({"path": "round-1.py"}, [{"path": "round-1.py", "line": 1}]),
                ({"path": "round-2.py"}, [{"path": "round-2.py", "line": 2}]),
            ):
                choice = await self.on_permission(
                    {
                        "session_id": session_id,
                        "tool_call": {
                            "toolCallId": "call-shared",
                            "title": "Read a file",
                            "rawInput": raw_input,
                            "locations": locations,
                        },
                        "options": [
                            {
                                "optionId": "deny",
                                "name": "Reject once",
                                "kind": "reject_once",
                            }
                        ],
                    }
                )
                FakeACP.permission_answers.append(("call-shared", raw_input, choice))
        if self.permissions:
            choice = await self.on_permission(
                {
                    "session_id": session_id,
                    "tool_call": {"toolCallId": "call-1", "title": "Read a file"},
                    "options": [{"optionId": "deny", "name": "Reject once", "kind": "reject_once"}],
                }
            )
            if choice is None:
                return SimpleNamespace(text="", stop_reason="cancelled", session_id=session_id)
            assert choice == "deny"
        if message == "wait":
            await self.cancelled.wait()
            return SimpleNamespace(text="", stop_reason="cancelled", session_id=session_id)
        if self.never_completes:
            await asyncio.Future()
        return SimpleNamespace(text="Kiro result", stop_reason="end_turn", session_id=session_id)

    async def cancel(self, session_id: str) -> None:
        self.cancelled.set()


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from codex_herdr_bridge.workers import Workers

    config = Config("test", kiro_workspaces={"reviewer": str(tmp_path)})
    store = TaskStore(tmp_path / "tasks.sqlite")
    FakeACP.sends = 0
    FakeACP.permissions = False
    FakeACP.never_completes = False
    FakeACP.permission_rounds = False
    FakeACP.permission_answers = []
    FakeACP.session_definitions = []
    FakeACP.launch_definitions = []
    FakeACP.unverified_servers = ()
    FakeACP.exit_failure = None
    FakeACP.block_open = False
    monkeypatch.setattr("codex_herdr_bridge.workers.KiroACP", FakeACP)
    return Workers(config, tmp_path / "config.json", store)


async def test_worker_returns_immediate_receipt_and_never_resends_duplicate(manager: Any) -> None:
    receipt = await manager.submit(
        alias="reviewer",
        message="Review",
        request_id="one",
        expected_cwd=manager.config.kiro_workspaces["reviewer"],
    )
    finished = await wait_status(manager.store, receipt["id"], "completed")
    retry = await manager.submit("reviewer", "Review", "one")
    assert retry["id"] == receipt["id"]
    assert finished["result"] == "Kiro result"
    assert FakeACP.sends == 1
    assert manager.store.worker_session("reviewer")["session_id"] == "kiro-session-1"
    await manager.close()


async def test_worker_budget_expiry_before_prompt_marks_task_failed(
    manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.active_budget import ActiveBudget as RealActiveBudget

    manager.config.kiro_active_budget = 60
    FakeACP.block_open = True

    def tiny_budget(seconds: int) -> RealActiveBudget:
        assert seconds == 60
        return RealActiveBudget(0.01)

    monkeypatch.setattr("answerback_bridge.workers.ActiveBudget", tiny_budget)
    receipt = await manager.submit("reviewer", "Review", "before-prompt-budget")
    finished = await wait_status(manager.store, receipt["id"], "failed")
    assert finished["detail"] == (
        "Kiro task exceeded the 0.01-second active execution budget before its prompt was sent"
    )
    assert finished["metadata"]["native_stopped"] is True
    await manager.close()


async def test_worker_budget_expiry_names_configured_budget_and_stop_fact(
    manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.active_budget import ActiveBudget as RealActiveBudget

    manager.config.kiro_active_budget = 60
    FakeACP.never_completes = True

    def tiny_budget(seconds: int) -> RealActiveBudget:
        assert seconds == 60
        return RealActiveBudget(0.01)

    monkeypatch.setattr("answerback_bridge.workers.ActiveBudget", tiny_budget)
    receipt = await manager.submit("reviewer", "Review", "configured-budget")
    finished = await wait_status(manager.store, receipt["id"], "uncertain")
    assert finished["detail"] == (
        "Kiro task exceeded the 0.01-second active execution budget "
        "after its prompt was sent; no automatic replay"
    )
    assert finished["metadata"]["native_stopped"] is True
    await manager.close()


async def test_worker_budget_expiry_through_cleanup_failure_names_budget_and_stop_failure(
    manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.active_budget import ActiveBudget as RealActiveBudget

    manager.config.kiro_active_budget = 60
    FakeACP.never_completes = True
    FakeACP.exit_failure = CommandCleanupError(
        "Command cleanup failed to confirm owned child reaping"
    )

    def tiny_budget(seconds: int) -> RealActiveBudget:
        assert seconds == 60
        return RealActiveBudget(0.01)

    monkeypatch.setattr("answerback_bridge.workers.ActiveBudget", tiny_budget)
    receipt = await manager.submit("reviewer", "Review", "cleanup-failure-budget")
    finished = await wait_status(manager.store, receipt["id"], "uncertain")
    assert finished["detail"] == (
        "Kiro task exceeded the 0.01-second active execution budget "
        "after its prompt was sent; no automatic replay"
    )
    assert finished["metadata"]["native_stopped"] is False
    await manager.close()


async def test_permission_wait_pauses_configured_active_budget(
    manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.active_budget import ActiveBudget

    manager.config.kiro_active_budget = 60
    FakeACP.permissions = True
    now = 0.0
    budgets: list[ActiveBudget] = []

    def configured_budget(seconds: int) -> ActiveBudget:
        assert seconds == 60
        budget = ActiveBudget(seconds, clock=lambda: now)
        budgets.append(budget)
        return budget

    monkeypatch.setattr("answerback_bridge.workers.ActiveBudget", configured_budget)
    receipt = await manager.submit("reviewer", "Review", "paused-configured-budget")
    await wait_status(manager.store, receipt["id"], "blocked")
    before = budgets[0].remaining
    now += 3_600
    assert budgets[0].remaining == before
    assert manager.store.get(receipt["id"])["status"] == "blocked"
    permission_id = manager.permissions(receipt["id"])["permissions"][0]["id"]
    manager.answer(receipt["id"], permission_id, "deny")
    await wait_status(manager.store, receipt["id"], "completed")
    await manager.close()


async def test_permission_blocks_until_supplied_choice_and_rejects_invented_option(
    manager: Any,
) -> None:
    FakeACP.permissions = True
    receipt = await manager.submit("reviewer", "Review", "permission")
    await wait_status(manager.store, receipt["id"], "blocked")
    inbox = manager.permissions(receipt["id"])
    assert len(inbox["permissions"]) == 1
    permission_id = inbox["permissions"][0]["id"]
    with pytest.raises(ValueError, match="option"):
        manager.answer(receipt["id"], permission_id, "invented-allow")
    assert manager.store.get(receipt["id"])["status"] == "blocked"
    manager.answer(receipt["id"], permission_id, "deny")
    await wait_status(manager.store, receipt["id"], "completed")
    assert manager.permissions(receipt["id"])["permissions"] == []
    await manager.close()


async def test_managed_kiro_repeats_permission_rounds_and_budget_pauses(
    manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.active_budget import ActiveBudget

    budget_events: list[str] = []
    original_pause = ActiveBudget.pause
    original_resume = ActiveBudget.resume

    def observed_pause(budget: ActiveBudget) -> None:
        budget_events.append("pause")
        original_pause(budget)

    def observed_resume(budget: ActiveBudget) -> None:
        budget_events.append("resume")
        original_resume(budget)

    monkeypatch.setattr(ActiveBudget, "pause", observed_pause)
    monkeypatch.setattr(ActiveBudget, "resume", observed_resume)
    FakeACP.permission_rounds = True

    receipt = await manager.submit("reviewer", "Review", "permission-rounds")
    first = await wait_permission_round(manager, receipt["id"], {"path": "round-1.py"})
    manager.answer(receipt["id"], first["id"], "deny")
    second = await wait_permission_round(manager, receipt["id"], {"path": "round-2.py"})
    manager.answer(receipt["id"], second["id"], "deny")
    await wait_status(manager.store, receipt["id"], "completed")

    assert first["id"] != second["id"]
    assert [answer[:2] for answer in FakeACP.permission_answers] == [
        ("call-shared", {"path": "round-1.py"}),
        ("call-shared", {"path": "round-2.py"}),
    ]
    assert [answer[2] for answer in FakeACP.permission_answers] == ["deny", "deny"]
    assert budget_events == ["pause", "resume", "pause", "resume"]
    assert manager.permissions(receipt["id"])["permissions"] == []
    await manager.close()


async def test_managed_kiro_receipt_preserves_resolved_permission_round_facts(
    manager: Any,
) -> None:
    FakeACP.permission_rounds = True

    receipt = await manager.submit("reviewer", "Review", "permission-round-receipt")
    first = await wait_permission_round(manager, receipt["id"], {"path": "round-1.py"})
    manager.answer(receipt["id"], first["id"], "deny")
    second = await wait_permission_round(manager, receipt["id"], {"path": "round-2.py"})
    manager.answer(receipt["id"], second["id"], "deny")
    finished = await wait_status(manager.store, receipt["id"], "completed")
    await manager.close()

    permission_events = finished["metadata"].get("permission_events", [])
    assert [event["permission_id"] for event in permission_events] == [first["id"], second["id"]]
    assert [event["toolCallId"] for event in permission_events] == ["call-shared", "call-shared"]
    assert [event["option_id"] for event in permission_events] == ["deny", "deny"]
    assert [event["ordinal"] for event in permission_events] == [1, 2]
    for event in permission_events:
        assert set(event) == {
            "permission_id",
            "toolCallId",
            "option_id",
            "requested_at",
            "answered_at",
            "ordinal",
        }
        assert isinstance(event["requested_at"], float)
        assert event["answered_at"] >= event["requested_at"]


async def test_managed_kiro_receipt_records_cancelled_permission_round(manager: Any) -> None:
    FakeACP.permissions = True

    receipt = await manager.submit("reviewer", "Review", "cancelled-permission-round")
    await wait_status(manager.store, receipt["id"], "blocked")
    (pending,) = manager.permissions(receipt["id"])["permissions"]
    await manager.cancel(receipt["id"])
    finished = await wait_status(manager.store, receipt["id"], "cancelled")
    await manager.close()

    (event,) = finished["metadata"]["permission_events"]
    assert event["permission_id"] == pending["id"]
    assert event["toolCallId"] == "call-1"
    assert event["option_id"] == "cancelled"
    assert event["ordinal"] == 1
    assert event["answered_at"] >= event["requested_at"]


async def test_permission_registration_is_rolled_back_when_store_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding F2: a failing store.update during a permission request must not
    leak a paused budget and an orphaned pending-future.

    Drives ACPWorkerRuntime.callbacks() directly (the function shared by Workers
    and a plugin's workers), with store.update stubbed to raise exactly as F2's repro
    does, and observes the budget through its public ``remaining`` with an
    injected clock rather than the private pause counter.
    """
    from answerback_bridge.active_budget import ActiveBudget
    from answerback_bridge.workers import ACPWorkerRuntime, Job

    config = Config("test")
    store = TaskStore(tmp_path / "tasks.sqlite")

    def failing_update(task_id: str, status: str, **kwargs: Any) -> Any:
        raise RuntimeError("transient store failure")

    monkeypatch.setattr(store, "update", failing_update)

    runtime = ACPWorkerRuntime(config, tmp_path / "config.json", store)
    now = 1000.0

    def clock() -> float:
        return now

    job = Job(budget=ActiveBudget(600, clock=clock))
    _, permission = runtime.callbacks("some-task", job)

    before = job.budget.remaining
    with pytest.raises(RuntimeError, match="transient store failure"):
        await permission(
            {
                "tool_call": {"toolCallId": "call-1", "title": "Read a file"},
                "options": [{"optionId": "deny", "name": "Reject once", "kind": "reject_once"}],
            }
        )

    assert job.pending == {}
    now += 5
    assert job.budget.remaining == before - 5
    with pytest.raises(RuntimeError, match="not paused"):
        job.budget.resume()


async def test_cancel_waits_for_provider_confirmation(manager: Any) -> None:
    receipt = await manager.submit("reviewer", "wait", "cancel")
    await wait_status(manager.store, receipt["id"], "running")
    await manager.cancel(receipt["id"])
    await wait_status(manager.store, receipt["id"], "cancelled")
    await manager.close()


async def test_cancel_during_adapter_teardown_returns_clean_receipt_not_bridge_error(
    manager: Any,
) -> None:
    """Finding ACP-2: cancelling a job whose owned adapter already reports itself
    closed -- the ordinary, benign race where a turn has already completed and
    the adapter is mid-teardown -- must return the current public task record,
    not an unhandled BridgeError from calling an already-closed adapter's own
    cancel().

    Uses the same FakeACP harness as test_cancel_waits_for_provider_confirmation,
    with its closed flag flipped directly to stand in for the moment a real
    ACPClient's private _closed flag flips to True (synchronously, before its
    own async shutdown completes) -- the state ACPWorkerRuntime.cancel() must
    now check before ever calling job.adapter.cancel().
    """
    receipt = await manager.submit("reviewer", "wait", "teardown-cancel")
    await wait_status(manager.store, receipt["id"], "running")
    job = manager.jobs[receipt["id"]]
    job.adapter.closed = True
    result = await manager.cancel(receipt["id"])
    assert result["id"] == receipt["id"]
    assert result["status"] == "running"
    assert not job.adapter.cancelled.is_set()
    await manager.close()


async def test_shutdown_keeps_running_work_uncertain_and_does_not_replay(manager: Any) -> None:
    receipt = await manager.submit("reviewer", "wait", "shutdown")
    await wait_status(manager.store, receipt["id"], "running")
    await manager.close()
    assert manager.store.get(receipt["id"])["status"] == "uncertain"
    assert FakeACP.sends == 1


async def test_cancel_before_worker_starts_releases_the_peer(manager: Any) -> None:
    receipt = await manager.submit("reviewer", "never-start", "early-cancel")
    await manager.cancel(receipt["id"])
    assert manager.store.get(receipt["id"])["status"] == "cancelled"
    assert not manager.running(receipt["id"])
    assert FakeACP.sends == 0
    next_task = await manager.submit("reviewer", "Review", "after-cancel")
    await wait_status(manager.store, next_task["id"], "completed")
    assert FakeACP.sends == 1
    await manager.close()


async def test_new_and_reused_workers_require_connected_bridge_tools(manager: Any) -> None:
    for request in ("first", "second"):
        receipt = await manager.submit("reviewer", "Review", request)
        await wait_status(manager.store, receipt["id"], "completed")
    calls = FakeACP.session_definitions
    assert calls[0]["session_id"] is None
    assert calls[1]["session_id"] == "kiro-session-1"
    for call in calls:
        server = call["servers"][0]
        assert server["name"] == "answerback_bridge"
        assert server["_meta"]["kiro"] == {"waitForReady": True, "timeout": 60000}
        assert call["required"] == {"answerback_bridge": {"peer_submit", "task_wait"}}
    await manager.close()


async def test_worker_selects_scoped_profile_on_creation_and_resume(manager: Any) -> None:
    for request in ("profile-first", "profile-second"):
        receipt = await manager.submit("reviewer", "Review", request)
        finished = await wait_status(manager.store, receipt["id"], "completed")
        assert finished["metadata"]["worker_profile"] == "answerback_bridge"
    for call in FakeACP.session_definitions:
        profile = call["profile"]
        assert profile["id"] == "answerback_bridge"
        assert set(profile["tools"]) == {"read", "write", "shell", "@answerback_bridge"}
        assert profile["includeMcpJson"] is False
        assert profile["includePowers"] is False
        assert "permissions" not in profile
    assert FakeACP.session_definitions[1]["session_id"] == "kiro-session-1"
    assert manager.store.worker_session("reviewer")["profile"] == "answerback_bridge"
    await manager.close()


async def test_legacy_worker_creates_scoped_session_without_replaying_history(manager: Any) -> None:
    cwd = manager.config.kiro_workspaces["reviewer"]
    manager.store.save_worker_session("reviewer", "legacy-default-session", cwd)
    receipt = await manager.submit("reviewer", "Review", "profile-migration")
    finished = await wait_status(manager.store, receipt["id"], "completed")
    assert FakeACP.session_definitions[0]["session_id"] is None
    assert finished["metadata"]["previous_session_id"] == "legacy-default-session"
    assert manager.store.worker_session("reviewer")["session_id"] == "kiro-session-1"
    assert FakeACP.sends == 1
    await manager.close()


async def test_failed_profile_migration_preserves_previous_session(
    manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_herdr_bridge.process import BridgeError

    cwd = manager.config.kiro_workspaces["reviewer"]
    manager.store.save_worker_session("reviewer", "legacy-default-session", cwd)
    attempted_sessions = []

    async def fail_open(self: Any, **kwargs: Any) -> str:
        attempted_sessions.append(kwargs["session_id"])
        raise BridgeError("Required profile not selected")

    monkeypatch.setattr(FakeACP, "open_session", fail_open)
    receipt = await manager.submit("reviewer", "Review", "failed-profile-migration")
    await wait_status(manager.store, receipt["id"], "uncertain")
    assert attempted_sessions == [None]
    assert manager.store.worker_session("reviewer")["session_id"] == "legacy-default-session"
    assert FakeACP.sends == 0
    await manager.close()


async def test_removed_workspace_cannot_be_submitted_after_async_directory_check(
    manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_herdr_bridge.workspaces import Workspaces

    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_check(*args: Any, **kwargs: Any) -> bool:
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr("codex_herdr_bridge.workers.asyncio.to_thread", delayed_check)
    submission = asyncio.create_task(manager.submit("reviewer", "Review", "racing-removal"))
    await entered.wait()
    registry = Workspaces(manager.config, manager.config_path, manager.store)
    assert registry.remove("reviewer", apply=True)["applied"]
    release.set()
    try:
        with pytest.raises(ValueError, match="workspace changed"):
            await submission
        assert manager.store.counts() == {}
        assert FakeACP.sends == 0
    finally:
        await manager.close()


async def test_worker_threads_configured_kiro_engine_and_trust_policy(
    manager: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "worker-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    manager.config.kiro_acp_engine = "v2"
    manager.config.kiro_trust_all_tools = True
    receipt = await manager.submit("reviewer", "Review", "v2-trust-policy")
    finished = await wait_status(manager.store, receipt["id"], "completed")
    assert FakeACP.launch_definitions == [
        {
            "binary": "kiro-cli",
            "cwd": manager.config.kiro_workspaces["reviewer"],
            "agent_engine": "v2",
            "trust_all_tools": True,
            "trust_tools": [],
            "agent": "answerback_bridge",
        }
    ]
    assert finished["metadata"]["kiro_trust"] == "all"
    agent_file = home / ".kiro" / "agents" / "answerback_bridge.json"
    assert agent_file.stat().st_mode & 0o777 == 0o600
    assert json.loads(agent_file.read_text())["name"] == "answerback_bridge"
    await manager.close()


async def test_worker_v2_trust_tools_reach_launch_and_receipt(
    manager: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "worker-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    manager.config.kiro_acp_engine = "v2"
    manager.config.kiro_trust_tools = ["read"]
    receipt = await manager.submit("reviewer", "Review", "v2-trust-read")
    finished = await wait_status(manager.store, receipt["id"], "completed")
    assert FakeACP.launch_definitions[0]["trust_tools"] == ["read"]
    assert FakeACP.launch_definitions[0]["trust_all_tools"] is False
    assert FakeACP.launch_definitions[0]["agent"] == "answerback_bridge"
    assert finished["metadata"]["kiro_trust"] == ["read"]
    assert public_task(finished)["metadata"]["kiro_trust"] == ["read"]
    assert finished["metadata"]["mcp_readiness"] == "tools_verified"
    assert "unverified_mcp_tools" not in finished["metadata"]
    await manager.close()


async def test_worker_records_v2_server_initialized_readiness_on_the_receipt(
    manager: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "worker-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    manager.config.kiro_acp_engine = "v2"
    FakeACP.unverified_servers = ("answerback_bridge",)
    receipt = await manager.submit("reviewer", "Review", "v2-readiness")
    finished = await wait_status(manager.store, receipt["id"], "completed")
    shown = public_task(finished)["metadata"]
    assert shown["mcp_readiness"] == "server_initialized"
    assert shown["unverified_mcp_tools"] == ["answerback_bridge"]
    assert shown["kiro_trust"] == "ask"
    await manager.close()


def test_recipients_scope_the_tool_verification_claim_to_v3() -> None:
    """RECIPIENTS.md must not say the required MCP tools are verified before prompting
    without naming the engine: v2 reports only `server_initialized`, so its tools stay
    unverified until the worker's first bridge call, with `mcp_readiness` on the receipt
    as the evidence. The sentence in the Kiro section is pinned to that split. The
    HARNESSES.md support table must agree with its own managed-route paragraph: the
    opt-in v2 trust mode is what managed workers run on, not something "not yet used"."""
    docs = Path(__file__).resolve().parents[1] / "docs"
    section = " ".join((docs / "RECIPIENTS.md").read_text().split())
    assert "verifies the selected profile and required enabled MCP tools" not in section
    assert "On the default v3 engine it also verifies the required bridge MCP tools" in section
    assert '"mcp_readiness": "tools_verified"' in section
    assert "tools stay unverified until the worker's first bridge call" in section
    assert '"mcp_readiness": "server_initialized"' in section
    assert '"unverified_mcp_tools": ["answerback_bridge"]' in section
    harnesses = " ".join((docs / "HARNESSES.md").read_text().split())
    assert "not yet used by managed workers" not in harnesses
    kiro_row = next(line for line in harnesses.split("| Experimental |") if "Kiro CLI" in line)
    assert "Managed workers run unattended on the opt-in v2 engine" in kiro_row
    assert "`kiro_trust_all_tools` or `kiro_trust_tools`" in kiro_row
    assert "The managed Kiro route runs unattended v2 workers" in harnesses
    assert (
        "Each receipt retains the newest 128 resolved permission rounds as metadata-only "
        "`permission_events` facts"
    ) in harnesses
    assert "`permission_events_truncated` counts older facts dropped." in harnesses


async def test_worker_v3_launch_is_unchanged_and_records_ask(
    manager: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "worker-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    receipt = await manager.submit("reviewer", "Review", "v3-default")
    finished = await wait_status(manager.store, receipt["id"], "completed")
    assert FakeACP.launch_definitions == [
        {
            "binary": "kiro-cli",
            "cwd": manager.config.kiro_workspaces["reviewer"],
            "agent_engine": "v3",
            "trust_all_tools": False,
            "trust_tools": [],
            "agent": None,
        }
    ]
    assert finished["metadata"]["kiro_trust"] == "ask"
    assert finished["metadata"]["mcp_readiness"] == "tools_verified"
    # v3 selects the inline profile; no agent file is written for it.
    assert not (home / ".kiro").exists()
    await manager.close()


async def test_worker_v2_refuses_a_symlinked_agent_folder(
    manager: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "worker-home"
    (home / ".kiro").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / ".kiro" / "agents").symlink_to(elsewhere)
    monkeypatch.setenv("HOME", str(home))
    manager.config.kiro_acp_engine = "v2"
    receipt = await manager.submit("reviewer", "Review", "v2-symlink")
    finished = await wait_status(manager.store, receipt["id"], "uncertain")
    assert "symlink" in finished["detail"]
    assert FakeACP.launch_definitions == []
    assert list(elsewhere.iterdir()) == []
    await manager.close()


async def test_worker_v2_refuses_a_hard_linked_agent_file(
    manager: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "worker-home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    victim = agents / "other.json"
    victim.write_text("keep\n")
    victim.chmod(0o644)
    os.link(victim, agents / "answerback_bridge.json")
    monkeypatch.setenv("HOME", str(home))
    manager.config.kiro_acp_engine = "v2"
    receipt = await manager.submit("reviewer", "Review", "v2-hard-link")
    finished = await wait_status(manager.store, receipt["id"], "uncertain")
    assert "hard-linked elsewhere" in finished["detail"]
    assert FakeACP.launch_definitions == []
    assert victim.read_text() == "keep\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert victim.stat().st_nlink == 2
    await manager.close()
