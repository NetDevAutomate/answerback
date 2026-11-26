"""How a worker on another machine puts its result file in place (review 2, finding M3).

The brief typed into a terminal on another machine gives three steps: a command
that makes the result folder private, the name of a draft file inside that folder,
and a command that renames the draft to the name the bridge reads. These tests take
the lines from `CoreTasks.envelope` itself, in the order it prints them, run them
with `/bin/sh` in a temporary `HOME` whose name holds a space, and look at the disk.
The worker's own part, writing the draft, is one `printf` to the name as printed.

Fix round `ans-fix1` (review 1, Q4) set the modes; fix round `ans-fix2` moved the
draft into the folder; fix round 3 (review 3, N1) made step 2 say how the draft
is written, and its tests are labelled against `72859ac6` (`red_at_tip`). Other
labels say what a test shows at the start commit of `ans-fix2`, `0b4c2707`:
`red_at_pretip` fails there, `pins_existing` passes there on purpose. At `0b4c2707`
the brief names no draft file, so every test that runs the steps is `red_at_pretip`.
"""

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from test_core_result_file import (
    MACHINE,
    REMOTE,
    FakeCore,
    delivered,
    make_bridge,
    make_fake,
    today_local_envelope,
)

from answerback_bridge.providers.herdr.adapters.core import parse_result
from answerback_bridge.providers.herdr.core import RESULT_DIRECTORY, result_path, validate_message

TASK = "0d9e7c1a-5b7e-4a52-9c7b-0f4f8a1c2e3d"
TOKEN = "".join(f"{digit:x}" for digit in range(16)) * 2
BODY = f"answerback-result {TASK} {TOKEN}\nAll checks passed.\nanswerback-end {TOKEN}\n"
RESULT = result_path(TASK, TOKEN)
DRAFT = RESULT.removesuffix(".txt") + ".draft"
# The first word of every line that the brief could print as a shell command.
COMMAND_WORDS = {"mkdir", "chmod", "mv", "cp", "cd", "ln", "install", "umask", "touch", "cat"}
MODES = ["at once", "one at a time"]
# Bytes of the remote brief around an empty message (fix round 3).
OVERHEAD = 1805


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


@pytest.fixture
async def bridge(fake: FakeCore, tmp_path: Path) -> Any:
    bridge = make_bridge(fake, tmp_path)
    yield bridge
    await bridge.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home dir"
    home.mkdir()
    return home


def brief(bridge: Any) -> str:
    return bridge.core_tasks.envelope(TASK, "Review", token=TOKEN)


def steps(envelope: str) -> list[tuple[str, str]]:
    """The brief's steps in printed order: ("command", line) or ("draft", name line)."""
    found = []
    for line in envelope.split("\n\n", 1)[1].splitlines():
        if line.split(" ", 1)[0] in COMMAND_WORDS:
            found.append(("command", line))
        elif line.startswith('"$HOME/'):
            found.append(("draft", line))
    return found


def commands(envelope: str) -> list[str]:
    return [line for kind, line in steps(envelope) if kind == "command"]


def worker_lines(envelope: str, body: str = BODY) -> list[str]:
    """What a worker that follows the brief runs: its commands, and the draft written
    to the printed name where the brief names it."""
    return [
        line if kind == "command" else f"printf %s {shlex.quote(body)} > {line}"
        for kind, line in steps(envelope)
    ]


