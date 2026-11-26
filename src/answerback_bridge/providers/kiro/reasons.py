"""Receipt reason rules owned by the kiro provider package."""

from ...receipt_snapshot import ReasonRule

REASONS = (
    ReasonRule(
        "delivery_interrupted",
        ("Bridge interrupted Kiro; no automatic replay",),
        {"providers/kiro/workers.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "cancel_confirmed",
        ("Kiro confirmed cancellation",),
        {"providers/kiro/workers.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "awaiting_permission",
        ("Awaiting a human permission decision",),
        {"worker_runtime.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "cancelled_before_submission",
        ("Cancelled before prompt submission",),
        {"worker_runtime.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "bridge_stopped_before_worker_settled",
        ("Bridge stopped before worker settled",),
        {"worker_runtime.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "kiro_deadline_exceeded",
        ("Kiro task exceeded the ",),
        {"providers/kiro/workers.py": (1,)},
        name_at=1,
        begins=True,
    ),
    ReasonRule(
        "recipient_configuration_changed",
        (": ", "Kiro workspace changed; register a new alias"),
        {"providers/kiro/workers.py": (1, 1)},
        name_at=0,
    ),
)
REASON_ORDER = (6, 17, 18, 19, 20, 21, 56)
