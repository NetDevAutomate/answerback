# Connect a client to the bridge

Register the bridge's MCP tools in the coding client you work in, once, and reuse that one
connection across projects and chats. The client can then find a recipient, hand over work and
collect the receipt without you carrying messages between windows.

This page is for someone with an installed, running bridge
([Install the bridge](GETTING-STARTED.md)). It connects a client to that bridge; it does not
decide which clients and providers the bridge lets in, which is
[Registration and presence](REGISTRY.md).

## Or let your own agent do it

Connect the client with the first two commands; the third installs the two skills that teach it
to hand work off. `codex` is the example client throughout this page; `claude`, `grok`, `kiro`
and `crew` work the same way.

```sh
answerback clients install --client codex
answerback clients install --client codex --apply
answerback skill install --client codex --apply
```

The first command prints each destination file, the entry it would write and any conflict, and
changes nothing. The second applies that plan after rechecking the files. Reload the client's
MCP connections, or open a new session, then say in its chat:

> Use answerback-collaboration for /absolute/path/to/project. Reuse the configured Answerback
> connection, find the current project's agents across providers, and delegate the requested
> work to the appropriate available peers.

Your agent then calls `peer_search`, `peer_submit` and `task_wait` itself, and the receipt comes
back into the same chat ([Receipts](RECEIPTS.md)). The rest of this page is the detail behind
those three commands.

## 1. Select the regular bridge

If you followed [Install the bridge](GETTING-STARTED.md) and only hand work to owned workers
(`codex_cli`, `claude_code`, `grok` or `kiro`), your configuration already exists: go to step 2.
This step is for agents in Herdr App/core terminals, and for anyone who runs more than one bridge.

A fresh `configure` writes `~/.config/answerback-bridge/config.json`. When a configuration
already exists under that name or under the tool's earlier names, the bridge uses the first it
finds, in the order `answerback-bridge`, `herdr-bridge`, `codex-herdr-bridge`. An explicit
`--config /absolute/path/to/config.json` on any command wins, and renaming never moves state.
One configuration covers agents across several Herdr App spaces and projects, so a new project
does not normally need a new configuration.

To create a configuration that discovers agents in Herdr App/core terminals, or to add that to
an existing bridge:

```sh
answerback configure --runtime hc     # new configuration
answerback start
answerback hc-configure               # existing bridge, in place
```

