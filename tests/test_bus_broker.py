import asyncio
import json
import plistlib
import sys
from pathlib import Path

import pytest
from bus_helpers import BrokerFixture

pytest.importorskip("nats", reason="nats-py is the optional answerback[bus] extra")
from codex_herdr_bridge.bus.broker import Broker, BrokerSupervisor
from codex_herdr_bridge.bus.config import BusConfig
from codex_herdr_bridge.process import BridgeError, CommandError, Output


async def test_foreign_listener_is_not_replaced(tmp_path: Path) -> None:
    server = BrokerFixture(tmp_path / "first")
    await server.start()
    try:
        other = BusConfig.create(
            tmp_path / "second/config.json",
            server_binary=server.config.server_binary,
            port=server.config.port,
        )
        broker = Broker(other)
        with pytest.raises(BridgeError, match="occupied"):
            await broker.start()
        assert not broker.supervisor.marker.exists()
        assert await Broker(server.config).reachable()
    finally:
        await server.stop()


async def test_stop_does_not_kill_unmanaged_broker(tmp_path: Path) -> None:
    server = BrokerFixture(tmp_path)
    await server.start()
    try:
        with pytest.raises(BridgeError, match="unmanaged"):
            await Broker(server.config).stop()
        assert await Broker(server.config).reachable()
    finally:
        await server.stop()


def test_supervisor_passes_config_path_not_credential(tmp_path: Path) -> None:
    server = BrokerFixture(tmp_path)
    supervisor = BrokerSupervisor(server.config)
    definition = supervisor.launchd_definition()
    assert server.config.token().encode() not in definition
    properties = plistlib.loads(definition)
    assert properties["ProgramArguments"] == [
        server.config.server_binary,
        "-c",
        str(server.config.broker_config),
    ]
    assert properties["KeepAlive"] is True
    assert properties["Umask"] == 0o077


def test_supervisor_restarts_the_broker_after_every_systemd_exit(tmp_path: Path) -> None:
    definition = _broker_of_folder(str(tmp_path)).systemd_definition()
    assert "Restart=always\n" in definition
    assert "Restart=on-failure\n" not in definition


def _broker_of_folder(folder: str) -> BrokerSupervisor:
    # A folder written out, not a temporary one: the constructors and systemd_definition()
    # neither read nor write it, and no value depends on the name of a temporary folder.
    return BrokerSupervisor(BusConfig(Path(folder) / "config.json", 4222, "/nats"))


def test_broker_systemd_working_directory_is_written_like_the_bridge_unit() -> None:
    """red_at_pretip. systemd 259.9 refused the broker's unit of d2146a5 as it refused the
    bridge's ("WorkingDirectory= path is not absolute": the value was quoted). The value is
    now the bus folder with '%' doubled and '/.' appended; a '$' in it is written once."""
    definition = _broker_of_folder("/t146-no-such-folder/a b %h $x").systemd_definition()
    values = [
        line[len("WorkingDirectory=") :]
        for line in definition.split("\n")
        if line.startswith("WorkingDirectory=")
    ]
    assert values == ["/t146-no-such-folder/a b %%h $x/bus/."]


def test_broker_systemd_exec_start_keeps_its_quoting() -> None:
    """pins_existing. ExecStart= of the broker's unit is the text that d2146a5 wrote."""
    definition = _broker_of_folder("/t146-no-such-folder/a b %h $x").systemd_definition()
    lines = [line for line in definition.split("\n") if line.startswith("ExecStart=")]
    assert lines == ['ExecStart="/nats" "-c" "/t146-no-such-folder/a b %%h $$x/bus/nats.conf"']


@pytest.mark.parametrize("name", ["a\nb", "a\rb"])
def test_broker_systemd_definition_refuses_a_line_break_in_the_folder(name: str) -> None:
    """red_at_pretip. d2146a5 wrote both folders into a unit, as JSON escapes in quotes."""
    supervisor = _broker_of_folder("/t146-no-such-folder/" + name)
    with pytest.raises(BridgeError, match="line feed or a carriage return"):
        supervisor.systemd_definition()


