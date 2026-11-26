#!/usr/bin/env python3
"""Assert that the project environment holds the public composition before a gate runs in it.

    python3 tools/assert_composition.py public     # only the kernel's own providers are installed

The kernel's providers are the `answerback.providers` entry points the `answerback`
distribution itself declares. PUBLIC means no other distribution contributes one. `just check`
starts with this check so the kernel's gate never silently runs in an environment that also
holds a provider plugin (a plugin's own repository gates the composition it makes).
Exit 1 names what is wrong and the `uv sync` that fixes it.
"""

from __future__ import annotations

import sys
from importlib import metadata

PROVIDER_GROUP = "answerback.providers"
KERNEL_DISTRIBUTION = "answerback"


def _normalized(name: str) -> str:
    return name.lower().replace("_", "-")


def _foreign_provider_distributions() -> set[str]:
    """Distributions other than the kernel that declare a provider entry point."""
    found: set[str] = set()
    for entry in metadata.entry_points(group=PROVIDER_GROUP):
        distribution = getattr(entry, "dist", None)
        name = distribution.metadata.get("Name") if distribution else None
        if name and _normalized(name) != KERNEL_DISTRIBUTION:
            found.add(_normalized(name))
    return found


def main(argv: list[str]) -> int:
    if argv[1:] != ["public"]:
        print(__doc__, file=sys.stderr)
        return 2
    foreign = _foreign_provider_distributions()
    if foreign:
        print(
            "assert_composition: the environment holds provider plugins outside the kernel "
            f"({', '.join(sorted(foreign))}); the public gate needs the default sync: "
            "run `uv sync --all-extras`",
            file=sys.stderr,
        )
        return 1
    print("composition: public (kernel providers only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
