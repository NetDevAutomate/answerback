# Show your fleet in the Herdr App

`answerback fleet serve` measures load and memory on this machine and on the machines Herdr Core knows,
reads the bridge's health and receipts, and publishes the result as one JSON file, `fleet.json`. The
fleet column of the Herdr App reads that file to show load, capacity, advice and the receipts that wait
for a person.

It is for someone who runs agents on several machines with the Herdr App. The writer uses the same probe
as `answerback capacity` ([CAPACITY.md](CAPACITY.md)), and other machines must be reachable through Core.
The second half of this page is the reference for the document's fields.

## Try one cycle

```sh
answerback fleet serve --once --dir /tmp/fleet-trial
answerback fleet show --dir /tmp/fleet-trial
```

Real output of the second command on a Linux coordinator, with machine names replaced:

```text
fleet.json 0 s old · mode paused · /tmp/fleet-trial/fleet.json
coordinator ok load 0.26 per core
mac-a unknown · probes paused
linux-b unknown link unreachable · no link
mac-c unknown · probes paused
```

The first command exited 0, so a document was published. The writer is in mode `paused`: the
coordinator's swap was 100% in use, over the 95% stop, so no worker was probed and the workers read
`unknown`. One worker had no Core link at all. See "Modes and intervals" for when probes pause.

## Run it as a service

```sh
answerback fleet configure --machine build-a --machine build-b --ceiling build-a=6
answerback start
answerback status --json
```

`configure` writes the settings file and prints its path. Once that file exists, `answerback start` (and
`answerback install`) start the writer as its own user service after the bridge is ready, and
`answerback status --json` reports it as `fleet_writer`. Details are under "Native supervision".

## What the writer does

The writer is a second long-lived process on the machine that runs the bridge service. That machine is the
**coordinator**. The other machines that Core knows are the **workers**. In each cycle the writer:

1. measures the coordinator with the probe of `answerback capacity`;
2. lists the machines and terminals with `hc ls --json`, and the labelled lanes with
   `hc ls --json --label K=V`;
3. measures every connected worker through `hc exec --on`;
4. asks the bridge service for its health and its receipts;
5. writes `fleet.json`.

It changes nothing in the bridge service, adds no key to the bridge configuration file and installs nothing
on a worker. This page says what the document says; what a reader draws from it is the reader's matter.

## Commands and options

```sh
answerback fleet serve
answerback fleet serve --machine build-a --machine build-b --ceiling build-a=6
answerback fleet serve --lane-label team.lane=1
answerback fleet serve --once --dir /tmp/fleet-trial
answerback fleet configure --machine build-a --machine build-b --ceiling build-a=6
answerback fleet serve --settings ~/.config/answerback-bridge/fleet-writer.json
answerback fleet show
answerback fleet show --json
```

- `serve` runs until SIGTERM or SIGINT, then exits 0. `--once` runs one cycle, publishes and exits 0, or 1
  when nothing could be published.
- `--dir DIR` is the published folder. The default is `fleet/` in the folder of the bridge configuration
  file, beside its `runtime/` folder.
- `--machine NAME` names a worker; repeat it for more. Without it the writer measures every machine that
  `hc ls --json` lists. Names follow `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`, and `local` (the coordinator's
  own name) is refused. At most 15 workers, 16 machines with the coordinator.
- `--ceiling NAME=N` gives a worker an agent ceiling from 1 to 99. Without it that worker has no `agents`
  signal.
- `--lane-label K=V` is the label that marks an unattended lane, default `ab.lane=1`. Key and value are 1
  to 64 letters, digits, `.`, `_` or `-`, and the key starts with a letter or digit. It reaches `hc` as one
  argument after `--label`.
- The global `--config PATH` gives the `hc` binary, the Core session and the service socket, as for
  `answerback capacity`. Without a configuration file the writer uses `hc` on `PATH` and the default Core
  session.
- `--settings FILE` reads `--dir`, `--machine`, `--ceiling` and `--lane-label` from the file `configure`
  writes. Combined with one of those four options, the command exits 2 and names the option. `--once` and
  `--config` combine with it.
- `show` prints the age of the document and one line per machine, and `--json` prints the document. It
  exits 1 when there is no document.
- `configure` takes the same four options as `serve`, and `--settings FILE` for where to write. It measures
  nothing, publishes nothing and does not make the published folder.

Every setting is an option of the command or the same field in the settings file. **There is no key in the
bridge configuration file**, and the bridge never reads the settings file. The intervals and deadlines below
are fixed.

### Exit codes and signals

| Code | Meaning |
| --- | --- |
| 0 | ended by SIGTERM or SIGINT, or `--once` published; the lock and the folders were released |
| 1 | `--once` published nothing, or the folders cannot be used, or the lock or a folder could not be released at the end (the last file may already be published; one line says why); or an error the writer does not expect (one line, or a Python traceback in place of the line) |
| 2 | a wrong option, a settings file that cannot be read or used, or a settings path at or below the published folder |
| 3 | another writer holds the folder (one line says why) |
| 130 | a second SIGINT while the writer releases its lock and its folders at the end |

