"""Row A2: the sender's bridge reads a remote result file through Herdr core.

The fake `hc` and its sample forms live in `test_core_result_file.py`. Labels as there:
the first docstring line of each test is `red_at_pretip` or `pins_existing`.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_core_result_file import (
    MACHINE,
    REMOTE,
    FakeCore,
    binding_of,
    delivered,
    make_bridge,
    make_fake,
)

from answerback_bridge import process
from answerback_bridge.process import BridgeError
from answerback_bridge.providers.herdr import core as core_module
from answerback_bridge.providers.herdr.adapters import core as adapter_module
from answerback_bridge.providers.herdr.core import Core
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import TaskStore


class Clock:
    """Drives the adapter's 5-second read interval without sleeping."""

    def __init__(self) -> None:
        self.now = 1_795_200_000.0

    def __call__(self) -> float:
        return self.now


def install_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(adapter_module, "_clock", clock, raising=False)
    return clock


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return install_clock(monkeypatch)


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> Any:
    bridge = make_bridge(fake, tmp_path)
    yield bridge
    await bridge.close()


def path_of(task: dict[str, Any]) -> str:
    return f".answerback/results/{task['id']}-{task['metadata']['result_token']}.txt"


def valid(task: dict[str, Any], body: str = "All checks passed.\nNo findings.") -> str:
    """A result file as the envelope asks for it (and as the "read: an existing file"
    sample holds it): header line, body, last line, final newline."""
    token = task["metadata"]["result_token"]
    return f"answerback-result {task['id']} {token}\n{body}\nanswerback-end {token}\n"


def read_argv(task: dict[str, Any]) -> list[str]:
    return ["file", "read", "--on", MACHINE, "--json", path_of(task)]


async def remote_task(bridge: Bridge, request: str = "remote", target: str = REMOTE) -> Any:
    return await delivered(bridge, target, "Review the parser", request)


# The envelope.


