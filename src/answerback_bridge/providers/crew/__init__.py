"""KiroCrew provider package."""

from typing import Any


def crew_plugin() -> Any:
    """Return the KiroCrew provider plugin."""
    from .plugin import crew_plugin as factory

    return factory()
