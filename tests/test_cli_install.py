"""Fully mocked coverage for cli.install(), the single most state-mutating command.

Every scenario below uses fake, local ``herdr``/``codex``/``uv`` executables and a
PATH confined to ``tmp_path``, so this module can never resolve or run the real ``uv``
binary; ``uv tool install`` is only ever exercised against our own scripted stand-in,
and that stand-in records every invocation so the assertions can prove it.

Ported from 8dcb517 part C onto the answerback_bridge package (messages and the
wheel name differ from the 0.5.0 line; the refusals themselves are cli.py's).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_herdr_bridge.cli import dispatch, parser
from codex_herdr_bridge.config import Config
from codex_herdr_bridge.process import BridgeError

HERDR_SCRIPT = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "herdr 9.9.9"
    exit 0
fi
if [ "$1" = "agent" ] && [ "$2" = "--help" ]; then
    echo "usage: herdr agent prompt [options]"
    exit 0
fi
exit 1
"""

CODEX_SCRIPT = """#!/bin/sh
if [ "$1" = "queue" ] && [ "$2" = "--help" ]; then
    echo "usage: codex queue --thread THREAD_ID"
    exit 0
fi
exit 1
"""


def uv_script(tool_root: Path, bin_dir: Path, calls: Path) -> str:
    """A uv stand-in: records argv, accepts ``tool install``, answers ``tool dir``."""
    return f"""#!/bin/sh
echo "$*" >> "{calls}"
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
    exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "dir" ] && [ "$3" = "--bin" ]; then
    echo "{bin_dir}"
    exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then
    echo "{tool_root}"
    exit 0
fi
exit 1
"""


