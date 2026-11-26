"""Kernel address behavior and its inward-only import boundary."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.addresses import (
    address_machine,
    sendable_address,
    validate_machine_names,
)
from answerback_bridge.config import Config
from answerback_bridge.providers.herdr.core import Core

PACKAGE = Path(__file__).parents[1] / "src" / "answerback_bridge"


def _core_imports(path: Path) -> list[int]:
    """Return source lines that import the provider-specific core module."""
    tree = ast.parse(path.read_text(), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "core" or alias.name.endswith(".core") for alias in node.names
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and (
            node.module == "core"
            or (node.module is not None and node.module.endswith(".core"))
            or any(alias.name == "core" for alias in node.names)
        ):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("module", ["config.py", "addresses.py"])
def test_kernel_address_modules_do_not_import_core(module: str) -> None:
    path = PACKAGE / module
    assert path.is_file(), f"missing kernel module: {path}"
    assert _core_imports(path) == []


@pytest.mark.parametrize(
    ("address", "machine"),
    [("t7", None), ("tAb9", None), ("example-host/t5", "example-host"), ("a.b_c-d/t1", "a.b_c-d")],
)
def test_address_machine_splits_exact_core_addresses(address: str, machine: str | None) -> None:
    assert address_machine(address) == machine


@pytest.mark.parametrize(
    "address",
    [
        "",
        "7",
        "example-host/",
        "/t5",
        "example-host/t5 ",
        " example-host/t5",
        "a/b/t5",
        "example host/t5",
        None,
        5,
    ],
)
def test_address_machine_refuses_anything_else(address: Any) -> None:
    with pytest.raises(ValueError):
        address_machine(address)


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("t7", True),
        ("example-host/t5", True),
        ("Example-Host/t5", False),
        ("example-host2/t5", False),
        ("example-host /t5", False),
        ("example-host/t5 ", False),
        ("mini/t5", False),
        ("other/t5", False),
        ("not an address", False),
    ],
)
def test_sendable_address_is_exact(address: str, allowed: bool) -> None:
    assert sendable_address(address, frozenset({"example-host"})) is allowed


def test_core_reexports_and_uses_the_same_rule() -> None:
    from answerback_bridge.providers.herdr import core as core_module

    assert core_module.address_machine is address_machine
    assert core_module.sendable_address is sendable_address
    assert core_module.validate_machine_names is validate_machine_names
    core = Core(send_machines=["example-host"])
    for address in ("t7", "example-host/t5", "example-host2/t5", "Example-Host/t5"):
        assert core._sendable_address(address) is sendable_address(address, {"example-host"})


@pytest.mark.parametrize(
    "names",
    [
        ["example host"],
        [" example-host"],
        ["example-host "],
        ["example-host/t1"],
        [""],
        ["-example-host"],
        ["mac\nmini"],
        ["x" * 129],
        [7],
        "example-host",
        ["example-host", "example-host"],
    ],
)
def test_malformed_machine_names_are_refused(names: Any) -> None:
    with pytest.raises(ValueError):
        validate_machine_names(names)
    with pytest.raises(ValueError):
        Core("hc", send_machines=names)
    with pytest.raises(ValueError):
        Config(hc_send_machines=names)


@pytest.mark.parametrize("names", [["local"], ["example-host", "local"]])
def test_machine_name_local_is_refused(names: list[str]) -> None:
    """Review open question 3: `local` would collide with the `core:local` observer."""
    for build in (validate_machine_names, lambda n: Core("hc", send_machines=n)):
        with pytest.raises(ValueError, match="reserved"):
            build(names)
    with pytest.raises(ValueError, match="reserved"):
        Config(hc_send_machines=names)
    # Exact comparison: another spelling is a different name and observer.
    assert validate_machine_names(["Local", "localhost"]) == ("Local", "localhost")


def test_machine_names_are_returned_exactly_as_an_immutable_sequence() -> None:
    assert validate_machine_names(["example-host", "a.b_c-d"]) == ("example-host", "a.b_c-d")


def test_machine_name_list_is_bounded() -> None:
    names = [f"machine-{index}" for index in range(64)]
    assert validate_machine_names(names) == tuple(names)
    with pytest.raises(ValueError, match="at most 64"):
        validate_machine_names([*names, "one-too-many"])