async def test_remote_envelope_names_the_result_file_with_home_and_no_tilde(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: the remote brief asks for the result file, in plain lines.

    Changed in fix round `ans-fix1` (review Q4) and again in fix round `ans-fix2`
    (review 2, finding M3): red at `0b4c2707`, whose brief lets the draft be anywhere.
    Step 2 changed in fix round 3 (review 3, N1): red at `72859ac6` too.
    """
    task = await remote_task(bridge)
    token = task["metadata"]["result_token"]
    name = f"$HOME/.answerback/results/{task['id']}-{token}"
    (send,) = fake.sends()
    assert send[-1] == (
        f"Bridge task: {task['id']}\n"
        "Review the parser\n"
        "\n"
        "This assignment came from another machine. "
        "Its result is collected from a file on this machine.\n"
        "Do not run any answerback command and do not call task_complete: "
        "neither can reach the sender.\n"
        "When the assignment is complete, do these three steps in order.\n"
        "Step 1. Run this command. It makes the result folder, for this account only "
        "(mode 0700):\n"
        'mkdir -p "$HOME/.answerback/results" && chmod 700 "$HOME/.answerback/results"\n'
        "Step 2. Write the draft file as UTF-8 text. Write it whole, in place of any earlier "
        "draft: do not append to one.\n"
        "The draft's path is printed below as a shell reads it. "
        "A tool that writes files without a shell needs\n"
        "this account's home folder in place of $HOME, "
        "and no quotation marks around the path:\n"
        f'"{name}.draft"\n'
        "The draft has three parts. The draft's first line is exactly:\n"
        f"answerback-result {task['id']} {token}\n"
        "Then the result itself, from 1 to 200000 characters. "
        "An empty result or a longer one is refused.\n"
        "The draft's last line, with nothing else on it, is exactly:\n"
        f"answerback-end {token}\n"
        "Step 3. Run this command. It sets the draft to mode 0600 and renames it inside "
        "that folder:\n"
        f'chmod 600 "{name}.draft" && mv "{name}.draft" "{name}.txt"\n'
        "Do not change the renamed file afterwards. Keep normal tool approvals. "
        "If approval or work is outstanding,\n"
        "say so in this terminal and do not run step 3."
    )
    assert "~" not in send[-1]


async def test_envelope_bound_covers_the_token_before_reservation(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: a message that fits locally but not with the remote text is refused.

    Made exact to the byte in fix round `ans-fix2` (review 2 finding B3): red at
    `0b4c2707`, which accepts the envelope of 131,072 bytes built here. Before, the
    message was two-byte characters only and stopped one byte short of the bound.
    """
    placeholder = "00000000-0000-0000-0000-000000000000"
    remote = bridge.core_tasks.envelope(placeholder, "", token="0" * 32)
    local = bridge.core_tasks.envelope(placeholder, "")
    largest = 128 * 1024 - 1  # the largest accepted envelope, in bytes
    room = largest - len(remote.encode())
    assert len(local.encode()) < len(remote.encode())

    def filled(size: int) -> str:
        return "é" * (size // 2) + "x" * (size % 2)

    message = filled(room + 1)  # exactly one byte over with the remote text
    over = bridge.core_tasks.envelope(placeholder, message, token="0" * 32)
    assert len(over.encode()) == largest + 1
    core_module.validate_message(bridge.core_tasks.envelope(placeholder, message))
    with pytest.raises(ValueError, match="128 KiB"):
        await bridge.peer_submit(
            f"hc:{REMOTE}", message, "too-big", binding=binding_of(bridge, REMOTE)
        )
    assert bridge.store.counts() == {}
    assert fake.sends() == []
    task = await remote_task_with(bridge, filled(room))
    assert task["status"] == "submitted"
    (send,) = fake.sends()
    assert len(send[-1].encode()) == largest


async def remote_task_with(bridge: Bridge, message: str) -> dict[str, Any]:
    return await delivered(bridge, REMOTE, message, "fits")


async def test_remote_ready_peer_advertises_result_file(bridge: Bridge) -> None:
    """T-226 item 1: a ready remote peer reports through its result file.

    It cannot reach this bridge's socket, so it never advertises `worker_report`.
    """
    assert (await bridge.core.peer(REMOTE))["capabilities"] == ["read", "send", "result_file"]


def test_result_limit_is_the_one_complete_worker_enforces(tmp_path: Path) -> None:
    """red_at_pretip: the file body limit is the one `TaskStore.complete_worker` enforces."""
    store = TaskStore(tmp_path / "tasks.sqlite")
    task, _ = store.reserve("hc:default:t1", "Review", "limit", {"kind": "hc"})
    store.confirm_core_delivery(task["id"], {"turn": 1, "started": True, "status": "idle"})
    with pytest.raises(ValueError, match="200000"):
        store.complete_worker(task["id"], "x" * (core_module.RESULT_MAX_CHARS + 1))
    done = store.complete_worker(task["id"], "x" * core_module.RESULT_MAX_CHARS)
    assert done["status"] == "completed"


# Not yet, and files that do not count.


async def test_missing_file_keeps_submitted(bridge: Bridge, fake: FakeCore) -> None:
    """red_at_pretip: a missing file (exit 3, not_found) is "not yet"."""
    task = await remote_task(bridge)
    observed = await bridge.get_task(task["id"])
    assert observed["status"] == "submitted"
    assert fake.reads() == [read_argv(task)]
    assert fake.gets()[-1] == ["get", REMOTE, "--json"]


async def test_file_without_its_last_line_keeps_submitted(bridge: Bridge, fake: FakeCore) -> None:
    """red_at_pretip: a half-written file has no last line and is "not yet"."""
    task = await remote_task(bridge)
    token = task["metadata"]["result_token"]
    fake.put_file(path_of(task), f"answerback-result {task['id']} {token}\nhalf of the resu")
    assert (await bridge.get_task(task["id"]))["status"] == "submitted"
    assert len(fake.reads()) == 1


@pytest.mark.parametrize(
    "shape",
    ["wrong_header_token", "wrong_end_token", "wrong_task", "no_header", "trailing_text"],
)
async def test_file_that_does_not_match_this_receipt_is_ignored(
    bridge: Bridge, fake: FakeCore, shape: str
) -> None:
    """red_at_pretip: header, last line and task ID must all match this receipt."""
    task = await remote_task(bridge)
    token = task["metadata"]["result_token"]
    other = "f" * 32
    text = {
        "wrong_header_token": f"answerback-result {task['id']} {other}\nBody\n"
        f"answerback-end {token}\n",
        "wrong_end_token": f"answerback-result {task['id']} {token}\nBody\n"
        f"answerback-end {other}\n",
        "wrong_task": f"answerback-result 11111111-2222-3333-4444-555555555555 {token}\nBody\n"
        f"answerback-end {token}\n",
        "no_header": f"Body\nanswerback-end {token}\n",
        "trailing_text": valid(task) + "more\n",
    }[shape]
    fake.put_file(path_of(task), text)
    after = await bridge.get_task(task["id"])
    assert after["status"] == "submitted"
    assert after["result"] is None


async def test_file_named_for_another_token_is_never_read(bridge: Bridge, fake: FakeCore) -> None:
    """red_at_pretip: the bridge reads only the path it named for this receipt."""
    task = await remote_task(bridge)
    wrong = {**task, "metadata": {**task["metadata"], "result_token": "e" * 32}}
    fake.put_file(path_of(wrong), valid(wrong))
    assert (await bridge.get_task(task["id"]))["status"] == "submitted"
    assert fake.reads() == [read_argv(task)]


async def test_body_over_the_limit_is_refused_and_not_read_again_until_reconciled(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: limit plus one character sets `detail`, keeps the status."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), valid(task, "x" * (core_module.RESULT_MAX_CHARS + 1)))
    after = await bridge.get_task(task["id"])
    assert after["status"] == "submitted"
    assert "200000" in after["detail"]
    clock.now += 60
    gets = len(fake.gets())
    await bridge.get_task(task["id"])
    assert len(fake.reads()) == 1
    # Fix round 1 (F3): the poll still looks at the terminal, never at the file.
    assert fake.gets()[gets:] == [["get", REMOTE, "--json"]]
    assert task["id"] in bridge.observation_ids()
    fake.put_file(path_of(task), valid(task, "Short now"))
    done = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert done["status"] == "completed" and done["result"] == "Short now"
    assert len(fake.reads()) == 2


@pytest.mark.parametrize("body", ["", "\n"])
async def test_empty_body_is_refused_and_keeps_the_status(
    bridge: Bridge, fake: FakeCore, body: str
) -> None:
    """red_at_pretip: an empty body is refused like an oversized one."""
    task = await remote_task(bridge)
    token = task["metadata"]["result_token"]
    text = f"answerback-result {task['id']} {token}\n{body}answerback-end {token}\n"
    fake.put_file(path_of(task), text)
    after = await bridge.get_task(task["id"])
    assert after["status"] == "submitted"
    assert "empty" in after["detail"]


# A valid file.


async def test_valid_file_completes_exactly_once(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: a valid file completes the receipt; later observations read nothing."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), valid(task, "line two: café ✓"))
    done = await bridge.get_task(task["id"])
    assert done["status"] == "completed"
    assert done["result"] == "line two: café ✓"
    assert done["detail"] is None
    clock.now += 60
    again = await bridge.call("task_wait", {"task_id": task["id"], "seconds": 1})
    assert again["status"] == "completed"
    assert len(fake.reads()) == 1
    assert len(fake.sends()) == 1
    assert task["id"] not in bridge.observation_ids()


@pytest.mark.parametrize(
    ("ending", "body"),
    [
        ("\n", "First\nSecond"),
        ("", "First\nSecond"),
        ("\r\n", "First\nSecond"),
        ("\n\n", None),
    ],
)
async def test_line_ends_and_the_final_newline(
    bridge: Bridge, fake: FakeCore, ending: str, body: str | None
) -> None:
    """red_at_pretip: one final newline is optional; CRLF counts as LF; a blank line
    after the last line leaves the file incomplete."""
    task = await remote_task(bridge)
    token = task["metadata"]["result_token"]
    lines = [
        f"answerback-result {task['id']} {token}",
        "First",
        "Second",
        f"answerback-end {token}",
    ]
    newline = "\r\n" if ending == "\r\n" else "\n"
    fake.put_file(path_of(task), newline.join(lines) + ending)
    after = await bridge.get_task(task["id"])
    if body is None:
        assert after["status"] == "submitted"
    else:
        assert after["status"] == "completed" and after["result"] == body


async def test_file_found_before_delivery_is_verified_is_held_provisionally(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: the file path keeps `complete_worker`'s provisional rule.

    Through `observe`, `poll_ids` and `reconcile` a read needs `submitted`, which
    `confirm_delivery` sets only together with `delivery_verified`. This drives the
    adapter's collection step directly on the `pending` receipt that a slow send
    leaves, to pin what `complete_worker` does with a file found in that window.
    """
    fake.update(delay={"send": 1.5})
    pending = await bridge.peer_submit(
        f"hc:{REMOTE}", "Review the parser", "early", binding=binding_of(bridge, REMOTE)
    )
    task = bridge.store.get(pending["id"])
    assert task["status"] == "pending"
    fake.put_file(path_of(task), valid(task, "Early result"))
    adapter = bridge.adapters.get("hc")
    await adapter.collect_result(task["id"])
    held = bridge.store.get(task["id"])
    assert held["status"] == "pending"
    assert held["metadata"]["provisional_result"] == "Early result"
    assert "Early result" not in json.dumps(await bridge.get_task(task["id"], refresh=False))
    jobs = list(bridge.core_tasks.jobs.values())
    async with asyncio.timeout(10):
        await asyncio.gather(*jobs)
    done = bridge.store.get(task["id"])
    assert done["status"] == "completed" and done["result"] == "Early result"


async def test_identity_invalidated_refuses_the_file(bridge: Bridge, fake: FakeCore) -> None:
    """red_at_pretip: an invalidated receipt is never read and never completed."""
    task = await remote_task(bridge)
    bridge.store.invalidate_submission(task["id"], "Core terminal identity changed after send")
    fake.put_file(path_of(task), valid(task))
    after = await bridge.get_task(task["id"])
    assert after["status"] == "uncertain"
    assert fake.reads() == []
    with pytest.raises(ValueError, match="invalidated"):
        await bridge.call("task_reconcile", {"task_id": task["id"]})
    with pytest.raises(ValueError, match="invalidated"):
        await bridge.adapters.get("hc").collect_result(task["id"])
    assert bridge.store.get(task["id"])["status"] == "uncertain"


async def test_reading_resumes_after_a_restart(fake: FakeCore, tmp_path: Path) -> None:
    """red_at_pretip: the token is in the stored row, so a new bridge reads on."""
    first = make_bridge(fake, tmp_path)
    try:
        task = await remote_task(first)
    finally:
        await first.close()
    fake.put_file(path_of(task), valid(task, "After the restart"))
    second = make_bridge(fake, tmp_path)
    try:
        assert task["id"] in second.observation_ids()
        done = await second.get_task(task["id"])
        assert done["status"] == "completed" and done["result"] == "After the restart"
    finally:
        await second.close()


# Receipts and paths the reader leaves alone.


async def test_local_receipt_makes_no_file_read_and_no_get(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """pins_existing: a local receipt makes no `hc` call when observed or reconciled."""
    task = await delivered(bridge, "t1", "Review the parser", "local")
    calls = len(fake.calls())
    for _ in range(3):
        clock.now += 60
        assert (await bridge.get_task(task["id"]))["status"] == "submitted"
    assert task["id"] not in bridge.observation_ids()
    with pytest.raises(ValueError, match="Provider hc does not support explicit reconciliation"):
        await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert len(fake.calls()) == calls


@pytest.mark.parametrize(
    "path",
    [
        "~/.answerback/results/11111111-2222-3333-4444-555555555555-"
        "0123456789abcdef0123456789abcdef.txt",
        "/home/user/.answerback/results/11111111-2222-3333-4444-555555555555-"
        "0123456789abcdef0123456789abcdef.txt",
        ".answerback/results/../../.ssh/id_ed25519",
        ".answerback/results/11111111-2222-3333-4444-555555555555-"
        "0123456789ABCDEF0123456789ABCDEF.txt",
        ".answerback/results/11111111-2222-3333-4444-555555555555.txt",
        ".answerback/results/11111111-2222-3333-4444-555555555555-"
        "0123456789abcdef0123456789abcdef.txt\n",
        "code/personal/tools/herdr_bridge-worktrees/_fablane/probe/sample.txt",
    ],
)
async def test_path_outside_the_pattern_is_refused_without_a_call(
    fake: FakeCore, path: str
) -> None:
    """red_at_pretip: `read_result` checks the path before it runs `hc`."""
    core = Core(str(fake.binary), send_machines=[MACHINE])
    with pytest.raises(ValueError, match="result file"):
        await core.read_result(MACHINE, path)
    assert fake.calls() == []


async def test_machine_removed_from_the_list_is_not_read(fake: FakeCore, tmp_path: Path) -> None:
    """red_at_pretip: only machines in the current `send_machines` are read."""
    first = make_bridge(fake, tmp_path)
    try:
        task = await remote_task(first)
    finally:
        await first.close()
    fake.put_file(path_of(task), valid(task))
    before = TaskStore(tmp_path / "tasks.sqlite").get(task["id"])
    second = make_bridge(fake, tmp_path, machines=[])
    try:
        assert second.observation_ids() == ()
        assert (await second.get_task(task["id"]))["status"] == "submitted"
        with pytest.raises(ValueError, match="not in hc_send_machines"):
            await second.call("task_reconcile", {"task_id": task["id"]})
        assert second.store.get(task["id"]) == before
    finally:
        await second.close()
    assert fake.reads() == []
    with pytest.raises(BridgeError, match="allow-listed"):
        await Core(str(fake.binary)).read_result(MACHINE, path_of(task))
    assert fake.reads() == []


# Read errors change nothing but the read stamp.


@pytest.mark.parametrize("failure", ["machine_unknown", "not_json", "no_text", "directory"])
async def test_read_error_changes_nothing(bridge: Bridge, fake: FakeCore, failure: str) -> None:
    """red_at_pretip: an unknown machine, odd output or another error kind is an error."""
    task = await remote_task(bridge)
    before = bridge.store.get(task["id"])
    if failure == "machine_unknown":
        fake.update(machines=["example-host"])  # "read: a machine name that hc does not know"
    elif failure == "not_json":
        fake.update(raw_read="answerback-result without --json")
    elif failure == "no_text":
        fake.update(raw_read=json.dumps({"first": 1, "last": 1, "lines": 1}))
    else:
        fake.update(directories=[f"{MACHINE}:{path_of(task)}"])
    with pytest.raises(BridgeError):
        await bridge.get_task(task["id"])
    after = bridge.store.get(task["id"])
    assert after["status"] == before["status"] == "submitted"
    assert (after["result"], after["detail"]) == (None, None)
    assert {k: v for k, v in after["metadata"].items() if k != "last_result_read"} == before[
        "metadata"
    ]
    assert len(fake.sends()) == 1


async def test_read_timeout_and_oversized_output_change_nothing(
    bridge: Bridge, fake: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_pretip: the runner's time and size bounds end a read as an error."""
    task = await remote_task(bridge)
    monkeypatch.setattr(core_module, "COMMAND_SECONDS", 0.5)
    fake.update(delay={"file": 2})
    with pytest.raises(BridgeError, match="timed out"):
        await bridge.get_task(task["id"])
    fake.update(delay={}, raw_read="x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(BridgeError, match="exceeded"):
        await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert bridge.store.get(task["id"])["status"] == "submitted"


# Fix round 1, F1: a result file that is not valid UTF-8 never completes a receipt.


def file_read_bytes(text_bytes: bytes) -> str:
    """Hex of the stdout of the "read: an existing file" sample around `text_bytes`.

    Herdr core prints the file in the JSON `text` string; here its bytes are placed
    there raw, so stdout itself is not valid UTF-8 (the reviewer's F1 reproduction).
    """
    lines = text_bytes.count(b"\n")
    head = f'{{"first":1,"last":{lines},"lines":{lines},"text":"'.encode()
    escaped = text_bytes.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(b"\n", b"\\n")
    return (head + escaped + b'"}\n').hex()


async def test_file_with_invalid_utf8_is_refused_and_stores_nothing_of_it(
    bridge: Bridge, fake: FakeCore, clock: Clock
) -> None:
    """red_at_pretip: an invalid byte used to become U+FFFD and complete the receipt."""
    task = await remote_task(bridge)
    raw = valid(task, "bad-HERE-byte").encode().replace(b"HERE", b"\xff")
    fake.update(raw_read_hex=file_read_bytes(raw))
    after = await bridge.get_task(task["id"])
    stored = bridge.store.get(task["id"])
    assert after["status"] == stored["status"] == "submitted"
    assert stored["result"] is None
    assert "UTF-8" in stored["detail"]
    assert stored["metadata"]["result_refused"] is True
    assert "provisional_result" not in stored["metadata"]
    assert "\ufffd" not in json.dumps(stored, ensure_ascii=False)
    assert "bad-" not in json.dumps(stored, ensure_ascii=False)
    clock.now += 60
    await bridge.get_task(task["id"])
    assert len(fake.reads()) == 1  # a refused file is read again only on task_reconcile
    fake.update(raw_read_hex=None)
    fake.put_file(path_of(task), valid(task, "Clean text now"))
    done = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert done["status"] == "completed" and done["result"] == "Clean text now"


async def test_file_with_a_lone_surrogate_escape_is_refused(bridge: Bridge, fake: FakeCore) -> None:
    """red_at_pretip: JSON `\\ud800` decodes to a string that is not UTF-8 text."""
    task = await remote_task(bridge)
    text = json.dumps(valid(task, "half-PAIR-of-a-pair")).replace("PAIR", "\\ud800")
    fake.update(raw_read=f'{{"first":1,"last":3,"lines":3,"text":{text}}}')
    after = await bridge.get_task(task["id"])
    assert after["status"] == "submitted"
    assert "UTF-8" in after["detail"]
    assert bridge.store.get(task["id"])["result"] is None


async def test_unfinished_file_with_invalid_utf8_is_only_not_yet(
    bridge: Bridge, fake: FakeCore
) -> None:
    """red_at_pretip: a file cut inside a character, with no last line, is "not yet".

    An agent that writes in place can leave half of a multi-byte character; such a
    file is not refused, so the finished file is still read later.
    """
    task = await remote_task(bridge)
    token = task["metadata"]["result_token"]
    partial = f"answerback-result {task['id']} {token}\ncaf".encode() + "é".encode()[:1]
    fake.update(raw_read_hex=file_read_bytes(partial))
    after = await bridge.get_task(task["id"])
    assert after["status"] == "submitted"
    assert after["detail"] is None
    assert "result_refused" not in bridge.store.get(task["id"])["metadata"]


async def test_authored_replacement_character_completes(bridge: Bridge, fake: FakeCore) -> None:
    """red_at_pretip: U+FFFD written as valid UTF-8 is ordinary text."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), valid(task, "a literal \ufffd sign"))
    done = await bridge.get_task(task["id"])
    assert done["status"] == "completed" and done["result"] == "a literal \ufffd sign"


async def test_other_runner_callers_still_decode_with_replacement() -> None:
    """pins_existing: `process.run` without the new option replaces bad bytes, as before."""
    output = await process.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok-\\xff')"]
    )
    assert output.stdout == "ok-\ufffd"
