"""T-56 review round 7 (finding B1): the shape of the saved lease file.

Every file in this module is JSON text written out by hand, in the shape
`DebugController._save` writes (events/debug.py, the `json.dump` call:
``{"leases": {<scope>: {"started": <number>, "until": <number>}}, "excluded":
[<scope>, ...]}``). None of it is produced by `json.dumps` of a controller's
own state, so a loader that accepts more than the writer writes shows here.
The length bound of a scope (finding B4) is in tests/test_debug_scope_bound.py.

Each test carries one label:

- ``red_at_pretip``: fails at ``7d65216``, the lane's tip before this round.
- ``pins_existing``: passes at ``7d65216`` too; it pins behaviour that had no
  test built from a file.
"""

import json
from pathlib import Path

import pytest

from answerback_bridge.events.debug import MAX_JSON_DEPTH, DebugController, _check_json_depth

NOW = 702.0
# One lease as `on()` stores it (events/debug.py: `{"started": now, "until": until}`).
LEASE = '{"started": 702.0, "until": 1302.0}'
# core.py:285 builds the peer id of a local Core terminal as "hc:<terminal>".
CORE = "hc:t61"
# workers.py:182 reserves a Kiro receipt as "kiro:<alias>:<cwd>".
KIRO = "kiro:work:/tmp/My Project"


def start(directory: Path, text: str) -> DebugController:
    """A bridge start on a runtime directory whose saved file holds ``text``."""
    (directory / "debug").mkdir(parents=True)
    (directory / "debug" / "leases.json").write_text(text)
    return DebugController(directory, clock=lambda: NOW)


def assert_failed_closed(restored: DebugController) -> None:
    assert restored.leases == {}
    assert restored.excluded == set()
    assert restored.error
    assert restored.status()["scopes"] == []
    assert restored.status()["excluded"] == []
    assert restored.until(CORE) is None


def test_the_writer_writes_exactly_the_shape_these_files_have(tmp_path: Path) -> None:
    """The provenance of every file below. Label: pins_existing.

    The saved file is an object with the two keys ``leases`` and ``excluded``;
    a lease is an object with the two keys ``started`` and ``until``; the
    exclusions are a sorted list of strings.
    """
    controller = DebugController(tmp_path, clock=lambda: NOW)
    controller.on("all")
    controller.on(CORE)
    controller.off(KIRO)
    controller.off("fixtureprov:thread-1")
    assert json.loads(controller.path.read_text()) == {
        "leases": {
            "all": {"started": 702.0, "until": 1302.0},
            "hc:t61": {"started": 702.0, "until": 1302.0},
        },
        "excluded": ["fixtureprov:thread-1", "kiro:work:/tmp/My Project"],
    }


def test_a_hand_written_file_in_the_writers_shape_loads(tmp_path: Path) -> None:
    """The control for the refusals below. Label: pins_existing."""
    restored = start(
        tmp_path,
        '{"leases": {"all": ' + LEASE + ', "hc:t61": ' + LEASE + "}, "
        '"excluded": ["fixtureprov:thread-1", "kiro:work:/tmp/My Project"]}',
    )
    assert restored.error is None
    assert restored.leases == {
        "all": {"started": 702.0, "until": 1302.0},
        "hc:t61": {"started": 702.0, "until": 1302.0},
    }
    assert restored.excluded == {"fixtureprov:thread-1", KIRO}
    assert restored.until(CORE) == 1302.0
    assert restored.until(KIRO) is None


BOTH = '{"all": ' + LEASE + ', "hc:t61": ' + LEASE + "}"

