"""Receipt reason rules owned by the rex provider package."""

from ...receipt_snapshot import ReasonRule

REASONS = (
    ReasonRule(
        "recipient_binding_changed",
        ("Observed Rex terminal binding changed",),
        {"providers/rex/service.py": (2,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_configuration_changed",
        ("Rex configuration changed",),
        {"providers/rex/service.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_absent",
        ("Rex terminal confirmed absent",),
        {"providers/rex/service.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_binding_changed",
        ("Rex endpoint mismatch",),
        {"providers/rex/client.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_binding_changed",
        ("Rex expected host mismatch",),
        {"providers/rex/client.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_binding_changed",
        ("Rex server host changed",),
        {"providers/rex/client.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_absent",
        ("Rex session confirmed absent",),
        {"providers/rex/client.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_absent",
        ("Rex terminal confirmed absent from its session",),
        {"providers/rex/client.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_binding_changed",
        ("Rex block identity mismatch",),
        {"providers/rex/client.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_binding_changed",
        ("Rex block is not a terminal",),
        {"providers/rex/client.py": (1,)},
        name_at=1,
    ),
)
REASON_ORDER = tuple(range(32, 42))
