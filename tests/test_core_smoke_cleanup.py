import runpy
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.process import BridgeError, Output


async def test_native_smoke_stops_only_its_core_after_uncertain_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/smoke_core.py"))
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def run(args: list[str], *, env: dict[str, str], **kwargs: Any) -> Output:
        calls.append((args, env))
        if args[1] == "run":
            raise BridgeError("launch reply lost")
        return Output("{}", "")

    monkeypatch.setitem(module["smoke"].__globals__, "run", run)
    monkeypatch.setattr(module["shutil"], "which", lambda _: "/fake/hc")
    monkeypatch.setenv("HC_SESSION", "user-session")
    monkeypatch.setenv("HC_SOCKET", "/user/socket")
    with pytest.raises(BridgeError, match="reply lost"):
        await module["smoke"](tmp_path)
    assert any(args[:3] == ["/fake/hc", "server", "stop"] for args, _ in calls)
    assert all(
        env["HC_SESSION"].startswith("smoke-") and "HC_SOCKET" not in env for _, env in calls
    )
    assert all(env["XDG_CONFIG_HOME"] == str(tmp_path / "config") for _, env in calls)
