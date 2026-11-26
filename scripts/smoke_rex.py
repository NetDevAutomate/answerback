"""Opt-in owned native Rex smoke. Never imports this fixture into ordinary runtime."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from answerback_bridge.config import atomic_write
from answerback_bridge.mcp_server import create_server
from answerback_bridge.process import BridgeError, run
from answerback_bridge.rex import MAX_SESSIONS, Rex, _full_id
from answerback_bridge.transport import RemoteBridge


def control_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("REX_")}


async def wait_file(path: Path, seconds: float = 25) -> dict:
    async with asyncio.timeout(seconds):
        while not await asyncio.to_thread(path.exists):  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    result = json.loads(await asyncio.to_thread(path.read_text))
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


class Native:
    def __init__(self, binary: str, endpoint: str) -> None:
        self.prefix = [binary, "--autostart=false", "--timeout=2s", "-S", endpoint]
        self.session: str | None = None
        self.close_attempted = False

    async def command(self, *args: str) -> str:
        return (
            await run([*self.prefix, *args], env=control_env(), max_seconds=5, cleanup_seconds=1)
        ).stdout

    async def close(self) -> dict:
        if self.session is None:
            return {"session_created": False}
        target = _full_id(self.session, "session")
        if not self.close_attempted:
            self.close_attempted = True
            await self.command("kill", target)
        listing = Rex._listing(
            json.loads(await self.command("api", "call", "session.list", "{}")),
            "sessions",
            MAX_SESSIONS,
        )
        assert target not in {row["session_id"] for row in listing}, "Owned session still listed"
        self.session = None
        return {"closed_session_id": target, "absence_verified": True}


def validate_read(read: dict) -> None:
    assert read["capture_kind"] == "bounded_prefix"
    assert read["truncated"] is True
    assert read["bytes_returned"] == len(read["text"].encode("utf-8")) <= 1024
    assert "雪🦊" in read["text"]
    assert read["total_bytes"] > read["bytes_returned"]


class AbsenceProofBridge:
    """Expose only the fixture's allowlisted absence diagnostic through MCP."""

    def __init__(self, remote: RemoteBridge) -> None:
        self.remote = remote

    async def call(self, method: str, params: dict) -> dict:
        try:
            return await self.remote.call(method, params)
        except BridgeError as error:
            # MCP masks RuntimeError text. Preserve only this exact, secret-free
            # RPC diagnostic for the owned-session proof; leave other errors masked.
            if method == "rex_receive" and str(error) == "Rex session confirmed absent":
                raise ToolError(str(error)) from error
            raise


def validate_absence(response: CallToolResult) -> dict:
    expected = "Error executing tool rex_receive: Rex session confirmed absent"
    assert (
        response.is_error
        and len(response.content) == 1
        and isinstance(response.content[0], TextContent)
        and response.content[0].text == expected
    ), "Expected exact confirmed-absence MCP refusal"
    # Retain the allowlisted response, omitting arbitrary metadata/structured data.
    return {"isError": True, "content": [{"type": "text", "text": response.content[0].text}]}