def sh(home: Path, umask: str, script: str) -> int:
    """Run `script` as the worker's shell would: its own `HOME`, its own umask."""
    done = subprocess.run(
        ["/bin/sh", "-c", f"umask {umask}\n{script}"],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return done.returncode


def run(home: Path, umask: str, lines: list[str], how: str) -> list[int]:
    """All lines in one shell, or each line in a shell of its own. Exit codes."""
    if how == "at once":
        return [sh(home, umask, "\n".join(lines))]
    return [sh(home, umask, line) for line in lines]


def publish(bridge: Any, home: Path, umask: str, how: str) -> tuple[Path, Path, Path]:
    """Follow the brief; just before its last command, note the draft's inode and
    whether the final name exists. Returns the folder, the result and the draft."""
    lines = worker_lines(brief(bridge))
    assert len(lines) == 3
    probe = (
        f'ls -i "$HOME/{DRAFT}" > "$HOME/probe"; '
        f'test -e "$HOME/{RESULT}" && echo early >> "$HOME/probe"; true'
    )
    codes = run(home, umask, [*lines[:-1], probe, lines[-1]], how)
    assert set(codes) == {0}
    return home / RESULT_DIRECTORY, home / RESULT, home / DRAFT


def parses(line: str) -> bool:
    """Whether `/bin/sh` reads `line` as a complete command (nothing is run)."""
    done = subprocess.run(["/bin/sh", "-n", "-c", line], capture_output=True, check=False)
    return done.returncode == 0


def device(path: Path) -> int:
    return path.stat().st_dev


def folder_on_another_file_system(home: Path) -> Path | None:
    """A new folder on a file system other than `home`'s, or None where there is none."""
    for candidate in ("/dev/shm", "/var/tmp", str(Path.home()), str(Path(__file__).parent)):
        root = Path(candidate)
        if root.is_dir() and os.access(root, os.W_OK) and device(root) != device(home):
            return Path(tempfile.mkdtemp(prefix="answerback-results-", dir=root))
    return None


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def assert_published(home: Path, result: Path, draft: Path) -> None:
    """The result is the draft, renamed: same inode, and it was not there before."""
    probe = (home / "probe").read_text().split()
    assert "early" not in probe  # the final name did not exist before the last line
    assert result.stat().st_ino == int(probe[0])  # a rename, not a copy
    assert not draft.exists()
    assert result.read_text() == BODY
    assert mode(result) == 0o600
    assert mode(result.parent) == 0o700


@pytest.mark.parametrize("how", MODES)
@pytest.mark.parametrize("umask", ["022", "077", "000"])
async def test_steps_leave_a_private_folder_and_file_whatever_the_umask(
    bridge: Any, home: Path, umask: str, how: str
) -> None:
    """red_at_pretip: the lines one at a time and all at once end in the same state."""
    folder, result, draft = publish(bridge, home, umask, how)
    assert_published(home, result, draft)
    assert [entry.name for entry in folder.iterdir()] == [result.name]


@pytest.mark.parametrize("how", MODES)
@pytest.mark.parametrize("umask", ["022", "077"])
async def test_steps_narrow_a_folder_that_exists_with_mode_0755(
    bridge: Any, home: Path, umask: str, how: str
) -> None:
    """red_at_pretip. At `0b4c2707` the lines run one at a time left the folder 0755."""
    folder = home / RESULT_DIRECTORY
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    (folder / "earlier.txt").write_text("kept")
    folder, result, draft = publish(bridge, home, umask, how)
    assert_published(home, result, draft)
    assert (folder / "earlier.txt").read_text() == "kept"


@pytest.mark.parametrize("umask", ["022", "000"])
async def test_folder_is_private_before_the_draft_is_written_inside_it(
    bridge: Any, home: Path, umask: str
) -> None:
    """red_at_pretip: the draft is private from its first byte, whatever its own mode."""
    printed = steps(brief(bridge))
    assert [kind for kind, _ in printed] == ["command", "draft", "command"]
    assert printed[1][1] == f'"$HOME/{DRAFT}"'
    assert Path(DRAFT).parent == Path(RESULT).parent == Path(RESULT_DIRECTORY)
    lines = worker_lines(brief(bridge))
    assert sh(home, umask, lines[0]) == 0
    folder = home / RESULT_DIRECTORY
    assert mode(folder) == 0o700
    assert list(folder.iterdir()) == []
    assert sh(home, umask, lines[1]) == 0
    assert mode(folder) == 0o700
    assert [entry.name for entry in folder.iterdir()] == [Path(DRAFT).name]
    assert not (home / RESULT).exists()


async def test_every_command_line_is_complete_and_every_path_is_quoted(bridge: Any) -> None:
    """red_at_pretip: at `0b4c2707` the first line ends in `&&` and the draft's path
    is not quoted."""
    lines = commands(brief(bridge))
    assert len(lines) == 2
    for line in lines:
        assert not line.rstrip().endswith(("&&", "||", "|", "\\"))
        assert parses(line), line
        outside_quotes = re.sub(r'"[^"]*"', "", line)
        assert "/" not in outside_quotes and "$" not in outside_quotes, line
    last = lines[-1]
    assert f'mv "$HOME/{DRAFT}" "$HOME/{RESULT}"' in last
    assert last.index("chmod 600 ") < last.index("mv ")  # private before it is in place
    text = brief(bridge)
    for words in ("one step", "somewhere else", "one command"):
        assert words not in text


@pytest.mark.parametrize("how", MODES)
async def test_rename_stays_inside_a_result_folder_on_another_file_system(
    bridge: Any, home: Path, how: str
) -> None:
    """red_at_pretip: `results` is a symbolic link, owned by the account, to a folder
    on another file system. The draft is written through the link, so the last step
    is still a rename (same inode). Skipped where no second file system is writable."""
    other = folder_on_another_file_system(home)
    if other is None:
        pytest.skip("no writable folder on a second file system")
    try:
        other.chmod(0o755)
        (home / ".answerback").mkdir()
        (home / RESULT_DIRECTORY).symlink_to(other, target_is_directory=True)
        folder, result, draft = publish(bridge, home, "022", how)
        assert folder.is_symlink()
        assert mode(other) == 0o700
        assert_published(home, result, draft)
        assert device(result) == device(other) != device(home)
        assert [entry.name for entry in other.iterdir()] == [result.name]
    finally:
        shutil.rmtree(other, ignore_errors=True)


@pytest.mark.parametrize("how", MODES)
async def test_draft_left_by_an_interrupted_earlier_try_is_replaced(
    bridge: Any, home: Path, how: str
) -> None:
    """red_at_pretip: the draft's name is fixed for the task, so a second try writes
    over what the first left; only the finished text is ever renamed."""
    folder = home / RESULT_DIRECTORY
    folder.mkdir(parents=True)
    leftover = home / DRAFT
    leftover.write_text(f"answerback-result {TASK} {TOKEN}\nhalf a res")
    leftover.chmod(0o644)
    folder, result, draft = publish(bridge, home, "022", how)
    assert_published(home, result, draft)
    assert [entry.name for entry in folder.iterdir()] == [result.name]


@pytest.mark.parametrize("how", MODES)
async def test_steps_that_fail_publish_nothing(bridge: Any, home: Path, how: str) -> None:
    """pins_existing: with a file where the folder should be, every line fails by
    itself and no result appears, in one shell and in separate shells. (At `0b4c2707`
    it passes too: there the first line alone is not a command at all.)"""
    (home / ".answerback").write_text("a file where the folder should be")
    codes = run(home, "022", worker_lines(brief(bridge)), how)
    assert codes[-1] != 0
    assert 0 not in codes
    assert (home / ".answerback").read_text() == "a file where the folder should be"
    assert not (home / RESULT).exists()


async def test_reader_is_never_given_the_drafts_name(bridge: Any, fake: FakeCore) -> None:
    """red_at_pretip by its first assertion only (at `0b4c2707` the brief names no
    draft); the rest passes there, because the reading side is unchanged. It builds
    one name per receipt, the `.txt` one, and `Core.read_result` refuses any other
    before `hc` runs; so a complete result under the draft's name completes nothing."""
    task = await delivered(bridge, REMOTE, "Review", "draft-unread")
    token = task["metadata"]["result_token"]
    final = result_path(task["id"], token)
    draft = final.removesuffix(".txt") + ".draft"
    assert f'"$HOME/{draft}"' in fake.sends()[0][-1]  # the name this brief gave the worker
    with pytest.raises(ValueError, match="not a bridge result file"):
        await bridge.core.read_result(MACHINE, draft)
    assert fake.reads() == []
    fake.put_file(draft, f"answerback-result {task['id']} {token}\nDone.\nanswerback-end {token}\n")
    seen = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert seen["status"] == "submitted" and seen["result"] is None
    assert fake.reads() != []
    assert {read[-1] for read in fake.reads()} == {final}


def step_two(envelope: str) -> str:
    """What the brief says about writing the draft: from "Step 2." up to "Step 3."."""
    return envelope[envelope.index("Step 2.") : envelope.index("Step 3.")]


def tool_path(envelope: str, home: Path) -> Path:
    """The draft's path as step 2 tells a tool without a shell to take it: the home
    folder in place of `$HOME`, and no quotation marks."""
    (printed,) = [line for kind, line in steps(envelope) if kind == "draft"]
    return Path(printed.strip('"').replace("$HOME", str(home)))


def write_as_printed(work: Path, printed: str, text: str) -> Path:
    """What a tool whose working folder is `work` does with the path line as printed."""
    wrong = work / printed
    wrong.parent.mkdir(parents=True)
    wrong.write_text(text)
    return wrong


def on_disk(fake: FakeCore, home: Path, final: str) -> None:
    """Give the reader what the worker's machine holds under the name it reads."""
    if (home / final).exists():
        fake.put_file(final, (home / final).read_text())


async def remote_brief(bridge: Any, fake: FakeCore, request: str) -> tuple[dict[str, Any], str]:
    task = await delivered(bridge, REMOTE, "Review", request)
    return task, fake.sends()[0][-1]


def framed(task: dict[str, Any], result: str) -> str:
    token = task["metadata"]["result_token"]
    return f"answerback-result {task['id']} {token}\n{result}\nanswerback-end {token}\n"


async def test_step_two_says_the_draft_is_written_whole_and_not_appended_to(
    bridge: Any, fake: FakeCore, home: Path
) -> None:
    """red_at_tip (`72859ac6`) by the words; the two runs pass there. Review 3, N1 (a):
    a draft written whole over an earlier finished one gives the new result; the
    same text appended to it gives a result that holds the earlier draft's lines."""
    task, text = await remote_brief(bridge, fake, "whole")
    words = step_two(text).lower()
    assert "whole" in words and "append" in words
    final = result_path(task["id"], task["metadata"]["result_token"])
    first, last = commands(text)
    draft = tool_path(text, home)
    assert sh(home, "022", first) == 0
    draft.write_text(framed(task, "An earlier try."))
    with draft.open("a") as file:  # what the brief says not to do
        file.write(framed(task, "Done."))
    appended = draft.read_text()
    draft.write_text(framed(task, "Done."))  # what the brief says to do
    assert sh(home, "022", last) == 0
    on_disk(fake, home, final)
    seen = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert seen["status"] == "completed" and seen["result"] == "Done."
    token = task["metadata"]["result_token"]
    verdict, body = parse_result(appended, task["id"], token)
    assert verdict == "body" and body is not None
    assert "An earlier try." in body and f"answerback-end {token}" in body


async def test_draft_written_by_a_tool_to_the_path_without_shell_marks_is_published_and_read(
    bridge: Any, fake: FakeCore, home: Path
) -> None:
    """red_at_tip by the words; the run passes there. Review 3, N1 (b): step 2 tells a
    tool that is no shell to put the home folder in place of `$HOME` and to leave the
    quotation marks out. A file written to that path (no shell, here Python) is
    renamed by step 3 and completes the receipt. The path line stays shell text."""
    task, text = await remote_brief(bridge, fake, "tool")
    final = result_path(task["id"], task["metadata"]["result_token"])
    printed = f'"$HOME/{final.removesuffix(".txt")}.draft"'
    before, _, after = step_two(text).partition(printed)
    assert after  # the quoted line is there, unchanged
    assert "home folder in place of $HOME" in before
    assert "no quotation marks" in before
    first, last = commands(text)
    assert sh(home, "077", first) == 0
    target = tool_path(text, home)
    assert target == home / (final.removesuffix(".txt") + ".draft")
    target.write_text(framed(task, "Done by a tool."))
    assert sh(home, "077", last) == 0
    assert mode(home / final) == 0o600 and not target.exists()
    on_disk(fake, home, final)
    seen = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert seen["status"] == "completed" and seen["result"] == "Done by a tool."


async def test_draft_written_to_the_path_as_printed_by_a_tool_is_never_published(
    bridge: Any, fake: FakeCore, home: Path, tmp_path: Path
) -> None:
    """pins_existing: why step 2 says it. A tool given the line as printed, with the
    marks and a literal `$HOME`, writes below its working folder; step 3 then fails
    and the reader finds nothing."""
    task, text = await remote_brief(bridge, fake, "literal")
    final = result_path(task["id"], task["metadata"]["result_token"])
    (printed,) = [line for kind, line in steps(text) if kind == "draft"]
    first, last = commands(text)
    assert sh(home, "022", first) == 0
    written = write_as_printed(tmp_path / "work", printed, framed(task, "Lost."))
    assert home not in written.parents
    assert sh(home, "022", last) != 0
    assert not (home / final).exists()
    on_disk(fake, home, final)
    seen = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert seen["status"] == "submitted" and seen["result"] is None


async def test_step_two_names_three_parts_of_the_draft_and_says_its_never(bridge: Any) -> None:
    """red_at_tip. Review 3, N1 (c): each marker line is called the draft's line, the
    result is named as the part between them, and no sentence of step 2 starts with
    a bare "Its"."""
    text = step_two(brief(bridge))
    lines = text.splitlines()
    head = lines.index(f"answerback-result {TASK} {TOKEN}")
    tail = lines.index(f"answerback-end {TOKEN}")
    assert lines[-1] == f"answerback-end {TOKEN}"
    assert lines[head - 1].endswith("The draft's first line is exactly:")
    assert lines[tail - 1].startswith("The draft's last line")
    assert lines[tail - 1].endswith("is exactly:")
    between = " ".join(lines[head + 1 : tail - 1])
    assert "the result itself" in between
    assert not re.search(r"\bIts\b|\bits\b", text)


def stated_bounds(envelope: str) -> tuple[int, int]:
    """The two numbers step 2 gives for the result's length."""
    found = re.search(r"from (\d+) to (\d+) characters", step_two(envelope))
    assert found, step_two(envelope)
    return int(found[1]), int(found[2])


@pytest.mark.parametrize(
    ("edge", "accepted"), [("least", True), ("most", True), ("under", False), ("over", False)]
)
async def test_reader_accepts_and_refuses_exactly_what_step_two_says(
    bridge: Any, fake: FakeCore, edge: str, accepted: bool
) -> None:
    """red_at_tip: there step 2 gives no lower bound. Review 3, N1 (c): the reader takes
    a result of the least and of the greatest length the text states, and refuses
    one character less and one more, as the text says."""
    task, text = await remote_brief(bridge, fake, f"edge-{edge}")
    least, most = stated_bounds(text)
    assert "refused" in step_two(text)
    size = {"least": least, "most": most, "under": least - 1, "over": most + 1}[edge]
    result = "é" * size
    final = result_path(task["id"], task["metadata"]["result_token"])
    fake.put_file(final, framed(task, result))
    seen = await bridge.call("task_reconcile", {"task_id": task["id"]})
    if accepted:
        assert seen["status"] == "completed" and seen["result"] == result
    else:
        assert seen["status"] == "submitted" and seen["result"] is None
        assert "Result file" in seen["detail"]


async def test_last_marker_that_shares_a_line_with_the_result_completes_nothing(
    bridge: Any, fake: FakeCore
) -> None:
    """red_at_tip by the words; the reader's part passes there. Why step 2 says that
    the draft's last line has nothing else on it."""
    task, text = await remote_brief(bridge, fake, "shared-line")
    assert "with nothing else on it" in step_two(text)
    token = task["metadata"]["result_token"]
    final = result_path(task["id"], token)
    fake.put_file(final, f"answerback-result {task['id']} {token}\nDone. answerback-end {token}\n")
    seen = await bridge.call("task_reconcile", {"task_id": task["id"]})
    assert seen["status"] == "submitted" and seen["result"] is None


async def test_only_step_two_changed_since_72859ac6(bridge: Any) -> None:
    """pins_existing for every line it names: review 3, N1 (d). The two commands, the
    quoted path line, the two marker lines, the order and the text before step 2
    and from step 3 on are byte for byte those of `72859ac6`."""
    text = brief(bridge)
    name = f"$HOME/{RESULT.removesuffix('.txt')}"
    assert text.startswith(
        f"Bridge task: {TASK}\nReview\n\n"
        "This assignment came from another machine. "
        "Its result is collected from a file on this machine.\n"
        "Do not run any answerback command and do not call task_complete: "
        "neither can reach the sender.\n"
        "When the assignment is complete, do these three steps in order.\n"
        "Step 1. Run this command. It makes the result folder, for this account only "
        "(mode 0700):\n"
        'mkdir -p "$HOME/.answerback/results" && chmod 700 "$HOME/.answerback/results"\n'
        "Step 2. "
    )
    assert text.endswith(
        f"answerback-end {TOKEN}\n"
        "Step 3. Run this command. It sets the draft to mode 0600 and renames it inside "
        "that folder:\n"
        f'chmod 600 "{name}.draft" && mv "{name}.draft" "{name}.txt"\n'
        "Do not change the renamed file afterwards. Keep normal tool approvals. "
        "If approval or work is outstanding,\n"
        "say so in this terminal and do not run step 3."
    )
    middle = step_two(text).splitlines()
    assert middle.count(f'"{name}.draft"') == 1
    assert middle.count(f"answerback-result {TASK} {TOKEN}") == 1
    assert [kind for kind, _ in steps(text)] == ["command", "draft", "command"]


async def test_remote_brief_has_one_length_for_every_task_id_and_token(bridge: Any) -> None:
    """The brief's fixed text has one length whatever the task ID and the token are,
    so the bound checked before reservation holds for the real ones. `pins_existing`
    at `0b4c2707` for the one length; the number is the overhead since fix round 3
    (1,507 bytes at `72859ac6`), which leaves 129,266 bytes for the message."""
    ids = ["00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"]
    lengths = {
        len(bridge.core_tasks.envelope(task, "", token=token).encode())
        for task in ids
        for token in ("0" * 32, "f" * 32)
    }
    assert lengths == {OVERHEAD}
    room = "x" * (131_071 - OVERHEAD)
    validate_message(bridge.core_tasks.envelope(ids[0], room, token="0" * 32))
    with pytest.raises(ValueError):
        validate_message(bridge.core_tasks.envelope(ids[0], room + "x", token="0" * 32))
    for task, token in ((ids[0] + "f", "f" * 32), (ids[0], "f" * 33)):
        with pytest.raises(ValueError, match="result file"):
            bridge.core_tasks.envelope(task, "", token=token)


async def test_local_envelope_is_byte_for_byte_what_it_was(bridge: Any) -> None:
    """pins_existing: without a token the text is the one of `e84b70b` (and of
    `51933f2`): no result file, no folder, no mode."""
    local = bridge.core_tasks.envelope(TASK, "Review")
    assert local == today_local_envelope(bridge.config_path, TASK, "Review")
    assert local == bridge.core_tasks.envelope(TASK, "Review", token=None)
    for word in ("mkdir", "chmod", "mv ", RESULT_DIRECTORY, "$HOME"):
        assert word not in local
