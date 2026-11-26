"""The local and CI gates share the same just recipes.

Pin what `just check` and the manual pre-commit stage run, and keep the ty suppression
count in src honest: every suppression names its rule and its reason (F-M2, D5).
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

# shortcut: these tests read justfile and .pre-commit-config.yaml, which the sdist does not
# ship, so they need a repository checkout; add a skip when running the suite from an
# unpacked sdist becomes a supported path.
PROJECT = Path(__file__).resolve().parents[1]
TY_SRC = "uv run ty check src"
TY_INTERNAL = "uv run ty check src packages/*/src"
COMPOSITION = "uv run --no-sync python tools/assert_composition.py"
PUBLIC_TREE = "{{quote(just_executable())}} --justfile {{quote(justfile())}} assert-public-tree"
PATH_ENV = "ANSWERBACK_SUITE_LOCK"
HELD_ENV = "ANSWERBACK_SUITE_LOCK_HELD"
SUITE_LOCK = "python3 tools/suite_lock.py run -- "
REQUIRE_LOCK = "python3 tools/suite_lock.py require"
TY_IGNORE = re.compile(r"#\s*ty:\s*ignore")
EXPLAINED_TY_IGNORE = re.compile(r"#\s*ty:\s*ignore\[[a-z-]+(?:,\s*[a-z-]+)*\]\s+--\s+\S")
TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore")


def _recipe(name: str) -> list[str]:
    """Return the command lines of one justfile recipe, in order."""
    lines = (PROJECT / "justfile").read_text().splitlines()
    # A recipe's first line is its name, its parameters when it has any, and a colon.
    heads = [line.removesuffix(":").split()[:1] if line.endswith(":") else [] for line in lines]
    body: list[str] = []
    for line in lines[heads.index([name]) + 1 :]:
        if not line[:1].isspace():
            break
        body.append(line.strip())
    return body


def _hook(name: str) -> list[str]:
    """Return the lines of one pre-commit hook, from its `- id:` line to the next entry."""
    config = (PROJECT / ".pre-commit-config.yaml").read_text()
    lines = [line.strip() for line in config.splitlines()]
    start = lines.index(f"- id: {name}")
    end = start + 1
    while end < len(lines) and not lines[end].startswith(("- id:", "- repo:")):
        end += 1
    return lines[start:end]


def _src_lines(pattern: re.Pattern[str]) -> list[str]:
    """Return `path:line: text` for every line under src/ that matches the pattern."""
    return [
        f"{path.relative_to(PROJECT)}:{number}: {line.strip()}"
        for path in sorted((PROJECT / "src").rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if pattern.search(line)
    ]


# Changed for P6: the gate's commands moved from `check` to `_check`, which `check` runs
# under the suite lock. red_at_pretip (5c135ad has no `_check` recipe).
def test_just_check_runs_ty_on_src_before_pytest() -> None:
    check = _recipe("_check")
    assert TY_SRC in check
    pytest_line = next(
        index for index, line in enumerate(check) if line.startswith("uv run pytest")
    )
    assert check.index(TY_SRC) < pytest_line


# red_at_pretip
def test_just_check_is_one_command_that_runs_the_whole_gate_under_the_suite_lock() -> None:
    # One command, so no part of the gate can run outside the lock.
    (command,) = _recipe("check")
    assert command.startswith(SUITE_LOCK)
    assert command.endswith(" _check")
    assert "--no-wait" not in command and "--wait" not in command
    assert _recipe("_check") == [
        REQUIRE_LOCK,
        f"{COMPOSITION} public",
        "uv run ruff check .",
        "uv run ruff format --check .",
        TY_SRC,
        "uv run pytest",
        PUBLIC_TREE,
    ]


# Step 11 of the modular split: the internal composition has its own one-command gate.
def test_just_check_internal_is_one_command_that_runs_the_whole_gate_under_the_suite_lock() -> None:
    (command,) = _recipe("check-internal")
    assert command.startswith(SUITE_LOCK)
    assert command.endswith(" _check-internal")
    assert _recipe("_check-internal") == [
        REQUIRE_LOCK,
        f"{COMPOSITION} internal",
        "uv run ruff check .",
        "uv run ruff format --check .",
        TY_INTERNAL,
        "uv run pytest",
        'for suite in packages/*/tests; do uv run pytest "$suite" || exit 1; done',
    ]


# red_at_pretip
def test_no_recipe_starts_a_whole_suite_outside_the_suite_lock() -> None:
    assert _recipe("test") == [f"{SUITE_LOCK}uv run pytest"]
    # `just ci` runs `just check` in a container, whose own lock file no other run shares.
    assert _recipe("ci")[-1].startswith(f"{SUITE_LOCK}act -j check ")
    # `_check` and `_check-internal` are the recipes that start a suite with no `run` in
    # front. Their first command ends them with exit 77 unless a run above holds the lock
    # (fix 1, finding 8).
    gates = _recipe("_check") + _recipe("_check-internal")
    assert _recipe("_check")[0] == _recipe("_check-internal")[0] == REQUIRE_LOCK
    lines = [line.strip() for line in (PROJECT / "justfile").read_text().splitlines()]
    starters = [line for line in lines if line.startswith(("uv run pytest", "act "))]
    assert [line for line in starters if line not in gates] == []


# A stand-in for `uv`: it writes down what it was asked to run and runs nothing.
UV_STUB = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$P6_STEPS"
"""


