"""fleet: an answer that came and cannot be used is called so, not "did not answer"."""

from typing import Any

import pytest
from test_fleet_fakes import FakeClock, Fleet, Service, linux, writer

from answerback_bridge.fleet import loop
from answerback_bridge.process import BridgeError


def _up() -> Service:
    s = Service()
    s.up = True
    return s


def _writer(s: Any) -> tuple[loop.Writer, FakeClock, Any]:
    return writer(Fleet({"local": linux()}), s)


def _service_errors(w: loop.Writer) -> list[str]:
    return [e["text"] for e in w.errors if e["where"] == "service"]


async def test_a_status_with_an_unusable_count_is_an_answer_that_cannot_be_used() -> None:
    s = _up()
    s.status["tasks"] = {"uncertain": "many"}
    w, _, sink = _writer(s)
    await w.once()
    text = "service answer cannot be used: status answer has an unusable count for uncertain"
    assert sink.docs[-1]["health"]["error"] == text
    assert sink.docs[-1]["receipts"]["error"] == text
    assert _service_errors(w) == [text]


async def test_an_unusable_list_of_receipts_is_an_answer_that_cannot_be_used() -> None:
    s = _up()
    s.tasks = "unavailable"  # type: ignore[assignment]
    w, _, sink = _writer(s)
    await w.once()
    text = "service answer cannot be used: list of receipts cannot be used"
    assert sink.docs[-1]["receipts"]["error"] == text
    assert _service_errors(w) == [text]


async def test_a_service_that_does_not_answer_is_still_called_so() -> None:
    w, _, sink = _writer(Service())
    await w.once()
    assert sink.docs[-1]["health"]["error"] == (
        "service did not answer: Bridge is stopped. Run answerback start first."
    )


async def test_an_error_answer_of_the_service_is_called_an_error_answer() -> None:
    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise BridgeError("Bridge request failed; inspect service logs and the task receipt.")

    w, _, sink = _writer(call)
    await w.once()
    assert sink.docs[-1]["health"]["error"] == (
        "service answered with an error: "
        "Bridge request failed; inspect service logs and the task receipt."
    )


async def test_an_unexpected_error_in_the_service_step_is_no_usable_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise KeyError("surprise")

    monkeypatch.setattr(loop, "ask", broken)
    w, _, sink = _writer(_up())
    await w.once()
    text = "service answer cannot be used: unexpected KeyError"
    assert sink.docs[-1]["health"]["error"] == text
    assert _service_errors(w) == [text]
    assert not any("in time" in t for t in _service_errors(w))


# ---------------------------------------------------------------- v4: a call never started


async def test_a_status_call_that_was_never_started_says_no_time_left() -> None:
    import asyncio

    from answerback_bridge.fleet import service

    s = _up()
    answer = await service.ask(s, timeout=2.0, deadline=asyncio.get_running_loop().time())
    assert s.methods == []
    assert answer.status is None and answer.status_error == "no time left in the cycle"


async def test_a_status_call_that_got_no_answer_did_not_answer_in_time() -> None:
    import asyncio

    from answerback_bridge.fleet import service

    async def hang(method: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    answer = await service.ask(hang, timeout=0.05)
    assert answer.status_error == "service did not answer in time"


async def test_a_client_not_built_in_time_did_not_answer_in_time() -> None:
    import threading

    from answerback_bridge.fleet import service

    release = threading.Event()
    remote = service.Remote(lambda: release.wait(5))
    try:
        answer = await service.ask(remote, timeout=0.05)
    finally:
        release.set()
    assert answer.status_error == "service did not answer in time"


async def test_a_status_call_never_started_in_a_cycle_gives_one_no_time_entry() -> None:
    import time

    s = _up()

    class Blocking:
        async def prepare(self, timeout: float) -> None:  # noqa: ASYNC109
            time.sleep(0.15)  # noqa: ASYNC251 - blocks past the deadline, on purpose

        async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            return await s(method, params)

    w, _, sink = writer(Fleet({"local": linux()}), timings=loop.Timings(cycle=0.12, reserve=0.02))
    w.call = Blocking()
    await w.once()
    assert s.methods == []
    assert sink.docs[-1]["health"]["error"] == "no time left in the cycle"
    assert [e["text"] for e in w.errors].count("no time left in the cycle") == 1
