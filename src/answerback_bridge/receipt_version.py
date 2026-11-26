"""Which bridge version created a receipt and which one ended it."""

import re
from typing import Any

from . import __version__

CREATED = "bridge_version_created"
ENDED = "bridge_version_ended"
KEYS = (CREATED, ENDED)
NOT_RECORDED = "not recorded"
UNREADABLE = "unreadable"
# Equals tasks.TERMINAL, which this module cannot import; a test holds the two equal.
END_STATES = ("completed", "failed", "abandoned", "cancelled")
REFUSAL = "Receipt version fields are written by the store"
_BOUND = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!_-]{0,63}")


def current() -> str | None:
    """This bridge's version, or None when it is not a text inside the bound."""
    value = __version__
    if isinstance(value, str) and _BOUND.fullmatch(value):
        return value
    return None


def names_key(changes: Any) -> bool:
    return isinstance(changes, dict) and any(key in changes for key in KEYS)


def stamp_created(metadata: dict[str, Any]) -> dict[str, Any]:
    stamped = dict(metadata)
    version = current()
    if version is not None:
        stamped[CREATED] = version
    return stamped


def stamp_ended(metadata: dict[str, Any]) -> dict[str, Any]:
    """A copy with this bridge's version as the ending one; an older ending value never stays."""
    stamped = dict(metadata)
    stamped.pop(ENDED, None)
    version = current()
    if version is not None:
        stamped[ENDED] = version
    return stamped


def without_ended(metadata: Any) -> Any:
    """The metadata without the ending key; a value that is not a dict is returned as it is."""
    if not isinstance(metadata, dict):
        return metadata
    return {key: value for key, value in metadata.items() if key != ENDED}


def _shown(metadata: dict[str, Any], key: str) -> str:
    if key not in metadata:
        return NOT_RECORDED
    value = metadata[key]
    if isinstance(value, str) and _BOUND.fullmatch(value):
        return value
    return UNREADABLE


def read(task: dict[str, Any]) -> dict[str, str | None]:
    """What a caller is shown: the ending version only while the receipt is in an end state."""
    metadata = task.get("metadata")
    ended = task.get("status") in END_STATES
    if not isinstance(metadata, dict):
        return {"created": NOT_RECORDED, "ended": NOT_RECORDED if ended else None}
    return {
        "created": _shown(metadata, CREATED),
        "ended": _shown(metadata, ENDED) if ended else None,
    }
