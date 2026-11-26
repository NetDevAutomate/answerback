"""A receipt's subject: text the sender asserts about what a hand-off concerns (T-277)."""

from typing import Any

MAX_SUBJECT = 256
SUBJECT_REFUSAL = (
    f"subject must be 1-{MAX_SUBJECT} printable characters without leading or trailing whitespace"
)


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
