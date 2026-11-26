# Troubleshooting

Find the symptom you are seeing, read the one-sentence cause, and run the command next to it.
Each entry ends with a link to the guide that holds the full behaviour.

This page is for someone whose run failed. If you have not run the bridge at all yet, start with
[Install the bridge](GETTING-STARTED.md). If your question is which agents can call or receive
work, read [Recipients](RECIPIENTS.md) first.

## Start here

Three commands answer most questions about a running bridge:

```sh
answerback doctor
answerback status --json
answerback logs --follow
```

`doctor` probes each enabled provider it can probe without starting a session: the older Herdr
TUI, Herdr App/core, the Kiro CLI, KiroCrew and Rex. For a configured Claude Code workspace it
runs `<binary> --version` with a 10-second timeout and reports `binary`, `version`,
`settings.{path,size_bytes,limit_bytes}` and `sdk.{found,pinned}`; `refusal` is the exact text
the first task would be refused with for an old CLI, a settings file over 8 MiB or a missing or
mismatched SDK, and `null` when all three pass. Codex CLI and Grok Build workers report
registration only. `status --json` reports the service identity, receipts and provider
observations. `logs --follow` tails the rotating operational log, which never contains task or
result bodies.

## The first run

**The self-test did not report success.**
The report has a top-level `"success"` field and a `"phases"` list, each phase with its own
`"status"`. The first phase whose status is not `"passed"` is where the run stopped; its other
fields (`outcome`, `nonce_match`, and so on) say why. The self-test never touches your real
configuration, so rerunning it is always safe. From the source tree, with nothing installed:

```sh
uv run --no-dev answerback self-test --report /tmp/answerback-self-test.json
```

The saved file holds the same JSON. Once the tool is installed, plain `answerback self-test`
runs the same test.

