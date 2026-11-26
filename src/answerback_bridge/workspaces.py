"""Named native CLI project directories; no native agent or project-data deletion."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import WORKSPACE_PROVIDERS, Config, atomic_write
from .tasks import TaskStore


class Workspaces:
    def __init__(
        self, config: Config, config_path: Path, store: TaskStore, *, provider: str = "kiro"
    ) -> None:
        if provider not in WORKSPACE_PROVIDERS:
            raise ValueError("Unknown workspace provider")
        self.provider = provider
        self.config = config
        self.config_path = config_path
        self.store = store

    @property
    def entries(self) -> dict[str, str]:
        return getattr(self.config, f"{self.provider}_workspaces")

    def session_key(self, alias: str) -> str:
        return alias if self.provider == "kiro" else f"{self.provider}:{alias}"

    def _activity(self, alias: str) -> tuple[Sequence[str], float | None]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id,status,updated FROM tasks WHERE json_extract(metadata,'$.kind')=? "
                "AND json_extract(metadata,'$.alias')=?",
                (self.provider, alias),
            ).fetchall()
        from .tasks import TERMINAL

        return (
            [row["id"] for row in rows if row["status"] not in TERMINAL],
            max((row["updated"] for row in rows), default=None),
        )

    def list(self) -> dict[str, Any]:
        entries = []
        for alias, cwd in sorted(self.entries.items()):
            active, used = self._activity(alias)
            entries.append(
                {
                    "name": alias,
                    "peer_id": f"{self.provider}:{alias}",
                    "project": cwd,
                    "exists": Path(cwd).is_dir(),
                    "last_used": used,
                    "outstanding_task_ids": active,
                    "saved_session": self.store.worker_session(self.session_key(alias)) is not None,
                }
            )
        return {
            "workspaces": entries,
            "config_path": str(self.config_path),
            "active_execution_budget": getattr(self.config, f"{self.provider}_active_budget"),
        }

    async def add(
        self,
        alias: str,
        cwd: str,
        *,
        binary: str | None = None,
        create: bool = False,
        home: str | None = None,
        active_budget: int | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", alias):
            raise ValueError("Use a lowercase workspace alias of at most 32 characters")
        path = await asyncio.to_thread(lambda: Path(cwd).expanduser().resolve())
        resolved_home = (
            await asyncio.to_thread(lambda: str(Path(home).expanduser().resolve()))
            if home is not None
            else None
        )
        resolved = shutil.which(binary or getattr(self.config, f"{self.provider}_binary"))
        if resolved is None:
            raise ValueError(f"{self.provider} executable is unavailable")
        if home is not None and self.provider == "kiro":
            raise ValueError("--home is supported for the new native CLI providers only")
        settings: dict[str, Any] = {f"{self.provider}_binary": resolved}
        if resolved_home is not None:
            settings[f"{self.provider}_home"] = resolved_home
        if active_budget is not None:
            settings[f"{self.provider}_active_budget"] = active_budget
        if any(getattr(self.config, key) != value for key, value in settings.items()):
            if any(self._activity(name)[0] for name in self.entries):
                raise ValueError("Provider has unfinished work; configuration cannot change")
        previous = self.entries.get(alias)
        saved = self.store.worker_session(self.session_key(alias))
        if (previous and previous != str(path)) or (saved and saved["cwd"] != str(path)):
            raise ValueError(
                "Alias uses a different project; remove it explicitly or choose a new name"
            )
        if not path.is_dir():
            if not create:
                raise ValueError("Workspace directory is missing; use --create to create it")
            # From this check through the config save no other service task can retarget the alias.
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        candidate = replace(
            self.config,
            **{
                f"{self.provider}_workspaces": {**self.entries, alias: str(path)},
                **settings,
            },
        )
        candidate.select_provider(self.provider, True)
        candidate.save(self.config_path)
        setattr(
            self.config,
            f"{self.provider}_workspaces",
            getattr(candidate, f"{self.provider}_workspaces"),
        )
        for key, value in settings.items():
            setattr(self.config, key, value)
        self.config.provider_selection = candidate.provider_selection
        return {
            "peer_id": f"{self.provider}:{alias}",
            "project": str(path),
            "kind": "bridge_managed",
        }

    def remove(self, alias: str, *, apply: bool = False) -> dict[str, Any]:
        if alias not in self.entries:
            raise ValueError("Unknown workspace alias")
        active, _ = self._activity(alias)
        cwd = self.entries[alias]
        result = {
            "name": alias,
            "project": cwd,
            "can_remove": not active,
            "applied": False,
            "outstanding_task_ids": active,
            "retained": [
                "project directory and files",
                "native conversation history",
                "task receipts",
            ],
        }
        if not apply:
            return result
        if active:
            raise ValueError("Workspace has unfinished work; inspect its receipts before removal")
        saved = self.store.worker_session(self.session_key(alias))
        backup = (
            self.config_path.parent
            / "workspace-backups"
            / f"{time.time_ns()}-{self.provider}-{alias}.json"
        )
        atomic_write(
            backup,
            json.dumps({"alias": alias, "project": cwd, "saved_session": saved}, indent=2) + "\n",
        )
        remaining = {name: path for name, path in self.entries.items() if name != alias}
        candidate = replace(self.config, **{f"{self.provider}_workspaces": remaining})
        candidate.save(self.config_path)
        setattr(self.config, f"{self.provider}_workspaces", remaining)
        # Remove only the resume pointer. Native history and ownership records remain.
        with self.store.connection() as db:
            db.execute("DELETE FROM worker_sessions WHERE alias=?", (self.session_key(alias),))
        return {**result, "applied": True, "backup": str(backup)}
