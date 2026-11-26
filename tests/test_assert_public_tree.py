"""Integration tests for the public-tree provider-reference guard."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
TOOL = PROJECT / "tools" / "assert_public_tree.py"
# Keep this spelling constructed so the test suite stays clean when the guard
# scans its own tracked tree. The indirection is deliberate. Do not simplify it.
TARGET = "".join(("a", "ki"))


def _commit_tree(root: Path, files: dict[str, bytes | str]) -> None:
    """Create a committed git tree containing files."""
    for relative_path, contents in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, bytes):
            path.write_bytes(contents)
        else:
            path.write_text(contents, encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "test tree",
        ],
        cwd=root,
        check=True,
    )


def _tree(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    """Return a small committed repository with the standard empty allowlist."""
    root = tmp_path / "repository"
    root.mkdir()
    _commit_tree(
        root,
        {
            "tools/public-tree-allowlist.txt": "# Deliberately empty.\n",
            **files,
        },
    )
    return root


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the tool through its subprocess command-line seam."""
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), *arguments],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_clean_tree_exits_zero(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/clean.py": "provider = 'herdr'\n"})

    result = _run(root)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("summary:")
    assert "hits=0" in result.stdout


def test_content_path_and_mixed_case_hits_exit_one(tmp_path: Path) -> None:
    uppercase_target = TARGET.upper()
    root = _tree(
        tmp_path,
        {
            "src/provider.py": f"provider = '{uppercase_target}'\n",
            f"docs/{uppercase_target}.md": "A clean filename-only test.\n",
        },
    )

    result = _run(root)

    assert result.returncode == 1
    assert result.stdout.splitlines()[:2] == [
        f"docs/{uppercase_target}.md:0: docs/{uppercase_target}.md",
        f"src/provider.py:1: provider = '{uppercase_target}'",
    ]


def test_decision_a_matches_camel_case_but_not_english_run_ons(tmp_path: Path) -> None:
    title_target = TARGET.title()
    all_caps_target = TARGET.upper()
    root = _tree(
        tmp_path,
        {
            "src/identifiers.py": "\n".join(
                [
                    f"class {title_target}Adapter:",
                    f"{title_target}Workers(",
                    f"get{title_target}()",
                    f"{title_target}1",
                    f"{all_caps_target}REM",
                    title_target,
                    TARGET[0] + TARGET[1].upper() + TARGET[2],
                    "making",
                    "flakiness",
                    "akis",
                    "yakisoba",
                    "myaki",
                    "Akira",
                ]
            )
            + "\n",
            f"sub/{title_target}Adapter.py": "clean\n",
        },
    )

    result = _run(root)

    assert result.returncode == 1
    assert f"sub/{title_target}Adapter.py:0:" in result.stdout
    for line in range(1, 8):
        assert f"src/identifiers.py:{line}:" in result.stdout
    for line in range(8, 14):
        assert f"src/identifiers.py:{line}:" not in result.stdout


def test_all_caps_references_after_lowercase_identifiers_are_hits(tmp_path: Path) -> None:
    all_caps_target = TARGET.upper()
    root = _tree(
        tmp_path,
        {"src/identifiers.py": f"get{all_caps_target}\nis{all_caps_target}()\n"},
    )

    result = _run(root)

    assert result.returncode == 1
    assert "src/identifiers.py:1:" in result.stdout
    assert "src/identifiers.py:2:" in result.stdout


def test_identifier_boundary_matches_identifier_separators_only(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "docs/boundaries.txt": "\n".join(
                [
                    TARGET,
                    f"{TARGET}_register",
                    f"answerback-{TARGET}",
                    f"{TARGET}:ui-123",
                    f"y{TARGET}soba",
                    f"{TARGET}s",
                    f"my{TARGET}",
                ]
            )
            + "\n",
        },
    )

    result = _run(root)

    assert result.returncode == 1
    assert result.stdout.splitlines()[:4] == [
        f"docs/boundaries.txt:1: {TARGET}",
        f"docs/boundaries.txt:2: {TARGET}_register",
        f"docs/boundaries.txt:3: answerback-{TARGET}",
        f"docs/boundaries.txt:4: {TARGET}:ui-123",
    ]
    assert f"y{TARGET}soba" not in result.stdout
    assert f"{TARGET}s" not in result.stdout
    assert f"my{TARGET}" not in result.stdout