@pytest.fixture(params=["darwin", "linux"])
def native_manager(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[str, list[list[str]]]:
    """Exercise the real supervisor lifecycle without registering an OS service.

    HOME is faked: on macOS the supervisor installs its plist under
    ~/Library/LaunchAgents, and this test must never reach the owner's real one.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("answerback_bridge.supervision.sys.platform", request.param)
    calls: list[list[str]] = []
    registered = False

    async def run_manager(args: list[str], *, max_seconds: float = 30) -> Output:
        nonlocal registered
        calls.append(args)
        if args[:2] == ["launchctl", "print"] and not registered:
            raise CommandError(113, "Fixture service is absent")
        if args[:2] == ["launchctl", "bootstrap"]:
            registered = True
        elif args[:2] == ["launchctl", "bootout"]:
            registered = False
        return Output("", "")

    monkeypatch.setattr("answerback_bridge.supervision.run", run_manager)
    return ("launchd" if request.param == "darwin" else "systemd"), calls


def _native_broker(tmp_path: Path) -> BrokerSupervisor:
    # No NATS process or credentials are needed to verify service ownership.
    config = BusConfig(tmp_path / "path with spaces %value $literal/config.json", 4222, "/nats")
    supervisor = BrokerSupervisor(config)
    supervisor.root.mkdir(parents=True)
    return supervisor


def _write_definition(supervisor: BrokerSupervisor, manager: str) -> Path:
    path = supervisor.root / (supervisor.label + (".plist" if manager == "launchd" else ".service"))
    if manager == "launchd":
        path.write_bytes(supervisor.launchd_definition())
    else:
        path.write_text(supervisor.systemd_definition())
    return path


async def test_broker_native_first_start_repeat_start_and_stop(
    tmp_path: Path, native_manager: tuple[str, list[list[str]]]
) -> None:
    manager, calls = native_manager
    supervisor = _native_broker(tmp_path)
    assert not supervisor.legacy_installed()
    await supervisor.start()
    definition = (
        Path.home() / "Library" / "LaunchAgents" / f"{supervisor.label}.plist"
        if manager == "launchd"
        else supervisor.root / f"{supervisor.label}.service"
    )
    assert definition.is_file()
    assert definition.is_relative_to(tmp_path), "the broker's plist lands under the fake HOME"
    assert json.loads(supervisor.marker.read_text()) == {
        "manager": manager,
        "label": supervisor.label,
        "definition": str(definition),
    }
    await supervisor.start()
    assert not supervisor.legacy_installed()
    await supervisor.stop_if_installed()
    assert not supervisor.marker.exists()
    assert not definition.exists()
    assert not list(supervisor.root.glob("*.plist"))
    assert not list(supervisor.root.glob("*.service"))
    if manager == "launchd":
        assert [args[1] for args in calls if args[1] != "print"] == [
            "bootstrap",
            "kickstart",
            "bootout",
        ]
    else:
        assert [args[2] for args in calls] == [
            "enable",
            "daemon-reload",
            "start",
            "enable",
            "daemon-reload",
            "start",
            "stop",
            "disable",
        ]


async def test_broker_start_replaces_an_owned_launcher_definition_after_reconfigure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_manager: tuple[str, list[list[str]]],
) -> None:
    manager, calls = native_manager
    bridge = tmp_path / "config.json"
    shim = tmp_path / "nats-server"
    shim.write_text('#!/bin/sh\nexec mise x -- nats-server "$@"\n')
    shim.chmod(0o700)
    old = BrokerSupervisor(BusConfig(bridge, 4222, str(shim)))
    old.root.mkdir(parents=True)
    definition = _write_definition(old, manager)
    old.marker.write_text(json.dumps({"manager": manager, "label": old.label}))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    resolved = await asyncio.to_thread(Path(sys.executable).resolve)
    mise.write_text(f"#!/bin/sh\nprintf '%s\\n' '{resolved}'\n")
    mise.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))
    supervisor = BrokerSupervisor(BusConfig(bridge, 4222, str(resolved)))

    await supervisor.start()

    installed = Path(json.loads(supervisor.marker.read_text())["definition"])
    installed_exists, installed_text = await asyncio.to_thread(
        lambda: (installed.is_file(), installed.read_text())
    )
    assert installed_exists
    assert str(resolved) in installed_text
    assert str(shim) not in installed_text
    assert calls
    if definition != installed:
        assert not definition.exists()


async def test_broker_stop_accepts_its_launcher_definition_after_cwd_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_manager: tuple[str, list[list[str]]],
) -> None:
    manager, calls = native_manager
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    first_cwd.mkdir()
    second_cwd.mkdir()
    bridge = tmp_path / "config.json"
    shim = tmp_path / "nats-server"
    shim.write_text('#!/bin/sh\nexec mise x -- nats-server "$@"\n')
    shim.chmod(0o700)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    mise = fake_bin / "mise"
    configured = await asyncio.to_thread(Path(sys.executable).resolve)
    different = tmp_path / "different-nats-server"
    different.symlink_to("/bin/sh")
    mise.write_text(
        "#!/bin/sh\n"
        f'if test "$PWD" = "{first_cwd}"; then printf \'%s\\n\' "{configured}"; '
        f"else printf '%s\\n' \"{different}\"; fi\n"
    )
    mise.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.chdir(first_cwd)
    old = BrokerSupervisor(BusConfig(bridge, 4222, str(shim)))
    old.root.mkdir(parents=True)
    _write_definition(old, manager)
    old.marker.write_text(json.dumps({"manager": manager, "label": old.label}))
    supervisor = BrokerSupervisor(BusConfig(bridge, 4222, str(configured)))

    monkeypatch.chdir(second_cwd)
    await supervisor.stop_if_installed()

    assert not supervisor.marker.exists()
    assert calls


def test_broker_recognizes_its_existing_native_definition(
    tmp_path: Path, native_manager: tuple[str, list[list[str]]]
) -> None:
    manager, calls = native_manager
    supervisor = _native_broker(tmp_path)
    _write_definition(supervisor, manager)
    supervisor._verify_definition(supervisor.label, manager)
    assert calls == []


@pytest.mark.parametrize("native_manager", ["darwin"], indirect=True)
@pytest.mark.parametrize("operation", ["start", "stop_if_installed"])
async def test_broker_refuses_launchd_program_override(
    tmp_path: Path,
    native_manager: tuple[str, list[list[str]]],
    operation: str,
) -> None:
    manager, calls = native_manager
    supervisor = _native_broker(tmp_path)
    definition = _write_definition(supervisor, manager)
    document = plistlib.loads(definition.read_bytes())
    document["Program"] = "/unrelated/server"
    definition.write_bytes(plistlib.dumps(document))
    supervisor.marker.write_text(json.dumps({"manager": manager, "label": supervisor.label}))
    original = definition.read_bytes()
    with pytest.raises(BridgeError, match="ownership"):
        await getattr(supervisor, operation)()
    assert calls == []
    assert supervisor.marker.exists()
    assert definition.read_bytes() == original


@pytest.mark.parametrize("native_manager", ["darwin"], indirect=True)
def test_broker_accepts_matching_explicit_launchd_program(
    tmp_path: Path, native_manager: tuple[str, list[list[str]]]
) -> None:
    manager, calls = native_manager
    supervisor = _native_broker(tmp_path)
    definition = _write_definition(supervisor, manager)
    document = plistlib.loads(definition.read_bytes())
    document["Program"] = supervisor.bus.server_binary
    definition.write_bytes(plistlib.dumps(document))
    supervisor._verify_definition(supervisor.label, manager)
    assert calls == []


@pytest.mark.parametrize("operation", ["start", "stop_if_installed"])
@pytest.mark.parametrize("foreign", ["binary", "config", "bridge", "symlink"])
async def test_broker_refuses_foreign_native_definition_before_manager_calls(
    tmp_path: Path,
    native_manager: tuple[str, list[list[str]]],
    operation: str,
    foreign: str,
) -> None:
    manager, calls = native_manager
    supervisor = _native_broker(tmp_path)
    definition = _write_definition(supervisor, manager)
    supervisor.marker.write_text(json.dumps({"manager": manager, "label": supervisor.label}))
    if foreign == "symlink":
        target = definition.with_suffix(".saved")
        definition.rename(target)
        definition.symlink_to(target)
    else:
        replacements = {
            "binary": ("/nats", "/unrelated/nats"),
            "config": ("nats.conf", "other.conf"),
            "bridge": ("/nats", "/usr/bin/python3 -m answerback_bridge"),
        }
        old, new = replacements[foreign]
        definition.write_text(definition.read_text().replace(old, new))
    original = definition.read_bytes()
    with pytest.raises(BridgeError, match="ownership"):
        await getattr(supervisor, operation)()
    assert calls == []
    assert supervisor.marker.exists()
    assert definition.read_bytes() == original


@pytest.mark.parametrize("label", [None, "local.unrelated.service"])
@pytest.mark.parametrize("operation", ["start", "stop_if_installed"])
async def test_broker_refuses_unknown_marker_labels(
    tmp_path: Path,
    native_manager: tuple[str, list[list[str]]],
    label: str | None,
    operation: str,
) -> None:
    manager, calls = native_manager
    supervisor = _native_broker(tmp_path)
    original = json.dumps({"manager": manager, "label": label})
    supervisor.marker.write_text(original)
    with pytest.raises(BridgeError, match="another configuration"):
        await getattr(supervisor, operation)()
    assert calls == []
    assert supervisor.marker.read_text() == original


# --- T-149: a broker start that is going to refuse makes nothing first ---

# Folders that no definition of the service manager can name, and how the refusal reads:
# BridgeError of systemd_working_directory, ValueError of plistlib.
_REFUSED_FOLDERS: dict[str, tuple[list[str], type[Exception], str]] = {
    "systemd": (["a\nb", "a\rb"], BridgeError, "line feed or a carriage return"),
    "launchd": (["a\x01b"], ValueError, "control characters"),
}


def _tree(top: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(top)): (path.read_bytes() if path.is_file() else None)
        for path in sorted(top.rglob("*"))
    }


def _unreachable_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: Path, started: list[list[str]]
) -> Broker:
    """A broker in `folder` that answers once the stand-in service manager started it.

    No port is opened and none is probed: reachability is what the recorded calls say,
    and the probe for a foreign listener is left out.
    """
    server = tmp_path / "nats-server"
    server.write_text("#!/bin/sh\n")
    server.chmod(0o755)
    broker = Broker(BusConfig(folder / "config.json", 4222, str(server)))

    async def reachable() -> bool:
        return any(
            call[:2] == ["launchctl", "bootstrap"] or call[:3] == ["systemctl", "--user", "start"]
            for call in started
        )

    async def no_listener() -> None:
        return None

    monkeypatch.setattr(broker, "reachable", reachable)
    monkeypatch.setattr(broker, "_refuse_occupied_port", no_listener)
    return broker


async def test_broker_start_refused_for_its_definition_makes_no_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_manager: tuple[str, list[list[str]]],
) -> None:
    """red_at_pretip: Broker.start() made the supervisor's folder and then was refused."""
    manager, calls = native_manager
    names, error, text = _REFUSED_FOLDERS[manager]
    for name in names:
        folder = tmp_path / name
        folder.mkdir()
        broker = _unreachable_broker(tmp_path, monkeypatch, folder, calls)
        before = _tree(tmp_path)

        with pytest.raises(error, match=text) as raised:
            await broker.start()

        assert type(raised.value) is error
        assert calls == []
        assert not broker.supervisor.root.exists()
        assert not broker.config.root.exists()
        assert _tree(tmp_path) == before


async def test_broker_start_makes_the_supervisor_folder_of_an_ordinary_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_manager: tuple[str, list[list[str]]],
) -> None:
    """pins_existing: the folder is made, private, by a start that is not refused."""
    manager, calls = native_manager
    folder = tmp_path / "path with spaces %value $literal"
    folder.mkdir()
    broker = _unreachable_broker(tmp_path, monkeypatch, folder, calls)
    assert not broker.supervisor.root.exists()

    assert await broker.start() == {"broker_running": True, "managed": True}

    root = broker.supervisor.root
    assert root.is_dir() and not root.is_symlink()
    assert root.stat().st_mode & 0o777 == 0o700
    definition = (
        Path.home() / "Library" / "LaunchAgents" / f"{broker.supervisor.label}.plist"
        if manager == "launchd"
        else root / f"{broker.supervisor.label}.service"
    )
    assert definition.is_file()
    assert json.loads(broker.supervisor.marker.read_text())["definition"] == str(definition)