def make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture(autouse=True)
def fake_service_start(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record install's service start without touching the owner's service manager."""
    from answerback_bridge import lifecycle

    calls: list[Path] = []

    async def start(config_path: Path) -> dict[str, object]:
        path = await asyncio.to_thread(lambda: config_path.expanduser().resolve())
        assert await asyncio.to_thread(path.is_file), (
            "install must save the bridge config before starting services"
        )
        calls.append(path)
        return {
            "running": True,
            "accepting": True,
            "fleet_writer": {
                "installed": False,
                "running": False,
                "pid": None,
                "settings_path": str(path.parent / "fleet-writer.json"),
                "last_exit": None,
                "failure_reason": None,
                "message": "fleet writer not configured; run `answerback fleet configure`",
            },
        }

    monkeypatch.setattr(lifecycle, "start", start)
    return calls


@pytest.fixture
def fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A PATH containing only scripted herdr/codex/uv stand-ins; no real uv is reachable."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    tool_root = tmp_path / "uv-tools"
    tool_root.mkdir()
    uv_bin_dir = tmp_path / "uv-tool-bin"
    uv_bin_dir.mkdir()
    uv_calls = tmp_path / "uv-calls.log"
    herdr = bin_dir / "herdr"
    codex = bin_dir / "codex"
    uv = bin_dir / "uv"
    make_executable(herdr, HERDR_SCRIPT)
    make_executable(codex, CODEX_SCRIPT)
    make_executable(uv, uv_script(tool_root, uv_bin_dir, uv_calls))
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("uv") == str(uv), "the real uv must not be reachable from this PATH"
    return SimpleNamespace(
        bin_dir=bin_dir,
        herdr=herdr,
        codex=codex,
        uv=uv,
        tool_root=tool_root,
        uv_bin_dir=uv_bin_dir,
        uv_calls=uv_calls,
    )


@pytest.fixture
def layout(tmp_path: Path) -> SimpleNamespace:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    source = tmp_path / "answerback-0.10.0-py3-none-any.whl"
    source.touch()
    return SimpleNamespace(
        config_path=tmp_path / "bridge" / "config.json", codex_home=codex_home, source=source
    )


def install_args(layout: SimpleNamespace, tools: SimpleNamespace) -> list[str]:
    return [
        "--config",
        str(layout.config_path),
        "install",
        "--source",
        str(layout.source),
        "--session",
        "test-session",
        "--herdr",
        str(tools.herdr),
        "--codex",
        str(tools.codex),
        "--codex-home",
        str(layout.codex_home),
    ]


def uv_invocations(tools: SimpleNamespace) -> list[str]:
    return tools.uv_calls.read_text().splitlines() if tools.uv_calls.exists() else []


async def test_install_refuses_existing_bridge_with_another_session(
    fake_tools: SimpleNamespace, layout: SimpleNamespace
) -> None:
    Config("existing-session").save(layout.config_path)
    args = parser().parse_args(install_args(layout, fake_tools))
    with pytest.raises(ValueError, match="another session"):
        await dispatch(args)
    # Nothing in the conflicting bridge config was touched and uv never ran.
    assert Config.load(layout.config_path).herdr_session == "existing-session"
    assert not (layout.codex_home / "config.toml").exists()
    assert uv_invocations(fake_tools) == []


async def test_install_refuses_symlinked_codex_config(
    tmp_path: Path, fake_tools: SimpleNamespace, layout: SimpleNamespace
) -> None:
    real_config = tmp_path / "elsewhere-config.toml"
    real_config.write_text("")
    (layout.codex_home / "config.toml").symlink_to(real_config)
    args = parser().parse_args(install_args(layout, fake_tools))
    with pytest.raises(ValueError, match="symlinked Codex config"):
        await dispatch(args)
    assert real_config.read_text() == ""
    assert (layout.codex_home / "config.toml").is_symlink()
    assert not layout.config_path.exists()
    assert uv_invocations(fake_tools) == []


async def test_install_refuses_unowned_mcp_entry(
    fake_tools: SimpleNamespace, layout: SimpleNamespace
) -> None:
    original = '[mcp_servers.answerback_bridge]\ncommand = "someone-else"\n'
    (layout.codex_home / "config.toml").write_text(original)
    args = parser().parse_args(install_args(layout, fake_tools))
    with pytest.raises(ValueError, match="MCP entry conflict"):
        await dispatch(args)
    assert (layout.codex_home / "config.toml").read_text() == original
    assert not layout.config_path.exists()
    assert uv_invocations(fake_tools) == []


async def test_install_refuses_while_the_herdr_bridge_distribution_is_installed(
    fake_tools: SimpleNamespace, layout: SimpleNamespace
) -> None:
    receipt = fake_tools.tool_root / "herdr-bridge" / "uv-receipt.toml"
    receipt.parent.mkdir()
    receipt.write_text("")
    args = parser().parse_args(install_args(layout, fake_tools))
    with pytest.raises(ValueError, match="herdr-bridge distribution is still installed"):
        await dispatch(args)
    assert not any(call.startswith("tool install") for call in uv_invocations(fake_tools))
    assert not layout.config_path.exists()
    assert not (layout.codex_home / "config.toml").exists()


async def test_install_fails_when_uv_does_not_produce_the_expected_executable(
    fake_tools: SimpleNamespace, layout: SimpleNamespace
) -> None:
    args = parser().parse_args(install_args(layout, fake_tools))
    with pytest.raises(BridgeError, match="uv did not produce the expected answerback executable"):
        await dispatch(args)
    # The fake uv's "tool install" ran (bounded, scripted) but never wrote an executable,
    # and no bridge config or MCP entry was persisted as a result.
    assert f"tool install --force {layout.source}" in uv_invocations(fake_tools)
    assert not any(fake_tools.uv_bin_dir.iterdir())
    assert not layout.config_path.exists()
    assert not (layout.codex_home / "config.toml").exists()


async def test_first_install_explicitly_enables_codex(
    fake_tools: SimpleNamespace, layout: SimpleNamespace, fake_service_start: list[Path]
) -> None:
    """pins_existing: the Codex installer must still opt into its own provider."""
    make_executable(fake_tools.uv_bin_dir / "answerback", "#!/bin/sh\nexit 0\n")
    args = parser().parse_args(install_args(layout, fake_tools))
    result = await dispatch(args)
    assert result["installed"] is True
    assert fake_service_start == [layout.config_path.resolve()]
    assert result["fleet_writer"]["message"] == (
        "fleet writer not configured; run `answerback fleet configure`"
    )
    saved = Config.load(layout.config_path)
    assert saved.codex_enabled is True
    assert saved.provider_enabled("codex") is True
    raw = json.loads(layout.config_path.read_text())
    assert raw["providers"]["codex"]["enabled"] is True
    assert "codex_enabled" not in raw


@pytest.mark.parametrize("enabled", [False, True])
async def test_install_preserves_explicit_legacy_codex_choice(
    fake_tools: SimpleNamespace, layout: SimpleNamespace, enabled: bool
) -> None:
    """pins_existing: upgrading keeps the stored choice, including explicit opt-out."""
    layout.config_path.parent.mkdir()
    layout.config_path.write_text(
        json.dumps({"herdr_session": "test-session", "codex_enabled": enabled})
    )
    make_executable(fake_tools.uv_bin_dir / "answerback", "#!/bin/sh\nexit 0\n")
    args = parser().parse_args(install_args(layout, fake_tools))
    assert (await dispatch(args))["installed"] is True
    saved = Config.load(layout.config_path)
    assert saved.codex_enabled is enabled
    assert saved.provider_enabled("codex") is enabled
    assert saved.provider_selection is None


@pytest.mark.parametrize("paired", [False, True])
async def test_install_migrates_pre_050_desktop_intent(
    fake_tools: SimpleNamespace, layout: SimpleNamespace, paired: bool
) -> None:
    """Paired: pins_existing at BASE, red_at_review_tip; unpaired: red_at_base."""
    threads = {"planner": "11111111-2222-3333-4444-555555555555"} if paired else {}
    layout.config_path.parent.mkdir()
    layout.config_path.write_text(
        json.dumps({"herdr_session": "test-session", "desktop_threads": threads})
    )
    make_executable(fake_tools.uv_bin_dir / "answerback", "#!/bin/sh\nexit 0\n")
    args = parser().parse_args(install_args(layout, fake_tools))
    assert (await dispatch(args))["installed"] is True
    saved = Config.load(layout.config_path)
    assert saved.codex_enabled is paired
    assert saved.provider_enabled("codex") is paired
    assert saved.desktop_threads == threads
    assert json.loads(layout.config_path.read_text())["providers"]["codex"]["enabled"] is paired


async def test_install_stores_new_pairing_without_enabling_unpaired_pre_050_config(
    fake_tools: SimpleNamespace, layout: SimpleNamespace
) -> None:
    """pins_existing at MM2 tip: an upgrade carries the previous disabled choice."""
    thread_id = "11111111-2222-3333-4444-555555555555"
    layout.config_path.parent.mkdir()
    layout.config_path.write_text(json.dumps({"herdr_session": "test-session"}))
    make_executable(fake_tools.uv_bin_dir / "answerback", "#!/bin/sh\nexit 0\n")
    args = parser().parse_args(
        install_args(layout, fake_tools) + ["--desktop", f"planner={thread_id}"]
    )
    assert (await dispatch(args))["installed"] is True
    saved = Config.load(layout.config_path)
    assert saved.desktop_threads == {"planner": thread_id}
    assert saved.codex_enabled is False
    assert saved.provider_enabled("codex") is False
    raw = json.loads(layout.config_path.read_text())["providers"]["codex"]
    assert raw["desktop_threads"] == {"planner": thread_id}
    assert raw["enabled"] is False


async def test_install_puts_the_herdr_app_panel_in_the_apps_folder(
    fake_tools: SimpleNamespace,
    layout: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-181: `answerback install` installs the side panel and the themes, and reports it."""
    app = tmp_path / "herdr-app"
    (app / "mods").mkdir(parents=True)
    monkeypatch.setenv("HERDR_APP_CONFIG", str(app))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    make_executable(fake_tools.uv_bin_dir / "answerback", "#!/bin/sh\nexit 0\n")
    result = await dispatch(parser().parse_args(install_args(layout, fake_tools)))
    assert result["installed"] is True
    report = result["herdr_app"]
    assert report["applied"] is True and report["success"] is True
    assert (app / "mods" / "answerback-fleet" / "main.js").is_file()
    assert (app / "themes" / "rex-macchiato.json").is_file()


async def test_install_without_the_herdr_app_reports_it_and_writes_nothing_there(
    fake_tools: SimpleNamespace, layout: SimpleNamespace, tmp_path: Path
) -> None:
    """The conftest sandbox points HERDR_APP_CONFIG at a folder that is not there."""
    make_executable(fake_tools.uv_bin_dir / "answerback", "#!/bin/sh\nexit 0\n")
    result = await dispatch(parser().parse_args(install_args(layout, fake_tools)))
    assert result["installed"] is True
    assert result["herdr_app"]["present"] is False
    assert "not here" in result["herdr_app"]["note"]


async def test_uninstall_removes_the_panel_before_the_tool_it_runs_from_is_removed(
    fake_tools: SimpleNamespace,
    layout: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 (ABM-T181F): `answerback uninstall --remove-tool` runs the Herdr App step first.

    `uv tool uninstall answerback` removes this package from disk; a module imported after it is
    gone in a real tool process. The module is made as that process meets it: not yet imported
    (conftest's tripwire imports it; the import is undone here) and not importable once the uv
    stand-in has removed the tool.

    red at 350c792: the tool was removed first and the import that followed failed.
    """
    import importlib.abc
    import importlib.machinery
    import sys

    from answerback_bridge.providers import herdr as herdr_provider
    from answerback_bridge.providers.herdr import herdr_app

    app = tmp_path / "herdr-app"
    (app / "mods").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    assert herdr_app.install(app, apply=True, home=home)["success"] is True
    monkeypatch.setenv("HERDR_APP_CONFIG", str(app))
    monkeypatch.setenv("HOME", str(home))
    tool_removed = tmp_path / "tool-removed"
    make_executable(
        fake_tools.uv,
        f"""#!/bin/sh
echo "$*" >> "{fake_tools.uv_calls}"
if [ "$1" = "tool" ] && [ "$2" = "uninstall" ]; then
    : > "{tool_removed}"
    exit 0
fi
exit 1
""",
    )

    class Gone(importlib.abc.MetaPathFinder):
        def find_spec(
            self, fullname: str, path: object, target: object = None
        ) -> importlib.machinery.ModuleSpec | None:
            if fullname == "answerback_bridge.providers.herdr.herdr_app" and tool_removed.exists():
                raise ModuleNotFoundError(f"{fullname} was removed with the tool")
            return None

    monkeypatch.delitem(sys.modules, "answerback_bridge.providers.herdr.herdr_app")
    monkeypatch.delattr(herdr_provider, "herdr_app")
    monkeypatch.setattr(sys, "meta_path", [Gone(), *sys.meta_path])

    args = parser().parse_args(
        [
            "--config",
            str(layout.config_path),
            "uninstall",
            "--codex-home",
            str(layout.codex_home),
            "--remove-tool",
        ]
    )
    result = await dispatch(args)
    assert result["herdr_app"]["success"] is True
    assert result["herdr_app"]["applied"] is True
    assert not (app / "mods" / "answerback-fleet" / "main.js").exists()
    assert tool_removed.exists()
    assert "tool uninstall answerback" in fake_tools.uv_calls.read_text()
