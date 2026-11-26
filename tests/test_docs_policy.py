"""The task_complete caller policy is stated where operators and adapter authors read it."""

from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
POLICY = "Any local socket client may call `task_complete`; the caller's identity is not checked."


def section(text: str, start: str) -> str:
    """Return the text from the first occurrence of ``start`` up to the next ``##`` heading."""
    begin = text.index(start)
    end = text.find("\n## ", begin)
    return text[begin:] if end == -1 else text[begin:end]


def test_readme_states_the_task_complete_caller_policy() -> None:
    readme = (PROJECT / "README.md").read_text()
    paragraphs = section(readme, "not an isolation boundary").split("\n\n")
    # The policy is the paragraph right after the one stating the OS-user boundary.
    assert len(paragraphs) >= 2, "no paragraph follows the OS-user boundary statement"
    assert POLICY in paragraphs[1]
    assert "`complete_worker`" in paragraphs[1]
    assert "not implemented" in paragraphs[1]


def test_adapters_invariant_8_states_the_task_complete_caller_policy() -> None:
    adapters = (PROJECT / "docs" / "ADAPTERS.md").read_text()
    invariants = section(adapters, "## Receipt and failure invariants")
    assert f"8. {POLICY}" in invariants
    assert invariants.index("7. ") < invariants.index(f"8. {POLICY}")
    assert "`complete_worker`" in invariants


def r1_table(document: Path) -> str:
    """Return the R1 support table without its document-specific evidence link."""
    text = document.read_text()
    start = text.index("| R1 status | Route | Boundary | Address starts with |")
    end = text.index("\n\n", start)
    return text[start:end]


def test_r1_support_table_is_identical_in_public_documentation() -> None:
    """Keep the release boundary consistent wherever operators read it."""
    expected = r1_table(PROJECT / "README.md")
    assert r1_table(PROJECT / "docs" / "HARNESSES.md") == expected
    assert r1_table(PROJECT / "docs" / "RECIPIENTS.md") == expected


def test_installed_bus_upgrade_uses_tool_entrypoints_and_preserves_receipt_extras() -> None:
    troubleshooting = (PROJECT / "docs" / "TROUBLESHOOTING.md").read_text()
    bus = section(troubleshooting, "## The optional bus")
    receipt = "~/.local/share/uv/tools/answerback/uv-receipt.toml"
    assert receipt in bus
    assert "~/.local/bin/answerback" in bus
    assert "~/.local/bin/agent-bus" in bus
    assert bus.index(receipt) < bus.index("uv tool install --force")


def test_tool_removal_requires_stopping_the_broker_first() -> None:
    warning = "Run `agent-bus stop` before `answerback uninstall --remove-tool`."
    assert warning in (PROJECT / "README.md").read_text()
    assert warning in (PROJECT / "docs" / "TROUBLESHOOTING.md").read_text()
