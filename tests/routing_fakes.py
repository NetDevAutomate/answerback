"""Scripted core, Herdr App and bridge for the routing tests. No terminal is started.

One `World` holds the state the three fakes share, so a test reads like the real
sequence: a tab is made, the launch script execs the harness, the scanner sees it idle,
a submission makes it work, completion leaves it `done` (undeliverable, T-13).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from answerback_bridge.process import BridgeError
from answerback_bridge.routing.policy import Policy, load_policy
from answerback_bridge.routing.pool import Pool, Registry
from answerback_bridge.routing.ports import LayoutError, RoutingError

FIXTURE = Path(__file__).parent / "fixtures" / "routing-policy.json"
PROJECT = "/work/answerback"
SPACE = "spcproject"
AGENTS = {"claude": "claude", "codex": "codex", "grok": "grok", "kiro-cli": "kiro"}


def policy(**lifecycle: Any) -> Policy:
    """The fixture policy, optionally with lifecycle keys replaced."""
    if not lifecycle:
        return load_policy(FIXTURE)
    from answerback_bridge.routing.policy import parse_policy

    raw = json.loads(FIXTURE.read_text())
    raw["lifecycle"].update(lifecycle)
    return parse_policy(raw)


class World:
    def __init__(self) -> None:
        self.clock = 1_800_000_000.0
        self.terminals: dict[str, dict[str, Any]] = {}
        self.tabs: dict[str, list[str]] = {}
        # Tabs the owner has open per space, besides any in `tabs`. The project space
        # normally has one; clear it to make a worker's tab the last in its space.
        self.owner_tabs: dict[str, list[str]] = {SPACE: ["tbowner0"]}
        self.ls_fails = False
        self.closed_spaces: list[str | None] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.sent: list[dict[str, Any]] = []
        self.handoffs: dict[str, str] = {}
        self.todo: list[str] = []
        self.boot_seconds = 0.0  # how long the launch script stays in the foreground
        self.never_ready = False
        self.app_down = False
        self.app_hangs = False
        self.right_click_fails: Exception | None = None
        self.selected_space = SPACE  # where terminal.keep lands
        self.foreground_override: list[str] | None = None
        self.scan_complete = True
        self.broken: set[str] = set()  # bridge methods that fail
        # The bridge's `pressure_status` result, or a function of the world that makes it.
        self.pressure: dict[str, Any] | Callable[[World], dict[str, Any]] = {}
        # When set, `peer_submit` answers it (a `provider_backoff` result): nothing is sent.
        self.backoff: dict[str, Any] | None = None
        self._next = 0

    # --- time -----------------------------------------------------------------
    def now(self) -> float:
        return self.clock

    async def sleep(self, seconds: float) -> None:
        self.clock += seconds

    # --- building blocks --------------------------------------------------------
    def spawn(
        self,
        argv: list[str],
        *,
        name: str | None = None,
        cwd: str = PROJECT,
        space: str | None = None,
        tab: str | None = None,
        state: str | None = "idle",
        ready_at: float | None = None,
    ) -> str:
        self._next += 1
        address = f"t{self._next:x}"
        labels = {}
        if tab is not None:
            labels = {"herdr.space": space or SPACE, "herdr.tab": tab}
            self.tabs.setdefault(tab, []).append(address)
        self.terminals[address] = {
            "address": address,
            "name": name,
            "cwd": cwd,
            "state": "live",
            "created_ms": int(self.clock * 1000) + self._next,
            "labels": labels,
            "argv": argv,
            "agent_state": state,
            "ready_at": self.clock if ready_at is None else ready_at,
        }
        return address

    def owner_tab(self, tool: str = "claude", *, name: str | None = None, **kw: Any) -> str:
        """A terminal the owner opened in the App: a tab, no registry row."""
        return self.spawn([tool], name=name, tab=f"tbowner{self._next + 1}", **kw)

    def _foreground(self, row: dict[str, Any]) -> list[str]:
        if self.foreground_override is not None:
            return self.foreground_override
        argv = row["argv"]
        if self.clock < row["ready_at"] or self.never_ready:
            return ["/bin/zsh", argv[0]]  # the launch script has not exec'd the harness yet
        return argv[1:] if argv[0].endswith("harness_launch.sh") else argv

    def detail(self, address: str) -> dict[str, Any] | None:
        row = self.terminals.get(address)
        if row is None:
            return None
        agent = AGENTS.get(Path(self._foreground(row)[0]).name)
        status = (
            {"app": agent, "state": row["agent_state"]} if agent and row["agent_state"] else None
        )
        return {
            "address": address,
            "name": row["name"],
            "cwd": row["cwd"],
            "state": row["state"],
            "created_ms": row["created_ms"],
            "labels": dict(row["labels"]),
            "status": status,
            "foreground": {"argv": self._foreground(row), "cwd": row["cwd"], "pid": 100},
        }

    def peers(self) -> list[dict[str, Any]]:
        found = []
        for address, row in self.terminals.items():
            agent = AGENTS.get(Path(self._foreground(row)[0]).name)
            if agent is None or row["state"] != "live":
                continue  # the scanner excludes a shell or a wrapper as a non-agent
            state = row["agent_state"] or "unknown"
            # Like Core._peer, bind discovery to the instance and foreground identity,
            # while allowing readiness to change without changing the binding.
            identity = {
                "address": address,
                "created_ms": row["created_ms"],
                "argv": row["argv"],
                "foreground_argv": self._foreground(row),
                "cwd": row["cwd"],
                "agent": agent,
                "agent_identity_source": row.get("identity_source", "foreground_argv"),
            }
            binding = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            found.append(
                {
                    "peer_id": f"hc:{address}",
                    "provider": "hc",
                    "target": address,
                    "agent_kind": agent,
                    "agent_identity_source": row.get("identity_source", "foreground_argv"),
                    "project": row["cwd"],
                    "project_known": True,
                    "state": state,
                    "send_ready": state == "idle",
                    "send_readiness_reason": "ready" if state == "idle" else state,
                    "binding": binding,
                }
            )
        return found

    def complete(self, receipt_id: str, status: str = "completed") -> None:
        receipt = self.receipts[receipt_id]
        receipt.update(status=status, updated=self.clock)
        address = receipt["target"].rsplit(":", 1)[-1]
        if address in self.terminals:
            # T-13: a finished interactive agent reports done, which is not deliverable.
            self.terminals[address]["agent_state"] = "done"

    def names(self, method: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == method]


class FakeTerminals:
    def __init__(self, world: World) -> None:
        self.world = world

    async def live(self) -> list[dict[str, Any]]:
        # The keys of a real `hc ls --json` row (read 2026-11-17): there is no name.
        return [
            {
                "address": row["address"],
                "agent": None,
                "cwd": row["cwd"],
                "state": row["state"],
                "status": None,
                "turn": None,
                "where": "local",
            }
            for row in self.world.terminals.values()
            if row["state"] == "live"
        ]

    async def get(self, address: str) -> dict[str, Any] | None:
        return self.world.detail(address)

    async def run(self, *, name: str, cwd: str, argv: list[str]) -> str:
        self.world.calls.append(("hc.run", name, cwd, list(argv)))
        return self.world.spawn(
            list(argv),
            name=name,
            cwd=cwd,
            ready_at=self.world.clock + self.world.boot_seconds,
        )

    async def kill(self, address: str) -> None:
        self.world.calls.append(("hc.kill", address))
        self.world.terminals[address]["state"] = "finished"

    async def remove(self, address: str) -> None:
        self.world.calls.append(("hc.rm", address))
        del self.world.terminals[address]


class FakeLayout:
    def __init__(self, world: World) -> None:
        self.world = world

    async def tab_new(self, **kwargs: Any) -> dict[str, Any]:
        self.world.calls.append(("tab.new", dict(kwargs)))
        if self.world.app_hangs:
            raise RoutingError("herdr-app do timed out; its effect is unknown")
        if self.world.app_down:
            raise LayoutError("herdr-app do tab.new failed; is the App open?")
        tab = f"tb{len(self.world.tabs) + 1}"
        # The App names the tab; the core terminal itself has no name.
        address = self.world.spawn(
            list(kwargs["argv"]),
            cwd=kwargs["folder"],
            space=kwargs["space"],
            tab=tab,
            ready_at=self.world.clock + self.world.boot_seconds,
        )
        return {"terminal": address, "pane": "p1", "tab": tab}

    async def keep(self, address: str) -> None:
        self.world.calls.append(("terminal.keep", address))
        if self.world.app_down and self.world.selected_space is None:
            raise LayoutError("herdr-app do terminal.keep failed; is the App open?")
        tab = f"tbkept{address}"
        self.world.tabs[tab] = [address]
        self.world.terminals[address]["labels"] = {
            "herdr.space": self.world.selected_space,
            "herdr.tab": tab,
        }

    async def right_click(self, address: str) -> None:
        self.world.calls.append(("terminal.right_click", address, "menu"))
        if self.world.right_click_fails is not None:
            raise self.world.right_click_fails

    async def tab_terminals(self, tab: str) -> list[str] | None:
        return list(self.world.tabs[tab]) if tab in self.world.tabs else None

    def _space(self, tab: str) -> str | None:
        for address in self.world.tabs.get(tab, []):
            return self.world.terminals[address]["labels"].get("herdr.space")
        return None

    async def space_tabs(self, tab: str) -> list[str] | None:
        if self.world.ls_fails:
            raise LayoutError("herdr-app ls failed; is the App open?")
        if tab not in self.world.tabs:
            return None
        space = self._space(tab)
        shown = [other for other in self.world.tabs if self._space(other) == space]
        return shown + list(self.world.owner_tabs.get(space or "", []))

    async def tab_close(self, tab: str) -> None:
        self.world.calls.append(("tab.close", tab))
        if len(await self.space_tabs(tab) or []) <= 1:
            self.world.closed_spaces.append(self._space(tab))  # what the App really does
        for address in self.world.tabs.pop(tab):
            self.world.terminals.pop(address, None)


class FakeBridge:
    def __init__(self, world: World) -> None:
        self.world = world

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        world = self.world
        world.calls.append((method, dict(params)))
        if method in world.broken:
            raise BridgeError(f"{method} failed")
        if method == "peer_search":
            return {"peers": world.peers(), "complete": world.scan_complete, "errors": {}}
        if method == "peer_submit":
            address = params["selector"].removeprefix("hc:")
            if params.get("binding") is not None:
                peer = next((peer for peer in world.peers() if peer["target"] == address), {})
                if peer.get("binding") != params["binding"]:
                    raise ValueError("Selected peer changed since discovery; search again")
            if world.backoff is not None:
                return dict(world.backoff)
            receipt = {
                "id": f"r{len(world.receipts) + 1}",
                "request_key": params["request_id"],
                "target": f"hc:default:{address}",
                "status": "submitted",
                "created": world.clock,
                "updated": world.clock,
            }
            world.receipts[receipt["id"]] = receipt
            world.sent.append(dict(params))
            world.terminals[address]["agent_state"] = "working"
            return dict(receipt)
        if method == "task_get":
            if params["task_id"] not in world.receipts:
                raise BridgeError("Unknown task")
            return dict(world.receipts[params["task_id"]])
        if method == "task_list":
            return {"tasks": [dict(receipt) for receipt in world.receipts.values()]}
        if method == "task_for_peer":
            address = params["selector"].removeprefix("hc:")
            for receipt in reversed(list(world.receipts.values())):
                if receipt["target"].endswith(":" + address):
                    return dict(receipt)
            raise BridgeError("No receipt for this peer among the latest 100 tasks")
        if method == "pressure_status":
            pressure = world.pressure
            return dict(pressure(world) if callable(pressure) else pressure)
        if method == "hc_read":
            return {
                "target": params["target"],
                "complete": True,
                "how": "screen",
                "text": "Sign in",
            }
        raise AssertionError(f"unexpected bridge call {method}")


class FakeHandoffs:
    def __init__(self, world: World) -> None:
        self.world = world

    def state(self, task: str) -> str:
        return self.world.handoffs.get(task, "open")


class FakeLedger:
    def __init__(self, world: World) -> None:
        self.world = world

    def queued(self) -> list[str]:
        return list(self.world.todo)


def make_pool(world: World, tmp_path: Path, policy_: Policy | None = None) -> Pool:
    return Pool(
        policy_ or policy(),
        FakeTerminals(world),
        FakeLayout(world),
        FakeBridge(world),
        Registry(tmp_path / "routing-workers.json"),
        FakeHandoffs(world),
        FakeLedger(world),
        now=world.now,
        sleep=world.sleep,
        poll_seconds=3.0,
    )
