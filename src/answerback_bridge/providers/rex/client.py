"""Bounded, read-only public CLI access to a pinned local Rex server.

Captures are prefixes of individual snapshots, never transcript cursors or tails.
"""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ...process import BridgeError, CommandCleanupError, run

TERMINAL = "com.superlogical.terminal"
MAX_METADATA = 256 * 1024
MAX_SESSIONS = 32
MAX_BLOCKS = 128
MAX_CAPTURE = 64 * 1024
OPERATION_SECONDS = 20
# Spend 18 seconds on work, at most 1 on owned-child cleanup, and retain
# 1 second for response handling. Fractions keep test budgets proportional.
WORK_BUDGET_FRACTION = 0.9
CLEANUP_BUDGET_FRACTION = 0.05


def resolve_binary(binary: str) -> str:
    """Pin a configuration-time executable choice, including relative PATH entries."""
    resolved = shutil.which(binary)
    if resolved is None:
        raise ValueError("Rex executable is unavailable")
    return str(Path(resolved).absolute())


def _owned_foreground(foreground: object) -> bool:
    return (
        isinstance(foreground, dict)
        and type(foreground.get("user_id")) is int
        and foreground["user_id"] == os.getuid()
    )


def _owned_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    """Withhold native rows without ownership evidence at a public listing boundary."""
    peers = [peer for peer in inventory["peers"] if _owned_foreground(peer.get("foreground"))]
    if len(peers) == len(inventory["peers"]):
        return inventory
    return {
        **inventory,
        "peers": peers,
        "complete": False,
        "errors": [
            *inventory.get("errors", []),
            {"error": "Rex terminals withheld: foreground ownership by this OS user is unverified"},
        ],
    }


def _local_endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise BridgeError("Rex endpoint is missing")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 for char in value)
    ):
        raise BridgeError("Rex requires an explicit local Unix endpoint")
    return value


def _full_id(value: object, kind: str) -> str:
    if not isinstance(value, str) or not value.startswith(kind + ":"):
        raise BridgeError(f"Rex requires a full {kind} ID")
    try:
        uuid = UUID(value[len(kind) + 1 :])
    except ValueError as error:
        raise BridgeError(f"Rex requires a full {kind} UUID") from error
    if str(uuid) != value[len(kind) + 1 :].lower():
        raise BridgeError(f"Rex requires a full {kind} UUID")
    return value


class RexIdentityChanged(BridgeError):
    """A positively observed identity differs or bounded enumeration proves absence."""


@dataclass(frozen=True)
class _ServerSnapshot:
    """One operation's server authority; concurrent probes cannot relabel it."""

    endpoint: str
    host_id: str
    instance_id: str
    api_revision: Any
    capabilities: frozenset[str]

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.endpoint, self.host_id, self.instance_id

    def public(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "server_host_id": self.host_id,
            "server_instance_id": self.instance_id,
            "api_revision": self.api_revision,
            "capabilities": {
                key: key in self.capabilities
                for key in ("discovery", "process", "program_status", "read", "enroll")
            },
        }


