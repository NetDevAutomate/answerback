"""The public package smoke must start independently of historical release probes."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from zipfile import ZipFile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def helpers() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "package_smoke_helpers", SCRIPTS / "package_smoke_helpers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_smoke_entrypoint_can_display_help_without_starting_fixtures() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "smoke_install.py"), "--help"],
        cwd=SCRIPTS.parent,
        env={**os.environ, "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--upgrade-from" in result.stdout


def test_smoke_environment_preserves_parent_and_scopes_native_state(tmp_path: Path) -> None:
    original = {
        "HOME": "/home/example",
        "CODEX_HOME": "/home/example/codex",
        "PYTHONPATH": "/development/source",
        "VIRTUAL_ENV": "/development/venv",
        "ANSWERBACK_CONFIG": "/live/config",
        "HERDR_SOCKET_PATH": "/live/socket",
        "REX_SERVER": "unix:///live/rex",
        "KEEP": "unchanged",
    }
    before = dict(original)
    env = helpers().isolated_environment(tmp_path, original)
    assert original == before
    assert env["HOME"] == original["HOME"]
    assert env["KEEP"] == "unchanged"
    assert env["CODEX_HOME"] == str(tmp_path / "codex")
    assert env["UV_TOOL_DIR"] == str(tmp_path / "tools")
    assert env["UV_OFFLINE"] == "1"
    assert not set(env).intersection(
        {"PYTHONPATH", "VIRTUAL_ENV", "ANSWERBACK_CONFIG", "HERDR_SOCKET_PATH", "REX_SERVER"}
    )


def test_wheel_identity_reads_metadata_without_a_release_specific_version(tmp_path: Path) -> None:
    wheel = tmp_path / "example.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("example-2.3.4.dist-info/METADATA", "Name: example\nVersion: 2.3.4\n")
    identity = helpers().wheel_identity(wheel)
    assert identity["name"] == "example"
    assert identity["version"] == "2.3.4"
    assert identity["path"] == str(wheel.resolve())
    assert len(identity["sha256"]) == 64


@pytest.mark.parametrize("records", [[], ["Name: example\n"], ["Name: x\nVersion: 1\n"] * 2])
def test_wheel_identity_refuses_missing_or_ambiguous_metadata(
    tmp_path: Path, records: list[str]
) -> None:
    wheel = tmp_path / "invalid.whl"
    with ZipFile(wheel, "w") as archive:
        for index, record in enumerate(records):
            archive.writestr(f"example-{index}.dist-info/METADATA", record)
    with pytest.raises(ValueError, match="metadata|name/version"):
        helpers().wheel_identity(wheel)


def test_importable_serves_a_folder_for_the_block_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    (tmp_path / "smoke_sibling_fixture.py").write_text("VALUE = 7\n")
    path_before = list(sys.path)
    with helpers().importable(tmp_path):
        import smoke_sibling_fixture  # type: ignore[import-not-found]

        assert smoke_sibling_fixture.VALUE == 7
        assert sys.path[0] == str(tmp_path.resolve())
    assert sys.path == path_before
    assert "smoke_sibling_fixture" not in sys.modules
    with pytest.raises(ModuleNotFoundError):
        import smoke_sibling_fixture  # noqa: F401


def test_importable_removes_its_entry_when_the_block_fails(tmp_path: Path) -> None:
    path_before = list(sys.path)
    with pytest.raises(RuntimeError), helpers().importable(tmp_path):
        raise RuntimeError("load failed")
    assert sys.path == path_before


def tool_result(
    structured: object = None, *, text: str = "", is_error: bool = False
) -> SimpleNamespace:
    """A stand-in for the client's tool result: text blocks, structured content, error flag."""
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)] if text else [],
        structured_content=structured,
        is_error=is_error,
    )