`hc-configure --session NAME` selects a named core session; App space names are not session
values. The older TUI route is documented
[separately](RECIPIENTS.md#older-herdr-tui-compatibility).

### `config.json` layout

The file holds the bridge's own fields at the top level and one block per provider under
`providers`:

```json
{
  "provider_selection": ["hc", "kiro"],
  "client_registrations": ["codex", "claude"],
  "heartbeat_interval": 10,
  "heartbeat_misses": 3,
  "providers": {
    "hc": {"enabled": true, "binary": "/Users/me/.local/bin/hc", "session": null,
           "send_to_done": false, "send_machines": ["build-box"], "events": false,
           "placement_policy": null},
    "kiro": {"binary": "kiro-cli", "workspaces": {"worker": "/path/to/project"},
             "acp_engine": "v3", "trust_all_tools": false, "trust_tools": [],
             "active_budget": 600}
  }
}
```

A provider block appears only when the provider is selected or one of its settings differs from
its default. Where the bridge prints a setting by its pre-0.16 flat name, this page uses that
name too: `hc_send_machines` is `providers.hc.send_machines`, `kiro_trust_tools` is
`providers.kiro.trust_tools`, and in general `<provider>_<field>`. Both examples are
Experimental settings that ship off by default; remote results through `hc_send_machines` are
qualified for Linux with Codex CLI only ([Harnesses](HARNESSES.md)).

A file written before 0.16 with flat keys loads unchanged and is rewritten in this shape on its
first save. The flat keys stay readable through 0.16; dropping them is planned for 0.17
(CHANGELOG, "Deferred to 0.17"). A file saved by 0.16 is not readable by 0.15. A `providers`
block for a provider this installation does not have (the internal provider package on a public
install, or a third-party package the last `uv tool install` left out) is disabled with one
warning and kept, never refused.

The bridge reads these settings when it starts, so restart it after editing the file.

### Who may own the configuration file

Loading and saving require a regular file in a directory owned by you. Neither the file nor any
directory above it may be writable by group or other users; root-owned ancestors are allowed,
including a sticky system temporary directory. The check runs component by component, follows a
symlink owned by you or root only after its containing directory passes, stops after 16 hops,
and refuses `..` in the supplied path and any link that sits directly inside a directory other
accounts can write (`/tmp`, `/var/tmp`, `/Users/Shared`). Saving creates missing directories
with mode `0700`, writes the file with mode `0600` and refuses a symlink at the file itself.

Inputs older versions accepted and this one refuses, with the remedy:

| Refused | Remedy |
| --- | --- |
| A `0664` file or a `0775` directory above it, typically from `umask 002` | `chmod go-w /path/to/config.json /path/to/directory` |
| A file or directory owned by another account, including container mounts with mapped UIDs | Run as the owning UID, or choose a directory owned by the runtime user |
| `..` in the supplied path | Use the direct absolute path |
| A symlink at `config.json` when saving | Point `--config` at the physical file |
| A dangling link, a loop or a chain over 16 hops | Repair the link or use a shorter trusted path |

Commands that resolve a path before loading check only the resulting physical path.

## 2. Preview and install MCP connections

```sh
answerback clients install --client codex
answerback clients install --client codex --apply
```

Repeat `--client` to install several clients in one run; `--all` selects every supported target
and does not detect or install applications. If one selected target is not ready, fix what the
preview reports or select only the clients you use. The targets and their order come from the
loaded client registrars, so a provider plugin that is not installed has no target here.

The default connection name is `answerback_bridge`; `--name NAME` selects another. A
conflicting entry is refused by default. To update one on purpose, preview with `--replace`,
then apply:

```sh
answerback clients install --client codex --replace
answerback clients install --client codex --replace --apply
```

Changed files get a private backup under `~/.local/state/answerback-bridge/backups/`. Unrelated
entries and existing environment or tool restrictions are kept. A bridge already registered
under another name is reported rather than duplicated; reuse that name. This applies to the
legacy combined installer's `codex_herdr_bridge` entry.

### Migrating an existing installation

Skip this when your MCP entries already use the `answerback_bridge` name, or you are installing
for the first time. For an entry under an earlier default name:

```sh
answerback --config /absolute/path/to/config.json clients migrate --client codex
answerback --config /absolute/path/to/config.json clients migrate --client codex --apply
```

Migration renames only owned `herdr_bridge` and `codex_herdr_bridge` defaults to
`answerback_bridge`: the resolved config path and a recognised bridge module, console script or
ownership marker must agree. Custom names stay. Environment, disabled tools, user settings and
connection arguments are kept, and namespaced permission tool IDs are translated. Duplicate
routes, conflicting canonical names and ambiguous ownership are refused.

The one argument migration adds is `-P`: every owned `python -m answerback_bridge` or
`python -m codex_herdr_bridge` launch is written with `-P` first, so the interpreter cannot
pick up a same-named directory from the client's working directory. An entry that already has
the current name is touched only when one owned launch lacks the flag. Each client's report
lists every launch it carries over or rewrites under `launches`; read it in the preview before
`--apply`.

For Claude Code, migration also covers `~/.claude/settings.json` (or the real
`CLAUDE_CONFIG_DIR`) and the project `.claude/settings.json` and `.claude/settings.local.json`
files that `~/.claude.json` and the current directory's ancestors name. Deny, ask and allow
rules keep their scope. Managed policy files are inspected and never rewritten: a legacy rule
there needs an administrator first. Windows migration is refused. The full rules are in
[Upgrade](ANSWERBACK-UPGRADE.md).

### Destinations and reload behavior

| Target | Registration scope | After installation |
| --- | --- | --- |
| `codex` | `CODEX_HOME/config.toml`, normally `~/.codex/config.toml` | Reload MCP or start a new Codex CLI session |
| `claude` | Claude Code user scope in `~/.claude.json` | Reload MCP or start a new Claude Code session |
| `grok` | `~/.grok/config.toml` | Start a new Grok Build session. Grok also reads a project's own `./.grok/config.toml`, so check both when an entry looks missing |
| `kiro` | `~/.kiro/settings/mcp.json` | Reload or start a chat; custom agent definitions can restrict tools |
| `crew` | The same Kiro global entry; Crew-specific overrides take precedence | Check the effective Crew setting, then reload the chat |

Selecting both `kiro` and `crew` plans one shared file update. KiroCrew can read Kiro's global
configuration, but registration alone does not show that a running Crew chat inherits it:
confirm the tools in the intended chat.

These are outbound connections: they let a client call the bridge. An incoming route, through
which the bridge hands work to an agent, is separate. Claude Code inside Herdr App, for example,
is discovered by `hc`, and registering it as an MCP client does not make it a recipient.

### Crew-specific overrides

Crew also reads `~/.kiro/crew/mcp.json`, whose same-named entry takes precedence over the shared
Kiro entry. Updating the global entry alone does not change an existing Crew override, and a
migration of either `kiro` or `crew` inspects that override first: a matching route, or an entry
shadowing the old or canonical name, blocks the whole batch before the shared file changes.
Resolve it in Crew's native MCP settings, then preview again. Do not hand-edit the generated
`kirocrew-skill-view-*` files.

After a change, use **Reload** in the intended Crew chat's menu. The conversation is kept and
only that chat's agent process is relaunched with the new settings; a saved setting does not
refresh a connection that is already running. This has been seen directly: a Crew entry still
pointed at a stopped test bridge, and correcting the entry and reloading the chat restored
`peer_search` and `task_complete`.

### Excluded desktop route

The Codex Desktop/IDE route and standalone Claude Desktop configuration are present in the code
and not part of this release.

## 3. Install the bundled skills

The wheel includes two skill folders: `answerback-collaboration`, with its references and agent
metadata, and the setup skill `answerback-fleet` (`SKILL.md`, three references,
`scripts/fleet.py` and `.answerback-fleet.json` with the skill's name and version). One command
installs both:

```sh
answerback skill install --client codex
answerback skill install --client codex --apply
```

Repeat `--client` to narrow the selection, as with MCP installation. The helper keeps a shared
copy of each skill in `~/.agents/skills/` and links the selected Codex, Claude, Grok and Kiro
skill directories to it; Crew uses Kiro's skill directory. `--source /absolute/path/to/folder`
installs another complete folder as the collaboration skill, and the setup skill is then left
out of that run.

Every line of the result names its skill, and `sources` names the folder each came from. Both
skills are planned before anything is written, so one conflict stops the whole run. Equal
content is reported as `unchanged`. A collaboration skill with different contents needs a
reviewed `--replace`, which backs up the old folder under
`~/.local/state/answerback-bridge/backups/`. The setup skill has one status of its own:

- `kept`: a usable setup skill that differs from the packaged one is already there (a link to a
  checkout, an older copy). It is left as it is and does not stop the run; `--replace` backs it
  up and installs the packaged copy. Something at the destination that is not a setup skill (a
  `SKILL.md` whose front matter names it and a `scripts/fleet.py`, neither a link) is a conflict.

An existing full `herdr-collaboration` skill also needs `--replace`. It becomes a small alias
pointing at `answerback-collaboration`, so the old name keeps working without a second full
copy. Keep the separate upstream `herdr` skill for older TUI work.

Reload skills or open a new session. Invoke `$answerback-collaboration` in Codex,
`/answerback-collaboration` in Claude Code, or ask the client to use the skill by name. Check
skill loading in the client itself.

On a LAN machine, the setup skill's own `fleet.py apply` writes the copy from the coordinator.
`fleet.py ensure` reads the bridge on each machine (version, configuration, service, client
entries, skill, MCP handshake) and writes nothing; `fleet.py ensure --yes` installs, upgrades,
stops and starts bridges and has so far run against stand-ins in tests only.

### The Herdr App side panel

When the Herdr App is installed, `answerback install` also installs its side panel (the fleet
column) and six theme files. To preview that step, run it alone, or see what is there:

```sh
answerback herdr-app install
answerback herdr-app install --apply
answerback herdr-app status
```

Files you changed are kept unless `--replace` is given, and are backed up before replacement.
Without the App's folder nothing is done ([Herdr App panel](HERDR-APP-MOD.md)).

## 4. Verify discovery and choose recipients

```sh
answerback doctor
answerback find "project keywords"
answerback find "project keywords" --provider hc
```

Check that the client lists `peer_search`, `peer_submit`, `task_get`, `task_wait` and `hc_read`.
A chat that was open before the install may need a reload to see them. An unfiltered
`peer_search` searches every enabled provider. Owned workers are registered with
`workspace add`; agents in Herdr App/core terminals are discovered without registration once
the `hc` provider is enabled.

For an `hc` peer, read the exact returned address, `send_ready` and `send_readiness_reason`.
Only a recognised, live agent on this machine with a known current directory and a status
report of `idle` can receive work; a working, blocked, errored or unknown-status agent is
inspection only, through `hc_read` or `answerback read-hc`. Agents such as Kiro CLI and Claude
Code report `done` after a turn, and a `done` agent reports `send_readiness_reason: done`.

`"send_to_done": true` in the `providers.hc` block (`hc_send_to_done`) lets the bridge send to a
`done` agent without a status condition. The setting is Experimental and off by default
([Harnesses](HARNESSES.md)). The text can then land in a turn that has
started, and a send receipt whose status is not `done` or `idle` leaves the task `uncertain`.
Core foreground detection accepts `kiro` and `kiro-cli` as Kiro and `grok` and `grok-build` as
Grok; a Crew terminal with no status report stays read-only.

Terminals on another machine (`machine/tNN`) are read-only (`remote_read_only`) unless that
machine is listed in `"send_machines"` of the `providers.hc` block (`hc_send_machines`), using the
exact names `hc machine ls` prints; the list is empty by default and the name `local` is
refused. Remote sending is Experimental, and remote result files are qualified for Linux with
Codex CLI only ([Harnesses](HARNESSES.md)). Listing a machine means
trusting that machine's Herdr core and its OS user: an agent there reports through a result
file in `$HOME/.answerback/results` on its own machine, which the bridge reads through
`hc file read`, and any program of that user can write it.

Such a receipt reads `submitted` while the terminal lives and `uncertain` with
`terminal_ended_no_result` if the terminal ends without a valid file; `task --reconcile` reads
the file at once. The file format, the token, the guards before a remote send and
`hc_placement_policy` are in
[Herdr integration](HERDR-INTEGRATION.md#guards-before-a-remote-send-and-what-a-remote-receipt-says)
and [Recipients](RECIPIENTS.md).

Discovery can read Herdr Core's `hc events --json` stream instead of running `hc ls` and one
`hc get` per terminal: set `"events": true` in the `providers.hc` block (`hc_events`, default
`false`). Readiness is decided the same way, and a send still checks the terminal with `hc get`
before and after.

Once a peer is chosen, submit the authorised assignment once with its request ID and follow the
receipt. Reuse a request ID only for the same assignment to the same recipient. A timeout or an
`uncertain` receipt needs inspection before any retry ([Receipts](RECEIPTS.md)).

## Optional providers

Every provider is a plugin the kernel loads from the `answerback.providers` entry-point group.
The built-ins ship inside the `answerback` distribution; a provider from another package (the
internal provider package, or a third party's) is installed beside the kernel with
`uv tool install ... --with <package>` and appears in `doctor`, `providers list`,
`answerback --help` and the MCP tool list exactly as a built-in does. A provider whose package
is not installed is not an error: its block is kept and `doctor` reports it as `disabled` with
`no loaded plugin claims this provider; its settings are kept`. Only `providers register` for
such a name is refused.

An owned worker needs only a workspace alias; `--provider` defaults to `kiro`:

```sh
answerback workspace add reviewer --cwd /absolute/project
```

For incoming KiroCrew access, keep the Crew gateway running and enable it once per bridge:

```sh
answerback crew-configure
answerback find "project keywords" --provider crew
```

Some desktop distributions wrap `kirocrew` in a launcher that does application upkeep even for
`--version`; if your installation ships a backend CLI, select it with
`crew-configure --binary /absolute/path/to/kirocrew`. A connection-refused result means the
Crew gateway must be started separately; starting the bridge does not start Crew.

## Manual or other client registration

For a client outside the helper's targets, print a connection definition:

```sh
answerback --config /absolute/path/to/config.json mcp-config --client codex
```

This prints JSON and edits nothing. Generate it from the installed tool so the executable path
is durable, then merge it into the client's configuration, keeping its other servers. A common
STDIO shape:

```json
{
  "mcpServers": {
    "answerback_bridge": {
      "command": "/absolute/path/to/answerback",
      "args": ["--config", "/absolute/path/to/config.json", "mcp", "--client", "codex"]
    }
  }
}
```

Use resolved absolute paths: JSON strings do not expand `~` or `$HOME`. `--client` takes `codex`,
`claude`, `grok`, `kiro`, `crew` or `kiro-crew`; an unrecognised client can omit it and use the
tools without presence or an inbox. Registration gives a client the tools through its own
approval controls. It does not make that client a recipient.

## Maintain the connection

Stop the bridge before replacing the installed package, then start it again. Preview skill
updates with `skill install`; apply with `--replace` when updating a different installed
version, including a setup skill reported as `kept`. Existing MCP entries normally stay valid;
recheck the tools after a client reload. `doctor` checks enabled providers; a provider it lists
as `disabled` has a block in the configuration and no installed plugin
([what to do](TROUBLESHOOTING.md#provider-and-module-plugins)).

A successful registration shows that the client can reach the bridge. It does not show that a
live model completed a hand-off; [VALIDATION.md](../VALIDATION.md) records that.

## What it cannot do yet

- The Codex Desktop/IDE route (`desktop`) and standalone Claude Desktop configuration are
  present in the code and excluded from this release.
- KiroCrew as a recipient is excluded from R1, and KiroCrew as a sender is Experimental; a running
  Crew chat's inheritance of the shared Kiro entry is not verified by the installer.
- `hc_send_to_done` and `hc_send_machines` are Experimental and off by default. Remote result
  files are qualified for Linux with Codex CLI only ([Harnesses](HARNESSES.md)).
- `kiro_trust_tools` (and `kiro_trust_all_tools`) are Experimental and off by default: a
  managed Kiro worker runs unattended only on the opt-in v2 engine, and `["read"]` is the only
  list shown live ([Harnesses](HARNESSES.md)).
- The setup skill's `fleet.py ensure --yes` has run against stand-ins in tests only.

## See also

- [Install the bridge](GETTING-STARTED.md): install, start and the first hand-off.
- [Registration and presence](REGISTRY.md): which providers and clients the bridge lets in,
  heartbeats and inboxes.
- [Recipients](RECIPIENTS.md): every incoming route and its readiness rules.
- [Herdr integration](HERDR-INTEGRATION.md): every Herdr interface the bridge uses.
- [Troubleshooting](TROUBLESHOOTING.md#client-setup): tools that do not appear after an install.
