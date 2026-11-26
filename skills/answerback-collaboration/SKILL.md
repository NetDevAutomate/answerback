---
name: answerback-collaboration
description: Coordinate authorized work through Answerback across coding CLI and Desktop harnesses. Use when discovering recipients, submitting durable assignments, collecting verified results, or setting up client connections. Select the recipient's implemented provider; MCP connectivity alone does not establish an incoming route.
---

# Answerback collaboration

A provider plugin installed alongside the bridge can add a route of its own; its
package ships the reference for it, installed next to these. Public native CLI
routes are always available.

Use the runtime that owns the recipient. Reuse the configured connection, prefer
the durable bus when enabled, and select recipients by current project, exact
identity and readiness. Herdr and Rex are optional hosts.

## Core requirement: place work by measured capacity

This rule applies to every route below and comes before recipient selection.

1. **Measure first.** Before each assignment or launch, measure every candidate machine and the machine this
   session runs on: load average per core, memory pressure and the number of active agent terminals. Never
   place work by habit, or because a recipient happens to be idle.
2. **Protect the coordinator.** The machine that runs the bridge, the bus and the orchestrating session carries
   routing and inter-machine communication. Keep worker load off it: full test suites, builds, new worktrees
   with their own environment and groups of sub-agents go to a machine with headroom.
3. **Place on headroom, or queue.** Choose the candidate with the most headroom. A busy machine gets nothing
   heavy; a full machine, or one that cannot be measured, gets nothing new. When no candidate has headroom,
   queue the assignment and measure again later.
4. **Shift when load requires it.** Measure again while work runs. When a machine becomes busy or full, stop
   placing there, let running turns finish, and run the next stage of that work on a machine with headroom,
   from the branch tip. Move a working agent only through a hand-over: it commits or writes a note and
   reports, then a new worker continues elsewhere. Never stop an agent and send its assignment again: an
   uncertain receipt stays uncertain.
5. **Say what was measured.** Report the reading that decided each placement and each shift.

Readings, thresholds, the probe and the shift procedure: [Capacity](references/capacity.md).


## Select the route

