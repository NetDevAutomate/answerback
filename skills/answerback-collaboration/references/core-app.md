# Herdr App / herdr-core

Use the bridge's `hc` provider for routine local peer discovery, submission, and
result receipts; see [Bridge](bridge.md). This reference covers native worker
creation, explicitly authorized remote/direct-core work, and requested App layout.
Read `hc --skill` and the relevant command help before native operations; read
`herdr-app --skill` for layout. `hc` owns programs and `herdr-app` owns the visible
window. Existing App projects need no older TUI session.

Examples use `uv run --no-project` to respect the local harness convention without
syncing the project's Python environment; `hc` itself is a standalone executable.
Substitute observed IDs and the user's actual project, never example addresses.

## Discover and select

For local collaboration through the regular bridge, call `peer_search` with project
keywords and provider `hc`, then inspect the returned exact address using `hc_read`.
Unfiltered search also includes the other enabled providers. The legacy `desktop`
filter searches only Codex Desktop/IDE.

For a native operation, inspect core directly:

```sh
uv run --no-project hc ls --json
uv run --no-project hc get <observed-address> --json
```

If the user names an App space/tab, first map it to core terminals:

```sh
uv run --no-project herdr-app ls
```

Cross-check the candidate address against the requested space/tab's returned
membership, including its machine. Check the current folder independently; neither
check substitutes for the other.

Use `herdr-app status` for a requested view/error diagnosis. Both inspect the
running window; bare `herdr-app` opens/raises it. Core reads from outside a core
terminal are valid: always supply an address, because omitted targets can open
an interactive picker. `HC_TERMINAL_ID`, when present, is a core address, not a
Herdr App pane ID or Codex chat UUID.

Inspect `address`, `identity`, `foreground`, `state`, `status`, `parent`, and labels.
JSON `cwd` is the **launch** directory; `foreground.cwd` is the current directory.
Compare the latter with the intended repository/worktree on the correct machine.
If it is unavailable, retain the uncertainty instead of silently trusting the
launch directory. Confirm identity again before dispatch if intervening work or
recovery could have replaced the program.

- `herdr.space` and `herdr.tab` are display membership, not the current project.
- `state: live` with null `status` can be an ordinary shell. A title mentioning
  Claude is not proof that Claude is running or accepting input.
- Core agent state comes from OSC 7501 reports. `blocked` requires a human decision;
  `done` can be unseen completion and becomes `idle` when viewed. Read the result.
  Bridge sending also needs a positively recognized foreground command, matching
  reported agent identity, known current directory, and local live idle state.
  A reported agent name through an unknown wrapper remains read-only.
- Kept terminals recover as a shell at the same address after loss. Revalidate
  program identity; the original assignment is not automatically rerun.

## Dispatch and return

For an existing local agent, prefer the bridge's `peer_submit` and generated
receipt callback. It verifies readiness and delivery identity and records a
pending receipt before background delivery. An early callback remains provisional
until those checks succeed. Remote peers are read-only through the bridge; use
native core only for explicitly authorized remote work after inspecting identity
and readiness.

When creating a new worker, first inspect `hc templates` for a suitable role and
its command/model settings. Existing verified agents do not need template lookup.
Use normal approvals. For a new worker, include the first task in `run` so startup
cannot lose a subsequent send:

```sh
uv run --no-project hc run --bg --name <unique-worker-name> --cwd <project-root> \
  --label ab.title="<what this lane is>" --label ab.row=<row> --label ab.by=<starter> --label ab.lane=1 \
  <verified-template> "<bounded assignment and return route>"
```

