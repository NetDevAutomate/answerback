import json
import os
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.lifecycle import service_command
from codex_herdr_bridge.process import module_launch_argv


def test_service_command_is_immune_to_the_supervisors_working_directory(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    assert service_command(config_path) == module_launch_argv(
        config_path.expanduser().resolve(), "serve"
    )


async def test_stop_removes_the_launch_agent_the_uninstall_path_relies_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`answerback uninstall` calls lifecycle.stop first; it must leave no plist and no job."""
    from codex_herdr_bridge import lifecycle
    from codex_herdr_bridge.config import Config
    from codex_herdr_bridge.process import CommandError, Output
    from codex_herdr_bridge.supervision import NativeSupervisor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    config_path = tmp_path / "cfg" / "config.json"
    Config("explicit-session").save(config_path)
    supervisor = NativeSupervisor(config_path)
    target = f"gui/{os.getuid()}/{supervisor.label}"
    plist = home / "Library" / "LaunchAgents" / f"{supervisor.label}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(supervisor.launchd_definition())
    supervisor.root.mkdir(mode=0o700)
    supervisor.marker.write_text(
        json.dumps({"manager": "launchd", "label": supervisor.label, "definition": str(plist)})
    )
    registered = {target}
    calls: list[list[str]] = []

    async def launchctl(args: list[str], **kwargs: Any) -> Output:
        calls.append(list(args))
        if args[1] == "print":
            if args[2] in registered:
                return Output("state = running", "")
            raise CommandError(113, "Could not find service")
        assert args[1] == "bootout"
        registered.discard(args[2])
        return Output("", "")

    class StoppedBridge:
        def __init__(self, config_path: Path) -> None:
            pass

        async def status(self) -> dict[str, Any]:
            return {"running": False}

    monkeypatch.setattr("codex_herdr_bridge.supervision.sys.platform", "darwin")
    monkeypatch.setattr("codex_herdr_bridge.supervision.run", launchctl)
    monkeypatch.setattr("codex_herdr_bridge.lifecycle.RemoteBridge", StoppedBridge)

    status = await lifecycle.stop(config_path)

    assert status == {"running": False}
    assert ["launchctl", "bootout", target] in calls
    assert registered == set()
    assert not plist.exists()
    assert not supervisor.marker.exists()
