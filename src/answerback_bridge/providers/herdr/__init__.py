"""Herdr App/core and TUI provider package."""

from typing import Any


def hc_plugin() -> Any:
    """Return the Herdr App/core provider plugin."""
    from .plugin import hc_plugin as factory

    return factory()


def herdr_plugin() -> Any:
    """Return the Herdr TUI provider plugin."""
    from .plugin import herdr_plugin as factory

    return factory()
