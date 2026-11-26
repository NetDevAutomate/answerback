import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from bus_helpers import unused_server_binary

from codex_herdr_bridge.bus.config import BusConfig, routing_digest
from codex_herdr_bridge.config import Config


def test_private_configuration_is_idempotent_and_does_not_change_bridge_config(
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "config.json"
    Config(provider_selection=["codex"]).save(bridge)
    before = bridge.read_bytes()
    binary = unused_server_binary(tmp_path)
    assert Path(binary).is_file(), "The fixture must provide an executable test dependency"
    cfg = BusConfig.create(bridge, server_binary=binary, port=4222)
    token = cfg.token()
    assert len(token) >= 32
    assert token not in cfg.path.read_text()
    assert cfg.root.stat().st_mode & 0o777 == 0o700
    assert cfg.token_path.stat().st_mode & 0o777 == 0o600
    assert cfg.broker_config.stat().st_mode & 0o777 == 0o600
    assert cfg.url == "nats://127.0.0.1:4222"
    assert BusConfig.create(bridge, server_binary=binary, port=4222) == cfg
    assert cfg.token() == token
    assert bridge.read_bytes() == before
    with pytest.raises(ValueError, match="different"):
        BusConfig.create(bridge, server_binary=binary, port=4223)


def test_token_permissions_symlinks_and_config_tampering_fail_closed(tmp_path: Path) -> None:
    cfg = BusConfig.create(
        tmp_path / "config.json", server_binary=unused_server_binary(tmp_path), port=4222
    )
    cfg.token_path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        cfg.token()
    cfg.token_path.chmod(0o600)
    elsewhere = tmp_path / "other"
    cfg.token_path.rename(elsewhere)
    cfg.token_path.symlink_to(elsewhere)
    with pytest.raises(ValueError, match="private"):
        cfg.token()
    data = json.loads(cfg.path.read_text())
    data["host"] = "0.0.0.0"
    cfg.path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        BusConfig.load(tmp_path / "config.json")


def test_routing_digest_ignores_heartbeats_but_changes_with_target_bindings(tmp_path: Path) -> None:
    cfg = Config(provider_selection=["kiro"], kiro_workspaces={"worker": str(tmp_path)})
    before = routing_digest(cfg)
    cfg.heartbeat_interval += 1
    assert routing_digest(cfg) == before
    cfg.kiro_workspaces["worker"] = str(tmp_path / "different")
    assert routing_digest(cfg) != before


@pytest.mark.parametrize("port", [0, 65536, True, "4222"])
def test_invalid_listener_is_rejected(tmp_path: Path, port: object) -> None:
    with pytest.raises(ValueError):
        BusConfig.create(tmp_path / "config.json", server_binary=os.devnull, port=port)


def test_setup_obeys_single_writer_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    binary = unused_server_binary(tmp_path)
    contended = Event()
    original_flock = fcntl.flock

    def observe_contention(fd: int, operation: int) -> None:
        try:
            original_flock(fd, operation)
        except BlockingIOError:
            contended.set()
            raise

    monkeypatch.setattr(fcntl, "flock", observe_contention)
    root = tmp_path / "bus"
    root.mkdir(mode=0o700)
    with (root / "setup.lock").open("w") as lock, ThreadPoolExecutor() as pool:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        future = pool.submit(
            BusConfig.create,
            tmp_path / "config.json",
            server_binary=binary,
            port=4222,
        )
        try:
            assert contended.wait(timeout=2), "Setup must reach the held writer lock"
            assert not future.done(), "Configuration must wait for the other writer"
            assert not (root / "config.json").exists()
            assert not (root / "token").exists()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        config = future.result(timeout=2)
    assert config.token() in config.broker_config.read_text()


def test_receipts_remain_readable_after_broker_binary_is_removed(tmp_path: Path) -> None:
    from codex_herdr_bridge.bus.api import BusAPI

    executable = tmp_path / "removed-nats-server"
    shutil.copyfile(Path(sys.executable).resolve(), executable)
    executable.chmod(0o700)
    path = tmp_path / "config.json"
    Config(provider_selection=["kiro"]).save(path)
    BusConfig.create(path, server_binary=str(executable))
    api = BusAPI(path)
    row = api.submit("kiro:worker", str(tmp_path), "Review", "upgrade-test")
    executable.unlink()
    assert api.result(row["command_id"])["delivery"] == "queued"
    assert api.cancel(row["command_id"])["delivery"] == "cancelled"


def test_config_resolves_a_mise_launcher_to_the_binary_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "nats-server"
    shim.write_text('#!/bin/sh\nexec mise x -- nats-server "$@"\n')
    shim.chmod(0o700)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    resolved = Path(sys.executable).resolve()
    mise.write_text(
        "#!/bin/sh\n"
        'test "$#" -eq 2 && test "$1" = which && test "$2" = nats-server '
        f"&& printf '%s\\n' '{resolved}'\n"
    )
    mise.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))

    config = BusConfig.create(tmp_path / "config.json", server_binary=str(shim))

    assert config.server_binary == str(resolved)
    assert json.loads(config.path.read_text())["server_binary"] == str(resolved)