# Files the writer cannot write and that the loader at 7d65216 accepted, with
# `error` unset. The first is the reviewer's file (finding B1): an object where
# the list of exclusions belongs, whose keys were then read as exclusions.
WRONG_SHAPE_ACCEPTED_AT_PRETIP = {
    "exclusions-an-object": '{"leases": ' + BOTH + ', "excluded": {"' + KIRO + '": false}}',
    "exclusions-an-empty-object": '{"leases": ' + BOTH + ', "excluded": {}}',
    "exclusions-an-empty-string": '{"leases": ' + BOTH + ', "excluded": ""}',
    "lease-with-a-third-field": (
        '{"leases": {"hc:t61": {"started": 702.0, "until": 1302.0, "note": "x"}}, "excluded": []}'
    ),
    "file-with-a-third-field": '{"leases": ' + BOTH + ', "excluded": [], "extra": 1}',
}


@pytest.mark.parametrize(
    "text", WRONG_SHAPE_ACCEPTED_AT_PRETIP.values(), ids=WRONG_SHAPE_ACCEPTED_AT_PRETIP.keys()
)
def test_a_field_of_the_wrong_shape_clears_every_lease(tmp_path: Path, text: str) -> None:
    """B1: the shape of every stored field is checked. Label: red_at_pretip, every row."""
    assert_failed_closed(start(tmp_path, text))


# Wrong shapes that 7d65216 already refused, one for each stored field and each
# JSON type it must not have. No test built such a file by hand before.
WRONG_SHAPE_REFUSED_AT_PRETIP = {
    "file-a-list": "[]",
    "file-null": "null",
    "file-a-string": '"leases"',
    "file-a-number": "7",
    "file-not-json": '{"leases": {}, "excluded": [',
    "file-empty": "",
    "no-leases": '{"excluded": []}',
    "no-exclusions": '{"leases": ' + BOTH + "}",
    "leases-null": '{"leases": null, "excluded": []}',
    "leases-a-list": '{"leases": [], "excluded": []}',
    "leases-a-string": '{"leases": "", "excluded": []}',
    "leases-a-number": '{"leases": 0, "excluded": []}',
    "leases-true": '{"leases": true, "excluded": []}',
    "exclusions-null": '{"leases": ' + BOTH + ', "excluded": null}',
    "exclusions-a-number": '{"leases": ' + BOTH + ', "excluded": 0}',
    "exclusions-true": '{"leases": ' + BOTH + ', "excluded": true}',
    "exclusions-a-string": '{"leases": ' + BOTH + ', "excluded": "hc:t62"}',
    "exclusions-an-object-with-a-bad-key": '{"leases": ' + BOTH + ', "excluded": {"all": 1}}',
    "exclusion-an-object": '{"leases": ' + BOTH + ', "excluded": [{"hc:t62": false}]}',
    "lease-null": '{"leases": {"hc:t61": null}, "excluded": []}',
    "lease-a-list": '{"leases": {"hc:t61": [702.0, 1302.0]}, "excluded": []}',
    "lease-a-string": '{"leases": {"hc:t61": "on"}, "excluded": []}',
    "lease-a-number": '{"leases": {"hc:t61": 1302.0}, "excluded": []}',
    "lease-empty": '{"leases": {"hc:t61": {}}, "excluded": []}',
    "lease-without-until": '{"leases": {"hc:t61": {"started": 702.0}}, "excluded": []}',
    "lease-without-started": '{"leases": {"hc:t61": {"until": 1302.0}}, "excluded": []}',
    "started-a-string": (
        '{"leases": {"hc:t61": {"started": "702", "until": 1302.0}}, "excluded": []}'
    ),
    "started-true": '{"leases": {"hc:t61": {"started": true, "until": 1302.0}}, "excluded": []}',
    "started-null": '{"leases": {"hc:t61": {"started": null, "until": 1302.0}}, "excluded": []}',
    "started-not-a-number": (
        '{"leases": {"hc:t61": {"started": NaN, "until": 1302.0}}, "excluded": []}'
    ),
    "until-a-string": '{"leases": {"hc:t61": {"started": 702.0, "until": "1302"}}, "excluded": []}',
    "until-true": '{"leases": {"hc:t61": {"started": 702.0, "until": true}}, "excluded": []}',
    "until-a-list": '{"leases": {"hc:t61": {"started": 702.0, "until": [1302.0]}}, "excluded": []}',
    "until-infinite": (
        '{"leases": {"hc:t61": {"started": 702.0, "until": Infinity}}, "excluded": []}'
    ),
}


