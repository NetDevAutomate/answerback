"""Versioned, bounded command envelopes."""

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

# Re-export the kernel helpers so existing bus imports remain compatible.
from ..digests import KEY as KEY
from ..digests import PEER as PEER
from ..digests import canonical as canonical
from ..digests import digest as digest
from ..digests import unique_object as unique_object

MAX_MESSAGE_BYTES = 100_000
MAX_WIRE_BYTES = 128_000


@dataclass(frozen=True, slots=True)
class Command:
    schema_version: int
    command_id: str
    request_id: str
    peer_id: str
    project: str
    routing_digest: str
    message: str
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported command schema")
        if not isinstance(self.command_id, str) or str(UUID(self.command_id)) != self.command_id:
            raise ValueError("A canonical command UUID is required")
        if not isinstance(self.request_id, str) or not KEY.fullmatch(self.request_id):
            raise ValueError("Invalid request_id")
        if not isinstance(self.peer_id, str) or not PEER.fullmatch(self.peer_id):
            raise ValueError("Use an exact canonical peer ID from discovery")
        if not isinstance(self.project, str) or not Path(self.project).is_absolute():
            raise ValueError("An absolute expected project is required")
        if "\0" in self.project or len(self.project) > 4096:
            raise ValueError("Invalid expected project")
        if not isinstance(self.routing_digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.routing_digest
        ):
            raise ValueError("Invalid routing digest")
        if (
            not isinstance(self.message, str)
            or not self.message.strip()
            or len(self.message.encode()) > MAX_MESSAGE_BYTES
        ):
            raise ValueError("Message must contain 1–100000 UTF-8 bytes")
        for value in (self.created_at, self.expires_at):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid command time")
        if not 0 < self.expires_at - self.created_at <= 86400:
            raise ValueError("Command TTL must be positive and at most one day")

    @property
    def adapter_request_id(self) -> str:
        return f"bus:{self.command_id}"

    def to_bytes(self) -> bytes:
        result = canonical(asdict(self)).encode()
        if len(result) > MAX_WIRE_BYTES:
            raise ValueError("Command exceeds wire size limit")
        return result

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Command":
        if len(raw) > MAX_WIRE_BYTES:
            raise ValueError("Command exceeds wire size limit")
        try:
            value = json.loads(raw, object_pairs_hook=unique_object)
            if not isinstance(value, dict):
                raise ValueError("Command must be an object")
            return cls(**value)
        except (TypeError, UnicodeError, RecursionError, OverflowError) as error:
            raise ValueError("Invalid command envelope") from error
