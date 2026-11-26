"""Bounded crash-loop guard: a bridge that cannot start must stop retrying forever.

launchd (`KeepAlive: {SuccessfulExit: False}`) and systemd (`Restart=on-failure`)
restart the unit only after a non-zero exit. A config that fails on every startup
(an incompatible upgrade) would otherwise crash-loop at the ten-second throttle for
ever. `serve` records each startup failure in the bridge's own runtime directory;
once the threshold is reached inside the window it prints the reason and exits 0,
which is what stops both supervisors. A successful start clears the record, and a
record older than the window is forgotten, so a later manual `serve` starts normally.
"""

import asyncio
import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from answerback_bridge.config import Config
from answerback_bridge.runtime import (
    CRASH_LOOP_FAILURE_LIMIT,
    CRASH_LOOP_WINDOW_SECONDS,
    Service,
    clear_startup_failures,
    record_startup_failure,
    startup_failures_path,
)
from answerback_bridge.transport import runtime_dir


@pytest.fixture
def short_config_dir() -> Iterator[Path]:
    # macOS Unix sockets have a short pathname limit and serve() checks it before
    # Config.load(), so these tests use the same short /tmp prefix as test_runtime.py.
    # The guard file lands in <dir>/runtime/, never under the owner's config home.
    with tempfile.TemporaryDirectory(prefix="hb-", dir="/tmp") as directory:
        yield Path(directory)


def test_defaults_are_bounded_as_the_spec_says() -> None:
    assert CRASH_LOOP_FAILURE_LIMIT == 5
    assert CRASH_LOOP_WINDOW_SECONDS == 120.0


def test_record_startup_failure_does_not_trip_before_the_limit(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    assert record_startup_failure(config, limit=3, window=60.0) is False
    assert record_startup_failure(config, limit=3, window=60.0) is False


def test_record_startup_failure_trips_once_the_limit_is_reached_within_the_window(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    assert record_startup_failure(config, limit=3, window=60.0) is False
    assert record_startup_failure(config, limit=3, window=60.0) is False
    assert record_startup_failure(config, limit=3, window=60.0) is True


def test_record_startup_failure_forgets_failures_older_than_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    now = 1_000_000.0
    monkeypatch.setattr("answerback_bridge.runtime.time.time", lambda: now)
    assert record_startup_failure(config, limit=2, window=10.0) is False
    now += 20.0  # outside the window: the first failure must be forgotten
    assert record_startup_failure(config, limit=2, window=10.0) is False
    now += 1.0
    assert record_startup_failure(config, limit=2, window=10.0) is True


def test_record_lives_in_the_runtime_directory_with_count_timestamps_and_mode_0600(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    record_startup_failure(config, limit=5, window=60.0)
    record_startup_failure(config, limit=5, window=60.0)
    marker = startup_failures_path(config)
    assert marker.parent == runtime_dir(config)
    assert marker.stat().st_mode & 0o777 == 0o600
    assert marker.parent.stat().st_mode & 0o777 == 0o700
    record = json.loads(marker.read_text())
    assert record["count"] == 2
    assert len(record["timestamps"]) == 2
    assert all(isinstance(stamp, float) for stamp in record["timestamps"])


def test_a_corrupt_record_counts_as_no_prior_failures(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    marker = startup_failures_path(config)
    marker.parent.mkdir(mode=0o700)
    marker.write_text("{not json")
    assert record_startup_failure(config, limit=2, window=60.0) is False
    assert json.loads(marker.read_text())["count"] == 1


def test_clear_startup_failures_resets_the_guard(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    assert record_startup_failure(config, limit=2, window=60.0) is False
    clear_startup_failures(config)
    assert not startup_failures_path(config).exists()
    assert record_startup_failure(config, limit=2, window=60.0) is False


def test_clear_startup_failures_is_a_no_op_without_a_prior_failure(tmp_path: Path) -> None:
    clear_startup_failures(tmp_path / "config.json")  # must not raise
    assert not startup_failures_path(tmp_path / "config.json").exists()


def test_serve_stops_the_restart_loop_after_repeated_incompatible_configs(
    short_config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from answerback_bridge.cli import main

    config = short_config_dir / "config.json"
    # An unknown key stands in for schema drift after an incompatible upgrade.
    config.write_text(json.dumps({"some_removed_field": "x"}))
    monkeypatch.setattr("answerback_bridge.runtime.CRASH_LOOP_FAILURE_LIMIT", 2)
    monkeypatch.setattr("answerback_bridge.runtime.CRASH_LOOP_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr(sys, "argv", ["answerback", "--config", str(config), "serve"])

    with pytest.raises(SystemExit) as info:
        main()
    assert info.value.code == 1
    first = json.loads(capsys.readouterr().err)
    assert "incompatible with this version" in first["error"]
    assert "crash_loop_guard" not in first

    # The second consecutive failure trips the guard: exit 0 is what stops launchd's
    # and systemd's restart-on-failure, and the reason is printed where they log it.
    main()
    tripped = json.loads(capsys.readouterr().err)
    assert "incompatible with this version" in tripped["error"]
    assert "answerback start" in tripped["crash_loop_guard"]
    assert startup_failures_path(config).parent == runtime_dir(config)


def test_serve_reports_a_single_failure_without_the_guard(
    short_config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from answerback_bridge.cli import main

    config = short_config_dir / "config.json"
    config.write_text("{not valid json")
    monkeypatch.setattr(sys, "argv", ["answerback", "--config", str(config), "serve"])

    with pytest.raises(SystemExit) as info:
        main()
    assert info.value.code == 1
    error = json.loads(capsys.readouterr().err)
    assert "not valid JSON" in error["error"]
    assert "crash_loop_guard" not in error
    assert json.loads(startup_failures_path(config).read_text())["count"] == 1


def test_a_successful_start_clears_the_record(short_config_dir: Path) -> None:
    config = short_config_dir / "config.json"
    Config(provider_selection=[]).save(config)
    record_startup_failure(config, limit=5, window=60.0)
    assert startup_failures_path(config).exists()

    async def start_then_stop() -> None:
        service = Service(config)
        serving = asyncio.create_task(service.serve())
        for _ in range(400):  # up to 20 s for the socket to appear
            if service.path.exists():
                break
            await asyncio.sleep(0.05)
        assert service.path.exists()
        assert not startup_failures_path(config).exists()
        service.stopping.set()
        await serving

    asyncio.run(start_then_stop())
