"""Durable core assignments; slow terminal delivery never holds the MCP request."""

import asyncio
import re
import secrets
import shlex
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ...config import Config, public_task
from ...placement.facts import FactsError, FactsUnreadable, load_facts
from ...pressure import harness_of_peer, provider_of
from ...pressure.gate import notice
from ...process import BridgeError, module_launch_argv
from ...retention import matches_work
from ...tasks import TaskStore
from .core import (
    NOT_SENT_MACHINE_CHANGED,
    RESULT_DIRECTORY,
    RESULT_MAX_CHARS,
    CoreIdentityChanged,
    CoreNotReady,
    CoreNotSent,
    CoreTypedWhileWorking,
    address_machine,
    result_path,
    validate_message,
)

if TYPE_CHECKING:
    from ...pressure import PressureStore

# Shapes of provider credentials that must never be typed into a terminal: the
# variable names the harness launcher resolves (followed by `=` or `:`, optionally
# after a closing quote), a 1Password secret reference, a Bedrock API key and a
# 1Password service account token. A match refuses the brief before any receipt.
SECRET_SHAPES = (
    re.compile(
        r"(?:AWS_BEARER_TOKEN_BEDROCK|CODE_HARNESS_BEDROCK_TOKEN|OP_SERVICE_ACCOUNT_TOKEN)"
        r"[\"']?\s*[=:]"
    ),
    re.compile(r"op://"),
    re.compile(r"(?<![A-Za-z0-9])ABSK[A-Za-z0-9+/=_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_])ops_[A-Za-z0-9+/=_-]{20,}"),
)
SHAPE_REFUSAL = "Brief matches a provider secret shape; remove the credential and send again"
OUTSIDE_ROOTS = (
    "Remote terminal's current directory is outside its machine's placement project root"
)
MACHINE_CHANGED = (
    "Machine's core id is not the one pinned at its first send (machine_id_changed); "
    "clear the pin only after checking the machine"
)
POLICY_UNREADABLE = "Placement policy for remote sends is unreadable; fix hc_placement_policy first"
PIN_RACED = (
    "Machine's core id was pinned to another id while this delivery was sent "
    "(machine_id_changed); delivery is uncertain. Do not resubmit blindly."
)
POLICY_INVALID = (
    "Placement policy for remote sends is invalid (a project of / or with .. is refused); "
    "fix its placement block first"
)


def refuse_secret_shapes(message: str) -> None:
    """Raise ValueError when the brief holds a provider credential shape.

    The text of the match is never repeated: the refusal is one fixed sentence.
    """
    if any(shape.search(message) for shape in SECRET_SHAPES):
        raise ValueError(SHAPE_REFUSAL)


def inside_root(cwd: Any, root: str) -> bool:
    """Whether the absolute `cwd` is `root` or below it, compared by path parts.

    A `..` part in either path never matches: Core reports the remote directory
    as text, and this bridge cannot resolve it on the other machine.
    """
    if not isinstance(cwd, str):
        return False
    path, base = PurePosixPath(cwd), PurePosixPath(root)
    if not path.is_absolute() or not base.is_absolute() or ".." in path.parts + base.parts:
        return False
    return path == base or base in path.parents


