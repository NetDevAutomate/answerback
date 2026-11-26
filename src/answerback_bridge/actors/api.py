"""Capability-bound actor operations over the shared local service."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..bus.config import BusConfig
from .model import MAX_RECEIVE, ActorAuth, ActorError, unique_object
from .store import ActorStore

BINDING_LIMIT = 4096
AUTH_FIELDS = {"run_id", "actor_id", "epoch", "credential", "frontend_id"}


def load_binding(path: Path) -> ActorAuth:
    """Read an owner-only capability once, using the opened file's metadata."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > BINDING_LIMIT
        ):
            raise ActorError("binding_invalid")
        raw = os.read(descriptor, BINDING_LIMIT + 1)
        if len(raw) > BINDING_LIMIT:
            raise ActorError("binding_invalid")
        value = json.loads(raw, object_pairs_hook=unique_object)
        if (
            not isinstance(value, dict)
            or set(value) != (AUTH_FIELDS - {"frontend_id"}) | {"schema_version"}
            or type(value["schema_version"]) is not int
            or value.pop("schema_version") != 1
        ):
            raise ActorError("binding_invalid")
        return ActorAuth(**value, frontend_id=str(uuid4()))
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ActorError("binding_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class ActorAPI:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser().resolve()
        self.path = self.config_path.parent / "bus/actors.sqlite"

    def available_store(self) -> ActorStore | None:
        """An unconfigured actor lane never creates files as a side effect of discovery."""
        if not ActorStore.has_runs(self.path):
            return None
        return ActorStore(self.path)

    def store(self) -> ActorStore:
        try:
            BusConfig.load(self.config_path)
            store = self.available_store()
        except (FileNotFoundError, ValueError):
            raise ActorError("unavailable") from None
        if store is None:
            raise ActorError("unavailable")
        return store

    @staticmethod
    def auth(value: dict[str, Any]) -> ActorAuth:
        if not isinstance(value, dict) or set(value) != AUTH_FIELDS:
            raise ActorError("invalid_request")
        try:
            return ActorAuth(**value)
        except (TypeError, ValueError):
            raise ActorError("invalid_request") from None

    def _call(
        self, operation: Callable[..., dict[str, Any]], auth: dict[str, Any], **params: Any
    ) -> dict[str, Any]:
        try:
            return operation(self.auth(auth), **params)
        except ActorError:
            raise
        except (ValueError, TypeError, KeyError):
            raise ActorError("invalid_request") from None

    def send(
        self, auth: dict[str, Any], to_actor: str, request_id: str, kind: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._call(
            self.store().send,
            auth,
            to_actor=to_actor,
            request_id=request_id,
            kind=kind,
            body=body,
        )

    async def receive(
        self,
        auth: dict[str, Any],
        limit: int = MAX_RECEIVE,
        wait_seconds: int = 0,
        *,
        hold: Callable[[ActorAuth], AbstractContextManager[None]] | None = None,
    ) -> dict[str, Any]:
        """Read this actor's offered messages, waiting up to `wait_seconds` for one.

        `hold`, when given, is entered once the first poll has authenticated the caller and
        found nothing to return; the service uses it to bound one actor's waiting receives.
        """
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 20:
            raise ActorError("invalid_request")
        # Every store read is synchronous SQLite: each runs in a worker thread so a waiting
        # receive (it polls every 100 ms) never blocks the event loop that serves the rest.
        store = await asyncio.to_thread(self.store)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds

        async def poll() -> dict[str, Any]:
            return await asyncio.to_thread(self._call, store.receive, auth, limit=limit)

        def settled(result: dict[str, Any]) -> bool:
            return bool(
                result["messages"]
                or result["run_state"] not in {"staging", "sealed"}
                or deadline - loop.time() <= 0
            )

        result = await poll()
        if settled(result):
            return result
        with hold(self.auth(auth)) if hold is not None else nullcontext():
            while not settled(result):
                await asyncio.sleep(max(0.0, min(0.1, deadline - loop.time())))
                result = await poll()
        return result

    def acknowledge(
        self, auth: dict[str, Any], message_id: str, receipt_nonce: str
    ) -> dict[str, Any]:
        return self._call(
            self.store().acknowledge, auth, message_id=message_id, receipt_nonce=receipt_nonce
        )

    def reply(
        self, auth: dict[str, Any], reply_to: str, request_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._call(
            self.store().reply, auth, reply_to=reply_to, request_id=request_id, body=body
        )
