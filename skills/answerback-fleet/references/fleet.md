# Fleet and nodes

## How a node is reached

The coordinator sends `fleet.py` itself to a node on standard input (`hc exec --on <machine> -i`) and the
node's own `python3` runs it. So one piece of code decides what is safe on every machine, with that
machine's home directory, user and links. A survey or a plan leaves nothing on the node.

For an install, the skill folder travels with the script as an archive. The node unpacks it into a
temporary folder, checks its hash against the one the coordinator computed, plans against its own
directories, and only then writes: staged beside the destination, hashed again, moved into place in one
step. The temporary folder is removed.

The previous copy or link is moved aside into the staging folder by one rename, and is put back by one
rename when a later step fails or the run is interrupted. It is at the destination or at `old` in the
staging folder. A staging folder is removed only when the file system says that the previous copy or link
is not in it, or that the new copy is in its place.

- A failed update says in its error where the previous copy or link is: `the previous one is back in
  place`, `the previous copy is kept at <staging folder>/old` when it could not be put back, `nothing was
  replaced`, or `nothing was there before`. The error names a staging folder that is still there.
- When the new copy cannot be moved into place and the file system then does not answer where the
  previous one is (an error that is not "no such file"), nothing is moved and nothing is removed. The
  error says `it could not be read where the previous one is (<error>); nothing was removed, look at
  <staging folder>/old`: the previous copy or link is there, and the new copy is at `new` beside it.
- An interrupted update says the same in one line on standard error (`fleet: stopped while updating
  <destination>...`) when the previous copy or link was moved or a staging folder is left.
- When a second interrupt comes while the previous one is put back, nothing more is moved or tidied.
  When the previous copy is still in the staging folder, or the file system does not say, one line says
  where it is (`fleet: stopped while putting back <destination>; the previous copy is kept at <staging
  folder>/old`). When it is already back in place, no line is written, and a staging folder that holds
  only the new copy may stay.
- After an update that is done, a staging folder that cannot be removed is named in the step's result.
  When an interrupt comes while that folder is removed, the new copy is in its place, nothing more is
  removed, and one line on standard error names the folder when it is still there (`fleet: stopped
  after updating <destination>; the staging folder <path> is still there`). The previous copy may
  still be at `old` in it.

Requirements on a node: registered and `connected` in `hc machine ls`, and `python3` 3.9 or later. A node
that fails either is `left out` with the reason. Nothing is installed to make a node qualify.

## What a node gets

A copy in `~/.agents/skills/answerback-fleet` with a manifest file (`.answerback-fleet.json`: the skill's
name and its version), and links from the harnesses that need one. The manifest is how the next `apply`
knows it may update that folder. A folder without it is someone else's and is refused.

A bridge that ships this skill installs the same folder with `answerback skill install`: the same files,
the manifest included. `apply` treats that copy as its own: equal content is `unchanged`, other content
is updated, and `--remove` removes it. The bridge's installer in turn reports a copy with other content
as `kept` and replaces it only with `--replace`.

The repo is not cloned on a node. To update a node, run `apply` again from the coordinator; `plan` shows
`copy ... [updates the copy this tool wrote]`. A node that was offline is simply `left out` and picked up
by the next run.

## Drift lines and what to do with them

| Line | Meaning | Action |
| --- | --- | --- |
| `bridge versions differ: ...` | Machines run different bridge releases. Commands and skill text may not match. | Report. The owner upgrades. |
| `<machine>: the bridge service is not running` | The CLI is there, its service is not. A harness on that machine can load the skill but has no bridge to call. | Report. Never start it. |
| `answerback-fleet differs: ...` | Machines hold different content of this skill: the coordinator's entry is a link to a live checkout, a node holds the copy of its last `apply` or of its bridge's `skill install`. | Run `apply` for the nodes named, after the owner's yes. It updates a copy with the manifest, whoever wrote it. |
| `<machine>: answerback is not installed` | No CLI. The bridge steps are skipped there. | Report. |
| `<machine>: <dir> is a link to <path>, not to .agents/skills` | That harness's skills live in another shared folder. | The plan links into it; mention it. |

