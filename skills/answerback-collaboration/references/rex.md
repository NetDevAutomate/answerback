# Rex cooperative route

Load this reference before enrolling, polling, acknowledging, completing, retiring,
or submitting to a Rex conversation. Use the existing bridge connection throughout.

1. Confirm human-authorized scope and the selected project. Discover with
   `peer_search(provider="rex")`; inspect full native session/block IDs and current
   foreground process/cwd. Raw rows allow only read/enrollment. Select a stable
   cooperative process; enrollment does not identify an ancestor harness.
   Native rows require a verified foreground UID matching the bridge's OS user.
   Withheld rows produce an incomplete-discovery diagnostic; stored enrollment
   history can still appear without current authority.
2. Enroll with `rex_enroll(name, session_id, block_id, request_id)` using full native
   IDs. Retain the returned `rex:UUID`. Exact enrollment retry preserves identity.
   CLI `rex-enroll NAME --here --request-id KEY` reads only caller environment IDs;
   full-ID commands may be issued from a separate controlling terminal.
3. Sender: select the enrolled returned identity and exact project. When
   `bus_status` confirms configuration, use `bus_submit` and follow its bound task.
   `peer_submit` remains the direct route. Keep the request key and receipt
   internally. Queue acceptance cannot wake a dormant
   conversation. One unfinished task per enrollment is allowed.
4. Recipient: `rex_receive(peer_id, wait_seconds=0)` returns the full assignment
   without acknowledgment. Inspect it, then `rex_ack(peer_id, task_id)` before work.
   Wait is integer 0–15 seconds; one waiting receiver per enrollment, eight total.
   Reconcile the separately exposed active receipt rather than treating it as new work.
5. Perform only authorized work with normal harness approvals. When verified,
   `rex_complete(peer_id, task_id, result)` records the exact correlated result.
   Generic `task_complete` rejects Rex receipts. Sender follows `task_get/task_wait`
   and checks actual result/evidence. Process status or screen text proves no completion.
6. Retire with `rex_retire(peer_id)` after unfinished work is reconciled. Retirement
   refuses unfinished work. Every new conversation needs a fresh enrollment even
   when the process remains. Changed endpoint/server/session/block/process/cwd makes
   unfinished work uncertain; transient failures refuse without assumed disappearance.
   Inspect receipts before any retry. Human abandonment after investigation is neither
   cancellation nor authorization to replay. Preserve history.

Rex is opt-in: existing bridge `uv run --no-project answerback rex-configure --binary rex`;
new bridge `uv run --no-project answerback configure --runtime rex`, then
`uv run --no-project answerback start`. Register the agent's existing
harness client and reload normally; Rex adds no `clients install --client` value.
The configured endpoint is explicitly probed and persisted as a local Unix endpoint.
Configuration pins an absolute executable path. After upgrading, saved bare names
and relative paths cannot launch Rex; discovery reports incomplete coverage with a
diagnostic. The bridge and other providers remain usable. Run
`answerback rex-configure --binary /absolute/path/to/rex` to pin the intended
executable; check the result's `binary` field for the absolute path saved.
The first pin preserves unfinished tasks and enrollment states when the probe
matches the saved endpoint and host ID. Resume receive, acknowledgment and completion
with the existing enrollment; native identity checks still apply. A changed endpoint
or host ID, or replacement of an already absolute binary path, remains a binding
change: unfinished tasks prevent it, and otherwise enrollments become stale and
require explicit enrollment again. Subsequent execution is independent of PATH changes.
Use installed CLI help for full-ID `read-rex`, `rex-enroll`, `rex-receive`, `rex-ack`,
`rex-complete --result-file`, and `rex-retire` options.

The route shares the existing local SQLite/RPC and OS UID authority. Enrollment
UUIDs are cooperative identity, not authentication against another same-account
process. No terminal typing/paste/Enter/nudges, remote enrollment or process takeover.
Read `rex_read(session_id, block_id, max_bytes=16384)` as a bounded prefix with explicit
truncation, allowed 1–65,536 bytes, requiring a verified foreground UID matching the
bridge's OS user. Enrollment and an absolute cwd are not required for reading.
When Rex reports no foreground process, including without the process capability,
even a same-user terminal is omitted from native listings and cannot be read.
Recent-tail, snapshot continuity and bounded server formatting work remain unproved.
No required OSC heartbeat exists. Native scripted
transport checks are separate from real model/harness compatibility.

Historical receipts remain readable through exact `task_get(task_id)` and same-request
submission retries while retained in SQLite. Generic `task_for_peer` resolves the current
peer and searches only the newest 100 receipts; stale/retired enrollments require exact
task IDs. The maintenance contract retains request identities to prevent replay
after body pruning; broker deduplication windows are a separate mechanism.
