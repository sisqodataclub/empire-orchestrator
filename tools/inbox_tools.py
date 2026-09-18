# tools/inbox_tools.py
import json
from crewai.tools import tool
from orchestration.inbox_db import InboxDB

# Global reference set by ActiveTask
_current_inbox_db = None

def set_inbox_db(db: InboxDB):
    global _current_inbox_db
    _current_inbox_db = db

def _get_db():
    if _current_inbox_db is None:
        raise RuntimeError("Inbox database not initialised for this mission.")
    return _current_inbox_db

@tool("Read Inbox")
def read_inbox(thread_id: str, limit: int = 20) -> str:
    """
    Get recent messages from an inbox thread. Returns both user and CEO messages.
    Args:
        thread_id: The conversation thread ID.
        limit: Maximum number of messages to return (default 20).
    """
    db = _get_db()
    history = db.get_thread_history(thread_id, limit)
    if not history:
        return "No messages in this thread."
    return json.dumps(history, indent=2)

@tool("Get New Inbox Messages")
def get_new_inbox_messages(thread_id: str, since_id: int = 0) -> str:
    """
    Get new incoming messages from the user that have not been processed yet.
    Args:
        thread_id: The conversation thread ID.
        since_id: Return only messages with ID greater than this value.
    """
    db = _get_db()
    messages = db.get_new_in_messages(thread_id, since_id)
    if not messages:
        return "No new messages."
    return json.dumps(messages, indent=2)

@tool("Send User Message")
def send_user_message(thread_id: str, body: str, attachments: str = "[]") -> str:
    """
    Send a message to the user in the same thread.
    Args:
        thread_id: The conversation thread ID.
        body: The message text.
        attachments: JSON array of file paths to attach (optional).
    """
    db = _get_db()
    try:
        att_list = json.loads(attachments)
    except:
        att_list = []
    msg_id = db.add_message(
        thread_id=thread_id,
        direction="OUT",
        body=body,
        sender="CEO",
        recipient="user",
        attachments=att_list,
        status="PENDING_DELIVERY"
    )
    return f"Message sent to user (id={msg_id})."

@tool("Ask User")
def ask_user(thread_id: str, question: str) -> str:
    """
    Ask the user a question and wait for their reply.
    Sends an OUT message and signals that the CEO should pause.
    """
    db = _get_db()
    msg_id = db.add_message(
        thread_id=thread_id,
        direction="OUT",
        body=question,
        sender="CEO",
        recipient="user",
        status="PENDING_DELIVERY"
    )
    return f"Question sent to user (id={msg_id}). Wait for a reply before continuing."
