# Worktree Guard run-evidence contract

Audience: maintainers running a full Worktree Guard scenario attempt and
operating `tools/worktree_guard_evidence.py`.

Every full [Worktree Guard scenario](../../WORKTREE-GUARD-SCENARIO.md) attempt
must leave a versioned evidence record, including failed or interrupted builds.
The software result and the five-client collaboration result are separate
claims.

This public directory contains documentation only. Real run records belong in
a **dedicated private local Git repository with no remote or push**. Native
startup and control traces can contain unrelated configuration metadata even
after credentials are removed; they must not be committed to Answerback's
public source repository.

## Capture layout

```text
private-history/
└── runs/
    ├── index.json
    └── <run-id>/
        └── snapshots/
            └── <capture-id>/
                ├── manifest.json
                ├── outcome.json
                ├── trace.jsonl
                ├── exchanges.json
                ├── artifacts.json
                ├── checks.json
                ├── report.md
                └── sources/
```

A run identity names one fixed scenario, roster and authorization scope.
A capture identity names one observation of that run. Capturing again creates
a new snapshot; it must not overwrite an existing one. `runs/index.json` is
the version-controlled pointer to each run's latest snapshot, while earlier
snapshots remain available.

| File | Purpose |
| --- | --- |
| `manifest.json` | Snapshot identity, source inventory, raw-source hashes, retained-file hashes and capture limitations. |
| `outcome.json` | Recorded project-check outcome and full-scenario acceptance outcome, with their evidence basis. |
| `trace.jsonl` | Redacted native, control and ActorBus events, preserving source-relative path, ordinal, raw-source hash, kind and event data. |
| `exchanges.json` | Recorded review matrices, request/reply IDs and receipt occurrences; native and semantic joins remain explicitly unassessed. |
| `artifacts.json` | Recorded artifact-manifest observations with source provenance; source snapshots and digests are also retained through the manifest. |
| `checks.json` | Saved project-check reports and the status for the current build invocation, when available. |
| `report.md` | Human-readable outcome, passed gates, failed gates and remaining evidence gaps. |
| `sources/` | Redacted source snapshots supporting the derived records. |

### Schema version 1

| Record | Key fields |
| --- | --- |
| `manifest.json` | `schema_version`, `run_id`, `capture_id`, `captured_at`, `profile`, `scenario`, `clients`, `expected_directed_reviews`, `expected_review_messages`, `collector`, `sources`, `selection`, `files`. |
| `manifest.collector` | `source_path`, `raw_source_sha256`, `snapshot_path`, `representation: "redacted view"`; retain the collector implementation used for this capture. |
| `manifest.sources[]` | Sanitized `source_path`, `raw_source_path_sha256`, `raw_source_sha256`, `raw_bytes`, `format`, `snapshot_path`, `read_status`, `redaction_gaps`, `raw_dependencies`. |
| `manifest.files[]` | `path`, `sha256`, `bytes` for retained capsule files. |
| `outcome.json` | `schema_version`, `run_id`, `scenario_acceptance`, `project_checks`, `runner`, `trace`, `cleanup`. |
| `outcome.scenario_acceptance` | `status`, `basis`, `declarations`; preserve the source of reported acceptance. |
| `outcome.project_checks` | `status`, `reports`; independent of the scenario-acceptance status. |
| `outcome.trace` | `completeness`, `gaps`, `cross_source_order`, `counts`, `unique_actor_message_ids`. |
| `exchanges.json` | `schema_version`, `required_matrices`, `matrix_observations`, `recorded_rows`, `derivation_status`, `gaps`, `acceptance_assessed: false`. |
| `runs/index.json` | `schema_version` and `runs`, keyed by run ID; each run has `latest_capture` and `captures`. |
| Each indexed capture | `capture_id`, `captured_at`, `manifest_sha256`, `scenario_acceptance`, `project_checks`, `trace_counts`, `trace_completeness`. |

The implementation's schema remains authoritative. A missing source or field
must remain missing/unknown in the record. A collector reports saved
declarations and observations; it cannot invent an exchange, native final,
artifact authorship or successful check. Message counts alone do not establish
the number of valid directed reviews.