class RecordingClient:
    """A stand-in MCP client: records each call and answers from a table by tool name."""

    def __init__(self, answers: dict[str, SimpleNamespace]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((name, dict(arguments)))
        return self.answers[name]


def test_tool_answer_returns_the_structured_content() -> None:
    assert helpers().tool_answer("task_get", tool_result({"id": "t1"})) == {"id": "t1"}


def test_tool_answer_names_the_tool_and_carries_the_text_of_an_error() -> None:
    smoke = helpers()
    refused = tool_result(text="Error executing tool peer_submit: refused", is_error=True)
    with pytest.raises(smoke.SmokeStepFailed) as failure:
        smoke.tool_answer("peer_submit", refused)
    assert "peer_submit" in str(failure.value)
    assert "Error executing tool peer_submit: refused" in str(failure.value)


def test_tool_answer_refuses_a_result_without_structured_content() -> None:
    smoke = helpers()
    with pytest.raises(smoke.SmokeStepFailed) as failure:
        smoke.tool_answer("bus_status", tool_result(None, text="plain words only"))
    assert "bus_status" in str(failure.value)
    assert "plain words only" in str(failure.value)
    assert isinstance(failure.value, AssertionError)


def test_tool_answer_refuses_an_error_even_with_structured_content() -> None:
    smoke = helpers()
    with pytest.raises(smoke.SmokeStepFailed, match="task_get"):
        smoke.tool_answer("task_get", tool_result({"id": "t1"}, text="late", is_error=True))


async def test_search_then_submit_passes_back_the_binding_the_search_returned() -> None:
    smoke = helpers()
    peers = [
        {"peer_id": "herdr:other", "name": "other", "binding": "b-other"},
        {"peer_id": "herdr:worker", "name": "worker", "binding": "b-worker"},
    ]
    client = RecordingClient(
        {
            "peer_search": tool_result({"peers": peers}),
            "peer_submit": tool_result({"id": "task-1"}),
        }
    )
    sent = await smoke.search_then_submit(
        client,
        search={"provider": "herdr", "query": "worker"},
        chosen=lambda peer: peer.get("name") == "worker",
        submit={"selector": "worker", "message": "m", "request_id": "r", "provider": "herdr"},
    )
    assert sent == {"id": "task-1"}
    assert client.calls == [
        ("peer_search", {"provider": "herdr", "query": "worker"}),
        (
            "peer_submit",
            {
                "selector": "worker",
                "message": "m",
                "request_id": "r",
                "provider": "herdr",
                "binding": "b-worker",
            },
        ),
    ]


@pytest.mark.parametrize("peer_extra", [{}, {"binding": None}, {"binding": 7}])
async def test_search_then_submit_sends_no_binding_for_a_peer_without_a_string_binding(
    peer_extra: dict[str, Any],
) -> None:
    smoke = helpers()
    peer = {"peer_id": "desktop:abc", "kind": "discovered_codex", **peer_extra}
    client = RecordingClient(
        {
            "peer_search": tool_result({"peers": [peer]}),
            "peer_submit": tool_result({"id": "task-2"}),
        }
    )
    sent = await smoke.search_then_submit(
        client,
        search={"provider": "desktop", "query": "Fixture Desktop"},
        chosen=lambda found: found.get("kind") == "discovered_codex",
        submit={"message": "m", "request_id": "r"},
    )
    assert sent == {"id": "task-2"}
    assert [name for name, _ in client.calls] == ["peer_search", "peer_submit"]
    assert client.calls[1][1] == {"selector": "desktop:abc", "message": "m", "request_id": "r"}


async def test_search_then_submit_stops_before_the_submit_when_no_peer_is_meant() -> None:
    smoke = helpers()
    client = RecordingClient(
        {
            "peer_search": tool_result({"peers": [{"peer_id": "herdr:other", "name": "other"}]}),
            "peer_submit": tool_result({"id": "never"}),
        }
    )
    with pytest.raises(smoke.SmokeStepFailed, match="peer_search"):
        await smoke.search_then_submit(
            client,
            search={"provider": "herdr", "query": "worker"},
            chosen=lambda peer: peer.get("name") == "worker",
            submit={"selector": "worker", "message": "m", "request_id": "r"},
        )
    assert [name for name, _ in client.calls] == ["peer_search"]


async def test_search_then_submit_names_the_search_when_it_fails() -> None:
    smoke = helpers()
    client = RecordingClient({"peer_search": tool_result(text="search broke", is_error=True)})
    with pytest.raises(smoke.SmokeStepFailed, match="peer_search.*search broke"):
        await smoke.search_then_submit(
            client,
            search={"provider": "herdr", "query": "worker"},
            chosen=lambda peer: True,
            submit={"message": "m", "request_id": "r"},
        )
    assert [name for name, _ in client.calls] == ["peer_search"]


def test_broker_config_argument_is_the_spelling_the_bridge_checks(tmp_path: Path) -> None:
    from answerback_bridge.bus.config import BusConfig

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    bridge_config = link / "config.toml"
    argument = helpers().broker_config_argument(bridge_config)
    # The bridge accepts a broker only when its command line is exactly this path.
    assert argument == str(BusConfig(bridge_config, 4222, "/usr/bin/true").broker_config)
    assert argument == str(real.resolve() / "bus/nats.conf")
    assert argument != str(link / "bus/nats.conf")