@pytest.mark.parametrize(
    ("variable", "suffix"),
    [("MISE_SHIMS_DIR", ""), ("MISE_DATA_DIR", "shims")],
)
def test_config_recognizes_custom_mise_shim_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    suffix: str,
) -> None:
    configured_directory = tmp_path / "configured-mise"
    shim_directory = configured_directory / suffix
    shim_directory.mkdir(parents=True)
    shim = shim_directory / "nats-server"
    shutil.copyfile(Path(sys.executable).resolve(), shim)
    shim.chmod(0o700)
    resolved = tmp_path / "resolved-nats-server"
    shutil.copyfile(Path(sys.executable).resolve(), resolved)
    resolved.chmod(0o700)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    mise.write_text(f"#!/bin/sh\nprintf '%s\\n' '{resolved}'\n")
    mise.chmod(0o700)
    monkeypatch.setenv(variable, str(configured_directory))
    monkeypatch.setenv("PATH", str(fake_bin))

    config = BusConfig.create(tmp_path / "config.json", server_binary=str(shim))

    assert config.server_binary == str(resolved)


def test_config_refuses_a_non_mise_wrapper_instead_of_replacing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / "nats-server"
    wrapper.write_text('#!/bin/sh\nexec /opt/homebrew/bin/nats-server "$@"\n')
    wrapper.chmod(0o700)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    mise.write_text(f"#!/bin/sh\nprintf '%s\\n' '{Path(sys.executable).resolve()}'\n")
    mise.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))

    with pytest.raises(ValueError, match="launcher shim cannot be owned"):
        BusConfig.create(tmp_path / "config.json", server_binary=str(wrapper))

    assert not (tmp_path / "bus/config.json").exists()


def test_config_refuses_an_unresolvable_launcher_without_persisting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "nats-server"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    with pytest.raises(
        ValueError,
        match=(
            rf"^{shim}: configure the real nats-server binary; "
            r"a launcher shim cannot be owned$"
        ),
    ):
        BusConfig.create(tmp_path / "config.json", server_binary=str(shim))

    assert not (tmp_path / "bus/config.json").exists()


def test_configure_again_migrates_a_legacy_mise_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "config.json"
    original = BusConfig.create(bridge, server_binary=unused_server_binary(tmp_path))
    token = original.token()
    shim = tmp_path / "nats-server"
    shim.write_text('#!/bin/sh\nexec mise x -- nats-server "$@"\n')
    shim.chmod(0o700)
    saved = json.loads(original.path.read_text())
    saved["server_binary"] = str(shim)
    original.path.write_text(json.dumps(saved))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    resolved = Path(sys.executable).resolve()
    mise.write_text(f"#!/bin/sh\nprintf '%s\\n' '{resolved}'\n")
    mise.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))

    migrated = BusConfig.create(bridge, server_binary=str(shim))

    assert migrated.server_binary == str(resolved)
    assert BusConfig.load(bridge) == migrated
    assert migrated.token() == token