## The pool

`fleet.py pool` lists each machine with `yes` or `no`: capacity `ok`, at least one harness, the bridge CLI
present. It is a reading taken now, for the usage skill's placement step. It is not a registry, it is not
stored, and it does not authorize sending work anywhere. The coordinator is marked; keep heavy work off it
even when it reads `ok`.

## What `ensure` does on a machine

| Step | Looks at | With `--yes`, when missing |
| --- | --- | --- |
| `install` | `answerback --version` against the coordinator's; a newer bridge is `ok` and the line names both versions | installs the coordinator's release wheel as a `uv` tool, then reads the version again; never an older program over a newer one |
| `configure` | the configuration file beside the bridge's socket | `answerback configure --runtime hc`, only when no configuration is there |
| `start` | `answerback status`: running and accepting; where a systemd user manager answers, which unit holds the bridge | see "The `start` step" below |
| `register` | preview of `answerback clients install` | the same with `--apply`, then previews again |
| `skill` | preview of `answerback skill install` | the same with `--apply`, then previews again |
| `skill-link` | only for a harness with a linked skills directory: the one link is there | creates that link |
| `handshake` | starts the command of each entry the installer reports as registered and sends MCP `initialize`; an entry the installer refused is not tried | nothing; it only reads |

A step that fails stops the steps that need it. When `install` fails, also on a machine that has an older
bridge, every later step is `skip` with `needs the bridge at <version>`: an old bridge's installers are
not run.

**What a look starts.** A look (no `--yes`) writes no file. It runs `hc machine ls` (where the
terminal host is installed), `answerback --version` and
`answerback status`, asks `systemctl --user` (where there is one) what is enabled and active,
runs the two installers without
`--apply`, and, when the bridge runs and an entry is known, starts the command of each entry the
installer reports as registered and sends it one MCP `initialize`. An entry that the installer refused,
because it differs from what the installer would write, is not started. Where the installer's row
carries the command it would write, that command is tried instead and the line says `whose registered
entry differs and was not tried`. `uv tool
dir` is run only with `--yes`, and only when a node needs an install. For each chosen node the
coordinator makes a call through `hc` that runs this file there as a look. A look on this machine with no
bridge says `install answerback: no release wheel is known here`.

**The wheel.** It comes from the coordinator's own install receipt, so a coordinator installed from a
source folder has nothing to send. The version in the wheel's own `METADATA` must be the version the
coordinator's program reports; otherwise no wheel is sent and the node's `install` is `fail` with `the
coordinator's wheel is <x>, its program reports <y>`. On the node the message is checked before anything
is written: plain names for the wheel, the release and each extra, and the hash of the bytes. The wheel
is written to a private folder under `~/.local/state/answerback-bridge/releases/`: the folders
`answerback-bridge`, `releases` and the release are opened one inside the other without following a
link (a link in the place of one of them is an error and nothing is written), and the wheel and the
constraints file are created and renamed inside the open release folder.
`ensure` checks right before it starts the installer that the paths lead to the bytes it wrote. A
process of the same user that exchanges the folder between that check and the installer's read is not
defended against. When the bytes behind a path differ, or a path cannot be read, the installer is not
started and `install` is `fail`: `the release folder changed while it was written (<file>); nothing was
installed`. `install` is `done`
only when `answerback --version`, read again, is at least the wanted version.

**The order of an upgrade.** `ensure` puts the new program in place while the old bridge runs, and
restarts the bridge afterwards. The manual procedure in `docs/CLIENT-SETUP.md` stops the bridge first.
While the install runs, the bridge that runs is the old one, and it may load a module from the new
program. After an upgrade the `start` step is `done` only when the bridge that runs reports at least the
installed version (`the bridge that runs reports <old>, not <installed>: it was not replaced` otherwise).
A bridge that does not say its version in its status is `done` all the same, and the line adds `the
running bridge does not say its version`.

**What is not rolled back.** `uv tool install --force` replaces the installed program; `ensure` cannot put
the old one back, and the preview of an upgrade says so. `answerback configure`, the installers' entries
and the one link stay as written when a later step fails.

