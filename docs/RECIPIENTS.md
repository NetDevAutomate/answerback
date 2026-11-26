# Set up a route that can receive work

Kiro CLI workers, KiroCrew chats and Herdr terminals each receive work through their own route. For each one you
get the setup commands, the checks the bridge makes before it sends, and what a completed receipt proves.

It is for someone who has chosen a recipient in [Choose which worker takes the work](HARNESSES.md) and has a
running bridge ([Getting started](GETTING-STARTED.md)). Codex CLI, Claude Code and Grok Build workers are in
[Owned native CLI workers](NATIVE-CLI.md), and Rex is in [Rex conversations](REX.md).

Connecting a client to the bridge ([client setup](CLIENT-SETUP.md)) lets it call the bridge's tools. It does not
create a route that can receive work.

## R1 route support

The R1 boundary is subject to the live gate at the release candidate. An MCP registration lets a client call
bridge tools but never creates an incoming route.

| R1 status | Route | Boundary | Address starts with |
| --- | --- | --- | --- |
| Supported | Codex CLI | Bridge-owned worker | `codex_cli:` |
| Supported | Grok Build | Dedicated profile | `grok:` |
| Supported | Herdr terminal | One assignment per session | `hc:` |
| Supported | Claude Code | Installed CLI 2.1.322 or newer; the bridge process must hold the provider credentials (see NATIVE-CLI.md: wrapper-injected tokens are not inherited; a supervised bridge has no credential path yet) | `claude_code:` |
| Supported | Kiro CLI | A tool use waits for a person in the default v3 mode; the bridge never approves. Managed workers run unattended on the opt-in v2 engine with `kiro_trust_all_tools` or `kiro_trust_tools` (below), off by default and Experimental | `kiro:` |
| Experimental | `hc_send_to_done` | Ships off by default | `hc:` |
| Experimental | Remote machines (`hc_send_machines`) | Linux with Codex CLI is the only qualified pair | `hc:` |
| Experimental | KiroCrew as sender | Ships, but still needs the gate | `crew:` |
| Excluded from R1 | KiroCrew as recipient | Requires the bridge tool server in KiroCrew with its reporting tool pre-approved; KiroCrew then approves all bridge tools for its agents | `crew:` |

The Claude Code and Kiro CLI rows rest on live runs on 24 November 2026 on macOS and Linux: Kiro CLI
3.5.0 at `4a03461b`, and Claude Code 2.1.330 with its round trip and repeat at `4a03461b` and its
cancel at `5c8995a5`. Those run records name the maintainers' hosts and home directories, so they
are kept in the maintainers' archive, not in the public tree.

Codex Desktop/IDE and Claude Desktop configuration is present in the code and is not part of the initial release.

### Recipients of a provider plugin

A provider installed alongside the kernel as a separate package adds its own recipients, addressed with its own
prefix (`<name>:<id>`). That package documents its registration step, supported response formats and recovery
limits. The kernel's rules still hold for them: exact peer selection, a stable request ID and no replay of an
uncertain receipt.

### Managed Kiro workers

```sh
answerback workspace add reviewer --cwd /absolute/path/to/project
```

```json
{
  "peer_id": "kiro:reviewer",
  "project": "/absolute/path/to/project",
  "kind": "bridge_managed"
}
```

That is the whole output, with the project path replaced. `workspace add` without `--provider` registers a Kiro
worker, and `kiro-register` is an alias for it. Then send the work and wait for its receipt:

```sh
answerback send kiro:reviewer --request-id review-1 --message "Review this project"
answerback task --peer kiro:reviewer --wait 30
answerback permissions --peer kiro:reviewer
```

The hand-off is done when `task` exits `0` and the receipt reads `"status": "completed"`. Exit `2` means a
permission request is waiting (below), and exit `3` means the wait ran out first.

Registration starts no agent. The bridge starts or loads its owned conversation when work arrives and supplies
MCP itself. Kiro needs an existing sign-in. The bridge drives its v3 agent engine over ACP protocol version 1.

