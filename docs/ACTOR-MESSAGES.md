# Let enrolled agents message each other

Actor messaging lets agents that are already running exchange project messages through the
bus, each under an identity the operator enrolled, with an acknowledgement for every
message read. It does not start a model, wake a chat or reserve an execution task; it moves
messages between the participants of one bounded run.

This page is for the operator who runs such a run. The [agent bus](AGENT-BUS.md) must be
configured and started first; the actor stream uses its connection and needs NATS Server
2.10.0 or newer.

## The shortest path

Create the run with its complete role list, stage one actor per role, record the native
session you observed for each, then seal the roster:

```sh
answerback actors create --project /absolute/project \
  --role coordinator --role author --role reviewer
answerback actors stage RUN_ID --role author --harness codex --binding-file /private/author.json
answerback actors activate RUN_ID ACTOR_ID --native-binding-file /private/author-native.json
answerback actors seal RUN_ID --owner-token-file /private/owner.token
```

Real output of `create`, trimmed to five of its fields:

```json
{
  "run_id": "<run-id>",
  "state": "staging",
  "expires_at": 1795642419.4624557,
  "expected_roles": ["author", "coordinator", "reviewer"],
  "owner_token": "<owner-token>"
}
```

`state: "staging"` is a run that accepts enrollment; `expires_at` is `--ttl` seconds after
creation (default 3600, at most 24 hours). `stage` answers with the actor's `actor_id`,
`epoch: 1` and `state: "staged"`, and writes the capability file. `seal` moves the run to
`sealed`, after which actors can send. `owner_token` is shown this once (below).

