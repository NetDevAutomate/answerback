"""Receipt reason rules owned by the native provider package."""

from ...receipt_snapshot import ReasonRule

REASONS = (
    ReasonRule(
        "delivery_interrupted",
        ("Native worker interrupted; no automatic replay",),
        {"providers/native/workers.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "codex_cli_deadline_exceeded",
        ("codex_cli task exceeded the ",),
        {"providers/native/workers.py": (1,)},
        name_at=1,
        begins=True,
    ),
    ReasonRule(
        "claude_code_deadline_exceeded",
        ("claude_code task exceeded the ",),
        {"providers/native/workers.py": (1,)},
        name_at=1,
        begins=True,
    ),
    ReasonRule(
        "grok_deadline_exceeded",
        ("grok task exceeded the ",),
        {"providers/native/workers.py": (1,)},
        name_at=1,
        begins=True,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Unknown native workspace; register a named project first"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Saved native session ownership is invalid"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Native session ownership or configuration profile changed"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Native progress has an unexpected session"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "recipient_configuration_changed",
        (": ", "Native routing changed before execution"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_result_unavailable",
        (": ", "Native turn completed but its final response is unavailable"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Native response exceeds its supported bound"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Native session identity is invalid"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Native resume changed the owned session identity"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Native terminal event has the wrong session"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_execution_unverified",
        (": ", "Native terminal event has an invalid status"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "cancel_confirmed",
        (" confirmed ", "cancelled"),
        {"providers/native/workers.py": (1, 1)},
        name_at=0,
    ),
    ReasonRule(
        "native_failure_confirmed",
        (" confirmed ", "failed"),
        {"providers/native/workers.py": (1, 2)},
        name_at=0,
    ),
)
REASON_ORDER = (5, 22, 23, 24, *range(45, 56), 58, 59)
