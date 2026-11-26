"""new_feature: capacity readings at the parser and runner seams."""

import asyncio
import re
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from answerback_bridge.placement import probe
from answerback_bridge.placement.assess import assess
from answerback_bridge.placement.probe import (
    LOCAL,
    PROBE_SCRIPT,
    Reading,
    ReadingCache,
    parse_reading,
    read,
)

FIXTURES = Path(__file__).parent / "fixtures/placement"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (
            "darwin-idle.txt",
            Reading(
                "node",
                100,
                os="Darwin",
                cores=16,
                load1=3.71,
                load5=3.86,
                load15=4.01,
                mem_total_kb=134217728,
                pressure_level=1,
                mem_level=96,
                compressor_kb=0,
                swap_used_kb=0,
                largest_process_kb=8981584,
                largest_process_name="python",
                process_count=1041,
                harness_processes=8,
                suites_running=0,
            ),
        ),
        (
            "darwin-compressor-warning.txt",
            Reading(
                "node",
                100,
                os="Darwin",
                cores=14,
                load1=6.85,
                load5=5.24,
                load15=4.72,
                mem_total_kb=67108864,
                pressure_level=2,
                mem_level=49,
                compressor_kb=29091552,
                swap_used_kb=0,
                largest_process_kb=3923888,
                largest_process_name="backupd",
                process_count=758,
                harness_processes=5,
                suites_running=0,
            ),
        ),
        (
            "darwin-swap-residue.txt",
            Reading(
                "node",
                100,
                os="Darwin",
                cores=16,
                load1=11.20,
                load5=17.62,
                load15=38.31,
                mem_total_kb=134217728,
                pressure_level=1,
                mem_level=84,
                compressor_kb=8933232,
                swap_used_kb=35839805,
                largest_process_kb=3066544,
                largest_process_name="agent",
                process_count=1812,
                harness_processes=28,
                suites_running=0,
            ),
        ),
        (
            "linux-a.txt",
            Reading(
                "node",
                100,
                os="Linux",
                cores=16,
                load1=2.11,
                load5=1.77,
                load15=1.77,
                mem_total_kb=64092432,
                mem_available_kb=45446832,
                swap_total_kb=524284,
                swap_free_kb=503504,
                psi_some_avg10=0,
                largest_process_kb=3485400,
                largest_process_name="frigate.embeddi",
                process_count=706,
                harness_processes=6,
                suites_running=1,
            ),
        ),
        (
            "linux-b.txt",
            Reading(
                "node",
                100,
                os="Linux",
                cores=16,
                load1=0.34,
                load5=0.39,
                load15=0.43,
                mem_total_kb=64092288,
                mem_available_kb=53460212,
                swap_total_kb=8388604,
                swap_free_kb=8363332,
                psi_some_avg10=0,
                largest_process_kb=2491996,
                largest_process_name="python",
                process_count=641,
                harness_processes=2,
                suites_running=0,
            ),
        ),
    ],
)
def test_new_feature_real_fixtures(filename: str, expected: Reading) -> None:
    actual = parse_reading("node", (FIXTURES / filename).read_bytes(), 100)
    assert actual == expected
    assert actual.to_json() == expected.to_json()
    assert list(actual.to_json()) == [
        "machine",
        "taken_at",
        "os",
        "cores",
        "load1",
        "load5",
        "load15",
        "mem_total_kb",
        "pressure_level",
        "mem_level",
        "compressor_kb",
        "swap_used_kb",
        "mem_available_kb",
        "swap_total_kb",
        "swap_free_kb",
        "psi_some_avg10",
        "largest_process_kb",
        "largest_process_name",
        "process_count",
        "harness_processes",
        "suites_running",
        "error",
    ]
    with pytest.raises(FrozenInstanceError):
        actual.cores = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "text",
    [
        "x" * (10 * 1024 * 1024),
        "ignored=1\n" * 100_000,
        "no separator",
        b"\x00\xff\x80",
        "cores=-1\nload=nan inf 1\nmem_total_kb=99999999999999999999999\nprobe_end=1\n",
        "cores=1e3\nload=nan inf 1\nmem_total_kb=100\nprobe_end=1\n",
        "cores=0\nload=100001 -1 1e3\nmem_total_kb=100\nprobe_end=1\n",
        "cores=4097\nload=NaN Infinity +1\nmem_total_kb=100\nprobe_end=1\n",
        "cores=１\nload=１ ２ ３\nmem_total_kb=100\nprobe_end=1\n",
    ],
)
def test_new_feature_hostile_output_is_unparsed(text: str | bytes) -> None:
    """pins_existing: completed answers must still reject unusable CPU values."""
    reading = parse_reading("node", text, 100)
    assert reading.error == "unparsed"
    assert reading.cores is None
    assert reading.load1 is None


