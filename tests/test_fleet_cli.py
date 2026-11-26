"""fleet: ``answerback fleet serve`` and ``show`` through the real command line."""

import argparse
import dataclasses
import errno
import json
import os
import signal
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_fleet_deadline import deadline
from test_fleet_fakes import Fleet, Service, linux
from test_fleet_lifetime import Child, lock_state, no_descriptor_left, process_unchanged

from answerback_bridge import cli
from answerback_bridge.config import Config
from answerback_bridge.fleet import cli as fleet_cli
from answerback_bridge.fleet import document, publish


@pytest.fixture(autouse=True)
def _no_descriptor_left(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with no_descriptor_left(), process_unchanged():
        yield
        monkeypatch.undo()  # what the test changed through it is put back before the checks


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Any:
    fleet = Fleet({"local": linux(), "worker-a": linux()})
    service = Service()
    monkeypatch.setattr(fleet_cli, "make_runner", lambda binary, session: (fleet, binary))
    monkeypatch.setattr(fleet_cli, "make_call", lambda path: service)
    monkeypatch.setattr(cli, "default_config_path", lambda: tmp_path / "absent" / "config.json")

    def invoke(arguments: list[str]) -> tuple[int, str, str]:
        monkeypatch.setattr(sys, "argv", ["answerback", "fleet", *arguments])
        code: Any = 0
        try:
            cli.main()
        except SystemExit as error:
            code = error.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    invoke.fleet = fleet  # type: ignore[attr-defined]
    return invoke


def test_fleet_serve_once_publishes_and_exits_0(run: Any, tmp_path: Path) -> None:
    folder = tmp_path / "out" / "fleet"
    code, _, err = run(["serve", "--once", "--dir", str(folder), "--ceiling", "worker-a=4"])
    assert code == 0, err
    assert os.listdir(folder) == ["fleet.json"]
    doc = json.loads((folder / "fleet.json").read_text(encoding="utf-8"))
    assert doc["machines"][1]["name"] == "worker-a"
    assert doc["machines"][1]["reading"]["agents"] == {"active": 0, "ceiling": 4, "lanes": 0}
    assert doc["health"]["service"]["state"] == "unreachable"


def test_fleet_serve_default_folder_is_beside_the_runtime_folder(run: Any, tmp_path: Path) -> None:
    code, _, err = run(["serve", "--once"])
    assert code == 0, err
    assert (tmp_path / "absent" / "fleet" / "fleet.json").is_file()


def test_a_second_writer_for_the_same_folder_exits_3(run: Any, tmp_path: Path) -> None:
    folder = tmp_path / "fleet"
    first = publish.Publisher(folder)
    first.open()
    try:
        with deadline("serve --once beside a writer that holds the lock"):
            code, out, err = run(["serve", "--once", "--dir", str(folder)])
    finally:
        first.close()
    assert code == 3
    lines = (out + err).strip().splitlines()
    assert len(lines) == 1 and "another fleet writer" in lines[0]


def test_a_failed_close_in_the_cleanup_of_a_start_beside_a_writer_still_exits_3(
    run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "fleet"
    first = publish.Publisher(folder)
    first.open()
    lock = os.stat(tmp_path / "fleet.stage" / "writer.lock")
    real = os.close
    failed: list[int] = []

    def closes_then_raises(fd: int) -> None:
        """Closes the second start's lock for real, once, then raises."""
        info = os.fstat(fd)
        mine = fd != first._lock_fd and (info.st_dev, info.st_ino) == (lock.st_dev, lock.st_ino)
        real(fd)
        if mine and not failed:
            failed.append(fd)
            raise OSError(errno.EIO, "injected error at the close of the lock")

    try:
        with monkeypatch.context() as m:
            m.setattr(publish.os, "close", closes_then_raises)
            with deadline("serve --once beside a writer, with a close that fails"):
                code, out, err = run(["serve", "--once", "--dir", str(folder)])
    finally:
        first.close()
    assert failed, "the close of the second start's lock was not asked for"
    assert code == 3
    assert (out + err).strip().splitlines() == [
        f"another fleet writer already publishes to {folder}"
    ]


def test_an_error_of_flock_other_than_would_block_exits_1_not_3(
    run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "injected: no locks available")

    monkeypatch.setattr(publish.fcntl, "flock", no_locks)
    code, out, err = run(["serve", "--once", "--dir", str(tmp_path / "fleet")])
    assert code == 1
    assert (out + err).strip().splitlines() == [
        f"fleet writer refused to start: [Errno {errno.ENOLCK}] injected: no locks available"
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["serve", "--machine", "bad name"],
        ["serve", "--machine", "local"],
        ["serve", "--ceiling", "worker-a=0"],
        ["serve", "--ceiling", "worker-a=100"],
        ["serve", "--ceiling", "worker-a"],
        ["serve", *sum((["--machine", f"w{n}"] for n in range(16)), [])],
        ["serve", "--lane-label", "ab.lane"],
        ["serve", "--lane-label", "--all=1"],
        ["serve", "--lane-label", "k=1 --all"],
        ["serve", "--lane-label", "k=" + "v" * 65],
    ],
)
def test_bad_options_are_refused(run: Any, arguments: list[str]) -> None:
    code, _, _ = run(arguments)
    assert code == 2


def test_the_lane_label_option_reaches_hc(run: Any, tmp_path: Path) -> None:
    folder = tmp_path / "fleet"
    code, _, err = run(["serve", "--once", "--dir", str(folder), "--lane-label", "my.lane=on"])
    assert code == 0, err
    assert ["hc", "ls", "--json", "--label", "my.lane=on"] in run.fleet.calls
    code, _, err = run(["serve", "--once", "--dir", str(folder)])
    assert ["hc", "ls", "--json", "--label", "ab.lane=1"] in run.fleet.calls


def _serve_option_names() -> set[str]:
    """The names under which the options of ``fleet serve`` are stored, from its parser."""
    top = argparse.ArgumentParser()
    fleet_cli.add_parser(top.add_subparsers())
    fleet = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))
    actions = next(
        a for a in fleet.choices["fleet"]._actions if isinstance(a, argparse._SubParsersAction)
    )
    serve = actions.choices["serve"]
    return {a.dest for a in serve._actions if a.option_strings and a.dest != "help"}


