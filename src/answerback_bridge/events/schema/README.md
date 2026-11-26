# Vendored AG-UI 1.0 JSON Schema

| | |
|---|---|
| File | `ag-ui-1.0.schema.json`, byte-for-byte as published |
| Source | <https://docs.ag-ui.com/spec/1.0/schema.json> (`$id` `https://ag-ui.com/spec/1.0/schema.json`) |
| Fetched | 2026-11-17 |
| sha256 | `4b5c93226838a0e72d88e6c5df20633c686c49fcb75d9be2815c6cbf9e48e71a` |
| Upstream | <https://github.com/ag-ui-protocol/ag-ui> |
| Licence | MIT, copied here as `LICENSE.ag-ui` |

Answerback does not depend on the AG-UI SDK. Events are plain dictionaries built in
`answerback_bridge/events/model.py`; this file is the contract the tests validate them against
(`tests/test_events_schema.py`). Nothing at runtime loads it.

To update: fetch the new file, replace this one without reformatting, update the hash above and run the
schema tests. A new protocol version gets a new file name; do not overwrite 1.0.
