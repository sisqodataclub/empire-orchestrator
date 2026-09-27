# orchestration/messenger.py
"""
Messenger tools.

`send_message` is the delegation tool — writes a row into another
agent's inbox and ensures that agent's dispatcher is running.
Workers don't have it. `read_agent_log` reads a worker's tool log
for verification.

`send_message` tolerates extra keyword arguments the LLM invents
(`message_type`, `priority`, etc.) so a hallucinated field does not
crash the delegation. Ignored args are logged at INFO.

Loop guard
──────────
The guard runs inside `inbox.send`, which means every inter-agent
message passes through it — not just delegations, but also the
worker's report, the CEO's reply, and so on. When the guard blocks,
`inbox.send` returns a BlockedSend object. This module detects that
and renders a clear tool result for the LLM, explaining what
happened and what to do instead.

The rendered result is short and prescriptive: the LLM reads it as
the tool's return value and knows not to rephrase and retry.
"""
import logging
import os
import threading
from datetime import datetime
from typing import List, Optional

from orchestration import agents, inbox
from orchestration.inbox import BlockedSend


logger = logging.getLogger(__name__)


# ── Thread-local context (set by run_agent_turn before each turn) ────
_ctx = threading.local()


def set_context(agent_name: str, current_msg_id: Optional[int]) -> None:
    _ctx.agent_name = agent_name
    _ctx.current_msg_id = current_msg_id


def _get_context():
    return (
        getattr(_ctx, "agent_name", None),
        getattr(_ctx, "current_msg_id", None),
    )


# ── Tool wrapper ─────────────────────────────────────────────────────
class Tool:
    def __init__(self, name: str, description: str, func):
        self.name = name
        self.description = description
        self.func = func

    def run(self, **kwargs):
        return self.func(**kwargs)


# ── send_message ─────────────────────────────────────────────────────
def _resolve_target(to: str) -> str:
    if to == "ceo" or to.startswith(("worker_", "user_")):
        return to
    return agents.worker_thread_name(to)


def _render_blocked_result(target: str, block) -> str:
    """
    Turn a BlockedSend into a prescriptive tool result.
    The LLM reads this and knows not to rephrase and retry.
    """
    if block.reason == "terminal_ack":
        return (
            f"send_message to {target} skipped — the recipient is "
            f"signalling the thread is done. Do not send further "
            f"messages here. If you have something new for the USER, "
            f"SEND_REPLY. Otherwise FINISH."
        )

    matched = (block.matched_body or "").replace("\n", " ")[:200]
    sim = (
        f" (similarity {block.similarity:.2f})"
        if block.similarity is not None and block.similarity < 1.0
        else ""
    )
    return (
        f"send_message to {target} BLOCKED by loop guard: "
        f"{block.reason}{sim}.\n"
        f"You already sent a near-identical message "
        f"{int(block.matched_age_sec)}s ago:\n"
        f"  \"{matched}\"\n\n"
        f"Do NOT rephrase and retry — the guard will block that too.\n"
        f"What to do instead:\n"
        f"  • If the worker already reported back, SEND_REPLY to the "
        f"user with what was done.\n"
        f"  • If you're waiting on the worker, SEND_REPLY saying it's "
        f"in progress, or FINISH.\n"
        f"  • Do NOT call send_message to {target} again this turn."
    )


def _send_message(
    to: str,
    body: str,
    attachments: Optional[List[str]] = None,
    **kwargs,
) -> str:
    """
    Send a message to another agent's inbox.

    `**kwargs` absorbs extra fields the LLM invents. Without it, a
    hallucinated field raises TypeError and three of them trip the
    hard-stop.

    The loop guard runs inside inbox.send(). If the message is
    blocked, the LLM gets a tool result explaining why and what to
    do instead.
    """
    if kwargs:
        logger.info(
            f"send_message ignoring extra args: {sorted(kwargs.keys())}"
        )

    agent_name, msg_id = _get_context()
    if not agent_name:
        return "send_message: no active agent context."
    if not to or not body:
        return "send_message: `to` and `body` are required."

    target = _resolve_target(to)

    result = inbox.send(
        thread=target,
        sender=agent_name,
        body=body,
        attachments=attachments,
        parent_id=msg_id,
    )

    # ── Loop guard blocked the message ───────────────────────────────
    if isinstance(result, BlockedSend):
        logger.warning(
            f"[{agent_name}] send_message blocked by loop guard: "
            f"to={target} reason={result.block.reason}"
        )
        return _render_blocked_result(target, result.block)
    # ── Delivered normally ───────────────────────────────────────────

    row_id = result  # int

    if target.startswith("worker_"):
        agents.ensure_worker(target)

    if target != "ceo":
        agents.ensure_dispatcher(target, start_from=row_id - 1)

    return f"Message sent to {target} (id={row_id})."


# ── read_agent_log ───────────────────────────────────────────────────
def _read_agent_log(agent: str, lines: int = 30) -> str:
    """
    Read the last N lines of an agent's tool-call log.

    Prepends a header with the last-write timestamp and total line
    count. The header does NOT indicate whether the worker is
    currently running.
    """
    name = agent
    if name != "ceo" and not name.startswith("worker_"):
        name = agents.worker_thread_name(name)
    path = os.path.join(agents.logs_dir(name), "tools.log")
    if not os.path.exists(path):
        return f"(no log file for {name})"

    try:
        with open(path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except Exception as e:
        return f"read_agent_log failed: {e}"

    tail = all_lines[-int(lines):]
    body = "".join(tail).rstrip() or "(empty log)"

    try:
        mtime   = os.path.getmtime(path)
        last_ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")
        header  = f"Last write: {last_ts}\nTotal lines: {len(all_lines)}\n\n"
    except Exception:
        header = ""

    return header + body


# ── Exported tool objects ────────────────────────────────────────────
send_message_tool = Tool(
    name="send_message",
    description=(
        "Send a message to another agent's inbox and return immediately. "
        "Use for delegation: send_message(to='React Dev', body='...'). "
        "Only 'to', 'body', and optional 'attachments' are accepted; "
        "other fields are ignored. "
        "A loop guard blocks repeated or reworded messages to the same "
        "recipient; if you get a BLOCKED result, do not rephrase and retry."
    ),
    func=_send_message,
)

read_agent_log_tool = Tool(
    name="read_agent_log",
    description=(
        "Read the last N lines of an agent's tool-call log. "
        "Returns a header with the log's last-write timestamp and total "
        "line count, followed by the log tail. "
        "The header shows when the log was last written — it does NOT "
        "indicate whether the worker is currently running."
    ),
    func=_read_agent_log,
)

MESSENGER_TOOLS = [send_message_tool, read_agent_log_tool]