Then give each native session its own STDIO MCP entry with
`mcp --actor-binding-file /private/author.json`, which exposes the four actor tools and
nothing else (the entry is under [Supervised enrollment](#supervised-enrollment)). The
`--config /absolute/config.json` the commands above omit is needed wherever the bridge does
not use the default configuration path.

## Delivery and evidence

```mermaid
flowchart LR
    A[Bound MCP frontend] --> O[Actor ledger and outbox]
    O --> N[NATS JetStream]
    N --> I[Recipient inbox]
    I --> R[Explicit model read]
    R --> K[Explicit acknowledgement]
    K --> P[Correlated reply through the same bus]
```

*One message's path: committed to the actor ledger, published, read and acknowledged by the
recipient's model, answered over the same bus.*

The frontend exposes four tools, and nothing else:

| Tool | Purpose |
| --- | --- |
| `actor_send` | Persist a message for an exact enrolled recipient |
| `actor_receive` | Read delivered messages and obtain their receipt nonces |
| `actor_ack` | Acknowledge one exact message after reading it |
| `actor_reply` | Reply to an acknowledged request; the service derives the recipient |

Every call carries an identity the frontend supplies: the model cannot choose a sender, run,
credential or epoch in tool arguments. An epoch is a generation number for one actor's
enrollment in a run; re-enrolling the same actor before the run is sealed needs a higher
epoch, and the previous one stays invalid. The first authenticated frontend is pinned to its
epoch, and a capability reused in another frontend is refused. Before sealing, supervised
recovery may enroll a fresh epoch; after sealing, a replacement needs a new run, because the
roster is immutable. Neither path replays unfinished work.

An acknowledgement proves that the bound client called `actor_ack`. Proving that a
particular model conversation did so needs that conversation's native tool-call trace; a
shared MCP process alone cannot give that provenance. Native results and the project's
acceptance tests stay separate evidence.

## Supervised enrollment

`create` returns the generated `run_id` and an `owner_token`, shown once. Save the token in
a private file (owned by you, mode 0600, not a symlink): it authorises inspection of the
whole run, also after expiry, and sealing or finishing it. The ledger stores only its
digest, and no later result returns it. An unchanged `actors create --run-id RUN_ID …` retry
needs `--owner-token-file PATH` or `--binding-file PATH` and returns the existing status
without issuing a token.

`stage` creates the capability file exclusively, owner-only, and never overwrites an
existing file or prints the credential. `activate` records the native session you observed
for that actor; native bindings are immutable. Keep these files outside the project. Once
every expected role has an active binding, `seal` freezes the roster. An actor may read
while the run is staging and cannot send until it is sealed.

Give each native session its own STDIO MCP entry, which exposes the four actor tools only;
discovery, task submission, administration and inbox tools are absent, and `--client` has
no effect in this mode:

```json
{
  "command": "/absolute/answerback",
  "args": [
    "--config", "/absolute/config.json",
    "mcp", "--actor-binding-file", "/private/author.json"
  ]
}
```

Only the `coordinator` role can send `assignment`, `correction` or `finish`, and it cannot
send `review_request` or `review_reply`. Participants send their review requests first,
then keep reading, acknowledging and replying while their own requests are outstanding.
Blocking on another participant while refusing its incoming messages is a cyclic wait.

## Retries and limits

Use a stable `request_id` for each send and reply. Retrying unchanged content returns the
original message; changed content under the same identity is refused. Broker duplicate
suppression is an optimisation: the SQLite ledger keeps idempotency across broker restarts
and lost publish acknowledgements. There is no direct outbox-to-inbox delivery when NATS
is unavailable: the message waits in the outbox.

| Item | Bound |
| --- | --- |
| Body | 16 KiB |
| Envelope | 64 KiB |
| One `actor_receive` | 8 messages, waits up to 20 seconds |
| One run | 256 messages, 16 roles, at most 24 hours |
| Open runs | 8 unexpired at once |
| One actor | 128 of a run's 256 messages; 4 waiting `actor_receive` calls of the service's 16 |

Past a per-actor limit the call is refused with `capacity`. Messages are stored separately
in `bus/actors.sqlite`; task maintenance does not prune this ledger.

## Inspect, revoke and finish

`actors status RUN_ID`, `actors roster RUN_ID` and `actors export RUN_ID` need exactly one
proof:

| Option | Proof |
| --- | --- |
| `--owner-token-file PATH` | A private file (owned by you, mode 0600, not a symlink) holding the token returned when the run was created |
| `--binding-file PATH` | An existing private capability file of a non-revoked actor in this run |
| `--owner-token TOKEN` | Deprecated, removed in the next release: the token on the command line, where other local accounts can read it in process listings. It prints a warning |

```sh
answerback --config /absolute/config.json actors export RUN_ID --binding-file /private/author.json
```

These reads return the complete run for either proof, including other participants' message
bodies and native bindings, and omit credentials, credential hashes and receipt nonces. A
credential from another run or a revoked epoch is refused. Inspection does not pin or
replace the actor's messaging frontend and stays available after the run expires or
finishes. Possession of a participant file grants run inspection: treat it, the owner token
and exported content as project-confidential.

`actors revoke RUN_ID ACTOR_ID --reason TEXT` revokes one epoch without deleting evidence.
`actors finish RUN_ID --reason TEXT --owner-token-file PATH` closes messaging and keeps the
evidence; `seal`, `finish` and a repeated `create` also accept `--binding-file`. Finishing
is administrative: it certifies neither project success nor task completion. Stop the native
sessions and remove their temporary MCP entries only after collecting their final evidence.

### A lost owner token

The token cannot be shown again. Without it, a retained non-revoked participant binding
still serves for status, roster, export, seal, finish and an unchanged create retry. For an
older run with no enrolled participant, stage an expected role and use its new binding.
There is no token reset or automatic grant. Opening an older ledger adds a nullable
owner-token digest column; existing runs receive no token.

If no usable proof remains, the CLI refuses inspection, seal, finish and repeated create.
The OS owner can still read the evidence from the ledger, read-only; protect the output as
project content:

```sh
sqlite3 -readonly /absolute/bus/actors.sqlite <<'SQL'
BEGIN;
SELECT payload FROM actor_messages WHERE run_id = 'RUN_ID'
  ORDER BY created_at, message_id;
SELECT actor_id, epoch, role, native_binding FROM actors WHERE run_id = 'RUN_ID'
  ORDER BY role, epoch;
COMMIT;
SQL
```

This reads evidence only. It restores no CLI access and reactivates no actor, and records on
disk prove neither that a model read a message nor that the work completed.

## What it cannot do yet

- No live-model run of actor messaging is recorded; the recorded qualification is an
  installed test with scripted participants ([VALIDATION.md](../VALIDATION.md)).
- While NATS is down a sent message waits in the actor outbox. Nothing delivers it to the
  recipient's inbox without the broker.
- `--owner-token` on the command line still works, with a warning, and goes away in the
  next release.

## See also

- [AGENT-BUS.md](AGENT-BUS.md): setting up the bus the actor stream runs on.
- [ADAPTERS.md](ADAPTERS.md#modules): the bus module that owns actor admission.
- [VALIDATION.md](../VALIDATION.md): the recorded actor qualification.
- [GLOSSARY.md](GLOSSARY.md): actor, epoch, coordinator.
