# Read fleet health and receipts in one call

The read-only service method `fleet_snapshot` returns the bridge's health and bounded receipt summaries in
one reply: counts per state, the receipts that wait for a person, and the newest receipts. The fleet
writer (`answerback fleet serve`, [FLEET.md](FLEET.md)) calls it on the private service socket.

This page is for someone writing a program that publishes a fleet document or a similar summary. Pass `{}`
as the parameters. There is no CLI command or MCP tool for it. The socket protocol stays at version 1, and
the `status` and `task_list` replies are unchanged.

Real reply from a bridge with an empty receipt store, trimmed to five of its fields:

```json
{"schema": "answerback.fleet_snapshot/1",
 "counts": {"pending": 0, "queued": 0, "submitted": 0, "running": 0, "blocked": 0, "uncertain": 0,
            "completed": 0, "failed": 0, "abandoned": 0, "cancelled": 0, "total": 0},
 "needs_person_total": 0, "needs_person_omitted": 0, "errors": []}
```

`errors` is empty, so every section was read, and `needs_person_total` equals
`counts["uncertain"] + counts["blocked"]`.

## What the caller may rely on

The three receipt sections come from one read-only connection and one read
transaction: `counts`, `needs_person` with its total, and `recent`. When all
three were read, this equality always holds:

```text
needs_person_total == counts["uncertain"] + counts["blocked"]
```

`counts` has the shape of the `tasks` object returned by `status`, plus `total`.
It fills every listed receipt state, including those with zero receipts.
`total` counts every receipt, including states this version does not list,
so it can exceed the sum of the listed states. The waiting
list searches the whole store. Old waiting receipts do not disappear behind
100 newer receipts. Each query can fail separately; that section becomes unknown.

A busy or locked receipt store gives the ordinary timeout answer. The caller
should ask again. Any other failure of both receipt lists gives the ordinary
service error. A partial answer never claims a failed section is empty.

## Result fields

| Field | Meaning |
| --- | --- |
| `schema` | Always `answerback.fleet_snapshot/1`. |
| `read_at` | Seconds since the epoch when the service began the read. |
| `health` | The five service fields listed below. These need no store read. |
| `counts` | Every state count and `total`, or `null` if its query failed. |
| `needs_person` | Up to 200 `uncertain` or `blocked` rows, or `null` on failure. |
| `needs_person_total` | The full count of waiting receipts, or `null` on failure. |
| `needs_person_omitted` | The waiting total minus rows returned, or `null` on failure. |
| `recent` | The newest 20 receipts by `created`, or `null` on failure. |
| `tasks` | `recent` followed by waiting rows not already in it; each ID occurs once. |
| `errors` | Section failures; empty when every requested section was read. |

The count states are `pending`, `queued`, `submitted`, `running`, `blocked`,
`uncertain`, `completed`, `failed`, `abandoned` and `cancelled`.
The waiting rows are newest first by `updated`. Recent rows use the same order
as `task_list`. The combined `tasks` list has at most 220 rows.

| Health field | Meaning |
| --- | --- |
| `running` | Always `true` in a successful reply. |
| `accepting` | Whether the service accepts work; `false` while draining. |
| `instance_id` | The current service instance ID. |
| `started` | Service start time, in seconds since the epoch. |
| `version` | Running package version. |

The snapshot remains readable while the service drains.

## Receipt rows

Every row has exactly these nine fields:

| Field | Meaning |
| --- | --- |
| `id` | Stored receipt ID, cut to 64 characters. |
| `request_key` | Stored request key, cut to 160 characters. |
| `target` | Stored recipient address, cut to 160 characters. |
| `status` | Stored receipt state. |
| `created` | Stored numeric creation time, or `null` if invalid. |
| `updated` | Stored numeric update time, or `null` if invalid. |
| `title` | Title from the sender's request key, or `null`; at most 80 characters. |
| `reason` | Fixed code for a receipt that ended or waits; otherwise `null`. |
| `cause` | `recipient_not_ready` for that reason; otherwise `null`. |

A row never says which Core read a terminal's identity: that is only known from
the receipt's delivery metadata, which the snapshot does not read. The fleet page
shows `identity read by` only for a `task_list` receipt whose delivery recorded
`identity_guard: observed_before_and_after`.

Times are seconds since the epoch. Only finite integers and floats are retained.
Booleans, text, non-finite numbers and integers too large for a finite float
become `null`. No text from a time column enters the reply.

The method reads what the bridge's own functions wrote; if another program
stores BLOB values in text columns, the caller gets the ordinary error answer.

### Titles

Row shaping reads each receipt text field (`id`, `request_key`, `target`, and
`status`) only up to its first 512 characters, before cleaning or deriving a
title. Direct title calls also limit their source key and target to 512
characters. The store admits request keys of at most 128 characters.

A title removes one leading `<word>-YYYYMMDD-` prefix. A word contains ASCII
letters, digits or underscores. If that prefix is absent, it removes one
`-YYYYMMDD` token instead. The year must be 2000–2099, the month 01–12 and the
day 01–31. These are field bounds; there is no month-specific calendar check.
An eight-digit number outside those ranges is kept.

