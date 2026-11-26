import json
from pathlib import Path
from uuid import uuid4

import pytest
from bus_helpers import unused_server_binary

from answerback_bridge.actors.api import load_binding
from answerback_bridge.actors.cli import dispatch
from answerback_bridge.actors.model import ActorError
from answerback_bridge.bus.config import BusConfig
from answerback_bridge.cli import parser


def args(path: Path, *values: str):
    return parser().parse_args(["--config", str(path), "actors", *values])


def test_owner_cli_enrollment_never_prints_or_overwrites_capability(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    BusConfig.create(config, server_binary=unused_server_binary(tmp_path))
    run = dispatch(args(config, "create", "--project", str(tmp_path), "--role", "worker"))
    binding = tmp_path / "worker.json"
    command = args(
        config,
        "stage",
        run["run_id"],
        "--role",
        "worker",
        "--harness",
        "test",
        "--binding-file",
        str(binding),
    )
    actor = dispatch(command)
    raw = binding.read_bytes()
    auth = load_binding(binding)
    assert auth.credential not in json.dumps(actor)
    with pytest.raises(FileExistsError):
        dispatch(command)
    assert binding.read_bytes() == raw
    native = tmp_path / "native.json"
    native.write_text(json.dumps({"session_id": str(uuid4())}))
    native.chmod(0o600)
    dispatch(
        args(
            config,
            "activate",
            run["run_id"],
            actor["actor_id"],
            "--native-binding-file",
            str(native),
        )
    )
    assert (
        dispatch(args(config, "seal", run["run_id"], "--owner-token", run["owner_token"]))["state"]
        == "sealed"
    )
    result = dispatch(
        args(
            config,
            "finish",
            run["run_id"],
            "--reason",
            "Test ended",
            "--owner-token",
            run["owner_token"],
        )
    )
    assert result["state"] == "finished"
    assert "success" not in result


def test_owner_read_does_not_create_actor_store(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    BusConfig.create(config, server_binary=unused_server_binary(tmp_path))
    with pytest.raises(ActorError):
        dispatch(args(config, "status", str(uuid4()), "--owner-token", "0" * 64))
    assert not (tmp_path / "bus/actors.sqlite").exists()


def test_run6_owner_token_file_is_the_private_proof_and_argv_is_deprecated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """red_at_base (run-6 hardening h4): the owner token could only be passed on argv."""
    config = tmp_path / "config.json"
    BusConfig.create(config, server_binary=unused_server_binary(tmp_path))
    run = dispatch(args(config, "create", "--project", str(tmp_path), "--role", "worker"))
    capsys.readouterr()
    assert dispatch(args(config, "status", run["run_id"], "--owner-token", run["owner_token"]))
    warning = capsys.readouterr().err
    assert "--owner-token is deprecated" in warning and "--owner-token-file" in warning

    token = tmp_path / "owner-token"
    token.write_text(run["owner_token"] + "\n")
    token.chmod(0o600)
    status = dispatch(args(config, "status", run["run_id"], "--owner-token-file", str(token)))
    assert status["run_id"] == run["run_id"]
    assert run["owner_token"] not in capsys.readouterr().err
    token.chmod(0o644)
    with pytest.raises(ActorError) as refused:
        dispatch(args(config, "status", run["run_id"], "--owner-token-file", str(token)))
    assert refused.value.code == "auth_invalid"
    token.chmod(0o600)
    link = tmp_path / "linked-token"
    link.symlink_to(token)
    with pytest.raises(ActorError):
        dispatch(args(config, "status", run["run_id"], "--owner-token-file", str(link)))
    with pytest.raises(ActorError):
        dispatch(args(config, "status", run["run_id"], "--owner-token-file", str(tmp_path / "x")))
    with pytest.raises(SystemExit):
        args(
            config,
            "status",
            run["run_id"],
            "--owner-token",
            run["owner_token"],
            "--owner-token-file",
            str(token),
        )
