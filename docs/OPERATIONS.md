# Run the bridge day to day

Start, stop, read and remove one installed bridge, and know where its files are. The bridge
runs as one service for your OS user, keeps its receipts in one private directory, and
answers its lifecycle commands with JSON you can read or script against.

This page is for whoever operates a bridge that is already installed. Installing and
connecting a client are in [GETTING-STARTED.md](GETTING-STARTED.md) and
[CLIENT-SETUP.md](CLIENT-SETUP.md).

## The shortest check

```sh
answerback status --json
```

Real output, trimmed to five of its fields:

```json
{
  "version": "<version>",
  "running": true,
  "accepting": true,
  "pid": <pid>,
  "socket": "/home/<you>/.config/answerback-bridge/runtime/bridge.sock"
}
```

`running` and `accepting` both `true` means the service answers on its socket and takes new
work. `version` is the code the service runs; if it differs from `answerback --version`,
the service was started before the last install and needs `answerback restart`.

## Local by design

The kernel opens no TCP port; the optional bus, dashboard and event viewer bind
authenticated listeners to 127.0.0.1. Receipts are kept in an owner-private configuration
directory. Agents keep their provider login and native permission mechanism.

## Commands and aliases

The package installs five commands. `answerback` is the one the docs use; the others are
the same program under names kept for earlier installs.

| Command | What it is |
| --- | --- |
| `answerback` | The bridge command |
| `answerback-bridge`, `herdr-bridge`, `codex-herdr` | Aliases of `answerback` |
| `agent-bus` | The bus; the same program as `answerback bus` |

The Python package is `answerback_bridge`; `python -m answerback_bridge` runs the same
command. An earlier name of a config directory, MCP server ID or receipt store keeps
working: a rename never moves state ([upgrade guide](ANSWERBACK-UPGRADE.md)).

## Service and configuration lifecycle

Every command below takes `--config /absolute/path/config.json` before the subcommand to
work on another configuration. Without it the default path is used (next section).

| Command | What it does |
| --- | --- |
| `configure` | Writes a fresh configuration and prints its path. With one already present it changes nothing, prints `Configuration already exists` and exits `1` |
| `start` | Installs the bridge as a launchd agent on macOS (`~/Library/LaunchAgents/local.answerback-bridge.<digest>.plist`) or an enabled systemd user unit on Linux, so it starts again at login. Leaves a running bridge alone. When `fleet-writer.json` exists beside the config, installs and starts the fleet writer after the bridge is ready |
| `start --standalone` | Detaches the bridge without a supervisor. For tests, not for a machine you leave running |
| `restart` | Stops and starts the service. Interrupted receipts are not re-sent (below) |
| `stop` | Removes the fleet writer's service, then the bridge's, asks the bridge to stop and waits up to 10 seconds for it to go |
| `status [--json]` | Service identity, receipt counts, providers, clients, the loop-lag guard's count (`loop`, below) and the fleet writer's state |
| `doctor` | Probes each enabled provider. A provider whose plugin is not installed reads `disabled` with the reason |
| `logs [--follow] [--lines N] [--json]` | Operational log lines, 1 to 1000 of them (default 50), with no task or result bodies |
| `reset --connections` | Clears provider observations; pairings and receipts stay |
| `serve` | Runs the bridge in the foreground, for a supervisor of your own |

On Linux the unit ends with `[Install] WantedBy=default.target` and `Restart=on-failure`
with a 10-second delay; `loginctl enable-linger` is your optional step if the bridge
should run before you log in. On macOS the plist sets `KeepAlive.SuccessfulExit=false`,
so a clean exit stays down and a crash restarts after 10 seconds.

A `stop` or a crash ends owned workers and leaves their work where it was. On the next
start, a receipt that was `pending` when the bridge went down is marked `uncertain` with
the detail `Bridge stopped during submission; inspect before abandoning`. Nothing is sent
again. Work running in a Herdr terminal or a KiroCrew chat is not cancelled by a stop.

### When the service will not start

A `config.json` this version cannot load makes `serve` print one error naming the path and
exit non-zero: `Bridge config at <path> is incompatible with this version of Answerback: …`
for an unknown field or a wrong-shaped value, `Bridge config at <path> is not valid JSON: …`
for malformed JSON. After 5 such failures within 120 seconds, `serve` logs a
`crash_loop_guard` message and exits 0, so launchd and systemd stop restarting it every
10 seconds. Fix the file, then run `answerback start`. The failure record is
`runtime/start-failures.json` beside the config; a successful start clears it.

### When the panels say the service is offline while it runs

Every request the service answers waits for its event loop. When the loop is blocked, by one
operation that computes for seconds, every caller waits that long: the fleet writer gives
`status` 2 seconds and then writes `health.error: service did not answer in time`, and the
Herdr App panels read "Answerback offline". The service counts such blocks itself. `status
--json` carries a `loop` object: `lag_events` is the number of times the loop was found more
than 1 second late since the service started, `max_lag_seconds` and `last_lag_seconds` the
longest and the latest of those delays, `last_lag_at` the wall-clock time of the latest. Each
one is also a `loop_lag` entry in the log, with the delay as `seconds`:

