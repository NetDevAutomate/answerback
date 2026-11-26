"""new_feature: CLI output and exit behavior, with no live Core or bridge."""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge import cli
from answerback_bridge.placement import cli as capacity_cli
from answerback_bridge.placement import probe

GOOD = (
    "os=Linux\ncores=10\nload=1 2 3\nmemtotal_kb=1000\nmemavailable_kb=900\n"
    "largest_kb=100\nlargest_name=python\nprocs=12\nprobe_end=1\n"
)


@pytest.fixture
def command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Callable[[list[str]], tuple[int, str, str]], dict[str, str], list[list[str]]]:
    outputs = {
        "local": GOOD,
        "ok": GOOD,
        "busy": GOOD.replace("1 2 3", "7 2 3"),
        "full": GOOD.replace("1 2 3", "10 2 3"),
        "unknown": "unparsed",
    }
    calls: list[list[str]] = []

    async def runner(argv: list[str], *, env: dict[str, str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, outputs["local" if argv[0] == "/bin/sh" else argv[3]]

    monkeypatch.setattr(probe, "run_probe", runner)
    monkeypatch.setattr(cli, "default_config_path", lambda: tmp_path / "absent.json")

    def invoke(arguments: list[str]) -> tuple[int, str, str]:
        monkeypatch.setattr(sys, "argv", ["answerback", "capacity", *arguments])
        code = 0
        try:
            cli.main()
        except SystemExit as error:
            code = error.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke, outputs, calls


def test_new_feature_cli_json_mixed_states(command: Any) -> None:
    invoke, _, calls = command
    code, stdout, stderr = invoke(
        [
            "--machine",
            "ok",
            "--machine",
            "busy",
            "--machine",
            "full",
            "--machine",
            "unknown",
            "--json",
        ]
    )
    assert code == 0 and stderr == ""
    data = json.loads(stdout)
    assert set(data) == {"taken_at", "coordinator", "machines", "ranked_for_heavy_work", "advice"}
    assert data["coordinator"] == "local"
    assert isinstance(data["taken_at"], (int, float))
    assert [(item["machine"], item["state"]) for item in data["machines"]] == [
        ("local", "ok"),
        ("ok", "ok"),
        ("busy", "busy"),
        ("full", "full"),
        ("unknown", "unknown"),
    ]
    assert data["ranked_for_heavy_work"] == ["ok"]
    assert len(calls) == 5
    assert data["machines"][0]["reading"]["cores"] == 10
    assert data["machines"][0]["load_per_core"] == 0.2
    assert set(data["machines"][0]) == {
        "machine",
        "state",
        "load_per_core",
        "signals",
        "reasons",
        "reading",
    }


def test_new_feature_cli_text_and_advice(command: Any) -> None:
    invoke, _, _ = command
    code, stdout, _ = invoke(["--machine", "busy", "--machine", "ok", "--advise"])
    assert code == 0
    lines = stdout.splitlines()
    assert lines[0].startswith("local ok")
    assert "0.200" in lines[0] and "1.00/2.00/3.00" in lines[0] and "cores=10" in lines[0]
    assert "available 90.00%" in lines[0]
    assert "python" in lines[0] and "100 kB" in lines[0] and "processes=12" in lines[0]
    assert lines[1].startswith("busy busy") and "reasons:" in lines[1]
    assert "nothing heavy" in stdout and "finish" in stdout
    assert "Heavy-work targets: ok" in stdout


@pytest.mark.parametrize(
    ("require", "machine", "expected"),
    [
        ("ok", "ok", 0),
        ("ok", "busy", 1),
        ("ok", "full", 1),
        ("ok", "unknown", 1),
        ("busy", "ok", 0),
        ("busy", "busy", 0),
        ("busy", "full", 1),
        ("busy", "unknown", 1),
    ],
)
def test_new_feature_require_uses_existing_exit_mapper(
    command: Any,
    require: str,
    machine: str,
    expected: int,
) -> None:
    invoke, outputs, _ = command
    outputs["local"] = outputs["full"]
    code, stdout, _ = invoke(["--machine", machine, "--require", require, "--json"])
    assert code == expected
    assert json.loads(stdout)["machines"][1]["state"] == machine


def test_new_feature_require_defaults_to_local_and_checks_all_targets(command: Any) -> None:
    invoke, outputs, _ = command
    outputs["local"] = outputs["busy"]
    assert invoke(["--require", "ok"])[0] == 1
    assert invoke(["--require", "busy"])[0] == 0
    assert invoke(["--machine", "ok", "--machine", "full", "--require", "ok"])[0] == 1
    assert invoke(["--machine", "local", "--machine", "ok", "--require", "ok"])[0] == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["--machine=-oProxyCommand=x"],
        ["--machine", "a;b"],
        ["--machine", "a b"],
        ["--machine", "../x"],
        ["--machine", "a/b"],
        ["--machine", ""],
        ["--machine", "x" * 64],
        ["--timeout", "0"],
        ["--timeout", "31"],
        ["--timeout", "nan"],
        ["--timeout", "inf"],
        ["--timeout", "word"],
        ["--require", "full"],
        [arg for i in range(16) for arg in ("--machine", f"node{i}")],
    ],
)
def test_new_feature_usage_errors_start_nothing(command: Any, arguments: list[str]) -> None:
    invoke, _, calls = command
    code, stdout, stderr = invoke(arguments)
    assert code == 2 and stdout == ""
    assert "error:" in stderr
    assert calls == []


