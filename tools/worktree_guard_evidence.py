#!/usr/bin/env python3
"""Private, local, immutable evidence history for the Worktree Guard scenario.

This utility records observations; it does not approve tools, replay commands, or
promote a successful software check to a successful multi-client scenario.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import selectors
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA = 1
DEFAULT_SOURCE_LIMIT = 64 * 1024 * 1024
DEFAULT_TOTAL_LIMIT = 512 * 1024 * 1024
PUMP_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
)
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SECRET_KEY = re.compile(
    r"^(?:(?:access|refresh|session|auth|bearer|csrf|id)[_-]?)?token$|"
    r"^[a-z][a-z0-9_-]*(?:api[_-]?key|session[_-]?token|access[_-]?token|"
    r"refresh[_-]?token|secret[_-]?access[_-]?key)$|"
    r"^[a-z0-9_]+_TOKEN$|"
    r"^(?:password|passwd|secret|client[_-]?secret|credential|credential[_-]?digest|"
    r"receipt[_-]?nonce|nonce|authorization|api[_-]?key|x-api-key|private[_-]?key|"
    r"(?:aws[_-]?)?(?:secret[_-]?)?access[_-]?key(?:[_-]?id)?|cookie|set-cookie)$",
    re.I,
)
INLINE = (
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+([A-Za-z0-9_./+=:%-]+)"),
    re.compile(
        r"""(?ix)(?:password|passwd|secret|token|receipt[_-]?nonce|api[_-]?key|"""
        r"""access[_-]?key|credential)\s*["']?\s*[:=]\s*["']?([^\s"'&,;<>\\]{8,})"""
    ),
    re.compile(r"\b((?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,})"),
    # An AWS access key ID; the prefix is spelled with a character class so the public-tree
    # gate (tools/assert_public_tree.py) does not read it as the word it denies.
    re.compile(r"\b(A[K]IA[A-Z0-9]{16})\b"),
    re.compile(r"\b(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://([^/\s:@]+:([^/\s@]+))@", re.I),
    re.compile(r"(-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----)", re.S),
)
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
    "uv-cache",
    "node_modules",
    "dist",
    "build",
    "bindings",
    "credentials",
    ".aws",
    ".ssh",
}
PRIVATE_TREE_NAMES = {
    "home",
    "homes",
    "client-home",
    "client-homes",
    "private-home",
    "private-homes",
    "retained-home",
    "retained-crew-home",
    "crew-home",
    "kiro-home",
    "kirocrew-home",
    "codex-home",
    "claude-home",
    "grok-home",
    "profiles",
    "private-profiles",
    ".claude",
    ".codex",
    ".kiro",
    ".kirocrew",
    ".grok",
    ".config",
    ".local",
}
TEXT_SUFFIXES = {
    ".json",
    ".jsonl",
    ".md",
    ".txt",
    ".log",
    ".py",
    ".toml",
    ".lock",
    ".yaml",
    ".yml",
    ".stdout",
    ".stderr",
}
RUN_DIRS = {
    "native-evidence",
    "native-turns",
    "control-results",
    "commands",
    "artifact-snapshots",
    "project-snapshot",
    "qualification",
    "orchestration",
    "project-builds",
    "runtime-revisions",
    "startup-attempts",
    "status-history",
    "assignments",
    "crew-recovered-native-001",
}
PROJECT_DIRS = {"src", "tests", "docs"}
PROJECT_FILES = {
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "AGENTS.md",
    "LIVE_PROTOCOL.md",
    "acceptance-scope.json",
    "actor-roster.json",
    "scenario.json",
    "CONTRACT.md",
    "ACTOR_ROSTER.json",
}
# The script's bytes travel through a private anonymous descriptor, not argv or
# stdin. Keep normal script arguments, __main__, __file__ and sibling imports.
PINNED_BOOTSTRAP = """\
def _execute_pinned():
    import os
    import sys
    from importlib.machinery import SourceFileLoader
    with os.fdopen(int(sys.argv[1]), "rb") as source:
        code = source.read()
    sys.argv = sys.argv[2:]
    sys.path[0] = os.path.dirname(sys.argv[0])
    sys.dont_write_bytecode = True
    namespace = sys.modules["__main__"].__dict__
    namespace.pop("_execute_pinned")
    namespace.update(__file__=sys.argv[0], __cached__=None,
                     __loader__=SourceFileLoader("__main__", sys.argv[0]))
    exec(compile(code, sys.argv[0], "exec"), namespace)
_execute_pinned()
"""


class EvidenceError(Exception):
    """A bounded, non-secret error code safe to report to an operator."""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode()


def identifier(value: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise EvidenceError("invalid_identifier")
    return value


def canonical(path: Path) -> Path:
    """Reject symlinks in every existing component, including directory aliases."""
    if not path.is_absolute() or ".." in path.parts:
        raise EvidenceError("absolute_nontraversing_path_required")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise EvidenceError("symlink_refused")
    return path.resolve()


def require_private_directory(path: Path) -> None:
    """Check ownership and permissions on the directory actually opened."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(fd)
        finally:
            os.close(fd)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise EvidenceError("source_read_failed") from error
    except OSError as error:
        raise EvidenceError("run_root_must_be_private_and_owned") from error
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise EvidenceError("run_root_must_be_private_and_owned")


def read(path: Path, limit: int) -> bytes:
    """Read an unchanged, singly linked regular file through no-follow descriptors."""
    canonical(path)
    try:
        directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.parts[1:-1]:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise EvidenceError("non_regular_source")
            if before.st_nlink != 1:
                raise EvidenceError("hard_linked_source_refused")
            if before.st_size > limit:
                raise EvidenceError("source_byte_limit_exceeded")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise EvidenceError("source_read_failed") from error
    if len(data) > limit:
        raise EvidenceError("source_byte_limit_exceeded")
    if after.st_nlink != 1:
        raise EvidenceError("hard_linked_source_refused")
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise EvidenceError("source_changed_during_read")
    return data


def write(path: Path, data: bytes) -> None:
    """Create private evidence through no-follow directory descriptors."""
    canonical(path)
    try:
        directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.parts[1:-1]:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            fd = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
    except OSError as error:
        raise EvidenceError("evidence_write_failed") from error


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=60, check=False
    )
    if check and result.returncode:
        # Hook diagnostics may contain arbitrary credentials: never print them.
        raise EvidenceError("git_operation_failed:" + args[0])
    return result


