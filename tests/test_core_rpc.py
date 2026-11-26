"""Tests for the ``hc connect`` session and document store (T-87).

A fake ``hc connect`` child stands in for Herdr Core. It speaks the plain
line mode observed on hc 0.1.21: one JSON-RPC request per stdin line, one
response per stdout line. Per-method misbehaviour is scripted through a
``modes.json`` file next to the fake binary.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.process import ChildProcess, stop_child
from answerback_bridge.providers.herdr import core_rpc
from answerback_bridge.providers.herdr.core_rpc import (
    MAX_DOC_BYTES,
    CoreCallTimeout,
    CoreErrorReply,
    CoreProtocolError,
    CoreSession,
    CoreSessionClosed,
    DocConflict,
    DocRefused,
    DocStore,
    validate_doc_name,
)

# ---------------------------------------------------------------- fixtures

FAKE_HC = """\
import json, os, pathlib, sys, time
d = pathlib.Path({directory!r})
n = len(list(d.glob("started-*")))
(d / f"started-{{n}}").write_text(
    json.dumps({{"pid": os.getpid(), "argv": sys.argv[1:],
                "terminal_env": "HC_TERMINAL_ID" in os.environ,
                "session": os.environ.get("HC_SESSION")}})
)
modes = json.loads((d / "modes.json").read_text()) if (d / "modes.json").exists() else {{}}
docs = {{}}
log = open(d / "requests.jsonl", "a")
out = sys.stdout.buffer

def send(obj):
    out.write(json.dumps(obj).encode() + b"\\n")
    out.flush()

for raw in sys.stdin.buffer:
    request = json.loads(raw)
    log.write(json.dumps(request) + "\\n")
    log.flush()
    rid, method, params = request.get("id"), request["method"], request.get("params", {{}})
    mode = modes.get(method, "normal")
    if mode == "silent":
        continue
    if mode == "exit":
        sys.exit(3)
    if mode == "partial_exit":
        out.write(b'{{"id":'); out.flush(); sys.exit(0)
    if mode == "malformed":
        out.write(b"{{not json\\n"); out.flush(); continue
    if mode == "not_object":
        send([1, 2]); continue
    if mode == "oversized":
        out.write(b'"' + b"x" * 4096 + b'"\\n'); out.flush(); continue
    if mode == "unknown_id":
        send({{"jsonrpc": "2.0", "id": 999, "result": {{}}}}); continue
    if mode == "previous_id":
        send({{"jsonrpc": "2.0", "id": rid - 1, "result": {{}}}}); continue
    if mode == "both":
        error = {{"code": 1, "message": "x"}}
        send({{"jsonrpc": "2.0", "id": rid, "result": {{}}, "error": error}})
        continue
    if mode == "no_jsonrpc":
        send({{"id": rid, "result": {{}}}}); continue
    if mode == "core_request":
        send({{"jsonrpc": "2.0", "id": 77, "method": "ping", "params": {{}}}}); continue
    if mode == "notify_first":
        send({{"jsonrpc": "2.0", "method": "heartbeat", "params": {{"seq": 1}}}})
    if mode == "error":
        send({{"jsonrpc": "2.0", "id": rid, "error": {{"code": -32000,
              "message": "boom\\x1b[31m", "data": {{"kind": "unavailable"}}}}}})
        continue
    if mode == "bad_version":
        send({{"jsonrpc": "2.0", "id": rid, "result": {{"value": None, "version": "1"}}}})
        continue
    if mode == "list_value":
        send({{"jsonrpc": "2.0", "id": rid, "result": {{"value": [1], "version": 3}}}})
        continue
    if mode.startswith("huge_version:"):
        huge = int(mode.split(":", 1)[1])
        send({{"jsonrpc": "2.0", "id": rid, "result": {{"value": None, "version": huge}}}})
        continue
    if method == "hello":
        framed = mode == "framed"
        generation = 2 if mode == "generation" else 1
        send({{"jsonrpc": "2.0", "id": rid, "result": {{
            "core": {{"id": "c0ffee", "name": "fake"}}, "features": ["framed", "events"],
            "framed": framed, "generation": generation, "version": "0.1.21",
            "methods": ["hello", "rpc.discover", "doc.get", "doc.set", "terminal.send"]}}}})
    elif method == "rpc.discover":
        send({{"jsonrpc": "2.0", "id": rid, "result": {{"openrpc": "1.3.2", "methods": []}}}})
    elif method == "doc.get":
        value, version = docs.get(params["name"], (None, 0))
        send({{"jsonrpc": "2.0", "id": rid, "result": {{"value": value, "version": version}}}})
    elif method == "doc.set":
        value, version = docs.get(params["name"], (None, 0))
        if params["version"] != version:
            send({{"jsonrpc": "2.0", "id": rid, "error": {{"code": -32000,
                  "message": f"document is at version {{version}}, not {{params['version']}}",
                  "data": {{"kind": "conflict"}}}}}})
        else:
            docs[params["name"]] = (params["value"], version + 1)
            send({{"jsonrpc": "2.0", "id": rid, "result": {{"version": version + 1}}}})
    else:
        send({{"jsonrpc": "2.0", "id": rid, "error": {{"code": -32601,
              "message": "unknown method", "data": {{"kind": "unknown_method"}}}}}})
