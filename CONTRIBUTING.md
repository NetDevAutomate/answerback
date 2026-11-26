# Contributing to Answerback

A change to Answerback is a commit that passes `just check` and `just self-test` from a source
checkout, with a test and a changelog line beside it. Below: the checkout setup, the checks that
have to pass before you send a change, and the conventions in force. If you only want to run the
tool, start at [README.md](README.md); if you are adding a provider, go straight to
[docs/ADAPTERS.md](docs/ADAPTERS.md#adding-a-provider).

There is no package on PyPI yet; install from the source tree. Send a change, or the problem you
found, to the repository you cloned from (the clone URL is in [README.md](README.md)).

## Set up

You need [`uv`](https://docs.astral.sh/uv/) and a Python in the supported range (`>=3.11,<3.16`,
`pyproject.toml`). From your checkout:

```sh
uv sync --locked --all-extras
uv run playwright install chromium
```

```text
Resolved 49 packages in 1ms
Checked 46 packages in 0.26ms
```

The two lines are `uv sync` on an environment that is already current; a first sync downloads and
installs the same 49 packages. `--all-extras` installs the `bus`, `claude` and `debug` extras beside
the core dependencies (`pyproject.toml`). Chromium is for the test suite's web-monitor fixtures,
which need it and are not skipped without it.

This is the public composition: the kernel and its own providers, no plugin package. A provider
plugin lives in a repository of its own and gates its composition there ([the two
compositions](docs/TESTING-HARNESSES.md#the-two-compositions)).

## Checks before you send a change

| Layer | Command | Covers | Needs an account |
| --- | --- | --- | --- |
| Regression gate | `just check` | Composition check, Ruff lint and format, `ty check src`, pytest, then the public-tree assertion, under the suite lock | No |
| Scripted protocols | `just self-test` | Real local RPC, CLI, MCP STDIO, ACP and SQLite receipts, with scripted peers | No |
| Attended acceptance | `just live-test` | Prints the [attended hc checklist](docs/LIVE-TEST.md#attended-hc-acceptance-integration-build); an owner runs it against real harness sessions | Yes |

`.github/workflows/ci.yml` runs the gate and `just self-test` on every push and pull request, on
Ubuntu and macOS. Run both before sending a change. The evidence and
prerequisites behind each layer are in
[docs/TESTING-HARNESSES.md](docs/TESTING-HARNESSES.md#choose-a-test-layer).

## Conventions already in force

- Ruff lints and formats at a 100-column line length (`pyproject.toml`); `ty` type-checks `src`,
  not `tests` (`justfile`). Functions carry type hints throughout the codebase. Keep adding them.
- Add or update tests beside a code change, under `tests/`.
- Commit messages follow `type(scope): what changed`, lower case, present tense. Two examples from
  this repository's own history: `feat(tracing): add timed local debug scopes and redacted drawer`
  and `fix(ci): remove installed broker dependency from offline tests`.
- Add a changelog entry under `## Unreleased` in `CHANGELOG.md` in the same change.

## Documentation changes

Every command, flag, path and number you write must check out against `--help` output or the
source, not against another doc. Write commands as the installed `answerback` and `agent-bus`
commands, without a `uv run` prefix.

This repository names no internal provider. An internal provider package is called exactly that,
and nothing about its commands, paths or behaviour appears anywhere in the tracked tree. The gate
for this is the public-tree assertion, the last step of `just check`, run over every tracked file:

```sh
just assert-public-tree
```

```text
summary: tracked=706 scanned=692 skipped_binary=14 skipped_dir=0 content_hits=0 path_hits=0 binary_hits=0 allowlisted=0 hits=0
```

The summary line is the report's last line; a hit would be listed above it with its file and line.
The tree passes only at `hits=0`. Text that applies only to the internal provider goes into that
package's own documentation, not fenced inside a public page.

Tests pin some passages word for word. Change one only together with its test:

| Passage | Where | Test |
| --- | --- | --- |
| The `task_complete` caller policy sentence, with `complete_worker` and "not implemented" | `README.md` (the paragraph after the OS-user boundary), `docs/ADAPTERS.md` (invariant 8 under "Receipt and failure invariants") | `tests/test_docs_policy.py` |
| The R1 support table, byte-identical | `README.md`, `docs/HARNESSES.md`, `docs/RECIPIENTS.md` | `tests/test_docs_policy.py` |
| The uv receipt path, both `~/.local/bin` paths and `Run \`agent-bus stop\` before \`answerback uninstall --remove-tool\`.` | `docs/TROUBLESHOOTING.md` ("The optional bus"), `README.md` | `tests/test_docs_policy.py` |
| The remote trust statement, verbatim; the remote result-file checklist | `README.md`, `SECURITY.md`, `docs/RECIPIENTS.md`, `docs/HARNESSES.md`; `docs/LIVE-TEST.md` | `tests/test_core_result_provenance.py` |
| The Kiro v3/v2 MCP-readiness sentences and the managed-route paragraph | `docs/RECIPIENTS.md`, `docs/HARNESSES.md` | `tests/test_workers.py` |
| "The Kiro agent file": the refused list and what the write does not defend against; the "Exit codes" heading | `docs/NATIVE-CLI.md` | `tests/test_kiro_agent.py` |
| The count, precheck and aid SQL | `docs/MAINTENANCE.md` | `tests/test_receipt_versions.py` |
| The 8192-character scope bound, as a number | `docs/TRACING.md` | `tests/test_debug_scope_bound.py` |
| The suite-lock sentence "One that stayed in the command's process group can still get the signals that the wrapper passes on, until the wrapper has waited for the command." | `docs/TESTING-HARNESSES.md` ("What the lock does not cover") | `tests/test_suite_lock.py:1838-1840`, which quotes it in its comment |
| The `ANSWERBACK_LIVE_LAUNCHD=1` opt-in sentence | `docs/TESTING-HARNESSES.md` ("Running the suite") | `tests/test_runtime.py:478-482`, whose skip reason points at it |

Run `uv run pytest tests/test_docs_policy.py -q` and the test in the row you touched before you
commit a doc change.

## Adding or changing a provider plugin

A provider plugin is how the bridge discovers, binds and submits work to one kind of recipient.
[docs/ADAPTERS.md](docs/ADAPTERS.md#adding-a-provider) has the adapter contract, the required
operations and the steps for adding one; [docs/PROVIDER-TEMPLATE.md](docs/PROVIDER-TEMPLATE.md) has the
package layout and the conformance test a separate package runs.

## Security problems

Report a security problem by following [SECURITY.md](SECURITY.md) directly, not as a normal change
or a comment on one.

## Licence

Contributions are accepted under the Apache License, Version 2.0. Unless you state otherwise in
writing, anything you submit for inclusion is licensed under the same terms as the rest of the
project, with no additional conditions (`LICENSE`, section 5, "Submission of Contributions").

## See also

- [README.md](README.md): install, run and the documentation map.
- [docs/TESTING-HARNESSES.md](docs/TESTING-HARNESSES.md): full detail on each test layer and the suite lock.
- [docs/ADAPTERS.md](docs/ADAPTERS.md): the provider plugin contract.
- [SECURITY.md](SECURITY.md): the trust model and how to report a problem.