class CoreTasks:
    def __init__(
        self,
        store: TaskStore,
        config_path: Path,
        config: Config | None = None,
        *,
        pressure: "PressureStore | None" = None,
    ) -> None:
        self.store = store
        self.config_path = config_path
        # Read at delivery time only, for the one-line notice; it never delays a delivery.
        self.pressure = pressure
        # The bridge's live Config (never copied), for `hc_placement_policy`.
        self.config = config
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.slots = asyncio.Semaphore(2)
        # One lock per machine name, held by a first delivery (no pin yet) across
        # its final pin check, send and pin, so a second first delivery to the
        # same machine waits and then meets the pin. In-process only: one bridge
        # runs per config, and a second process is out of scope (the id
        # `pin_machine` returns is still compared after the send).
        self.first_pin: dict[str, asyncio.Lock] = {}
        self.closing = False

    async def submit(
        self,
        core: Any,
        target: str,
        message: str,
        request_id: str,
        expected: dict[str, Any] | None = None,
        admission_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if self.closing:
            raise BridgeError("Bridge is stopping")
        refuse_secret_shapes(message)
        key = self.target_key(core, target)
        previous = self.previous(core, target, message, request_id)
        if previous is not None:
            return previous
        machine = self.remote_machine(target)
        # The task UUID and the token have fixed widths, so validate the complete
        # envelope before reserving.
        validate_message(
            self.envelope(
                "00000000-0000-0000-0000-000000000000",
                message,
                token=None if machine is None else "0" * 32,
            )
        )
        if len(self.jobs) >= 32:
            raise BridgeError("Core delivery queue is full; inspect existing tasks")
        peer = await core.peer(target)
        if expected is not None and peer.get("binding") != expected.get("binding"):
            raise ValueError("Selected core agent changed since discovery; search again")
        if (pressure_line := self.delivery_notice(peer)) is not None:
            # Under pressure now: the text that would be typed now must fit as well.
            validate_message(
                self.envelope(
                    "00000000-0000-0000-0000-000000000000",
                    message,
                    token=None if machine is None else "0" * 32,
                    notice=pressure_line,
                )
            )
        if not peer.get("send_ready"):
            raise ValueError(
                "Core agent is not ready: " + peer.get("send_readiness_reason", "unknown")
            )
        machine_id = None
        if machine is not None:
            # The id Core gives this name now must be the one pinned at its first
            # verified send; a machine not connected raises CoreNotSent here.
            machine_id = await core.machine_id(machine)
            pinned = self.store.machine_pin(machine)
            if pinned is not None and pinned != machine_id:
                raise ValueError(MACHINE_CHANGED)
        roots = None if machine is None else self.project_roots(machine)
        if roots is not None and not any(inside_root(peer.get("project"), r) for r in roots):
            raise ValueError(OUTSIDE_ROOTS)
        if self.closing:
            raise BridgeError("Bridge is stopping")
        if admission_guard is not None:
            admission_guard()
        metadata = {"kind": "hc", "target": target, "peer": peer, "completion": "worker_report"}
        if machine is not None:
            metadata["project_roots"] = "unconfigured" if roots is None else "enforced"
            metadata["machine_id"] = machine_id
            # A terminal on another machine reports through a result file whose name
            # is derived from the task ID and this token (`core.result_path`). Both
            # are written by the one INSERT that creates the receipt, so no receipt
            # exists without the token that names its file.
            metadata.update(
                completion="result_file",
                result_machine=machine,
                result_token=secrets.token_hex(16),
            )
        task, created = self.store.reserve(key, message, request_id, metadata)
        if created:
            token = task["metadata"].get("result_token")
            job = asyncio.create_task(
                self._deliver(
                    core, task["id"], target, message, peer, token=token, machine_id=machine_id
                )
            )
            self.jobs[task["id"]] = job
            job.add_done_callback(lambda _: self.jobs.pop(task["id"], None))
        return public_task(task)

    def project_roots(self, machine: str) -> tuple[str, ...] | None:
        """The project folders a remote terminal's directory must be in; None if unset.

        They come from the `placement` block of the file `hc_placement_policy`
        names: the machine's `project`. No file configured, no facts for the
        machine, or no folder in them gives None. A configured file that cannot
        be read (POLICY_UNREADABLE) or whose placement block is refused
        (POLICY_INVALID) refuses the send (ValueError) rather than allowing it.
        """
        policy = None if self.config is None else self.config.hc_placement_policy
        if policy is None:
            return None
        try:
            facts = load_facts(Path(policy))
        except FactsUnreadable:
            raise ValueError(POLICY_UNREADABLE) from None
        except FactsError:
            # Read, but its placement block is refused: not JSON, a bad key, or a
            # project folder of `/` or with a `..` part.
            raise ValueError(POLICY_INVALID) from None
        found = facts.machines.get(machine)
        if found is None or found.project is None:
            return None
        return (found.project,)

    @staticmethod
    def remote_machine(target: str) -> str | None:
        """The machine of a `machine/tNN` target; None for a local one.

        A malformed target is left to `core.peer`, which refuses it.
        """
        try:
            return address_machine(target)
        except ValueError:
            return None

    @staticmethod
    def target_key(core: Any, target: str) -> str:
        return f"hc:{getattr(core, 'session', None) or 'default'}:{target}"

    def previous(
        self, core: Any, target: str, message: str, request_id: str
    ) -> dict[str, Any] | None:
        task = self.store.by_request(request_id)
        if task is None:
            return None
        if not matches_work(task, self.target_key(core, target), message, request_id, "hc"):
            raise ValueError("request_id already describes different work")
        return public_task(task)

    def envelope(
        self,
        task_id: str,
        message: str,
        *,
        token: str | None = None,
        notice: str | None = None,
    ) -> str:
        """The text typed into the target terminal.

        `notice` (provider pressure, T-227) is the line after `Bridge task: <id>`; only
        the typed text carries it, never the stored receipt message. Without a notice
        the text is exactly what it was before.

        A terminal on another machine (one with a result `token`) cannot run this
        bridge's interpreter or reach its config, so its envelope names neither.
        It asks for a result file instead, which this bridge reads through Herdr
        core. `$HOME`, never a tilde: Herdr core does not expand one.

        The brief gives three steps. The first command makes the result folder
        and sets it to 0700 before anything is written. The draft is then
        written inside that folder, under the result's name with `.draft` in
        place of `.txt`, a name the reading side never builds. The last command
        sets the draft to 0600 and renames it to the name that is read; both
        names are in one folder, so the name that is read never holds a part of
        the text. Each command is one complete line and every path is quoted.

        Step 2 says what `parse_result` holds the file to: the draft's first and
        last line, and between them a result of 1 to RESULT_MAX_CHARS characters.
        It also says that the draft is written whole, not appended to, and how a
        tool that is not a shell takes the printed path.
        """
        head = f"Bridge task: {task_id}\n" + (notice or "")
        if notice and not notice.endswith("\n"):
            head += "\n"
        if token is not None:
            name = result_path(task_id, token)
            draft = name.removesuffix(".txt") + ".draft"
            return (
                f"{head}{message}\n\n"
                "This assignment came from another machine. "
                "Its result is collected from a file on this machine.\n"
                "Do not run any answerback command and do not call task_complete: "
                "neither can reach the sender.\n"
                "When the assignment is complete, do these three steps in order.\n"
                "Step 1. Run this command. It makes the result folder, "
                "for this account only (mode 0700):\n"
                f'mkdir -p "$HOME/{RESULT_DIRECTORY}" && chmod 700 "$HOME/{RESULT_DIRECTORY}"\n'
                "Step 2. Write the draft file as UTF-8 text. Write it whole, "
                "in place of any earlier draft: do not append to one.\n"
                "The draft's path is printed below as a shell reads it. "
                "A tool that writes files without a shell needs\n"
                "this account's home folder in place of $HOME, "
                "and no quotation marks around the path:\n"
                f'"$HOME/{draft}"\n'
                "The draft has three parts. The draft's first line is exactly:\n"
                f"answerback-result {task_id} {token}\n"
                f"Then the result itself, from 1 to {RESULT_MAX_CHARS} characters. "
                "An empty result or a longer one is refused.\n"
                "The draft's last line, with nothing else on it, is exactly:\n"
                f"answerback-end {token}\n"
                "Step 3. Run this command. It sets the draft to mode 0600 "
                "and renames it inside that folder:\n"
                f'chmod 600 "$HOME/{draft}" && mv "$HOME/{draft}" "$HOME/{name}"\n'
                "Do not change the renamed file afterwards. Keep normal tool approvals. "
                "If approval or work is outstanding,\n"
                "say so in this terminal and do not run step 3."
            )
        command = shlex.join(
            module_launch_argv(self.config_path, "complete", task_id, "--result-file")
        )
        return (
            f"{head}{message}\n\n"
            "When this assignment is complete, write the final result to a UTF-8 file and run:\n"
            f"{command} /absolute/path/to/result.txt\n"
            "Alternatively call task_complete through the MCP connection for exactly this "
            f"bridge config: {self.config_path}. Use the assigned task ID. "
            "Keep normal tool approvals; report a blocker instead of claiming completion "
            "while approval or work is outstanding."
        )

    async def _deliver(
        self,
        core: Any,
        task_id: str,
        target: str,
        message: str,
        peer: dict[str, Any],
        *,
        token: str | None = None,
        machine_id: str | None = None,
    ) -> None:
        machine = self.remote_machine(target)
        try:
            async with self.first_delivery(machine):
                try:
                    async with self.slots:
                        envelope = self.delivery_text(task_id, message, peer, token=token)
                        if machine is None:
                            receipt = await core.send(target, envelope, expected=peer)
                        else:
                            # A pin written since admission (a first send that held
                            # the machine's lock before this one) must match the
                            # admitted id. `core.send` then reads the id as its last
                            # step before `hc send` (CoreNotSent: nothing typed) and
                            # again after it (CoreIdentityChanged: uncertain).
                            pinned = self.store.machine_pin(machine)
                            if machine_id is None or (pinned is not None and pinned != machine_id):
                                raise CoreNotSent(NOT_SENT_MACHINE_CHANGED)
                            receipt = await core.send(
                                target, envelope, expected=peer, machine_id=machine_id
                            )
                    self.pin(machine, machine_id)
                    self.store.confirm_core_delivery(task_id, receipt)
                except CoreTypedWhileWorking as error:
                    # The text is in the selected terminal, in a turn the agent
                    # began on its own; the worker's task_complete settles it.
                    self.pin(machine, machine_id)
                    self.store.confirm_core_typed_while_working(
                        task_id,
                        error.receipt,
                        turn_before=error.turn_before,
                        turn_after=error.turn_after,
                    )
        except CoreIdentityChanged as error:
            self.store.invalidate_submission(task_id, str(error))
        except (CoreNotReady, CoreNotSent) as error:
            # Both are raised before `hc send` ran, so nothing reached a terminal;
            # every other failure below may come after the text was typed.
            self.store.update(task_id, "failed", detail=str(error))
        except asyncio.CancelledError:
            self.store.update(
                task_id, "uncertain", detail="Core delivery interrupted; inspect before retrying"
            )
            raise
        except Exception as error:
            self.store.update(
                task_id,
                "uncertain",
                detail=(
                    f"Core delivery unverified ({type(error).__name__}); inspect before retrying"
                ),
            )

    def delivery_notice(self, peer: dict[str, Any]) -> str | None:
        """The pressure notice for this peer's provider account while it is held."""
        if self.pressure is None:
            return None
        return notice(self.pressure, provider_of(harness_of_peer(peer)))

    def delivery_text(
        self, task_id: str, message: str, peer: dict[str, Any], *, token: str | None
    ) -> str:
        """The typed text, with the notice decided now, at delivery time.

        Pressure that began after admission still adds the notice. The notice is
        advice: when it would push an admitted text past the size bound, the text
        goes without it. The receipt's stored message is never changed.
        """
        plain = self.envelope(task_id, message, token=token)
        pressure_line = self.delivery_notice(peer)
        if pressure_line is None:
            return plain
        noticed = self.envelope(task_id, message, token=token, notice=pressure_line)
        try:
            validate_message(noticed)
        except ValueError:
            return plain
        return noticed

    def first_delivery(self, machine: str | None) -> AbstractAsyncContextManager[Any]:
        """The machine's first-pin lock while it has no pin; otherwise nothing to hold.

        A pin is never replaced by the bridge, so once one exists a delivery's own
        check against it decides, and deliveries to the machine run two at once.
        """
        if machine is None or self.store.machine_pin(machine) is not None:
            return nullcontext()
        return self.first_pin.setdefault(machine, asyncio.Lock())

    def pin(self, machine: str | None, machine_id: str | None) -> None:
        """Pin a remote delivery's admitted id; raise if another id holds the pin.

        `pin_machine` keeps an existing pin, so a different returned id means the
        machine was pinned to another core while this text was sent (for example
        by a second bridge process): the delivery is never confirmed.
        """
        if machine is None or machine_id is None:
            return
        if self.store.pin_machine(machine, machine_id) != machine_id:
            raise CoreIdentityChanged(PIN_RACED)

    async def close(self) -> None:
        self.closing = True
        jobs = list(self.jobs.items())
        for _, job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        for task_id, _ in jobs:
            if self.store.get(task_id)["status"] == "pending":
                self.store.update(
                    task_id, "uncertain", detail="Bridge stopped with core delivery pending"
                )
