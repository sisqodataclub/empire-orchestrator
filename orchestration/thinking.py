# orchestration/thinking.py
"""
The thinking box.

Before every action, an agent must produce a structured reasoning
block. The block forces the LLM to separate what it knows from what
it doesn't, name what it needs before getting it, and state the
inference chain from evidence to decision.

Required shape:

    "thinking": {
      "question":  "What am I actually being asked to do?",
      "know":      ["fact from context", ...],
      "dont_know": ["gap that matters", ...],
      "need":      ["specific information to close the gap", ...],
      "how":       ["which tool / which agent / which file", ...],
      "connect":   "The chain from what I know to what I'll do"
    }

The block is validated for structure before the action runs. It's
written to agents/<name>/logs/thinking.log for inspection.

One soft cross-check: if `dont_know` is non-empty and the action is
SEND_REPLY, the reply body should acknowledge the unknowns. If it
doesn't, a warning is appended to the prompt (once per turn) so the
LLM can reconsider.

Nothing here depends on the mission file, the reply contract, or
the framework. The thinking box is a standalone reasoning trace.
"""
import os
import re
from datetime import datetime
from typing import Optional

from orchestration import agents


REQUIRED_FIELDS = ("question", "know", "dont_know", "need", "how", "connect")
LIST_FIELDS     = ("know", "dont_know", "need", "how")
STRING_FIELDS   = ("question", "connect")

# Phrases that signal a reply is being honest about not knowing.
_HONESTY_MARKERS = (
    "don't know", "do not know", "not sure", "cannot verify",
    "can't verify", "couldn't", "could not", "unable to",
    "no way to know", "unverified", "cannot confirm",
    "can't confirm", "haven't started", "not started",
    "i don't have", "i have no",
)


# ── Validation ───────────────────────────────────────────────────────
def parse_and_validate(plan: dict) -> tuple[Optional[dict], Optional[str]]:
    """
    Extract the thinking block from `plan` and validate its structure.

    Returns (thinking_dict, None) on success, or (None, error_message)
    on failure. The caller appends error_message to the prompt tail
    and continues the loop.
    """
    if not isinstance(plan, dict):
        return None, "Response is not a JSON object."

    think = plan.get("thinking")
    if think is None:
        return None, (
            "Missing 'thinking' block. Every response must start with "
            f"a thinking object containing: {', '.join(REQUIRED_FIELDS)}."
        )
    if not isinstance(think, dict):
        return None, "'thinking' must be a JSON object, not a string or list."

    missing = [f for f in REQUIRED_FIELDS if f not in think]
    if missing:
        return None, (
            f"'thinking' is missing required fields: {missing}. "
            f"All of these are required: {', '.join(REQUIRED_FIELDS)}."
        )

    for field in STRING_FIELDS:
        if not isinstance(think[field], str) or not think[field].strip():
            return None, f"'thinking.{field}' must be a non-empty string."

    for field in LIST_FIELDS:
        value = think[field]
        if not isinstance(value, list):
            return None, f"'thinking.{field}' must be a list (may be empty)."
        for i, item in enumerate(value):
            if not isinstance(item, str):
                return None, f"'thinking.{field}[{i}]' must be a string."

    return think, None


# ── Reply consistency ────────────────────────────────────────────────
def check_reply_consistency(think: dict, reply_body: str) -> Optional[str]:
    """
    Soft check: if the thinking block lists unknowns but the reply
    doesn't acknowledge any of them, return a warning string. The
    caller appends it to the prompt once and lets the LLM decide
    whether to resend.

    Returns None if the check passes or doesn't apply.
    """
    gaps = think.get("dont_know") or []
    if not gaps:
        return None

    body_lower = (reply_body or "").lower()

    # If the reply already acknowledges uncertainty, we're fine.
    if any(marker in body_lower for marker in _HONESTY_MARKERS):
        return None

    # Otherwise, check whether the reply overlaps with any listed gap.
    body_words = _content_words(body_lower)
    overlapped = []
    for gap in gaps:
        gap_words = _content_words(gap.lower())
        if not gap_words:
            continue
        overlap = len(body_words & gap_words) / max(len(gap_words), 1)
        if overlap >= 0.5:
            overlapped.append(gap)

    if not overlapped:
        return None

    gap_lines = "\n".join(f"  • {g}" for g in overlapped)
    return (
        f"[SYSTEM] Your thinking block listed these as unknowns:\n"
        f"{gap_lines}\n\n"
        f"Your reply doesn't acknowledge them. If the reply asserts any "
        f"of these as fact, rewrite it to be honest about what you "
        f"don't know. If the reply is genuinely fine as written, "
        f"resend it unchanged."
    )


def _content_words(text: str) -> set:
    """Words of 5+ chars, lowercased. Loose filter for matching."""
    return {w for w in re.findall(r"\b[a-z]{5,}\b", text)}


# ── Logging ──────────────────────────────────────────────────────────
def write_thinking_log(
    agent_name: str,
    turn: int,
    think: dict,
    action_type: str,
    action_payload: dict,
) -> None:
    """
    Append the thinking block to agents/<name>/logs/thinking.log.

    Format is human-readable — one block per turn, indented lists,
    timestamped header. Not machine-parsed; exists for inspection.
    """
    d = agents.logs_dir(agent_name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "thinking.log")

    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    action_desc = action_type or "?"
    if action_type == "CALL_TOOL":
        tool = (action_payload or {}).get("tool_name", "?")
        action_desc = f"CALL_TOOL {tool}"

    lines = [
        "",
        "─" * 70,
        f"{ts}  turn={turn}  action={action_desc}",
        "─" * 70,
        f"Question:  {think.get('question', '')}",
    ]

    for field, label in (
        ("know",      "Know"),
        ("dont_know", "Don't know"),
        ("need",      "Need"),
        ("how",       "How"),
    ):
        items = think.get(field) or []
        if items:
            lines.append(f"{label}:")
            for item in items:
                lines.append(f"  • {item}")
        else:
            lines.append(f"{label}: (none)")

    lines.append(f"Connect:   {think.get('connect', '')}")
    lines.append("")

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        # Never let logging break a turn.
        pass
