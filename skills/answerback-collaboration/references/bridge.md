# Shared bridge routes

Use the existing MCP connection or one explicit config path throughout an exchange.
Read installed help if the version or available interfaces differ from this guide.
The source repository's README and client guide provide the full installation
walkthrough; locate the user's checkout rather than assuming a local path.

The default MCP name is `answerback_bridge`. Reuse custom names and explicit
config paths. Naming upgrades use `answerback clients migrate --client NAME`
for a preview and `--apply` for a reviewed change. Preserve task IDs, workspace
aliases and state directories throughout an upgrade. An existing full legacy
collaboration skill requires an explicit backed-up `skill install --replace`;
the legacy invocation then points to this canonical skill.

## Providers

| Filter | Recipient | Completion evidence |
| --- | --- | --- |
| `hc` | Recognized Herdr App/core agents | Verified delivery plus explicit worker result callback |
| `herdr` | Agents in the configured older TUI session | Explicit worker result callback |
| `kiro` | Managed ACP worker alias and project directory | Native ACP prompt result |
| `crew` | Authenticated Kirocrew gateway chat | Explicit worker result callback |
| `codex` (`desktop` legacy alias) | Indexed Codex Desktop/IDE chat | Matching persisted completed turn and final answer |
| a plugin's name | Recipients of a provider plugin installed alongside the bridge | As its own reference states |

An unfiltered search includes enabled providers. The `desktop` alias is specific
to Codex; it does not mean every application running in a desktop window. Claude,
Grok, or other recognized agents in Herdr App are `hc` peers. A provider plugin
that resumes native sessions requires its own registration; read the reference its
package installed next to this one.
Generic MCP registration provides outbound tool access and an optional inbox;
ordinary ChatGPT web conversations are outside the Codex local index.

## Select, submit, and collect

MCP: `peer_search` → select a returned identity → `peer_submit` with that peer's
`binding` → `task_get` / `task_wait`. Keep receipt IDs inside the orchestration
workflow. A `binding` that no longer matches the re-resolved peer is refused
before anything is reserved. The caller gets a generic failure that does not
name this cause; search again and select afresh.

```sh
uv run --no-project answerback --config <config-path> find "<project keywords>"
uv run --no-project answerback --config <config-path> find "<project keywords>" --provider hc
uv run --no-project answerback --config <config-path> read-hc <returned-core-address> --lines 100
uv run --no-project answerback --config <config-path> send <returned-peer-id> --binding <returned-binding> --message-file <assignment-file> --request-id <stable-request-id>
uv run --no-project answerback --config <config-path> task <returned-task-id> --wait 30
```

CLI `task --wait` exits `0` only for a completed task: `1` is a failed, cancelled,
abandoned, uncertain or ambiguous result, `2` a task `blocked` on a pending
permission, `3` a wait that expired first (`wait_timed_out: true`). Over MCP,
`task_wait` returns the same fields directly; check them instead of an exit code.

Check project, machine, provider, and current identity. For core, `hc_read` accepts
an explicit `t…` or `machine/t…` address. CLI `read-hc` also accepts the returned
`hc:` handle. Select exact returned handles for submission. Retain machine prefixes.
Current project comes from foreground metadata; App labels and launch directories
are separate. Missing current directories remain unknown.

Core sends require `send_ready: true`: local, live, idle, recognized foreground
command, known current directory, and matching reported status. Read
`send_readiness_reason` when false. OSC-reported identity with missing or unknown
foreground command permits reading only. Busy, blocked, done, error, unknown, and
remote peers cannot receive a new bridge assignment. Use the native-core reference
for explicitly requested remote operations; it is not a readiness bypass.
Core 0.1.21 accepts only literal `idle` for `hc send --if idle`, so a `done`
recipient must report idle again before guarded bridge delivery is possible.

Partial or ambiguous searches need inspection or explicit returned selection
before submission. For Codex, follow `desktop-sessions --cursor` when provided.
Its canonical `desktop:UUID` handle remains the same with the `codex` filter.
Indexed metadata does not prove the owning app loaded the chat. Queued work is not
a verified wake-up; preserve the existing execution owner.

Core submission persists a `pending` receipt before background delivery. Keep that
receipt and reuse its request ID only for identical work to the identical target.
For timeout/uncertainty, inspect the receipt and recipient before another send.
Bridge restart recovers receipts without automatically replaying assignments.

