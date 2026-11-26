"""The install smoke script must get past the load of its configuration (T-154).

The recipe ``smoke-install`` starts ``scripts/smoke_install.py`` as a plain program, so no
``tests/`` folder is on its import path. These tests start it the same way and stop it right
after the configuration step with ``--config-only``. They never run the smoke itself.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def start_script(*options: str) -> subprocess.CompletedProcess[str]:
    """Start the script as the second line of the recipe does: by path, from the root."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONNOUSERSITE"] = "1"
    return subprocess.run(
        [sys.executable, "scripts/smoke_install.py", *options],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_script_loads_its_configuration_when_started_as_a_plain_program() -> None:
    result = start_script("--config-only")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    folder = Path(lines[0])
    # The configuration step made its folder (the fixture's prefix) and the stop removed it.
    assert folder.is_absolute() and folder.name.startswith("hb-"), folder
    assert not folder.exists(), f"{folder} was left behind"


def test_config_only_is_an_option_and_the_default_is_the_whole_smoke() -> None:
    result = start_script("--help")
    assert result.returncode == 0, result.stderr
    assert "--config-only" in result.stdout
    assert "--upgrade-from" in result.stdout