Source observations are sanitized separately from generated schema fields,
identities, counts, file hashes and prior index records. If redacting object keys
would create a collision, the export retains every entry using an explicit
`redacted_object_pairs` representation and records the limitation. Malformed
source data remains available even when a derived summary must be unknown.

Client home and profile trees are explicitly excluded before traversal.
`manifest.selection.excluded_private_tree_names` records this policy. Symlinks
inside selected evidence still cause refusal. JSONL event kinds distinguish
native events, coordinator I/O, project-build I/O and other recorded observations.

The first utility version performs ID-reference joins for review requests,
replies and receipt occurrences. It does not independently validate native
tool attribution, literal artifact excerpts or the substance of peer answers.
`scenario_acceptance` reports explicit source declarations and retains their
basis; it does not certify them.

`project_checks.status` applies to a matching report from the current `build`
invocation and its observed exit. Plain `capture` retains historical reports
but leaves that current-build status `unknown`. Filename order cannot prove
which historical report describes the present project files.

## What “full trace” must preserve

The record needs the complete captured sequence through preparation, enrollment,
assignment, peer review, corrections, package validation, finish and cleanup.
It must retain the source events needed to examine a failure, rather than only
the final summary or a short terminal tail.

- **Inputs:** contract, protocol, scope, exact roster, baseline/inherited files,
  bridge revision and working changes, selected routes and native versions.
- **Native identity:** attributable process/session/prompt/tool identifiers and
  their actor/frontend/run binding.
- **Messages:** original addressed assignments and actor operations, transport
  observations, read offers, acknowledgements, correlated replies, corrections
  and finish/result messages.
- **Coordinator controls:** every interactive controller input and stdout/stderr
  event, with source ordinal and timestamp, captured during execution rather
  than reconstructed from terminal history.
- **Permissions:** every native request round, one-time decision and response,
  with the relevant native IDs. A repeated row ID in another consent round
  must not erase the later event.
- **Artifacts:** actual source bytes or retained content snapshots, their
  hashes, owner and baseline provenance; reviews must join the correct version.
- **Verification:** commands, stdout/stderr, exit codes, timing, pinned verifier
  identity and produced wheel identity when those observations exist.
- **Lifecycle:** native final/exit, observer failure, cancellation acknowledgement,
  confirmed cancellation and owned-resource cleanup as separate observations.

Preserve source order and every occurrence. Similar timestamps, duplicated
broker delivery or reused native row IDs do not establish duplicate semantic
events. Preserve both the original evidence and later reconciliation.
An observer timeout can occur while the model keeps running.

Capture coverage must identify absent, unreadable, truncated or unsupported
sources. A snapshot collected after an incident cannot reconstruct events that
were never retained. Such a retrospective record remains useful, but cannot
claim a complete earlier trace.
The default capture bounds are 64 MiB per source and 512 MiB across selected
sources, configurable through `--max-source-bytes` and `--max-total-bytes`.
Exceeding a bound fails capture with a nonzero result. Inspect `read_status`,
`selection` and `trace.gaps` on a successful capsule: the completeness value
`complete_for_selected_sources` describes the selected inventory, not proof of
every possible native event. Cross-source causal order is not inferred.

## Private source and redaction

Raw evidence stays in its private run directory. The history capsule preserves
its source-relative reference and SHA-256 alongside the redacted copy.
The original and redacted representations therefore have different byte
identities; the manifest must distinguish them.

Selected source files must have exactly one hard link, checked on the opened
descriptor both before and after reading. A second name could otherwise admit
an excluded credential file as ordinary project text. A hard-linked source
fails capture with `hard_linked_source_refused`; it is not silently omitted.
This includes run or project trees restored from a hard-link backup, any gathered
file given a second name anywhere on the filesystem, and a copy of this tool
installed by a hard-linking installer. Manifests, plans, the history index and
actor database/WAL reads use the same rule.

