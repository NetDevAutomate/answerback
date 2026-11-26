"""The disabled-core discovery error names every command that enables the provider."""

from typing import Any

import pytest

from codex_herdr_bridge.adapters.core import CoreDiscovery
from codex_herdr_bridge.cli import parser
from codex_herdr_bridge.config import Config

# Both routes exist in the CLI (cli.py: the `hc-configure` command and
# `configure --runtime hc`); the message must send the operator to either.
COMMANDS = ("`answerback hc-configure`", "`answerback configure --runtime hc`")


def never_native() -> Any:
    raise AssertionError("a disabled core provider must not touch the native client")


async def discovery_error(config: Config) -> str:
    page = await CoreDiscovery(config, never_native).discover()
    record = page.as_record()
    assert record["peers"] == []
    return record["errors"]["hc"]


async def test_disabled_core_discovery_error_names_both_enabling_commands() -> None:
    message = await discovery_error(Config(hc_enabled=False, codex_enabled=False))
    assert message.startswith("Core is not configured; run ")
    for command in COMMANDS:
        assert command in message, f"{command} missing from {message!r}"


async def test_disabled_core_message_offers_the_two_commands_as_alternatives() -> None:
    message = await discovery_error(Config(hc_enabled=False, codex_enabled=False))
    first, separator, second = message.partition(" or ")
    assert separator, f"the two commands are not offered as alternatives: {message!r}"
    assert COMMANDS[0] in first and COMMANDS[1] in second


@pytest.mark.parametrize("command", COMMANDS)
def test_each_named_command_parses_in_the_cli(command: str) -> None:
    # The message must never name a route the CLI does not accept.
    argv = command.strip("`").split()[1:]
    args = parser().parse_args(argv)
    assert args.command in {"hc-configure", "configure"}
    if args.command == "configure":
        assert args.runtime == "hc"
