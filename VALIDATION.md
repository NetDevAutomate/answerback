# Validation: what has been shown, and what has not

Every test and qualification run that the project stands on is below, including the ones that
failed. A line here means a run happened on a named date, version and commit, and says what that
run showed and what it did not. It is not a statement of current support: the support table is in
[docs/HARNESSES.md](docs/HARNESSES.md), and the record for each row is here.

It is for someone checking the project's claims against its evidence, who knows the terms receipt,
owned worker and route ([GLOSSARY.md](docs/GLOSSARY.md)) from the [README](README.md).

## How to read this record

Each qualification below names its date, the version under test, the harness versions and the
record it left. A committed record is a path in this repository that a reader can open. Where a
run left no committed record, the entry says so. Nothing recorded here was re-run on a later
version unless the entry says it was.

In one table, newest first:

| Date | Version and commit | Showed | Did not show | Record |
| --- | --- | --- | --- | --- |
| 24 November 2026 | 0.14.0+internal at `4a03461b`; cancel at `5c8995a5` | Kiro CLI 3.5.0 and Claude Code 2.1.330 passed the owned-worker gate on macOS and Linux | Grok Build and Codex CLI were not in this gate | No committed record in the public tree; see below |
| 20 November 2026 | 0.12.0+internal at `5c135ad` | One live model turn through an owned worker for Codex CLI 0.170.0, Claude Code 2.1.325 and Kiro CLI 3.5.0 | Grok Build 1.0.89 did not start; no permission was answered | [the recorded run](docs/LIVE-TEST.md#recorded-run-20-november-2026) |
| 14 to 15 November 2026 | 0.10.0 public candidate at `eb7664768f36bed41fdaec7bbf023b1282423a02` | The source, regression, lint, package, transport and monitor gates; live round trips with Codex CLI 0.169.0 and Claude Code 2.1.322 | Grok Build 1.0.77 actor-tool execution; whole-tree type checking | [Preserved public baseline](#preserved-public-baseline) |
| CLI-1, mid-November 2026 | the 0.10.0 candidate line; commit not recorded | Five native prompts admitted, roster sealed, 155 tests passed in a later wheel build | The five-client package was not delivered: `FAILED_INCOMPLETE` | [the CLI-1 record](docs/WORKTREE-GUARD-SCENARIO.md#cli-1-result-and-retained-limitations) |

The 24 November gate covered, for Kiro CLI, a no-tool task, a permission-blocked read then
answered, a repeat under the same request ID and a cancel; for Claude Code, round trip and repeat
at `4a03461b` and, at `5c8995a5`, cancellation while permission-blocked and while a foreground
Bash was running. It also showed that a wrapper-injected credential is invisible to an owned
worker: the bridge forwards only the allow-listed keys from its own environment. Its redacted
receipts, `doctor.json`, transcripts and a `RESULT.md` per run name the maintainers' hosts and home
directories, so they are kept in the maintainers' private archive and not in the public tree.

The 20 November run started each worker from a bridge started with `start --standalone`, and a
repeat returned the same receipt; the Claude run with the machine's normal home was refused
(settings file over 256 KiB, CLI older than 2.1.322). Its record is a table of single tasks, not a
qualification. At the 0.10.0 candidate, Grok Build 1.0.77 passed generation, permission handling
and cancellation, and actor-tool execution was blocked in an untrusted project; the exact artifact
hashes are in the external build receipt. The CLI-1 captures are in a private history with no public
copy, and the exact date is not recorded here.

Versions 0.11.0+internal to 0.16.0+internal shipped through their own merge gates, recorded in
[CHANGELOG.md](CHANGELOG.md) and in the project's private ledger; no end-to-end qualification of
those versions is recorded below. The combined source tree prints `0.16.0+internal` from
`answerback --version`.

## What it cannot do yet

- **Several agents do not yet deliver one package together.** The five-kind attempt (CLI-1) is on
  record as `FAILED_INCOMPLETE`. Single hand-offs with a receipt, on one machine, are what works.
- **Live-model proof is uneven.** The automated tests and the scripted self-test pass on the current
  code, and neither calls a live model. Codex CLI, Claude Code and Kiro CLI have a live model turn
  on record through an owned worker; Grok Build has a live turn on record only at the 0.10.0
  candidate, in a trusted temporary folder, and did not start in the run of 20 November 2026.
- **The actor protocol is proven with scripted participants.** The two-STDIO-MCP round trip passed,
  also from an installed wheel; the live review exchanges that the Worktree Guard scenario requires
  did not.
- **One machine, one coordinator.** No run here used a shared broker or a second machine as the
  recipient. Remote `hc` sends are Experimental in the support table.
- **No upgrade gate between public releases.** There is no earlier public release, so the authentic
  release-to-release upgrade and rollback gate is still to come; results from development builds
  do not supply that baseline.

## Preserved public baseline

The public candidate passed the source and package gates below on 14 November 2026. Follow-up live
checks through 15 November verified Claude actor-tool execution and two Codex CLI/Desktop exchange
chains, but the full seven-client project has not passed. Grok's successful permission probe used a
trusted temporary folder; the later untrusted project could not invoke actor tools.

Exact artifact hashes are recorded in the external build qualification receipt; embedding a
package's own digest inside it would be circular. The acceptance scope at that time was the
[five-CLI Worktree Guard project](docs/WORKTREE-GUARD-SCENARIO.md); CLI-1 ended `FAILED_INCOMPLETE`.
The earlier seven-client figures under [Actor messaging qualification](#actor-messaging-qualification)
remain historical evidence.

| Gate | Status at the 0.10.0 candidate | Evidence |
| --- | --- | --- |
| Public source boundary | Passed | Source/documentation review and wheel/sdist private-identifier checks |
| Full regression suite | Passed | 2105 passed, 1 skipped of 2106 collected in 345.20 s on `eb7664768f36bed41fdaec7bbf023b1282423a02`; see below |
| Lint and formatting | Passed | Ruff across source, tests and scripts; 234 Python files formatted |
| Types | Scoped pass | 78 canonical modules pass; nine inherited diagnostics remain in three unchanged files |
| Package and compatibility | Passed | Installed smoke, entry points, module identities, reinstall, optional Claude SDK extra and isolated installed-wheel actor round trip |
| Durable transport | Passed | Installed MCP to real local NATS to coordinator to fixture owner, with restart and request-identity checks |
| Installed monitor | Passed | Browser, authenticated assets and completed task projection |
| Native Codex CLI | Passed for bounded scenario | Real generation, exact terminal IDs, owned-session resume and remembered-marker response |
| Native Claude Code | Generation, actor tools and cancellation passed for bounded scenarios | Native receive/ack calls and their results matched the actor ledger under explicit run-scoped human delegation; cancellation, idle and process cleanup were observed |
| Native Grok Build | Generation and permission/cancellation path passed; actor execution blocked | An explicitly trusted temporary project reached permission handling; the later untrusted worktree denied actor calls before an approval callback |
| Five-CLI Worktree Guard project | Failed/incomplete (CLI-1) | Integrated project delivery, 20 directed peer reviews / 40 review messages, native read/ack/reply evidence and matching final results; see [the scenario](docs/WORKTREE-GUARD-SCENARIO.md) |

The regression run was a sandbox run with a private pytest basetemp: the launchd opt-in test is
skipped by design, and on the default basetemp `just check` also skips the 40 evidence-reader tests
([docs/TESTING-HARNESSES.md](docs/TESTING-HARNESSES.md#running-the-suite)).

The remaining type diagnostics at the candidate were in `harness.py`, `live_test.py` and the
legacy namespace's `__init__.py`. This is not a claim that whole-tree type checking passes.

Native protocol qualification used Codex CLI 0.169.0, Claude Code 2.1.322 with the published
Claude Agent SDK 0.2.182, and Grok Build 1.0.77. Those are the versions of that qualification, not
current releases. The bridge itself requires Claude Code 2.1.322 or newer and sets no minimum
version for Codex CLI or Grok Build.

Independent adversarial review corrected terminal contradictions, late approvals, missing response
text, published-SDK metadata loss, cleanup races and lost recovery IDs. Claude and Grok workers
start fresh sessions until resume/history preservation is separately qualified. Initial marker
probes made no approval grants. The later acceptance supervisor used explicit human delegation for
one-time decisions on four actor tools in one run; commands, files, other tools, persistent grants
and later runs were excluded.

Three defects the live project exposed, and what fixed them:

- The first Claude model probe failed because the isolated worker omitted its native provider
  settings. The corrected worker forwards only allow-listed provider/auth/model environment
  settings, preserving process-environment precedence and excluding ambient execution policy. Its
  real no-tools model probe then completed.
- Hook `prompt_id` is a native identity distinct from the SDK user-message UUID. The corrected guard
  joins them through an attributed native tool proposal, then verifies each hook's tool ID, name,
  input and native prompt. Contradictory UUID declarations and permission answers after a latched
  protocol failure are rejected. A fresh read-only MCP probe passed that guard, reached normal
  permission handling and was cancelled without answering, as the probe required.
- Grok's direct Bedrock profile could not obtain a token from its configured helper and returned
  HTTP 401. A separate clean profile using the existing local gateway completed the probe through
  the same native Grok Build client; the key was resolved at invocation and held only in memory
  and the owned child environment. Grok imports Claude permission policy independently of its five
  compatibility scan switches: in an explicitly trusted disposable project, the project-local
  `permissions.defaultMode: "default"` enabled the normal human request path. The probe received
  one-time options and was cancelled without granting permission. Neither this observation nor an
  ended prompt proves that the requested model tool executed.

## What the tests must establish

- A command accepted locally survives producer, broker and coordinator restarts.
- Duplicate delivery reuses its existing task; uncertainty never authorizes replay.
- A changed recipient, project or routing configuration refuses admission.
- Explicit permission requests stay with the human and the execution owner.
- A completed receipt cites the matching native turn or required worker callback.
- Maintenance protects unfinished work and pending publications, and retains request identity
  after eligible result content is pruned.
- Installation preserves compatible configuration, receipts, aliases and restrictions; unsupported
  configuration is refused rather than silently altered.

Scripted native processes test the adapter contract. Real broker and browser checks test transport
and presentation. A live model round trip establishes only the selected harness, version and
scenario. None is a substitute for the others.

## Recorded limits of the 0.10.0 qualification

This is the historical record of the qualification named above; Desktop references record the
routes tested then and do not state current R1 support.

That preview supported one local machine and one coordinator and claimed no remote or highly
available deployment. An MCP connection and its heartbeat are client availability evidence; they
do not establish an incoming chat adapter, agent readiness, acknowledgement by a model or task
completion. Core and Crew identity checks are observations around delivery, not atomic native
generation-conditional sends.

Codex Desktop/IDE queue acceptance does not guarantee an unloaded chat wakes. Existing CLI
conversations and new owned worker sessions must be qualified separately. Standalone Claude Desktop
has no incoming adapter in this public core.

## Actor messaging qualification

The actor mailbox has a real two-STDIO-MCP to local RPC to NATS request/read/acknowledgement/reply
test, plus transport restart and lost-acknowledgement tests. The same round trip also passed from
an isolated installed wheel. This is protocol evidence. The earlier seven-client `worktree-guard`
attempts and their 42 directed model-driven review requirement did not pass. The current five-CLI
requirement is 20 directed reviews and 40 review messages.

The first project attempt enrolled all seven clients and delivered seven assignments, but completed
zero directed peer reviews. It was explicitly ended as a diagnostic run after native tool-path
failures. Crew injected internal MCP servers beyond the requested scope; its next-run specification
excludes them. A Desktop code-mode connection also did not survive the boundary between bootstrap
and work turns. These observations are not successful project delivery, and unit or packaging
tests do not substitute for the required live run.

The next run delivered seven assignments and preserved two Codex-authored modules. Its ledger
contains 12 review requests and two replies. Independent native trace joins verified two Codex
CLI/Desktop send/read/acknowledgement/reply chains; neither pair passed the literal-excerpt
requirement because its excerpt was a prose summary. Seventy successful actor-operation
observations included staging reads, so they are not seventy completed work exchanges.

That run ended as a diagnostic failure. Native Grok inspection confirmed its exact worktree was
untrusted; documented folder-trust behaviour explains why the local normal-ask override did not
replace the existing global policy. Kiro's request metadata exposed a parser gap in the approval
supervisor. Claude Code and Crew executed actor operations, then waited for file permissions
outside the delegated scope; those writes were cancelled.

Claude Desktop only completed its no-tool bootstrap before the work-turn UI action timed out. The
Crew supervisor recorded 31 process-sampling errors; its final retained-process set was empty and
its listener closed, but historical sampling coverage remains partial. Native histories, messages,
source snapshots and the partial project were retained; no complete project or exhaustive cleanup
claim is made for that run.

## Five-CLI project acceptance: CLI-1

The [Worktree Guard scenario](docs/WORKTREE-GUARD-SCENARIO.md) replaces the seven-client acceptance
scope with Kiro CLI, KiroCrew, Codex CLI, Claude Code and Grok Build CLI. It requires a delivered,
independently tested Python package and a complete attributable trace of peer requests, reads,
acknowledgements, replies and final results.

CLI-1 admitted all five native prompts and sealed the actor roster, but ended
**`FAILED_INCOMPLETE`**. A Claude foreground `task_started` event failed its hook path and the task
ended without the required finish/result exchange. An active-budget observer failure also occurred
while a native model continued, and Kiro consent handling exposed a multiple-round cache defect.
These observations do not establish successful project delivery or termination of every native
process.

Partial source, message and native evidence are retained. The
[versioned run-evidence contract](docs/evidence/worktree-guard/README.md) requires a separate
record for each full build attempt, including failures. CLI-1 predates the interactive `supervise`
wrapper and lacks a complete dedicated coordinator-input journal; retained native, ActorBus and
control-result evidence cannot fill that historical gap. Capture implementation and any later
repair verification must have their own evidence; they cannot rewrite CLI-1's outcome.

Three real CLI-1 captures are committed in a separate private Git history with no remote; there
is no committed record of them in this repository. Their 432, 488 and 546 retained payload hashes
and index references were independently verified. The build captures record an isolated wheel
build, installation and **155 passing tests**, followed by a failing Ruff check. It preserves the
full failed result, rather than reporting test success as project acceptance. A separate read-only
probe showed that six of Ruff's eight diagnostics came from import classification after relocating
the tests; an unused participant test import and an overlong coordinator test line remained.

A subsequent source check found another qualification gap: the frozen coordinator test helper
overrides `core.hooksPath` with `os.devnull` for its synthetic Git commands. That violates the
scenario contract. Both executed test copies match that frozen source; their passing assertions
cannot qualify the whole acceptance procedure. The private history contains a separate
digest-bound annotation, while the prospective verifier removes the override. This finding does
not describe the separate Answerback source commits, whose mandatory security hooks ran normally.

## See also

- [docs/HARNESSES.md](docs/HARNESSES.md): the support table these records stand behind.
- [docs/LIVE-TEST.md](docs/LIVE-TEST.md): how a live check is run and the dated runs.
- [docs/WORKTREE-GUARD-SCENARIO.md](docs/WORKTREE-GUARD-SCENARIO.md): the five-client scenario and its one attempt.
- [docs/TESTING-HARNESSES.md](docs/TESTING-HARNESSES.md): the gates a change passes before any of this.
