"""Use Herdr's version-checked CLI with an explicit session on every call."""

import json
from typing import Any

from ...config import Config
from ...process import BridgeError, herdr_env, run


class Herdr:
    def __init__(self, config: Config) -> None:
        self.config = config

    async def call(self, *args: str) -> dict[str, Any]:
        if self.config.herdr_session is None:
            raise ValueError("Herdr TUI is not configured; use provider hc for Herdr App")
        response = await run(
            [self.config.herdr_binary, "--session", self.config.herdr_session, *args],
            env=herdr_env(),
        )
        try:
            value = json.loads(response.stdout)
        except json.JSONDecodeError as error:
            raise BridgeError("Herdr returned an unexpected non-JSON response") from error
        if "error" in value:
            raise BridgeError(f"Herdr rejected the operation: {value['error']}")
        return value.get("result", value)

    async def check(self) -> str:
        if self.config.herdr_session is None:
            raise ValueError("Herdr TUI is not configured; use provider hc for Herdr App")
        version = await run([self.config.herdr_binary, "--version"])
        help_text = await run([self.config.herdr_binary, "agent", "--help"])
        if "prompt" not in help_text.stdout:
            raise BridgeError(
                f"{version.stdout.strip()} lacks agent prompt. Choose a newer Herdr binary "
                "with --herdr. No upgrade or server restart has been performed."
            )
        return version.stdout.strip()

    async def get(self, target: str) -> dict[str, Any]:
        result = await self.call("agent", "get", target)
        return result.get("agent", result)

    async def read(self, target: str, lines: int = 100) -> dict[str, Any]:
        if self.config.herdr_session is None:
            raise ValueError("Herdr TUI is not configured; use provider hc for Herdr App")
        if not 1 <= lines <= 500:
            raise ValueError("lines must be between 1 and 500")
        output = await run(
            [
                self.config.herdr_binary,
                "--session",
                self.config.herdr_session,
                "agent",
                "read",
                target,
                "--source",
                "recent-unwrapped",
                "--lines",
                str(lines),
            ],
            env=herdr_env(),
        )
        return {"target": target, "source": "recent-unwrapped", "text": output.stdout}
