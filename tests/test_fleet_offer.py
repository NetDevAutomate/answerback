"""fleet: an unexpected error while a document is built is a failed publication."""

import asyncio
from typing import Any

import pytest
from test_fleet_fakes import Fleet, linux, writer

from answerback_bridge.fleet import document


@pytest.mark.parametrize("step", ["build", "content_key", "encode"])
async def test_an_error_while_the_document_is_made_is_a_failed_publication(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    w, clock, sink = writer(Fleet({"local": linux(), "worker-a": linux()}))
    real = getattr(document, step)
    calls: list[float] = []
    failing = {"at": 15.0}

    def maybe_broken(*args: Any, **kwargs: Any) -> Any:
        if step == "build":
            calls.append(clock.now_mono)
        if clock.now_mono == failing["at"]:
            raise KeyError("surprise")
        return real(*args, **kwargs)

    monkeypatch.setattr(document, step, maybe_broken)
    stop = asyncio.Event()
    clock.stop, clock.until = stop, 31.0
    await w.serve(stop)
    # Published at 0; the attempt at 15 failed; the next comes 5 s later, at 20.
    assert sink.at[:2] == [0, 20]
    if step == "build":
        assert calls[:3] == [0, 15, 20]
    errors = [e for e in sink.docs[1]["errors"] if e["where"] == "publish"]
    assert len(errors) == 1 and "KeyError" in errors[0]["text"]
    assert sink.at[-1] >= 30  # the writer goes on


async def test_a_build_error_publishes_nothing_and_once_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w, _, sink = writer(Fleet({"local": linux()}))

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise KeyError("surprise")

    monkeypatch.setattr(document, "build", broken)
    assert await w.once() is False
    assert sink.docs == []
    assert [e["where"] for e in w.errors] == ["service", "publish"]
    assert "KeyError" in w.errors[-1]["text"]
