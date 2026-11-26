#!/usr/bin/env python3
"""Assert that a tracked tree contains no internal-provider references.

The matching rule has three branches: ASCII-bounded, case-insensitive ``a[k]i``;
camel case ``A[k]i`` next to an ASCII case or digit transition; and all-caps
``A[K]I`` before an uppercase letter, digit, or underscore, or after a lowercase
letter or digit. Lowercase run-ons such as ``a[k]iadapter``, title-case ``Akira``,
all-caps forms followed by lowercase letters such as ``A[K]Ia`` and ``A[K]Is``,
and mixed-case ``aKiAdapter`` remain misses. Greek and fullwidth lookalikes also
remain misses because the rule is ASCII-only. Tracked paths use the same rule.

Only ``git ls-files -z`` supplies the inventory. The configured allowlist is
parsed as control data: exact entry text is exempt, while comments and entry paths
are scanned. Binary files have a NUL in their first 8 KiB. Paths are checked before
the binary probe; binary payloads are scanned as raw ASCII and UTF-16LE bytes and
reported with ``kind: "binary"`` and ``skipped_binary_paths``. Tracked symlinks are
scanned as their link payload, never through their referent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

# This pattern uses character classes so the guard does not match its own source.
# The indirect spelling is deliberate. Do not simplify it.
PATTERN = re.compile(
    r"(?:(?i:(?<![A-Za-z0-9])a[k]i(?![A-Za-z0-9]))|(?<=[a-z0-9])A[k]i|"
    r"(?<=[a-z0-9])A[K]I|A[k]i(?=[A-Z0-9])|A[K]I(?=[A-Z0-9_]))"
)
BINARY_PATTERN = re.compile(
    rb"(?:(?i:(?<![A-Za-z0-9])a[k]i(?![A-Za-z0-9]))|(?<=[a-z0-9])A[k]i|"
    rb"(?<=[a-z0-9])A[K]I|A[k]i(?=[A-Z0-9])|A[K]I(?=[A-Z0-9_]))"
)
UTF16LE_PATTERN = re.compile(
    rb"(?:(?i:(?<![A-Za-z0-9]\0)a\0[k]\0i\0(?![A-Za-z0-9]\0))|"
    rb"(?<=[a-z0-9]\0)A\0[k]\0i\0|(?<=[a-z0-9]\0)A\0[K]\0I\0|"
    rb"A\0[k]\0i\0(?=[A-Z0-9]\0)|A\0[K]\0I\0(?=[A-Z0-9_]\0))"
)
BINARY_PROBE_BYTES = 8 * 1024
DEFAULT_ALLOWLIST = Path("tools/public-tree-allowlist.txt")


class ToolError(Exception):
    """A usage or allowlist error that exits with code 2."""


@dataclass(frozen=True)
class Hit:
    """One source line or tracked path that matched the internal-provider pattern."""

    path: str
    line: int
    text: str
    kind: str


@dataclass(frozen=True)
class AllowlistEntry:
    """One exact source location permitted until the public split removes it."""

    path: str
    line: int
    text: str
    allowlist_line: int


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="Git worktree to scan; defaults to the git root containing this script.",
    )
    parser.add_argument(
        "--skip-dir",
        action="append",
        default=[],
        metavar="PATH",
        help="Relative tracked directory to skip. May be repeated.",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help="Exact-match allowlist path, relative to --root by default.",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    return parser


def _git_root(directory: Path) -> Path:
    """Return the worktree root containing directory, or raise ToolError."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ToolError(f"cannot find a git worktree from {directory}") from error
    return Path(result.stdout.strip()).resolve()


