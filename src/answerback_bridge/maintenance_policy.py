"""Deterministic eligibility and exact reference closure; no writes or provider calls."""

import json
import logging
import math
import sqlite3
from typing import Any

from .adapters.catalog import definition
from .digests import canonical, digest, unique_object
from .plugin import MaintenanceContribution, MaintenanceProtected
from .retention import (
    content_identity,
    decode_retention,
    task_projection_fingerprint,
    task_retention,
)
from .tasks import TERMINAL, StoreHookRegistry

Key = tuple[str, str, str]
MAX_COMPONENT_ROWS = 2000
MAX_COMPONENT_BYTES = 16 * 1024 * 1024
_LOG = logging.getLogger(__name__)
KNOWN = {
    "tasks": {
        "tasks",
        "worker_sessions",
        "native_sessions",
        "crew_creation_receipts",
        "hc_machine_pins",
        "rex_peers",
        "retention_records",
        "retention_applications",
        "sqlite_sequence",
        # Receipt subjects (T-277): short sender text, kept with the receipt, never pruned.
        "task_subjects",
    },
    "bus": {
        "retention_history",
        "commands",
        "outbox",
        "outbox_failures",
        "outbox_holds",
        "events",
        "retention_records",
        "retention_applications",
        "sqlite_sequence",
    },
    "clients": {"messages", "retention_records", "retention_applications", "sqlite_sequence"},
    # Provider pressure (T-227): levels and a bounded signal ring, never a retention target.
    "pressure": {"pressure", "signals", "sqlite_sequence"},
}

COLUMNS = {
    "crew_creation_receipts": {"request_key", "title", "project", "identity"},
    "native_sessions": {"provider", "session_id", "alias", "cwd", "profile"},
    "hc_machine_pins": {"machine", "id", "pinned"},
    "task_subjects": {"task_id", "subject"},
    "tasks": {
        "id",
        "request_key",
        "target",
        "message",
        "status",
        "created",
        "updated",
        "metadata",
        "result",
        "detail",
        "turn_id",
    },
    "commands": {
        "id",
        "request_key",
        "payload",
        "payload_hash",
        "delivery",
        "task_id",
        "task_fingerprint",
        "target_key",
        "created",
        "updated",
        "detail",
    },
    "messages": {"id", "client", "request_id", "topic", "message", "created", "acknowledged"},
    "outbox": {"id", "kind", "command_id", "payload", "created", "published_at"},
    "outbox_failures": {"id", "attempts", "last_attempt_at", "error_type", "quarantined_at"},
    "outbox_holds": {"id", "not_before", "reason"},
    "events": {"seq", "id", "command_id", "type", "payload", "created"},
    "rex_peers": {
        "peer_id",
        "request_key",
        "name",
        "binding",
        "state",
        "created",
        "updated",
        "endpoint",
        "session_id",
        "block_id",
    },
    "retention_records": {"entity", "row_id", "changed_at", "record"},
    "retention_applications": {"digest", "applied_at"},
    "retention_history": {"singleton", "deleted_event_ceiling"},
}


class Protected(ValueError):
    """This component cannot be maintained under the current evidence."""


def old(value: object, cutoff: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 < value < cutoff
    )


def object_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value, object_pairs_hook=unique_object)
    except (ValueError, TypeError, RecursionError) as error:
        raise Protected("malformed_relationship") from error
    if not isinstance(parsed, dict):
        raise Protected("malformed_relationship")
    return parsed


def references(payload: dict[str, Any]) -> set[str]:
    result = set()
    for name in ("task_id", "blocking_task_id", "result_ref"):
        value = payload.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise Protected("malformed_relationship")
        if name == "result_ref":
            if not value.startswith("task:") or not value[5:]:
                raise Protected("malformed_relationship")
            value = value[5:]
        result.add(value)
    return result


