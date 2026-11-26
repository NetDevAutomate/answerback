"""Opt-in authentic wheel upgrade/rollback gate; every write is disposable."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from package_smoke_helpers import isolated_environment, wheel_identity

PROJECT = Path(__file__).resolve().parents[1]


def validate_package_pair(old: dict[str, str], new: dict[str, str]) -> None:
    if old["name"] != "answerback" or old["version"] != "0.9.0":
        raise ValueError("This gate requires the authentic Answerback 0.9.0 distribution")
    if new["name"] != "answerback" or new["version"] not in {"0.10.0", "0.10.0+internal"}:
        raise ValueError("Candidate must be Answerback 0.10.0 or 0.10.0+internal")


def run_gate(previous: Path, candidate: Path) -> dict[str, Any]:
    old, new = wheel_identity(previous), wheel_identity(candidate)
    validate_package_pair(old, new)
    probe = PROJECT / "scripts" / "package_roundtrip_probe.py"
    with tempfile.TemporaryDirectory(prefix="ab-package-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        env = isolated_environment(root, dict(os.environ))

        def command(*argv: str) -> str:
            result = subprocess.run(
                argv, env=env, cwd=root, text=True, capture_output=True, timeout=180
            )
            if result.returncode:
                raise AssertionError(f"Command failed: {argv!r}\n{result.stdout}\n{result.stderr}")
            return result.stdout

        def install(identity: dict[str, str]) -> Path:
            command("uv", "tool", "install", "--force", identity["path"])
            python = root / "tools" / identity["name"] / "bin" / "python"
            assert python.is_file()
            provenance = json.loads(
                command(
                    str(python),
                    "-c",
                    "import importlib.metadata,json,sys;"
                    "print(json.dumps({'version':importlib.metadata.version('answerback'),"
                    "'python':sys.executable}))",
                )
            )
            assert provenance["version"] == identity["version"]
            assert Path(provenance["python"]).absolute() == python
            return python

        def phase(python: Path, name: str) -> dict[str, Any]:
            return json.loads(command(str(python), str(probe), name, str(root)))

        seeded = phase(install(old), "seed")
        upgraded = phase(install(new), "upgrade")
        saved_configuration = phase(root / "tools/answerback/bin/python", "candidate_save")
        candidate_snapshot = phase(root / "tools/answerback/bin/python", "snapshot")
        rolled_back = phase(install(old), "rollback")
        phase(root / "tools/answerback/bin/python", "write_old")
        reupgraded = phase(install(new), "reupgrade")
        executable = str(root / "bin/answerback")
        for entry in ("answerback", "answerback-bridge", "herdr-bridge", "codex-herdr"):
            assert "start" in command(str(root / "bin" / entry), "--help")
        assert "submit" in command(str(root / "bin/agent-bus"), "--help")
        config = str(root / "bridge.json")
        assert json.loads(command(executable, "--config", config, "maintenance", "stats"))["stores"]
        plan_path = str(root / "reviewed-plan.json")
        plan = json.loads(
            command(
                executable,
                "--config",
                config,
                "maintenance",
                "plan",
                "--output",
                plan_path,
            )
        )
        assert plan["actions"] > 0, plan
        applied = json.loads(
            command(executable, "--config", config, "maintenance", "apply", "--plan", plan_path)
        )
        assert applied["status"] == "applied", applied
        retained = phase(root / "tools/answerback/bin/python", "retained")
        installed_mcp = phase(root / "tools/answerback/bin/python", "mcp")
        pruned_snapshot = phase(root / "tools/answerback/bin/python", "snapshot")
        pruned_rollback = phase(install(old), "pruned_rollback")
        final = phase(install(new), "retained")
        # Compaction requires evidence that an owned service is stopped. Establish
        # that prerequisite in a separate fixture with every native provider disabled.
        compact_root = root / "compact"
        compact_config = str(compact_root / "bridge.json")
        command(
            str(root / "tools/answerback/bin/python"),
            "-c",
            "import sys;from pathlib import Path;from answerback_bridge.config import Config;"
            "Config(codex_enabled=False,provider_selection=[]).save(Path(sys.argv[1]))",
            compact_config,
        )
        try:
            command(executable, "--config", compact_config, "start", "--standalone")
        finally:
            command(executable, "--config", compact_config, "stop")
        compact = json.loads(
            command(
                executable, "--config", compact_config, "maintenance", "compact", "--store", "tasks"
            )
        )
        assert compact["status"] == "compacted", compact
        command("uv", "tool", "uninstall", "answerback")
        assert not (root / "bin/answerback").exists()
        return {
            "previous": old,
            "candidate": new,
            "seed": seeded,
            "upgrade": upgraded,
            "candidate_saved_old_schema_configuration": saved_configuration,
            "candidate_database_snapshot": candidate_snapshot,
            "rollback": rolled_back,
            "reupgrade_after_old_positional_insert": reupgraded,
            "installed_maintenance_plan": plan,
            "installed_maintenance_apply": applied,
            "retained": retained,
            "installed_mcp_retained_result": installed_mcp,
            "pruned_database_snapshot": pruned_snapshot,
            "rollback_after_pruning": pruned_rollback,
            "reupgrade_after_pruned_rollback": final,
            "installed_compact": compact,
            "temporary_resources": "removed",
            "live_providers": "not contacted",
        }


def main() -> None:
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--previous", type=Path, required=True)
    options.add_argument("--candidate", type=Path, required=True)
    options.add_argument("--report", type=Path)
    args = options.parse_args()
    result = run_gate(args.previous.resolve(), args.candidate.resolve())
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(encoded)
    print(encoded)


if __name__ == "__main__":
    main()
