"""Fakes for the fleet writer's tests: a child runner, a service, a clock and a sink."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from answerback_bridge.errors import PublicBridgeError
from answerback_bridge.fleet import loop
from answerback_bridge.process import BridgeError
from answerback_bridge.transport import TransportUnavailable


def linux(load: float = 1.0, suites: int = 0, avail: int = 900_000, swapfree: int = 1000) -> str:
    """A Linux probe answer with ten cores and 1 GB of memory."""
    return (
        f"os=Linux\ncores=10\nload={load} {load} {load}\nmemtotal_kb=1000000\n"
        f"memavailable_kb={avail}\nswaptotal_kb=1000\nswapfree_kb={swapfree}\n"
        f"largest_kb=100\nlargest_name=python\nprocs=12\nsuites={suites}\nprobe_end=1\n"
    )


def darwin(
    load: float = 1.0,
    pressure: int = 1,
    compressor: int = 0,
    suites: int = 0,
    cores: int = 10,
    swap_mb: float = 0.0,
) -> str:
    """A macOS probe answer; ``compressor`` is in kB of 1,000,000 kB."""
    return (
        f"os=Darwin\ncores={cores}\nload={load} {load} {load}\nmem_total_kb=1000000\n"
        f"pressure={pressure}\nmem_level=50\n"
        f"swap=total = 1024.00M  used = {swap_mb:.2f}M  free = 0.00M  (encrypted)\n"
        f"page_bytes=1024\ncompressor_pages={compressor}\nlargest_kb=100\nlargest_name=x\n"
        f"procs=10\nsuites={suites}\nprobe_end=1\n"
    )


class Fleet:
    """A runner that answers like ``/bin/sh``, ``hc ls --json`` and ``hc exec --on``."""

    def __init__(self, outputs: dict[str, str], links: dict[str, str] | None = None) -> None:
        self.outputs = outputs
        self.links = (
            links if links is not None else {n: "connected" for n in outputs if n != "local"}
        )
        self.terminals: list[dict[str, Any]] = []
        self.labelled: list[dict[str, Any]] = []
        self.label_fails = False
        self.label_hangs = False
        self.hang: set[str] = set()
        self.slow_end: dict[str, float] = {}
        self.fail: set[str] = set()
        self.calls: list[list[str]] = []
        self.cancelled: list[str] = []
        self.ls_fails = False
        self.raw_listing: str | None = None
        self.on_call: Any = None

    def listing(self) -> str:
        return json.dumps(
            {
                "machines": [{"name": n, "state": s} for n, s in self.links.items()],
                "terminals": self.terminals,
            }
        )

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if self.on_call is not None:
            self.on_call(argv)
        if argv[1:] == ["ls", "--json"]:
            if self.ls_fails:
                return 1, ""
            return 0, self.listing() if self.raw_listing is None else self.raw_listing
        if argv[1:4] == ["ls", "--json", "--label"]:
            if self.label_hangs:
                await asyncio.Event().wait()
            if self.label_fails:
                return 1, ""
            machines = [{"name": n, "state": s} for n, s in self.links.items()]
            return 0, json.dumps({"machines": machines, "terminals": self.labelled})
        name = "local" if argv[0] == "/bin/sh" else argv[3]
        if name in self.hang:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.append(name)
                if name in self.slow_end:
                    await asyncio.sleep(self.slow_end[name])
        if name in self.fail:
            return 1, ""
        return 0, self.outputs[name]

    def probed(self) -> list[str]:
        return [argv[3] for argv in self.calls if argv[1:2] == ["exec"]]


class Service:
    """A bridge service that answers or not."""

    def __init__(self) -> None:
        self.up = False
        self.status: dict[str, Any] = {
            "running": True,
            "accepting": True,
            "version": "0.13.0+internal",
            "instance_id": "svc",
            "started": 1.0,
            "tasks": {"completed": 3, "uncertain": 1},
            "bus": {"connected": True, "last_error": None},
            "clients": {"clients": []},
        }
        self.tasks: list[dict[str, Any]] = [
            {
                "id": "r-1",
                "request_key": "k",
                "target": "hc:t8",
                "status": "uncertain",
                "created": 5.0,
                "updated": 6.0,
            }
        ]
        self.methods: list[str] = []
        # How ``fleet_snapshot`` is answered: "refuse" (a service without the method:
        # operation_unknown), "fail" (another error answer, as a locked store gives), "slow" (no
        # answer in time) or "answer" (``snapshot_answer``, else the receipts).
        self.snapshot = "refuse"
        self.snapshot_answer: dict[str, Any] | None = None
        self.snapshot_error: BaseException | None = None  # raised by fleet_snapshot when set

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.methods.append(method)
        if not self.up:
            raise TransportUnavailable("Bridge is stopped. Run answerback start first.")
        if method == "status":
            return self.status
        if method == "fleet_snapshot" and self.snapshot_error is not None:
            raise self.snapshot_error
        if method == "fleet_snapshot" and self.snapshot == "slow":
            await asyncio.sleep(30)
        if method == "fleet_snapshot" and self.snapshot == "answer":
            return self.snapshot_answer or {"tasks": self.tasks}
        if method == "fleet_snapshot" and self.snapshot == "fail":
            raise BridgeError("Bridge request failed; inspect service logs and the task receipt.")
        if method == "fleet_snapshot":
            raise PublicBridgeError("operation_unknown")
        if method == "task_list":
            assert params == {"limit": 100}
            return {"tasks": self.tasks, "searched_latest": 100}
        raise ValueError("operation_unknown")


class FakeClock:
    """Wall and monotonic time that move only when the loop sleeps or a test moves them."""

    def __init__(self, wall: float = 1_800_000_000.0) -> None:
        self.now_wall = wall
        self.now_mono = 0.0
        self.sleeps: list[float] = []
        self.stop: asyncio.Event | None = None
        self.until = float("inf")

    def move(self, seconds: float, wall: float | None = None) -> None:
        self.now_mono += seconds
        self.now_wall += seconds if wall is None else wall

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.move(seconds)
        if self.stop is not None and self.now_mono >= self.until:
            self.stop.set()
        await asyncio.sleep(0)

    def clock(self) -> loop.Clock:
        return loop.Clock(lambda: self.now_wall, lambda: self.now_mono, self.sleep)


class Sink:
    """Collects published documents."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.docs: list[dict[str, Any]] = []
        self.at: list[float] = []
        self.clock = clock
        self.fail = False

    def publish(self, data: bytes) -> None:
        from answerback_bridge.fleet.publish import PublishError

        if self.fail:
            raise PublishError("publication failed: test")
        self.docs.append(json.loads(data))
        self.at.append(self.clock.now_mono if self.clock else 0.0)


def writer(
    fleet: Fleet,
    service: Service | None = None,
    clock: FakeClock | None = None,
    sink: Sink | None = None,
    **settings: Any,
) -> tuple[loop.Writer, FakeClock, Sink]:
    """A writer on the fakes."""
    clock = clock or FakeClock()
    sink = sink or Sink(clock)
    w = loop.Writer(
        loop.Settings(**settings),
        runner=fleet,
        call=service or Service(),
        sink=sink,
        clock=clock.clock(),
    )
    return w, clock, sink
