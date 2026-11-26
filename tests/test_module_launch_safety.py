"""Whole-tree guard: no `python -m answerback_bridge` re-entry may skip `-P`.

Scoped to ``src/answerback_bridge``: these are the bridge's own launch commands,
handed to an MCP client, a peer agent, a supervisor or kiro-cli to run in a
working directory the bridge does not control. Test-only invocations of the CLI
under ``tests/`` run from a cwd the test suite itself controls, so they are a
different, already-safe case and are out of scope here.
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from answerback_bridge.install import owns
from answerback_bridge.process import module_launch_argv
from answerback_bridge.supervision import NativeSupervisor

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "answerback_bridge"
HELPER_PATH = SOURCE_ROOT / "process.py"
INSTALLER_KIT_PATH = SOURCE_ROOT / "installer_kit.py"
# The two kernel places that READ a re-entry argv (the shared installer kit and the
# supervisor definitions) legitimately name the bare legacy shape so old installs stay
# owned. A provider package's native marker reader (a plugin's registrar) is the plugin's own.
MATCHER_PATHS = {
    INSTALLER_KIT_PATH,
    SOURCE_ROOT / "supervision.py",
}
ALLOWED_PATHS = frozenset({HELPER_PATH, *MATCHER_PATHS})

# The tree-wide guard looks for the argv element that selects a module, not for the
# package name beside it. Any string literal in a module's AST that is exactly `-m`, or
# `-m` glued to a bridge package name (`python -manswerback_bridge` launches it too), is
# an offender whatever follows it: a package name as a literal or a variable, an f-string,
# a tuple element, the next line of a list, appended one element at a time, in single or
# double quotes. Out of reach, by design: a `-m` assembled at runtime from fragments
# (`"-" + "m"`, `f"-{flag}"`) or buried in a longer shell string that is split later;
# those need a reader, not a test. Comments and prose that mention `-m` are not literals
# equal to `-m`, so they do not count.
_MODULE_FLAG_LITERAL = re.compile(r"-m(?:answerback_bridge|codex_herdr_bridge)?")
_SAFE_MODULE_LAUNCH = re.compile(
    r"""(["'])-P\1\s*,\s*\n?\s*(["'])-m\2\s*,\s*\n?\s*(["'])answerback_bridge\3"""
)


def _module_flag_literals(
    root: Path, allowed: frozenset[Path] = frozenset()
) -> list[tuple[str, int]]:
    """``(file, line)`` of every ``-m`` string literal under ``root``, outside ``allowed``."""
    found: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*.py")):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        found.extend(
            (str(path.relative_to(root)), node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _MODULE_FLAG_LITERAL.fullmatch(node.value)
        )
    return sorted(found)


def test_module_launch_argv_disables_a_shadowing_working_directory() -> None:
    assert module_launch_argv("/cfg/config.json", "mcp") == [
        sys.executable,
        "-P",
        "-m",
        "answerback_bridge",
        "--config",
        "/cfg/config.json",
        "mcp",
    ]


def test_module_launch_argv_accepts_a_path_and_any_number_of_trailing_arguments() -> None:
    argv = module_launch_argv(Path("/cfg/config.json"), "complete", "task-1", "--result-file")
    assert argv[:6] == [
        sys.executable,
        "-P",
        "-m",
        "answerback_bridge",
        "--config",
        "/cfg/config.json",
    ]
    assert argv[6:] == ["complete", "task-1", "--result-file"]


def test_re_entry_runs_the_installed_package_not_a_same_named_directory_in_cwd(
    tmp_path: Path,
) -> None:
    """A real re-entry from a cwd holding a decoy ``answerback_bridge`` package."""
    decoy = tmp_path / "answerback_bridge"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("print('DECOY-IMPORT')\n")
    (decoy / "__main__.py").write_text("print('DECOY-MAIN')\n")
    result = subprocess.run(
        module_launch_argv(tmp_path / "config.json", "--help"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "DECOY" not in result.stdout
    assert result.stdout.startswith("usage: answerback")


def test_only_the_shared_helper_constructs_a_bare_module_launch() -> None:
    offenders = [
        f"{file}:{line}" for file, line in _module_flag_literals(SOURCE_ROOT, ALLOWED_PATHS)
    ]
    assert not offenders, (
        "Found a `-m` launch literal outside process.py and the two argv matchers; build "
        "the argv with process.module_launch_argv() instead, which always includes -P: "
        f"{offenders}"
    )


def test_the_allowed_files_are_exactly_those_that_name_the_module_flag() -> None:
    """The guard is not vacuous, and the allowlist is no wider than the tree needs."""
    files = sorted({file for file, _line in _module_flag_literals(SOURCE_ROOT)})
    assert files == [
        "installer_kit.py",
        "process.py",
        "supervision.py",
    ]


_BYPASS_SHAPES = {
    "variable_package": 'package = "answerback_bridge"\nargv = [sys.executable, "-m", package]\n',
    "literal_package": 'argv = [sys.executable, "-m", "answerback_bridge", "mcp"]\n',
    "tuple_argv": 'argv = (sys.executable, "-m", f"{PACKAGE}", "mcp")\n',
    "package_on_the_next_line": 'argv = [\n    sys.executable,\n    "-m",\n    PACKAGE,\n]\n',
    "single_quotes": "argv = [sys.executable, '-m', 'codex_herdr_bridge']\n",
    "glued_package": 'argv = [sys.executable, "-manswerback_bridge", "mcp"]\n',
    "appended_one_element_at_a_time": 'argv = [sys.executable]\nargv.append("-m")\n',
}


@pytest.mark.parametrize("shape", sorted(_BYPASS_SHAPES))
def test_guard_flags_every_known_bypass_shape_with_its_line(tmp_path: Path, shape: str) -> None:
    source = 'import sys\n\nPACKAGE = "answerback_bridge"\n\n' + _BYPASS_SHAPES[shape]
    (tmp_path / "launch.py").write_text(source)
    # The one line of the sample that carries the module flag.
    expected_line = next(
        n for n, text in enumerate(source.splitlines(), 1) if '"-m' in text or "'-m" in text
    )
    assert _module_flag_literals(tmp_path) == [("launch.py", expected_line)]


def test_guard_ignores_prose_other_options_and_the_safe_flag(tmp_path: Path) -> None:
    (tmp_path / "quiet.py").write_text(
        '"""Run it as ``python -m answerback_bridge mcp`` from any directory."""\n'
        "import sys\n"
        "\n"
        "# built by module_launch_argv(); a bare -m in this comment is not a literal\n"
        'HELP = "use -m to select a module"\n'
        'FLAGS = [sys.executable, "-P", "--mode", "-", "m", "-mode"]\n'
    )
    assert _module_flag_literals(tmp_path) == []


def test_the_shared_helper_itself_places_minus_p_immediately_before_minus_m() -> None:
    assert _SAFE_MODULE_LAUNCH.search(HELPER_PATH.read_text()), (
        "module_launch_argv must build its argv as [sys.executable, '-P', '-m', "
        "'answerback_bridge', ...] so no caller can omit -P"
    )


def test_helper_is_defined_exactly_once() -> None:
    definitions = [
        str(path.relative_to(SOURCE_ROOT))
        for path in SOURCE_ROOT.rglob("*.py")
        if re.search(r"^def module_launch_argv\(", path.read_text(), re.MULTILINE)
    ]
    assert definitions == ["process.py"]


def _entry(args: list[str]) -> dict[str, object]:
    return {"command": "/opt/venv/bin/python3.13", "args": args}


def test_registration_recognises_the_safe_re_entry_shape_as_ours(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    launch = module_launch_argv(config, "mcp")
    assert owns({"command": launch[0], "args": launch[1:]}, str(config))
    assert owns(
        _entry(["-P", "-m", "codex_herdr_bridge", "--config", str(config), "mcp"]), str(config)
    )


def test_registration_still_recognises_installed_legacy_entries_without_minus_p(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    assert owns(_entry(["-m", "answerback_bridge", "--config", str(config), "mcp"]), str(config))
    assert owns(_entry(["-m", "codex_herdr_bridge", "--config", str(config), "mcp"]), str(config))


def test_migration_rewrites_a_recognised_legacy_entry_to_the_form_the_helper_builds(
    tmp_path: Path,
) -> None:
    """red_at_base (T-70): what migration writes is the safe form, not the recognised one."""
    from answerback_bridge.registration import SERVER_NAME, _prepare_migration

    config = tmp_path / "config.json"
    safe = module_launch_argv(config, "mcp")
    legacy = {"command": safe[0], "args": [item for item in safe[1:] if item != "-P"]}
    assert owns(legacy, str(config))
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"herdr_bridge": legacy}}))
    plan, previous = _prepare_migration(path, config)
    assert plan is not None and previous == "herdr_bridge"
    entry = json.loads(plan.updated)["mcpServers"][SERVER_NAME]
    assert [entry["command"], *entry["args"]] == safe
    assert owns(entry, str(config))


def test_registration_does_not_claim_other_modules_or_misplaced_flags(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    assert not owns(_entry(["-m", "other_tool", "--config", str(config), "mcp"]), str(config))
    assert not owns(_entry(["-P", "-m", "other_tool", "--config", str(config), "mcp"]), str(config))
    assert not owns(
        _entry(["-m", "-P", "answerback_bridge", "--config", str(config), "mcp"]), str(config)
    )
    assert not owns(
        _entry(["-P", "answerback_bridge", "--config", str(config), "mcp"]), str(config)
    )


def test_supervision_verifies_both_the_safe_and_the_legacy_service_command(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    supervisor = NativeSupervisor(config)
    resolved = str(config.resolve())
    assert supervisor._matches_command(module_launch_argv(resolved, "serve"))
    assert supervisor._matches_command(
        ["/usr/bin/python3", "-P", "-m", "codex_herdr_bridge", "--config", resolved, "serve"]
    )
    assert supervisor._matches_command(
        ["/usr/bin/python3", "-m", "answerback_bridge", "--config", resolved, "serve"]
    )
    assert supervisor._matches_command(
        ["/usr/bin/python3", "-m", "codex_herdr_bridge", "--config", resolved, "serve"]
    )


def test_supervision_rejects_a_foreign_or_malformed_service_command(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    supervisor = NativeSupervisor(config)
    resolved = str(config.resolve())
    assert not supervisor._matches_command(
        ["/usr/bin/python3", "-P", "-m", "other_tool", "--config", resolved, "serve"]
    )
    assert not supervisor._matches_command(
        ["/usr/bin/python3", "-P", "-P", "-m", "answerback_bridge", "--config", resolved, "serve"]
    )
    assert not supervisor._matches_command(
        ["/usr/bin/python3", "-m", "-P", "answerback_bridge", "--config", resolved, "serve"]
    )
    assert not supervisor._matches_command(
        ["/usr/bin/python3", "-P", "-m", "answerback_bridge", "--config", resolved, "mcp"]
    )
    assert not supervisor._matches_command(
        [
            "/usr/bin/python3",
            "-P",
            "-m",
            "answerback_bridge",
            "--config",
            "/elsewhere.json",
            "serve",
        ]
    )


def test_supervision_accepts_its_own_written_launchd_definition(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    supervisor = NativeSupervisor(config)
    path = supervisor._definition_path(supervisor.label, "launchd")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(supervisor.launchd_definition())
    supervisor._verify_definition(supervisor.label, "launchd")