def test_title_and_true_mixed_case_whole_words_are_hits(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"docs/case.txt": f"{TARGET.title()}\n{TARGET[0] + TARGET[1].upper() + TARGET[2]}\n"},
    )

    result = _run(root)

    assert result.returncode == 1
    assert "docs/case.txt:1:" in result.stdout
    assert "docs/case.txt:2:" in result.stdout


def test_invalid_utf8_and_crlf_content_are_scanned(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "docs/invalid.txt": b"\xff" + TARGET.encode() + b"\xfe\n",
            "docs/crlf.txt": f"line {TARGET}\r\n",
        },
    )

    result = _run(root)

    assert result.returncode == 1
    assert f"docs/invalid.txt:1: �{TARGET}�" in result.stdout
    assert f"docs/crlf.txt:1: line {TARGET}" in result.stdout


def test_allowlist_control_file_is_not_scanned_as_provider_content(tmp_path: Path) -> None:
    all_caps_target = TARGET.upper()
    root = _tree(tmp_path, {"docs/token.txt": f"value = {all_caps_target}A\n"})
    (root / "tools/public-tree-allowlist.txt").write_text(
        f"docs/token.txt:1:value = {all_caps_target}A\n",
        encoding="utf-8",
    )

    result = _run(root)

    assert result.returncode == 0, result.stdout


def test_allowlist_comments_are_scanned_as_control_data(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/clean.py": "clean\n"})
    allowlist = root / "tools/public-tree-allowlist.txt"
    allowlist.write_text(f"# {TARGET} note\n", encoding="utf-8")

    result = _run(root)

    assert result.returncode == 1
    assert f"tools/public-tree-allowlist.txt:1: # {TARGET} note" in result.stdout


def test_allowlist_entries_exempt_only_their_text_field(tmp_path: Path) -> None:
    root = _tree(tmp_path, {f"docs/{TARGET}.txt": "clean\n"})
    allowlist = root / "tools/public-tree-allowlist.txt"
    allowlist.write_text(
        f"docs/{TARGET}.txt:0:docs/{TARGET}.txt\n",
        encoding="utf-8",
    )

    result = _run(root)

    assert result.returncode == 1
    assert f"tools/public-tree-allowlist.txt:1: docs/{TARGET}.txt" in result.stdout


def test_allowlist_with_only_valid_entries_exits_zero(tmp_path: Path) -> None:
    all_caps_target = TARGET.upper()
    root = _tree(tmp_path, {"docs/token.txt": f"value = {all_caps_target}A\n"})
    allowlist = root / "tools/public-tree-allowlist.txt"
    allowlist.write_text(
        f"docs/token.txt:1:value = {all_caps_target}A\n",
        encoding="utf-8",
    )

    result = _run(root)

    assert result.returncode == 0, result.stdout


def test_header_only_allowlist_is_valid(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/clean.py": "clean\n"})

    result = _run(root)

    assert result.returncode == 0


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("docs/provider.txt:1:one\ndocs/provider.txt:1:one\n", "duplicate allowlist entry"),
        ("not-an-entry\n", "invalid allowlist entry"),
    ],
)
def test_duplicate_and_malformed_allowlist_entries_exit_two(
    tmp_path: Path, entry: str, message: str
) -> None:
    root = _tree(tmp_path, {"docs/provider.txt": "one\n"})
    allowlist = root / "allowlist.txt"
    allowlist.write_text(entry, encoding="utf-8")

    result = _run(root, "--allowlist", str(allowlist))

    assert result.returncode == 2
    assert message in result.stderr


def test_json_errors_are_machine_readable(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/clean.py": "clean\n"})

    result = _run(root, "--json", "--skip-dir", "..")

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "error": "--skip-dir must be a relative path below the worktree: .."
    }


def test_non_git_root_is_a_usage_error(tmp_path: Path) -> None:
    root = tmp_path / "not-a-repository"
    root.mkdir()

    result = _run(root, "--json")

    assert result.returncode == 2
    assert "cannot find a git worktree" in json.loads(result.stdout)["error"]


