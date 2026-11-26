"""Bus-owned decoding and protection rules for kernel maintenance plans."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..digests import digest
from ..plugin import MaintenanceProtected
from .model import Command


class BusMaintenance:
    """Interpret the two bus rows whose payloads use the command wire schema."""

    tables = frozenset({("bus", "commands"), ("bus", "outbox")})

    def inspect(
        self,
        store: str,
        table: str,
        row: Mapping[str, Any],
        *,
        retention: Mapping[str, Any] | None = None,
        related: Mapping[str, Mapping[str, Any]] | None = None,
        planned_at: float,
    ) -> Mapping[str, Any]:
        if (store, table) not in self.tables:
            raise ValueError("Maintenance contributor does not own this table")
        if table == "commands":
            if retention is not None:
                return {}
            command = Command.from_bytes(row["payload"].encode())
            envelope = json.loads(command.to_bytes())
            envelope.pop("message")
            if digest(json.loads(row["payload"])) != row["payload_hash"]:
                raise MaintenanceProtected("conflicting_envelope")
            return {"envelope": envelope, "message": command.message}
        if row["kind"] != "command":
            return {}
        command_row = (related or {}).get("command")
        if command_row is None:
            raise MaintenanceProtected("missing_relationship")
        parsed = Command.from_bytes(row["payload"].encode())
        if (
            row["id"] != row["command_id"]
            or parsed.command_id != row["id"]
            or digest(json.loads(parsed.to_bytes())) != command_row["payload_hash"]
        ):
            raise MaintenanceProtected("conflicting_command_publication")
        return {}