def test_broker_reinstall_needed_is_true_when_the_definition_cannot_be_made(
    tmp_path: Path, native_manager: tuple[str, list[list[str]]]
) -> None:
    """red_at_pretip: the refusal of the definition came out of reinstall_needed()."""
    manager, calls = native_manager
    names, error, text = _REFUSED_FOLDERS[manager]
    for name in names:
        supervisor = BrokerSupervisor(BusConfig(tmp_path / name / "config.json", 4222, "/nats"))
        supervisor.root.mkdir(parents=True)
        with pytest.raises(error, match=text):
            getattr(supervisor, f"{manager}_definition")()
        supervisor.marker.write_text(
            json.dumps(
                {
                    "manager": manager,
                    "label": supervisor.label,
                    "definition": str(supervisor._installed_definition_path(manager)),
                }
            )
        )

        assert supervisor.reinstall_needed() is True

    assert calls == []


@pytest.mark.parametrize("namespace", ["wrong", "matching", "missing"])
@pytest.mark.parametrize("operation", ["probe", "bus"])
async def test_unverified_peer_never_receives_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, namespace: str, operation: str
) -> None:
    """red_at_base: even a copied public namespace is not proof of ownership."""
    await _check_unverified_peer(tmp_path, monkeypatch, namespace, operation, "absent")


