# orchestration/conversations.py
"""
The conversation graph.

The inbox is a directed graph: every message has a parent_message_id
pointing at the message it responds to. A "conversation" starts at a
message with no parent (typically a user request) and includes every
descendant message across every thread — the CEO's delegations, the
worker's reports, the CEO's replies to the user.

This module walks that graph to give the CEO a single coherent view
of every active conversation, with a derived status:

  OPEN       — last message is from user/worker and unanswered
  STALLED    — OPEN and older than STALL_THRESHOLD_MINUTES
  RESOLVED   — last message is from the CEO, or has been replied to

The CEO prompt shows the OPEN/STALLED conversations first (these need
action) and RESOLVED ones below (context only — do NOT re-act).
"""
from datetime import datetime
from typing import Optional

from orchestration import inbox


# Only conversations with activity in this window are considered.
ACTIVE_WINDOW_MINUTES = 90

# An unanswered conversation older than this is "stalled".
STALL_THRESHOLD_MINUTES = 10

# Max conversations shown; max messages rendered per conversation.
MAX_CONVERSATIONS = 6
MAX_MESSAGES_PER_CONV = 8

# Body truncation.
BODY_CHARS = 200


def _fmt_ts(iso: str) -> str:
    if not iso:
        return "??:??"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return iso[11:16] if len(iso) >= 16 else iso


def _short(text: str, n: int = BODY_CHARS) -> str:
    s = (text or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _role(sender: str, recipient: str) -> str:
    def one(x: str) -> str:
        if x == "ceo":
            return "CEO"
        if x.startswith("user_"):
            return "USER"
        if x.startswith("worker_"):
            return "WORKER:" + x[len("worker_"):]
        return x
    return f"{one(sender)} → {one(recipient)}"


def _age_minutes(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 60
    except Exception:
        return 0.0


def _status_for(group: list[dict], children: dict[int, list[int]]) -> str:
    """
    Determine conversation status from the last message in the group.
    """
    last = group[-1]
    last_id = last["id"]
    last_sender = last.get("sender", "")

    # If anything points at the last message, it's been responded to.
    if children.get(last_id):
        return "resolved"

    # Last message from the CEO means the CEO already acted on it.
    if last_sender == "ceo":
        return "resolved"

    # Last message from user or worker, unanswered.
    age = _age_minutes(last.get("created_at", ""))
    return "stalled" if age > STALL_THRESHOLD_MINUTES else "open"


def build_conversations(
    limit_msgs: int = 300,
    viewer: str = "ceo",
) -> list[dict]:
    """
    Return conversations, most recent first. Each is a dict with:
        root_id, root_ts, root_sender, root_body,
        messages (list of dicts),
        last_ts, last_sender, status ('open'|'stalled'|'resolved'),
        needs_action (bool)
    """
    db = inbox._db()
    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM inbox ORDER BY id ASC"
    ).fetchall()][-limit_msgs:]
    if not rows:
        return []

    by_id = {r["id"]: r for r in rows}
    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for r in rows:
        p = r.get("parent_message_id")
        if p is None or p not in by_id:
            roots.append(r["id"])
        else:
            children.setdefault(p, []).append(r["id"])

    seen: set[int] = set()
    conversations: list[dict] = []

    for root_id in roots:
        # BFS every descendant.
        stack = [root_id]
        group: list[dict] = []
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            m = by_id.get(cur)
            if m:
                group.append(m)
            stack.extend(children.get(cur, []))
        if not group:
            continue
        group.sort(key=lambda m: m.get("id", 0))

        # Trim messages but keep first + last MAX_MESSAGES_PER_CONV.
        if len(group) > MAX_MESSAGES_PER_CONV:
            trimmed = group[: 2] + group[-(MAX_MESSAGES_PER_CONV - 2):]
            group = trimmed

        status = _status_for(group, children)
        last_ts = group[-1].get("created_at", "")
        age = _age_minutes(last_ts)

        # Drop conversations completely outside the active window.
        if age > ACTIVE_WINDOW_MINUTES:
            continue

        conversations.append({
            "root_id":     root_id,
            "root_ts":     _fmt_ts(group[0].get("created_at", "")),
            "root_sender": group[0].get("sender", "?"),
            "root_body":   _short(group[0].get("body", "")),
            "messages": [
                {
                    "id":        m["id"],
                    "ts":        _fmt_ts(m.get("created_at", "")),
                    "sender":    m.get("sender", "?"),
                    "recipient": m.get("recipient", "?"),
                    "body":      _short(m.get("body", "")),
                }
                for m in group
            ],
            "last_ts":      _fmt_ts(last_ts),
            "last_sender":  group[-1].get("sender", "?"),
            "status":       status,
            "needs_action": status in ("open", "stalled"),
        })

    conversations.sort(key=lambda c: c["root_id"], reverse=True)
    return conversations[: MAX_CONVERSATIONS * 2]


def render(conversations: list[dict], viewer: str = "ceo") -> str:
    """
    Render for prompt injection. Open conversations first, then resolved.
    """
    if not conversations:
        return "(no active conversations)"

    open_convos     = [c for c in conversations if c["needs_action"]]
    resolved_convos = [c for c in conversations if not c["needs_action"]]
    open_convos     = open_convos[:MAX_CONVERSATIONS]
    resolved_convos = resolved_convos[: MAX_CONVERSATIONS - len(open_convos)]

    out: list[str] = []

    if open_convos:
        out.append("─── OPEN — NEEDS YOUR ACTION ───")
        for c in open_convos:
            out.append(_render_one(c, viewer))
    if resolved_convos:
        out.append("─── RESOLVED — DO NOT RE-ACT ───")
        for c in resolved_convos:
            out.append(_render_one(c, viewer))

    return "\n".join(out).rstrip()


def _render_one(c: dict, viewer: str) -> str:
    icon = {"open": "🔵", "stalled": "🟡", "resolved": "✅"}.get(c["status"], "?")
    header = (
        f"\n{icon} Conversation starting msg #{c['root_id']} "
        f"at {c['root_ts']} (last activity {c['last_ts']}, "
        f"state={c['status'].upper()})"
    )
    lines = [header]
    for m in c["messages"]:
        role = _role(m["sender"], m["recipient"])
        lines.append(
            f"    [{m['ts']}] (msg #{m['id']}) {role}: {m['body']}"
        )

    # Action hint
    if c["status"] in ("open", "stalled"):
        last_sender = c["last_sender"]
        if last_sender.startswith("worker_"):
            hint = (
                f"    ⚠️  Last message is from {last_sender}. "
                f"You must either reply to that worker, or verify its "
                f"report and respond to the user. Do NOT re-delegate."
            )
        elif last_sender.startswith("user_"):
            hint = (
                f"    ⚠️  Last message is from the user. "
                f"You must SEND_REPLY (or ASK_USER / delegate) — this "
                f"conversation is unanswered."
            )
        else:
            hint = f"    ⚠️  Unanswered. Act on this conversation."
        lines.append(hint)
    return "\n".join(lines)
