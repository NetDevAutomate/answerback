"""Configuration owned by the Kiro managed-worker provider."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from ...provider_config import ProviderConfig

_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,31}")
KIRO_TOOL_NAME = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9_@./-]{0,63}")


def validate_kiro_trust_tools(trust_tools: object) -> tuple[str, ...]:
    """Return validated Kiro tool names, or raise ``ValueError``."""
    if not isinstance(trust_tools, (list, tuple)):
        raise ValueError("Kiro trust-tools must be a list of tool names")
    names: list[str] = []
    for name in trust_tools:
        if not isinstance(name, str) or not KIRO_TOOL_NAME.fullmatch(name):
            raise ValueError("Kiro trust-tools entries must be non-empty tool names")
        if name in names:
            raise ValueError(f"Kiro trust-tools lists {name!r} twice")
        names.append(name)
    return tuple(names)


@dataclass
class KiroConfig(ProviderConfig):
    provider: ClassVar[str] = "kiro"

    binary: str = "kiro-cli"
    workspaces: dict[str, str] = field(default_factory=dict)
    acp_engine: Literal["v3", "v2"] = "v3"
    trust_all_tools: bool = False
    trust_tools: list[str] = field(default_factory=list)
    active_budget: int = 600

    def validate(self) -> None:
        if type(self.active_budget) is not int or not 60 <= self.active_budget <= 14_400:
            raise ValueError("kiro active execution budget must be 60–14400 seconds")
        for alias, cwd in self.workspaces.items():
            if not _ALIAS.fullmatch(alias):
                raise ValueError("Kiro aliases must be lowercase names of at most 32 characters")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise ValueError("Kiro working directories must be absolute paths")
        if self.acp_engine not in ("v2", "v3"):
            raise ValueError("Kiro ACP engine must be 'v2' or 'v3'")
        if type(self.trust_all_tools) is not bool:
            raise ValueError("Kiro trust-all-tools switch must be a boolean")
        if self.trust_all_tools and self.acp_engine != "v2":
            raise ValueError("Kiro --trust-all-tools (-a) is a v2-only flag")
        if validate_kiro_trust_tools(self.trust_tools):
            if self.acp_engine != "v2":
                raise ValueError("Kiro --trust-tools is a v2-only flag")
            if self.trust_all_tools:
                raise ValueError(
                    "Kiro --trust-tools and --trust-all-tools (-a) are mutually exclusive"
                )

    def legacy_selected(self) -> bool:
        return bool(self.workspaces)
