# Test a change to Answerback

Every change passes through one gate, `just check`, which runs lint, format, types, the whole
test suite and the public-tree assertion under one per-machine lock. A scripted protocol run and an
attended checklist sit beside it for the things a unit test cannot show. What you get back is an
exit code and, for the scripted run, a JSON report with one outcome per phase.

This page is for a contributor with a source checkout who wants to validate a change or a provider
package. Set the checkout up as [CONTRIBUTING.md](../CONTRIBUTING.md) says first.

The shortest path, from the checkout:

```sh
just check
```

```text
summary: tracked=706 scanned=692 skipped_binary=14 skipped_dir=0 content_hits=2 path_hits=0 binary_hits=0 allowlisted=2 hits=0
```

The line shown is the gate's last, from `just assert-public-tree`; `hits=0` and exit `0` mean every
step before it passed. The recipe takes the suite lock, checks that the environment is the public
composition (`tools/assert_composition.py public`), then runs `uv run ruff check .`,
`uv run ruff format --check .`, `uv run ty check src`, `uv run pytest` and `just assert-public-tree`
in that order (`justfile`, recipe `_check`). It stops at the first failing command. While another
gate runs on the machine, yours waits and names the holder. To see who holds the lock without
waiting:

```sh
python3 tools/suite_lock.py status
```

```text
held
pid: <pid>
command pid: <pid>
started: 2026-11-25T19:32:09Z (546 s ago)
command: <just> --justfile <checkout>/justfile _check
cwd: <checkout>
lock: ~/.local/state/answerback/suite.lock
usable: yes
```

Three of its lines matter: `held` or `free` is the lock's state now, `command` is the gate that
holds it, and `usable: yes` means `run` would accept the lock's folder.

## Choose a test layer

