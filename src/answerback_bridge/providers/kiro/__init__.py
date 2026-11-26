"""Kiro managed-worker provider package."""

from typing import Any


def kiro_plugin() -> Any:
    """Return the Kiro provider plugin."""
    from .plugin import kiro_plugin as factory

    return factory()
