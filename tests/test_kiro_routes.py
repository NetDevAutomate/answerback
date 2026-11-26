import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from codex_herdr_bridge.cli import parser
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.errors import CrewVersionUnsupported
from codex_herdr_bridge.mcp_server import create_server
from codex_herdr_bridge.process import BridgeError
from codex_herdr_bridge.service import Bridge


def test_cli_uses_peer_names_instead_of_requiring_task_ids() -> None:
    assert parser().parse_args(["find", "payments kiro"]).query == "payments kiro"
    args = parser().parse_args(["send", "kiro:reviewer", "--message", "Review"])
    assert args.selector == "kiro:reviewer"
    assert args.request_id
    assert parser().parse_args(["task", "--peer", "kiro:reviewer"]).peer == "kiro:reviewer"
    assert parser().parse_args(["permissions", "--peer", "kiro:reviewer"]).peer == "kiro:reviewer"


async def test_mcp_discovers_then_submits_real_acp_worker(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures/acp_agent.py"
    binary = tmp_path / "fake-kiro"
    binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    binary.chmod(0o700)
    (tmp_path / "agent-settings.json").write_text(
        json.dumps(
            {
                "mcp_open_plans": [
                    {
                        "after": [
                            {
                                "servers": [
                                    {
                                        "name": "answerback_bridge",
                                        "status": "connected",
                                        "tools": [
                                            {"name": "peer_submit", "disabled": False},
                                            {"name": "task_wait", "disabled": False},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        )
    )
    bridge = Bridge(
        Config("test", kiro_binary=str(binary), kiro_workspaces={"reviewer": str(tmp_path)}),
        tmp_path / "config.json",
    )
    try:
        async with Client(create_server(bridge)) as client:
            found = await client.call_tool("peer_search", {"query": "reviewer", "provider": "kiro"})
            assert found.structured_content["peers"][0]["peer_id"] == "kiro:reviewer"
            sent = await client.call_tool(
                "peer_submit",
                {
                    "selector": "kiro:reviewer",
                    "message": "hello",
                    "request_id": "mcp-acp-one",
                },
            )
            task_id = sent.structured_content["id"]
            async with asyncio.timeout(5):
                while True:
                    read = await client.call_tool("task_get", {"task_id": task_id})
                    if read.structured_content["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
            assert read.structured_content["result"] == "Hello world"
            latest = await client.call_tool("task_for_peer", {"selector": "kiro:reviewer"})
            assert latest.structured_content["id"] == task_id
    finally:
        await bridge.close()


async def test_kiro_sessions_launches_kiro_cli_in_the_bridge_runtime_directory(
    tmp_path: Path,
) -> None:
    """kiro_sessions is a read-only MCP tool; its caller-supplied cwd must only ever
    filter the listing, never choose where kiro-cli's process actually starts."""
    from codex_herdr_bridge.transport import runtime_dir

    fixture = Path(__file__).parent / "fixtures/acp_agent.py"
    binary = tmp_path / "fake-kiro"
    binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    binary.chmod(0o700)
    config_path = tmp_path / "config.json"
    runtime = runtime_dir(config_path)
    runtime.mkdir(parents=True)
    (runtime / "agent-settings.json").write_text("{}")
    attacker_project = tmp_path / "attacker-project"
    attacker_project.mkdir()
    # The fake kiro also runs happily from the attacker's directory, so the only
    # thing that decides where its pid marker lands is the bridge's choice of cwd.
    (attacker_project / "agent-settings.json").write_text("{}")
    bridge = Bridge(Config("test", kiro_binary=str(binary)), config_path)
    try:
        result = await bridge.kiro_sessions(cwd=str(attacker_project))
    finally:
        await bridge.close()
    assert not (attacker_project / "agent-pid").exists()
    assert (runtime / "agent-pid").exists()
    requests = [
        json.loads(line) for line in (runtime / "agent-requests.jsonl").read_text().splitlines()
    ]
    listing = next(request for request in requests if request["method"] == "session/list")
    assert listing["params"]["cwd"] == str(attacker_project)
    assert result["sessions"][0]["sessionId"] == "saved"


async def test_pairing_desktop_preserves_kiro_configuration(tmp_path: Path) -> None:
    config = Config("test", kiro_workspaces={"reviewer": str(tmp_path)}, crew_enabled=True)
    bridge = Bridge(config, tmp_path / "config.json")
    await bridge.pair("desk", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    restored = Config.load(tmp_path / "config.json")
    assert restored.kiro_workspaces == {"reviewer": str(tmp_path)}
    assert restored.crew_enabled is True
    await bridge.close()


def test_cli_exposes_simulated_and_explicit_live_harness() -> None:
    args = parser().parse_args(["self-test"])
    assert args.mode == "simulated"
    live = parser().parse_args(
        [
            "self-test",
            "--mode",
            "live",
            "--herdr-peer",
            "herdr:test-runner",
            "--kiro-peer",
            "kiro:test-worker",
        ]
    )
    assert live.herdr_peer == "herdr:test-runner"
    assert live.kiro_peer == "kiro:test-worker"


async def test_crew_configuration_updates_visible_provider_state(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")

    class Crew:
        async def peers(self) -> list:
            return []

    import unittest.mock

    with (
        unittest.mock.patch("codex_herdr_bridge.crew.Crew", return_value=Crew()),
        unittest.mock.patch(
            "answerback_bridge.providers.crew.verbs.shutil.which", return_value="/bin/echo"
        ),
    ):
        try:
            await bridge.crew_configure()
            assert bridge.config.crew_enabled is True
            assert bridge.config.crew_port is None
            assert bridge.provider_status["kirocrew"]["state"] == "connected"
        finally:
            await bridge.close()


async def test_doctor_reports_the_crew_version_read_by_its_own_check(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")

    class Crew:
        version: str | None = None
        version_status: str | None = None
        version_checked_at: float | None = None
        readings = iter([("0.13.0.4", 100.0), ("0.13.1", 200.0)])

        async def peers(self) -> list:
            self.version, self.version_checked_at = next(self.readings)
            self.version_status = "untested"
            return []

    import unittest.mock

    with (
        unittest.mock.patch("codex_herdr_bridge.crew.Crew", return_value=Crew()),
        unittest.mock.patch(
            "answerback_bridge.providers.crew.verbs.shutil.which", return_value="/bin/echo"
        ),
    ):
        try:
            await bridge.crew_configure()
            status = bridge.provider_status["kirocrew"]
            assert (status["version"], status["version_status"]) == ("0.13.0.4", "untested")
            assert status["version_checked_at"] == 100.0
            # Crew was updated in between: doctor shows what its own check read.
            checks = await bridge.doctor()
            status = checks["providers"]["kirocrew"]
            assert (status["version"], status["version_status"]) == ("0.13.1", "untested")
            assert status["version_checked_at"] == 200.0
            assert status["state"] == "connected"
        finally:
            await bridge.close()


async def test_setup_and_doctor_name_a_refused_crew_version(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")

    class Crew:
        version: str | None = None
        version_status: str | None = None

        async def peers(self) -> list:
            self.version, self.version_status = "0.12.1", "unsupported"
            raise CrewVersionUnsupported(
                "Unsupported Crew version 0.12.1; this adapter requires kirocrew 0.12.2 or later"
            )

    import unittest.mock

    with (
        unittest.mock.patch("codex_herdr_bridge.crew.Crew", return_value=Crew()),
        unittest.mock.patch(
            "answerback_bridge.providers.crew.verbs.shutil.which", return_value="/bin/echo"
        ),
    ):
        try:
            with pytest.raises(CrewVersionUnsupported, match="Unsupported Crew version 0.12.1"):
                await bridge.crew_configure()
            status = bridge.provider_status["kirocrew"]
            assert (status["state"], status["version"], status["version_status"]) == (
                "unavailable",
                "0.12.1",
                "unsupported",
            )
            checks = await bridge.doctor()
            assert checks["crew_error"].startswith("Unsupported Crew version 0.12.1;")
            assert "crew_peer_count" not in checks
            status = checks["providers"]["kirocrew"]
            assert (status["state"], status["version"], status["version_status"]) == (
                "unavailable",
                "0.12.1",
                "unsupported",
            )
        finally:
            await bridge.close()


async def test_a_replaced_crew_client_does_not_keep_the_previous_version(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")

    class Reachable:
        version: str | None = None
        version_status: str | None = None
        version_checked_at: float | None = None

        async def peers(self) -> list:
            self.version, self.version_status, self.version_checked_at = (
                "0.13.0.4",
                "untested",
                100.0,
            )
            return []

    during: list[Any] = []

    class Offline:
        async def peers(self) -> list:
            # While the new client's first call is in flight, a status read must not
            # show the version its predecessor read.
            during.append(bridge.provider_status["kirocrew"].get("version", "absent"))
            raise BridgeError("Crew gateway is offline or unavailable on port 1")

    import unittest.mock

    with unittest.mock.patch(
        "answerback_bridge.providers.crew.verbs.shutil.which", return_value="/bin/echo"
    ):
        try:
            with unittest.mock.patch("codex_herdr_bridge.crew.Crew", return_value=Reachable()):
                await bridge.crew_configure()
            assert bridge.provider_status["kirocrew"]["version"] == "0.13.0.4"
            with unittest.mock.patch("codex_herdr_bridge.crew.Crew", return_value=Offline()):
                with pytest.raises(BridgeError, match="offline"):
                    await bridge.crew_configure(port=1)
            assert during == [None]
            status = bridge.provider_status["kirocrew"]
            assert status["state"] == "unavailable"
            assert (status["version"], status["version_status"], status["version_checked_at"]) == (
                None,
                None,
                None,
            )
        finally:
            await bridge.close()


async def test_a_later_login_publishes_its_version_without_a_setup_command(tmp_path: Path) -> None:
    bridge = Bridge(Config("test"), tmp_path / "config.json")

    class Crew:
        version: str | None = None
        version_status: str | None = None
        version_checked_at: float | None = None
        on_version: Any = None

        async def peers(self) -> list:
            self.version, self.version_status, self.version_checked_at = (
                "0.13.0.4",
                "untested",
                100.0,
            )
            return []

    import unittest.mock

    client = Crew()
    with (
        unittest.mock.patch("codex_herdr_bridge.crew.Crew", return_value=client),
        unittest.mock.patch(
            "answerback_bridge.providers.crew.verbs.shutil.which", return_value="/bin/echo"
        ),
    ):
        try:
            await bridge.crew_configure()
            assert bridge.provider_status["kirocrew"]["version"] == "0.13.0.4"
            # What the real client does at a login that discovery, a read or a send
            # triggered after a Crew update: set its fields, then announce the reading.
            client.version, client.version_checked_at = "0.13.1", 200.0
            assert callable(client.on_version)
            client.on_version()
            status = bridge.provider_status["kirocrew"]
            assert (status["version"], status["version_status"]) == ("0.13.1", "untested")
            assert status["version_checked_at"] == 200.0
            assert status["state"] == "connected"
        finally:
            await bridge.close()


async def test_crew_setup_persists_authenticated_endpoint_for_offline_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")

    class Crew:
        origin = "http://localhost:54321"
        port: int | None = None

        async def peers(self) -> list:
            return []

    monkeypatch.setattr("codex_herdr_bridge.crew.Crew", lambda *args, **kwargs: Crew())
    monkeypatch.setattr(
        "answerback_bridge.providers.crew.verbs.shutil.which", lambda _: "/bin/echo"
    )
    await bridge.crew_configure()
    assert Config.load(tmp_path / "config.json").crew_port == 54321
    assert bridge.crew.port == 54321
    await bridge.close()


async def test_peer_submission_cannot_follow_alias_into_another_project(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    import pytest

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    bridge = Bridge(
        Config("test", kiro_workspaces={"reviewer": str(old)}), tmp_path / "config.json"
    )

    async def changed(*args, **kwargs):
        bridge.config.kiro_workspaces["reviewer"] = str(new)
        return {
            "peer_id": "kiro:reviewer",
            "provider": "kiro",
            "target": "reviewer",
            "project": str(old),
        }

    try:
        with (
            patch("codex_herdr_bridge.discovery.Directory.resolve", changed),
            patch.object(bridge.workers, "_run", AsyncMock()),
        ):
            with pytest.raises(ValueError, match="changed"):
                await bridge.peer_submit("old", "Review this project", "stale-discovery")
        assert bridge.store.counts() == {}
    finally:
        await bridge.close()
