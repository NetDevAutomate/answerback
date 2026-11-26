# Prune and compact the receipt stores

Shrink a bridge's SQLite stores without losing the identities that stop a repeat from
running twice. `maintenance stats` shows what has accumulated, `plan` writes a reviewable
plan, `apply` carries it out, and `compact` reclaims the space. Every step is explicit;
nothing prunes on its own.

This page is for the operator of an installed bridge ([OPERATIONS.md](OPERATIONS.md)). The
same stores answer, with three SQL queries, how many hand-offs one bridge version completed.

## The shortest path

```sh
answerback maintenance stats
answerback maintenance plan --older-than-days 30 --output /private/plan.json
answerback maintenance apply --plan /private/plan.json
```

Real output of `plan` and `apply` on a bridge with nothing old enough to prune:

```json
{
  "status": "planned",
  "output": "/private/plan.json",
  "digest": "<sha256-digest>",
  "actions": 0,
  "logical_bytes": 0,
  "protected": {
    "recent_or_invalid_time": 1
  },
  "truncated": false,
  "continuation": null
}
{
  "status": "applied",
  "actions": 0,
  "logical_bytes_pruned": 0,
  "digest": "<sha256-digest>"
}
```

`status: "applied"` with the same `digest` as the plan (a 64-character SHA-256 hex string)
proves that this plan, and no other, ran; `actions` is how many rows it changed and `protected` counts, by reason, the reference
groups the plan left alone (here one group newer than the cutoff).

`stats` prints one object per store (`tasks`, `bus`, `clients`, `pressure`) with its file
size, page count, `freelist_count` and row counts per table.

`stats` and `plan` read the existing files only: they construct no store, create no
database, migrate no schema, contact no provider and prune nothing. A plan holds row IDs,
preimage hashes, reference groups, limits, a fixed cutoff and a digest; it holds no prompt
or result body. Keep plan files private. A plan that is stale, conflicting or partly
applied is rejected as a whole, and lock contention leaves it unapplied.

## What a plan prunes

A plan drops the result and prompt bodies of receipts whose whole reference group is older
than the cutoff and in a final state (`completed`, `failed`, `cancelled`, `abandoned`).
Task, command and request IDs and the UTF-8 content digests stay, so a repeat of the same
request is still recognised.

Protected, and never in a plan: unfinished and `uncertain` work, reconcilable Desktop
failures, invalidated or provisional evidence, unacknowledged client messages, pending
broker publications, unprojected task results, unresolved references and unknown schemas.
The Crew creation-receipt table (`crew_creation_receipts`) is never pruned: its rows are the
identity a creation retry is checked against. Provider metadata, enrollment bindings and
worker sessions stay in full. Identity rows and metadata keep growing; this is not a
promise of a constant-size database.

Before selecting task references, maintenance requires a supported bus schema and reads
the complete event and publication inventory once per snapshot, capped at 2,000 rows and
16 MiB; exceeding either cap protects the affected task groups with `component_limit`.
Missing graph tables, unexpected columns or an unsupported schema version protect the
dependent task roots, because this version cannot prove that unfamiliar fields hold no
reference.

### Filters and limits

| Flag | Default | Meaning |
| --- | --- | --- |
| `--older-than-days N` | 30 | The fixed cutoff |
| `--provider NAME` | all | Exact catalog name or alias (`codex` selects stored `desktop` receipts) |
| `--project PATH` | all | Exact persisted project path; historical paths are not resolved through current symlinks, and receipts with unknown project ownership are excluded |
| `--max-actions N` | 500 | Actions per plan, including linked event and publication rows; at most 1,000 |
| `--max-roots N` | 500 | Roots inspected per plan; at most 1,000 |
| `--cursor PATH` | none | Continue a truncated plan from the previous private plan, with the same filters and limits |

Notifications have no provider or project ownership and are excluded when either filter is
set. A component that exceeds the action limit stays protected; raise the limit rather than
splitting its reference group.

## After pruning

A completed receipt whose result was pruned returns `result: null`, `content_pruned: true`,
`has_result: true` and `result_available: false`. A submission or completion retry that
compares pruned content is refused with
`Pruned task content cannot be verified; inspect the existing receipt`, whether the content
matches or not; this includes a worker repeating a completion on a finished, pruned task.
Read the existing receipt instead of sending the work again under a new request ID. A queued
bus command that reaches lookup after its task is terminal and pruned finishes as
`uncertain` without being sent again.

An upgraded, unpruned database keeps the row shapes the previous package writes. After
pruning, an older package can show a null historical result and reject a matching retry,
because it cannot read retained-content identities; the permanent request rows still
prevent a second execution. An older coordinator may also count the retained command
identities against its old admission limit, so a pruned bus can refuse admission under it.
Keep the current version to read pruning markers and to project pruned history.

Bus event history is deleted with its publication receipt and requires the
`retention_history` table with exactly one usable row; missing or invalid metadata protects
the store. Event cursors stay monotonic and can contain retention gaps: a client should
surface the returned gap and floor rather than claim a shortened history is complete.
SQLite event retention does not alter JetStream retention.

## Compact a stopped store

```sh
answerback maintenance compact --store tasks
```

