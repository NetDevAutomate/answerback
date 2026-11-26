"""Codex Desktop provider package."""

from typing import Any


def codex_plugin() -> Any:
    """Return the Codex Desktop provider plugin."""
    from .plugin import codex_plugin as factory

    return factory()