def _folder_for_path(base: Path) -> Path:
    """Make and return a folder that can be an entry of PATH: its path holds no
    `os.pathsep`. It is `base / "bin"` when that holds none, and else a new folder under
    the first temporary folder of this machine whose path holds none."""
    if os.pathsep not in str(base):
        folder = base / "bin"
        folder.mkdir()
        return folder
    for root in (tempfile.gettempdir(), "/tmp", "/var/tmp"):
        if os.pathsep not in root:
            try:
                return Path(tempfile.mkdtemp(prefix="gate-stub-", dir=root))
            except OSError:
                continue  # This one cannot be written here (a sandbox); try the next.
    pytest.fail(f"no temporary folder without {os.pathsep!r} in its path can be made")


# T-129. The recipes call `uv` and `act` by name, so a stand-in is found through PATH, and
# an entry of PATH cannot hold a colon: with the stand-in under a temporary folder whose
# path has one, the entry is cut in two, the stand-in is not found, and the next `uv` or
# `act` on PATH is started, which is the real one.
@pytest.fixture
def stand_in() -> Iterator[Callable[[Path, str, str], Path]]:
    """Write a stand-in program `name` with the text `text` for a test whose files are
    under `base`, and return its path. Its folder is the entry for PATH. A folder that
    is made outside `base` is removed when the test has ended."""
    outside: list[Path] = []

    def _stand_in(base: Path, name: str, text: str) -> Path:
        folder = _folder_for_path(base)
        if base not in folder.parents:
            outside.append(folder)
        stub = folder / name
        stub.write_text(text)
        stub.chmod(0o700)
        return stub

    yield _stand_in
    for folder in outside:
        shutil.rmtree(folder, ignore_errors=True)


def test_stand_in_is_found_through_path_when_the_base_folder_has_a_colon(
    tmp_path: Path, stand_in: Callable[[Path, str, str], Path]
) -> None:
    base = tmp_path / "with:colon"
    base.mkdir()

    stub = stand_in(base, "uv", UV_STUB)

    assert os.pathsep == ":" and os.pathsep not in str(stub.parent)
    path = f"{stub.parent}{os.pathsep}{os.environ['PATH']}"
    assert shutil.which("uv", path=path) == str(stub)
    # The folder under `base` is the entry that is cut in two: nothing is found in it.
    assert shutil.which("uv", path=str(base / "bin")) is None


