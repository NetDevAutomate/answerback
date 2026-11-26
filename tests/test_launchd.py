"""Supervisor stop must observe removal, not just bootout acknowledgement."""

import json
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.process import BridgeError, Output
from codex_herdr_bridge.supervision import NativeSupervisor


async def test_launchd_stop_waits_for_registration_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = NativeSupervisor(tmp_path / "config.json")
    supervisor.root.mkdir()
    supervisor.marker.write_text(json.dumps({"manager": "launchd", "label": supervisor.label}))
    state = {"stopping": False, "removed": False, "observations": 0}

    async def launchctl(args: list[str], **kwargs: Any) -> Output:
        if args[1] == "bootout":
            state["stopping"] = True
            return Output("", "")
        assert args[1] == "print"
        if state["stopping"]:
            state["observations"] += 1
            if state["observations"] >= 2:
                from codex_herdr_bridge.process import CommandError

                state["removed"] = True
                raise CommandError(113, "Could not find service")
            return Output("state = SIGTERMed", "")
        return Output("state = running", "")

    monkeypatch.setattr("codex_herdr_bridge.supervision.sys.platform", "darwin")
    monkeypatch.setattr("codex_herdr_bridge.supervision.run", launchctl)
    await supervisor.stop_if_installed()
    assert state["removed"] is True
    assert not supervisor.marker.exists()


async def test_unknown_launchd_state_keeps_supervisor_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = NativeSupervisor(tmp_path / "config.json")
    supervisor.root.mkdir()
    supervisor.marker.write_text(json.dumps({"manager": "launchd", "label": supervisor.label}))

    async def launchctl(args: list[str], **kwargs: Any) -> Output:
        raise BridgeError("Permission denied while querying supervisor")

    monkeypatch.setattr("codex_herdr_bridge.supervision.sys.platform", "darwin")
    monkeypatch.setattr("codex_herdr_bridge.supervision.run", launchctl)
    with pytest.raises(BridgeError, match="Permission denied"):
        await supervisor.stop_if_installed()
    assert supervisor.marker.exists()
