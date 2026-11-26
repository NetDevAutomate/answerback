import asyncio
from typing import Any

import pytest
from mcp import Client

from codex_herdr_bridge import discovery
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.core import Core
from codex_herdr_bridge.discovery import Directory, SelectionRequired
from codex_herdr_bridge.mcp_server import create_server
from codex_herdr_bridge.process import BridgeError, CommandTimeout
from codex_herdr_bridge.transport import timeout_message


class CountedQuery(str):
    def __init__(self, value: str) -> None:
        self.calls = 0

    def casefold(self) -> str:
        self.calls += 1
        return super().casefold()


async def test_discovery_rejects_excess_terms_before_provider_work() -> None:
    """red_at_76db6d7a: search and resolve reached providers with 20,000 terms."""
    calls = 0

    class Herdr:
        async def call(self, *args: str) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"agents": []}

    directory = Directory(Config("test", provider_selection=["herdr"]), Herdr(), None)
    query = "word " * 20_000
    with pytest.raises(ValueError, match="^query must contain at most 32 search terms$"):
        await directory.search(query)
    with pytest.raises(ValueError, match="^query must contain at most 32 search terms$"):
        await directory.resolve(query)
    assert calls == 0


async def test_discovery_caps_union_and_marks_results_incomplete() -> None:
    """red_at_76db6d7a: the complete peer union had no size bound."""
    peers = [
        {
            "peer_id": f"desktop:thread-{index}",
            "provider": "desktop",
            "target": f"thread-{index}",
            "thread_id": f"thread-{index}",
            "title": "bounded inventory",
        }
        for index in range(1001)
    ]

    class Desktop:
        async def inventory(self) -> dict[str, Any]:
            return {"peers": peers, "complete": True}

    directory = Directory(
        Config("test", codex_enabled=True, provider_selection=["codex"]),
        None,
        None,
        Desktop(),
    )
    result = await directory.search()
    assert len(result["peers"]) == 1000
    assert result["complete"] is False
    assert "directory" in result["errors"]


async def test_discovery_parses_once_and_offloads_bounded_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """red_at_76db6d7a: matching parsed per peer and blocked the event loop."""
    peers = [
        {
            "peer_id": f"desktop:thread-{index}",
            "provider": "desktop",
            "target": f"thread-{index}",
            "thread_id": f"thread-{index}",
            "title": "needle",
        }
        for index in range(3)
    ]

    class Desktop:
        async def inventory(self) -> dict[str, Any]:
            return {"peers": peers, "complete": True}

    offloads = 0
    original = asyncio.to_thread

    async def counted_offload(function: Any, *args: Any) -> Any:
        nonlocal offloads
        offloads += 1
        return await original(function, *args)

    monkeypatch.setattr(discovery.asyncio, "to_thread", counted_offload)
    directory = Directory(
        Config("test", codex_enabled=True, provider_selection=["codex"]),
        None,
        None,
        Desktop(),
    )
    query = CountedQuery("needle")
    result = await directory.search(query)
    assert len(result["peers"]) == len(peers)
    assert query.calls == 1
    assert offloads == 1


async def test_discovery_caps_each_search_haystack() -> None:
    """red_at_76db6d7a: an arbitrarily long peer field remained searchable whole."""
    peer = {
        "peer_id": "desktop:thread-long",
        "provider": "desktop",
        "target": "thread-long",
        "thread_id": "thread-long",
        "title": "x" * 100_000 + " unique-tail",
    }

    class Desktop:
        async def inventory(self) -> dict[str, Any]:
            return {"peers": [peer], "complete": True}

    directory = Directory(
        Config("test", codex_enabled=True, provider_selection=["codex"]),
        None,
        None,
        Desktop(),
    )
    assert (await directory.search("unique-tail"))["peers"] == []


async def test_offline_crew_is_excluded_without_losing_healthy_results() -> None:
    from codex_herdr_bridge.process import ProviderOffline

    class OfflineCrew:
        async def peers(self) -> list[dict[str, Any]]:
            raise ProviderOffline("Crew gateway is offline")

    directory = Directory(
        Config(codex_enabled=False, crew_enabled=True, kiro_workspaces={"demo": "/work"}),
        None,
        OfflineCrew(),
    )
    result = await directory.search()
    assert result["peers"][0]["peer_id"] == "kiro:demo"
    assert result["errors"] == {}
    assert result["excluded"] == {"crew": "Crew gateway is offline"}
    assert result["complete"] is False


async def test_slow_provider_retains_healthy_peers_before_rpc_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Small provider budget, generous outer guard: assert the missing result,
    # rather than timing a busy machine to millisecond precision.
    monkeypatch.setattr(discovery, "PROVIDER_SECONDS", 0.05, raising=False)
    stopped = asyncio.Event()

    class SlowCrew:
        async def peers(self) -> list[dict[str, Any]]:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    class HealthyCore:
        async def inventory(self) -> dict[str, Any]:
            return {
                "peers": [{"peer_id": "hc:healthy", "provider": "hc", "target": "healthy"}],
                "complete": True,
            }

    directory = Directory(
        Config(codex_enabled=False, crew_enabled=True, hc_enabled=True),
        None,
        SlowCrew(),
        core=HealthyCore(),
    )
    async with asyncio.timeout(1):
        result = await directory.search()
    assert [peer["peer_id"] for peer in result["peers"]] == ["hc:healthy"]
    assert result["complete"] is False
    assert "timed out" in result["errors"]["crew"].lower()
    assert stopped.is_set()
    with pytest.raises(SelectionRequired):
        async with asyncio.timeout(1):
            await directory.resolve("healthy")


