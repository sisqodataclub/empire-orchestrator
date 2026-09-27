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
      "connect":   "The chain from what I know to what I'll do",
      "checked_attempts": ["#3", "#5"]     # conditional
    }

`checked_attempts` is required when the CEO prompt contains a
non-empty "### Attempts" section (from the session summary). The
caller passes `require_checked_attempts=True` in that case. When
required, the field must be a non-empty list of strings — usually
attempt IDs like "#3", or the sentinel ["none-match"].

The block is validated for structure before the action runs. It's
written to agents/<name>/logs/thinking.log for inspection.

One soft cross-check: if `dont_know` is non-empty and the action is
SEND_REPLY, the reply body should acknowledge the unknowns. If it
doesn't, a warning is appended to the prompt (once per turn).
"""
import os
import re
from datetime import datetime
from typing import Optional

from orchestration import agents


REQUIRED_FIELDS = ("question", "know", "dont_know", "need", "how", "connect")
OPTIONAL_FIELDS = ("checked_attempts",)
LIST_FIELDS     = ("know", "dont_know", "need", "how")
STRING_FIELDS   = ("question", "connect")

_HONESTY_MARKERS = (
    "don't know", "do not know", "not sure", "cannot verify",
    "can't verify", "couldn't", "could not", "unable to",
    "no way to know", "unverified", "cannot confirm",
    "can't confirm", "haven't started", "not started",
    "i don't have", "i have no",
)


# ── Validation ───────────────────────────────────────────────────────
def parse_and_validate(
    plan: dict,
    require_checked_attempts: bool = False,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Extract the thinking block from `plan` and validate its structure.

    If `require_checked_attempts` is True, the field must be present
    and a non-empty list of strings. Pass this when the prompt
    contains a non-empty "### Attempts" section, so the agent is
    forced to state which prior attempts it has considered.

    Returns (thinking_dict, None) on success, or (None, error_message)
    on failure.
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

    # ── checked_attempts (conditional) ──────────────────────────────
    checked = think.get("checked_attempts")
    if require_checked_attempts:
        if checked is None:
            return None, (
                "'thinking.checked_attempts' is required this turn "
                "because the session summary contains an Attempts "
                "section. List the attempt IDs (e.g. [\"#3\", \"#7\"]) "
                "from '### Attempts' that are relevant to what you are "
                "about to do. If none apply, write [\"none-match\"] and "
                "explain why in 'connect'."
            )
        if not isinstance(checked, list) or not checked:
            return None, (
                "'thinking.checked_attempts' must be a non-empty list "
                "of strings."
            )
        for i, item in enumerate(checked):
            if not isinstance(item, str):
                return None, f"'thinking.checked_attempts[{i}]' must be a string."
    else:
        if checked is not None:
            if not isinstance(checked, list):
                return None, "'thinking.checked_attempts' must be a list."
            for i, item in enumerate(checked):
                if not isinstance(item, str):
                    return None, f"'thinking.checked_attempts[{i}]' must be a string."

    return think, None


# ── Reply consistency ────────────────────────────────────────────────
def check_reply_consistency(think: dict, reply_body: str) -> Optional[str]:
    """
    Soft check: if the thinking block lists unknowns but the reply
    doesn't acknowledge any of them, return a warning string.
    """
    gaps = think.get("dont_know") or []
    if not gaps:
        return None

    body_lower = (reply_body or "").lower()
    if any(marker in body_lower for marker in _HONESTY_MARKERS):
        return None

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
    return {w for w in re.findall(r"\b[a-z]{5,}\b", text)}


# ── Logging ──────────────────────────────────────────────────────────
def write_thinking_log(
    agent_name: str,
    turn: int,
    think: dict,
    action_type: str,
    action_payload: dict,
) -> None:
    """Append the thinking block to thinking.log."""
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

    checked = think.get("checked_attempts")
    if checked:
        lines.append(f"Checked attempts: {', '.join(checked)}")

    lines.append(f"Connect:   {think.get('connect', '')}")
    lines.append("")

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