def clean_history(history: Path, root: Path, project: Path) -> None:
    canonical(history)
    metadata = history.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise EvidenceError("history_repository_must_be_private_and_owned")
    if not (history / ".git").is_dir() or (history / ".git").is_symlink():
        raise EvidenceError("standalone_history_repository_required")
    actual = git(history, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if Path(actual) != history:
        raise EvidenceError("history_repository_root_mismatch")
    if git(history, "remote").stdout.strip():
        raise EvidenceError("history_repository_must_have_no_remotes")
    source_repo = git(Path(__file__).parent, "rev-parse", "--show-toplevel", check=False)
    excluded = [root, project]
    if source_repo.returncode == 0:
        excluded.append(Path(source_repo.stdout.decode().strip()))
    if any(history == parent or history.is_relative_to(parent) for parent in excluded):
        raise EvidenceError("history_must_be_separate_from_sources")
    if git(history, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout:
        raise EvidenceError("history_repository_not_clean")


class ObjectPairs(list[tuple[str, Any]]):
    """Discovery-only JSON object that retains every duplicate key occurrence."""


class Redactor:
    """Discover secrets before rendering; identical values get identical markers."""

    def __init__(self) -> None:
        self.secrets: set[str] = set()
        self.pattern: re.Pattern[str] | None = None
        self.replacements: dict[str, str] = {}
        self.key_collisions = 0

    def add(self, value: Any) -> None:
        if isinstance(value, ObjectPairs):
            for _, child in value:
                self.add(child)
        elif isinstance(value, dict):
            for child in value.values():
                self.add(child)
        elif isinstance(value, list):
            for child in value:
                self.add(child)
        if isinstance(value, str) and value and not value.startswith(("<redacted", "[redacted")):
            self.secrets.add(value)
            self.pattern = None

    def discover(self, value: Any) -> None:
        if isinstance(value, (dict, ObjectPairs)):
            pairs = list(value.items()) if isinstance(value, dict) else value
            headers = [
                child for key, child in pairs if key in {"name", "key"} and isinstance(child, str)
            ]
            for key, child in pairs:
                if (
                    SECRET_KEY.search(str(key))
                    and isinstance(child, str)
                    or str(key).lower().endswith(("_base64", "_b64"))
                    or str(key).lower()
                    in {"api_keys", "api-keys", "passwords", "tokens", "secrets"}
                ):
                    self.add(child)
                if key == "value" and any(SECRET_KEY.search(header) for header in headers):
                    self.add(child)
                self.discover(child)
        elif isinstance(value, list):
            for child in value:
                self.discover(child)
        elif isinstance(value, str):
            for pattern in INLINE:
                for match in pattern.finditer(value):
                    for credential in match.groups():
                        self.add(credential)
            if value.lstrip().startswith(("{", "[")):
                try:
                    self.discover(json.loads(value, object_pairs_hook=ObjectPairs))
                except (ValueError, RecursionError):
                    pass

    def text(self, value: str) -> str:
        if not self.secrets:
            return value
        if self.pattern is None:
            self.replacements = {}
            short_variants: set[str] = set()
            for secret in sorted(self.secrets):
                marker = f"<redacted:sha256:{sha(secret.encode('utf-8', 'surrogatepass'))}>"
                variants = {
                    secret,
                    json.dumps(secret)[1:-1],
                    quote(secret, safe="", errors="surrogatepass"),
                }
                for variant in variants:
                    if variant:
                        self.replacements[variant] = marker
                        if len(secret) < 8:
                            short_variants.add(variant)
            self.pattern = re.compile(
                "|".join(
                    (r"(?<!\w)" + re.escape(secret) + r"(?!\w)")
                    if secret in short_variants
                    else re.escape(secret)
                    for secret in sorted(self.replacements, key=len, reverse=True)
                )
            )
        return self.pattern.sub(lambda match: self.replacements[match.group()], value)

    def discover_yaml(self, text: str) -> None:
        """Recognize credential scalar lines only in explicitly selected YAML."""
        for match in re.finditer(r"(?m)^[ \t]*([\w.-]+):[ \t]*(.*?)\s*$", text):
            key, value = match.groups()
            if not SECRET_KEY.search(key) or not value:
                continue
            if value.startswith('"'):
                try:
                    value, _ = json.JSONDecoder().raw_decode(value)
                except ValueError:
                    continue
            elif value.startswith("'"):
                quoted = re.match(r"'((?:[^']|'')*)'", value)
                if quoted is None:
                    continue
                value = quoted.group(1).replace("''", "'")
            else:
                value = value.split(" #", 1)[0].rstrip()
                if value in {"null", "~", "true", "false", "|", ">"}:
                    continue
            self.add(value)

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            pairs = [(self.text(str(key)), self.redact(child)) for key, child in value.items()]
            if len({key for key, _ in pairs}) != len(pairs):
                self.key_collisions += 1
                return {
                    "representation": "redacted_object_pairs",
                    "limitation": "redacted_key_collision",
                    "pairs": [
                        {
                            "key": key,
                            "value": child,
                            "key_raw_sha256": sha(
                                str(original_key).encode("utf-8", "surrogatepass")
                            ),
                        }
                        for (key, child), original_key in zip(pairs, value, strict=True)
                    ],
                }
            return dict(pairs)
        if isinstance(value, list):
            return [self.redact(child) for child in value]
        if isinstance(value, str):
            return self.text(value)
        return value


@dataclass
class Source:
    path: str
    raw: bytes
    value: Any
    format: str
    error: str | None = None
    dependencies: dict[str, str] | None = None
    redaction_gaps: list[str] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return sha(self.raw)


def excluded(path: Path) -> bool:
    name = path.name.lower()
    return (
        any(part in SKIP_DIRS for part in path.parts)
        or name.startswith(".env")
        or name in {"config.json", "auth.json", "settings.json", "credentials.json", "token"}
        or name.endswith((".pem", ".key", ".sqlite", ".db", ".sqlite-wal", ".sqlite-shm"))
        or "capability" in name
    )


def walk(directory: Path) -> list[Path]:
    paths: list[Path] = []
    for parent, dirs, files in os.walk(directory, followlinks=False):
        base = Path(parent)
        # Decide scope by the entry name before inspecting or descending into
        # a retained profile. Even a broken profile symlink is outside evidence.
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in SKIP_DIRS and name.lower() not in PRIVATE_TREE_NAMES
        )
        files = [
            name
            for name in files
            if name.lower() not in PRIVATE_TREE_NAMES and not excluded(base / name)
        ]
        for name in dirs + files:
            path = base / name
            if path.is_symlink():
                raise EvidenceError("symlink_refused")
        for name in sorted(files):
            path = base / name
            if not excluded(path) and path.suffix in TEXT_SUFFIXES:
                paths.append(path)
    return paths


def decode_source(path: str, raw: bytes) -> Source:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return Source(path, raw, raw.decode("utf-8", "backslashreplace"), "text", "invalid_utf8")
    if path.endswith(".jsonl"):
        rows: list[Any] = []
        error = None
        for line in text.splitlines():
            try:
                rows.append(load_json(line))
            except (ValueError, RecursionError):
                rows.append({"unparsed_line": line})
                error = "unparsed_jsonl"
        return Source(path, raw, rows, "jsonl", error)
    if path.endswith(".json"):
        try:
            return Source(path, raw, load_json(text), "json")
        except (ValueError, RecursionError):
            return Source(path, raw, text, "text", "unparsed_json")
    return Source(path, raw, text, "text")


def load_json(data: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=pairs)


def pinned(
    root: Path, required: str | None, maximum: int, *, allow_mismatch: bool = False
) -> list[Path]:
    """Return validated source paths, preserving the capture/import helper API."""
    paths, _ = pinned_sources(root, required, maximum, allow_mismatch=allow_mismatch)
    return paths


def pinned_sources(
    root: Path, required: str | None, maximum: int, *, allow_mismatch: bool = False
) -> tuple[list[Path], bytes | None]:
    """Validate pins, enforce execution snapshot membership, and retain entry bytes."""
    snapshot = root / "source-snapshot"
    manifest_path = snapshot / "manifest.json"
    if required:
        try:
            mode = manifest_path.stat(follow_symlinks=False).st_mode
        except OSError as error:
            raise EvidenceError("source_read_failed") from error
        if not stat.S_ISREG(mode):
            raise EvidenceError("source_snapshot_unlisted_entry")
    manifest = load_json(read(manifest_path, maximum))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise EvidenceError("source_manifest_invalid")
    paths: list[Path] = [manifest_path]
    seen: set[str] = set()
    selected = None
    for entry in files:
        name = entry["path"]
        if not isinstance(name, str) or name in seen:
            raise EvidenceError("source_manifest_invalid")
        seen.add(name)
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or excluded(relative)
            or any(part.lower() in PRIVATE_TREE_NAMES for part in relative.parts)
        ):
            raise EvidenceError("source_manifest_path_refused")
        paths.append(snapshot / relative)
    if required and required not in seen:
        raise EvidenceError("required_source_pin_missing")
    if required:
        require_exact_snapshot(snapshot, paths)
    for entry, path in zip(files, paths[1:], strict=True):
        raw = read(path, maximum)
        if sha(raw) != entry["sha256"] and not allow_mismatch:
            raise EvidenceError("pinned_source_digest_mismatch")
        if entry["path"] == required:
            selected = raw
    return paths, selected