The writer handles SIGTERM and SIGINT itself only while the cycles run, and SIGTERM or SIGINT during a
`--once` cycle also ends it with 0. Before the last step (releasing the lock and folders) it gives each
signal back the handling it had before. After a usual start, a first SIGINT during that step does not
interrupt it and the command ends with the code it would have had. A second SIGINT after the first was
handled ends it with 130 and no line, two that arrive together count as one, and SIGTERM ends the process as
an unhandled signal does. In both cases the lock is free and the last file stays. The last step has no time
limit, and a first SIGINT does not end a wait inside a system call.

A writer started with one of the two signals ignored also ends its cycles on that signal, exits 0, and
ignores the signal again during the last step. A shell script that starts `answerback fleet serve &` has
SIGINT ignored at its start.

### The settings file

The settings file lets a supervisor (launchd, systemd) start the writer with
`answerback fleet serve --settings FILE` and nothing else, so no service definition carries a machine name.
`answerback fleet configure` rewrites the file without a change to the service definition.

- The default path is `fleet-writer.json` beside the bridge configuration file, which on a fresh install is
  **`~/.config/answerback-bridge/fleet-writer.json`**. `--config` moves it with the configuration file.
- The file is not inside the published folder `fleet/`: the writer refuses a published folder that holds a
  file it does not own, and the settings file is yours. For the same reason `configure` and
  `serve --settings` refuse a settings path that is the published folder or below it, with exit code 2 and
  one line that names both paths; nothing is written or published.
- `configure` writes the whole file with mode 0600 and renames it into place, so a reader sees the old file
  or the new one. A missing folder is made with mode 0700 (set after it is made, so the umask does not
  thin it); a folder that was there keeps its mode. A relative `--dir` is made absolute from the folder the
  command runs in.
- `serve --settings FILE` follows a symbolic link at `FILE` or in its folders. What the path leads to must be
  a regular file of at most 64 KiB (65536 bytes): a folder, device or FIFO is refused with exit code 2 before
  any byte is read, and the open does not wait for a FIFO writer.
- The file is a JSON object. Every field but `schema` may be left out: `machines` (a list of names; empty
  means every machine of `hc ls --json`), `ceilings` (name to a whole number from 1 to 99), `dir` (an
  absolute path, or `null` for the default) and `lane_label` (default `ab.lane=1`). Another `schema`, an
  unknown field, a value that breaks a rule, more than 64 KiB, or a missing file or one that is not JSON in
  UTF-8 ends `serve` with exit code 2 and one line that names the file and the rule.

```json
{
  "schema": "answerback.fleet-writer/1",
  "machines": ["build-a", "build-b"],
  "ceilings": {"build-a": 6},
  "dir": null,
  "lane_label": "ab.lane=1"
}
```

### Native supervision

Once the settings file exists, `answerback install` and `answerback start` start
the writer after the bridge is ready. Without the file they do not install or
start the writer and report ``fleet writer not configured; run `answerback fleet
configure` ``. `answerback status --json` reports the separate `fleet_writer`
service: whether it is installed and running, its PID, settings path, last exit
code and a fixed failure reason when the service manager cannot be read.

On macOS the launch agent is
`~/Library/LaunchAgents/local.answerback-fleet.<digest>.plist`; on Linux it is an
enabled systemd user unit with the same label. The 12-character digest is the
bridge configuration path's digest, exactly as for
`local.answerback-bridge.<digest>`. The definition contains only
`answerback fleet serve --settings <file>`: machine names and ceilings stay in
the private settings file. Logs are written under the configuration's
`runtime/` folder.

The launch agent uses `KeepAlive = {SuccessfulExit = false}` and the systemd
unit uses `Restart=on-failure`. A crash (exit 1, 2 or 3) is retried after ten
seconds. A clean exit happens only on SIGTERM or SIGINT, that is an intentional
stop (`answerback stop`, or an operator), and is not restarted. When its machine
reads `memory_full` or `paused` the writer does not exit: it keeps serving at a
45-second cadence (probes pause only in `paused`). `answerback start` refuses to
start the writer while its settings file is missing or invalid, so an unusable
file cannot put launchd into a relaunch loop. `answerback stop` and
`answerback uninstall` unload and remove the writer service before they stop
the bridge service.

For a temporary foreground run, `answerback fleet serve` still works directly.
Do not run it against the same published folder as the supervised writer; the
second writer exits with code 3 because the first owns the lock.

## One cycle

Every deadline uses the monotonic clock.

1. Note the wall-clock time. This is the cycle's `sent_at`.
2. Measure the coordinator. Its state comes from load and memory only: no
   agent count and no ceiling. Deadline: 5 seconds. It runs first and alone.
3. Choose the mode from the coordinator's reading (below).
4. Run `hc ls --json` and `hc ls --json --label K=V` at the same time, each once.
   Deadline: 3 seconds each.
5. Unless probes are paused, measure every worker whose link is `connected`, all
   at the same time. Deadline for one worker: 8 seconds.
6. Ask the service. Deadline: 2 seconds per call.
7. Build the document, check it, publish it when the write rules say so.

Steps 5 and 6 run at the same time. They start in this order.

**The budget.** A cycle has 12 seconds. The last 2 are kept for ending
children. A step waits until second 10 of the cycle at most, and starts only
while time is left before second 10. A step that is not started counts as
failed, and one error entry says `no time left in the cycle`. Ending and reaping
a child takes 2 seconds at most, and children are ended at the same time.

