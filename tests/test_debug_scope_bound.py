"""T-56 review round 7 (finding B4): the bound on a stored scope, pinned by its number.

docs/TRACING.md and CHANGELOG.md state the bound as 8192 characters. At
``7d65216`` every test read it from the implementation's `MAX_SCOPE_CHARS`, so
a wrong constant passed all of them. Here the lengths 8191, 8192 and 8193 are
written as numbers, for `on()`, for `off()` and for the loader of the saved
file. The loader's files are JSON text written out by hand, in the shape
`DebugController._save` writes; none is a controller's own output.

Every test in this module is ``pins_existing``: it passes at ``7d65216``, the
lane's tip before this round, and it goes red when the constant is wrong
(the mutation runs are in the hand-back). The last test is of review round 8
(finding 4) and is ``pins_existing`` against ``62090f3``.
"""

from pathlib import Path

import pytest

from answerback_bridge.adapters.contracts import Peer
from answerback_bridge.config import Config
from answerback_bridge.events import debug as module
from answerback_bridge.events.debug import DebugController
from answerback_bridge.plugin import kernel_context
from answerback_bridge.providers.herdr.adapters.herdr import HerdrAdapter
from answerback_bridge.service import Bridge

NOW = 702.0
# One lease as `on()` stores it (events/debug.py: `{"started": now, "until": now + seconds}`).
LEASE = '{"started": 702.0, "until": 1302.0}'
# core.py:285 builds the peer id of a local Core terminal as "hc:<terminal>".
CORE = "hc:t61"


def start(directory: Path, text: str) -> DebugController:
    """A bridge start on a runtime directory whose saved file holds ``text``."""
    (directory / "debug").mkdir(parents=True)
    (directory / "debug" / "leases.json").write_text(text)
    return DebugController(directory, clock=lambda: NOW)


def assert_failed_closed(restored: DebugController) -> None:
    assert restored.leases == {}
    assert restored.excluded == set()
    assert restored.error == "ValueError"
    assert restored.status()["scopes"] == []
    assert restored.status()["excluded"] == []
    assert restored.until(CORE) is None


def scope_of(length: int) -> str:
    """A scope of exactly ``length`` characters in the form of workers.py:182.

    Provenance, stated honestly: "kiro:<alias>:<cwd>" is the form of a Kiro
    receipt target, and every directory name here is at most 200 characters,
    under the 255 a file system allows for one name. The total length is
    synthetic. workers.py:171 reserves only for a directory that exists, so a
    real path ends at 4,095 characters (Linux PATH_MAX) and no Kiro or native
    CLI reserve path builds a scope near 8,192 characters. That is said of
    those forms only: a Herdr target has no length bound on the bridge's side
    (the last test of this module). The bound is a chosen limit on the saved
    file, and these scopes exist to stand exactly on it.
    """
    prefix = "kiro:work:"
    names, rest = divmod(length - len(prefix), 200)
    if rest == 1:  # A lone "/" is no directory name: take one whole name back.
        names, rest = names - 1, 201
    path = ("/" + "d" * 199) * names + ("/" + "d" * (rest - 1) if rest else "")
    scope = prefix + path
    assert len(scope) == length
    assert all(0 < len(name) <= 255 for name in path.split("/")[1:])
    return scope


def test_the_documented_bound_is_8192_characters() -> None:
    """B4: the number in docs/TRACING.md and CHANGELOG.md, as a number.

    Label: pins_existing. At 7d65216 every test read the bound from
    `MAX_SCOPE_CHARS`, so a wrong constant passed all of them.
    """
    assert module.MAX_SCOPE_CHARS == 8192
    assert module.scope_refusal(scope_of(8193)) == "Debug scope is longer than 8192 characters"


@pytest.mark.parametrize(("length", "allowed"), [(8191, True), (8192, True), (8193, False)])
def test_on_stands_on_the_bound_of_8192_characters(
    tmp_path: Path, length: int, allowed: bool
) -> None:
    """B4 for `on()`: 8191 and 8192 are stored, 8193 is refused. Label: pins_existing."""
    scope = scope_of(length)
    controller = DebugController(tmp_path, clock=lambda: NOW)
    controller.on(CORE)
    if allowed:
        controller.on(scope)
    else:
        with pytest.raises(ValueError, match="^Debug scope is longer than 8192 characters$"):
            controller.on(scope)
    assert set(controller.leases) == ({CORE, scope} if allowed else {CORE})
    restarted = DebugController(tmp_path, clock=lambda: NOW)
    assert restarted.error is None
    assert restarted.leases == controller.leases


