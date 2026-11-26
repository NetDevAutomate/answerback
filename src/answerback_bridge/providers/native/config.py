"""Configuration shared by the owned native CLI providers."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ...provider_config import ProviderConfig

NATIVE_PROVIDERS = ("codex_cli", "claude_code", "grok")
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,31}")


@dataclass
class NativeConfig(ProviderConfig):
    """A bridge-owned native CLI worker."""

    binary: str = ""
    home: str | None = None
    workspaces: dict[str, str] = field(default_factory=dict)
    active_budget: int = 600

    def validate(self) -> None:
        provider = self.provider
        if not isinstance(self.binary, str) or not self.binary.strip():
            raise ValueError(f"{provider} executable must be nonempty text")
        if self.home is not None and (
            not isinstance(self.home, str) or not Path(self.home).is_absolute()
        ):
            raise ValueError(f"{provider} home must be an absolute path")
        if not isinstance(self.workspaces, dict):
            raise ValueError(f"{provider} workspaces must be a mapping")
        for alias, cwd in self.workspaces.items():
            if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
                raise ValueError(f"{provider} workspace alias is invalid")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise ValueError(f"{provider} workspace must be an absolute path")
        self.workspaces = {
            alias: str(Path(cwd).resolve()) for alias, cwd in self.workspaces.items()
        }
        if type(self.active_budget) is not int or not 60 <= self.active_budget <= 14_400:
            raise ValueError(f"{provider} active execution budget must be 60–14400 seconds")


@dataclass
class CodexCliConfig(NativeConfig):
    provider: ClassVar[str] = "codex_cli"
    binary: str = "codex"


@dataclass
class ClaudeCodeConfig(NativeConfig):
    provider: ClassVar[str] = "claude_code"
    binary: str = "claude"


@dataclass
class GrokConfig(NativeConfig):
    provider: ClassVar[str] = "grok"
    binary: str = "grok"
