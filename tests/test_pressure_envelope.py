"""T-227 gate: the hc envelope carries the pressure notice on the wire, never in the receipt."""

from pathlib import Path
from typing import Any

import pytest
from pressure_fakes import NOTICE, FakePressure
from test_core_message_bound import LARGEST, filled
from test_core_result_file import (
    FakeCore,
    binding_of,
    delivered,
    make_bridge,
    make_fake,
    today_local_envelope,
)

from answerback_bridge.providers.herdr.core import validate_message

TASK = "0d9e7c1a-5b7e-4a52-9c7b-0f4f8a1c2e3d"
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
TOKEN = "0" * 32
LINE = NOTICE + "\n"


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> Any:
    bridge = make_bridge(fake, tmp_path)
    yield bridge
    await bridge.close()


def bedrock_held(bridge: Any) -> FakePressure:
    # The fake terminal runs Claude: its account is `bedrock`.
    pressure = FakePressure({("bedrock", "default"): (1, 60.0)})
    bridge.core_tasks.pressure = pressure
    return pressure


async def test_the_notice_is_the_line_after_the_task_line(bridge: Any) -> None:
    core_tasks = bridge.core_tasks
    plain = core_tasks.envelope(TASK, "Review")
    assert plain == today_local_envelope(bridge.config_path, TASK, "Review")
    assert core_tasks.envelope(TASK, "Review", notice=None) == plain
    noticed = core_tasks.envelope(TASK, "Review", notice=LINE)
    assert noticed.split("\n")[:3] == [f"Bridge task: {TASK}", NOTICE, "Review"]
    assert noticed == plain.replace("\n", "\n" + LINE, 1)
    remote = core_tasks.envelope(TASK, "Review", token=TOKEN)
    assert core_tasks.envelope(TASK, "Review", token=TOKEN, notice=None) == remote
    assert core_tasks.envelope(TASK, "Review", token=TOKEN, notice=LINE) == remote.replace(
        "\n", "\n" + LINE, 1
    )


async def test_an_oversize_message_is_refused_before_reserve_under_pressure(
    bridge: Any, fake: FakeCore
) -> None:
    bedrock_held(bridge)
    before = bridge.store.counts()
    with pytest.raises(ValueError, match="128 KiB"):
        await bridge.peer_submit(
            "hc:t1", "x" * 131_000, "too-big", binding=binding_of(bridge, "t1")
        )
    # A message that fits only without the notice is refused while the notice applies.
    room = LARGEST - len(bridge.core_tasks.envelope(PLACEHOLDER, "").encode())
    exact = filled(room)
    validate_message(bridge.core_tasks.envelope(PLACEHOLDER, exact))
    with pytest.raises(ValueError, match="128 KiB"):
        await bridge.peer_submit("hc:t1", exact, "exact", binding=binding_of(bridge, "t1"))
    assert bridge.store.counts() == before
    assert fake.sends() == []
    # Without pressure the same message is accepted at the bound, as before.
    bridge.core_tasks.pressure = FakePressure()
    task = await delivered(bridge, "t1", exact, "exact")
    assert task["status"] == "submitted"


async def test_delivery_under_pressure_types_the_notice_and_keeps_the_message(
    bridge: Any, fake: FakeCore
) -> None:
    bedrock_held(bridge)
    task = await delivered(bridge, "t1", "Review the parser", "held-a")
    assert task["status"] == "submitted"
    assert task["message"] == "Review the parser"
    (send,) = fake.sends()
    assert send[-1] == today_local_envelope(
        bridge.config_path, task["id"], "Review the parser"
    ).replace("\n", "\n" + LINE, 1)


async def test_delivery_without_pressure_is_byte_identical(bridge: Any, fake: FakeCore) -> None:
    bridge.core_tasks.pressure = FakePressure({("kiro", "default"): (2, 60.0)})
    task = await delivered(bridge, "t1", "Review the parser", "plain-a")
    (send,) = fake.sends()
    assert send[-1] == today_local_envelope(bridge.config_path, task["id"], "Review the parser")


async def test_pressure_after_admission_never_pushes_a_text_past_the_bound(bridge: Any) -> None:
    peer = await bridge.core.peer("t1")
    room = LARGEST - len(bridge.core_tasks.envelope(TASK, "").encode())
    exact = filled(room)
    bedrock_held(bridge)
    text = bridge.core_tasks.delivery_text(TASK, exact, peer, token=None)
    assert text == bridge.core_tasks.envelope(TASK, exact)
    short = bridge.core_tasks.delivery_text(TASK, "Review", peer, token=None)
    assert short == bridge.core_tasks.envelope(TASK, "Review", notice=LINE)
