"""T-227 signature table: each fault class matches its own line and nothing else."""

import re
import time

import pytest

from answerback_bridge.pressure import (
    SIGNATURES_V1,
    Signature,
    harness_of_peer,
    match_lines,
    provider_of,
)
from answerback_bridge.pressure.signatures import LINE_CHARS, line_digest

KIRO_LIMITER = (
    "You're sending requests faster than your rate limit allows. Please wait a moment "
    "and try again, or run fewer sessions at the same time."
)
FIXTURES = [
    # (harness, line, signature key, provider)
    ("kiro", KIRO_LIMITER, "kiro.rate_limit", "kiro"),
    ("kiro", "Tip: run fewer sessions at the same time", "kiro.rate_limit", "kiro"),
    (
        "claude",
        "API Error: stream error (invalid_request_error): Mantle request was rejected",
        "mantle.stream_error",
        "bedrock",
    ),
    (
        "claude",
        "An error occurred (ThrottlingException) when calling the InvokeModel operation",
        "bedrock.throttling",
        "bedrock",
    ),
    ("grok", "TooManyRequestsException: Too many tokens", "bedrock.throttling", "bedrock"),
    ("claude", "HTTP 429 Too Many Requests", "bedrock.http_429", "bedrock"),
    ("codex", "error: rate_limit_exceeded for gpt-5", "codex.rate_limit", "bedrock"),
    ("codex", "You have been rate limited; retrying in 20s", "codex.rate_limit", "bedrock"),
]
NEVER = [
    "2114 passed, 4 skipped in 309 s",
    "429 passed in 12.1 s",
    "return 429;",
    "Per-line matching on a 400-character slice, `re.IGNORECASE`, no nested quantifiers",
]


def keys(text: str, harness: str, **options: object) -> list[str]:
    return [
        match.signature
        for match in match_lines(
            text, harness=harness, source="hc_screen", account_hint="default", **options
        )
    ]


@pytest.mark.parametrize(("harness", "line", "key", "provider"), FIXTURES)
def test_every_signature_matches_its_fixture_line(
    harness: str, line: str, key: str, provider: str
) -> None:
    (match,) = match_lines(line, harness=harness, source="hc_screen", account_hint="work")
    assert (match.signature, match.provider, match.account_hint) == (key, provider, "work")
    assert match.harness == harness and match.source == "hc_screen"
    assert match.line_digest == line_digest(line) and line not in repr(match)


def test_every_signature_in_the_table_has_a_fixture() -> None:
    assert {signature.key for signature in SIGNATURES_V1} == {key for _, _, key, _ in FIXTURES}


@pytest.mark.parametrize("line", NEVER)
@pytest.mark.parametrize("harness", ["kiro", "claude", "codex", "grok", "crew", "unknown"])
def test_test_summaries_code_and_prose_never_match(line: str, harness: str) -> None:
    assert keys(line, harness) == []


def test_exclude_drops_the_brief_line_and_only_that_line() -> None:
    brief = f"Assignment\n  {KIRO_LIMITER}  \nmore"
    screen = f"{KIRO_LIMITER}\nYou're sending requests faster than your rate limit allows!"
    assert keys(KIRO_LIMITER, "kiro") == ["kiro.rate_limit"]
    assert keys(KIRO_LIMITER, "kiro", exclude=brief) == []
    assert keys(screen, "kiro", exclude=brief) == ["kiro.rate_limit"]


def test_generic_phrases_count_only_on_their_own_harness() -> None:
    assert keys("the rate limit is 10 per second", "kiro") == []
    assert keys("the rate limit is 10 per second", "claude") == []
    assert keys(KIRO_LIMITER, "claude") == []
    assert keys("stream error (invalid_request_error): Mantle", "kiro") == []
    # A Bedrock fault on a Kiro screen is the Kiro account's.
    (match,) = match_lines(
        "ThrottlingException", harness="kiro", source="hc_screen", account_hint={"kiro": "a"}
    )
    assert (match.provider, match.account_hint) == ("kiro", "a")


def test_only_the_leading_slice_of_a_line_is_matched() -> None:
    assert keys("x" * LINE_CHARS + " ThrottlingException", "claude") == []
    assert keys("x" * (LINE_CHARS - 18) + "ThrottlingException", "claude") == []
    assert keys("x" * (LINE_CHARS - 19) + "ThrottlingException", "claude") == ["bedrock.throttling"]


def test_provider_and_harness_tables() -> None:
    assert {h: provider_of(h) for h in ("kiro", "crew", "claude", "claude_code")} == {
        "kiro": "kiro",
        "crew": "kiro",
        "claude": "bedrock",
        "claude_code": "bedrock",
    }
    assert {h: provider_of(h) for h in ("codex", "codex_cli", "grok", "fixtureprov", "gemini")} == {
        "codex": "bedrock",
        "codex_cli": "bedrock",
        "grok": "bedrock",
        "fixtureprov": "unknown",
        "gemini": "unknown",
    }
    peers = {
        "kiro": {"provider": "hc", "agent_kind": "kiro"},
        "crew": {"provider": "hc", "agent_kind": "kirocrew"},
        "claude": {"provider": "hc", "agent_kind": "claude"},
        "codex": {"provider": "hc", "agent_kind": "codex"},
        "grok": {"provider": "hc", "agent_kind": "grok"},
        "unknown": {"provider": "hc", "agent_kind": "gemini"},
    }
    assert {name: harness_of_peer(peer) for name, peer in peers.items()} == {
        name: name for name in peers
    }
    assert [
        harness_of_peer({"provider": provider})
        for provider in ("kiro", "crew", "claude_code", "codex_cli", "codex", "grok", "rex")
    ] == ["kiro", "crew", "claude", "codex", "codex", "grok", "unknown"]
    assert harness_of_peer({"provider": "desktop", "agent_kind": "codex"}) == "codex"


ADVERSARIAL = [
    "4" * LINE_CHARS,
    "429 " * (LINE_CHARS // 4),
    "rate " * (LINE_CHARS // 5),
    "stream error (" * (LINE_CHARS // 14),
    "faster than your " * (LINE_CHARS // 17),
    "Throttling" * (LINE_CHARS // 10),
    "a" * LINE_CHARS,
]


@pytest.mark.parametrize("signature", SIGNATURES_V1, ids=lambda s: s.key)
def test_each_pattern_is_fast_on_an_adversarial_line(signature: Signature) -> None:
    patterns = [signature.pattern]
    if signature.needs_context is not None:
        patterns.append(signature.needs_context)
    for pattern in patterns:
        for line in ADVERSARIAL:
            assert len(line) <= LINE_CHARS
            best = min(_timed(pattern, line) for _ in range(5))
            assert best < 0.001, (signature, line[:20], best)


def _timed(pattern: re.Pattern[str], line: str) -> float:
    start = time.perf_counter()
    pattern.search(line)
    return time.perf_counter() - start
