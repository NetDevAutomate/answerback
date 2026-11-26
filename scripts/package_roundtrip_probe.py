"""Installed-wheel probe used by smoke_package_roundtrip; no live endpoints."""

import asyncio
import hashlib
import importlib
import json
import sqlite3
import sys
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from answerback_bridge.bus.config import BusConfig, routing_digest
    from answerback_bridge.bus.store import BusStore
    from answerback_bridge.client_mailbox import ClientMailbox
    from answerback_bridge.config import Config
    from answerback_bridge.tasks import TaskStore
else:
    # Authentic 0.9 predates the canonical namespace; exercise its real imports.
    from codex_herdr_bridge.bus.config import BusConfig, routing_digest
    from codex_herdr_bridge.bus.store import BusStore
    from codex_herdr_bridge.client_mailbox import ClientMailbox
    from codex_herdr_bridge.config import Config
    from codex_herdr_bridge.tasks import TaskStore


def snapshot(root: Path) -> dict[str, Any]:
    """Retain entire original tables, including column order and nullable cells."""
    result = {}
    for name in ("tasks.sqlite", "bus/ledger.sqlite", "client-mailbox.sqlite"):
        with sqlite3.connect(root / name) as db:
            tables = db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            result[name] = {
                table: {
                    "sql": sql,
                    "columns": db.execute(f'PRAGMA table_info("{table}")').fetchall(),
                    "rows": db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall(),
                }
                for table, sql in tables
            }
    # JSON normalizes SQLite tuples; comparison uses this exact complete representation.
    return json.loads(json.dumps(result))


def verify_snapshot(root: Path, name: str) -> dict[str, Any]:
    before = json.loads((root / name).read_text())
    after = snapshot(root)
    for database, tables in before.items():
        for table, expected in tables.items():
            assert after[database][table] == expected, (database, table)
    return after


def stores(root: Path) -> tuple[TaskStore, BusStore, ClientMailbox]:
    return (
        TaskStore(root / "tasks.sqlite"),
        BusStore(root / "bus/ledger.sqlite", clock=lambda: 100),
        ClientMailbox(root / "client-mailbox.sqlite"),
    )


def task_request(key: str) -> tuple[str, str, str, dict[str, Any]]:
    return "kiro:" + key, "résumé " + key, key, {"kind": "kiro", "cwd": "/project"}


def bus_request(root: Path, key: str) -> dict[str, Any]:
    fingerprint = json.loads((root / "identity.json").read_text())["routing_digest"]
    return {
        "request_id": key,
        "peer_id": "kiro:" + key,
        "project": "/project",
        "routing_digest": fingerprint,
        "message": "résumé " + key,
        "ttl_seconds": 60,
    }


def assert_duplicates(root: Path, *, pruned: bool = False) -> None:
    tasks, bus, notifications = stores(root)
    identities = json.loads((root / "identity.json").read_text())
    for key in ("completed", "uncertain", "pending"):
        old = tasks.by_request(key)
        assert old is not None and old["id"] == identities["tasks"][key]
        if pruned and key == "completed":
            assert old["result"] is None
            continue
        retried, created = tasks.reserve(*task_request(key))
        assert not created and retried["id"] == old["id"]
    for key, ident in identities["commands"].items():
        assert bus.submit(**bus_request(root, key))["command_id"] == ident
    for key, ident in identities["notifications"].items():
        assert notifications.notify("codex", "notice " + key, key)["id"] == ident


def assert_conflict(call: Callable[[], Any]) -> None:
    try:
        call()
    except ValueError:
        return
    raise AssertionError("Changed request unexpectedly accepted")


def assert_routing(root: Path) -> None:
    identities = json.loads((root / "identity.json").read_text())
    config_path = root / "bridge.json"
    loaded = Config.load(config_path)
    assert routing_digest(loaded) == identities["routing_digest"]
    assert BusConfig(config_path, 4222, "/nonexistent/nats").namespace == identities["namespace"]
    saved_path = root / "saved-config-identity.json"
    saved = json.loads(saved_path.read_text()) if saved_path.exists() else {}
    expected_hash = saved.get("sha256", identities["config_sha256"])
    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == expected_hash
    if saved:
        assert loaded.heartbeat_interval == saved["heartbeat_interval"]
        assert loaded.client_registrations == saved["client_registrations"]