class Rex:
    def __init__(
        self,
        binary: str = "rex",
        server: str | None = None,
        expected_host_id: str | None = None,
    ) -> None:
        self.binary = binary
        self.endpoint = _local_endpoint(server) if server is not None else None
        self.expected_host_id = expected_host_id
        self._identity: dict[str, Any] | None = None
        self._capabilities: dict[str, bool] = {}

    async def _cli(self, *args: str, limit: int = MAX_METADATA) -> dict[str, Any]:
        if not os.path.isabs(self.binary):
            raise BridgeError(
                "Rex executable must be an absolute path; run answerback rex-configure"
            )
        env = {k: v for k, v in os.environ.items() if not k.startswith("REX_")}
        argv = [self.binary, "--autostart=false", "--timeout=2s"]
        if self.endpoint is not None:
            argv += ["-S", self.endpoint]
        output = await run(
            argv + list(args),
            env=env,
            max_seconds=3,
            cleanup_seconds=OPERATION_SECONDS * CLEANUP_BUDGET_FRACTION,
        )
        if len(output.stdout.encode()) > limit:
            raise BridgeError("Rex response exceeded collection bound")
        try:
            result = json.loads(output.stdout)
        except json.JSONDecodeError as error:
            raise BridgeError("Rex returned malformed JSON") from error
        if not isinstance(result, dict) or "error" in result:
            raise BridgeError("Rex returned an invalid or rejected result")
        return result

    async def _call(self, method: str, **args: Any) -> dict[str, Any]:
        return await self._cli("api", "call", method, json.dumps(args))

    @staticmethod
    def _schema_fields(
        schema: object, expected: dict[str, str], *, input_arguments: bool = False
    ) -> bool:
        if not isinstance(schema, dict) or schema.get("type", "object") != "object":
            return False
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return False
        if not all(isinstance(key, str) for key in required):
            return False
        # Only the six pinned signatures are callable: additional mandatory
        # arguments would require support in the corresponding call site.
        if input_arguments and any(key not in expected for key in required):
            return False
        for key, kind in expected.items():
            field = properties.get(key)
            if not isinstance(field, dict):
                return False
            declared = field.get("type")
            if declared != kind and not (
                isinstance(declared, list)
                and kind in declared
                and all(t in (kind, "null") for t in declared)
            ):
                return False
        return True

    async def probe(self) -> dict[str, Any]:
        async with asyncio.timeout(OPERATION_SECONDS * WORK_BUDGET_FRACTION):
            return (await self._probe()).public()

    async def _probe(self) -> _ServerSnapshot:
        identity = await self._cli("whoami", "--json")
        endpoint = _local_endpoint(identity.get("endpoint"))
        if self.endpoint is not None and endpoint != self.endpoint:
            raise RexIdentityChanged("Rex endpoint mismatch")
        self.endpoint = endpoint
        host = identity.get("host_id")
        instance = identity.get("instance_id")
        if not isinstance(host, str) or not host or not isinstance(instance, str) or not instance:
            raise BridgeError("Rex server host or incarnation is missing")
        principal = identity.get("principal")
        if not isinstance(principal, dict) or principal.get("transport") != "unix":
            raise BridgeError("Rex identity is not a local Unix connection")
        if self.expected_host_id is not None and host != self.expected_host_id:
            raise RexIdentityChanged("Rex expected host mismatch")
        if self._identity and host != self._identity["host_id"]:
            raise RexIdentityChanged("Rex server host changed")
        capabilities = dict(self._capabilities)
        if self._identity is None or instance != self._identity["instance_id"]:
            specs = {
                "session.list": ([], ["sessions"]),
                "session.list_blocks": (["session_id"], ["blocks"]),
                "session.describe_block": (["session_id", "block_id"], ["block"]),
                TERMINAL + ".process": ([], ["foreground"]),
                TERMINAL + ".program_status": ([], ["records"]),
                TERMINAL + ".format": (["format", "max_bytes"], ["content"]),
            }
            available = {}
            for method, (inputs, outputs) in specs.items():
                try:
                    schema = await self._cli("api", "describe", method, "--json")
                    input_schema = schema.get("args_schema", schema.get("input_schema"))
                    available[method] = (
                        isinstance(schema.get("invoke"), dict)
                        and schema["invoke"].get("method") == method
                        and self._schema_fields(
                            input_schema,
                            {k: "integer" if k == "max_bytes" else "string" for k in inputs},
                            input_arguments=True,
                        )
                        and self._schema_fields(
                            schema.get("output_schema"),
                            {
                                k: {
                                    "sessions": "array",
                                    "blocks": "array",
                                    "records": "array",
                                    "block": "object",
                                    "foreground": "object",
                                    "content": "string",
                                }[k]
                                for k in outputs
                            },
                        )
                    )
                except CommandCleanupError:
                    raise
                except BridgeError:
                    available[method] = False
            capabilities = {
                "discovery": all(available[k] for k in specs if k.startswith("session.")),
                "process": available[TERMINAL + ".process"],
                "program_status": available[TERMINAL + ".program_status"],
                "read": available[TERMINAL + ".format"],
                "enroll": available[TERMINAL + ".process"],
            }
        snapshot = _ServerSnapshot(
            endpoint,
            host,
            instance,
            identity.get("api_revision", 0),
            frozenset(key for key, available in capabilities.items() if available),
        )
        # Capability descriptions must belong to the same incarnation as whoami.
        await self._check_collection_identity(snapshot.scope)
        self._identity = identity
        self._capabilities = capabilities
        return snapshot

    @staticmethod
    def _listing(result: dict[str, Any], key: str, bound: int) -> list[dict[str, Any]]:
        # These pinned public responses have exactly one collection field.
        # Unknown pagination/coverage metadata cannot authorize negative evidence.
        if set(result) != {key}:
            raise BridgeError(f"Rex {key} enumeration coverage unknown")
        rows = result[key]
        if rows is None:
            rows = []
        if not isinstance(rows, list) or len(rows) > bound:
            raise BridgeError(f"Rex {key} enumeration malformed or truncated")
        if any(not isinstance(row, dict) for row in rows):
            raise BridgeError(f"Rex {key} enumeration malformed")
        kind = "session" if key == "sessions" else "block"
        for row in rows:
            _full_id(row.get(kind + "_id"), kind)
        return rows

    async def _confirm_absence(
        self, session_id: str, block_id: str, original_scope: tuple[str, str, str]
    ) -> None:
        sessions = self._listing(await self._call("session.list"), "sessions", MAX_SESSIONS)
        if session_id not in {row["session_id"] for row in sessions}:
            await self._check_collection_identity(original_scope)
            raise RexIdentityChanged("Rex session confirmed absent")
        blocks = self._listing(
            await self._call("session.list_blocks", session_id=session_id), "blocks", MAX_BLOCKS
        )
        if block_id not in {row["block_id"] for row in blocks}:
            await self._check_collection_identity(original_scope)
            raise RexIdentityChanged("Rex terminal confirmed absent from its session")

    async def _check_collection_identity(self, original_scope: tuple[str, str, str]) -> None:
        identity = await self._cli("whoami", "--json")
        if (
            tuple(identity.get(key) for key in ("endpoint", "host_id", "instance_id"))
            != original_scope
            or not isinstance(identity.get("principal"), dict)
            or identity["principal"].get("transport") != "unix"
        ):
            raise BridgeError("Rex collection server identity changed or is unknown")

    async def _terminal(
        self, session_id: str, block_id: str, snapshot: _ServerSnapshot
    ) -> dict[str, Any]:
        _full_id(session_id, "session")
        _full_id(block_id, "block")
        if "discovery" not in snapshot.capabilities:
            raise BridgeError("Rex discovery capability is unavailable")
        original_scope = snapshot.scope
        try:
            details = await self._call(
                "session.describe_block", session_id=session_id, block_id=block_id
            )
        except CommandCleanupError:
            raise
        except BridgeError:
            await self._confirm_absence(session_id, block_id, original_scope)
            raise
        block = details.get("block")
        if not isinstance(block, dict) or not isinstance(block.get("block_id"), str):
            raise BridgeError("Rex block identity missing or malformed")
        _full_id(block["block_id"], "block")
        if block.get("block_id") != block_id:
            raise RexIdentityChanged("Rex block identity mismatch")
        if not isinstance(block.get("creator_name"), str):
            raise BridgeError("Rex block creator missing or malformed")
        if block.get("creator_name") != TERMINAL:
            raise RexIdentityChanged("Rex block is not a terminal")
        local = bool(block.get("host_id") and block["host_id"] == snapshot.host_id)
        foreground = None
        records = []
        if local and "process" in snapshot.capabilities:
            process = await self._call(
                TERMINAL + ".process", session_id=session_id, block_id=block_id, args={}
            )
            foreground = process.get("foreground")
            if foreground is not None and (
                not isinstance(foreground, dict)
                or type(foreground.get("pid")) is not int
                or foreground["pid"] <= 0
                or not isinstance(foreground.get("name"), str)
                or not foreground["name"].strip()
                or any(
                    foreground.get(field) is not None and not isinstance(foreground[field], str)
                    for field in ("cwd", "path", "script")
                )
            ):
                raise BridgeError("Rex returned malformed foreground process")
        if local and "program_status" in snapshot.capabilities:
            status = await self._call(
                TERMINAL + ".program_status", session_id=session_id, block_id=block_id, args={}
            )
            raw = status.get("records")
            if (
                not isinstance(raw, list)
                or len(raw) > 128
                or any(not isinstance(r, dict) for r in raw)
            ):
                raise BridgeError("Rex returned malformed status records")
            for record in raw:
                if not isinstance(record.get("state"), str):
                    raise BridgeError("Rex returned malformed status state")
                owner = record.get("owner")
                records.append(
                    {
                        **record,
                        "owner_matches": bool(
                            foreground
                            and isinstance(owner, dict)
                            and owner.get("pid") == foreground["pid"]
                            and owner.get("name") == foreground["name"]
                        ),
                        "known_state": record.get("state")
                        in {"idle", "working", "blocked", "done", "error"},
                    }
                )
        await self._check_collection_identity(original_scope)
        binding = {
            "endpoint": snapshot.endpoint,
            "server_instance_id": snapshot.instance_id,
            "server_host_id": snapshot.host_id,
            "session_id": session_id,
            "block_id": block_id,
            "block_host_id": block.get("host_id"),
        }
        for field in ("pid", "name", "path", "script", "cwd"):
            binding["foreground_" + field] = foreground.get(field) if foreground else None
        return {
            "session_id": session_id,
            "block_id": block_id,
            "label": block.get("label"),
            "placement": block.get("placement"),
            "window_id": block.get("window_id"),
            "owner": block.get("owner"),
            "block_host_id": block.get("host_id"),
            "local": local,
            "project": foreground.get("cwd") if foreground else None,
            "foreground": foreground,
            "reported_status": records,
            "binding": binding,
            "capabilities": {
                "read": local and "read" in snapshot.capabilities and _owned_foreground(foreground),
                "enroll": local
                and bool(
                    foreground
                    and _owned_foreground(foreground)
                    and foreground.get("cwd")
                    and os.path.isabs(foreground["cwd"])
                ),
            },
        }

    async def terminal(self, session_id: str, block_id: str) -> dict[str, Any]:
        async with asyncio.timeout(OPERATION_SECONDS * WORK_BUDGET_FRACTION):
            snapshot = await self._probe()
            return await self._terminal(session_id, block_id, snapshot)

    async def inventory(self) -> dict[str, Any]:
        peers: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        scope = None
        try:
            async with asyncio.timeout(OPERATION_SECONDS * WORK_BUDGET_FRACTION):
                snapshot = await self._probe()
                original_scope = snapshot.scope
                scope = {
                    "endpoint": original_scope[0],
                    "server_host_id": original_scope[1],
                    "server_instance_id": original_scope[2],
                }
                if "discovery" not in snapshot.capabilities:
                    raise BridgeError("Rex discovery unavailable")
                result = await self._call("session.list")
                if (
                    isinstance(result.get("sessions"), list)
                    and len(result["sessions"]) > MAX_SESSIONS
                ):
                    errors.append({"error": "Session inventory truncated"})
                elif set(result) != {"sessions"}:
                    errors.append({"error": "Rex session enumeration coverage unknown"})
                if "sessions" not in result:
                    raise BridgeError("Rex session list missing")
                sessions = result.get("sessions")
                if sessions is None:
                    sessions = []
                if not isinstance(sessions, list):
                    raise BridgeError("Rex session list malformed")
                if len(sessions) > MAX_SESSIONS:
                    errors.append({"error": "Session inventory truncated"})
                examined = 0
                for session in sessions[:MAX_SESSIONS]:
                    session_id = session.get("session_id") if isinstance(session, dict) else None
                    try:
                        session_id = _full_id(session_id, "session")
                        result = await self._call("session.list_blocks", session_id=session_id)
                        if set(result) != {"blocks"}:
                            raise BridgeError("Rex block enumeration coverage unknown")
                        if "blocks" not in result:
                            raise BridgeError("Rex block list missing")
                        blocks = result.get("blocks")
                        if blocks is None:
                            blocks = []
                        if not isinstance(blocks, list):
                            raise BridgeError("Rex block list malformed")
                        for block in blocks:
                            if examined >= MAX_BLOCKS:
                                raise BridgeError("Block inventory truncated")
                            examined += 1
                            if not isinstance(block, dict):
                                raise BridgeError("Rex block metadata malformed")
                            _full_id(block.get("block_id"), "block")
                            if not isinstance(block.get("creator_name"), str):
                                raise BridgeError("Rex block creator missing or malformed")
                            if block.get("creator_name") != TERMINAL:
                                continue
                            try:
                                block_id = _full_id(block.get("block_id"), "block")
                                peer = await self._terminal(session_id, block_id, snapshot)
                                peers.append(
                                    {
                                        **peer,
                                        "provider": "rex",
                                        "peer_id": f"rex:terminal:{session_id}/{block_id}",
                                        "display_name": peer["label"],
                                    }
                                )
                            except CommandCleanupError:
                                raise
                            except BridgeError as error:
                                errors.append({"session_id": session_id, "error": str(error)})
                    except CommandCleanupError:
                        raise
                    except BridgeError as error:
                        errors.append({"session_id": str(session_id), "error": str(error)})
                try:
                    await self._check_collection_identity(original_scope)
                except BridgeError:
                    peers.clear()
                    raise
        except (BridgeError, TimeoutError, OSError) as error:
            errors.append({"error": str(error) or "Rex inventory deadline exceeded"})
        return {"peers": peers, "complete": not errors, "errors": errors, "absence_scope": scope}

    async def read(self, session_id: str, block_id: str, max_bytes: int = 16384) -> dict[str, Any]:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CAPTURE:
            raise ValueError(f"max_bytes must be between 1 and {MAX_CAPTURE}")
        async with asyncio.timeout(OPERATION_SECONDS * WORK_BUDGET_FRACTION):
            snapshot = await self._probe()
            target = await self._terminal(session_id, block_id, snapshot)
            if not target["capabilities"]["read"]:
                raise BridgeError("Rex terminal lacks local bounded-read capability")
            result = await self._call(
                TERMINAL + ".format",
                session_id=session_id,
                block_id=block_id,
                args={"format": "text", "max_bytes": max_bytes, "trim": True},
            )
            text = result.get("content")
            if not isinstance(text, str):
                raise BridgeError("Rex capture content malformed")
            size = len(text.encode())
            blob = result.get("id")
            total = result.get("total")
            eof = result.get("eof")
            if (
                size > max_bytes
                or (blob is not None and (type(blob) is not int or blob <= 0))
                or (total is not None and (type(total) is not int or total < size))
                or (eof is not None and type(eof) is not bool)
                or (blob is not None and eof is True)
                or (eof is True and total is not None and total != size)
                or (blob is None and total is not None and total != size)
                or (blob is None and eof is False)
            ):
                raise BridgeError("Rex capture bounds or continuity metadata inconsistent")
            await self._check_collection_identity(snapshot.scope)
            return {
                "text": text,
                "capture_kind": "bounded_prefix",
                "truncated": blob is not None,
                "bytes_returned": size,
                "max_bytes": max_bytes,
                "blob_id": blob,
                "total_bytes": total,
                "eof": eof,
                "offset": 0,
                "continuity": "independent_snapshot",
                "binding": target["binding"],
            }