class Policy:
    def __init__(
        self,
        db: sqlite3.Connection,
        stores: dict[str, Any],
        cutoff: float,
        selection: Any,
        planned_at: float,
        hooks: StoreHookRegistry,
        contributors: tuple[MaintenanceContribution, ...],
    ) -> None:
        self.db, self.stores, self.cutoff = db, stores, cutoff
        self.selection, self.planned_at = selection, planned_at
        # The loaded providers' receipt-store policy: which terminal receipts still have a
        # recovery path, and where each provider keeps a receipt's project.
        self.hooks = hooks
        self.contributors: dict[tuple[str, str], MaintenanceContribution] = {}
        for contribution in contributors:
            for key in contribution.tables:
                if key[0] != contribution.module_name:
                    message = (
                        f"Module {contribution.module_name!r} maintenance claim {key!r} "
                        "is outside its store namespace"
                    )
                    if contribution.built_in:
                        raise ValueError(message)
                    _LOG.warning("Skipping third-party %s", message)
                    continue
                previous = self.contributors.get(key)
                if previous is None:
                    self.contributors[key] = contribution
                    continue
                if previous.built_in and contribution.built_in:
                    raise ValueError(f"Maintenance table {key!r} has two built-in contributors")
                skipped = contribution
                if contribution.built_in:
                    skipped = previous
                    self.contributors[key] = contribution
                _LOG.warning(
                    "Skipping third-party module %r maintenance claim %r; already claimed by %r",
                    skipped.module_name,
                    key,
                    self.contributors[key].module_name,
                )
        self.tables: dict[str, set[str]] = {}
        self.unsupported: set[str] = set()
        self._incoming: dict[str, set[Key]] | None = None
        self._incoming_error: str | None = None
        for store, info in stores.items():
            self.tables[store] = set()
            if info["status"] == "present":
                self.tables[store] = {
                    r[0]
                    for r in db.execute(
                        f'SELECT name FROM {info["alias"]}.sqlite_master WHERE type="table"'
                    )
                }
                required = (
                    {"commands", "events", "outbox", "retention_history"}
                    if store == "bus"
                    else {store if store in ("tasks", "pressure") else "messages"}
                )
                if not required <= self.tables[store]:
                    self.unsupported.add(store)
                for table in self.tables[store] & COLUMNS.keys():
                    columns = {
                        r[1] for r in db.execute(f"PRAGMA {info['alias']}.table_info({table})")
                    }
                    if columns != COLUMNS[table]:
                        self.unsupported.add(store)
                if store == "bus" and store not in self.unsupported:
                    history = db.execute(
                        f"SELECT singleton,deleted_event_ceiling FROM {info['alias']}."
                        "retention_history LIMIT 2"
                    ).fetchall()
                    if (
                        len(history) != 1
                        or history[0]["singleton"] != 1
                        or type(history[0]["deleted_event_ceiling"]) is not int
                        or history[0]["deleted_event_ceiling"] < 0
                    ):
                        # Event deletion must update an existing cursor-gap row;
                        # an absent or invalid row cannot be repaired in preview.
                        self.unsupported.add(store)

    def inspect_module_row(
        self,
        store: str,
        table: str,
        row: dict[str, Any],
        *,
        retention: dict[str, Any] | None = None,
        related: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        contributor = self.contributors.get((store, table))
        if contributor is None:
            raise Protected("module_policy_unavailable")
        try:
            facts = contributor.inspect(
                store,
                table,
                row,
                retention=retention,
                related=related,
                planned_at=self.planned_at,
            )
        except MaintenanceProtected as error:
            raise Protected(str(error)) from error
        return dict(facts)

    def table(self, store: str, table: str) -> str:
        if table not in KNOWN[store]:
            raise ValueError("Unknown maintenance table")
        return f"{self.stores[store]['alias']}.{table}"

    def present(self, store: str, table: str) -> bool:
        return table in self.tables[store]

    def application_store(self) -> str | None:
        """Choose a supported existing receipt store, independently of main's alias."""
        return next(
            (
                store
                for store in self.stores
                if store not in self.unsupported
                and self.stores[store].get("user_version", 0) == 0
                and not self.tables[store] - KNOWN[store]
                and {"retention_records", "retention_applications"} <= self.tables[store]
            ),
            None,
        )

    def incoming(self, ident: str) -> set[Key]:
        """Prove the bounded reference inventory unambiguous before selecting edges."""
        if self._incoming_error is not None:
            raise Protected(self._incoming_error)
        if self._incoming is None:
            try:
                self._incoming = self._reference_inventory()
            except Protected as error:
                self._incoming_error = str(error)
                raise
        return self._incoming.get(ident, set())

    def _reference_inventory(self) -> dict[str, set[Key]]:
        if self.stores["bus"]["status"] == "present" and (
            "bus" in self.unsupported or self.stores["bus"].get("user_version", 0) != 0
        ):
            # Unknown known-table fields can carry incoming edges that this
            # version cannot enumerate. Unrelated tables keep their own guard.
            raise Protected("unsupported_schema")
        tables = [table for table in ("events", "outbox") if self.present("bus", table)]
        row_count = byte_count = 0
        # Aggregate lengths before transferring any body: a huge malformed row must
        # not defeat the same limits that protect normal component traversal.
        for table in tables:
            kind_size = "+COALESCE(length(CAST(kind AS BLOB)),0)" if table == "outbox" else ""
            count, size = self.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(COALESCE(length(CAST(id AS BLOB)),0)"
                f"+COALESCE(length(CAST(payload AS BLOB)),0){kind_size}),0) "
                f"FROM {self.table('bus', table)}"
            ).fetchone()
            row_count += count
            byte_count += size
        if row_count > MAX_COMPONENT_ROWS or byte_count > MAX_COMPONENT_BYTES:
            raise Protected("component_limit")
        incoming: dict[str, set[Key]] = {}
        for table in tables:
            kind = ",kind" if table == "outbox" else ""
            for row in self.db.execute(f"SELECT id,payload{kind} FROM {self.table('bus', table)}"):
                payload = object_json(row["payload"])
                if table == "outbox":
                    if row["kind"] == "command":
                        continue
                    if row["kind"] != "event" or not isinstance(payload.get("payload"), dict):
                        raise Protected("malformed_relationship")
                    payload = payload["payload"]
                for task_id in references(payload):
                    incoming.setdefault(task_id, set()).add(("bus", table, str(row["id"])))
        return incoming

    def rows(
        self, store: str, table: str, where: str, args: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        if not self.present(store, table):
            return []
        result: list[dict[str, Any]] = []
        byte_count = 0
        for row in self.db.execute(
            f"SELECT * FROM {self.table(store, table)} WHERE {where} LIMIT ?",
            (*args, MAX_COMPONENT_ROWS + 1),
        ):
            decoded = dict(row)
            byte_count += len(canonical(decoded).encode())
            if len(result) >= MAX_COMPONENT_ROWS or byte_count > MAX_COMPONENT_BYTES:
                raise Protected("component_limit")
            result.append(decoded)
        return result

    def retained(self, store: str, table: str, ident: str) -> dict[str, Any]:
        if not self.present(store, "retention_records") or not self.present(
            store, "retention_applications"
        ):
            raise Protected("migration_required")
        rows = self.rows(store, "retention_records", "entity=? AND row_id=?", (table, ident))
        if len(rows) != 1:
            raise Protected("mutation_age_unknown")
        return rows[0]

    def _closure(self, root: Key) -> dict[Key, dict[str, Any]]:
        pending = [root]
        collected: dict[Key, dict[str, Any]] = {}
        byte_count = 0
        while pending:
            key = pending.pop()
            if key in collected:
                continue
            store, table, ident = key
            if (
                store in self.unsupported
                or self.stores[store].get("user_version", 0) != 0
                or self.tables[store] - KNOWN[store]
            ):
                raise Protected("unsupported_schema")
            pk = "peer_id" if table == "rex_peers" else "id"
            rows = self.rows(store, table, f"{pk}=?", (ident,))
            if len(rows) != 1:
                raise Protected("missing_relationship")
            row = rows[0]
            collected[key] = row
            byte_count += len(canonical(row).encode())
            if len(collected) > MAX_COMPONENT_ROWS or byte_count > MAX_COMPONENT_BYTES:
                raise Protected("component_limit")
            if table == "tasks":
                # Establish the bus schema before querying even direct edges:
                # unsupported known graph tables cannot prove absence. Strict
                # JSON validates every reference before selecting edges; SQL
                # json_extract and application readers disagree on duplicates.
                pending.extend(sorted(self.incoming(ident)))
                if row["request_key"].startswith("bus:"):
                    pending.append(("bus", "commands", row["request_key"][4:]))
                for command in self.rows("bus", "commands", "task_id=?", (ident,)):
                    pending.append(("bus", "commands", command["id"]))
                metadata = object_json(row["metadata"])
                if metadata.get("kind") == "rex":
                    if metadata.get("peer_id") != row["target"]:
                        raise Protected("conflicting_rex_identity")
                    pending.append(("tasks", "rex_peers", row["target"]))
            elif table == "commands":
                if row["task_id"] is not None:
                    pending.append(("tasks", "tasks", row["task_id"]))
                for task in self.rows("tasks", "tasks", "request_key=?", ("bus:" + ident,)):
                    pending.append(("tasks", "tasks", task["id"]))
                for relation in ("events", "outbox"):
                    for member in self.rows("bus", relation, "command_id=?", (ident,)):
                        pending.append(("bus", relation, str(member["id"])))
            elif table in ("events", "outbox"):
                pending.append(("bus", "commands", row["command_id"]))
                payload = object_json(row["payload"])
                if table == "outbox":
                    if row["kind"] == "command":
                        continue
                    if (
                        row["kind"] != "event"
                        or payload.get("event_id") != ident
                        or type(payload.get("schema_version")) is not int
                        or payload.get("schema_version") != 1
                    ):
                        raise Protected("malformed_relationship")
                    payload = payload.get("payload")
                    if not isinstance(payload, dict):
                        raise Protected("malformed_relationship")
                    pending.append(("bus", "events", ident))
                else:
                    pending.append(("bus", "outbox", ident))
                for task_id in references(payload):
                    pending.append(("tasks", "tasks", task_id))
        return collected

    def _task(
        self, row: dict[str, Any], retention: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str | None]:
        metadata = object_json(row["metadata"])
        policy = definition(metadata.get("kind", ""))
        if policy is None or metadata["kind"] != policy.name:
            raise Protected("provider_unknown")
        task = {**row, "metadata": metadata}
        if retention is not None:
            task["_retention"] = retention
        if row["status"] not in TERMINAL:
            raise Protected("unfinished")
        if (
            self.hooks.is_reconcilable(task)
            or metadata.get("identity_invalidated")
            or "provisional_result" in metadata
            or metadata.get("completion_received")
            and not metadata.get("delivery_verified")
        ):
            raise Protected("reconcilable_or_contradictory")
        if row["status"] == "completed" and (row["result"] is None and retention is None):
            raise Protected("completion_evidence_missing")
        if (
            row["status"] == "completed"
            and retention is not None
            and (
                retention.get("current_result_present") is not True
                or retention["fields"]["result"]["present"] is not True
            )
        ):
            raise Protected("completion_evidence_missing")
        provider = metadata["kind"]
        if not row["target"].startswith(provider + ":"):
            raise Protected("conflicting_provider_identity")
        if self.selection.provider and provider != self.selection.provider:
            raise Protected("provider_excluded")
        project = self.hooks.project_of(metadata)
        if project is not None and (not isinstance(project, str) or not project.startswith("/")):
            raise Protected("project_invalid")
        return task, project

    def group(self, root: Key) -> dict[str, Any]:
        collected = self._closure(root)
        members, actions = [], []
        tasks: dict[str, tuple[dict[str, Any], str | None]] = {}
        records: dict[Key, dict[str, Any]] = {}
        for key, row in collected.items():
            store, table, ident = key
            timestamps = (
                ("created", "updated")
                if table in ("tasks", "commands", "rex_peers")
                else ("created",)
            )
            if not all(old(row.get(k), self.cutoff) for k in timestamps):
                raise Protected("recent_or_invalid_time")
            if table in ("tasks", "commands", "messages"):
                retained = self.retained(store, table, ident)
                records[key] = retained
                retention = decode_retention(retained["record"])
                if table == "tasks":
                    if not old(retained["changed_at"], self.cutoff):
                        raise Protected("recent_mutation")
                    tasks[ident] = self._task(row, retention)
                elif table == "messages":
                    if self.selection.provider or self.selection.project:
                        raise Protected("client_store_excluded")
                    if not old(row["acknowledged"], self.cutoff):
                        raise Protected("unacknowledged_or_recent")
            elif table == "outbox":
                if row["published_at"] is None:
                    raise Protected("pending_publication")
                if not old(row["published_at"], self.cutoff):
                    raise Protected("recent_publication")
        linked_projects: dict[str, str] = {}
        for key, row in collected.items():
            store, table, ident = key
            if table != "commands":
                continue
            if row["delivery"] not in ("bound", "cancelled", "expired", "rejected"):
                raise Protected("unfinished_command")
            retention = decode_retention(records[key]["record"])
            facts = self.inspect_module_row(store, table, row, retention=retention)
            if retention is None:
                envelope = facts["envelope"]
                message_identity = content_identity(facts["message"])
            else:
                envelope = retention["envelope"]
                message_identity = retention["message_identity"]
            if envelope["command_id"] != ident or envelope["request_id"] != row["request_key"]:
                raise Protected("conflicting_envelope")
            provider = definition(envelope["peer_id"].partition(":")[0])
            if provider is None:
                raise Protected("provider_unknown")
            if self.selection.provider and provider.name != self.selection.provider:
                raise Protected("provider_excluded")
            if self.selection.project and envelope["project"] != self.selection.project:
                raise Protected("project_excluded")
            related = [t for t, _ in tasks.values() if t["request_key"] == "bus:" + ident]
            if row["delivery"] == "bound":
                if len(related) != 1 or related[0]["id"] != row["task_id"] or not row["target_key"]:
                    raise Protected("conflicting_bound_identity")
                task = related[0]
                if (
                    task["target"] != row["target_key"]
                    or task["metadata"]["kind"] != provider.name
                    or (
                        task.get("_retention", {}).get("fields", {}).get("message")
                        or content_identity(task["message"])
                    )
                    != message_identity
                ):
                    raise Protected("conflicting_bound_identity")
                if task_projection_fingerprint(task) != row["task_fingerprint"]:
                    raise Protected("unprojected_task")
                linked_projects[task["id"]] = envelope["project"]
            elif related or row["task_id"] is not None:
                raise Protected("unbound_reserved_task")
            if retention is None:
                record = {
                    "schema_version": 1,
                    "pruned_at": self.planned_at,
                    "fields": {key: content_identity(row[key]) for key in ("payload", "detail")},
                    "envelope": envelope,
                    "message_identity": message_identity,
                    "ttl_seconds": envelope["expires_at"] - envelope["created_at"],
                }
                actions.append(self._prune(key, row, record, {"payload": "", "detail": None}))
        for ident, (task, project) in tasks.items():
            linked = linked_projects.get(ident)
            if project is not None and linked is not None and project != linked:
                raise Protected("conflicting_project")
            actual = project or linked
            if self.selection.project and actual != self.selection.project:
                raise Protected("project_unknown" if actual is None else "project_excluded")
            if task["metadata"]["kind"] == "rex":
                peer = collected[("tasks", "rex_peers", task["target"])]
                if object_json(peer["binding"]) != task["metadata"].get("binding"):
                    raise Protected("conflicting_rex_identity")
            if task.get("_retention") is None:
                row = collected[("tasks", "tasks", ident)]
                actions.append(
                    self._prune(
                        ("tasks", "tasks", ident),
                        row,
                        task_retention(task, self.planned_at),
                        {"message": "", "result": None, "detail": None},
                    )
                )
        for key, row in collected.items():
            store, table, ident = key
            if table == "messages" and records[key]["record"] is None:
                record = {
                    "schema_version": 1,
                    "pruned_at": self.planned_at,
                    "fields": {"message": content_identity(row["message"])},
                }
                actions.append(self._prune(key, row, record, {"message": ""}))
            elif table in ("events", "outbox"):
                if table == "outbox" and row["kind"] == "command":
                    self.inspect_module_row(
                        store,
                        table,
                        row,
                        related={"command": collected[("bus", "commands", row["command_id"])]},
                    )
                if table == "events":
                    outbox = collected[("bus", "outbox", ident)]
                    envelope = object_json(outbox["payload"])
                    if (
                        outbox["kind"] != "event"
                        or outbox["command_id"] != row["command_id"]
                        or envelope.get("command_id") != row["command_id"]
                        or envelope.get("type") != row["type"]
                        or envelope.get("created_at") != row["created"]
                        or envelope.get("payload") != object_json(row["payload"])
                    ):
                        raise Protected("conflicting_event_publication")
                actions.append(
                    {
                        "store": store,
                        "table": table,
                        "id": ident,
                        "kind": "delete",
                        "before_hash": digest(row),
                        "after_hash": None,
                        "retention": None,
                        "bytes": len(row["payload"].encode()),
                    }
                )
        by_key = {(a["store"], a["table"], a["id"]): a for a in actions}
        for key, row in sorted(collected.items()):
            store, table, ident = key
            action = by_key.get(key)
            members.append(
                {
                    "store": store,
                    "table": table,
                    "id": ident,
                    "hash": digest(row),
                    "after_hash": action["after_hash"] if action else digest(row),
                }
            )
            if key in records:
                retained = records[key]
                after = (
                    {**retained, "record": canonical(action["retention"])} if action else retained
                )
                members.append(
                    {
                        "store": store,
                        "table": "retention_records",
                        "id": table + ":" + ident,
                        "hash": digest(retained),
                        "after_hash": digest(after),
                        "retention_after": after,
                    }
                )
        return {
            "root": list(root),
            "members": members,
            "actions": sorted(actions, key=lambda a: (a["store"], a["table"], a["id"])),
        }

    @staticmethod
    def _prune(
        key: Key, row: dict[str, Any], record: dict[str, Any], changes: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "store": key[0],
            "table": key[1],
            "id": key[2],
            "kind": "prune",
            "before_hash": digest(row),
            "after_hash": digest({**row, **changes}),
            "retention": record,
            "bytes": sum(v["bytes"] for v in record["fields"].values()),
        }
