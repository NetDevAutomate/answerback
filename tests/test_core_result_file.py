"""Answer path for Core terminals on another machine (plan rows A0 to A2).

The fake `hc` below answers `get`, `send` and `file read` in the forms recorded in
`hc-file-samples.md` (Herdr core 0.1.21, captured 2026-11-20 against
node-two): standard output, standard error and exit codes included. Each
helper that builds an answer names the sample it copies.

Every test names its label on the first line of its docstring: `red_at_pretip`
(it fails at 51933f2, the start commit) or `pins_existing` (it passes there and
pins behaviour that had no test).
"""

import asyncio
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.config import Config
from answerback_bridge.errors import public_bridge_error
from answerback_bridge.process import module_launch_argv
from answerback_bridge.providers.herdr import core as core_module
from answerback_bridge.service import Bridge

MACHINE = "node-two"
REMOTE = f"{MACHINE}/t15"


def row(address: str = "t1", **changes: Any) -> dict[str, Any]:
    """One `hc get --json` row of a ready Claude terminal.

    The field set follows the "get: a live terminal on that machine" sample, with the
    foreground and status of an agent that reports `idle` (as `test_core_send_policy`).
    """
    return {
        "address": address,
        "argv": ["/usr/bin/zsh"],
        "pid": 1692186,
        "created_ms": 1795044121162,
        "cwd": "/home/user",
        "foreground": {
            "pid": 1692190,
            "name": "claude",
            "argv": ["claude"],
            "cwd": "/home/user/code/project",
        },
        "exit_code": None,
        "finished_ms": None,
        "reason": None,
        "state": "live",
        "status": {"app": "claude", "state": "idle"},
        "turn": 3,
        "labels": {"where": "tools / 1"},
        **changes,
    }


def finished(address: str = REMOTE) -> dict[str, Any]:
    """The "get: that terminal after its program has ended (exit code 7)" sample."""
    return row(
        address,
        exit_code=7,
        finished_ms=1795197676137,
        reason="exited",
        state="finished",
        status=None,
    )


@dataclass
class FakeCore:
    """A scripted `hc` executable that records every argv it receives."""

    binary: Path
    settings_path: Path
    log_path: Path

    def settings(self) -> dict[str, Any]:
        return json.loads(self.settings_path.read_text())

    def update(self, **changes: Any) -> None:
        self.settings_path.write_text(json.dumps(self.settings() | changes))

    def put_file(self, path: str, text: str, machine: str = MACHINE) -> None:
        files = self.settings()["files"]
        files[f"{machine}:{path}"] = text
        self.update(files=files)

    def calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def sends(self) -> list[list[str]]:
        return [args for args in self.calls() if args[0] == "send"]

    def reads(self) -> list[list[str]]:
        return [args for args in self.calls() if args[:2] == ["file", "read"]]

    def gets(self) -> list[list[str]]:
        return [args for args in self.calls() if args[0] == "get"]


