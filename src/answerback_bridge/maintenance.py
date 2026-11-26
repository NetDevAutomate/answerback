"""Explicit, preview-first maintenance of the selected config's known SQLite stores."""

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapters.catalog import definition
from .digests import canonical, digest, namespace_for_config, unique_object
from .maintenance_policy import Policy, Protected
from .plugin import MaintenanceContribution, load_maintenance_contributions
from .tasks import StoreHookRegistry
from .transport import check_config_owner, runtime_dir

MAX_ACTIONS = 1000
MAX_PLAN_BYTES = 4 * 1024 * 1024
TABLES = {
    "tasks": ("tasks",),
    "bus": ("commands", "events", "outbox"),
    "clients": ("messages",),
    "pressure": (),  # Levels and a bounded signal ring: no content to retain or prune.
}


class MaintenanceError(ValueError):
    """A refusal with a machine-readable outcome; no partial apply is successful."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class RetentionSelection:
    older_than_days: int = 30
    provider: str | None = None
    project: str | None = None
    max_actions: int = 500
    max_roots: int = 500

    def __post_init__(self) -> None:
        for value, maximum, label in (
            (self.older_than_days, 36500, "age"),
            (self.max_actions, MAX_ACTIONS, "action limit"),
            (self.max_roots, MAX_ACTIONS, "root limit"),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"Invalid positive integral {label}")
        if self.provider is not None:
            provider = definition(self.provider)
            if provider is None:
                raise ValueError("Unknown exact provider name")
            object.__setattr__(self, "provider", provider.name)
        if self.project is not None and (
            not isinstance(self.project, str)
            or not Path(self.project).is_absolute()
            or "\0" in self.project
            or len(self.project) > 4096
        ):
            raise ValueError("Project must be an exact absolute persisted path")


class Maintenance:
    def __init__(
        self,
        config_path: Path,
        *,
        lock_timeout: float = 0.25,
        hooks: StoreHookRegistry | None = None,
        contributors: tuple[MaintenanceContribution, ...] | None = None,
    ) -> None:
        if not config_path.is_absolute():
            raise ValueError("An absolute configuration path is required")
        self.config_path = config_path
        if hooks is None:
            # Maintenance runs without a service, so no plugin has registered its hooks on
            # a store; the built-ins' hooks are the policy until entry-point loading lands.
            from .adapters.builtins import builtin_store_hooks

            hooks = builtin_store_hooks()
        self.hooks = hooks
        if contributors is None:
            contributors = load_maintenance_contributions()
        self.contributors = contributors
        # Namespace calculation requires no configured or running broker.
        self.namespace = namespace_for_config(config_path)
        self.paths = {
            "tasks": config_path.parent / "tasks.sqlite",
            "bus": config_path.parent / "bus" / "ledger.sqlite",
            "clients": config_path.parent / "client-mailbox.sqlite",
            "pressure": config_path.parent / "pressure.sqlite",
        }
        if not math.isfinite(lock_timeout) or not 0 <= lock_timeout <= 5:
            raise ValueError("Lock timeout must be between zero and five seconds")
        self.lock_timeout = lock_timeout

    def _project_of(self, metadata: Any) -> Any:
        """The project a receipt's provider reads from its stored metadata; None if unknown.

        Metadata that is not a JSON object, a kind no loaded plugin serves, and a provider
        that keeps no project all count as unknown.
        """
        try:
            parsed = json.loads(metadata)
        except (ValueError, TypeError, RecursionError):
            return None
        return self.hooks.project_of(parsed) if isinstance(parsed, dict) else None

    def stats(self, selection: RetentionSelection | None = None) -> dict[str, Any]:
        selection = selection or RetentionSelection()
        stores: dict[str, Any] = {}
        content = {
            "tasks": ("message", "result", "detail"),
            "commands": ("payload", "detail"),
            "messages": ("message",),
            "events": ("payload",),
            "outbox": ("payload",),
        }
        with self._snapshot() as (db, identities):
            if db is None:
                return {"stores": identities, "selection": asdict(selection)}
            policy = Policy(
                db,
                identities,
                time.time() - selection.older_than_days * 86400,
                selection,
                time.time(),
                self.hooks,
                self.contributors,
            )
            for name, path in self.paths.items():
                if identities[name]["status"] == "absent":
                    stores[name] = {"status": "absent"}
                    continue
                alias = identities[name]["alias"]
                tables: dict[str, Any] = {}
                logical = 0
                for table in TABLES[name]:
                    if not policy.present(name, table):
                        continue
                    columns = {r[1] for r in db.execute(f"PRAGMA {alias}.table_info({table})")}
                    if not set(content[table]) <= columns:
                        tables[table] = {"status": "unsupported_schema"}
                        continue
                    sums = "+".join(
                        f"COALESCE(length(CAST({k} AS BLOB)),0)" for k in content[table]
                    )
                    count, total = db.execute(
                        f"SELECT COUNT(*),COALESCE(SUM({sums}),0) FROM {alias}.{table}"
                    ).fetchone()
                    tables[table] = {"rows": count, "logical_content_bytes": total}
                    logical += total
                    if table == "tasks":
                        tables[table]["retained_metadata_bytes"] = db.execute(
                            "SELECT COALESCE(SUM(length(CAST(metadata AS BLOB))),0) "
                            f"FROM {alias}.tasks"
                        ).fetchone()[0]
                        tables[table]["project_unknown_rows"] = sum(
                            self._project_of(metadata) is None
                            for (metadata,) in db.execute(f"SELECT metadata FROM {alias}.tasks")
                        )
                        tables[table]["project_coverage"] = (
                            "Persisted standalone task facts; "
                            "validated command joins are evaluated by plan."
                        )
                        tables[table]["unfinished_rows"] = db.execute(
                            f"SELECT COUNT(*) FROM {alias}.tasks WHERE status NOT IN "
                            "('completed','failed','abandoned','cancelled')"
                        ).fetchone()[0]
                retained = 0
                if policy.present(name, "retention_records"):
                    retained = db.execute(
                        f"SELECT COUNT(*) FROM {alias}.retention_records WHERE record IS NOT NULL"
                    ).fetchone()[0]
                page_size = db.execute(f"PRAGMA {alias}.page_size").fetchone()[0]
                free = db.execute(f"PRAGMA {alias}.freelist_count").fetchone()[0]
                stores[name] = {
                    "status": "present",
                    "file_bytes": path.stat().st_size,
                    "page_count": db.execute(f"PRAGMA {alias}.page_count").fetchone()[0],
                    "page_size": page_size,
                    "freelist_count": free,
                    "approximate_reclaimable_page_bytes": free * page_size,
                    "logical_content_bytes": logical,
                    "retained_identities": retained,
                    "tables": tables,
                    "journal_mode": identities[name]["journal_mode"],
                    "retention_schema_available": policy.present(name, "retention_records"),
                    "excluded": name == "clients" and bool(selection.provider or selection.project),
                }
        return {
            "stores": stores,
            "selection": asdict(selection),
            "scope": "whole-store aggregates; filters apply to retention plans",
            "limitations": [
                "Metadata and identity rows are retained indefinitely.",
                "Logical content bytes are separate from reclaimable file pages.",
            ],
        }

    def plan(
        self, selection: RetentionSelection | None = None, *, cursor: dict[str, str] | None = None
    ) -> dict[str, Any]:
        selection = selection or RetentionSelection()
        cursor = dict(cursor or {})
        if any(
            k not in TABLES or not isinstance(v, str) or len(v) > 256 for k, v in cursor.items()
        ):
            raise ValueError("Invalid continuation cursor")
        now = time.time()
        with self._snapshot() as (db, identities):
            actions, groups, protected = [], [], {}
            application_store = None
            continuation = dict(cursor)
            truncated, scanned = False, 0
            if db is not None:
                policy = Policy(
                    db,
                    identities,
                    now - selection.older_than_days * 86400,
                    selection,
                    now,
                    self.hooks,
                    self.contributors,
                )
                application_store = policy.application_store()
                seen = set()
                for store, table in (
                    ("tasks", "tasks"),
                    ("bus", "commands"),
                    ("clients", "messages"),
                ):
                    if store == "clients" and (selection.provider or selection.project):
                        protected["client_store_excluded"] = 1
                        continue
                    if not policy.present(store, table):
                        continue
                    # TEXT cursors also cover integer notification IDs.
                    candidates = db.execute(
                        f"SELECT CAST(id AS TEXT) AS id FROM {policy.table(store, table)} "
                        "WHERE CAST(id AS TEXT)>? ORDER BY CAST(id AS TEXT) LIMIT ?",
                        (cursor.get(store, ""), selection.max_roots + 1),
                    ).fetchall()
                    for row in candidates:
                        root = (store, table, row["id"])
                        if scanned >= selection.max_roots:
                            truncated = True
                            break
                        if root in seen:
                            continuation[store] = row["id"]
                            continue
                        scanned += 1
                        try:
                            group = policy.group(root)
                        except (Protected, ValueError, KeyError, TypeError) as error:
                            reason = (
                                str(error) if isinstance(error, Protected) else "malformed_record"
                            )
                            protected[reason] = protected.get(reason, 0) + 1
                        else:
                            if len(group["actions"]) > selection.max_actions:
                                protected["component_action_limit"] = (
                                    protected.get("component_action_limit", 0) + 1
                                )
                            elif len(actions) + len(group["actions"]) > selection.max_actions:
                                truncated = True
                                break
                            else:
                                seen.update(
                                    (m["store"], m["table"], m["id"]) for m in group["members"]
                                )
                                if group["actions"]:
                                    groups.append(group)
                                    actions.extend(group["actions"])
                        continuation[store] = row["id"]
                    if truncated:
                        break
            actions.sort(key=lambda a: (a["store"], a["table"], a["id"]))
            result = {
                "schema_version": 1,
                "policy_version": 1,
                "config_path": str(self.config_path),
                "config_identity": self._config_identity(),
                "namespace": self.namespace,
                "stores": identities,
                "application_store": application_store,
                "cutoff": now - selection.older_than_days * 86400,
                "planned_at": now,
                "selection": asdict(selection),
                "actions": actions,
                "groups": groups,
                "protected": protected,
                "scanned_roots": scanned,
                "truncated": truncated,
                "continuation": continuation if truncated else None,
            }
        result["digest"] = digest(result)
        if len(canonical(result).encode()) > MAX_PLAN_BYTES:
            raise MaintenanceError(
                "plan_limit", "Plan is too large; use a smaller action/root limit"
            )
        return result

    def write_plan(self, plan: dict[str, Any], path: Path) -> None:
        self._validate_plan(plan)
        if path.is_symlink():
            raise MaintenanceError("unsafe_path", "Plan output cannot be a symlink")
        descriptor, name = tempfile.mkstemp(prefix=".retention-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(canonical(plan) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def read_plan(self, path: Path) -> dict[str, Any]:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > MAX_PLAN_BYTES
            ):
                raise MaintenanceError("invalid_plan", "Plan must be a private owned bounded file")
            raw = source.read(MAX_PLAN_BYTES + 1)
            if len(raw) > MAX_PLAN_BYTES:
                raise MaintenanceError("invalid_plan", "Plan exceeds size limit")
        plan = json.loads(raw, object_pairs_hook=unique_object)
        self._validate_plan(plan)
        return plan

    def compact(self, store: str) -> dict[str, Any]:
        if store not in self.paths:
            raise ValueError("Unknown named maintenance store")
        path = self.paths[store]
        lock = runtime_dir(self.config_path) / "owner.lock"
        try:
            descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise MaintenanceError(
                "service_owner_unknown",
                "Existing runtime owner lock required; stop owned services first",
            ) from error
        with os.fdopen(descriptor, "rb") as owner:
            info = os.fstat(owner.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise MaintenanceError(
                    "unsafe_path", "Runtime owner lock is not an owned regular file"
                )
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise MaintenanceError(
                    "service_running", "Stop the owned service before compaction"
                ) from error
            check_config_owner(self.config_path)
            if not path.is_file() or path.is_symlink() or path.resolve() != path:
                raise MaintenanceError("unsafe_path", "Store must be an existing regular database")
            original = path.stat()
            db = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=self.lock_timeout)
            try:
                mode = db.execute("PRAGMA journal_mode").fetchone()[0]
                if mode not in ("delete", "truncate", "persist"):
                    raise MaintenanceError(
                        "unsupported_journal", "Compaction requires an on-disk rollback journal"
                    )

                def measurements() -> dict[str, int]:
                    return {
                        "file_bytes": path.stat().st_size,
                        "page_count": db.execute("PRAGMA page_count").fetchone()[0],
                        "freelist_count": db.execute("PRAGMA freelist_count").fetchone()[0],
                    }

                before = measurements()
                current_lock = lock.lstat()
                current_store = path.lstat()
                if (current_lock.st_dev, current_lock.st_ino) != (info.st_dev, info.st_ino) or (
                    current_store.st_dev,
                    current_store.st_ino,
                ) != (original.st_dev, original.st_ino):
                    raise MaintenanceError("stale_plan", "Store or owner lock identity changed")
                db.execute("VACUUM")
                return {
                    "status": "compacted",
                    "store": store,
                    "before": before,
                    "after": measurements(),
                }
            except sqlite3.OperationalError as error:
                code = "busy" if "locked" in str(error) or "busy" in str(error) else "unavailable"
                raise MaintenanceError(code, str(error)) from error
            finally:
                db.close()

    def _config_identity(self) -> dict[str, Any]:
        if not self.config_path.exists() and not self.config_path.is_symlink():
            return {"status": "absent"}
        info = self.config_path.lstat()
        if not stat.S_ISREG(info.st_mode) or self.config_path.resolve() != self.config_path:
            raise MaintenanceError("unsafe_path", "Configuration identity cannot use a symlink")
        if info.st_size > MAX_PLAN_BYTES:
            raise MaintenanceError("invalid_config", "Configuration is too large")
        return {
            "device": info.st_dev,
            "inode": info.st_ino,
            "sha256": hashlib.sha256(self.config_path.read_bytes()).hexdigest(),
        }

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        self._validate_plan(plan)
        selection = RetentionSelection(**plan["selection"])
        with self._snapshot(write=True) as (db, identities):
            if identities != plan["stores"] or self._config_identity() != plan["config_identity"]:
                raise MaintenanceError("stale_plan", "Store, schema or configuration changed")
            if db is None:
                return {"status": "already_applied", "actions": 0, "digest": plan["digest"]}
            policy = Policy(
                db,
                identities,
                plan["cutoff"],
                selection,
                plan["planned_at"],
                self.hooks,
                self.contributors,
            )
            application_store = policy.application_store()
            if application_store != plan["application_store"]:
                raise MaintenanceError("stale_plan", "Application receipt store changed")
            application_table = (
                policy.table(application_store, "retention_applications")
                if application_store is not None
                else None
            )
            current_groups = []
            for group in plan["groups"]:
                try:
                    current_groups.append(policy.group(tuple(group["root"])))
                except (Protected, ValueError, KeyError, TypeError) as error:
                    raise MaintenanceError(
                        "stale_plan", "Selected eligibility or references changed"
                    ) from error
            applied = (
                db.execute(
                    f"SELECT applied_at FROM {application_table} WHERE digest=?",
                    (plan["digest"],),
                ).fetchone()
                if application_table is not None
                else None
            )
            if current_groups == plan["groups"] and applied is None:
                applied_at = time.time()
                for action in plan["actions"]:
                    effective = dict(action)
                    if action["retention"] is not None:
                        effective["retention"] = {**action["retention"], "pruned_at": applied_at}
                    self._execute_action(db, identities, effective)
                if plan["actions"]:
                    db.execute(
                        f"INSERT INTO {application_table} VALUES (?,?)",
                        (plan["digest"], applied_at),
                    )
                return {
                    "status": "applied",
                    "actions": len(plan["actions"]),
                    "logical_bytes_pruned": sum(a["bytes"] for a in plan["actions"]),
                    "digest": plan["digest"],
                }

            def expected_hash(member: dict[str, Any]) -> str | None:
                if (
                    "retention_after" not in member
                    or applied is None
                    or member["hash"] == member["after_hash"]
                ):
                    return member["after_hash"]
                row = dict(member["retention_after"])
                record = json.loads(row["record"]) if row["record"] else None
                if record and record["pruned_at"] == plan["planned_at"]:
                    row["record"] = canonical({**record, "pruned_at": applied["applied_at"]})
                return digest(row)

            expected_after = [
                {
                    (m["store"], m["table"], m["id"]): expected_hash(m)
                    for m in group["members"]
                    if m["after_hash"] is not None
                }
                for group in plan["groups"]
            ]
            actual_after = [
                {(m["store"], m["table"], m["id"]): m["hash"] for m in group["members"]}
                for group in current_groups
            ]
            if (
                applied is not None
                and expected_after == actual_after
                and all(not g["actions"] for g in current_groups)
            ):
                return {"status": "already_applied", "actions": 0, "digest": plan["digest"]}
            raise MaintenanceError("stale_plan", "Reviewed row preimages no longer match")

    def _validate_plan(self, plan: dict[str, Any]) -> None:
        try:
            encoded = canonical(plan)
            if len(encoded.encode()) > MAX_PLAN_BYTES or not isinstance(plan, dict):
                raise ValueError("Plan size")
            if (
                plan["schema_version"] != 1
                or plan["policy_version"] != 1
                or plan["config_path"] != str(self.config_path)
                or plan["namespace"] != self.namespace
                or (
                    plan["application_store"] is not None
                    and plan["application_store"] not in TABLES
                )
                or (plan["actions"] and plan["application_store"] is None)
                or plan["digest"] != digest({k: v for k, v in plan.items() if k != "digest"})
            ):
                raise ValueError("Plan identity")
            selection = RetentionSelection(**plan["selection"])
            if (
                type(plan["planned_at"]) not in (int, float)
                or not math.isfinite(plan["planned_at"])
                or plan["planned_at"] <= 0
                or plan["planned_at"] > time.time() + 1
                or plan["cutoff"] != plan["planned_at"] - selection.older_than_days * 86400
            ):
                raise ValueError("Fixed cutoff")
            actions = plan["actions"]
            if (
                not isinstance(actions, list)
                or len(actions) > selection.max_actions
                or len(plan["groups"]) > selection.max_roots
                or actions
                != sorted(
                    [a for g in plan["groups"] for a in g["actions"]],
                    key=lambda a: (a["store"], a["table"], a["id"]),
                )
            ):
                raise ValueError("Action bounds or group mismatch")
            seen = set()
            for action in actions:
                if (
                    set(action)
                    != {
                        "store",
                        "table",
                        "id",
                        "kind",
                        "before_hash",
                        "after_hash",
                        "retention",
                        "bytes",
                    }
                    or action["store"] not in TABLES
                    or action["table"] not in TABLES[action["store"]]
                    or action["kind"] not in ("prune", "delete")
                    or type(action["id"]) is not str
                ):
                    raise ValueError("Action schema")
                key = (action["store"], action["table"], action["id"])
                if key in seen:
                    raise ValueError("Duplicate action")
                seen.add(key)
        except (TypeError, KeyError, ValueError, OverflowError, RecursionError) as error:
            raise MaintenanceError(
                "invalid_plan", "Invalid, oversized or altered retention plan"
            ) from error

    @staticmethod
    def _execute_action(
        db: sqlite3.Connection, stores: dict[str, Any], action: dict[str, Any]
    ) -> None:
        alias = stores[action["store"]]["alias"]
        table = action["table"]
        if action["kind"] == "delete" and table in ("events", "outbox"):
            if table == "events":
                seq = db.execute(
                    f"SELECT seq FROM {alias}.events WHERE id=?", (action["id"],)
                ).fetchone()[0]
                updated = db.execute(
                    f"UPDATE {alias}.retention_history "
                    "SET deleted_event_ceiling=max(deleted_event_ceiling,?) WHERE singleton=1",
                    (seq,),
                )
                if updated.rowcount != 1:
                    raise MaintenanceError(
                        "stale_plan", "Required event retention history was not updated"
                    )
            db.execute(f"DELETE FROM {alias}.{table} WHERE id=?", (action["id"],))
        elif action["kind"] == "prune" and table in ("tasks", "commands", "messages"):
            db.execute(
                f"UPDATE {alias}.retention_records SET record=? WHERE entity=? AND row_id=?",
                (canonical(action["retention"]), table, action["id"]),
            )
            sets = {
                "tasks": "message='',result=NULL,detail=NULL",
                "commands": "payload='',detail=NULL",
                "messages": "message=''",
            }
            db.execute(f"UPDATE {alias}.{table} SET {sets[table]} WHERE id=?", (action["id"],))
        else:
            raise MaintenanceError("invalid_plan", "Unsupported action")

    def _verify_files(self, identities: dict[str, Any]) -> None:
        for name, path in self.paths.items():
            expected = identities[name]
            if expected["status"] == "absent":
                if path.exists() or path.is_symlink():
                    raise MaintenanceError("stale_plan", "A missing relationship store appeared")
                continue
            current = path.lstat()
            if (
                path.resolve() != path
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (expected["device"], expected["inode"])
            ):
                raise MaintenanceError("stale_plan", "Database file identity changed")

    @contextmanager
    def _snapshot(self, *, write: bool = False) -> Iterator[tuple[Any, dict[str, Any]]]:
        identities: dict[str, Any] = {}
        files: set[tuple[int, int]] = set()
        for name, path in self.paths.items():
            if not path.exists() and not path.is_symlink():
                identities[name] = {"status": "absent"}
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.resolve() != path:
                raise MaintenanceError("unsafe_path", "Database must be a regular non-symlink path")
            with path.open("rb") as source:
                header = source.read(20)
            if header[:16] == b"SQLite format 3\x00" and header[18:20] == b"\x02\x02":
                raise MaintenanceError(
                    "unsupported_journal",
                    "WAL maintenance is unsupported: "
                    "read-only SQLite can create WAL/SHM files. "
                    "Use a separately prepared rollback-journal database; "
                    "mode is never changed here.",
                )
            identity = (info.st_dev, info.st_ino)
            if identity in files:
                raise MaintenanceError("duplicate_store", "Known stores alias one physical file")
            files.add(identity)
            identities[name] = {
                "status": "present",
                "path": str(path),
                "device": info.st_dev,
                "inode": info.st_ino,
                "alias": name,
            }
        present = [name for name in self.paths if identities[name]["status"] == "present"]
        if not present:
            yield None, identities
            return
        main = present[0]
        identities[main]["alias"] = "main"
        mode = "rw" if write else "ro"
        db = sqlite3.connect(
            f"{self.paths[main].as_uri()}?mode={mode}", uri=True, timeout=self.lock_timeout
        )
        db.row_factory = sqlite3.Row
        try:
            for name in present[1:]:
                db.execute(
                    f"ATTACH DATABASE ? AS {name}", (f"{self.paths[name].as_uri()}?mode={mode}",)
                )
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            journals = []
            for name in present:
                alias = identities[name]["alias"]
                schema = [
                    tuple(row)
                    for row in db.execute(
                        f"SELECT type,name,tbl_name,sql FROM {alias}.sqlite_master "
                        "ORDER BY type,name"
                    )
                ]
                identities[name]["schema_hash"] = digest(schema)
                identities[name]["user_version"] = db.execute(
                    f"PRAGMA {alias}.user_version"
                ).fetchone()[0]
                mode_name = db.execute(f"PRAGMA {alias}.journal_mode").fetchone()[0].lower()
                identities[name]["journal_mode"] = mode_name
                journals.append(mode_name)
            if (
                write
                and len(present) > 1
                and any(j not in ("delete", "truncate", "persist") for j in journals)
            ):
                raise MaintenanceError(
                    "unsupported_journal",
                    "Atomic multi-store apply requires on-disk rollback journals; "
                    "WAL is unsupported",
                )
            self._verify_files(identities)
            yield db, identities
            if write:
                self._verify_files(identities)
                db.commit()
        except sqlite3.OperationalError as error:
            code = "busy" if "locked" in str(error) or "busy" in str(error) else "unavailable"
            raise MaintenanceError(code, str(error)) from error
        finally:
            db.close()
