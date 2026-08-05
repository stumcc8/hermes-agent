from unittest.mock import patch

import pytest

from cron.desktop_notifications import (
    acknowledge_desktop_notification,
    claim_desktop_notifications,
    enqueue_desktop_notification,
    release_desktop_notification,
)


@pytest.fixture
def notification_db(tmp_path):
    path = tmp_path / "desktop-notifications.db"
    with patch("cron.desktop_notifications._db_path", return_value=path):
        yield path


def test_claim_is_scoped_to_exact_session_and_acknowledged_once(notification_db):
    first_id = enqueue_desktop_notification("session-a", "first result")
    enqueue_desktop_notification("session-b", "other result")

    claimed = claim_desktop_notifications("session-a", "runtime-a")

    assert claimed == [
        {
            "id": first_id,
            "session_id": "session-a",
            "content": "first result",
            "created_at": claimed[0]["created_at"],
        }
    ]
    assert claim_desktop_notifications("session-a", "runtime-b") == []
    assert acknowledge_desktop_notification(first_id, "runtime-b") is False
    assert acknowledge_desktop_notification(first_id, "runtime-a") is True
    assert claim_desktop_notifications("session-a", "runtime-a") == []
    assert notification_db.stat().st_mode & 0o777 == 0o600


def test_failed_emission_can_release_notification_immediately(notification_db):
    notification_id = enqueue_desktop_notification("session-a", "retry me")
    assert claim_desktop_notifications("session-a", "runtime-a")

    assert release_desktop_notification(notification_id, "runtime-a") is True
    reclaimed = claim_desktop_notifications("session-a", "runtime-b")

    assert [row["id"] for row in reclaimed] == [notification_id]


def test_stale_claim_is_recoverable(notification_db):
    notification_id = enqueue_desktop_notification("session-a", "recover me")
    assert claim_desktop_notifications("session-a", "dead-runtime")

    reclaimed = claim_desktop_notifications(
        "session-a",
        "new-runtime",
        lease_seconds=0,
    )

    assert [row["id"] for row in reclaimed] == [notification_id]


@pytest.mark.parametrize(
    ("session_id", "content"),
    [("", "result"), ("session-a", ""), (" ", "result")],
)
def test_enqueue_rejects_incomplete_notification(notification_db, session_id, content):
    with pytest.raises(ValueError):
        enqueue_desktop_notification(session_id, content)