@pytest.mark.parametrize(
    "evidence", ["uid", "binary", "config", "socket", "listen", "gone", "denied"]
)
@pytest.mark.parametrize("operation", ["probe", "bus"])
async def test_peer_requires_complete_process_and_socket_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str, operation: str
) -> None:
    """red_at_base: no individual claim can replace complete OS peer evidence."""
    await _check_unverified_peer(tmp_path, monkeypatch, "matching", operation, evidence)


@pytest.mark.parametrize("namespace", ["wrong", "missing", "wrong_type"])
@pytest.mark.parametrize("operation", ["probe", "bus"])
async def test_owned_peer_requires_matching_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, namespace: str, operation: str
) -> None:
    """red_at_base: complete OS evidence cannot replace the INFO namespace check."""
    await _check_unverified_peer(tmp_path, monkeypatch, namespace, operation, "complete")


@pytest.mark.parametrize("namespace", ["array", "null"])
@pytest.mark.parametrize("operation", ["probe", "bus"])
async def test_malformed_info_is_refused_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, namespace: str, operation: str
) -> None:
    """red_at_base: JSON which is not an INFO object must yield a refused connection."""
    await _check_unverified_peer(tmp_path, monkeypatch, namespace, operation, "absent")


@pytest.mark.parametrize(
    ("namespace", "evidence", "message"),
    [
        ("wrong", "complete", "Broker peer identity could not be verified"),
        ("matching", "absent", "Broker peer identity could not be verified"),
        ("matching", "gone", "Broker peer identity could not be verified"),
        ("matching", "denied", "Broker process evidence could not be read on this machine"),
        ("matching", "scan_denied", "Broker process evidence could not be read on this machine"),
        ("matching", "oserror", "Broker process evidence could not be read on this machine"),
        ("matching", "scan_oserror", "Broker process evidence could not be read on this machine"),
    ],
)
async def test_status_explains_broker_identity_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    evidence: str,
    message: str,
) -> None:
    """red_at_review_tip: status distinguishes unavailable evidence from a mismatching peer."""
    await _check_unverified_peer(
        tmp_path, monkeypatch, namespace, "status", evidence, status_error=message
    )


