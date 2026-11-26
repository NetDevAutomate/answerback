from pathlib import Path
from stat import S_IMODE

import pytest


def test_notifications_survive_restart_and_polling_does_not_acknowledge(tmp_path: Path) -> None:
    from codex_herdr_bridge.client_mailbox import ClientMailbox

    path = tmp_path / "mailbox.sqlite"
    box = ClientMailbox(path)
    sent = box.notify("kiro", "Bridge is ready", "notice-one", "bridge.status")
    assert sent["status"] == "queued"
    duplicate = box.notify("kiro", "Bridge is ready", "notice-one", "bridge.status")
    assert duplicate["id"] == sent["id"]
    assert box.pending("codex")["messages"] == []
    assert box.pending("kiro") == ClientMailbox(path).pending("kiro")
    assert len(box.pending("kiro")["messages"]) == 1
    box.acknowledge("kiro", [sent["id"]])
    assert ClientMailbox(path).pending("kiro")["messages"] == []


def test_notification_idempotency_key_cannot_change_message(tmp_path: Path) -> None:
    from codex_herdr_bridge.client_mailbox import ClientMailbox

    box = ClientMailbox(tmp_path / "mailbox.sqlite")
    box.notify("kiro", "first", "same-request", "bridge.status")
    with pytest.raises(ValueError, match="different"):
        box.notify("kiro", "second", "same-request", "bridge.status")


def test_client_cannot_acknowledge_another_clients_notification(tmp_path: Path) -> None:
    from codex_herdr_bridge.client_mailbox import ClientMailbox

    box = ClientMailbox(tmp_path / "mailbox.sqlite")
    notice = box.notify("kiro", "message", "request", "bridge.status")
    box.acknowledge("codex", [notice["id"]])
    assert len(box.pending("kiro")["messages"]) == 1


@pytest.mark.parametrize("target_exists", [False, True])
def test_mailbox_refuses_database_symlinks(tmp_path: Path, target_exists: bool) -> None:
    """red_at_base: reject both existing and dangling database symlinks."""
    from codex_herdr_bridge.client_mailbox import ClientMailbox

    directory = tmp_path / "state"
    directory.mkdir()
    target = tmp_path / "target.sqlite"
    if target_exists:
        target.touch()
        target.chmod(0o640)
    path = directory / "mailbox.sqlite"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="Client mailbox database must not be a symlink"):
        ClientMailbox(path)

    assert path.is_symlink()
    assert target.exists() is target_exists
    if target_exists:
        assert target.read_bytes() == b""
        assert S_IMODE(target.stat().st_mode) == 0o640


def test_mailbox_resecures_existing_directory_on_restart(tmp_path: Path) -> None:
    """red_at_base: tighten an owned loose directory without losing its notices."""
    from codex_herdr_bridge.client_mailbox import ClientMailbox

    path = tmp_path / "state" / "mailbox.sqlite"
    original = ClientMailbox(path)
    sent = original.notify("codex", "Ready", "ready")
    path.parent.chmod(0o777)

    reopened = ClientMailbox(path)

    assert S_IMODE(path.parent.stat().st_mode) == 0o700
    assert S_IMODE(path.stat().st_mode) == 0o600
    assert reopened.pending("codex")["messages"][0]["id"] == sent["id"]


def test_notification_capacity_is_per_client(tmp_path: Path) -> None:
    """red_at_base: one full inbox cannot consume another client's allowance."""
    from codex_herdr_bridge.client_mailbox import ClientMailbox

    box = ClientMailbox(tmp_path / "mailbox.sqlite")
    for index in range(150):
        box.notify("claude", "Ready", f"notice-{index}")
    assert box.notify("codex", "Ready", "first")["status"] == "queued"

    with pytest.raises(ValueError, match="Notification inbox capacity reached for this client"):
        box.notify("claude", "Ready", "overflow")

    assert box.status("claude", "overflow")["found"] is False
    assert len(box.pending("codex")["messages"]) == 1


def test_notification_retry_and_acknowledgement_at_capacity(tmp_path: Path) -> None:
    """pins_existing: retries keep identity and acknowledgements free a slot."""
    from codex_herdr_bridge.client_mailbox import ClientMailbox

    box = ClientMailbox(tmp_path / "mailbox.sqlite")
    first = box.notify("claude", "Ready", "first")
    for index in range(149):
        box.notify("claude", "Ready", f"notice-{index}")
    box = ClientMailbox(box.path)
    assert box.notify("claude", "Ready", "first") == first
    with pytest.raises(ValueError, match="different notification"):
        box.notify("claude", "Changed", "first")
    box.acknowledge("claude", [first["id"]])
    assert box.notify("claude", "Ready", "replacement")["status"] == "queued"
    assert box.notify("claude", "Ready", "first") == {
        **first,
        "status": "acknowledged",
    }


def test_notification_global_capacity_remains_a_backstop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pins_existing: the total limit still applies across independent inboxes."""
    from codex_herdr_bridge import client_mailbox
    from codex_herdr_bridge.client_mailbox import ClientMailbox
    from codex_herdr_bridge.config import CLIENT_NAMES

    # Filling the total of 1000 needs at least seven inboxes under the per-client cap of
    # 150; the kernel's six clients plus one a plugin would add.
    names = (*CLIENT_NAMES, "plugin-client")
    monkeypatch.setattr(client_mailbox, "CLIENT_NAMES", names)
    box = ClientMailbox(tmp_path / "mailbox.sqlite")
    first = box.notify(names[0], "Ready", "notice-0")
    for index in range(1, 1000):
        box.notify(names[index % len(names)], "Ready", f"notice-{index}")
    with pytest.raises(ValueError, match="^Notification inbox capacity reached$"):
        box.notify("kiro", "Ready", "overflow")
    assert box.notify(names[0], "Ready", "notice-0") == first
    box.acknowledge(names[0], [first["id"]])
    assert box.notify("kiro", "Ready", "after-ack")["status"] == "queued"
