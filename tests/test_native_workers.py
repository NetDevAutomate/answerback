import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.config import Config
from answerback_bridge.errors import AdmissionRefused
from answerback_bridge.native_protocol import NativeResult
from answerback_bridge.native_workers import NativeWorkers
from answerback_bridge.process import BridgeError, CommandCleanupError
from answerback_bridge.tasks import TaskStore
from answerback_bridge.workspaces import Workspaces


class FakeTransport:
    opened = []
    prompts = []
    result = NativeResult("owned", "completed", "verified answer", "turn-1")
    failure = None
    never_completes = False
    wait_permission = False
    permission_seen = None
    exit_failure = None

    def __init__(self, binary, cwd, update, permission, *, home=None):
        self.permission = permission
        self.home = home
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.exit_failure is not None:
            raise self.exit_failure
        self.native_stopped = True

    async def open_session(self, session_id=None, mcp_servers=None):
        self.opened.append(session_id)
        return session_id or "owned"

    async def prompt(self, session_id, message, *, prompt_id):
        self.prompts.append((session_id, message, prompt_id))
        if self.wait_permission:
            asyncio.get_running_loop().call_soon(self.permission_seen.set)
            answer = await self.permission(
                {
                    "session_id": session_id,
                    "tool_call": {"toolCallId": "native-call-1", "title": "Read a file"},
                    "options": [
                        {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                        {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
                    ],
                }
            )
            if answer is None:
                return NativeResult(
                    session_id,
                    "cancelled",
                    None,
                    "turn-1",
                    evidence={
                        "cancel_requested": True,
                        "cancel_lifecycle": [
                            {
                                "task_id": "foreground-task",
                                "tool_use_id": "foreground-bash",
                                "subtype": "task_notification",
                                "status": "stopped",
                                "time": 1.0,
                            }
                        ],
                    },
                )
        if self.never_completes:
            await asyncio.Future()
        if self.failure:
            raise self.failure
        return self.result

    async def cancel(self, session_id):
        pass


@pytest.fixture
def setup(tmp_path):
    FakeTransport.opened = []
    FakeTransport.prompts = []
    FakeTransport.result = NativeResult("owned", "completed", "verified answer", "turn-1")
    FakeTransport.failure = None
    FakeTransport.never_completes = False
    FakeTransport.wait_permission = False
    FakeTransport.permission_seen = asyncio.Event()
    FakeTransport.exit_failure = None
    # Hermetic: a Codex home of the test's own, so native_home() never falls back
    # to CODEX_HOME or the account's ~/.codex. The folder need not exist.
    config = Config(
        codex_cli_workspaces={"test": str(tmp_path)},
        codex_cli_home=str(tmp_path / "native-home"),
    )
    config.select_provider("codex_cli", True)
    path = tmp_path / "config.json"
    config.save(path)
    store = TaskStore(tmp_path / "tasks.sqlite")
    workers = NativeWorkers("codex_cli", config, path, store, transport_factory=FakeTransport)
    return config, path, store, workers


async def finish(workers, task_id):
    task = workers.jobs[task_id].task
    await task
    return workers.store.get(task_id)


async def test_terminal_response_and_owned_resume_are_correlated(setup):
    config, path, store, workers = setup
    first = await workers.submit("test", "one", "request-1")
    result = await finish(workers, first["id"])
    assert result["status"] == "completed"
    assert result["result"] == "verified answer"
    assert result["turn_id"] == "turn-1"
    assert store.native_session_owner("codex_cli", "owned")["alias"] == "test"
    second = await workers.submit("test", "two", "request-2")
    await finish(workers, second["id"])
    assert FakeTransport.opened == [None, "owned"]
    assert FakeTransport.prompts[0][2] == first["id"]


async def test_repeated_request_never_submits_twice(setup):
    _, _, _, workers = setup
    first = await workers.submit("test", "one", "request-1")
    await finish(workers, first["id"])
    again = await workers.submit("test", "one", "request-1")
    assert again["id"] == first["id"]
    assert len(FakeTransport.prompts) == 1


@pytest.mark.parametrize("mode", ["eof", "missing_answer", "wrong_session"])
async def test_uncertainty_keeps_target_reserved(setup, mode):
    _, _, store, workers = setup
    if mode == "eof":
        FakeTransport.failure = ConnectionError("lost native terminal event")
    elif mode == "missing_answer":
        FakeTransport.result = NativeResult("owned", "completed", None, "turn-1")
    else:
        FakeTransport.result = NativeResult("foreign", "completed", "wrong", "turn-2")
    first = await workers.submit("test", "one", "request-1")
    result = await finish(workers, first["id"])
    assert result["status"] == "uncertain"
    with pytest.raises(AdmissionRefused, match=first["id"]):
        await workers.submit("test", "two", "request-2")
    assert store.worker_session("codex_cli:test") is None


async def test_permission_is_pending_until_human_answers(setup):
    _, _, store, workers = setup
    FakeTransport.wait_permission = True
    first = await workers.submit("test", "one", "request-1")
    async with asyncio.timeout(2):
        await FakeTransport.permission_seen.wait()
    permission = workers.permissions(first["id"])["permissions"][0]
    assert store.get(first["id"])["status"] == "blocked"
    with pytest.raises(ValueError):
        workers.answer(first["id"], permission["id"], "allow_always")
    workers.answer(first["id"], permission["id"], "once")
    result = await finish(workers, first["id"])
    assert result["status"] == "completed"
    with pytest.raises(ValueError):
        workers.answer(first["id"], permission["id"], "once")


async def test_resolved_native_permission_round_is_recorded_without_request_content(setup):
    _, _, _, workers = setup
    FakeTransport.wait_permission = True
    receipt = await workers.submit("test", "secret prompt", "request-1")
    async with asyncio.timeout(2):
        await FakeTransport.permission_seen.wait()
    (pending,) = workers.permissions(receipt["id"])["permissions"]
    workers.answer(receipt["id"], pending["id"], "once")
    result = await finish(workers, receipt["id"])

    (event,) = result["metadata"]["permission_events"]
    assert event["permission_id"] == pending["id"]
    assert event["toolCallId"] == "native-call-1"
    assert event["option_id"] == "once"
    assert event["ordinal"] == 1
    assert set(event) == {
        "permission_id",
        "toolCallId",
        "option_id",
        "requested_at",
        "answered_at",
        "ordinal",
    }
    assert event["answered_at"] >= event["requested_at"]
    assert "secret prompt" not in str(event)


async def test_cancel_ack_does_not_create_terminal_result(setup):
    _, _, _, workers = setup
    FakeTransport.wait_permission = True
    first = await workers.submit("test", "one", "request-1")
    async with asyncio.timeout(2):
        await FakeTransport.permission_seen.wait()
    reply = await workers.cancel(first["id"])
    assert reply["status"] != "cancelled"
    result = await finish(workers, first["id"])
    assert result["status"] == "cancelled"
    # The receipt names the worker's own acknowledgement, as the Kiro path does
    # with "Kiro confirmed cancellation", and keeps the adapter's native evidence.
    assert result["detail"] == "codex_cli confirmed cancelled"
    assert result["metadata"]["native_terminal"] == "cancelled"
    assert result["metadata"]["native_evidence"]["cancel_requested"] is True
    assert result["metadata"]["native_evidence"]["cancel_lifecycle"][0]["status"] == "stopped"
    (permission_event,) = result["metadata"]["permission_events"]
    assert permission_event["toolCallId"] == "native-call-1"
    assert permission_event["option_id"] == "cancelled"
    assert permission_event["ordinal"] == 1
    assert set(permission_event) == {
        "permission_id",
        "toolCallId",
        "option_id",
        "requested_at",
        "answered_at",
        "ordinal",
    }


async def test_cancel_without_the_native_stop_witness_settles_uncertain(setup):
    """The adapter's refusal names the task; the receipt is uncertain, never cancelled or failed."""
    _, _, _, workers = setup
    FakeTransport.failure = BridgeError(
        "Claude cancelled without the native stop witness for foreground task "
        "'foreground-task' (tool use 'foreground-bash')"
    )
    first = await workers.submit("test", "one", "request-1")
    result = await finish(workers, first["id"])
    assert result["status"] == "uncertain"
    assert result["detail"] == (
        "BridgeError: Claude cancelled without the native stop witness for foreground task "
        "'foreground-task' (tool use 'foreground-bash')"
    )
    assert "native_terminal" not in result["metadata"]


async def test_budget_expiry_after_native_prompt_names_configured_budget_and_stop_fact(
    setup, monkeypatch: pytest.MonkeyPatch
):
    config, _, store, workers = setup
    from answerback_bridge.active_budget import ActiveBudget as RealActiveBudget

    config.codex_cli_active_budget = 60
    FakeTransport.never_completes = True

    def tiny_budget(seconds: int) -> RealActiveBudget:
        assert seconds == 60
        return RealActiveBudget(0.01)

    monkeypatch.setattr("answerback_bridge.native_workers.ActiveBudget", tiny_budget)
    receipt = await workers.submit("test", "one", "configured-budget")
    result = await finish(workers, receipt["id"])
    assert result["status"] == "uncertain"
    assert result["detail"] == (
        "codex_cli task exceeded the 0.01-second active execution budget "
        "after its prompt was sent; no automatic replay"
    )
    assert result["metadata"]["native_stopped"] is True


async def test_budget_expiry_through_cleanup_failure_names_budget_and_stop_failure(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, _, workers = setup
    from answerback_bridge.active_budget import ActiveBudget as RealActiveBudget

    config.codex_cli_active_budget = 60
    FakeTransport.never_completes = True
    FakeTransport.exit_failure = CommandCleanupError(
        "Command cleanup failed to confirm owned child reaping"
    )

    def tiny_budget(seconds: int) -> RealActiveBudget:
        assert seconds == 60
        return RealActiveBudget(0.01)

    monkeypatch.setattr("answerback_bridge.native_workers.ActiveBudget", tiny_budget)
    receipt = await workers.submit("test", "one", "cleanup-failure-budget")
    result = await finish(workers, receipt["id"])
    assert result["status"] == "uncertain"
    assert result["detail"] == (
        "codex_cli task exceeded the 0.01-second active execution budget "
        "after its prompt was sent; no automatic replay"
    )
    assert result["metadata"]["native_stopped"] is False


async def test_claude_budget_expiry_without_stop_witness_is_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.active_budget import ActiveBudget as RealActiveBudget

    class ClaudeTransport(FakeTransport):
        async def __aexit__(self, *args):
            pass

    FakeTransport.never_completes = True
    config = Config(
        claude_code_workspaces={"test": str(tmp_path)},
        claude_code_home=str(tmp_path / "native-home"),
    )
    config.select_provider("claude_code", True)
    store = TaskStore(tmp_path / "tasks.sqlite")
    workers = NativeWorkers(
        "claude_code", config, tmp_path / "config.json", store, transport_factory=ClaudeTransport
    )

    def tiny_budget(seconds: int) -> RealActiveBudget:
        assert seconds == 600
        return RealActiveBudget(0.01)

    monkeypatch.setattr("answerback_bridge.native_workers.ActiveBudget", tiny_budget)
    receipt = await workers.submit("test", "one", "claude-budget")
    result = await finish(workers, receipt["id"])
    assert result["status"] == "uncertain"
    assert result["metadata"]["native_stopped"] == "unknown"


async def test_budget_expiry_before_native_prompt_marks_task_failed(
    setup, monkeypatch: pytest.MonkeyPatch
):
    config, _, _, workers = setup
    from answerback_bridge.active_budget import ActiveBudget as RealActiveBudget

    config.codex_cli_active_budget = 60

    class BlockingOpen(FakeTransport):
        async def open_session(self, *args, **kwargs):
            await asyncio.Future()

    def tiny_budget(seconds: int) -> RealActiveBudget:
        assert seconds == 60
        return RealActiveBudget(0.01)

    workers._factory = BlockingOpen
    monkeypatch.setattr("answerback_bridge.native_workers.ActiveBudget", tiny_budget)
    receipt = await workers.submit("test", "one", "before-prompt-budget")
    result = await finish(workers, receipt["id"])
    assert result["status"] == "failed"
    assert result["detail"] == (
        "codex_cli task exceeded the 0.01-second active execution budget before its prompt was sent"
    )
    assert result["metadata"]["native_stopped"] is True


async def test_config_change_cannot_resume_old_native_identity(setup):
    config, _, store, workers = setup
    first = await workers.submit("test", "one", "request-1")
    await finish(workers, first["id"])
    config.codex_cli_home = "/different-native-home"
    with pytest.raises(ValueError, match="configuration|profile"):
        await workers.submit("test", "two", "request-2")
    assert len(FakeTransport.prompts) == 1


async def test_unowned_legacy_pointer_cannot_grant_ownership(setup):
    _, _, store, workers = setup
    store.save_worker_session("codex_cli:test", "foreign", str(store.path.parent), profile="x")
    with pytest.raises(ValueError, match="ownership|configuration|profile"):
        await workers.submit("test", "one", "request-1")
    assert not FakeTransport.opened


async def test_shutdown_preserves_uncertainty_and_revokes_permission(setup):
    _, _, store, workers = setup
    FakeTransport.wait_permission = True
    first = await workers.submit("test", "one", "request-1")
    async with asyncio.timeout(2):
        await FakeTransport.permission_seen.wait()
    await workers.close()
    assert store.get(first["id"])["status"] == "uncertain"
    assert not workers.permissions(first["id"])["permissions"]


@pytest.mark.parametrize("provider", ["codex_cli", "claude_code", "grok"])
async def test_workspace_registration_removal_preserves_native_ownership(tmp_path, provider):
    config = Config()
    path = tmp_path / "config.json"
    store = TaskStore(tmp_path / "tasks.sqlite")
    spaces = Workspaces(config, path, store, provider=provider)
    result = await spaces.add("test", str(tmp_path), binary=sys.executable)
    assert result["peer_id"] == f"{provider}:test"
    assert Config.load(path).provider_enabled(provider)
    store.claim_native_session(provider, "owned", "test", str(tmp_path), "profile")
    assert spaces.remove("test", apply=True)["applied"]
    assert store.native_session_owner(provider, "owned") is not None


def test_unused_provider_blocks_do_not_change_saved_configuration(tmp_path):
    config = Config()
    path = tmp_path / "config.json"
    config.save(path)
    text = path.read_text()
    assert "codex_cli" not in text and "claude_code" not in text and '"grok_' not in text


def test_manual_workspace_binding_survives_symlink_retarget(tmp_path):
    original = tmp_path / "original"
    other = tmp_path / "other"
    alias = tmp_path / "alias"
    original.mkdir()
    other.mkdir()
    alias.symlink_to(original, target_is_directory=True)
    config = Config(codex_cli_workspaces={"work": str(alias)})
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    config.__post_init__()
    assert config.codex_cli_workspaces["work"] == str(original.resolve())


@pytest.mark.parametrize("provider", ["codex_cli", "claude_code", "grok"])
async def test_service_routes_registered_native_work_and_exact_result(
    tmp_path, monkeypatch, provider, setup
):
    from answerback_bridge import native_workers
    from answerback_bridge.service import Bridge

    monkeypatch.setattr(native_workers, "transport_for", lambda _: FakeTransport)
    # Hermetic: a provider home of the test's own, so native_home() never falls
    # back to the provider's variable or the account's home.
    config = Config(
        **{
            f"{provider}_workspaces": {"work": str(tmp_path)},
            f"{provider}_home": str(tmp_path / "native-home"),
        }
    )
    config.select_provider(provider, True)
    path = tmp_path / f"{provider}.json"
    config.save(path)
    bridge = Bridge(config, path)
    try:
        found = await bridge.peer_search(provider=provider)
        assert [peer["peer_id"] for peer in found["peers"]] == [f"{provider}:work"]
        receipt = await bridge.peer_submit(f"{provider}:work", "review parser", "service-1")
        owner = bridge.worker_owner(receipt["id"])
        await finish(owner, receipt["id"])
        result = await bridge.get_task(receipt["id"])
        assert result["status"] == "completed"
        assert result["result"] == "verified answer"
        assert result["metadata"]["kind"] == provider
    finally:
        await bridge.close()


def test_native_recovery_never_replays_after_restart(setup):
    _, _, store, _ = setup
    task, _ = store.reserve("codex_cli:test", "one", "request-1", {"kind": "codex_cli"})
    store.update(task["id"], "blocked")
    store.recover_pending()
    assert store.get(task["id"])["status"] == "uncertain"


async def test_owned_cli_session_is_excluded_from_desktop_routes(setup):
    from answerback_bridge.service import Bridge

    config, path, store, _ = setup
    ident = "2b8dad88-5c7b-44a8-9125-9d3ee060d352"
    config.desktop_threads["owned"] = ident
    config.select_provider("codex", True)
    store.claim_native_session("codex_cli", ident, "test", str(path.parent), "profile")
    bridge = Bridge(config, path)
    # Bridge's store uses its state directory; use that actual authority too.
    bridge.store.claim_native_session("codex_cli", ident, "test", str(path.parent), "profile")
    try:
        with pytest.raises(ValueError, match="Owned CLI"):
            await bridge.adapters.get("desktop").submit_native(ident, "unsafe route", "route-1")
        with pytest.raises(ValueError, match="Owned CLI"):
            await bridge.adapters.get("desktop").resolve_exact("desktop:owned")
    finally:
        await bridge.close()


def test_payload_maintenance_preserves_native_ownership(tmp_path):
    from answerback_bridge.maintenance import Maintenance

    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve(
        "codex_cli:test",
        "private request",
        "old-request",
        {"kind": "codex_cli", "alias": "test", "cwd": str(tmp_path)},
    )
    store.update(task["id"], "completed", result="private result")
    store.claim_native_session("codex_cli", "owned", "test", str(tmp_path), "profile")
    with store.connection() as db:
        db.execute("UPDATE tasks SET created=1,updated=2")
        db.execute("UPDATE retention_records SET changed_at=3")
    maintenance = Maintenance(tmp_path / "config.json")
    plan = maintenance.plan()
    assert any(item["id"] == task["id"] for item in plan["actions"])
    maintenance.apply(plan)
    assert store.get(task["id"])["result"] is None
    assert store.native_session_owner("codex_cli", "owned")["alias"] == "test"


def test_distinctive_native_originator_is_not_desktop_discovery():
    from answerback_bridge.desktop import peer_from_thread

    assert (
        peer_from_thread(
            {
                "id": "2b8dad88-5c7b-44a8-9125-9d3ee060d352",
                "source": "vscode",
                "originator": "answerback-native-cli",
            }
        )
        is None
    )


async def test_run6_native_queue_is_capped_before_a_receipt_is_reserved(tmp_path: Path) -> None:
    """red_at_base (run-6 hardening h6): every admitted request queued a job, uncapped."""
    from answerback_bridge.providers.native import workers as native_workers

    cap = getattr(native_workers, "MAX_QUEUED_JOBS", 32)
    FakeTransport.opened, FakeTransport.prompts = [], []
    FakeTransport.failure, FakeTransport.wait_permission = None, False
    FakeTransport.never_completes = True
    spaces = {}
    for index in range(cap + 1):
        (tmp_path / f"w{index}").mkdir()
        spaces[f"w{index}"] = str(tmp_path / f"w{index}")
    config = Config(codex_cli_workspaces=spaces, codex_cli_home=str(tmp_path / "native-home"))
    config.select_provider("codex_cli", True)
    path = tmp_path / "config.json"
    config.save(path)
    store = TaskStore(tmp_path / "tasks.sqlite")
    sessions = iter(range(10_000))

    class DistinctSessions(FakeTransport):
        async def open_session(
            self, session_id: str | None = None, mcp_servers: list[dict[str, Any]] | None = None
        ) -> str:
            return session_id or f"owned-{next(sessions)}"

    workers = NativeWorkers("codex_cli", config, path, store, transport_factory=DistinctSessions)
    try:
        first = await workers.submit("w0", "one", "request-0")
        for index in range(1, cap):
            await workers.submit(f"w{index}", "one", f"request-{index}")
        assert len(workers.jobs) == cap
        with pytest.raises(BridgeError, match="queue is full"):
            await workers.submit(f"w{cap}", "one", f"request-{cap}")
        assert store.by_request(f"request-{cap}") is None
        assert (await workers.submit("w0", "one", "request-0"))["id"] == first["id"]
    finally:
        FakeTransport.never_completes = False
        await workers.close()
