"""T-51 fix round: a cancel arriving the instant teardown starts, on the real
adapter classes (plan rule 6; T3, T4, T5 of the specification). T3 and T4 use
the real NativeWorkers plus, respectively, the real ClaudeCLI with the
installed claude_agent_sdk's own Transport seam, and the real GrokCLI with the
real tests/fixtures/grok_acp_agent.py fixture. T5 exercises the real `closed`
accessor of every class that can be a Job.adapter.
"""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from composition import contributed

from answerback_bridge import native_claude
from answerback_bridge.acp_client import ACPClient, KiroACP
from answerback_bridge.config import Config
from answerback_bridge.native_claude import ClaudeCLI
from answerback_bridge.native_codex import CodexCLI
from answerback_bridge.native_grok import GrokCLI
from answerback_bridge.native_workers import NativeWorkers
from answerback_bridge.tasks import TaskStore

# The installed plugin's ACP client is a Job.adapter too (tests/composition.py).
PLUGIN_ADAPTERS = contributed("ACP_CLIENTS", {})
JOB_ADAPTERS = [
    "KiroACP",
    *PLUGIN_ADAPTERS,
    "ClaudeCLI",
    "CodexCLI",
    "GrokCLI",
]

claude_sdk = pytest.importorskip("claude_agent_sdk")


async def ignore_update(_: dict[str, Any]) -> None:
    pass


async def reject_permission(_: dict[str, Any]) -> str | None:
    return None


# --- T3: the Claude window ------------------------------------------------------------


class _ImmediateTerminalPeer(claude_sdk.Transport):
    """Replies to the control handshake, then ends the first turn at once."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def connect(self) -> None:
        pass

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message["type"] == "control_request":
            request = message["request"]
            if request["subtype"] == "initialize":
                await self._queue.put(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": message["request_id"],
                            "response": {
                                "claude_code_version": "2.1.322",
                                "session_state": "idle",
                                "current_permission_mode": "default",
                                "hook_on_failure_supported": True,
                                "hooks_applied": True,
                            },
                        },
                    }
                )
            return
        if message["type"] == "user":
            session, prompt = message["session_id"], message["uuid"]
            await self._queue.put(
                {
                    "type": "result",
                    "session_id": session,
                    "uuid": str(uuid4()),
                    "user_message_uuid": prompt,
                    "user_message_uuids": [prompt],
                    "subtype": "success",
                    "is_error": False,
                    "terminal_reason": "completed",
                    "stop_reason": "end_turn",
                    "result": "Native final answer",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "num_turns": 1,
                }
            )
            await self._queue.put(
                {
                    "type": "system",
                    "subtype": "session_state_changed",
                    "session_id": session,
                    "state": "idle",
                    "sdk_host_only": True,
                }
            )

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while (frame := await self._queue.get()) is not None:
            yield frame

    async def close(self) -> None:
        await self._queue.put(None)

    async def end_input(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True


async def test_cancel_at_the_instant_claude_teardown_starts_returns_clean_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: X3 / Codex F3. ClaudeCLI.__aexit__ creates _close_task before
    its own first await; a cancellation scheduled the moment the worker writes
    native_terminal metadata -- runnable at exactly that instant, before
    _close_task exists under the unfixed accessor -- must see the adapter
    already closed, must never enter ClaudeCLI.cancel(), and the receipt must
    still end completed once the owner task continues.
    """
    monkeypatch.setattr(
        native_claude, "_subprocess_transport", lambda options: _ImmediateTerminalPeer()
    )
    # Hermetic: an empty Claude home of the test's own (no settings.json), so that
    # neither CLAUDE_CONFIG_DIR nor the account's ~/.claude is read. Without it,
    # native_home() falls back to those, and a settings.json over
    # MAX_SETTINGS_BYTES fails the run before the hook below can fire.
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()
    config = Config(
        claude_code_workspaces={"project": str(tmp_path)},
        claude_code_home=str(claude_home),
    )
    config.select_provider("claude_code", True)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    store = TaskStore(tmp_path / "tasks.sqlite")
    workers = NativeWorkers("claude_code", config, config_path, store)

    cancel_calls: list[asyncio.Task[dict[str, Any]]] = []
    closed_at_admission: list[bool | None] = []
    original_metadata = store.metadata

    def metadata_then_cancel(task_id: str, values: dict[str, Any]) -> dict[str, Any]:
        result = original_metadata(task_id, values)
        if "session_id" in values:
            # The worker writes session_id with no await after open_session() has
            # returned: the session is admitted and no teardown can have begun.
            closed_at_admission.append(getattr(workers.jobs[task_id].adapter, "closed", None))
        if "native_terminal" in values:
            cancel_calls.append(asyncio.create_task(workers.cancel(task_id)))
        return result

    monkeypatch.setattr(store, "metadata", metadata_then_cancel)

    cancel_entered = False
    real_cancel = ClaudeCLI.cancel

    async def tracking_cancel(self: ClaudeCLI, session_id: str) -> None:
        nonlocal cancel_entered
        cancel_entered = True
        await real_cancel(self, session_id)

    monkeypatch.setattr(ClaudeCLI, "cancel", tracking_cancel)

    try:
        receipt = await workers.submit("project", "review fixture", "T3-claude-window")
        await asyncio.wait_for(workers.jobs[receipt["id"]].task, 5)
        # Precondition, not the product assertion: the hook fires only when the
        # worker reaches native_terminal. If it did not, name the receipt's reason.
        settled = store.get(receipt["id"])
        assert len(cancel_calls) == 1, (
            "native_terminal was never written, so the cancel was never scheduled: "
            f"receipt status={settled['status']!r} detail={settled['detail']!r}"
        )
        assert closed_at_admission == [False]  # open while its session was admitted
        cancel_result = await cancel_calls[0]
        assert cancel_result["status"] != "failed"
        assert not cancel_entered
        final = store.get(receipt["id"])
        assert final["status"] == "completed"
    finally:
        await workers.close()