def require_exact_snapshot(snapshot: Path, paths: list[Path]) -> None:
    """Refuse entries outside the manifest through a no-follow directory walk."""
    files = {path.relative_to(snapshot) for path in paths}
    parents = {parent for path in files for parent in path.parents}

    def visit(directory_fd: int, relative: Path) -> None:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                name = relative / entry.name
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISREG(mode) and name in files:
                    continue
                if stat.S_ISDIR(mode) and name in parents and entry.name != "__pycache__":
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    try:
                        visit(child_fd, name)
                    finally:
                        os.close(child_fd)
                else:
                    raise EvidenceError("source_snapshot_unlisted_entry")

    try:
        fd = os.open(snapshot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            visit(fd, Path())
        finally:
            os.close(fd)
    except OSError as error:
        raise EvidenceError("source_snapshot_unlisted_entry") from error


def actor_store(root: Path, run_id: str, maximum: int) -> list[Source]:
    """Query only the selected run; raw DB/WAL files are hashed, never copied."""
    path = root / "bus/actors.sqlite"
    if not path.exists():
        return []
    raw = read(path, maximum)
    wal_path = path.with_name(path.name + "-wal")
    wal = read(wal_path, maximum) if wal_path.exists() else None
    dependencies = {"bus/actors.sqlite": sha(raw)}
    if wal is not None:
        dependencies["bus/actors.sqlite-wal"] = sha(wal)
    kinds = {
        "actor_runs": "actor_run",
        "actors": "actor_roster",
        "actor_messages": "actor_message",
        "actor_receipts": "actor_receipt",
        "actor_transport_observations": "actor_transport",
    }
    result: list[Source] = []
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, kind in kinds.items():
            if table not in tables:
                continue
            if table in {"actor_runs", "actors", "actor_messages"}:
                query = f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid"
            else:
                query = (
                    f"SELECT item.* FROM {table} AS item JOIN actor_messages AS message "
                    "ON item.message_id=message.message_id "
                    "WHERE message.run_id=? ORDER BY item.rowid"
                )
            rows = []
            projected_bytes = 0
            for row in db.execute(query, (run_id,)):
                value = dict(row)
                projected_bytes += len(encode(value))
                if projected_bytes > maximum:
                    raise EvidenceError("actor_projection_byte_limit_exceeded")
                rows.append(value)
            result.append(
                Source(
                    f"bus/actors.sqlite#{table}",
                    raw,
                    {"kind": kind, "rows": rows},
                    "sqlite_projection",
                    dependencies=dependencies,
                )
            )
        db.rollback()
    if read(path, maximum) != raw or (
        (read(wal_path, maximum) if wal_path.exists() else None) != wal
    ):
        raise EvidenceError("actor_store_changed_during_capture")
    return result


def gather(
    root: Path, plan: dict[str, Any], maximum: int, total: int, *, allow_mismatch: bool = False
) -> list[Source]:
    paths = set(pinned(root, None, maximum, allow_mismatch=allow_mismatch))
    for path in root.iterdir():
        if path.name.lower() in PRIVATE_TREE_NAMES:
            continue
        if path.is_symlink():
            raise EvidenceError("symlink_refused")
        if path.is_file() and path.suffix in TEXT_SUFFIXES and not excluded(path):
            paths.add(path)
        elif (
            path.name in RUN_DIRS or path.name.startswith("crew-recovered-native-")
        ) and path.is_dir():
            paths.update(walk(path))
    project = canonical(Path(plan["project"]))
    if project.is_dir():
        for path in project.iterdir():
            if path.name in PROJECT_DIRS and path.is_dir():
                paths.update(walk(path))
            elif path.name in PROJECT_FILES and path.is_file():
                paths.add(path)
    sources: list[Source] = []
    size = 0
    for path in sorted(paths):
        raw = read(path, maximum)
        size += len(raw)
        if size > total:
            raise EvidenceError("total_byte_limit_exceeded")
        relative = (
            "project/" + str(path.relative_to(project))
            if path.is_relative_to(project)
            else str(path.relative_to(root))
        )
        sources.append(decode_source(relative, raw))
    sources.extend(actor_store(root, plan["run_id"], maximum))
    sources.append(
        decode_source(
            "collector/worktree_guard_evidence.py", read(Path(__file__).absolute(), maximum)
        )
    )
    if allow_mismatch:
        manifest = load_json(read(root / "source-snapshot/manifest.json", maximum))
        expected = {"source-snapshot/" + item["path"]: item["sha256"] for item in manifest["files"]}
        for source in sources:
            if source.path in expected and source.digest != expected[source.path]:
                source.error = "pinned_source_digest_mismatch"
    if sum(len(source.raw) for source in sources) > total:
        raise EvidenceError("total_byte_limit_exceeded")
    return sources


def event_rows(source: Source) -> list[tuple[str, str, Any]]:
    """Return every source occurrence, without pretending cross-source causal order."""
    value = source.value
    if source.format == "sqlite_projection":
        return [(value["kind"], f"/rows/{i}", row) for i, row in enumerate(value["rows"])]
    if source.path == "actor-evidence.json" and isinstance(value, dict):
        rows = []
        for key, kind in (
            ("run", "actor_run"),
            ("roster", "actor_roster"),
            ("messages", "actor_message"),
            ("receipts", "actor_receipt"),
            ("transport_observations", "actor_transport"),
        ):
            data = value.get(key, [])
            if not isinstance(data, list):
                data = [data]
            rows.extend((kind, f"/{key}/{i}", row) for i, row in enumerate(data))
        return rows
    if source.format == "jsonl":
        if source.path.startswith("orchestration/"):
            kind = "coordinator_io"
        elif source.path.startswith("native-evidence/"):
            kind = "native_event"
        elif source.path.startswith("project-builds/"):
            kind = "project_build_io"
        elif source.path.startswith(("control-results/", "native-turns/", "commands/")):
            kind = "control_observation"
        else:
            kind = "recorded_event"
        return [(kind, f"/{i}", row) for i, row in enumerate(value)]
    if source.path.startswith(("control-results/", "native-turns/", "commands/")):
        return [("control_observation", "", value)]
    if source.path.startswith("native-evidence/"):
        return [("native_observation", "", value)]
    return []


def classify(sources: list[Source], plan: dict[str, Any], runner: Any) -> dict[str, Any]:
    by_path = {source.path: source for source in sources}
    gaps = [f"{source.path}:{source.error}" for source in sources if source.error]
    gaps.extend(f"{source.path}:{gap}" for source in sources for gap in source.redaction_gaps)
    for source in sources:
        if source.path.endswith("/runner.json") and isinstance(source.value, dict):
            completeness = source.value.get("stream_completeness", "")
            if not isinstance(completeness, str):
                gaps.append(f"{source.path}:invalid_runner_completeness")
            elif source.value.get("error") or completeness.startswith("partial"):
                gaps.append(f"{source.path}:runner_stream_incomplete")
    if not any(source.path.startswith("orchestration/") for source in sources):
        gaps.append("coordinator_stream_missing")
    if "actor-evidence.json" not in by_path:
        if not any(source.format == "sqlite_projection" for source in sources):
            gaps.append("actor_evidence_missing")
    for client in plan.get("clients", []):
        if not any(
            source.path.startswith("native-evidence/") and client in source.path
            for source in sources
        ):
            gaps.append(f"native_trace_missing:{client}")
    if not any(source.path.startswith("project/") for source in sources):
        gaps.append("project_working_files_missing")
    declarations = []
    for path in ("attempt-failure-decision.json", "scenario-result.json", "acceptance-result.json"):
        source = by_path.get(path)
        if source and isinstance(source.value, dict):
            observed_status = source.value.get("status", "unknown")
            status = declared_status(observed_status)
            if status == "unknown" and observed_status != "unknown":
                gaps.append(f"{path}:invalid_scenario_status")
            declarations.append(
                {
                    "source": path,
                    "raw_source_sha256": source.digest,
                    "status": status,
                    "observed_status": observed_status,
                }
            )
        elif source:
            gaps.append(f"{path}:invalid_scenario_declaration")
    distinct = {row["status"] for row in declarations}
    scenario = {
        "status": "unknown"
        if any("invalid_scenario_" in gap for gap in gaps)
        else next(iter(distinct))
        if len(distinct) == 1
        else ("conflicting" if distinct else "unknown"),
        "basis": "explicit source declarations only; not independently certified",
        "declarations": declarations,
    }
    checks = [
        {
            "source": source.path,
            "raw_source_sha256": source.digest,
            "status": source.value.get("status", "unknown"),
            "checks": source.value.get("checks", []),
            "errors": source.value.get("errors", []),
        }
        for source in sources
        if source.path.endswith("project-checks.json") and isinstance(source.value, dict)
    ]
    for check in checks:
        if declared_status(check["status"]) == "unknown" and check["status"] != "unknown":
            check["observed_status"] = check["status"]
            check["status"] = "unknown"
            gaps.append(f"{check['source']}:invalid_project_check_status")
    current = [
        check
        for check in checks
        if runner and check["source"].startswith(runner.get("output_relative", "") + "/")
    ]
    # Without this invocation's runner, histories are incomparable declarations:
    # filename order is not proof that one report describes the current files.
    selected = current if runner and runner.get("mode") == "build" else []
    project_status = selected[0]["status"] if len(selected) == 1 else "unknown"
    if runner and runner["mode"] == "build" and runner.get("exit_code") != 0:
        project_status = "failed"
    return {
        "schema_version": SCHEMA,
        "run_id": plan["run_id"],
        "scenario_acceptance": scenario,
        "project_checks": {"status": project_status, "reports": checks},
        "runner": runner,
        "trace": {
            "completeness": "partial" if gaps else "complete_for_selected_sources",
            "gaps": gaps,
            "cross_source_order": "not inferred",
        },
        "cleanup": {
            "observations": [
                {"source": source.path, "raw_source_sha256": source.digest, "value": source.value}
                for source in sources
                if source.path.endswith("cleanup-final-assessment.json")
            ],
            "status": "recorded"
            if any(source.path.endswith("cleanup-final-assessment.json") for source in sources)
            else "unknown",
        },
    }


def declared_status(value: Any) -> str:
    """Only recognized status labels may become generated outcome metadata."""
    labels = {
        "unknown",
        "passed",
        "pass",
        "failed",
        "fail",
        "failed_incomplete",
        "complete",
        "completed",
        "success",
        "succeeded",
        "running",
        "active",
        "blocked",
        "cancelled",
        "canceled",
        "not_run",
        "unverified",
        "partial",
        "incomplete",
    }
    return value if isinstance(value, str) and value.lower() in labels else "unknown"


def verify_git_objects(history: Path, intended: dict[str, bytes], committed: bool) -> None:
    algorithm = git(history, "rev-parse", "--show-object-format").stdout.decode().strip()
    expected = {
        path: hashlib.new(algorithm, b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        for path, data in intended.items()
    }
    if committed:
        raw = git(history, "ls-tree", "-r", "-z", "HEAD", "--", *intended).stdout
    else:
        raw = git(history, "ls-files", "--stage", "-z", "--", *intended).stdout
    observed = {}
    for line in raw.split(b"\0"):
        if line:
            metadata, path = line.split(b"\t", 1)
            fields = metadata.decode().split()
            observed[path.decode()] = fields[2] if committed else fields[1]
    if observed != expected:
        raise EvidenceError("committed_content_changed" if committed else "staged_content_changed")


def exchanges(sources: list[Source], trace: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    matrices: list[dict[str, Any]] = [
        {"source": source.path, "raw_source_sha256": source.digest, "value": source.value}
        for source in sources
        if source.path.endswith("ACTOR_ROSTER.json")
        and isinstance(source.value, dict)
        and source.value.get("run_id") == run_id
    ]
    actor_rows = [row for row in trace if row["kind"].startswith("actor_")]
    gaps: list[str] = []
    messages: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for row in actor_rows:
        event = row["event"]
        if row["kind"] in {"actor_message", "actor_receipt"}:
            if not isinstance(event, dict) or not isinstance(event.get("message_id"), str):
                gaps.append(f"{row['source_path']}:{row['source_pointer']}:invalid_{row['kind']}")
            elif row["kind"] == "actor_message":
                messages.append(event)
            else:
                receipts.append(row)

    def body(message: dict[str, Any]) -> dict[str, Any]:
        envelope = message.get("envelope", message.get("payload", {}))
        if isinstance(envelope, str):
            try:
                envelope = load_json(envelope)
            except ValueError:
                return {}
        if not isinstance(envelope, dict):
            return {}
        result = envelope.get("body", envelope)
        return result if isinstance(result, dict) else {}

    observations = []
    for matrix in matrices:
        required_items = matrix["value"].get("exchanges", [])
        if not isinstance(required_items, list):
            gaps.append(f"{matrix['source']}:/exchanges:invalid_required_exchanges")
            continue
        for ordinal, required in enumerate(required_items):
            if not isinstance(required, dict) or not isinstance(required.get("exchange_id"), str):
                gaps.append(f"{matrix['source']}:/exchanges/{ordinal}:invalid_required_exchange")
                continue
            matched = [
                message
                for message in messages
                if body(message).get("exchange_id") == required["exchange_id"]
            ]
            request_ids = sorted(
                {
                    message["message_id"]
                    for message in matched
                    if message.get("kind") == "review_request"
                }
            )
            reply_ids = sorted(
                {
                    message["message_id"]
                    for message in messages
                    if message.get("reply_to") in request_ids
                }
            )
            observations.append(
                {
                    "matrix_source": matrix["source"],
                    "required": required,
                    "observed_request_ids": request_ids,
                    "observed_reply_ids": reply_ids,
                    "receipt_occurrences": [
                        row
                        for row in receipts
                        if row["event"].get("message_id") in request_ids + reply_ids
                    ],
                    "validation": "ID references only; native and semantic joins unassessed",
                }
            )
    return {
        "schema_version": SCHEMA,
        "required_matrices": matrices,
        "matrix_observations": observations,
        "recorded_rows": actor_rows,
        "acceptance_assessed": False,
        "derivation_status": "partial" if gaps else "ID_references_only",
        "gaps": gaps,
    }


class CapturedViews:
    """Sanitize observations at explicit fields, never the enclosing evidence schema."""

    def __init__(self, redactor: Redactor, sources: list[Source]) -> None:
        self.redactor = redactor
        self.labels = {source.path: redactor.text(source.path) for source in sources}

    def gap(self, value: str) -> str:
        for original, label in self.labels.items():
            if value.startswith(original + ":"):
                return label + value[len(original) :]
        return value

    def source(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "source": self.labels[row["source"]],
            "value": self.redactor.redact(row["value"]),
        }

    def trace(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "source_path": self.labels[row["source_path"]],
            "event": self.redactor.redact(row["event"]),
        }

    def checks(self, data: dict[str, Any]) -> dict[str, Any]:
        reports = []
        for report in data["reports"]:
            row = {**report, "source": self.labels[report["source"]]}
            for key in ("checks", "errors", "observed_status"):
                if key in row:
                    row[key] = self.redactor.redact(row[key])
            reports.append(row)
        return {**data, "reports": reports}

    def outcome(self, data: dict[str, Any]) -> dict[str, Any]:
        scenario = data["scenario_acceptance"]
        declarations = [
            {
                **row,
                "source": self.labels[row["source"]],
                "observed_status": self.redactor.redact(row["observed_status"]),
            }
            for row in scenario["declarations"]
        ]
        runner = data["runner"]
        if runner is not None:
            runner = dict(runner)
            if "argv" in runner:
                runner["argv"] = self.redactor.redact(runner["argv"])
        return {
            **data,
            "scenario_acceptance": {**scenario, "declarations": declarations},
            "project_checks": self.checks(data["project_checks"]),
            "trace": {**data["trace"], "gaps": [self.gap(gap) for gap in data["trace"]["gaps"]]},
            "cleanup": {
                **data["cleanup"],
                "observations": [self.source(row) for row in data["cleanup"]["observations"]],
            },
            "runner": runner,
        }

    def exchanges(self, data: dict[str, Any]) -> dict[str, Any]:
        observations = []
        for row in data["matrix_observations"]:
            observations.append(
                {
                    **row,
                    "matrix_source": self.labels[row["matrix_source"]],
                    "required": self.redactor.redact(row["required"]),
                    "observed_request_ids": self.redactor.redact(row["observed_request_ids"]),
                    "observed_reply_ids": self.redactor.redact(row["observed_reply_ids"]),
                    "receipt_occurrences": [
                        self.trace(receipt) for receipt in row["receipt_occurrences"]
                    ],
                }
            )
        return {
            **data,
            "required_matrices": [self.source(row) for row in data["required_matrices"]],
            "matrix_observations": observations,
            "recorded_rows": [self.trace(row) for row in data["recorded_rows"]],
            "gaps": [self.gap(gap) for gap in data["gaps"]],
        }


def capture(args: argparse.Namespace, runner: Any = None) -> dict[str, Any]:
    root = canonical(Path(args.run_root))
    history = canonical(Path(args.history_repo))
    plan = load_json(read(root / "run-plan.json", args.max_source_bytes))
    run_id = identifier(plan["run_id"])
    capture_id = identifier(args.capture_id)
    clean_history(history, root, canonical(Path(plan["project"])))
    lock_path = canonical(history / ".git/worktree-guard-evidence.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        clean_history(history, root, canonical(Path(plan["project"])))
        destination = history / "runs" / run_id / "snapshots" / capture_id
        canonical(destination)
        if destination.exists():
            raise EvidenceError("capture_already_exists")
        sources = gather(
            root,
            plan,
            args.max_source_bytes,
            args.max_total_bytes,
            allow_mismatch=bool(runner and runner.get("pid")),
        )
        redactor = Redactor()
        for source in sources:
            redactor.discover(source.value)
            redactor.discover(source.path)
            if source.path.endswith((".yaml", ".yml")) and isinstance(source.value, str):
                redactor.discover_yaml(source.value)
        redactor.discover(runner)
        views = CapturedViews(redactor, sources)
        redacted_values = []
        for source in sources:
            before = redactor.key_collisions
            redacted_values.append(redactor.redact(source.value))
            if redactor.key_collisions != before:
                source.redaction_gaps.append("redacted_key_collision")
        inventory, trace = [], []
        for index, source in enumerate(sources):
            snapshot_path = f"sources/{index:06d}.json"
            inventory.append(
                {
                    "source_path": views.labels[source.path],
                    "raw_source_path_sha256": sha(source.path.encode()),
                    "raw_source_sha256": source.digest,
                    "raw_bytes": len(source.raw),
                    "format": source.format,
                    "snapshot_path": snapshot_path,
                    "read_status": source.error or "read",
                    "raw_dependencies": source.dependencies,
                    "redaction_gaps": source.redaction_gaps,
                }
            )
            for ordinal, (kind, pointer, event) in enumerate(event_rows(source), 1):
                trace.append(
                    {
                        "trace_ordinal": len(trace) + 1,
                        "source_path": source.path,
                        "source_ordinal": ordinal,
                        "source_pointer": pointer,
                        "raw_source_sha256": source.digest,
                        "kind": kind,
                        "event": event,
                    }
                )
        counts = dict(Counter(row["kind"] for row in trace))
        outcome = classify(sources, plan, runner)
        exchange_report = exchanges(sources, trace, run_id)
        outcome["trace"]["gaps"].extend(exchange_report["gaps"])
        if outcome["trace"]["gaps"]:
            outcome["trace"]["completeness"] = "partial"
        outcome["trace"]["counts"] = counts
        outcome["trace"]["unique_actor_message_ids"] = len(
            {
                row["event"].get("message_id")
                for row in trace
                if row["kind"] == "actor_message"
                and isinstance(row["event"], dict)
                and isinstance(row["event"].get("message_id"), str)
            }
        )

        def selected(suffix: str) -> list[dict[str, Any]]:
            return [
                views.source(
                    {
                        "source": source.path,
                        "raw_source_sha256": source.digest,
                        "value": source.value,
                    }
                )
                for source in sources
                if source.path.endswith(suffix)
            ]

        outcome = views.outcome(outcome)
        payloads: dict[str, bytes] = {
            "outcome.json": encode(outcome),
            "trace.jsonl": b"".join(
                json.dumps(views.trace(row), sort_keys=True).encode() + b"\n" for row in trace
            ),
            "exchanges.json": encode(views.exchanges(exchange_report)),
            "artifacts.json": encode(selected("artifact-manifest.json")),
            "checks.json": encode(outcome["project_checks"]),
        }
        report = (
            f"# Worktree Guard evidence: {run_id}\n\n"
            f"Capture: {capture_id}\n\n"
            f"Scenario acceptance: {outcome['scenario_acceptance']['status']} "
            "(declared by source; not certified by capture).\n\n"
            f"Project checks: {outcome['project_checks']['status']}.\n\n"
            f"Trace completeness: {outcome['trace']['completeness']}.\n\n"
            f"Source files: {len(sources)}. Recorded occurrences: {len(trace)}.\n\n"
            "Missing/limited evidence: " + ", ".join(outcome["trace"]["gaps"]) + "\n\n"
            "Raw originals remain outside this repository. Sources are redacted views. "
            "Cross-source ordering and scenario completion are not inferred.\n"
        )
        payloads["report.md"] = report.encode()
        for item, redacted_value in zip(inventory, redacted_values, strict=True):
            payloads[item["snapshot_path"]] = encode({**item, "content": redacted_value})
        manifest = {
            "schema_version": SCHEMA,
            "run_id": run_id,
            "capture_id": capture_id,
            "captured_at": now(),
            "profile": plan.get("profile", "unknown"),
            "scenario": plan.get("scenario", "worktree-guard"),
            "clients": plan.get("clients", []),
            "expected_directed_reviews": plan.get("expected_directed_reviews"),
            "expected_review_messages": plan.get("expected_review_messages"),
            "collector": next(
                {
                    "source_path": item["source_path"],
                    "raw_source_sha256": item["raw_source_sha256"],
                    "snapshot_path": item["snapshot_path"],
                    "representation": "redacted view",
                }
                for item, source in zip(inventory, sources, strict=True)
                if source.path == "collector/worktree_guard_evidence.py"
            ),
            "sources": inventory,
            "selection": {
                "run_directories": sorted(RUN_DIRS),
                "project_directories": sorted(PROJECT_DIRS),
                "credential_files": "excluded",
                "excluded_private_tree_names": sorted(PRIVATE_TREE_NAMES),
                "excluded_subtree_handling": "names pruned before symlink inspection or reads",
                "max_source_bytes": args.max_source_bytes,
                "max_total_bytes": args.max_total_bytes,
                "scope": "selected recorded evidence only; no live collection",
                "recovery_directory_prefixes": ["crew-recovered-native-"],
                "external_references": "references retained; external paths not dereferenced",
                "redaction_boundary": "source observations; generated schema and hashes retained",
            },
            "files": [
                {"path": path, "sha256": sha(data), "bytes": len(data)}
                for path, data in sorted(payloads.items())
            ],
        }
        payloads["manifest.json"] = encode(manifest)
        index_path = history / "runs/index.json"
        canonical(index_path)
        index_value: dict[str, Any] = (
            json.loads(read(index_path, args.max_source_bytes))
            if index_path.exists()
            else {"schema_version": SCHEMA, "runs": {}}
        )
        entry = index_value["runs"].setdefault(run_id, {"captures": []})
        entry["captures"].append(
            {
                "capture_id": capture_id,
                "captured_at": manifest["captured_at"],
                "manifest_sha256": sha(payloads["manifest.json"]),
                "scenario_acceptance": outcome["scenario_acceptance"]["status"],
                "project_checks": outcome["project_checks"]["status"],
                "trace_counts": counts,
                "trace_completeness": outcome["trace"]["completeness"],
            }
        )
        entry["latest_capture"] = capture_id
        # Materialize only after all reads and redaction succeed.
        for name, data in payloads.items():
            write(destination / name, data)
        index_bytes = encode(index_value)
        fd, temporary_name = tempfile.mkstemp(prefix=".index-", dir=history / "runs")
        with os.fdopen(fd, "wb") as stream:
            stream.write(index_bytes)
        os.replace(temporary_name, index_path)
        relative = str(destination.relative_to(history))
        intended = {f"{relative}/{name}": data for name, data in payloads.items()}
        intended["runs/index.json"] = index_bytes
        git(history, "add", "--", relative, "runs/index.json")
        staged = git(history, "diff", "--cached", "--name-only", "-z").stdout
        if set(staged.decode().strip("\0").split("\0")) != set(intended):
            raise EvidenceError("unexpected_staged_paths")
        verify_git_objects(history, intended, committed=False)
        git(
            history,
            "commit",
            "--only",
            "-m",
            f"chore(evidence): record {run_id} {capture_id}",
            "--",
            relative,
            "runs/index.json",
        )
        verify_git_objects(history, intended, committed=True)
        committed_paths = git(
            history, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", "HEAD"
        ).stdout
        if set(committed_paths.decode().strip("\0").split("\0")) != set(intended):
            raise EvidenceError("unexpected_committed_paths")
        clean_history(history, root, canonical(Path(plan["project"])))
        return {
            "capture_id": capture_id,
            "run_id": run_id,
            "commit": git(history, "rev-parse", "HEAD").stdout.decode().strip(),
            "snapshot": str(destination),
            "outcome": outcome,
        }


def validate_pass_env(names: list[str]) -> None:
    """Accept explicit environment names without permitting code-loading overrides."""
    for name in names:
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            or name.startswith(("PYTHON", "LD_", "DYLD_"))
            or name in {"__PYVENV_LAUNCHER__", "ANSWERBACK_ACCEPTANCE_ROOT"}
        ):
            raise EvidenceError("pass_env_name_refused")


def pump(
    argv: list[str],
    root: Path,
    logs: Path,
    timeout: float | None,
    maximum: int,
    interactive: bool,
    *,
    source: bytes | None = None,
    pass_env: list[str] | None = None,
) -> dict[str, Any]:
    """Journal stream chunks before forwarding; execute no content read from a log."""
    validate_pass_env(pass_env or [])
    requested = set(pass_env or [])
    environment = {
        key: os.environ[key] for key in (*PUMP_ENV_KEYS, *requested) if key in os.environ
    }
    environment["ANSWERBACK_ACCEPTANCE_ROOT"] = str(root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    record: dict[str, Any] = {
        "argv": argv,
        "started_at": now(),
        "exit_code": None,
        "error": None,
        "controller_descendant_cleanup": "not assessed",
        "environment_passed": sorted(environment),
        "environment_requested_unset": sorted(requested.difference(environment)),
    }
    pending = bytearray()
    input_eof = not interactive
    total_by_stream: Counter[str] = Counter()
    sequence = 0
    process: subprocess.Popen[bytes] | None = None
    with (
        (logs / "runner.stdout").open("xb") as stdout,
        (logs / "runner.stderr").open("xb") as stderr,
        (logs / "events.jsonl").open("xb") as journal,
        selectors.DefaultSelector() as selector,
        contextlib.ExitStack() as resources,
    ):

        def observed(stream: str, data: bytes, **extra: Any) -> None:
            nonlocal sequence
            sequence += 1
            # Raw private journal uses lossless surrogate escapes. The tracked view
            # is JSON escaped and sanitized; byte identity is independently retained.
            event = {
                "ordinal": sequence,
                "observed_at": now(),
                "monotonic_ns": time.monotonic_ns(),
                "stream": stream,
                "bytes": len(data),
                "sha256": sha(data),
                "text": data.decode("utf-8", "surrogateescape"),
                **extra,
            }
            journal.write(json.dumps(event, ensure_ascii=True).encode() + b"\n")
            journal.flush()
            os.fsync(journal.fileno())
            total_by_stream[stream] += len(data)
            if total_by_stream[stream] > maximum:
                raise EvidenceError("runner_stream_byte_limit_exceeded")

        try:
            command = argv
            descriptors: tuple[int, ...] = ()
            if source is not None:
                snapshot = resources.enter_context(tempfile.TemporaryFile(dir=logs))
                snapshot.write(source)
                snapshot.seek(0)
                descriptors = (snapshot.fileno(),)
                command = [argv[0], "-B", "-c", PINNED_BOOTSTRAP, str(snapshot.fileno()), *argv[1:]]
                record["source_transport"] = "verified_bytes_via_descriptor"
            process = subprocess.Popen(
                command,
                cwd=root,
                env=environment,
                pass_fds=descriptors,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert process.stdin is not None and process.stdout is not None
            assert process.stderr is not None
            record["pid"] = process.pid
            record["native_started_at"] = now()
            for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            os.set_blocking(process.stdin.fileno(), False)
            if interactive:
                selector.register(sys.stdin, selectors.EVENT_READ, "input")
            else:
                process.stdin.close()
            start, exited_at = time.monotonic(), None
            while selector.get_map():
                if timeout is not None and time.monotonic() - start >= timeout:
                    raise EvidenceError("runner_timeout")
                if process.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    if interactive:
                        with contextlib.suppress(KeyError, ValueError):
                            selector.unregister(sys.stdin)
                    with contextlib.suppress(KeyError, ValueError):
                        selector.unregister(process.stdin)
                    if pending:
                        observed("stdin_undelivered", bytes(pending))
                        pending.clear()
                    if time.monotonic() - exited_at > 5:
                        raise EvidenceError("descendant_output_pipe_still_open")
                for key, _ in selector.select(0.1):
                    if key.data == "write_input":
                        try:
                            count = os.write(key.fd, pending)
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            observed("stdin_undelivered", bytes(pending))
                            pending.clear()
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                            continue
                        observed("stdin", bytes(pending[:count]))
                        del pending[:count]
                        if not pending:
                            selector.unregister(key.fileobj)
                            if input_eof:
                                process.stdin.close()
                        continue
                    try:
                        data = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if key.data == "input":
                        if data:
                            observed("stdin_received", data)
                            pending.extend(data)
                            if len(pending) > 8 * 1024 * 1024:
                                raise EvidenceError("stdin_pending_byte_limit_exceeded")
                            if process.stdin not in [
                                item.fileobj for item in selector.get_map().values()
                            ]:
                                selector.register(
                                    process.stdin, selectors.EVENT_WRITE, "write_input"
                                )
                        else:
                            input_eof = True
                            selector.unregister(key.fileobj)
                            if not pending:
                                process.stdin.close()
                    elif not data:
                        selector.unregister(key.fileobj)
                    else:
                        target = stdout if key.data == "stdout" else stderr
                        target.write(data)
                        target.flush()
                        observed(key.data, data)
                        if interactive:
                            output = (
                                sys.stdout.buffer if key.data == "stdout" else sys.stderr.buffer
                            )
                            output.write(data)
                            output.flush()
            record["exit_code"] = process.wait(timeout=5)
        except (Exception, KeyboardInterrupt) as error:
            record["error"] = (
                str(error) if isinstance(error, EvidenceError) else type(error).__name__
            )
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                record["exit_code"] = process.returncode
            record["stream_completeness"] = "partial_after_runner_error"
        finally:
            if interactive:
                # Release a producer still writing when the controller ends.
                # This utility process will never read another command.
                sys.stdin.close()
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            if pending:
                observed("stdin_undelivered", bytes(pending))
                pending.clear()
    record["ended_at"] = now()
    record.setdefault("stream_completeness", "complete_for_controller_process")
    return record


def execute_and_capture(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    validate_pass_env(args.pass_env)
    root = canonical(Path(args.run_root))
    require_private_directory(root)
    require_private_directory(root / "source-snapshot")
    history = canonical(Path(args.history_repo))
    plan = load_json(read(root / "run-plan.json", args.max_source_bytes))
    identifier(plan["run_id"])
    identifier(args.capture_id)
    clean_history(history, root, canonical(Path(plan["project"])))
    target = "verify_project.py" if args.command == "build" else "run_controller.py"
    # Refuse unsafe preflight before any native execution or durable reservation.
    _, source = pinned_sources(root, target, args.max_source_bytes)
    assert source is not None
    if (history / "runs" / plan["run_id"] / "snapshots" / args.capture_id).exists():
        raise EvidenceError("capture_already_exists")
    parent = root / ("project-builds" if args.command == "build" else "orchestration")
    canonical(parent)
    parent.mkdir(mode=0o700, exist_ok=True)
    output = parent / args.capture_id
    if output.exists():
        raise EvidenceError("runner_output_already_exists")
    work = Path(tempfile.mkdtemp(prefix=".runner-", dir=parent))
    runner: dict[str, Any] = {
        "mode": args.command,
        "exit_code": None,
        "error": None,
        "output_relative": str(output.relative_to(root)),
        "source_path": "source-snapshot/" + target,
        "source_sha256": sha(source),
    }
    argv = [args.python, str(root / "source-snapshot" / target)]
    if args.command == "build":
        argv.extend(
            [
                "--plan",
                str(root / "run-plan.json"),
                "--output",
                str(output),
                "--python",
                args.python,
                "--timeout",
                str(args.check_timeout),
            ]
        )
        if (root / "artifact-manifest.json").exists():
            argv.extend(["--announcements", str(root / "artifact-manifest.json")])
    try:
        runner.update(
            pump(
                argv,
                root,
                work,
                args.timeout,
                args.max_source_bytes,
                args.command == "supervise",
                source=source,
                pass_env=args.pass_env,
            )
        )
        pinned(root, target, args.max_source_bytes)
    except (Exception, KeyboardInterrupt) as error:
        runner["error"] = str(error) if isinstance(error, EvidenceError) else type(error).__name__
    finally:
        canonical(output)
        output.mkdir(mode=0o700, exist_ok=True)
        for path in work.iterdir():
            if (output / path.name).exists():
                raise EvidenceError("runner_artifact_conflict")
            path.rename(output / path.name)
        work.rmdir()
        write(output / "runner.json", encode(runner))
        result = capture(args, runner)
    exit_code = runner["exit_code"]
    if runner["error"]:
        return result, 2
    if exit_code != 0:
        return result, exit_code if isinstance(exit_code, int) and 0 < exit_code < 126 else 2
    if args.command == "build" and result["outcome"]["project_checks"]["status"] != "passed":
        return result, 2
    return result, 0


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    for command in ("capture", "build", "supervise"):
        sub = commands.add_parser(command)
        sub.add_argument("--run-root", required=True)
        sub.add_argument("--history-repo", required=True)
        sub.add_argument(
            "--capture-id",
            default=dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8],
        )
        sub.add_argument("--max-source-bytes", type=int, default=DEFAULT_SOURCE_LIMIT)
        sub.add_argument("--max-total-bytes", type=int, default=DEFAULT_TOTAL_LIMIT)
        if command != "capture":
            sub.add_argument("--python", default=sys.executable)
            sub.add_argument("--timeout", type=float, default=600 if command == "build" else None)
            sub.add_argument(
                "--pass-env",
                action="append",
                default=[],
                metavar="NAME",
                help="Pass one explicitly selected parent environment variable; repeat for more.",
            )
        if command == "build":
            sub.add_argument("--check-timeout", type=int, default=180)
    return cli


def main() -> int:
    args = parser().parse_args()
    try:
        if args.max_source_bytes <= 0 or args.max_total_bytes <= 0:
            raise EvidenceError("positive_byte_limits_required")
        if getattr(args, "timeout", None) is not None and args.timeout <= 0:
            raise EvidenceError("positive_timeout_required")
        result, exit_code = (
            (capture(args), 0) if args.command == "capture" else execute_and_capture(args)
        )
    except Exception as error:
        code = str(error) if isinstance(error, EvidenceError) else type(error).__name__
        print(json.dumps({"status": "failed", "error": code}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
