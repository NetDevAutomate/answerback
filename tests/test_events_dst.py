"""T-16 R2-N2: exercise the real no-argument viewer clock in both DST directions."""

from datetime import UTC, datetime, tzinfo

import pytest

from answerback_bridge.events import board


@pytest.mark.parametrize(
    ("viewing", "event", "expected"),
    [
        ("2026-11-17T12:00:00", "2026-07-01T12:00:00", "2026-07-01 13:00:00"),
        ("2026-07-01T12:00:00", "2026-01-15T12:00:00", "2026-01-15 12:00:00"),
    ],
)
def test_r2n2_default_viewer_uses_each_events_offset(
    monkeypatch: pytest.MonkeyPatch, viewing: str, event: str, expected: str
) -> None:
    import time

    current = datetime.fromisoformat(viewing)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return current if tz is None else current.astimezone(tz)

    monkeypatch.setenv("TZ", "Europe/London")
    monkeypatch.setattr(board, "datetime", Clock)
    time.tzset()
    try:
        stamp = int(datetime.fromisoformat(event).replace(tzinfo=UTC).timestamp() * 1000)
        assert board._clock(stamp) == expected
    finally:
        monkeypatch.undo()
        time.tzset()
