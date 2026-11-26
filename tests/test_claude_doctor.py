from pathlib import Path

import pytest

from answerback_bridge.config import Config
from answerback_bridge.native_claude import (
    MAX_SETTINGS_BYTES,
    SDK_VERSION,
    _cli_version_error,
    _missing_sdk_error,
    _sdk_version_error,
    _settings_bound_error,
)
from answerback_bridge.process import Output
from answerback_bridge.service import Bridge


def _claude_doctor_bridge(
    tmp_path: Path, version_output: str = "2.1.330 (Claude Code)"
) -> tuple[Bridge, Path, Path]:
    home = tmp_path / "claude-home"
    home.mkdir()
    settings = home / "settings.json"
    settings.write_text('{"env":{}}')
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version_output}'\n")
    binary.chmod(0o700)
    bridge = Bridge(
        Config(
            "test",
            provider_selection=["claude_code"],
            claude_code_binary=str(binary),
            claude_code_home=str(home),
            claude_code_workspaces={"review": str(tmp_path)},
        ),
        tmp_path / "config.json",
    )
    return bridge, settings, binary


@pytest.mark.parametrize(
    ("version_output", "expected_refusal", "healthy"),
    [
        ("2.1.330 (Claude Code)", None, True),
        ("2.1.292 (Claude Code)", str(_cli_version_error("2.1.292")), False),
        ("garbage", "Claude CLI version output could not be parsed: 'garbage'", False),
    ],
)
async def test_doctor_parses_claude_version_command_output(
    tmp_path: Path, version_output: str, expected_refusal: str | None, healthy: bool
) -> None:
    """Doctor accepts the actual CLI format and reports malformed output."""
    bridge, settings, binary = _claude_doctor_bridge(tmp_path, version_output)
    try:
        checks = await bridge.doctor()
    finally:
        await bridge.close()

    assert checks["claude"] == {
        "binary": str(binary.resolve()),
        "version": version_output,
        "settings": {
            "path": str(settings),
            "size_bytes": settings.stat().st_size,
            "limit_bytes": MAX_SETTINGS_BYTES,
        },
        "sdk": {"found": SDK_VERSION, "pinned": SDK_VERSION},
        "refusal": expected_refusal,
    }
    assert checks["healthy"] is healthy


async def test_doctor_reports_the_task_path_missing_sdk_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor exposes the missing-SDK failure a task would raise."""
    bridge, _, _ = _claude_doctor_bridge(tmp_path)

    def missing_sdk(name: str) -> object:
        assert name == "claude_agent_sdk"
        raise ImportError

    monkeypatch.setattr("answerback_bridge.service.importlib.import_module", missing_sdk)
    try:
        checks = await bridge.doctor()
    finally:
        await bridge.close()

    assert checks["claude"]["refusal"] == str(_missing_sdk_error())


async def test_doctor_reports_the_task_path_sdk_version_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor exposes an installed SDK version that the task would reject."""
    bridge, _, _ = _claude_doctor_bridge(tmp_path)
    sdk = type("SDK", (), {"__version__": "0.0.0"})
    monkeypatch.setattr("answerback_bridge.service.importlib.import_module", lambda _: sdk)
    try:
        checks = await bridge.doctor()
    finally:
        await bridge.close()

    assert checks["claude"]["refusal"] == str(_sdk_version_error("0.0.0"))


async def test_doctor_reports_the_task_path_oversized_settings_refusal(tmp_path: Path) -> None:
    """Doctor exposes oversized settings before a task can start."""
    bridge, settings, _ = _claude_doctor_bridge(tmp_path)
    settings.write_bytes(b"x" * (MAX_SETTINGS_BYTES + 1))
    try:
        checks = await bridge.doctor()
    finally:
        await bridge.close()

    assert checks["claude"]["refusal"] == str(_settings_bound_error(MAX_SETTINGS_BYTES + 1))


async def test_doctor_limits_the_claude_version_command_to_ten_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor bounds its version-only subprocess more tightly than worker startup."""
    bridge, _, binary = _claude_doctor_bridge(tmp_path)

    async def fake_run(args: list[str], **kwargs: object) -> Output:
        assert args == [str(binary.resolve()), "--version"]
        assert kwargs == {"max_seconds": 10}
        return Output("2.1.330 (Claude Code)\n", "")

    monkeypatch.setattr("answerback_bridge.service.run", fake_run)
    try:
        checks = await bridge.doctor()
    finally:
        await bridge.close()

    assert checks["claude"]["refusal"] is None
    assert checks["healthy"] is True


async def test_doctor_omits_claude_preflight_without_a_claude_workspace(tmp_path: Path) -> None:
    bridge = Bridge(Config("test", provider_selection=["claude_code"]), tmp_path / "config.json")
    try:
        checks = await bridge.doctor()
    finally:
        await bridge.close()

    assert "claude" not in checks
