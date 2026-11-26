"""Rex provider package."""

from typing import Any


def rex_plugin() -> Any:
    """Return the Rex provider plugin."""
    from .plugin import rex_plugin as factory

    return factory()
