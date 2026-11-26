"""Provider contract. Native records are immutable snapshots, opaque to orchestration."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from ..subjects import validate_subject

TaskStatus = Literal[
    "pending",
    "queued",
    "submitted",
    "running",
    "blocked",
    "uncertain",
    "completed",
    "failed",
    "abandoned",
    "cancelled",
]
STATUSES = frozenset(get_args(TaskStatus))
Guard = Callable[[], None]


def snapshot(record: Mapping[str, Any]) -> str:
    try:
        return json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Adapter record must be a JSON object") from error


def required_text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Adapter record requires a nonempty {key}")
    return value


@dataclass(frozen=True, slots=True)
class Peer:
    _json: str = field(repr=False)
    peer_id: str = field(init=False)
    provider: str = field(init=False)
    target: str = field(init=False)
    project: str = field(init=False)
    kind: str = field(init=False)

    def __post_init__(self) -> None:
        record = self.as_record()
        for key in ("peer_id", "provider", "target"):
            object.__setattr__(self, key, required_text(record, key))
        for key in ("project", "kind"):
            value = record.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"Adapter peer {key} must be text")
            object.__setattr__(self, key, value)
        if not self.peer_id.startswith(self.provider + ":"):
            raise ValueError("Peer identity conflicts with provider")

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Peer":
        return cls(snapshot(record))

    def as_record(self) -> dict[str, Any]:
        """Compatibility/native boundary only; each call returns a fresh copy."""
        value = json.loads(self._json)
        if not isinstance(value, dict):
            raise ValueError("Adapter peer must be an object")
        return value


@dataclass(frozen=True, slots=True)
class PreparedPeer:
    target_key: str
    peer: Peer

    def __post_init__(self) -> None:
        if (
            not isinstance(self.target_key, str)
            or not self.target_key
            or not isinstance(self.peer, Peer)
        ):
            raise ValueError("Preparation requires an immutable peer and target key")

    @property
    def key(self) -> str:
        return self.target_key


@dataclass(frozen=True, slots=True)
class Submission:
    message: str
    request_id: str
    # What the sender says the hand-off is about. An adapter passes it to
    # `TaskStore.reserve(..., subject=)`, so the receipt holds it; the router refuses a
    # receipt whose subject differs from it.
    subject: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Submission message is required")
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("Submission request ID is required")
        validate_subject(self.subject)


@dataclass(frozen=True, slots=True)
class TaskReceipt:
    _json: str = field(repr=False)
    task_id: str = field(init=False)
    request_id: str = field(init=False)
    target: str = field(init=False)
    provider: str = field(init=False)
    status: TaskStatus = field(init=False)

    def __post_init__(self) -> None:
        record = self.as_record()
        for attr, key in (("task_id", "id"), ("request_id", "request_key"), ("target", "target")):
            object.__setattr__(self, attr, required_text(record, key))
        state = record.get("status")
        if not isinstance(state, str) or state not in STATUSES:
            raise ValueError("Invalid adapter receipt status")
        object.__setattr__(self, "status", state)
        metadata = record.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Receipt metadata must be an object")
        provider = metadata.get("kind", "")
        if not isinstance(provider, str):
            raise ValueError("Invalid receipt provider")
        object.__setattr__(self, "provider", provider)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TaskReceipt":
        return cls(snapshot(record))

    def as_record(self) -> dict[str, Any]:
        value = json.loads(self._json)
        if not isinstance(value, dict):
            raise ValueError("Adapter receipt must be an object")
        return value


@dataclass(frozen=True, slots=True)
class DiscoveryPage:
    peers: tuple[Peer, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()
    cursors: tuple[tuple[str, str], ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        peers = tuple(self.peers)
        if any(not isinstance(peer, Peer) for peer in peers):
            raise ValueError("Discovery requires immutable peers")
        object.__setattr__(self, "peers", peers)
        for key in ("errors", "cursors", "excluded"):
            pairs = tuple(tuple(pair) for pair in getattr(self, key))
            if any(
                len(pair) != 2 or any(not isinstance(value, str) for value in pair)
                for pair in pairs
            ):
                raise ValueError("Discovery diagnostics must be text pairs")
            object.__setattr__(self, key, pairs)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "DiscoveryPage":
        return cls(
            tuple(Peer.from_record(peer) for peer in record.get("peers", [])),
            tuple(record.get("errors", {}).items()),
            tuple(record.get("cursors", {}).items()),
            tuple(record.get("excluded", {}).items()),
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "peers": [peer.as_record() for peer in self.peers],
            "complete": not self.errors and not self.excluded,
            "errors": dict(self.errors),
            **({"cursors": dict(self.cursors)} if self.cursors else {}),
            **({"excluded": dict(self.excluded)} if self.excluded else {}),
        }


class DiscoverySource(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def aliases(self) -> tuple[str, ...]: ...
    def enabled(self) -> bool: ...
    async def discover(self) -> DiscoveryPage: ...
    async def resolve_exact(self, selector: str) -> Peer | None: ...


class TaskControls(Protocol):
    def running(self, task_id: str) -> bool: ...
    def permissions(self, task_id: str) -> dict[str, Any]: ...
    def answer(self, task_id: str, permission_id: str, option_id: str) -> dict[str, Any]: ...
    async def cancel(self, task_id: str) -> dict[str, Any]: ...


@runtime_checkable
class TaskReconciler(Protocol):
    """Optional explicit native-evidence recovery; it never submits a new prompt."""

    async def reconcile(self, task_id: str) -> dict[str, Any]: ...


class ProviderAdapter(DiscoverySource, Protocol):
    @property
    def controls(self) -> TaskControls | None: ...
    @property
    def recovery_states(self) -> tuple[str, ...]: ...
    async def prepare(self, peer: Peer) -> PreparedPeer: ...
    async def submit(
        self, prepared: PreparedPeer, submission: Submission, *, guard: Guard
    ) -> TaskReceipt: ...
    async def observe(self, receipt: TaskReceipt, *, refresh: bool) -> TaskReceipt: ...
    def previous(self, selector: str, submission: Submission) -> TaskReceipt | None: ...
    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool: ...
    def poll_ids(self, limit: int) -> tuple[str, ...]: ...
    async def close(self) -> None: ...
