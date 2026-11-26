# Upgrade an installed bridge

Move an installed `answerback`, `herdr-bridge` or `codex-herdr` bridge to a newer version
without losing a receipt, a request identity or a client connection. The upgrade is a stop,
an install with the same extras and plugins, and a start; `status --json` then shows the
new version running.

This page is for someone who already runs the bridge. A first install is in
[GETTING-STARTED.md](GETTING-STARTED.md). An install on 0.15 also has a composition to
choose and a new `config.json` layout
([Upgrade to 0.16](#upgrade-to-016-provider-plugins-and-the-config-layout)).

## The shortest path

```sh
cat ~/.local/share/uv/tools/answerback/uv-receipt.toml   # the extras and plugins the host has
answerback stop
uv tool install --force '/absolute/path/to/answerback-<version>-py3-none-any.whl[claude,bus]'
answerback start
answerback status --json
```

Real output of the last command, trimmed to its first three fields:

```json
{
  "version": "<version>",
  "runtime": {
    "interpreter": "<path>/bin/python",
    "package_path": "<path>/answerback_bridge",
    "package_version": "<version>"
  },
  "running": true
}
```

`running: true` with `version` equal to what `answerback --version` prints is the proof: the
service that answers runs the code you installed. A `version` that still shows the old
number means the install replaced the tool but `start` found a service already running; run
`answerback restart`.

## Before any upgrade

1. Let active work settle: `answerback tasks` lists recent receipts. An `uncertain` receipt
   stays `uncertain`; an install never re-sends it.
2. Copy `config.json`. The first save by 0.16 rewrites it in a shape 0.15 cannot read.
3. Keep the whole configuration directory: receipts, mailboxes, the bus ledger, locks and
   request identities are bound to that path. Back up the SQLite files with SQLite's own
   backup API, not by copying a live database.
4. Read what the earlier install had before you reinstall. Each `uv tool install` replaces
   the earlier set of extras and `--with` packages:

   ```sh
   cat ~/.local/share/uv/tools/answerback/uv-receipt.toml
   ```

   Its `[tool].requirements` list has one entry per package: the `answerback` entry carries
   `extras = [...]`, and every `--with` package is an entry of its own.

What stays the same across every version since 0.10: the `answerback` command and its
aliases, the `agent-bus` command, the `answerback_bridge` Python package, existing MCP server
names, the configuration path and search order, the service labels
`local.answerback-bridge.<digest>` and `local.answerback-fleet.<digest>` (the digest is the
first 12 hex characters of the SHA-256 of the config path), and every broker namespace,
command, task and alias ID. An explicit `--config` path always wins over the search order.

## Upgrade to 0.16: provider plugins and the config layout

Since 0.16 the bridge is a kernel that names no provider, plus provider plugins it loads
through the `answerback.providers` entry-point group and modules it loads through
`answerback.modules` ([ADAPTERS.md](ADAPTERS.md#composition-the-plugin-half)). For an
operator this changes three things: which packages make up an installation, the layout of
`config.json`, and what `doctor` says about a provider that is not installed. Receipts,
request identities, service labels, RPC verbs, MCP tool names and every `answerback`
subcommand are unchanged; `tests/test_plugin_surfaces.py` pins them byte for byte against
the pre-split tree.

### Choose a composition

A composition is the set of installed plugins. The public composition is the `answerback`
distribution alone: the kernel, its own providers (`codex_cli`, `claude_code`, `grok`,
`kiro`, `crew`, `rex`, `hc`, `herdr`) and the `bus` module. A provider plugin installed
alongside the kernel is added with `--with` in the same install command; each plugin pins
the kernel version it was built for (`answerback==X.Y.Z`). The kernel is identical in
both, and nothing in `config.json` records the choice: the same file loads under either,
and a provider block the installed composition has no plugin for is kept and disabled
(below).

Name every extra and every plugin in the one command, because each `uv tool install`
replaces the earlier set:

```sh
# the kernel alone
uv tool install --force '/absolute/path/to/answerback-<version>-py3-none-any.whl[claude,bus]'
# the kernel plus a provider plugin
uv tool install --force '/absolute/path/to/answerback-<version>-py3-none-any.whl[claude,bus]' \
  --with /absolute/path/to/<provider-plugin>-<version>-py3-none-any.whl
```

The plugin operand is the provider package's checkout or wheel. A developer's checkout of
the kernel uses `uv sync --all-extras` instead
([the compositions](TESTING-HARNESSES.md#the-two-compositions)); a plugin's checkout
documents how it installs itself editable beside the kernel. A plugin built for another
kernel version fails at resolve time, before anything is replaced.

### The `config.json` layout

Provider settings move under one block per provider, `providers.<name>`, with the kernel's
own fields at the top level. The pre-0.16 flat key `<provider>_<field>` is the block's
field: `hc_send_machines` is `providers.hc.send_machines`, `kiro_trust_tools` is
`providers.kiro.trust_tools`; the one odd name, `desktop_threads`, is
`providers.codex.desktop_threads`. The full shape with an example is in
[CLIENT-SETUP.md](CLIENT-SETUP.md#configjson-layout).

What happens to an existing file:

- **It loads unchanged.** 0.16 reads the flat keys and gives every setting the value it
  had; `tests/test_config_migration.py` pins six real configurations to load identically.
  No warning is printed for a flat-only file. One warning names the keys when a flat key and
  its nested twin are both present; the nested value wins.
- **The first save rewrites it in the nested shape**, with no flat key left. `configure`
  writes a fresh file in that shape and refuses to touch an existing one. An existing file
  is rewritten by the first command that saves it: `providers register` or `unregister`,
  `workspace add` or `remove --apply`, `hc-configure`, `crew-configure`, `rex-configure`,
  `pair`, `clients policy`, or a client registering itself over MCP for the first time. A
  block is written only when its provider is selected or a setting differs from the
  default, so the file names no provider it does not use.
- **A file saved by 0.16 is not readable by 0.15**, which has no `providers` key. To roll
  back, stop the bridge, restore the copy you made and install the previous wheel
  ([Rollback](#rollback)).
- **The flat keys are read until 0.17**, which removes the flat-key reading. Until then the
  docs still name a setting by its flat key where that is the name the bridge prints.

Routing digests, receipt metadata, bus admission guards and live-test manifests hash the
flat names, so no stored digest changes and a queued command from 0.15 is still admitted.

### A provider that is not installed

A `providers.<name>` block or `provider_selection` entry that no loaded plugin claims is
**disabled, not refused**. The loader keeps the block, writes it back unchanged on every
save, leaves the provider out of the registered set and prints one warning at load:

```text
Bridge config at /path/config.json names providers no loaded plugin claims; they are disabled and their settings are kept: <name>
```

`answerback doctor` and `answerback status --json` list the provider under `providers` as

```json
"<name>": {"state": "disabled", "capabilities": [], "reason": "no loaded plugin claims this provider; its settings are kept"}
```

The same rule covers a `client_registrations` entry whose registrar is not installed:
kept, not notified, one warning. Only the operator's own choice is refused:
`answerback providers register <name>` for a name no plugin claims answers
`Unknown provider`. Install the plugin, restart the bridge, and the block is live again
with the settings it had. This is how a kernel-only installation reads a configuration
written on one that had a plugin, and how the project's own fleet fixtures (most of which
name a plugin provider) load on the public gate.

A pre-0.16 flat key of such a provider, `<name>_<field>`, is read as the field `<field>` of
its kept block when the same file's `provider_selection` names the provider, and saved
nested like the rest of the block. A flat key whose prefix is neither a loaded provider nor
a name the file selects stays an unknown key, and the file is refused, so a mistyped key is
still caught. To load such a file on a kernel-only installation, load and save it once on
an installation that has the plugin, or move the keys into a `providers.<name>` block.

A plugin the loader refuses (wrong `api_version`, a config flat key that shadows a kernel
name or one another provider claims, a missing `registration_binary_field` key) is skipped
with a warning naming its entry point, and its provider then reads as disabled by the rule
above. A module whose long-poll declarations exceed the 32-connection cap fails bridge
start. Symptoms and remedies are in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#provider-and-module-plugins).

### Apply the upgrade across a fleet

There is no `fleet upgrade` yet (`answerback fleet --help` lists `serve`, `show` and
`configure`). Run this on each host in turn. The fleet skill's `fleet.py ensure` shows,
without writing, which hosts run an older bridge; its `--yes` form has run against
stand-ins in tests only.

1. Let active work settle: `answerback tasks`.
2. Copy `config.json`.
3. `answerback stop`. The bus broker may keep running; `agent-bus stop` is only for
   `uninstall --remove-tool`.
4. Install with every extra and plugin the host had, as above. Read `uv-receipt.toml`
   first; a host that had a plugin needs `--with` again or its provider comes up disabled.
5. `answerback start`. The first start also migrates an older service definition
   ([below](#skills-service-and-installed-verification)).
6. `answerback herdr-app install --apply` where the Herdr App is installed; a wheel upgrade
   does not refresh the panel.
7. `answerback skill install --client <client> --replace --apply` for each client on the
   host.
8. Verify: `answerback status --json` (the `runtime` object's version and package path),
   `answerback doctor` (every expected provider live, none unexpectedly `disabled`), and
   `answerback bus status` where the bus is configured.

Two things the 0.15 roll-out hit, both visible in step 8: a host whose `~/.config` was
group-writable (`umask 002`) crash-looped on the configuration permission check until
`chmod g-w ~/.config` ([the check](CLIENT-SETUP.md#configjson-layout)), and a
`uv tool install` without the earlier extras silently dropped the bus client.

## Replace a `herdr-bridge` distribution

For an installation still named `herdr-bridge`, stop the coordinator and remove that
distribution first, so two packages do not own the same CLI aliases:

```sh
answerback --config /absolute/path/to/config.json stop
uv tool uninstall herdr-bridge
uv tool install '/absolute/path/to/answerback-<version>-py3-none-any.whl[claude,bus]'
```

The uninstall keeps all state. The broker can keep running. A stable path such as
`~/.local/bin/herdr-bridge` works through the alias. A client entry that points directly
inside an old tool environment needs its own review; naming migration keeps its command and
arguments, apart from the `-P` flag below.

## Review owned client naming

```sh
answerback --config /absolute/path/to/config.json clients migrate --client codex
answerback --config /absolute/path/to/config.json clients migrate --client codex --apply
```

`--client` takes `codex`, `claude`, `grok`, `kiro` or `crew`; repeat it for more than one.
The preview performs no native command and writes nothing. Migration renames only the owned
default `herdr_bridge` and `codex_herdr_bridge` identifiers. Ownership requires the exact
resolved config path plus a recognized Python module, a verified bridge console script or
an existing ownership marker; an unrelated executable with matching arguments is refused.
Custom names, comments, user settings, environment and tool restrictions stay as they are.
Private backups support rollback.

Read the launch commands in the preview before you add `--apply`. Each client's report has
a `launches` list: one record per entry, with the file or project scope, `renamed_from`
when the entry is renamed, and `command`, the launch command as a list with one item per
argument. Control characters and direction marks are shown as `\uXXXX` escapes. For a
console-script command the record also has `launcher_in_this_installation`: `true` when the
registered command resolves to a script of the installation running `clients migrate`,
`false` otherwise, with a `launcher_note`. The value is a note about location only; nothing
is refused because of it. If you do not recognise the command, do not apply; correct the
registration first.

One part of a launch line does change. An owned Python launch of the bridge module
(`python -m answerback_bridge …` or `python -m codex_herdr_bridge …`) is written with `-P`
as its first interpreter argument. Without `-P`, Python looks for the package in the
directory the client starts from before the installation, so a same-named directory in a
project could run instead of the bridge. `-P` needs Python 3.11 or later, which is this
package's minimum (`requires-python = ">=3.11,<3.16"`). A record with
`safe_path_flag_added: true` marks each launch that gained the flag. Migration leaves as
found: console-script commands, launches that already carry `-P`, custom names, entries it
cannot prove it owns, and launches through another program (`uv run python …`,
`env python …`). One form is refused: a launch with an interpreter option before any `-P`
that migration cannot read with certainty (a long option, or a letter CPython does not
define); the report says `interpreter options that cannot be checked for -P`. Put `-P`
first, directly after the interpreter, and run the preview again.

## Skills, service and installed verification

```sh
answerback skill install --client codex --replace
answerback skill install --client codex --replace --apply
answerback --config /absolute/path/to/config.json fleet configure --machine build-a --ceiling build-a=6
answerback --config /absolute/path/to/config.json start
answerback --config /absolute/path/to/config.json status --json
answerback --config /absolute/path/to/config.json doctor
answerback --config /absolute/path/to/config.json bus status
```

`skill install` installs two skills: `answerback-collaboration` and the setup skill
`answerback-fleet`. Without `--replace` a setup skill that is already there and differs is
reported as `kept` and left as it is. `--source` installs a folder as the collaboration
skill and leaves the setup skill out. Replacement backs up a user-edited skill privately.

A fleet settings file beside the bridge configuration opts the fleet writer into native
supervision; `fleet configure` writes it. `start` waits for the bridge's readiness
handshake before it installs and starts the writer's separate launchd agent or systemd user
unit. Without the settings file it reports
``fleet writer not configured; run `answerback fleet configure` `` and leaves the writer
alone. `status --json` adds a `fleet_writer` object; `stop` and `uninstall` remove the
writer before the bridge. The writer's label is `local.answerback-fleet.<digest>` with the
bridge's digest. Both services restart 10 seconds after a crash and stay down after a clean
exit.

The first `start` after an upgrade migrates an older service definition. Earlier releases
kept the launchd plist in the config's `runtime/` directory, which launchd never reloads
after a reboot; since 0.11 the plist is
`~/Library/LaunchAgents/local.answerback-bridge.<digest>.plist`, and on Linux the unit ends
with `[Install] WantedBy=default.target` and is enabled. On that first start the bridge
boots the registered job out, removes the old definition, writes the new one and starts it,
so the bridge is briefly restarted. A service still running under an old label is switched
to the canonical one; if the service manager then refuses the canonical service, `start`
restores the old one and fails with an error that names the cause and says that the
previous service runs again. `supervisor.json` in the runtime directory records the label in
use. `start` leaves a running bridge alone unless it still runs under an old label, so run
`answerback restart` once to rewrite an older Linux unit. The bus broker has its own service
and its own one-`start` migration ([AGENT-BUS.md](AGENT-BUS.md#the-broker-service)). This
migration is covered by unit tests that record the `launchctl` and `systemctl` calls; verify
the restart after a real login on your own machine.

### The Herdr App side panel and themes

Since 0.15 the wheel carries the Herdr App's fleet column and six theme files. A wheel
upgrade does not refresh them; run the install on its own:

```sh
answerback herdr-app status
answerback herdr-app install
answerback herdr-app install --apply
```

A copy that is one of the earlier shipped versions is backed up and updated. A copy you
edited is kept and named in the report; `--replace` backs it up and installs the shipped
one. Backups go to `~/.local/state/answerback-bridge/backups/`. The App reloads the column
on its own. Details: [HERDR-APP-MOD.md](HERDR-APP-MOD.md#install).

### If the service will not start after the upgrade

A `config.json` this version cannot load makes `serve` print one error that names the path
and exit non-zero: `Bridge config at <path> is incompatible with this version of
Answerback: …` for a file that parses but does not fit (an unknown or stale field, a
wrong-shaped value, a top level that is not an object), and
`Bridge config at <path> is not valid JSON: …` for malformed JSON. launchd and systemd
would restart such a service every 10 seconds for ever, so `serve` keeps a record of
startup failures in `runtime/start-failures.json` (mode 0600). After 5 failures inside
120 seconds it logs a `crash_loop_guard` message with the recovery step and exits 0, which
stops both supervisors. Fix or restore the config, then run `start` again; a successful
start clears the record.

## Rollback

Stop the new bridge with the same explicit config path. Restore the client config files you
changed from their private backups, and saved skills if required. Uninstall `answerback`,
install the previous wheel with the extras it had, and start it with the same config path.
Both service labels are recognised by the new version; the canonical service must be
stopped before an older binary starts.

Keep the current receipt databases rather than replacing them with an older backup, which
would lose newer work and request identities. An upgraded, unpruned database keeps the row
shapes the previous package writes. After explicit pruning, use the current package: an
older one can reject a matching historical retry or show a null result where one was
pruned. The retained request identity still prevents re-execution
([maintenance compatibility](MAINTENANCE.md)).

Two version-specific notes. A `config.json` saved by 0.16 is not readable by 0.15: restore
the copy you made before the upgrade. A configuration that selects Rex, or sets any Rex
option, is not readable by 0.9: reconcile unfinished Rex work and restore the earlier
configuration from its backup before installing that version.

## What it cannot do yet

- No `fleet upgrade` or `fleet restart`: a fleet is upgraded one host at a time with the
  recipe above.
- The setup skill's `fleet.py ensure --yes` has run against stand-ins in tests only. Read
  what it proposes before you let it change a machine.
- The 0.16 roll-out to this project's own Linux hosts has no committed record.

## See also

- [OPERATIONS.md](OPERATIONS.md): the service lifecycle and where the files are.
- [CLIENT-SETUP.md](CLIENT-SETUP.md#configjson-layout): the full `config.json` shape.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md#provider-and-module-plugins): a provider that
  reads `disabled`.
- [MAINTENANCE.md](MAINTENANCE.md): what pruning means for an older package.
- [CHANGELOG.md](../CHANGELOG.md): every change by version.
