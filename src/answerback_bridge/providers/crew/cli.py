"""CLI surface owned by the KiroCrew provider."""

import argparse
import uuid
from typing import TYPE_CHECKING, Any

from ...cli_kit import binary_argument
from ...plugin import CliRunner

if TYPE_CHECKING:
    from ...transport import Operations


def crew(commands: argparse._SubParsersAction, run: CliRunner) -> None:
    configure = commands.add_parser(
        "crew-configure", help="Enable authenticated local Kirocrew access"
    )
    configure.add_argument("--binary", default="kirocrew")
    configure.add_argument("--port", type=int)
    create = commands.add_parser("crew-create", help="Create a dedicated Kirocrew project chat")
    create.add_argument("title")
    create.add_argument("--project", required=True)
    create.add_argument("--request-id", default=str(uuid.uuid4()))
    read = commands.add_parser("read-crew", help="Read bounded Kirocrew chat history")
    read.add_argument("target")

    async def run_configure(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "crew_configure", {"binary": binary_argument(args.binary), "port": args.port}
        )

    async def run_create(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        return await bridge.call(
            "crew_create",
            {"title": args.title, "project": args.project, "request_id": args.request_id},
        )

    async def run_read(args: argparse.Namespace, bridge: "Operations") -> dict[str, Any]:
        peer = await bridge.call("peer_search", {"query": args.target, "provider": "crew"})
        candidates = peer["peers"]
        if not peer["complete"] or len(candidates) != 1:
            return {"status": "needs_selection", "matches": candidates, "errors": peer["errors"]}
        return await bridge.call("crew_read", {"target": candidates[0]["target"]})

    run(configure, run_configure)
    run(create, run_create)
    run(read, run_read)