def _fleet_fields(names: Iterable[str]) -> list[str]:
    options = _serve_option_names()
    return sorted(n for n in names if "fleet" in n or n in options)


def test_no_field_of_the_shared_configuration_belongs_to_the_fleet_writer() -> None:
    # Every setting of the writer is an option of its command; an older bridge refuses a key
    # of the configuration file that it does not know.
    assert {"dir", "machine", "ceiling", "lane_label", "once"} <= _serve_option_names()
    assert _fleet_fields(f.name for f in dataclasses.fields(Config)) == []


def test_fleet_show_prints_age_and_one_line_per_machine(run: Any, tmp_path: Path) -> None:
    folder = tmp_path / "fleet"
    assert run(["serve", "--once", "--dir", str(folder)])[0] == 0
    code, out, _ = run(["show", "--dir", str(folder)])
    lines = out.strip().splitlines()
    assert code == 0 and "old" in lines[0]
    assert lines[1].startswith("coordinator") and lines[2].startswith("worker-a")
    code, out, _ = run(["show", "--dir", str(folder), "--json"])
    assert code == 0 and json.loads(out)["schema"] == "answerback.fleet/1"


def test_fleet_show_without_a_document_exits_1(run: Any, tmp_path: Path) -> None:
    code, out, err = run(["show", "--dir", str(tmp_path / "none")])
    assert code == 1 and "no fleet document" in (out + err)


def test_serve_ends_with_0_on_sigterm_and_leaves_only_fleet_json(tmp_path: Path) -> None:
    folder = tmp_path / "fleet"
    config = tmp_path / "cfg" / "config.json"
    hc = tmp_path / "hc"
    hc.write_text('#!/bin/sh\necho \'{"machines": [], "terminals": []}\'\n', encoding="utf-8")
    hc.chmod(0o700)
    config.parent.mkdir()
    config.write_text(json.dumps({"hc_binary": str(hc)}), encoding="utf-8")
    command = [sys.executable, "-m", "answerback_bridge", "--config", str(config)]
    with Child([*command, "fleet", "serve", "--dir", str(folder)], tmp_path) as child:
        deadline = time.monotonic() + 20
        while not (folder / "fleet.json").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert (folder / "fleet.json").exists()
        child.send_signal(signal.SIGTERM)
        code = child.wait(20)
    assert code == 0, child.stderr
    assert os.listdir(folder) == ["fleet.json"]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_serve_once_exits_0_and_leaves_no_child(
    tmp_path: Path, signum: int
) -> None:
    folder = tmp_path / "fleet"
    config = tmp_path / "cfg" / "config.json"
    pids = tmp_path / "pids"
    hc = tmp_path / "hc"
    hc.write_text(f'#!/bin/sh\necho $$ >> "{pids}"\nexec sleep 30\n', encoding="utf-8")
    hc.chmod(0o700)
    config.parent.mkdir()
    config.write_text(json.dumps({"hc_binary": str(hc)}), encoding="utf-8")
    command = [sys.executable, "-m", "answerback_bridge", "--config", str(config)]
    with Child([*command, "fleet", "serve", "--once", "--dir", str(folder)], tmp_path) as child:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if pids.exists() and len(pids.read_text(encoding="utf-8").split()) >= 2:
                break
            time.sleep(0.05)
        children = [int(p) for p in pids.read_text(encoding="utf-8").split()]
        assert len(children) == 2
        child.send_signal(signum)
        code = child.wait(20)
    assert code == 0, child.stderr
    for pid in children:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not (folder / "fleet.json").exists()


