"""T-189: a remote result says how it was authenticated, and listing a machine says
what it delegates.

A remote Core result file proves that the brief reached the listed machine's OS
account, not which program of that account wrote it. The completed receipt records
`result_authentication: "trusted_os_account"` and its `result_machine`, immutably,
and `doctor` and `status` name every listed machine with the trust statement. The
fake `hc` is the one in `test_core_result_file.py`.
"""

import asyncio
import re
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
from test_core_result_file_read import file_read_bytes, install_clock, path_of, valid

from answerback_bridge.config import REMOTE_TRUST_STATEMENT, remote_trust_line
from answerback_bridge.runtime import Service
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import TRUSTED_OS_ACCOUNT, TaskStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    return install_clock(monkeypatch)


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> Any:
    bridge = make_bridge(fake, tmp_path)
    yield bridge
    await bridge.close()


async def remote_task(bridge: Bridge, request: str = "remote") -> dict[str, Any]:
    return await delivered(bridge, REMOTE, "Review the parser", request)


# The receipt label.


async def test_remote_result_records_account_level_authentication(
    bridge: Bridge, fake: FakeCore
) -> None:
    """A completed remote receipt names its authentication and machine on every surface."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), valid(task, "Done."))
    done = await bridge.get_task(task["id"])
    assert done["status"] == "completed"
    surfaces = [
        done,
        await bridge.call("task_get", {"task_id": task["id"]}),
        await bridge.call("task_wait", {"task_id": task["id"], "seconds": 1}),
    ]
    for surface in surfaces:
        meta = surface["metadata"]
        assert meta["result_authentication"] == TRUSTED_OS_ACCOUNT == "trusted_os_account"
        assert meta["result_machine"] == MACHINE
        # Herdr Core's machine record names no account, so none is claimed.
        assert "result_account" not in meta
        assert "provisional_provenance" not in meta


async def test_label_is_written_with_the_completion(bridge: Bridge, fake: FakeCore) -> None:
    """Before a valid file is read the receipt carries no label; after, it does."""
    task = await remote_task(bridge)
    assert "result_authentication" not in bridge.store.get(task["id"])["metadata"]
    fake.put_file(path_of(task), valid(task))
    await bridge.get_task(task["id"])
    stored = bridge.store.get(task["id"])
    assert stored["status"] == "completed"
    assert stored["metadata"]["result_authentication"] == TRUSTED_OS_ACCOUNT


@pytest.mark.parametrize("case", ["wrong_token", "wrong_task", "empty_body", "not_utf8"])
async def test_no_label_without_a_valid_file(bridge: Bridge, fake: FakeCore, case: str) -> None:
    """A file that does not complete the receipt leaves no authentication label."""
    task = await remote_task(bridge, "neg-" + case)
    token = task["metadata"]["result_token"]
    other = "11111111-2222-3333-4444-555555555555"
    texts = {
        "wrong_token": valid(task).replace(token, "0" * 32),
        "wrong_task": valid(task).replace(task["id"], other),
        "empty_body": f"answerback-result {task['id']} {token}\nanswerback-end {token}\n",
    }
    if case == "not_utf8":
        raw = valid(task, "bad-HERE-byte").encode().replace(b"HERE", b"\xff")
        fake.update(raw_read_hex=file_read_bytes(raw))
    else:
        fake.put_file(path_of(task), texts[case])
    after = await bridge.get_task(task["id"])
    assert after["status"] == "submitted"
    assert "result_authentication" not in bridge.store.get(task["id"])["metadata"]


async def test_local_worker_report_has_no_remote_label(bridge: Bridge) -> None:
    """A local terminal's `task_complete` result keeps its metadata as before."""
    task = await delivered(bridge, "t1", "Review the parser", "local")
    done = bridge.complete(task["id"], "Local result")
    assert done["status"] == "completed"
    assert "result_authentication" not in done["metadata"]


async def test_label_held_with_a_provisional_result_is_hidden_until_delivery(
    bridge: Bridge, fake: FakeCore
) -> None:
    """A file read before delivery is verified holds its label privately, then publishes it."""
    fake.update(delay={"send": 1.5})
    pending = await bridge.peer_submit(
        f"hc:{REMOTE}", "Review the parser", "early", binding=binding_of(bridge, REMOTE)
    )
    task = bridge.store.get(pending["id"])
    fake.put_file(path_of(task), valid(task, "Early result"))
    await bridge.adapters.get("hc").collect_result(task["id"])
    held = bridge.store.get(task["id"])
    assert held["status"] == "pending"
    assert held["metadata"]["provisional_provenance"]["result_authentication"] == (
        TRUSTED_OS_ACCOUNT
    )
    shown = await bridge.get_task(task["id"], refresh=False)
    assert "result_authentication" not in shown["metadata"]
    assert "provisional_provenance" not in shown["metadata"]
    async with asyncio.timeout(10):
        await asyncio.gather(*list(bridge.core_tasks.jobs.values()))
    done = bridge.store.get(task["id"])
    assert done["status"] == "completed"
    assert done["metadata"]["result_authentication"] == TRUSTED_OS_ACCOUNT
    assert done["metadata"]["result_machine"] == MACHINE
    assert "provisional_provenance" not in done["metadata"]


# Immutability.