def _tracked_paths(root: Path) -> list[str]:
    """Return tracked paths from git without walking the filesystem."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ToolError(f"cannot list tracked files in {root}") from error
    return [path for path in result.stdout.decode(errors="surrogateescape").split("\0") if path]


def _skip_paths(values: list[str]) -> tuple[str, ...]:
    """Validate and normalize relative directory prefixes."""
    paths: list[str] = []
    for value in values:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ToolError(f"--skip-dir must be a relative path below the worktree: {value}")
        normalized = path.as_posix().strip("/")
        if not normalized or normalized == ".":
            raise ToolError("--skip-dir must name a directory below the worktree")
        paths.append(normalized)
    return tuple(paths)


def _is_skipped(path: str, skip_paths: tuple[str, ...]) -> bool:
    """Return whether path is directly in, or below, an excluded directory."""
    return any(path == directory or path.startswith(f"{directory}/") for directory in skip_paths)


def _read_allowlist(
    path: Path, display_path: str
) -> tuple[dict[tuple[str, int, str], AllowlistEntry], list[Hit]]:
    """Parse entries and scan comment and path control data for provider references."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ToolError(f"cannot read allowlist {path}: {error.strerror or error}") from error

    entries: dict[tuple[str, int, str], AllowlistEntry] = {}
    control_hits: list[Hit] = []
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if PATTERN.search(raw_line):
                control_hits.append(Hit(display_path, number, line, "content"))
            continue
        parts = line.split(":", maxsplit=2)
        if len(parts) != 3 or not parts[0] or not parts[1]:
            raise ToolError(
                f"invalid allowlist entry at {path}:{number}; expected path:line:expected text"
            )
        try:
            source_line = int(parts[1])
        except ValueError as error:
            raise ToolError(f"invalid allowlist line number at {path}:{number}") from error
        if source_line < 0:
            raise ToolError(f"invalid allowlist line number at {path}:{number}")
        expected = parts[2]
        if expected != expected.strip():
            raise ToolError(f"allowlist text must already be stripped at {path}:{number}")
        key = (parts[0], source_line, expected)
        if key in entries:
            raise ToolError(f"duplicate allowlist entry at {path}:{number}")
        entries[key] = AllowlistEntry(*key, allowlist_line=number)
        if PATTERN.search(parts[0]):
            control_hits.append(Hit(display_path, number, parts[0], "content"))
    return entries, control_hits


def _line_hits(root: Path, relative_path: str) -> list[Hit]:
    """Return matching text lines for a tracked file known not to be binary."""
    path = root / relative_path
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            return [
                Hit(relative_path, number, line.strip(), "content")
                for number, line in enumerate(source, start=1)
                if PATTERN.search(line)
            ]
    except OSError as error:
        message = f"cannot scan tracked file {relative_path}: {error.strerror or error}"
        raise ToolError(message) from error


def _binary_hits(relative_path: str, content: bytes) -> list[Hit]:
    """Return one raw-byte hit for a binary file, preferring the earliest match."""
    matches = [
        match
        for pattern in (BINARY_PATTERN, UTF16LE_PATTERN)
        if (match := pattern.search(content)) is not None
    ]
    if not matches:
        return []
    matched = min(matches, key=lambda match: match.start()).group().replace(b"\0", b"")
    text = f"<binary match: {matched.decode('ascii', errors='replace')}>"
    return [Hit(relative_path, 0, text, "binary")]


def _symlink_hits(path: Path, relative_path: str) -> list[Hit]:
    """Return matching lines from a tracked symlink payload without dereferencing it."""
    try:
        target = os.readlink(path)
    except OSError as error:
        message = f"cannot scan tracked symlink {relative_path}: {error.strerror or error}"
        raise ToolError(message) from error
    return [
        Hit(relative_path, number, line.strip(), "content")
        for number, line in enumerate(target.splitlines(), start=1)
        if PATTERN.search(line)
    ]


