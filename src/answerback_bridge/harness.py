"""Two-direction collaboration checks with correlated, durable task receipts."""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, cast

from . import lifecycle
from .config import Config
from .discovery import matches
from .exit_codes import EXIT_BLOCKED, EXIT_FAILURE, EXIT_WAIT_TIMED_OUT
from .process import BridgeError, module_launch_argv, run
from .tasks import TERMINAL
from .transport import RemoteBridge

STOP_STATES = {*TERMINAL, "blocked", "uncertain", "needs_selection", "provider_backoff"}
PREFIX = "HERDR_BRIDGE_HARNESS "


class HarnessFailure(Exception):
    """A bounded diagnostic safe to show without dumping peer output or credentials."""


def _prepare_simulation(root: Path) -> Path:
    for filename, provider in (("fake-herdr", "herdr"), ("fake-kiro", "acp")):
        binary = root / filename
        binary.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            "from answerback_bridge.testing.peers import main\n"
            f"main({provider!r}, Path(__file__).resolve().parent)\n"
        )
        binary.chmod(0o700)
    config_path = root / "config.json"
    Config(
        "harness",
        herdr_binary=str(root / "fake-herdr"),
        codex_binary=str(root / "fake-herdr"),
        kiro_binary=str(root / "fake-kiro"),
        kiro_workspaces={"harness-kiro": str(root)},
    ).save(config_path)
    return config_path