@pytest.mark.parametrize("available", [True, False])
async def test_probe_clears_previous_identity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: bool
) -> None:
    """red_at_review_tip: a later healthy or offline probe must not retain a stale refusal."""
    import nats.errors

    from answerback_bridge.bus import broker as broker_module

    class ConnectedClient:
        async def close(self) -> None:
            pass

    async def connect(config: BusConfig, *, connect_timeout: float) -> ConnectedClient:
        if not available:
            raise nats.errors.NoServersError()
        return ConnectedClient()

    monkeypatch.setattr(broker_module, "connect_broker", connect)
    broker = Broker(BusConfig(tmp_path / "config.json", 4222, "/nats"))
    broker.identity_error = "Broker process evidence could not be read on this machine"
    assert await broker.reachable() is available
    assert broker.identity_error is None


async def _check_unverified_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    operation: str,
    evidence: str,
    *,
    status_error: str | None = None,
) -> None:
    import argparse
    import asyncio
    import os
    from collections.abc import Iterator
    from types import SimpleNamespace

    import nats.errors
    import psutil
    from bus_helpers import unused_server_binary
    from nats.aio.client import Client

    from answerback_bridge.bus import cli
    from answerback_bridge.bus.jetstream import JetStreamBus

    captured = bytearray()
    finished = asyncio.Event()
    connections: list[asyncio.StreamWriter] = []
    config: BusConfig

    class PeerProcess:
        def uids(self) -> SimpleNamespace:
            return SimpleNamespace(effective=os.getuid() + (evidence == "uid"))

        def exe(self) -> str:
            return __file__ if evidence == "binary" else config.server_binary

        def cmdline(self) -> list[str]:
            return [
                config.server_binary,
                "-c",
                str(config.root / "other.conf" if evidence == "config" else config.broker_config),
            ]

        def net_connections(self, *, kind: str) -> list[SimpleNamespace]:
            assert kind == "tcp4"
            if evidence == "denied":
                raise psutil.AccessDenied()
            if evidence == "oserror":
                raise OSError("Fixture process diagnostic must not appear in status")
            return [connection]

        def is_running(self) -> bool:
            return evidence != "gone"

    connection: SimpleNamespace

    async def listener(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connection
        connections.append(writer)
        connection = SimpleNamespace(
            status=psutil.CONN_LISTEN if evidence == "listen" else psutil.CONN_ESTABLISHED,
            laddr=writer.get_extra_info("sockname"),
            raddr=("127.0.0.1", 0) if evidence == "socket" else writer.get_extra_info("peername"),
        )
        info = {"server_name": config.namespace if namespace == "matching" else "unrelated"}
        if namespace == "missing":
            info = {}
        elif namespace == "wrong_type":
            info = {"server_name": [config.namespace]}
        elif namespace == "array":
            info = []
        elif namespace == "null":
            info = None
        writer.write(b"INFO " + json.dumps(info).encode() + b"\r\n")
        await writer.drain()
        try:
            while line := await reader.readline():
                captured.extend(line)
                if line == b"PING\r\n":
                    writer.write(b"PONG\r\n")
                    await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    def stop_after_authentication(*args: object, **kwargs: object) -> None:
        raise nats.errors.Error("Fixture stops after authentication")

    def processes() -> Iterator[PeerProcess]:
        if evidence == "scan_denied":
            raise psutil.AccessDenied()
        if evidence == "scan_oserror":
            raise OSError("Fixture process table diagnostic must not appear in status")
        if evidence != "absent":
            yield PeerProcess()

    async def remote_status(*args: object, **kwargs: object) -> dict:
        return {"coordinator": {"connected": False}}

    # Supply only fixture process evidence, never inspect a real process.
    monkeypatch.setattr(psutil, "process_iter", processes)
    monkeypatch.setattr(Client, "jetstream", stop_after_authentication)
    monkeypatch.setattr(cli.RemoteBridge, "call", remote_status)
    server = await asyncio.start_server(listener, "127.0.0.1", 0)
    async with server:
        config = BusConfig.create(
            tmp_path / "config.json",
            port=server.sockets[0].getsockname()[1],
            server_binary=unused_server_binary(tmp_path),
        )
        original_token = BusConfig.token
        dummy_credential = config.token()
        credential_reads = 0

        def read_token(self: BusConfig) -> str:
            nonlocal credential_reads
            credential_reads += 1
            return original_token(self)

        # CLI status validates the token file while loading BusConfig, before probing.
        if operation != "status":
            monkeypatch.setattr(BusConfig, "token", read_token)
        try:
            if operation == "probe":
                reachable = await Broker(config).reachable()
            elif operation == "status":
                status = await cli.dispatch(
                    argparse.Namespace(config=config.bridge_path, operation="status")
                )
            else:
                with pytest.raises(nats.errors.Error):
                    async with JetStreamBus(config):
                        pytest.fail("An unverified peer must not be used")
        finally:
            # BASE does not close a partially connected client on malformed INFO.
            # The fixture must release its own sockets even when that assertion fails.
            for writer in connections:
                writer.close()
                await writer.wait_closed()
            await asyncio.wait_for(finished.wait(), 2)
    if operation != "status":
        assert credential_reads == 0
    assert dummy_credential.encode() not in captured
    assert captured == b""
    if operation == "probe":
        assert not reachable
    elif operation == "status":
        assert status["broker_reachable"] is False
        assert status["broker_identity_error"] == status_error


async def test_launchd_clean_exit_reports_why_the_broker_stayed_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-fix plist turns SIGINT's clean exit into a permanent outage."""
    from answerback_bridge.bus import broker as broker_module

    config = BusConfig(tmp_path / "config.json", 4222, "/invented/nats-server")
    supervisor = BrokerSupervisor(config)
    supervisor.root.mkdir(parents=True)
    definition = tmp_path / "invented-broker.plist"
    document = plistlib.loads(supervisor.launchd_definition())
    document["KeepAlive"] = {"SuccessfulExit": False}
    definition.write_bytes(plistlib.dumps(document))
    supervisor.marker.write_text(
        json.dumps(
            {
                "manager": "launchd",
                "label": supervisor.label,
                "definition": str(definition),
            }
        )
    )

    async def manager_status(args: list[str], *, max_seconds: float = 30) -> Output:
        assert args == ["launchctl", "print", f"gui/{__import__('os').getuid()}/{supervisor.label}"]
        return Output(
            "state = not running\nlast exit code = 0\njob state = exited\n",
            "",
        )

    monkeypatch.setattr(broker_module, "run", manager_status)
    assert await supervisor.failure_reason() == (
        "broker not running (launchd: exited 0, not restarted)"
    )


def _supervised_broker(
    tmp_path: Path, manager: str, *, restarts_cleanly: bool = True
) -> BrokerSupervisor:
    config = BusConfig(tmp_path / "config.json", 4222, "/invented/nats-server")
    supervisor = BrokerSupervisor(config)
    supervisor.root.mkdir(parents=True)
    suffix = ".plist" if manager == "launchd" else ".service"
    definition = tmp_path / f"invented-broker{suffix}"
    if manager == "launchd":
        document = plistlib.loads(supervisor.launchd_definition())
        if not restarts_cleanly:
            document["KeepAlive"] = {"SuccessfulExit": False}
        definition.write_bytes(plistlib.dumps(document))
    else:
        text = supervisor.systemd_definition()
        if not restarts_cleanly:
            text = text.replace("Restart=always", "Restart=on-failure")
        definition.write_text(text)
    supervisor.marker.write_text(
        json.dumps({"manager": manager, "label": supervisor.label, "definition": str(definition)})
    )
    return supervisor


@pytest.mark.parametrize(
    ("manager", "output", "expected"),
    [
        (
            "launchd",
            "\tstate = running\n\tlast exit code = 0\n",
            "broker unreachable (launchd: running)",
        ),
        (
            "systemd",
            "LoadState=loaded\nActiveState=active\nSubState=running\n"
            "Result=success\nExecMainStatus=0\n",
            "broker unreachable (systemd: running)",
        ),
    ],
)
async def test_running_supervisor_reports_broker_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager: str,
    output: str,
    expected: str,
) -> None:
    from answerback_bridge.bus import broker as broker_module

    supervisor = _supervised_broker(tmp_path, manager)

    async def manager_status(args: list[str], *, max_seconds: float = 30) -> Output:
        return Output(output, "")

    monkeypatch.setattr(broker_module, "run", manager_status)
    assert await supervisor.failure_reason() == expected