def _report(root: Path, skip_paths: tuple[str, ...], allowlist_path: Path) -> dict[str, object]:
    """Scan tracked paths and content, then apply the exact allowlist."""
    try:
        allowlist_relative = allowlist_path.relative_to(root).as_posix()
    except ValueError:
        allowlist_relative = None
    entries, control_hits = _read_allowlist(
        allowlist_path, allowlist_relative or str(allowlist_path)
    )
    all_hits: list[Hit] = control_hits
    scanned = 0
    skipped_binary = 0
    skipped_binary_paths: list[str] = []
    skipped_dir = 0

    tracked_paths = _tracked_paths(root)
    for relative_path in tracked_paths:
        if _is_skipped(relative_path, skip_paths):
            skipped_dir += 1
            continue
        path = root / relative_path
        if PATTERN.search(relative_path):
            all_hits.append(Hit(relative_path, 0, relative_path, "path"))
        if relative_path == allowlist_relative:
            scanned += 1
            continue
        if path.is_symlink():
            scanned += 1
            all_hits.extend(_symlink_hits(path, relative_path))
            continue
        try:
            with path.open("rb") as source:
                binary = b"\0" in source.read(BINARY_PROBE_BYTES)
        except OSError as error:
            message = f"cannot scan tracked file {relative_path}: {error.strerror or error}"
            raise ToolError(message) from error
        if binary:
            skipped_binary += 1
            skipped_binary_paths.append(relative_path)
            try:
                all_hits.extend(_binary_hits(relative_path, path.read_bytes()))
            except OSError as error:
                message = f"cannot scan tracked file {relative_path}: {error.strerror or error}"
                raise ToolError(message) from error
            continue
        scanned += 1
        all_hits.extend(_line_hits(root, relative_path))

    hit_keys = {(hit.path, hit.line, hit.text) for hit in all_hits}
    stale = [entry for key, entry in entries.items() if key not in hit_keys]
    if stale:
        descriptions = ", ".join(f"{allowlist_path}:{entry.allowlist_line}" for entry in stale)
        raise ToolError(f"stale allowlist entry: {descriptions}")

    allowed_keys = set(entries)
    hits = [hit for hit in all_hits if (hit.path, hit.line, hit.text) not in allowed_keys]
    hits.sort(key=lambda hit: (hit.path, hit.line, hit.kind))
    summary = {
        "tracked": len(tracked_paths),
        "scanned": scanned,
        "skipped_binary": skipped_binary,
        "skipped_dir": skipped_dir,
        "content_hits": sum(hit.kind == "content" for hit in all_hits),
        "path_hits": sum(hit.kind == "path" for hit in all_hits),
        "binary_hits": sum(hit.kind == "binary" for hit in all_hits),
        "allowlisted": len(all_hits) - len(hits),
        "hits": len(hits),
    }
    return {
        "hits": [asdict(hit) for hit in hits],
        "skipped_binary_paths": skipped_binary_paths,
        "summary": summary,
    }


def _print_report(report: dict[str, object], as_json: bool) -> None:
    """Print either the human report or its stable JSON form."""
    if as_json:
        print(json.dumps(report, sort_keys=True))
        return
    for hit in report["hits"]:  # type: ignore[index, union-attr]
        print(f"{hit['path']}:{hit['line']}: {hit['text'][:120]}")
    summary = report["summary"]  # type: ignore[assignment]
    print("summary: " + " ".join(f"{key}={value}" for key, value in summary.items()))


def _error(message: str, as_json: bool) -> NoReturn:
    """Emit an error in the requested format and exit with code 2."""
    if as_json:
        print(json.dumps({"error": message}, sort_keys=True))
    else:
        print(f"assert_public_tree.py: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    """Run the public-tree assertion."""
    parser = _parser()
    arguments = parser.parse_args()
    try:
        root = _git_root(arguments.root or Path(__file__).resolve().parent)
        skip_paths = _skip_paths(arguments.skip_dir)
        allowlist_path = arguments.allowlist
        if not allowlist_path.is_absolute():
            allowlist_path = root / allowlist_path
        report = _report(root, skip_paths, allowlist_path)
    except ToolError as error:
        _error(str(error), arguments.json)
    _print_report(report, arguments.json)
    return 1 if report["summary"]["hits"] else 0  # type: ignore[index]


if __name__ == "__main__":
    raise SystemExit(main())