**What the budget bounds.** It bounds what the writer waits for. The writer
starts no step and no call to the service at or after second 10, waits for
nothing after second 10, and then ends its children. Two things are outside the
budget:

1. The time the operating system needs to create a child process. The writer
   cannot interrupt it. On a machine with an endpoint security agent it can be
   long.
2. A child whose end the system does not confirm within the 2 seconds. One error
   entry says so, and the cycle goes on. Such a child may remain.

The client for the service's socket reads files when it is built. It is built
outside the event loop, in one thread, one build at a time, under the deadline
of the service step. A build that is not ready in time counts as "the service
did not answer in time" for that cycle. When it becomes ready, it is used from
the next cycle. The service step gets the cycle's deadline as an absolute time
and starts none of its calls at or after it.

- What is ready by second 10 is published. What is not ready counts as failed.
- A child that passed its deadline is ended and reaped. The writer reads at most
  1 MiB of a child's output; a child that prints more is ended and counts as
  failed.
- **No task of a cycle is left running.** Whatever ends a cycle or one of its
  stages (an error, the deadline, a signal), every task the cycle started is
  cancelled and awaited before the cycle returns. So no child of the cycle runs
  while the document is published, except a child whose end the system did not
  confirm (above).
- One cycle runs at a time. A tick that falls into a running cycle is skipped
  and is not caught up.
- Every reading of a cycle is stamped with the cycle's `sent_at`. A late answer
  is not stamped as fresh. A failed probe does not change the stamp of the last
  good reading.
- A probe that timed out says nothing about the link. The link state is what
  `hc ls --json` reports.
- **A listing that cannot be used.** When `hc ls --json` fails, or its answer
  has no `machines` list or no `terminals` list, there is no listing: every link
  is `unknown`, `agents.active` is `null`, no worker is probed in that cycle, and
  one error entry says so. A missing count is never taken as zero.
- When only the labelled listing fails, `agents.lanes` is `null` and the agents
  of the first listing are still counted. One error entry says so. It is not
  repeated while the failure lasts.
- **An unexpected error** (one that no rule names) ends the smallest piece of
  work it happened in, and one error entry names its type:
  - in a listing: that listing failed. The first listing is then no listing
    (above); for the labelled listing the lanes are not known;
  - in the probe of one worker: that worker is `unknown` with `probe failed`;
    the other machines are measured and published;
  - in the probe of the coordinator: that probe failed, as for a worker. The
    cycle goes on: with no current reading of the coordinator the mode is
    `unmeasured`, the workers are measured and the document is published;
  - in the service step: the service gave no usable answer;
  - anywhere else in the cycle: the cycle ends, and the writer goes on with the
    next tick.

  A cancellation by a signal is not such an error.
- **Which text of an exception enters the document.** Only text that was made
  on purpose: the message of an `OSError`, of a `BridgeError` (the command
  runner and the service client raise it with fixed sentences), and of the
  writer's own error for a listing that cannot be used (too large, not JSON, no
  list of machines or terminals, or `hc ls` ended with a code). Every other
  exception, also a plain `RuntimeError` or `ValueError`, is unexpected: its
  entry names its type and never its message. This holds for the first
  listing, the labelled listing and the probe of a machine.

## Modes and intervals

Memory signals are all signals of the assessment except `cpu` and `agents`.

| Coordinator's reading | Mode | Next cycle after |
| --- | --- | --- |
| no memory signal is busy or full | `normal` | 15 s |
| the worst memory signal is busy | `memory_busy` | 30 s |
| the worst memory signal is full | `memory_full` | 45 s |
| emergency | `paused` | 45 s |
| no current reading of the coordinator | `unmeasured` | 45 s |

"Current" is decided at the start of each cycle. The last good reading of the
coordinator is current while it is at most 60 seconds old at that moment, also
when the newest probe failed. No timer fires at the 60th second. So after the
coordinator stops answering, an emergency seen in its last good reading keeps
worker probes paused until the first cycle that starts more than 60 seconds
after that reading. With cycles of 45 seconds that is up to 90 seconds: a good
emergency reading at 0 and failed measurements at 45 and 90 give `paused` at 45
and `unmeasured` at 90. A failed measurement is no sign that the pressure is
over, so the writer errs on this side.

`unmeasured`: the coordinator's probe failed or its answer could not be used,
and no reading of it is current. A coordinator that cannot measure itself may be
the machine in trouble, so the writer takes the longest interval. Worker probes
go on, the service is asked and the document is published. `capacity.paused` is
`null`: the emergency rule needs a reading.

Emergency means: no worker probe is started. The coordinator is still measured,
the service is still asked and the document is still published.

- macOS: the kernel's memory pressure level is 4.
- Linux: swap in use is 95% or more of the swap total, and the total is not 0.

On macOS the swap total grows with use, so its share is no stop there. The
document still reports swap. When the emergency ends, probing starts again at
the next cycle. CPU load never pauses probes.

## When the file is written

