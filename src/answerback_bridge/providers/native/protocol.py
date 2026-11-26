"""Small transport contract for bridge-owned native CLI sessions."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal, Protocol, Self

UpdateCallback = Callable[[dict[str, Any]], Awaitable[None]]
PermissionCallback = Callable[[dict[str, Any]], Awaitable[str | None]]


@dataclass(frozen=True)
class NativeResult:
    """A correlated native terminal event, separately from available response text."""

    session_id: str
    status: Literal["completed", "failed", "cancelled"]
    text: str | None
    turn_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class SessionControl(Protocol):
    @property
    def closed(self) -> bool:
        """Whether the owned transport already reports itself closed or disconnected."""
        ...

    @property
    def native_stopped(self) -> bool | None:
        """Whether cleanup confirmed stopping the owned native process or process group."""
        ...

    async def cancel(self, session_id: str) -> None: ...


class NativeTransport(SessionControl, Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None: ...

    async def open_session(
        self,
        session_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str: ...

    async def prompt(self, session_id: str, message: str, *, prompt_id: str) -> NativeResult: ...