def test_allowlist_suppresses_one_hit_and_rejects_stale_entry(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"docs/providers.txt": f"{TARGET} one\n{TARGET} two\n"},
    )
    allowlist = root / "allowlist.txt"
    allowlist.write_text(f"docs/providers.txt:1:{TARGET} one\n", encoding="utf-8")

    allowed = _run(root, "--allowlist", str(allowlist))

    assert allowed.returncode == 1
    assert "docs/providers.txt:1:" not in allowed.stdout
    assert f"docs/providers.txt:2: {TARGET} two" in allowed.stdout

    allowlist.write_text(f"docs/providers.txt:99:{TARGET} missing\n", encoding="utf-8")
    stale = _run(root, "--allowlist", str(allowlist))

    assert stale.returncode == 2
    assert "stale allowlist entry" in stale.stderr


def test_repeated_skip_directories_exclude_only_their_paths(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "docs/inside.txt": f"{TARGET}\n",
            "generated/inside.txt": f"{TARGET}\n",
            "docs2/visible.txt": f"{TARGET}\n",
        },
    )

    result = _run(root, "--skip-dir", "docs", "--skip-dir", "generated")

    assert result.returncode == 1
    assert result.stdout.splitlines()[0] == f"docs2/visible.txt:1: {TARGET}"
    assert "skipped_dir=2" in result.stdout


def test_tracked_symlink_payload_is_scanned_without_dereferencing(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"links/clean.txt": "clean\n"})
    target = Path("..") / "external" / f"{TARGET}-target.txt"
    link = root / "links" / "provider-link"
    link.symlink_to(target)
    subprocess.run(["git", "add", str(link.relative_to(root))], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "add symlink",
        ],
        cwd=root,
        check=True,
    )

    result = _run(root)

    assert result.returncode == 1
    assert f"links/provider-link:1: {target}" in result.stdout


def test_binary_file_path_is_a_hit_even_when_content_is_skipped(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {f"assets/{TARGET}.bin": b"\0still binary\n"},
    )

    result = _run(root)

    assert result.returncode == 1
    assert f"assets/{TARGET}.bin:0: assets/{TARGET}.bin" in result.stdout
    assert "skipped_binary=1" in result.stdout
    assert "path_hits=1" in result.stdout


def test_binary_content_hits_detect_nul_early_and_utf16le_text(tmp_path: Path) -> None:
    title_target = TARGET.title()
    root = _tree(
        tmp_path,
        {
            "src/nulled.py": f"clean\n\0\nclass {title_target}Thing:\n".encode(),
            "docs/utf16.txt": f"class {title_target}Thing\n".encode("utf-16-le"),
        },
    )

    result = _run(root, "--json")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [(hit["path"], hit["line"], hit["kind"]) for hit in report["hits"]] == [
        ("docs/utf16.txt", 0, "binary"),
        ("src/nulled.py", 0, "binary"),
    ]
    assert report["summary"]["skipped_binary"] == 2
    assert report["summary"]["binary_hits"] == 2
    assert (
        report["summary"]["content_hits"]
        + report["summary"]["path_hits"]
        + report["summary"]["binary_hits"]
        == report["summary"]["hits"] + report["summary"]["allowlisted"]
    )
    assert report["skipped_binary_paths"] == ["docs/utf16.txt", "src/nulled.py"]


def test_guard_has_no_hits_in_its_own_branch_files() -> None:
    self_check_files = {
        "tools/assert_public_tree.py",
        "tools/public-tree-allowlist.txt",
        "tests/test_assert_public_tree.py",
        "justfile",
    }
    # CHANGELOG.md and docs/TESTING-HARNESSES.md carry pre-existing provider
    # lines in this combined tree; the public repository's full check covers them.
    result = _run(PROJECT, "--json")
    report = json.loads(result.stdout)
    own_hits = [hit for hit in report["hits"] if hit["path"] in self_check_files]

    assert own_hits == []


def test_json_report_has_machine_readable_hit_and_summary_shape(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/provider.py": f"{TARGET}\n"})

    result = _run(root, "--json")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["hits"] == [
        {
            "kind": "content",
            "line": 1,
            "path": "src/provider.py",
            "text": TARGET,
        }
    ]
    assert report["summary"]["hits"] == 1
    assert report["summary"]["scanned"] == 2
