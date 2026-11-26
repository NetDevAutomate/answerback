"""Bounded actor identities and the separate actor-message wire envelope."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from ..plugin import ModuleError

MAX_BODY_BYTES = 16 * 1024
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_RECEIVE = 8
KINDS = frozenset(
    {
        "assignment",
        "review_request",
        "review_reply",
        "reply",
        "artifact_ready",
        "correction",
        "finish",
        "actor_result",
    }
)
ERROR_MESSAGES = {
    "invalid_request": "Actor request is invalid",
    "unavailable": "Actor messaging is unavailable",
    "capacity": "Actor capacity limit reached",
    "binding_invalid": "Actor binding is invalid or no longer active",
    "run_unsealed": "Actor run must be sealed before work",
    "run_expired": "Actor run has expired",
    "run_finished": "Actor run is finished",
    "auth_invalid": "Invalid actor credential or identity",
    "frontend_mismatch": "Actor epoch is bound to a different frontend",
    "role_forbidden": "Actor role is not allowed to send this message kind",
    "credential_reused": "Actor credential is already assigned",
}


class ActorError(ModuleError):
    """Fixed, public-safe actor refusal; untyped exceptions remain private."""

    kind = "actor"

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in ERROR_MESSAGES:
            raise ValueError("Unknown actor error code")
        self.code = code
        super().__init__(ERROR_MESSAGES[code])


def uuid_text(value: Any, label: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError(f"{label} must be a canonical UUID") from None
    return value


def positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def bounded_text(value: Any, label: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\0" in value:
        raise ValueError(f"Invalid {label}")
    return value


def canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError("Value is not bounded JSON") from None


def json_object(value: Any, label: str, limit: int = MAX_BODY_BYTES) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")

    def check(item: Any, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError(f"{label} exceeds its nesting bound")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError(f"{label} keys must be strings")
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError(f"{label} contains a non-JSON value")

    check(value)
    encoded = canonical(value)
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeError:
        raise ValueError(f"{label} contains invalid Unicode") from None
    if size > limit:
        raise ValueError(f"{label} exceeds its {limit}-byte bound")
    return encoded


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Actor envelope contains duplicate JSON fields")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ActorAuth:
    run_id: str
    actor_id: str
    epoch: int
    credential: str = field(repr=False)
    frontend_id: str

    def __post_init__(self) -> None:
        uuid_text(self.run_id, "run_id")
        uuid_text(self.actor_id, "actor_id")
        uuid_text(self.frontend_id, "frontend_id")
        positive_integer(self.epoch, "epoch")
        if not isinstance(self.credential, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.credential
        ):
            raise ValueError("Actor credential must be 32 bytes of lowercase hex")


@dataclass(frozen=True, slots=True)
class ActorEnvelope:
    schema_version: int
    message_id: str
    run_id: str
    project: str
    sender_actor_id: str
    sender_epoch: int
    recipient_actor_id: str
    recipient_epoch: int
    roster_digest: str
    request_id: str
    kind: str
    body: dict[str, Any]
    reply_to: str | None
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported actor envelope schema")
        for name in ("message_id", "run_id", "sender_actor_id", "recipient_actor_id"):
            uuid_text(getattr(self, name), name)
        if self.reply_to is not None:
            uuid_text(self.reply_to, "reply_to")
        positive_integer(self.sender_epoch, "sender_epoch")
        positive_integer(self.recipient_epoch, "recipient_epoch")
        if (
            not isinstance(self.project, str)
            or not Path(self.project).is_absolute()
            or len(self.project) > 4096
            or "\0" in self.project
        ):
            raise ValueError("Actor project must be an absolute path")
        if not isinstance(self.roster_digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.roster_digest
        ):
            raise ValueError("Invalid roster_digest")
        if not isinstance(self.request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", self.request_id
        ):
            raise ValueError("Invalid actor request_id")
        if not isinstance(self.kind, str) or self.kind not in KINDS:
            raise ValueError("Unsupported actor message kind")
        if (self.reply_to is not None) != (self.kind in {"reply", "review_reply"}):
            raise ValueError("Reply kind and reply_to must agree")
        for value in (self.created_at, self.expires_at):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid actor message time")
        if not 0 < self.expires_at - self.created_at <= 86400:
            raise ValueError("Invalid actor message expiry")
        object.__setattr__(self, "body", json.loads(json_object(self.body, "Actor body")))
        self.to_bytes()

    def to_bytes(self) -> bytes:
        json_object(self.body, "Actor body")
        try:
            encoded = canonical(asdict(self)).encode("utf-8")
        except UnicodeError:
            raise ValueError("Actor envelope contains invalid Unicode") from None
        if len(encoded) > MAX_ENVELOPE_BYTES:
            raise ValueError("Actor envelope exceeds its byte bound")
        return encoded

    @classmethod
    def from_bytes(cls, raw: bytes) -> ActorEnvelope:
        if not isinstance(raw, bytes) or len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError("Actor envelope exceeds its byte bound")
        try:
            value = json.loads(raw, object_pairs_hook=unique_object)
            if not isinstance(value, dict):
                raise ValueError("Actor envelope must be an object")
            return cls(**value)
        except (TypeError, UnicodeError, RecursionError, OverflowError) as error:
            raise ValueError("Invalid actor envelope") from error