More: [Test layers](TESTING-HARNESSES.md#choose-a-test-layer).

**Your shell cannot find `uv`, or `uv` cannot find a supported Python.**
Answerback needs Python 3.11 up to, but not including, 3.16. `uv` installs a matching interpreter
for you, but `uv` itself has to be on your `PATH` first. Install `uv`, then retry the command.

More: [Install the bridge](GETTING-STARTED.md#install-and-start).

**`answerback configure` reports `Configuration already exists`.**
A configuration is already in place, so `configure` changed nothing and exited `1`. That includes
one under the tool's earlier names, `herdr-bridge` and `codex-herdr-bridge`: the bridge uses the
first one that exists, looking for `answerback-bridge` first. Skip `configure` and carry on. To
add to an existing configuration, use the command for what you are adding, with the bridge
running: `answerback workspace add` for an owned worker, `answerback hc-configure` for agents in
Herdr App/core terminals.

More: [Select the regular bridge](CLIENT-SETUP.md#1-select-the-regular-bridge),
[Operations](OPERATIONS.md#service-and-configuration-lifecycle).

**`workspace add` answers `Bridge request failed; inspect service logs and the task receipt.`**
`workspace add` refuses an alias in the wrong form, a missing executable or project directory,
an alias registered for another project, and a new binary or home while that provider has
unfinished work. All of these answer with that one general text, and the service log records
only the operation and the kind of error.

More: [Register and submit](NATIVE-CLI.md#register-and-submit), which lists what to check.

## Finding and reaching a peer

**`find`/`peer_search` returns an agent in a Herdr App/core terminal (an `hc` peer), but it is read-only or the send is refused.**
An `hc` peer carries a `send_ready` flag and a `send_readiness_reason`. Only a recognised, live
agent that reports `idle` can receive new work (`done` too when `hc_send_to_done` is on). A
working, blocked, errored or unknown-status one is inspection only, and so is one on another
machine unless that machine is listed in `hc_send_machines` (reason `remote_read_only`, or an
earlier identity reason such as `foreground_unknown` or `foreground_agent_unknown`). Both
settings are Experimental and off by default, and remote results are qualified for Linux with
Codex CLI only ([what it cannot do yet](#what-it-cannot-do-yet)).

A send to a peer that is not ready is refused before any task is created, with the general
`Bridge request failed; inspect service logs and the task receipt.` The peer's own row says why.

```sh
answerback find "<project keywords>" --provider hc
answerback read-hc <returned-core-address> --lines 100
```

The matching row's `send_ready` and `send_readiness_reason` say whether it can take work now. A
read-only peer can still be inspected with `read-hc`. The bridge reads `hc_send_to_done` and
`hc_send_machines` when it starts, so restart it after changing either.

More: [Herdr App/core readiness](RECIPIENTS.md#herdr-appcore-readiness).

**A task sent to a terminal on another machine never completes.**
The send worked: the machine is listed in `hc_send_machines` (Experimental, off by default) and
the agent got the assignment. An agent on another machine cannot reach the bridge's private
socket, so it reports through a result file on its own machine, and the bridge reads that file.
Remote result files are qualified for Linux with Codex CLI only; another pair may never produce
one. A missing file means "not yet".

After a file is refused (empty, too long or not valid UTF-8 text), ordinary reads skip the file
and still check the terminal; when the terminal has ended, the bridge reads the file once more.

```sh
answerback read-hc <returned-core-address> --lines 100
answerback task <task-id> --reconcile
answerback complete <task-id> --result-file /absolute/path/to/result.txt
```

Read the terminal to see what the agent did. `task <task-id> --reconcile` reads the result file
on the other machine at once; it applies while the machine is listed, the receipt is `submitted`
or `uncertain` with a verified delivery, and the delivery identity was not invalidated, and it
completes the receipt only when a valid file is there. `complete <task-id> --result-file` does
not read the other machine: it reads a file on this machine (at most 200,000 characters) and
records that text as the result, for example a result copied out of the terminal. It completes a
receipt that is not final and whose delivery was verified; when delivery was not verified, the
bridge holds the text and the status does not change. It is refused for an invalidated delivery
and for a `failed`, `abandoned` or `cancelled` receipt.

More: [Remote results](RECIPIENTS.md#remote-results-what-listing-a-machine-trusts).

**A send with a `--binding` is refused after the terminal or chat changed hands.**
A binding (the peer identity snapshot that discovery returns alongside a peer) comes with `hc`,
older Herdr TUI and KiroCrew peers, and with the peers of a provider plugin that declares one.
It is checked again at send time: if a different agent now occupies that terminal or chat, the
provider refuses rather than guess, and no task is created.
The reply is the general `Bridge request failed; inspect service logs and the task receipt.`, as
for the other refusals on this page; it does not say that the peer changed. Search again and
send to the freshly returned peer and binding.

```sh
answerback find "<project keywords>"
answerback send <fresh-peer-id> --binding <fresh-binding> --request-id <new-id> --message "<message>"
```

A successful send returns a new task ID for the current occupant.

More: [Recipients](RECIPIENTS.md).

**The same request ID was sent again with a different message, or to a different peer.**
A request ID is good for one recipient and message: sending it again with the same message to the
same peer returns the earlier receipt, so retries are safe. Sending it again with a different
message, or to a different peer, is refused instead of guessed, and the reply is the general
`Bridge request failed; inspect service logs and the task receipt.` Send new work under a fresh
request ID, and look up the earlier task before deciding what to do next.

```sh
answerback send <peer-id> --request-id <new-id> --message "<message>"
answerback task --peer <peer-id>
```

`task --peer` finds that peer's latest receipt without needing its task ID.

More: [Receipts](RECEIPTS.md#done-is-not-evidence).

## Receipts that do not complete

**A task for an owned worker fails at once.**
Read the receipt's `detail`. How long the task took does not say what failed.

```sh
answerback task <task-id>
```

- A `failed` receipt of a Codex CLI, Claude Code or Grok Build worker whose `detail` has the form
  `ErrorName: text`: the worker failed before the task was sent, for example because it could
  not start. Read the text, then register the worker again with another `--binary` or `--home`,
  or fix its sign-in.
- A `failed` receipt whose `detail` reads `KIND confirmed failed`: the worker started, ran the
  task and itself reported a failed turn. Look for the reason in the worker's own history.
- A Kiro worker that cannot start leaves an `uncertain` receipt, not a `failed` one; see the
  entry on `uncertain` receipts below.

`doctor` opens no worker session. For Codex CLI, Claude Code and Grok Build it reports
registration only (`configured`), so it cannot show their start failures beforehand. For Kiro it
runs the CLI and reports `cli_available` or `unavailable`: a missing executable, or a CLI without
the required ACP interface, shows there. Whether a Kiro session can start does not.

More: [What a worker needs to start](NATIVE-CLI.md#what-a-worker-needs-to-start), which lists
each `detail` text with its cause.

**`task --wait` exits 3.**
Exit code 3 means only that the `--wait` deadline expired, not that the task failed. Wait again,
or inspect the receipt directly. `--wait` takes 1 to 30 seconds; a longer value is refused.

```sh
answerback task <task-id> --wait 30
answerback task <task-id>
```

A plain read without `--wait` can exit 0 for a task that is not finished, so check the printed
`status` field itself rather than the exit code alone.

More: [Receipts](RECEIPTS.md#ask-the-receipt-not-the-agent).

**Exit 1 with an `uncertain` receipt.**
An interrupted connection, a contradictory event or a missing final response leaves a receipt
`uncertain` rather than `failed`. The recipient stays reserved for investigation; never resend the
same work under a new request ID to work around it. Inspect the receipt and the recipient's own
terminal or chat first. Only after confirming the earlier work has settled, release the receipt
with `abandon`, which does not cancel execution and keeps the prior outcome in an audit record:

```sh
answerback task <task-id>
answerback abandon <task-id> --reason "<what you verified>"
```

`abandon` refuses while the bridge still runs a worker for that task. Its success does not show
that the earlier work has ended, which is why the check above comes first. A successful
`abandon` frees the recipient for new work and does not authorise replay.

More: [Permissions and result evidence](NATIVE-CLI.md#permissions-and-result-evidence),
[Receipts](RECEIPTS.md).

## Permissions

**Exit 2: the task is blocked on a permission.**
List the pending request, then answer it with the exact permission ID shown; answering a
different, no-longer-pending request is refused rather than silently applied.

```sh
answerback permissions <task-id>
answerback permission-answer <task-id> --permission-id <permission-id> --option <option-id>
```

A successful answer echoes the `approved_tool_call` back, so you can confirm what you allowed.

More: [Permissions and result evidence](NATIVE-CLI.md#permissions-and-result-evidence).

## The service

**The service will not start after an upgrade, or `serve` prints
`` Bridge config at <path> is incompatible with this version of Answerback: … ``.**
A config field the new version does not recognise, or a wrongly shaped value, makes `serve` exit
non-zero instead of starting. After five such failures inside two minutes, it logs a
`crash_loop_guard` message and exits 0 instead, so launchd or systemd stop restarting it every
ten seconds.

```sh
answerback start
```

Fix or restore the config first; a successful start clears the recorded failures.

More: [If the service will not start after the upgrade](ANSWERBACK-UPGRADE.md#if-the-service-will-not-start-after-the-upgrade).

**Where receipts and logs live.**
Receipts are a `tasks.sqlite` file next to your config file; logs are `bridge.log` in that
config's `runtime/` directory (for the default config,
`~/.config/answerback-bridge/runtime/bridge.log`). Each config has its own such directory; never
share one between two configs.

```sh
answerback maintenance stats
answerback logs --lines 100
```

`maintenance stats` reports sizes without changing anything; `logs` reads the same file
`--follow` tails.

More: [Maintenance](MAINTENANCE.md).

## Provider and module plugins

Since 0.16 every provider is a plugin loaded from an installed package's entry points, and the bus
is a module loaded the same way ([the contract](ADAPTERS.md#composition-the-plugin-half)). The
warnings below are printed on stderr by any `answerback` command that composes the bridge
(`doctor`, `status`, `serve`, ...) and are in `bridge.log` for the service. A warning means the
rest of the bridge is running without that one plugin; an error means the bridge did not start.

**`doctor` lists a provider as `disabled` with `no loaded plugin claims this provider; its
settings are kept`, and the load printed `Bridge config at … names providers no loaded plugin
claims; they are disabled and their settings are kept: <names>`.**
Your `config.json` has a `providers.<name>` block or a `provider_selection` entry for a provider
that no installed plugin provides. The block is kept and written back unchanged; nothing is
refused. Either the provider's package is not installed in this `uv tool` environment (the
internal provider package on a public install; a third-party package that the last
`uv tool install` left out, since each install replaces the `--with` set), or its plugin was
refused by one of the checks below and the warning naming it is a few lines above.

```sh
answerback providers list
uv tool install --force '<wheel>[<extras>]' --with <plugin package>
answerback restart
```

The same message with `registers MCP clients no loaded plugin installs` is the client form: a
`client_registrations` entry whose registrar is not installed is kept and not notified.

More: [A provider that is not installed](ANSWERBACK-UPGRADE.md#a-provider-that-is-not-installed).

**`Skipping third-party plugin entry point answerback.providers:<name> (<distribution>; <target>): Plugin <name> declares api_version <n>; this kernel is 1`.**
The package was written against another plugin API version. The kernel skips it, and its provider
reads `disabled` as above. Install a release of that package built for `PLUGIN_API` 1, or the
kernel release the package names; a package that pins the kernel version (`answerback==X.Y.Z`)
fails at `uv tool install` time instead, before anything is replaced.

**`Skipping third-party plugin entry point …: Provider <name> config flat key '<key>' (field '<field>') shadows Config.<key>`**, or **`… is already claimed by provider <other>`**, or **`… config fields '<a>' and '<b>' both declare flat key '<key>'`.**
The plugin's configuration block declares a pre-0.16 flat key the kernel cannot route: it spells a
kernel field, method or property; a provider loaded earlier already owns that flat name (refused
in favour of the earlier provider so an existing saved key keeps its meaning); or two fields of
the same block alias one key. The plugin is refused as a whole, so it owns neither the key nor
its registration name, and the others load. This is the plugin author's bug: report it with the
message. Until it is fixed the provider reads `disabled`; its block in `config.json` is untouched.

**`Skipping third-party plugin entry point …: Provider <name> registration_binary_field '<field>' requires config flat key '<name>_<field>'`.**
The plugin's catalog row names a registration binary field its block does not declare. Same
remedy: the plugin author's bug; the provider reads `disabled` meanwhile.

**`Skipping third-party plugin entry point …: factory raised <Type>: <message>`.**
The plugin's factory failed to import or construct (a missing dependency of that package is the
usual cause). Reinstall it with its extras, or read its traceback with the bridge logger at debug
level.

**Every `answerback` command stops with a Python traceback whose last line ends with
`Built-in plugin entry point answerback.providers:<name> (answerback; <target>) failed with <Type>`,
`Plugin <name> declares api_version <n>; this kernel is 1`,
`Plugin registration '<name>' is declared by entry points … and …` or
`` The installed metadata has no answerback.providers entry points: run `uv sync` or reinstall the package ``.**
A plugin that the `answerback` distribution itself owns failed one of the checks above, two
installed packages claim one registration name, or the installed metadata is stale (a source
checkout whose `pyproject.toml` changed since the last `uv sync`, or a half-replaced tool
environment). The kernel loads its providers when it is imported, so the failure stops every
command, `answerback --help` included, and it prints the raw traceback rather than the usual
one-line JSON error. Built-in failures stop startup on purpose: a damaged installation must not
run with half its providers. Run `uv sync` in a checkout, or `uv tool install --force` the wheel
again; for a registration collision, reinstall without one of the two packages.

**The bridge does not start, and `answerback serve` (or the service's stderr) ends with a
traceback whose last line ends with `Module '<name>' long-poll capacity exceeds connection cap 32`
(or names `unknown long-poll verbs`, `invalid long-poll capacity for` or
`duplicate long-poll verb`).**
An installed module declares more concurrent long polls than the bridge's 32-connection client
budget allows, together with the other modules, or declares a verb it does not own. This refusal
is fail-fast by ruling, and a disabled-with-reason path is planned for 0.17 (CHANGELOG,
"Deferred to 0.17"): the bridge does not start until the module is fixed or removed.

```sh
uv tool install --force '<wheel>[<extras>]'   # the same command without that module's --with
answerback start
```

More: [Modules](ADAPTERS.md#modules).

## The optional bus

**A `bus`/`agent-bus` command fails naming a missing extra or `nats-server`.**
The bus is opt-in. Without the `bus` package extra, commands that reach a
broker stop with an error naming `answerback[bus]`; without the separate
`nats-server` binary on your `PATH`, `agent-bus configure` refuses with
`Install nats-server before configuring the bus`. Every other core command,
including `answerback --help`, keeps working either way.

```sh
answerback stop                  # only if the bridge is running
uv tool install '.[bus]'         # from the repository root; with Claude Code too: '.[claude,bus]'
brew install nats-server
```

Then run `agent-bus start`, which starts the bridge again, and repeat the bus
command. Each `uv tool install` replaces the earlier set of extras, so name
every extra you use in one command. A bridge that was running while the extra
was missing does not try the bus again until it restarts. In a source checkout
the extra comes from `uv sync --extra bus` instead, and commands run as
`uv run …`.

For an installed-tool wheel recovery, read
`~/.local/share/uv/tools/answerback/uv-receipt.toml` before
`uv tool install --force` and retain every extra listed there. Invoke the installed
`~/.local/bin/answerback` and `~/.local/bin/agent-bus` entry points. Running
`uv run --no-project` from inside a source checkout can select that checkout's
`.venv` and write its interpreter into the service definition.

Run `agent-bus stop` before `answerback uninstall --remove-tool`.
Without that step, removing the tool leaves the separately supervised broker loaded
and removes its owned stop command.

**Bus configuration names a launcher shim.** `agent-bus configure` resolves symlinks
before saving the broker executable. It asks `mise which nats-server` only for a
`nats-server` path in mise's default or configured shim directory, a symlink to the
`mise` executable, or a small script whose body names `mise`. Other scripts are
refused; configure their real executable path directly. If `answerback bus status`
reports ``configured executable is a launcher shim; run `agent-bus configure` again``,
rerun configure; it preserves the existing bus token and port and replaces the old
launcher path. Then run `agent-bus start` to rewrite and reload the owned broker
service with the resolved executable.

More: [AGENT-BUS.md, "Install and enable"](AGENT-BUS.md#install-and-enable).

**The dashboard says `bus down`, or bus status says the broker is not running.**
`answerback bus status` checks the configured loopback broker and, when it is
down, reads only fixed state fields from its owned launchd or systemd service.
The `broker_error` field gives the cause. The same text appears under
`bus.last_error` in `answerback status --json` and under
`coordinator.last_error` in MCP `bus_status`.

Run the read-only checks first:

```sh
answerback bus status
answerback status --json
```

`broker not running (launchd: exited 0, not restarted)` identifies a plist from
before the all-exit restart policy: the broker exited cleanly, and the loaded job
was configured to restart only unsuccessful exits. Upgrade Answerback with the
`bus` extra, then run `agent-bus start`. That start reloads a registered job when
the plist changed and starts the broker. Current macOS jobs use unconditional
`KeepAlive`; Linux units use `Restart=always`. `agent-bus stop` still unloads or
disables the service and removes its definition, so it does not respawn.

More: [AGENT-BUS.md, "Install and enable"](AGENT-BUS.md#install-and-enable).

## Client setup

**My agent cannot see the new tools after `clients install`.**
Installing or updating an MCP connection does not refresh a client that is already running.
Reload that client's MCP connections, or open a new chat or session in it, before expecting the
new tools to appear.

```sh
answerback clients install --client <client> --apply
```

Then reload or start a new session in that client; which one depends on the client, so check the
table linked below.

More: [Destinations and reload behavior](CLIENT-SETUP.md#destinations-and-reload-behavior).

## What it cannot do yet

- `hc_send_to_done` and `hc_send_machines` are Experimental and ship off by default. Remote
  result files are qualified for Linux with Codex CLI only; macOS, and Kiro CLI, Claude Code and
  Grok Build on any system, are not qualified for remote completion ([Harnesses](HARNESSES.md)).
- A module that exceeds the long-poll cap stops the bridge from starting; a disabled-with-reason
  path is planned for 0.17.
- The Codex Desktop/IDE route and KiroCrew as a recipient are excluded from this release, so
  none of the entries above covers them.

## See also

- [Install the bridge](GETTING-STARTED.md): the self-test and the first hand-off, if you have
  not run one yet.
- [Receipts](RECEIPTS.md): the ten states and the exit codes behind the entries above.
- [Owned native CLI workers](NATIVE-CLI.md): each `detail` text of a worker that would not start.
- [Recipients](RECIPIENTS.md): readiness, bindings and remote results.
- [Glossary](GLOSSARY.md): peer, receipt and binding.
