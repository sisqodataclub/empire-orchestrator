# orchestration/inbox.py
"""
Thin helpers over InboxDB. Everything in the system talks through here.

Loop guard integration
─────────────────────
`send()` routes every inter-agent message through LoopGuard before it
hits the DB. This is the single choke point: delegations, worker
reports, terminal acks, SEND_REPLY, ASK_CEO, ASK_USER, grace replies,
and error messages all funnel through `inbox.send`, so a hook here
catches every leg of any loop.

The guard applies only to inter-agent senders — `"ceo"` and
`"worker_*"`. User messages (`user_*`) and system messages
(`__loop_guard__` and any other internal sender) pass through
untouched.

When the guard blocks a message, `send()` returns a BlockedSend
object instead of a row id. Callers that need the id must check the
type. Callers that ignore the return (SEND_REPLY, ASK_CEO, etc.) are
unaffected — their message simply doesn't reach the DB.

Fail-open: if the guard can't be imported for any reason, delivery
proceeds normally. A broken guard must not silence the system.
"""
import logging
import os
from typing import List, Optional, Union

from orchestration.inbox_db import InboxDB

logger = logging.getLogger(__name__)

_inbox: Optional[InboxDB] = None


def init(db_path: str) -> None:
    global _inbox
    _inbox = InboxDB(db_path)


def _db() -> InboxDB:
    if _inbox is None:
        raise RuntimeError("inbox.init(db_path) was not called")
    return _inbox


# ══════════════════════════════════════════════════════════════════════
# Loop guard integration
# ══════════════════════════════════════════════════════════════════════
class BlockedSend:
    """
    Returned by send() when the loop guard stops a message.

    Carries the Block descriptor from loop_guard so callers can render
    a useful tool result or log entry without re-querying the guard.
    """
    __slots__ = ("block",)

    def __init__(self, block):
        self.block = block

    def __repr__(self):
        return f"<BlockedSend reason={self.block.reason}>"


def _should_guard(sender: str) -> bool:
    """
    Only inter-agent messages pass through the guard. Users and
    system senders are exempt.
    """
    if not sender:
        return False
    return sender == "ceo" or sender.startswith("worker_")


def _get_guard_or_none():
    """
    Lazy import + fail-open. Returns the guard on success, None if
    the guard module can't be imported. Never raises.
    """
    try:
        from orchestration.loop_guard import get_guard
        return get_guard()
    except Exception:
        logger.exception(
            "loop_guard: import failed — guard disabled for this send"
        )
        return None


def _handle_block(sender: str, thread: str, block, guard) -> None:
    """
    Log a guard block, and (for non-terminal blocks) notify the user
    at most once per cooldown.

    Terminal acks are silent — nothing is wrong, the thread is
    simply finished, and the user doesn't need to hear about it.
    """
    # Local import to avoid the module-load circular dependency with
    # agent_loop (which imports messenger, which imports inbox).
    try:
        from orchestration.agent_loop import log_tool_event
        log_tool_event(
            sender,
            "loop_guard_block",
            f"to={thread} reason={block.reason}",
        )
    except Exception:
        logger.exception("loop_guard: failed to write tool log")

    # Terminal acks: nothing wrong, no user notification.
    if block.reason == "terminal_ack":
        return

    # Near/exact repeats: notify the user once per cooldown.
    if not guard.should_notify_user():
        return

    org = os.environ.get("ORG_ID")
    if not org:
        # Console mode — no user thread to notify.
        return

    user_thread = f"user_tg_{org}"
    try:
        # Direct DB insert, bypassing send() — this is a system
        # notification, not something the guard should re-check.
        _db().add_message(
            thread_id=user_thread,
            direction="IN",
            body=(
                "Heads-up: I paused an agent-to-agent exchange that "
                "was repeating the same message. No action needed "
                "unless you want me to look into it."
            ),
            sender="__loop_guard__",
            recipient=user_thread,
            status="NEW",
            attachments=None,
            parent_message_id=None,
        )
        logger.info(f"loop_guard: user notified on {user_thread}")
    except Exception:
        logger.exception("loop_guard: user notification failed")


def send(
    thread: str,
    sender: str,
    body: str,
    attachments: Optional[List[str]] = None,
    parent_id: Optional[int] = None,
) -> Union[int, BlockedSend]:
    """
    Write one row into `thread`, attributed to `sender`.

    If the loop guard blocks the message, returns a BlockedSend
    instead of a row id. Callers that need the id must check the
    return type.
    """
    if _should_guard(sender):
        guard = _get_guard_or_none()
        if guard is not None:
            block = guard.observe(sender, thread, body)
            if block is not None:
                logger.warning(
                    f"[loop-guard] blocked {sender} -> {thread}: "
                    f"{block.reason}"
                )
                _handle_block(sender, thread, block, guard)
                return BlockedSend(block)

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


# ══════════════════════════════════════════════════════════════════════
# Read helpers (unchanged)
# ══════════════════════════════════════════════════════════════════════
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

    Special case: the CEO receiving a worker report. The report's
    parent chain leads back to the original user_* message, so the
    CEO's reply goes to that user — not to the worker.
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