class Run:
    def __init__(self, config_path: Path, mode: str, timeout: int) -> None:
        self.config_path = config_path
        self.mode = mode
        self.deadline = time.monotonic() + timeout
        self.run_id = f"harness-{uuid.uuid4().hex}"
        self.remote: RemoteBridge
        self.phase: dict[str, Any] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.submissions: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {
            "mode": mode,
            "success": False,
            "run_id": self.run_id,
            "phases": [],
            "outstanding_task_ids": [],
            "limitations": [],
        }

    def begin(self, name: str) -> None:
        self.phase = {"name": name, "status": "running"}
        self.report["phases"].append(self.phase)

    def passed(self, **fields: Any) -> None:
        self.phase.update(status="passed", **fields)

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessFailure("Harness deadline expired; inspect outstanding receipts")
        return remaining

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with asyncio.timeout(min(28, self.remaining())):
            result = await self.remote.call(method, params)
        if "id" in result and "status" in result:
            self.record(result)
        return result

    def record(self, task: dict[str, Any]) -> None:
        self.receipts[task["id"]] = task
        for phase in self.report["phases"]:
            if task["id"] == phase.get("parent_task_id"):
                phase["parent_status"] = task["status"]
            if task.get("request_key") and task["request_key"] == phase.get("child_request_id"):
                phase.update(child_task_id=task["id"], child_status=task["status"])

    async def observe_child(self) -> None:
        request_id = self.phase.get("child_request_id")
        if not request_id:
            return
        found = await self.call("task_list", {"query": request_id, "limit": 100})
        for child in found["tasks"]:
            if child.get("request_key") != request_id:
                continue
            child = await self.call("task_get", {"task_id": child["id"]})
            self.record(child)
            if child["status"] != "completed" and child["status"] in STOP_STATES:
                self.phase["outcome"] = child["status"]
                raise HarnessFailure(
                    f"Child task stopped with {child['status']}; inspect its receipt"
                )
            if child.get("metadata", {}).get("kind") == "herdr" and child["status"] != "completed":
                selected = await self.select(f"herdr:{child['metadata']['target']}", "herdr")
                if selected.get("state") == "blocked":
                    self.phase["outcome"] = "blocked"
                    raise HarnessFailure("Herdr child is blocked; human action is required")

    async def select(self, selector: str, provider: str) -> dict[str, Any]:
        prefix, separator, _ = selector.partition(":")
        if separator and prefix in ("herdr", "kiro", "crew", "desktop") and prefix != provider:
            raise HarnessFailure(f"The {provider} target selects a different provider")
        discovery = await self.call("peer_search", {"provider": provider, "query": ""})
        if not discovery.get("complete"):
            raise HarnessFailure(f"The {provider} peer inventory is incomplete; no dispatch")
        exact = [p for p in discovery["peers"] if p["peer_id"] == selector]
        found = exact or [p for p in discovery["peers"] if matches(p, selector)]
        if len(found) != 1:
            raise HarnessFailure(f"Select one unambiguous {provider} peer; found {len(found)}")
        expected = "interactive" if provider == "herdr" else "bridge_managed"
        if found[0].get("provider") != provider or found[0].get("kind") != expected:
            raise HarnessFailure(f"The selected {provider} peer has an unsupported kind")
        return found[0]

    async def ready(self, herdr: dict[str, Any], kiro: dict[str, Any]) -> None:
        if herdr.get("state") not in ("idle", "done"):
            raise HarnessFailure("Selected Herdr agent must be idle/done; no dispatch")
        tasks = await self.call("task_list", {"limit": 100})
        for task in tasks["tasks"]:
            if task["status"] in TERMINAL:
                continue
            task = await self.call("task_get", {"task_id": task["id"]})
            meta = task.get("metadata", {})
            selected = (meta.get("kind") == "herdr" and meta.get("target") == herdr["target"]) or (
                meta.get("kind") == "kiro" and meta.get("alias") == kiro["target"]
            )
            if selected and task["status"] not in TERMINAL:
                self.record(task)
                raise HarnessFailure("A selected peer has outstanding work; no dispatch")

    async def settled_herdr(self, expected: dict[str, Any]) -> dict[str, Any]:
        # A worker can report its result before rendering its final answer and becoming idle.
        async with asyncio.timeout(min(20, self.remaining())):
            while True:
                current = await self.select(expected["peer_id"], "herdr")
                for key in ("terminal_id", "agent_session"):
                    if expected.get(key) is not None and current.get(key) != expected[key]:
                        raise HarnessFailure(
                            "Herdr peer changed after completion; no reverse probe"
                        )
                if current.get("state") in ("idle", "done"):
                    return current
                if current.get("state") != "working":
                    raise HarnessFailure("Herdr peer is blocked or unknown; no reverse probe")
                await asyncio.sleep(min(0.2, self.remaining()))

    async def wait(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if "id" not in receipt:
            raise HarnessFailure("Submission returned no task ID; no automatic resubmission")
        task_id = receipt["id"]
        self.record(receipt)
        while receipt.get("status") != "completed":
            await self.observe_child()
            if receipt.get("status") in STOP_STATES:
                self.phase["outcome"] = receipt["status"]
                raise HarnessFailure(f"Task stopped with {receipt['status']}; inspect its receipt")
            seconds = max(1, min(2, int(self.remaining())))
            receipt = await self.call("task_wait", {"task_id": task_id, "seconds": seconds})
            if receipt.get("id") != task_id:
                raise HarnessFailure("Wait returned a receipt for a different task")
            if receipt.get("metadata", {}).get("kind") == "herdr":
                selected = await self.select(f"herdr:{receipt['metadata']['target']}", "herdr")
                if selected.get("state") == "blocked":
                    self.phase["outcome"] = "blocked"
                    raise HarnessFailure("Herdr agent is blocked; human action is required")
        return receipt

    async def child(self, request_id: str, peer: dict[str, Any]) -> dict[str, Any]:
        result = await self.call("task_list", {"query": request_id, "limit": 100})
        children = [task for task in result["tasks"] if task.get("request_key") == request_id]
        if len(children) != 1:
            raise HarnessFailure("The expected child receipt was not recorded exactly once")
        receipt = await self.call("task_get", {"task_id": children[0]["id"]})
        self.record(receipt)
        self.phase["child_task_id"] = receipt["id"]
        meta = receipt.get("metadata", {})
        actual = meta.get("alias") if peer["provider"] == "kiro" else meta.get("target")
        if meta.get("kind") != peer["provider"] or actual != peer["target"]:
            raise HarnessFailure("Child receipt does not belong to the selected peer")
        return await self.wait(receipt)

    def message(
        self, action: str, nonce: str, request_id: str, peer: dict[str, Any], child_request: str
    ) -> str:
        if self.mode == "simulated":
            return PREFIX + json.dumps(
                {
                    "action": action,
                    "nonce": nonce,
                    "request_id": request_id,
                    "child_selector": peer["peer_id"],
                    "child_binding": peer.get("binding"),
                    "child_request_id": child_request,
                    "deadline": self.deadline,
                }
            )
        child_message = (
            "The human authorized this reply-only bridge collaboration check. "
            f"Return exactly this nonce and no additional text: {nonce}. "
            "Follow any completion receipt instructions appended by the bridge. "
            "Do not perform other work, change project files, or contact other agents."
        )
        if peer["provider"] == "kiro":
            command = shlex.join(
                module_launch_argv(
                    self.config_path,
                    "send",
                    peer["peer_id"],
                    "--message",
                    child_message,
                    "--request-id",
                    child_request,
                )
            )
            wait_command = shlex.join(module_launch_argv(self.config_path, "task"))
            instruction = (
                f"Run this argument-quoted bridge CLI command once:\n{command}\n"
                "Retain its returned child task ID. Poll that exact receipt with "
                f"{wait_command} CHILD_TASK_ID --wait 2. "
                "Its exit code tells you what to do next: 0 means the child completed, "
                f"read the result; {EXIT_WAIT_TIMED_OUT} means the wait ran out, run the "
                f"same poll again; {EXIT_BLOCKED} means a human permission is pending, "
                "stop and report the receipt. "
                f"{EXIT_FAILURE} means the child failed, was cancelled or abandoned, its "
                "delivery is uncertain, or the selection was ambiguous: stop polling and "
                "report the receipt. "
            )
        else:
            arguments = {
                "selector": peer["peer_id"],
                "message": child_message,
                "request_id": child_request,
                "binding": peer.get("binding"),
            }
            instruction = (
                "Use the supplied answerback_bridge MCP server. Call peer_submit once with: "
                f"{json.dumps(arguments)}. Retain its returned child task ID and poll "
                "that exact ID with task_wait, seconds=2. "
            )
        return (
            "The human explicitly authorized this bounded bridge collaboration check. "
            + instruction
            + f"Stop polling after {max(1, int(self.remaining()))} seconds. "
            "Require status completed and result equal to the nonce. If blocked, failed, "
            "uncertain, unavailable, or timed out, stop and report that outcome. Do not "
            "resubmit, answer approval prompts, trust tools, create/stop sessions, or start "
            "other agents. After verified child completion, return exactly "
            f"{nonce}. If a parent completion command follows, write exactly that nonce "
            "to a temporary UTF-8 result file and execute that command with its path. "
            "Do not mark the parent complete without the matching child receipt."
        )

    async def round_trip(
        self, name: str, parent_peer: dict[str, Any], child_peer: dict[str, Any]
    ) -> None:
        self.begin(name)
        nonce = f"{self.run_id}-{name}-{uuid.uuid4().hex}"
        request_id = f"{self.run_id}-{name}"
        child_request = request_id + "-child"
        self.phase.update(
            nonce=nonce,
            parent_peer=parent_peer["peer_id"],
            child_peer=child_peer["peer_id"],
            request_id=request_id,
            child_request_id=child_request,
        )
        params = {
            "selector": parent_peer["peer_id"],
            "binding": parent_peer.get("binding"),
            "request_id": request_id,
            "message": self.message("delegate", nonce, request_id, child_peer, child_request),
        }
        self.submissions.append(params)
        receipt = await self.call("peer_submit", params)
        if receipt.get("id"):
            self.phase["parent_task_id"] = receipt["id"]
        self.phase["parent_status"] = receipt.get("status", "unknown")
        parent = await self.wait(receipt)
        self.phase["parent_status"] = parent["status"]
        child = await self.child(child_request, child_peer)
        self.phase["child_status"] = child["status"]
        if parent.get("result", "").strip() != nonce or child.get("result", "").strip() != nonce:
            raise HarnessFailure("Parent or child result did not match the exact nonce")
        self.passed(
            nonce_match=True,
            transport="cli-rpc-acp" if name == "herdr_to_kiro" else "acp-mcp-stdio-rpc-herdr",
        )

    async def collect_outstanding(self) -> None:
        # Request IDs recover receipts after an interrupted send; never send the request again.
        try:
            async with asyncio.timeout(3):
                found = await self.remote.call("task_list", {"query": self.run_id, "limit": 100})
            for task in found["tasks"]:
                self.record(task)
        except (BridgeError, OSError, ValueError, TimeoutError):
            self.report["limitations"].append(
                "Final receipt refresh was unavailable; request IDs identify uncertain sends."
            )
        self.report["outstanding_task_ids"] = [
            task_id for task_id, task in self.receipts.items() if task["status"] not in TERMINAL
        ]


def _events(root: Path) -> list[dict[str, Any]]:
    path = root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _counts(root: Path) -> dict[str, int]:
    events = _events(root)
    return {
        kind: sum(e["event"] == "prompt" and e["provider"] == kind for e in events)
        for kind in ("herdr", "acp")
    }


async def _stop_simulation(remote: RemoteBridge) -> bool:
    # This config and service belong exclusively to the temporary harness run.
    async with asyncio.timeout(15):
        status = await remote.status()
        if status["running"]:
            await remote.call("stop", {})
        for _ in range(150):
            if not (await remote.status())["running"]:
                return True
            await asyncio.sleep(0.1)
        return False


async def run_harness(
    config_path: Path,
    mode: str = "simulated",
    herdr_peer: str | None = None,
    kiro_peer: str | None = None,
    timeout: int = 120,  # noqa: ASYNC109 -- Public CLI duration; each await is independently bounded.
) -> dict[str, Any]:
    """Verify both directions; simulated success never claims live model compatibility.

    Live runs require an already running configured bridge and explicit target selectors.
    They submit only bounded reply-only probes and retain unresolved work for inspection.
    """
    resolved = await asyncio.to_thread(lambda: config_path.expanduser().resolve())
    check = Run(resolved, mode, timeout)
    check.begin("preflight")
    root: Path | None = None
    started = False
    try:
        if mode not in ("simulated", "live"):
            raise HarnessFailure("Choose simulated or live mode")
        if not 1 <= timeout <= 600:
            raise HarnessFailure("Harness timeout must be between 1 and 600 seconds")
        if mode == "simulated":
            check.report["limitations"].append(
                "Scripted local peers exercise real RPC, CLI, MCP STDIO, ACP and receipt storage; "
                "this is not proof of live Herdr, Kiro, model, authentication or approval behavior."
            )
            directory = await asyncio.to_thread(tempfile.mkdtemp, prefix="hb-harness-", dir="/tmp")
            # to_thread hides mkdtemp's overloads; str arguments always return a str path.
            root = await asyncio.to_thread(Path(cast(str, directory)).resolve)
            await asyncio.to_thread(root.chmod, 0o700)
            check.config_path = await asyncio.to_thread(_prepare_simulation, root)
            check.remote = RemoteBridge(check.config_path)
            started = True
            async with asyncio.timeout(min(28, check.remaining())):
                initial = await lifecycle.start(check.config_path, standalone=True)
            herdr_peer, kiro_peer = "herdr:harness-worker", "kiro:harness-kiro"
        else:
            check.report["limitations"].append(
                "Live probes use only the selected peers and leave the configured service and "
                "sessions intact. Pending approvals require human action; no approval is automatic."
            )
            if not herdr_peer or not herdr_peer.strip() or not kiro_peer or not kiro_peer.strip():
                raise HarnessFailure("Live mode requires explicit Herdr and Kiro peer selections")
            config = await asyncio.to_thread(Config.load, check.config_path)
            check.remote = RemoteBridge(check.config_path)
            async with asyncio.timeout(min(5, check.remaining())):
                initial = await check.remote.status()
            if not initial.get("accepting"):
                raise HarnessFailure("Start the configured bridge before a live harness run")
        herdr = await check.select(herdr_peer, "herdr")
        kiro = await check.select(kiro_peer, "kiro")
        await check.ready(herdr, kiro)
        if mode == "live":
            if kiro["target"] not in config.kiro_workspaces:
                raise HarnessFailure("Selected Kiro peer is not configured in this bridge")
            if not await asyncio.to_thread(Path(config.kiro_workspaces[kiro["target"]]).is_dir):
                raise HarnessFailure("Selected Kiro working directory is unavailable")
            if shutil.which(config.kiro_binary) is None:
                raise HarnessFailure("Configured Kiro executable is unavailable")
            output = await run(
                [config.kiro_binary, "acp", "--help"], max_seconds=min(10, check.remaining())
            )
            if "agent-engine" not in output.stdout:
                raise HarnessFailure("Configured Kiro lacks the required ACP interface")
        check.passed(herdr_peer=herdr["peer_id"], kiro_peer=kiro["peer_id"])
        await check.round_trip("herdr_to_kiro", herdr, kiro)
        check.begin("reverse_preflight")
        herdr = await check.settled_herdr(herdr)
        await check.ready(herdr, kiro)
        check.passed(herdr_state=herdr["state"])
        await check.round_trip("kiro_to_herdr", kiro, herdr)
        if root is not None:
            check.begin("idempotency")
            before = _counts(root)
            for params in check.submissions:
                old = next(
                    t for t in check.receipts.values() if t["request_key"] == params["request_id"]
                )
                duplicate = await check.call("peer_submit", params)
                if duplicate.get("id") != old["id"] or duplicate.get("result") != old["result"]:
                    raise HarnessFailure("Retry did not preserve the original receipt")
            if _counts(root) != before or before != {"herdr": 2, "acp": 2}:
                raise HarnessFailure("Duplicate request caused an extra provider prompt")
            events = _events(root)
            if not any(e["event"] == "child_submitted" and e["via"] == "mcp-stdio" for e in events):
                raise HarnessFailure("ACP callback did not cross the real STDIO MCP frontend")
            check.passed(prompt_counts=before, all_prompts_reserved=True)
            check.begin("restart")
            await _stop_simulation(check.remote)
            async with asyncio.timeout(min(28, check.remaining())):
                restarted = await lifecycle.start(check.config_path, standalone=True)
            if restarted["instance_id"] == initial["instance_id"]:
                raise HarnessFailure("Restart did not create a new service instance")
            receipts = list(check.receipts.values())
            for old in receipts:
                current = await check.call("task_get", {"task_id": old["id"]})
                if current.get("status") != "completed" or current.get("result") != old["result"]:
                    raise HarnessFailure("Completed receipt did not survive service restart")
            if _counts(root) != before:
                raise HarnessFailure("Restart replayed a provider prompt")
            check.passed(receipts_verified=len(receipts), instance_changed=True)
        check.report["success"] = True
    except HarnessFailure as error:
        check.phase.update(status="failed", detail=str(error))
    except Exception as error:
        # This diagnostic boundary turns SDK, protocol and setup failures into a report.
        check.phase.update(
            status="failed",
            detail=(
                f"Harness operation failed ({type(error).__name__}); inspect the named phase and "
                "task receipts. Peer output is omitted to avoid disclosing private content."
            ),
        )
    finally:
        if hasattr(check, "remote"):
            await check.collect_outstanding()
        if root is not None:
            check.begin("cleanup")
            stopped = False
            try:
                if started:
                    stopped = await _stop_simulation(check.remote)
                else:
                    stopped = True
            except (BridgeError, OSError, ValueError, TimeoutError):
                check.report["success"] = False
                check.phase.update(status="failed", detail="Temporary service shutdown unverified")
            if root is not None:
                check.report["fixture_pids"] = sorted({e["pid"] for e in _events(root)})
            if stopped:
                await asyncio.to_thread(shutil.rmtree, root)
                check.passed(service_stopped=True, temporary_directory_removed=not root.exists())
            else:
                # Keep its socket and receipts available for recovery instead of orphaning it.
                check.report["success"] = False
                check.phase.update(
                    status="failed",
                    detail="Temporary service shutdown unverified",
                    service_stopped=False,
                    temporary_directory_removed=False,
                    recovery_config=str(check.config_path),
                )
    return check.report
