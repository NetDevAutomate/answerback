"""Local broker configuration and routing identity; no secrets in public output."""

import errno
import fcntl
import json
import os
import secrets
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Re-export the kernel helpers so existing bus imports remain compatible.
from ..config import atomic_write
from ..config import routing_digest as routing_digest
from ..digests import namespace_for_config as namespace_for_config
from ..transport import private_directory

LAUNCHER_STATUS_ERROR = "configured executable is a launcher shim; run `agent-bus configure` again"
_MAX_LAUNCHER_BYTES = 64 * 1024


def _absolute(path: str) -> Path:
    return Path(path).expanduser().absolute()


def _mise_shim_directories() -> set[Path]:
    directories = {_absolute("~/.local/share/mise/shims")}
    if value := os.environ.get("MISE_SHIMS_DIR"):
        directories.add(_absolute(value))
    if value := os.environ.get("MISE_DATA_DIR"):
        directories.add(_absolute(value) / "shims")
    return directories


def _known_mise_shim(path: Path) -> bool:
    return path.name == "nats-server" and path.parent in _mise_shim_directories()


def _small_script_body(path: Path) -> bytes | None:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LAUNCHER_BYTES:
            return None
        with path.open("rb") as source:
            body = source.read(_MAX_LAUNCHER_BYTES + 1)
        return body if body.startswith(b"#!") else None
    except OSError:
        return None


def _small_script(path: Path) -> bool:
    return _small_script_body(path) is not None


def _script_references_mise(path: Path) -> bool:
    body = _small_script_body(path)
    return body is not None and b"mise" in body


def _mise_executable(supplied: Path, resolved: Path) -> bool:
    if resolved.name == "mise":
        return True
    if supplied.name != "nats-server" or supplied == resolved:
        return False
    mise = shutil.which("mise")
    if mise is None:
        return False
    try:
        return resolved.samefile(mise)
    except OSError:
        return False


def launcher_shim(server_binary: str) -> bool:
    """Recognize launchers whose process executable will differ from this path."""
    path = _absolute(server_binary)
    resolved = path.resolve()
    return _known_mise_shim(path) or _small_script(resolved) or _mise_executable(path, resolved)


def _launcher_error(path: Path) -> ValueError:
    return ValueError(
        f"{path}: configure the real nats-server binary; a launcher shim cannot be owned"
    )


def resolve_server_binary(server_binary: str, *, cwd: Path | None = None) -> str:
    """Return the executable file the configured launcher will actually run."""
    supplied = _absolute(server_binary)
    resolved = supplied.resolve()
    known_mise_shim = _known_mise_shim(supplied)
    script = _small_script(resolved)
    if script and not (known_mise_shim or _script_references_mise(resolved)):
        raise _launcher_error(supplied)
    mise_executable = False if script else _mise_executable(supplied, resolved)
    if not (known_mise_shim or script or mise_executable):
        return str(resolved)
    mise = shutil.which("mise")
    if mise is not None:
        try:
            completed = subprocess.run(
                [mise, "which", "nats-server"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
                timeout=5,
                cwd=str(cwd or Path.home()),
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key in {"HOME", "PATH"} or key.startswith("MISE_")
                },
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            output = completed.stdout.strip()
            candidate = Path(output).expanduser() if "\n" not in output else Path()
            if candidate.is_absolute():
                candidate = candidate.resolve()
                if (
                    candidate != resolved
                    and candidate.is_file()
                    and os.access(candidate, os.X_OK)
                    and not launcher_shim(str(candidate))
                ):
                    return str(candidate)
    raise _launcher_error(supplied)


def private_read(path: Path, limit: int) -> str:
    """Read a private, owned regular file once, through the descriptor whose metadata passed.

    The open refuses a symlink and never blocks (a FIFO opens and is then refused as not a
    regular file); the checks and the bounded read all use that one descriptor, so a
    directory-entry swap after the check cannot change which bytes are returned.
    """
    message = "Bus credentials/configuration must be private, owned regular files"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK, errno.ENXIO):
            raise ValueError(message) from None
        raise
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > limit
        ):
            raise ValueError(message)
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ValueError(message)
    return raw.decode()