async def test_launchd_status_uses_top_level_state_and_ignores_non_numeric_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.bus import broker as broker_module

    supervisor = _supervised_broker(tmp_path, "launchd")
    fixture = await asyncio.to_thread(
        (Path(__file__).parent / "fixtures" / "launchctl-print-t182.txt").read_text
    )
    fixture = fixture.replace("\tlast exit code = 0", "\tlast exit code = (never exited)")
    fixture = fixture.replace("\tjob state = exited\n", "")

    async def manager_status(args: list[str], *, max_seconds: float = 30) -> Output:
        return Output(fixture, "")

    monkeypatch.setattr(broker_module, "run", manager_status)
    assert await supervisor.failure_reason() == "broker not running (launchd: not running)"


@pytest.mark.parametrize(
    ("manager", "restarts_cleanly", "output", "returncode", "expected"),
    [
        ("launchd", True, "", 113, "broker not running (launchd: not loaded)"),
        ("launchd", True, "", 1, "broker not running (launchd status unavailable)"),
        (
            "launchd",
            True,
            "\tstate = not running\n\tlast exit code = 7\n",
            None,
            "broker not running (launchd: exited 7, restart pending)",
        ),
        (
            "systemd",
            False,
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
            "Result=success\nExecMainStatus=0\n",
            None,
            "broker not running (systemd: exited 0, not restarted)",
        ),
        (
            "systemd",
            True,
            "LoadState=loaded\nActiveState=activating\nSubState=auto-restart\n"
            "Result=exit-code\nExecMainStatus=7\n",
            None,
            "broker not running (systemd: exited 7, restart pending)",
        ),
        (
            "systemd",
            True,
            "LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=signal\n",
            None,
            "broker not running (systemd: failed)",
        ),
        ("systemd", True, "", 1, "broker not running (systemd status unavailable)"),
    ],
)
async def test_failure_reason_covers_fixed_manager_status_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager: str,
    restarts_cleanly: bool,
    output: str,
    returncode: int | None,
    expected: str,
) -> None:
    from answerback_bridge.bus import broker as broker_module

    supervisor = _supervised_broker(tmp_path, manager, restarts_cleanly=restarts_cleanly)

    async def manager_status(args: list[str], *, max_seconds: float = 30) -> Output:
        if returncode is not None:
            raise CommandError(returncode, "fixture manager diagnostic")
        return Output(output, "")

    monkeypatch.setattr(broker_module, "run", manager_status)
    assert await supervisor.failure_reason() == expected


