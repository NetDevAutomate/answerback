"""Domain failures whose execution boundary is known."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from .process import BridgeError

# Public messages are reviewed literals, never provider output or str(unexpected_error).
# Codes let a new client render its own diagnostic rather than trust a peer's raw text.
_PUBLIC_VALIDATIONS = {
    "reconciliation_receipt_changed": "Receipt changed during reconciliation; inspect it again",
    "rex_disabled": "Rex is disabled; run rex-configure first",
    "rex_binding_required": "Rex requires a probed binding; run rex-configure first",
    "rex_binding_changed": ("Rex native binding differs from its configuration; run rex-configure"),
    "rex_routing_changed": "Rex routing configuration changed during native observation",
    "pressure_provider_unknown": "Unknown pressure provider; use kiro or bedrock",
    "pressure_account_invalid": "Pressure account hint must be a 1–64 character label",
    "brief_secret_shape": (
        "Brief matches a provider secret shape; remove the credential and send again"
    ),
    "remote_project_outside_roots": (
        "Remote terminal's current directory is outside its machine's placement project root"
    ),
    "machine_id_changed": (
        "Machine's core id is not the one pinned at its first send (machine_id_changed); "
        "clear the pin only after checking the machine"
    ),
    "placement_policy_unreadable": (
        "Placement policy for remote sends is unreadable; fix hc_placement_policy first"
    ),
    "placement_policy_invalid": (
        "Placement policy for remote sends is invalid (a project of / or with .. is refused); "
        "fix its placement block first"
    ),
}
_PUBLIC_MESSAGES = {
    **_PUBLIC_VALIDATIONS,
    "protocol_incompatible": "Incompatible bridge protocol",
    "configuration_mismatch": "Request belongs to another configuration directory",
    "operation_unknown": "Unknown bridge operation",
    "parameters_invalid": "Parameters must be an object",
    "parameters_unexpected": "Operation takes no parameters",
    "draining": "Bridge is draining; inspect outstanding task receipts",
    "stopping": "Bridge is stopping",
    "core_queue_full": "Core delivery queue is full; inspect existing tasks",
    "crew_timeout": "Crew request timed out; check its gateway",
    "crew_request_timeout": "Crew request timed out",
    "crew_version_unsupported": (
        "Crew version is unsupported or unreadable; run answerback doctor to see what it read"
    ),
    "core_read_timeout": "Core read timed out; retry the read",
    "machine_not_connected": (
        "Core delivery not attempted: the machine is not connected; nothing was sent"
    ),
    "machine_list_unreadable": (
        "Core delivery not attempted: the machine list could not be read; nothing was sent"
    ),
    "machine_listed_twice": (
        "Core delivery not attempted: the machine list names the machine more than once; "
        "nothing was sent"
    ),
    "machine_id_malformed": (
        "Core delivery not attempted: the machine list gives the machine an id that is not "
        "32 lowercase hex digits; nothing was sent"
    ),
    "core_command_timeout": (
        "Core command timed out; delivery may be uncertain. Do not resubmit blindly."
    ),
    "command_timeout": ("Command timed out; delivery may be uncertain. Do not resubmit blindly."),
    "rpc_frame_limit": "RPC message exceeds the 4 MiB limit",
    "rex_endpoint_mismatch": "Rex endpoint mismatch",
    "rex_host_mismatch": "Rex expected host mismatch",
    "rex_host_changed": "Rex server host changed",
    "rex_session_absent": "Rex session confirmed absent",
    "rex_terminal_absent": "Rex terminal confirmed absent from its session",
    "rex_block_mismatch": "Rex block identity mismatch",
    "rex_block_not_terminal": "Rex block is not a terminal",
    "rex_receive_capacity": "Rex receive capacity reached",
    "transport_stopped": "Bridge is stopped. Run answerback start first.",
    "transport_disconnected": (
        "Bridge disconnected; delivery may be uncertain. Inspect the receipt."
    ),
}
_PUBLIC_CODES = {message: code for code, message in _PUBLIC_MESSAGES.items()}
_VALIDATION_CODES = {message: code for code, message in _PUBLIC_VALIDATIONS.items()}


def register_public_validations(codes: Mapping[str, str]) -> None:
    """Add a plugin's reviewed ``ValueError`` diagnostics to the catalogue.

    Each entry is a code and the exact literal its raise sites use; the runtime renders a
    matching ``ValueError`` as that code. A plugin registers its codes when it is imported,
    so every process that loads the plugin (service, CLI, MCP server) knows them; a client
    without the plugin falls back to the generic failure text. A code or message already
    catalogued under another entry is refused, so two providers cannot share a diagnostic.
    """
    for code, message in codes.items():
        if not isinstance(code, str) or not code or not isinstance(message, str) or not message:
            raise ValueError("Public diagnostic codes and messages must be nonempty text")
        if _PUBLIC_MESSAGES.get(code, message) != message:
            raise ValueError(f"Public diagnostic code {code!r} is already catalogued")
        if _PUBLIC_CODES.get(message, code) != code:
            raise ValueError(f"Public diagnostic text of {code!r} is already catalogued")
    for code, message in codes.items():
        _PUBLIC_VALIDATIONS[code] = message
        _PUBLIC_MESSAGES[code] = message
        _PUBLIC_CODES[message] = code
        _VALIDATION_CODES[message] = code


class PublicBridgeError(BridgeError):
    """An approved code with a locally rendered fixed diagnostic."""

    def __init__(self, code: str) -> None:
        if code not in _PUBLIC_MESSAGES:
            raise ValueError("Unknown public bridge diagnostic code")
        self.code = code
        super().__init__(_PUBLIC_MESSAGES[code])


class CrewVersionUnsupported(BridgeError):
    """Kiro Crew is older than the adapter's minimum, or its version cannot be read.

    Raised before a credential is requested, so nothing was sent to the gateway. The
    message carries bounded text that the executable printed; it appears in discovery
    and doctor results. Where the error itself crosses the RPC boundary, only the
    reviewed literal of `crew_version_unsupported` is shown.
    """


def public_bridge_error(error: Exception) -> PublicBridgeError | None:
    """Approve only exact known bridge diagnostics, including existing raise sites."""
    if isinstance(error, CrewVersionUnsupported):
        # The type is the approval: its own text names what an executable printed.
        return PublicBridgeError("crew_version_unsupported")
    if isinstance(error, BridgeError):
        code = _PUBLIC_CODES.get(str(error))
    elif type(error) is ValueError:
        code = _VALIDATION_CODES.get(str(error))
    else:
        return None
    return PublicBridgeError(code) if code is not None else None


class AdmissionRefused(ValueError):
    """No reservation or external submission occurred for this attempt.

    Raise only before the adapter crosses its reservation boundary. An arbitrary
    ValueError does not carry this guarantee and must remain uncertain.
    """

    def __init__(self, blocking_task_id: str, target: str | None = None) -> None:
        self.blocking_task_id = blocking_task_id
        self.target = target
        context = f" ({target})" if target else ""
        super().__init__(
            f"Target already has task {blocking_task_id}{context}; inspect its receipt first"
        )


def admission_refusal_info(error: AdmissionRefused) -> dict[str, Any]:
    return {
        "kind": "admission_refused",
        "blocking_task_id": error.blocking_task_id,
        "target": error.target,
    }


def admission_refusal_from_info(info: dict[str, Any]) -> AdmissionRefused | None:
    """Validate the limited public context before reconstructing a refusal."""
    if set(info) != {"kind", "blocking_task_id", "target"} or info["kind"] != "admission_refused":
        return None
    task_id, target = info["blocking_task_id"], info["target"]
    if not isinstance(task_id, str) or len(task_id) != 36:
        return None
    try:
        if str(UUID(task_id)) != task_id:
            return None
    except ValueError:
        return None
    if target is not None and (
        not isinstance(target, str) or not 0 < len(target) <= 1024 or not target.isprintable()
    ):
        return None
    return AdmissionRefused(task_id, target)