Recognized credential fields and token patterns, including `receipt_nonce`,
are redacted. This is not a guarantee of detecting arbitrary unlabeled secrets.
Stable hash-based placeholders preserve joins across repeated occurrences
without exposing the recognized value. Redaction must be explicit and must
not silently remove a failed event or merge two distinct native identities.

Redaction does not make a native trace suitable for a public repository.
Project text, tool output and configuration metadata still require the private
history boundary. Only a deliberately reviewed, bounded summary belongs in
public [VALIDATION.md](../../../VALIDATION.md).

## Supervise, build and capture

The capture entry point is
[`tools/worktree_guard_evidence.py`](../../../tools/worktree_guard_evidence.py).
`supervise`, `build` and `capture` require the private run root and history repository
explicitly; no default may silently select the normal bridge or public Git
checkout.

The prepared run root must include `run-plan.json` with the run identity and
project path, plus the frozen `source-snapshot/manifest.json` and its referenced
files. The history path must already be a clean, standalone Git repository
owned by the current user with owner-only directory permissions. Remotes,
linked-worktree metadata and history nested inside the run, project or
collector source repository are refused.

Before `build` or `supervise`, the run root and `source-snapshot` directory
must also be owned by the current effective user with no group or other
permission bits. Prepare these directories with mode `0700`. The tool refuses
unsafe metadata with `run_root_must_be_private_and_owned` before reserving
runner output or starting the interpreter; it does not repair permissions.
Missing paths and paths that are regular files instead of directories report
`source_read_failed`.

For execution, `source-snapshot` must contain exactly the regular
`manifest.json` file, the regular files it lists, and directories needed as parents
of those listed files. Unlisted files, `__pycache__`, empty directories, symbolic
links and other file types are refused with `source_snapshot_unlisted_entry`.
The check walks without following links, before reserving runner output and again
after execution. A post-run addition is recorded in `runner.error` and the capture,
just as a post-run digest mismatch is. `capture` keeps its existing file selection
and starts no interpreter.

**Upgrade:** a run root prepared before this hardening needs mode `0700` on both
the run root and `source-snapshot`, and no unlisted entries in `source-snapshot`
(including an old `__pycache__` or Finder `.DS_Store`).

| Operation | Boundary recorded |
| --- | --- |
| `supervise` | Runs the pinned scenario controller interactively; journals every forwarded coordinator input and stdout/stderr event with ordinal/time. |
| `build` | Runs the pinned project verifier and records its invocation and outcome, including installed-project checks. |
| `capture` | Imports/checkpoints evidence already present in the run directory; it does not reconstruct missing earlier events. |

Use `supervise` from the beginning of each new full scenario attempt. It passes
the human/coordinator's JSON input to the selected controller and records
the control stream as it happens. The wrapper must not replay a saved control
journal or automatically answer permission prompts. The scenario controller
retains its own authorization and native lifecycle rules.

Run these commands from the Answerback source checkout, supplying the intended
private paths:

```sh
uv run python tools/worktree_guard_evidence.py supervise --run-root /absolute/path/to/private-run --history-repo /absolute/path/to/private-history --python /absolute/path/to/run-environment/bin/python

uv run python tools/worktree_guard_evidence.py build --run-root /absolute/path/to/private-run --history-repo /absolute/path/to/private-history --python /absolute/path/to/run-environment/bin/python --check-timeout 180 --timeout 600

uv run python tools/worktree_guard_evidence.py capture --run-root /absolute/path/to/private-run --history-repo /absolute/path/to/private-history
```

`capture` is the existing-run checkpoint/import operation. An optional
`--capture-id` supplies a new capture identity; omitting it lets the tool choose
one. `--python` selects the controller/verifier interpreter and defaults to
the current interpreter. Build's default timeout is 600 seconds; the
default `--check-timeout` is 180 seconds per verifier command. The interactive
controller timeout is optional.

