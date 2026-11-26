"""Bounds for every text and list that enters the fleet document (spec section 8)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..placement.probe import LOCAL

NAME = 64
TITLE = 120
TEXT = 200
MACHINES = 16
NEEDS_PERSON = 200
RECENT = 8
CLIENTS = 32
ERRORS = 20
DOCUMENT_BYTES = 256 * 1024

# The rule placement.probe.validate_machines applies; "local" is the coordinator's own name.
_MACHINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
_DROP = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Co", "Cn"})


def clean(value: Any, limit: int) -> str | None:
    """Return ``value`` without control characters and cut to ``limit``; None for non-text."""
    if not isinstance(value, str):
        return None
    # Look at a bounded prefix only: a hostile text can be very long.
    kept = "".join(c for c in value[: limit * 4] if unicodedata.category(c) not in _DROP)
    kept = kept[:limit]
    return kept or None


def machine_name_ok(value: Any) -> bool:
    """True for a worker name the placement code accepts and that is not ``local``."""
    return isinstance(value, str) and value != LOCAL and _MACHINE.fullmatch(value) is not None
