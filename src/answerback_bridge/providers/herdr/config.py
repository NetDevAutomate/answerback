"""Configuration owned by the Herdr App/core and TUI providers."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ...addresses import validate_machine_names
from ...provider_config import ProviderConfig

_SESSION = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass
class HerdrConfig(ProviderConfig):
    provider: ClassVar[str] = "herdr"

    session: str | None = None
    binary: str = "herdr"

    def validate(self) -> None:
        if self.session is not None and not _SESSION.fullmatch(self.session):
            raise ValueError("An explicit valid Herdr session name is required")
        if self.session and not self.binary:
            raise ValueError("Herdr TUI executable is required for its configured session")

    def legacy_selected(self) -> bool:
        return self.session is not None


@dataclass
class HcConfig(ProviderConfig):
    """Herdr core: the local core and the send policy for its terminals."""

    provider: ClassVar[str] = "hc"

    enabled: bool = False
    binary: str = "hc"
    session: str | None = None
    send_to_done: bool = False
    send_machines: list[str] = field(default_factory=list)
    events: bool = False
    placement_policy: str | None = None

    def validate(self) -> None:
        if self.enabled and not self.binary:
            raise ValueError("Core executable is required when its provider is enabled")
        if self.session is not None and not _SESSION.fullmatch(self.session):
            raise ValueError("Core session must be a valid explicit name")
        if type(self.enabled) is not bool:
            raise ValueError("Provider enablement flags must be booleans")
        if type(self.send_to_done) is not bool:
            raise ValueError("Core send-to-done switch must be a boolean")
        if type(self.events) is not bool:
            raise ValueError("Core events switch must be a boolean")
        if self.placement_policy is not None and (
            not isinstance(self.placement_policy, str)
            or not Path(self.placement_policy).is_absolute()
            or not self.placement_policy.isprintable()
        ):
            raise ValueError("Core placement policy must be an absolute path")
        if not isinstance(self.send_machines, list):
            raise ValueError("Core send machines must be a list of machine names")
        validate_machine_names(self.send_machines)

    def legacy_selected(self) -> bool:
        return self.enabled
