"""Argument helpers shared by the kernel's CLI and the plugins' subcommands."""

from pathlib import Path


def binary_argument(value: str | None) -> str | None:
    """Keep bare PATH names, but bind explicit paths to the calling directory."""
    if value and "/" in value:
        return str(Path(value).expanduser().absolute())
    return value


def bounded_file(path: Path, maximum: int) -> str:
    with path.open("rb") as handle:
        raw = handle.read(maximum * 4 + 1)
    if len(raw) > maximum * 4:
        raise ValueError("Input file exceeds the supported size")
    value = raw.decode("utf-8")
    if len(value) > maximum:
        raise ValueError("Input file exceeds the supported character count")
    return value