def test_serve_refuses_a_folder_that_holds_another_file_with_exit_1(
    run: Any, tmp_path: Path
) -> None:
    folder = tmp_path / "fleet"
    folder.mkdir()
    (folder / "keep.txt").write_text("keep", encoding="utf-8")
    code, out, err = run(["serve", "--once", "--dir", str(folder)])
    assert code == 1
    assert len((out + err).strip().splitlines()) == 1
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_serve_once_exits_1_when_the_document_cannot_be_built(
    run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from answerback_bridge.fleet import document

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise KeyError("surprise")

    folder = tmp_path / "fleet"
    assert run(["serve", "--once", "--dir", str(folder)])[0] == 0
    before = (folder / "fleet.json").read_bytes()
    monkeypatch.setattr(document, "build", broken)
    code, _, _ = run(["serve", "--once", "--dir", str(folder)])
    assert code == 1
    assert (folder / "fleet.json").read_bytes() == before


# ---------------------------------------------------------------- the two signals at the end


def _one(signum: int, frame: Any) -> None:
    """A handler of the test."""


def _other(signum: int, frame: Any) -> None:
    """Another handler of the test."""


_BOTH = (signal.SIGINT, signal.SIGTERM)


def _handlers() -> tuple[Any, Any]:
    return signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)


def _noting(monkeypatch: pytest.MonkeyPatch, *names: str) -> dict[str, list[Any]]:
    """Wrap the publisher's methods so that each notes both handlers, then makes the real call."""
    seen: dict[str, list[Any]] = {name: [] for name in names}
    real_getsignal = signal.getsignal
    for name in names:
        real = getattr(publish.Publisher, name)

        def wrapper(self: Any, *args: Any, _real: Any = real, _name: str = name) -> Any:
            seen[_name].append((real_getsignal(signal.SIGINT), real_getsignal(signal.SIGTERM)))
            result = _real(self, *args)
            seen[_name].append("returned")
            return result

        monkeypatch.setattr(publish.Publisher, name, wrapper)
    return seen


@pytest.mark.parametrize(
    ("sigint", "sigterm"),
    [
        ("as found", "as found"),
        (signal.SIG_IGN, signal.SIG_IGN),
        (_one, _other),
        (signal.SIG_IGN, _one),
        (_one, signal.SIG_IGN),
    ],
    ids=[
        "as-found",
        "ignored-ignored",
        "function-function",
        "ignored-function",
        "function-ignored",
    ],
)
@pytest.mark.parametrize("ending", ["once", "own-signal"])
def test_the_command_gives_each_signal_back_the_handling_it_found(
    run: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sigint: Any,
    sigterm: Any,
    ending: str,
) -> None:
    # Without ``--once`` the writer runs until its own handler of SIGTERM sets its stop: the
    # signal is sent to the test process after the first publication. In this process the
    # handlers can be compared by identity, which a child process cannot show.
    earlier = _handlers()
    try:
        for signum, handler in zip(_BOTH, (sigint, sigterm), strict=True):
            if handler != "as found":
                signal.signal(signum, handler)
        start = _handlers()
        with monkeypatch.context() as m:
            seen = _noting(m, "open", "close")
            arguments = ["serve", "--dir", str(tmp_path / "fleet")]
            if ending == "once":
                arguments.append("--once")
            else:
                real_publish = publish.Publisher.publish

                def publish_then_signal(self: Any, data: bytes) -> None:
                    real_publish(self, data)
                    # Sent only to a handler of the writer: the default or ignoring, or the
                    # handler of the start, would end or miss the signal of this process.
                    handler = signal.getsignal(signal.SIGTERM)
                    if handler in (signal.SIG_DFL, signal.SIG_IGN) or handler is start[1]:
                        raise AssertionError(
                            f"the writer set no handler of SIGTERM while it ran: {handler!r};"
                            " no signal was sent"
                        )
                    os.kill(os.getpid(), signal.SIGTERM)

                m.setattr(publish.Publisher, "publish", publish_then_signal)
            with deadline(f"serve, ended by {ending}"):
                code, _, err = run(arguments)
        after = _handlers()
    finally:
        for signum, handler in zip(_BOTH, earlier, strict=True):
            signal.signal(signum, handler)
    assert code == 0, err
    at_open, at_close = seen["open"][0], seen["close"][0]
    assert seen["close"][1] == "returned"
    assert at_close[0] is at_open[0] and at_close[1] is at_open[1]
    assert after[0] is start[0] and after[1] is start[1]


