# See where the load is before you place work

`answerback capacity` measures load and memory on this machine and on the other machines you name, and
says for each one whether it is `ok`, `busy`, `full` or `unknown`. `answerback rebalance` reads the same
measurements and previews at most one move of a worker to a machine with room, or says why nothing should
move. Neither command changes anything.

It is for someone who hands work to agents on more than one machine, or runs heavy work beside them. Other
machines are reached through Herdr Core (`hc exec --on`), so they must be machines Core knows
([HERDR-INTEGRATION.md](HERDR-INTEGRATION.md)).

## Measure this machine

```sh
answerback capacity
```

Real output from a Linux machine, with the largest process's name replaced:

```text
local ok load/core=0.324 loads=4.58/5.19/3.72 cores=16; available 34.82% of physical memory; Memory stall some avg10 0.00%; largest_process 5.58% of physical memory; largest=<name> (3579128 kB); processes=674
```

The machine is `ok`: load per core is under 0.70 and more than 15% of memory is available. Local
measurement needs neither configuration nor a running bridge. To make a script stop on a busy machine:

```sh
answerback capacity --machine build-a --machine build-b --advise --require ok --timeout 10
```

That command exits 0 only when `local`, `build-a` and `build-b` are all `ok`, and 1 when one of them is not.

## What is measured

The defaults are in `placement/assess.py` (`Thresholds`).

| Signal | Measurement | ok | busy | full |
| --- | --- | --- | --- | --- |
| CPU | max(load1, load5) / logical cores | < 0.70 | ≥ 0.70 and < 1.00 | ≥ 1.00 |
| macOS pressure | kernel pressure level | 1 | 2; unrecognised values | 4 |
| macOS compressor | compressor kB / physical memory kB | < 25% | ≥ 25% and < 40% | ≥ 40% |
| macOS swap | used swap kB / physical memory kB | < 25% | ≥ 25% | never alone |
| Linux available | available kB / physical memory kB | ≥ 15% | ≥ 7% and < 15% | < 7% |
| Linux stall | memory PSI `some avg10` | < 10 | ≥ 10 | never alone |
| Largest process | resident kB / physical memory kB | < 25% | ≥ 25% and < 50% | ≥ 50% |
| Agents | supplied active count against supplied ceiling | below ceiling | (none) | at or above ceiling |

The worst signal wins. The state is `unknown` after an error, when the cores or any of the three loads are
missing, when the reading is older than 60 seconds, or when no free-memory signal could be computed. The OS
must be `Darwin` or `Linux`. macOS needs a pressure level or a compressor share, and Linux needs a
`MemAvailable` share. Swap, stall and the largest process can raise the state, but none of them counts as a
measurement of free memory.

The 15-minute load is shown but not assessed. On macOS swap is compared with physical memory, because the
swap total grows with use; sub-kB swap values are rounded down. The probe also records the process count,
the count of recognised agent programs (`claude`, `codex`, `kiro-cli`, `grok`) and running pytest suites.
Agent programs are not a count of active turns.

## The command

- The command always measures `local` first, as the coordinator. Each `--machine` is measured at the same
  time through `hc exec --on`, and duplicate names collapse. At most 16 machines, `local` included.
- `--timeout` takes 1 to 30 seconds, default 10. The remote Core timeout is rounded up to whole seconds,
  and child cleanup can take two more seconds.
- For other machines, `--config PATH` supplies `hc_binary` and `hc_session`. Without a configuration file
  the command uses `hc` on `PATH` and the default Core session.
- Text output gives each machine's state, load per core, three loads, core count, memory signals, largest
  process and process count, and the reasons for a machine that is not `ok`. `--advise` adds advice and the
  machines ranked for heavy work.
- `--json` always includes the advice. Its top-level fields are `taken_at`, `coordinator`, `machines`,
  `ranked_for_heavy_work` and `advice`. Each machine has `machine`, `state`, `load_per_core`, `signals`,
  `reasons` and `reading`, and `reading` holds every probe field (absent ones are `null`). Each machine that
  is not `ok` has an advice record with `machine`, `level`, `text` and `targets`.

