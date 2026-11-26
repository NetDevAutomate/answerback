"""`answerback rebalance`: a preview that writes its readings file and nothing else."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge import cli
from answerback_bridge.config import Config
from answerback_bridge.placement import probe, rebalance_cli
from answerback_bridge.placement.facts import Facts, HarnessFacts, MachineFacts
from answerback_bridge.placement.probe import LOCAL, PROBE_SCRIPT, Reading
from answerback_bridge.placement.rebalance import ManagedWorker, plan_rebalance

START = 1_800_000_000.0


def output(load: float, cores: int = 16) -> str:
    return (
        f"os=Linux\ncores={cores}\nload={load} {load} {load}\nmemtotal_kb=1000\n"
        "memavailable_kb=900\nlargest_kb=100\nlargest_name=python\nprocs=12\n"
        "harness_procs=5\nprobe_end=1\n"
    )


@dataclass
class Stage:
    """A bridge folder, a project folder and scripted machines; nothing real is run."""

    home: Path
    project: Path
    invoke: Callable[..., tuple[int, str, str]]
    outputs: dict[str, str]
    calls: list[list[str]] = field(default_factory=list)
    clock: float = START

    @property
    def readings(self) -> Path:
        return self.home / "placement-readings.json"

    def stored(self) -> dict[str, list[dict[str, Any]]]:
        return json.loads(self.readings.read_text())["machines"]

    def tree(self) -> dict[str, tuple[bytes, int, int]]:
        root = self.home.parent
        return {
            str(path.relative_to(root)): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def november(self) -> None:
        """22 November 13:04: load 181 on 16 cores here; mac-a at 0.18 a core, 0 of 10."""
        self.outputs.update({LOCAL: output(181), "mac-a": output(2.88)})
        Config(hc_send_machines=["mac-a"]).save(self.home / "config.json")
        policy = {
            "harnesses": {"claude": {}},
            "placement": {
                "machines": {
                    "mac-a": {
                        "project": "/work/answerback",
                        "max_total": 10,
                        "harnesses": {"claude": {"ready": True}},
                    }
                }
            },
        }
        (self.project / "status").mkdir()
        (self.project / "status" / "routing.json").write_text(json.dumps(policy))


@pytest.fixture
def stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> Stage:
    home, project = tmp_path / "bridge", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    made: list[Stage] = []

    async def runner(argv: list[str], *, env: dict[str, str]) -> tuple[int, str]:
        made[0].calls.append(argv)
        return 0, made[0].outputs[LOCAL if argv[0] == "/bin/sh" else argv[3]]

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("rebalance started a process or called the bridge")

    monkeypatch.setattr(probe, "run_probe", runner)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refused)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refused)
    monkeypatch.setattr(cli.RemoteBridge, "call", refused)
    monkeypatch.setattr(cli, "default_config_path", lambda: home / "config.json")
    monkeypatch.setattr(rebalance_cli, "wall_clock", lambda: made[0].clock)
    monkeypatch.chdir(project)

    def invoke(*arguments: str, after: float = 0.0) -> tuple[int, str, str]:
        made[0].clock += after
        monkeypatch.setattr(sys, "argv", ["answerback", "rebalance", *arguments])
        code = 0
        try:
            cli.main()
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 1
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    made.append(Stage(home, project, invoke, {LOCAL: output(1.6), "mac-a": output(1.6)}))
    return made[0]


def test_default_changes_nothing(stage: Stage) -> None:
    stage.november()
    registry = stage.home / "routing-workers.json"
    registry.write_text(json.dumps({"version": 1, "workers": {}, "gates": {}}))
    before = stage.tree()
    for after in (0.0, 120.0):
        code, _, stderr = stage.invoke("--machine", "mac-a", after=after)
        assert code == 0 and stderr == ""
    changed = {name for name, value in stage.tree().items() if before.get(name) != value}
    assert changed == {"bridge/placement-readings.json"}
    assert set(stage.tree()) == set(before) | changed  # and no temporary file is left
    assert stat.S_IMODE(stage.readings.stat().st_mode) == 0o600
    # Every command it ran is the probe of `answerback capacity`: nothing starts, stops or sends.
    assert len(stage.calls) == 4
    for argv in stage.calls:
        assert argv in (
            ["/bin/sh", "-c", PROBE_SCRIPT],
            ["hc", "exec", "--on", "mac-a", "--timeout", "10", "sh", "-c", PROBE_SCRIPT],
        )
        assert not {"run", "send", "interrupt", "kill", "wait", "rm"} & set(argv[:-1])


def test_no_force_flag_exists(stage: Stage) -> None:
    for arguments in (
        ["--apply"],
        ["--apply", "R17"],
        ["--force"],
        ["-f"],
        ["--yes"],
        ["--continue", "M1"],
        ["--to", "mac-a"],
        ["--timeout", "5"],
    ):
        code, stdout, stderr = stage.invoke(*arguments)
        assert (code, stdout) == (2, ""), arguments
        assert "unrecognized arguments" in stderr
    assert not stage.calls and not stage.readings.exists()
    commands = next(
        action for action in cli.parser()._actions if getattr(action, "choices", None)
    ).choices
    options = {text for action in commands["rebalance"]._actions for text in action.option_strings}
    assert options == {"-h", "--help", "--config", "--machine", "--json"}
    assert "move" not in commands and "moves" not in commands
    code, stdout, _ = stage.invoke("--help")
    assert code == 0 and "--apply" not in stdout and "--force" not in stdout


def test_first_run_has_one_reading_and_says_so(stage: Stage) -> None:
    stage.november()
    code, stdout, stderr = stage.invoke("--machine", "mac-a")
    assert code == 0 and stderr == ""
    assert "NOTHING_MOVES: one reading only." in stdout
    assert "A plan needs two readings of a machine, 60 s to 600 s apart." in stdout
    assert "local: first reading." in stdout and "mac-a: first reading." in stdout
    assert "plan: none" in stdout and "changed: nothing" in stdout
    assert {name: len(rows) for name, rows in stage.stored().items()} == {LOCAL: 1, "mac-a": 1}


def test_readings_too_close_or_too_far_apart_count_as_one(stage: Stage) -> None:
    stage.november()
    stage.invoke("--machine", "mac-a")
    code, stdout, _ = stage.invoke("--machine", "mac-a", after=20.0)
    assert code == 0
    assert "NOTHING_MOVES: one reading only." in stdout
    assert "earlier reading 20 s before: too close" in stdout
    code, stdout, _ = stage.invoke("--machine", "mac-a", after=700.0)
    assert code == 0
    assert "NOTHING_MOVES: one reading only." in stdout
    assert "earlier reading 700 s before: too far apart" in stdout
    # The last two readings of each machine are kept, and no more.
    times = [row["taken_at"] for row in stage.stored()["mac-a"]]
    assert times == [START + 20, START + 720]
    code, stdout, _ = stage.invoke("--machine", "mac-a", after=120.0)
    assert code == 0 and "NOTHING_MOVES: no worker load on local." in stdout


def test_22_november_output(stage: Stage) -> None:
    stage.november()
    stage.invoke("--machine", "mac-a")
    code, stdout, stderr = stage.invoke("--machine", "mac-a", after=120.0)
    assert code == 0 and stderr == ""
    lines = stdout.splitlines()
    assert lines[0].split()[:2] == [LOCAL, "full"]
    assert "load/core 11.31 (was full)" in lines[0] and "memory ok" in lines[0]
    assert "workers 0" in lines[0] and "harness processes 5" in lines[0]
    assert lines[0].endswith("not a taker: the coordinator takes no work")
    assert lines[1].split()[:2] == ["mac-a", "ok"]
    assert "load/core 0.18 (was ok)" in lines[1] and "workers 0/10" in lines[1]
    assert lines[1].endswith("taker")
    assert lines[2] == ""
    assert lines[3:] == [
        "NOTHING_MOVES: no worker load on local.",
        "  local is full, but holds no managed worker.",
        "  Its load comes from processes this command may not move.",
        "  New work would fit on mac-a.",
        f"plan: none        changed: nothing (readings kept in {stage.readings})",
    ]


def test_json(stage: Stage) -> None:
    stage.november()
    stage.invoke("--machine", "mac-a", "--json")
    code, stdout, stderr = stage.invoke("--machine", "mac-a", "--json", after=120.0)
    assert code == 0 and stderr == ""
    data = json.loads(stdout)
    assert set(data) == {"taken_at", "coordinator", "machines", "plan", "changed", "readings_file"}
    assert (data["taken_at"], data["coordinator"]) == (START + 120, LOCAL)
    assert data["changed"] == "nothing" and data["readings_file"] == str(stage.readings)
    assert data["plan"] == {
        "status": "NOTHING_MOVES",
        "id": None,
        "cause": "no worker load on local",
        "notes": [
            "local is full, but holds no managed worker.",
            "Its load comes from processes this command may not move.",
            "New work would fit on mac-a.",
        ],
        "move": None,
    }
    local, mac_a = data["machines"]
    assert (local["machine"], local["state"], local["was"], local["role"]) == (
        LOCAL,
        "full",
        "full",
        "",
    )
    assert (mac_a["machine"], mac_a["state"], mac_a["role"], mac_a["ceiling"]) == (
        "mac-a",
        "ok",
        "taker",
        10,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--machine", "bad name!"],
        ["--machine"],
        ["--bogus"],
        ["extra"],
        [item for n in range(16) for item in ("--machine", f"m{n}")],
    ],
    ids=["machine-name", "machine-missing", "unknown-option", "positional", "seventeen-machines"],
)
def test_wrong_argument_exits_2(stage: Stage, arguments: list[str]) -> None:
    code, stdout, stderr = stage.invoke(*arguments)
    assert (code, stdout) == (2, "") and stderr
    assert not stage.calls and not stage.readings.exists()


@pytest.mark.parametrize(
    "content",
    ["{", "[]", '{"version": 2, "machines": {}}', '{"version": 1, "machines": {"local": [{}]}}'],
)
def test_a_readings_file_it_cannot_read_is_an_error_and_is_left_alone(
    stage: Stage, content: str
) -> None:
    stage.readings.write_text(content)
    code, stdout, stderr = stage.invoke()
    assert (code, stdout) == (1, "")
    assert "readings file" in json.loads(stderr)["error"]
    assert stage.readings.read_text() == content and not stage.calls


def test_a_registered_worker_counts_and_stays_in_this_row(stage: Stage) -> None:
    stage.november()
    row = {"name": "w1", "class": "port-fix", "harness": "claude", "created_at": START - 900}
    (stage.home / "routing-workers.json").write_text(
        json.dumps({"version": 1, "workers": {"t04": row}, "gates": {}})
    )
    stage.invoke("--machine", "mac-a")
    code, stdout, _ = stage.invoke("--machine", "mac-a", after=120.0)
    assert code == 0
    assert "workers 1" in stdout.splitlines()[0]
    assert "NOTHING_MOVES: no worker may move: every managed worker on local stays." in stdout
    assert "  t04: its receipt is unknown." in stdout
    assert "plan: none" in stdout


def test_a_machine_that_cannot_be_measured_is_shown_and_takes_nothing(stage: Stage) -> None:
    stage.november()
    stage.outputs["mac-a"] = "garbage"
    stage.invoke("--machine", "mac-a")
    code, stdout, _ = stage.invoke("--machine", "mac-a", after=120.0)
    assert code == 0
    line = stdout.splitlines()[1]
    assert line.split()[:2] == ["mac-a", "unknown"] and line.endswith("not a taker: not measured")
    assert "load/core ?" in line
    assert "  No other machine has room; new work waits." in stdout


def test_a_kept_reading_with_an_integer_too_large_for_a_float_is_not_measured(stage: Stage) -> None:
    stage.november()
    stage.invoke("--machine", "mac-a")
    kept = json.loads(stage.readings.read_text())
    kept["machines"]["mac-a"][-1]["process_count"] = 10**400
    stage.readings.write_text(json.dumps(kept))
    code, stdout, stderr = stage.invoke("--machine", "mac-a", after=120.0)
    assert code == 0 and stderr == ""
    line = stdout.splitlines()[1]
    assert line.split()[:2] == ["mac-a", "ok"]
    assert "(a reading is not a measurement)" in line
    assert line.endswith("not a taker: a reading is not a measurement")
    assert "NOTHING_MOVES: no worker load on local." in stdout and "plan: none" in stdout


FLOAT_FIELDS = ("taken_at", "load1", "load5", "load15", "psi_some_avg10")
NO_FINITE_NUMBER = {
    "too-large": 10**400,
    "too-large-negative": -(10**400),
    "nan": float("nan"),
    "infinity": float("inf"),
    "negative-infinity": float("-inf"),
    "true": True,
    "false": False,
    "text": "1.5",
}
FINITE = {
    "zero": 0,
    "integer": 3,
    "largest-power": 1e308,
    "negative-integer": -1,
    "negative-float": -0.5,
}


def refused_in_the_newest_kept_reading(stage: Stage, name: str, value: object) -> None:
    """Put `value` in one field of the newest kept reading of mac-a; the next run refuses the file.

    Exit code 1, nothing on standard output, the message that names the file on standard
    error as JSON, the file byte for byte as it was, and no probe call in that run.
    """
    stage.november()
    stage.invoke("--machine", "mac-a")
    kept = json.loads(stage.readings.read_text())
    kept["machines"]["mac-a"][-1][name] = value
    stage.readings.write_text(json.dumps(kept))
    before, calls = stage.readings.read_bytes(), len(stage.calls)
    assert calls == 2
    code, stdout, stderr = stage.invoke("--machine", "mac-a", after=120.0)
    assert (code, stdout) == (1, "")
    assert json.loads(stderr) == {
        "error": f"The readings file cannot be read: {stage.readings}. "
        "Remove it to start with a first reading."
    }
    assert stage.readings.read_bytes() == before
    assert len(stage.calls) == calls


@pytest.mark.parametrize("name", FLOAT_FIELDS)
@pytest.mark.parametrize("value", list(NO_FINITE_NUMBER.values()), ids=list(NO_FINITE_NUMBER))
def test_a_kept_reading_with_no_finite_number_in_a_float_field_is_a_file_it_cannot_read(
    stage: Stage, name: str, value: object
) -> None:
    """Each float field of a kept reading refuses a value that is not a finite number.

    An integer too large for a float, its negative, not-a-number, both infinities, true,
    false and a text: the command answers each as a readings file it cannot read, with
    its message and never with a traceback. It leaves the file alone and measures nothing.
    """
    refused_in_the_newest_kept_reading(stage, name, value)


def test_a_reading_taken_with_a_clock_before_1970_is_read_by_the_next_run(stage: Stage) -> None:
    """The command reads what it wrote itself, also when its clock was set before 1970.

    The first run has a negative clock and keeps a reading with that negative time. The
    file is not touched. The next run, with the clock right again, reads it, measures
    and exits 0.
    """
    stage.november()
    stage.clock = -120.0
    code, _, stderr = stage.invoke("--machine", "mac-a")
    assert code == 0 and stderr == ""
    assert [row["taken_at"] for row in stage.stored()["mac-a"]] == [-120.0]
    stage.clock = START
    code, stdout, stderr = stage.invoke("--machine", "mac-a")
    assert code == 0 and stderr == ""
    assert "plan: none" in stdout
    assert len(stage.calls) == 4
    assert [row["taken_at"] for row in stage.stored()["mac-a"]] == [-120.0, START]


def test_a_kept_reading_without_its_time_is_a_file_it_cannot_read(stage: Stage) -> None:
    """The time of a kept reading is the one float field that may not be absent."""
    refused_in_the_newest_kept_reading(stage, "taken_at", None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        *((name, value) for name in FLOAT_FIELDS for value in FINITE.values()),
        *((name, None) for name in FLOAT_FIELDS[1:]),
    ],
    ids=[
        *(f"{name}-{label}" for name in FLOAT_FIELDS for label in FINITE),
        *(f"{name}-absent" for name in FLOAT_FIELDS[1:]),
    ],
)
def test_a_kept_reading_with_a_finite_number_in_a_float_field_is_read(
    stage: Stage, name: str, value: object
) -> None:
    """The rule is no wider than it says: a finite number is read, and kept as it stands.

    Zero, an integer, the float 1e308, a negative integer, a negative float, and an absent
    value in the four fields that may be absent: the next run exits 0 and writes the kept
    reading back with the value it had. An integer stays an integer.
    """
    stage.november()
    stage.invoke("--machine", "mac-a")
    kept = json.loads(stage.readings.read_text())
    kept["machines"]["mac-a"][-1][name] = value
    stage.readings.write_text(json.dumps(kept))
    code, stdout, stderr = stage.invoke("--machine", "mac-a", after=120.0)
    assert code == 0 and stderr == ""
    assert "plan: none" in stdout
    assert len(stage.calls) == 4
    earlier, newest = stage.stored()["mac-a"]
    assert earlier[name] == value and type(earlier[name]) is type(value)
    assert newest["taken_at"] == START + 120


def test_a_registered_worker_created_at_an_integer_too_large_for_a_float_counts(
    stage: Stage,
) -> None:
    stage.november()
    row = {"name": "w1", "class": "port-fix", "harness": "claude", "created_at": 10**400}
    (stage.home / "routing-workers.json").write_text(
        json.dumps({"version": 1, "workers": {"t04": row}, "gates": {}})
    )
    stage.invoke("--machine", "mac-a")
    code, stdout, stderr = stage.invoke("--machine", "mac-a", after=120.0)
    assert code == 0 and stderr == ""
    assert "workers 1" in stdout.splitlines()[0]
    assert "  t04: its receipt is unknown." in stdout
    assert "plan: none" in stdout
    workers = rebalance_cli.managed_workers(stage.home / "config.json")
    assert [worker.since for worker in workers] == [0.0]


def test_text_of_a_plan_names_the_move_and_offers_no_apply(tmp_path: Path) -> None:
    def reading(machine: str, load: float, at: float) -> Reading:
        return probe.parse_reading(machine, output(load), at)

    readings = {LOCAL: reading(LOCAL, 181, START - 5), "mac-a": reading("mac-a", 2.88, START - 5)}
    previous = {
        LOCAL: reading(LOCAL, 181, START - 125),
        "mac-a": reading("mac-a", 2.88, START - 125),
    }
    ready = {"claude": HarnessFacts(True)}
    facts = Facts(
        machines={"mac-a": MachineFacts("mac-a", "/work/p", max_total=10, harnesses=ready)}
    )
    worker = ManagedWorker(
        "t04",
        LOCAL,
        "claude",
        "port-fix",
        None,
        "idle",
        "completed",
        False,
        False,
        False,
        1.0,
        "T81",
    )
    plan = plan_rebalance(START, readings, previous, facts, [worker], ["mac-a"], [])
    text = rebalance_cli.text(plan, tmp_path / "placement-readings.json")
    assert text.splitlines()[3:] == [
        f"plan {plan.id}: 1 move",
        "  T81  idle with context  claude  local/t04 -> mac-a",
        "  why: local full twice; mac-a ok twice; gap 11.07; projected 0.24 < 0.40",
        "  how: hand-over brief, then a new worker on mac-a. The conversation is not carried.",
        f"changed: nothing (readings kept in {tmp_path / 'placement-readings.json'})."
        "   apply: not built yet; this plan is a preview.",
    ]
    assert "--apply" not in text
