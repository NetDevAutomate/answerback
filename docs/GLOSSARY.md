# Glossary

Each term below has one meaning across these guides. Each entry links to the page that has the
full detail. Terms are in alphabetical order.

### ACP

Agent Client Protocol: the open protocol Answerback uses to run Kiro CLI and Grok Build owned
workers. Codex CLI workers run over `codex app-server` instead, and Claude Code workers through
its own SDK. See: [Protocols](PROTOCOLS.md#why-acp-now).

### Actor

One enrolled participant in an actor-messages run, given a role such as coordinator, author or
reviewer and bound to one observed native session for that run only. Actor messages use the same
NATS broker as the agent bus, on a separate stream with separate receipts. See:
[Actor messages](ACTOR-MESSAGES.md#supervised-enrollment).

### AG-UI

An open event format for agent activity. Answerback writes its observe-only event stream in
AG-UI 1.0's event vocabulary. Nothing on that stream accepts a command: work is still sent with
`peer_submit` or `bus_submit`. See: [Events](EVENTS.md#what-is-emitted).

### Agent bus

The optional durable command queue, the `bus` module. `agent-bus` commits a command locally first,
then publishes it to NATS JetStream, so it survives a disconnected producer, a broker restart or a
coordinator restart. It is a separate channel from actor messages (see Actor). See:
[Agent bus](AGENT-BUS.md).

### Binding

A digest of the identity the bridge observed for a peer when it was discovered: an agent in a
Herdr terminal, a KiroCrew chat, or a peer of any provider plugin that declares one. Pass the
binding back on send, and the bridge refuses the call, with no task created, if the terminal or
chat has since been replaced. The remedy is to search again; the refusal text is the general one
and does not say so. An owned worker has no binding. See:
[Protocols](PROTOCOLS.md#what-binding-is).

### Command receipt

The bus's record of whether a queued command was accepted, claimed and bound to a task. A
`bound` command receipt means a task receipt exists. Read that task's own status next, because
delivery and execution are two separate receipts (see Receipt). See:
[Agent bus](AGENT-BUS.md#delivery-and-execution-have-different-receipts).

### Composition

The set of plugin packages installed beside the kernel. The public composition is the kernel with
its own providers and the `bus` module. A composition can add a provider package with `--with` in
the same `uv tool install` command. See:
[Testing](TESTING-HARNESSES.md#the-two-compositions).

### Coordinator

The one process per machine that claims a durable agent-bus command and admits it to a provider
plugin. This differs from the "coordinator" role inside an actor-messages run, which is a label on
one participant. See: [Agent bus](AGENT-BUS.md#how-a-command-moves).

### Hand-off

One bounded assignment from one agent to another, with one receipt. A hand-off is sent with
`peer_submit` or `answerback send` under a request ID. See:
[Getting started](GETTING-STARTED.md#then-hand-off-real-work).

### Harness

The CLI or app an agent runs in: Claude Code, Codex CLI, Grok Build, Kiro CLI or KiroCrew. Each one
needs its own execution contract before Answerback can hand it work. See:
[Harnesses](HARNESSES.md).

### hc

The provider that addresses an agent already running in a Herdr App/core terminal, with an
address such as `hc:t7`. Only a recognized, live agent that reports `idle`, or `done` when
`hc_send_to_done` is on, can receive a new assignment this way. A terminal on another machine can
receive one only when that machine is listed in `hc_send_machines`. See:
[Recipients](RECIPIENTS.md#herdr-appcore-readiness).

### hc_send_machines

A bridge setting: the names of the other machines, as `hc machine ls` prints them, whose Herdr
App/core terminals can receive work. It is empty by default, which keeps every terminal on another
machine read-only. Listing a machine means trusting the process identity that machine's core
reports, and that machine's OS user: any program of that user can write the result file that
completes a task sent there. The bridge reads the setting when it starts. See:
[Client setup](CLIENT-SETUP.md#4-verify-discovery-and-choose-recipients).

### hc_send_to_done

A bridge setting, `false` by default: only an agent in a Herdr App/core terminal that reports
`idle` can receive work. Set it to `true` and one that reports `done` (it has finished a turn) can
receive work too; that send has no state condition. The bridge reads the setting when it starts.
See: [Recipients](RECIPIENTS.md#herdr-appcore-readiness).

### Herdr

A terminal multiplexer for coding agents, by a separate project, and an optional recipient host
for Answerback: the bridge can discover agents in its terminals, either the App ("core") or an
older TUI. It is not the message bus and does not own the receipt protocol. See:
[Harnesses](HARNESSES.md#transport-and-application-evidence).

### Kernel

The part of the bridge that owns receipts, routing, the configuration file, the service and the
socket. The kernel names no provider. It builds its commands, RPC verbs and MCP tools from what the
loaded provider plugins and modules declare. See:
[Architecture](architecture/README.md#kernel-provider-plugins-and-modules).

### KiroCrew

The `crew` provider: existing or dedicated KiroCrew gateway chats, reached through their own
authenticated local API. An explicit worker result is required; a successful HTTP response alone
does not complete a task. See: [Recipients](RECIPIENTS.md#kirocrew).

### Managed worker

A Herdr App/core terminal tab that `answerback route` or `workers create` starts and tracks in its
own registry, then addresses through the `hc` provider once it reports idle. It runs in a visible
terminal, so it is not an owned worker (see Owned worker). `workers gc` removes only the ones this
tool created. The R1 support table and the Kiro guides still say "managed Kiro workers" for Kiro
owned workers; that pinned wording predates this term. See:
[Routing](ROUTING.md#which-terminals-gc-may-remove).

### MCP

Model Context Protocol: the tool-calling interface an agent client such as Claude Code, Codex CLI,
Grok Build or Kiro CLI uses to call Answerback's tools, for example `peer_search` and `task_wait`.
See: [Client setup](CLIENT-SETUP.md).

### Module

A service the kernel loads from the `answerback.modules` entry-point group. Today the only module
is `bus`, which owns the agent bus and actor messages. See:
[Adapter contract](ADAPTERS.md#modules).

### Owned worker

A session the bridge starts and owns for one assignment: Codex CLI, Claude Code, Grok Build or
Kiro CLI. It never takes over a session you have open, and it is not the managed worker that
`route` creates (see Managed worker). See: [Native CLI workers](NATIVE-CLI.md).

### Peer

The word the tools use for a recipient (see Recipient), as in `peer_search`, `peer_submit` and the
`peer_id` field. See: [Recipients](RECIPIENTS.md#r1-route-support).

### Provider

The adapter Answerback uses to reach one kind of recipient: `hc`, `herdr`, `codex`, `kiro`,
`crew`, `rex`, `codex_cli`, `claude_code` or `grok` in the public composition. Choosing one
filters which recipients a search can find or send to. Each provider comes from a provider plugin
(see Provider plugin). See: [Recipients](RECIPIENTS.md#r1-route-support).

### Provider plugin

A Python package that the kernel loads from the `answerback.providers` entry-point group and that
supplies one or more providers. The kernel's own providers load this way, and so does a provider
package installed alongside the kernel. A configuration that names a provider no installed plugin
claims is kept, and `answerback doctor` reports that provider as `disabled`. See:
[Adapter contract](ADAPTERS.md#composition-the-plugin-half) and
[Provider template](PROVIDER-TEMPLATE.md).

### Receipt (task receipt)

The record of one hand-off's execution. Its status is one of ten values: `pending`, `queued`,
`submitted`, `running`, `blocked`, `uncertain`, `completed`, `failed`, `abandoned` or `cancelled`.
Only `completed` means the recipient's own matching evidence confirmed the work. A command receipt
confirms only delivery (see Command receipt). See: [Receipts](RECEIPTS.md).

### Recipient

Whoever receives a hand-off: an owned worker, an agent in a Herdr terminal, a KiroCrew chat or an
enrolled Rex conversation. Each has an address made of its provider's name, a colon and an ID,
such as `codex_cli:reviewer`. See: [Harnesses](HARNESSES.md#pick-a-recipient).

### Request ID

The caller-chosen key for one hand-off. Reusing it returns the existing receipt; a different
message, recipient or subject, or on the bus a different TTL, under the same key is refused rather than sent again. See:
[Receipts](RECEIPTS.md#done-is-not-evidence).

### Route

`answerback route` classifies a brief against a policy file and picks the agent that should get
it: it reuses an idle agent in a Herdr App/core terminal, managed or not, or creates a managed
worker. It never dispatches to an owned worker, and no model is consulted. See:
[Routing](ROUTING.md#what-a-route-does).

### send_ready

A flag the bridge reports for an `hc` peer: true only for a recognized, live agent that can
receive a new assignment now. The agent qualifies when it reports `idle`, or `done` when
`hc_send_to_done` is on. Working, blocked, errored and unknown-status peers stay read-only, and so
do peers on another machine unless that machine is listed in `hc_send_machines`. The field beside
it, `send_readiness_reason`, says why a peer is not ready. See:
[Recipients](RECIPIENTS.md#herdr-appcore-readiness).

### Subject

Optional text the sender stores on a receipt to say what the hand-off is about, such as a commit
SHA, an artifact digest or a release tag: 1 to 256 printable characters, set with `send
--subject` or the `subject` argument of `peer_submit`, and never changed afterwards. `tasks
--subject` finds the receipts with exactly that subject. The bridge never checks a subject
against anything, and it proves nothing about the work. See:
[Receipts](RECEIPTS.md#name-what-a-hand-off-is-about).

### Uncertain

A receipt status: the bridge could not settle the receipt, for example after an interrupted
connection, a service restart or a missing final response. The recipient stays reserved for
investigation, and the bridge never re-sends the work automatically. A person releases it with
`answerback abandon <task-id> --reason "<what you verified>"`. See:
[Receipts](RECEIPTS.md#done-is-not-evidence).

## See also

- [Documentation map](README.md): every guide, by what you are trying to do.
