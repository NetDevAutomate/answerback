"""T-227 pressure store: levels, holds, decay, persistence and the file's protections."""

import json
import sqlite3
from pathlib import Path
from stat import S_IMODE

import pytest

from answerback_bridge.events.collector import Settings
from answerback_bridge.pressure import Match, Pressure, PressureSettings, PressureStore


class Clocks:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = 1_800_000_000.0

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def kiro(line: str = "a") -> Match:
    return Match("kiro", "default", "kiro", "hc_screen", "kiro.rate_limit", line * 64)


def store_at(path: Path | None, clocks: Clocks, **settings: object) -> PressureStore:
    return PressureStore(
        path,
        settings=PressureSettings(**settings),  # type: ignore[arg-type]
        clock=lambda: clocks.mono,
        wall=lambda: clocks.wall,
    )


def test_level_ladder_doubles_to_the_cap(tmp_path: Path) -> None:
    clocks = Clocks()
    store = store_at(tmp_path / "pressure.sqlite", clocks)
    holds = []
    for _ in range(5):
        pressure = store.signal(kiro())
        holds.append(pressure.retry_after(clocks.mono))
        assert pressure.until_wall == clocks.wall + holds[-1]
        clocks.advance(1)
    assert holds == [60, 120, 240, 480, 480]
    assert store.status("kiro").level == 5
    assert store.status("bedrock").level == 0 and not store.status("bedrock").held


def test_a_signal_after_the_hold_keeps_the_level_and_per_provider_overrides_apply(
    tmp_path: Path,
) -> None:
    clocks = Clocks()
    store = store_at(
        tmp_path / "pressure.sqlite",
        clocks,
        providers={"bedrock": {"base_seconds": 10, "cap_seconds": 25}},
    )
    bedrock = Match("bedrock", "default", "claude", "native_evidence", "bedrock.http_429", "d")
    assert store.signal(bedrock).retry_after(clocks.mono) == 10
    assert store.signal(bedrock).retry_after(clocks.mono) == 20
    assert store.signal(bedrock).retry_after(clocks.mono) == 25
    clocks.advance(30)
    again = store.signal(bedrock)
    assert again.level == 3 and again.retry_after(clocks.mono) == 25
    assert store.signal(kiro()).retry_after(clocks.mono) == 60


def test_decay_is_one_step_per_clean_window_down_to_zero(tmp_path: Path) -> None:
    clocks = Clocks()
    store = store_at(tmp_path / "pressure.sqlite", clocks)
    transitions: list[tuple[str, int]] = []
    store.subscribe(lambda transition, pressure: transitions.append((transition, pressure.level)))
    store.signal(kiro())
    store.signal(kiro())
    store.signal(kiro())  # level 3, held 240 s
    assert transitions == [("enter", 1), ("raise", 2), ("raise", 3)]
    clocks.advance(599)
    assert store.decay() == []
    clocks.advance(1)
    (changed,) = store.decay()
    assert changed.level == 2 and changed.clean_since_wall == clocks.wall
    clocks.advance(599)
    assert store.decay() == []
    clocks.advance(1)
    assert [p.level for p in store.decay()] == [1]
    clocks.advance(600)
    (cleared,) = store.decay()
    assert cleared.level == 0 and cleared.until_wall is None and cleared.until_mono is None
    assert transitions[-1] == ("exit", 0)
    assert [t for t, _ in transitions].count("exit") == 1
    clocks.advance(6000)
    assert store.decay() == []


def test_a_held_account_does_not_decay_even_past_the_clean_window(tmp_path: Path) -> None:
    clocks = Clocks()
    store = store_at(tmp_path / "pressure.sqlite", clocks, clean_seconds=30, cap_seconds=480)
    for _ in range(4):
        store.signal(kiro())  # level 4: held 480 s
    clocks.advance(400)
    assert store.decay() == [] and store.status("kiro").level == 4


def test_listeners_get_enter_once_raise_on_level_two_and_exit_on_clear(tmp_path: Path) -> None:
    clocks = Clocks()
    store = store_at(tmp_path / "pressure.sqlite", clocks)
    seen: list[tuple[str, Pressure]] = []
    store.subscribe(lambda transition, pressure: seen.append((transition, pressure)))
    store.signal(kiro())
    store.signal(kiro())
    cleared = store.clear("kiro")
    assert [(t, p.level) for t, p in seen] == [("enter", 1), ("raise", 2), ("exit", 0)]
    assert cleared.level == 0 and not cleared.held
    store.clear("kiro")  # Nothing set: no second exit.
    assert len(seen) == 3


def test_a_failing_listener_never_blocks_a_signal(tmp_path: Path) -> None:
    store = store_at(tmp_path / "pressure.sqlite", Clocks())

    def broken(transition: str, pressure: Pressure) -> None:
        raise RuntimeError("listener failed")

    store.subscribe(broken)
    assert store.signal(kiro()).level == 1 and store.error == "RuntimeError"


def test_persistence_across_a_second_store(tmp_path: Path) -> None:
    clocks = Clocks()
    path = tmp_path / "pressure.sqlite"
    first = store_at(path, clocks)
    first.signal(kiro())
    first.signal(kiro())
    clocks.advance(30)
    second = store_at(path, clocks)
    status = second.status("kiro")
    assert status.level == 2 and status.until_wall == first.status("kiro").until_wall
    assert status.retry_after(clocks.mono) == pytest.approx(90)
    assert second.snapshot()["kiro:default"]["level"] == 2
    (recent, earlier) = second.signals()
    assert recent["signature"] == "kiro.rate_limit" and set(recent) == {
        "provider",
        "account_hint",
        "harness",
        "source",
        "signature",
        "model",
        "observed_at",
    }
    assert earlier["observed_at"] <= recent["observed_at"]


