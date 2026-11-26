import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.codex import HistoryReader, queue
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.process import BridgeError, run
from codex_herdr_bridge.service import Bridge


def test_equivalent_chat_ids_share_one_outstanding_assignment(tmp_path: Path) -> None:
    chat = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    config = Config("test", desktop_threads={"first": chat, "second": chat.upper()})
    bridge = Bridge(config, tmp_path / "config.json")
    bridge.store.reserve(f"desktop:{config.thread('first')}", "First", "first")
    with pytest.raises(ValueError, match="already has task"):
        bridge.store.reserve(f"desktop:{config.thread('second')}", "Second", "second")


async def test_queue_and_reader_use_the_selected_codex_home(tmp_path: Path) -> None:
    fake = tmp_path / "codex"
    selected_home = str(tmp_path / "selected-home")
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys,os,json\n"
        f"assert os.environ['CODEX_HOME'] == {selected_home!r}\n"
        "if sys.argv[1] == 'queue':\n"
        "    print('Queued message test for thread ' + sys.argv[sys.argv.index('--thread')+1])\n"
        "else:\n"
        "    for line in sys.stdin:\n"
        "        request=json.loads(line)\n"
        "        if 'id' in request:\n"
        "            reply={'id':request['id'],'result':{'selected':True}}\n"
        "            print(json.dumps(reply),flush=True)\n"
    )
    fake.chmod(0o700)
    assert "Queued message" in await queue(
        str(fake), "test-thread", "task", "message", selected_home
    )
    async with HistoryReader(str(fake), selected_home) as reader:
        assert await reader.request("thread/read", {"threadId": "test-thread"}) == {
            "selected": True
        }
        with pytest.raises(ValueError, match="only read"):
            await reader.request("turn/start", {})


async def test_timed_out_launcher_cannot_leave_a_child_writing_files(tmp_path: Path) -> None:
    marker = tmp_path / "unexpected-marker"
    child = (
        "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"time.sleep(0.7); pathlib.Path({str(marker)!r}).write_text('leaked')"
    )
    parent = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "time.sleep(10)"
    )
    with pytest.raises(BridgeError, match="timed out"):
        await run([sys.executable, "-c", parent], max_seconds=0.25)
    await asyncio.sleep(0.8)
    assert not marker.exists()


async def test_old_completion_is_found_on_a_later_history_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    bridge = Bridge(
        Config("test", codex_enabled=True, desktop_threads={"reviewer": chat}),
        tmp_path / "config.json",
    )
    task, _ = bridge.store.reserve(
        f"desktop:{chat}",
        "Review",
        "review",
        {"kind": "desktop", "alias": "reviewer", "thread_id": chat},
    )

    class FakeReader:
        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeReader":
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def scan(
            self, thread_id: str, *, pages: int = 5, cursor: str | None = None
        ) -> tuple[list[dict[str, Any]], str | None]:
            if cursor is None:
                return [], "page2"
            if cursor == "page2":
                return [], "page6"
            assert cursor == "page6"
            return [
                {
                    "id": "matching-turn",
                    "status": "completed",
                    # Started after the receipt was created (T-189 history binding).
                    "startedAt": 4_000_000_000,
                    "completedAt": 4_000_000_001,
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [
                                {"type": "text", "text": f"[codex-herdr-task:{task['id']}]\nReview"}
                            ],
                        },
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Found older answer",
                        },
                    ],
                }
            ], None

    monkeypatch.setattr("answerback_bridge.providers.codex.history.HistoryReader", FakeReader)
    first = await bridge.get_task(task["id"])
    assert first["metadata"]["history_search_incomplete"] is True
    second = await bridge.get_task(task["id"])
    assert second["status"] == "completed"
    assert second["result"] == "Found older answer"


async def test_changed_herdr_delivery_identity_is_uncertain(tmp_path: Path) -> None:
    fake = tmp_path / "herdr"
    get_result = {
        "result": {
            "agent": {
                "terminal_id": "expected-terminal",
                "pane_id": "old-pane",
                "agent_status": "idle",
            }
        }
    }
    prompt_result = {"result": {"agent": {"terminal_id": "different-terminal"}}}
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys,json\n"
        "if '--version' in sys.argv: print('herdr test')\n"
        "elif '--help' in sys.argv: print('agent prompt')\n"
        f"elif 'get' in sys.argv: print({json.dumps(json.dumps(get_result))})\n"
        "else:\n"
        "    assert sys.argv[sys.argv.index('prompt')+1] == 'named-worker'\n"
        f"    print({json.dumps(json.dumps(prompt_result))})\n"
    )
    fake.chmod(0o700)
    bridge = Bridge(Config("test", str(fake)), tmp_path / "config.json")
    receipt = await bridge.herdr_submit(
        "named-worker", "Review", "review", expected={"terminal_id": "expected-terminal"}
    )
    assert receipt["status"] == "uncertain"
    assert "different-terminal" in receipt["detail"]
    with pytest.raises(ValueError, match="identity was invalidated"):
        bridge.complete(receipt["id"], "Wrong recipient finished")
    assert bridge.store.get(receipt["id"])["status"] == "uncertain"


def test_result_can_reconcile_lost_ack_but_not_changed_identity(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")
    task, _ = bridge.store.reserve("herdr:test", "Review", "review", {"kind": "herdr"})
    bridge.store.update(task["id"], "uncertain", detail="Lost acknowledgement")
    assert bridge.complete(task["id"], "Done")["status"] == "completed"
    bridge.store.invalidate_submission(task["id"], "Different terminal in late delivery receipt")
    with pytest.raises(ValueError, match="identity was invalidated"):
        bridge.complete(task["id"], "Done")
