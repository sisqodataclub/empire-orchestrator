# inbox.py
"""Thin helpers over InboxDB. Everything in the system talks through here."""
import os
from typing import List, Optional

from orchestration.inbox_db import InboxDB

_inbox: Optional[InboxDB] = None


def init(db_path: str) -> None:
    global _inbox
    _inbox = InboxDB(db_path)


def _db() -> InboxDB:
    if _inbox is None:
        raise RuntimeError("inbox.init(db_path) was not called")
    return _inbox


def send(thread: str, sender: str, body: str,
         attachments: Optional[List[str]] = None,
         parent_id: Optional[int] = None) -> int:
    """Write one row into `thread`, attributed to `sender`."""
    return _db().add_message(
        thread_id=thread,
        direction="IN",
        body=body,
        sender=sender,
        recipient=thread,
        status="NEW",
        attachments=attachments,
        parent_message_id=parent_id,
    )


def recent(thread: str, limit: int = 20) -> List[dict]:
    try:
        return _db().get_thread_history(thread, limit=limit) or []
    except Exception:
        return []


def since(thread: str, after_id: int) -> List[dict]:
    try:
        return _db().get_messages_since(thread, after_id) or []
    except AttributeError:
        rows = recent(thread, limit=500)
        return [m for m in rows if int(m.get("id", 0)) > after_id]
    except Exception:
        return []


def max_id(thread: str) -> int:
    rows = recent(thread, limit=1)
    return int(rows[-1]["id"]) if rows else 0


def get(message_id: int) -> Optional[dict]:
    try:
        return _db().get_message(message_id)
    except Exception:
        return None


def format_history(thread: str, agent_name: str, limit: int = 12) -> str:
    rows = recent(thread, limit=limit)
    if not rows:
        return "(nothing before this)"
    lines = []
    for m in rows:
        sender = m.get("sender", "?")
        body = (m.get("body") or "").strip().replace("\n", " ")[:180]
        prefix = "→" if sender == agent_name else "←"
        lines.append(f"{prefix} [{sender}] {body}")
    return "\n".join(lines)


def reply_target(agent_name: str, msg: dict) -> str:
    """
    Where should `agent_name` send the reply to `msg`?

    Default: back to whoever sent the current message.

    Special case: the CEO receiving a worker report. The report's parent
    chain leads back to the original user_* message, so the CEO's reply
    goes to that user — not to the worker.
    """
    sender = msg.get("sender", "") or ""
    if agent_name == "ceo" and sender.startswith("worker_"):
        origin = _walk_to_user(msg.get("parent_message_id"))
        if origin:
            return origin
    return sender


def _walk_to_user(msg_id: Optional[int]) -> Optional[str]:
    seen = set()
    cur = msg_id
    while cur and cur not in seen:
        seen.add(cur)
        m = get(cur)
        if not m:
            return None
        sender = m.get("sender", "") or ""
        if sender.startswith("user_"):
            return sender
        cur = m.get("parent_message_id")
    return None


def list_user_threads() -> List[str]:
    try:
        rows = _db().conn.execute(
            "SELECT DISTINCT thread_id FROM inbox WHERE thread_id LIKE 'user_%'"
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
