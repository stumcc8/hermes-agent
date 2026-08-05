"""Tests for gateway/mirror.py — session mirroring."""

import json
from unittest.mock import patch, MagicMock

import gateway.mirror as mirror_mod
from gateway.mirror import (
    append_desktop_cron_result,
    desktop_session_exists,
    mirror_to_session,
    _find_session_id,
)


def _setup_sessions(tmp_path, sessions_data):
    """Helper to write a fake sessions.json and patch module-level paths."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_file = sessions_dir / "sessions.json"
    index_file.write_text(json.dumps(sessions_data), encoding="utf-8")
    return sessions_dir, index_file


class TestFindSessionId:
    def test_finds_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "agent:main:telegram:dm": {
                "session_id": "sess_abc",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            }
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result == "sess_abc"

    def test_returns_most_recent(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "old": {
                "session_id": "sess_old",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "new": {
                "session_id": "sess_new",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result == "sess_new"

    def test_thread_id_disambiguates_same_chat(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "topic_a": {
                "session_id": "sess_topic_a",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "10"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "topic_b": {
                "session_id": "sess_topic_b",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "11"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "-1001", thread_id="10")

        assert result == "sess_topic_a"


class TestMirrorToSession:


    def test_successful_mirror_uses_user_id_for_group_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "alice": {
                "session_id": "sess_alice",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "alice"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "bob": {
                "session_id": "sess_bob",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "bob"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("gateway.mirror._append_to_sqlite") as mock_sqlite:
            result = mirror_to_session(
                "telegram",
                "-1001",
                "Hello group!",
                source_label="cli",
                user_id="alice",
            )

        assert result is True
        mock_sqlite.assert_called_once()
        assert mock_sqlite.call_args[0][0] == "sess_alice"

    def test_no_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {})

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = mirror_to_session("telegram", "99999", "Hello!")

        assert result is False


class TestDesktopCronSession:
    def test_real_session_db_round_trip_is_alternating_and_idempotent(self, tmp_path):
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_state import SessionDB

        token = set_hermes_home_override(str(tmp_path))
        try:
            db = SessionDB()
            db.create_session("desktop-session-real", "desktop")
            db.append_message(
                session_id="desktop-session-real",
                role="assistant",
                content="previous response",
            )
            db.close()

            assert append_desktop_cron_result(
                "desktop-session-real",
                "Synthetic desktop completion.",
                notification_id=77,
                source_label="cron:desktop-proof",
            )

            db = SessionDB()
            messages = db.get_messages("desktop-session-real")
            assert [message["role"] for message in messages] == [
                "assistant",
                "user",
                "assistant",
            ]
            assert messages[-2]["display_kind"] == "hidden"
            assert messages[-1]["display_kind"] == "cron_delivery"
            assert messages[-1]["content"] == "Synthetic desktop completion."
            count = db.message_count("desktop-session-real")
            db.close()

            assert append_desktop_cron_result(
                "desktop-session-real",
                "Synthetic desktop completion.",
                notification_id=77,
                source_label="cron:desktop-proof",
            )
            db = SessionDB()
            assert db.message_count("desktop-session-real") == count
            db.close()
        finally:
            reset_hermes_home_override(token)

    def test_existing_session_is_detected_and_connection_closed(self):
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"session_id": "desktop-session-123"}

        with patch("hermes_state.SessionDB", return_value=mock_db):
            result = desktop_session_exists("desktop-session-123")

        assert result is True
        mock_db.get_session.assert_called_once_with("desktop-session-123")
        mock_db.append_message.assert_not_called()
        mock_db.close.assert_called_once()

    def test_last_assistant_gets_hidden_bridge_before_cron_result(self):
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"session_id": "desktop-session-123"}
        mock_db.message_count.return_value = 1
        mock_db.get_messages.return_value = [
            {"role": "assistant", "content": "previous response"}
        ]

        with patch("hermes_state.SessionDB", return_value=mock_db):
            result = append_desktop_cron_result(
                "desktop-session-123",
                "Synthetic desktop completion.",
                notification_id=7,
                source_label="cron:desktop-cron",
            )

        assert result is True
        assert mock_db.append_message.call_count == 2
        mock_db.append_message.assert_any_call(
            session_id="desktop-session-123",
            role="user",
            content="[Scheduled job completed: cron:desktop-cron]",
            display_kind="hidden",
            display_metadata={"cron_notification_id": 7},
        )
        mock_db.append_message.assert_any_call(
            session_id="desktop-session-123",
            role="assistant",
            content="Synthetic desktop completion.",
            display_kind="cron_delivery",
            display_metadata={
                "cron_notification_id": 7,
                "source": "cron:desktop-cron",
            },
        )
        mock_db.close.assert_called_once()

    def test_last_user_gets_assistant_result_without_bridge(self):
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"session_id": "desktop-session-123"}
        mock_db.message_count.return_value = 1
        mock_db.get_messages.return_value = [{"role": "user", "content": "hello"}]

        with patch("hermes_state.SessionDB", return_value=mock_db):
            result = append_desktop_cron_result(
                "desktop-session-123",
                "Synthetic desktop completion.",
                notification_id=8,
                source_label="cron:desktop-cron",
            )

        assert result is True
        mock_db.append_message.assert_called_once_with(
            session_id="desktop-session-123",
            role="assistant",
            content="Synthetic desktop completion.",
            display_kind="cron_delivery",
            display_metadata={
                "cron_notification_id": 8,
                "source": "cron:desktop-cron",
            },
        )

    def test_retry_with_existing_notification_id_is_idempotent(self):
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"session_id": "desktop-session-123"}
        mock_db.message_count.return_value = 2
        mock_db.get_messages.return_value = [
            {"role": "user", "display_kind": "hidden"},
            {
                "role": "assistant",
                "display_metadata": {"cron_notification_id": 9},
            },
        ]

        with patch("hermes_state.SessionDB", return_value=mock_db):
            result = append_desktop_cron_result(
                "desktop-session-123",
                "Synthetic desktop completion.",
                notification_id=9,
            )

        assert result is True
        mock_db.append_message.assert_not_called()

    def test_missing_session_is_not_created(self):
        mock_db = MagicMock()
        mock_db.get_session.return_value = None

        with patch("hermes_state.SessionDB", return_value=mock_db):
            result = append_desktop_cron_result(
                "missing-session",
                "Synthetic desktop completion.",
                notification_id=10,
            )

        assert result is False
        mock_db.append_message.assert_not_called()
        mock_db.close.assert_called_once()


class TestAppendToSqlite:
    def test_connection_is_closed_after_use(self, tmp_path):
        """Verify _append_to_sqlite closes the SessionDB connection."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()

        with patch("hermes_state.SessionDB", return_value=mock_db):
            _append_to_sqlite("sess_1", {"role": "assistant", "content": "hello"})

        mock_db.append_message.assert_called_once()
        mock_db.close.assert_called_once()