async def test_failure_reason_handles_a_non_object_marker(tmp_path: Path) -> None:
    supervisor = BrokerSupervisor(BusConfig(tmp_path / "config.json", 4222, "/nats"))
    supervisor.root.mkdir(parents=True)
    supervisor.marker.write_text("[]")

    assert await supervisor.failure_reason() == (
        "broker not running (supervisor status unavailable)"
    )


async def test_broker_failure_reason_preserves_identity_error(tmp_path: Path) -> None:
    broker = Broker(BusConfig(tmp_path / "config.json", 4222, "/nats"))
    broker.identity_error = "Broker peer identity could not be verified"

    assert await broker.failure_reason() == "Broker peer identity could not be verified"


async def test_cli_status_exposes_the_broker_supervisor_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    import nats.errors
    from bus_helpers import unused_server_binary

    from answerback_bridge.bus import broker as broker_module
    from answerback_bridge.bus import cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = BusConfig.create(
        tmp_path / "config.json", server_binary=unused_server_binary(tmp_path), port=4222
    )
    supervisor = BrokerSupervisor(config)
    supervisor.root.mkdir(parents=True)
    definition = home / "Library" / "LaunchAgents" / f"{supervisor.label}.plist"
    definition.parent.mkdir(parents=True)
    document = plistlib.loads(supervisor.launchd_definition())
    document["KeepAlive"] = {"SuccessfulExit": False}
    definition.write_bytes(plistlib.dumps(document))
    supervisor.marker.write_text(
        json.dumps(
            {
                "manager": "launchd",
                "label": supervisor.label,
                "definition": str(definition),
            }
        )
    )

    async def manager_status(args: list[str], *, max_seconds: float = 30) -> Output:
        return Output("state = not running\nlast exit code = 0\njob state = exited\n", "")

    async def remote_status(*args: object, **kwargs: object) -> dict:
        return {"coordinator": {"connected": False, "last_error": None}}

    async def unreachable(config: BusConfig, *, connect_timeout: float) -> None:
        raise nats.errors.NoServersError()

    monkeypatch.setattr(broker_module, "run", manager_status)
    monkeypatch.setattr(broker_module, "connect_broker", unreachable)
    monkeypatch.setattr(cli.RemoteBridge, "call", remote_status)
    status = await cli.dispatch(argparse.Namespace(config=config.bridge_path, operation="status"))
    assert status["broker_reachable"] is False
    assert status["broker_error"] == "broker not running (launchd: exited 0, not restarted)"