@pytest.mark.parametrize(("length", "allowed"), [(8191, True), (8192, True), (8193, False)])
def test_off_stands_on_the_bound_of_8192_characters(
    tmp_path: Path, length: int, allowed: bool
) -> None:
    """B4 for `off()` under an `all` lease: the same three lengths. Label: pins_existing."""
    scope = scope_of(length)
    controller = DebugController(tmp_path, clock=lambda: NOW)
    controller.on("all")
    controller.off(CORE)
    if allowed:
        controller.off(scope)
    else:
        with pytest.raises(ValueError, match="^Debug scope is longer than 8192 characters$"):
            controller.off(scope)
    assert controller.excluded == ({CORE, scope} if allowed else {CORE})
    restarted = DebugController(tmp_path, clock=lambda: NOW)
    assert restarted.error is None
    assert set(restarted.leases) == {"all"}
    assert restarted.excluded == controller.excluded


@pytest.mark.parametrize(("length", "allowed"), [(8191, True), (8192, True), (8193, False)])
def test_the_loader_stands_on_the_bound_of_8192_characters_for_a_lease(
    tmp_path: Path, length: int, allowed: bool
) -> None:
    """B4 for a lease key in a hand-written file. Label: pins_existing."""
    scope = scope_of(length)
    restored = start(
        tmp_path,
        '{"leases": {"hc:t61": ' + LEASE + ', "' + scope + '": ' + LEASE + '}, "excluded": []}',
    )
    if allowed:
        assert restored.error is None
        assert set(restored.leases) == {CORE, scope}
        assert restored.until(scope) == 1302.0
    else:
        assert_failed_closed(restored)


@pytest.mark.parametrize(("length", "allowed"), [(8191, True), (8192, True), (8193, False)])
def test_the_loader_stands_on_the_bound_of_8192_characters_for_an_exclusion(
    tmp_path: Path, length: int, allowed: bool
) -> None:
    """B4 for an exclusion in a hand-written file. Label: pins_existing."""
    scope = scope_of(length)
    restored = start(
        tmp_path,
        '{"leases": {"all": ' + LEASE + '}, "excluded": ["hc:t61", "' + scope + '"]}',
    )
    if allowed:
        assert restored.error is None
        assert set(restored.leases) == {"all"}
        assert restored.excluded == {CORE, scope}
        assert restored.until(scope) is None
    else:
        assert_failed_closed(restored)


async def test_a_herdr_target_has_no_length_bound_of_its_own_and_meets_this_one(
    tmp_path: Path,
) -> None:
    """Review round 8, finding 4: 8,192 is a storage policy, not the longest target.

    Label: pins_existing. adapters/herdr.py:117 builds
    "herdr:<session>:<terminal_id>" from the terminal id as Herdr gives it and
    does not bound its length (adapters/herdr.py:182-183 reserves the same
    form). The target here is built by `HerdrAdapter.prepare`; the terminal id
    of 8,192 characters is synthetic, and no Herdr id of that length is known.
    Such a target is past the bound, so a debug lease on it is refused.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    config = Config(provider_selection=["herdr"], herdr_session="review")
    adapter = HerdrAdapter(kernel_context(config, bridge.config_path, bridge.store), lambda: None)
    peer = Peer.from_record(
        {
            "peer_id": "herdr:review",
            "provider": "herdr",
            "target": "review",
            "terminal_id": "t" * 8192,
        }
    )
    target = (await adapter.prepare(peer)).target_key
    assert target == "herdr:review:" + "t" * 8192
    assert len(target) == 8205
    assert module.scope_refusal(target) == "Debug scope is longer than 8192 characters"
    controller = DebugController(tmp_path, clock=lambda: NOW)
    with pytest.raises(ValueError, match="^Debug scope is longer than 8192 characters$"):
        controller.on(target)
    assert controller.leases == {}