Controller/verifier pins come from
`<run-root>/source-snapshot/manifest.json`, whose `files` entries contain
`path` and `sha256`. `supervise` requires the recorded `run_controller.py`;
`build` requires the recorded `verify_project.py`. Use the run's frozen
manifest and actual source bytes, rather than inventing a digest or silently
using a newer verifier. The collector retains the verified entry script bytes
from `<run-root>/source-snapshot/` and gives them to the selected interpreter
through an open temporary-file descriptor instead of reopening the entry script.
On Linux, `TemporaryFile` uses a file with no directory entry when `O_TMPFILE`
is supported; otherwise it creates a random name and immediately unlinks it.
macOS uses that create-then-unlink fallback. A process of the same user that opens
the brief-lived name can retain a writable handle; on Linux, descriptor access
through `/proc` is another remaining same-user route. Retaining the bytes removes
the original script-path substitution gap, but does not isolate the temporary
inode from another process of the same user.

The interpreter preserves the script's `__file__`, `__main__`, arguments and
sibling-import directory. Interactive stdin remains available to the controller;
source bytes are not placed in the input journal or command arguments. The
runner's `source_sha256` identifies the retained bytes, `argv` records the
logical script invocation, and `source_transport` records
`verified_bytes_via_descriptor`. A post-run pin check still detects later
source changes and records the mismatch. Tracebacks include the bootstrap frames.
The bootstrap starts with `-B` and disables bytecode writing. The collector also
sets `PYTHONDONTWRITEBYTECODE=1` for the child, regardless of the operator's
environment. Ordinary Python subprocesses inherit that setting; multiprocessing
spawn workers inherit the `-B` interpreter flag, including when `-E` or `-I`
causes them to ignore Python environment settings. This keeps their imports of
listed files from adding `__pycache__` to the snapshot.

A child that removes the setting, explicitly enables bytecode writing, or starts
another interpreter with settings that bypass suppression can still create a
cache. The post-run check then records `source_snapshot_unlisted_entry` and exits
2, even if the controller exited 0; the next execution is refused at preflight.
Correct the child launch to preserve bytecode suppression, delete the generated
cache entries from the snapshot, and run again with a new capture ID. Cache
directories remain forbidden; the collector does not exempt or relocate them.

**Remaining import limit:** a listed sibling is still loaded by path by Python's
import system after its digest check. A process of the same user can replace it
after preflight and restore it before the post-run check without detection.
The membership checks also cannot detect an unlisted file created and removed
entirely between those checks. Spawned Python workers may reload `__file__` by
path. Serving listed siblings from retained bytes through an import hook is outside
this change and needs the real controller and verifier sources to verify compatibility.

The child inherits only `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ` and
`TMPDIR` when present. The collector sets `ANSWERBACK_ACCEPTANCE_ROOT` to the
selected run root and fixes `PYTHONDONTWRITEBYTECODE` to `1`. The latter cannot
be changed through the operator's environment or `--pass-env`. Other ambient
variables, including cloud credentials, SSH-agent sockets and `PYTHONPATH`, are not
forwarded. These are hardening measures within the operator's account; the
selected interpreter, home directory and executable search path remain trusted.

`build` and `supervise` also accept repeatable `--pass-env NAME`, for example
`--pass-env UV_CACHE_DIR --pass-env TERM`. Names must match `[A-Za-z_][A-Za-z0-9_]*`;
names starting with `PYTHON`, `LD_` or `DYLD_`, plus `__PYVENV_LAUNCHER__` and
`ANSWERBACK_ACCEPTANCE_ROOT`, are refused with `pass_env_name_refused`.
Every other name is the operator's explicit decision and is recorded.
Prefix selection, wildcards and files of names are not supported. Unset variables
are omitted. The runner records sorted, unique
`environment_passed` names (including defaults and both collector-set names) and
`environment_requested_unset` names. Those fields contain no values. Child output
is still journalled, so the option does not prevent a child from printing a passed
value. Without this option, the default environment selection above applies.
Git operations and history-repository hooks still inherit the collector's full
environment; this option applies only to the controller/verifier child.