- The content changed and the last write is at least 5 seconds old.
- Or the last write is 30 seconds old: the heartbeat.
- "Changed" ignores `written_at`.
- **The gap of 5 seconds holds between two attempts, successful or not.** After
  a failed publication the next attempt comes no sooner than 5 seconds after
  it. A tick does not shorten that gap: the cycle runs at its tick, and its
  document is offered once the gap has passed. The heartbeat obeys the same
  gap. Every step of the wait sleeps. A publication that fails every time costs a few
  attempts in a cycle, never a busy processor.
- A rewrite between cycles rebuilds the document from what is known. It never
  makes an old reading look current: readings expire by their own age,
  `taken_at` stays, and the clock-jump check runs first.

## Publication

- The published folder and the staging folder `<dir>.stage` have mode 0700. The
  file has mode 0600. Both folders must be on one file system; the writer
  refuses to start otherwise.
- **An existing folder.** A published folder that exists may hold `fleet.json`
  and nothing else. A staging folder that exists may hold the lock file
  `writer.lock` and the staged file `fleet.json.new`, and nothing else. An
  entry under one of these names must be a regular file of the current user,
  not a symbolic link, with one name only: a file that also has another name (a
  hard link) is refused, and the message says that it has a second name.
  Otherwise the writer refuses to start (exit 1, one line). It checks both
  folders before it changes either. The lock file is checked
  once more on its open file (type, owner, one name) before the lock is taken
  and before its mode is set.
- **What a refused start leaves as it was.** A folder this start made gets its
  mode when it is made; the mode of a folder that was there before is set only
  after the lock is taken and the lock's own mode is set, and removing a
  leftover staged file is the very last step of the start. So:
  - a start that is refused by the check of an existing folder has changed
    nothing and made nothing: both existing folders are checked before anything
    is made;
  - a start that fails later, before it holds the lock, has changed no entry
    that existed and no mode of a folder that existed; it may have made the
    missing folders and the lock file;
  - a start that fails while it holds the lock (the mode of the lock or of a
    folder cannot be set, or the leftover staged file cannot be removed) may
    also have set the mode of the lock file and of a folder that existed.
- **A start that fails removes nothing.** The writer never removes its lock file
  and never removes a folder, not at a start that fails and not at its end. What
  a start made can stay: parent folders that were missing, the published folder
  and the staging folder (empty, mode 0700), and an empty lock file. The next
  start uses them. Why: a start cannot tell whether another start already uses
  what it made, and removing it could let two writers run for one folder.
- **The folders are held by descriptor.** At its start the writer opens the
  published folder and the staging folder once, without following a symbolic
  link, and keeps both open until it ends. A symbolic link given as a folder is
  refused. Owner, type, content and file system are checked through the open
  folders, and their mode is set through them. The lock file and the staged
  file are opened inside the open staging folder without following a link; the
  lock is checked and its mode set on its open file. The rename goes from the
  open staging folder to the open published folder, and the folder is synced
  through it. So a folder path that is replaced by a link after the check does
  not make the writer change or write into another folder. This guard has
  limits (see "What it cannot do yet"). `answerback fleet show` reads the file by its path.
- **The staged file is always made new.** For each publication it is created
  with an exclusive creation inside the staging folder, without following a
  link. The writer never opens, truncates or writes a file that was there
  before. When something stands under the staged name, that publication fails
  with one error entry, the writer removes the name (the name only: a file that
  has another name too is not touched), and the next attempt makes the file
  new. A folder under the staged name is not removed: every publication fails
  until a person removes that folder, the previous file stays, and the next
  attempt after that succeeds. Such a folder can only come from another process
  of the same user after the start; at the start it is refused.
- The document is written and synced in the staging folder, then renamed into
  the published folder. The writer puts nothing but `fleet.json` into the
  published folder, not for a moment. The reader watches that
  folder and gives up when it sees several changes in one second.
- Before the rename the document is checked (below). A document that fails is
  not published. The previous file stays. The next document carries an error.
- When writing or renaming fails, the previous file stays and the next document
  carries an error.
- An unexpected error while the document is built, checked or encoded is a
  failed publication too: one error entry with the type of the error, the
  previous file stays, the gap of 5 seconds holds, and the writer goes on. With
  `--once` the exit code is then 1.
