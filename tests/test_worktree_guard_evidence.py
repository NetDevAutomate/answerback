"""Behavioral tests use disposable runs, Git repositories and inert native stand-ins."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlunsplit
from uuid import uuid4

import pytest
from conftest import basetemp_untraversable_component

TOOL = Path(__file__).parents[1] / "tools" / "worktree_guard_evidence.py"


@pytest.fixture(autouse=True)
def traversable_basetemp(tmp_path: Path) -> None:
    """Skip when the sandbox refuses the O_NOFOLLOW walk the evidence reader performs."""
    component = basetemp_untraversable_component(tmp_path)
    if component is not None:
        pytest.skip(
            f"basetemp component {component!r} is not O_NOFOLLOW-traversable from /; "
            "run with --basetemp=/private/tmp/<dir>"
        )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    for key, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Evidence tests",
        "GIT_AUTHOR_EMAIL": "evidence@example.invalid",
        "GIT_COMMITTER_NAME": "Evidence tests",
        "GIT_COMMITTER_EMAIL": "evidence@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    root, history, project = (tmp_path / name for name in ("run", "history", "project"))
    for path in (root, history, project):
        path.mkdir(mode=0o700)
    git(history, "init", "-q")
    write_json(
        root / "run-plan.json",
        {
            "run_id": "run-one",
            "profile": "cli",
            "project": str(project),
            "clients": ["kiro_cli", "kirocrew"],
            "expected_directed_reviews": 2,
            "expected_review_messages": 4,
        },
    )
    (project / "src").mkdir()
    (project / "src/example.py").write_text("VALUE = 1\n")
    pin(root, "verify_project.py", "raise SystemExit(0)\n")
    write_json(root / "actor-evidence.json", {"messages": [], "receipts": []})
    write_json(root / "attempt-failure-decision.json", {"status": "FAILED_INCOMPLETE"})
    return root, history, project


def pin(root: Path, name: str, source: str) -> None:
    path = root / "source-snapshot" / name
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(source)
    manifest = path.parent / "manifest.json"
    data = json.loads(manifest.read_text()) if manifest.exists() else {"files": []}
    data["files"] = [row for row in data["files"] if row["path"] != name]
    data["files"].append({"path": name, "sha256": digest(path.read_bytes())})
    write_json(manifest, data)


def invoke(
    run: tuple[Path, Path, Path],
    command: str = "capture",
    *,
    capture_id: str = "capture-one",
    options: dict[str, str | int | float] | None = None,
    input: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    root, history, _ = run
    args = [
        sys.executable,
        str(TOOL),
        command,
        "--run-root",
        str(root),
        "--history-repo",
        str(history),
        "--capture-id",
        capture_id,
    ]
    for key, value in (options or {}).items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    return subprocess.run(args, capture_output=True, text=True, timeout=30, input=input, env=env)


def capsule(history: Path, capture_id: str = "capture-one") -> Path:
    return history / "runs/run-one/snapshots" / capture_id


def test_capture_preserves_occurrences_redacts_and_commits(run):
    root, history, _ = run
    token = "canary-secret-token-08349"
    nonce = "canary-receipt-nonce-4567"
    native = root / "native-evidence/kiro_cli.jsonl"
    native.parent.mkdir()
    events = [
        {"id": "same", "consentRound": 1, "token": token, "receipt_nonce": nonce},
        {"id": "same", "consentRound": 2, "text": f"Authorization: Bearer {token}"},
    ]
    native.write_text("".join(json.dumps(event) + "\n" for event in events))
    write_json(
        root / "actor-evidence.json",
        {
            "messages": [{"message_id": "m1", "payload": {"receipt_nonce": nonce}}],
            "receipts": [{"id": 1, "message_id": "m1", "kind": "ack"}],
        },
    )
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    trace = [json.loads(line) for line in (saved / "trace.jsonl").read_text().splitlines()]
    native_rows = [row for row in trace if row["source_path"].endswith("kiro_cli.jsonl")]
    assert [row["source_ordinal"] for row in native_rows] == [1, 2]
    assert [row["event"]["consentRound"] for row in native_rows] == [1, 2]
    assert all(row["raw_source_sha256"] == digest(native.read_bytes()) for row in native_rows)
    assert len([row for row in trace if row["kind"] == "actor_message"]) == 1
    assert len([row for row in trace if row["kind"] == "actor_receipt"]) == 1
    outcome = json.loads((saved / "outcome.json").read_text())
    assert outcome["scenario_acceptance"]["status"] == "FAILED_INCOMPLETE"
    assert outcome["project_checks"]["status"] == "unknown"
    assert "coordinator_stream_missing" in outcome["trace"]["gaps"]
    assert git(history, "status", "--porcelain") == ""
    for relative in git(history, "ls-files").splitlines():
        content = (history / relative).read_bytes()
        assert token.encode() not in content
        assert nonce.encode() not in content
    assert f"<redacted:sha256:{digest(nonce.encode())}>" in (saved / "trace.jsonl").read_text()
    index = json.loads((history / "runs/index.json").read_text())
    assert index["runs"]["run-one"]["latest_capture"] == "capture-one"
    assert invoke(run).returncode != 0
    assert invoke(run, capture_id="capture-two").returncode == 0
    assert git(history, "rev-list", "--count", "HEAD") == "2"
    manifest = json.loads((capsule(history, "capture-two") / "manifest.json").read_text())
    assert manifest["collector"]["raw_source_sha256"] == digest(TOOL.read_bytes())
    assert (capsule(history, "capture-two") / manifest["collector"]["snapshot_path"]).is_file()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_build_records_actual_exit_and_never_infers_scenario_success(run, exit_code):
    root, history, _ = run
    pin(
        root,
        "verify_project.py",
        "import argparse,json,pathlib\n"
        "p=argparse.ArgumentParser();p.add_argument('--plan');p.add_argument('--output');"
        "p.add_argument('--python');p.add_argument('--timeout');a=p.parse_args()\n"
        "o=pathlib.Path(a.output);o.mkdir();"
        "(o/'project-checks.json').write_text(json.dumps("
        f"{{'status':{'passed' if exit_code == 0 else 'failed'!r},'checks':[]}}))\n"
        "print('actual verifier stdout');"
        "print('actual verifier stderr',file=__import__('sys').stderr)\n"
        f"raise SystemExit({exit_code})\n",
    )
    result = invoke(run, "build")
    assert result.returncode == (0 if exit_code == 0 else 7), result.stderr
    outcome = json.loads((capsule(history) / "outcome.json").read_text())
    assert outcome["runner"]["exit_code"] == exit_code
    assert outcome["scenario_acceptance"]["status"] == "FAILED_INCOMPLETE"
    assert outcome["project_checks"]["status"] == ("passed" if exit_code == 0 else "failed")
    assert (root / "project-builds/capture-one/runner.stdout").read_text().strip() == (
        "actual verifier stdout"
    )
    assert git(history, "status", "--porcelain") == ""


def test_build_zero_without_report_is_unknown_and_nonzero(run):
    result = invoke(run, "build")
    assert result.returncode != 0
    assert (capsule(run[1]) / "manifest.json").exists()
    outcome = json.loads((capsule(run[1]) / "outcome.json").read_text())
    assert outcome["runner"]["exit_code"] == 0
    assert outcome["project_checks"]["status"] == "unknown"


def test_supervise_forwards_exact_input_and_records_all_streams_on_failure(run):
    root, history, _ = run
    pin(
        root,
        "run_controller.py",
        "import sys,os\n"
        "assert os.environ['ANSWERBACK_ACCEPTANCE_ROOT']\n"
        "for line in sys.stdin:\n"
        " print(line,end='',flush=True);print('stderr:'+line,end='',file=sys.stderr,flush=True)\n"
        "raise SystemExit(9)\n",
    )
    sent = '{"operation":"inspect"}\n{"operation":"quit"}\n'
    result = invoke(run, "supervise", input=sent)
    assert result.returncode == 9, result.stderr
    journal = [
        json.loads(line)
        for line in (root / "orchestration/capture-one/events.jsonl").read_text().splitlines()
    ]
    assert "".join(row["text"] for row in journal if row["stream"] == "stdin") == sent
    assert "".join(row["text"] for row in journal if row["stream"] == "stdout") == sent
    assert "".join(row["text"] for row in journal if row["stream"] == "stderr") == (
        "stderr:" + sent.replace("\n", "\nstderr:", 1)
    )
    assert [row["ordinal"] for row in journal] == list(range(1, len(journal) + 1))
    outcome = json.loads((capsule(history) / "outcome.json").read_text())
    assert outcome["runner"]["exit_code"] == 9
    assert outcome["project_checks"]["status"] == "unknown"
    assert "coordinator_stream_missing" not in outcome["trace"]["gaps"]


def test_current_actor_store_preserves_all_rows_for_selected_run(run):
    root, history, _ = run
    (root / "bus").mkdir()
    with sqlite3.connect(root / "bus/actors.sqlite") as db:
        db.executescript(
            "CREATE TABLE actor_messages(message_id TEXT,run_id TEXT,payload TEXT);"
            "CREATE TABLE actor_receipts(id INTEGER,message_id TEXT,kind TEXT);"
        )
        db.executemany(
            "INSERT INTO actor_messages VALUES(?,?,?)",
            [("m1", "run-one", '{"text":"one"}'), ("m2", "other-run", '{"text":"private"}')],
        )
        db.executemany(
            "INSERT INTO actor_receipts VALUES(?,?,?)", [(1, "m1", "ack"), (2, "m2", "ack")]
        )
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    rows = json.loads((capsule(history) / "exchanges.json").read_text())["recorded_rows"]
    messages = [row for row in rows if row["kind"] == "actor_message"]
    assert [row["event"]["message_id"] for row in messages] == ["m1"]
    assert [row["event"]["id"] for row in rows if row["kind"] == "actor_receipt"] == [1]
    assert all("other-run" not in path.read_text() for path in capsule(history).rglob("*.json"))
    assert not list(capsule(history).rglob("*.sqlite"))


def test_credentials_are_removed_from_every_tracked_output(run):
    root, history, project = run
    secrets = [
        "canary-header-547812",
        "canary-nested-548613",
        "canary-cookie-45903",
        "canary-inline-59130",
        "canary-secret-with-a-quote'54213",
        "canary-url-67394",
    ]
    write_json(
        root / "native-evidence/client.json",
        {
            "headers": [{"name": "Authorization", "value": secrets[0]}],
            "payload": json.dumps({"accessToken": secrets[1], "ordinary": secrets[1]}),
            "Cookie": secrets[2],
            "text": f"password={secrets[3]}",
            "receipt_nonce": secrets[4],
            "url": "https://example.invalid/?token=" + secrets[5],
            "copied_value": secrets[0],
        },
    )
    (root / "bindings").mkdir()
    write_json(
        root / "bindings/actor.json", {"completely_unlabelled": "excluded-capability-canary"}
    )
    (project / ".env").write_text("unlabelled=excluded-dotenv-canary")
    (project / "src/example.py").write_text(f"# Copied credential: {secrets[0]}\n")
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    tracked = b"".join(
        (history / name).read_bytes() for name in git(history, "ls-files").splitlines()
    )
    for secret in [*secrets, "excluded-capability-canary", "excluded-dotenv-canary"]:
        assert secret.encode() not in tracked
    assert all(
        source["source_path"] != "bindings/actor.json"
        for source in json.loads((capsule(history) / "manifest.json").read_text())["sources"]
    )


@pytest.mark.parametrize("violation", ["symlink", "oversize", "dirty", "remote"])
def test_capture_refuses_unreviewable_inputs_without_commit(run, violation, tmp_path):
    root, history, _ = run
    options: dict[str, str | int | float] = {}
    if violation == "symlink":
        (root / "native-evidence").mkdir()
        outside = tmp_path / "secret.json"
        outside.write_text("{}")
        (root / "native-evidence/escape.json").symlink_to(outside)
    elif violation == "oversize":
        (root / "large.txt").write_text("x" * 4097)
        options = {"max_source_bytes": 4096}
    elif violation == "dirty":
        (history / "unrelated.txt").write_text("retain")
    elif violation == "remote":
        git(history, "remote", "add", "origin", "https://example.invalid/private")
    result = invoke(run, options=options)
    assert result.returncode != 0
    assert not capsule(history).exists()
    assert (
        subprocess.run(
            ["git", "-C", str(history), "rev-parse", "--verify", "HEAD"], capture_output=True
        ).returncode
        != 0
    )
    if violation == "dirty":
        assert (history / "unrelated.txt").read_text() == "retain"


def test_failed_git_hook_cannot_produce_a_green_build(run):
    root, history, _ = run
    hook = history / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o700)
    result = invoke(run, "build")
    assert result.returncode != 0
    assert "git_operation_failed:commit" in result.stderr
    assert (capsule(history) / "outcome.json").exists()
    assert (root / "project-builds/capture-one/runner.json").exists()


def test_runner_spawn_failure_is_captured_and_committed(run):
    result = invoke(run, "build", options={"python": "/no-such-interpreter"})
    assert result.returncode != 0
    outcome = json.loads((capsule(run[1]) / "outcome.json").read_text())
    assert outcome["runner"]["error"] == "FileNotFoundError"
    assert outcome["runner"]["exit_code"] is None
    assert git(run[1], "status", "--porcelain") == ""


def test_pinned_source_mismatch_never_executes(run):
    root, history, project = run
    (root / "source-snapshot/verify_project.py").write_text(
        f"from pathlib import Path;Path({str(project / 'forbidden')!r}).touch()\n"
    )
    result = invoke(run, "build")
    assert result.returncode != 0
    assert "pinned_source_digest_mismatch" in result.stderr
    assert not (project / "forbidden").exists()
    assert not (root / "project-builds").exists()
    assert not capsule(history).exists()


def test_supervise_exiting_without_reading_input_does_not_claim_delivery(run):
    root, history, _ = run
    pin(root, "run_controller.py", "print('finished',flush=True)\n")
    result = invoke(run, "supervise", input="x" * 100000)
    assert result.returncode in (0, 2), result.stderr
    saved = capsule(history)
    assert saved.exists()
    rows = [
        json.loads(line)
        for line in (root / "orchestration/capture-one/events.jsonl").read_text().splitlines()
    ]
    received = sum(row["bytes"] for row in rows if row["stream"] == "stdin_received")
    delivered = sum(row["bytes"] for row in rows if row["stream"] == "stdin")
    undelivered = sum(row["bytes"] for row in rows if row["stream"] == "stdin_undelivered")
    assert received == delivered + undelivered
    assert "finished" in result.stdout


def test_runner_timeout_retains_failure_and_raw_partial_output(run):
    root, history, _ = run
    pin(root, "run_controller.py", "import time\nprint('started',flush=True)\ntime.sleep(60)\n")
    result = invoke(run, "supervise", input="", options={"timeout": 0.3})
    assert result.returncode != 0
    outcome = json.loads((capsule(history) / "outcome.json").read_text())
    assert outcome["runner"]["error"] == "runner_timeout"
    assert outcome["runner"]["stream_completeness"] == "partial_after_runner_error"
    assert (root / "orchestration/capture-one/runner.stdout").read_text() == "started\n"


def test_failed_supervisor_commit_returns_nonzero(run):
    root, history, _ = run
    pin(root, "run_controller.py", "pass\n")
    hook = history / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o700)
    result = invoke(run, "supervise", input="")
    assert result.returncode != 0
    assert "git_operation_failed:commit" in result.stderr
    assert (root / "orchestration/capture-one/runner.json").exists()


def test_malformed_native_line_is_preserved_and_marked_partial(run):
    root, history, _ = run
    path = root / "native-evidence/broken.jsonl"
    path.parent.mkdir()
    canary = "canary-partial-" + str(uuid4())
    path.write_text('{"valid":true}\npartial native { ' + f"token={canary}\n")
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    rows = [json.loads(line) for line in (saved / "trace.jsonl").read_text().splitlines()]
    selected = [row for row in rows if row["source_path"].endswith("broken.jsonl")]
    assert len(selected) == 2
    assert "unparsed_line" in selected[1]["event"]
    assert canary not in (saved / "trace.jsonl").read_text()
    assert (
        "native-evidence/broken.jsonl:unparsed_jsonl"
        in json.loads((saved / "outcome.json").read_text())["trace"]["gaps"]
    )


def test_structured_secret_collections_and_opaque_payloads_are_redacted(run):
    root, history, _ = run
    write_json(
        root / "native-evidence/collections.json",
        {
            "api_keys": ["canary-array-19181", {"primary": "canary-object-184131"}],
            "payload_base64": "Y2FuYXJ5LW9wYXF1ZS0xODMx",
            "other": "canary-object-184131",
        },
    )
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    tracked = b"".join(path.read_bytes() for path in capsule(history).rglob("*") if path.is_file())
    for value in ("canary-array-19181", "canary-object-184131", "Y2FuYXJ5LW9wYXF1ZS0xODMx"):
        assert value.encode() not in tracked


def test_authorization_metadata_and_actual_roster_cleanup_are_preserved(run):
    root, history, project = run
    (project / "CONTRACT.md").write_text("Contract applies to this authored project.\n")
    write_json(
        project / "ACTOR_ROSTER.json",
        {
            "run_id": "run-one",
            "exchanges": [{"exchange_id": "e1", "from_actor": "a1", "to_actor": "a2"}],
        },
    )
    write_json(root / "control-results/cleanup-final-assessment.json", {"status": "observed_gone"})
    write_json(
        root / "human-authorization.json",
        {
            "authorization": {"status": "approved", "clients": ["kiro_cli", "kirocrew"]},
            "authorization_reference": "call-human-1234",
        },
    )
    write_json(
        root / "actor-evidence.json",
        {
            "messages": [
                {
                    "message_id": "m1",
                    "kind": "review_request",
                    "envelope": {"body": {"exchange_id": "e1"}},
                },
                {"message_id": "m2", "kind": "review_response", "reply_to": "m1"},
            ],
            "receipts": [{"id": 1, "message_id": "m1", "kind": "ack"}],
        },
    )
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    all_text = "".join(path.read_text() for path in (saved / "sources").glob("*.json"))
    assert "Contract applies" in all_text
    assert "call-human-1234" in all_text
    assert '"approved"' in all_text and '"kiro_cli"' in all_text
    report = json.loads((saved / "exchanges.json").read_text())
    assert report["matrix_observations"][0]["observed_request_ids"] == ["m1"]
    assert report["matrix_observations"][0]["observed_reply_ids"] == ["m2"]
    assert report["acceptance_assessed"] is False
    cleanup = json.loads((saved / "outcome.json").read_text())["cleanup"]
    assert cleanup["observations"][0]["value"]["status"] == "observed_gone"


def test_duplicate_json_keys_preserve_both_raw_values_as_unparsed_evidence(run):
    root, history, _ = run
    p = root / "native-evidence/duplicate.jsonl"
    p.parent.mkdir()
    p.write_text('{"state":"working","state":"done"}\n')
    assert invoke(run).returncode == 0
    rows = [
        json.loads(line) for line in (capsule(history) / "trace.jsonl").read_text().splitlines()
    ]
    raw = next(row["event"]["unparsed_line"] for row in rows if "unparsed_line" in row["event"])
    assert '"working"' in raw and '"done"' in raw


def test_postrun_pin_mismatch_is_recorded_with_untrusted_source(run):
    root, history, _ = run
    pin(
        root,
        "verify_project.py",
        "from pathlib import Path\nPath(__file__).write_text('# changed\\n')\n",
    )
    result = invoke(run, "build")
    assert result.returncode != 0
    saved = capsule(history)
    outcome = json.loads((saved / "outcome.json").read_text())
    assert outcome["runner"]["error"] == "pinned_source_digest_mismatch"
    assert (
        "source-snapshot/verify_project.py:pinned_source_digest_mismatch"
        in (outcome["trace"]["gaps"])
    )
    assert git(history, "status", "--porcelain") == ""


def test_total_budget_and_private_history_are_enforced(run):
    assert invoke(run, options={"max_total_bytes": 1}).returncode != 0
    run[1].chmod(0o755)
    result = invoke(run)
    assert result.returncode != 0
    assert "history_repository_must_be_private_and_owned" in result.stderr


def test_recovery_and_startup_sources_remain_distinct(run):
    root, history, _ = run
    for directory in (
        "crew-recovered-native-002",
        "startup-attempts",
        "assignments",
        "status-history",
    ):
        write_json(root / directory / "observation.json", {"original": directory})
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    paths = {
        source["source_path"]
        for source in json.loads((capsule(history) / "manifest.json").read_text())["sources"]
    }
    assert "crew-recovered-native-002/observation.json" in paths
    assert "startup-attempts/observation.json" in paths
    assert "assignments/observation.json" in paths
    assert "status-history/observation.json" in paths


def test_provider_prefixed_short_credentials_are_redacted(run):
    root, history, _ = run
    # Construct six-character values at runtime; none has a provider key prefix.
    values = ["short" + suffix for suffix in "ABC"]
    write_json(
        root / "native-evidence/env.json",
        {
            "ANTHROPIC_API_KEY": values[0],
            "AWS_SESSION_TOKEN": values[1],
            "customApiKey": values[2],
            "authorization": {"status": "approved", "run_id": "run-one"},
        },
    )
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    tracked = b"".join(path.read_bytes() for path in capsule(history).rglob("*") if path.is_file())
    for secret in values:
        assert secret.encode() not in tracked
    assert b'"approved"' in tracked


@pytest.mark.parametrize("suffix", ["json", "jsonl"])
def test_duplicate_secret_keys_preserve_conflict_without_exposing_short_values(run, suffix):
    root, history, _ = run
    path = root / f"native-evidence/duplicates.{suffix}"
    path.parent.mkdir()
    values = ["short" + str(index) for index in (1, 2)]
    # Build pairs explicitly: a dict would erase the duplicate-key boundary.
    pairs = ",".join(f'"password":{json.dumps(value)}' for value in values)
    path.write_text("{" + pairs + "}\n")
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    tracked = b"".join(path.read_bytes() for path in saved.rglob("*") if path.is_file())
    assert all(value.encode() not in tracked for value in values)
    assert b"unparsed" in tracked
    assert b'"partial"' in (saved / "outcome.json").read_bytes()


def test_retained_client_home_is_excluded_before_any_symlink_inspection(run, tmp_path):
    root, history, _ = run
    retained = root / "startup-attempts/01/retained-crew-home"
    (retained / "kirocrew-bin").mkdir(parents=True)
    (retained / "kirocrew-bin/kirocrew").symlink_to(tmp_path / "absent-vendor-executable")
    write_json(retained / "unlabelled-profile.json", {"value": "home-credential-canary-83731"})
    write_json(root / "startup-attempts/01/receipt.json", {"status": "startup_failed"})
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    tracked = b"".join(path.read_bytes() for path in saved.rglob("*") if path.is_file())
    assert b"home-credential-canary-83731" not in tracked
    manifest = json.loads((saved / "manifest.json").read_text())
    assert "retained-crew-home" in manifest["selection"]["excluded_private_tree_names"]
    paths = {row["source_path"] for row in manifest["sources"]}
    assert "startup-attempts/01/receipt.json" in paths
    assert not any("retained-crew-home" in path for path in paths)


def test_excluded_home_symlink_does_not_relax_selected_source_symlink_rejection(run, tmp_path):
    root, history, _ = run
    startup = root / "startup-attempts/01"
    startup.mkdir(parents=True)
    (startup / "retained-crew-home").symlink_to(tmp_path / "absent-private-home")
    (startup / "selected.json").symlink_to(tmp_path / "absent-evidence.json")
    result = invoke(run)
    assert result.returncode != 0
    assert "symlink_refused" in result.stderr
    assert not capsule(history).exists()


def test_only_native_evidence_records_count_as_native_events(run):
    root, history, _ = run
    expected = {
        "native-evidence/kiro_cli/observations.jsonl": "native_event",
        "orchestration/controller/events.jsonl": "coordinator_io",
        "project-builds/check/events.jsonl": "project_build_io",
        "messaging-approval-proposal-claims.jsonl": "recorded_event",
        "control-results/decisions.jsonl": "control_observation",
    }
    for path in expected:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"event":"observed"}\n')
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    rows = [json.loads(line) for line in (saved / "trace.jsonl").read_text().splitlines()]
    for path, kind in expected.items():
        assert [row["kind"] for row in rows if row["source_path"] == path] == [kind]
    outcome = json.loads((saved / "outcome.json").read_text())
    assert outcome["trace"]["counts"]["native_event"] == 1


def test_yaml_short_password_and_database_url_userinfo_never_enter_history(run):
    root, history, _ = run
    native = root / "native-evidence"
    native.mkdir()
    yaml_value = "".join(("abc", "123"))
    (native / "output.yaml").write_text(f"password: {yaml_value}\nstatus: approved\n")
    credential = "canary-" + str(uuid4())
    authority = f"alice:{credential}@db.invalid"
    database_url = urlunsplit(("postgresql", authority, "/db", "", ""))
    write_json(
        native / "connection.json",
        {
            "DATABASE_URL": database_url,
            "copied_password": credential,
        },
    )
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    tracked = b"".join(path.read_bytes() for path in saved.rglob("*") if path.is_file())
    assert yaml_value.encode() not in tracked
    assert credential.encode() not in tracked
    assert b"approved" in tracked


def test_redacted_dictionary_key_collision_preserves_both_observations(run):
    root, history, _ = run
    secret = str(uuid4())
    marker = f"<redacted:sha256:{digest(secret.encode())}>"
    write_json(
        root / "native-evidence/collision.json",
        {
            "token": secret,
            secret: {"observation": "first-observation"},
            marker: {"observation": "second-observation"},
        },
    )
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    tracked = b"".join(path.read_bytes() for path in saved.rglob("*") if path.is_file())
    assert b"first-observation" in tracked
    assert b"second-observation" in tracked
    assert secret.encode() not in tracked
    outcome = json.loads((saved / "outcome.json").read_text())
    assert "native-evidence/collision.json:redacted_key_collision" in outcome["trace"]["gaps"]
    assert outcome["trace"]["completeness"] == "partial"


@pytest.mark.parametrize("bad_source", ["scenario_status", "actor_message", "roster_exchange"])
def test_malformed_derivations_preserve_sources_and_explicit_gaps(run, bad_source):
    root, history, project = run
    if bad_source == "scenario_status":
        write_json(root / "scenario-result.json", {"status": []})
    elif bad_source == "actor_message":
        write_json(root / "actor-evidence.json", {"messages": [42]})
        write_json(
            project / "ACTOR_ROSTER.json",
            {
                "run_id": "run-one",
                "exchanges": [{"exchange_id": "e1"}],
            },
        )
    else:
        write_json(project / "ACTOR_ROSTER.json", {"run_id": "run-one", "exchanges": [42]})
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    outcome = json.loads((saved / "outcome.json").read_text())
    assert outcome["trace"]["completeness"] == "partial"
    assert any("invalid_" in gap for gap in outcome["trace"]["gaps"])
    if bad_source == "scenario_status":
        assert outcome["scenario_acceptance"]["status"] == "unknown"
    if bad_source == "actor_message":
        rows = [json.loads(line) for line in (saved / "trace.jsonl").read_text().splitlines()]
        assert any(row["kind"] == "actor_message" and row["event"] == 42 for row in rows)
    assert git(history, "status", "--porcelain") == ""


@pytest.mark.parametrize("secret", ["a", "b", "synthetic-long-secret-43128"])
def test_source_secrets_never_rewrite_generated_metadata(run, secret):
    root, history, _ = run
    plan = json.loads((root / "run-plan.json").read_text())
    plan["run_id"] = "run-b"
    write_json(root / "run-plan.json", plan)
    write_json(root / "attempt-failure-decision.json", {"status": "blocked"})
    path = root / "native-evidence/password.json"
    write_json(
        path,
        {
            "password": secret,
            "copied": secret,
            "text": f"password: {secret}",
        },
    )
    raw_hash = digest(path.read_bytes())
    result = invoke(run, capture_id="capture-ab")
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    saved = history / "runs/run-b/snapshots/capture-ab"
    manifest = json.loads((saved / "manifest.json").read_text())
    outcome = json.loads((saved / "outcome.json").read_text())
    assert response["run_id"] == manifest["run_id"] == outcome["run_id"] == "run-b"
    assert response["capture_id"] == manifest["capture_id"] == "capture-ab"
    assert response["outcome"]["scenario_acceptance"]["status"] == "blocked"
    assert outcome["scenario_acceptance"]["status"] == "blocked"
    assert outcome["trace"]["counts"]["native_observation"] == 1
    for item in manifest["files"]:
        assert item["sha256"] == digest((saved / item["path"]).read_bytes())
        assert item["bytes"] == (saved / item["path"]).stat().st_size
    source = next(
        item
        for item in manifest["sources"]
        if item["source_path"] == ("native-evidence/password.json")
    )
    assert source["raw_source_sha256"] == raw_hash
    view = json.loads((saved / source["snapshot_path"]).read_text())
    assert view["raw_source_sha256"] == raw_hash
    marker = f"<redacted:sha256:{digest(secret.encode())}>"
    assert view["content"]["password"] == marker
    assert view["content"]["copied"] == marker
    assert view["content"]["text"] == f"password: {marker}"
    index = json.loads((history / "runs/index.json").read_text())
    assert index["runs"]["run-b"]["latest_capture"] == "capture-ab"
    assert index["runs"]["run-b"]["captures"][0]["manifest_sha256"] == digest(
        (saved / "manifest.json").read_bytes()
    )
    assert git(history, "status", "--porcelain") == ""


def test_new_short_secret_does_not_rewrite_previous_history_index(run):
    root, history, _ = run
    assert invoke(run).returncode == 0
    previous = json.loads((history / "runs/index.json").read_text())["runs"]["run-one"]["captures"][
        0
    ]
    write_json(root / "native-evidence/password.json", {"password": "b"})
    result = invoke(run, capture_id="capture-b")
    assert result.returncode == 0, result.stderr
    current = json.loads((history / "runs/index.json").read_text())["runs"]["run-one"]
    assert current["captures"][0] == previous
    assert current["latest_capture"] == "capture-b"


def test_credential_equal_to_generated_digest_does_not_change_digest_metadata(run):
    root, history, project = run
    expected = digest((project / "src/example.py").read_bytes())
    write_json(root / "native-evidence/password.json", {"password": expected})
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    saved = capsule(history)
    manifest = json.loads((saved / "manifest.json").read_text())
    project_source = next(
        item for item in manifest["sources"] if item["source_path"] == "project/src/example.py"
    )
    assert project_source["raw_source_sha256"] == expected
    source = next(
        item
        for item in manifest["sources"]
        if item["source_path"] == "native-evidence/password.json"
    )
    view = json.loads((saved / source["snapshot_path"]).read_text())
    assert view["content"]["password"] == f"<redacted:sha256:{digest(expected.encode())}>"
    for item in manifest["files"]:
        assert item["sha256"] == digest((saved / item["path"]).read_bytes())


@pytest.fixture
def evidence_tool(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the same script used by CLI tests without running its entry point."""
    spec = importlib.util.spec_from_file_location("worktree_guard_evidence_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def build_args(evidence_tool: ModuleType, run: tuple[Path, Path, Path]) -> argparse.Namespace:
    root, history, _ = run
    return evidence_tool.parser().parse_args(
        [
            "build",
            "--run-root",
            str(root),
            "--history-repo",
            str(history),
            "--capture-id",
            "capture-one",
        ]
    )


@pytest.mark.parametrize("replacement", ["overwrite", "rename"])
def test_build_executes_verified_bytes_after_source_substitution(
    run: tuple[Path, Path, Path],
    evidence_tool: ModuleType,
    build_args: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    """red_at_base: replacing a pinned script at launch cannot change executed bytes."""
    root, history, _ = run
    original = "print('verified source')\n"
    pin(root, "verify_project.py", original)
    target = root / "source-snapshot/verify_project.py"
    real_popen = subprocess.Popen
    substituted = False

    def replace_before_launch(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        nonlocal substituted
        if argv[0] == sys.executable and kwargs.get("cwd") == root:
            if replacement == "rename":
                target.rename(target.with_suffix(".previous"))
            target.write_text("print('substituted source')\n")
            substituted = True
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", replace_before_launch)
    evidence_tool.execute_and_capture(build_args)
    assert substituted
    assert (root / "project-builds/capture-one/runner.stdout").read_text() == "verified source\n"
    outcome = json.loads((capsule(history) / "outcome.json").read_text())
    assert outcome["runner"]["source_sha256"] == digest(original.encode())
    assert outcome["runner"]["error"] == (
        "source_snapshot_unlisted_entry"
        if replacement == "rename"
        else "pinned_source_digest_mismatch"
    )


@pytest.mark.parametrize("directory", ["run", "source-snapshot"])
@pytest.mark.parametrize("violation", ["permissions", "owner"])
def test_execution_requires_private_owned_source_directories(
    run: tuple[Path, Path, Path],
    evidence_tool: ModuleType,
    build_args: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    violation: str,
) -> None:
    """red_at_base: unsafe run or snapshot metadata is refused before launch."""
    root, history, _ = run
    selected = root if directory == "run" else root / directory
    if violation == "permissions":
        selected.chmod(0o755)
    else:
        identity = selected.stat()
        real_fstat = os.fstat

        def foreign_owner(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(evidence_tool.EvidenceError, match="run_root_must_be_private_and_owned"):
        evidence_tool.execute_and_capture(build_args)
    assert not (root / "project-builds").exists()
    assert not capsule(history).exists()


def test_build_preserves_large_encoded_script_context(run: tuple[Path, Path, Path]) -> None:
    """pins_existing: script metadata, imports, encoding and large sources still work."""
    root, _, _ = run
    pin(root, "sibling.py", "VALUE = 'sibling'\n")
    source = (
        "# coding: latin-1\n#" + "x" * (256 * 1024) + "\nimport json,pathlib,sys,sibling\n"
        "assert __name__ == '__main__'\n"
        "assert sys.modules[__name__].__dict__ is globals()\n"
        "assert __file__ == sys.argv[0]\n"
        "assert sys.path[0] == str(pathlib.Path(__file__).parent)\n"
        "assert '--announcements' in sys.argv\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "output.mkdir()\n"
        "(output/'project-checks.json').write_text(json.dumps({'status':'passed','checks':[]}))\n"
        "print('café:' + sibling.VALUE)\n"
    )
    pin(root, "verify_project.py", source)
    raw = source.encode("latin-1")
    (root / "source-snapshot/verify_project.py").write_bytes(raw)
    manifest_path = root / "source-snapshot/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][-1]["sha256"] = digest(raw)
    write_json(manifest_path, manifest)
    write_json(root / "artifact-manifest.json", {})
    result = invoke(run, "build")
    assert result.returncode == 0, result.stderr
    assert (root / "project-builds/capture-one/runner.stdout").read_text() == "café:sibling\n"


@pytest.mark.parametrize("entry", ["read", "run", "project"])
def test_hard_linked_credential_alias_is_refused(
    run: tuple[Path, Path, Path],
    evidence_tool: ModuleType,
    tmp_path: Path,
    entry: str,
) -> None:
    """red_at_base: an admitted alias cannot import an excluded credential file."""
    root, history, project = run
    credential = tmp_path / "credentials.json"
    credential.write_text(
        '{"DUMMY_NOT_A_REAL_SECRET":"test-only-value"}\n'  # pragma: allowlist secret
    )
    parent = project / "src" if entry == "project" else root
    alias = parent / "notes.md"
    alias.hardlink_to(credential)
    assert evidence_tool.excluded(credential)
    assert not evidence_tool.excluded(alias)
    if entry == "read":
        with pytest.raises(evidence_tool.EvidenceError, match="hard_linked_source_refused"):
            evidence_tool.read(alias, 1024)
    else:
        result = invoke(run)
        assert result.returncode == 2
        assert "hard_linked_source_refused" in result.stderr
        assert not capsule(history).exists()


def test_link_created_during_read_is_refused(
    evidence_tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: the descriptor must still have one link after reading."""
    source = tmp_path / "notes.md"
    source.write_text("test-only content\n")
    identity = source.stat()
    real_fstat = os.fstat
    linked = False

    def link_after_initial_stat(fd: int) -> os.stat_result:
        nonlocal linked
        info = real_fstat(fd)
        if not linked and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            (tmp_path / "credentials.json").hardlink_to(source)
            linked = True
        return info

    monkeypatch.setattr(os, "fstat", link_after_initial_stat)
    with pytest.raises(evidence_tool.EvidenceError, match="hard_linked_source_refused"):
        evidence_tool.read(source, 1024)


@pytest.mark.parametrize("bytecode_setting", [None, "", "0", "1"])
def test_pump_passes_only_selected_environment(
    evidence_tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bytecode_setting: str | None,
) -> None:
    """red_at_base: a real child receives selected settings, never planted dummy secrets."""
    allowed = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LANG": "C",
        "LC_ALL": "C",
        "LC_CTYPE": "C",
        "TMPDIR": str(tmp_path),
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    denied = {
        "T67_UNRELATED_SECRET": "dummy-unrelated-value",  # pragma: allowlist secret
        "AWS_SESSION_TOKEN": "dummy-cloud-value",  # pragma: allowlist secret
        "SSH_AUTH_SOCK": "dummy-agent-socket",
        "PYTHONPATH": str(tmp_path / "dummy-python-path"),
    }
    for key, value in {**allowed, **denied, "ANSWERBACK_ACCEPTANCE_ROOT": "dummy-root"}.items():
        monkeypatch.setenv(key, value)
    if bytecode_setting is None:
        monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    else:
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", bytecode_setting)
    names = [*allowed, *denied, "ANSWERBACK_ACCEPTANCE_ROOT"]
    child = f"import json,os;print(json.dumps({{key:os.environ.get(key) for key in {names!r}}}))"
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    result = evidence_tool.pump([sys.executable, "-c", child], tmp_path, logs, 5, 65536, False)
    assert result["exit_code"] == 0
    observed = json.loads((logs / "runner.stdout").read_text())
    assert {key: observed[key] for key in denied} == dict.fromkeys(denied)
    assert {key: observed[key] for key in allowed} == allowed
    assert observed["ANSWERBACK_ACCEPTANCE_ROOT"] == str(tmp_path)
    assert result["environment_passed"] == sorted([*allowed, "ANSWERBACK_ACCEPTANCE_ROOT"])
    assert result["environment_requested_unset"] == []
    journal = (logs / "events.jsonl").read_text()
    assert all(value not in journal for value in denied.values())


@pytest.mark.parametrize("when", ["after_preflight", "during_mkdir"])
def test_write_refuses_substituted_parent(
    evidence_tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """red_at_base: parent substitution must never redirect an evidence payload."""
    snapshots = tmp_path / "history/runs/run-one/snapshots"
    snapshots.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = snapshots / "capture-one/outcome.json"
    evidence_tool.canonical(destination)
    if when == "after_preflight":
        snapshots.rename(snapshots.with_name("original-snapshots"))
        snapshots.symlink_to(outside, target_is_directory=True)
    else:
        real_mkdir = os.mkdir

        def substitute_after_mkdir(
            path: str | Path, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> None:
            real_mkdir(path, mode, dir_fd=dir_fd)
            if Path(path).name == "capture-one":
                (snapshots / "capture-one").rename(snapshots / "original-capture")
                (snapshots / "capture-one").symlink_to(outside, target_is_directory=True)

        monkeypatch.setattr(os, "mkdir", substitute_after_mkdir)
    with pytest.raises(evidence_tool.EvidenceError, match="symlink_refused|evidence_write_failed"):
        evidence_tool.write(destination, b'{"dummy":"evidence"}\n')
    assert list(outside.iterdir()) == []


def test_write_preserves_private_exclusive_creation(
    evidence_tool: ModuleType, tmp_path: Path
) -> None:
    """pins_existing: create private evidence and never overwrite an existing file."""
    path = tmp_path / "capture-one/outcome.json"
    evidence_tool.write(path, b"original\n")
    assert path.read_bytes() == b"original\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises((OSError, evidence_tool.EvidenceError)):
        evidence_tool.write(path, b"replacement\n")
    assert path.read_bytes() == b"original\n"


@pytest.fixture
def listed_nested_snapshot(run: tuple[Path, Path, Path]) -> Path:
    """Add a listed nested module so the membership walk must visit its parent."""
    snapshot = run[0] / "source-snapshot"
    module = snapshot / "pkg/mod.py"
    module.parent.mkdir()
    module.write_text("VALUE = 'listed nested module'\n")
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].append({"path": "pkg/mod.py", "sha256": digest(module.read_bytes())})
    write_json(manifest_path, manifest)
    return snapshot


@pytest.mark.parametrize("command", ["build", "supervise"])
@pytest.mark.parametrize(
    "entry",
    [
        "colorsys.py",
        "__pycache__",
        "empty",
        "symlink",
        "listed_symlink",
        "fifo",
        "pkg/extra.py",
        "pkg/__pycache__",
        "pkg/sub",
    ],
)
def test_execution_refuses_unlisted_snapshot_entries(
    run: tuple[Path, Path, Path],
    listed_nested_snapshot: Path,
    evidence_tool: ModuleType,
    build_args: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    entry: str,
) -> None:
    """red_at_base: unexpected snapshot entries must be refused before interpreter launch."""
    root, history, project = run
    pin(root, "run_controller.py", "import colorsys\n")
    pin(root, "verify_project.py", "import colorsys\n")
    snapshot = listed_nested_snapshot
    if entry in {"__pycache__", "empty", "pkg/__pycache__", "pkg/sub"}:
        (snapshot / entry).mkdir()
        if entry == "pkg/__pycache__":
            (snapshot / entry / "mod.ignored.pyc").write_bytes(b"not bytecode\n")
    elif entry in {"symlink", "listed_symlink"}:
        name = "verify_project.py" if entry == "listed_symlink" else "alias.py"
        if entry == "listed_symlink":
            (snapshot / name).unlink()
        (snapshot / name).symlink_to(project / "src/example.py")
    elif entry == "fifo":
        os.mkfifo(snapshot / "pipe")
    else:
        (snapshot / entry).write_text("raise AssertionError('unlisted module ran')\n")
    build_args.command = command
    real_popen = subprocess.Popen
    launches = []

    def forbid_interpreter(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        if argv[0] == sys.executable and kwargs.get("cwd") == root:
            launches.append(argv)
            raise AssertionError("interpreter must not start")
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", forbid_interpreter)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(evidence_tool.EvidenceError, match="^source_snapshot_unlisted_entry$"):
        evidence_tool.execute_and_capture(build_args)
    assert launches == []
    assert not (root / "project-builds").exists()
    assert not (root / "orchestration").exists()
    assert not capsule(history).exists()


@pytest.mark.parametrize("command", ["build", "supervise"])
@pytest.mark.parametrize("entry", ["unlisted.py", "pkg/extra.py", "pkg/__pycache__", "pkg/sub"])
def test_execution_records_snapshot_entry_created_during_run(
    run: tuple[Path, Path, Path],
    listed_nested_snapshot: Path,
    command: str,
    entry: str,
) -> None:
    """red_at_base: post-run membership changes remain visible in the runner and capsule."""
    root, history, _ = run
    target = "verify_project.py" if command == "build" else "run_controller.py"
    create = (
        "path.mkdir()\n"
        if entry in {"pkg/__pycache__", "pkg/sub"}
        else "path.write_text('# new\\n')\n"
    )
    if entry == "pkg/__pycache__":
        create += "(path / 'mod.ignored.pyc').write_bytes(b'not bytecode\\n')\n"
    pin(
        root,
        target,
        f"from pathlib import Path\npath = Path(__file__).parent / {entry!r}\n" + create,
    )
    result = invoke(run, command, input="")
    assert result.returncode == 2, result.stderr
    parent = "project-builds" if command == "build" else "orchestration"
    runner = json.loads((root / parent / "capture-one/runner.json").read_text())
    assert runner["exit_code"] == 0
    assert runner["error"] == "source_snapshot_unlisted_entry"
    outcome = json.loads((capsule(history) / "outcome.json").read_text())
    assert outcome["runner"]["error"] == "source_snapshot_unlisted_entry"
    assert git(history, "status", "--porcelain") == ""


@pytest.mark.parametrize("command", ["build", "supervise"])
def test_execution_accepts_manifest_listed_nested_module(
    run: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """pins_existing: listed nested imports work even without a bytecode environment setting."""
    root, _, _ = run
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    snapshot = root / "source-snapshot"
    (snapshot / "pkg").mkdir()
    (snapshot / "pkg/mod.py").write_text("VALUE = 'listed nested module'\n")
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].append(
        {"path": "pkg/mod.py", "sha256": digest((snapshot / "pkg/mod.py").read_bytes())}
    )
    write_json(manifest_path, manifest)
    target = "verify_project.py" if command == "build" else "run_controller.py"
    pin(
        root,
        target,
        "import json,sys\nfrom pathlib import Path\nfrom pkg.mod import VALUE\n"
        "print(VALUE)\n"
        "if '--output' in sys.argv:\n"
        " output = Path(sys.argv[sys.argv.index('--output') + 1]); output.mkdir()\n"
        " (output/'project-checks.json').write_text(json.dumps({'status':'passed','checks':[]}))\n",
    )
    result = invoke(run, command, input="")
    assert result.returncode == 0, result.stderr
    parent = "project-builds" if command == "build" else "orchestration"
    assert (root / parent / "capture-one/runner.stdout").read_text() == "listed nested module\n"


@pytest.mark.parametrize("command", ["build", "supervise"])
@pytest.mark.parametrize("child", ["subprocess", "spawn", "spawn-E", "spawn-I"])
def test_execution_keeps_python_descendants_cache_free(
    run: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    child: str,
) -> None:
    """red_at_base: propagate bytecode policy to children; also red at the round-1 tip."""
    root, history, _ = run
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    pin(root, "helper.py", "def double(value):\n    return value * 2\n")
    options: dict[str, str | int | float] = {}
    if child in {"spawn-E", "spawn-I"}:
        # Exercise real spawn workers that ignore PYTHON* environment settings.
        interpreter = root / "python-with-flags"
        interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -{child[-1]} "$@"\n')
        interpreter.chmod(0o700)
        options["python"] = str(interpreter)
    if child == "subprocess":
        work = (
            " subprocess.run([sys.executable, '-c', "
            "'import sys; sys.path.insert(0, sys.argv[1]); "
            "import helper; print(helper.double(3))', str(Path(__file__).parent)], check=True)\n"
        )
    else:
        work = (
            " import helper\n"
            " with multiprocessing.get_context('spawn').Pool(1) as pool:\n"
            "  print(pool.map(helper.double, [3])[0])\n"
        )
    target = "verify_project.py" if command == "build" else "run_controller.py"
    pin(
        root,
        target,
        "import json,multiprocessing,subprocess,sys\nfrom pathlib import Path\n"
        "if __name__ == '__main__':\n" + work + " if '--output' in sys.argv:\n"
        "  output = Path(sys.argv[sys.argv.index('--output') + 1]); output.mkdir()\n"
        "  (output/'project-checks.json').write_text("
        "json.dumps({'status':'passed','checks':[]}))\n",
    )
    parent = "project-builds" if command == "build" else "orchestration"
    for capture_id in ("capture-one", "capture-two"):
        result = invoke(run, command, input="", options=options, capture_id=capture_id)
        assert result.returncode == 0, result.stderr
        output = root / parent / capture_id
        assert (output / "runner.stdout").read_text() == "6\n"
        assert not list((root / "source-snapshot").rglob("__pycache__"))
        runner = json.loads((output / "runner.json").read_text())
        assert runner["exit_code"] == 0 and runner["error"] is None
        recorded = json.loads((capsule(history, capture_id) / "outcome.json").read_text())
        assert recorded["runner"]["environment_passed"] == runner["environment_passed"]


def test_capture_keeps_ignoring_unlisted_snapshot_entries(run: tuple[Path, Path, Path]) -> None:
    """pins_existing: capture selects listed files without imposing execution membership."""
    root, history, _ = run
    (root / "source-snapshot/colorsys.py").write_text("# unused\n")
    (root / "source-snapshot/__pycache__").mkdir()
    result = invoke(run)
    assert result.returncode == 0, result.stderr
    sources = json.loads((capsule(history) / "manifest.json").read_text())["sources"]
    assert all(row["source_path"] != "source-snapshot/colorsys.py" for row in sources)


@pytest.mark.parametrize("command", ["build", "supervise"])
def test_execution_passes_only_explicit_extra_environment(
    run: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """red_at_base: repeated pass-env selects names and records selection without values."""
    root, history, _ = run
    monkeypatch.setenv("T67_CHOSEN", "dummy-chosen-value")
    monkeypatch.setenv("T67_OMITTED", "dummy-omitted-value")
    monkeypatch.delenv("T67_UNSET", raising=False)
    target = "verify_project.py" if command == "build" else "run_controller.py"
    pin(
        root,
        target,
        "import json,os\n"
        "print(json.dumps({name:os.environ.get(name) "
        "for name in ['T67_CHOSEN','T67_OMITTED','T67_UNSET']}))\n",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            command,
            "--run-root",
            str(root),
            "--history-repo",
            str(history),
            "--capture-id",
            "capture-one",
            "--pass-env",
            "T67_CHOSEN",
            "--pass-env",
            "T67_UNSET",
            "--pass-env",
            "T67_CHOSEN",
        ],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (2 if command == "build" else 0), result.stderr
    parent = "project-builds" if command == "build" else "orchestration"
    output = root / parent / "capture-one"
    assert json.loads((output / "runner.stdout").read_text()) == {
        "T67_CHOSEN": "dummy-chosen-value",
        "T67_OMITTED": None,
        "T67_UNSET": None,
    }
    runner = json.loads((output / "runner.json").read_text())
    assert runner["exit_code"] == 0 and runner["error"] is None
    assert runner["environment_passed"] == sorted(set(runner["environment_passed"]))
    assert "T67_CHOSEN" in runner["environment_passed"]
    assert "ANSWERBACK_ACCEPTANCE_ROOT" in runner["environment_passed"]
    assert "T67_OMITTED" not in runner["environment_passed"]
    assert "T67_UNSET" not in runner["environment_passed"]
    assert runner["environment_requested_unset"] == ["T67_UNSET"]
    recorded = json.loads((capsule(history) / "outcome.json").read_text())["runner"]
    assert recorded["environment_passed"] == runner["environment_passed"]
    assert recorded["environment_requested_unset"] == ["T67_UNSET"]
    for value in ("dummy-chosen-value", "dummy-omitted-value"):
        assert value not in json.dumps(runner)
        assert value not in json.dumps(recorded)


@pytest.mark.parametrize("command", ["build", "supervise"])
@pytest.mark.parametrize(
    "name",
    [
        "",
        "bad-name",
        "1NAME",
        "NAME=value",
        "T67_*",
        "PYTHONPATH",
        "PYTHONCUSTOM",
        "PYTHONDONTWRITEBYTECODE",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "__PYVENV_LAUNCHER__",
        "ANSWERBACK_ACCEPTANCE_ROOT",
    ],
)
def test_execution_refuses_unsafe_environment_names(
    run: tuple[Path, Path, Path], command: str, name: str
) -> None:
    """red_at_base: invalid or code-loading names receive one fixed preflight refusal."""
    root, history, _ = run
    pin(root, "run_controller.py", "raise AssertionError('must not start')\n")
    result = invoke(run, command, input="", options={"pass_env": name})
    assert result.returncode == 2
    assert json.loads(result.stderr) == {"status": "failed", "error": "pass_env_name_refused"}
    assert not (root / "project-builds").exists()
    assert not (root / "orchestration").exists()
    assert not capsule(history).exists()


@pytest.mark.parametrize("name", ["PYTHONPATH", "__PYVENV_LAUNCHER__"])
def test_pump_refuses_unsafe_environment_names_before_opening_logs(
    evidence_tool: ModuleType, tmp_path: Path, name: str
) -> None:
    """red_at_base: direct imported pump callers cannot bypass environment validation."""
    with pytest.raises(evidence_tool.EvidenceError, match="^pass_env_name_refused$"):
        evidence_tool.pump(
            [sys.executable, "-c", "pass"],
            tmp_path,
            tmp_path,
            5,
            65536,
            False,
            pass_env=[name],
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", ["build", "supervise"])
@pytest.mark.parametrize("directory", ["run", "source-snapshot"])
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_execution_preserves_missing_directory_error(
    run: tuple[Path, Path, Path], command: str, directory: str, kind: str
) -> None:
    """pins_existing: restore BASE's missing/wrong-type error (red at the reviewed tip)."""
    root, history, _ = run
    selected = root if directory == "run" else root / directory
    selected.rename(selected.with_name("original-" + selected.name))
    if kind == "file":
        selected.write_text("not a directory\n")
    result = invoke(run, command, input="")
    assert result.returncode == 2
    assert json.loads(result.stderr) == {"status": "failed", "error": "source_read_failed"}
    assert not capsule(history).exists()


def test_write_keeps_opened_parent_during_final_create(
    evidence_tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: a last-instant parent swap cannot redirect an already opened directory."""
    parent = tmp_path / "history/runs/run-one/snapshots/capture-one"
    parent.mkdir(parents=True)
    held = parent.with_name("held-capture")
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = parent / "outcome.json"
    substituted = False

    def substitute() -> None:
        nonlocal substituted
        if not substituted:
            parent.rename(held)
            parent.symlink_to(outside, target_is_directory=True)
            substituted = True

    real_open = os.open
    real_io_open = io.open

    def create_after_swap(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if Path(path).name == destination.name and flags & os.O_CREAT:
            substitute()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def path_create_after_swap(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        # Path.open uses io.open; keep the same boundary observable for a path-based writer.
        if isinstance(file, (str, Path)) and Path(file) == destination and "x" in mode:
            substitute()
        return real_io_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", create_after_swap)
    monkeypatch.setattr(io, "open", path_create_after_swap)
    evidence_tool.write(destination, b"retained directory\n")
    assert substituted
    assert list(outside.iterdir()) == []
    assert (held / destination.name).read_bytes() == b"retained directory\n"
