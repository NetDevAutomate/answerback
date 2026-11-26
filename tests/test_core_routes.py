import asyncio
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.cli import parser
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.service import Bridge


def core_peer(**changes: Any) -> dict[str, Any]:
    return {
        "peer_id": "hc:t55",
        "provider": "hc",
        "target": "t55",
        "name": "Analytics Claude",
        "project": "/work/analytics",
        "project_known": True,
        "agent_kind": "claude",
        "kind": "core_agent",
        "state": "idle",
        "send_ready": True,
        "binding": "first",
        "session": None,
        **changes,
    }


class FakeCore:
    def __init__(self) -> None:
        self.current = core_peer()
        self.sent: list[str] = []
        self.release = asyncio.Event()

    async def inventory(self) -> dict[str, Any]:
        return {"peers": [self.current], "complete": True, "errors": {}}

    async def peer(self, target: str) -> dict[str, Any]:
        assert target == "t55"
        return self.current

    async def send(self, target: str, message: str, expected: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(message)
        await self.release.wait()
        return {"turn": 10, "started": True, "status": "idle"}

    async def read(self, target: str, lines: int = 100) -> dict[str, Any]:
        return {"text": "Agent screen", "complete": True, "how": "screen"}


def test_core_configuration_does_not_require_codex_or_tui(tmp_path: Path) -> None:
    config = Config(hc_enabled=True, codex_enabled=False)
    config.save(tmp_path / "config.json")
    restored = Config.load(tmp_path / "config.json")
    assert restored.herdr_session is None
    assert restored.hc_enabled and not restored.codex_enabled


def test_cli_names_core_and_codex_explicitly() -> None:
    assert parser().parse_args(["find", "Analytics", "--provider", "hc"]).provider == "hc"
    assert parser().parse_args(["find", "Analytics", "--provider", "codex"]).provider == "codex"
    assert parser().parse_args(["configure", "--runtime", "hc"]).runtime == "hc"


@pytest.fixture
async def bridge(tmp_path: Path) -> Any:
    bridge = Bridge(Config(hc_enabled=True, codex_enabled=False), tmp_path / "config.json")
    bridge.core = FakeCore()
    yield bridge
    await bridge.close()


def test_core_completion_command_is_immune_to_the_workers_working_directory(
    bridge: Bridge,
) -> None:
    import shlex

    from codex_herdr_bridge.process import module_launch_argv

    envelope = bridge.core_tasks.envelope("task-1", "Review this")
    command_line = next(
        line for line in envelope.splitlines() if line.endswith("/absolute/path/to/result.txt")
    )
    command = shlex.split(command_line)[:-1]
    assert command == module_launch_argv(bridge.config_path, "complete", "task-1", "--result-file")


async def test_default_discovery_includes_core_without_codex_or_tui(bridge: Bridge) -> None:
    result = await bridge.peer_search("Analytics")
    assert result["complete"]
    assert result["peers"][0]["agent_kind"] == "claude"
    assert result["peers"][0]["provider"] == "hc"


async def test_core_submission_returns_before_delivery_and_deduplicates(bridge: Bridge) -> None:
    async with asyncio.timeout(1):
        task = await bridge.peer_submit("hc:t55", "Review", "unique", binding="first")
        same = await bridge.peer_submit("hc:t55", "Review", "unique")
    assert task["id"] == same["id"]
    assert task["status"] == "pending"
    await asyncio.sleep(0)
    assert len(bridge.core.sent) == 1
    assert task["id"] in bridge.core.sent[0]
    assert str(bridge.config_path) in bridge.core.sent[0]
    jobs = list(bridge.core_tasks.jobs.values())
    bridge.core.release.set()
    async with asyncio.timeout(1):
        await asyncio.gather(*jobs)
    assert bridge.store.get(task["id"])["status"] == "submitted"
    result = bridge.complete(task["id"], "Verified result")
    assert result["status"] == "completed"
    assert result["result"] == "Verified result"


async def test_submit_refuses_a_terminal_replaced_since_discovery(bridge: Bridge) -> None:
    # The binding peer_search returned is compared with the RE-RESOLVED peer before
    # anything is reserved, so a terminal re-occupied between search and submit is
    # refused and nothing is sent to the replacement.
    discovered = await bridge.peer_search(provider="hc")
    binding = discovered["peers"][0]["binding"]
    bridge.core.current = core_peer(binding="second")
    with pytest.raises(ValueError, match="search again"):
        await bridge.peer_submit("hc:t55", "Review", "race", binding=binding)
    assert bridge.store.counts() == {}
    assert bridge.core.sent == []


async def test_submit_delivers_when_the_terminal_is_unchanged_since_discovery(
    bridge: Bridge,
) -> None:
    discovered = await bridge.peer_search(provider="hc")
    binding = discovered["peers"][0]["binding"]
    task = await bridge.peer_submit("hc:t55", "Review", "race-ok", binding=binding)
    assert task["status"] == "pending"
    await asyncio.sleep(0)
    assert len(bridge.core.sent) == 1
    jobs = list(bridge.core_tasks.jobs.values())
    bridge.core.release.set()
    async with asyncio.timeout(1):
        await asyncio.gather(*jobs)


async def test_cli_send_passes_the_discovery_binding_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_herdr_bridge.cli import dispatch

    captured: dict[str, Any] = {}

    async def call(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        captured["method"] = method
        captured.update(params)
        return {}

    monkeypatch.setattr("codex_herdr_bridge.cli.RemoteBridge.call", call)
    args = parser().parse_args(
        [
            "--config",
            str(tmp_path / "config.json"),
            "send",
            "hc:t55",
            "--message",
            "Review",
            "--request-id",
            "cli-binding",
            "--binding",
            "first",
        ]
    )
    await dispatch(args)
    assert captured["method"] == "peer_submit"
    assert captured["binding"] == "first"
    assert captured["selector"] == "hc:t55"


async def test_unknown_readiness_is_discoverable_but_not_sent(bridge: Bridge) -> None:
    bridge.core.current = core_peer(
        state="unknown", send_ready=False, send_readiness_reason="status not reported"
    )
    assert len((await bridge.peer_search(provider="hc"))["peers"]) == 1
    with pytest.raises(ValueError, match="ready|reported"):
        await bridge.peer_submit("hc:t55", "Review", "not-ready", binding="first")
    assert bridge.store.counts() == {}


async def test_core_shutdown_preserves_uncertain_receipt(bridge: Bridge) -> None:
    task = await bridge.peer_submit("hc:t55", "Review", "shutdown", binding="first")
    await asyncio.sleep(0)
    await bridge.close()
    assert bridge.store.get(task["id"])["status"] == "uncertain"
    assert len(bridge.core.sent) == 1


async def test_exact_retry_survives_disappeared_terminal(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from codex_herdr_bridge.process import BridgeError

    task = await bridge.peer_submit("hc:t55", "Review", "retry-after-close", binding="first")
    monkeypatch.setattr(bridge.core, "peer", AsyncMock(side_effect=BridgeError("Terminal closed")))
    same = await bridge.peer_submit("hc:t55", "Review", "retry-after-close")
    assert same["id"] == task["id"]


async def test_core_config_preserves_changes_while_inventory_runs(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    replacement = FakeCore()

    async def inventory() -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"peers": [], "complete": True, "errors": {}}

    monkeypatch.setattr(replacement, "inventory", inventory)
    monkeypatch.setattr(
        "answerback_bridge.providers.herdr.core.Core", lambda *args, **kwargs: replacement
    )
    monkeypatch.setattr(
        "answerback_bridge.providers.herdr.verbs.shutil.which", lambda *args: "/bin/hc"
    )
    changing = asyncio.create_task(bridge.hc_configure(session="second"))
    await entered.wait()
    await bridge.pair("reviewer", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    release.set()
    await changing
    assert Config.load(bridge.config_path).desktop_threads == bridge.config.desktop_threads


async def test_core_config_rechecks_active_assignments_before_saving(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    replacement = FakeCore()

    async def inventory() -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"peers": [], "complete": True, "errors": {}}

    monkeypatch.setattr(replacement, "inventory", inventory)
    monkeypatch.setattr(
        "answerback_bridge.providers.herdr.core.Core", lambda *args, **kwargs: replacement
    )
    monkeypatch.setattr(
        "answerback_bridge.providers.herdr.verbs.shutil.which", lambda *args: "/bin/hc"
    )
    changing = asyncio.create_task(bridge.hc_configure(session="second"))
    await entered.wait()
    await bridge.peer_submit("hc:t55", "Review", "race", binding="first")
    release.set()
    with pytest.raises(ValueError, match="unfinished"):
        await changing
    assert bridge.config.hc_session is None


async def test_disabled_codex_never_reads_provider_and_retains_receipts(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, Mock

    page = AsyncMock()
    history = Mock(side_effect=AssertionError("Disabled Codex must not run"))
    monkeypatch.setattr(bridge.desktop_index, "page", page)
    monkeypatch.setattr("codex_herdr_bridge.codex.HistoryReader", history)
    with pytest.raises(ValueError, match="disabled"):
        await bridge.desktop_sessions()
    page.assert_not_called()
    task, _ = bridge.store.reserve(
        "desktop:old",
        "old",
        "old-codex",
        {"kind": "desktop", "thread_id": "old", "binding": "discovered"},
    )
    result = await bridge.get_task(task["id"])
    assert result["id"] == task["id"] and result["status"] == "pending"
    assert result["provider_disabled"] is True
    history.assert_not_called()


async def test_oversized_core_envelope_never_reserves_or_sends(bridge: Bridge) -> None:
    with pytest.raises(ValueError, match="limit|bound|KiB|large|bytes"):
        await bridge.peer_submit("hc:t55", "😀" * 40000, "oversized", binding="first")
    assert bridge.store.counts() == {}
    assert bridge.core.sent == []


async def test_failed_core_inventory_is_not_configured_as_connected(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from codex_herdr_bridge.process import BridgeError

    replacement = FakeCore()
    replacement.inventory = AsyncMock(
        return_value={"peers": [], "complete": False, "errors": {"inventory": "Core unavailable"}}
    )
    monkeypatch.setattr(
        "answerback_bridge.providers.herdr.core.Core", lambda *args, **kwargs: replacement
    )
    monkeypatch.setattr(
        "answerback_bridge.providers.herdr.verbs.shutil.which", lambda *args: "/bin/hc"
    )
    with pytest.raises(BridgeError, match="unavailable|unverified"):
        await bridge.hc_configure(session="new")
    assert bridge.config.hc_session is None


@pytest.mark.parametrize("valid_delivery", [True, False])
async def test_early_core_result_is_provisional_until_delivery_verified(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch, valid_delivery: bool
) -> None:
    import re

    from codex_herdr_bridge.core import CoreIdentityChanged

    reported, release = asyncio.Event(), asyncio.Event()

    async def fast_report(target: str, message: str, expected: dict[str, Any]) -> dict[str, Any]:
        task_id = re.search(r"Bridge task: ([0-9a-f-]{36})", message).group(1)
        bridge.complete(task_id, "Early result")
        reported.set()
        await release.wait()
        if not valid_delivery:
            raise CoreIdentityChanged("Wrong address")
        return {"turn": 10, "started": True, "status": "idle"}

    monkeypatch.setattr(bridge.core, "send", fast_report)
    task = await bridge.peer_submit("hc:t55", "Review", "early-report", binding="first")
    await reported.wait()
    before = await bridge.get_task(task["id"])
    assert before["status"] == "pending"
    assert before["result"] is None
    assert "Early result" not in str(before)
    jobs = list(bridge.core_tasks.jobs.values())
    release.set()
    await asyncio.gather(*jobs)
    after = await bridge.get_task(task["id"])
    if valid_delivery:
        assert after["status"] == "completed" and after["result"] == "Early result"
    else:
        assert after["status"] == "uncertain" and after["result"] is None
        with pytest.raises(ValueError, match="invalidated"):
            bridge.complete(task["id"], "Early result")


# T-187: a send that landed in the agent's own turn is settled by the worker's report.


def typed_while_working(receipt: dict[str, Any]) -> Any:
    from codex_herdr_bridge.core import CoreTypedWhileWorking

    return CoreTypedWhileWorking(
        "Core typed into a terminal that was not done or idle",
        receipt={
            "target": "t55",
            **receipt,
            "identity_guard": "observed_before_and_after",
            "identity_observer": "core:local",
        },
        turn_before=3,
        turn_after=4,
    )


async def test_typed_while_working_is_uncertain_until_the_worker_reports(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def racing_send(target: str, message: str, expected: dict[str, Any]) -> dict[str, Any]:
        raise typed_while_working({"turn": 4, "started": False, "status": "working"})

    monkeypatch.setattr(bridge.core, "send", racing_send)
    task = await bridge.peer_submit("hc:t55", "Review", "race", binding="first")
    await asyncio.gather(*bridge.core_tasks.jobs.values())
    stored = bridge.store.get(task["id"])
    assert stored["status"] == "uncertain"
    assert stored["result"] is None
    assert stored["metadata"]["typed_while_working"] is True
    assert stored["metadata"]["delivery_verified"] is True
    assert stored["metadata"]["delivery"]["status"] == "working"
    assert (stored["metadata"]["turn_before"], stored["metadata"]["turn_after"]) == (3, 4)
    public = await bridge.get_task(task["id"])
    assert public["status"] == "uncertain"
    with pytest.raises(ValueError, match="already has task"):
        await bridge.peer_submit("hc:t55", "Next", "while-uncertain", binding="first")
    done = bridge.complete(task["id"], "Reviewed")
    assert done["status"] == "completed" and done["result"] == "Reviewed"
    # The terminal is free: the next assignment is admitted and delivered.
    again = await bridge.peer_submit("hc:t55", "Next", "after-race", binding="first")
    assert again["id"] != task["id"] and again["status"] == "pending"


async def test_early_report_before_a_typed_while_working_receipt_completes(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re

    async def fast_report(target: str, message: str, expected: dict[str, Any]) -> dict[str, Any]:
        task_id = re.search(r"Bridge task: ([0-9a-f-]{36})", message).group(1)
        bridge.complete(task_id, "Early result")
        raise typed_while_working({"turn": 4, "started": True, "status": "working"})

    monkeypatch.setattr(bridge.core, "send", fast_report)
    task = await bridge.peer_submit("hc:t55", "Review", "early-race", binding="first")
    await asyncio.gather(*bridge.core_tasks.jobs.values())
    after = await bridge.get_task(task["id"])
    assert after["status"] == "completed" and after["result"] == "Early result"
    metadata = bridge.store.get(task["id"])["metadata"]
    assert metadata["typed_while_working"] is True
    assert "provisional_result" not in metadata