def candidate_save(root: Path) -> dict[str, Any]:
    """Exercise an ordinary save; never restore the old file to fake rollback."""
    config_path = root / "bridge.json"
    before = config_path.read_bytes()
    config = Config.load(config_path)
    config.heartbeat_interval += 1
    config.client_registrations.append("claude")
    config.save(config_path)
    saved = {
        "original_sha256": hashlib.sha256(before).hexdigest(),
        "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "heartbeat_interval": config.heartbeat_interval,
        "client_registrations": config.client_registrations,
        "document": json.loads(config_path.read_text()),
    }
    assert saved["original_sha256"] != saved["sha256"]
    (root / "saved-config-identity.json").write_text(json.dumps(saved))
    configured_rex = Config.load(config_path)
    configured_rex.rex_enabled = True
    configured_rex.rex_server = "unix:///tmp/disposable-rex-does-not-exist.sock"
    configured_rex.rex_host_id = "fixture-host"
    configured_rex.select_provider("rex", True)
    configured_rex.save(root / "configured-rex.json")
    assert_routing(root)
    return saved


def old_rex_configuration_limit(root: Path) -> dict[str, str]:
    try:
        Config.load(root / "configured-rex.json")
    except (ValueError, TypeError):
        return {
            "explicit_rex_configuration": "unsupported by authentic 0.9",
            "rollback_prerequisite": "reviewed old-schema config restore or Rex disable migration",
            "baseline_configuration": "left as candidate-saved heartbeat/client document",
        }
    raise AssertionError("0.9 unexpectedly accepted explicitly configured Rex")


def seed(root: Path) -> dict[str, Any]:
    config_path = root / "bridge.json"
    Config(
        herdr_session=None,
        codex_enabled=False,
        kiro_workspaces={"fixture": "/project"},
        client_registrations=["codex"],
    ).save(config_path)
    identity: dict[str, Any] = {
        "routing_digest": routing_digest(Config.load(config_path)),
        "namespace": BusConfig(config_path, 4222, "/nonexistent/nats").namespace,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "tasks": {},
        "commands": {},
        "notifications": {},
    }
    (root / "identity.json").write_text(json.dumps(identity))
    tasks, bus, notifications = stores(root)
    for key in ("completed", "uncertain", "pending"):
        task, created = tasks.reserve(*task_request(key))
        assert created
        if key != "pending":
            tasks.update(task["id"], key, result="retained answer" if key == "completed" else None)
        identity["tasks"][key] = task["id"]
    with tasks.connection() as db:
        db.execute("UPDATE tasks SET created=1,updated=2")
    tasks.save_worker_session("fixture", "old-session", "/project", profile="herdr_bridge")
    for key in ("bound", "queued"):
        command = bus.submit(**bus_request(root, key))
        ident = command["command_id"]
        identity["commands"][key] = ident
        if key == "bound":
            assert bus.claim(ident, "kiro:bound")
            task, _ = tasks.reserve(
                "kiro:bound",
                "résumé bound",
                "bus:" + ident,
                {"kind": "kiro", "cwd": "/project"},
            )
            task = tasks.update(task["id"], "completed", result="bus answer")
            with tasks.connection() as db:
                db.execute("UPDATE tasks SET created=1,updated=2 WHERE id=?", (task["id"],))
            bus.bind(ident, tasks.get(task["id"]))
            for item in bus.pending_outbox():
                bus.mark_published(item["id"])
    for key in ("acknowledged", "queued"):
        notice = notifications.notify("codex", "notice " + key, key)
        identity["notifications"][key] = notice["id"]
        if key == "acknowledged":
            notifications.acknowledge("codex", [notice["id"]])
    (root / "identity.json").write_text(json.dumps(identity))
    before = snapshot(root)
    (root / "original-tables.json").write_text(json.dumps(before))
    assert_duplicates(root)
    return {"all_original_tables": before, "identity": identity}