@pytest.mark.parametrize(
    ("key", "value"),
    [("result_authentication", "verified_agent"), ("result_machine", "other-host")],
)
async def test_provenance_cannot_be_rewritten(
    bridge: Bridge, fake: FakeCore, key: str, value: str
) -> None:
    """Once stored, the label and the machine refuse any other value."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), valid(task))
    await bridge.get_task(task["id"])
    before = bridge.store.get(task["id"])
    with pytest.raises(ValueError, match="provenance is immutable"):
        bridge.store.metadata(task["id"], {key: value})
    assert bridge.store.get(task["id"]) == before
    # The same value is not a change.
    bridge.store.metadata(task["id"], {key: before["metadata"][key]})
    assert bridge.store.get(task["id"])["metadata"] == before["metadata"]


async def test_result_machine_is_immutable_from_reservation(bridge: Bridge) -> None:
    """The machine stored when the receipt is reserved cannot be moved either."""
    task = await remote_task(bridge)
    with pytest.raises(ValueError, match="provenance is immutable: result_machine"):
        bridge.store.metadata(task["id"], {"result_machine": "other-host"})


async def test_repeat_completion_keeps_the_first_label(bridge: Bridge, fake: FakeCore) -> None:
    """Completing a completed receipt again with other provenance changes nothing."""
    task = await remote_task(bridge)
    fake.put_file(path_of(task), valid(task, "Body"))
    await bridge.get_task(task["id"])
    before = bridge.store.get(task["id"])
    bridge.store.complete_worker(
        task["id"], "Body", provenance={"result_authentication": "something_else"}
    )
    assert bridge.store.get(task["id"]) == before


def test_unknown_provenance_key_is_refused(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    with pytest.raises(ValueError, match="Unknown result provenance key"):
        store.complete_worker("any", "x", provenance={"signed_by": "agent"})


# What listing a machine says: doctor, status, and receipts a delist strands.


def test_trust_line_names_each_machine_and_the_statement() -> None:
    assert remote_trust_line([]) is None
    line = remote_trust_line(["nuc1", "other-host"])
    assert line is not None
    assert "\n" not in line
    assert line == f"hc_send_machines: nuc1, other-host. {REMOTE_TRUST_STATEMENT}"


async def test_doctor_and_status_state_the_trust(bridge: Bridge, tmp_path: Path) -> None:
    doctor = await bridge.doctor()
    assert doctor["remote_trust"] == remote_trust_line([MACHINE])
    assert "remote_unlisted_receipts" not in doctor
    service = Service(tmp_path / "config.json")
    service.bridge = bridge
    status = service.status()
    assert status["remote_trust"] == remote_trust_line([MACHINE])
    assert "remote_unlisted_receipts" not in status


async def test_no_listed_machine_says_nothing(fake: FakeCore, tmp_path: Path) -> None:
    bridge = make_bridge(fake, tmp_path, machines=[])
    try:
        assert bridge.remote_results() == {}
        assert "remote_trust" not in await bridge.doctor()
    finally:
        await bridge.close()


async def test_delisted_machine_with_open_receipts_is_warned_about(
    fake: FakeCore, tmp_path: Path
) -> None:
    """A restart without the machine names each open receipt it strands; doctor stays healthy."""
    first = make_bridge(fake, tmp_path)
    try:
        done = await remote_task(first, "finished")
        fake.put_file(path_of(done), valid(done))
        assert (await first.get_task(done["id"]))["status"] == "completed"
        task = await remote_task(first)
    finally:
        await first.close()
    second = make_bridge(fake, tmp_path, machines=[])
    try:
        report = second.remote_results()
        assert report["remote_unlisted_receipts"] == [
            {"id": task["id"], "machine": MACHINE, "status": "submitted"}
        ]
        assert "answerback task <id> --wait <seconds>" in report["remote_unlisted_warning"]
        assert "remote_trust" not in report
        doctor = await second.doctor()
        assert doctor["remote_unlisted_receipts"] == report["remote_unlisted_receipts"]
        service = Service(tmp_path / "config.json")
        service.bridge = second
        assert service.status()["remote_unlisted_receipts"] == report["remote_unlisted_receipts"]
    finally:
        await second.close()
    third = make_bridge(fake, tmp_path)
    try:
        assert "remote_unlisted_receipts" not in third.remote_results()
    finally:
        await third.close()


# The documents quote the one statement verbatim.


@pytest.mark.parametrize(
    "document", ["README.md", "SECURITY.md", "docs/RECIPIENTS.md", "docs/HARNESSES.md"]
)
def test_documents_quote_the_trust_statement_verbatim(document: str) -> None:
    text = re.sub(r"\s+", " ", (ROOT / document).read_text().replace("\n> ", " "))
    assert REMOTE_TRUST_STATEMENT in text


def test_live_test_guide_has_the_remote_qualification_checklist() -> None:
    text = (ROOT / "docs/LIVE-TEST.md").read_text()
    assert "## Remote result-file qualification" in text
    for case in ("wrong token", "wrong task ID", "non-UTF-8", "hc_send_machines"):
        assert case in text


def test_live_test_checklist_starts_a_fresh_terminal_before_each_later_send() -> None:
    # A terminal takes one assignment by default (`done` after a turn and
    # `hc_send_to_done` off), so every send after the first needs a new terminal.
    text = (ROOT / "docs/LIVE-TEST.md").read_text()
    section = text.split("## Remote result-file qualification", 1)[1].split("\n## ", 1)[0]
    section = re.sub(r"\s+", " ", section).lower()
    fresh = "start a fresh idle terminal"
    chunks = section.split("send with `peer_submit`")
    sends = len(chunks) - 1
    assert sends >= 3
    assert section.count(fresh) >= sends - 1
    for index in range(1, sends):
        assert fresh in chunks[index], f"send {index + 1} has no fresh terminal before it"
