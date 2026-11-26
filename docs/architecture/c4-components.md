# Trace a command through the bridge

Inside the one bridge process, a command passes through the coordinator, the
adapter router and one provider adapter, and ends as a task receipt. Actor
messages among already-enrolled sessions travel a separate, independently
scheduled lane, so slow task admission does not stop them.

This page is for a contributor changing the coordinator, a provider adapter or
the actor lane; read [the processes and stores on one machine](c4-containers.md)
first.

Follow one bus command: **Bridge operations** stores it in **Command
persistence**, and the **Command coordinator** claims it there and hands it to
the **Adapter router and registry**. The router invokes the selected **Native and
host adapters**, which persist the native outcome in **Task persistence**, where
the coordinator reads the receipt. A direct `peer_submit` skips the coordinator
and enters at the router.

![Answerback C4 bridge components](c4-components.svg)

```mermaid
---
title: "Answerback: Bridge components"
config:
  layout: elk
  theme: base
  flowchart:
    nodeSpacing: 55
    rankSpacing: 75
---
flowchart TB
    subgraph service["Bridge service · one Python process"]
        api["<b>Bridge operations</b><br/>[Component · service / BusAPI / ActorAPI]<br/>Routes operations; authenticates actors"]
        coordinator["<b>Command coordinator</b><br/>[Component · Coordinator / BridgeGateway]<br/>Claims once; checks peer/project;<br/>reconciles interrupted admission"]
        router["<b>Adapter router and registry</b><br/>[Component · AdapterRouter / AdapterRegistry]<br/>Selects provider; validates receipt identity"]
        providers["<b>Native and host adapters</b><br/>[Component · ProviderAdapter implementations]<br/>Own submission, permissions<br/>and completion interpretation"]
        actorlane["<b>Actor delivery lane</b><br/>[Component · ActorLane / asyncio task]<br/>Publishes outbox; stores broker envelopes"]
        commandstore["<b>Command persistence</b><br/>[Component · BusStore / SQLite]<br/>Claims, idempotency, command/event outboxes"]
        taskstore["<b>Task persistence</b><br/>[Component · TaskStore / SQLite]<br/>Reservation and correlated native outcome"]
        actorstore["<b>Actor persistence</b><br/>[Component · ActorStore / SQLite]<br/>Run/epoch binding; delivery/read/ack/reply facts"]

        api -->|"Stores assignment"| commandstore
        coordinator -->|"Claims and reconciles"| commandstore
        coordinator -->|"Prepares and sends"| router
        api -->|"Submits direct work"| router
        router -->|"Invokes selected contract"| providers
        providers -->|"Persists native evidence"| taskstore
        coordinator -->|"Reads through gateway"| taskstore
        api -->|"Sends, reads, acknowledges, replies"| actorstore
        actorlane -->|"Publishes pending envelopes;<br/>records broker delivery"| actorstore
    end

    classDef component fill:#e5eff9,stroke:#3774ad,color:#102f4c
    classDef persistence fill:#f0f3f7,stroke:#66788c,color:#1f334a
    classDef actor fill:#e6f4ef,stroke:#288568,color:#153d31
    class api,coordinator,router,providers component
    class commandstore,taskstore persistence
    class actorlane,actorstore actor
    style service fill:#fbfcfe,stroke:#3774ad,stroke-dasharray:6 4
```

*The parts of the bridge service and the stores they write. The arrow labels are
examples of what passes between parts, not a complete list.*

The persistence components wrap SQLite stores; their physical databases are
shown at container level. Broker connections and
native client processes are omitted here; the container diagram shows those
external dependencies.

This diagram does not yet show three newer parts. The adapter router and
registry are now filled from provider plugins, and the coordinator and actor
lane belong to the bus module
([kernel and plugins](README.md#kernel-provider-plugins-and-modules)). The worker-tracing and
event-stream hub (`answerback debug`, `answerback events`) runs inside the
bridge process. The routing and managed-worker-pool orchestrator
(`answerback route`, `answerback workers`) does not: it runs in the command's
own process and reaches the bridge over the same private socket as any other
client.
[Routing](../ROUTING.md), [events](../EVENTS.md) and [tracing](../TRACING.md)
describe them as they exist today.

## Contract that keeps the broker independent

The coordinator uses this small Python protocol from
[bus/coordinator.py](../../src/answerback_bridge/bus/coordinator.py):

```python
class Gateway(Protocol):
    async def prepare(self, command: Command) -> PreparedTarget: ...
    async def send(self, target: PreparedTarget, command: Command) -> TaskReceipt: ...
    def lookup(self, command: Command) -> TaskReceipt | None: ...
    def get(self, task_id: str) -> TaskReceipt: ...
```

`BridgeGateway` supplies the adapter-backed implementation. The coordinator
needs stable admission and lookup operations; it does not need to understand a
Codex turn, a Claude prompt or a terminal session. This boundary allows an adapter
to change without making NATS responsible for native execution policy.

A command is checked against the local journal before dispatch, then claimed
before native submission. If an admission response is lost, lookup can bind
the existing receipt; missing or contradictory evidence leaves uncertainty.
The coordinator never automatically resends claimed work.

Actor delivery uses the existing JetStream connection, a separate stream and
consumer, and an independently scheduled task. A message is written to the
recipient inbox before the broker is acknowledged. The model must subsequently
call `actor_receive`, `actor_ack` and, when appropriate, `actor_reply`.
Those calls do not create a native execution task or wake an inactive client.

## Source evidence

- [Coordinator and gateway protocol](../../src/answerback_bridge/bus/coordinator.py)
  and [adapter-backed gateway](../../src/answerback_bridge/bus/gateway.py).
- [Adapter router](../../src/answerback_bridge/adapters/router.py),
  [registry](../../src/answerback_bridge/adapters/registry.py) and
  [composition root](../../src/answerback_bridge/adapters/factory.py).
- [Provider contracts](../../src/answerback_bridge/adapters/contracts.py) and
  [catalog](../../src/answerback_bridge/adapters/catalog.py); the provider packages under
  [`providers/`](../../src/answerback_bridge/providers/) and the
  [bus module](../../src/answerback_bridge/bus/plugin.py), composed by
  [`plugin.py`](../../src/answerback_bridge/plugin.py).
- [Actor API](../../src/answerback_bridge/actors/api.py),
  [store](../../src/answerback_bridge/actors/store.py) and
  [delivery transport](../../src/answerback_bridge/actors/transport.py).

## See also

- [Architecture overview](README.md): the kernel, plugins and modules that fill these components.
- [Find the processes and stores on one machine](c4-containers.md): where these components run.
- [Adapter contract](../ADAPTERS.md): what a provider adapter must implement.