**A configuration that is there is never edited.** Without the core provider it is `refused` (`<path>
does not enable the core provider; ensure does not edit a configuration that is there`); one that cannot
be read as a mapping is `fail`. `start` is then `skip`.

**The installers.** An installer's answer counts only when it is whole: a mapping with a list of rows, one
row for every client asked, only statuses the installer can give in the phase that was read (a look, or
an apply), and in every row the change flag the installer writes (`would_change`), fitting the row's
status: true for `planned`, `installed` and `migrated`; false for `unchanged`, `kept` and `skipped`;
either for `refused` and `blocked`. `installed` and `migrated` come only from an apply: a look that
answers one is `fail` (`the installer's look answered installed for claude, which only an apply gives`),
and a flag that does not fit names the row, the status and the phase. The skill installer's answer must
also name both skills the bridge ships, the usage skill and this setup skill, under `sources` and as a
row for every client asked. These are two messages: a skill that is missing whole is `fail` with `the
installer's answer names no <skill>`; a row that is missing for one client is `fail` with `no row for
<skill> for <client>` inside `the installer's answer is not usable (...)`. Anything else
that is not whole, and an exit code other than 0 without a `refused` row, is `fail` with `the installer's
answer is not usable (...)`. A row that says `planned` is work, whatever its flag says: the step is
never `ok` from rows that plan a change. A `refused` row makes the step `refused`. With `--yes` the
answer of the apply itself counts, whatever a look afterwards says: rows it `refused` make the step
`refused` (`refused by the apply: ...`), rows `blocked` or an exit code other than 0 make it `fail`.
Rows the installer `kept` or `skipped` are named in the detail. With `--replace` the preview says `back up
and replace what is there, and install:` and names each entry, for the skill installer each skill for
each client.

**A skill installer of an older build.** A bridge that was built before the setup skill was part of the
package ships the usage skill alone, and its installer does not name skills. Its answer has `source`
(one path) where the newer answer has `sources` (a mapping), and no row has a `skill`. An answer of
exactly that shape is read as an answer about the usage skill: its rows are reported as rows of the
usage skill (`ok`, `change`, `refused`, and with `--yes` `done` or `fail`, by the rules above), the setup
skill is not asked for, and the `skill` line ends with `the installed bridge's installer is an older
build: it does not name skills and does not ship the setup skill`. Where the installed version is the
wanted one, the `install` line also ends, once, with `the installed bridge is an older build than a
source of the same version (its skill installer answers in the older form); only a reinstall from the
source changes that, which is the owner's step`: the version alone does not tell the two builds apart.
Neither sentence changes a state, so a machine whose every line is `ok` reads `ok`. With `--yes` the
answer is read the same way for the look, for the apply and for the look after it; the older form is
never a reason to install, upgrade, stop, start or write anything, and `ensure` does not reinstall the
bridge because of it. An answer that is not whole in another way is `fail` as before: a `skill` that is
there and is no text, rows of which only some have a `skill`, or rows without a `skill` in an answer
that has `sources`, is `the installer's answer is not usable (the row for <client> names no skill)`.
The newer answer has both `source` and `sources` and names a skill in every row; it is read as before.

### The `start` step

A look says `change` for a bridge that does not run, for one that runs with nothing to restart it (where
a user manager answers), and for one that an upgrade or a newly written configuration would restart,
also when the configuration is gone and the `configure` step plans to write it: a bridge that runs is
shown as `restart ...`, not as `answerback start`. When a running bridge would be stopped or restarted,
the line names how many tasks are unfinished.

