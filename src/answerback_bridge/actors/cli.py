"""Local administration; actor capabilities stay private, owner tokens return once."""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..bus.config import BusConfig, private_read
from .api import ActorAPI, load_binding
from .model import ActorError, unique_object
from .store import ActorStore


def add_parser(commands: Any) -> argparse.ArgumentParser:
    group = commands.add_parser("actors", help="Administer explicitly enrolled project actors")
    actions = group.add_subparsers(dest="actor_operation", required=True)
    create = actions.add_parser("create", help="Create a bounded project run")
    create.add_argument("--run-id")
    create.add_argument("--project", type=Path, required=True)
    create.add_argument("--role", action="append", required=True)
    create.add_argument("--ttl", type=int, default=3600)
    _proof_arguments(create, required=False)
    stage = actions.add_parser("stage", help="Stage one actor and save its private capability")
    stage.add_argument("run_id")
    stage.add_argument("--actor-id")
    stage.add_argument("--role", required=True)
    stage.add_argument("--harness", required=True)
    stage.add_argument("--epoch", type=int, default=1)
    stage.add_argument("--binding-file", type=Path, required=True)
    activate = actions.add_parser("activate", help="Bind an actor to observed native identity")
    activate.add_argument("run_id")
    activate.add_argument("actor_id")
    activate.add_argument("--epoch", type=int, default=1)
    activate.add_argument("--native-binding-file", type=Path, required=True)
    for name in ("seal", "status", "roster", "export", "finish"):
        action = actions.add_parser(name)
        action.add_argument("run_id")
        _proof_arguments(action, required=True)
        if name == "finish":
            action.add_argument("--reason", required=True)
    revoke = actions.add_parser("revoke", help="Revoke one actor epoch without deleting evidence")
    revoke.add_argument("run_id")
    revoke.add_argument("actor_id")
    revoke.add_argument("--epoch", type=int, default=1)
    revoke.add_argument("--reason", required=True)
    return group


OWNER_TOKEN_ARGV_WARNING = (
    "warning: --owner-token is deprecated and will be removed in the next release: "
    "a command-line argument is visible to other local accounts in process listings. "
    "Save the token in a private file (mode 0600) and pass --owner-token-file PATH."
)


def _proof_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    proof = parser.add_mutually_exclusive_group(required=required)
    proof.add_argument(
        "--owner-token",
        help="Deprecated (visible in process listings): use --owner-token-file",
    )
    proof.add_argument(
        "--owner-token-file",
        type=Path,
        help="Private file (owned, mode 0600, not a symlink) holding the run owner token",
    )
    proof.add_argument("--binding-file", type=Path, help="Private capability of a run participant")


def _owner_token(args: argparse.Namespace) -> str | None:
    """The owner token from its private file, or from argv with a deprecation warning."""
    token_file = getattr(args, "owner_token_file", None)
    if token_file is not None:
        try:
            return private_read(token_file, 1024).strip()
        except (OSError, ValueError):
            raise ActorError("auth_invalid") from None
    token = getattr(args, "owner_token", None)
    if token is not None:
        print(OWNER_TOKEN_ARGV_WARNING, file=sys.stderr)
    return token


def _proof(args: argparse.Namespace) -> dict[str, Any]:
    binding_file = getattr(args, "binding_file", None)
    return {
        "owner_token": _owner_token(args),
        "auth": load_binding(binding_file) if binding_file is not None else None,
    }


def _stage(store: ActorStore, args: argparse.Namespace) -> dict[str, Any]:
    credential = secrets.token_hex(32)
    actor_id = args.actor_id or str(uuid4())
    binding = {
        "schema_version": 1,
        "run_id": args.run_id,
        "actor_id": actor_id,
        "epoch": args.epoch,
        "credential": credential,
    }
    # Exclusive creation preserves any earlier enrollment, including its secret.
    descriptor = os.open(
        args.binding_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    created = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(binding) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        actor = store.stage_actor(
            args.run_id, actor_id, args.role, args.harness, credential, epoch=args.epoch
        )
    except Exception:
        try:
            current = args.binding_file.lstat()
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                args.binding_file.unlink()
        raise
    return {**actor, "binding_file": str(args.binding_file.absolute())}


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    config = BusConfig.load(args.config)
    store = (
        ActorStore(config.root / "actors.sqlite")
        if args.actor_operation == "create"
        else ActorAPI(args.config).store()
    )
    try:
        if args.actor_operation == "create":
            return store.create_run(
                args.run_id or str(uuid4()),
                str(args.project.expanduser().resolve(strict=True)),
                args.role,
                ttl_seconds=args.ttl,
                **_proof(args),
            )
        if args.actor_operation == "stage":
            return _stage(store, args)
        if args.actor_operation == "activate":
            binding = json.loads(
                private_read(args.native_binding_file, 16384), object_pairs_hook=unique_object
            )
            return store.activate_actor(args.run_id, args.actor_id, args.epoch, binding)
        if args.actor_operation == "seal":
            return store.seal_run(args.run_id, **_proof(args))
        if args.actor_operation == "finish":
            return store.finish_run(args.run_id, args.reason, **_proof(args))
        if args.actor_operation == "revoke":
            return store.revoke_actor(args.run_id, args.actor_id, args.epoch, args.reason)
        operations = {"status": store.status, "roster": store.roster, "export": store.export}
        return operations[args.actor_operation](args.run_id, **_proof(args))
    except ActorError:
        raise
    except (ValueError, TypeError, KeyError):
        raise ActorError("invalid_request") from None
