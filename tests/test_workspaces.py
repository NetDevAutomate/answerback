import asyncio
from pathlib import Path
from typing import Any

import pytest

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.tasks import TaskStore


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from codex_herdr_bridge.workspaces import Workspaces

    monkeypatch.setattr("codex_herdr_bridge.workspaces.shutil.which", lambda value: "/bin/echo")
    config = Config("test")
    return Workspaces(config, tmp_path / "config.json", TaskStore(tmp_path / "tasks.sqlite"))


async def test_add_existing_workspace_persists_without_starting_agent(
    registry: Any, tmp_path: Path
) -> None:
    result = await registry.add("project", str(tmp_path))
    assert result["peer_id"] == "kiro:project"
    assert Config.load(registry.config_path).kiro_workspaces == {"project": str(tmp_path)}
    assert registry.store.counts() == {}
    assert registry.store.worker_session("project") is None


async def test_creation_is_explicit_and_idempotent(registry: Any, tmp_path: Path) -> None:
    directory = tmp_path / "new-project"
    with pytest.raises(ValueError, match="create"):
        await registry.add("project", str(directory))
    assert not directory.exists()
    await registry.add("project", str(directory), create=True)
    assert directory.is_dir()
    await registry.add("project", str(directory), create=True)
    assert len(registry.config.kiro_workspaces) == 1


async def test_missing_binary_never_creates_directory(
    registry: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("codex_herdr_bridge.workspaces.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="executable"):
        await registry.add("project", str(tmp_path / "missing"), create=True)
    assert not (tmp_path / "missing").exists()


async def test_list_identifies_missing_workspace(registry: Any, tmp_path: Path) -> None:
    registry.config.kiro_workspaces["gone"] = str(tmp_path / "missing")
    item = registry.list()["workspaces"][0]
    assert item["name"] == "gone" and item["exists"] is False


async def test_remove_preview_preserves_config_and_files(registry: Any, tmp_path: Path) -> None:
    await registry.add("project", str(tmp_path))
    original = registry.config_path.read_bytes()
    plan = registry.remove("project")
    assert plan["can_remove"] is True and plan["applied"] is False
    assert registry.config_path.read_bytes() == original
    assert registry.config.kiro_workspaces["project"] == str(tmp_path)


async def test_remove_retains_history_and_files_but_releases_local_alias_binding(
    registry: Any, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "important.txt").write_text("keep")
    await registry.add("project", str(project))
    registry.store.save_worker_session("project", "saved-session", str(project))
    result = registry.remove("project", apply=True)
    assert result["applied"] is True
    backup_text = await asyncio.to_thread(Path(result["backup"]).read_text)
    assert "saved-session" in backup_text
    assert registry.config.kiro_workspaces == {}
    assert registry.store.worker_session("project") is None
    assert (project / "important.txt").read_text() == "keep"


@pytest.mark.parametrize(
    "status", ["pending", "queued", "submitted", "running", "blocked", "uncertain"]
)
async def test_remove_refuses_every_unresolved_task(
    registry: Any, tmp_path: Path, status: str
) -> None:
    await registry.add("project", str(tmp_path))
    receipt, _ = registry.store.reserve(
        "kiro:project", "Work", "work", {"kind": "kiro", "alias": "project", "cwd": str(tmp_path)}
    )
    if status != "pending":
        registry.store.update(receipt["id"], status)
    assert registry.remove("project")["can_remove"] is False
    with pytest.raises(ValueError, match="unfinished"):
        registry.remove("project", apply=True)
    assert registry.config.kiro_workspaces


async def test_alias_cannot_silently_move_to_another_project(registry: Any, tmp_path: Path) -> None:
    await registry.add("project", str(tmp_path))
    other = tmp_path / "other"
    with pytest.raises(ValueError, match="different"):
        await registry.add("project", str(other), create=True)
    assert not other.exists()


async def test_adding_project_preserves_enabled_crew_and_port(
    registry: Any, tmp_path: Path
) -> None:
    registry.config.crew_enabled = True
    registry.config.crew_port = 5476
    await registry.add("project", str(tmp_path))
    restored = Config.load(registry.config_path)
    assert restored.crew_enabled is True
    assert restored.crew_port == 5476


async def test_workspace_commands_set_and_show_provider_active_budget(
    registry: Any, tmp_path: Path
) -> None:
    await registry.add("project", str(tmp_path), active_budget=601)
    assert Config.load(registry.config_path).kiro_active_budget == 601
    assert registry.list()["active_execution_budget"] == 601
