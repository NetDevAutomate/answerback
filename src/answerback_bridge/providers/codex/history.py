"""Queue through Codex; read persisted history without resuming its execution."""

import asyncio
import json
import math
import os
import re
from typing import Any

from ...process import BridgeError, ChildProcess, codex_env, run, settle, spawn_owned, stop_child

READ_METHODS = {"thread/list", "thread/read", "thread/turns/list", "thread/items/list"}


def task_marker(task_id: str) -> str:
    return f"[answerback-task:{task_id}]"


def _legacy_marker(task_id: str) -> str:
    return f"[codex-herdr-task:{task_id}]"


def queued_submission_id(receipt: str) -> str | None:
    """The id in Codex's ``Queued message ID for thread THREAD.`` receipt, or None.

    It names the record in Codex's queue, which Codex deletes when the turn starts
    (``codex-rs/ext/queue/src/service.rs``, ``start``). Nothing in the thread history
    carries it, so it is kept as evidence of the submission, not used to find the turn.
    """
    match = re.fullmatch(r"Queued message (\S+) for thread \S+\.?", receipt.strip())
    return match.group(1) if match else None


def user_text(item: dict[str, Any]) -> str:
    return "\n".join(
        part.get("text", "")
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


def has_terminal_timestamp(turn: dict[str, Any]) -> bool:
    """Distinguish a recorded end from the cold reader's synthetic interruption.

    A separate app-server projects an actively owned Desktop turn as interrupted,
    but leaves completedAt null. Recorded task_complete/turn_aborted events supply
    that timestamp. Older/incomplete projections remain unconfirmed.
    """
    completed = turn.get("completedAt")
    if (
        not isinstance(completed, (int, float))
        or isinstance(completed, bool)
        or not math.isfinite(completed)
        or completed < 0
    ):
        return False
    started = turn.get("startedAt")
    return started is None or (
        type(started) in (int, float) and math.isfinite(started) and started <= completed
    )


def _started_at_or_after(turn: dict[str, Any], floor: int | float) -> bool:
    started = turn.get("startedAt")
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return False
    return math.isfinite(started) and started >= floor


def _user_texts(turn: dict[str, Any]) -> list[str]:
    return [
        user_text(item)
        for item in turn.get("items", [])
        if isinstance(item, dict) and item.get("type") == "userMessage"
    ]


def submitted_turns(
    task_id: str, message: str, floor: int | float, turns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every turn in ``turns`` that could be this bridge's own submission.

    ``codex queue`` returns no turn identity (its receipt names a queue record that is
    gone once the turn starts), so the turn is found by what only the bridge knows: the
    whole text it queued, marker line and private message body, in a turn that started
    at or after the submission. The marker line alone is public (every receipt shows the
    task id) and is never enough.
    """
    expected = {
        f"{task_marker(task_id)}\n{message}".rstrip(),
        f"{_legacy_marker(task_id)}\n{message}".rstrip(),
    }
    return [
        turn
        for turn in turns
        if _started_at_or_after(turn, floor)
        and any(text.rstrip() in expected for text in _user_texts(turn))
    ]


def submitted_turn(
    task_id: str, message: str, floor: int | float, turns: list[dict[str, Any]]
) -> str | None:
    """The id of the one turn this bridge's queue call produced, or None.

    Two such turns bind nothing: a copy made after the message appeared cannot be told
    from the original. The caller must remember that it saw two (``CodexAdapter.
    observe_native`` does): a later window that holds only the copy would otherwise
    bind it.
    """
    matching = submitted_turns(task_id, message, floor, turns)
    if len(matching) != 1:
        return None
    ident = matching[0].get("id")
    return ident if isinstance(ident, str) and ident else None


def result_from_turns(
    task_id: str, turn_id: str, turns: list[dict[str, Any]]
) -> tuple[str, str, str] | None:
    """Select a result only from the bound turn, found exactly once in this window.

    The bound turn is the one :func:`submitted_turn` identified. A marked turn that is
    not it, however alone on the page, is never a result.
    """
    matching = [turn for turn in turns if turn.get("id") == turn_id]
    if len(matching) != 1:
        return None
    turn = matching[0]
    prefixes = (task_marker(task_id) + "\n", _legacy_marker(task_id) + "\n")
    if not any(text.startswith(prefixes) for text in _user_texts(turn)):
        return None
    status = turn.get("status")
    if status in ("failed", "interrupted"):
        if not has_terminal_timestamp(turn):
            return None
        return "failed", f"Codex turn {status}", turn["id"]
    if status != "completed":
        return None
    finals = [
        item["text"]
        for item in turn.get("items", [])
        if item.get("type") == "agentMessage" and item.get("phase") == "final_answer"
    ]
    # Missing phase is deliberately not guessed to be a completed answer.
    if finals:
        return "completed", "\n\n".join(finals), turn["id"]
    return None


class HistoryReader:
    """Owns only a disposable read-only protocol connection, never a Desktop turn."""

    def __init__(self, binary: str, home: str | None = None) -> None:
        self.binary = binary
        self.home = home
        self.proc: ChildProcess | None = None
        self.sequence = 0
        self.diagnostics = bytearray()
        self.stderr_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "HistoryReader":
        self.proc = await spawn_owned(
            self.binary,
            "app-server",
            "--stdio",
            env=codex_env(self.home),
            start_new_session=os.name == "posix",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        self.stderr_task = asyncio.create_task(self._read_stderr())
        try:
            await self._request(
                "initialize",
                {"clientInfo": {"name": "codex_herdr_history_reader", "version": "0.1.0"}},
            )
            await self._send({"method": "initialized", "params": {}})
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        proc, stderr_task = self.proc, self.stderr_task
        if proc is None:
            return

        async def cleanup() -> None:
            try:
                await stop_child(proc)
            finally:
                if stderr_task:
                    try:
                        await asyncio.wait_for(stderr_task, 1)
                    except TimeoutError:
                        stderr_task.cancel()
                        await asyncio.gather(stderr_task, return_exceptions=True)

        try:
            if proc.stdin:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except TimeoutError:
                pass
        finally:
            await settle(asyncio.create_task(cleanup()))

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while chunk := await self.proc.stderr.read(4096):
            self.diagnostics.extend(chunk)
            del self.diagnostics[:-4000]

    async def _send(self, value: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(value) + "\n").encode())
        await self.proc.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        self.sequence += 1
        request_id = self.sequence
        await self._send({"id": request_id, "method": method, "params": params})
        try:
            async with asyncio.timeout(25):
                while raw := await self.proc.stdout.readline():
                    message = json.loads(raw)
                    if message.get("id") == request_id and "method" not in message:
                        if "error" in message:
                            raise BridgeError(f"Codex {method}: {message['error']}")
                        return message["result"]
                    if "id" in message and "method" in message:
                        await self._send(
                            {
                                "id": message["id"],
                                "error": {"code": -32601, "message": "Read-only history client"},
                            }
                        )
        except TimeoutError as error:
            raise BridgeError("Codex history read timed out") from error
        detail = self.diagnostics.decode(errors="replace").strip()
        raise BridgeError(
            "Codex history reader exited before responding. "
            "If execution permissions were denied, use your host's normal approval flow. " + detail
        )

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in READ_METHODS:
            raise ValueError("History reader permits only read methods")
        return await self._request(method, params)

    async def turns(self, thread_id: str, *, pages: int = 5) -> list[dict[str, Any]]:
        turns, _ = await self.scan(thread_id, pages=pages)
        return turns

    async def scan(
        self, thread_id: str, *, pages: int = 5, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        turns: list[dict[str, Any]] = []
        for _ in range(pages):
            result = await self.request(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 20,
                    "itemsView": "full",
                    "sortDirection": "desc",
                    "cursor": cursor,
                },
            )
            turns.extend(result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return turns, cursor


async def queue(
    binary: str, thread_id: str, task_id: str, message: str, home: str | None = None
) -> str:
    output = await run(
        [
            binary,
            "queue",
            "--thread",
            thread_id,
            "--message",
            f"{task_marker(task_id)}\n{message}",
        ],
        env=codex_env(home),
    )
    if "Queued message " not in output.stdout or thread_id not in output.stdout:
        raise BridgeError("Codex did not return a recognizable queue receipt")
    return output.stdout.strip()