@contextmanager
def setup_lock(root: Path) -> Iterator[None]:
    """Serialise token/config publication so concurrent setup cannot split credentials."""
    private_directory(root)
    fd = os.open(root / "setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Bus setup lock must be an owned regular file")
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Another bus setup is still running") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class BusConfig:
    bridge_path: Path
    port: int
    server_binary: str

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Invalid broker port")
        if not isinstance(self.server_binary, str) or not Path(self.server_binary).is_absolute():
            raise ValueError("An absolute nats-server binary is required")

    def require_binary(self) -> None:
        if not os.access(self.server_binary, os.X_OK) or not Path(self.server_binary).is_file():
            raise ValueError("Configured nats-server binary is unavailable")

    @property
    def root(self) -> Path:
        return self.bridge_path.expanduser().resolve().parent / "bus"

    @property
    def path(self) -> Path:
        return self.root / "config.json"

    @property
    def namespace(self) -> str:
        return namespace_for_config(self.bridge_path)

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    @property
    def token_path(self) -> Path:
        return self.root / "token"

    @property
    def broker_config(self) -> Path:
        return self.root / "nats.conf"

    @property
    def commands_stream(self) -> str:
        return f"AGENT_COMMANDS_{self.namespace.upper()}"

    @property
    def events_stream(self) -> str:
        return f"AGENT_EVENTS_{self.namespace.upper()}"

    @property
    def commands_subject(self) -> str:
        return f"agents.v1.{self.namespace}.commands"

    @property
    def events_subject(self) -> str:
        return f"agents.v1.{self.namespace}.events"

    def token(self) -> str:
        value = private_read(self.token_path, 1024).strip()
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("Invalid private bus token")
        return value

    def public(self) -> dict[str, str | int]:
        return {
            "url": self.url,
            "namespace": self.namespace,
            "server_binary": self.server_binary,
            "config_path": str(self.path),
            "port": self.port,
        }

    @classmethod
    def load(cls, bridge_path: Path) -> "BusConfig":
        root = bridge_path.expanduser().resolve().parent / "bus"
        value = json.loads(private_read(root / "config.json", 8192))
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "port", "server_binary", "namespace"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError("Invalid bus configuration")
        config = cls(bridge_path.resolve(), value["port"], value["server_binary"])
        if value["namespace"] != config.namespace:
            raise ValueError("Bus configuration belongs to a different coordinator")
        config.token()
        return config

    @classmethod
    def create(cls, bridge_path: Path, *, server_binary: str, port: int = 4222) -> "BusConfig":
        bridge_path = bridge_path.resolve()
        root = bridge_path.parent / "bus"
        private_directory(root)
        config = cls(
            bridge_path,
            port,
            resolve_server_binary(server_binary, cwd=root),
        )
        config.require_binary()
        with setup_lock(config.root):
            return config._create_locked()

    def _write_config(self) -> None:
        atomic_write(
            self.path,
            json.dumps(
                {
                    "schema_version": 1,
                    "port": self.port,
                    "server_binary": self.server_binary,
                    "namespace": self.namespace,
                },
                indent=2,
            )
            + "\n",
        )

    def _create_locked(self) -> "BusConfig":
        config = self
        if config.path.exists():
            existing = self.load(config.bridge_path)
            if existing != config:
                if launcher_shim(existing.server_binary):
                    if config.port != existing.port:
                        raise ValueError(
                            "A different bus is already configured; stop and inspect it first"
                        )
                    migrated = type(self)(config.bridge_path, existing.port, config.server_binary)
                    migrated.require_binary()
                    migrated._write_config()
                    return migrated
                raise ValueError("A different bus is already configured; stop and inspect it first")
            return existing
        private_directory(config.root)
        token = secrets.token_hex(32)
        atomic_write(config.token_path, token + "\n")
        private_directory(config.root / "jetstream")
        settings = (
            'host: "127.0.0.1"\n'
            f"port: {config.port}\n"
            f"server_name: {json.dumps(config.namespace)}\n"
            f"authorization {{ token: {json.dumps(token)} }}\n"
            "max_payload: 131072\n"
            "jetstream {\n"
            f"  store_dir: {json.dumps(str(config.root / 'jetstream'))}\n"
            "  max_memory_store: 1048576\n"
            "  max_file_store: 268435456\n"
            "}\n"
        )
        atomic_write(config.broker_config, settings)
        config._write_config()
        return config
