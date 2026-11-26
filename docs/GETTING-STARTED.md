# Install the bridge and make your first hand-off

From a copy of the source tree, you can run the scripted self-test, install the `answerback`
command, start the bridge and hand one task to a worker. What you get back is a receipt that
reads `completed` only when the worker's own final answer came back for that request.

This page is for someone with a clone of the source tree and [`uv`](https://docs.astral.sh/uv/)
on macOS or Linux. There is no package on PyPI yet, so every step installs from the tree. Python
3.11 to 3.15 is required; `uv` fetches one if you have none.

## Prove the plumbing: the self-test

From the source tree, run `uv run --no-dev answerback self-test`. It uses scripted agents, needs
no account, no API key and no model, and never reads your configuration or talks to a bridge you
already run. The first run creates a `.venv` in the source tree and downloads any dependencies
that `uv` has not already cached, so it normally needs a network connection. Later runs need no
network and take about five seconds.

```sh
uv run --no-dev answerback self-test
```

Two scripted agents hand each other a task through a throwaway bridge, both requests are sent
again, the service restarts, and everything is deleted. Real output, trimmed to five of its
seven phases and to the fields that matter here:

```json
{
  "success": true,
  "phases": [
    {"name": "herdr_to_kiro", "status": "passed", "nonce_match": true},
    {"name": "kiro_to_herdr", "status": "passed", "nonce_match": true},
    {"name": "idempotency", "status": "passed", "prompt_counts": {"herdr": 2, "acp": 2}},
    {"name": "restart", "status": "passed", "receipts_verified": 4},
    {"name": "cleanup", "status": "passed", "temporary_directory_removed": true}
  ]
}
```

Each hand-off returned its one-time token, neither repeat ran twice, and all four receipts
survived the restart. The two phases not shown, `preflight` and `reverse_preflight`, check
that each scripted agent is reachable before the hand-off in that direction.

| Phase | What it proves |
| --- | --- |
| `herdr_to_kiro`, `kiro_to_herdr` | One hand-off in each direction. Each carries a one-time token that has to come back (`nonce_match`). |
| `idempotency` | Both requests were sent again and neither ran twice: each agent still saw two prompts. |
| `restart` | All four receipts are still there, and still correct, after the service restarts. |
| `cleanup` | The temporary bridge has stopped and its directory is gone. |

The agents are scripted. One stands in for an agent in a Herdr terminal and the other for Kiro
CLI, which is where the phase names come from. The command line, the socket, the receipt store
and the two protocols are the real ones. The report states its own limit in its `limitations`
field: scripted agents prove the plumbing and say nothing about a live model, a login or an
approval. If `"success"` is not `true`, see
[The first run](TROUBLESHOOTING.md#the-first-run).

## Install and start

```sh
uv tool install .
answerback configure
answerback start
```

`uv tool install .` installs the public composition: the kernel with its own providers for
Codex CLI, Claude Code, Grok Build, Kiro CLI, KiroCrew, Rex and the two Herdr routes, and the
optional `bus` module. Name every extra you want in that one command: `'.[claude]'` to hand
work to Claude Code, `'.[bus]'` for the bus, `'.[claude,bus]'` for both. Each `uv tool install`
replaces the earlier set of extras, so `'.[bus]'` on its own removes `claude`. The internal
provider package is the same kernel plus one more provider, installed with `--with` in the
same command; nothing else on this page depends on it.

`configure` asks nothing. On a fresh install it writes one configuration file and prints its
path, normally `~/.config/answerback-bridge/config.json`. If a configuration already exists,
including one under the tool's earlier names (`herdr-bridge`, `codex-herdr-bridge`), it changes
nothing, reports `Configuration already exists` and exits `1`; that configuration stays in use.

`start` runs one bridge for your OS user and starts it again at login. On macOS it installs a
launchd agent. On Linux it writes a systemd user unit; the unit is generated and tested, but no
live run of it is on record.

To back out later, `answerback uninstall --remove-tool` stops the bridge, removes the service
that `start` installed, removes the Herdr App side panel it put in (its files that are as
shipped; the themes stay) and then uninstalls the tool. If the optional bus is configured, its
separately supervised broker must be stopped first.
Run `agent-bus stop` before `answerback uninstall --remove-tool`.
Your configuration and receipts stay on disk.

## Then hand off real work

One command-line agent is enough, and it can be the one you already use: the bridge starts a
second, separate session of it as the worker. That session runs on your own account with the
CLI's existing login, so the CLI must be installed and logged in, and what it does counts
against that account's usage like any other session. With Codex CLI as the worker:

```sh
answerback workspace add reviewer --provider codex_cli --cwd "$PWD"
answerback send codex_cli:reviewer --request-id hello-1 --subject hello \
  --message "Reply exactly ANSWERBACK_OK."
answerback task --peer codex_cli:reviewer --wait 30
```

Or with Claude Code as the worker (installed with the `claude` extra, above):

```sh
answerback workspace add reviewer --provider claude_code --cwd "$PWD"
answerback send claude_code:reviewer --request-id hello-1 --message "Reply exactly ANSWERBACK_OK."
answerback task --peer claude_code:reviewer --wait 30
```

Run one of the two. `workspace add` registers an owned worker: a session the bridge starts and
owns, never a terminal you have open. `--provider` takes `codex_cli`, `claude_code`, `grok` or
`kiro`, and a worker's address is its provider's name, a colon and the alias you chose.

`send` prints the receipt with `"status": "pending"`. `task --wait 30` waits up to thirty
seconds, the longest wait the command accepts, and prints the receipt again. It exits `0` only
for a completed receipt, which reads like this (three of its fields):

```json
{
  "request_key": "hello-1",
  "status": "completed",
  "result": "ANSWERBACK_OK"
}
```

`--subject` is optional. It labels the receipt with what the work is about, here the word
`hello`; for real work it is usually the commit SHA under review, and `answerback tasks
--subject <text>` lists the receipts with it, newest first ([Receipts](RECEIPTS.md#name-what-a-hand-off-is-about)).
The bridge stores it and never checks it.

A receipt that reads `completed` with `ANSWERBACK_OK` is a live model round trip through a
worker, on your machine and on this version. The receipt never includes the message you sent.
Sending the same request ID again returns the same receipt and runs nothing twice; the same ID
with a different message or subject, or for a different peer, is refused.

A real task can take longer than thirty seconds. A wait that runs out exits `3` and cancels
nothing: run the same `task` command
again. The other exit codes, the ten receipt states and what a receipt does and does not prove
are in [Receipts](RECEIPTS.md).

The worker does not see the conversation you are having with the first agent, so put what it
needs in the brief. Do not count on it having read your project's instruction files either: a
Claude Code worker does not load the project's `CLAUDE.md` or its skills.

## Or let your own agent do it

The source includes two skills. `answerback-collaboration` teaches an agent the three MCP tools
it needs: `peer_search` finds a recipient, `peer_submit` hands over one assignment with a
request ID, and `task_wait` collects the receipt. `answerback-fleet` surveys which of your
machines and coding tools can take work, and installs the skills and the bridge connection on
them. Connect the agent with the first command; the second installs both skills:

```sh
answerback clients install --client claude --apply
answerback skill install --client claude --apply
```

`--client` takes `codex`, `claude`, `grok`, `kiro` or `crew`. Without `--apply`, each command
only prints what it would change. Reload that client's MCP connections or open a new session,
then say in its chat:

```text
Use answerback-collaboration for /absolute/path/to/project. Reuse the configured Answerback connection, find the current project's agents across providers, and delegate the requested work to the appropriate available peers.
```

Your agent then calls `peer_search`, `peer_submit` and `task_wait` itself. Each client's
destination file and reload step is in [Connect a client](CLIENT-SETUP.md).

The setup skill's repair step, `fleet.py ensure --yes`, installs, upgrades, stops and starts
bridges. It has so far run against stand-ins in tests only, so read what `fleet.py ensure`
proposes before you let it change a machine.

## What it cannot do yet

- One machine, one OS user. Sharing one bridge between machines or between people is not built.
- No package on PyPI. Every install is `uv tool install` from the source tree.
- Windows is not supported. On Linux the service unit is generated and tested, with no live run
  on record.
- A Kiro CLI worker's tool use waits for a person in the default mode, and the bridge never
  approves it. Unattended Kiro workers are an opt-in, Experimental setting
  ([Kiro workers](RECIPIENTS.md)).
- The setup skill's `ensure --yes` has run against stand-ins in tests only.

## See also

- [Receipts](RECEIPTS.md): the ten states, the exit codes and what a receipt proves.
- [Connect a client](CLIENT-SETUP.md): each client's destination file, reload step and the
  `config.json` layout.
- [Owned native CLI workers](NATIVE-CLI.md): Codex CLI, Claude Code and Grok Build workers,
  permissions and the full exit-code table.
- [Operations](OPERATIONS.md): the service, configuration paths, upgrade and removal.
- [Troubleshooting](TROUBLESHOOTING.md): symptom, cause and the command to run.
