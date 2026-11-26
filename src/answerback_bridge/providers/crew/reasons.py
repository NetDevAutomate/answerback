"""Receipt reason rules owned by the crew provider package."""

from ...receipt_snapshot import ReasonRule

REASONS = (
    ReasonRule(
        "delivery_interrupted",
        ("Crew submission was interrupted",),
        {"providers/crew/adapter.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_binding_changed",
        ("Crew chat identity changed or its identity evidence is incomplete",),
        {"providers/crew/client.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "delivery_identity_mismatch",
        ("Crew delivery receipt named a different chat",),
        {"providers/crew/client.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "delivery_unverified",
        ("Crew delivery is uncertain: ",),
        {"providers/crew/client.py": (1,)},
        name_at=1,
        begins=True,
    ),
)
REASON_ORDER = (4, 42, 43, 60)
