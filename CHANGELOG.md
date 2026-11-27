# Changelog

Notable changes to Answerback, most recent first. This file starts at the repository split;
the history of 0.16.0 and earlier, and the task ledger its codes (such as `T-252`) refer
to, stay in the maintainers' private archive.

## Unreleased

### Fixed

- **Reading a Crew chat history could block the service for seconds, long enough for `status`
  to time out in the fleet writer and the panels to show "offline".** The Crew provider removes
  every credential it holds (link tokens and session cookies, a set that grows at each hourly login) from every
  response it decodes. It did that on the event loop, once per string of the decoded tree and
  once per credential, re-quoting the credential each time: a 200-message history of a few
  megabytes with forty credentials blocked the loop for about a second in a reproduction, for
  every Crew chat at every tracing scan; the CPU profile of the work Mac's 2-7 second stalls
  matches this path and is not otherwise confirmed. The redaction now runs in a worker thread
  with a snapshot of the credentials, and skips the walk when the raw text cannot hold an echo
  (every credential is made of characters a JSON encoder writes literally, and the text has no
  `\u` escape, no `%` and no credential in it); otherwise the decoded tree is walked. What is
  removed changed: a string or key that holds a credential, raw or after one URL-decoding pass
  (so in any percent-encoded spelling, where before only the canonical uppercase spelling was
  matched), is replaced whole by `[redacted]` instead of having the matched text cut out. A
  credential learned from a concurrent response while a redaction runs makes that redaction
  run again with the new snapshot, up to three times.

### Service

- **The service counts the times its event loop was blocked.** A task on the loop sleeps
  0.1 seconds and measures how late it wakes; a delay above 1 second is counted in `status
  --json` under `loop` (`lag_events`, `max_lag_seconds`, `last_lag_seconds`, `last_lag_at`) and
  logged as a `loop_lag` entry, so the next stall that makes the fleet writer or an MCP client
  time out is visible on the service itself. See
  [Operations](docs/OPERATIONS.md#when-the-panels-say-the-service-is-offline-while-it-runs).

### Documentation

- README, SECURITY and MIGRATION name the public repository,
  [github.com/NetDevAutomate/answerback](https://github.com/NetDevAutomate/answerback): the clone
  command, tagged releases, the issue tracker and GitHub private vulnerability reporting replace
  the repository-split placeholders. There is still no package on PyPI.

### Receipts

- **A receipt can carry a subject: what the sender says the hand-off is about.** `answerback
  send --subject <text>` and the `subject` argument of the MCP `peer_submit` store 1 to 256
  printable characters (a commit SHA, an artifact digest, a release tag, a document ID) as the
  receipt's top-level `subject`, shown by `task`, `tasks`, `task_get` and `task_list`.
  `answerback tasks --subject <text>` and `task_list` with `subject` return the newest 100
  receipts of the whole store with exactly that subject, with a `subject_matches` count of all
  of them. The same request ID
  with a different subject, or with one where the first had none, is refused as different
  work. The bridge never checks a subject. `Submission` gains `subject`, and every adapter
  passes it to `TaskStore.reserve(..., subject=)`; the router refuses a receipt whose stored
  subject differs from the submission's, or one reserved in another store, with "inspect it
  before sending again". The provider conformance fixture gains a sixth case, `subject`, run
  with a declared `subject_probe`, which checks both the receipt the provider returns and the
  stored row; the provider template's declaration carries a minimal one, and a test builds the
  template's package and runs all six cases against it. The task store gains a `task_subjects` table, added on
  first open, with triggers that refuse changing a stored subject, deleting it while its
  receipt exists, or adding one other than with its receipt's own insert; maintenance refuses a `task_subjects` table with unknown columns; the `tasks` table itself is
  unchanged and older receipts read `null`. Maintenance keeps a subject when it prunes a
  receipt's message and result. A bridge downgraded onto a migrated store still sends and
  reads receipts but shows and compares no subject, and its maintenance treats the store as an
  unsupported schema and prunes nothing in it. See [Receipts](docs/RECEIPTS.md#name-what-a-hand-off-is-about).

## 0.16.2 - 2026-11-26

### Configuration

- **The flat keys of a provider that is not installed are read by one generic rule.** A
  pre-0.16 flat key `<name>_<field>` that no loaded provider claims is read as the field
  `<field>` of the provider `<name>` when the same file's `provider_selection` names that
  provider. The block is then kept and disabled with the one warning of
  [a provider that is not installed](docs/ANSWERBACK-UPGRADE.md#a-provider-that-is-not-installed),
  and the first save writes it nested under `providers.<name>` with no flat key left. When
  the file also carries the nested value, the nested value wins and the same warning about
  shadowed flat keys is printed. A flat key whose prefix is neither a loaded provider nor a
  name the file selects is still refused as an unknown key. The kernel no longer keeps a
  table of one departed provider's flat key names, and `tools/public-tree-allowlist.txt`
  lists no exception.

## 0.16.1 - 2026-11-26

### The repository split

- **This repository is the public Answerback kernel.** It was created as a fresh history
  from the combined source tree at `1370511d0c9c4f992f79955033eeef6dc9b382bf`, not as a
  rewrite of that history. It holds the `answerback` distribution: the kernel, which names
  no provider, its own providers (`codex`, `codex_cli`, `claude_code`, `grok`, `kiro`,
  `crew`, `rex`, `hc`, `herdr`), the optional `bus` module, the shipped skills, the
  documentation and the test suite. A provider plugin is a separate distribution that is
  installed alongside the kernel and pins its version
  ([the compositions](docs/ANSWERBACK-UPGRADE.md#choose-a-composition)).
- **Earlier history is in the maintainers' private archive.** The changelog of 0.16.0 and
  earlier versions, the task ledger and its evidence were not copied here. Versions before
  this one carried a `+internal` suffix in the combined tree; 0.16.1 has none.
- **What changed in the code: nothing a user sees.** 0.16.1 is the 0.16.0 kernel with its
  version, packaging and gate made for a repository of its own: `uv sync` and `just check`
  are the whole gate, and `just assert-public-tree` checks the tracked tree with nothing
  skipped. [MIGRATION.md](MIGRATION.md) says how an installation moves to it.

### Carried over from 0.16.0

The 0.16.0 entry of the old changelog sends an existing installation to
[Upgrade to 0.16](docs/ANSWERBACK-UPGRADE.md#upgrade-to-016-provider-plugins-and-the-config-layout)
first. Three of the facts it lists there hold for this kernel as they did for 0.16.0:

- **The `config.json` layout.** In the old entry's words: "the new `config.json` layout
  (loads unchanged, rewritten on the first save, not readable by 0.15)". "Flat provider keys
  are read until 0.17; written nested from 0.16."
- **A provider that is not installed is disabled, not refused.** "the disabled-not-refused
  rule for a provider that is not installed": "A `providers.<name>` block or
  `provider_selection` entry no loaded plugin claims is disabled with one warning at load,
  kept through saves and listed as `disabled` in `status`/`doctor`, never a refusal".
- **The fleet recipe.** "the per-host fleet recipe that stands in until `answerback fleet
  upgrade` (T-252) exists".
