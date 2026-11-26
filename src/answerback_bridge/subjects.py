"""A receipt's subject: text the sender asserts about what a hand-off concerns (T-277)."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MAX_SUBJECT = 256
SUBJECT_REFUSAL = (
    f"subject must be 1-{MAX_SUBJECT} printable characters without leading or trailing whitespace"
)
# The subjects of the submissions being admitted, by store file and request ID. The router
# registers one around `adapter.submit`; adapters reserve without naming a subject, and the
# store takes it from here. Keyed by request ID, so no other reservation takes it, and held
# for the process rather than a context, so an adapter that reserves on a thread still does.
# Each entry holds the subject and the number of admissions of that request ID now running.
_ADMITTING: dict[tuple[str, str], tuple[str | None, int]] = {}
# The subjects of cancelled admissions that had not reserved yet. Cancelling the await does
# not stop an adapter's thread or executor job, which may still reserve; it takes the subject
# from here until a reservation of that request ID settles it. Oldest first, at most
# `MAX_KEPT`; a cancelled admission whose work never reserves keeps its entry until evicted.
_KEPT: dict[tuple[str, str], str | None] = {}
MAX_KEPT = 1024
_LOCK = threading.Lock()


def validate_subject(value: Any) -> str | None:
    """`value` if it is a valid subject, None for None; otherwise ValueError.

    A subject is text the sender asserts about a hand-off (a commit SHA, a digest, a tag):
    1-256 printable characters, so no newline, tab, control or bidi formatting character,
    and no leading or trailing whitespace. The kernel never checks it against anything.
    """
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_SUBJECT
        or not value.isprintable()
        or value != value.strip()
    ):
        raise ValueError(SUBJECT_REFUSAL)
    return value


def _key(store: Path, request_key: str) -> tuple[str, str]:
    return (str(store.resolve()), request_key)


@contextmanager
def admission_subject(store: Path, request_key: str, subject: str | None) -> Iterator[None]:
    """While admitting `request_key` to the store at `store`, reserve it with `subject`.

    `None` is registered too: an admission without a subject is not the same work as one
    with. A second admission of the same request ID with another subject, while this one
    runs, is refused as different work; with the same subject it proceeds and the store's
    own repeat rule answers it. Overlapping admissions are counted, so the registration
    lasts until the last of them ends, whatever order they end in. A subject kept after a
    cancelled admission (`keep_after_cancel`) is held to the same rule.
    """
    subject = validate_subject(subject)
    key = _key(store, request_key)
    with _LOCK:
        if key in _KEPT and _KEPT[key] != subject:
            raise ValueError("request_id already describes different work")
        if key in _ADMITTING:
            held, count = _ADMITTING[key]
            if held != subject:
                raise ValueError("request_id already describes different work")
            _ADMITTING[key] = (held, count + 1)
        else:
            _ADMITTING[key] = (subject, 1)
    try:
        yield
    finally:
        with _LOCK:
            held, count = _ADMITTING[key]
            if count == 1:
                del _ADMITTING[key]
            else:
                _ADMITTING[key] = (held, count - 1)


def keep_after_cancel(store: Path, request_key: str) -> None:
    """Keep the running admission's subject for `request_key` after it is cancelled.

    Call it while the admission is still registered. The adapter's thread or executor job
    may reserve later; it still takes this subject, until `settled` drops the entry.
    """
    key = _key(store, request_key)
    with _LOCK:
        if key not in _ADMITTING:
            return
        _KEPT.pop(key, None)
        _KEPT[key] = _ADMITTING[key][0]
        while len(_KEPT) > MAX_KEPT:
            del _KEPT[next(iter(_KEPT))]


def settled(store: Path, request_key: str) -> None:
    """A receipt for `request_key` exists now: a kept subject is no longer needed."""
    key = _key(store, request_key)
    with _LOCK:
        _KEPT.pop(key, None)


def admitted_subject(store: Path, request_key: str, subject: str | None) -> str | None:
    """The subject a reservation of `request_key` stores: explicit, or the admission's.

    `None` means "not named here" and takes the admission's subject; an explicit subject
    that differs from it is refused.
    """
    subject = validate_subject(subject)
    key = _key(store, request_key)
    with _LOCK:
        if key in _ADMITTING:
            admitted = _ADMITTING[key][0]
        elif key in _KEPT:
            admitted = _KEPT[key]
        else:
            return subject
    if subject is not None and subject != admitted:
        raise ValueError("subject conflicts with the admission subject")
    return admitted