Supervision writes its private raw journal to
`orchestration/<capture-id>/events.jsonl`. Each coordinator input and
stdout/stderr event records `ordinal`, `observed_at`, `monotonic_ns`, `stream`,
`bytes`, `sha256` and `text`. Received, delivered and undelivered input remain
distinct as `stdin_received`, `stdin` and `stdin_undelivered`; output streams
are `stdout` and `stderr`. These records are I/O chunks, which need not align
with JSON command or line boundaries. Text uses surrogate-escape decoding to preserve non-UTF-8 bytes;
byte count and hash identify the original event content. Build output is stored
under `project-builds/<capture-id>/`. Both locations are beneath the selected
private run root and become sources for the capsule.

Immutable capsule payloads and `runner.json` are created exclusively with mode
`0600`. Their writer creates missing directories with mode `0700` and traverses
each parent using descriptor-relative `O_NOFOLLOW` opens. A symlink substituted
after preflight cannot redirect those writes. Path validation reports
`symlink_refused`; filesystem failures during creation report
`evidence_write_failed`.

After execution begins, the finalization paths for both `supervise` and `build`
attempt to capture and commit a new snapshot even when the controller or
verifier fails. Preflight rejection runs neither program. A failed capture
returns nonzero; it cannot report a successful fully recorded attempt. Inspect
the tool's `--help` before use to confirm the installed command contract.

Capture observes retained evidence; it must not resume clients, answer
permissions, repair source files or alter an earlier outcome. The Git commit
records a local snapshot and is not an external publication.

## Compare runs without moving the goalposts

Read the immutable reports and structured gate results, using the index to
find the latest capture for each run. Compare:

| Dimension | Why it matters |
| --- | --- |
| Scenario/contract/roster digests | A seven-client run and a five-client run have different denominators. |
| Bridge/native/verifier versions and dirty source | A changed observer or adapter can change evidence collection as well as execution. |
| Attributed contributions and inherited versions | Reusing earlier code must not earn fresh authoring credit. |
| Directed exchanges and missing native joins | More queued messages can coexist with zero valid completed reviews. |
| Independent project checks and wheel identity | Tests from one source snapshot do not qualify another artifact. |
| Finish/final/cleanup evidence | Working source does not establish completed orchestration. |
| Capture coverage and redaction omissions | A cleaner summary may reflect less evidence, rather than fewer failures. |

Keep `FAILED_INCOMPLETE` distinct from a failed software test and from an
unverified observation. A later source fix, passing test or fuller capture may
explain a failure; it cannot retroactively make the original run successful.
The first utility version has no `summary` or `compare` subcommand; the
version-controlled index and immutable reports are the comparison interface.

## Current adoption

CLI-1 remains `FAILED_INCOMPLETE`, with partial source and native evidence
retained. It predates `supervise`: native, ActorBus and control-result records
exist, but there is no complete dedicated coordinator-input stream. A later
capture must report this legacy gap rather than claim complete all-layer trace.

The reusable supervise/build/capture utility and its
[synthetic tests](../../../tests/test_worktree_guard_evidence.py) now exist.
The three command interfaces and schema were checked against its source.
Three real CLI-1 captures have been committed locally: one of the closed run
and two wrapping isolated installed-package checks. All 432, 488 and 546
retained payload-file hashes were independently verified, together with every
index-to-manifest reference. The installed
package passed 155 tests, but its complete project check failed on Ruff;
that failure was captured and committed automatically. All three records retain
the missing coordinator-input journal.

Later records preserve multiple source copies of the same roster. There are
20 unique required directed reviews; repeated source copies do not increase
that denominator. Native and semantic exchange certification remain separate
from the collector's ID-reference projections.

Independent synthetic checks reproduced and corrected an earlier defect where
very short credential values corrupted generated metadata. The corrected
collector keeps those metadata fields exact while redacting source observations.
The synthetic reproduction and the independently verified real records are
separate evidence. Future full runs must use the reviewed collector revision
and its recorded source hash.

The corrected collector was also exercised on the real project: it recorded
the repeated failing build in a new commit and left the earlier records intact.

Later source review found a coordinator fixture hook override that violates the
scenario contract. The private history adds a separate annotation tied to the
affected capture manifests. Annotations supplement immutable captures; they
must not overwrite an earlier result or silently turn it into a pass.
