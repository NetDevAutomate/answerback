"""`errors.register_public_validations`: the hook a provider package adds its reviewed
``ValueError`` diagnostics through (modular split, step 11).

The catalogue is the kernel's module state, so each test works on a copy it restores.
"""

from collections.abc import Iterator

import pytest

from answerback_bridge import errors
from answerback_bridge.errors import (
    PublicBridgeError,
    public_bridge_error,
    register_public_validations,
)

_TABLES = ("_PUBLIC_VALIDATIONS", "_PUBLIC_MESSAGES", "_PUBLIC_CODES", "_VALIDATION_CODES")


@pytest.fixture(autouse=True)
def restored_catalogue() -> Iterator[None]:
    saved = {name: dict(getattr(errors, name)) for name in _TABLES}
    try:
        yield
    finally:
        for name, table in saved.items():
            live = getattr(errors, name)
            live.clear()
            live.update(table)


def test_a_registered_code_is_what_the_catalogue_serves_afterwards() -> None:
    register_public_validations({"probe_refused": "Probe refused its reviewed reason"})

    rendered = public_bridge_error(ValueError("Probe refused its reviewed reason"))
    assert isinstance(rendered, PublicBridgeError)
    assert rendered.code == "probe_refused"
    assert str(rendered) == "Probe refused its reviewed reason"
    assert str(PublicBridgeError("probe_refused")) == "Probe refused its reviewed reason"
    # Only the exact literal maps; a `ValueError` subclass or other text stays masked.
    assert public_bridge_error(ValueError("Probe refused its reviewed reason.")) is None
    assert public_bridge_error(ValueError("Probe refused")) is None


def test_the_same_mapping_registered_twice_is_a_no_op() -> None:
    codes = {"probe_a": "Probe text A", "probe_b": "Probe text B"}
    register_public_validations(codes)
    before = {name: dict(getattr(errors, name)) for name in _TABLES}

    register_public_validations(dict(codes))  # a plugin imported twice (service, CLI, MCP)

    assert {name: dict(getattr(errors, name)) for name in _TABLES} == before


def test_a_code_already_catalogued_under_another_message_is_refused() -> None:
    before = {name: dict(getattr(errors, name)) for name in _TABLES}

    with pytest.raises(ValueError, match="code 'transport_stopped' is already catalogued"):
        register_public_validations({"transport_stopped": "different reviewed text"})
    with pytest.raises(ValueError, match="code 'rex_disabled' is already catalogued"):
        register_public_validations({"rex_disabled": "different reviewed text"})

    assert {name: dict(getattr(errors, name)) for name in _TABLES} == before


def test_a_message_already_catalogued_under_another_code_is_refused() -> None:
    before = {name: dict(getattr(errors, name)) for name in _TABLES}
    kernel_text = errors._PUBLIC_MESSAGES["transport_stopped"]
    validation_text = errors._PUBLIC_VALIDATIONS["rex_disabled"]

    with pytest.raises(ValueError, match="text of 'probe_stopped' is already catalogued"):
        register_public_validations({"probe_stopped": kernel_text})
    with pytest.raises(ValueError, match="text of 'probe_rex' is already catalogued"):
        register_public_validations({"probe_rex": validation_text})

    assert {name: dict(getattr(errors, name)) for name in _TABLES} == before


@pytest.mark.parametrize(
    "codes",
    [
        {1: "Probe text"},
        {"": "Probe text"},
        {"probe": ""},
        {"probe": None},
        {"probe": 3},
        {b"probe": "Probe text"},
    ],
    ids=["int-code", "empty-code", "empty-text", "none-text", "int-text", "bytes-code"],
)
def test_a_malformed_entry_is_refused(codes: dict[object, object]) -> None:
    before = {name: dict(getattr(errors, name)) for name in _TABLES}

    with pytest.raises(ValueError, match="must be nonempty text"):
        register_public_validations(codes)  # type: ignore[arg-type]

    assert {name: dict(getattr(errors, name)) for name in _TABLES} == before


def test_a_refused_mapping_registers_nothing_even_when_an_entry_is_valid() -> None:
    """Every entry is checked before any is written, so a plugin's catalogue lands whole or
    not at all."""
    before = {name: dict(getattr(errors, name)) for name in _TABLES}

    with pytest.raises(ValueError):
        register_public_validations(
            {"probe_valid": "Probe valid text", "transport_stopped": "different reviewed text"}
        )

    assert {name: dict(getattr(errors, name)) for name in _TABLES} == before
    assert public_bridge_error(ValueError("Probe valid text")) is None
