"""Injected adapter selection. This module contains no built-in provider branches."""

from collections.abc import Iterable, Iterator
from typing import cast

from .contracts import DiscoverySource, Peer, ProviderAdapter, TaskControls


class DiscoveryRegistry:
    def __init__(self, adapters: Iterable[DiscoverySource]) -> None:
        self._adapters: dict[str, DiscoverySource] = {}
        self._names: dict[str, str] = {}
        for adapter in adapters:
            for name in (adapter.name, *adapter.aliases):
                if name in self._names:
                    raise ValueError(f"Duplicate provider registration: {name}")
                self._names[name] = adapter.name
            self._adapters[adapter.name] = adapter

    def __iter__(self) -> Iterator[DiscoverySource]:
        return iter(self._adapters.values())

    def contains(self, name: str) -> bool:
        return name in self._names

    def canonical(self, name: str) -> str:
        try:
            return self._names[name]
        except KeyError:
            raise ValueError(f"Unknown provider: {name}") from None

    def get(self, name: str) -> DiscoverySource:
        return self._adapters[self.canonical(name)]


class AdapterRegistry(DiscoveryRegistry):
    def __init__(self, adapters: Iterable[ProviderAdapter]) -> None:
        super().__init__(adapters)

    def get(self, name: str) -> ProviderAdapter:
        return cast(ProviderAdapter, super().get(name))

    def __iter__(self) -> Iterator[ProviderAdapter]:
        return (self.get(name) for name in self._adapters)

    def for_peer(self, peer: Peer) -> ProviderAdapter:
        return self.get(peer.provider)

    def controls(self, name: str) -> TaskControls:
        control = self.get(name).controls
        if control is None:
            raise ValueError(f"Provider {name} does not support task controls")
        return control