# --- T4: the Grok window ---------------------------------------------------------------


@pytest.fixture
def grok_agent(tmp_path: Path) -> tuple[str, str]:
    source = Path(__file__).parent / "fixtures" / "grok_acp_agent.py"
    binary = tmp_path / "fixture-grok"
    binary.write_text(f"#!{sys.executable}\n" + source.read_text())
    binary.chmod(0o700)
    (tmp_path / "grok-settings.json").write_text("{}")
    return str(binary), str(tmp_path)


async def test_cancel_while_the_inner_grok_client_is_closing_returns_clean_receipt(
    grok_agent: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: X4 (Grok section 3(a) / Codex WP-10 table, GrokCLI row). The
    inner _GrokACP closes itself inside _call when the "error" prompt RPC
    fails -- synchronously, before any await -- while the wrapper's own
    _closed flag stays false until GrokCLI.close() runs later. The window is
    held open deterministically on an event the test controls (the inner
    client's own _shutdown), so a cancel issued inside that window must see
    GrokCLI already closed, must return the current receipt without raising,
    and the receipt must then settle uncertain once the owner task continues,
    with exactly the bridge's own detail sentence.
    """
    binary, cwd = grok_agent
    reached_shutdown = asyncio.Event()
    release_shutdown = asyncio.Event()
    original_shutdown = ACPClient._shutdown

    async def held_shutdown(self: ACPClient) -> None:
        reached_shutdown.set()
        await release_shutdown.wait()
        await original_shutdown(self)

    monkeypatch.setattr(ACPClient, "_shutdown", held_shutdown)

    config = Config(grok_binary=binary, grok_workspaces={"project": cwd}, grok_home=cwd)
    config.select_provider("grok", True)
    config_path = Path(cwd) / "config.json"
    config.save(config_path)
    store = TaskStore(Path(cwd) / "tasks.sqlite")
    workers = NativeWorkers("grok", config, config_path, store)

    closed_at_admission: list[bool | None] = []
    original_metadata = store.metadata

    def metadata_then_read_closed(task_id: str, values: dict[str, Any]) -> dict[str, Any]:
        result = original_metadata(task_id, values)
        if "session_id" in values:
            # The worker writes session_id with no await after open_session() has
            # returned: the session is admitted, the "error" prompt is not yet sent,
            # so neither the inner client nor the wrapper can have begun to close.
            closed_at_admission.append(getattr(workers.jobs[task_id].adapter, "closed", None))
        return result

    monkeypatch.setattr(store, "metadata", metadata_then_read_closed)

    try:
        receipt = await workers.submit("project", "error", "T4-grok-window")
        task_id = receipt["id"]
        job = workers.jobs[task_id]
        try:
            await asyncio.wait_for(reached_shutdown.wait(), 5)
            assert job.adapter is not None
            assert closed_at_admission == [False]  # open while its session was admitted
            inner = job.adapter._client  # type: ignore[attr-defined]
            assert inner.closed  # the inner _GrokACP already considers itself closed
            assert not job.adapter._closed  # type: ignore[attr-defined]  # outer still false
            assert job.adapter.closed  # the fix under test: the wrapper reads through
            cancel_result = await workers.cancel(task_id)
            assert cancel_result["status"] == "running"
        finally:
            # Always release the held shutdown, even on an assertion failure above,
            # so the owner task can finish instead of deadlocking workers.close().
            release_shutdown.set()
        await asyncio.wait_for(job.task, 5)
        final = store.get(task_id)
        assert final["status"] == "uncertain"
        # The detail is the bridge's own sentence: the class and the code of the
        # error, and none of the provider's own words (the fixture's message text
        # "Fixture provider failure" must not appear).
        assert final["detail"] == "BridgeError: ACP session/prompt failed: RequestError -32603"
    finally:
        await workers.close()


# --- T5: the real `closed` accessor of every class that can be a Job.adapter -----------


def _adapters(tmp_path: Path) -> dict[str, Any]:
    return {
        "KiroACP": lambda: KiroACP("unused", str(tmp_path), ignore_update, reject_permission),
        **{
            name: lambda build=build: build(tmp_path, ignore_update, reject_permission)
            for name, build in PLUGIN_ADAPTERS.items()
        },
        "ClaudeCLI": lambda: ClaudeCLI("unused", str(tmp_path), ignore_update, reject_permission),
        "CodexCLI": lambda: CodexCLI("unused", str(tmp_path), ignore_update, reject_permission),
        "GrokCLI": lambda: GrokCLI(
            "unused", str(tmp_path), ignore_update, reject_permission, home=str(tmp_path)
        ),
    }


@pytest.mark.parametrize("name", JOB_ADAPTERS)
async def test_closed_is_false_after_construction_and_true_once_teardown_has_started(
    tmp_path: Path, name: str
) -> None:
    """``closed`` is false on a freshly constructed adapter and true from the
    moment ``__aexit__`` has started. No adapter is entered and no session is
    admitted here (the binary is "unused"), so this does not show "false while
    open". That ``closed`` stays false while a session is admitted is pinned
    for ClaudeCLI and GrokCLI only, by the T3 and T4 tests above; KiroACP,
    the plugin's client and CodexCLI have no such test in this file.

    red_at_base for all five: before row T-51 no adapter class had a ``closed``
    accessor (WP-10 added them), so each case failed there. Before the fix
    round of T-51 the KiroACP, plugin, CodexCLI and GrokCLI cases passed (their
    own close()/__aexit__ already set the driving flag synchronously, before
    any await) and only ClaudeCLI failed (X3's own accessor). None of these
    five is a test double standing in for SessionControl: each is the real
    production class that ACPWorkerRuntime.cancel() actually reads
    job.adapter.closed from.
    """
    adapter = _adapters(tmp_path)[name]()
    assert adapter.closed is False
    exit_task = asyncio.create_task(adapter.__aexit__(None, None, None))
    await asyncio.sleep(0)
    assert adapter.closed is True
    await exit_task
    assert adapter.closed is True
