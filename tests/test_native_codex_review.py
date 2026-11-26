"""Independent adversarial review cases; only disposable protocol peers run."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.native_codex import CodexCLI
from answerback_bridge.process import BridgeError


@pytest.fixture
def codex_review_agent(tmp_path: Path) -> tuple[str, str]:
    source = (Path(__file__).parent / "fixtures" / "native_codex_agent.py").read_text()
    source = source.replace("import sys\n", "import sys\nimport time\n")
    source = source.replace(
        '        events("after_response")',
        '        if settings.get("review_gate"):\n'
        '            while not (root / "review-release").exists():\n'
        "                time.sleep(0.005)\n"
        '        events("after_response")',
    )
    source = source.replace(
        "    for value in settings.get(name, []):\n        send(replace(value))",
        "    rows = [json.dumps(replace(value)) for value in settings.get(name, [])]\n"
        '    if rows:\n        sys.stdout.write("\\n".join(rows) + "\\n")\n'
        "        sys.stdout.flush()",
    )
    binary = tmp_path / "review-codex"
    binary.write_text(f"#!{sys.executable}\n" + source)
    binary.chmod(0o700)
    (tmp_path / "codex-settings.json").write_text("{}")
    return str(binary), str(tmp_path)


def configure(agent: tuple[str, str], **settings: Any) -> None:
    (Path(agent[1]) / "codex-settings.json").write_text(json.dumps(settings))


async def ignore_update(update: dict[str, Any]) -> None:
    pass


async def no_permission(request: dict[str, Any]) -> None:
    return None


def event(method: str, **params: Any) -> dict[str, Any]:
    return {"method": method, "params": {"threadId": "$thread", **params}}


def finished(status: str) -> dict[str, Any]:
    return event("turn/completed", turn={"id": "$turn", "status": status, "items": []})


async def test_conflicting_terminal_batch_cannot_be_reported_as_completed(
    codex_review_agent,
) -> None:
    configure(
        codex_review_agent,
        review_gate=True,
        hold=True,
        after_response=[
            event(
                "item/completed",
                turnId="$turn",
                item={
                    "id": "answer",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "first result",
                },
            ),
            finished("completed"),
            finished("failed"),
        ],
    )
    async with CodexCLI(*codex_review_agent, ignore_update, no_permission) as client:
        session = await client.open_session()
        prompt = asyncio.create_task(client.prompt(session, "review fixture", prompt_id="review"))
        try:
            await asyncio.sleep(0)
            state = client._active
            assert state is not None
            await asyncio.wait_for(state.ready.wait(), 3)
            await asyncio.to_thread((Path(codex_review_agent[1]) / "review-release").touch)
            with pytest.raises(BridgeError, match="conflicting"):
                await asyncio.wait_for(prompt, 3)
        finally:
            prompt.cancel()
            await asyncio.gather(prompt, return_exceptions=True)


async def test_whitespace_final_answer_remains_unavailable(codex_review_agent) -> None:
    configure(codex_review_agent, answer=" \n\t ")
    async with CodexCLI(*codex_review_agent, ignore_update, no_permission) as client:
        result = await client.prompt(
            await client.open_session(), "review fixture", prompt_id="review"
        )
        assert result.status == "completed"
        assert result.text is None


async def test_cancel_invalidates_pending_permission_before_late_human_answer(
    codex_review_agent,
) -> None:
    configure(
        codex_review_agent,
        hold=True,
        cancel_ack_only=True,
        permissions=[
            {
                "id": "pending-approval",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "$thread",
                    "turnId": "$turn",
                    "itemId": "command",
                    "startedAtMs": 123,
                    "command": "echo fixture",
                    "cwd": codex_review_agent[1],
                },
            }
        ],
    )
    waiting = asyncio.Event()
    late_answer = asyncio.Event()
    callback_finished = asyncio.Event()

    async def choose(request):
        waiting.set()
        try:
            await late_answer.wait()
            return "accept"
        finally:
            callback_finished.set()

    async with CodexCLI(*codex_review_agent, ignore_update, choose) as client:
        session = await client.open_session()
        prompt = asyncio.create_task(client.prompt(session, "review fixture", prompt_id="review"))
        try:
            await asyncio.wait_for(waiting.wait(), 3)
            approvals = list(client._permissions.values())
            await client.cancel(session)
            late_answer.set()
            await asyncio.wait_for(callback_finished.wait(), 3)
            # Wait for the callback's outgoing send, without requiring a model
            # terminal event or assuming that the interrupt acknowledgement is one.
            await asyncio.wait_for(asyncio.gather(*approvals, return_exceptions=True), 3)
            await asyncio.sleep(0.03)
            rows = [
                json.loads(line)
                for line in (Path(codex_review_agent[1]) / "codex-requests.jsonl")
                .read_text()
                .splitlines()
            ]
            assert not any(
                row.get("id") == "pending-approval"
                and row.get("result", {}).get("decision") == "accept"
                for row in rows
            )
        finally:
            prompt.cancel()
            await asyncio.gather(prompt, return_exceptions=True)