@pytest.mark.parametrize("missing", ["cores=", "load="])
def test_pins_existing_missing_cpu_fields_are_unparsed(missing: str) -> None:
    # A platform command can fail while the script still reaches its marker.
    text = (FIXTURES / "linux-b.txt").read_text()
    text = "".join(line for line in text.splitlines(keepends=True) if not line.startswith(missing))
    reading = parse_reading("node", text, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


def test_pins_existing_os_name_is_case_sensitive() -> None:
    text = (FIXTURES / "linux-b.txt").read_text().replace("os=Linux", "os=linux")
    reading = parse_reading("node", text, 100)
    assert reading.os is None
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize("fixture", ["darwin-idle.txt", "linux-b.txt"])
def test_pins_existing_crlf_answer_is_ok(fixture: str) -> None:
    # A transport may turn every LF from the platform probe into CRLF.
    text = (FIXTURES / fixture).read_text().replace("\n", "\r\n")
    reading = parse_reading("node", text, 100)
    assert reading.error is None
    assert assess(reading, now=100).state == "ok"


@pytest.mark.parametrize("name", ["\x1b[31mhidden", "x" * 500, "café", "bad\x00name", "\tbad"])
def test_new_feature_process_names_are_cleaned(name: str) -> None:
    assert parse_reading("node", "largest_name=" + name, 0).largest_process_name == "?"


def test_new_feature_parser_bounds_and_locale() -> None:
    text = (
        "os=Darwin\ncores=4096\nload=100000 0,25 1.5\nmem_total_kb=1125899906842624\nprobe_end=1\n"
    )
    reading = parse_reading("node", text, 0)
    assert (reading.cores, reading.load1, reading.load5, reading.load15) == (
        4096,
        100000,
        0.25,
        1.5,
    )
    assert reading.mem_total_kb == 2**50
    assert reading.error is None
    assert parse_reading("node", "ignored=0\n" * 200 + "cores=1\nload=0 0 0", 0).cores is None
    assert parse_reading("node", "x" * 65536 + "\ncores=1\nload=0 0 0", 0).cores is None
    assert parse_reading("node", "cores=" + "0" * 401 + "1", 0).cores is None


def test_new_feature_derived_memory_is_bounded_and_decimal() -> None:
    reading = parse_reading(
        "node",
        "swap=total = 20M used = 1,50M free = 18.50M\npage_bytes=16384\ncompressor_pages=2",
        0,
    )
    assert reading.swap_used_kb == 1536
    assert reading.compressor_kb == 32
    reading = parse_reading(
        "node",
        "swap=used = 1125899906842624M\npage_bytes=16384\ncompressor_pages=1125899906842624",
        0,
    )
    assert reading.swap_used_kb is None and reading.compressor_kb is None


async def test_new_feature_reads_concurrently_and_cancels_timeout() -> None:
    called: list[str] = []
    cancelled = asyncio.Event()
    all_started = asyncio.Event()

    async def runner(argv: list[str]) -> tuple[int, str]:
        machine = LOCAL if argv[0] == "/bin/sh" else argv[3]
        called.append(machine)
        if len(called) == 4:
            all_started.set()
        await all_started.wait()
        if machine == "slow":
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return 0, (FIXTURES / "linux-a.txt").read_text()

    start = time.monotonic()
    readings = await read(
        [LOCAL, "one", "two", "slow"], runner=runner, timeout=0.3, clock=lambda: 100
    )
    assert time.monotonic() - start < 1.3
    assert len(called) == 4 and cancelled.is_set()
    assert [row.error for row in readings.values()] == [None, None, None, "timeout"]
    assert all(row.taken_at == 100 for row in readings.values())


@pytest.mark.parametrize("failure", [1, FileNotFoundError("fixture missing executable")])
async def test_new_feature_unreachable_is_a_reading(failure: int | Exception) -> None:
    async def runner(argv: list[str]) -> tuple[int, str]:
        if isinstance(failure, Exception):
            raise failure
        return failure, "cores=2\nload=0 0 0"

    assert (await read(["remote"], runner=runner))["remote"].error == "unreachable"


@pytest.mark.parametrize(
    "machines",
    [
        ["good", "-oProxyCommand=x"],
        ["a;b"],
        ["a b"],
        ["../x"],
        ["a/b"],
        [""],
        ["a" * 64],
        ["a\n"],
        ["é"],
        [f"node{i}" for i in range(17)],
    ],
)
async def test_new_feature_names_refused_before_any_start(machines: list[str]) -> None:
    async def runner(argv: list[str]) -> tuple[int, str]:
        pytest.fail("No process may start for a refused request")

    with pytest.raises(ValueError, match="Machine names|16"):
        await read(machines, runner=runner)


async def test_new_feature_exact_argv_and_duplicate_collapse() -> None:
    calls: list[list[str]] = []

    async def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, (FIXTURES / "linux-b.txt").read_text()

    readings = await read(
        [LOCAL, LOCAL, "Node-1.a_b", "Node-1.a_b"],
        runner=runner,
        timeout=1.2,
        hc_binary="/fixture/hc",
    )
    assert list(readings) == [LOCAL, "Node-1.a_b"]
    assert calls == [
        ["/bin/sh", "-c", PROBE_SCRIPT],
        ["/fixture/hc", "exec", "--on", "Node-1.a_b", "--timeout", "2", "sh", "-c", PROBE_SCRIPT],
    ]


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
async def test_new_feature_invalid_timeout_starts_nothing(seconds: float) -> None:
    async def runner(argv: list[str]) -> tuple[int, str]:
        pytest.fail("No process may start for a refused timeout")

    with pytest.raises(ValueError, match="timeout"):
        await read([LOCAL], runner=runner, timeout=seconds)


def test_new_feature_cache_expires_and_replaces() -> None:
    cache = ReadingCache()
    reading = Reading(LOCAL, 100)
    cache.put(reading)
    assert cache.get(LOCAL, 159) is reading
    assert cache.get(LOCAL, 160) is reading
    assert cache.get(LOCAL, 161) is None
    assert cache.get("absent", 100) is None
    newer = Reading(LOCAL, 200)
    cache.put(newer)
    assert cache.get(LOCAL, 200) is newer
    short = ReadingCache(max_age=1)
    short.put(newer)
    assert short.get(LOCAL, 202) is None


@pytest.mark.parametrize("mode", ["success", "timeout", "overflow"])
async def test_new_feature_default_runner_bounds_and_reaps(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        consumed = 0

        async def read(self, size: int) -> bytes:
            if mode == "timeout":
                await asyncio.Future()
            if mode == "overflow":
                self.consumed += size
                return b"x" * size
            if self.consumed:
                return b""
            self.consumed += 1
            return (
                b"os=Linux\ncores=2\nload=0 0 0\nmemtotal_kb=100\nmemavailable_kb=80\nprobe_end=1\n"
            )

    class Child:
        stdout = Stream()
        returncode = 0
        stopped = False

        async def wait(self) -> int:
            return self.returncode

    child = Child()
    monkeypatch.setenv("HC_TERMINAL_ID", "fixture-terminal")
    monkeypatch.setenv("HC_SOCKET", "/fixture/socket")
    monkeypatch.setenv("HC_SESSION", "fixture-inherited")
    monkeypatch.setenv("T96_TEST_VALUE", "fixture-preserved")

    async def spawn(*argv: str, **kwargs: Any) -> Child:
        assert argv[0] == "/fixture/hc"
        # Assert only planted values; never capture or print the real environment.
        planted = {
            name: kwargs["env"].get(name)
            for name in ("HC_TERMINAL_ID", "HC_SOCKET", "HC_SESSION", "T96_TEST_VALUE")
        }
        assert planted == {
            "HC_TERMINAL_ID": None,
            "HC_SOCKET": None,
            "HC_SESSION": "fixture-explicit",
            "T96_TEST_VALUE": "fixture-preserved",
        }
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        return child

    async def stop(proc: Child, **kwargs: Any) -> None:
        assert proc is child
        child.stopped = True

    monkeypatch.setattr(probe, "spawn_owned", spawn)
    monkeypatch.setattr(probe, "stop_child", stop)
    reading = (
        await read(
            ["remote"],
            timeout=0.03,
            hc_binary="/fixture/hc",
            hc_session="fixture-explicit",
        )
    )["remote"]
    assert reading.error == {"success": None, "timeout": "timeout", "overflow": "unreachable"}[mode]
    assert child.stopped
    assert child.stdout.consumed <= 65536


async def test_new_feature_real_local_probe() -> None:
    reading = (await read([LOCAL]))[LOCAL]
    assert reading.error is None
    assert reading.cores is not None and reading.cores >= 1


@pytest.mark.parametrize("fixture", sorted(path.name for path in FIXTURES.glob("*.txt")))
def test_red_at_pretip_probe_and_fixtures_end_with_marker(fixture: str) -> None:
    assert PROBE_SCRIPT.rstrip().endswith('echo "probe_end=1"')
    assert (FIXTURES / fixture).read_text().splitlines()[-1] == "probe_end=1"


@pytest.mark.parametrize("fixture", ["darwin-idle.txt", "linux-b.txt"])
def test_red_at_pretip_every_incomplete_fixture_prefix_is_unparsed(fixture: str) -> None:
    # The transport can return success even when its output stops after any line.
    lines = (FIXTURES / fixture).read_text().splitlines()
    for end in range(1, len(lines) + 1):
        if lines[end - 1] == "probe_end=1":
            continue
        reading = parse_reading("node", "\n".join(lines[:end]) + "\n", 100)
        assert reading.error == "unparsed", f"cut after line {end}"
        assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize(
    "case",
    [
        "darwin-cut",
        "linux-cut",
        "linux-no-free-memory",
        "no-os-cut",
        "four-lines",
        "191-banner-lines",
        "unsupported-os",
        "darwin-no-free-memory",
    ],
)
def test_red_at_pretip_review_broken_readings_are_unknown(case: str) -> None:
    # The review derived all eight forms from these shipped probe fixtures.
    darwin = (FIXTURES / "darwin-idle.txt").read_text()
    linux = (FIXTURES / "linux-b.txt").read_text()
    texts = {
        "darwin-cut": "\n".join(darwin.splitlines()[:9]),
        "linux-cut": "\n".join(linux.splitlines()[:9]),
        "linux-no-free-memory": "\n".join(
            line for line in linux.splitlines() if not line.startswith(("memavailable", "psi_"))
        ),
        "no-os-cut": "\n".join(darwin.splitlines()[1:9]),
        "four-lines": "cores=8\nload=0.1 0.1 0.1\nmem_total_kb=1000000\nlargest_kb=10\n",
        "191-banner-lines": "banner\n" * 191 + darwin,
        "unsupported-os": darwin.replace("os=Darwin", "os=FreeBSD"),
        "darwin-no-free-memory": "\n".join(
            line
            for line in darwin.splitlines()
            if not line.startswith(("pressure", "swap", "page_", "compressor"))
        ),
    }
    reading = parse_reading("node", texts[case], 100)
    assert assess(reading, now=100).state == "unknown"
    if case in {"darwin-cut", "linux-cut", "no-os-cut", "four-lines", "191-banner-lines"}:
        assert reading.error == "unparsed"


@pytest.mark.parametrize("ending", ["", "probe_end=0\n", "probe_end=1\nextra\n"])
def test_red_at_pretip_marker_must_be_the_last_line(ending: str) -> None:
    text = (FIXTURES / "darwin-idle.txt").read_text().removesuffix("probe_end=1\n")
    assert parse_reading("node", text + ending, 100).error == "unparsed"


@pytest.mark.parametrize(("line_end", "empty_lines"), [("\n", 1), ("\n", 3), ("\r\n", 1)])
def test_red_at_pretip_empty_trailing_lines_are_ignored(line_end: str, empty_lines: int) -> None:
    # A transport can add empty lines after the probe's final marker.
    text = (FIXTURES / "darwin-idle.txt").read_text().replace("\n", line_end)
    reading = parse_reading("node", text + line_end * empty_lines, 100)
    assert reading.error is None
    assert assess(reading, now=100).state == "ok"


@pytest.mark.parametrize("trailer", [" \n", "\t\n", "extra\n", "\r\r\n"])
def test_pins_existing_nonempty_trailing_lines_are_unparsed(trailer: str) -> None:
    text = (FIXTURES / "darwin-idle.txt").read_text()
    reading = parse_reading("node", text + trailer, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize(("banner_lines", "empty_lines"), [(0, 400), (185, 1)])
def test_pins_existing_trailing_empty_lines_count_toward_bound(
    banner_lines: int, empty_lines: int
) -> None:
    text = "banner\n" * banner_lines + (FIXTURES / "darwin-idle.txt").read_text()
    reading = parse_reading("node", text + "\n" * empty_lines, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


def test_pins_existing_fix3_lone_cr_counts_toward_bound() -> None:
    # The transport's final lone CR is a physical line even when dropped as empty.
    text = "banner\n" * 185 + (FIXTURES / "darwin-idle.txt").read_text() + "\r"
    reading = parse_reading("node", text, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize("text", ["", "\n\n", "\r"])
def test_pins_existing_fix3_empty_answer_is_unparsed(text: str) -> None:
    # A command/transport can return no measured fields, including only line endings.
    reading = parse_reading("node", text, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


def test_pins_existing_fix3_crlf_space_trailer_is_unparsed() -> None:
    text = (FIXTURES / "darwin-idle.txt").read_text().replace("\n", "\r\n") + " \r\n"
    reading = parse_reading("node", text, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize("loads", ["0.1 x 0.3", "0.1 0.2 x"])
def test_pins_existing_fix3_partial_load_is_unparsed(loads: str) -> None:
    # A malformed later load from the platform command must invalidate the answer.
    text = (FIXTURES / "linux-b.txt").read_text().replace("load=0.34 0.39 0.43", f"load={loads}")
    reading = parse_reading("node", text, 100)
    assert reading.load1 == 0.1
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


def test_pins_existing_fix3_lf_name_removes_one_cr() -> None:
    # A terminal CR on the name is intentionally removed even when other lines use LF.
    text = (
        (FIXTURES / "linux-b.txt")
        .read_text()
        .replace("largest_name=python", "largest_name=python\r")
    )
    reading = parse_reading("node", text, 100)
    assert reading.largest_process_name == "python"
    assert reading.error is None
    assert assess(reading, now=100).state == "ok"


@pytest.mark.parametrize(
    "marker", [" probe_end=1", "probe_end=1 ", "probe_end=1\t", "probe_end=1\r\r"]
)
def test_pins_existing_marker_whitespace_is_unparsed(marker: str) -> None:
    text = (FIXTURES / "darwin-idle.txt").read_text().replace("probe_end=1", marker)
    reading = parse_reading("node", text, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize("banner_lines", [50, 185, 186, 191])
def test_red_at_pretip_banner_cannot_hide_truncation(banner_lines: int) -> None:
    text = "banner\n" * banner_lines + (FIXTURES / "darwin-idle.txt").read_text()
    reading = parse_reading("node", text, 100)
    expected = None if banner_lines <= 185 else "unparsed"
    assert reading.error == expected


@pytest.mark.parametrize(
    "extra",
    [
        "cores=4096\nload=0 0 0\n",
        "os=Darwin\n",
        "largest_name=x\n",
        "probe_end=1\n",
    ],
)
def test_red_at_pretip_duplicate_keys_are_unparsed(extra: str) -> None:
    # Repeated probe fields can come from a second answer or a login banner.
    text = (FIXTURES / "darwin-idle.txt").read_text()
    reading = parse_reading("node", text.replace("probe_end=1\n", extra + "probe_end=1\n"), 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize(
    "banner",
    [
        "==========\nAuthorized use only\n==========\n",
        "=> disk 85% used\n=> 1 update pending\n",
        "ignored=0\nignored=1\n",
    ],
)
def test_red_at_pretip_repeated_banner_keys_are_ignored(banner: str) -> None:
    # The review's transport banners precede a complete measured answer.
    text = banner + (FIXTURES / "darwin-idle.txt").read_text()
    reading = parse_reading("node", text, 100)
    assert reading.error is None
    assert assess(reading, now=100).state == "ok"


def test_red_at_pretip_read_keys_match_probe_output() -> None:
    # pins_existing: a third printing form would evade the echo/print pattern.
    assert "printf" not in PROBE_SCRIPT
    pattern = r'\b(?:echo|print) "([a-z_][a-z0-9_]*)='
    printed = set(re.findall(pattern, PROBE_SCRIPT))
    # Linux awk generates the keys from its /proc/meminfo selection.
    memory = re.search(r'/\^\(([^)]+)\):/.*?print tolower\(\$1\) "(_[a-z]+)="', PROBE_SCRIPT)
    assert memory is not None
    printed.update(name.lower() + memory[2] for name in memory[1].split("|"))
    assert probe.READ_KEYS == printed
    shared_start, branches = PROBE_SCRIPT.split('if [ "$(uname -s)" = Darwin ]; then ')
    darwin, linux = branches.split("; else ")
    linux, shared_end = linux.rsplit("; fi; ", 1)
    shared = set(re.findall(pattern, shared_start + shared_end))
    darwin_keys = set(re.findall(pattern, darwin))
    linux_keys = set(re.findall(pattern, linux))
    linux_keys.update(name.lower() + memory[2] for name in memory[1].split("|"))
    shared |= darwin_keys & linux_keys
    assert probe.DARWIN_MEMORY_KEYS == darwin_keys - shared
    assert probe.LINUX_MEMORY_KEYS == linux_keys - shared
    assert probe.DARWIN_MEMORY_KEYS | probe.LINUX_MEMORY_KEYS | shared == probe.READ_KEYS


@pytest.mark.parametrize(
    ("case", "banner", "control_state"),
    [
        ("linux-low", "mem_total_kb=20000000\n", "full"),
        ("linux-low", "mem_total_kb=20000001\n", "full"),
        ("linux-blind", "memavailable_kb=60000000\n", "unknown"),
        ("darwin-blind", "page_bytes=16384\ncompressor_pages=0\n", "unknown"),
    ],
)
def test_red_at_pretip_review3_leading_probe_fields_are_unparsed(
    case: str, banner: str, control_state: str
) -> None:
    # Finding 1 derives these controls and leading fields from the shipped probe fixtures.
    linux = (FIXTURES / "linux-b.txt").read_text()
    darwin = (FIXTURES / "darwin-idle.txt").read_text()
    controls = {
        "linux-low": linux.replace("memavailable_kb=53460212", "memavailable_kb=3000000"),
        "linux-blind": linux.replace("memavailable_kb=53460212\n", ""),
        "darwin-blind": darwin.replace("pressure=1", "pressure=")
        .replace("page_bytes=16384\n", "")
        .replace("compressor_pages=0\n", ""),
    }
    control = parse_reading("node", controls[case], 100)
    assert control.error is None
    assert assess(control, now=100).state == control_state
    reading = parse_reading("node", banner + controls[case], 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize("banner", ["Mem_total_kb=20000000\n", " mem_total_kb=20000000\n"])
def test_pins_existing_review3_similar_banner_keys_are_ignored(banner: str) -> None:
    # The review's case/space variants are not fields printed by the probe.
    text = (
        (FIXTURES / "linux-b.txt")
        .read_text()
        .replace("memavailable_kb=53460212", "memavailable_kb=3000000")
    )
    control = parse_reading("node", text, 100)
    reading = parse_reading("node", banner + text, 100)
    assert reading == control
    assert reading.error is None
    assert assess(reading, now=100).state == "full"


@pytest.mark.parametrize("fixture", ["darwin-idle.txt", "linux-b.txt"])
def test_red_at_pretip_review3_other_platform_memory_is_unparsed(fixture: str) -> None:
    # A complete answer must not mix fields from the probe's two memory branches.
    text = (FIXTURES / fixture).read_text()
    other = (
        "memtotal_kb memavailable_kb swaptotal_kb swapfree_kb psi_some_avg10"
        if fixture.startswith("darwin")
        else "mem_total_kb pressure mem_level swap page_bytes compressor_pages"
    )
    control = parse_reading("node", text, 100)
    for key in other.split():
        reading = parse_reading("node", text.replace("\n", f"\n{key}=1\n", 1), 100)
        assert reading.error == "unparsed", key
        assert assess(reading, now=100).state == "unknown", key
        assert reading.mem_total_kb == control.mem_total_kb, key
    total = "mem_total_kb" if fixture.startswith("darwin") else "memtotal_kb"
    other_total = "memtotal_kb" if fixture.startswith("darwin") else "mem_total_kb"
    reading = parse_reading("node", text.replace(f"\n{total}=", f"\n{other_total}="), 100)
    assert reading.mem_total_kb is None
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


def test_red_at_pretip_review3_missing_os_is_unparsed() -> None:
    # A lost first line must not leave an apparently complete probe answer.
    text = (FIXTURES / "linux-b.txt").read_text().replace("os=Linux\n", "")
    reading = parse_reading("node", text, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize(
    ("fixture", "state", "signals"),
    [
        (
            "darwin-idle.txt",
            "ok",
            "cpu:ok pressure:ok compressor:ok swap:ok largest_process:ok",
        ),
        (
            "darwin-compressor-warning.txt",
            "full",
            "cpu:ok pressure:busy compressor:full swap:ok largest_process:ok",
        ),
        (
            "darwin-swap-residue.txt",
            "full",
            "cpu:full pressure:ok compressor:ok swap:busy largest_process:ok",
        ),
        ("linux-a.txt", "ok", "cpu:ok available:ok stall:ok largest_process:ok"),
        ("linux-b.txt", "ok", "cpu:ok available:ok stall:ok largest_process:ok"),
    ],
)
def test_pins_existing_review3_fixture_signals(fixture: str, state: str, signals: str) -> None:
    # States and ordered signals captured from the five fixtures at this round's start.
    reading = parse_reading("node", (FIXTURES / fixture).read_bytes(), 100)
    result = assess(reading, now=100)
    assert reading.error is None
    assert result.state == state
    assert " ".join(f"{signal.name}:{signal.state}" for signal in result.signals) == signals


def test_pins_existing_repeated_memory_field_is_unparsed() -> None:
    text = "mem_total_kb=134217728\n" + (FIXTURES / "darwin-idle.txt").read_text()
    reading = parse_reading("node", text, 100)
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


def test_red_at_pretip_refused_value_still_counts_as_duplicate() -> None:
    text = (FIXTURES / "darwin-idle.txt").read_text()
    reading = parse_reading("node", "cores=" + "1" * 401 + "\n" + text, 100)
    assert reading.error == "unparsed"


@pytest.mark.parametrize("fixture", ["darwin-idle.txt", "linux-b.txt"])
@pytest.mark.parametrize("value", ["text", "0", "-1", ""])
def test_red_at_pretip_memory_total_must_be_positive(fixture: str, value: str) -> None:
    # Empty/unparsable totals are possible when the platform command fails.
    lines = (FIXTURES / fixture).read_text().splitlines()
    text = "\n".join(
        line.partition("=")[0] + "=" + value
        if line.startswith(("mem_total_kb=", "memtotal_kb="))
        else line
        for line in lines
    )
    reading = parse_reading("node", text, 100)
    assert reading.mem_total_kb is None
    assert reading.error == "unparsed"
    assert assess(reading, now=100).state == "unknown"


@pytest.mark.parametrize(
    ("available", "expected_error"),
    [(64092288, None), (64092289, "unparsed")],
)
def test_red_at_pretip_linux_available_cannot_exceed_total(
    available: int, expected_error: str | None
) -> None:
    # Both values come from the same /proc/meminfo reading.
    text = (
        (FIXTURES / "linux-b.txt")
        .read_text()
        .replace("memavailable_kb=53460212", f"memavailable_kb={available}")
    )
    reading = parse_reading("node", text, 100)
    assert reading.error == expected_error
    if expected_error:
        assert assess(reading, now=100).state == "unknown"


async def test_pins_existing_leading_dash_refused_before_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def core(*args: Any, **kwargs: Any) -> None:
        pytest.fail("A refused name must not reach command construction")

    monkeypatch.setattr(probe, "Core", core)
    with pytest.raises(ValueError, match="Machine names"):
        probe.validate_machines(["-x"])
    with pytest.raises(ValueError, match="Machine names"):
        await read(["good", "-x"])


@pytest.mark.parametrize(("length", "expected"), [(400, 1), (401, None)])
def test_pins_existing_value_length_boundary(length: int, expected: int | None) -> None:
    text = "cores=" + "0" * (length - 1) + "1\n"
    assert parse_reading("node", text, 100).cores == expected


@pytest.mark.parametrize(("value", "expected"), [(2**50, 2**50), (2**50 + 1, None)])
def test_pins_existing_kilobyte_boundary(value: int, expected: int | None) -> None:
    assert parse_reading("node", f"mem_total_kb={value}\n", 100).mem_total_kb == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("100000", 100000), ("100001", None), ("1e3", None)]
)
def test_pins_existing_load_boundary_and_exponent(value: str, expected: float | None) -> None:
    assert parse_reading("node", f"load={value} 0 0\n", 100).load1 == expected


@pytest.mark.parametrize(("value", "expected"), [("100", 100), ("100.01", None)])
def test_pins_existing_psi_boundary(value: str, expected: float | None) -> None:
    assert parse_reading("node", f"psi_some_avg10={value}\n", 100).psi_some_avg10 == expected


@pytest.mark.parametrize("length", [64, 65])
def test_pins_existing_process_name_length_boundary(length: int) -> None:
    name = "x" * length
    expected = name if length == 64 else "?"
    assert parse_reading("node", f"largest_name={name}\n", 100).largest_process_name == expected


@pytest.mark.parametrize("name", ["x=1", "x); processes=1"])
def test_red_at_pretip_process_name_cannot_imitate_field(name: str) -> None:
    text = (
        (FIXTURES / "linux-b.txt")
        .read_text()
        .replace("largest_name=python", f"largest_name={name}")
    )
    reading = parse_reading("node", text, 100)
    assert reading.largest_process_name == "?"
    assert reading.process_count == 641
    assert reading.error is None


@pytest.mark.parametrize("name", ["x\nprocesses=1", "x\rprocesses=1"])
def test_pins_existing_process_name_rejects_line_breaks(name: str) -> None:
    assert probe.clean_process_name(name) == "?"


@pytest.mark.parametrize("fixture", ["darwin-idle.txt", "linux-b.txt"])
def test_red_at_pretip_crlf_preserves_process_name(fixture: str) -> None:
    text = (FIXTURES / fixture).read_text().replace("\n", "\r\n")
    reading = parse_reading("node", text, 100)
    assert reading.largest_process_name == "python"
    assert reading.error is None
    assert assess(reading, now=100).state == "ok"


@pytest.mark.parametrize("name", ["\rpython", "py\rthon", "python\r"])
def test_pins_existing_crlf_name_rejects_other_carriage_returns(name: str) -> None:
    # Only the CR that terminates the physical line belongs to the transport.
    text = (
        (FIXTURES / "linux-b.txt")
        .read_text()
        .replace("largest_name=python", f"largest_name={name}")
    )
    reading = parse_reading("node", text.replace("\n", "\r\n"), 100)
    assert reading.largest_process_name == "?"
    assert reading.error is None


def test_red_at_pretip_process_name_is_only_its_own_line() -> None:
    text = (FIXTURES / "linux-b.txt").read_text()
    reading = parse_reading("node", text, 100)
    assert reading.largest_process_name == "python"
    assert reading.harness_processes == 2
    # A line break cannot add text to the label or overwrite an earlier probe field.
    reading = parse_reading(
        "node", text.replace("largest_name=python", "largest_name=x\nprocs=1"), 100
    )
    assert reading.largest_process_name == "x"
    assert reading.process_count == 641
    assert reading.error == "unparsed"
