"""Core paths tolerate missing bus dependencies; bus paths explain the extra."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from textwrap import dedent

import pytest
from bus_helpers import BrokerFixture

from answerback_bridge.bus.extra import MISSING_BUS_EXTRA, missing_bus_extra
from answerback_bridge.config import Config
from answerback_bridge.process import BridgeError

EXTRA = "answerback[bus]"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# Each script runs in a fresh interpreter whose `nats` import is halted, which is what an
# install without the extra looks like to the package.
WITHOUT_NATS = "import sys\nsys.modules['nats'] = None\n"
WITHOUT_PSUTIL = "import sys\nimport nats\nsys.modules['psutil'] = None\n"
BROKEN_NATS_CLIENT = dedent(
    """
    import sys
    from types import ModuleType
    import nats

    sys.modules["nats.aio.client"] = ModuleType("nats.aio.client")
    """
)

PARSER_SCRIPT = WITHOUT_NATS + dedent(
    """
    import answerback_bridge.cli as cli

    cli.parser()
    print("ok")
    """
)

BUS_COMMANDS_SCRIPT = WITHOUT_NATS + dedent(
    """
    import argparse
    import asyncio
    import json
    from pathlib import Path

    from answerback_bridge.bus import cli
    from answerback_bridge.bus.api import BusAPI
    from answerback_bridge.bus.config import BusConfig
    from answerback_bridge.process import BridgeError

    config = Path(sys.argv[1])
    BusConfig.create(config, server_binary=sys.executable, port=4222)
    submit = dict(
        peer_id="kiro:worker",
        project=str(config.parent),
        message="Review",
        message_file=None,
        request_id="extra-one",
        ttl=3600,
        offline=False,
    )
    outcome = {}
    for operation in ("stop", "status", "submit"):
        args = argparse.Namespace(config=config, operation=operation, **submit)
        try:
            asyncio.run(cli.dispatch(args))
        except BridgeError as error:
            outcome[operation] = str(error)
        else:
            outcome[operation] = None
    outcome["stored"] = len(BusAPI(config).events(0, 50)["events"])
    print(json.dumps(outcome))
    """
)

SERVICE_SCRIPT = WITHOUT_NATS + dedent(
    """
    import asyncio
    import json
    from pathlib import Path

    from answerback_bridge.bus.config import BusConfig
    from answerback_bridge.bus.extra import MISSING_BUS_EXTRA
    from answerback_bridge.config import Config
    from answerback_bridge.runtime import Service
    from answerback_bridge.service import Bridge

    bridge_path = Path(sys.argv[1])
    config = Config(provider_selection=[])
    config.save(bridge_path)
    BusConfig.create(bridge_path, server_binary=sys.executable, port=4222)
    service = Service(bridge_path)
    service.bridge = Bridge(config, bridge_path)
    events = []
    service.event = lambda name, **fields: events.append((name, fields))
    asyncio.run(asyncio.wait_for(service.run_bus(), 10))
    assert events == [("bus_disabled", {"reason": MISSING_BUS_EXTRA})], events
    print(json.dumps(service.bridge.bus_state))
    """
)


def run(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *arguments], capture_output=True, text=True, timeout=60
    )


def test_cli_parser_builds_without_nats() -> None:
    result = run(PARSER_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_bus_commands_explain_the_missing_extra(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    Config(provider_selection=["kiro"], kiro_workspaces={"worker": str(tmp_path)}).save(config)
    result = run(BUS_COMMANDS_SCRIPT, str(config))
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    for operation in ("stop", "status", "submit"):
        assert outcome[operation] is not None, f"{operation} ran without nats-py"
        assert EXTRA in outcome[operation], outcome[operation]
    # A submit that cannot attempt publication is refused before anything is stored.
    assert outcome["stored"] == 0


def test_service_records_the_missing_extra_instead_of_crashing(tmp_path: Path) -> None:
    result = run(SERVICE_SCRIPT, str(tmp_path / "config.json"))
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["connected"] is False
    assert EXTRA in state["last_error"], state


def test_pyproject_declares_bus_extra() -> None:
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    assert not any(entry.startswith("nats-py") for entry in project["dependencies"])
    bus = project["optional-dependencies"]["bus"]
    assert any(entry.startswith("nats-py") for entry in bus), bus


def test_broker_fixture_skips_without_nats_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "nats", None)
    with pytest.raises(pytest.skip.Exception, match=r"answerback\[bus\]"):
        BrokerFixture(tmp_path)


@pytest.mark.parametrize("factory", ["broker_class", "jetstream_bus_class"])
def test_bus_factories_explain_missing_psutil(factory: str) -> None:
    """red_at_review_tip: an older bus installation gets the extra installation guide."""
    pytest.importorskip("nats")
    result = run(
        WITHOUT_PSUTIL
        + dedent(
            """
            from answerback_bridge.bus import extra
            from answerback_bridge.process import BridgeError

            try:
                getattr(extra, sys.argv[1])()
            except BridgeError as error:
                print(str(error))
            else:
                raise AssertionError("Missing process inspection must refuse the bus")
            """
        ),
        factory,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == MISSING_BUS_EXTRA
    assert result.stderr == ""


@pytest.mark.parametrize("missing", ["nats", "psutil"])
def test_bus_command_reports_missing_dependency_as_json(tmp_path: Path, missing: str) -> None:
    """pins_existing for nats; red_at_review_tip for psutil: preserve the CLI error shape."""
    if missing == "psutil":
        pytest.importorskip("nats")
    result = run(
        (WITHOUT_NATS if missing == "nats" else WITHOUT_PSUTIL)
        + dedent(
            """
            from pathlib import Path
            from answerback_bridge.bus.config import BusConfig
            from answerback_bridge.cli import main

            config = Path(sys.argv[1])
            BusConfig.create(config, server_binary=sys.executable, port=4222)
            sys.argv = ["answerback", "--config", str(config), "bus", "status"]
            main()
            """
        ),
        str(tmp_path / "config.json"),
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == json.dumps({"error": MISSING_BUS_EXTRA})


@pytest.mark.parametrize("missing", ["nats", "psutil"])
def test_cli_help_works_without_bus_dependency(missing: str) -> None:
    """pins_existing: core help must not import either optional bus dependency."""
    if missing == "psutil":
        pytest.importorskip("nats")
    result = run(
        (WITHOUT_NATS if missing == "nats" else WITHOUT_PSUTIL)
        + dedent(
            """
            from answerback_bridge.cli import main

            sys.argv = ["answerback", "--help"]
            main()
            """
        )
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "--help" in result.stdout
    assert result.stderr == ""


def test_service_records_missing_psutil_instead_of_crashing(tmp_path: Path) -> None:
    """red_at_review_tip: the core service stays available when its bus extra is incomplete."""
    pytest.importorskip("nats")
    result = run(
        SERVICE_SCRIPT.replace(WITHOUT_NATS, WITHOUT_PSUTIL, 1), str(tmp_path / "config.json")
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["connected"] is False
    assert state["last_error"] == MISSING_BUS_EXTRA


@pytest.mark.parametrize("name", [None, "unrelated", "unrelated.module"])
@pytest.mark.parametrize("error_type", [ImportError, ModuleNotFoundError])
def test_bus_extra_preserves_unrelated_import_errors(
    name: str | None, error_type: type[ImportError]
) -> None:
    """pins_existing: unrelated import defects must not be presented as a missing extra."""
    error = error_type("Fixture import defect", name=name)
    with pytest.raises(ImportError) as raised:
        missing_bus_extra(error)
    assert raised.value is error


@pytest.mark.parametrize(
    ("error_type", "name"),
    [
        (ImportError, "nats"),
        (ImportError, "psutil"),
        (ImportError, "nats.aio.client"),
        (ImportError, "psutil._common"),
        (ModuleNotFoundError, "nats.js"),
        (ModuleNotFoundError, "psutil._common"),
    ],
)
def test_bus_extra_preserves_broken_dependency_errors(
    error_type: type[ImportError], name: str
) -> None:
    """red_at_review_tip: installed package defects retain the original exception."""
    error = error_type("Fixture dependency import defect", name=name)
    with pytest.raises(error_type) as raised:
        missing_bus_extra(error)
    assert raised.value is error


@pytest.mark.parametrize("name", ["nats", "psutil"])
def test_bus_extra_explains_only_absent_root_packages(name: str) -> None:
    """pins_existing: genuinely absent bus packages keep the installation guidance."""
    error = ModuleNotFoundError("Fixture missing package", name=name)
    explained = missing_bus_extra(error)
    assert isinstance(explained, BridgeError)
    assert str(explained) == MISSING_BUS_EXTRA


@pytest.mark.parametrize("factory", ["broker_class", "jetstream_bus_class"])
def test_bus_factories_preserve_cannot_import_name(factory: str) -> None:
    """red_at_review_tip: a stand-in dependency with a missing symbol is a broken install."""
    pytest.importorskip("nats")
    result = run(
        BROKEN_NATS_CLIENT
        + dedent(
            """
            from answerback_bridge.bus import extra

            try:
                getattr(extra, sys.argv[1])()
            except ImportError as error:
                assert type(error) is ImportError
                assert error.name == "nats.aio.client"
                assert "cannot import name 'Client'" in str(error)
            else:
                raise AssertionError("The broken dependency must not load")
            """
        ),
        factory,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_bus_command_preserves_broken_dependency_traceback(tmp_path: Path) -> None:
    """red_at_review_tip: CLI reports the import defect instead of missing-extra JSON."""
    pytest.importorskip("nats")
    result = run(
        BROKEN_NATS_CLIENT
        + dedent(
            """
            from pathlib import Path
            from answerback_bridge.bus.config import BusConfig
            from answerback_bridge.cli import main

            config = Path(sys.argv[1])
            BusConfig.create(config, server_binary=sys.executable, port=4222)
            sys.argv = ["answerback", "--config", str(config), "bus", "status"]
            main()
            """
        ),
        str(tmp_path / "config.json"),
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Traceback (most recent call last):" in result.stderr
    assert "ImportError: cannot import name 'Client' from 'nats.aio.client'" in result.stderr
    assert MISSING_BUS_EXTRA not in result.stderr


def test_service_reports_broken_dependency_in_bus_status(tmp_path: Path) -> None:
    """red_at_review_tip: startup import failures remain visible after the worker exits."""
    pytest.importorskip("nats")
    result = run(
        BROKEN_NATS_CLIENT
        + dedent(
            """
            import asyncio
            import json
            from pathlib import Path

            from answerback_bridge.bus.config import BusConfig
            from answerback_bridge.config import Config
            from answerback_bridge.runtime import Service
            from answerback_bridge.service import Bridge

            async def check():
                bridge_path = Path(sys.argv[1])
                config = Config(provider_selection=[])
                config.save(bridge_path)
                BusConfig.create(bridge_path, server_binary=sys.executable, port=4222)
                service = Service(bridge_path)
                service.bridge = Bridge(config, bridge_path)
                events = []
                service.event = lambda name, **fields: events.append((name, fields))
                worker = asyncio.create_task(service.run_bus())
                try:
                    await asyncio.wait_for(worker, 10)
                except ImportError as error:
                    assert type(error) is ImportError
                    assert error.name == "nats.aio.client"
                    assert "cannot import name 'Client'" in str(error)
                else:
                    raise AssertionError("The original import failure must be re-raised")
                assert events == [], events
                status = await service.bridge.call("bus_status", {})
                print(json.dumps(status["coordinator"]))

            asyncio.run(check())
            """
        ),
        str(tmp_path / "config.json"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    state = json.loads(result.stdout)
    assert state["connected"] is False
    assert state["last_error"] == "ImportError", state
    assert MISSING_BUS_EXTRA not in result.stdout
