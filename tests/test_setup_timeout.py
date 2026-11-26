import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.crew import Crew
from codex_herdr_bridge.process import BridgeError, CommandError
from codex_herdr_bridge.runtime import Service
from codex_herdr_bridge.service import Bridge
from codex_herdr_bridge.transport import RPC_VERSION, encode


class Writer:
    data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


async def test_setup_has_its_own_timeout_without_extending_regular_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("codex_herdr_bridge.transport.REQUEST_SECONDS", 0.005, raising=False)
    monkeypatch.setattr("codex_herdr_bridge.transport.SETUP_SECONDS", 0.2, raising=False)
    path = tmp_path / "config.json"
    Config("fixture").save(path)
    service = Service(path)
    service.bridge = Bridge(Config.load(path), path)
    monkeypatch.setattr(service, "event", lambda *a, **kw: None)

    async def slow(*args: Any) -> dict[str, bool]:
        await asyncio.sleep(0.03)
        return {"configured": True}

    monkeypatch.setattr(service.bridge, "call", slow)
    for method, expected_ok in [("crew_configure", True), ("doctor", False)]:
        reader, writer = asyncio.StreamReader(), Writer()
        reader.feed_data(
            encode(
                {
                    "version": RPC_VERSION,
                    "config_path": str(path.resolve()),
                    "method": method,
                    "params": {},
                }
            )
        )
        await service.handle(reader, writer)
        assert json.loads(writer.data)["ok"] is expected_ok


async def test_failed_crew_version_check_reports_exit_code_without_child_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed(*args: Any, **kwargs: Any) -> None:
        raise CommandError(9, "secret-credential-do-not-print")

    monkeypatch.setattr("codex_herdr_bridge.crew.run", failed)
    with pytest.raises(BridgeError) as error:
        await Crew("/missing/crew").peers()
    assert "exit 9" in str(error.value)
    assert "secret-credential" not in str(error.value)