def upgrade(root: Path) -> dict[str, Any]:
    stores(root)
    verify_snapshot(root, "original-tables.json")
    assert_routing(root)
    assert_duplicates(root)
    for suffix in ("", ".tasks", ".bus.store", ".client_mailbox", ".rex_mailbox", ".retention"):
        assert importlib.import_module("answerback_bridge" + suffix) is importlib.import_module(
            "codex_herdr_bridge" + suffix
        )
    for namespace in ("answerback_bridge", "codex_herdr_bridge"):
        resource = files(namespace)
        assert resource.joinpath("bus/assets/favicon.ico").read_bytes().startswith(b"\0\0\1\0")
        assert resource.joinpath("data/skills/answerback-collaboration/SKILL.md").is_file()
        assert resource.joinpath("data/skills/answerback-fleet/scripts/fleet.py").is_file()
    from answerback_bridge.rex_mailbox import RexMailbox

    mailbox = RexMailbox(TaskStore(root / "tasks.sqlite"))
    peer = mailbox.enroll(
        "fixture",
        {
            "endpoint": "unix:///tmp/disposable-rex-does-not-exist.sock",
            "server_instance_id": "fixture-server",
            "server_host_id": "fixture-host",
            "session_id": "session:11111111-1111-1111-1111-111111111111",
            "block_id": "block:22222222-2222-2222-2222-222222222222",
            "block_host_id": "fixture-host",
            "foreground_pid": 42,
            "foreground_name": "fixture",
            "foreground_path": None,
            "foreground_script": None,
            "foreground_cwd": "/project",
        },
        "rex-enrollment",
    )
    task = mailbox.submit(peer["peer_id"], "Rex work", "rex-work")
    mailbox.acknowledge(peer["peer_id"], task["id"])
    mailbox.complete(peer["peer_id"], task["id"], "Rex answer")
    (root / "rex-identity.json").write_text(json.dumps({"peer": peer, "task": task}))
    (root / "candidate-tables.json").write_text(json.dumps(snapshot(root)))
    return {
        "original_rows": "exact",
        "routing": "exact",
        "aliases_resources": "passed",
        "rex": "passed",
    }


def write_old(root: Path) -> dict[str, Any]:
    tasks, bus, notifications = stores(root)
    task, created = tasks.reserve(*task_request("old-write"))
    assert created
    bus.submit(**bus_request(root, "old-write"))
    notifications.notify("codex", "old notification write", "old-write")
    with tasks.connection() as db:
        assert len(db.execute("PRAGMA table_info(tasks)").fetchall()) == 11
    return {"old_positional_task_insert": "passed", "task_id": task["id"]}


def reupgrade(root: Path) -> dict[str, Any]:
    tasks, _, _ = stores(root)
    assert tasks.by_request("old-write") is not None
    assert_routing(root)
    assert_duplicates(root)
    from answerback_bridge.rex_mailbox import RexMailbox

    mailbox = RexMailbox(tasks)
    identity = json.loads((root / "rex-identity.json").read_text())
    assert mailbox.get(identity["peer"]["peer_id"]) == identity["peer"]
    assert (
        mailbox.submit(identity["peer"]["peer_id"], "Rex work", "rex-work")["id"]
        == (identity["task"]["id"])
    )
    # Age only disposable rows. Companion timestamps are intentionally aged too.
    for name, table in (
        ("tasks.sqlite", "tasks"),
        ("bus/ledger.sqlite", "commands"),
        ("client-mailbox.sqlite", "messages"),
    ):
        with sqlite3.connect(root / name) as db:
            if table == "messages":
                db.execute(
                    "UPDATE messages SET created=1,acknowledged=2 WHERE acknowledged IS NOT NULL"
                )
            elif table == "tasks":
                db.execute(f"UPDATE {table} SET created=1,updated=2")
            db.execute("UPDATE retention_records SET changed_at=3")
    return {"old_writes": "readable", "rex_rows": "exact", "old_table_columns": "unchanged"}


