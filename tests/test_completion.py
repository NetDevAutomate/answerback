import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_herdr_bridge.cli import parser


def complete(tmp_path: Path, line: str) -> list[str]:
    output = tmp_path / "completions"
    env = {
        **os.environ,
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\v",
        "_ARGCOMPLETE_STDOUT_FILENAME": str(output),
    }
    subprocess.run(
        [sys.executable, "-m", "codex_herdr_bridge"],
        env=env,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return output.read_text().split("\v") if output.exists() else []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("herdr-bridge wor", "workspace"),
        ("herdr-bridge workspace ", "add"),
        ("herdr-bridge workspace add demo --c", "--cwd"),
        ("herdr-bridge find --provider ", "desktop"),
        ("herdr-bridge config ", "archive"),
        ("codex-herdr cre", "crew-configure"),
        ("answerback bus ", "monitor"),
    ],
)
def test_completes_without_contacting_running_service(tmp_path: Path, line: str, expected: str):
    assert expected in [word.rstrip() for word in complete(tmp_path, line)]


def test_completion_handles_paths_with_spaces(tmp_path: Path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    suggestions = complete(tmp_path, f"herdr-bridge workspace add demo --cwd {tmp_path}/pro")
    assert any("project" in text and "spaces" in text for text in suggestions)


@pytest.mark.parametrize("shell", ["zsh", "bash", "fish"])
def test_prints_shell_registration_script(shell: str):
    result = subprocess.run(
        [sys.executable, "-m", "codex_herdr_bridge", "completion", "--shell", shell],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "herdr-bridge" in result.stdout and "codex-herdr" in result.stdout
    assert not result.stdout.startswith("{")


def test_config_is_accepted_before_or_after_subcommands(tmp_path: Path):
    path = tmp_path / "config.json"
    for arguments in [
        ["--config", str(path), "crew-configure"],
        ["crew-configure", "--config", str(path)],
        ["workspace", "--config", str(path), "list"],
        ["workspace", "list", "--config", str(path)],
        ["--config", str(path), "bus", "status"],
        ["bus", "--config", str(path), "status"],
        ["bus", "status", "--config", str(path)],
    ]:
        assert parser().parse_args(arguments).config == path


def test_config_option_inside_message_is_not_reparsed():
    arguments = parser().parse_args(["send", "test", "--message", "keep --config literal"])
    assert arguments.message == "keep --config literal"