async def smoke(root: Path, report: dict) -> None:
    binary = shutil.which("rex")
    assert binary, "Rex is not installed"
    adapter = Rex(binary)
    report["probe"] = await adapter.probe()
    report["cli_version"] = (
        await run([binary, "version", "--json"], env=control_env())
    ).stdout.strip()
    native = Native(binary, report["probe"]["endpoint"])
    prefix = [sys.executable, "-m", "answerback_bridge", "--config", str(root / "config.json")]

    async def bridge(*args: str) -> None:
        await run([*prefix, *args], env=control_env(), max_seconds=30)

    indices = {"a": 0, "b": 0}

    async def peer(name: str, action: str, **params: object) -> dict:
        index = indices[name]
        atomic_write(root / f"{name}.{index}.command", json.dumps(dict(action=action, **params)))
        result = await wait_file(root / f"{name}.{index}.result")
        indices[name] += 1
        return result

    try:
        await bridge(
            "configure",
            "--runtime",
            "rex",
            "--no-codex",
            "--rex",
            binary,
            "--rex-server",
            report["probe"]["endpoint"],
        )
        await bridge("start", "--standalone")
        script = str(Path(__file__).with_name("rex_smoke_peer.py").resolve())
        report["creation_label"] = "bridge-smoke-" + uuid.uuid4().hex[:8]
        report["creation_uncertain"] = True
        created = json.loads(
            await native.command(
                "new",
                report["creation_label"],
                "-c",
                str(root),
                "--shell=none",
                "--json",
                "--",
                sys.executable,
                "-I",
                script,
                str(root),
                "a",
            )
        )
        native.session = _full_id(created["session_id"], "session")
        report["session_creation"] = created
        report["creation_uncertain"] = False
        second = json.loads(
            await native.command(
                "run",
                "-s",
                native.session,
                "--label",
                "peer-b",
                "--focus=false",
                "-c",
                str(root),
                "--shell=none",
                "--json",
                "--",
                sys.executable,
                "-I",
                script,
                str(root),
                "b",
            )
        )
        report["second_creation"] = second
        peers = {name: await wait_file(root / f"{name}.ready") for name in ("a", "b")}
        report["peers"] = peers
        exchanges = []
        for sender, receiver in (("a", "b"), ("b", "a")):
            nonce = "smoke-" + uuid.uuid4().hex
            sent = await peer(sender, "submit", target=peers[receiver]["peer_id"], nonce=nonce)
            if sender == "a":
                await bridge("stop")
                await bridge("start", "--standalone")
                report["restart_between_queue_and_receive"] = True
            handled = await peer(receiver, "receive", nonce=nonce)
            assert handled["queued"]["status"] == "queued"
            assert handled["ack"]["status"] == "submitted"
            assert handled["completed"]["status"] == "completed"
            assert handled["retry"]["id"] == sent["id"]
            for receipt in (
                sent,
                handled["queued"],
                handled["ack"],
                handled["completed"],
                handled["retry"],
            ):
                assert receipt["id"] == sent["id"]
                assert receipt["request_key"] == nonce
                assert receipt["target"] == peers[receiver]["peer_id"]
                assert receipt["metadata"]["peer_id"] == peers[receiver]["peer_id"]
                assert receipt["metadata"]["kind"] == "rex"
            assert handled["ack"]["metadata"]["acknowledged_by"] == peers[receiver]["peer_id"]
            retrieved = await peer(sender, "get", task_id=sent["id"], nonce=nonce)
            again = await peer(sender, "submit", target=peers[receiver]["peer_id"], nonce=nonce)
            assert again["id"] == sent["id"]
            exchanges.append(dict(sent=sent, handled=handled, retrieved=retrieved, retry=again))
        report["exchanges"] = exchanges
        await peer("a", "output")
        async with Client(
            create_server(AbsenceProofBridge(RemoteBridge(root / "config.json")))
        ) as client:
            tasks = (await client.call_tool("task_list", {"limit": 30})).structured_content
            report["task_list"] = tasks
            assert len(tasks["tasks"]) == 2
            read = await client.call_tool(
                "rex_read",
                dict(
                    session_id=native.session,
                    block_id=created["initial_windows"][0]["block_ids"][0],
                    max_bytes=1024,
                ),
            )
            assert not read.is_error, read
            validate_read(read.structured_content)
            report["bounded_read"] = read.structured_content
            report["cleanup_native"] = await native.close()
            absent = await client.call_tool("rex_receive", {"peer_id": peers["a"]["peer_id"]})
            report["absence_receive_response"] = validate_absence(absent)
            report["absence_receive_refused"] = True
            saved = await client.call_tool("task_get", {"task_id": exchanges[0]["sent"]["id"]})
            assert saved.structured_content["status"] == "completed"
            report["completed_receipt_after_close"] = saved.structured_content
    finally:
        errors = []
        try:
            if native.session:
                report["cleanup_native"] = await native.close()
        except Exception as error:
            errors.append(str(error))
            report["cleanup_target"] = native.session
        try:
            await bridge("stop")
            report["fixture_bridge_stopped"] = True
        except Exception as error:
            errors.append(str(error))
        if errors:
            report["cleanup_errors"] = errors
            raise RuntimeError("Fixture cleanup failed: " + "; ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.report.exists() and not args.overwrite:
        parser.error("Report exists; choose an unused path or --overwrite")
    report = {
        "status": "failed",
        "scope": "one owned local session, two scripted MCP peers",
        "limitations": [
            "No real LLM harness compatibility or remote support proved",
            "Capture is bounded prefix; tail offsets and server computation unproved",
        ],
    }
    directory = tempfile.mkdtemp(prefix="hbr-", dir="/tmp")
    report["fixture_directory"] = directory
    try:
        asyncio.run(smoke(Path(directory), report))
        report["status"] = "passed"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        if report.get("cleanup_errors") or report.get("creation_uncertain"):
            report["temporary_resources_removed"] = False
        else:
            shutil.rmtree(directory)
            report["temporary_resources_removed"] = not Path(directory).exists()
        atomic_write(args.report, json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"status": report["status"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