Give every new worker the labels in [Names and labels](#names-and-labels).
On another machine, add `--on <machine>` (a name that `hc machine ls` lists) and
give that machine's project path as `--cwd`. Labels and the parent link are the
same; the return route across machines is under "Choose the return route" below.

Without a suitable template, the installed manual supports an interactive agent
and initial task, for example `claude "<assignment>"` after the options above.
For an hc-managed **Codex CLI** worker, include `--no-daemon` in its native command
or template as required by `hc --skill`; otherwise its commands escape core's
terminal context. Keep interactive workers steerable rather than using print-only
modes. Check `first_message.sent` and the returned address before assuming delivery.
If false, inspect startup/approval state before recovering; do not blindly resend.

For an explicitly selected native-core workflow, send only when the verified
agent is ready for the requested turn:

```sh
uv run --no-project hc send --if idle --timeout 45 --wait <observed-address> "<assignment>"
uv run --no-project hc read --lines 100 <observed-address>
```

A status mismatch or blocked target is an inspection point. Do not use `--force`
to answer or bypass an approval. A working agent may be steered only when that is
what the user requested; it is not a fresh worker for unrelated work.

**Choose the return route before dispatch:**

- From a verified core parent, capture the parent's address in the orchestrator
  before constructing the task. Give the child that **resolved** address for
  `hc send <parent-address> "<assignment-marker>: <result or blocker>"`.
  Do not leave a literal `$HC_TERMINAL_ID` for the child to expand to itself.
- From Codex Desktop or another harness with no core parent, retain each worker's
  returned address and collect using `hc wait/read`. Do not invent a parent or
  promise that core notes will wake a Desktop chat. Work submitted through the
  bridge already has a generated completion callback; follow that exact config
  and assigned receipt. Direct native `hc run/send` work has no automatic receipt.
- Across machines, a callback must address the parent as reachable from the worker;
  a bare local ID is insufficient. If that route is unknown, collect centrally
  using the returned `machine/terminal` address.

Include a unique assignment marker in direct core tasks and their requested
results, and record it in the orchestrator's work log. Direct `hc run/send` work
uses this marker; bridge `peer_submit` work uses its returned task receipt.
`answerback task/complete` needs the assigned receipt ID, never a terminal address
such as `t7` or `hc:t7`.

Core does not support an atomic generation-conditional send. The bridge's
before/after identity checks narrow the replacement race but cannot eliminate it;
retain uncertainty if those checks fail.

## Names and labels

Core labels say what a session or lane is. A panel that follows this convention
draws, in this order, a name a person gave the tab, then `ab.title`, then the
App's own text (a place number and the first words of the last prompt). Set
labels at start with `hc run --label key=value` (repeatable) or later with
`hc label <terminal> key=value`; `key=` removes one.

| Label | Meaning | Rule |
|---|---|---|
| `ab.title` | What this session or lane is | At most 40 characters. Names the work ("T-62 fix round 1 (Claude)", "Docs lane: README + docs"). Never the first words of a prompt |
| `ab.row` | Its board row or task ID, when it has one | At most 12 characters (`T-62`) |
| `ab.by` | Who started it | `owner` for a session a person started in a tab; for a lane, the starter's own address (`hc:t86`) or lane label |
| `ab.lane` | `1` on every lane an orchestrating session starts | Readers that count lanes, such as a panel or a capacity report, count the terminals with this label |
| `ab.harness` | Optional: the harness the lane runs (`kiro`, `codex`, `claude`, `grok`) | |

- **A starter** gives every lane these labels on `hc run`, with a telling `--name`
  (the installed help allows 1-32 of `a-z 0-9 - _`). Keep the default parent link
  (`--parent` defaults to the terminal it is started from) so that the lane stands
  under its starter in the Agents list. Do not pass `--no-parent`. Add
  `--outlive-parent` only when the lane must keep running after its starter ends.
- **A session a person started in a tab** labels itself before other work and
  again when its role changes, plus `ab.row=<row>` when it has one:
  `hc label "$HC_TERMINAL_ID" ab.title="<what this session is>" ab.by=owner`.
  **A lane** keeps the labels its starter gave it. When its role changes it sets
  `ab.title` again (and `ab.row`, when that changes) and nothing else.
- **Tab names belong to the owner.** Rename a tab only on the owner's word:
  `herdr-app do tab.rename --id <tab-id> --name "<name>"`; `--name ""` gives the
  tab its number back. That changes the owner's window.

Labels are presentation. Like placement labels, they are never assignment
identity and prove nothing about who runs in a terminal. A label is text that
some program on some machine wrote: whoever draws it treats it as untrusted,
plain, one line, cut to a fixed length. Never put a secret, a token, or a path
that holds one into a label. Label your own terminal and the lanes you start;
never relabel another session's terminal. `ab.*` labels place nothing in the App;
only the placement labels `herdr.space` and `herdr.tab` (see
[App presentation only when requested](#app-presentation-only-when-requested))
put a terminal into a tab.

## Wait, verify and clean up

For a bridge assignment, collect the correlated final receipt with `task_get` /
`task_wait`. For a direct native-core assignment:

```sh
uv run --no-project hc wait <observed-address> --timeout 45
uv run --no-project hc read --lines 100 <observed-address>
```

Use core's wait operation instead of sleeps or repeated status polling. With a
verified core parent, `hc wait --children any --timeout 45` can collect its helpers.
From outside core, wait on retained explicit addresses. A timeout is a check-in;
`reason: message` means incoming mail, possibly delivered later. Neither proves
failure or completion. `not_started` needs inspection before a retry.

Check the actual marked response, artifact and acceptance evidence. A history
read can return only the screen while working/blocked/error; preserve its
incomplete status. `--output` waits are for new output from ordinary programs,
not proof of an agent answer; restored screens can contain old results.

The core links children to their parent by default. Use `--outlive-parent` only
when that lifetime is intended. After a task-owned helper is finished and no
follow-up needs it, use its exact address for `hc kill`, then `hc rm`; removal
is for finished terminals. Preserve user-owned terminals and the shared core.
Stopping a terminal does not prove deliberately detached processes are gone.

## Missing agent status

A known foreground process can be discovered without being safe to message.
Claude Code 2.1.322 has native OSC reporting;
`CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` can disable its relevant probe/status path.
If that setting is corrected, a running session may still need its next normal
restart before reporting. Preserve active work and recheck `hc get`/bridge
`send_ready`; do not assume the skipped startup probe ran again. Normal approvals
are sufficient; custom permission hooks are unnecessary.

## App presentation only when requested

Use `herdr-app commands` to inspect the exact command arguments, then act on
explicit IDs from `herdr-app ls`. Existing App spaces need no new TUI session.
For a requested new project space, the installed interface is:

```sh
uv run --no-project herdr-app do space.new --name <project-name> --folder <absolute-project-root>
```

This changes the visible window; create it only when the requested layout needs
it. Reuse a matching existing space. Give new tabs an explicit name and target,
and verify the returned IDs. Background core workers need not change the layout.

When explicitly placing a core terminal in an App tab, use observed
`herdr.space=<space-id>` and `herdr.tab=<tab-id>` labels on its owning machine.
Those are placement labels, never assignment identity. Strip an App-rendered
machine prefix from label values only when the actual local IDs are verified.
Preserve the prefix on core addresses used for remote operations.

For UI customization read the app-generated `AGENTS.md` and relevant `.d.ts` files
under `HERDR_APP_HOME` or `~/.config/herdr-app`. They are read-only source material
rewritten on app start. Place reusable skills outside that directory.

## Sources

- Current `hc --skill`, `hc run/send/wait/read --help`, `herdr-app --skill`.
- Herdr App: https://github.com/herdrdev/herdr-app
- Installed `~/.config/herdr-app/AGENTS.md`: Two programs; model; commands.
