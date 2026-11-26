#!/usr/bin/env python3
"""Assert which composition the project environment holds before a gate runs in it.

    python3 tools/assert_composition.py public     # only the kernel's own providers are installed
    python3 tools/assert_composition.py internal   # every package of the `internal` group is too

The kernel's providers are the `answerback.providers` entry points the `answerback`
distribution itself declares. PUBLIC means no other distribution contributes one; INTERNAL
means each distribution the `internal` dependency group names (pyproject.toml) is installed.
`just check` runs the public gate and `just check-internal` the internal one; each starts
with this check so a gate never silently runs in the other composition's environment.
Exit 1 names what is wrong and the `uv sync` that fixes it.
"""

from __future__ import annotations

import sys
import tomllib
from importlib import metadata
from pathlib import Path

PROVIDER_GROUP = "answerback.providers"
KERNEL_DISTRIBUTION = "answerback"
INTERNAL_GROUP = "internal"


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


def _internal_group(pyproject: Path) -> list[str]:
    with pyproject.open("rb") as stream:
        data = tomllib.load(stream)
    group = data.get("dependency-groups", {}).get(INTERNAL_GROUP, [])
    names: list[str] = []
    for item in group:
        if isinstance(item, str):
            requirement = item.split(";", 1)[0]
            for separator in ("==", ">=", "<=", "~=", "!=", "<", ">", "[", " "):
                requirement = requirement.split(separator, 1)[0]
            names.append(_normalized(requirement.strip()))
    return names


def _installed(name: str) -> bool:
    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return False
    return True


def main(argv: list[str]) -> int:
    if argv[1:] not in (["public"], ["internal"]):
        print(__doc__, file=sys.stderr)
        return 2
    wanted = argv[1]
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    group = _internal_group(pyproject)
    foreign = _foreign_provider_distributions()
    if wanted == "public":
        if foreign:
            print(
                "assert_composition: the environment holds provider plugins outside the kernel "
                f"({', '.join(sorted(foreign))}); the public gate needs the default sync: "
                "run `uv sync --all-extras` (or `just check-internal` for this composition)",
                file=sys.stderr,
            )
            return 1
        print("composition: public (kernel providers only)")
        return 0
    missing = [name for name in group if not _installed(name)]
    if missing:
        print(
            "assert_composition: the internal gate needs every package of the `internal` "
            f"group installed; missing {', '.join(missing)}: run "
            f"`uv sync --all-extras --group {INTERNAL_GROUP}`",
            file=sys.stderr,
        )
        return 1
    print(f"composition: internal ({', '.join(group)} installed beside the kernel)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
