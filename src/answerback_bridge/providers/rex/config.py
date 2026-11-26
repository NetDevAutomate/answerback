"""Configuration owned by the Rex provider."""

from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit

from ...provider_config import ProviderConfig


@dataclass
class RexConfig(ProviderConfig):
    provider: ClassVar[str] = "rex"

    enabled: bool = False
    binary: str = "rex"
    server: str | None = None
    host_id: str | None = None

    def validate(self) -> None:
        if not isinstance(self.binary, str) or not self.binary.strip():
            raise ValueError("Rex executable must be nonempty text")
        if self.server is not None:
            if not isinstance(self.server, str):
                raise ValueError("Rex endpoint must be text")
            endpoint = urlsplit(self.server)
            if (
                endpoint.scheme != "unix"
                or endpoint.netloc
                or not endpoint.path.startswith("/")
                or endpoint.query
                or endpoint.fragment
                or any(ord(char) < 32 for char in self.server)
            ):
                raise ValueError("Rex requires an explicit local Unix endpoint")
        if self.host_id is not None and (
            not isinstance(self.host_id, str)
            or not self.host_id.strip()
            or any(ord(char) < 32 for char in self.host_id)
        ):
            raise ValueError("Rex expected host must be nonempty text without controls")
        if type(self.enabled) is not bool:
            raise ValueError("Provider enablement flags must be booleans")

    def legacy_selected(self) -> bool:
        return self.enabled