def test_new_feature_hostile_process_name_is_safe_in_both_formats(command: Any) -> None:
    invoke, outputs, _ = command
    outputs["ok"] = GOOD.replace("largest_name=python", 'largest_name=\x1b[31m"bad"')
    code, stdout, _ = invoke(["--machine", "ok", "--json"])
    assert code == 0
    assert json.loads(stdout)["machines"][1]["reading"]["largest_process_name"] == "?"
    _, stdout, _ = invoke(["--machine", "ok"])
    assert all(char == "\n" or " " <= char <= "~" for char in stdout)


def test_new_feature_configured_hc_path_timeout_and_duplicates(
    command: Any, tmp_path: Path
) -> None:
    invoke, _, calls = command
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"hc_binary": "/fixture/configured-hc", "hc_session": "fixture"}))
    code, _, _ = invoke(
        [
            "--config",
            str(config),
            "--machine",
            "ok",
            "--machine",
            "ok",
            "--machine",
            "local",
            "--timeout",
            "1.5",
        ]
    )
    assert code == 0
    assert len(calls) == 2
    assert calls[1] == [
        "/fixture/configured-hc",
        "exec",
        "--on",
        "ok",
        "--timeout",
        "2",
        "sh",
        "-c",
        probe.PROBE_SCRIPT,
    ]


def test_new_feature_local_needs_no_config_and_empty_rank_queues(
    command: Any, tmp_path: Path
) -> None:
    invoke, _, calls = command
    broken = tmp_path / "broken.json"
    broken.write_text("not json")
    code, stdout, _ = invoke(["--config", str(broken), "--advise"])
    assert code == 0
    assert "queue" in stdout
    assert calls == [["/bin/sh", "-c", probe.PROBE_SCRIPT]]


def test_new_feature_help_lists_capacity(command: Any) -> None:
    invoke, _, calls = command
    code, stdout, _ = invoke(["--help"])
    assert code == 0 and "--require {ok,busy}" in stdout and "--machine" in stdout
    assert "capacity" in cli.parser().format_help()
    assert calls == []


@pytest.mark.parametrize("arguments", [["--machine=-x"], ["--timeout", "0.5"]])
def test_pins_existing_exact_usage_boundaries(command: Any, arguments: list[str]) -> None:
    invoke, _, calls = command
    code, stdout, stderr = invoke(arguments)
    assert code == 2 and stdout == "" and "error:" in stderr
    assert calls == []


def test_pins_existing_timeout_default(command: Any) -> None:
    invoke, _, calls = command
    assert cli.parser().parse_args(["capacity"]).timeout == 10
    assert invoke(["--machine", "ok"])[0] == 0
    assert calls[1][4:6] == ["--timeout", "10"]


async def test_red_at_pretip_capacity_dispatch_returns_after_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []

    async def run_cli(args: argparse.Namespace) -> None:
        called.append(args.command)

    def bridge(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Capacity must not fall through into bridge dispatch")

    monkeypatch.setattr(capacity_cli, "run_cli", run_cli)
    monkeypatch.setattr(cli, "RemoteBridge", bridge)
    assert await cli.dispatch(cli.parser().parse_args(["capacity"])) is None
    assert called == ["capacity"]


def test_red_at_pretip_help_explains_machine_and_timeout_limits(command: Any) -> None:
    invoke, _, calls = command
    code, stdout, _ = invoke(["--help"])
    help_text = " ".join(stdout.split())
    assert code == 0
    assert "16 including local" in help_text
    assert "1-30 seconds" in help_text and "default: 10" in help_text
    assert calls == []


@pytest.mark.parametrize("name", ["x=1", "x); processes=1"])
def test_red_at_pretip_name_cannot_add_text_output_fields(command: Any, name: str) -> None:
    invoke, outputs, _ = command
    outputs["local"] = GOOD.replace("largest_name=python", f"largest_name={name}")
    code, stdout, _ = invoke([])
    assert code == 0 and len(stdout.splitlines()) == 1
    assert "largest=? (100 kB)" in stdout and stdout.count("processes=") == 1
    assert "processes=12" in stdout
    code, stdout, _ = invoke(["--json"])
    assert code == 0
    assert json.loads(stdout)["machines"][0]["reading"]["largest_process_name"] == "?"