# red_at_pretip (fix 1, finding 8: at 607e026 `just _check` runs the gate with no lock).
# The form is the reviewer's (cases recipe-failure of in/extra_checks.py): a copy of the
# justfile and of the tool in a scratch folder, and a `uv` that only writes a line. No
# gate runs, and the lock file is a scratch file.
def test_private_check_recipe_runs_nothing_unless_a_run_above_holds_the_lock(
    tmp_path: Path,
    stand_in: Callable[[Path, str, str], Path],
) -> None:
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed; no recipe can be started here at all")
    project = tmp_path / "project"
    (project / "tools").mkdir(parents=True)
    shutil.copy(PROJECT / "justfile", project / "justfile")
    shutil.copy(PROJECT / "tools" / "suite_lock.py", project / "tools" / "suite_lock.py")
    stub = stand_in(tmp_path, "uv", UV_STUB)
    steps = tmp_path / "steps.log"
    environ = {key: value for key, value in os.environ.items() if key != HELD_ENV}
    environ["PATH"] = f"{stub.parent}{os.pathsep}{environ['PATH']}"
    environ["P6_STEPS"] = str(steps)
    environ[PATH_ENV] = str(tmp_path / "state" / "suite.lock")
    recipe = [just, "--justfile", str(project / "justfile"), "_check"]

    alone = subprocess.run(recipe, env=environ, capture_output=True, text=True, timeout=30)

    assert alone.returncode == 77, alone.stderr
    assert not steps.exists()

    # The same recipe below a run: the lock descriptor reaches the recipe's command
    # through `just` and the shell, and the four steps of the gate are asked for.
    held = subprocess.run(
        ["python3", str(project / "tools" / "suite_lock.py"), "run", "--no-wait", "--", *recipe],
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert held.returncode == 0, held.stderr
    recorded = steps.read_text().splitlines()
    assert recorded[:5] == [
        "run --no-sync python tools/assert_composition.py public",
        "run ruff check .",
        "run ruff format --check .",
        "run ty check src",
        "run pytest",
    ]
    # The public-tree assertion is the last step, through the `assert-public-tree` recipe.
    (public_tree,) = recorded[5:]
    assert public_tree.startswith("run --no-sync python tools/assert_public_tree.py --skip-dir ")


# Fix 2: `just ci` runs one job at a time. red_at_pretip (c6b5a31 passes no
# `--concurrent-jobs` and puts the forwarded arguments into the shell line as text).
def test_just_ci_passes_one_job_and_refuses_a_second_concurrent_jobs() -> None:
    lines = (PROJECT / "justfile").read_text().splitlines()
    # The arguments reach the recipe as words ("$@"), not as text for the shell to read
    # again, so what the refusal below looked at is what `act` gets.
    assert lines[lines.index("ci *args:") - 1] == "[positional-arguments]"
    refusal, _, command = _recipe("ci")
    assert refusal.startswith("{{ if args =~ '--concurrent-jobs' { error(")
    assert command == (
        f'{SUITE_LOCK}act -j check --concurrent-jobs 1 --artifact-server-path .act-artifacts "$@"'
    )


# A stand-in for `act`: it writes down its arguments, one a line, and runs nothing.
ACT_STUB = """\
#!/bin/sh
printf '%s\\n' "$@" >> "$P6_STEPS"
"""
ONE_JOB = ["-j", "check", "--concurrent-jobs", "1", "--artifact-server-path", ".act-artifacts"]


# red_at_pretip. A copy of the justfile and of the tool in a scratch folder, an `act` that
# only writes its arguments, and a scratch lock file: no container and no gate runs.
def test_just_ci_refuses_a_forwarded_concurrent_jobs_before_anything_runs(
    tmp_path: Path,
    stand_in: Callable[[Path, str, str], Path],
) -> None:
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed; no recipe can be started here at all")
    project = tmp_path / "project"
    (project / "tools").mkdir(parents=True)
    shutil.copy(PROJECT / "justfile", project / "justfile")
    shutil.copy(PROJECT / "tools" / "suite_lock.py", project / "tools" / "suite_lock.py")
    stub = stand_in(tmp_path, "act", ACT_STUB)
    steps = tmp_path / "steps.log"
    lock = tmp_path / "state" / "suite.lock"
    environ = {key: value for key, value in os.environ.items() if key != HELD_ENV}
    environ["PATH"] = f"{stub.parent}{os.pathsep}{environ['PATH']}"
    environ["P6_STEPS"] = str(steps)
    environ[PATH_ENV] = str(lock)

    def ci(*arguments: str) -> subprocess.CompletedProcess[str]:
        recipe = [just, "--justfile", str(project / "justfile"), "ci", *arguments]
        return subprocess.run(recipe, env=environ, capture_output=True, text=True, timeout=30)

    for arguments in (
        ["--concurrent-jobs", "4"],
        ["--concurrent-jobs=4"],
        ["--dryrun", "--concurrent-jobs=2"],
    ):
        refused = ci(*arguments)
        assert refused.returncode != 0, arguments
        assert "--concurrent-jobs" in refused.stderr
        assert not steps.exists()
        assert not lock.exists()
        assert not (project / ".act-artifacts").exists()

    # Text that a shell would turn into the flag stays one word that is not the flag.
    words = ["--dryrun", "--matrix", "os:ubuntu latest", "'--concurrent''-jobs=4'", "$(id)"]
    passed = ci("--dryrun", "--matrix", "os:ubuntu latest")
    assert passed.returncode == 0, passed.stderr
    assert steps.read_text().splitlines() == ONE_JOB + words[:3]
    steps.unlink()
    assert ci(*words[3:]).returncode == 0
    assert steps.read_text().splitlines() == ONE_JOB + words[3:]


# red_at_pretip
def test_just_resolves_the_check_recipe_without_running_it() -> None:
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed; `just check` cannot be started here at all")
    done = subprocess.run(
        [just, "--justfile", str(PROJECT / "justfile"), "--dry-run", "check"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    # A dry run prints the command it would start, and starts nothing.
    (command,) = done.stderr.splitlines()
    words = shlex.split(command)
    assert words[:4] == ["python3", "tools/suite_lock.py", "run", "--"]
    # The program that just names as itself: behind a shim or a link it is not `just`.
    assert os.access(words[4], os.X_OK)
    assert words[5] == "--justfile"
    assert os.path.samefile(words[6], PROJECT / "justfile")
    assert words[7:] == ["_check"]


def test_manual_pre_commit_stage_runs_ty_not_pyright_or_a_coverage_threshold() -> None:
    config = (PROJECT / ".pre-commit-config.yaml").read_text()
    assert "uv run pyright" not in config
    assert "--cov-fail-under" not in config
    hook = _hook("ty")
    assert f"entry: {TY_SRC}" in hook
    assert any(line.startswith("stages: [manual]") for line in hook)


def test_ty_is_locked() -> None:
    assert re.search(r'^name = "ty"$', (PROJECT / "uv.lock").read_text(), re.MULTILINE)


def test_every_ty_suppression_in_src_names_its_rule_and_reason() -> None:
    unexplained = [line for line in _src_lines(TY_IGNORE) if not EXPLAINED_TY_IGNORE.search(line)]
    assert unexplained == []


def test_src_has_no_type_ignore_comment() -> None:
    # ty honours `# type: ignore` as well, so one would suppress a diagnostic uncounted.
    assert _src_lines(TYPE_IGNORE) == []
