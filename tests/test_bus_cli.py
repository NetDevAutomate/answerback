import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from bus_helpers import unused_server_binary

from codex_herdr_bridge.config import Config


def run(config: Path, *arguments: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "codex_herdr_bridge.bus.cli", "--config", str(config), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_neutral_cli_accepts_inspects_and_cancels_offline(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    Config(provider_selection=["kiro"], kiro_workspaces={"worker": str(tmp_path)}).save(config)
    configured = run(config, "configure", "--server", unused_server_binary(tmp_path))
    assert configured["url"].startswith("nats://127.0.0.1:")
    queued = run(
        config,
        "submit",
        "kiro:worker",
        "--project",
        str(tmp_path),
        "--message",
        "Review",
        "--request-id",
        "cli-one",
        "--offline",
    )
    assert queued["delivery"] == "queued"
    assert run(config, "result", queued["command_id"])["task"] is None
    assert run(config, "events")["events"][0]["type"] == "command.accepted"
    assert run(config, "cancel", queued["command_id"])["delivery"] == "cancelled"


def test_new_neutral_configuration_does_not_require_herdr(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    run(config, "configure", "--server", unused_server_binary(tmp_path))
    saved = Config.load(config)
    assert saved.registered_providers() == []
    assert saved.herdr_session is None
    assert not saved.hc_enabled


@pytest.mark.parametrize("mask", [0o002, 0o022, 0o077], ids=["002", "022", "077"])
def test_first_setup_creates_a_private_configuration_folder_whatever_the_umask(
    tmp_path: Path, mask: int
) -> None:
    """T-77 (fix round): `configure` on a machine that has no configuration folder yet.

    The bus setup makes `<configuration folder>/bus` before it saves the bridge
    configuration. The folder above `bus` got the default mode (0775 under umask 002), and
    the save then refused the folder that the same command had just made. The suite runs
    under umask 022, so the test sets the umask itself; the child process inherits it.
    """
    server = unused_server_binary(tmp_path)
    config = tmp_path / "new-bridge" / "config.json"
    os.umask(mask)

    configured = run(config, "configure", "--server", server)

    assert configured["configured"] is True
    assert Config.load(config).registered_providers() == []
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((config.parent / "bus").stat().st_mode) == 0o700


def test_run6_message_file_from_a_fifo_is_read_with_a_bound(tmp_path: Path) -> None:
    """red_at_base (run-6 hardening h1): st_size is 0 for a FIFO, so the size check passed."""
    import argparse
    import threading

    from answerback_bridge.bus.cli import body

    fifo = tmp_path / "message"
    os.mkfifo(fifo, 0o600)

    def feed() -> None:
        try:
            with fifo.open("wb") as writer:
                writer.write(b"x" * (1024 * 1024))
        except BrokenPipeError:
            pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    args = argparse.Namespace(message=None, message_file=fifo)
    with pytest.raises(ValueError, match="exceeds"):
        body(args)
    writer.join(timeout=10)
    large = tmp_path / "large"
    large.write_bytes(b"x" * 100_001)
    with pytest.raises(ValueError, match="Message file exceeds 100000 bytes"):
        body(argparse.Namespace(message=None, message_file=large))
    small = tmp_path / "small"
    small.write_text("hello\n")
    assert body(argparse.Namespace(message=None, message_file=small)) == "hello\n"