Codex history is a separate observation of Desktop execution. An unfinished turn
may project as interrupted with no completion timestamp; keep it pending. For an
investigated false-failed Desktop receipt from an older bridge, the explicit CLI
`task <task-id> --reconcile` verifies the original completed turn and final answer,
retains the prior failure as audit metadata, and never resends the prompt. Normal
task reads preserve terminal receipts.

Core/TUI/Crew workers use the generated callback, for example:

```sh
uv run --no-project answerback --config <same-config-path> complete <assigned-task-id> --result-file <result-file>
```

Alternatively call `task_complete` through the exact same bridge's MCP connection.
The generated task ID identifies the assignment; `hc:t7` or `t7` does not identify
a receipt. Only report work actually completed. Core stores an early result
provisionally until delivery identity is verified. Native Kiro and Codex result
correlation is adapter-managed.

Core exposes no atomic generation-conditional send. The bridge checks identity
before delivery. It checks again only after a send receipt that it accepts; a
program change in that gap can leave delivery uncertain. Any other outcome of
the send is recorded as an uncertain delivery at once, without the second
check. A plausible result must not hide a failed
identity check.

## Setup and project reuse

For a new regular bridge with Herdr App/core:

```sh
uv tool install /absolute/path/to/answerback_bridge
uv run --no-project answerback configure --runtime hc
uv run --no-project answerback start
```

Bare `configure` selects no providers; the explicit `--runtime hc` above selects
core. Register other requested providers with `providers register NAME`. For an existing
bridge, start it and run `hc-configure`; this preserves other provider settings.
`hc-configure --binary /path/to/hc --session CORE_SESSION` optionally selects a
named core. App space IDs/names are never session values.

Preview only the selected client/skill destinations, then apply the authorized plan:

```sh
uv run --no-project answerback clients install --client codex
uv run --no-project answerback skill install --client codex
uv run --no-project answerback clients install --client codex --apply
uv run --no-project answerback skill install --client codex --apply
```

Both support repeated `--client` flags for `codex`, `claude`, `grok`, `kiro`, `crew`,
and the client name of an installed provider plugin. `--all` selects those targets
rather than installing their applications; a plugin's client may need its harness's
native CLI. Resolve any reported refusal before applying. Identical
entries remain unchanged; intentional conflicts require `--replace` and get
backups. Client installation defaults to connection name `answerback_bridge`; `--name`
selects another name. Reuse a same-config entry already registered under another
name instead of duplicating it.

The wheel bundles the complete skill. `skill install --source /path/to/folder`
is optional. The installer maintains a shared skill copy and harness links; a
plugin's client may use its harness's native skill installer instead, with the
profile its package documents. Crew shares Kiro's global MCP entry and skill directory; check inheritance
and reload in the actual chat. Reload/new sessions may be needed in all clients.

Reuse one regular core-enabled connection across projects. Existing App/core
agents are discovered by current project automatically. `workspace add ALIAS
--cwd PATH` registers only a managed Kiro ACP workspace and starts no agent;
`--create` explicitly creates a missing directory. `kiro-register` is its older alias.

`crew-configure` enables incoming Crew gateway access once per bridge. Its port is
optional; keep the gateway running. Outbound Crew MCP registration is separate.
For a deliberately selected older TUI, use `configure --runtime tui --session NAME`
in a separate config directory; bare `configure --session NAME` retains that legacy
meaning. No TUI session is needed for an App-only bridge.

## Status, permissions, and maintenance

If a verified Claude command has missing status, inspect the relevant reporting
configuration. Claude Code 2.1.322 has native OSC reporting;
`CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` can suppress the probe/status path. Correcting
the setting does not prove a running session retried its startup probe. Recheck
status after its next normal restart; preserve current work. Use normal approvals,
with no custom hooks required to make core reporting or bridge receipts work.

`permissions` shows blocked Kiro requests; `permission-answer` is a human CLI
operation requiring an explicit one-time choice. `--permission-id` is mandatory
and must match an ID currently in that task's inbox, so answering only ever
approves the request the human actually reviewed with `permissions`, never a
different one that replaced it after the task changed. Agent messages cannot
supply that human approval. Other agents retain their own host's approval
controls.

`config list` inventories configurations. Workspace removal/config archival
preview by default; apply only requested cleanup after unfinished work has been
reconciled. Abandoning a receipt does not cancel its recipient's work.

`self-test` uses scripted peers. `live-test` provisions an isolated older TUI
environment and does not test App/core. The regression suite includes scripted
native-core MCP round trips; live model compatibility still needs separate
observations. A live probe requires scoped recipients, a unique marker, and final
result correlation. Discovery alone never authorizes using someone's active agent.