| Exit code | Meaning |
| --- | --- |
| 0 | measured; without `--require`, also when a state is `unknown` |
| 1 | `--require ok` and a named machine is not `ok`, or `--require busy` and one is `full` or `unknown` |
| 2 | an invalid name, too many machines or an invalid option |

When no machine is named, `--require` applies to `local`.

## Rules for a session that hands off work

1. Measure before each launch or hand-off. A reading expires after 60 seconds.
2. Keep heavy work off the coordinator while another machine is `ok`. The heavy-work ranking never lists the
   coordinator.
3. A `busy` machine gets nothing heavy. A `full` or `unknown` machine gets nothing new.
4. Ok machines are ranked by load per core in bands of 0.25, then by the supplied machine order, then by
   name. The command supplies no order.
5. When no machine is eligible, queue the work and measure again later. Run one full test suite per machine
   at a time.

| Level | Trigger | Advice |
| --- | --- | --- |
| 0 | unknown | Place nothing until it can be measured. |
| 1 | busy | Place nothing heavy; running turns finish. |
| 2 | full, or busy in two consecutive supplied readings | Place nothing new; move each lane's next stage at its boundary. |
| 3 | coordinator full | Each working agent commits or writes a hand-over note and reports; a new worker continues on a target. |

Never stop a working agent and send its assignment again. Keep the branch tip, brief, hand-over note and
receipt state across a stage boundary. The command keeps no earlier reading, so the "busy twice" advice
needs a library caller that passes the previous assessment.

Library callers can pass their own frozen `Thresholds` to `assess`, an active-agent count and ceiling, and
previous assessments to `advise`. `ReadingCache` is an optional in-process cache with a 60-second maximum
age; the command always measures afresh.

## Preview a move with `answerback rebalance`

```sh
answerback rebalance --machine mac-a
```

The command measures the machines with the same probe and prints the one move the planner would name, or
`NOTHING_MOVES: <cause>`. It starts, stops, interrupts and sends to nothing, and asks the bridge and Core
only for the probe. There is no option that applies a move.

The only file it writes is `placement-readings.json` beside the bridge configuration (mode 0600, written
whole and moved into place), which keeps the last two readings of each machine. A plan needs two readings
of a machine 60 to 600 seconds apart. On the first run it says `NOTHING_MOVES: one reading only.` with the
reason per machine; run it again a minute later.

Output recorded on 22 November 2026 at 13:04, when the coordinator was `full` with 11.31 load per core and
another machine was nearly idle:

```text
local  full     load/core 11.31 (was full)  memory ok  workers 0  harness processes 5  not a taker: the coordinator takes no work
mac-a  ok       load/core 0.18 (was ok)  memory ok  workers 0/10  harness processes 5  taker

NOTHING_MOVES: no worker load on local.
  local is full, but holds no managed worker.
  Its load comes from processes this command may not move.
  New work would fit on mac-a.
plan: none        changed: nothing (readings kept in ~/.config/answerback-bridge/placement-readings.json)
```

Nothing moved because no managed worker ran on the coordinator; its load came from its own services and
from sessions a person had opened. `--json` prints the same as one object with `taken_at`, `coordinator`,
`machines`, `plan`, `changed` and `readings_file`. Exit code 0 means done or nothing to do, 2 a wrong
argument, and 1 any other error, such as a readings file it cannot read (it leaves the file as it is).

### Where its facts come from

- Machine facts: the `placement` block of `status/routing.json` in the folder you run the command in.
- Machines it may send to: `hc_send_machines` in the bridge configuration.
- Managed workers: the worker registry, read and never written. A row records its machine; an older row
  reads as `local`.

The planner is `plan_rebalance` in `placement/rebalance.py`. It is pure: the time, readings, facts, workers
and move log are its arguments. The classes are those of `assess`, and the target is the first machine
`choose` ranks among the takers. Its constants:

| Constant | Value | Meaning |
| --- | --- | --- |
| busy and full lines | 0.70 and 1.00 load per core | from `assess`, not repeated in the planner |
| `TAKER_CEILING` | 0.40 | a taker's load per core with one more worker must stay under it |
| `MIN_GAP` | 0.30 | the giver's load per core must exceed the taker's projected load by this |
| `MAX_AGE` | 60 s | a machine's newest reading counts only when at most this old |
| `MIN_SPACING`, `MAX_SPACING` | 60 s, 600 s | the allowed gap between a machine's two readings |
| `MACHINE_COOLDOWN` | 5 min | a machine that gave work takes none for this long |
| `WORKER_COOLDOWN` | 30 min | a worker that moved, and its successor, stay for this long |
| `MAX_MOVES` | 1 | one move in the whole fleet at a time |

A taker's projected load is its load per core plus `worker_cost` (from the facts, default 1.0) times the
placements of the last 120 seconds plus one, divided by its cores.

A machine gives when it is `full`, or `busy` on both readings, and holds a managed worker that may move. A
machine takes when it is `ok` on both readings, is not the coordinator, is listed in `hc_send_machines`, has
facts with a project folder and a harness marked ready, and gave no work in the last five minutes. Good
memory does not turn `full` into `ok`. A worker stays when its lane must stay on the coordinator, its class
runs the test gate, its receipt is `uncertain`, `blocked` or unknown, a person opened its terminal, or its
work is finished.

## Handling probe output

The probe script is fixed and nothing is inserted into it. Other machines are reached only through Core's
existing transport with an argument list, and machine names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`.
The Core environment helper removes inherited terminal, socket and session identity and restores only the
configured session.

Another machine's output is untrusted:

- At most 64 KiB and 200 lines are parsed, values over 400 characters are ignored, and numbers must be
  strict bounded decimals. A login banner counts toward the line bound, and a banner that pushes the end
  marker past it makes the reading `unparsed`.
- The answer starts at the first `os=` line and must end with `probe_end=1`. Empty LF or CRLF lines after
  the marker are ignored but still count toward the 200-line bound; white space on the marker or any other
  text after it is refused.
- Only fields the probe prints count toward repetition. Other text before the answer, including repeated
  unrelated keys and banner rulers, is ignored.
- The reading is `unparsed` and its state `unknown` when a probe field repeats, appears before `os=`, or
  `os=` or the end marker is missing or misplaced; when the memory total is absent or not positive, or
  available memory exceeds it; or when a memory key of the other platform appears (Darwin uses
  `mem_total_kb`, Linux `memtotal_kb`).
- The largest process name must be printable ASCII of at most 64 characters without `=`, after removing one
  trailing CR; otherwise it becomes `?`. JSON strings are escaped by `json.dumps`.
- A child that reaches the output limit is stopped and reported unreachable, and a child that times out is
  killed and reaped. The probe prints no command line and no environment.

## What it cannot do yet

- The result is advice. `answerback route` and the worker pool do not read the measurement, and the bridge
  never moves work.
- `answerback rebalance` is a preview only. There is no `--apply`, no `answerback move` and no move store,
  because handing a worker over to another machine is not built. A move would be a hand-over, not a
  transport: a new worker continues from a written brief, and the conversation is not carried.
- The rebalance preview does not ask the bridge for receipt states, so it treats every managed worker as
  protected (`its receipt is unknown`) and names no worker for a move yet.
- The assignment store (`assignments.py`) is not wired to any route. One `send` addresses one peer.
- The agents signal needs an active count that the command does not collect.
- The thresholds are initial defaults and may change.

## See also

- [FLEET.md](FLEET.md): the same measurements published every 15 seconds for the Herdr App.
- [TRACING.md](TRACING.md): load per worker instead of per machine.
- [ROUTING.md](ROUTING.md): the policy file whose `placement` block `rebalance` reads.
- [HERDR-INTEGRATION.md](HERDR-INTEGRATION.md): how other machines are reached through Core.