With `--yes` the step tries the bridge's own supervisor first (`answerback start`, or `answerback restart`
for a bridge that runs). The result of every command it sends is read. The bridge is observed with
`answerback status` after each stop, start, restart, `disable --now` and `enable --now` that the step
sends itself, with two exceptions: the repair of the unit sends `daemon-reload` and `enable --now` one
after the other, and the bridge is observed after the two, not between them; and where a repaired unit
of this tool is disabled before `answerback start` for a bridge that did not run, the bridge is observed
after that start, not between the two. An observation has three answers: up (the status was read and says
running and accepting), down (the status was read and says not running), and not known (anything else).
`done` is written only after "up"; `now stopped` only after "down"; otherwise the line says `whether it
runs cannot be told (<reason>)`. A command that reports failure is a failure of the step even when the
bridge is up afterwards. Two cases that the bullets below describe are not the end of the step: a stop
that reports failure and leaves the bridge down, and a restart that reports failure and leaves the
bridge not up, are followed by the next command, and the step can then end `done`. Every result after a stop, a restart or a `disable --now` of a bridge whose status said that it runs
names a count of unfinished tasks. The count is from the status that was read right before the first
of them on that path, and it is read again right before each later stop of the path (the stop of a
bridge that the bridge's own start left up outside its unit); what an earlier command of the path said
stays in the line after the new count. For a bridge that did not run at the start, the count is from
the status read right before the stop of the bridge that this step started. Where a repaired unit is
disabled before `answerback start` and the status read before said that no bridge runs, nothing is
stopped and the result names no count; where that status said running and not accepting, the result
names the count. A status without a task map gives `an unknown
number of unfinished tasks`; no older count is used. A program that cannot be started at all counts
as a command that failed, not as a crash of `ensure`.

- **A unit of our name that is not ours holds the bridge.** Where a file
  `answerback-bridge-fleet.service` is there that this tool did not write for this configuration, that
  unit is enabled or active, and the bridge's own unit for this configuration is not enabled and active,
  the step is `refused`, in a look and with `--yes`: nothing is stopped, started or disabled.
- **A restart.** When `answerback restart` fails and the bridge is up, the line is `fail`: `answerback
  restart failed (...); the bridge that ran before still runs`, and no further command is sent. When it
  fails and the bridge is not up, `answerback start` follows, and the step is `done` when that start
  works and the bridge is observed up. When it
  reports success and the bridge that answers has the same `instance_id` as before, the line is `fail`:
  `the restart changed nothing: the bridge that runs is the one from before`. A bridge that was under its
  own unit before and afterwards runs with that unit not enabled and active is `fail`: `running, but its
  own unit is no longer enabled and active: nothing restarts it`. Where no user manager answers, the
  line says `running; what restarts it cannot be told here`.

- **The bridge did not run.** A repaired unit of this tool is disabled first, then `answerback start`.
  When that fails and `--standalone` was given: on Linux
  with a user manager, the repaired unit; elsewhere a detached `answerback start --standalone` (through
  `systemd-run` or `setsid`), which nothing restarts. When the repair is refused or fails, no detached
  start hides it. Where no user manager answers and `answerback start` reports failure with the bridge
  up, the line is `fail` with `--standalone` too: `running; what restarts it cannot be told here
  (answerback start reported failure: ...)`, and no detached start is made.
- **The bridge ran with nothing to restart it.** `answerback stop`; when the stop fails and the bridge
  is observed up, nothing else is done (`could not stop the running bridge (...); it still runs as
  before`). When the stop fails and the status cannot be read, one detached start is tried and the line
  says what was then observed. Then `answerback start`, and with `--standalone` the repaired unit. A
  bridge that the bridge's own start left up outside its unit is stopped before the repaired unit is
  started (the bullet on that stop, below, says what follows it). When neither supervisor holds, the
  bridge is started detached again (on Linux through `systemd-run` or `setsid`), also without `--standalone`, and the line is
  `fail`: `could not be put under a supervisor (...); it runs again as before, with nothing to restart
  it`.
- **The bridge ran under the repaired unit, and was upgraded**: the unit is disabled, the bridge's
  own supervisor is tried, and the repaired unit is removed
  once that works, so the workaround retires itself when a release carries the fix (when the file cannot
  be removed, or the reload after it fails, the line stays `done` and says which). When the bridge's own
  supervisor fails, the repaired unit is enabled again and the line says `as before`; when that enable
  fails, the line says `was enabled before and could not be enabled again`. When the `disable` itself
  fails and the bridge is not up, the unit is enabled again once and the line says what that gave.
- **The stop of a bridge that the bridge's own start left up outside its unit** (in each of the three
  cases above) is judged by what is observed after it. A stop that reports failure while the status says
  running and not accepting (`answerback stop` gives up after ten seconds while the bridge drains) is
  followed by up to 30 more reads, one a second, until the bridge is up or down; the first stop of a
  bridge that ran with nothing to restart it is read the same way.
  - Up, whatever the stop reported: `fail`, and the step ends: `the bridge runs outside its unit and
    could not be stopped (...); nothing restarts it`. Nothing is repaired, reloaded or enabled.
  - Down, and the stop reported success: the step goes on.
  - Down, and the stop reported failure: the step goes on only when `systemctl --user is-enabled` says
    of none of the bridge's own units for this configuration that it is enabled (where no user manager
    answers, it goes on). The line then ends with `; the stop of the bridge outside its unit reported
    failure (...), and the bridge was down after it`, whatever the result is. When such a unit is
    still enabled: `fail`, and the step ends: `the stop of the bridge outside its unit reported failure
    (...); the bridge is down and its own unit <name> is still enabled; nothing was repaired or enabled`.
  - Not known: one attempt at the way back, and the line says what was tried and what was then seen;
    the result is `fail`. A bridge that ran with nothing to restart it: one detached start. A bridge
    that ran under the repaired unit: `enable --now` of that unit. No bridge ran before: nothing is
    started, and a repaired unit that was enabled before this run is enabled again without `--now`.
  - When one of these results leaves the repaired unit disabled although it was enabled before the
    run, the line ends with `answerback-bridge-fleet.service was enabled before, was disabled by this
    run and is not enabled again`. The same sentence ends two results of a bridge that did not run,
    whose repaired unit was enabled and was disabled before `answerback start`: the `fail` after a
    start that reports failure and leaves the bridge up (`running, but nothing restarts it (...)`),
    and, with `--standalone`, the `fail` of a repair after which `systemctl --user is-enabled` answers
    `disabled`. When that question gets no answer, the line ends instead with `was enabled before and
    was disabled by this run; whether it is enabled again cannot be told (<reason>)`. Other
    failures of the step that leave it disabled say `was enabled before and could not be enabled
    again` where the enable was tried.