FAKE = r"""#!{python}
import json
import sys
import time
from pathlib import Path

settings_path = Path({settings!r})
settings = json.loads(settings_path.read_text())
args = sys.argv[1:]
with Path({log!r}).open("a") as log:
    log.write(json.dumps(args) + "\n")
known = settings["machines"]


def fail(code, kind, message, hint):
    # Error form of every sample: nothing on stdout, one JSON object on stderr.
    error = {{"hint": hint, "kind": kind, "message": message}}
    print(json.dumps({{"error": error}}, separators=(",", ":")), file=sys.stderr)
    sys.exit(code)


def unknown(machine):
    # Sample "read: a machine name that hc does not know": exit 4, machine_unknown.
    fail(4, "machine_unknown", f'unknown machine "{{machine}}"',
         "machines: " + ", ".join(known))


time.sleep(settings.get("delay", {{}}).get(args[0], 0))
if args[0] == "get" and args[-1] == "--json":
    address = args[1]
    machine, slash, terminal = address.rpartition("/")
    if slash and (machine not in known or settings.get("unknown_on_get")):
        unknown(machine)
    if settings.get("files_after_get"):
        settings["files"].update(settings.pop("files_after_get"))
        settings_path.write_text(json.dumps(settings))
    value = settings["terminals"].get(address)
    if value is None:
        # Sample "get: a terminal address that does not exist on that machine": exit 3.
        fail(3, "not_found", f'no terminal "{{terminal}}"', "see `hc ls --all`")
    left = settings.get("vanish", {{}}).get(address)
    if left is not None:
        # The terminal answers `left` more times, then is gone (the "removed" samples).
        if left <= 1:
            del settings["terminals"][address]
        settings["vanish"][address] = left - 1
        settings_path.write_text(json.dumps(settings))
elif args == ["machine", "ls", "--json"]:
    # The `hc machine ls --json` shape of Herdr core 0.1.21: name, state and the
    # permanent core id, among other keys the bridge does not read.
    # `machine_ls_ok`: that many more answers are as if every machine were connected
    # with its first id, before `machine_states`, `machine_ids` and
    # `machine_ls_error` apply.
    left = settings.get("machine_ls_ok")
    normal = left is not None and left > 0
    if left is not None:
        settings["machine_ls_ok"] = left - 1
        settings_path.write_text(json.dumps(settings))
    if settings.get("machine_ls_error") and not normal:
        sys.exit(1)
    states = {{}} if normal else settings.get("machine_states", {{}})
    ids = {{}} if normal else settings.get("machine_ids", {{}})
    value = {{"machines": [
        {{"name": name, "host": name, "state": states.get(name, "connected"),
         "id": ids.get(name, (name.encode().hex() + "0" * 32)[:32]), "via": False}}
        for name in known
    ] + ([] if normal else settings.get("machine_extra", []))}}
elif args[0] == "send" and "--" in args:
    target = args[-2]
    value = settings["send"]
    if settings.get("on_send"):
        # Settings that change as `hc send` runs: after every read the bridge made
        # before it, and before any read after it.
        settings.update(settings.pop("on_send"))
        settings_path.write_text(json.dumps(settings))
    if settings.get("after_send"):
        settings["terminals"][target].update(settings["after_send"])
        settings_path.write_text(json.dumps(settings))
elif args[:3] == ["file", "read", "--on"] and args[4] == "--json" and len(args) == 6:
    machine, path = args[3], args[5]
    if machine not in known:
        unknown(machine)
    raw = settings.get("raw_read")
    if raw is not None:
        print(raw)
        sys.exit(0)
    raw_hex = settings.get("raw_read_hex")
    if raw_hex is not None:
        # Exact stdout bytes, for a file whose bytes are not valid UTF-8.
        sys.stdout.buffer.write(bytes.fromhex(raw_hex))
        sys.exit(0)
    if f"{{machine}}:{{path}}" in settings.get("directories", []):
        # Sample "read: a directory": exit 6, invalid_params.
        fail(6, "invalid_params", f"/home/user/{{path}} is not a regular file",
             "see `hc schema`")
    text = settings["files"].get(f"{{machine}}:{{path}}")
    if text is None:
        # Sample "read: a missing file" (and "a path that starts with a tilde"): exit 3.
        fail(3, "not_found", f"no file /home/user/{{path}}", "check the path, or hc exec ls")
    lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    # Sample "read: an existing file": exit 0, one JSON object on stdout.
    value = {{"first": 1, "last": lines, "lines": lines, "text": text}}
    print(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
    sys.exit(0)
else:
    sys.exit(2)
print(json.dumps(value))
"""


def make_fake(tmp_path: Path) -> FakeCore:
    cli = FakeCore(tmp_path / "hc", tmp_path / "settings.json", tmp_path / "calls.jsonl")
    cli.settings_path.write_text(
        json.dumps(
            {
                "machines": ["example-host", "other-host", "node-one", MACHINE],
                "terminals": {"t1": row("t1"), REMOTE: row(REMOTE)},
                "send": {"turn": 4, "started": True, "status": "working"},
                "files": {},
            }
        )
    )
    cli.binary.write_text(
        FAKE.format(python=sys.executable, settings=str(cli.settings_path), log=str(cli.log_path))
    )
    cli.binary.chmod(0o700)
    return cli


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


def make_bridge(fake: FakeCore, tmp_path: Path, machines: list[str] | None = None) -> Bridge:
    path = tmp_path / "config.json"
    Config(
        hc_enabled=True,
        codex_enabled=False,
        hc_binary=str(fake.binary),
        hc_send_machines=[MACHINE] if machines is None else machines,
    ).save(path)
    return Bridge(Config.load(path), path)


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> Any:
    bridge = make_bridge(fake, tmp_path)
    yield bridge
    await bridge.close()