A trailing terminal name is removed only when it matches the receipt's own
Core target. Empty titles and UUIDs become `null`. Examples:

| Request key | Target | Title |
| --- | --- | --- |
| `ab-20260115-review-docs-t12` | `hc:t12` | `review-docs` |
| `t3a-desktop-probe-20260115-1` | `hc:t12` | `t3a-desktop-probe-1` |
| `ab-20260115-harness-check-1` | `hc:t12` | `harness-check-1` |
| `build-10000000-run` | `hc:t84` | `build-10000000-run` |
| `ab-20261340-x` | `hc:t84` | `ab-20261340-x` |

A bus receipt keeps `bus:<command id>` in `request_key`. Its title uses the
matching command's request key from the local ledger. The bus counts as
configured when `bus/config.json` exists beside the bridge configuration.
That check reads only file metadata. Title lookup reads neither the contents
of that configuration nor the token. A missing or unreadable token does not
affect it. The ledger is `bus/ledger.sqlite` in the same configuration folder.

No bus or no matching command gives a `null` title. A missing, busy or unreadable
ledger gives `null` bus titles and one `titles` error. Other titles remain
available. The lookup never constructs or migrates a bus store.

### Reasons

Failed, uncertain, blocked and cancelled receipts use fixed codes derived
from their details. Unknown details give `unclassified`. An abandoned
receipt always gives `abandoned_by_person`, without the person's words.
Pending, queued, submitted, running and completed receipts have null reason
and cause.

The [reason table](../src/answerback_bridge/receipt_snapshot.py) is the single
source of these mappings. Its guard requires at least the recorded number of
copies of each fragment in each writer; recorded zeros are not checked.
Additional copies are allowed. A beginning rule counts constants that start
with its part; other rules count exact constants.

When changing a detail's fixed text, update its table row in the same commit.
`test_reason_table_sentences_remain_constants_in_each_writer` in
`tests/test_service_fleet_snapshot.py` fails when a required fragment loses
copies. Without a matching table update, receipts using reworded details
can become `unclassified`.

Two Core rules match by their fixed beginning because added readiness or remote
restriction text does not change their reason. Every wrapped Crew send error
beginning with `Crew delivery is uncertain:` followed by a space gets
`delivery_unverified`, with no cause. These are the only three beginning rules.
Only the code leaves the process, never the following text. A beginning found
only in the middle of a detail does not match.

Other rules match exact sentences or templates with at most one bounded ASCII
identifier of up to 64 characters. Native confirmations fix the terminal
status per rule, leaving only the provider name variable. Stop reasons,
unrecognized exception text and delivery messages containing expected and
observed identities stay `unclassified`. Codes match `[a-z][a-z_]{0,63}`.

Only `recipient_not_ready` sets a cause. The reader also knows `peer_not_running`,
but no current receipt sentence proves that cause, so this method never emits it.

## Failures, time and size

An error has `section` and `error` fields. The section is `counts`, `needs_person`,
`recent` or `titles`. The error is exactly `store read failed` or
`bus ledger read failed`. Exception text is never returned.

A failed section is `null`, never an empty list or zero counts. If the waiting
query fails, both its total and omitted count are also `null`. `tasks` holds
rows from the lists that succeeded. If both lists fail, the whole RPC fails.
See the [local RPC error contract](PROTOCOLS.md#local-rpc-error-contract).

The service has two seconds from reading the request line; its own client waits
one second longer. The store work shares a 1.5-second monotonic budget, including
time waiting for its worker thread. SQLite lock waits and query execution are
bounded by the remaining time. A busy or locked receipt store gives the caller
the ordinary timeout answer, the same as for the two-second request deadline.

Both the receipt read and the ledger read take their store's shared lock once,
before the first table query, and keep it until the read ends. No query can
then wait for the lock a second time.

The ledger gets the remaining budget, at most 0.5 seconds. If less than 0.05
seconds remains, the lookup is skipped and reported as a `titles` error.
A busy ledger is a section error, not a timeout of the whole method.

Store reads, ledger reads and row shaping run in a worker thread. The method
uses no dispatch slot and writes no store or service state. Successful calls
add no request log line. Failed calls are logged normally.

All strings lose control characters. The UTF-8 reply stays under 256 KiB,
including its RPC envelope. Large replies may shorten request keys, targets
and titles further, while keeping rows and counts. Shrinking stops after eight
passes or when nothing can be shortened. If the answer still exceeds its
budget, the RPC gives its ordinary service error.

The method never reads message or result columns. No row contains message,
result, detail, metadata, turn ID or an abandonment explanation.

## See also

- [FLEET.md](FLEET.md): how the fleet writer uses this reply and falls back to `status` and `task_list`.
- [PROTOCOLS.md](PROTOCOLS.md#local-rpc-error-contract): the error answers this method shares with other RPCs.
