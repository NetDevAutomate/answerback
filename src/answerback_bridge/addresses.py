"""Generic Herdr terminal address validation and send policy."""

import re
from collections.abc import Collection
from typing import Any

_MACHINE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_ADDRESS = re.compile(rf"(?:{_MACHINE}/)?t[A-Za-z0-9]{{1,127}}\Z")
MAX_SEND_MACHINES = 64
# The name `identity_observer` and the capacity report give this machine's own
# core; a remote machine allow-listed under it would read as local.
RESERVED_MACHINE = "local"


def validate_machine_names(names: Any) -> tuple[str, ...]:
    """Return allow-listed machine names exactly as given, or raise ValueError.

    Each name must be a whole `hc machine ls` name (the part before `/tNN` in an
    address). Names are compared exactly later, so no case folding or trimming
    happens here: anything with whitespace, a slash or a control character is
    refused rather than repaired. The name `local` is refused: `core:local` names
    this machine's core, so a remote machine under that name would read as local.
    """
    if not isinstance(names, (list, tuple)) or len(names) > MAX_SEND_MACHINES:
        raise ValueError(f"Core send machines must be a list of at most {MAX_SEND_MACHINES} names")
    if not all(isinstance(name, str) and re.fullmatch(_MACHINE, name) for name in names):
        raise ValueError("Core send machine names must be exact hc machine names")
    if RESERVED_MACHINE in names:
        raise ValueError(
            "Core send machine name 'local' is reserved for this machine's own core; "
            "use the machine's hc name"
        )
    if len(set(names)) != len(names):
        raise ValueError("Core send machine names must not repeat")
    return tuple(names)


def address_machine(address: Any) -> str | None:
    """Return the machine of an exact Core address: None for a local `tNN`.

    `machine/tNN` gives `machine` exactly as written. Anything that is not an
    exact `tID` or `machine/tID` address raises ValueError.
    """
    machine, slash, _ = _address(address).rpartition("/")
    return machine if slash else None


def identity_observer(address: Any) -> str:
    """`core:local` for a local terminal, `core:<machine>` for one on another machine.

    It names the Core that reads the terminal's identity before and after a send.
    A malformed address raises ValueError.
    """
    machine = address_machine(address)
    return "core:local" if machine is None else f"core:{machine}"


def sendable_address(address: Any, machines: Collection[str]) -> bool:
    """Whether work may go to this address: local, or on an exactly listed machine.

    `machines` holds names already checked by `validate_machine_names`; the
    comparison is exact (no case folding or trimming). A malformed address is
    never sendable.
    """
    try:
        machine = address_machine(address)
    except ValueError:
        return False
    return machine is None or machine in machines


def _address(value: Any) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise ValueError("Core requires an exact tID or machine/tID terminal address")
    return value