async def test_cli_status_probes_a_legacy_launcher_before_attaching_its_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from bus_helpers import unused_server_binary

    from answerback_bridge.bus import cli

    config = BusConfig.create(
        tmp_path / "config.json", server_binary=unused_server_binary(tmp_path), port=4222
    )
    shim = tmp_path / "nats-server"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o700)
    saved = json.loads(config.path.read_text())
    saved["server_binary"] = str(shim)
    config.path.write_text(json.dumps(saved))

    class ReachableBroker:
        identity_error = None
        probes = 0

        def __init__(self, config: BusConfig) -> None:
            pass

        async def reachable(self) -> bool:
            type(self).probes += 1
            return True

        async def failure_reason(self) -> str:
            return "fixture must not replace the launcher diagnosis"

    async def remote_status(*args: object, **kwargs: object) -> dict:
        return {"coordinator": {"connected": False, "last_error": None}}

    monkeypatch.setattr(cli, "broker_class", lambda: ReachableBroker)
    monkeypatch.setattr(cli.RemoteBridge, "call", remote_status)

    status = await cli.dispatch(argparse.Namespace(config=config.bridge_path, operation="status"))

    assert ReachableBroker.probes == 1
    assert status["broker_reachable"] is True
    assert status["broker_error"] == (
        "configured executable is a launcher shim; run `agent-bus configure` again"
    )
    assert "broker_identity_error" not in status
