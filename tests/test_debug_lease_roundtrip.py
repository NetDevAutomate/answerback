"""T-56 round 6 (finding M-1/R-1): what the debug controller can store, it can load.

Every test drives a real ``DebugController`` on ``tmp_path`` and reads the
result back through a second instance on the same runtime directory, as a
bridge restart does. The scope forms are the ones the bridge itself produces;
each test or table row names the source line that produces its form.

Each test carries one label:

- ``red_at_pretip``: fails at ``eb7ce9b``, the lane's tip before this round.
- ``pins_existing``: passes at ``eb7ce9b`` too; it pins behaviour that must
  stay as it is.
"""

import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.config import Config
from answerback_bridge.events import debug as module
from answerback_bridge.events.debug import DebugController
from answerback_bridge.service import Bridge
from answerback_bridge.transport import runtime_dir

REMOTE_TEXT = "Remote peer debug is unavailable until T-29"

# core.py:285 builds the peer id of a local Core terminal as "hc:<terminal>".
CORE = "hc:t61"
# workers.py:182 reserves a Kiro receipt as "kiro:<alias>:<cwd>"; config.py:189-193
# asks only that <cwd> is an absolute path, so a directory name with a space is real.
SPACE = "kiro:work:/tmp/My Project"
# The same form with a working directory that makes the target longer than the
# 256 characters `local_peer()` allows for text a caller typed: seven directory
# names of 40 characters, 301 characters in all. This is the string form only;
# no directory is created (review round 7, N1: it used to be one name of 260
# characters, which no file system holds). A receipt whose long directory
# exists is in tests/test_debug.py,
# `test_debug_on_a_long_receipt_target_is_granted_not_remote`.
LONG = "kiro:work:/tmp" + ("/" + "a" * 40) * 7
# core.py:22 and core.py:285: a terminal on another machine is "hc:<machine>/<terminal>".
REMOTE = "hc:example-host/t61"


