import json
import os
import sys

import pytest

from codex_herdr_bridge.process import BridgeError, codex_env, herdr_env, run


async def test_prompt_metacharacters_are_literal_arguments() -> None:
    prompt = "Review `not-a-command` $(also-not-a-command); and 'quotes'\nsecond line"
    output = await run(
        [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1]))", prompt]
    )
    assert json.loads(output.stdout) == prompt


async def test_command_timeout_is_reported_as_uncertain_delivery() -> None:
    with pytest.raises(BridgeError, match="delivery may be uncertain"):
        await run([sys.executable, "-c", "import time; time.sleep(10)"], max_seconds=0.1)


async def test_child_failure_is_not_a_success_receipt() -> None:
    with pytest.raises(BridgeError, match="exit 7"):
        await run([sys.executable, "-c", "raise SystemExit(7)"])


def test_inherited_pane_and_socket_do_not_override_explicit_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/wrong/server")
    monkeypatch.setenv("HERDR_PANE_ID", "wrong:p1")
    monkeypatch.setenv("HERDR_SESSION", "wrong")
    assert "HERDR_SOCKET_PATH" not in herdr_env()
    assert "HERDR_PANE_ID" not in herdr_env()
    assert "HERDR_SESSION" not in herdr_env()


@pytest.mark.parametrize("home", [None, "/selected-codex-home"])
def test_codex_environment_excludes_unrelated_ambient_names(
    monkeypatch: pytest.MonkeyPatch, home: str | None
) -> None:
    """red_at_base: unrelated credentials and execution settings stay out of the child.

    pins_existing at reviewed tip: base-URL and unlisted proxy/certificate names stay out.
    """
    excluded = {
        "DUMMY_OTHER_PROVIDER_API_KEY",
        "DUMMY_SERVICE_TOKEN",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "HERDR_SESSION",
        "PYTHONPATH",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        "CODEX_UNRECOGNIZED_SETTING",
        "OPENAI_BASE_URL",
        "FTP_PROXY",
        "REQUESTS_CA_BUNDLE",
    }
    monkeypatch.setattr(os, "environ", dict.fromkeys(excluded, "dummy"))
    assert excluded.isdisjoint(codex_env(home))


def test_codex_environment_preserves_launch_and_provider_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pins_existing: an explicit allowlist must retain launch and provider inputs."""
    required = {
        "PATH",
        "HOME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
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
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_CA_CERTIFICATE",
    }
    monkeypatch.setattr(os, "environ", dict.fromkeys(required, "dummy"))
    assert set(codex_env(None)) == required


def test_codex_environment_preserves_proxy_and_certificate_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pins_existing: BASE retains these inputs; their presence is red at the reviewed tip."""
    network_names = {
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
    monkeypatch.setattr(os, "environ", {})
    assert network_names.isdisjoint(codex_env(None))
    monkeypatch.setattr(os, "environ", dict.fromkeys(network_names, "dummy"))
    environment = codex_env(None)
    assert set(environment) == network_names
    assert environment["HTTPS_PROXY"] == "dummy"


@pytest.mark.parametrize("home", [None, "/selected-codex-home", ""])
def test_codex_environment_preserves_home_precedence(
    monkeypatch: pytest.MonkeyPatch, home: str | None
) -> None:
    """pins_existing: an explicit home overrides the ambient home, including empty input."""
    monkeypatch.setattr(os, "environ", {"CODEX_HOME": "/ambient-codex-home"})
    assert codex_env(home)["CODEX_HOME"] == ("/ambient-codex-home" if home is None else home)
    assert os.environ["CODEX_HOME"] == "/ambient-codex-home"


def test_codex_environment_does_not_invent_absent_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """pins_existing: absent names stay absent and no home override is required."""
    monkeypatch.setattr(os, "environ", {})
    assert codex_env(None) == {}
