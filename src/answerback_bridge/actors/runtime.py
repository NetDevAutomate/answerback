"""Run optional actor delivery independently of slow task admission."""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..bus.config import BusConfig
from ..bus.extra import missing_bus_extra
from .store import ActorStore

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

    from .transport import ActorLane


def actor_lane_class() -> type["ActorLane"]:
    """The NATS-backed lane is imported on first use: nats-py ships as the optional `bus` extra."""
    try:
        from .transport import ActorLane
    except ImportError as error:
        raise missing_bus_extra(error) from error
    return ActorLane


async def run_actor_lane(
    js: "JetStreamContext",
    config: BusConfig,
    store_factory: Callable[[], ActorStore | None],
    state: dict[str, Any],
    stopping: asyncio.Event,
) -> None:
    lane = None
    try:
        while not stopping.is_set():
            try:
                if lane is None:
                    store = store_factory()
                    if store is not None:
                        lane = actor_lane_class()(js, config, store)
                        await lane.start()
                if lane is not None:
                    state.update(enabled=True, last_error=None, **await lane.step())
                    if not state["ready"]:
                        await asyncio.sleep(0.25)
                    continue
                state.update(enabled=False, ready=False)
            except Exception as error:
                # A mailbox failure never cancels unrelated task delivery.
                state.update(ready=False, last_error=type(error).__name__)
                if lane is not None:
                    await lane.close()
                    lane = None
            try:
                await asyncio.wait_for(stopping.wait(), 1)
            except TimeoutError:
                pass
    finally:
        if lane is not None:
            await lane.close()
            state.update(lane.status())
        state["ready"] = False
