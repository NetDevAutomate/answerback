"""Human-friendly discovery with explicit routing and no ambiguous dispatch."""

import asyncio
from typing import Any

from .adapters.registry import DiscoveryRegistry
from .config import Config
from .process import BridgeError

# Providers run concurrently. Leave time within the 31-second RPC deadline for
# subprocess cleanup, result encoding, and the response to reach the caller.
PROVIDER_SECONDS = 25
MAX_QUERY_TERMS = 32
MAX_DISCOVERY_PEERS = 1000
MAX_SEARCH_FIELD_CHARS = 4096
MAX_SEARCH_HAYSTACK_CHARS = 48 * 1024
_SEARCH_FIELDS = (
    "peer_id",
    "name",
    "title",
    "preview",
    "terminal_title",
    "terminal_title_stripped",
    "project",
    "provider",
    "agent_kind",
    "application",
    "target",
)


class SelectionRequired(ValueError):
    def __init__(self, message: str, matches: list[dict[str, Any]], errors: dict[str, str]) -> None:
        super().__init__(message)
        self.matches = matches
        self.errors = errors


def search_terms(query: str) -> tuple[str, ...]:
    """Case-fold and bound a discovery query once, before provider work."""
    terms = query.casefold().split(maxsplit=MAX_QUERY_TERMS)
    if len(terms) > MAX_QUERY_TERMS:
        raise ValueError(f"query must contain at most {MAX_QUERY_TERMS} search terms")
    return tuple(terms)


def _search_text(value: Any) -> str:
    if isinstance(value, str):
        return value[:MAX_SEARCH_FIELD_CHARS]
    if type(value) in (bool, int, float):
        return str(value)[:MAX_SEARCH_FIELD_CHARS]
    return ""


def _search_haystack(peer: dict[str, Any]) -> str:
    """Render each searchable field once within a fixed per-peer budget."""
    text = " ".join(
        _search_text(peer.get(key, "")).casefold()[:MAX_SEARCH_FIELD_CHARS]
        for key in _SEARCH_FIELDS
    )
    return text[:MAX_SEARCH_HAYSTACK_CHARS]


def matching_peers(peers: list[dict[str, Any]], terms: tuple[str, ...]) -> list[dict[str, Any]]:
    """Filter a bounded peer list using one prepared haystack per peer."""
    selected = []
    for peer in peers:
        haystack = _search_haystack(peer)
        if all(term in haystack for term in terms):
            selected.append(peer)
    return selected


def matches(peer: dict[str, Any], query: str) -> bool:
    """Compatibility matcher for callers outside Directory's bulk path."""
    return bool(matching_peers([peer], search_terms(query)))


class Directory:
    def __init__(
        self,
        config: Config,
        herdr: Any,
        crew: Any,
        desktop: Any = None,
        core: Any = None,
        *,
        registry: DiscoveryRegistry | None = None,
    ) -> None:
        from .adapters.factory import build_discovery

        self.config = config
        self.adapters = registry or build_discovery(config, herdr, crew, desktop, core)

    async def search(self, query: str = "", provider: str | None = None) -> dict[str, Any]:
        terms = search_terms(query)
        result = await self._discover(provider)
        if terms:
            result["peers"] = await asyncio.to_thread(matching_peers, result["peers"], terms)
        return result

    async def _discover(self, provider: str | None) -> dict[str, Any]:
        providers = (
            [self.adapters.canonical(provider)]
            if provider is not None
            else [adapter.name for adapter in self.adapters if adapter.enabled()]
        )
        tasks = [asyncio.create_task(self._bounded_provider(name)) for name in providers]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            # Caller cancellation is not a partial success. Stop and collect all
            # owned provider work, including when another provider raises.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        peers: list[dict[str, Any]] = []
        overflow = False
        for result in results:
            available = MAX_DISCOVERY_PEERS - len(peers)
            if available <= 0:
                overflow |= bool(result["peers"])
                continue
            peers.extend(result["peers"][:available])
            overflow |= len(result["peers"]) > available

        errors = {name: error for result in results for name, error in result["errors"].items()}
        if overflow:
            errors["directory"] = (
                f"Peer inventory exceeds {MAX_DISCOVERY_PEERS}; select a provider or exact peer_id"
            )
        cursors = {
            name: cursor for result in results for name, cursor in result.get("cursors", {}).items()
        }
        excluded = {
            name: reason
            for result in results
            for name, reason in result.get("excluded", {}).items()
        }
        return {
            "peers": peers,
            "complete": not errors and not excluded,
            "errors": errors,
            **({"cursors": cursors} if cursors else {}),
            **({"excluded": excluded} if excluded else {}),
        }

    async def _bounded_provider(self, provider: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(PROVIDER_SECONDS):
                return await self._search_provider(provider)
        except TimeoutError:
            return {
                "peers": [],
                "complete": False,
                "errors": {provider: f"{provider} discovery timed out; retry this provider"},
            }
        except (BridgeError, OSError, ValueError) as error:
            return {
                "peers": [],
                "complete": False,
                "errors": {provider: f"Invalid provider inventory ({type(error).__name__})"},
            }

    async def _search_provider(self, provider: str) -> dict[str, Any]:
        return (await self.adapters.get(provider).discover()).as_record()

    async def resolve(self, selector: str, provider: str | None = None) -> dict[str, Any]:
        terms = search_terms(selector)
        if not terms:
            raise ValueError("A peer name, project keyword, or canonical peer handle is required")
        if provider is not None:
            provider = self.adapters.canonical(provider)
        prefix, separator, target = selector.partition(":")
        if separator and self.adapters.contains(prefix):
            selected = self.adapters.canonical(prefix)
            if provider is not None and provider != selected:
                raise ValueError("Peer handle conflicts with the selected provider")
            provider = selected
            selector = selected + ":" + target
            resolved = await self.adapters.get(selected).resolve_exact(selector)
            if resolved is not None:
                return resolved.as_record()
        result = await self._discover(provider)
        exact = [peer for peer in result["peers"] if peer["peer_id"] == selector]
        choices = exact or await asyncio.to_thread(matching_peers, result["peers"], terms)
        if (
            len(choices) != 1
            or not result["complete"]
            or (not exact and not isinstance(choices[0].get("binding"), str))
        ):
            raise SelectionRequired(
                "Select a unique peer by peer_id; unavailable providers cannot be assumed "
                "to have no matches",
                choices,
                result["errors"],
            )
        return choices[0]