```sh
answerback logs --lines 1000 | grep -A1 '"loop_lag"'
```

```text
      "event": "loop_lag",
      "seconds": 1.116
```

A `lag_events` of 0 with "offline" panels means the writer, not the service, could not keep its
cycle (its own log says why). The guard sleeps 0.1 seconds between measurements and reads a
block when it ends, so a delay it reports is up to 0.1 seconds shorter than the block was.

## Where the files are

The default configuration is `$XDG_CONFIG_HOME/answerback-bridge/config.json`, which is
`~/.config/answerback-bridge/config.json` when `XDG_CONFIG_HOME` is unset. If that file is
absent, an existing `herdr-bridge/config.json` and then `codex-herdr-bridge/config.json`
under the same root is used instead. `answerback --help` prints the path it resolved.

Beside the configuration file:

| Path | Holds |
| --- | --- |
| `tasks.sqlite` | Every task receipt |
| `runtime/` | The socket `bridge.sock`, the owner lock, `supervisor.json`, the crash-loop record and the logs |
| `runtime/config-owner.json` | The config path this directory belongs to. A second config in the same directory is refused: `This configuration directory belongs to another bridge` |
| `bus/` | The optional bus: its token, `nats.conf` and ledgers ([AGENT-BUS.md](AGENT-BUS.md)) |
| `routing-workers.json` | The registry of managed workers ([ROUTING.md](ROUTING.md)) |

Back up the whole directory, not single files: receipts, request identities and the bus
namespace are bound to it. Each configuration needs a directory of its own. The socket path
has an OS limit of about 100 bytes, so keep the directory path short.

`config list` inventories known configuration directories. `config archive` previews
archiving the one named by `--config`; with `--apply` it requires a stopped bridge and
reconciled receipts. Client registrations are not rewritten: restore an archive to its
original path before starting it again.

## Workspaces and receipts

`workspace list` shows registered project directories, missing paths and unfinished tasks;
it defaults to Kiro workspaces, and `--provider codex_cli|claude_code|grok` selects another
owned-worker provider. `workspace remove ALIAS` previews a removal and `--apply` performs
it with a recoverable backup; it refuses an alias with an unfinished task.

`abandon TASK_ID --reason "what was verified"` releases a receipt that stays unfinished
after you have investigated it. The prior outcome is kept in an audit record. It refuses a
receipt whose owned worker is still live, and it refuses a receipt that changed while the
command ran (`Receipt changed during abandonment; inspect it again`). It cancels nothing
and authorises no replay: the recipient is not told. Do not let a parent chat wait on a
child chat that is waiting on the parent; that circular wait ends only when one side
abandons. Reading receipts is in [RECEIPTS.md](RECEIPTS.md).

## Storage maintenance

```sh
answerback maintenance stats
answerback maintenance plan --older-than-days 30 --output /private/path/retention-plan.json
```

Neither command starts the service or changes a store. Applying a plan and compacting a
stopped store are separate, explicit steps in [MAINTENANCE.md](MAINTENANCE.md).

## Upgrade and removal

To upgrade, stop the bridge, reinstall the package with every extra and plugin the earlier
install had, then start it. The per-host recipe and what changes in `config.json` at 0.16
are in the
[upgrade guide](ANSWERBACK-UPGRADE.md#upgrade-to-016-provider-plugins-and-the-config-layout).

`answerback uninstall --remove-tool` stops the bridge, removes the service that `start`
installed, removes the Herdr App side panel it put in (its files that are as shipped; the
themes stay) and then uninstalls the tool. If the optional bus is configured, its
separately supervised broker must be stopped first: run `agent-bus stop` before
`answerback uninstall --remove-tool`. Your configuration and receipts stay on disk. MCP
entries in clients other than the legacy combined installer's Codex entry are removed with
each client's own controls.

## Shell completion

```sh
eval "$(answerback completion --shell zsh)"
```

Use `--shell bash` for bash, or pipe `completion --shell fish` to `source` for fish.
Completion contacts no agent.

## What it cannot do yet

- The Linux service has no live run recorded; the unit is covered by tests that record the
  `systemctl` calls.
- `stop` cannot cancel work in a Herdr terminal or a KiroCrew chat. Only the owned workers
  the bridge started are ended.
- There is no `fleet upgrade`: each host is upgraded by hand
  ([upgrade guide](ANSWERBACK-UPGRADE.md#apply-the-upgrade-across-a-fleet)).

## See also

- [GETTING-STARTED.md](GETTING-STARTED.md): install, configure and the first hand-off.
- [ANSWERBACK-UPGRADE.md](ANSWERBACK-UPGRADE.md): the 0.16 upgrade and rollback.
- [MAINTENANCE.md](MAINTENANCE.md): pruning and compacting the SQLite stores.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): symptom, cause and the command that shows it.
- [AGENT-BUS.md](AGENT-BUS.md): the optional durable queue and its broker service.
