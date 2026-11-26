"""Stable kernel JSON identities and validation helpers."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

PEER = re.compile(r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9_./:-]{1,250}")
KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Object contains a duplicate JSON field")
        result[key] = value
    return result


def namespace_for_config(config_path: Path) -> str:
    """Pure, stable namespace identity shared by runtime and offline maintenance."""
    return "ab" + hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:12]
