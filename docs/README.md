# Documentation

Every guide here, grouped by what you are trying to do. Each guide opens with what it lets you do
and what you need first. New here? Start with the first table: run the self-test and make one
hand-off, then see whether Answerback fits your work. Unfamiliar terms are in the glossary.

## Start and decide

| Guide | Read it to |
| --- | --- |
| [GETTING-STARTED.md](GETTING-STARTED.md) | Run the self-test, install the command, start the bridge and make a first hand-off |
| [USE-CASES.md](USE-CASES.md) | See seven scenarios with their commands, results and limits, and the project's own record |
| [RECEIPTS.md](RECEIPTS.md) | Tell whether a hand-off is done: the ten states, the exit codes and what a receipt proves |
| [GLOSSARY.md](GLOSSARY.md) | Look up a term; each entry links to the guide with the detail |

## Connect clients and choose recipients

| Guide | Read it to |
| --- | --- |
| [CLIENT-SETUP.md](CLIENT-SETUP.md) | Register the MCP connection and the skills in Claude Code, Codex CLI, Grok Build, Kiro CLI or KiroCrew |
| [HARNESSES.md](HARNESSES.md) | Choose which worker takes the work, and read the release support boundary |
| [RECIPIENTS.md](RECIPIENTS.md) | Set up each route that can receive work: Kiro workers, KiroCrew, Herdr terminals, remote results |
| [NATIVE-CLI.md](NATIVE-CLI.md) | Hand work to a Codex CLI, Claude Code or Grok Build worker the bridge starts and owns |
| [REX.md](REX.md) | Hand work to a conversation enrolled through Rex |
| [HERDR-INTEGRATION.md](HERDR-INTEGRATION.md) | Send work to agents in Herdr terminals, here and on other machines, and see what the bridge uses from Herdr |
| [REGISTRY.md](REGISTRY.md) | Limit which providers a client sees, and read presence and inboxes |
| [PROTOCOLS.md](PROTOCOLS.md) | Understand how the bridge talks to each agent: ACP, binding, refusals and the RPC error contract |

## Run it day to day

| Guide | Read it to |
| --- | --- |
| [OPERATIONS.md](OPERATIONS.md) | Start, stop and inspect the service, and find its configuration and files |
| [ANSWERBACK-UPGRADE.md](ANSWERBACK-UPGRADE.md) | Upgrade an installed bridge, including to the 0.16 provider plugins and configuration layout |
| [AGENT-BUS.md](AGENT-BUS.md) | Queue work that survives a restart, with the optional bus |
| [ACTOR-MESSAGES.md](ACTOR-MESSAGES.md) | Let enrolled agents message each other over the bus |
| [ROUTING.md](ROUTING.md) | Let a policy file choose the agent for a brief, and clean up the workers it starts |
| [MAINTENANCE.md](MAINTENANCE.md) | Inspect the receipt stores, then prune and compact them from a reviewable plan |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Go from a symptom to its cause and the command to run |

## Watch and place work

| Guide | Read it to |
| --- | --- |
| [EVENTS.md](EVENTS.md) | Follow the read-only event stream, the board and the dashboard; Answerback for macOS |
| [TRACING.md](TRACING.md) | Tune the alerts for stuck, hung and hot workers, and provider rate pressure |
| [CAPACITY.md](CAPACITY.md) | Read a machine's load and memory before you place work, and preview a move with `rebalance` |
| [HERDR-APP-MOD.md](HERDR-APP-MOD.md) | Install the Answerback side panel and themes in the Herdr App |
| [FLEET.md](FLEET.md) | Publish the fleet document the Herdr App's panel reads |
| [FLEET-SNAPSHOT.md](FLEET-SNAPSHOT.md) | Read fleet health and receipt summaries in one bounded call, for a program that writes a fleet document |

## Understand the design and extend it

| Guide | Read it to |
| --- | --- |
| [architecture/README.md](architecture/README.md) | See how Answerback is built: the kernel, provider plugins and modules, and the offline diagram viewer (`architecture/index.html`) |
| [architecture/c4-context.md](architecture/c4-context.md) | See who and what Answerback talks to |
| [architecture/c4-containers.md](architecture/c4-containers.md) | Find the processes and stores on one machine |
| [architecture/c4-components.md](architecture/c4-components.md) | Trace a command through the bridge |
| [ADAPTERS.md](ADAPTERS.md) | Build a provider plugin against the adapter contract and its receipt invariants |
| [PROVIDER-TEMPLATE.md](PROVIDER-TEMPLATE.md) | Package a provider plugin: layout, metadata, configuration block and conformance test |

## Test and check the claims

| Guide | Read it to |
| --- | --- |
| [TESTING-HARNESSES.md](TESTING-HARNESSES.md) | Choose a test layer for a change: the gate, the suite lock, the public-tree assertion, offline CI |
| [LIVE-TEST.md](LIVE-TEST.md) | Check a route with a live model, and read the recorded runs |
| [WORKTREE-GUARD-SCENARIO.md](WORKTREE-GUARD-SCENARIO.md) | Read the five-client acceptance project and its one attempt, CLI-1 |
| [evidence/worktree-guard/README.md](evidence/worktree-guard/README.md) | Read the versioned record contract for a Worktree Guard attempt |
| [../VALIDATION.md](../VALIDATION.md) | See every qualification run, including the ones that failed |
| [../CHANGELOG.md](../CHANGELOG.md) | See what changed in each version |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Set up a checkout, run the checks and follow the conventions |
| [../SECURITY.md](../SECURITY.md) | Read the trust model on one page, and report a problem |
