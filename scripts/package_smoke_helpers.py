"""Release-independent metadata and environment helpers for package smoke checks."""

import hashlib
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.parser import Parser
from pathlib import Path
from typing import Any
from zipfile import ZipFile


def wheel_identity(path: Path) -> dict[str, str]:
    with ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("Wheel must contain exactly one distribution metadata record")
        metadata = Parser().parsestr(archive.read(names[0]).decode())
    name, version = metadata["Name"], metadata["Version"]
    if not name or not version:
        raise ValueError("Wheel lacks distribution name/version")
    return {
        "path": str(path.resolve()),
        "name": name,
        "version": version,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def isolated_environment(root: Path, original: dict[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in original.items()
        if key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")
        and not key.startswith(("HERDR_", "ANSWERBACK_", "REX_"))
    }
    env.update(
        UV_TOOL_DIR=str(root / "tools"),
        UV_TOOL_BIN_DIR=str(root / "bin"),
        CODEX_HOME=str(root / "codex"),
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_STATE_HOME=str(root / "state"),
        XDG_DATA_HOME=str(root / "data"),
        PYTHONNOUSERSITE="1",
        UV_OFFLINE="1",
    )
    return env


@contextmanager
def importable(folder: Path) -> Iterator[None]:
    """Make the modules of ``folder`` importable inside the block, and only there.

    The folder goes first on ``sys.path`` for the block. Afterwards the entry is removed and
    so is every module the block loaded from the folder, so a later ``import conftest`` of
    the same process does not find a test helper. Objects already taken from those modules
    keep working: a function holds its own globals.
    """
    entry = str(folder.resolve())
    before = set(sys.modules)
    sys.path.insert(0, entry)
    try:
        yield
    finally:
        if entry in sys.path:
            sys.path.remove(entry)
        for name in set(sys.modules) - before:
            origin = getattr(sys.modules[name], "__file__", None)
            if origin and Path(origin).resolve().is_relative_to(entry):
                del sys.modules[name]


def broker_config_argument(bridge_config: Path) -> str:
    """The broker configuration path as the bridge spells it: under the resolved bus folder.

    The bridge accepts a broker as its own only when the broker's command line is exactly
    ``-c`` and this path. A folder under ``/tmp`` resolves to ``/private/tmp`` on macOS, so
    the path as the smoke first wrote it names the same file and still fails that check.
    """
    return str(bridge_config.expanduser().resolve().parent / "bus" / "nats.conf")


class SmokeStepFailed(AssertionError):
    """A step of the smoke ended without the answer it needs; the message names the step."""


def tool_answer(tool: str, result: Any) -> dict[str, Any]:
    """Return the structured content of one MCP tool result, or name the tool that failed.

    The client does not raise for a tool error: it returns a result whose error flag is set
    and whose structured content is absent. The message carries the text the bridge answered.
    """
    blocks = getattr(result, "content", None) or []
    texts = [text for block in blocks if isinstance(text := getattr(block, "text", None), str)]
    answer = "\n".join(texts) or "(the result carried no text)"
    if getattr(result, "is_error", False):
        raise SmokeStepFailed(f"MCP tool {tool} answered with an error: {answer}")
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        raise SmokeStepFailed(f"MCP tool {tool} returned no structured content: {answer}")
    return structured


async def search_then_submit(
    client: Any,
    *,
    search: dict[str, Any],
    chosen: Callable[[dict[str, Any]], bool],
    submit: dict[str, Any],
) -> dict[str, Any]:
    """Search for a peer, then submit to it with the discovery binding that search returned.

    ``chosen`` picks the peer that is meant from the search result. ``submit`` holds the other
    arguments of ``peer_submit``; without a ``selector`` the peer's ``peer_id`` is used. A
    ``binding`` is passed only when the chosen peer carries a string one, taken from this
    search and from nowhere else.
    """
    found = tool_answer("peer_search", await client.call_tool("peer_search", search))
    peers = found.get("peers")
    peer = next(
        (item for item in peers or [] if isinstance(item, dict) and chosen(item)),
        None,
    )
    if peer is None:
        raise SmokeStepFailed(
            f"MCP tool peer_search with {search!r} did not return the peer that is meant: {found!r}"
        )
    arguments = {"selector": peer.get("peer_id"), **submit}
    binding = peer.get("binding")
    if isinstance(binding, str):
        arguments["binding"] = binding
    return tool_answer("peer_submit", await client.call_tool("peer_submit", arguments))