def binding_of(bridge: Bridge, target: str) -> str | None:
    """The binding discovery gives for the fake's terminal, with no `hc` call.

    `peer_submit` requires the binding that `peer_search` returned. A search here
    would add `hc get` calls that the tests count, so this builds the same peer
    record from the terminal row that the fake `hc` holds.
    """
    settings = json.loads(Path(bridge.config.hc_binary).with_name("settings.json").read_text())
    terminal = settings["terminals"].get(target)
    return None if terminal is None else bridge.core._peer(terminal, target)["binding"]


async def delivered(bridge: Bridge, target: str, message: str, request: str) -> dict[str, Any]:
    """Submit through `peer_submit` and wait until the delivery job has finished."""
    task = await bridge.peer_submit(
        f"hc:{target}", message, request, binding=binding_of(bridge, target)
    )
    jobs = list(bridge.core_tasks.jobs.values())
    async with asyncio.timeout(10):
        await asyncio.gather(*jobs)
    return bridge.store.get(task["id"])


# Row A0: a brief for a terminal on another machine names no sender command.


def sent_text(fake: FakeCore) -> str:
    (send,) = fake.sends()
    return send[-1]


async def test_remote_send_names_no_sender_interpreter_config_or_command(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: the remote envelope used to name the sender's own command."""
    task = await delivered(bridge, REMOTE, "Review the parser", "remote-a0")
    assert task["status"] == "submitted"
    text = sent_text(fake)
    assert text.startswith(f"Bridge task: {task['id']}\nReview the parser\n\n")
    assert sys.executable not in text
    assert str(bridge.config_path) not in text
    assert str(bridge.config_path.parent) not in text
    assert "answerback_bridge" not in text
    assert "--result-file" not in text
    assert " -P " not in text
    assert "complete " + task["id"] not in text
    assert "call task_complete" not in text.replace("do not call task_complete", "")
    assert "Alternatively" not in text


def today_local_envelope(config_path: Path, task_id: str, message: str) -> str:
    """The local envelope exactly as `CoreTasks.envelope` built it at 51933f2."""
    command = shlex.join(module_launch_argv(config_path, "complete", task_id, "--result-file"))
    return (
        f"Bridge task: {task_id}\n{message}\n\n"
        "When this assignment is complete, write the final result to a UTF-8 file and run:\n"
        f"{command} /absolute/path/to/result.txt\n"
        "Alternatively call task_complete through the MCP connection for exactly this "
        f"bridge config: {config_path}. Use the assigned task ID. "
        "Keep normal tool approvals; report a blocker instead of claiming completion "
        "while approval or work is outstanding."
    )


async def test_local_send_is_unchanged_byte_for_byte(bridge: Bridge, fake: FakeCore) -> None:
    """pins_existing: a local terminal gets exactly the text it got at 51933f2."""
    task = await delivered(bridge, "t1", "Review the parser", "local-a0")
    assert sent_text(fake) == today_local_envelope(
        bridge.config_path, task["id"], "Review the parser"
    )
    assert bridge.core_tasks.envelope("task-1", "Review") == today_local_envelope(
        bridge.config_path, "task-1", "Review"
    )


# Row A1: a one-time token, made at reservation, hidden from every caller.


async def test_remote_reservation_stores_a_token_and_the_machine(bridge: Bridge) -> None:
    """red_at_pretip: a remote receipt records its token, machine and completion route."""
    task = await delivered(bridge, REMOTE, "Review the parser", "remote-a1")
    meta = task["metadata"]
    assert meta["completion"] == "result_file"
    assert meta["result_machine"] == MACHINE
    assert len(meta["result_token"]) == 32
    assert int(meta["result_token"], 16) >= 0
    assert meta["result_token"] == meta["result_token"].lower()


async def test_local_reservation_is_unchanged(bridge: Bridge) -> None:
    """pins_existing: a local receipt keeps `worker_report` and gets no token."""
    task = await delivered(bridge, "t1", "Review the parser", "local-a1")
    meta = task["metadata"]
    assert meta["completion"] == "worker_report"
    assert not {"result_token", "result_machine", "result_path"} & meta.keys()


async def test_same_request_twice_gives_one_receipt_and_one_token(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: a repeat returns the stored receipt; the token never changes."""
    first = await bridge.peer_submit(
        f"hc:{REMOTE}", "Review the parser", "same-twice", binding=binding_of(bridge, REMOTE)
    )
    again = await bridge.peer_submit(f"hc:{REMOTE}", "Review the parser", "same-twice")
    jobs = list(bridge.core_tasks.jobs.values())
    async with asyncio.timeout(10):
        await asyncio.gather(*jobs)
    later = await bridge.peer_submit(f"hc:{REMOTE}", "Review the parser", "same-twice")
    assert first["id"] == again["id"] == later["id"]
    assert sum(bridge.store.counts().values()) == 1
    assert len(fake.sends()) == 1
    token = bridge.store.get(first["id"])["metadata"]["result_token"]
    assert bridge.store.get(later["id"])["metadata"]["result_token"] == token


async def test_different_message_under_the_same_request_is_refused(bridge: Bridge) -> None:
    """pins_existing: different work under one request ID is refused, as before."""
    task = await delivered(bridge, REMOTE, "Review the parser", "same-key")
    before = bridge.store.get(task["id"])
    with pytest.raises(ValueError, match="different work"):
        await bridge.peer_submit(
            f"hc:{REMOTE}", "Review the lexer", "same-key", binding=binding_of(bridge, REMOTE)
        )
    assert bridge.store.get(task["id"]) == before


async def test_no_caller_surface_shows_the_token_or_the_path(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: task_get, task_wait, task_list, task_for_peer and the run pages."""
    from answerback_bridge.events.history import read_runs

    submitted = await bridge.peer_submit(
        f"hc:{REMOTE}", "Review the parser", "hidden", binding=binding_of(bridge, REMOTE)
    )
    jobs = list(bridge.core_tasks.jobs.values())
    async with asyncio.timeout(10):
        await asyncio.gather(*jobs)
    token = bridge.store.get(submitted["id"])["metadata"]["result_token"]
    surfaces = [
        submitted,
        await bridge.call("task_get", {"task_id": submitted["id"]}),
        await bridge.call("task_wait", {"task_id": submitted["id"], "seconds": 1}),
        await bridge.call("task_list", {}),
        await bridge.call("task_for_peer", {"selector": f"hc:{REMOTE}"}),
        read_runs(bridge.store.path),
    ]
    for surface in surfaces:
        text = json.dumps(surface)
        assert token not in text
        assert "result_token" not in text
        assert "result_path" not in text
        assert ".answerback/results" not in text


async def test_task_list_search_does_not_answer_for_hidden_metadata(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: `task_list` matched its query against metadata it never shows.

    A query that finds a receipt confirms a guess, so a search over the stored token
    or a held `provisional_result` revealed them piece by piece.
    """
    remote = await delivered(bridge, REMOTE, "Review the parser", "search-remote")
    token = remote["metadata"]["result_token"]
    local = await delivered(bridge, "t1", "Review the lexer", "search-local")
    # Held as provisional the same way an early report is (tasks.py complete_worker).
    bridge.store.metadata(local["id"], {"provisional_result": "early secret words"})
    for probe in (token, token[:8], "result_token", "early secret words", "provisional"):
        assert (await bridge.call("task_list", {"query": probe}))["tasks"] == []
    # Shown metadata stays searchable.
    found = (await bridge.call("task_list", {"query": "result_file"}))["tasks"]
    assert [task["id"] for task in found] == [remote["id"]]


# Fix round 1, F2: local failure texts are those of the start commit.


@pytest.mark.parametrize("target", ["t1", REMOTE])
async def test_terminal_gone_before_send_fails_and_names_the_cause(
    bridge: Bridge, fake: FakeCore, target: str
) -> None:
    """T-226 item 4: a terminal that vanishes between reservation and the send has
    had nothing typed into it, so its receipt is `failed`, not `uncertain`.

    The `hc get` calls before reservation answer; the one inside `Core.send` gets the
    "get: a terminal address that does not exist" sample (exit 3, `not_found`).
    Until T-226 the receipt was `uncertain` with "Core delivery unverified
    (BridgeError)", which told a person to inspect a send that never ran.
    """
    # `peer_submit` reads the terminal twice before it reserves (resolve, then
    # `CoreTasks.submit`); `Core.send` reads it a third time.
    fake.update(vanish={target: 2})
    task = await delivered(bridge, target, "Review the parser", "gone-" + target.replace("/", "-"))
    assert task["status"] == "failed"
    assert task["detail"] == core_module.NOT_SENT_UNREAD
    assert fake.sends() == []


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"machine_states": {MACHINE: "disconnected"}}, "NOT_SENT_MACHINE"),
        ({"machine_ls_error": True}, "NOT_SENT_MACHINES_UNREAD"),
    ],
)
async def test_remote_machine_not_connected_at_submission_is_refused(
    bridge: Bridge, fake: FakeCore, changes: dict[str, Any], detail: str
) -> None:
    """T-226 item 4: a machine that is not connected at submission gets no receipt."""
    fake.update(**changes)
    with pytest.raises(core_module.CoreNotSent) as refused:
        await delivered(bridge, REMOTE, "Review the parser", "machine-down")
    assert str(refused.value) == getattr(core_module, detail)
    assert bridge.store.by_request("machine-down") is None
    assert fake.sends() == []


@pytest.mark.parametrize(
    ("changes", "detail", "code"),
    [
        (
            {"machine_extra": [{"name": MACHINE, "state": "disconnected", "id": "c" * 32}]},
            "NOT_SENT_MACHINE_TWICE",
            "machine_listed_twice",
        ),
        ({"machine_ids": {MACHINE: "A" * 32}}, "NOT_SENT_MACHINE_BAD_ID", "machine_id_malformed"),
        ({"machine_ids": {MACHINE: "a" * 31}}, "NOT_SENT_MACHINE_BAD_ID", "machine_id_malformed"),
        (
            {"machine_states": {MACHINE: "disconnected"}},
            "NOT_SENT_MACHINE",
            "machine_not_connected",
        ),
        ({"machine_ls_error": True}, "NOT_SENT_MACHINES_UNREAD", "machine_list_unreadable"),
    ],
)
async def test_each_machine_list_refusal_has_its_own_sentence(
    bridge: Bridge, fake: FakeCore, changes: dict[str, Any], detail: str, code: str
) -> None:
    """Review N1: a name listed twice or a malformed id is not "not connected" or
    "could not be read", so an operator does not retry what a retry cannot fix."""
    fake.update(**changes)
    with pytest.raises(core_module.CoreNotSent) as refused:
        await delivered(bridge, REMOTE, "Review the parser", "machine-row")
    assert str(refused.value) == getattr(core_module, detail)
    public = public_bridge_error(refused.value)
    assert public is not None and public.code == code
    assert bridge.store.by_request("machine-row") is None
    assert fake.sends() == []
    sentences = {
        getattr(core_module, name)
        for name in (
            "NOT_SENT_MACHINE",
            "NOT_SENT_MACHINES_UNREAD",
            "NOT_SENT_MACHINE_TWICE",
            "NOT_SENT_MACHINE_BAD_ID",
        )
    }
    assert len(sentences) == 4


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"machine_states": {MACHINE: "disconnected"}}, "NOT_SENT_MACHINE"),
        ({"machine_ls_error": True}, "NOT_SENT_MACHINES_UNREAD"),
    ],
)
async def test_remote_machine_lost_before_the_send_fails_the_receipt(
    bridge: Bridge, fake: FakeCore, changes: dict[str, Any], detail: str
) -> None:
    """T-226 item 4: a machine lost between reservation and the send fails, unsent."""
    fake.update(machine_ls_ok=1, **changes)
    task = await delivered(bridge, REMOTE, "Review the parser", "machine-lost")
    assert task["status"] == "failed"
    assert task["detail"] == getattr(core_module, detail)
    assert fake.sends() == []


async def test_failure_after_the_send_stays_uncertain(bridge: Bridge, fake: FakeCore) -> None:
    """T-226 item 4: once `hc send` has run, an unreadable identity is still uncertain."""
    fake.update(vanish={REMOTE: 3})
    task = await delivered(bridge, REMOTE, "Review the parser", "gone-after-send")
    assert len(fake.sends()) == 1
    assert task["status"] == "uncertain"