`--store` takes `tasks`, `bus` or `clients`. Stop the bridge first: compaction holds the
service's owner lock for the operation, and without an existing owned `runtime/owner.lock`
it reports that ownership cannot be established. It never creates a substitute runtime
directory or trusts a stale socket as proof that the service is stopped. `VACUUM` reports
the before and after file and page counts; a reduction is not guaranteed and temporary disk
space is needed. Compaction is not secure erasure, WAL checkpointing or broker cleanup.

## What it cannot do yet

- Only rollback-journal databases are supported. A WAL database is refused before it is
  opened, because SQLite's `mode=ro` can create WAL and SHM files, and multi-database apply
  needs the super-journal atomicity that WAL does not give. Switch the mode yourself first.
- Apply and compact have been accepted on disposable databases only; live verification is
  limited to `stats` and `plan`.
- There is no automatic history pruning. The bus ledger refuses new commands at its
  10,000-command cap until a plan has pruned some ([AGENT-BUS.md](AGENT-BUS.md#bounds)).

## Counting the hand-offs of one bridge version

A receipt stores the version of the bridge that created it and the version of the bridge that ended it, as `bridge_version_created` and `bridge_version_ended` in its metadata. A hand-off of a version is a receipt in an end state that this version both created and ended. The store writes the two fields itself and refuses a metadata write that names either of them; a caller reads them as `bridge_versions` in the receipt.

A count has these steps, in this order.

1. Check first that every row holds valid JSON. This must give 0; do not count before it does. A malformed row in an end state makes the count query fail with `malformed JSON`, and a malformed row in another state means the store needs a look before any number from it is used.

   ```sql
   SELECT count(*) FROM tasks WHERE NOT json_valid(metadata);
   ```

2. Find the store. It has no fixed path: it is the file `tasks.sqlite` in the directory of the configuration file the bridge was started with, the file given with `--config`. When that file is a link, the store is beside the file the link leads to. Without that option, `answerback --help` prints the path it resolved as the option's default.

3. Find the version to put in: `answerback --version`, or `version` in the service `status`.

4. Count, with that version in place of `0.15.0+internal` in both lines:

   ```sql
   SELECT count(*)
   FROM tasks
   WHERE status IN ('completed', 'failed', 'abandoned', 'cancelled')
     AND json_type(metadata, '$.bridge_version_created') = 'text'
     AND json_type(metadata, '$.bridge_version_ended') = 'text'
     AND json_extract(metadata, '$.bridge_version_created') = '0.15.0+internal'
     AND json_extract(metadata, '$.bridge_version_ended') = '0.15.0+internal';
   ```

The count has two limits.

The first: a bridge from before this change records no versions. When it ends a receipt again, the earlier ending stamp stays in place. So the count is exact only if no such bridge has written to this store since the first stamped receipt.

The second: the stamp never stops an ending. When the stamp cannot be prepared, the ending goes as it did before this change and the metadata is left as it is. That is the case when the stored metadata cannot be read, is not a JSON object, or holds a value that cannot be encoded again (a number outside the range of a double, such as `1e400`). A receipt whose metadata can be read, is a JSON object and holds no ending key reads "not recorded" as its ending version and is not counted. Metadata that cannot be read gives no version: an `update` into an end state commits the ending and then raises when it reads the receipt back, while `complete_worker` and a Rex completion refuse it before a stamp is prepared and the receipt stays open. With metadata that is not a JSON object, an `update` ends the receipt and returns it, but the public view of it fails before the version is read; the other two endings refuse it as well. One case reads otherwise: a receipt that had been ended before, was opened again and is then ended without a stamp keeps its earlier ending version, shows it, and is counted under it.

Two queries help with the first limit. Neither says who wrote a receipt.

```sql
SELECT min(created) FROM tasks WHERE json_type(metadata, '$.bridge_version_created') = 'text';
SELECT count(*), max(created) FROM tasks WHERE json_type(metadata, '$.bridge_version_created') IS NOT 'text';
```

The first gives the creation time of the oldest stamped receipt, in seconds since 1970. The second counts the receipts whose creation stamp is missing or is not text, in any state, and gives the creation time of the newest of them. Every receipt from before the first bridge that records versions is among them for good, so the count alone says nothing. Read the time: a newest unstamped receipt that is younger than the oldest stamped one was created without a stamp after a bridge that records versions had begun to write, which points to a bridge that records no versions at work on the same store. An older time, or no unstamped receipt at all, proves nothing: a bridge that records no versions may have ended receipts without creating any. Beyond that, a person can check which bridge versions ran against the store: the version of every installed bridge that uses this configuration (`answerback --version`; `version` and `runtime.package_path` in `status`).

A pruned receipt still counts. Pruning keeps the row, its status and its metadata.

## See also

- [OPERATIONS.md](OPERATIONS.md): stopping the bridge before a compaction, and where the stores are.
- [RECEIPTS.md](RECEIPTS.md): the receipt states and the version stamps a count relies on.
- [AGENT-BUS.md](AGENT-BUS.md#bounds): the bus ledger's caps and quarantine.
- [ANSWERBACK-UPGRADE.md](ANSWERBACK-UPGRADE.md#rollback): what pruning means for a rollback.