async def test_provider_calls_start_independently_and_preserve_inventory_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery, "PROVIDER_SECONDS", 0.2, raising=False)
    crew_started = asyncio.Event()

    class WaitingDesktop:
        async def inventory(self) -> dict[str, Any]:
            await crew_started.wait()
            return {
                "peers": [
                    {
                        "peer_id": "desktop:one",
                        "thread_id": "one",
                        "provider": "desktop",
                        "target": "one",
                    }
                ],
                "complete": False,
                "next_cursor": "next-page",
            }

    class Crew:
        async def peers(self) -> list[dict[str, Any]]:
            crew_started.set()
            return [{"peer_id": "crew:two", "provider": "crew", "target": "two"}]

    directory = Directory(
        Config(crew_enabled=True, codex_enabled=True), None, Crew(), WaitingDesktop()
    )
    async with asyncio.timeout(1):
        result = await directory.search()
    assert [peer["peer_id"] for peer in result["peers"]] == ["desktop:one", "crew:two"]
    assert result["cursors"] == {"desktop": "next-page"}
    assert result["complete"] is False


async def test_discovery_cancellation_stops_all_started_providers() -> None:
    starts = [asyncio.Event(), asyncio.Event()]
    stops = [asyncio.Event(), asyncio.Event()]

    class Provider:
        def __init__(self, index: int) -> None:
            self.index = index

        async def inventory(self) -> dict[str, Any]:
            starts[self.index].set()
            try:
                await asyncio.Event().wait()
            finally:
                stops[self.index].set()

        async def peers(self) -> list[dict[str, Any]]:
            await self.inventory()
            return []

    directory = Directory(
        Config(crew_enabled=True, codex_enabled=True), None, Provider(1), Provider(0)
    )
    task = asyncio.create_task(directory.search())
    try:
        async with asyncio.timeout(1):
            await asyncio.gather(*(event.wait() for event in starts))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert all(event.is_set() for event in stops)


@pytest.mark.parametrize("method", ["peer_search", "crew_read", "hc_read", "desktop_sessions"])
def test_read_timeout_does_not_claim_delivery_uncertainty(method: str) -> None:
    message = timeout_message(method)
    assert method in message
    assert "delivery may be uncertain" not in message
    assert "receipt" not in message


def test_write_timeout_preserves_delivery_uncertainty() -> None:
    assert "delivery may be uncertain" in timeout_message("peer_submit")


@pytest.mark.parametrize("method,params", [("peer_search", {}), ("crew_read", {"target": "chat"})])
async def test_expected_mcp_read_errors_remain_errors_with_useful_details(
    method: str, params: dict[str, Any]
) -> None:
    class UnavailableBridge:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise BridgeError("Crew request timed out; check its gateway")

    async with Client(create_server(UnavailableBridge())) as client:
        result = await client.call_tool(method, params)
    assert result.is_error
    assert "Crew request timed out" in str(result.content)


async def test_unexpected_mcp_read_error_stays_masked() -> None:
    class BrokenBridge:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("PRIVATE_TOKEN_SHOULD_NOT_LEAK")

    async with Client(create_server(BrokenBridge())) as client:
        result = await client.call_tool("crew_read", {"target": "chat"})
    assert result.is_error
    assert "PRIVATE_TOKEN_SHOULD_NOT_LEAK" not in str(result.content)


async def test_raw_mcp_timeout_has_operation_context_without_exception_text() -> None:
    class TimedOutBridge:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise TimeoutError("PRIVATE_TIMEOUT_DETAIL")

    async with Client(create_server(TimedOutBridge())) as client:
        result = await client.call_tool("crew_read", {"target": "chat"})
    assert result.is_error
    assert "crew_read timed out" in str(result.content)
    assert "PRIVATE_TIMEOUT_DETAIL" not in str(result.content)


async def test_core_send_timeout_keeps_delivery_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timeout(*args: Any, **kwargs: Any) -> None:
        raise CommandTimeout("timeout")

    monkeypatch.setattr("codex_herdr_bridge.core.run", timeout)
    with pytest.raises(BridgeError, match="delivery may be uncertain"):
        await Core()._call("send", "t1", "--text", "message", allow_error=True)


async def test_core_read_timeout_does_not_claim_delivery_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timeout(*args: Any, **kwargs: Any) -> None:
        raise CommandTimeout("timeout")

    monkeypatch.setattr("codex_herdr_bridge.core.run", timeout)
    with pytest.raises(BridgeError) as error:
        await Core()._call("list", "--json")
    assert "read" in str(error.value).lower()
    assert "uncertain" not in str(error.value)
