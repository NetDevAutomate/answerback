"""T-227 `answerback pressure`: read and clear through the bridge socket."""

import asyncio
import json
import sys

import pytest
from test_events_bridge import running_bridge

from answerback_bridge import cli
from answerback_bridge.exit_codes import EXIT_FAILURE
from answerback_bridge.pressure import Match
from answerback_bridge.service import Bridge
from answerback_bridge.transport import READ_METHODS

THROTTLED = Match("kiro", "default", "kiro", "hc_screen", "kiro.rate_limit", "0" * 64)


async def answerback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["answerback", *argv])
    try:
        await asyncio.to_thread(cli.main)
        code = 0
    except SystemExit as error:
        code = int(error.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


async def test_pressure_json_prints_the_snapshot_and_clear_lowers_the_level(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async with running_bridge() as (service, path):
        bridge = service.bridge
        bridge.pressure.signal(THROTTLED)
        bridge.pressure.signal(THROTTLED)
        code, out, _ = await answerback(
            monkeypatch, capsys, "--config", str(path), "pressure", "--json"
        )
        assert code == 0
        result = json.loads(out)
        row = result["pressure"]["kiro:default"]
        assert row["level"] == 2 and row["held"] is True and row["retry_after_s"] > 60
        assert result["pressure"]["bedrock:default"]["level"] == 0
        assert result["settings"]["providers"]["kiro"]["cap_seconds"] == 480
        assert result["signals"][0]["signature"] == "kiro.rate_limit"
        assert bridge.events.state["pressure"]["kiro:default"]["level"] == 2

        code, out, _ = await answerback(monkeypatch, capsys, "--config", str(path), "pressure")
        assert code == 0 and "kiro:default" in out and "yes" in out

        code, out, _ = await answerback(
            monkeypatch, capsys, "--config", str(path), "pressure", "--clear", "kiro", "--json"
        )
        assert code == 0
        cleared = json.loads(out)["pressure"]["kiro:default"]
        assert cleared["level"] == 0 and cleared["held"] is False and cleared["until"] is None
        assert bridge.pressure.status("kiro").level == 0
        assert bridge.events.state["pressure"]["kiro:default"]["level"] == 0


async def test_clear_is_refused_for_an_unknown_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async with running_bridge() as (service, path):
        service.bridge.pressure.signal(THROTTLED)
        code, out, err = await answerback(
            monkeypatch, capsys, "--config", str(path), "pressure", "--clear", "openai"
        )
        assert code == EXIT_FAILURE and out == ""
        assert "Unknown pressure provider" in json.loads(err)["error"]
        assert service.bridge.pressure.status("kiro").level == 1
        code, _, err = await answerback(
            monkeypatch, capsys, "--config", str(path), "pressure", "--account", "x"
        )
        assert code == EXIT_FAILURE and "--account requires --clear" in err


def test_status_is_a_read_method_and_clear_is_not() -> None:
    assert {"pressure_status", "pressure_clear"} <= Bridge.METHODS
    assert "pressure_status" in READ_METHODS
    assert "pressure_clear" not in READ_METHODS


async def test_the_bridge_publishes_each_signal_once_with_its_state_section() -> None:
    async with running_bridge() as (service, _):
        bridge = service.bridge
        bridge.pressure.signal(THROTTLED)
        events = [
            event
            for _, event in bridge.events.ring
            if event["type"] == "CUSTOM" and event["name"] == "answerback.provider_throttled"
        ]
        assert len(events) == 1 and events[0]["value"]["level"] == 1
        assert bridge.events.state["pressure"]["kiro:default"]["level"] == 1
        assert bridge.workers.pressure is bridge.pressure
        native = bridge.adapters.controls("codex_cli")
        assert native.pressure is bridge.pressure  # type: ignore[attr-defined]