@pytest.mark.parametrize(
    "text", WRONG_SHAPE_REFUSED_AT_PRETIP.values(), ids=WRONG_SHAPE_REFUSED_AT_PRETIP.keys()
)
def test_every_other_wrong_shape_still_clears_every_lease(tmp_path: Path, text: str) -> None:
    """B1, the cases that were refused before. Label: pins_existing, every row."""
    assert_failed_closed(start(tmp_path, text))


# A whole number no float can hold. JSON puts no limit on the digits of a number.
HUGE = "1" + "0" * 400

# Files whose contents cannot be applied safely. At 7d65216, a file of 200,000
# unmatched array openers made the constructor itself raise RecursionError from
# `json.loads`, and an extreme deadline raised OverflowError during arithmetic.
# The nesting fixture now stands one level past the reader's explicit bound.
RAISED_AT_PRETIP = {
    "nested-too-deep-to-parse": ("[" * (MAX_JSON_DEPTH + 1) + "0" + "]" * (MAX_JSON_DEPTH + 1)),
    "start-too-large-for-a-float": (
        '{"leases": {"hc:t61": {"started": -' + HUGE + ', "until": 1302.0}}, "excluded": []}'
    ),
}


def test_debug_state_nesting_has_an_explicit_boundary() -> None:
    """T-187: the reader owns its nesting limit instead of Python's call stack."""
    _check_json_depth("[" * MAX_JSON_DEPTH + "0" + "]" * MAX_JSON_DEPTH)

    with pytest.raises(ValueError) as caught:
        _check_json_depth("[" * (MAX_JSON_DEPTH + 1) + "0" + "]" * (MAX_JSON_DEPTH + 1))
    assert type(caught.value) is ValueError
    assert str(caught.value) == (
        f"Debug lease file is too deeply nested (maximum JSON depth {MAX_JSON_DEPTH})"
    )

    # Brackets, braces and escaped quotes inside JSON strings are data, not nesting.
    _check_json_depth(json.dumps({"text": '[{\\"' * (MAX_JSON_DEPTH + 1)}))


@pytest.mark.parametrize("text", RAISED_AT_PRETIP.values(), ids=RAISED_AT_PRETIP.keys())
def test_a_file_the_checks_cannot_compute_on_clears_every_lease(tmp_path: Path, text: str) -> None:
    """B1: these files clear every lease and the bridge starts. Label: red_at_pretip, every row.

    That is shown for the errors the loader handles: of the file, of the parse
    and of the validation. It is not shown for every file: the whole file is
    read and parsed before any limit applies, and a byte budget for that read
    is row T-74's (review round 8, note 6).
    """
    assert_failed_closed(start(tmp_path, text))


def test_the_error_of_a_wrong_shape_is_one_name(tmp_path: Path) -> None:
    """What `debug status` reports as ``error`` for these files. Label: red_at_pretip.

    Text that is not JSON keeps the name of the parser's error. A file past
    the reader's explicit nesting bound and every JSON file of the wrong shape
    report ``ValueError``. At 7d65216 the name followed the accident of which
    line tripped: ``KeyError``, ``TypeError`` or ``AttributeError`` for 21 of
    these files.
    """
    parser = {
        "file-not-json": "JSONDecodeError",
        "file-empty": "JSONDecodeError",
    }
    files = WRONG_SHAPE_ACCEPTED_AT_PRETIP | WRONG_SHAPE_REFUSED_AT_PRETIP | RAISED_AT_PRETIP
    names = {name: start(tmp_path / name, text).status()["error"] for name, text in files.items()}
    assert names == {name: parser.get(name, "ValueError") for name in files}


