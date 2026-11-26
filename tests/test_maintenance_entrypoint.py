"""Exercise maintenance through the actual CLI without starting the runtime."""

import json
import sys
from pathlib import Path

import pytest

from answerback_bridge import cli


@pytest.mark.parametrize("action", [[], ["stats"]])
def test_maintenance_entrypoint_is_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: list[str],
) -> None:
    def runtime_forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("maintenance must not connect to or start a runtime")

    monkeypatch.setattr(cli, "RemoteBridge", runtime_forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        ["answerback", "--config", str(tmp_path / "config.json"), "maintenance", *action],
    )
    cli.main()
    output = capsys.readouterr()
    assert not output.err
    if action:
        assert all(
            store["status"] == "absent" for store in json.loads(output.out)["stores"].values()
        )
    else:
        assert "stats" in output.out and "plan" in output.out and "compact" in output.out
    assert list(tmp_path.iterdir()) == []


def test_maintenance_refusal_has_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from answerback_bridge.maintenance import Maintenance, MaintenanceError

    def refuse(self: Maintenance, selection: object) -> None:
        raise MaintenanceError("unsupported_journal", "Journal mode is unsupported")

    monkeypatch.setattr(Maintenance, "stats", refuse)
    monkeypatch.setattr(
        sys,
        "argv",
        ["answerback", "--config", str(tmp_path / "config.json"), "maintenance", "stats"],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "Journal mode is unsupported",
        "code": "unsupported_journal",
    }
