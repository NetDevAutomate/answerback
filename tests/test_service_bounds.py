"""Bound service filtering before storage or provider work."""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from answerback_bridge.config import Config
from answerback_bridge.discovery import SelectionRequired
from answerback_bridge.service import Bridge


@pytest.fixture
async def bridge(tmp_path: Path) -> AsyncIterator[Bridge]:
    instance = Bridge(Config(provider_selection=[]), tmp_path / "bridge.json")
    try:
        yield instance
    finally:
        await instance.close()


class CountedQuery(str):
    calls = 0

    def casefold(self) -> str:
        self.calls += 1
        return super().casefold()


class CountedText:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def __str__(self) -> str:
        self.calls += 1
        return self.text

    # Receipt metadata is a mapping, and `str` of a mapping renders each value with `repr`.
    __repr__ = __str__


class SessionPage:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.list_sessions = AsyncMock(return_value={"sessions": records, "nextCursor": "next"})

    async def __aenter__(self) -> "SessionPage":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


@pytest.mark.parametrize("count", [33, 20_000])
def test_task_list_rejects_excess_terms_before_reading_receipts(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """red_at_base: even an empty store must refuse before any receipt read."""
    recent = Mock(return_value=[])
    monkeypatch.setattr(bridge.store, "recent", recent)
    with pytest.raises(ValueError, match="^query must contain at most 32 search terms$"):
        bridge.task_list("word " * count, limit=1)
    recent.assert_not_called()


def test_task_list_parses_query_once_and_renders_each_row_once(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: filter work must not multiply query or row preparation."""
    task, _ = bridge.store.reserve("herdr:one", "body", "request", {"kind": "herdr"})
    text = CountedText("Straße review")
    rows = [{**task, "metadata": {"note": text}}, {**task, "metadata": {"note": text}}]
    monkeypatch.setattr(bridge.store, "recent", Mock(return_value=rows))
    query = CountedQuery("STRASSE review request")
    assert len(bridge.task_list(query)["tasks"]) == 2
    assert (query.calls, text.calls) == (1, len(rows))


@pytest.mark.parametrize("query", ["", " \n\t ", " \t".join(["STRASSE"] * 32)])
def test_task_list_accepts_empty_and_maximum_unicode_queries(bridge: Bridge, query: str) -> None:
    """pins_existing: whitespace, case folding, summary shape and limits survive."""
    task, _ = bridge.store.reserve("herdr:Straße", "body", "request", {"kind": "herdr"})
    result = bridge.task_list(query, limit=1)
    assert result["searched_latest"] == 100
    assert [item["id"] for item in result["tasks"]] == [task["id"]]
    assert "message" not in result["tasks"][0] and "result" not in result["tasks"][0]


@pytest.mark.parametrize("limit", [0, 101])
def test_task_list_preserves_limit_validation(bridge: Bridge, limit: int) -> None:
    """pins_existing: the existing receipt-count limits remain enforced."""
    with pytest.raises(ValueError, match="^limit must be between 1 and 100$"):
        bridge.task_list(limit=limit)


@pytest.mark.parametrize("count", [33, 20_000])
async def test_kiro_sessions_rejects_excess_terms_before_provider_or_directory_work(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """red_at_base: refuse before runtime-dir mutation, ACP construction or listing."""
    page = SessionPage([])
    factory = Mock(return_value=page)
    directory = Mock()
    monkeypatch.setattr("answerback_bridge.providers.kiro.acp.KiroACP", factory)
    monkeypatch.setattr("answerback_bridge.providers.kiro.verbs.private_directory", directory)
    with pytest.raises(ValueError, match="^query must contain at most 32 search terms$"):
        await bridge.kiro_sessions("word " * count)
    factory.assert_not_called()
    directory.assert_not_called()
    page.list_sessions.assert_not_awaited()


async def test_desktop_sessions_rejects_excess_terms_before_index_work(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_76db6d7a: Desktop paging ran before its query was bounded."""
    page = AsyncMock(return_value={"peers": [], "next_cursor": None})
    monkeypatch.setattr(bridge, "desktop_index", SimpleNamespace(page=page))
    bridge.config.codex_enabled = True
    bridge.config.provider_selection = ["codex"]

    with pytest.raises(ValueError, match="^query must contain at most 32 search terms$"):
        await bridge.desktop_sessions("word " * 20_000)

    page.assert_not_awaited()


async def test_kiro_sessions_parses_query_once_and_renders_each_row_once(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: query and session preparation are linear in their own inputs."""
    title, cwd = CountedText("Straße review"), CountedText("/project")
    records = [{"sessionId": str(index), "title": title, "cwd": cwd} for index in range(3)]
    page = SessionPage(records)
    monkeypatch.setattr("answerback_bridge.providers.kiro.acp.KiroACP", Mock(return_value=page))
    query = CountedQuery("STRASSE review project")
    result = await bridge.kiro_sessions(query, cwd="/filter", cursor="previous")
    assert result["sessions"] == records
    assert result["next_cursor"] == "next"
    page.list_sessions.assert_awaited_once_with(cwd="/filter", cursor="previous")
    assert (query.calls, title.calls, cwd.calls) == (1, len(records), len(records))


@pytest.mark.parametrize("operation", ["peer_submit", "task_for_peer"])
async def test_resolve_selection_is_returned_to_every_consumer(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """red_at_76db6d7a: task_for_peer leaked SelectionRequired as an RPC failure."""
    match = {"peer_id": "desktop:22222222-2222-2222-2222-222222222222"}
    directory = SimpleNamespace(
        resolve=AsyncMock(side_effect=SelectionRequired("select by peer_id", [match], {}))
    )
    monkeypatch.setattr(bridge, "directory", lambda: directory)

    if operation == "peer_submit":
        result = await bridge.peer_submit("quarterly audit", "Review", "request")
    else:
        result = await bridge.task_for_peer("quarterly audit")

    assert result == {"status": "needs_selection", "matches": [match], "errors": {}}


@pytest.mark.parametrize("provider", ["hc", "herdr", "crew"])
@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("canonical", [False, True])
async def test_peer_submit_requires_the_discovered_binding(
    tmp_path: Path, provider: str, replace: bool, canonical: bool
) -> None:
    """red_at_base: an omitted binding never admits work to an addressable occupant."""
    from test_core_routes import FakeCore, core_peer
    from test_crew_identity_before_reservation import _Crew, _row
    from test_integration_regressions import _ReplaceableHerdr

    instance = Bridge(Config("test", hc_enabled=True, crew_enabled=True), tmp_path / "bridge.json")
    core, herdr, crew = FakeCore(), _ReplaceableHerdr(), _Crew()
    core.release.set()
    instance.core, instance.herdr, instance._crew_client = core, herdr, crew
    try:
        peer = (await instance.peer_search(provider=provider))["peers"][0]
        assert isinstance(peer["binding"], str)
        if replace:
            core.current = core_peer(binding="replacement")
            herdr.terminal_id, herdr.agent_session = "term-b", "session-b"
            crew.row = _row(created=99.0)
        selector = peer["peer_id"] if canonical else peer["name"]
        with pytest.raises(
            ValueError, match="^Selected peer changed since discovery; search again$"
        ):
            await instance.peer_submit(selector, "Review", "missing-binding", provider=provider)
        assert instance.store.counts() == {}
        assert core.sent == [] and herdr.sends == crew.sends == 0
    finally:
        await instance.close()


@pytest.mark.parametrize("binding", ["observed", ""])
async def test_peer_submit_requires_string_bindings_for_extension_providers(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    """red_at_base: discovery-token enforcement must not depend on provider names."""
    peer = {"peer_id": "extension:one", "provider": "extension", "binding": binding}
    directory = SimpleNamespace(resolve=AsyncMock(return_value=peer))
    submit = AsyncMock(return_value={"status": "submitted"})
    monkeypatch.setattr(bridge, "directory", lambda: directory)
    monkeypatch.setattr(bridge, "submit_resolved", submit)
    with pytest.raises(ValueError, match="search again"):
        await bridge.peer_submit("extension:one", "Review", "request")
    submit.assert_not_awaited()


@pytest.mark.parametrize(
    ("observed", "supplied", "refused"),
    [
        (None, None, False),
        (None, "unexpected", True),
        ("same", "same", False),
        ("same", "old", True),
    ],
)
async def test_peer_submit_preserves_explicit_binding_comparisons(
    bridge: Bridge,
    monkeypatch: pytest.MonkeyPatch,
    observed: str | None,
    supplied: str | None,
    refused: bool,
) -> None:
    """pins_existing: matching and unbound calls work; explicit mismatches refuse."""
    peer = {"peer_id": "extension:one", "provider": "extension", "binding": observed}
    directory = SimpleNamespace(resolve=AsyncMock(return_value=peer))
    submit = AsyncMock(return_value={"status": "submitted"})
    monkeypatch.setattr(bridge, "directory", lambda: directory)
    monkeypatch.setattr(bridge, "submit_resolved", submit)
    if refused:
        with pytest.raises(ValueError, match="search again"):
            await bridge.peer_submit("extension:one", "Review", "request", binding=supplied)
        submit.assert_not_awaited()
    else:
        assert await bridge.peer_submit("extension:one", "Review", "request", binding=supplied) == {
            "status": "submitted"
        }
        submit.assert_awaited_once()


@pytest.mark.parametrize("reuse", [False, True])
async def test_routing_passes_the_selected_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    """red_at_base: routed work must carry the token from the selected inventory."""
    from routing_fakes import World
    from test_routing_route import go

    world = World()
    if reuse:
        world.owner_tab("claude")
    peers = world.peers

    def bound_peers() -> list[dict[str, Any]]:
        return [{**peer, "binding": "seen-" + peer["peer_id"]} for peer in peers()]

    monkeypatch.setattr(world, "peers", bound_peers)
    await go(world, tmp_path)
    assert len(world.sent) == 1
    assert world.sent[0].get("binding") == "seen-" + world.sent[0]["selector"]


async def test_legacy_send_herdr_passes_the_discovery_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: the legacy CLI route must accept and forward an observed binding."""
    from answerback_bridge.cli import dispatch, parser

    call = AsyncMock(return_value={})
    monkeypatch.setattr("answerback_bridge.cli.RemoteBridge.call", call)
    args = parser().parse_args(
        [
            "--config",
            str(tmp_path / "bridge.json"),
            "send-herdr",
            "worker",
            "--message",
            "Review",
            "--request-id",
            "legacy",
            "--binding",
            "observed",
        ]
    )
    await dispatch(args)
    assert call.call_args.args == (
        "peer_submit",
        {
            "selector": "worker",
            "message": "Review",
            "request_id": "legacy",
            "provider": "herdr",
            "binding": "observed",
        },
    )


@pytest.mark.parametrize("mode", ["simulated", "live"])
def test_harness_child_instructions_carry_the_discovery_binding(tmp_path: Path, mode: str) -> None:
    """red_at_base: both scripted and attended messages preserve the selected child."""
    from answerback_bridge.harness import Run

    run = Run(tmp_path / "bridge.json", mode, 60)
    message = run.message(
        "delegate",
        "nonce",
        "parent",
        {"peer_id": "herdr:child", "provider": "herdr", "binding": "child-observed"},
        "child",
    )
    assert '"child-observed"' in message


async def test_harness_parent_submission_carries_the_discovery_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: the parent assignment is bound before the harness waits for it."""
    from answerback_bridge.harness import HarnessFailure, Run

    run = Run(tmp_path / "bridge.json", "simulated", 60)
    call = AsyncMock(return_value={})
    monkeypatch.setattr(run, "call", call)
    with pytest.raises(HarnessFailure, match="no task ID"):
        await run.round_trip(
            "test",
            {"peer_id": "herdr:parent", "binding": "parent-observed"},
            {"peer_id": "kiro:child"},
        )
    assert call.call_args.args[1].get("binding") == "parent-observed"


@pytest.mark.parametrize("query", ["", " \n\t ", " \t".join(["STRASSE"] * 32)])
async def test_kiro_sessions_accepts_empty_and_maximum_unicode_queries(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """pins_existing: preserve page order, optional metadata and cursor semantics."""
    records = [
        {"sessionId": "one", "title": "Straße"},
        {"sessionId": "two", "cwd": "/Straße"},
        {"sessionId": "other", "title": 12, "cwd": None},
    ]
    page = SessionPage(records)
    monkeypatch.setattr("answerback_bridge.providers.kiro.acp.KiroACP", Mock(return_value=page))
    result = await bridge.kiro_sessions(query)
    assert result["sessions"] == (records if not query.strip() else records[:2])
    assert result["next_cursor"] == "next"
