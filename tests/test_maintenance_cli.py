import argparse
from pathlib import Path

from test_maintenance import fixture_task


def test_cli_preview_apply_and_bare_help_do_not_start_services(tmp_path: Path) -> None:
    from answerback_bridge.maintenance_cli import add_parser, dispatch

    root = argparse.ArgumentParser()
    root.set_defaults(config=tmp_path / "bridge.json")
    add_parser(root.add_subparsers(dest="command", required=True))
    assert dispatch(root.parse_args(["maintenance"]))["status"] == "help"
    store, task = fixture_task(tmp_path)
    output = tmp_path / "plan.json"
    plan = dispatch(root.parse_args(["maintenance", "plan", "--output", str(output)]))
    assert plan["status"] == "planned" and output.exists()
    assert store.get(task["id"])["result"] == "private done"
    applied = dispatch(root.parse_args(["maintenance", "apply", "--plan", str(output)]))
    assert applied["status"] == "applied"
    assert store.get(task["id"])["result"] is None
