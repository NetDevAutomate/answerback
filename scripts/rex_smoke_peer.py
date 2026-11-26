"""Long-lived cooperative fixture; command files contain only coordination metadata."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client

from answerback_bridge.config import atomic_write
from answerback_bridge.mcp_server import create_server
from answerback_bridge.transport import RemoteBridge


def message(nonce: str) -> str:
    return f'{nonce}\nUnicode 雪 🦊 café; "quotes" and `backticks`'


async def main() -> None:
    root, name = Path(sys.argv[1]), sys.argv[2]
    async with Client(create_server(RemoteBridge(root / "config.json"))) as client:

        async def call(tool: str, **params: object) -> dict:
            response = await client.call_tool(tool, params)
            if response.is_error:
                raise RuntimeError(str(response.content))
            return response.structured_content

        peer = await call(
            "rex_enroll",
            name=name,
            session_id=os.environ["REX_SESSION"],
            block_id=os.environ["REX_BLOCK"],
            request_id=name,
        )
        atomic_write(root / f"{name}.ready", json.dumps(peer))
        index = 0
        while True:
            command = root / f"{name}.{index}.command"
            if not command.exists():
                await asyncio.sleep(0.05)
                continue
            instruction = json.loads(command.read_text())
            try:
                action = instruction["action"]
                if action == "submit":
                    result = await call(
                        "peer_submit",
                        selector=instruction["target"],
                        message=message(instruction["nonce"]),
                        request_id=instruction["nonce"],
                    )
                elif action == "receive":
                    inbox = await call("rex_receive", peer_id=peer["peer_id"])
                    task = inbox["tasks"][0]
                    assert task["message"] == message(instruction["nonce"])
                    assert task["request_key"] == instruction["nonce"]
                    assert task["target"] == peer["peer_id"]
                    assert task["metadata"]["peer_id"] == peer["peer_id"]
                    assert task["metadata"]["kind"] == "rex"
                    queued = await call("task_get", task_id=task["id"])
                    ack = await call("rex_ack", peer_id=peer["peer_id"], task_id=task["id"])
                    completed = await call(
                        "rex_complete",
                        peer_id=peer["peer_id"],
                        task_id=task["id"],
                        result=message(instruction["nonce"]),
                    )
                    retry = await call(
                        "rex_complete",
                        peer_id=peer["peer_id"],
                        task_id=task["id"],
                        result=message(instruction["nonce"]),
                    )
                    result = dict(queued=queued, ack=ack, completed=completed, retry=retry)
                elif action == "get":
                    result = await call("task_wait", task_id=instruction["task_id"], seconds=1)
                    assert result["result"] == message(instruction["nonce"])
                elif action == "output":
                    print(("雪🦊 café " * 4000), flush=True)
                    result = {"output_emitted": True}
                else:
                    raise ValueError(action)
                atomic_write(root / f"{name}.{index}.result", json.dumps(result))
            except Exception as error:
                atomic_write(root / f"{name}.{index}.result", json.dumps({"error": str(error)}))
            index += 1


if __name__ == "__main__":
    asyncio.run(main())
