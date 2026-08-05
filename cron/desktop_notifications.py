"""Durable wake queue for cron results delivered to Desktop sessions.

Cron execution can happen while the originating Desktop runtime is disconnected.
The result itself lives in the session database; this queue carries only the
short-lived renderer wake needed to surface that assistant message when the
exact session runtime is available again.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from hermes_cli.config import get_hermes_home


_SCHEMA = """
CREATE TABLE IF NOT EXISTS desktop_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    claim_token TEXT,
    claimed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_desktop_notifications_pending
    ON desktop_notifications(session_id, claim_token, claimed_at, id);
"""


def _db_path() -> Path:
    return get_hermes_home() / "cron" / "desktop_notifications.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA secure_delete = ON")
    conn.executescript(_SCHEMA)
    return conn


def enqueue_desktop_notification(session_id: str, content: str) -> int:
    """Persist one wake for an exact Desktop session and return its row ID."""
    durable_id = str(session_id or "").strip()
    text = str(content or "").strip()
    if not durable_id or not text:
        raise ValueError("desktop notification requires session_id and content")
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO desktop_notifications(session_id, content, created_at)
            VALUES (?, ?, ?)
            """,
            (durable_id, text, time.time()),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("desktop notification insert returned no row ID")
        return int(cursor.lastrowid)


def claim_desktop_notifications(
    session_id: str,
    claim_token: str,
    *,
    limit: int = 20,
    lease_seconds: float = 60.0,
) -> list[dict[str, Any]]:
    """Atomically lease pending wakes for one exact Desktop session."""
    durable_id = str(session_id or "").strip()
    token = str(claim_token or "").strip()
    if not durable_id or not token:
        return []
    now = time.time()
    cutoff = now - max(float(lease_seconds), 0.0)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id, session_id, content, created_at
              FROM desktop_notifications
             WHERE session_id = ?
               AND (claim_token IS NULL OR claimed_at < ?)
             ORDER BY id
             LIMIT ?
            """,
            (durable_id, cutoff, max(int(limit), 1)),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE desktop_notifications
                   SET claim_token = ?, claimed_at = ?
                 WHERE id IN ({placeholders})
                """,
                (token, now, *ids),
            )
        conn.commit()
        return [dict(row) for row in rows]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def acknowledge_desktop_notification(notification_id: int, claim_token: str) -> bool:
    """Delete a wake only when acknowledged by its current lease owner."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM desktop_notifications WHERE id = ? AND claim_token = ?",
            (int(notification_id), str(claim_token)),
        )
        return cursor.rowcount == 1


def release_desktop_notification(notification_id: int, claim_token: str) -> bool:
    """Release a failed emission immediately instead of waiting for lease expiry."""
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE desktop_notifications
               SET claim_token = NULL, claimed_at = NULL
             WHERE id = ? AND claim_token = ?
            """,
            (int(notification_id), str(claim_token)),
        )
        return cursor.rowcount == 1
