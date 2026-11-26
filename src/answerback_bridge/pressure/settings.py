"""The `pressure` block of the optional `events.json` beside the bridge config."""

import math
import re
from dataclasses import dataclass, field
from typing import Any

KNOWN_PROVIDERS = ("kiro", "bedrock")
DEFAULT_NOTICE = "Provider under rate pressure: fewer, larger tool calls; no parallel sub-agents."
_TOP = {"enabled", "base_seconds", "cap_seconds", "clean_seconds", "notice", "providers"}
_PER_PROVIDER = {"account_hint", "base_seconds", "cap_seconds"}
_HINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _seconds(name: str, value: Any, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"pressure.{name} must be {low:g}–{high:g} seconds")
    return float(value)


def _defaults() -> dict[str, dict[str, Any]]:
    return {name: {"account_hint": "default"} for name in KNOWN_PROVIDERS}


@dataclass(frozen=True)
class PressureSettings:
    enabled: bool = True
    base_seconds: float = 60
    cap_seconds: float = 480
    clean_seconds: float = 600
    notice: str = DEFAULT_NOTICE
    # Per provider: `account_hint` (a label, never a credential) and optional
    # `base_seconds`/`cap_seconds` that override the values above.
    providers: dict[str, dict[str, Any]] = field(default_factory=_defaults)

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("pressure.enabled must be true or false")
        _seconds("base_seconds", self.base_seconds, 1, 3600)
        _seconds("cap_seconds", self.cap_seconds, 1, 86400)
        _seconds("clean_seconds", self.clean_seconds, 30, 86400)
        if self.cap_seconds < self.base_seconds:
            raise ValueError("pressure.cap_seconds must not be below pressure.base_seconds")
        if (
            not isinstance(self.notice, str)
            or not 1 <= len(self.notice) <= 200
            or not self.notice.isprintable()
        ):
            raise ValueError("pressure.notice must be one printable line of 1–200 characters")
        if not isinstance(self.providers, dict) or set(self.providers) - set(KNOWN_PROVIDERS):
            raise ValueError(f"pressure.providers names only {', '.join(KNOWN_PROVIDERS)}")
        merged = _defaults()
        for name, values in self.providers.items():
            if not isinstance(values, dict) or set(values) - _PER_PROVIDER:
                raise ValueError(f"pressure.providers.{name} has unknown settings")
            hint = values.get("account_hint", "default")
            if not isinstance(hint, str) or not _HINT.fullmatch(hint):
                raise ValueError(
                    f"pressure.providers.{name}.account_hint must be a 1–64 character label"
                )
            own: dict[str, Any] = {"account_hint": hint}
            base = self.base_seconds
            if "base_seconds" in values:
                own["base_seconds"] = base = _seconds(
                    f"providers.{name}.base_seconds", values["base_seconds"], 1, 3600
                )
            cap = self.cap_seconds
            if "cap_seconds" in values:
                own["cap_seconds"] = cap = _seconds(
                    f"providers.{name}.cap_seconds", values["cap_seconds"], 1, 86400
                )
            if cap < base:
                raise ValueError(f"pressure.providers.{name}: cap_seconds is below base_seconds")
            merged[name] = own
        object.__setattr__(self, "providers", merged)

    @classmethod
    def from_json(cls, values: Any) -> "PressureSettings":
        if not isinstance(values, dict) or set(values) - _TOP:
            raise ValueError("events.pressure must be an object with known settings")
        return cls(**values)

    def account_hint(self, provider: str) -> str:
        return str(self.providers.get(provider, {}).get("account_hint", "default"))

    def hints(self) -> dict[str, str]:
        return {name: self.account_hint(name) for name in self.providers}

    def base(self, provider: str) -> float:
        return float(self.providers.get(provider, {}).get("base_seconds", self.base_seconds))

    def cap(self, provider: str) -> float:
        return float(self.providers.get(provider, {}).get("cap_seconds", self.cap_seconds))

    def delay(self, provider: str, level: int) -> float:
        """`min(cap, base * 2**(level-1))` seconds for a level of at least 1."""
        if level <= 0:
            return 0.0
        return min(self.cap(provider), self.base(provider) * 2 ** min(level - 1, 32))

    def effective(self) -> dict[str, Any]:
        """The values in force, per provider with its overrides applied."""
        return {
            "enabled": self.enabled,
            "base_seconds": self.base_seconds,
            "cap_seconds": self.cap_seconds,
            "clean_seconds": self.clean_seconds,
            "notice": self.notice,
            "providers": {
                name: {
                    "account_hint": self.account_hint(name),
                    "base_seconds": self.base(name),
                    "cap_seconds": self.cap(name),
                }
                for name in sorted(self.providers)
            },
        }
