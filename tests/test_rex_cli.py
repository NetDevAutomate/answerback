import uuid

import pytest

from answerback_bridge.cli import dispatch, parser
from answerback_bridge.config import Config
from answerback_bridge.process import BridgeError

SESSION = "session:" + str(uuid.uuid4())
BLOCK = "block:" + str(uuid.uuid4())


@pytest.mark.parametrize(
    ("argv", "method", "params"),
    [
        (
            ["rex-configure", "--binary", "./rex", "--server", "unix:///tmp/rex.sock"],
            "rex_configure",
            {"binary": "RELATIVE", "server": "unix:///tmp/rex.sock"},
        ),
        (
            ["read-rex", "--session", SESSION, "--block", BLOCK],
            "rex_read",
            {"session_id": SESSION, "block_id": BLOCK, "max_bytes": 16384},
        ),
        (
            ["rex-enroll", "agent", "--session", SESSION, "--block", BLOCK, "--request-id", "e"],
            "rex_enroll",
            {"name": "agent", "session_id": SESSION, "block_id": BLOCK, "request_id": "e"},
        ),
        (
            ["rex-receive", "rex:peer", "--wait", "5"],
            "rex_receive",
            {"peer_id": "rex:peer", "wait_seconds": 5},
        ),
        (["rex-ack", "rex:peer", "task"], "rex_ack", {"peer_id": "rex:peer", "task_id": "task"}),
        (
            ["rex-complete", "rex:peer", "task", "--result", "done"],
            "rex_complete",
            {"peer_id": "rex:peer", "task_id": "task", "result": "done"},
        ),
        (["rex-retire", "rex:peer"], "rex_retire", {"peer_id": "rex:peer"}),
        (["find", "--provider", "rex"], "peer_search", {"query": "", "provider": "rex"}),
        (
            ["send", "rex:peer", "--provider", "rex", "--message", "work", "--request-id", "r"],
            "peer_submit",
            {
                "selector": "rex:peer",
                "provider": "rex",
                "message": "work",
                "request_id": "r",
                "binding": None,
            },
        ),
    ],
)
async def test_dispatch(argv, method, params, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    if params.get("binary") == "RELATIVE":
        params = {**params, "binary": str(tmp_path / "rex")}

    async def call(self, actual, values):
        assert (actual, values) == (method, params)
        return {"ok": True}

    monkeypatch.setattr("answerback_bridge.cli.RemoteBridge.call", call)
    assert await dispatch(parser().parse_args(argv)) == {"ok": True}


async def test_here_uses_only_caller_environment(monkeypatch):
    monkeypatch.setenv("REX_SESSION", SESSION)
    monkeypatch.setenv("REX_BLOCK", BLOCK)

    async def call(self, method, params):
        assert params["session_id"] == SESSION
        assert params["block_id"] == BLOCK
        return params

    monkeypatch.setattr("answerback_bridge.cli.RemoteBridge.call", call)
    await dispatch(parser().parse_args(["rex-enroll", "a", "--here", "--request-id", "r"]))


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--session", SESSION],
        ["--here", "--block", BLOCK],
        ["--here", "--session", SESSION],
        ["--here"],
    ],
)
async def test_missing_and_conflicting_ids(extra, monkeypatch):
    monkeypatch.delenv("REX_SESSION", raising=False)
    monkeypatch.delenv("REX_BLOCK", raising=False)
    with pytest.raises((ValueError, BridgeError)):
        await dispatch(parser().parse_args(["rex-enroll", "a", "--request-id", "r", *extra]))


