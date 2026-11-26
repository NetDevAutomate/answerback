"""T-227 native matchers: a bridge-launched worker's throttle becomes a signal, by class.

Since the gate merge a throttled attempt is relaunched once after the hold, so a worker
whose every attempt is throttled produces two signals (one per launch) and ends with
`THROTTLED_TWICE`. The stores here run on a fake clock: no test sleeps the real hold.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pressure_fakes import ClockedStore
from test_native_workers import FakeTransport, finish
from test_native_workers import setup as setup  # noqa: F401  (the fixture, reused)
from test_workers import FakeACP, wait_status
from test_workers import manager as manager  # noqa: F401  (the fixture, reused)

from answerback_bridge.native_protocol import NativeResult
from answerback_bridge.native_workers import _throttle_match as native_match
from answerback_bridge.pressure import PressureStore
from answerback_bridge.process import BridgeError
from answerback_bridge.workers import THROTTLED_TWICE
from answerback_bridge.workers import _throttle_match as kiro_match

LIMITER = (
    "You're sending requests faster than your rate limit allows. Please wait a moment "
    "and try again, or run fewer sessions at the same time."
)
CLAUDE_429 = {
    "protocol": "claude-sdk-stream-json",
    "api_error_status": 429,
    "errors": ["API Error: 429 Too Many Requests"],
    "is_error": True,
}
MANTLE = {
    "protocol": "codex-app-server-v2",
    "native_status": "failed",
    "error": {"message": "stream error (invalid_request_error): Mantle rejected the request"},
}
# What one relaunch adds to a receipt's metadata, and nothing else.
RELAUNCH_METADATA = {"resumed_after_backoff", "provider_backoff_until", "throttled_turn_id"}


def recorded(store: PressureStore) -> list[tuple[str, str, str, str]]:
    return [
        (row["provider"], row["harness"], row["source"], row["signature"])
        for row in reversed(store.signals())
    ]


async def run_once(workers: Any, request: str, message: str = "Do the work") -> dict[str, Any]:
    receipt = await workers.submit("test", message, request)
    return await finish(workers, receipt["id"])


async def test_claude_429_evidence_is_a_bedrock_signal_per_launch_and_only_the_relaunch_differs(
    setup: Any, tmp_path: Path
) -> None:
    _, _, _, workers = setup
    FakeTransport.result = NativeResult("owned", "failed", None, "turn-1", evidence=CLAUDE_429)
    baseline = await run_once(workers, "without-pressure")
    assert len(FakeTransport.opened) == 1
    store = ClockedStore(tmp_path / "pressure.sqlite")
    workers.pressure = store
    observed = await run_once(workers, "with-pressure")
    # The first launch's signal holds bedrock for 60 s; the relaunch waits that out on the
    # fake clock and is throttled again, so there is one signal per launch.
    assert recorded(store) == [("bedrock", "codex", "native_evidence", "bedrock.http_429")] * 2
    assert store.slept == [60.0]
    assert len(FakeTransport.opened) == 3
    # A signal after the hold ended keeps the level (no escalation without overlap).
    assert store.status("bedrock").level == 1 and store.status("kiro").level == 0
    assert observed["status"] == baseline["status"] == "failed"
    assert baseline["detail"] != THROTTLED_TWICE and observed["detail"] == THROTTLED_TWICE
    assert set(observed["metadata"]) == set(baseline["metadata"]) | RELAUNCH_METADATA
    assert observed["metadata"]["resumed_after_backoff"] is True
    assert observed["metadata"]["throttled_turn_id"] == "turn-1"
    assert observed["metadata"]["native_evidence"] == CLAUDE_429


async def test_codex_mantle_error_is_the_mantle_signature(setup: Any, tmp_path: Path) -> None:
    _, _, _, workers = setup
    workers.pressure = store = ClockedStore(tmp_path / "pressure.sqlite")
    FakeTransport.result = NativeResult("owned", "failed", None, "turn-1", evidence=MANTLE)
    done = await run_once(workers, "mantle")
    assert (done["status"], done["detail"]) == ("failed", THROTTLED_TWICE)
    assert recorded(store) == [("bedrock", "codex", "native_evidence", "mantle.stream_error")] * 2
    assert done["metadata"]["resumed_after_backoff"] is True


@pytest.mark.parametrize("reviewed", [True, False])
async def test_a_bridge_error_is_matched_and_any_other_error_is_not(
    setup: Any, tmp_path: Path, reviewed: bool
) -> None:
    _, _, _, workers = setup
    workers.pressure = store = ClockedStore(tmp_path / "pressure.sqlite")
    text = "Codex failed: ThrottlingException (rate exceeded)"
    FakeTransport.failure = BridgeError(text) if reviewed else RuntimeError(text)
    done = await run_once(workers, "failing")
    assert done["status"] == "uncertain"
    if reviewed:
        # Matched on the error seam: relaunched once, throttled again, two signals.
        assert recorded(store) == [("bedrock", "codex", "native_error", "bedrock.throttling")] * 2
        assert done["detail"] == THROTTLED_TWICE
        assert done["metadata"]["resumed_after_backoff"] is True
        assert len(FakeTransport.opened) == 2
    else:
        assert recorded(store) == []
        assert done["detail"] == f"RuntimeError: {text}"
        assert "resumed_after_backoff" not in done["metadata"]
        assert len(FakeTransport.opened) == 1


async def test_a_completed_turn_without_a_signature_is_no_signal(
    setup: Any, tmp_path: Path
) -> None:
    _, _, _, workers = setup
    workers.pressure = store = PressureStore(tmp_path / "pressure.sqlite")
    assert (await run_once(workers, "fine"))["status"] == "completed"
    assert recorded(store) == [] and store.snapshot()["bedrock:default"]["level"] == 0


def test_the_native_helper_reads_each_evidence_shape() -> None:
    assert native_match(None, None, harness="claude") is None
    assert native_match({"api_error_status": 500}, None, harness="claude") is None
    match = native_match(CLAUDE_429, None, harness="claude", hints={"bedrock": "acct"})
    assert match is not None and (match.account_hint, match.signature) == (
        "acct",
        "bedrock.http_429",
    )
    errors = {"errors": ["first", "TooManyRequestsException: slow down"]}
    assert native_match(errors, None, harness="grok").signature == "bedrock.throttling"  # type: ignore[union-attr]
    as_text = {"error": "stream error (invalid_request_error): Mantle"}
    assert native_match(as_text, None, harness="codex").signature == "mantle.stream_error"  # type: ignore[union-attr]
    brief = "Fix the ThrottlingException handler"
    assert native_match({"errors": [brief]}, None, harness="claude", brief=brief) is None


class LimitedACP(FakeACP):
    answer = LIMITER
    stop_reason = "end_turn"
    then: list[tuple[str, str]] = []  # Scripted (answer, stop_reason) for later prompts.

    async def prompt(self, session_id: str, message: str) -> Any:
        FakeACP.sends += 1
        result = SimpleNamespace(
            text=LimitedACP.answer, stop_reason=LimitedACP.stop_reason, session_id=session_id
        )
        if LimitedACP.then:
            LimitedACP.answer, LimitedACP.stop_reason = LimitedACP.then.pop(0)
        return result


@pytest.fixture
def limited(manager: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    monkeypatch.setattr("codex_herdr_bridge.workers.KiroACP", LimitedACP)
    LimitedACP.answer, LimitedACP.stop_reason, LimitedACP.then = LIMITER, "end_turn", []
    manager.pressure = ClockedStore(tmp_path / "pressure.sqlite")
    return manager


async def test_kiro_limiter_answer_on_end_turn_is_a_kiro_signal(limited: Any) -> None:
    FakeACP.sends = 0
    LimitedACP.then = [("Reviewed.", "end_turn")]
    receipt = await limited.submit("reviewer", "Review", "limited")
    finished = await wait_status(limited.store, receipt["id"], "completed")
    # The limiter's own message is a signal and ends the attempt; the relaunch after the
    # hold (on the fake clock) is the receipt's result.
    assert finished["result"] == "Reviewed."
    assert finished["metadata"]["resumed_after_backoff"] is True
    assert FakeACP.sends == 2 and limited.pressure.slept == [60.0]
    assert recorded(limited.pressure) == [("kiro", "kiro", "acp_text", "kiro.rate_limit")]
    assert limited.pressure.status("kiro").level == 1
    await limited.close()


async def test_kiro_answers_that_discuss_the_limiter_are_not_signals(limited: Any) -> None:
    LimitedACP.answer = "Implemented the matcher.\n" + LIMITER + "\n" + "details " * 200
    receipt = await limited.submit("reviewer", "Review", "long")
    await wait_status(limited.store, receipt["id"], "completed")
    LimitedACP.answer = LIMITER
    quoted = await limited.submit("reviewer", f"Match this text:\n{LIMITER}", "quoted")
    await wait_status(limited.store, quoted["id"], "completed")
    assert recorded(limited.pressure) == []
    await limited.close()


async def test_kiro_stopped_turn_matches_any_kiro_signature(limited: Any) -> None:
    FakeACP.sends = 0
    LimitedACP.answer, LimitedACP.stop_reason = "ThrottlingException: rate exceeded", "refusal"
    receipt = await limited.submit("reviewer", "Review", "stopped")
    failed = await wait_status(limited.store, receipt["id"], "failed")
    # Every attempt is throttled: one signal per launch, then no third launch.
    assert recorded(limited.pressure) == [("kiro", "kiro", "acp_text", "bedrock.throttling")] * 2
    assert failed["detail"] == THROTTLED_TWICE and FakeACP.sends == 2
    assert limited.pressure.slept == [60.0]
    await limited.close()


def test_the_kiro_helper_needs_the_limiter_or_an_abnormal_stop() -> None:
    assert kiro_match(None, "end_turn") is None
    assert kiro_match("HTTP 429 error", "end_turn") is None
    assert kiro_match("HTTP 429 error", "max_tokens").signature == "bedrock.http_429"  # type: ignore[union-attr]
    assert kiro_match(LIMITER, "end_turn", account_hint="w").account_hint == "w"  # type: ignore[union-attr]
