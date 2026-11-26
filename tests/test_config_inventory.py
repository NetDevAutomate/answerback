"""Read-only discovery and lossless, guarded archival of owned bridge configs."""

import asyncio
import errno
import importlib
import json
import os
import shutil
import socket
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from answerback_bridge.actors.store import ActorStore
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.tasks import TaskStore


def module() -> Any:
    assert importlib.util.find_spec("codex_herdr_bridge.config_inventory") is not None, (
        "Configuration inventory and archive are missing"
    )
    return importlib.import_module("codex_herdr_bridge.config_inventory")


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "herdr-bridge-old" / "config.json"
    Config(
        "retired-session",
        desktop_threads={"desk": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        kiro_workspaces={"worker": str(tmp_path.resolve() / "project")},
    ).save(path)
    runtime = path.parent / "runtime"
    runtime.mkdir()
    (runtime / "config-owner.json").write_text(json.dumps({"config_path": str(path)}))
    (runtime / "owner.lock").touch()
    store = TaskStore(path.parent / "tasks.sqlite")
    task, _ = store.reserve("herdr:old:worker", "Retained private prompt", "completed-one")
    store.update(task["id"], "completed", result="Retained private result")
    (runtime / "bridge.log").write_text("Retained old log\n")
    return path


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def move_folder(folder: Path, target: Path) -> None:
    """Move the fixture's folder to `target`, also onto another file system (T-139).

    A plain rename fails with OSError EXDEV when pytest's temporary folder and /tmp are on
    different file systems (a /tmp in memory and a --basetemp on a disk). shutil.move renames
    when it can and otherwise copies the tree with its modes and removes the original.
    """
    shutil.move(folder, target)


def test_move_folder_arrives_when_a_rename_would_cross_file_systems(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-139: red before this row's move (Path.rename raises the OSError), green with it."""
    source = config_path.parent
    before = snapshot(source)
    modes = {path.relative_to(source): path.stat().st_mode for path in source.rglob("*")}
    folder_mode = source.stat().st_mode
    assert before and modes

    def cross_device(src: object, dst: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(src), None, str(dst))

    monkeypatch.setattr(os, "rename", cross_device)
    target = tmp_path / "short" / "herdr-bridge-old"
    target.parent.mkdir()
    with pytest.raises(OSError) as raised:
        source.rename(target)
    assert raised.value.errno == errno.EXDEV
    assert source.is_dir() and not target.exists()

    move_folder(source, target)

    assert not source.exists()
    assert snapshot(target) == before
    assert {path.relative_to(target): path.stat().st_mode for path in target.rglob("*")} == modes
    assert target.stat().st_mode == folder_mode


async def test_actor_run_retention_is_rechecked_when_archiving(config_path: Path) -> None:
    store = ActorStore(config_path.parent / "bus/actors.sqlite")
    run_id = str(uuid4())
    created = store.create_run(run_id, str(config_path.parent), ["worker"])
    plan = await module().archive_config(config_path)
    assert plan["unresolved_actor_items"] == 1
    assert not plan["archive_eligible"]
    applied = await module().archive_config(config_path, apply=True)
    assert not applied["archived"] and await asyncio.to_thread(config_path.exists)
    store.finish_run(run_id, "No participant was launched", owner_token=created["owner_token"])
    assert (await module().archive_config(config_path))["archive_eligible"]


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
async def test_actor_journal_retains_config_without_recovery(
    config_path: Path, suffix: str
) -> None:
    path = config_path.parent / "bus/actors.sqlite"
    path.parent.mkdir()
    journal = path.with_name(path.name + suffix)
    journal.write_bytes(b"unrecovered")
    before = snapshot(config_path.parent)
    plan = await module().archive_config(config_path)
    assert not plan["archive_eligible"]
    assert any("actor" in reason.lower() for reason in plan["reasons"])
    assert snapshot(config_path.parent) == before


async def test_unknown_actor_schema_retains_config(config_path: Path) -> None:
    path = config_path.parent / "bus/actors.sqlite"
    path.parent.mkdir()
    path.write_bytes(b"unreadable")
    plan = await module().archive_config(config_path)
    assert not plan["archive_eligible"]
    assert plan["unresolved_actor_items"] is None


async def test_inventory_and_dry_run_preserve_every_file(config_path: Path) -> None:
    inventory = module()
    root = config_path.parent.parent
    before = snapshot(root)
    listed = await inventory.list_configs(root)
    assert listed["complete"] is True
    assert listed["errors"] == []
    assert len(listed["configs"]) == 1
    row = listed["configs"][0]
    assert row["config_path"] == str(config_path)
    assert row["session"] == "retired-session"
    assert row["workspace_aliases"] == {"desktop": ["desk"], "kiro": ["worker"]}
    assert row["modified_at"] > 0
    assert row["bridge_status"]["state"] == "stopped"
    assert row["unresolved_receipts"] == 0
    assert row["archive_eligible"] is False
    assert row["content_check"] == "not_run"
    assert "archive preview" in " ".join(row["reasons"]).lower()
    assert "Retained private" not in json.dumps(listed)
    plan = await inventory.archive_config(config_path)
    assert plan["archived"] is False
    assert plan["archive_eligible"] is True
    assert plan["content_check"] == "passed"
    assert Path(plan["destination"]).parent == root / "herdr-bridge-archives"
    assert "update" in plan["client_references"].lower()
    assert "original" in plan["restore_required"].lower()
    assert snapshot(root) == before


async def test_inventory_selects_only_immediate_recognized_directories(
    config_path: Path,
) -> None:
    root = config_path.parent.parent
    for name in ("herdr-bridge", "codex-herdr-bridge", "unrelated", "herdr-bridge-archives"):
        Config("fixture").save(root / name / "config.json")
    Config("nested").save(root / "unrelated" / "herdr-bridge-hidden" / "config.json")
    listed = await module().list_configs(root)
    assert {Path(row["config_path"]).parent.name for row in listed["configs"]} == {
        "herdr-bridge-old",
        "herdr-bridge",
        "codex-herdr-bridge",
    }
    selected = await module().list_configs(config_path.parent)
    assert [row["config_path"] for row in selected["configs"]] == [str(config_path)]


async def test_default_inventory_uses_xdg_root(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_path.parent.parent))
    result = await module().list_configs()
    assert result["root"] == str(config_path.parent.parent)
    assert result["configs"][0]["config_path"] == str(config_path)


async def test_missing_database_or_runtime_does_not_create_them(tmp_path: Path) -> None:
    root = await asyncio.to_thread(tmp_path.resolve)
    path = root / "herdr-bridge-unused" / "config.json"
    Config("unused").save(path)
    before = snapshot(tmp_path)
    result = await module().list_configs(root)
    assert result["configs"][0]["unresolved_receipts"] == 0
    assert result["configs"][0]["archive_eligible"] is False
    assert "ownership" in " ".join(result["configs"][0]["reasons"]).lower()
    assert snapshot(tmp_path) == before


async def test_apply_moves_whole_directory_with_original_history(config_path: Path) -> None:
    source = config_path.parent
    nested = source / "history" / "old"
    nested.mkdir(parents=True)
    (nested / "receipt.bin").write_bytes(b"\x00\xffhistorical\x00")
    before = snapshot(source)
    result = await module().archive_config(config_path, apply=True)
    assert result["archived"] is True
    destination = Path(result["destination"])
    assert not source.exists()
    assert snapshot(destination) == before
    assert json.loads((destination / "runtime" / "config-owner.json").read_text()) == {
        "config_path": str(config_path)
    }
    # The unchanged owner record deliberately requires restoration before a restart.
    from codex_herdr_bridge.process import BridgeError
    from codex_herdr_bridge.transport import check_config_owner

    with pytest.raises(BridgeError, match="another bridge"):
        check_config_owner(destination / "config.json")


@pytest.mark.parametrize(
    "status", ["pending", "queued", "submitted", "running", "blocked", "uncertain"]
)
async def test_unresolved_receipts_prevent_archive(config_path: Path, status: str) -> None:
    store = TaskStore(config_path.parent / "tasks.sqlite")
    task, _ = store.reserve("herdr:old:other", "Unfinished", "unfinished")
    if status != "pending":
        store.update(task["id"], status)
    before = snapshot(config_path.parent.parent)
    result = await module().archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert result["archive_eligible"] is False
    assert result["unresolved_receipts"] == 1
    assert "receipt" in " ".join(result["reasons"]).lower()
    assert snapshot(config_path.parent.parent) == before


@pytest.mark.parametrize("lock_name", ["runtime/owner.lock", "run.lock"])
async def test_held_service_or_live_test_lock_prevents_archive(
    config_path: Path, lock_name: str
) -> None:
    import fcntl

    lock_path = config_path.parent / lock_name
    lock_path.touch(exist_ok=True)
    before = snapshot(config_path.parent.parent)
    with lock_path.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = await module().archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert "lock" in " ".join(result["reasons"]).lower()
    assert result["bridge_status"]["running"] is (lock_name == "runtime/owner.lock")
    assert snapshot(config_path.parent.parent) == before


@pytest.mark.parametrize("location", ["config", "owner", "lock", "nested", "directory"])
async def test_symlink_paths_are_refused(config_path: Path, location: str) -> None:
    root = config_path.parent.parent
    target = root / "external"
    target.mkdir()
    (target / "untouched").write_text("external bytes")
    if location == "directory":
        selected = root / "herdr-bridge-linked"
        selected.symlink_to(config_path.parent, target_is_directory=True)
        config_path = selected / "config.json"
    else:
        path = {
            "config": config_path,
            "owner": config_path.parent / "runtime" / "config-owner.json",
            "lock": config_path.parent / "runtime" / "owner.lock",
            "nested": config_path.parent / "nested-link",
        }[location]
        if path.exists():
            path.unlink()
        path.symlink_to(target, target_is_directory=True)
    result = await module().archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert "symlink" in " ".join(result["reasons"]).lower()
    assert config_path.parent.exists()
    assert (target / "untouched").read_text() == "external bytes"
    assert not (root / "herdr-bridge-archives").exists()


@pytest.mark.parametrize("ownership", ["missing", "mismatch", "invalid"])
async def test_exact_owner_binding_is_required(config_path: Path, ownership: str) -> None:
    owner = config_path.parent / "runtime" / "config-owner.json"
    if ownership == "missing":
        owner.unlink()
    elif ownership == "mismatch":
        owner.write_text(json.dumps({"config_path": str(config_path.with_name("other.json"))}))
    else:
        owner.write_text("broken json")
    before = snapshot(config_path.parent.parent)
    result = await module().archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert "ownership" in " ".join(result["reasons"]).lower()
    assert snapshot(config_path.parent.parent) == before


async def test_unowned_file_is_refused(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = module()
    monkeypatch.setattr(inventory.os, "getuid", lambda: os.stat(config_path).st_uid + 1)
    result = await inventory.archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert "owned" in " ".join(result["reasons"]).lower()
    assert await asyncio.to_thread(config_path.exists)


@pytest.mark.parametrize("damage", ["corrupt", "unknown_status", "missing_status"])
async def test_receipt_read_errors_fail_closed(config_path: Path, damage: str) -> None:
    database = config_path.parent / "tasks.sqlite"
    if damage == "corrupt":
        database.write_bytes(b"not a database")
    else:
        with sqlite3.connect(database) as connection:
            if damage == "unknown_status":
                connection.execute("UPDATE tasks SET status='new-unknown-status'")
            else:
                connection.execute("ALTER TABLE tasks RENAME COLUMN status TO old_status")
    before = snapshot(config_path.parent.parent)
    result = await module().archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert result["unresolved_receipts"] is None
    assert "receipt" in " ".join(result["reasons"]).lower()
    assert snapshot(config_path.parent.parent) == before


async def test_unresponsive_socket_prevents_archive(config_path: Path) -> None:
    # A listener which accepts no requests is materially different from a stale socket.
    import tempfile

    # A short folder under /tmp: the path of runtime/bridge.sock must fit the length limit of
    # a Unix socket path (about 104 bytes on macOS, 108 on Linux), and pytest's own temporary
    # folder can be longer than that.
    with tempfile.TemporaryDirectory(prefix="hb-inventory-", dir="/tmp") as short:
        root = await asyncio.to_thread(Path(short).resolve)
        source = root / "herdr-bridge-old"
        move_folder(config_path.parent, source)
        path = source / "config.json"
        (source / "runtime" / "config-owner.json").write_text(
            json.dumps({"config_path": str(path)})
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(source / "runtime" / "bridge.sock"))
            listener.listen(1)
            result = await module().archive_config(path, apply=True)
            assert result["archived"] is False
            assert "status" in " ".join(result["reasons"]).lower()
            assert path.exists()


async def test_invalid_targets_are_refused(config_path: Path) -> None:
    inventory = module()
    for target in (Path("relative/config.json"), config_path.with_name("other.json")):
        result = await inventory.archive_config(target, apply=True)
        assert result["archived"] is False
        assert result["reasons"]
    (config_path.parent / ".git").mkdir()
    result = await inventory.archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert "project" in " ".join(result["reasons"]).lower()


async def test_archive_container_must_be_safe(config_path: Path) -> None:
    root = config_path.parent.parent
    external = root / "external"
    external.mkdir()
    (root / "herdr-bridge-archives").symlink_to(external, target_is_directory=True)
    result = await module().archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert "symlink" in " ".join(result["reasons"]).lower()
    assert list(external.iterdir()) == []


async def test_readonly_sqlite_never_migrates_schema(config_path: Path) -> None:
    database = config_path.parent / "tasks.sqlite"
    database.unlink()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE tasks (status TEXT NOT NULL)")
        connection.execute("INSERT INTO tasks VALUES ('completed')")
    before = database.read_bytes()
    result = await module().list_configs(config_path.parent)
    assert result["configs"][0]["unresolved_receipts"] == 0
    assert database.read_bytes() == before
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        assert [row[0] for row in connection.execute("SELECT name FROM sqlite_master")] == ["tasks"]


async def test_wal_receipts_are_never_ignored_or_create_sidecars(config_path: Path) -> None:
    database = config_path.parent / "tasks.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE tasks SET status='uncertain'")
        connection.commit()
        before = snapshot(config_path.parent.parent)
        result = await module().archive_config(config_path, apply=True)
        assert result["archived"] is False
        assert result["unresolved_receipts"] is None
        assert "WAL" in " ".join(result["reasons"])
        assert snapshot(config_path.parent.parent) == before


async def test_receipts_are_rechecked_after_exclusive_owner_lock(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = module()
    original_lock = inventory._lock
    attempts = 0

    def lock_and_change(path: Path) -> int:
        nonlocal attempts
        descriptor = original_lock(path)
        attempts += 1
        # First is the read-only preflight probe; second is the application lock.
        if attempts == 2:
            store = TaskStore(config_path.parent / "tasks.sqlite")
            store.reserve("herdr:old:new", "Still needs reconciliation", "new-receipt")
        return descriptor

    monkeypatch.setattr(inventory, "_lock", lock_and_change)
    result = await inventory.archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert result["unresolved_receipts"] == 1
    assert not (config_path.parent.parent / "herdr-bridge-archives").exists()


async def test_move_holds_owner_and_live_test_locks(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    inventory = module()
    (config_path.parent / "run.lock").touch()
    original_move = inventory._move

    def checked_move(source: Path, destination: Path) -> None:
        for lock in (source / "runtime" / "owner.lock", source / "run.lock"):
            with lock.open("rb") as handle:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original_move(source, destination)

    monkeypatch.setattr(inventory, "_move", checked_move)
    result = await inventory.archive_config(config_path, apply=True)
    assert result["archived"] is True
    with (Path(result["destination"]) / "runtime" / "owner.lock").open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


async def test_inventory_reports_unreadable_contents(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = module()
    hidden = config_path.parent / "history"
    hidden.mkdir()
    original_scandir = inventory.os.scandir

    def restricted_scandir(path: Any) -> Any:
        if Path(path) == hidden:
            raise PermissionError("History directory cannot be read")
        return original_scandir(path)

    monkeypatch.setattr(inventory.os, "scandir", restricted_scandir)
    result = await inventory.archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert "cannot be read" in " ".join(result["reasons"])


@pytest.mark.parametrize(
    "reply",
    [
        b"[]\n",
        b'{"version":1,"ok":true,"result":{"running":true,"accepting":true,"pid":123}}\n',
        b"",
    ],
)
async def test_active_or_invalid_status_responses_prevent_archive(
    config_path: Path, reply: bytes
) -> None:
    import tempfile

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        assert request["method"] == "status"
        assert request["params"] == {}
        writer.write(reply)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    # A short folder under /tmp: the path of runtime/bridge.sock must fit the length limit of
    # a Unix socket path (about 104 bytes on macOS, 108 on Linux), and pytest's own temporary
    # folder can be longer than that.
    with tempfile.TemporaryDirectory(prefix="hb-status-", dir="/tmp") as short:
        root = await asyncio.to_thread(Path(short).resolve)
        source = root / "herdr-bridge-old"
        move_folder(config_path.parent, source)
        path = source / "config.json"
        (source / "runtime" / "config-owner.json").write_text(
            json.dumps({"config_path": str(path)})
        )
        server = await asyncio.start_unix_server(answer, str(source / "runtime" / "bridge.sock"))
        async with server:
            result = await module().archive_config(path, apply=True)
        assert result["archived"] is False
        assert result["reasons"]
        assert source.is_dir()
        assert not (root / "herdr-bridge-archives").is_dir()


@pytest.mark.parametrize(
    ("field", "value"),
    [("desktop_threads", []), ("kiro_workspaces", None)],
)
async def test_malformed_config_shape_is_a_row_error_not_an_inventory_failure(
    config_path: Path, field: str, value: Any
) -> None:
    root = config_path.parent.parent
    damaged = root / "herdr-bridge-malformed" / "config.json"
    Config("bad-shape").save(damaged)
    data = json.loads(damaged.read_text())
    data[field] = value
    damaged.write_text(json.dumps(data))
    before = snapshot(root)
    listed = await module().list_configs(root)
    assert listed["complete"] is False
    rows = {row["config_path"]: row for row in listed["configs"]}
    assert rows[str(config_path)]["session"] == "retired-session"
    assert rows[str(damaged)]["errors"]
    assert rows[str(damaged)]["archive_eligible"] is False
    assert snapshot(root) == before


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
@pytest.mark.parametrize("kind", ["file", "symlink", "broken_symlink"])
async def test_receipt_sidecars_block_immutable_reads(
    config_path: Path, suffix: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = module()
    database = config_path.parent / "tasks.sqlite"
    sidecar = database.with_name(database.name + suffix)
    if kind == "file":
        sidecar.write_bytes(b"uncheckpointed receipt data")
    else:
        target = config_path.parent.parent / "receipt-sidecar-target"
        if kind == "symlink":
            target.write_bytes(b"uncheckpointed receipt data")
        sidecar.symlink_to(target)
    original_database = database.read_bytes()
    original_sidecar = sidecar.read_bytes() if kind == "file" else sidecar.readlink()

    def never_open(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A sidecar must be refused before opening SQLite")

    monkeypatch.setattr(inventory.sqlite3, "connect", never_open)
    result = await inventory.list_configs(config_path.parent)
    row = result["configs"][0]
    assert result["complete"] is False
    assert row["unresolved_receipts"] is None
    assert row["archive_eligible"] is False
    assert "receipt" in " ".join(row["errors"]).lower()
    assert database.read_bytes() == original_database
    assert (sidecar.read_bytes() if kind == "file" else sidecar.readlink()) == original_sidecar
    assert not database.with_name(database.name + "-shm").exists()


async def test_list_does_not_traverse_configuration_workspaces(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = module()
    (config_path.parent / "workspace" / "node_modules" / "dependency").mkdir(parents=True)
    original_scandir = inventory.os.scandir
    scans: list[Path] = []
    root = config_path.parent.parent

    def immediate_root_only(path: Any) -> Any:
        scans.append(Path(path))
        assert Path(path) == root, "Listing must not walk configuration contents"
        return original_scandir(path)

    monkeypatch.setattr(inventory.os, "scandir", immediate_root_only)
    result = await inventory.list_configs(root)
    assert result["complete"] is True
    assert result["configs"][0]["content_check"] == "not_run"
    assert result["configs"][0]["archive_eligible"] is False
    assert scans == [root]


@pytest.mark.parametrize("interruption", ["limit", "read_error"])
async def test_root_scan_interruption_retains_discovered_rows_and_marks_partial(
    config_path: Path, interruption: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    inventory = module()
    root = config_path.parent.parent
    (root / "unrelated-one").mkdir()
    (root / "unrelated-two").mkdir()
    entries = sorted(root.iterdir(), key=lambda path: path != config_path.parent)
    original_scandir = inventory.os.scandir
    inspected: list[str] = []

    @contextmanager
    def ordered_root(path: Any) -> Any:
        if Path(path) != root:
            with original_scandir(path) as scan:
                yield scan
            return
        with original_scandir(root) as scan:
            by_name = {entry.name: entry for entry in scan}

        def bounded_entries() -> Any:
            for candidate in entries:
                if interruption == "read_error" and inspected:
                    raise PermissionError("Remaining root entries cannot be read")
                inspected.append(candidate.name)
                assert len(inspected) <= 3, "Inventory scanned beyond its limit and lookahead"
                yield by_name[candidate.name]

        yield bounded_entries()

    monkeypatch.setattr(inventory, "MAX_ROOT_ENTRIES", 2, raising=False)
    monkeypatch.setattr(inventory.os, "scandir", ordered_root)
    result = await inventory.list_configs(root)
    assert result["complete"] is False
    assert [row["config_path"] for row in result["configs"]] == [str(config_path)]
    assert ("limit" if interruption == "limit" else "cannot be read") in " ".join(
        result["errors"]
    ).lower()
    assert len(inspected) == (3 if interruption == "limit" else 1)


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("bound", ["entries", "depth"])
async def test_archive_contents_limits_fail_closed_without_writes(
    config_path: Path, bound: str, apply: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = module()
    if bound == "entries":
        monkeypatch.setattr(inventory, "MAX_CONTENT_ENTRIES", 2, raising=False)
    else:
        (config_path.parent / "workspace" / "node_modules" / "dependency").mkdir(parents=True)
        monkeypatch.setattr(inventory, "MAX_CONTENT_DEPTH", 2, raising=False)
    before = snapshot(config_path.parent.parent)
    result = await inventory.archive_config(config_path, apply=apply)
    assert result["archived"] is False
    assert result["archive_eligible"] is False
    assert result["content_check"] == "failed"
    assert "limit" in " ".join(result["reasons"]).lower()
    assert snapshot(config_path.parent.parent) == before


async def test_archive_rechecks_contents_after_acquiring_owner_lock(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = module()
    original_lock = inventory._lock
    attempts = 0

    def lock_and_change(path: Path) -> int:
        nonlocal attempts
        descriptor = original_lock(path)
        attempts += 1
        if attempts == 2:
            (config_path.parent / "late-link").symlink_to("missing-target")
        return descriptor

    monkeypatch.setattr(inventory, "_lock", lock_and_change)
    result = await inventory.archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert result["content_check"] == "failed"
    assert "symlink" in " ".join(result["reasons"]).lower()
    assert not (config_path.parent.parent / "herdr-bridge-archives").exists()


async def test_reusable_live_test_environment_is_retained(config_path: Path) -> None:
    (config_path.parent / "environment.json").write_text("{}")
    result = await module().archive_config(config_path, apply=True)
    assert result["archived"] is False
    assert "reusable live-test" in " ".join(result["reasons"]).lower()
    assert await asyncio.to_thread(config_path.exists)


async def test_core_only_configuration_has_verifiable_runtime(config_path: Path) -> None:
    Config(hc_enabled=True, codex_enabled=False).save(config_path)
    result = await module().list_configs(config_path.parent)
    row = result["configs"][0]
    assert result["complete"] is True
    assert row["session"] is None
    assert row["bridge_status"]["state"] == "stopped"
