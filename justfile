# A gate that starts through these recipes takes the suite lock of this OS user on this
# machine: the second one waits and names the one it waits for. The lock is cooperative.
# A gate holds it for as long as one of its processes has the lock descriptor; a process
# that closed the descriptor is not covered, and one that kept it holds the lock after
# the gate has ended. Nobody deletes the lock file. A wait that ends without the lock
# exits 75. Whether it is held, the holder's record, and a problem with the folder of
# the lock file, if one was found (a folder that is not there is not checked):
# python3 tools/suite_lock.py status
# The whole gate of the PUBLIC composition (the default `uv sync`: the kernel and its own
# providers, no plugin package), under the suite lock of this machine and OS user. It also
# asserts the shipped tree names no internal provider (`assert-public-tree`).
check:
    python3 tools/suite_lock.py run -- {{quote(just_executable())}} --justfile {{quote(justfile())}} _check

# The gate itself. Run it through `just check`, which takes the suite lock first: the
# first line ends the recipe with exit 77 when no run above it holds the lock. The second
# refuses an environment that holds a provider plugin, so the public gate never runs as the
# internal one by accident (`uv sync --all-extras` restores the public environment).
_check:
    python3 tools/suite_lock.py require
    uv run --no-sync python tools/assert_composition.py public
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check src
    uv run pytest
    {{quote(just_executable())}} --justfile {{quote(justfile())}} assert-public-tree

# The whole gate of the INTERNAL composition: the kernel plus the plugin packages of the
# `internal` dependency group (`uv sync --all-extras --group internal`), their tests included.
# The kernel suite and each package's suite are separate pytest runs: each has its own
# rootdir and `[tool.pytest.ini_options]`, and both carry a `tests/conftest.py` that the
# default import mode would register under the one module name `conftest`, so the kernel
# tests' `from conftest import ...` would reach the plugin's in a combined run.
check-internal:
    python3 tools/suite_lock.py run -- {{quote(just_executable())}} --justfile {{quote(justfile())}} _check-internal

_check-internal:
    python3 tools/suite_lock.py require
    uv run --no-sync python tools/assert_composition.py internal
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check src packages/*/src
    uv run pytest
    for suite in packages/*/tests; do uv run pytest "$suite" || exit 1; done

# The whole test suite alone, under the same lock as `just check`.
test:
    python3 tools/suite_lock.py run -- uv run pytest

# One protocol run, not a test suite: it does not take the suite lock.
# Scripted peers over real local RPC, CLI, MCP, ACP and durable receipts.
self-test:
    uv run answerback self-test --mode simulated

# Entry point for the attended hc checklist; never starts a nested Herdr TUI.
live-test:
    @printf '%s\n' 'Attended hc acceptance (T-05): docs/LIVE-TEST.md#attended-hc-acceptance-integration-build' 'Follow the checklist in an owner-attended orchestrator chat and record its results.' 'No live probes have been run by this recipe.'

# Pass act flags, e.g. just ci --dryrun --matrix os:ubuntu-latest.
# The workflow runs `just check` in a container. The lock file there is the container's
# own, so the suite lock of this machine is taken here, around act.
# The artifact server path lets the upload step pass under act (GitHub provides that
# service on its runners; act does not without the flag).
# One job at a time: the lock is one suite per machine, and act would start a job of the
# matrix for every processor. A forwarded --concurrent-jobs is refused before anything
# runs, and the arguments reach act as the words that were given ("$@"; the attribute
# below needs just 1.29 or newer).
# Local run of the GitHub workflow under act, one job at a time, under the suite lock.
[positional-arguments]
ci *args:
    {{ if args =~ '--concurrent-jobs' { error('just ci runs one job at a time (--concurrent-jobs 1, set by this recipe): leave --concurrent-jobs out') } else { '' } }}
    mkdir -p .act-artifacts
    python3 tools/suite_lock.py run -- act -j check --concurrent-jobs 1 --artifact-server-path .act-artifacts "$@"

# Checks the shipped tree for references that belong only to the internal plugin: the
# kernel package, its packaging, tools, integrations, CI, tests, scripts, docs and skills.
# Skipped here, in the combined repository, are the plugin package itself, `status/` (never
# copied to the public repository), the workspace lock, and the two history documents
# (CHANGELOG.md, STATUS.md) that the public repository starts afresh. The four allowlisted
# lines are the uv workspace wiring and the retired flat keys, both removed by the split
# (T-271) or 0.17 (T-256).
assert-public-tree:
    uv run --no-sync python tools/assert_public_tree.py \
        --skip-dir packages --skip-dir status --skip-dir uv.lock \
        --skip-dir CHANGELOG.md --skip-dir STATUS.md --skip-dir .gate-logs --skip-dir .gitignore

build:
    uv build

smoke-install:
    uv build
    uv run python scripts/smoke_install.py
