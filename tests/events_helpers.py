"""Scripted receipt store and peer scanner for the event-stream tests."""

from typing import Any

from answerback_bridge.events.collector import Collector, Settings, Sources, scripted
from answerback_bridge.events.hub import EventHub

RUN = "3f0c7a52-9d1e-4b6a-8c2f-5e7d1a9b0c44"
PEER = "hc:t61"


def receipt(status: str, updated: float, *, run: str = RUN, **fields: Any) -> dict[str, Any]:
    return {
        "id": run,
        "request_key": "ab-20261117-T16-design-t61",
        "target": "hc:default:t61",
        "status": status,
        "created": 100.0,
        "updated": updated,
        "head": "Bridge task: x\nAssignment from the orchestrator. Task T-16. Routed: design-impl",
        "kind": "hc",
        "peer_id": PEER,
        "detail": None,
        "result_chars": 42 if status == "completed" else None,
        **fields,
    }


def peer(state: str = "working", *, ident: str = PEER, **fields: Any) -> dict[str, Any]:
    return {
        "peer_id": ident,
        "provider": ident.split(":")[0],
        "target": ident.split(":", 1)[1],
        "name": "design-impl-claude-261117-1",
        "project": "/work/answerback",
        "agent_kind": "claude",
        "state": state,
        "send_ready": state == "idle",
        "send_readiness_reason": "ready" if state == "idle" else state,
        **fields,
    }


class Script:
    """What the bridge would observe, set by the test between collector passes."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.scan: dict[str, Any] = {"peers": [], "complete": True, "errors": {}}
        self.tails: dict[str, str] = {}
        self.board: dict[str, Any] = {"decisions": {}, "applyQueue": {}, "heads": {}}
        self.bus_rows: list[dict[str, Any]] = []

    async def receipts(self, cursor: tuple[float, str] | None) -> list[dict[str, Any]]:
        return list(self.rows)

    async def peers(self) -> dict[str, Any]:
        return self.scan

    async def tail(self, peer_id: str, record: dict[str, Any]) -> str | None:
        return self.tails.get(peer_id)

    async def read_board(self, paths: list[str]) -> dict[str, Any]:
        return self.board

    async def bus(self, cursor: int | None) -> tuple[list[dict[str, Any]], int | None]:
        rows, self.bus_rows = self.bus_rows, []
        return rows, (cursor or 0) + len(rows)

    def sources(self, *, full: bool = False) -> Sources:
        if not full:
            return scripted(self.receipts, self.peers)
        return Sources(self.receipts, self.peers, self.tail, self.read_board, self.bus)


def collector(script: Script, *, full: bool = False, history: int = 2000) -> Collector:
    return Collector(EventHub(history=history), script.sources(full=full), Settings())


async def acceptance_run(script: Script, observer: Collector) -> None:
    """The T-16 acceptance script: one receipt on one worker peer, start to finish."""
    script.rows = [receipt("pending", 101.0)]
    script.scan = {"peers": [peer("working")], "complete": True, "errors": {}}
    await observer.tick()
    script.rows = [receipt("submitted", 102.0)]
    await observer.tick()
    script.rows = [receipt("completed", 103.0)]
    script.scan = {"peers": [peer("idle")], "complete": True, "errors": {}}
    await observer.tick()


def types(page: dict[str, Any]) -> list[str]:
    return [item["event"]["type"] for item in page["events"]]


def in_order(sequence: list[str], wanted: list[str]) -> bool:
    remaining = iter(sequence)
    return all(kind in remaining for kind in wanted)
