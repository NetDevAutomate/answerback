"""ACP child environment hardening, using dummy settings and local fixture programs."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from composition import contributed

from answerback_bridge.acp_client import ACPClient, KiroACP
from answerback_bridge.process import spawn_owned, stop_child

# The installed plugin's ACP client runs the same cases (tests/composition.py): its own
# passed-through names, scripted agent and settings file come from its fixture module.
PLUGIN_CASES = contributed("ACP_CHILD_ENVIRONMENT", {})
PLUGIN_ENVIRONMENT_NAMES: frozenset[str] = contributed("ACP_ENVIRONMENT_NAMES", frozenset())
PROVIDERS = ["kiro", *PLUGIN_CASES]

BASE_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "TERM",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)
UNRELATED_NAMES = frozenset(
    {
        "DUMMY_VERIFY_SECRET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CA_BUNDLE",
        "USER",
        "LOGNAME",
        "SSH_AUTH_SOCK",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "LITELLM_API_KEY",
        "FIXTUREPROV_OTHER_SECRET",
        "KIRO_OTHER_SECRET",
        "Q_OTHER_SECRET",
        "FIXTUREPROV_DUMMY_SETTING",
        "KIRO_DUMMY_SETTING",
        "Q_DUMMY_SETTING",
        "FIXTUREPROV_HOME",
        "FIXTUREPROV_CONFIG_DIR",
        "FIXTUREPROV_PROFILE",
        "KIRO_HOME",
        "LC_PRIVATE_SECRET",
        "XDG_PRIVATE_SECRET",
    }
)


@pytest.fixture(params=PROVIDERS)
def provider(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def dummy_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> frozenset[str]:
    # Even the regression at BASE must never hand real credentials to a child.
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    directories = {name for name in BASE_NAMES if name.startswith("XDG_")} | {
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
    }
    for name in BASE_NAMES:
        value = "C"
        if name in directories:
            directory = tmp_path / name.lower()
            directory.mkdir()
            value = str(directory)
        elif name == "PATH":
            value = os.pathsep.join([str(tmp_path), str(Path(sys.executable).parent), os.defpath])
        elif name == "TERM":
            value = "dumb"
        elif name == "SHELL":
            value = "/bin/sh"
        elif name.lower() in {"https_proxy", "http_proxy", "all_proxy"}:
            value = "http://proxy.invalid:9"
        monkeypatch.setenv(name, value)
    # Another provider's own names are set for every client: one client's passed-through
    # name is every other client's unrelated one.
    for name in UNRELATED_NAMES | PLUGIN_ENVIRONMENT_NAMES:
        monkeypatch.setenv(name, "dummy")
    return BASE_NAMES | UNRELATED_NAMES | PLUGIN_ENVIRONMENT_NAMES


async def ignore(event: dict[str, Any]) -> None:
    pass


def make_client(provider: str, binary: str, root: Path) -> ACPClient:
    if provider in PLUGIN_CASES:
        return PLUGIN_CASES[provider].client(binary, root, ignore, ignore)
    return KiroACP(binary, str(root), ignore, ignore)


def expected_names(provider: str) -> frozenset[str]:
    if provider in PLUGIN_CASES:
        return BASE_NAMES | PLUGIN_CASES[provider].passed_names
    return BASE_NAMES


async def test_spawn_receives_only_allowed_names(
    provider: str, dummy_names: frozenset[str], tmp_path: Path
) -> None:
    """red_at_base: the real spawn sink must not inherit unrelated dummy settings."""
    # Review round 1: all proxy, certificate and shell names must reach each child;
    # identity, SSH-agent, AWS selectors and unlisted provider names stay excluded.
    client = make_client(provider, "unused-fixture", tmp_path)
    environment = client.environment()
    probe = (
        "import json, os; "
        f"print(json.dumps([name for name in {sorted(dummy_names)!r} if name in os.environ]))"
    )
    process = await spawn_owned(
        sys.executable,
        "-c",
        probe,
        env=environment,
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(5):
            assert process.stdout is not None
            received = set(json.loads(await process.stdout.read()))
            await process.wait()
    finally:
        await stop_child(process)
    assert process.returncode == 0
    assert received == expected_names(provider)
    assert environment is not None
    assert set(environment) == expected_names(provider)


@pytest.mark.parametrize("binary_form", ["absolute", "path"])
async def test_acp_startup_applies_environment_policy(
    provider: str, dummy_names: frozenset[str], tmp_path: Path, binary_form: str
) -> None:
    """red_at_base: both real adapters apply the policy during normal ACP startup."""
    snapshot = tmp_path / "child-names.json"
    source = (
        PLUGIN_CASES[provider].agent_source
        if provider in PLUGIN_CASES
        else Path(__file__).parent / "fixtures" / "acp_agent.py"
    )
    binary = tmp_path / "fixture-agent"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "from pathlib import Path\n"
        f"Path({str(snapshot)!r}).write_text(json.dumps("
        f"[name for name in {sorted(dummy_names)!r} if name in os.environ]))\n" + source.read_text()
    )
    binary.chmod(0o700)
    settings = (
        PLUGIN_CASES[provider].settings_file if provider in PLUGIN_CASES else "agent-settings.json"
    )
    (tmp_path / settings).write_text("{}")
    selected = str(binary) if binary_form == "absolute" else binary.name
    async with make_client(provider, selected, tmp_path) as client:
        await client.open_session()
    assert set(json.loads(snapshot.read_text())) == expected_names(provider)


def test_missing_and_empty_environment_settings(
    provider: str, dummy_names: frozenset[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: an empty environment is explicit, and present empty names survive."""
    client = make_client(provider, "unused-fixture", tmp_path)
    for name in dummy_names:
        monkeypatch.delenv(name)
    environment = client.environment()
    assert environment is not None
    assert set(environment) == set()
    for name in expected_names(provider):
        monkeypatch.setenv(name, "")
    environment = client.environment()
    assert environment is not None
    assert set(environment) == expected_names(provider)


def test_environment_is_fresh_and_does_not_modify_parent(
    provider: str, dummy_names: frozenset[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_at_base: each launch gets current settings without sharing the parent mapping."""
    client = make_client(provider, "unused-fixture", tmp_path)
    environment = client.environment()
    assert environment is not None
    environment.clear()
    monkeypatch.delenv("LANG")
    current = client.environment()
    assert current is not None
    assert set(current) == expected_names(provider) - {"LANG"}
    assert {name for name in dummy_names if name in os.environ} == dummy_names - {"LANG"}
