"""Explicit provider selection, independent of client connections and availability."""

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from .adapters.catalog import definition
from .config import PROVIDERS, Config
from .tasks import TERMINAL, TaskStore


class ProviderRegistry:
    def __init__(self, config: Config, path: Path, store: TaskStore) -> None:
        self.config, self.path, self.store = config, path, store

    def snapshot(self) -> dict[str, Any]:
        return {
            "providers": [
                {"provider": name, "registered": self.config.provider_enabled(name)}
                for name in PROVIDERS
            ]
        }

    def apply(self, candidate: Config) -> dict[str, Any]:
        """Persist ``candidate`` and make it the live configuration, in place.

        Existing adapters retain this data object's identity: mutate in place here, never
        ``self.config = replace(...)``. Every holder of this Config (the adapters and workers
        constructed with it, see Config's own docstring) keeps a long-lived reference to
        this exact object; reassigning ``self.config`` would silently give them a stale copy
        they never see another update to. This method is the only sanctioned way to apply a
        persisted change; a provider plugin that edits its own block calls it too.
        """
        candidate = replace(candidate)
        candidate.save(self.path)
        self.config.__dict__.update(candidate.__dict__)
        return self.snapshot()

    _save = apply

    def register(
        self,
        provider: str,
        binary: str | None = None,
        session: str | None = None,
        port: int | None = None,
    ) -> dict[str, Any]:
        if provider not in PROVIDERS:
            raise ValueError("Unknown provider")
        if provider == "rex":
            raise ValueError("Rex registration requires probe and binding; use rex-configure")
        if port is not None and provider != "crew":
            raise ValueError("--port is only for Crew")
        if session is not None and provider not in ("hc", "herdr"):
            raise ValueError("--session is only for core or Herdr TUI")
        entry = definition(provider)
        binary_field = entry.registration_binary_field if entry is not None else "binary"
        if binary_field is not None:
            field = f"{provider}_{binary_field}"
            resolved = shutil.which(binary or getattr(self.config, field))
            if resolved is None:
                raise ValueError("Selected provider executable is unavailable")
            candidate = replace(self.config, **{field: resolved})
        elif binary is not None:
            raise ValueError("Selected provider does not accept --binary")
        else:
            field = None
            candidate = replace(self.config)
        candidate.select_provider(provider, True)
        if provider == "herdr":
            candidate.herdr_session = session or candidate.herdr_session
            if not candidate.herdr_session:
                raise ValueError("Herdr TUI registration needs --session")
        elif provider == "hc":
            candidate.hc_enabled = True
            if session is not None:
                candidate.hc_session = session
        elif provider == "crew":
            candidate.crew_enabled = True
            # Explicit registration chooses one gateway; do not guess via CLI
            # launcher work each time the gateway is absent.
            candidate.crew_port = port if port is not None else (candidate.crew_port or 5476)
        elif provider == "codex":
            candidate.codex_enabled = True
        settings = tuple(
            key for key in (field, "herdr_session", "hc_session", "crew_port") if key is not None
        )
        if any(getattr(candidate, key) != getattr(self.config, key) for key in settings):
            self.ensure_idle(provider)
        return self.apply(candidate)

    def has_unfinished_work(self, provider: str, *, thread_id: str | None = None) -> bool:
        """Whether `provider` has a receipt that is not in a terminal state.

        `thread_id`, when given, additionally requires the row's own metadata thread_id to
        match, which scopes the answer to one registered thread of a provider instead of
        every thread. A provider that reports unfinished work as a result instead of
        raising asks this directly; every other caller goes through `ensure_idle`.
        """
        with self.store.connection() as connection:
            placeholders = ",".join("?" for _ in TERMINAL)
            rows = connection.execute(
                f"SELECT metadata FROM tasks WHERE status NOT IN ({placeholders})",
                TERMINAL,
            ).fetchall()
        for row in rows:
            metadata = json.loads(row["metadata"])
            kind = metadata.get("kind")
            if ("codex" if kind == "desktop" else kind) != provider:
                continue
            if thread_id is not None and metadata.get("thread_id") != thread_id:
                continue
            return True
        return False

    def ensure_idle(self, provider: str, *, thread_id: str | None = None) -> None:
        """Refuse while `provider` has unfinished work (see has_unfinished_work).

        A provider that re-registers one thread whose own routing identity changed passes
        `thread_id`, to scope the check to that thread. Every other caller (register()'s
        and unregister()'s provider-wide checks, a provider's new-binary check) omits it
        and keeps the unscoped scan.
        """
        if self.has_unfinished_work(provider, thread_id=thread_id):
            raise ValueError("Provider has unfinished work; finish or reconcile its receipts")

    _ensure_idle = ensure_idle

    def unregister(self, provider: str) -> dict[str, Any]:
        if provider not in PROVIDERS:
            raise ValueError("Unknown provider")
        self.ensure_idle(provider)
        candidate = replace(self.config)
        candidate.select_provider(provider, False)
        return self.apply(candidate)
