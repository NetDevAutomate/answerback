# See who and what Answerback talks to

Answerback sits between you, the coding agents on your machine and an optional
Herdr session host. You authorize work, agents hand it to each other through
Answerback and read its receipts, and Answerback submits it over the recipient's
native protocol or a verified Herdr session.

This page is for a contributor or reviewer who wants the system boundary before
the code; read the [architecture overview](README.md) first.

To follow one hand-off, start at **Human operator**, who authorizes the work. An
agent in **Five CLI clients** hands it to **Answerback**, which submits it to the
recipient client, through **Herdr App / core** only when the recipient runs in a
Herdr terminal. The sender then reads the receipt from **Answerback**.

![Answerback C4 system context](c4-context.svg)

```mermaid
---
title: "Answerback: System context"
config:
  layout: elk
  theme: base
  flowchart:
    nodeSpacing: 55
    rankSpacing: 75
---
flowchart TB
    human["Human operator<br/><b>[Person]</b><br/>Authorizes scope and permission decisions"]
    answerback["<b>Answerback</b><br/>[Software system]<br/>Durable assignments, actor messages<br/>and correlated task receipts"]
    clients["<b>Five CLI clients</b><br/>[External software systems]<br/>Kiro CLI · KiroCrew · Codex CLI<br/>Claude Code · Grok Build"]
    herdr["<b>Herdr App / core</b><br/>[External software system]<br/>Optional CLI session host<br/>Identity and readiness reports"]

    human -->|"Authorizes and inspects work<br/>CLI / MCP"| answerback
    clients -->|"Exchanges project messages;<br/>reads receipts · MCP"| answerback
    answerback -->|"Submits prompts; observes results<br/>Selected native adapter"| clients
    answerback -->|"Checks and addresses a session<br/>hc CLI"| herdr
    herdr -->|"Hosts selected sessions<br/>Terminal"| clients

    classDef person fill:#123d64,stroke:#123d64,color:#fff
    classDef system fill:#1765aa,stroke:#0d4b82,color:#fff
    classDef external fill:#4b5563,stroke:#364152,color:#fff
    class human person
    class answerback system
    class clients,herdr external
```

*Who authorizes, sends and hosts work around Answerback. The arrow labels are
examples of what crosses each boundary, not a complete list.*

Native and Herdr routes are alternatives selected per recipient. The Herdr
arrow does not claim that every installed CLI version has been qualified there.
The current five-client project remains an acceptance gate; Desktop clients
are outside this view.

A provider plugin from another package extends the system the same way the
shipped providers do ([kernel and plugins](README.md#kernel-provider-plugins-and-modules)).
That is a packaging boundary, not an extra deployed service.

## Source evidence

- [Public provider catalog](../../src/answerback_bridge/adapters/catalog.py):
  opt-in native providers and optional `hc`/legacy providers.
- [Plugin contract and entry-point loader](../../src/answerback_bridge/plugin.py):
  `ProviderPlugin`, `ModulePlugin`, `KernelContext`, the `answerback.providers` and
  `answerback.modules` groups.
- [Native worker transport selection](../../src/answerback_bridge/providers/native/workers.py):
  `codex app-server`, Claude SDK and Grok ACP execution ownership.
- [Kiro worker ownership](../../src/answerback_bridge/providers/kiro/workers.py) and
  [Crew gateway client](../../src/answerback_bridge/providers/crew/client.py).
- [Core identity/readiness checks](../../src/answerback_bridge/providers/herdr/core.py) and
  [Core adapter](../../src/answerback_bridge/providers/herdr/adapters/core.py).
- [Actor MCP frontend](../../src/answerback_bridge/actors/frontend.py):
  enrolled send, receive, acknowledge and reply operations.

## See also

- [Architecture overview](README.md): the kernel, plugins and modules behind the Answerback box.
- [Local containers](c4-containers.md): the processes and stores inside it.
- [Adapter contract](../ADAPTERS.md): what each provider must implement.
