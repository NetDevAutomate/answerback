"""Configuration owned by the KiroCrew provider."""

from dataclasses import dataclass
from typing import ClassVar

from ...provider_config import ProviderConfig


@dataclass
class CrewConfig(ProviderConfig):
    provider: ClassVar[str] = "crew"

    binary: str = "kirocrew"
    enabled: bool = False
    port: int | None = None

    def validate(self) -> None:
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("Kirocrew port must be between 1 and 65535")
        if type(self.enabled) is not bool:
            raise ValueError("Provider enablement flags must be booleans")

    def legacy_selected(self) -> bool:
        return self.enabled
