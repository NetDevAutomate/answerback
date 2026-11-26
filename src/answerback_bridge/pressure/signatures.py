"""The versioned table of provider rate-limit signatures, and the per-line matcher.

Text goes in and only `Match` records come out: the provider, the account hint, which
harness and surface it was seen on, the signature key and a sha256 of the matched line.
The line itself never leaves this module.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

LINE_CHARS = 400  # Each line is matched on this many leading characters, never more.
SOURCES = frozenset({"hc_screen", "native_evidence", "native_error", "acp_text"})
HARNESSES = frozenset({"kiro", "claude", "codex", "grok", "crew", "unknown"})


@dataclass(frozen=True, slots=True)
class Match:
    provider: str  # "kiro" | "bedrock": the account that is rate-limited
    account_hint: str
    harness: str  # "kiro" | "claude" | "codex" | "grok" | "crew" | "unknown"
    source: str  # "hc_screen" | "native_evidence" | "native_error" | "acp_text"
    signature: str  # signature table key, e.g. "kiro.rate_limit"
    line_digest: str  # sha256 hex of the matched line; never the line itself
    model: str | None = None


@dataclass(frozen=True, slots=True)
class Signature:
    """One fault class.

    `provider` names the account the fault belongs to; None means the account of the
    harness it was seen on (`provider_of`), with "bedrock" when that harness is unknown.
    `needs_context` is a second pattern the same line must also match. `harnesses`, when
    set, limits the signature to those harnesses: a generic phrase is evidence only where
    that harness prints it.
    """

    key: str
    provider: str | None
    pattern: re.Pattern[str]
    sources: frozenset[str]
    needs_context: re.Pattern[str] | None = None
    harnesses: frozenset[str] | None = None


def _pattern(text: str) -> re.Pattern[str]:
    return re.compile(text, re.IGNORECASE)


# Patterns are literal alternations with at most one optional group: no nested
# quantifiers, so a 400-character line is matched in linear time.
SIGNATURES_V1: tuple[Signature, ...] = (
    Signature(
        "kiro.rate_limit",
        "kiro",
        _pattern(r"faster than your rate limit|run fewer sessions at the same time"),
        SOURCES,
        harnesses=frozenset({"kiro", "crew", "unknown"}),
    ),
    Signature(
        "mantle.stream_error",
        "bedrock",
        _pattern(r"stream error \(invalid_request_error\): Mantle"),
        SOURCES,
        harnesses=frozenset({"claude", "codex", "grok", "unknown"}),
    ),
    Signature(
        "bedrock.throttling",
        None,
        _pattern(r"ThrottlingException|TooManyRequestsException"),
        SOURCES,
    ),
    Signature(
        "bedrock.http_429",
        None,
        _pattern(r"\b429\b"),
        SOURCES,
        needs_context=_pattern(r"status|HTTP|error|too many"),
    ),
    Signature(
        "codex.rate_limit",
        None,
        _pattern(r"rate_limit|rate limit(?:ed)?"),
        SOURCES,
        harnesses=frozenset({"codex"}),
    ),
)
SIGNATURE_VERSION = 1
BY_KEY = {signature.key: signature for signature in SIGNATURES_V1}

_PROVIDER_OF = {
    "kiro": "kiro",
    "crew": "kiro",
    "claude": "bedrock",
    "claude_code": "bedrock",
    "codex": "bedrock",
    "codex_cli": "bedrock",
    "grok": "bedrock",
}
_HARNESS_OF = {
    "kiro": "kiro",
    "kiro-cli": "kiro",
    "kirocrew": "crew",
    "crew": "crew",
    "claude": "claude",
    "claude_code": "claude",
    "codex": "codex",
    "codex_cli": "codex",
    "grok": "grok",
}


def provider_of(harness: str) -> str:
    """The account a harness spends: Kiro for Kiro and Crew, Bedrock for the others."""
    return _PROVIDER_OF.get(harness, "unknown")


def harness_of_peer(peer: dict[str, Any]) -> str:
    """The harness of a discovered peer: a terminal's detected agent, else its provider."""
    provider = str(peer.get("provider") or "")
    if provider in ("hc", "herdr", "desktop"):
        return _HARNESS_OF.get(str(peer.get("agent_kind") or ""), "unknown")
    return _HARNESS_OF.get(provider, "unknown")


def line_digest(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()


def account_of(signature: Signature, harness: str) -> str:
    if signature.provider is not None:
        return signature.provider
    provider = provider_of(harness)
    return "bedrock" if provider == "unknown" else provider


def classify(line: str, harness: str, source: str) -> Signature | None:
    """The first signature this one line matches, on its bounded slice."""
    head = line[:LINE_CHARS]
    for signature in SIGNATURES_V1:
        if source not in signature.sources:
            continue
        if signature.harnesses is not None and harness not in signature.harnesses:
            continue
        if signature.pattern.search(head) and (
            signature.needs_context is None or signature.needs_context.search(head)
        ):
            return signature
    return None


def match_lines(
    text: str,
    *,
    exclude: str | None = None,
    harness: str,
    source: str,
    account_hint: str | Mapping[str, str],
    only: frozenset[str] | None = None,
    model: str | None = None,
) -> list[Match]:
    """One `Match` per matching line, in order. A line that appears verbatim (stripped)
    in `exclude` (the assignment brief) never matches: a brief that quotes a signature
    must not pause dispatch. `only` limits the signature keys considered. `account_hint`
    is one hint, or the configured hint of each provider (`PressureSettings.hints`)."""
    if not isinstance(text, str) or not text:
        return []
    if harness not in HARNESSES:
        harness = "unknown"
    excluded = (
        {line.strip() for line in exclude.splitlines() if line.strip()}
        if isinstance(exclude, str)
        else set()
    )
    found: list[Match] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in excluded:
            continue
        signature = classify(line, harness, source)
        if signature is None or (only is not None and signature.key not in only):
            continue
        provider = account_of(signature, harness)
        hint = (
            account_hint if isinstance(account_hint, str) else account_hint.get(provider, "default")
        )
        found.append(
            Match(
                provider,
                hint,
                harness,
                source,
                signature.key,
                line_digest(line),
                model,
            )
        )
    return found