Workers use an inline `answerback_bridge` agent profile with native read, write and shell tools plus the bridge
MCP server. Other global MCP servers and powers are excluded, though Kiro may keep connections it requires. The
bridge verifies the selected profile before prompting. On the default v3 engine it also verifies the required
bridge MCP tools before prompting (`_kiro/mcp/status` lists each server's tools; the receipt records
`"mcp_readiness": "tools_verified"`). On `kiro_acp_engine: "v2"` the engine reports only that the bridge MCP
server initialized, without a tool list, so the bridge waits for that and no more: the tools stay unverified until
the worker's first bridge call, and the receipt says so (`"mcp_readiness": "server_initialized"`,
`"unverified_mcp_tools": ["answerback_bridge"]`).

The v3 engine changes no global agent file and no tool approval setting. With `kiro_acp_engine: "v2"` the bridge
writes one agent file, `~/.kiro/agents/answerback_bridge.json`, with the same profile, and the
`kiro_trust_all_tools` or `kiro_trust_tools` setting becomes Kiro's launch flag (`-a` or `--trust-tools`). See
[the Kiro agent file](NATIVE-CLI.md#the-kiro-agent-file) and
[unattended Kiro workers](HARNESSES.md#unattended-kiro-workers).

Two Kiro tasks can run at once. Each has an active execution budget, 600 seconds by default, that pending
permission decisions pause; `workspace list` shows it as `active_execution_budget`.

On the first assignment after an upgrade from an unscoped worker, the bridge starts a fresh scoped session and
records the earlier session ID in the new receipt. Native history and earlier receipts are kept, and the old
conversation is not replayed. Later assignments resume the scoped session. `kiro-sessions` lists saved session
metadata, and a listing gives no control of a live Kiro session.

A permission request pauses the task. `permission-answer` is a command for a person and is absent from MCP. It
needs `--permission-id` with the exact ID that `permissions` shows, and either `--option` or
`--decision allow-once|reject-once`. When that request is no longer pending, `permission-answer` refuses instead
of answering whatever is pending now. A successful answer echoes `approved_tool_call` so you can confirm what you
allowed.

`cancel` works for bridge-owned Kiro tasks. An uncertain delivery is never replayed automatically.

### KiroCrew

```sh
answerback crew-configure
answerback find "project keywords" --provider crew
answerback read-crew "chat title"
answerback crew-create "Review worker" --project /absolute/project --request-id review-worker
```

Keep Crew's local gateway running. `crew-configure` enables the Crew provider plugin once per bridge and finds the
gateway port, and `--port N` picks one. This is separate from installing the bridge's MCP connection into Crew.
The Crew client obtains short-lived credentials privately, keeps them in memory and sends automated input as
`turn_origin: agent`. HTTP success and assistant text do not replace the explicit result report, and there is no
per-task Crew cancellation.

The Crew client serves Crew 0.12.2 and every later release whose `kirocrew --version` states one version. A
release older than 0.12.2, or output that does not state one version, is refused before a credential is requested.
Crew is released more often than the bridge, so each answer from Crew is checked for the fields the client relies
on, and a release that changes one is refused at that answer. A check cannot undo a write already made.

`doctor` reports the release as `version` and, as `version_status`, whether that release series was exercised with
the Crew client: `tested`, `untested` or `unsupported`. [The Crew contract](PROTOCOLS.md#crew-contract) lists
every check.

Crew can keep its own MCP configuration in `~/.kiro/crew/mcp.json`, which overrides the shared Kiro entry. Update
that entry through Crew's MCP settings and reload the chat afterwards
([Crew overrides](CLIENT-SETUP.md#crew-specific-overrides)).

**How an assignment ends.** A chat that the bridge creates starts in the mode that asks. In runs on 21 November
2026 with KiroCrew 0.13.0 on three machines, the agent began its report with a file write, Crew asked a person
first, and the receipt read `submitted`. On two of them nobody answered: Crew declined the request, and the agent
put its result in the chat as text, which does not complete a receipt. Those receipts had been released before
the decline, so none was read after one. There is no committed record of these runs.

In one run on one macOS machine the report went through the bridge's `task_complete` tool with no person. A script
had added the bridge's MCP server to Crew's MCP file with `task_complete` on its `autoApprove` list and restarted
Crew, and the receipt completed. Linux, the same entry without the list and a chat under Trust were not tried.

Read from Crew's program and not tried: by default Crew approves a server from that file as a whole, so its agents
can then call every bridge tool without asking, including the tools that send or cancel work. Crew's program names
exceptions, among them a setting that makes a named server ask on every call. Weigh that before you mount the
server there. This is why KiroCrew as a recipient is excluded from R1.

## Herdr App/core readiness

The `hc` provider returns core addresses such as `hc:t7`. It checks the foreground command, the current
directory, the process identity and the agent status Core reports. Only a recognized live agent that reports
`idle` (or `done` when `hc_send_to_done` is on), with `send_ready: true`, can receive an assignment. Busy, blocked
and unknown-status agents stay available for reading only. A reported agent name alone cannot authorize delivery
through an unrecognized foreground wrapper.

Two settings control this. They sit in the `providers.hc` block of `config.json` as `send_to_done` and
`send_machines`, and a change needs a bridge restart:

| Setting | Default | Effect |
| --- | --- | --- |
| `hc_send_to_done` | `false` | `true` lets a terminal whose agent reports `done` receive work; that send has no state condition |
| `hc_send_machines` | `[]` | Machine names, as `hc machine ls` shows them, whose terminals can receive work. Empty keeps every remote terminal read-only |

A Herdr terminal takes one assignment per session; the next assignment goes to a new session of the same harness. `hc_send_to_done: true` lets a finished terminal take another. Core cannot refuse that send if the agent starts a turn first; the bridge then records the delivery as `uncertain`.

`hc send --if` accepts only `idle` or `working`, so a send to a `done` terminal goes without `--if`. The bridge
checks identity before the send and again after an accepted send receipt. When the second check finds the same
agent, the text landed in a turn the agent had started, and the worker's result still completes the receipt. When
the agent has changed, the receipt stays `uncertain`, a later `task_complete` is refused and no result file is
read. [Herdr integration](HERDR-INTEGRATION.md#sending-to-a-terminal-that-reports-done) has the details.

```sh
answerback read-hc <returned-core-address> --lines 100
```

MCP gives the same read as `hc_read`. The current project comes from `foreground.cwd`. Launch folders and App
space or tab labels are separate facts. Core has no atomic generation-conditional send, so a replacement during
the send can still leave an `uncertain` receipt. Inspect that receipt and the terminal before retrying.

If a recognized Claude process reports no status, check its terminal-reporting settings:
`CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` can disable the status path. A session that is already running does not
rerun its startup probe, so recheck after the agent's next normal restart.

For worker creation, explicit remote work or App layout operations, use the core reference of the
[collaboration skill](../skills/answerback-collaboration/SKILL.md).

### Terminals on other machines

A terminal on a machine listed in `hc_send_machines` is sent work the same way. The `answerback complete` command
and the `task_complete` tool reach the bridge through its private socket on this machine, so an agent on another
machine cannot call them. Its assignment tells it to write a result file on its own machine, and the bridge reads
that file and completes the receipt
([how a remote terminal reports](HERDR-INTEGRATION.md#how-a-terminal-on-another-machine-reports)).

A person on this machine has two commands for such a receipt. `answerback task <task-id> --reconcile` reads the
result file at once and completes the receipt only when a valid file is there.
`answerback complete <task-id> --result-file <path>` records a file on this machine as the result. It completes a
receipt that is not final and whose delivery was verified, and is refused when the delivery identity was
invalidated.

`answerback route` can reuse an idle terminal on a listed machine, except for a gated class of brief, and never
creates a worker there ([routing limits](ROUTING.md#limits)).

### Remote results: what listing a machine trusts

> Listing a machine in `hc_send_machines` delegates completion of tasks sent there to every program running under that machine's user account: the result file is proof that the brief reached that account, not that a particular agent wrote it. Before removing a machine from the list, let its open tasks finish or reconcile them (`answerback task <id> --wait <seconds>`); receipts whose machine is no longer listed are not read.

The bridge gives each such task a 32-hex token and names the result file after the task ID and the token. It
accepts a file only from a listed machine, only as UTF-8 text, and only when its first and last lines repeat the
task ID and the token. The assignment tells the agent to make the folder mode 0700 and the file mode 0600, which
limits the file to that machine's OS account. So the file proves that something running under that account had
the brief. It does not prove which program wrote it, and there is no signature or agent-held key.

A receipt completed this way says so. Its metadata, shown by `answerback task <task-id>` and `task_get`, holds
`"result_authentication": "trusted_os_account"` and `"result_machine"`, and neither can change afterwards. Herdr
Core's machine record names no account, so the receipt records no `result_account`. `answerback doctor` and
`answerback status` print a `remote_trust` line that names every listed machine with the statement above.

To remove a machine from the list:

1. List its open receipts with `answerback tasks`, then `answerback task <task-id>` for each one whose
   `result_machine` is that machine.
2. Let each finish, or read its result file now with `answerback task <task-id> --wait <seconds>` or
   `answerback task <task-id> --reconcile`. Abandon one only after you have inspected its terminal.
3. Remove the name from `hc_send_machines` and restart the bridge.

The bridge does not refuse a restart that removes a machine with open receipts, because the configuration has no
record of them. It warns instead: `doctor` and `status` list them under `remote_unlisted_receipts`, and the bridge
log records a `remote_unlisted_receipts` event at start. Put the name back and restart to read their files again.

A `completed` remote receipt proves that a program under the listed machine's OS account wrote a file holding
this task's ID and token. Its `identity_observer` (`core:<machine>`) says that the before and after identity reads
came from that machine's Herdr Core. It does not prove that the selected agent wrote the file, that the work is
right, or that anything stopped. Remote peers advertise `result_file`, not `worker_report`, carry `cancel: false`,
and cannot report `failed` or `blocked`. Before sending, the bridge refuses briefs with provider credential shapes,
machines that are not connected or whose core id changed since it was pinned, and terminals outside a project
folder that `hc_placement_policy` sets
([guards before a remote send](HERDR-INTEGRATION.md#guards-before-a-remote-send-and-what-a-remote-receipt-says)).

## Recipient-reported completion

On three routes the recipient reports its own result: Herdr App/core (`hc`), the older Herdr TUI (`herdr`) and
KiroCrew (`crew`). The code calls this policy `worker_reports`. Owned workers (`codex_cli`, `claude_code`, `grok`
and managed `kiro`) do not use it and complete on their own final answer. The managed worker that
`answerback route` creates is a Herdr terminal, so it is an `hc` recipient and reports as one.

Each assignment on these routes carries a completion instruction. A recipient on this machine writes its result
and runs the generated command, or calls `task_complete` through its bridge connection:

```sh
answerback complete <assigned-task-id> --result-file /absolute/path/to/result.txt
```

An assignment to a terminal on another machine carries no such command. It tells the recipient to publish the
named result file and not to run an Answerback command or call `task_complete`.

A Core submission reserves a durable `pending` receipt and delivers in the background. An early result stays
provisional until the delivery identity checks succeed. `queued`, `submitted`, an idle screen and transport
acceptance do not mean the work is complete. Only a correlated final result completes a receipt.

## Provider backoff

Provider backoff is tested with scripted stores and fakes only; no live run is recorded.

The bridge keeps a pressure level per provider account (`kiro` for Kiro and Crew workers, `bedrock` for Claude
Code, Codex CLI and Grok Build) from the rate-limit signatures it sees ([events](EVENTS.md)). While a recipient's
account is held, `peer_submit` reserves and sends nothing. Like an ambiguous selection (`needs_selection`), the
reply is a result and not an error. It has these keys (values in angle brackets are placeholders):

```text
{"status": "provider_backoff", "provider": "<kiro or bedrock>", "account_hint": "<label>",
 "level": <level>, "until": <epoch-seconds>, "retry_after_s": <seconds>}
```

`until` is the wall-clock time, in Unix seconds, when the hold ends. `retry_after_s` is how long to wait before
sending the same request again with the same `request_id`. `answerback send` exits `1` on it.

A hold never hides a receipt: a repeat of a request that already has one (same `request_id`, peer and message) is
answered with that receipt, and the same `request_id` for different work is refused as usual. A peer whose account
the bridge cannot tell, such as an hc terminal running an unrecognized program, is never held.

Other senders wait instead of getting this reply. `bus_submit` queues
([agent bus](AGENT-BUS.md#provider-backoff-not_before)), and bridge-launched workers wait `queued` and are
relaunched once after a throttled turn
([owned workers](NATIVE-CLI.md#provider-backoff-the-queued-wait-and-one-relaunch)). `answerback route` releases
the slot it reserved when `peer_submit` answers this way, since no receipt follows.

While an account is held, the next assignment typed to one of its owned Kiro or native CLI workers or Herdr
terminals carries one extra line. By default it reads `Provider under rate pressure: fewer, larger tool calls; no
parallel sub-agents.`, and the `notice` key of the `pressure` block in `events.json` changes it. A worker gets it
before the message, and a Herdr terminal gets it after the `Bridge task: <id>` line. Only the typed text carries
it, and the receipt's message is the caller's.

For a Herdr terminal, a text that the line would push past the 131,071-byte bound is refused before reservation
while the account is held, and delivered without the line when pressure began after admission. Nothing is sent to
a worker mid-turn.

Every client named in `client_registrations` gets a `provider_pressure` notice in its inbox (`read_inbox`). The
three notice texts are, with placeholders for the provider, the hold's end in local time, the level and the
number of bridge-owned jobs in flight:

```text
<provider> throttled: dispatch paused until <HH:MM:SS> (level <level>), <count> workers in flight
<provider> backoff ended: dispatch resumed (level <level>)
<provider> pressure cleared
```

The first goes when an account enters pressure, and the second when its hold ends at `until`. The level is still
above 0 then and decays one step per clean window ([tracing](TRACING.md#provider-pressure)), and the later drop to
0 sends nothing more. A raised level inside a pause sends nothing new but moves the resume notice to the new
`until`.

`answerback pressure --clear <provider>` while held sends the third text instead of the resume notice. A hold
carried over a bridge restart sends no resume notice, and its later drop to level 0 sends the third text.

## Older Herdr TUI compatibility

For a deliberately selected older TUI session, make a separate configuration:

```sh
answerback --config ~/.config/herdr-bridge-tui/config.json configure --runtime tui --session <tui-session-name>
answerback --config ~/.config/herdr-bridge-tui/config.json start
```

Start that TUI session separately. `configure --session NAME` keeps its TUI meaning. Herdr App spaces use core,
and starting `herdr --session ...` inside an App terminal nests the older UI.

The legacy `install.sh` installer is TUI and Codex CLI specific: it always configures a TUI session and Codex
CLI's MCP entry, and does not set up Claude Code, Grok Build or KiroCrew. Its `--desktop ALIAS=CHAT_ID` option is
not part of the initial release. Use [client setup](CLIENT-SETUP.md) for the usual multi-client setup.

## What it cannot do yet

- **Agents on your other machines** are found and read, and refused as recipients unless you list their machine
  in `hc_send_machines`, which is empty by default. Remote result-file completion is qualified only from Linux to
  Linux with Codex CLI. Every other pair, macOS included, and Kiro CLI, Claude Code and Grok Build on any system
  stay excluded until they pass the [remote qualification checklist](LIVE-TEST.md#remote-result-file-qualification).
- **A second assignment to one Herdr terminal** needs `hc_send_to_done`, which is Experimental and off by default.
- **KiroCrew as a recipient** is excluded from R1. Its reports need an approval that also opens every bridge tool
  to Crew's agents, and a Crew chat cannot be cancelled per task.
- **Unattended Kiro workers** need the v2 engine and a trust setting, both Experimental and off by default. On v2
  the bridge cannot verify the MCP tools before the first call.
- **Remote receipts** cannot be cancelled, cannot carry a permission request back and cannot report `failed`.

## See also

- [Choose which worker takes the work](HARNESSES.md): the decision table and the release boundary.
- [Owned native CLI workers](NATIVE-CLI.md): Codex CLI, Claude Code and Grok Build.
- [Herdr integration](HERDR-INTEGRATION.md): every Herdr call the bridge makes.
- [Receipts](RECEIPTS.md): states, exit codes and waiting.
- [Troubleshooting](TROUBLESHOOTING.md): when a route refuses or a receipt stays open.
