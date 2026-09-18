# orchestration/inbox_db.py
import os
import sqlite3
import json
from datetime import datetime
from typing import Optional, List, Dict, Any


class InboxDB:
    """
    Threaded inbox storage for bidirectional communication between
    the user and the CEO. Uses SQLite and supports persistent threads.
    """

    def __init__(self, db_path: str):
        """
        :param db_path: Path to the SQLite database file.
        """
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('IN','OUT')),
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                attachments TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'NEW',
                created_at TEXT NOT NULL,
                parent_message_id INTEGER,
                FOREIGN KEY(parent_message_id) REFERENCES inbox(id)
            );

            CREATE INDEX IF NOT EXISTS idx_inbox_thread ON inbox(thread_id);
            CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);

            CREATE TABLE IF NOT EXISTS sender_threads (
                sender_key TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Thread management
    # ------------------------------------------------------------------
    def create_thread(self, thread_id: str, title: str = "") -> None:
        """Register a new thread (optional – threads are just string IDs)."""
        # No separate threads table; we only need sender_threads mapping.
        pass

    def get_thread_id_for_sender(self, sender_key: str) -> Optional[str]:
        """Return the active thread_id for a given sender, if any."""
        row = self.conn.execute(
            "SELECT thread_id FROM sender_threads WHERE sender_key = ?",
            (sender_key,)
        ).fetchone()
        return row["thread_id"] if row else None

    def assign_thread_to_sender(self, sender_key: str, thread_id: str) -> None:
        """Map a sender to a thread_id (used for persistent conversations)."""
        now = datetime.now().isoformat()
        self.conn.execute(
            """
            INSERT INTO sender_threads (sender_key, thread_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(sender_key) DO UPDATE SET
                thread_id = excluded.thread_id,
                updated_at = excluded.updated_at
            """,
            (sender_key, thread_id, now)
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------
    def add_message(
        self,
        thread_id: str,
        direction: str,
        body: str,
        sender: str = "user",
        recipient: str = "CEO",
        attachments: Optional[List[str]] = None,
        parent_message_id: Optional[int] = None,
        status: str = "NEW"
    ) -> int:
        """
        Insert a new message into the inbox.
        Returns the new message ID.
        """
        if direction not in ("IN", "OUT"):
            raise ValueError("direction must be 'IN' or 'OUT'")
        attachments_json = json.dumps(attachments or [])
        created_at = datetime.now().isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO inbox
                (thread_id, direction, sender, recipient, body,
                 attachments, status, created_at, parent_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (thread_id, direction, sender, recipient, body,
             attachments_json, status, created_at, parent_message_id)
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM inbox WHERE id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_thread_history(self, thread_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM inbox
            WHERE thread_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (thread_id, limit)
        ).fetchall()
        # Return in chronological order
        return [dict(row) for row in reversed(rows)]

    def get_new_in_messages(self, thread_id: str, since_id: int = 0) -> List[Dict[str, Any]]:
        """Return IN messages newer than since_id (unprocessed)."""
        rows = self.conn.execute(
            """
            SELECT * FROM inbox
            WHERE thread_id = ? AND direction = 'IN' AND id > ?
            ORDER BY id ASC
            """,
            (thread_id, since_id)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_out_messages_since(self, thread_id: str, since_id: int = 0) -> List[Dict[str, Any]]:
        """Return OUT messages newer than since_id (for delivery to user)."""
        rows = self.conn.execute(
            """
            SELECT * FROM inbox
            WHERE thread_id = ? AND direction = 'OUT' AND id > ?
            ORDER BY id ASC
            """,
            (thread_id, since_id)
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_out_delivered(self, message_id: int) -> None:
        self.conn.execute(
            "UPDATE inbox SET status = 'DELIVERED' WHERE id = ? AND direction = 'OUT'",
            (message_id,)
        )
        self.conn.commit()

    def mark_in_processed(self, message_id: int) -> None:
        self.conn.execute(
            "UPDATE inbox SET status = 'PROCESSED' WHERE id = ? AND direction = 'IN'",
            (message_id,)
        )
        self.conn.commit()

    def mark_replied(self, message_id: int, reply_body: str) -> None:
        """Mark an IN message as replied (optional, can use status)."""
        self.conn.execute(
            "UPDATE inbox SET status = 'REPLIED' WHERE id = ? AND direction = 'IN'",
            (message_id,)
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
