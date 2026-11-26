"""The byte bound of one outbound Core text (fix round `ans-fix2`, review 2 finding B3).

`hc send` gets the whole text as one argument. Linux refuses one argument of
131,072 bytes and more on a kernel with 4 KiB pages (32 pages, and the final NUL
counts), so the largest text the bridge accepts is 131,071 bytes. A text of
exactly 131,072 bytes used to be accepted: the send could not start and the
receipt read `uncertain` with nothing typed.

The fake `hc` of `test_core_result_file.py` is a real executable, so the send at
131,071 bytes passes that text through a real `execve`. The sizes are built from
two-byte and one-byte characters, so the public limit of 100,000 characters is
not what refuses.

Labels say what a test shows at this round's start commit `0b4c2707`:
`red_at_pretip` fails there, `pins_existing` passes there on purpose.
"""

from pathlib import Path
from typing import Any

import pytest
from test_core_result_file import (
    REMOTE,
    FakeCore,
    binding_of,
    delivered,
    make_bridge,
    make_fake,
)

from answerback_bridge.providers.herdr.core import validate_message

LARGEST = 128 * 1024 - 1
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
SIZES = [
    pytest.param(LARGEST, id="131071-accepted"),  # pins_existing
    pytest.param(LARGEST + 1, id="131072-refused"),  # red_at_pretip
    pytest.param(LARGEST + 2, id="131073-refused"),  # pins_existing
]


def filled(size: int) -> str:
    """A text of exactly `size` bytes in fewer than 100,000 characters."""
    text = "é" * (size // 2) + "x" * (size % 2)
    assert len(text.encode()) == size
    return text


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> Any:
    bridge = make_bridge(fake, tmp_path)
    yield bridge
    await bridge.close()


def test_the_largest_accepted_text_is_one_byte_under_128_kib() -> None:
    """red_at_pretip: at `0b4c2707` a text of 131,072 bytes is accepted."""
    assert LARGEST == 131_071
    for text in ("x" * LARGEST, filled(LARGEST)):
        validate_message(text)
    for size in (LARGEST + 1, LARGEST + 2):
        for text in ("x" * size, filled(size)):
            with pytest.raises(ValueError, match="Core message exceeds the 128 KiB bound$"):
                validate_message(text)


@pytest.mark.parametrize("target", ["t1", REMOTE], ids=["local", "remote"])
@pytest.mark.parametrize("size", SIZES)
async def test_submission_is_sent_once_at_the_bound_and_refused_above_it(
    bridge: Any, fake: FakeCore, target: str, size: int
) -> None:
    """The public path, `peer_submit`, for a local and for a remote terminal.

    The size is the envelope's, as typed: the brief around the message counts.
    """
    token = None if target == "t1" else "0" * 32
    room = size - len(bridge.core_tasks.envelope(PLACEHOLDER, "", token=token).encode())
    message = filled(room)
    assert len(message) < 100_000
    assert len(bridge.core_tasks.envelope(PLACEHOLDER, message, token=token).encode()) == size
    if size > LARGEST:
        with pytest.raises(ValueError, match="128 KiB"):
            await bridge.peer_submit(
                f"hc:{target}", message, "bound", binding=binding_of(bridge, target)
            )
        assert bridge.store.recent() == []  # refused before any reservation
        assert fake.sends() == []  # and nothing is typed
        return
    task = await delivered(bridge, target, message, "bound")
    assert task["status"] == "submitted"
    (send,) = fake.sends()
    assert len(send[-1].encode()) == LARGEST


@pytest.mark.parametrize("size", SIZES)
async def test_direct_send_types_once_at_the_bound_and_nothing_above_it(
    bridge: Any, fake: FakeCore, size: int
) -> None:
    """The direct path, `Core.send`, which takes the text as it is typed."""
    peer = await bridge.core.peer("t1")
    assert peer["send_ready"] is True
    text = filled(size)
    if size > LARGEST:
        with pytest.raises(ValueError, match="128 KiB"):
            await bridge.core.send("t1", text, expected=peer)
        assert fake.sends() == []
        return
    await bridge.core.send("t1", text, expected=peer)
    (send,) = fake.sends()
    assert send[-1] == text