- **A bridge that ran and is not observed up at the last look** is always `fail`. `ensure` looks once
  more after the step, and the words about the bridge's state are made from that look. When the bridge
  is then not up and the line does not begin with what that look saw, the line becomes `previously
  running, now stopped (the start step had said: ...)` or `previously running, whether it runs cannot
  be told (...) (the start step had said: ...)`; this also holds for a line that began `previously
  running, now stopped` when the look can no longer tell whether the bridge runs. When the
  bridge is then up and a failed line begins with `previously running, `, the line becomes `the bridge
  answers at the last look (the start step had said: ...)`, and stays `fail`. A `refused` line is not
  changed.

The repaired unit is `~/.config/systemd/user/answerback-bridge-fleet.service`, written with mode 0600
from the unit the bridge generated for this configuration. Its `ExecStart=` is decoded word by word the
way the bridge writes it: exactly one word is `--config`, the word after it is this configuration's path,
and no other word begins with `--config`; otherwise `the unit <source> is not for this configuration`.
A command that holds the word `--` is not repaired (`the unit <source> cannot be repaired (its command
holds the word --)`, with the word in backticks), because the bridge reads no option after that word.
The unit starts the bridge again after a failure and at the next start of the user's manager: at login,
and at boot only when lingering is on for the user, which this tool never switches. It is written
through a new staged file that is created exclusively (never through a link or a file that is there)
and put in place by one rename. Its `WorkingDirectory=` is the decoded value with `/.` appended, as the
bridge writes it since T-146; a value that cannot be decoded, is not absolute, holds a line feed or a
carriage return, or has a `..` component is not repaired. The unit carries a line that marks it as this
tool's for this configuration. A unit of that name without the mark, or marked for another configuration,
is never written over, disabled or removed. `answerback stop` stops the unit (a clean exit is not
restarted).

