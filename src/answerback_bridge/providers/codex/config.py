"""Configuration owned by the Codex Desktop provider."""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from ...provider_config import ProviderConfig

_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,31}")


@dataclass
class CodexConfig(ProviderConfig):
    """Codex Desktop: paired chats and the CLI that queues to them."""

    provider: ClassVar[str] = "codex"
    flat_aliases: ClassVar[Mapping[str, str]] = {"desktop_threads": "desktop_threads"}

    binary: str = "codex"
    desktop_threads: dict[str, str] = field(default_factory=dict)
    home: str | None = None
    enabled: bool = False

    def validate(self) -> None:
        for alias, thread_id in self.desktop_threads.items():
            if not _ALIAS.fullmatch(alias):
                raise ValueError("Chat aliases must be lowercase names of at most 32 characters")
            self.desktop_threads[alias] = str(uuid.UUID(thread_id))
        if self.enabled and not self.binary:
            raise ValueError("Codex executable is required when its provider is enabled")
        if type(self.enabled) is not bool:
            raise ValueError("Provider enablement flags must be booleans")

    def legacy_selected(self) -> bool:
        return self.enabled
