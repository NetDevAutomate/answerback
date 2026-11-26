"""Receipt reason rules owned by the Herdr providers."""

from ...receipt_snapshot import ReasonRule

HC_REASONS = (
    ReasonRule(
        "bridge_stopped_during_delivery",
        ("Bridge stopped with core delivery pending",),
        {"providers/herdr/core_tasks.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "delivery_interrupted",
        ("Core delivery interrupted; inspect before retrying",),
        {"providers/herdr/core_tasks.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "delivery_unverified",
        ("Core delivery unverified (", "); inspect before retrying"),
        {"providers/herdr/core_tasks.py": (1, 1)},
        name_at=1,
    ),
    ReasonRule(
        "delivery_not_attempted",
        (
            "Core delivery not attempted: the terminal could not be read before the send; "
            "nothing was sent",
        ),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "delivery_not_attempted",
        ("Core delivery not attempted: the machine list could not be read; nothing was sent",),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "machine_listed_twice",
        (
            "Core delivery not attempted: the machine list names the machine more than once; "
            "nothing was sent",
        ),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "machine_id_malformed",
        (
            "Core delivery not attempted: the machine list gives the machine an id that is "
            "not 32 lowercase hex digits; nothing was sent",
        ),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "machine_not_connected",
        ("Core delivery not attempted: the machine is not connected; nothing was sent",),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "machine_id_changed",
        (
            "Core delivery not attempted: the machine's core id is not the one pinned at its "
            "first send (machine_id_changed); nothing was sent",
        ),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_not_ready",
        ("Core terminal needs a recognized foreground command, known directory, and reported ",),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
        begins=True,
    ),
    ReasonRule(
        "remote_read_only",
        ("Remote Core terminals support reading only",),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
        begins=True,
    ),
    ReasonRule(
        "recipient_identity_missing",
        ("Core send requires the selected terminal identity",),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_changed_before_send",
        ("Core foreground agent changed before send",),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_changed_before_send",
        ("Core terminal identity changed before send",),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "delivery_identity_mismatch",
        (
            "Core send receipt identifies a different terminal; delivery is uncertain. "
            "Do not resubmit blindly.",
        ),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_changed_after_send",
        (
            "Core foreground agent changed after send; delivery is uncertain. "
            "Do not resubmit blindly.",
        ),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_changed_after_send",
        (
            "Core terminal identity changed after send; delivery is uncertain. "
            "Do not resubmit blindly.",
        ),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "machine_id_changed",
        (
            "Machine's core id was pinned to another id while this delivery was sent "
            "(machine_id_changed); delivery is uncertain. Do not resubmit blindly.",
        ),
        {"providers/herdr/core_tasks.py": (1,)},
        name_at=1,
    ),
    ReasonRule(
        "recipient_binding_changed",
        ("Core returned a different terminal address",),
        {"providers/herdr/core.py": (1,)},
        name_at=1,
    ),
)
HC_REASON_ORDER = (1, 2, *range(7, 14), 15, 16, *range(25, 32), 44)

# The Herdr TUI's interrupted-submission sentence is shared with Codex Desktop. The
# kernel owns that one combined rule so its source evidence and historical position remain one.
HERDR_REASONS = ()
HERDR_REASON_ORDER = ()
