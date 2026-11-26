"""Shared implementation for owned native CLI providers."""

from typing import Any


def codex_cli_plugin() -> Any:
    """Return the Codex CLI provider plugin."""
    from .plugin import codex_cli_plugin as factory

    return factory()


def claude_code_plugin() -> Any:
    """Return the Claude Code provider plugin."""
    from .plugin import claude_code_plugin as factory

    return factory()


def grok_plugin() -> Any:
    """Return the Grok provider plugin."""
    from .plugin import grok_plugin as factory

    return factory()