def test_an_ended_lease_of_whole_numbers_too_large_for_a_float_is_dropped(tmp_path: Path) -> None:
    """The same arithmetic on a lease that is valid and has ended. Label: red_at_pretip.

    Both numbers are whole, one second apart and long past, so the record is
    well-formed and the lease is over. 7d65216 raised OverflowError here.
    """
    ended = '{"started": -' + HUGE + ', "until": -' + "9" * 400 + "}"
    restored = start(tmp_path, '{"leases": {"hc:t61": ' + ended + '}, "excluded": []}')
    assert restored.error is None
    assert restored.leases == {}


def test_what_the_loader_accepts_that_the_writer_does_not_write(tmp_path: Path) -> None:
    """The limits of "fails closed", stated instead of claimed. Label: pins_existing.

    The loader checks the type and the keys of every stored field. It does not
    check that the file is byte for byte what `_save` writes: whole numbers
    load as deadlines, the order of the exclusions is free, a repeated exclusion
    counts once, a lease that has ended is dropped without an error, and of a
    key written twice `json.loads` keeps the last one.
    """
    twice = start(
        tmp_path / "a-key-twice",
        '{"leases": {"hc:t62": '
        + LEASE
        + '}, "leases": {"hc:t61": '
        + LEASE
        + '}, "excluded": []}',
    )
    assert twice.error is None
    assert set(twice.leases) == {CORE}

    restored = start(
        tmp_path / "whole-numbers",
        '{"leases": {"all": {"started": 702, "until": 1302}, '
        '"hc:t62": {"started": 100, "until": 700}}, '
        '"excluded": ["kiro:work:/tmp/My Project", "fixtureprov:thread-1", '
        '"fixtureprov:thread-1"]}',
    )
    assert restored.error is None
    assert restored.leases == {"all": {"started": 702, "until": 1302}}
    assert restored.excluded == {"fixtureprov:thread-1", KIRO}

    # Exclusions mean something only beside a live `all` lease; without one
    # they are valid and are not kept.
    alone = start(
        tmp_path / "no-all-lease", '{"leases": {"hc:t61": ' + LEASE + '}, "excluded": ["hc:t62"]}'
    )
    assert alone.error is None
    assert set(alone.leases) == {CORE}
    assert alone.excluded == set()


def test_an_invalid_file_stays_on_disk_until_the_next_save(tmp_path: Path) -> None:
    """Review round 10, finding 2: the loader changes its own state, not the file.

    Label: pins_existing, against ``0458ad4``, the tip before review round 10.
    A start on an invalid file leaves the controller with no leases and no
    exclusions and names the class of the error. It neither rewrites nor
    deletes the file, and a status read does not either. The bytes change
    when the controller next saves: `off()` and `on()` write the file from
    the controller's own state. One file for each kind of error the loader
    handles, taken from the tables above.
    """
    files = {
        "wrong-shape": (WRONG_SHAPE_REFUSED_AT_PRETIP["leases-null"], "ValueError"),
        "not-json": (WRONG_SHAPE_REFUSED_AT_PRETIP["file-not-json"], "JSONDecodeError"),
        "too-deep": (RAISED_AT_PRETIP["nested-too-deep-to-parse"], "ValueError"),
    }
    for name, (text, error) in files.items():
        restored = start(tmp_path / name, text)
        assert_failed_closed(restored)
        assert restored.status()["error"] == error
        assert restored.path.read_bytes() == text.encode(), name

        restored.off()
        assert json.loads(restored.path.read_text()) == {"leases": {}, "excluded": []}
        restored.on(CORE, 600)
        assert json.loads(restored.path.read_text()) == {
            "leases": {CORE: {"started": NOW, "until": NOW + 600}},
            "excluded": [],
        }