def retained(root: Path) -> dict[str, Any]:
    from answerback_bridge.config import public_task

    assert_routing(root)
    assert_duplicates(root, pruned=True)
    tasks, bus, notifications = stores(root)
    task = tasks.by_request("completed")
    assert (
        task is not None and public_task(task)["content_pruned"] and task["status"] == "completed"
    )
    assert task["result"] is None
    retried, created = tasks.reserve(*task_request("completed"))
    assert not created and retried["id"] == task["id"]
    ident = json.loads((root / "identity.json").read_text())["commands"]["bound"]
    assert bus.get(ident)["content_pruned"]
    assert_conflict(lambda: bus.command(ident))
    assert_conflict(lambda: tasks.reserve("kiro:completed", "changed", "completed"))
    assert_conflict(lambda: notifications.notify("codex", "changed", "acknowledged"))
    return {"duplicates": "retained", "content": "explicitly unavailable", "conflicts": "refused"}


async def mcp_projection(root: Path) -> dict[str, Any]:
    """Real installed MCP serialization over a store facade, without native providers."""
    from mcp import Client

    from answerback_bridge.config import public_task
    from answerback_bridge.mcp_server import create_server

    tasks = TaskStore(root / "tasks.sqlite")

    class StoreFacade:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "task_get"
            return public_task(tasks.get(params["task_id"]))

    async with Client(create_server(StoreFacade())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        required = {
            "task_get",
            "bus_submit",
            "bus_result",
            "rex_receive",
            "rex_ack",
            "rex_complete",
            "notification_status",
        }
        assert required <= tools.keys()
        assert not any(name.startswith("maintenance") for name in tools)
        annotations = tools["task_get"].annotations
        assert annotations is not None and annotations.read_only_hint is True
        ident = json.loads((root / "identity.json").read_text())["tasks"]["completed"]
        response = await client.call_tool("task_get", {"task_id": ident})
        assert not response.is_error, response
        receipt = response.structured_content
        assert receipt["id"] == ident and receipt["content_pruned"] and receipt["result"] is None
    return {
        "tool_inventory": sorted(required),
        "maintenance_interface": "CLI only",
        "task_get_pruned_result": "explicitly unavailable",
        "scope": "MCP/store serialization; no native provider or service delivery",
    }


def pruned_rollback(root: Path) -> dict[str, Any]:
    before = snapshot(root)
    tasks, bus, notifications = stores(root)
    task = tasks.by_request("completed")
    assert task is not None and task["status"] == "completed" and task["result"] is None
    assert_conflict(lambda: tasks.reserve(*task_request("completed")))
    assert_conflict(lambda: bus.submit(**bus_request(root, "bound")))
    assert_conflict(lambda: notifications.notify("codex", "notice acknowledged", "acknowledged"))
    assert snapshot(root) == before
    assert_routing(root)
    return {
        "pruned_duplicates": "refused without new work",
        "pruned_payloads": "old version lacks availability markers; no full-content claim",
        "tables": "exact",
    }


def main() -> None:
    phase, directory = sys.argv[1:]
    root = Path(directory)
    if phase == "snapshot":
        result = snapshot(root)
    elif phase == "rollback":
        verify_snapshot(root, "candidate-tables.json")
        assert_routing(root)
        assert_duplicates(root)
        result = {
            "candidate_tables": "exact",
            "routing": "exact",
            "duplicates": "retained",
            "saved_configuration": "exact candidate-saved bytes with intentional changes",
            "rex_configuration_limit": old_rex_configuration_limit(root),
        }
    elif phase == "mcp":
        result = asyncio.run(mcp_projection(root))
    else:
        result = {
            "seed": seed,
            "upgrade": upgrade,
            "candidate_save": candidate_save,
            "write_old": write_old,
            "reupgrade": reupgrade,
            "retained": retained,
            "pruned_rollback": pruned_rollback,
        }[phase](root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