def test_wall_clock_jumps_never_lengthen_a_hold(tmp_path: Path) -> None:
    clocks = Clocks()
    path = tmp_path / "pressure.sqlite"
    store = store_at(path, clocks)
    store.signal(kiro())  # held 60 s
    clocks.wall += 3600  # The wall clock jumps forward; the monotonic hold is unchanged.
    assert store.status("kiro").retry_after(clocks.mono) == 60
    reopened = store_at(path, clocks)
    assert reopened.status("kiro").retry_after(clocks.mono) == 0
    assert reopened.status("kiro").level == 1  # The level decays; the hold has ended.
    clocks.wall -= 7200  # Backwards: the stored until is now hours ahead.
    backwards = store_at(path, clocks)
    assert backwards.status("kiro").retry_after(clocks.mono) == 480  # Never past the cap.


def test_the_signal_ring_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("answerback_bridge.pressure.store.SIGNAL_RING", 5)
    path = tmp_path / "pressure.sqlite"
    store = store_at(path, Clocks())
    for _ in range(12):
        store.signal(kiro())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 5


async def test_wait_returns_at_once_when_clear_and_after_until_when_held() -> None:
    clocks = Clocks()
    store = store_at(None, clocks)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clocks.advance(seconds)

    assert (await store.wait("kiro", sleep=sleep)).level == 0 and slept == []
    store.signal(kiro())
    done = await store.wait("kiro", sleep=sleep)
    assert slept == [60] and not done.retry_after(clocks.mono)


async def test_wait_rereads_the_hold_after_each_wake() -> None:
    clocks = Clocks()
    store = store_at(None, clocks)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clocks.advance(seconds)
        if len(slept) == 1:
            clocks.advance(-30)  # Woken early, at 30 s, by a new signal on the account.
            store.signal(kiro())

    store.signal(kiro())
    await store.wait("kiro", sleep=sleep)
    assert slept == [60, 120]


def test_disabled_settings_signal_nothing() -> None:
    store = store_at(None, Clocks(), enabled=False)
    assert store.signal(kiro()).level == 0 and store.snapshot()["kiro:default"]["level"] == 0


def test_a_storage_failure_keeps_the_signal_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_at(tmp_path / "pressure.sqlite", Clocks())

    def fail(*args: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_persist", fail)
    assert store.signal(kiro()).level == 1 and store.error == "OperationalError"


def test_clear_refuses_an_unknown_provider_or_hint() -> None:
    store = store_at(None, Clocks())
    with pytest.raises(ValueError, match="Unknown pressure provider"):
        store.clear("openai")
    with pytest.raises(ValueError, match="Pressure account hint"):
        store.clear("kiro", "../x")


def test_file_is_private_and_a_symlink_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state" / "pressure.sqlite"
    store_at(path, Clocks()).signal(kiro())
    assert S_IMODE(path.stat().st_mode) == 0o600
    assert S_IMODE(path.parent.stat().st_mode) == 0o700
    for target_exists in (False, True):
        directory = tmp_path / f"linked-{target_exists}"
        directory.mkdir()
        target = tmp_path / f"target-{target_exists}.sqlite"
        if target_exists:
            target.touch()
            target.chmod(0o640)
        link = directory / "pressure.sqlite"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="Pressure database must not be a symlink"):
            store_at(link, Clocks())
        assert link.is_symlink() and target.exists() is target_exists
        if target_exists:
            assert target.read_bytes() == b"" and S_IMODE(target.stat().st_mode) == 0o640


def test_snapshot_lists_configured_accounts_by_provider_and_hint() -> None:
    store = store_at(None, Clocks(), providers={"kiro": {"account_hint": "work"}})
    assert set(store.snapshot()) == {"bedrock:default", "kiro:work"}
    assert store.snapshot()["kiro:work"] == {
        "provider": "kiro",
        "account_hint": "work",
        "level": 0,
        "until": None,
        "last_signal": None,
        "clean_since": None,
    }


def test_events_json_pressure_block_is_parsed_and_unknown_keys_refused(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    assert Settings.load(config).pressure == PressureSettings()
    (tmp_path / "events.json").write_text(
        json.dumps(
            {
                "pressure": {
                    "base_seconds": 30,
                    "cap_seconds": 240,
                    "clean_seconds": 300,
                    "providers": {"bedrock": {"account_hint": "acct-1", "cap_seconds": 120}},
                }
            }
        )
    )
    settings = Settings.load(config).pressure
    assert settings.delay("kiro", 4) == 240 and settings.delay("bedrock", 4) == 120
    assert settings.hints() == {"kiro": "default", "bedrock": "acct-1"}
    assert settings.effective()["providers"]["bedrock"] == {
        "account_hint": "acct-1",
        "base_seconds": 30.0,
        "cap_seconds": 120.0,
    }
    for bad in (
        {"pressure": {"base": 1}},
        {"pressure": {"providers": {"openai": {}}}},
        {"pressure": {"providers": {"kiro": {"token": "x"}}}},
        {"pressure": {"providers": {"kiro": {"account_hint": "a b"}}}},
        {"pressure": {"cap_seconds": 10, "base_seconds": 60}},
        {"pressure": {"notice": "two\nlines"}},
        {"pressure": {"enabled": "yes"}},
        {"pressure": []},
    ):
        (tmp_path / "events.json").write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            Settings.load(config)