"""


class FakeHc:
    """A fake ``hc`` whose ``connect`` child is scripted per method."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.binary = directory / "hc"
        self.binary.write_text(f"#!{sys.executable}\n" + FAKE_HC.format(directory=str(directory)))
        self.binary.chmod(0o755)

    def modes(self, **modes: str) -> None:
        (self.directory / "modes.json").write_text(json.dumps(modes))

    def starts(self) -> list[dict[str, Any]]:
        paths = sorted(self.directory.glob("started-*"), key=lambda p: int(p.name[8:]))
        return [json.loads(path.read_text()) for path in paths]

    def requests(self) -> list[dict[str, Any]]:
        path = self.directory / "requests.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def pid(self) -> int:
        (start,) = self.starts()
        return int(start["pid"])


@pytest.fixture
def fake_hc(tmp_path: Path) -> FakeHc:
    return FakeHc(tmp_path)


def session(fake: FakeHc, **options: Any) -> CoreSession:
    return CoreSession(str(fake.binary), **options)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
async def started(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[ChildProcess]]:
    """The children a session started, recorded where the session starts them.

    The start record of the fake is written by the fake itself, so a child that
    was stopped before its interpreter reached that line leaves none. This list
    is filled on the session's side instead: it wraps the ``spawn_owned`` that
    ``core_rpc`` calls and keeps what it returned, so it needs nothing from the
    child. A child that a failing test left alive is stopped here.
    """
    children: list[ChildProcess] = []
    spawn = core_rpc.spawn_owned

    async def recording(*args: Any, **kwargs: Any) -> ChildProcess:
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(core_rpc, "spawn_owned", recording)
    yield children
    for child in children:
        if alive(child.pid):
            await stop_child(child, cleanup_seconds=2)


