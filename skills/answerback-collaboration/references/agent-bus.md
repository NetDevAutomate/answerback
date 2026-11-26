# Durable assignments through the optional bus

Use this route when the user authorises queued delivery and `bus_status` confirms
the bus is configured. Herdr App/TUI is optional. The current bus supports one
local machine and one coordinator.

1. Discover using the existing `peer_search`; retain the exact `peer_id` and
   absolute `project`. Resolve ambiguity before sending. Provider selection,
   identity checks and harness approvals still apply.
2. Call `bus_submit` once with the bounded authorised assignment, those exact
   fields, a stable request key and a TTL. Preserve the returned command ID.
3. Read `bus_result(command_id)`. `queued` means accepted locally and `bound`
   identifies an adapter task. Neither means the work completed.
4. Follow the bound task and verify its result/artifacts. Use the existing
   task tools for harness permission/cancellation handling. Treat `uncertain`
   as requiring inspection; never automatically resubmit it.
5. Use `bus_cancel` only before admission. Use `bus_events` for lifecycle
   history, and `bus_status` for connection/backlog information.

Reusing a request key is valid only for the identical command. Broker publication
and replay never grant additional authority. A routing or project change causes
rejection; it must not silently redirect an old assignment.

The `agent-bus` CLI exposes the same operations, plus configure/start/stop.
`submit --offline` saves locally while the broker/coordinator are unavailable.
`stop` stops the owned broker only and leaves admitted tasks running.
Do not treat a successful connection or a publish acknowledgement as a completed
harness round trip.
