"""The bus test fixture skips, instead of erroring, on a host without nats-server."""

import os
import shutil
import sys
from pathlib import Path

import pytest
from bus_helpers import BrokerFixture

# Without nats-py the fixture skips earlier, for the extra; that path is covered by
# tests/test_bus_optional_extra.py in both installs.
pytest.importorskip("nats", reason="nats-py is the optional answerback[bus] extra")


def executable_copy(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(sys.executable).resolve(), path)
    path.chmod(0o700)
    return path


def fake_discovered_binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    nats_server = tmp_path / "nats-server"
    target = Path(sys.executable).resolve()
    nats_server.symlink_to(target)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "missing-mise-data"))
    return nats_server, target


@pytest.mark.parametrize("value", ["1", "0", "true", ""])
def test_offline_ci_skips_nats_even_if_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ANSWERBACK_TEST_OFFLINE", value)
    _fake, target = fake_discovered_binaries(tmp_path, monkeypatch)
    if value == "1":
        with pytest.raises(pytest.skip.Exception, match="ANSWERBACK_TEST_OFFLINE"):
            BrokerFixture(tmp_path)
        assert not (tmp_path / "config.json").exists()
    else:
        assert BrokerFixture(tmp_path).config.server_binary == str(target)


def test_broker_fixture_skips_without_nats_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANSWERBACK_TEST_OFFLINE", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "missing-mise-data"))
    with pytest.raises(pytest.skip.Exception, match="nats-server"):
        BrokerFixture(tmp_path)


def test_broker_fixture_uses_the_discovered_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANSWERBACK_TEST_OFFLINE", raising=False)
    fake, target = fake_discovered_binaries(tmp_path / "bin", monkeypatch)

    try:
        fixture = BrokerFixture(tmp_path / "bus")
    except pytest.skip.Exception as error:
        pytest.fail(f"BrokerFixture skipped although nats-server was found: {error}")

    assert fixture.config.server_binary == str(target)
    assert fixture.process is None
    assert os.access(fixture.config.server_binary, os.X_OK)
    assert fake.is_symlink()


def test_broker_fixture_resolves_mise_shim_without_running_mise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANSWERBACK_TEST_OFFLINE", raising=False)
    mise_data = tmp_path / "mise-data"
    shim = mise_data / "shims" / "nats-server"
    shim.parent.mkdir(parents=True)
    shim.write_text('#!/bin/sh\nexec mise x -- nats-server "$@"\n')
    shim.chmod(0o700)
    installed = executable_copy(mise_data / "installs" / "nats-server" / "2.15.3" / "nats-server")
    monkeypatch.setenv("PATH", str(shim.parent))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MISE_DATA_DIR", str(mise_data))

    fixture = BrokerFixture(tmp_path / "bus")

    assert fixture.config.server_binary == str(installed)


def test_broker_fixture_prefers_plain_binary_before_mise_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANSWERBACK_TEST_OFFLINE", raising=False)
    mise_data = tmp_path / "mise-data"
    shim = mise_data / "shims" / "nats-server"
    shim.parent.mkdir(parents=True)
    shim.write_text('#!/bin/sh\nexec mise x -- nats-server "$@"\n')
    shim.chmod(0o700)
    installed = executable_copy(mise_data / "installs" / "nats-server" / "2.15.3" / "nats-server")
    plain_target = executable_copy(tmp_path / "plain-target" / "nats-server")
    plain_bin = tmp_path / "plain-bin"
    plain_bin.mkdir()
    (plain_bin / "nats-server").symlink_to(plain_target)
    monkeypatch.setenv("PATH", os.pathsep.join((str(plain_bin), str(shim.parent))))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MISE_DATA_DIR", str(mise_data))

    fixture = BrokerFixture(tmp_path / "bus")

    assert fixture.config.server_binary == str(plain_target)
    assert fixture.config.server_binary != str(installed)


def test_skip_reason_names_each_rejected_candidate_and_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANSWERBACK_TEST_OFFLINE", raising=False)
    wrapper_bin = tmp_path / "wrapper-bin"
    wrapper_bin.mkdir()
    wrapper = wrapper_bin / "nats-server"
    wrapper.write_text('#!/bin/sh\nexec /opt/nats/nats-server "$@"\n')
    wrapper.chmod(0o700)
    absent_bin = tmp_path / "absent-bin"
    monkeypatch.setenv("PATH", os.pathsep.join((str(absent_bin), str(wrapper_bin))))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "missing-mise-data"))

    with pytest.raises(pytest.skip.Exception) as skipped:
        BrokerFixture(tmp_path / "bus")

    message = str(skipped.value)
    assert "nats-server" in message
    assert f"{wrapper}: script (#!), not a plain executable" in message
    assert str(absent_bin) not in message