async def eventually(check: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- CoreSession


async def test_handshake_uses_plain_mode_and_scrubbed_env(
    fake_hc: FakeHc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HC_TERMINAL_ID", "t9")
    monkeypatch.setenv("HC_SESSION", "outer")
    async with session(fake_hc) as core:
        assert core.generation == 1
        assert core.core_version == "0.1.21"
        assert "doc.set" in core.methods
    (start,) = fake_hc.starts()
    assert start["argv"] == ["connect"]
    assert start["terminal_env"] is False
    assert start["session"] is None
    (hello,) = fake_hc.requests()
    assert hello == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "hello",
        "params": {"generation": 1, "framed": False},
    }
    assert not alive(start["pid"])


async def test_named_session_is_passed_through_env(fake_hc: FakeHc) -> None:
    async with session(fake_hc, session="work"):
        pass
    assert fake_hc.starts()[0]["session"] == "work"


@pytest.mark.parametrize("mode", ["framed", "generation"])
async def test_hello_refuses_framed_or_other_generation(fake_hc: FakeHc, mode: str) -> None:
    fake_hc.modes(hello=mode)
    with pytest.raises(CoreProtocolError):
        await session(fake_hc).start()
    assert not alive(fake_hc.pid())


async def test_call_matches_response_by_id(fake_hc: FakeHc) -> None:
    async with session(fake_hc) as core:
        assert await core.call("rpc.discover", {}, timeout=5) == {
            "openrpc": "1.3.2",
            "methods": [],
        }
        assert await core.call("doc.get", {"name": "answerback-x"}, timeout=5) == {
            "value": None,
            "version": 0,
        }
    assert [request["id"] for request in fake_hc.requests()] == [1, 2, 3]


async def test_notification_between_responses_is_ignored(fake_hc: FakeHc) -> None:
    fake_hc.modes(**{"rpc.discover": "notify_first"})
    async with session(fake_hc) as core:
        result = await core.call("rpc.discover", {}, timeout=5)
    assert result["openrpc"] == "1.3.2"


@pytest.mark.parametrize("mode", ["unknown_id", "previous_id"])
async def test_unknown_or_out_of_order_id_closes_session(fake_hc: FakeHc, mode: str) -> None:
    fake_hc.modes(**{"rpc.discover": mode})
    core = session(fake_hc)
    await core.start()
    with pytest.raises(CoreProtocolError, match="id"):
        await core.call("rpc.discover", {}, timeout=5)
    assert core.closed
    assert not alive(fake_hc.pid())
    with pytest.raises(CoreSessionClosed):
        await core.call("rpc.discover", {}, timeout=5)


@pytest.mark.parametrize(
    "mode", ["malformed", "not_object", "oversized", "both", "no_jsonrpc", "core_request"]
)
async def test_bad_line_closes_session_with_typed_error(fake_hc: FakeHc, mode: str) -> None:
    fake_hc.modes(**{"rpc.discover": mode})
    core = session(fake_hc, max_line_bytes=1024)
    await core.start()
    with pytest.raises(CoreProtocolError):
        await core.call("rpc.discover", {}, timeout=5)
    assert core.closed
    assert not alive(fake_hc.pid())


async def test_timeout_closes_session(fake_hc: FakeHc) -> None:
    fake_hc.modes(**{"rpc.discover": "silent"})
    core = session(fake_hc)
    await core.start()
    with pytest.raises(CoreCallTimeout):
        await core.call("rpc.discover", {}, timeout=0.2)
    assert core.closed
    assert not alive(fake_hc.pid())


async def test_hello_timeout_leaves_no_child(fake_hc: FakeHc, started: list[ChildProcess]) -> None:
    # The fake may be stopped before it wrote its start record, so the child is
    # taken from the session's own spawn, not from ``fake_hc.pid()``.
    fake_hc.modes(hello="silent")
    with pytest.raises(CoreCallTimeout):
        await session(fake_hc, hello_timeout=0.2).start()
    (child,) = started
    assert not alive(child.pid)


@pytest.mark.parametrize("mode", ["exit", "partial_exit"])
async def test_child_exit_mid_call(fake_hc: FakeHc, mode: str) -> None:
    fake_hc.modes(**{"doc.get": mode})
    core = session(fake_hc)
    await core.start()
    with pytest.raises(CoreSessionClosed):
        await core.call("doc.get", {"name": "answerback-x"}, timeout=5)
    assert core.closed
    assert not alive(fake_hc.pid())


async def test_cancel_mid_call_leaves_no_child(fake_hc: FakeHc) -> None:
    fake_hc.modes(**{"rpc.discover": "silent"})
    core = session(fake_hc)
    await core.start()
    pid = fake_hc.pid()
    task = asyncio.create_task(core.call("rpc.discover", {}, timeout=60))
    await eventually(lambda: len(fake_hc.requests()) == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert core.closed
    assert not alive(pid)


async def test_cancel_during_start_leaves_no_child(fake_hc: FakeHc) -> None:
    fake_hc.modes(hello="silent")
    core = session(fake_hc, hello_timeout=60)
    task = asyncio.create_task(core.start())
    await eventually(lambda: len(fake_hc.requests()) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(fake_hc.pid())


async def test_close_is_idempotent(fake_hc: FakeHc) -> None:
    core = session(fake_hc)
    await core.start()
    await core.close()
    await core.close()
    assert not alive(fake_hc.pid())


async def test_error_reply_is_typed_and_keeps_session(fake_hc: FakeHc) -> None:
    fake_hc.modes(**{"rpc.discover": "error"})
    async with session(fake_hc) as core:
        with pytest.raises(CoreErrorReply) as caught:
            await core.call("rpc.discover", {}, timeout=5)
        assert caught.value.kind == "unavailable"
        assert caught.value.code == -32000
        assert "\x1b" not in str(caught.value)
        assert not core.closed
        assert (await core.call("doc.get", {"name": "answerback-x"}, timeout=5))["version"] == 0


async def test_methods_outside_allow_list_never_reach_core(fake_hc: FakeHc) -> None:
    async with session(fake_hc) as core:
        for method in ("terminal.send", "hello", "doc.delete"):
            with pytest.raises(ValueError, match="not allowed"):
                await core.call(method, {}, timeout=5)
    assert [request["method"] for request in fake_hc.requests()] == ["hello"]


async def test_concurrent_calls_are_serialised(fake_hc: FakeHc) -> None:
    async with session(fake_hc) as core:
        results = await asyncio.gather(
            *(core.call("doc.get", {"name": f"answerback-{i}"}, timeout=5) for i in range(5))
        )
    assert all(result["version"] == 0 for result in results)


async def test_call_before_start_is_refused(fake_hc: FakeHc) -> None:
    with pytest.raises(CoreSessionClosed):
        await session(fake_hc).call("rpc.discover", {}, timeout=5)
    assert fake_hc.starts() == []


def test_constructor_validates_options() -> None:
    with pytest.raises(ValueError):
        CoreSession("")
    with pytest.raises(ValueError):
        CoreSession("hc", session="")
    with pytest.raises(ValueError):
        CoreSession("hc", max_line_bytes=0)


# ---------------------------------------------------------------- DocStore


async def test_docstore_round_trip_and_conflict(fake_hc: FakeHc) -> None:
    async with session(fake_hc) as core:
        docs = DocStore(core)
        assert await docs.get("answerback-test") == (None, 0)
        assert await docs.set("answerback-test", {"n": 1}, 0) == 1
        assert await docs.get("answerback-test") == ({"n": 1}, 1)
        with pytest.raises(DocConflict) as caught:
            await docs.set("answerback-test", {"n": 2}, 0)
        assert caught.value.kind == "conflict"
        assert await docs.delete("answerback-test", 1) == 2
        assert await docs.get("answerback-test") == (None, 2)
    sets = [r["params"] for r in fake_hc.requests() if r["method"] == "doc.set"]
    assert sets[-1] == {"name": "answerback-test", "value": None, "version": 1}
    assert all("on" not in params for params in sets)


@pytest.mark.parametrize(
    "name",
    [
        "herdr-app",
        "answerbac",
        "Answerback-x",
        "answerback x",
        "answerback/x",
        "answerback-" + "a" * 22,
        "",
        "answerback\n",
    ],
)
async def test_docstore_refuses_bad_names(fake_hc: FakeHc, name: str) -> None:
    async with session(fake_hc) as core:
        docs = DocStore(core)
        with pytest.raises(DocRefused):
            await docs.get(name)
        with pytest.raises(DocRefused):
            await docs.set(name, {}, 0)
        with pytest.raises(DocRefused):
            await docs.delete(name, 1)
    assert [request["method"] for request in fake_hc.requests()] == ["hello"]


def test_name_pattern_accepts_selftest_shape() -> None:
    assert validate_doc_name("answerback") == "answerback"
    assert validate_doc_name("answerback-selftest-0123456789ab") == (
        "answerback-selftest-0123456789ab"
    )
    with pytest.raises(DocRefused):
        validate_doc_name(7)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        [1, 2],
        "text",
        None,
        3,
        {"big": "x" * MAX_DOC_BYTES},
        {"nan": float("nan")},
        {1: "non-string key"},
        {"set": {1, 2}},
    ],
)
async def test_docstore_refuses_bad_values(fake_hc: FakeHc, value: Any) -> None:
    async with session(fake_hc) as core:
        with pytest.raises(DocRefused):
            await DocStore(core).set("answerback-test", value, 0)
    assert [request["method"] for request in fake_hc.requests()] == ["hello"]


async def test_docstore_refuses_deep_value(fake_hc: FakeHc) -> None:
    value: dict[str, Any] = {}
    for _ in range(100_000):
        value = {"a": value}
    async with session(fake_hc) as core:
        with pytest.raises(DocRefused):
            await DocStore(core).set("answerback-test", value, 0)


async def test_docstore_accepts_value_at_the_limit(fake_hc: FakeHc) -> None:
    padding = MAX_DOC_BYTES - len('{"p":""}')
    async with session(fake_hc, max_line_bytes=4 * MAX_DOC_BYTES) as core:
        assert await DocStore(core).set("answerback-test", {"p": "x" * padding}, 0) == 1
        with pytest.raises(DocRefused):
            await DocStore(core).set("answerback-test", {"p": "x" * (padding + 1)}, 1)


@pytest.mark.parametrize("version", [-1, True, 2**64, 1.0, "1"])
async def test_docstore_refuses_bad_versions(fake_hc: FakeHc, version: Any) -> None:
    async with session(fake_hc) as core:
        docs = DocStore(core)
        with pytest.raises(DocRefused):
            await docs.set("answerback-test", {}, version)
        with pytest.raises(DocRefused):
            await docs.delete("answerback-test", version)


@pytest.mark.parametrize("mode", ["bad_version", "list_value"])
async def test_docstore_rejects_malformed_results(fake_hc: FakeHc, mode: str) -> None:
    fake_hc.modes(**{"doc.get": mode})
    async with session(fake_hc) as core:
        with pytest.raises(CoreProtocolError):
            await DocStore(core).get("answerback-test")


@pytest.mark.parametrize("version", [2**64, 2**84, 10**400], ids=["2^64", "2^84", "10^400"])
@pytest.mark.parametrize("method", ["doc.get", "doc.set"])
async def test_docstore_rejects_result_version_above_uint64(
    fake_hc: FakeHc, method: str, version: int
) -> None:
    fake_hc.modes(**{method: f"huge_version:{version}"})
    async with session(fake_hc) as core:
        docs = DocStore(core)
        with pytest.raises(CoreProtocolError, match="version"):
            if method == "doc.get":
                await docs.get("answerback-test")
            else:
                await docs.set("answerback-test", {}, 0)


@pytest.mark.parametrize("method", ["doc.get", "doc.set"])
async def test_docstore_accepts_result_version_at_uint64_max(fake_hc: FakeHc, method: str) -> None:
    fake_hc.modes(**{method: f"huge_version:{2**64 - 1}"})
    async with session(fake_hc) as core:
        docs = DocStore(core)
        if method == "doc.get":
            assert await docs.get("answerback-test") == (None, 2**64 - 1)
        else:
            assert await docs.set("answerback-test", {}, 0) == 2**64 - 1


async def test_failed_spawn_leaves_session_closed(tmp_path: Path) -> None:
    core = CoreSession(str(tmp_path / "no-such-hc"))
    with pytest.raises(OSError):
        await core.start()
    assert core.closed
    assert core.close_reason is not None
    assert "start failed" in core.close_reason
    with pytest.raises(CoreSessionClosed):
        await core.call("rpc.discover", {}, timeout=5)
    with pytest.raises(CoreSessionClosed):
        await core.start()
    await core.close()