Known on 2026-11-21: `answerback start` fails on Linux with systemd 259, because the generated unit
quotes `WorkingDirectory=`. The fix is in the bridge since row T-146 and reaches a machine with the next
release; until a machine has it, `start` shows `fail` there without `--standalone`.

#### Known limits of `ensure --yes`

The last check of board row T-150 (2026-11-22) found three sentences in lines of results whose state
is `fail`. Board row T-155 changed all three, and the parts above describe them as they are now: the
last look makes the words about the bridge's state again also when a line began `previously running,
now stopped`; the count of unfinished tasks is read again right before a later stop; and a repaired unit
that this run disabled before `answerback start` is named where that start fails with the bridge up.
None of the three changed a state: each result was `fail` before and is `fail` now.

What remains:

- **Between a count and its command.** A count is read right before a stop, a restart or a `disable
  --now`, not together with it. Tasks that arrive between the read and the command are not in the count.
- **A bridge that is told apart by its installer only.** A bridge of an older build with the same
  version as the source is recognised by the answer of its skill installer (see "A skill installer of
  an older build" above), and by nothing else. `ensure` says so and does not reinstall it.

`ensure --yes` has run against stand-ins in tests only, on macOS and on Linux. It has not been run
against a real user manager or a real bridge. Run a look first, and try `--yes` on one machine before
several.

### The usage skill in a linked skills directory

A harness whose skills directory is a link to another shared folder (a machine whose `~/.claude/skills` is such a link) is left
out of `answerback skill install`, which refuses it and then installs for nobody. The `skill-link` step
puts one link to `~/.agents/skills/answerback-collaboration` in that directory instead. Whether the link is
there is asked of the file system (`os.path.samefile`), not read from two path texts. Every such harness is
looked at before the first link is made: anything else at one destination, or a file system that does
not say what is there, is `refused` and no link is made for any. A new link that does not open as the
skill is left in place and the step is `fail`: `<path> is linked and does not open as the skill (<why>);
the link was left in place`. No name is removed by this step. The next run finds the link, checks it
again, and is `ok` once the skill opens. When something else stands at the path right after the link was
made, the line says `what is at <path> now is not the link that was made, and was left`.

A fresh machine whose only harnesses have linked skills directories cannot get the usage skill this way:
the bridge's installer has no harness to install it for. `skill` is then `skip` and `skill-link` is `fail`
with `<shared path> is not there, and the bridge's installer has no harness to install it for here`, in a
look and with `--yes`. Install the usage skill in `~/.agents/skills` first.

### Nodes

The coordinator looks at 15 registered machines at most. Every further one is listed as left out with
`more than 15 machines are registered; this one was not looked at`, for every command that covers it.

A node's answer is checked before it is used. A node whose look does not come back or is not a result of
`ensure` is left out with the reason; nothing was sent that changes it. With `--yes`, a node that needs
work gets one acting call with a limit of 600 seconds. It is never sent a second time: a machine that
is named twice is taken once, and a result that is recorded for a machine is not replaced. From then on
the node is listed under `machines`, never as left out.

An acting call that takes longer than 600 seconds is lost to the coordinator, although the node may
still finish it. The node's steps then begin with `act`, `fail`. Do not run `ensure --yes` for that
machine again; wait, then run a plain `fleet.py ensure --machine <name>` (a look), which shows what the
node has now.

What the lines of a node that was told to act say:

- When the acting call does not come back or its answer is not usable, the node's steps begin with `act`,
  `fail`: `the acting call did not come back (<error>); this machine may be partly changed; nothing was
  sent again`. One look follows, and its steps are listed after that line.
- After the acting call `ensure` looks again over a new connection and reports a step that was `done` and
  is not `ok` now as `fail`; a `done` step that the second look no longer reports is `fail` with `no
  longer reported after the call ended`. The terminal host ends every descendant of a remote call when
  the call returns, so a result read inside the acting call is not proof.
- When that look does not come back, the last step is `look`, `fail`.

## Local-only

No terminal host, or no registered machine: `mode: local-only`. Every command works on this machine.
`--machine all` then means this machine.
