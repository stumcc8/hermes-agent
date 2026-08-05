from types import SimpleNamespace
from unittest.mock import patch

import tui_gateway.server as server


SESSION_ID = "desktop-session-123"


def _session(*, running=False, profile_home=None):
    session = {
        "session_key": "runtime-key",
        "agent": SimpleNamespace(session_id=SESSION_ID),
        "history_lock": __import__("threading").Lock(),
        "running": running,
    }
    if profile_home is not None:
        session["profile_home"] = str(profile_home)
    return session


def test_idle_exact_session_emits_completion_then_acknowledges():
    row = {"id": 7, "session_id": SESSION_ID, "content": "cron result"}
    emits = []
    with (
        patch(
            "cron.desktop_notifications.claim_desktop_notifications",
            return_value=[row],
        ) as claim_mock,
        patch(
            "cron.desktop_notifications.acknowledge_desktop_notification",
            return_value=True,
        ) as ack_mock,
        patch(
            "cron.desktop_notifications.release_desktop_notification",
        ) as release_mock,
        patch(
            "gateway.mirror.append_desktop_cron_result",
            return_value=True,
        ) as append_mock,
        patch.object(
            server,
            "_emit",
            side_effect=lambda event, sid, payload=None: emits.append(
                (event, sid, payload)
            ),
        ),
    ):
        delivered = server._dispatch_desktop_cron_notifications(
            "ui-runtime-1",
            _session(),
        )

    assert delivered == 1
    claim_mock.assert_called_once()
    assert claim_mock.call_args.args[0] == SESSION_ID
    claim_token = claim_mock.call_args.args[1]
    assert claim_token.endswith(":ui-runtime-1")
    assert emits == [
        (
            "message.complete",
            "ui-runtime-1",
            {"text": "cron result", "usage": {}, "status": "completed"},
        )
    ]
    append_mock.assert_called_once_with(
        SESSION_ID,
        "cron result",
        notification_id=7,
    )
    ack_mock.assert_called_once_with(7, claim_token)
    release_mock.assert_not_called()


def test_busy_session_leaves_notification_unclaimed():
    with patch(
        "cron.desktop_notifications.claim_desktop_notifications",
    ) as claim_mock:
        delivered = server._dispatch_desktop_cron_notifications(
            "ui-runtime-1",
            _session(running=True),
        )

    assert delivered == 0
    claim_mock.assert_not_called()


def test_emit_failure_releases_notification_for_retry():
    row = {"id": 8, "session_id": SESSION_ID, "content": "retry result"}
    with (
        patch(
            "cron.desktop_notifications.claim_desktop_notifications",
            return_value=[row],
        ) as claim_mock,
        patch(
            "cron.desktop_notifications.acknowledge_desktop_notification",
        ) as ack_mock,
        patch(
            "cron.desktop_notifications.release_desktop_notification",
            return_value=True,
        ) as release_mock,
        patch(
            "gateway.mirror.append_desktop_cron_result",
            return_value=True,
        ),
        patch.object(server, "_emit", side_effect=RuntimeError("socket closed")),
    ):
        delivered = server._dispatch_desktop_cron_notifications(
            "ui-runtime-1",
            _session(),
        )

    claim_token = claim_mock.call_args.args[1]
    assert delivered == 0
    ack_mock.assert_not_called()
    release_mock.assert_called_once_with(8, claim_token)


def test_profile_session_claims_from_profile_home(tmp_path):
    profile_home = tmp_path / "profiles" / "reviewer"
    profile_home.mkdir(parents=True)
    observed_homes = []

    def _claim(*args, **kwargs):
        from hermes_cli.config import get_hermes_home

        observed_homes.append(get_hermes_home())
        return []

    with patch(
        "cron.desktop_notifications.claim_desktop_notifications",
        side_effect=_claim,
    ):
        server._dispatch_desktop_cron_notifications(
            "ui-runtime-1",
            _session(profile_home=profile_home),
        )

    assert observed_homes == [profile_home]
