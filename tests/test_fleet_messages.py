"""fleet: only text made on purpose enters the document; other exceptions name their type."""

import json
from typing import Any

import pytest
from test_fleet_fakes import Fleet, linux, writer

from answerback_bridge.fleet import probes

MARKER = "private-marker-7f3a"


def _fleet() -> Fleet:
    return Fleet({"local": linux(), "worker-a": linux()})


def _raising(fleet: Fleet, place: str, error: Exception) -> Any:
    async def runner(argv: list[str]) -> tuple[int, str | bytes]:
        where = (
            "first"
            if argv[1:] == ["ls", "--json"]
            else "labelled"
            if "--label" in argv
            else "probe"
            if argv[1:4] == ["exec", "--on", "worker-a"]
            else None
        )
        if where == place:
            raise error
        return await Fleet.__call__(fleet, argv)

    return runner


@pytest.mark.parametrize("place", ["first", "labelled", "probe"])
@pytest.mark.parametrize("kind", [RuntimeError, ValueError])
async def test_the_message_of_an_unexpected_error_never_enters_the_document(
    place: str, kind: type[Exception]
) -> None:
    fleet = _fleet()
    w, _, sink = writer(fleet)
    w.runner = _raising(fleet, place, kind(f"{MARKER} /some/private/path"))
    assert await w.once()
    text = json.dumps(sink.docs[-1])
    assert MARKER not in text
    entries = [e["text"] for e in sink.docs[-1]["errors"] if kind.__name__ in (e["text"] or "")]
    assert len(entries) == 1, sink.docs[-1]["errors"]


def test_the_package_raises_its_own_class_for_a_listing_that_cannot_be_used() -> None:
    for raw in (
        b"x" * (probes.OUTPUT_LIMIT + 1),
        "not json",
        '{"terminals": []}',
        '{"machines": []}',
    ):
        with pytest.raises(probes.ListingError):
            probes.parse_listing(raw)


async def test_an_hc_ls_exit_code_is_a_listing_error_with_its_own_text() -> None:
    fleet = _fleet()
    fleet.ls_fails = True
    w, _, sink = writer(fleet)
    await w.once()
    assert "hc ls --json failed: hc ls exited with 1" in [
        e["text"] for e in sink.docs[-1]["errors"]
    ]


async def test_an_unexpected_error_in_the_coordinators_probe_gives_unmeasured_and_publishes() -> (
    None
):
    fleet = _fleet()

    async def runner(argv: list[str]) -> tuple[int, str | bytes]:
        if argv[0] == "/bin/sh":
            raise RuntimeError(f"{MARKER} in the coordinator")
        return await Fleet.__call__(fleet, argv)

    w, _, sink = writer(fleet)
    w.runner = runner
    assert await w.once()
    doc = sink.docs[-1]
    assert doc["writer"]["mode"] == "unmeasured"
    assert doc["machines"][1]["reading"]["state"] == "ok"
    assert MARKER not in json.dumps(doc)
    named = [e for e in doc["errors"] if "RuntimeError" in (e["text"] or "")]
    assert len(named) == 1
    assert not any(e["where"] == "cycle" for e in doc["errors"])