- An error while the file is written or renamed that is not an error of the
  operating system (a fault in the writer's own code) is not a failed
  publication. It ends the writer: the lock and the folders are released, and
  the command ends with exit code 1. A fault of the class `ValueError`, or of
  the bridge's own error class, is printed as one line like a refusal; a fault
  of any other class that derives from Python's `Exception` is printed as a
  Python traceback in place of the one line. The published file is then the
  previous one or the new one, whole.
- One writer for a folder: a lock file in the staging folder. A second `serve`
  for the same folder ends with exit code 3. Of two writers started at the same
  moment for one folder at most one goes on; the other ends with exit code 3 or
  1 and can be started again.
- SIGTERM and SIGINT, with or without `--once`, end the running cycle's
  children, leave the last file as it is, and exit 0 (for an error or a signal
  while the lock is released afterwards, see "Exit codes and signals").
- **Publication is synchronous, on purpose.** It happens between two cycles,
  after the cycle's children were ended. A publication in a thread could not be
  cancelled, and a signal would have to wait for its end before the lock is
  released. So a slow disk delays the next cycle, and the handling of a signal,
  by the time of one write.
- A writer that died leaves its last file. The reader greys a document that is
  older than 120 seconds.

## The document

`schema` is `answerback.fleet/1`. Times are seconds since the epoch, as numbers.
Every field is always present. `null` means "not known".

### Top level

| Field | Meaning |
| --- | --- |
| `schema` | `answerback.fleet/1` |
| `written_at` | when the file was written |
| `writer` | `version`, `instance_id` (new at each start), `pid`, `mode`, `interval_s` |
| `health` | the service's health |
| `capacity` | the fleet's capacity and the advice |
| `machines` | one entry for each machine |
| `receipts` | the receipts of the service |
| `errors` | at most 20 entries `{at, where, text}`, newest last |

The same error text from the same place is not repeated one after the other.

### `health`

| Field | Meaning |
| --- | --- |
| `source` | `status` |
| `read_at` | the last good answer; it stays when the service fails |
| `stale` | true when this cycle's `status` answer was not good |
| `error` | why, or `null` |
| `service` | `state` (`running`, `stopping`, `unreachable`), `accepting`, `started`, `version`, `instance_id` |
| `service.version` | the version of the running bridge: the `fleet_snapshot` answer's `health.version` when that answer was used, else the `version` of the `status` answer (`null` when neither says one); `writer.version` at the top level is the writer's own, and the two differ when a bridge still runs an earlier version than the one installed |
| `bus` | `connected`, `last_error` |
| `clients` | at most 32 `{client, available, live_instances, last_heartbeat_age_s}` |

- `state` is `stopping` only when `status` says `accepting: false`. An answer
  without that field is `running`.
- When `status` does not answer, or its answer cannot be used, `state` is
  `unreachable` and the other values are `null` or empty.
- When `status` answers and only the receipts cannot be read, health is what
  `status` said: a service that answers is running.

### `capacity`

| Field | Meaning |
| --- | --- |
| `taken_at` | the `sent_at` of the last cycle |
| `max_age_s` | 60: a reading older than this is expired |
| `paused` | `null`, or `{reason, since, text}`; `reason` is `pressure_critical` or `swap_exhausted` |
| `node_order` | the workers, in the order `hc ls --json` lists them |
| `ranked_for_heavy_work` | the machines that may get new work, best first |
| `new_work` | `{machine, why}`: the first of them, or `null` |
| `new_suite` | `{machine, why}`: the first of them that runs no test suite, or `null` |
| `recent_launches` | `{machine, seen_at, count}` for launches of the last 60 seconds; `machine` is `null` for the coordinator |

With `--machine`, `node_order` holds the named workers in the order `hc` lists
them, then the named workers that `hc` does not list.

### `machines`

`machines[0]` is the coordinator: `name: null`, `self: true`,
`role: "coordinator"`, `link.state: "self"`. Then come the workers in
`node_order`. Every configured machine is always present, measured or not.

| Field | Meaning |
| --- | --- |
| `name` | the worker's name; `null` for the coordinator |
| `self`, `role` | `true` and `coordinator`, or `false` and `worker` |
| `os` | `darwin`, `linux` or `null` |
| `link` | `{state, since_ms: null, why: null}`; `state` is `hc`'s word, `unknown`, or `self` |
| `reading.taken_at` | the stamp of the last good reading, or `null` |
| `reading.error` | why the reading is not current, or `null` |
| `reading.state` | `ok`, `busy`, `full` or `unknown` |
| `reading.was` | the last measured state behind `unknown`, or `null` |
| `reading.decided_by` | `{signal, state, text}` for each signal whose state equals the machine's busy or full state |
| `reading.pressure` | `{signal, fraction, text}` of the deciding signal with the highest fraction; `null` unless busy or full |
| `reading.load_per_core`, `loads`, `cores` | the CPU measurements |
| `reading.memory` | `{state, text}`: the worst memory signal and a short sentence; swap is in the sentence |
| `reading.agents` | `{active, ceiling, lanes}` (below) |
| `reading.suites` | the probe's count of running test suites |
| `reading.harness_processes` | the probe's count of agent programs on the machine, working or not; `null` when the probe did not give it |

`signal` is `cpu`, `memory` or `agents`. Every memory signal of the assessment
(pressure, compressor, swap, available, stall, largest process) is published as
`memory`. `decided_by[].text` has at most 80 characters, `pressure.text` at most
8 (`1.16`, `32%`, `level 4`, `6/6`).

### Agents and lanes

`agents.active` is the number of live terminals on that machine that are doing
work. It counts as the shared capacity probe of the lanes counts, so that the
document and that probe agree, and it adds the labelled lanes. A terminal counts
when one of these is true, and each address counts once:

- its status word is not `done`, `idle` or `blocked`, and it has a status word
  or its program is not a plain shell (`zsh`, `bash`, `sh`, `fish` or none);
- it is in the labelled listing (`hc ls --json --label K=V`) and has no status
  word.

A `blocked` terminal waits for a person and is not active.

An unattended lane is a plain shell that runs a program and reports no status,
so the first listing cannot tell it from an idle shell. Only its label shows it.
**A lane started without the label is not counted.** The repository defines no
such label: it is what the tools that start lanes put on a terminal
(`hc run --label ab.lane=1 ...`).

- `agents.lanes` is the number of terminals of the second kind. It is `null`
  when the labelled listing failed.
- `agents.ceiling` is the value of `--ceiling`, else `null`.
- With `--ceiling`, the `agents` signal of the assessment uses the same count:
  at or above the ceiling the worker is `full`.
- Without a usable listing `agents.active` is `null`.

The words of `reading.error`:

| Word | Meaning |
| --- | --- |
| `no link` | the link is not `connected`; no probe was started |
| `probes paused` | the emergency stops probes; the machine was never measured |
| `probe timed out` | no answer within the deadline |
| `probe answer not understood` | the answer could not be parsed |
| `probe failed` | `hc exec` failed, or the probe raised an unexpected error |
| `reading incomplete` | the answer missed an essential measurement |
| `reading expired` | the last good reading is older than 60 seconds, or the clocks jumped |
| `no time left in the cycle` | the probe was not started (the budget) |
| `not measured yet` | the machine has had no probe yet |

### The pressure scale

`fraction` puts every signal on one scale: the busy limit is 0.70, the full
limit is 1.00. It is given with three decimals.

- From 0 to the busy limit and between the limits the scale is linear. Beyond
  the full limit it keeps the same slope. It is never above 2.0.
- A busy signal that can become full keeps its value on the scale, but stays
  below 1.00 (0.999 at most), so a busy fraction never reaches the full mark.
  For example a load of 0.995 per core is 0.995.
- A signal that cannot be full alone (macOS swap, Linux stall) stops at 0.99.
- A full signal is 1.00 at least.
- The limits are read from `placement/assess.py`.

| Signal | Value | 0.70 | 1.00 |
| --- | --- | --- | --- |
| CPU | load per core | 0.70 | 1.00 |
| macOS pressure | kernel level | level 2 | level 4 |
| macOS compressor | share of memory | 25% | 40% |
| macOS swap | share of memory | 25% | never |
| Linux available | share of memory, falling | below 15% | below 7% |
| Linux stall | PSI `some avg10` | 10 | never |
| Largest process | share of memory | 25% | 50% |
| Agents | active / ceiling | (none) | at the ceiling |

For CPU the fraction is the load per core, within the rules above. The macOS
pressure level has two points only: 0.70 for level 2 or a level the assessment
does not know, 1.00 for level 4.

### Expiry

A reading whose age by the monotonic clock is over 60 seconds is published as
`state: "unknown"` with `was` set to its last measured state. `taken_at` stays.

A failed probe also makes the machine `unknown`, with `was` and the stamp of its
last good reading, and the error word. A machine that was never measured is
`unknown`, `was: null`, `taken_at: null`, and `error` says why.

**Clock jump.** When the wall clock and the monotonic clock disagree by more
than 5 seconds since the last cycle (the machine slept, or the clock was set),
every reading is expired and one error entry says so. The check runs at the
start of every cycle and before every rewrite between cycles.

### Advice

A machine may get new work when all of this is true:

- it is a worker;
- its link is `connected`;
- its last probe succeeded and its reading is not expired;
- its state is `ok`;
- no launch was seen on it in the last 60 seconds;
- probes are not paused.

A **launch** is a terminal address that is in this cycle's `hc ls --json` and
was not in the previous one. The first listing after the start is the baseline
and counts no launch. A launch is remembered for 60 seconds, also when its
terminal has ended.

- `ranked_for_heavy_work` is in the order of the placement code's ranking: load
  per core in bands of 0.25, then `node_order`, then name.
- `new_work.machine` is the first of them, or `null`.
- `new_suite.machine` is the first of them whose `suites` is 0, or `null`.
- With `null`, `why` names the rule that left no machine: `worker probes are
  paused`, `no current reading of a worker`, `no ok worker`, `a launch in the
  last minute on every ok worker`, `every ok machine runs a suite`.

The coordinator is never named. **The advice reserves nothing.** Two launchers
that read the same document can pick the same machine.

### `receipts`

| Field | Meaning |
| --- | --- |
| `source` | `fleet_snapshot` or `status+task_list` |
| `read_at` | the last good read; it stays when the service fails |
| `stale`, `error` | true and why, when this cycle's read was not good |
| `counts` | `{<state>: n, ..., total: n}` from `fleet_snapshot` when it gives them, else from `status`; `null` before any good read. `total` is the sum of the listed states: the writer computes it for both sources and never takes the `total` of an answer. A receipt in a state this version does not list is in no count and not in `total`. |
| `needs_person` | at most 200 receipts whose state is `uncertain` or `blocked`, oldest first |
| `needs_person_complete` | true only for a fresh read whose list holds `counts.uncertain + counts.blocked` receipts |
| `needs_person_omitted` | how many the counts show beyond the list, plus what the size rule dropped |
| `recent` | the 8 newest receipts |

A receipt is `{id, state, title, recipient {provider, machine, terminal, text},
request_key, reason, cause, identity_guard, identity_observer, created, updated}`.
`identity_observer` is `core:local` or `core:<machine>` only when the receipt's
`metadata.delivery.identity_guard` is `observed_before_and_after` (the identity was
read before and after the send) and its identity was not invalidated since; it is
`null` otherwise, including every `fleet_snapshot` row, which carries no metadata.
`identity_guard` is then `observed_before_and_after`, else `null`; the page prints
`identity read by <observer>` only when both are set.

**When more receipts wait than the list holds**, two things decide what is
listed. The service decides which receipts it sends: `fleet_snapshot`
([FLEET-SNAPSHOT.md](FLEET-SNAPSHOT.md)) sends at most 200 receipts that wait, those that changed last, and
besides them the newest 20 of any state; `task_list` sends the newest 100
receipts of any state. The writer takes the receipts that wait from everything
it received, sorts them by creation time (one without a usable creation time
counts as the newest) and keeps the newest 200 of them. So the list is not
guaranteed to be the oldest or the newest of all that wait, and it can have
gaps. `needs_person_complete` and `needs_person_omitted` compare the length of
the list with the counts the writer has. With no counts they are `false` and
`null`. With counts of the same read as the list, `needs_person_omitted` is the
number of waiting receipts that are not listed. With the counts of `status` they
compare two reads (see "What it cannot do yet"), and they can be wrong if receipts began or
ceased to wait between the two. The size rule (see "Bounds and the check before
publication") is applied after that: when it drops entries of the list,
`needs_person_complete` becomes `false` and `needs_person_omitted` grows by the
number of entries dropped, counted from 0 when it was `null`.

- **What can happen to `fleet_snapshot`.** A service that was built before this
  method was added does not know it. The service writes one error line to its
  log for every request it refuses, so a question in every cycle would add
  thousands of error lines a day. What the writer remembers is for the current
  instance only. In the first five cases it asks `task_list` in the same cycle,
  while time is left.

  | # | What happened | Remembered | Receipts of this cycle |
  | --- | --- | --- | --- |
  | 1 | No answer: the deadline passed, the transport gave no usable reply (the service is stopped, the connection broke, or the service sent its timeout answer, which the method gives when its store is busy), or another `OSError` | nothing: asked again in the next cycle | from `task_list` |
  | 2 | The error answer `operation_unknown` | a refusal: not asked again | from `task_list` |
  | 3 | Any other error answer of the service | asked again after 300 seconds | from `task_list` |
  | 4 | An answer that cannot be used: `tasks` is not a usable list, or `counts` is there, is not `null` and cannot be used | a refusal | from `task_list` |
  | 5 | An answer that is not complete: `recent` or `needs_person` is there and is `null` (the service could not read that part) | nothing: asked again in the next cycle | from `task_list` |
  | 6 | An unexpected error: any other exception | nothing | none: the last good receipts stay, stale, and one error entry names the type |
  | 7 | A usable, complete answer | the memory is cleared | from the answer |

  Why 300 seconds: a method that fails every time costs at most 288 requests a
  day, and a method that failed once is used again within five minutes. The
  check comes at the start of a cycle, so it is asked at the first cycle after
  the 300 seconds. When no time is left to start `fleet_snapshot`, neither it nor
  `task_list` is asked and nothing is remembered. A cancellation by a signal is
  no error; it ends the cycle.
- The writer remembers one value for the current instance, not a list that
  grows. A new instance id (the service was started again) forgets all of it and
  is asked at once. A status answer without an instance id counts as one
  unchanged instance.
- **Counts from the snapshot.** When the answer of `fleet_snapshot` has
  `counts`, an object in the shape of `status.tasks`, `receipts.counts` comes
  from it, so that the list and the counts are from one read. Without `counts`
  the counts of `status` are used. **`counts: null` means "not read"**: the
  method gives it when its count query failed and its lists were read. The
  counts of `status` are then used, and nothing is remembered. Any other value
  that is not a usable object is case 4.
- **What the writer reads of the answer:** `tasks` (each row is mapped as a row
  of `task_list` is, and it carries `title`, `reason` and `cause`), `counts`,
  and whether `recent` and `needs_person` are `null`. It does not read `health`,
  `read_at`, `needs_person_total`, `needs_person_omitted`, `errors` or the rows
  of the two part lists.
- Otherwise the writer uses `task_list` with a limit of 100 and no query. So
  `needs_person` covers the newest 100 receipts only; `needs_person_complete`
  says whether that was all.
- **From `task_list`, `title`, `reason` and `cause` are always `null`**: its
  rows carry no title, no reason and no cause. They come from the rows of
  `fleet_snapshot`. `cause` is `peer_not_running` or `recipient_not_ready` only
  when the service holds exactly that code. No code is ever read from free text.
- `recipient.provider` is the part of the target before the first `:`.
  `recipient.machine` and `recipient.terminal` come from a core peer id
  (`hc:t21`, `hc:build-a/t21`, `hc:default:t84`) only. A terminal id longer than
  64 characters gives neither. `recipient.text` is the target.

**Answers that cannot be used.** A status answer whose `tasks` is not an object,
or holds a count for a store state that is not a whole number of zero or more. A
list of receipts that is not a list, or holds an entry that is not an object or
has no usable id. Such an answer never replaces the last good read. A number
that is not finite, or too large for a floating-point number, is `null`.

**Wording.** `error` and the error entry say what happened:

- `service did not answer: ...` or `... did not answer in time` when a call was
  started and no answer came, also when the client could not be built in time;
- `no time left in the cycle` for a call that was never started because the
  time of the cycle was used up, also when it is the `status` call;
- `service answered with an error: ...` for an error answer;
- `service answer cannot be used: ...` for an answer that came and cannot be
  used;
- `service answer cannot be used: unexpected <type>` for an unexpected error in
  the service step, also inside the `fleet_snapshot` call or the build of the
  client;
- `... failed: unexpected <type>` and `unexpected error in the probe of <machine>:
  <type>` for an unexpected error in a listing or a probe;
- `<path> has a second name (a hard link); the writer refuses it` for a refused
  start.

When the service does not answer, or an answer cannot be used, the receipts keep
their last good values with `stale: true`, `error` set, `read_at` unchanged and
`needs_person_complete: false`. Before any good read: `counts: null`, empty
lists, `needs_person_complete: false`, `needs_person_omitted: null`. So a failure
never says in the document that no receipt needs a person. The capacity section
is built from the probes and the listings alone, and is published whatever the
service answers.

## Bounds and the check before publication

- Every text from `hc`, a probe or the service loses its control characters and
  is cut: a name to 64, a title to 120, any other text to 200 characters.
- A machine name from `hc` that breaks the naming rule is refused, and one error
  entry says so.
- Lists: 16 machines, 200 receipts in `needs_person`, 8 in `recent`, 32 clients,
  20 errors.
- The document is at most 256 KiB. When it is larger, `recent` is dropped, then
  the oldest entries of `needs_person`. What was dropped is counted in
  `needs_person_omitted`. Nothing is cut silently.
- Before the rename the document is checked: it parses, `schema` is right,
  `written_at` is a finite number, `machines` is a list whose first entry is
  the coordinator, and every state word is an allowed word.
- **What the check covers.** Every field that this file declares for the top
  level, `writer`, `health` and its `service`, `capacity`, each entry of
  `machines` with its `link` and `reading` (including `agents`, `memory`,
  `loads`, `cores`, `suites`, `harness_processes`), `receipts` and each receipt
  in its two lists: the field is there and has its declared type, or is `null`
  where that is allowed; a word field holds one of its allowed words. The code
  keeps these fields as one table. The check is not a second builder: it checks
  type and allowed words, not meaning. It never raises.

## What it cannot do yet

- The advice is advice. It reserves nothing and does not prevent two launchers
  from choosing the same machine.
- With a service that does not offer `fleet_snapshot`, `needs_person` holds only
  what the newest 100 receipts contain, and `title`, `reason` and `cause` are
  `null`.
- `unreachable` means that `status` gave no usable answer within 2 seconds. A
  service that runs and is slow to answer (its store is locked, for example)
  reads as `unreachable` for that cycle, and the receipts are not asked in that
  cycle.
- With `counts: null` in an answer of `fleet_snapshot`, the counts are those of
  `status`, which is another read, a moment before the lists.
  `needs_person_complete` then compares a count and a list of two reads.
- A lane started without the lane label is not counted as an agent.
- The swap stop on Linux (95%) is provisional. On macOS only pressure level 4
  stops probes.
- `link.since_ms` and `link.why` are always `null`: `hc ls --json` does not give
  them.
- Every reading of a cycle carries the cycle's start time, a few seconds before
  the probe ran.
- A slow disk delays the next cycle and the handling of a signal by the time of
  one write, because publication is synchronous.
- The writer does not watch its own memory. Its resident size was measured once
  on a Linux coordinator with no memory pressure, not near swap exhaustion.
- Ending a local `hc exec` child is proved; that the probe on the worker ends
  with it rests on `hc exec --timeout`.
- An endpoint security agent can slow every process start, and so every probe.
  The time to create a child is outside the cycle's budget.
- A child whose end the system does not confirm within 2 seconds may remain.
  One error entry says so.
- On macOS, a folder that was opened in the Finder can hold `.DS_Store`. The
  writer refuses such a folder like any other that holds a file it does not
  own; the message names the file.
- Only the last part of a folder's path is guarded against a symbolic link. A
  link higher in the path is followed, as for any path.
- The writer cannot keep another process of the same user from putting a file
  into its folders after the start.
- The writer runs on macOS and Linux. On a system without the file calls it
  needs, the command ends at its start with an error of Python.
- The sync of the folder after the rename is best effort and an error of it is
  ignored, so a published file is not promised to survive a loss of power.
- The writer removes whatever stands under its staged name, except a folder.
- The writer never removes its lock file or its folders, so a start that fails
  can leave empty folders and an empty lock file, and the next start uses them.
- Parent folders that were missing are made and stay.
- Removing the lock file by hand while a writer runs lets a second writer start
  beside it.
- Counts have no upper bound in this version; a reader written in JavaScript
  cannot hold a count above 2^53 exactly.

## See also

- [CAPACITY.md](CAPACITY.md): the probe, its signals and the thresholds this document publishes.
- [FLEET-SNAPSHOT.md](FLEET-SNAPSHOT.md): the service method the writer reads receipts from.
- [HERDR-APP-MOD.md](HERDR-APP-MOD.md): installing the Herdr App column that reads `fleet.json`.
- [EVENTS.md](EVENTS.md#answerback-for-macos): Answerback for macOS, a native window that also reads `fleet.json`.