@pytest.mark.parametrize("unreadable", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_a_handler_that_cannot_be_read_is_left_to_the_default_and_the_end_still_works(
    run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: int
) -> None:
    folder = tmp_path / "fleet"
    readable = signal.SIGINT if unreadable == signal.SIGTERM else signal.SIGTERM
    index = {signal.SIGINT: 0, signal.SIGTERM: 1}
    earlier = _handlers()
    real_getsignal = signal.getsignal

    def getsignal(signum: int) -> Any:
        return None if signum == unreadable else real_getsignal(signum)

    try:
        signal.signal(signal.SIGINT, _one)
        signal.signal(signal.SIGTERM, _other)
        own = {signal.SIGINT: _one, signal.SIGTERM: _other}
        with monkeypatch.context() as m:
            seen = _noting(m, "publish", "close")
            m.setattr(signal, "getsignal", getsignal)
            code, _, err = run(["serve", "--once", "--dir", str(folder)])
        after = _handlers()
    finally:
        for signum, handler in zip(_BOTH, earlier, strict=True):
            signal.signal(signum, handler)
    assert code == 0, err
    assert seen["close"][1] == "returned"
    assert lock_state(tmp_path / "fleet.stage") == "free"
    at_publish, at_close = seen["publish"][0], seen["close"][0]
    assert at_close[index[readable]] is own[readable]
    assert after[index[readable]] is own[readable]
    # The writer's own handling of the unreadable signal is gone at the last step.
    assert at_close[index[unreadable]] is not at_publish[index[unreadable]]
    # What asyncio's removal of the writer's handler leaves, at the last step and after it.
    default = signal.default_int_handler if unreadable == signal.SIGINT else signal.SIG_DFL
    assert at_close[index[unreadable]] is default
    assert after[index[unreadable]] is default


# A child process: sets the start of both signals, wraps the publisher, then runs the command.
CHILD = """
import json, os, signal, sys, threading, time
from pathlib import Path
from answerback_bridge.fleet import publish

case = json.loads(sys.argv[1])
starts = {"usual-int": signal.default_int_handler, "usual": signal.SIG_DFL,
          "ignored": signal.SIG_IGN}
signal.signal(signal.SIGINT, starts["usual-int" if case["sigint"] == "usual" else "ignored"])
signal.signal(signal.SIGTERM, starts[case["sigterm"]])
real_close = publish.Publisher.close

def spin():
    for _ in range(100000):  # lets the interpreter run a handler written in Python
        pass

def note(name):
    Path(case["notes"], name).write_text(repr(time.monotonic()), encoding="utf-8")

def wait_in_a_read(then):
    # The last step waits in a read that only the helper thread can end.
    r, w = os.pipe()
    reading = threading.Event()
    def helper():
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        reading.wait(30)  # the main thread is at its read
        time.sleep(0.5)
        note("first-sent")
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.5)
        note("second-step")
        if then == "byte":
            os.write(w, b"x")
        else:
            os.kill(os.getpid(), getattr(signal, then))
    threading.Thread(target=helper, daemon=True).start()
    note("reading")
    reading.set()
    os.read(r, 1)
    note("read-returned")
    os.close(r)
    os.close(w)

def close(self):
    if case.get("together"):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGINT)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
        spin()
    for name in case["send"]:
        os.kill(os.getpid(), getattr(signal, name))
        spin()
    if case.get("wait"):
        wait_in_a_read(case["wait"])
    real_close(self)
    Path(case["marker"]).write_text("returned", encoding="utf-8")

publish.Publisher.close = close
if case.get("fail_publish"):
    import errno
    from answerback_bridge.process import BridgeError
    kinds = {
        "ValueError": lambda: ValueError("a fault in the writer's own code"),
        "OSError": lambda: OSError(errno.EIO, "a fault in the writer's own code"),
        "BridgeError": lambda: BridgeError("a fault in the writer's own code"),
        "RuntimeError": lambda: RuntimeError("a fault in the writer's own code"),
        "UnicodeDecodeError": lambda: UnicodeDecodeError("utf-8", bytes([255]), 0, 1, "a fault"),
        "KeyError": lambda: KeyError("a fault in the writer's own code"),
    }
    real_publish = publish.Publisher.publish
    def failing(self, data):
        if case.get("after"):
            real_publish(self, data)  # the file is renamed into place, then the error
        raise kinds[case["fail_publish"]]()
    publish.Publisher.publish = failing
if case.get("noise"):
    sys.stderr.write("x" * case["noise"])  # before the first publication
    sys.stderr.flush()
sys.argv = ["answerback", "--config", case["config"], "fleet", "serve", *case["args"]]
from answerback_bridge.cli import main
main()
"""


def _child(tmp_path: Path, **case: Any) -> Child:
    config = tmp_path / "cfg" / "config.json"
    hc = tmp_path / "hc"
    hc.write_text('#!/bin/sh\necho \'{"machines": [], "terminals": []}\'\n', encoding="utf-8")
    hc.chmod(0o700)
    config.parent.mkdir()
    config.write_text(json.dumps({"hc_binary": str(hc)}), encoding="utf-8")
    case = {
        "sigint": "usual",
        "sigterm": "usual",
        "send": [],
        "marker": str(tmp_path / "closed"),
        "notes": str(tmp_path),
        "config": str(config),
        "args": ["--once", "--dir", str(tmp_path / "fleet")],
        **case,
    }
    return Child([sys.executable, "-c", CHILD, json.dumps(case)], tmp_path)


def _finish(child: Child) -> tuple[int, str, str]:
    """The exit code, standard output and standard error of a child, after its end."""
    with child:
        code = child.wait(30)
    return code, child.stdout, child.stderr


@pytest.mark.parametrize(
    ("sigint", "sigterm", "send", "code", "returned"),
    [
        ("usual", "usual", ["SIGINT"], 0, True),
        ("usual", "usual", ["SIGINT", "SIGINT"], 130, False),
        ("usual", "usual", ["SIGTERM"], -signal.SIGTERM, False),
        ("ignored", "usual", ["SIGINT"], 0, True),
        ("ignored", "usual", ["SIGINT", "SIGINT"], 0, True),
        ("usual", "ignored", ["SIGTERM"], 0, True),
        ("usual", "usual", "together", 0, True),
    ],
    ids=[
        "sigint-once",
        "sigint-twice",
        "sigterm-once",
        "ignored-sigint-once",
        "ignored-sigint-twice",
        "ignored-sigterm-once",
        "sigint-twice-together",
    ],
)
def test_a_signal_while_the_writer_releases_its_lock_and_folders(
    tmp_path: Path, sigint: str, sigterm: str, send: list[str] | str, code: int, returned: bool
) -> None:
    # "together": two SIGINT sent while SIGINT is blocked are pending at once, then unblocked.
    how = {"together": True, "send": []} if send == "together" else {"send": send}
    exit_code, _, err = _finish(_child(tmp_path, sigint=sigint, sigterm=sigterm, **how))
    assert exit_code == code, err
    assert err == ""
    assert (tmp_path / "closed").exists() is returned
    assert (tmp_path / "fleet" / "fleet.json").is_file()
    assert lock_state(tmp_path / "fleet.stage") == "free"


@pytest.mark.parametrize("name", ["SIGINT", "SIGTERM"])
def test_a_writer_started_with_a_signal_ignored_still_ends_its_cycles_on_it(
    tmp_path: Path, name: str
) -> None:
    folder = tmp_path / "fleet"
    ignored = {"sigint": "ignored"} if name == "SIGINT" else {"sigterm": "ignored"}
    with _child(tmp_path, args=["--dir", str(folder)], **ignored) as child:
        until = time.monotonic() + 20
        while not (folder / "fleet.json").exists() and time.monotonic() < until:
            time.sleep(0.1)
        assert (folder / "fleet.json").exists()
        child.send_signal(getattr(signal, name))
        code, _, err = _finish(child)
    assert code == 0, err
    assert os.listdir(folder) == ["fleet.json"]


@pytest.mark.parametrize("after", [False, True], ids=["before", "after"])
@pytest.mark.parametrize(
    "kind",
    ["ValueError", "OSError", "BridgeError", "RuntimeError", "UnicodeDecodeError", "KeyError"],
)
def test_an_error_of_the_publisher_that_is_no_failed_publication_ends_the_command(
    tmp_path: Path, kind: str, after: bool
) -> None:
    code, _, err = _finish(_child(tmp_path, fail_publish=kind, after=after))
    assert code == 1
    assert (tmp_path / "closed").exists()
    assert lock_state(tmp_path / "fleet.stage") == "free"
    published = tmp_path / "fleet" / "fleet.json"
    if after:
        assert document.validate(published.read_bytes()) == []
    else:
        assert not published.exists()
    last = err.strip().splitlines()[-1]
    if kind in ("RuntimeError", "KeyError"):
        # A class the command's outer handler does not name.
        assert "Traceback" in err
        assert last.startswith(kind)
    elif kind == "UnicodeDecodeError":
        # A subclass of ValueError: one line, as for a refusal.
        assert "Traceback" not in err
        line = json.loads(last)
        assert isinstance(line, dict) and isinstance(line["error"], str) and line["error"]
    else:
        # The command's outer handler prints one line, as for a refusal.
        assert "Traceback" not in err
        text = (
            f"[Errno {errno.EIO}] a fault in the writer's own code"
            if kind == "OSError"
            else "a fault in the writer's own code"
        )
        assert json.loads(last) == {"error": text}


def test_a_child_that_writes_much_to_its_standard_error_is_not_held_up_by_it(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "fleet"
    with _child(tmp_path, args=["--dir", str(folder)], noise=300_000) as child:
        until = time.monotonic() + 20
        while not (folder / "fleet.json").exists() and time.monotonic() < until:
            time.sleep(0.1)
        assert (folder / "fleet.json").exists(), "the child did not publish within 20 s"
        child.send_signal(signal.SIGTERM)
        code, _, err = _finish(child)
    assert code == 0
    assert len(err) >= 300_000


def test_children_run_alike_whatever_settings_that_make_the_interpreter_print(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "PYTHONMALLOCSTATS",
        "PYTHONPROFILEIMPORTTIME",
        "PYTHONVERBOSE",
        "PYTHONWARNDEFAULTENCODING",
        "FORCE_COLOR",
    ):
        monkeypatch.setenv(name, "1")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    test_a_signal_while_the_writer_releases_its_lock_and_folders(
        tmp_path / "a", "usual", "usual", ["SIGINT"], 0, True
    )
    test_an_error_of_the_publisher_that_is_no_failed_publication_ends_the_command(
        tmp_path / "b", "RuntimeError", False
    )


@pytest.mark.parametrize(
    ("then", "code", "returned"),
    [("byte", 0, True), ("SIGINT", 130, False), ("SIGTERM", -signal.SIGTERM, False)],
    ids=["read-released", "second-sigint", "sigterm"],
)
def test_a_first_sigint_does_not_end_a_last_step_that_waits_in_a_call_of_the_system(
    tmp_path: Path, then: str, code: int, returned: bool
) -> None:
    exit_code, _, err = _finish(_child(tmp_path, wait=then))
    assert exit_code == code, err
    if returned:
        assert err == ""
    assert (tmp_path / "closed").exists() is returned
    assert (tmp_path / "read-returned").exists() is returned
    assert lock_state(tmp_path / "fleet.stage") == "free"
    assert (tmp_path / "fleet" / "fleet.json").is_file()
    # Half a second after the first SIGINT the child still waited in its read.
    began = float((tmp_path / "reading").read_text(encoding="utf-8"))
    first = float((tmp_path / "first-sent").read_text(encoding="utf-8"))
    second = float((tmp_path / "second-step").read_text(encoding="utf-8"))
    assert began <= first < second and second - first >= 0.4
    if returned:
        # The read ended only by the byte of the helper's second step.
        assert float((tmp_path / "read-returned").read_text(encoding="utf-8")) > second