@pytest.mark.parametrize("failed", [False, True])
async def test_initial_rex_probes_before_saving(tmp_path, monkeypatch, failed):
    path = tmp_path / "config.json"
    executable = tmp_path / "rex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)

    async def probe(self):
        assert not path.exists()
        assert self.binary == str(tmp_path / "rex")
        if failed:
            raise BridgeError("probe failed")
        return {"endpoint": "unix:///tmp/rex.sock", "server_host_id": "host"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("answerback_bridge.rex.Rex.probe", probe)
    args = parser().parse_args(
        ["--config", str(path), "configure", "--runtime", "rex", "--rex", "./rex", "--no-codex"]
    )
    if failed:
        with pytest.raises(BridgeError):
            await dispatch(args)
        assert not path.exists()
    else:
        await dispatch(args)
        config = Config.load(path)
        assert config.rex_enabled and not config.hc_enabled and not config.codex_enabled
        assert config.herdr_session is None
        assert config.rex_server == "unix:///tmp/rex.sock"
        assert config.rex_host_id == "host"


async def test_initial_rex_pins_path_name_before_probe(tmp_path, monkeypatch):
    """red_at_base: initial CLI configuration also saves the chosen absolute binary."""
    executable = tmp_path / "rex-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    path = tmp_path / "config.json"
    probed = []

    async def probe(adapter):
        probed.append(adapter.binary)
        return {"endpoint": "unix:///tmp/rex.sock", "server_host_id": "host"}

    monkeypatch.setattr("answerback_bridge.rex.Rex.probe", probe)
    await dispatch(
        parser().parse_args(
            [
                "--config",
                str(path),
                "configure",
                "--runtime",
                "rex",
                "--rex",
                "rex-fixture",
                "--no-codex",
            ]
        )
    )
    assert probed == [str(executable)]
    assert Config.load(path).rex_binary == str(executable)


async def test_rex_rejects_legacy_session(tmp_path):
    args = parser().parse_args(
        [
            "--config",
            str(tmp_path / "c.json"),
            "configure",
            "--runtime",
            "rex",
            "--session",
            "legacy",
        ]
    )
    with pytest.raises(ValueError, match="session"):
        await dispatch(args)


@pytest.mark.parametrize("content", ["résultat", "x" * 200001])
async def test_result_file_bound(tmp_path, monkeypatch, content):
    path = tmp_path / "result.txt"
    path.write_text(content)

    async def call(self, method, params):
        assert params["result"] == content
        return {}

    monkeypatch.setattr("answerback_bridge.cli.RemoteBridge.call", call)
    args = parser().parse_args(["rex-complete", "rex:p", "t", "--result-file", str(path)])
    if len(content) > 200000:
        with pytest.raises(ValueError):
            await dispatch(args)
    else:
        await dispatch(args)


@pytest.mark.parametrize("runtime", ["hc", "tui"])
async def test_legacy_initialization(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr("answerback_bridge.cli.shutil.which", lambda value: "/bin/" + value)
    path = tmp_path / "config.json"
    extra = ["--session", "legacy"] if runtime == "tui" else []
    await dispatch(
        parser().parse_args(["--config", str(path), "configure", "--runtime", runtime, *extra])
    )
    config = Config.load(path)
    assert config.hc_enabled == (runtime == "hc")
    assert config.herdr_session == ("legacy" if runtime == "tui" else None)
    assert not config.rex_enabled


async def test_install_preserves_saved_rex_fields(tmp_path, monkeypatch):
    from answerback_bridge.process import Output

    path = tmp_path / "config.json"
    original = Config(
        "legacy",
        rex_enabled=True,
        rex_binary="/tmp/pinned-rex",
        rex_server="unix:///tmp/rex.sock",
        rex_host_id="host",
    )
    original.save(path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "answerback").touch()

    async def status(self):
        return {"running": False}

    async def check(self):
        return {}

    async def run(argv, **kwargs):
        if argv[-2:] == ["queue", "--help"]:
            return Output("--thread", "")
        if argv[-3:] == ["tool", "dir", "--bin"]:
            return Output(str(bin_dir), "")
        return Output("", "")

    async def start(config_path):
        assert config_path == path
        return {"fleet_writer": {"installed": False}}

    monkeypatch.setattr("answerback_bridge.cli.RemoteBridge.status", status)
    monkeypatch.setattr("answerback_bridge.cli.Herdr.check", check)
    monkeypatch.setattr("answerback_bridge.cli.run", run)
    monkeypatch.setattr("answerback_bridge.cli.executable_path", lambda value: "/bin/" + value)
    monkeypatch.setattr("answerback_bridge.lifecycle.start", start)
    await dispatch(
        parser().parse_args(
            [
                "--config",
                str(path),
                "install",
                "--source",
                str(tmp_path),
                "--session",
                "legacy",
                "--codex-home",
                str(tmp_path / "codex"),
            ]
        )
    )
    saved = Config.load(path)
    for field in ("rex_enabled", "rex_binary", "rex_server", "rex_host_id"):
        assert getattr(saved, field) == getattr(original, field)


@pytest.mark.parametrize("session,block", [("short", BLOCK), (SESSION, "block:short")])
async def test_here_rejects_non_full_ids(session, block, monkeypatch):
    monkeypatch.setenv("REX_SESSION", session)
    monkeypatch.setenv("REX_BLOCK", block)
    with pytest.raises(BridgeError):
        await dispatch(parser().parse_args(["rex-enroll", "a", "--here", "--request-id", "r"]))


def test_enrollment_requires_request_id():
    with pytest.raises(SystemExit):
        parser().parse_args(["rex-enroll", "a", "--here"])


def test_help_and_legacy_alias_contract():
    help_text = parser().format_help()
    assert "rex-enroll" in help_text and "read-rex" in help_text
    assert parser().parse_args(["find", "--provider", "desktop"]).provider == "desktop"
    assert parser().parse_args(["configure", "--session", "legacy"]).session == "legacy"
