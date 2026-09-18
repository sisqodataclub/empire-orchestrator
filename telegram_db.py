# telegram_db.py
import os
import sqlite3
from datetime import datetime
from typing import List, Optional, Dict, Any

DB_PATH = os.getenv("TELEGRAM_DB_PATH", os.path.join(os.getcwd(), "telegram_bots.db"))

def get_connection():
    """Return a new SQLite connection with row factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create the telegram_bots table if it doesn't exist."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id TEXT NOT NULL UNIQUE,
                bot_token TEXT NOT NULL,
                allowed_user_ids TEXT NOT NULL,  -- comma-separated Telegram user IDs
                api_key TEXT NOT NULL,           -- org's API key for main API auth
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()

def add_bot_config(organization_id: str, bot_token: str, allowed_user_ids: str, api_key: str) -> int:
    """Insert a new bot config. Returns the new row id."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO telegram_bots
                (organization_id, bot_token, allowed_user_ids, api_key, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (organization_id, bot_token, allowed_user_ids, api_key, now, now)
        )
        conn.commit()
        return cur.lastrowid

def update_bot_config(organization_id: str, bot_token: Optional[str] = None,
                      allowed_user_ids: Optional[str] = None,
                      api_key: Optional[str] = None,
                      status: Optional[str] = None) -> bool:
    """Update fields for an existing org. Returns True if a row was updated."""
    fields = []
    values = []
    if bot_token is not None:
        fields.append("bot_token = ?")
        values.append(bot_token)
    if allowed_user_ids is not None:
        fields.append("allowed_user_ids = ?")
        values.append(allowed_user_ids)
    if api_key is not None:
        fields.append("api_key = ?")
        values.append(api_key)
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if not fields:
        return False
    fields.append("updated_at = ?")
    values.append(datetime.now().isoformat())
    values.append(organization_id)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE telegram_bots SET {', '.join(fields)} WHERE organization_id = ?",
            values
        )
        conn.commit()
        return cur.rowcount > 0

def get_bot_config(organization_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single bot config by org id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM telegram_bots WHERE organization_id = ?", (organization_id,)
        ).fetchone()
    return dict(row) if row else None

def get_all_active_bots() -> List[Dict[str, Any]]:
    """Return all bot configs with status = 'active'."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM telegram_bots WHERE status = 'active'"
        ).fetchall()
    return [dict(row) for row in rows]

def delete_bot_config(organization_id: str) -> bool:
    """Delete a bot config."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM telegram_bots WHERE organization_id = ?", (organization_id,)
        )
        conn.commit()
        return cur.rowcount > 0

# Initialize DB when module is imported
init_db()