| Layer | Command | Evidence and prerequisites |
| --- | --- | --- |
| Regression gate | `just check` | After the default `uv sync`: lint, format, types, pytest, the [public-tree assertion](#public-tree-assertion), under the [suite lock](#suite-lock). No live model or account. |
| Scripted protocols | `just self-test` | `answerback self-test --mode simulated`: real local RPC, CLI, MCP STDIO, ACP and SQLite receipts, with scripted peers. No live model. |
| Attended acceptance | `just live-test` | Prints the entry point to the [attended hc checklist](LIVE-TEST.md#attended-hc-acceptance-integration-build); runs no probe itself. |

The gate refuses an environment that holds a provider plugin: `just check` stops when one is
installed, and `uv sync --all-extras` restores the public environment. The attended
layer is an owner at the actual Herdr App workers, recording results; the recipe is not an
acceptance result.

The scripted layer checks both hand-off directions, matching parent/child results, duplicate
suppression, persisted receipts after a service restart, and cleanup. It creates a private
temporary environment, starts a standalone service, then stops it and removes the temporary state.
It needs neither NATS nor launchd. Its JSON report carries each phase's outcome; a failed phase
exits nonzero. It does not take the suite lock: it is one protocol run, not a suite.

### The two compositions

The kernel composes every provider from the `answerback.providers` entry points the installed
distributions declare; nothing in `src/answerback_bridge/` names a provider plugin. This
repository is the public composition: the default `uv sync` installs the kernel and its own
providers, and no plugin. A provider plugin is a distribution of its own, kept in its own
repository, which installs it beside a clone of this one and gates that composition itself; that
package documents its own behaviour.

The kernel suite pins what differs when one plugin is installed side by side and selects by
`tests/composition.py`, which reads what the plugin adds from the plugin's own
`tests/fixtures/kernel_composition.py`. With no plugin, `builtin_plugins()` lists the kernel's nine
providers, and a saved config that names an absent provider loads with one warning as a disabled
provider whose settings are kept; `doctor` shows it as `disabled`.
`tools/assert_composition.py public` is the check `just check` starts with, so the kernel's gate
never runs in a plugin's composition by accident.

## Public-tree assertion

`just assert-public-tree` scans the Git-tracked files for references that belong only to an
internal provider package. It checks each path before classifying its payload. The three-branch
ASCII-only rule catches bounded case-insensitive `a[k]i`, camel-case `A[k]i` at an ASCII case or
digit transition, and all-caps `A[K]I` before uppercase, a digit, or an underscore, or after a
lowercase letter or digit. Lowercase run-ons such as `a[k]iadapter`, title-case `Akira`, all-caps
forms followed by lowercase letters such as `A[K]Ia` and `A[K]Is`, mixed-case `aKiAdapter`, and
Greek or fullwidth lookalikes are documented misses.

Binary detection follows the path check: a NUL in the first 8 KiB marks a payload binary, then raw
ASCII and UTF-16LE bytes are scanned without text decoding. Binary matches report `kind: "binary"`;
JSON (`--json`) also lists every binary path under `skipped_binary_paths`. The allowlist
(`tools/public-tree-allowlist.txt`) is control data, not a blanket content exemption: exact entry
text is exempt, while comments and entry paths are scanned. Its two entries are the retired flat
configuration keys the kernel reads until 0.17 ([the upgrade guide](ANSWERBACK-UPGRADE.md#the-configjson-layout)). The assertion scans only Git-tracked files, so an
untracked file is not checked until `git add` stages it.

`just check` runs the recipe over the whole tracked tree, with nothing skipped (`justfile`,
recipe `assert-public-tree`).

## Suite lock

Whole test suites that start through `tools/suite_lock.py` take one lock per machine and OS user:
`just check`, `just test` and `just ci` start through it, and `just ci` tells `act` to run one job
at a time. The lock is cooperative exclusion inside one OS account on one machine. It is not a
security boundary: a process of the same account can close the descriptor, lock the same file or
signal another run. The lock path is `.local/state/answerback/suite.lock` under the account's home
directory, shared by every checkout and worktree of the project. The home comes from the account
database, not from `HOME`, so a test or a sandbox that changes `HOME` still meets the same file.

| Invocation | Does |
| --- | --- |
| `python3 tools/suite_lock.py status` | Prints who holds the lock, or `free`; does not wait. |
| `python3 tools/suite_lock.py status --json` | The same as one JSON object. |
| `python3 tools/suite_lock.py run -- uv run pytest` | Takes the lock, waits without bound, runs the command. |
| `python3 tools/suite_lock.py run --wait 600 -- uv run pytest` | Waits at most 600 seconds, then exits 75. |
| `python3 tools/suite_lock.py run --no-wait -- uv run pytest` | Gives up at once when the lock is held. |
| `python3 tools/suite_lock.py require` | Exits 0 only below a run that holds the lock. |

**How long the lock is held.** A run holds the lock for as long as some process still has the
open file description that `run` locked: the wrapper, its command, and every process that
inherited the descriptor and kept it. Nothing else is claimed. The wrapper waits for its command
and for nothing else, and exits with the command's exit code.

- The lock is `flock` on the open file, and the command gets the locked descriptor. The kernel
  releases the lock when the last process that has the descriptor closes it or ends, also after
  the wrapper was killed (`SIGKILL`). No stale lock exists and nothing needs to be cleaned up.
- A process that closed the descriptor is not covered, and may still run when the next suite
  starts. Python's `subprocess` closes it in what it starts unless it is told `close_fds=False` or
  `pass_fds`. So the end of a run is not proof that its processes have ended.
- A process that kept the descriptor keeps the lock after the command has ended. The wrapper then
  says in one line that the lock is still held, and ends. A run that comes next names the holder
  as far as the record goes and waits, or gives up with 75 after `--wait`. To free the lock, end
  the process that kept the descriptor; never delete the lock file.
- `SIGINT`, `SIGTERM`, `SIGHUP` or `SIGQUIT` that reaches the wrapper is passed on to the process
  group that the wrapper created for its command. When the command still runs five seconds after
  the first signal was passed on, the group is sent `SIGTERM`, and ten seconds later `SIGKILL`,
  again every ten seconds (`TERM_AFTER_SECONDS` and `KILL_AFTER_SECONDS` at the top of the tool).
  A signal that the wrapper inherited as ignored (a `&` job of a shell for `SIGINT` and `SIGQUIT`,
  `nohup` for `SIGHUP`) stays ignored, in the wrapper and in its command.
- The group is signalled only until the wrapper has waited for its command. A process that left
  the group is never signalled. Under `run` the command leads a session of its own and has no
  controlling terminal, so it cannot open `/dev/tty`.

**The record.** The holder writes its process ID, the process ID of its command (which also names
the command's session and process group), start time, command and working directory into the lock
file, and clears them at its end when nobody holds the lock then. The record is for people and
probes. It is never the lock, and nothing in it is verified: any process of the same user can
write the file. Only a lock attempt says whether the lock is held now; `status` makes one, and
shows `held` with no holder when the lock is held and no living wrapper has a record.

**Waiting and exit codes.** `run` waits without a bound and prints the holder at most once a
minute. `--wait SECONDS` bounds the wait and `--no-wait` gives up at once. Otherwise `run` exits
with the command's exit code. The wrapper itself uses these codes, each with a line on standard
error that starts with `suite lock:`:

| Code | Meaning |
|------|---------|
| 75 | The wait ended without the lock. Nothing was run. |
| 73 | The lock file or its folder cannot be opened, made or trusted. Nothing was run. |
| 76 | The lock is held, the environment names a run as a holder of this lock file, and this process does not have that run's lock descriptor. Nothing was run. |
| 77 | `require` was not started below a run that holds the lock. |
| 64 | Wrong usage. |
| 127, 126 | The command was not found, or cannot be run. |
| 128 + N | The command was ended by signal N. |
| 130 | Interrupted (Ctrl-C) while waiting for the lock. |

A command can end with the same numbers, and a run that the command starts writes its own
`suite lock:` lines to the same standard error. So neither the number nor a `suite lock:` line
proves that this run started nothing. A caller that must know gives the command a marker of its
own, for example a file that the command makes as its first step. A marker that is missing proves
nothing: the command can have ended before it wrote it. Treat that outcome as unknown.

**Runs below a holder.** A recipe that runs under the lock may start another one. The inner `run`
does not wait for the lock when it can prove that a run above it holds it: the proof is the open
descriptor that the holder handed to its command, never the environment and never the record. The
inner run then takes the lock of its level, `suite.lock.1` beside the lock file (`suite.lock.2`
one level further down), so two runs that one holder starts do not run at the same time. Shells
and `just` pass the descriptor on; Python's `subprocess` needs `close_fds=False` or `pass_fds`.

Without the descriptor, an inner run that finds the lock held is refused with 76 and says why:
this run and the holder could wait for each other without end. The rule reads the environment
(`ANSWERBACK_SUITE_LOCK_HELD`, which names the lock file by device and inode) and the lock, never
the record. Unset the variable only for a launch that has nothing to do with the holder. If the
holder's command waits for an inner run that lacks the descriptor, bound the inner wait
(`--wait SECONDS` or `--no-wait`, exit 75).

`just _check` is the body of `just check`. It starts with `require`, so started on its own it ends
with 77 and runs nothing. `require` is a guard, not a way to take the lock: start the gate through
`run`.

**The lock path.** `run` makes the folder (mode 0700, whatever the umask is) and the file (mode
0600) when they are missing. It exits 73 and writes nothing when the path is a symbolic link, a
FIFO, a folder or another special file, when the file belongs to another user, or when the file has
a second name (a hard link). `run` also checks the folder of the lock file, and only that folder:
it exits 73 when the folder is a symbolic link, when it does not belong to this user, or when its
mode lets its group or everybody write; the line says which folder and how to fix it
(`chmod go-w <folder>`). The check is placement hygiene, not a defence: only the mode is read, and
the folders above are trusted.

Do not delete or replace the lock path to get past a refusal: a run may hold the file, and a run
that starts after the deletion does not see it. Remove the other name when no run is active, or set
`ANSWERBACK_SUITE_LOCK=<file>` to another path for every run (runs with different files do not
exclude each other).

`status` makes the same folder check, creates nothing, and reports `usable: yes` or
`usable: no. <reason>` (in JSON, `usable` and `unusable_because`). `usable` is about the folder and
`state` is about the lock; a held lock is `held` whatever `usable` says, and `free` is no
reservation. The tool never deletes the lock file. If somebody else deletes or replaces it, a
waiting run takes the lock of the file at the path now, and the holder says at its end that the
file changed; a run that starts after the change cannot see the old holder, so the two can run at
the same time.

**What the lock does not cover.**

- It sees only runs that take it. `uv run pytest` with no path, started by hand, runs a whole suite
  outside it; start it through the lock as shown above. A run of single test files does not need
  it. `just self-test` is one scripted protocol run and does not take it.
- A process that closed the lock descriptor runs on without the lock, during
  its suite and after it. The tool does not look for such processes. One that
  stayed in the command's process group can still get the signals that the
  wrapper passes on, until the wrapper has waited for the command.
- It is per OS user and per machine: two users do not exclude each other. A home on a network file
  system that two machines share is not tested.
- `just ci` holds the lock around one `act` call and passes `--concurrent-jobs 1`, so `act` runs
  the jobs of that call one after the other. A forwarded `--concurrent-jobs` is refused. The
  containers have lock files of their own.
- `status` does not wait for the lock, but it is not instant: it looks at a record twice, 20 ms
  apart, when a holder may be writing it. It looks at the lock file only, not at the files of the
  levels below it.

## One bounded assignment, by hand

The installed `answerback live-test` command is a legacy older-TUI harness: it provisions a nested
`herdr` TUI and does not validate the Herdr App. App users use `just live-test` and the attended
checklist; [the saved environment](LIVE-TEST.md#saved-environment) covers stale test configuration
and `--reset-test-env`. For the complete multi-client acceptance test, use the
[Worktree Guard scenario](WORKTREE-GUARD-SCENARIO.md); the single-route procedure below qualifies
a smaller boundary.

Use a dedicated, explicitly registered chat or workspace for your first test. Discovery proves
identity; a chat can still be unavailable or blocked by an unresolved receipt. The R1 table in
[HARNESSES.md](HARNESSES.md) says which routes are Supported, Experimental and Excluded. An
available adapter and its scripted stand-in tests do not establish R1 support. Whether a worker
starts on your machine is a separate question, which one real task answers: see the
[owned worker live check](LIVE-TEST.md#owned-worker-live-check).

### 1. Inspect service, recipient and existing work

```sh
answerback bus status
answerback find --provider kiro
answerback tasks kiro
```

The first prints the bus configuration and pending outbox count, the second the discovered peers,
the third the receipts for that provider. Copy the exact `peer_id` and absolute `project` from
discovery. Check any nonterminal receipt for that target first:

```sh
answerback task <task-id>
```

An `uncertain` receipt holds the target lock because its earlier work may have executed. Do not
bypass that lock by changing the request ID. Inspect the native chat and the stored receipt.
`answerback abandon <task-id> --reason '<why>'` releases a receipt **without cancelling
execution**; use it only after confirming that earlier work has settled and deciding to release it.

`answerback task <task-id> --reconcile` recovers a receipt from its provider's native evidence and
never resends; only a provider that implements reconciliation supports it (Codex does, and an
installed provider plugin documents whether it does). Rex setup is in
[REX.md](REX.md); other providers have theirs in [CLIENT-SETUP.md](CLIENT-SETUP.md).

### 2. Open the monitor

```sh
answerback bus monitor --web
```

The command prints a private session URL and opens the dashboard locally. Keep the command
running; Ctrl+C stops the monitor. `--no-open` prints the URL without opening a browser. Toggle
**Debug** to inspect event IDs, safe failure codes and blocking task references. Prompt and result
bodies are absent by design. For a resumable terminal record:

```sh
agent-bus monitor --after 0 --json
```

### 3. Submit one bounded assignment

Any provider's peer ID and project work the same way through `bus submit`. Replace the two
placeholders with the selected peer and project:

```sh
answerback bus submit '<peer-id>' \
  --project '/exact/project/path' \
  --message 'Reply exactly BUS_OK. Do not use tools, edit files, hand work off, or change settings.' \
  --request-id smoke-001
answerback bus result <command-id>
```

`bus result` prints the command receipt as JSON. Trimmed to the four fields that decide, a pass
looks like this:

```json
{
  "command_id": "<command-id>",
  "request_id": "smoke-001",
  "delivery": "bound",
  "task_id": "<task-id>",
  "task": {
    "status": "completed",
    "result": "BUS_OK"
  }
}
```

Success requires `delivery: "bound"`, a non-null `task_id`, `task.status: "completed"` and the exact
expected result. Confirm the native chat shows that input and answer once. `queued` or a broker
acknowledgement alone is not a completed test; the command exits `1` when delivery is `rejected`,
`expired` or `uncertain` or the task is `failed`, `uncertain` or `cancelled`.

Use a fresh request ID for new work. Repeating the same assignment uses its original ID and
returns its existing receipt; it does not reset `uncertain`, `rejected` or `expired` delivery. For
substantive prompts prefer `--message-file` so the text is not in process arguments.

### Reading a failure

| Observation | Meaning / next step |
| --- | --- |
| Queued, broker unavailable | Saved locally; wait for connectivity with the same command ID |
| Rejected with `target_busy` | No new submission; inspect the blocking task |
| Bound + blocked task | Human permission is required in the harness |
| Bound + uncertain task | A receipt exists; delivery or completion remains unverified |
| Uncertain + no task ID | Admission outcome is unproven; inspect before another attempt |
| Status unavailable in monitor | Status observation failed; it does not prove the service stopped |

Version 0.8 used "Coordinator stopped during admission" for arbitrary admission errors too, among
them a target blocked by an older uncertain task with no new reservation. Since 0.9 that proven
refusal is distinguished. Historical receipts retain the wording recorded at the time.

## Automated validation

| Step | Command | Result |
| --- | --- | --- |
| Install the locked environment | `uv sync --locked --all-extras` | `Resolved 50 packages`, `Checked 46 packages` at this commit |
| Browser for the monitor tests | `uv run playwright install chromium` | Chromium under Playwright's cache |
| The gate | `just check` | Exit `0`, `hits=0` as its last line |
| Scripted protocols | `just self-test` | A JSON report, one outcome per phase |
| Build | `uv build` | A wheel and an sdist under `dist/` |
| Install smoke | `uv run python scripts/smoke_install.py` | Below |

The suite uses disposable protocol fixtures and, when available, a private NATS process. It
exercises duplication, restarts, busy targets, native result correlation, monitor HTTP boundaries,
browser controls and reconnects. It does not send production model prompts or prove compatibility
with every installed GUI version.

### The install smoke

`scripts/smoke_install.py` is the one check that installs the built package and sends messages
through an installed bridge. It makes one temporary folder and keeps everything in it: uv tool
folders of its own, a Codex home and a fixture configuration. It installs the package there from
the source archive and from the wheel (the wheel with the `bus` extra), and starts a bridge of its
own with `start --standalone`, a private `nats-server` and the monitor dashboard.

The broker and the dashboard listen on `127.0.0.1` only. At the end it uninstalls the package,
stops the bridge and removes the temporary folder. It needs `nats-server` on `PATH` and
Playwright's Chromium.

`just check` does not run it, because it installs and starts programs. The suite only starts the
script with `--help` and with `--config-only`, which loads the fixture configuration and exits
(`tests/test_smoke_install_script.py`). The recipe `just smoke-install` runs `uv build` and then
the script. The script also takes `--upgrade-from <wheel>`: it first installs that earlier wheel,
lets its store write a completed receipt, removes it, and later checks that the new package reads
the same receipt back. Before a release, and before an install from the tree, run the smoke at
that commit with the wheel of the release that is installed: `uv build`, then
`uv run python scripts/smoke_install.py --upgrade-from <absolute path to the installed wheel>`.

A record of a pass names the commit, the date and the machine, and stands in the record of that
release or install. No program enforces this rule or checks that record.

### Running the suite

The suite is hermetic by default. The bus integration tests start a private `nats-server` on an
ephemeral loopback port and skip when no `nats-server` is on `PATH` or the optional `nats-py`
package is absent (`brew install nats-server`; see [AGENT-BUS.md](AGENT-BUS.md)).
`ANSWERBACK_TEST_OFFLINE=1` skips that fixture even if the binary is installed; other values leave
normal availability detection in place.

Bus unit tests and local HTTP/browser fixtures still run, using
`tests/bus_helpers.py::unused_server_binary`, which writes a private executable stub those tests
never launch; only `BrokerFixture` discovers and starts a real `nats-server`. To verify the
no-binary CI case locally, remove every NATS-containing directory from `PATH` and confirm
`shutil.which("nats-server") is None` in the test environment; setting `ANSWERBACK_TEST_OFFLINE=1`
alone does not prove that case.

The one test
that bootstraps a real launchd agent on your account,
`tests/test_runtime.py::test_native_launchd_start_restart_and_stop`, is skipped
unless you opt in with `ANSWERBACK_LIVE_LAUNCHD=1` (exactly `1`) in the
environment of `uv run pytest`. The check is `tests/conftest.py::launchd_live_opt_in`, and the
test runs on macOS only.

The evidence-reader tests in `tests/test_worktree_guard_evidence.py` open every directory of
pytest's temporary directory with `O_NOFOLLOW` from `/`; in a sandbox whose temporary directory
refuses that walk they skip and name the refused component. Run them with
`--basetemp=/private/tmp/<dir>` (any directory that walk can reach). Do not pin `--basetemp` in
`pyproject.toml`: pytest wipes that directory at start, so parallel runs would destroy each other's
files.

The evidence-tool hardening tests use disposable runs and inert Python children, plant dummy
environment values and inspect only those named variables; they never dump the operator's
environment. The native Codex descendant-cleanup test temporarily makes its pytest process a Linux
child subreaper, restores the previous setting, and requires a `SIGKILL` exit and PID
disappearance; a zombie is not accepted as a passing process check.

### Offline CI and local act runs

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the gate and `just self-test` on
`ubuntu-latest` and `macos-latest` (`just check` after the default sync), using Python 3.13 and
the locked dependencies.
Setup downloads dependencies and Playwright Chromium; the check and self-test steps then set
`UV_OFFLINE=1`. No model endpoint, external network service, NATS server, launchd job or systemd
user service is required. The tests still use temporary loopback HTTP and Unix sockets.

CI sets `ANSWERBACK_TEST_OFFLINE=1` for `tests/bus_helpers.py::BrokerFixture` and
`ANSWERBACK_LIVE_LAUNCHD=0` for the exact opt-in check in `tests/conftest.py::launchd_live_opt_in`.
Chromium is installed because the browser tests require it; they are not silently skipped. Each
matrix job retains its scripted JSON report as a GitHub Actions artifact named
`self-test-<os>-<composition>`, including a failed self-test's report.

With `act` (`brew install act` on macOS) and a running Docker daemon, `act` emulates the Linux job;
GitHub Actions runs the macOS job.

| Invocation | Does |
| --- | --- |
| `just ci --matrix os:ubuntu-latest -P ubuntu-latest=catthehacker/ubuntu:act-latest` | Runs the Linux job in a container. |
| `just ci --dryrun --matrix os:ubuntu-latest -P ubuntu-latest=catthehacker/ubuntu:act-latest` | Validates the job plan without executing containers. |
| `act -j check --list` | Inspects the plan when no Docker daemon is available. |

`just ci` wraps `act -j check --concurrent-jobs 1 --artifact-server-path .act-artifacts` and
forwards extra arguments as the words that were given. It takes the [suite lock](#suite-lock) of
the host around `act`, because the container's own lock file is not shared with other runs; a dry
run waits for the lock as well. The recipe refuses a forwarded `--concurrent-jobs` before anything
runs (it needs just 1.29 or newer).

The artifact server path is required for the upload step: GitHub's runners provide the artifact
service, `act` does not, so without it the job ends red after every test has passed. Artifacts land
under `.act-artifacts/<run>/self-test-<os>-<composition>/`.

A dry run checks the workflow plan; it does not prove that either platform's tests passed. Some
act versions still contact Docker during a container dry run; if the daemon is unavailable, record
that limitation and use `act -j check --list` to inspect the plan. Do not substitute `-self-hosted`
for a daemon-free dry run: act 0.2.98 can execute setup actions on the host even with `--dryrun`.

To exercise the same offline test policy directly after dependency and browser setup, prefix the
gate and the self-test with the CI environment:
`ANSWERBACK_TEST_OFFLINE=1 ANSWERBACK_LIVE_LAUNCHD=0 UV_OFFLINE=1 just check`, then the same
prefix with `just self-test`.

## Verify after an upgrade

The kernel-side check after an upgrade is the
[upgrade guide's verification step](ANSWERBACK-UPGRADE.md#skills-service-and-installed-verification).
A provider plugin whose harness caches generated MCP schemas documents its own post-upgrade check
(reload the connection, then call a read-only tool such as `bus_status` from inside the harness).

## What it cannot do yet

- No layer here calls a live model. `just check` and `just self-test` prove the plumbing; a route's
  R1 status comes from the live runs recorded in [VALIDATION.md](../VALIDATION.md).
- `just live-test` prints a checklist and runs nothing. The installed `answerback live-test` tests
  the older Herdr TUI only.
- The install smoke and the upgrade smoke run only when someone runs them; no program checks that
  their record exists before a release.
- The launchd test is opt-in and macOS only. The systemd unit has no live test in the suite.
- The suite lock is per OS user and per machine, and cooperative: a process that closes the lock
  descriptor is outside it.

## See also

- [CONTRIBUTING.md](../CONTRIBUTING.md): checkout setup and what has to pass before you send a change.
- [LIVE-TEST.md](LIVE-TEST.md): the attended checklist, the owned-worker live check and recorded runs.
- [WORKTREE-GUARD-SCENARIO.md](WORKTREE-GUARD-SCENARIO.md): the five-client acceptance project and its one attempt.
- [ADAPTERS.md](ADAPTERS.md#conformance): the conformance contract a provider package runs in its own tests.
- [VALIDATION.md](../VALIDATION.md): what each recorded qualification showed and did not show.