def test_legacy_launcher_migration_refuses_a_different_explicit_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "config.json"
    original = BusConfig.create(bridge, server_binary=unused_server_binary(tmp_path), port=4222)
    shim = tmp_path / "nats-server"
    shim.write_text('#!/bin/sh\nexec mise x -- nats-server "$@"\n')
    shim.chmod(0o700)
    saved = json.loads(original.path.read_text())
    saved["server_binary"] = str(shim)
    original.path.write_text(json.dumps(saved))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    mise.write_text(f"#!/bin/sh\nprintf '%s\\n' '{Path(sys.executable).resolve()}'\n")
    mise.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))

    with pytest.raises(ValueError, match="different bus"):
        BusConfig.create(bridge, server_binary=str(shim), port=4333)

    assert BusConfig.load(bridge).port == 4222


def test_config_persists_the_target_of_a_stable_binary_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Path(unused_server_binary(tmp_path)).resolve()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    mise.write_text("#!/bin/sh\nexit 1\n")
    mise.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))
    link = tmp_path / "nats-server"
    link.symlink_to(target)
    config = BusConfig.create(tmp_path / "config.json", server_binary=str(link))
    assert config.server_binary == str(target)


def test_config_resolves_a_symlink_to_the_mise_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    mise.symlink_to(Path(sys.executable).resolve())
    shim = tmp_path / "nats-server"
    shim.symlink_to(mise)
    resolved = tmp_path / "resolved-nats-server"
    shutil.copyfile(Path(sys.executable).resolve(), resolved)
    resolved.chmod(0o700)
    calls: list[list[str]] = []

    def run_mise(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, f"{resolved}\n", "")

    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setattr("answerback_bridge.bus.config.subprocess.run", run_mise)

    config = BusConfig.create(tmp_path / "config.json", server_binary=str(shim))

    assert config.server_binary == str(resolved)
    assert calls == [[str(mise), "which", "nats-server"]]


def test_mise_resolution_uses_bus_root_and_minimal_user_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in tuple(os.environ):
        if variable.startswith("MISE_"):
            monkeypatch.delenv(variable)
    shim = tmp_path / "nats-server"
    shim.write_text('#!/bin/sh\nexec mise x -- nats-server "$@"\n')
    shim.chmod(0o700)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    mise.write_text("#!/bin/sh\nexit 1\n")
    mise.chmod(0o700)
    resolved = Path(sys.executable).resolve()
    observed: dict[str, object] = {}

    def run_mise(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, f"{resolved}\n", "")

    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "mise-data"))
    monkeypatch.setenv("UNRELATED_FIXTURE_VALUE", "must-not-leak")
    monkeypatch.setattr("answerback_bridge.bus.config.subprocess.run", run_mise)

    BusConfig.create(tmp_path / "config.json", server_binary=str(shim))

    assert observed["cwd"] == str(tmp_path / "bus")
    assert observed["env"] == {
        "HOME": str(tmp_path / "home"),
        "MISE_DATA_DIR": str(tmp_path / "mise-data"),
        "PATH": str(fake_bin),
    }


def test_private_read_returns_the_bytes_of_the_inode_it_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory-entry swap after the metadata check must not change what is read.

    Stand-in for a lost rename race: the swap runs the moment the metadata check returns,
    whether that check is a pathname lstat or an fstat of the opened descriptor.
    """
    from answerback_bridge.bus import config as bus_config

    checked = tmp_path / "native.json"
    checked.write_text('{"native": "checked"}')
    checked.chmod(0o600)
    marker = tmp_path / "marker.json"
    marker.write_text('{"marker": "MARKER"}')
    marker.chmod(0o644)

    armed: list[bool] = []

    def swap() -> None:
        if armed and not checked.is_symlink():
            checked.rename(tmp_path / "moved.json")
            checked.symlink_to(marker)

    class RacePath(type(checked)):  # type: ignore[misc]
        def lstat(self) -> os.stat_result:
            info = super().lstat()
            swap()
            return info

    real_fstat = os.fstat

    def racing_fstat(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        swap()
        return info

    monkeypatch.setattr(bus_config.os, "fstat", racing_fstat)
    with pytest.raises(ValueError, match="private"):
        bus_config.private_read(marker, 16384)

    armed.append(True)
    assert bus_config.private_read(RacePath(checked), 16384) == '{"native": "checked"}'
    assert checked.is_symlink(), "the stand-in swap must have run"
    with pytest.raises(ValueError, match="private"):
        bus_config.private_read(checked, 16384)


def test_private_read_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    from answerback_bridge.bus.config import private_read

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="private"):
        private_read(fifo, 1024)