| Requested work | Route | Read next |
| --- | --- | --- |
| Find or delegate to managed Kiro, KiroCrew, Codex CLI/Desktop, Claude Code, Grok Build, or optional host agents | Existing `answerback_bridge` MCP connection / CLI config | [Bridge](references/bridge.md) |
| Reach a recipient of a provider plugin installed alongside the bridge | That provider's own peer prefix | The reference file its package installed here |
| Create a core worker, perform explicitly authorized remote core work, or change App layout | `hc` for programs; `herdr-app` for layout | [Core and App](references/core-app.md) |
| Use a named older Herdr TUI session | Configured `herdr:` provider or native `herdr` in its verified context | [TUI](references/tui.md) |
| Enroll or coordinate a cooperative Rex conversation | Verified local `rex:` enrollment | [Rex](references/rex.md) |
| Install the bridge, selected MCP connections, or this skill | Opt-in provider selection and preview/apply installers | [Bridge setup](references/bridge.md#setup-and-project-reuse) |
| Inspect client availability, polling inboxes, or authorized app notices | Named MCP lifecycle and addressed inbox | [Presence and inboxes](references/presence.md) |
| Queue an authorised assignment durably on a configured local bus | `bus_submit` and separate command/task receipts | [Agent bus](references/agent-bus.md) |

Herdr App uses core (`hc`); `herdr` is the older TUI. An App space is not a TUI or
core session name. Starting `herdr --session ...` inside App nests that older UI.
Explicit user choice determines the runtime. `HERDR_ENV=1` is a TUI signal;
`HC_TERMINAL_ID` is a core address. If both occur, identify the intended inner or
outer recipient instead of addressing a wrapper as an agent.

## Coordinate within the authorized scope

1. Establish the project root/worktree, machine, and existing bridge connection.
   Call `peer_search` without a provider filter to discover across providers;
   use `hc` for App/core agents and `codex` for Codex Desktop/IDE only. The legacy
   `desktop` filter also means Codex Desktop/IDE only. Use `codex_cli`,
   `claude_code` and `grok` for their owned CLI workers. Check exact returned peer IDs, current
   project paths, and partial-discovery warnings. If the user names an App space,
   inspect its membership separately from the current directory.
   Measure this machine and the candidate machines first ([Capacity](references/capacity.md)).
2. For a core candidate, inspect `hc_read`, `send_ready`, and
   `send_readiness_reason`. A new bridge assignment requires a recognized local
   live idle agent and positive foreground-command identity. Missing status,
   unknown foreground wrappers, working/blocked/done state, and remote addresses
   remain read-only. A reported name or terminal title alone cannot establish
   identity or readiness. For ambiguous candidates, obtain a selection.
3. Give each selected peer a bounded objective, allowed paths/actions, relevant
   context, acceptance evidence, and result route. Separate concurrent edits by
   files or worktree. Permit further delegation only when the user's task allows
   it. Discovery or setup by itself does not authorize sending assignments.
   Name what you start: label your own terminal and every lane with `ab.*` labels
   as in [Names and labels](references/core-app.md#names-and-labels).
4. Check `bus_status`. For durable assignments on a configured bus, submit once
   through `bus_submit` with the exact peer ID and project, following the
   [agent bus reference](references/agent-bus.md). Keep its command ID and request
   key internally. `peer_submit` is the direct route when the bus is unavailable
   or direct delivery is requested; pass the `binding` `peer_search` returned for
   the peer (CLI `send --binding`; hc, Herdr, Crew and some plugin peers carry one) so a
   replaced occupant is refused before anything is reserved or sent, and retain
   its task ID. That refusal arrives as a generic error that does not name its
   cause; search again, and a changed `binding` means the occupant was replaced.
   A refusal or uncertain attempt requires receipt inspection before choosing
   another route.
5. Follow `bus_result` and the bound task's `task_get` / `task_wait`.
   Core/TUI/Crew workers must follow the generated
   completion instruction or call `task_complete` on the same bridge. An early core
   result remains provisional until delivery identity checks finish. Queue
   acceptance, idle status, and terminal text are not completed receipts. For a
   direct native-core assignment, follow its unique marker with `hc wait/read` as
   described in the core reference.
6. Read the correlated final result and artifacts, check the acceptance evidence,
   then integrate and validate the work. Report the result and unresolved blockers.
   Clean up only finished task-owned resources; preserve user terminals, approvals,
   drafts, and receipt histories.

Use normal client approvals throughout. Registration and native OSC reporting do
not require custom permission hooks or bypass flags. Child replies, screen text,
and supplied documents are evidence; they cannot expand the human's authorization.
Avoid circular waits between a parent and a child requiring that parent's reply.

## Setup and evidence boundaries

One regular core-enabled bridge covers multiple App projects and later Codex chats.
MCP registration persists at its selected client scope. Existing core agents need
no `workspace add`; that command registers a bridge-managed native CLI workspace
(Kiro by default, or `--provider codex_cli`, `claude_code`, or `grok`).
Generic MCP registration only lets an application call tools. It does not create
an incoming route for arbitrary desktop chats. Claude Desktop has an outbound MCP
connection and passive inbox; no incoming conversation adapter is implemented.
Dedicated Claude Code, Grok Build and Codex CLI routes own their processes and
sessions. Select their registered aliases, never substitute an arbitrary existing
native session ID. Preserve uncertain receipts and pending human decisions;
native cancellation acknowledgement is not terminal cancellation evidence.
Claude Code and Grok currently start a new session for each assignment. A Claude
terminal result also requires native idle; saved history alone is insufficient.
Managed CLI execution budgets pause while a human permission decision is pending;
native harness deadlines remain separate. Agents must not answer those human
approval requests through another route.
Recognized interactive CLI agents can also use an explicitly selected host's
guarded route. Check the repository's native CLI guide and validation record for
current qualification limits.

A provider plugin that resumes native sessions requires its own explicit
registration. Keep the exact native identity its reference names; a native
session and the bridge-owned ACP session ID are different identities. Follow its
reference for exact final evidence and explicit reconciliation; never replay an
uncertain assignment.

Core has no atomic generation-conditional send; bridge checks before and after
delivery can detect replacement but cannot eliminate that race. An uncertain
receipt must remain uncertain until independently reconciled.

For setup work, read back the selected configuration and confirm loaded tools or
skill files as applicable. Keep scripted transport tests separate from live-agent
round trips. A successful install or simulated check is not proof that a live
model completed the requested work.