class Clock:
    def __init__(self, value: float = 702.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def bound() -> int:
    """The length bound of a stored scope; the constant does not exist at eb7ce9b."""
    return module.MAX_SCOPE_CHARS


def longest_real_form() -> str:
    """A string as long as the longest native CLI target a reserve path can build on Linux.

    native_workers.py:116 builds "<provider>:<alias>:<cwd>". The longest provider
    name in adapters/catalog.py is "claude_code" (11 characters), config.py:121
    bounds the alias to 32 characters, and native_workers.py:110 submits only to
    a working directory that exists. On Linux a path is at most 4,095 characters
    (PATH_MAX of 4,096 counts the closing NUL), which gives 4,140 characters.

    That is the longest of the forms with a bound that were analysed (Kiro and
    the native CLI workers), not the longest target of any provider (review
    round 8, finding 4): a Herdr target takes the terminal id as Herdr gives
    it, without a length bound (adapters/herdr.py:117 and :182-183; see
    tests/test_debug_scope_bound.py, the last test).

    Provenance, stated honestly (review round 7, N1): the path is twenty
    directory names of 200 characters and one of 74, each under the 255 that
    one name may have. It is a string and no directory is created. On macOS
    PATH_MAX is 1,024, so no worker there can build a target this long; the
    form tests the handling of the length, not a receipt that was produced.
    """
    path = ("/" + "d" * 200) * 20 + "/" + "d" * 74
    assert len(path) == 4095
    scope = "claude_code:" + "w" * 32 + ":" + path
    assert len(scope) == 4140
    return scope


def write_file(directory: Path, leases: dict[str, Any], excluded: Any) -> None:
    (directory / "debug").mkdir(parents=True, exist_ok=True)
    (directory / "debug" / "leases.json").write_text(
        json.dumps({"leases": leases, "excluded": excluded})
    )


def lease(clock: Clock) -> dict[str, float]:
    return {"started": clock.value, "until": clock.value + 600}


def test_two_leases_one_with_a_space_survive_a_restart(tmp_path: Path) -> None:
    """T1, the reported case. Label: red_at_pretip.

    `debug on <task id>` of a Kiro receipt grants the stored target SPACE. At
    eb7ce9b the loader then cleared this lease and the unrelated CORE lease.
    """
    clock = Clock()
    first = DebugController(tmp_path, clock=clock)
    first.on(CORE)
    first.on(SPACE)
    second = DebugController(tmp_path, clock=clock)
    assert second.error is None
    assert second.leases == first.leases
    assert set(second.leases) == {CORE, SPACE}
    assert second.until(CORE) == clock.value + 600
    assert second.until(SPACE) == clock.value + 600


@pytest.mark.parametrize("target", [LONG, longest_real_form], ids=["over-256", "longest-real"])
def test_a_long_target_survives_a_restart(tmp_path: Path, target: Any) -> None:
    """T2, long. Label: red_at_pretip.

    A target string longer than 256 characters, and one as long as the longest
    native CLI target a reserve path can build on Linux (4,140 characters, see
    `longest_real_form`). Neither directory is created.
    """
    scope = target() if callable(target) else target
    assert len(scope) > 256
    clock = Clock()
    first = DebugController(tmp_path, clock=clock)
    first.on(CORE)
    first.on(scope)
    second = DebugController(tmp_path, clock=clock)
    assert second.error is None
    assert second.leases == first.leases
    assert set(second.leases) == {CORE, scope}


@pytest.mark.parametrize("target", [SPACE, LONG], ids=["space", "over-256"])
def test_an_exclusion_of_a_receipt_target_survives_a_restart(tmp_path: Path, target: str) -> None:
    """T2, excluded. Label: red_at_pretip.

    `debug on all`, then `debug off <task id>` of a Kiro receipt: the stored
    target becomes an exclusion of the `all` lease.
    """
    clock = Clock()
    first = DebugController(tmp_path, clock=clock)
    first.on("all")
    first.off(target)
    assert first.excluded == {target}
    second = DebugController(tmp_path, clock=clock)
    assert second.error is None
    assert set(second.leases) == {"all"}
    assert second.excluded == {target}
    assert second.until(target) is None
    assert second.until(CORE) == clock.value + 600


def test_any_sequence_of_grants_and_removals_reloads_to_the_same_state(tmp_path: Path) -> None:
    """R2, the round trip, over a fixed pseudo-random sequence. Label: red_at_pretip.

    After every successful call a new controller on the same directory holds the
    same unexpired leases and the same exclusions, with no error. The pool holds
    one stored form of each provider (the table below names their source lines).
    """
    pool = [
        CORE,
        SPACE,
        LONG,
        "herdr:main:term_1",
        "desktop:3f0c7a52-9d1e-4b6a-8c2f-5e7d1a9b0c44",
        "crew:http://127.0.0.1:8123:chat-1",
        "fixtureprov:thread-1",
        "rex:terminal:session-1/block-1",
        "codex_cli:work:/Users/me@corp/My Project",
        "plugin:t1",
    ]
    chooser = random.Random(56)
    clock = Clock()
    controller = DebugController(tmp_path, clock=clock)
    for step in range(300):
        action = chooser.choice(["on", "on", "on_all", "off", "off", "off_all", "wait"])
        scope = chooser.choice(pool)
        if action == "on":
            controller.on(scope, chooser.choice([0.25, 7, 600, 3600]))
        elif action == "on_all":
            controller.on("all", chooser.choice([0.25, 7, 600, 3600]))
        elif action == "off":
            controller.off(scope)
        elif action == "off_all":
            controller.off(chooser.choice([None, "all"]))
        else:
            clock.value += chooser.choice([0.25, 1, 7, 600])
            controller.status()  # A read applies expiry, as the collector's reads do.
        reloaded = DebugController(tmp_path, clock=clock)
        assert reloaded.error is None, (step, action, scope)
        assert reloaded.leases == controller.leases, (step, action, scope)
        assert reloaded.excluded == controller.excluded, (step, action, scope)


# The decision table (T3). Columns: id, the form (a callable when it needs the
# bound), may it be a lease, may it be an exclusion, where the form comes from,
# and the label of the lease row and of the exclusion row.
TABLE: list[tuple[str, Any, bool, bool, str, str, str]] = [
    ("all", "all", True, False, "the keyword", "pins_existing", "pins_existing"),
    ("hc", CORE, True, True, "core.py:285 peer id", "pins_existing", "pins_existing"),
    (
        "hc-receipt",
        "hc:default:t61",
        True,
        True,
        "core_tasks.py:67 receipt target",
        "pins_existing",
        "pins_existing",
    ),
    (
        "herdr",
        "herdr:main:term_1",
        True,
        True,
        "adapters/herdr.py:183 receipt target",
        "pins_existing",
        "pins_existing",
    ),
    (
        "desktop",
        "desktop:3f0c7a52-9d1e-4b6a-8c2f-5e7d1a9b0c44",
        True,
        True,
        "adapters/codex.py:230 receipt target",
        "pins_existing",
        "pins_existing",
    ),
    (
        "codex-registration",
        "codex:work",
        True,
        True,
        "adapters/catalog.py: the registration name of the Desktop provider",
        "pins_existing",
        "pins_existing",
    ),
    (
        "kiro-scan",
        "kiro:work",
        True,
        True,
        "adapters/kiro.py:41 peer id",
        "pins_existing",
        "pins_existing",
    ),
    (
        "kiro-cwd",
        "kiro:work:/tmp/project",
        True,
        True,
        "workers.py:182 receipt target",
        "pins_existing",
        "pins_existing",
    ),
    ("crew", "crew:chat-1", True, True, "crew.py:164 peer id", "pins_existing", "pins_existing"),
    (
        "crew-receipt",
        "crew:http://127.0.0.1:8123:chat-1",
        True,
        True,
        "adapters/crew.py:118 receipt target",
        "pins_existing",
        "pins_existing",
    ),
    (
        "plugin",
        "fixtureprov:thread-1",
        True,
        True,
        "a plugin's receipt target, <provider>:<thread>",
        "pins_existing",
        "pins_existing",
    ),
    (
        "rex",
        "rex:3f0c7a52-9d1e-4b6a-8c2f-5e7d1a9b0c44",
        True,
        True,
        "rex_mailbox.py:144 peer id and receipt target",
        "pins_existing",
        "pins_existing",
    ),
    (
        "rex-terminal",
        "rex:terminal:session-1/block-1",
        True,
        True,
        "rex.py:460 peer id: a '/' after the second ':'",
        "pins_existing",
        "pins_existing",
    ),
    (
        "codex_cli",
        "codex_cli:work:/tmp/project",
        True,
        True,
        "native_workers.py:116 receipt target",
        "pins_existing",
        "pins_existing",
    ),
    (
        "claude_code",
        "claude_code:work:/tmp/project",
        True,
        True,
        "native_workers.py:116 receipt target",
        "pins_existing",
        "pins_existing",
    ),
    (
        "grok",
        "grok:work:/tmp/project",
        True,
        True,
        "native_workers.py:116 receipt target",
        "pins_existing",
        "pins_existing",
    ),
    (
        "cwd-space",
        SPACE,
        True,
        True,
        "workers.py:182, a cwd with a space",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "cwd-at-sign",
        "kiro:work:/Users/me@corp/project",
        True,
        True,
        "workers.py:182, a cwd with '@' and '/' after the second ':'",
        "pins_existing",
        "pins_existing",
    ),
    (
        "cwd-not-ascii",
        "kiro:work:/tmp/caf\u00e9-\u9879\u76ee",
        True,
        True,
        "workers.py:182, a cwd with letters outside ASCII (the file stores them escaped)",
        "pins_existing",
        "pins_existing",
    ),
    (
        "cwd-over-256",
        LONG,
        True,
        True,
        "workers.py:182, a long cwd",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "cwd-control-character",
        "kiro:work:/tmp/tab\there",
        True,
        True,
        "workers.py:182: a directory name may hold a tab; config.py:192 asks only that the "
        "path is absolute",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "cwd-newline",
        "codex_cli:work:/tmp/two\nlines",
        True,
        True,
        "native_workers.py:116: a directory name may hold a newline",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "longest-real",
        longest_real_form,
        True,
        True,
        "native_workers.py:116, a string with the longest provider, alias and Linux path",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "unknown-provider",
        "plugin:t1",
        True,
        True,
        "a provider `definition()` does not know: what a lease of a plug-in looks like "
        "once the plug-in is gone (row T-36)",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "longest-allowed",
        lambda: "kiro:work:/" + "a" * (bound() - 11),
        True,
        True,
        "exactly MAX_SCOPE_CHARS characters",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "one-over-the-bound",
        lambda: "kiro:work:/" + "a" * (bound() - 10),
        False,
        False,
        "MAX_SCOPE_CHARS + 1 characters",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "remote",
        REMOTE,
        False,
        False,
        "core.py:285 for a remote terminal",
        "pins_existing",
        "red_at_pretip",
    ),
    (
        "remote-user-host",
        "hc:user@host/t1",
        False,
        False,
        "a user@host machine prefix",
        "pins_existing",
        "red_at_pretip",
    ),
    ("empty", "", False, False, "no reserve path builds it", "red_at_pretip", "red_at_pretip"),
    (
        "no-colon",
        "no-colon-at-all",
        False,
        False,
        "no reserve path builds it; adapters/contracts.py:56 demands '<provider>:'",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "no-provider",
        ":t61",
        False,
        False,
        "no reserve path builds it",
        "red_at_pretip",
        "red_at_pretip",
    ),
    (
        "no-target",
        "hc:",
        False,
        False,
        "no reserve path builds it",
        "red_at_pretip",
        "red_at_pretip",
    ),
    ("number", 123, False, False, "not a string", "red_at_pretip", "red_at_pretip"),
    ("null", None, False, False, "not a string", "red_at_pretip", "red_at_pretip"),
    ("list", ["hc:t61"], False, False, "not a string", "red_at_pretip", "red_at_pretip"),
]


def table(side: int) -> list[Any]:
    return [
        pytest.param(row[1], row[side], id=f"{row[0]}-{row[4 + side - 1]}")
        for row in TABLE
        # JSON has no key that is not a string, and `off(None)` means "every
        # scope", so those rows exist only where they can be asked.
        if not (side == 2 and not isinstance(row[1], str | Callable))
        and not (side == 3 and row[1] is None)
    ]


def accepted(call: Callable[[], None]) -> bool:
    try:
        call()
    except ValueError:
        return False
    return True


def holds(items: Any, scope: Any) -> bool:
    """`scope in items`, also for a value that cannot be a member (a list)."""
    try:
        return scope in items
    except TypeError:
        return False


@pytest.mark.parametrize(("form", "expected"), table(2))
def test_decision_table_for_a_lease(tmp_path: Path, form: Any, expected: bool) -> None:
    """T3, lease side: `on()` accepts a scope exactly when a saved file with it loads.

    The row id ends with the row's label.
    """
    scope = form() if callable(form) else form
    clock = Clock()
    grants = tmp_path / "grants"
    controller = DebugController(grants, clock=clock)
    controller.on(CORE)
    stored = accepted(lambda: controller.on(scope)) and holds(controller.leases, scope)

    files = tmp_path / "files"
    write_file(files, {CORE: lease(clock), scope: lease(clock)}, [])
    restored = DebugController(files, clock=clock)
    loaded = restored.error is None and holds(restored.leases, scope)

    assert (stored, loaded) == (expected, expected)
    # What the controller wrote itself always loads, whatever `on()` answered.
    reloaded = DebugController(grants, clock=clock)
    assert reloaded.error is None
    assert reloaded.leases == controller.leases
    assert CORE in reloaded.leases
    # A refused file is refused whole: fail-closed, not "skip the bad entry".
    if not loaded:
        assert restored.leases == {}
        assert restored.error


@pytest.mark.parametrize(("form", "expected"), table(3))
def test_decision_table_for_an_exclusion(tmp_path: Path, form: Any, expected: bool) -> None:
    """T3, exclusion side: `off()` under an `all` lease stores a scope as an
    exclusion exactly when a saved file with that exclusion loads.

    The row id ends with the row's label.
    """
    scope = form() if callable(form) else form
    clock = Clock()
    grants = tmp_path / "grants"
    controller = DebugController(grants, clock=clock)
    controller.on("all")
    controller.off(CORE)
    accepted(lambda: controller.off(scope))
    stored = holds(controller.excluded, scope)

    files = tmp_path / "files"
    write_file(files, {"all": lease(clock)}, [CORE, scope])
    restored = DebugController(files, clock=clock)
    loaded = restored.error is None and holds(restored.excluded, scope)

    assert (stored, loaded) == (expected, expected)
    reloaded = DebugController(grants, clock=clock)
    assert reloaded.error is None
    assert reloaded.leases == controller.leases
    assert reloaded.excluded == controller.excluded
    if not loaded:
        assert restored.leases == {}
        assert restored.error


def test_a_lease_survives_its_provider_leaving_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap in the brief. Label: red_at_pretip.

    A plugin reserves "<provider>:<thread>". Row T-36 makes a provider a plug-in,
    so `definition("<provider>")` can stop answering between a grant and the next
    start. The stored-scope rule never asks `definition()`, so nothing is lost.
    """
    clock = Clock()
    first = DebugController(tmp_path, clock=clock)
    first.on(CORE)
    first.on("fixtureprov:thread-1")
    first.on("all")
    first.off("fixtureprov:thread-2")
    monkeypatch.setattr(module, "definition", lambda _name: None)
    second = DebugController(tmp_path, clock=clock)
    assert second.error is None
    assert second.leases == first.leases
    assert second.excluded == {"fixtureprov:thread-2"}


def test_off_refuses_a_remote_address_and_stores_nothing(tmp_path: Path) -> None:
    """T4, no address that names a machine is stored. Label: red_at_pretip.

    `Bridge.debug_off` already refuses a remote address a user types, with the
    T-29 text (`test_debug_off_refuses_remote_and_user_host_addresses` in
    tests/test_debug.py), so the controller refuses too instead of doing
    nothing silently. At eb7ce9b `off()` stored the address as an exclusion and
    the next start cleared every lease.
    """
    clock = Clock()
    controller = DebugController(tmp_path, clock=clock)
    controller.on("all")
    controller.off(CORE)
    generation = controller.generation
    for remote in (REMOTE, "hc:user@host/t1", "hc:user@host"):
        with pytest.raises(ValueError, match=f"^{REMOTE_TEXT}$"):
            controller.off(remote)
    assert controller.excluded == {CORE}
    assert controller.generation == generation
    reloaded = DebugController(tmp_path, clock=clock)
    assert reloaded.error is None
    assert set(reloaded.leases) == {"all"}
    assert reloaded.excluded == {CORE}


def test_off_refuses_a_remote_address_without_an_all_lease_too(tmp_path: Path) -> None:
    """T4: the refusal does not depend on which leases are active. Label: red_at_pretip.

    No lease on an address that names a machine can exist (`on()` and the loader
    refuse one), so the refusal never keeps a user from removing a lease.
    """
    clock = Clock()
    controller = DebugController(tmp_path, clock=clock)
    controller.on(CORE)
    with pytest.raises(ValueError, match=f"^{REMOTE_TEXT}$"):
        controller.off(REMOTE)
    assert set(controller.leases) == {CORE}
    assert DebugController(tmp_path, clock=clock).leases == controller.leases


def test_on_still_refuses_a_remote_address_with_the_same_text(tmp_path: Path) -> None:
    """R3 for a lease; the text other code matches is unchanged. Label: pins_existing."""
    controller = DebugController(tmp_path, clock=Clock())
    for remote in (REMOTE, "hc:user@host/t1", "hc:user@host"):
        with pytest.raises(ValueError, match=f"^{REMOTE_TEXT}$"):
            controller.on(remote)
    assert controller.leases == {}
    assert not controller.path.exists()


def test_a_scope_over_the_bound_is_refused_with_a_text_of_its_own(tmp_path: Path) -> None:
    """R5: `on()` and `off()` name the bound, and store nothing. Label: red_at_pretip."""
    clock = Clock()
    controller = DebugController(tmp_path, clock=clock)
    controller.on("all")
    too_long = "kiro:work:/" + "a" * (bound() - 10)
    assert len(too_long) == bound() + 1
    for call in (controller.on, controller.off):
        with pytest.raises(ValueError) as refusal:
            call(too_long)
        assert str(refusal.value) == f"Debug scope is longer than {bound()} characters"
        assert "T-29" not in str(refusal.value)
    assert set(controller.leases) == {"all"}
    assert controller.excluded == set()
    reloaded = DebugController(tmp_path, clock=clock)
    assert reloaded.error is None
    assert set(reloaded.leases) == {"all"}


@pytest.mark.parametrize("scope", ["", "no-colon-at-all", ":t61", "hc:", 123, ["hc:t61"]])
def test_a_scope_that_is_no_address_is_refused_with_a_text_of_its_own(
    tmp_path: Path, scope: Any
) -> None:
    """The rest of the rule: a stored scope is `all` or `<provider>:<peer>`.

    Label: red_at_pretip (at eb7ce9b `on()` stored the string forms, and `off()`
    stored the string forms and the number as an exclusion).
    """
    clock = Clock()
    controller = DebugController(tmp_path, clock=clock)
    controller.on("all")
    for call in (controller.on, controller.off):
        with pytest.raises(ValueError) as refusal:
            call(scope)
        assert str(refusal.value) == "Debug scope must be all or a local provider:peer address"
    assert set(controller.leases) == {"all"}
    assert controller.excluded == set()
    assert DebugController(tmp_path, clock=clock).error is None


def test_a_duration_below_the_clock_resolution_is_refused_not_stored(tmp_path: Path) -> None:
    """R2 for the deadline of a lease. Label: red_at_pretip.

    `Bridge.debug_on` hands `seconds` from the command socket to `on()` as a
    float (service.py:428). At a real epoch time a float clock resolves about
    0.24 microseconds, so a shorter duration gives `until == started`. At
    eb7ce9b `on()` stored that lease, and the loader, which demands
    `0 < until - started`, then cleared every lease.
    """
    clock = Clock(1_795_000_000.0)
    controller = DebugController(tmp_path, clock=clock)
    controller.on(CORE)
    assert clock.value + 1e-9 == clock.value
    with pytest.raises(ValueError, match="^Debug duration must be positive"):
        controller.on(SPACE, 1e-9)
    assert set(controller.leases) == {CORE}
    reloaded = DebugController(tmp_path, clock=clock)
    assert reloaded.error is None
    assert reloaded.leases == controller.leases


def test_a_deadline_rounded_past_the_maximum_is_refused_not_stored(tmp_path: Path) -> None:
    """R2 for the deadline of a lease. Label: red_at_pretip.

    In the hour before 2**31 seconds (2038-01-19T03:14:08Z) `now + 3600` lands
    where a float is half as fine, so the sum can round up and
    `until - started` is a little more than the 3,600 seconds the loader
    allows. At eb7ce9b `on()` stored that lease and the next start cleared
    every lease. The next float, where the sum is exact, is granted and loads.
    """
    start = float(2**31 - 1800)
    step = 2.0**-22  # The distance between two floats just below 2**31.
    rounds_up = next(
        start + count * step
        for count in range(1, 9)
        if (start + count * step + 3600) - (start + count * step) > 3600
    )
    clock = Clock(rounds_up)
    controller = DebugController(tmp_path, clock=clock)
    controller.on(CORE, 600)
    with pytest.raises(ValueError, match="^Debug duration must be positive and at most 60"):
        controller.on(SPACE, 3600)
    assert set(controller.leases) == {CORE}
    reloaded = DebugController(tmp_path, clock=clock)
    assert reloaded.error is None
    assert reloaded.leases == controller.leases

    clock.value = rounds_up + step
    assert (clock.value + 3600) - clock.value == 3600
    controller.on(SPACE, 3600)
    reloaded = DebugController(tmp_path, clock=clock)
    assert reloaded.error is None
    assert set(reloaded.leases) == {CORE, SPACE}


def unreadable_files(clock: Clock) -> list[Any]:
    good = {CORE: lease(clock)}
    everything = {"all": lease(clock), **good}
    return [
        pytest.param({**good, REMOTE: lease(clock)}, [], id="remote-lease-pins_existing"),
        pytest.param(everything, [REMOTE], id="remote-exclusion-pins_existing"),
        pytest.param(
            {**good, "kiro:work:/" + "a" * 100_000: lease(clock)},
            [],
            id="lease-far-over-the-bound-pins_existing",
        ),
        pytest.param(
            everything,
            ["kiro:work:/" + "a" * 100_000],
            id="exclusion-far-over-the-bound-pins_existing",
        ),
        pytest.param(everything, [123], id="exclusion-not-a-string-pins_existing"),
        pytest.param(everything, [None], id="exclusion-null-pins_existing"),
        pytest.param(everything, [["hc:t61"]], id="exclusion-a-list-pins_existing"),
        pytest.param(everything, ["all"], id="exclusion-all-pins_existing"),
        pytest.param(everything, "hc:t61", id="exclusions-one-string-pins_existing"),
        pytest.param({**good, "": lease(clock)}, [], id="empty-lease-key-pins_existing"),
        pytest.param({**good, "garbage": lease(clock)}, [], id="lease-with-no-colon-pins_existing"),
        pytest.param(
            {**good, SPACE: {"started": clock.value, "until": clock.value + 3601}},
            [],
            id="bad-deadline-on-a-space-target-pins_existing",
        ),
        pytest.param({**good, SPACE: {"enabled": True}}, [], id="no-deadline-pins_existing"),
        pytest.param({**good, SPACE: "on"}, [], id="lease-not-an-object-pins_existing"),
        pytest.param(
            {f"hc:t{index}": lease(clock) for index in range(201)},
            [],
            id="too-many-leases-pins_existing",
        ),
        pytest.param(
            everything,
            [f"hc:t{index}" for index in range(201)],
            id="too-many-exclusions-pins_existing",
        ),
        pytest.param([CORE], [], id="leases-a-list-pins_existing"),
    ]


@pytest.mark.parametrize(("leases", "excluded"), unreadable_files(Clock()))
def test_a_file_the_controller_cannot_have_written_still_fails_closed(
    tmp_path: Path, leases: Any, excluded: Any
) -> None:
    """T5 and R4: one bad entry clears every lease and sets `error`; nothing is skipped.

    Label: pins_existing, for every row.
    """
    clock = Clock()
    write_file(tmp_path, leases, excluded)
    restored = DebugController(tmp_path, clock=clock)
    assert restored.leases == {}
    assert restored.excluded == set()
    assert restored.error
    assert restored.status()["scopes"] == []
    assert restored.until(CORE) is None


def test_a_scope_one_over_the_bound_in_a_file_fails_closed(tmp_path: Path) -> None:
    """T5 at the exact bound. Label: red_at_pretip (the constant is new)."""
    clock = Clock()
    over = "kiro:work:/" + "a" * (bound() - 10)
    write_file(tmp_path, {CORE: lease(clock), over: lease(clock)}, [])
    restored = DebugController(tmp_path, clock=clock)
    assert restored.leases == {}
    assert restored.error == "ValueError"


def kiro_receipt(bridge: Bridge, target: str, request_key: str) -> dict[str, Any]:
    # The call workers.py:181-186 makes for a Kiro run, as the existing tests in
    # tests/test_debug.py reserve one.
    alias, cwd = target.split(":", 2)[1:]
    task, _ = bridge.store.reserve(
        target,
        "Task T-56. PRIVATE BRIEF",
        request_key,
        {"kind": "kiro", "alias": alias, "cwd": cwd, "completion": "acp_prompt_response"},
    )
    return task


@pytest.mark.parametrize("target", [SPACE, LONG], ids=["space", "over-256"])
async def test_debug_on_by_task_id_survives_a_bridge_restart(tmp_path: Path, target: str) -> None:
    """T6, through the service. Label: red_at_pretip.

    `Bridge.debug_on` with the task id of a receipt whose target holds a space
    (or is long), beside an unrelated lease; then a new `Bridge` on the same
    configuration path, which is what a restart builds.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    task = kiro_receipt(bridge, target, "debug-T56-roundtrip")
    await bridge.call("debug_on", {"scope": CORE})
    granted = await bridge.call("debug_on", {"scope": task["id"]})
    assert {entry["scope"] for entry in granted["scopes"]} == {CORE, target}

    reloaded = DebugController(runtime_dir(bridge.config_path))
    assert reloaded.error is None
    assert reloaded.leases == bridge.debug.leases

    restarted = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    status = await restarted.call("debug_status", {})
    assert status["error"] is None
    assert {entry["scope"] for entry in status["scopes"]} == {CORE, target}


async def test_debug_off_by_task_id_under_all_survives_a_bridge_restart(tmp_path: Path) -> None:
    """T6, through the service, for an exclusion. Label: red_at_pretip."""
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    task = kiro_receipt(bridge, SPACE, "debug-T56-roundtrip-off")
    await bridge.call("debug_on", {"scope": "all"})
    result = await bridge.call("debug_off", {"scope": task["id"]})
    assert result["excluded"] == [SPACE]

    reloaded = DebugController(runtime_dir(bridge.config_path))
    assert reloaded.error is None
    assert set(reloaded.leases) == {"all"}
    assert reloaded.excluded == {SPACE}

    restarted = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    status = await restarted.call("debug_status", {})
    assert status["error"] is None
    assert [entry["scope"] for entry in status["scopes"]] == ["all"]
    assert status["excluded"] == [SPACE]


async def test_debug_off_of_a_remote_receipt_stores_nothing(tmp_path: Path) -> None:
    """T4 through the service. Label: pins_existing.

    A receipt whose peer is on another machine (core.py:285 stores
    "hc:<machine>/<terminal>" in `metadata.peer.peer_id`; rows T-84 and T-90
    make such a receipt possible). `resolve_scope()` refuses it before the
    controller is asked, by task id and as a typed address, and nothing reaches
    the saved file.
    """
    bridge = Bridge(Config(provider_selection=[]), tmp_path / "config.json")
    task, _ = bridge.store.reserve(
        "hc:default:example-host/t61",
        "Task T-56. PRIVATE BRIEF",
        "debug-T56-remote",
        {"kind": "hc", "target": "example-host/t61", "peer": {"peer_id": REMOTE}},
    )
    await bridge.call("debug_on", {"scope": "all"})
    for scope in (task["id"], "debug-T56-remote", REMOTE):
        with pytest.raises(ValueError, match="T-29"):
            await bridge.call("debug_off", {"scope": scope})
    assert bridge.debug.excluded == set()
    reloaded = DebugController(runtime_dir(bridge.config_path))
    assert reloaded.error is None
    assert set(reloaded.leases) == {"all"}
    assert reloaded.excluded == set()
