# orchestration/session_summary.py
"""
Cumulative session summary — the CEO's long-term working memory.

Sits alongside the 12-message inbox window and the conversation graph.
Covers turns that have aged out of both. Updated by a background
summariser call after every completed turn.

Design
──────
- CUMULATIVE. Facts and decisions accumulate. Only contradiction
  replaces. The 20-message window is the summariser's INPUT, not a
  replacement for the summary.

- TASK WORKING SET. While a task is active, its Attempts, Findings,
  and Dead Ends are preserved verbatim. This is what stops the CEO
  from re-trying approaches already tried in a 30-turn bug hunt.

- ONE-PASS COMPRESSION. When a task ends, the entire Active Task
  section collapses to a single bullet in the next output. No
  two-phase commit — the summariser does not remember promises made
  in previous cycles.

- ATTEMPTS ARE PROTECTED. If the summary would exceed
  MAX_SUMMARY_CHARS, compression happens in a fixed order that never
  drops attempts while the task is live.

- NO SILENT TASK CLOSURE. A task is only closed on explicit signals
  (user says done, user changes subject, next_step executed). Silence
  is not abandonment — tasks taking 40 turns are the reason this
  exists.

Read path (every CEO turn):
    render_for_prompt(agent_name) → markdown string, or ""

Write path (after every turn, background thread):
    maybe_update(agent_name) → fires the summariser if enough new
    messages have accumulated.

Storage:
    <org_workspace>/ai_civilization/agents/<name>/session_summary.md
    <org_workspace>/ai_civilization/agents/<name>/session_summary_meta.json
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────
WINDOW_MESSAGES = 20          # how many recent messages feed the summariser
MIN_NEW_SINCE_UPDATE = 6      # don't fire below this many new messages
MAX_SUMMARY_CHARS = 6000      # soft cap — attempts can override
SUMMARY_HEADER = "# Session Summary"

# Where the summariser's LLM comes from. Defaults to the deepseek key
# the rest of the system uses. Change to a cheaper model if you like.
_SUMMARISER_API_KEY_ENV = "deepseek"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(agent_name: str) -> threading.Lock:
    with _locks_guard:
        if agent_name not in _locks:
            _locks[agent_name] = threading.Lock()
        return _locks[agent_name]


# ── Paths ────────────────────────────────────────────────────────────
def _agent_dir(agent_name: str) -> Path:
    d = Path.cwd() / "ai_civilization" / "agents" / agent_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _summary_path(agent_name: str) -> Path:
    return _agent_dir(agent_name) / "session_summary.md"


def _meta_path(agent_name: str) -> Path:
    return _agent_dir(agent_name) / "session_summary_meta.json"


# ── Secret redaction (defense in depth) ──────────────────────────────
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}"
    r"|gsk_[A-Za-z0-9]{20,}"
    r"|emp_live_[A-Za-z0-9_\-]{20,}"
    r"|ghp_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}"
    r"|GOCSPX-[A-Za-z0-9_\-]{20,}"
    r"|\b[A-Fa-f0-9]{32,}\b"
    r"|\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"
    r")"
)


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text or "")


# ── Read ─────────────────────────────────────────────────────────────
def load_raw(agent_name: str) -> str:
    p = _summary_path(agent_name)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        logger.exception(f"could not read summary for {agent_name}")
        return ""


def has_attempts(agent_name: str) -> bool:
    """
    Return True if the current summary contains a non-empty Attempts
    section. Used by agent_loop to decide whether the thinking box's
    checked_attempts field is required this turn.
    """
    raw = load_raw(agent_name)
    if not raw:
        return False
    # Look for "### Attempts" followed by at least one "#N " line.
    m = re.search(r"###\s*Attempts\b(.*?)(?=\n###|\n##|$)", raw, re.DOTALL)
    if not m:
        return False
    return bool(re.search(r"^\s*#\d+\s", m.group(1), re.MULTILINE))


def render_for_prompt(agent_name: str) -> str:
    """Return the summary markdown, or '' if none exists."""
    raw = load_raw(agent_name).strip()
    if not raw:
        return ""
    if len(raw) > MAX_SUMMARY_CHARS:
        raw = raw[:MAX_SUMMARY_CHARS] + "\n\n...[summary truncated]"
    return raw


# ── Meta (last-summarised message id) ────────────────────────────────
def _last_summarised_id(agent_name: str) -> int:
    p = _meta_path(agent_name)
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return int(data.get("last_message_id", 0))
    except Exception:
        return 0


def _write_meta(agent_name: str, last_id: int) -> None:
    p = _meta_path(agent_name)
    p.write_text(
        json.dumps({
            "last_message_id": last_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )


# ── Prompt ───────────────────────────────────────────────────────────
def _build_summariser_prompt(old_summary: str, new_messages: str) -> str:
    return f"""You maintain a cumulative session summary for an AI agent.

This is NOT a log and NOT an audit trail. It is the minimum the agent
needs to resume the CURRENT conversation fluidly: what is being worked
on, what has been decided, what has already been tried, what is still
open. Anything from earlier that is no longer relevant to the current
work should be dropped, not preserved.

You are given:
  A. The current summary (may be empty for the first run).
  B. Recent conversation turns that may contain new information.

Output: the updated summary. Same structure, next revision.

── CORE RULES ──────────────────────────────────────────────────────

1. TASK WORKING SET — PROTECTED
   While a task is ACTIVE, its Attempts, Findings, and Dead Ends are
   the most important content in the summary. Preserve every attempt
   as a discrete numbered record with its result and conclusion. Do
   NOT drop, merge, or compress attempts while the task is live.
   This is exactly why the summary exists — after 30 turns on the
   same bug, the raw message window no longer shows what was tried.

2. TASK COMPLETION → ONE-PASS COMPRESSION
   When a task ends, replace the ENTIRE Active Task section (header,
   attempts, findings, dead ends) with a single bullet at the top of
   Facts in the SAME output:
       resolved: <one-line outcome>
   Do not leave the task section in place with status="done" and
   promise to compress it next cycle. Compress it now.

3. RELEVANCE FILTER (session sections only)
   The following applies to top-level Facts, Decisions, and Open
   Questions — NOT to the active task's Attempts.
     - A fact that has been contradicted → replace with the current
       value.
     - A decision that has been reversed → drop or replace.
     - A question that has been answered → drop.
     - A narrative ("we tried X then Y") whose lesson is captured as
       a decision or fact → drop the narrative, keep the lesson.

4. SIZE DISCIPLINE
   Target under {MAX_SUMMARY_CHARS} characters. If your output would
   exceed this, compress in THIS ORDER:
     a. Compress narrative and Recent Actions.
     b. Collapse related facts into grouped sub-sections.
     c. Compress each attempt to one line: "#N <action> → <result>".
     d. If still over, DROP Dead Ends and Findings first — the
        attempts themselves carry most of the information.
   NEVER drop or merge attempts while the task is live. Over-budget
   is preferable to losing the task's working set.

── TASK BOUNDARIES ─────────────────────────────────────────────────

A task stays ACTIVE until one of these signals appears in (B):

  CLOSE the current task when:
    • The user explicitly says it's done / resolved / moving on.
    • The user requests something clearly unrelated to the task.
    • The task's next_step has been executed and its outcome
      resolves the task.

  DO NOT close a task because:
    • It has been open a long time.
    • The last few turns mention something else briefly.
    • The user is silent about it for a while.

  If unsure whether to close, KEEP the task open. Preserving an extra
  turn of state is far cheaper than re-doing 20 turns of work.

  If a task hasn't been referenced in the last 40 turns and its
  next_step hasn't advanced, mark it status="stalled" but KEEP all
  attempts. Stalled tasks are not dropped — only explicit closure
  drops the attempts.

── OUTPUT STRUCTURE ─────────────────────────────────────────────────

  {SUMMARY_HEADER}
  Last updated: <ISO timestamp>
  Turns covered: <first_id>–<last_id>
  Current focus: <one short phrase>

  ## Active Task
  <1–2 line description>
  Started: <ISO timestamp> (<N> turns ago)
  Status: investigating | implementing | waiting_on_user | stalled
  Next step: <one line>

  ### Attempts
  <numbered list, oldest first. Each entry:>
      #N  <short description>
          Result: <what happened>
          Conclusion: <what this tells us> OR "inconclusive"

  ### Findings
  <bullets — facts discovered about THIS specific problem>

  ### Dead Ends
  <bullets — things ruled out, so they are not retried>

  ## Facts
  <session-level atomic facts, one per bullet>
  <"key = value" style where possible>
  <stale facts may be marked "(last verified N turns ago)">

  ## Decisions
  <bullets: "decision — one-line rationale" — only decisions still in force>

  ## Open Questions
  <bullets — questions waiting for a human answer>

  ## Recent Actions
  <bullets — last ~10 state-changing actions across the session>

── CONSTRAINTS ──────────────────────────────────────────────────────

- If (A) is empty and (B) doesn't cover much, keep the output small.
  Do not invent structure that isn't there yet.
- NEVER include passwords, API keys, tokens, or anything that looks
  like a secret. Write "[REDACTED]" if such a value would appear.
- Do not invent facts. If it isn't in (A) or (B), it isn't in the
  output.
- Do not include meta-commentary ("this is taking a while", "the
  user seems frustrated").
- Output only the markdown. No code fences, no prose before or after.

--- CURRENT SUMMARY (A) ---
{old_summary or "(empty — this is the first update)"}

--- RECENT TURNS (B) ---
{new_messages}

--- END ---
"""


def _build_shrink_prompt(summary: str) -> str:
    return f"""The session summary below is {len(summary)} characters.
It must be under {MAX_SUMMARY_CHARS}.

Rewrite it shorter. Apply this drop order:
  1. Compress narrative and Recent Actions.
  2. Collapse related facts into groups.
  3. Compress each attempt to one line "#N <action> → <result>".
  4. Drop Dead Ends and Findings before touching attempts.

DO NOT drop: the Active Task, any Attempts, Open Questions, or
current Decisions. If you must choose between over-budget and
losing the working set, stay over-budget.

Same structure. Output only the shorter markdown.

--- SUMMARY ---
{summary}"""


# ── LLM call ─────────────────────────────────────────────────────────
def _call_summariser(prompt: str) -> Optional[str]:
    """Call the small model. Returns the new summary or None on failure."""
    try:
        from llm import NativeLLM
        llm = NativeLLM(
            api_key=os.getenv(_SUMMARISER_API_KEY_ENV),
            temperature=0.2,
        )
        raw = llm.call(messages=[{"role": "user", "content": prompt}])
    except Exception:
        logger.exception("summariser LLM call failed")
        return None

    if not raw:
        return None

    text = raw.strip()
    # Strip code fences if the model added them anyway.
    text = re.sub(r"^```(?:markdown|md)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text).strip()

    if not text.startswith(SUMMARY_HEADER):
        logger.warning(
            "summariser output missing expected header — discarding "
            "to avoid corrupting the existing summary"
        )
        return None

    return text


# ── Formatting for the summariser ────────────────────────────────────
def _format_messages(rows: list[dict]) -> str:
    out = []
    for m in rows:
        sender = m.get("sender", "?")
        body = (m.get("body") or "").strip()
        if len(body) > 800:
            body = body[:800] + " ...[truncated]"
        ts = (m.get("created_at") or "")[:19]
        out.append(f"[#{m.get('id')}] {ts} {sender}:\n{body}\n")
    return "\n".join(out)


# ── Update ───────────────────────────────────────────────────────────
def _do_update(agent_name: str) -> None:
    """One summariser cycle. Runs in a background thread."""
    from orchestration import inbox

    try:
        last_id = _last_summarised_id(agent_name)

        rows = inbox.recent(agent_name, limit=WINDOW_MESSAGES * 3) or []
        fresh = [m for m in rows if int(m.get("id") or 0) > last_id]

        if len(fresh) < MIN_NEW_SINCE_UPDATE:
            return

        window = fresh[-WINDOW_MESSAGES:]
        if not window:
            return

        old = load_raw(agent_name)
        transcript = _format_messages(window)

        new_summary = _call_summariser(
            _build_summariser_prompt(old, transcript)
        )
        if not new_summary:
            return

        # Hard-cap pass. Rare. Only fires if the model ignored the
        # size rule.
        if len(new_summary) > MAX_SUMMARY_CHARS:
            shrunk = _call_summariser(_build_shrink_prompt(new_summary))
            if shrunk and len(shrunk) < len(new_summary):
                new_summary = shrunk

        # Redact secrets that slipped past the prompt rule.
        new_summary = _redact(new_summary)

        # Atomic write.
        path = _summary_path(agent_name)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(new_summary, encoding="utf-8")
        os.replace(tmp, path)

        newest_id = int(window[-1].get("id") or 0)
        _write_meta(agent_name, newest_id)

        logger.info(
            f"session summary updated for {agent_name} "
            f"(msg #{last_id}→#{newest_id}, "
            f"{len(new_summary):,} chars)"
        )
    except Exception:
        logger.exception(f"session summary update failed for {agent_name}")


def maybe_update(agent_name: str) -> None:
    """
    Called after every completed turn. Fires a background summariser
    update if enough new messages have accumulated. Never blocks.
    """
    def _run():
        lock = _lock_for(agent_name)
        if not lock.acquire(blocking=False):
            return
        try:
            _do_update(agent_name)
        finally:
            lock.release()

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"summary-{agent_name}",
    ).start()


def force_update(agent_name: str) -> str:
    """Synchronous update. For debugging or a manual tool."""
    lock = _lock_for(agent_name)
    with lock:
        _do_update(agent_name)
    return load_raw(agent_name) or "(still empty)"# orchestration/session_summary.py
"""
Cumulative session summary — the CEO's long-term working memory.

Sits alongside the 12-message inbox window and the conversation graph.
Covers turns that have aged out of both. Updated by a background
summariser call after every completed turn.

Design
──────
- CUMULATIVE. Facts and decisions accumulate. Only contradiction
  replaces. The 20-message window is the summariser's INPUT, not a
  replacement for the summary.

- TASK WORKING SET. While a task is active, its Attempts, Findings,
  and Dead Ends are preserved verbatim. This is what stops the CEO
  from re-trying approaches already tried in a 30-turn bug hunt.

- ONE-PASS COMPRESSION. When a task ends, the entire Active Task
  section collapses to a single bullet in the next output. No
  two-phase commit — the summariser does not remember promises made
  in previous cycles.

- ATTEMPTS ARE PROTECTED. If the summary would exceed
  MAX_SUMMARY_CHARS, compression happens in a fixed order that never
  drops attempts while the task is live.

- NO SILENT TASK CLOSURE. A task is only closed on explicit signals
  (user says done, user changes subject, next_step executed). Silence
  is not abandonment — tasks taking 40 turns are the reason this
  exists.

Read path (every CEO turn):
    render_for_prompt(agent_name) → markdown string, or ""

Write path (after every turn, background thread):
    maybe_update(agent_name) → fires the summariser if enough new
    messages have accumulated.

Storage:
    <org_workspace>/ai_civilization/agents/<name>/session_summary.md
    <org_workspace>/ai_civilization/agents/<name>/session_summary_meta.json
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────
WINDOW_MESSAGES = 20          # how many recent messages feed the summariser
MIN_NEW_SINCE_UPDATE = 6      # don't fire below this many new messages
MAX_SUMMARY_CHARS = 6000      # soft cap — attempts can override
SUMMARY_HEADER = "# Session Summary"

# Where the summariser's LLM comes from. Defaults to the deepseek key
# the rest of the system uses. Change to a cheaper model if you like.
_SUMMARISER_API_KEY_ENV = "deepseek"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(agent_name: str) -> threading.Lock:
    with _locks_guard:
        if agent_name not in _locks:
            _locks[agent_name] = threading.Lock()
        return _locks[agent_name]


# ── Paths ────────────────────────────────────────────────────────────
def _agent_dir(agent_name: str) -> Path:
    d = Path.cwd() / "ai_civilization" / "agents" / agent_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _summary_path(agent_name: str) -> Path:
    return _agent_dir(agent_name) / "session_summary.md"


def _meta_path(agent_name: str) -> Path:
    return _agent_dir(agent_name) / "session_summary_meta.json"


# ── Secret redaction (defense in depth) ──────────────────────────────
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}"
    r"|gsk_[A-Za-z0-9]{20,}"
    r"|emp_live_[A-Za-z0-9_\-]{20,}"
    r"|ghp_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}"
    r"|GOCSPX-[A-Za-z0-9_\-]{20,}"
    r"|\b[A-Fa-f0-9]{32,}\b"
    r"|\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"
    r")"
)


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text or "")


# ── Read ─────────────────────────────────────────────────────────────
def load_raw(agent_name: str) -> str:
    p = _summary_path(agent_name)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        logger.exception(f"could not read summary for {agent_name}")
        return ""


def has_attempts(agent_name: str) -> bool:
    """
    Return True if the current summary contains a non-empty Attempts
    section. Used by agent_loop to decide whether the thinking box's
    checked_attempts field is required this turn.
    """
    raw = load_raw(agent_name)
    if not raw:
        return False
    # Look for "### Attempts" followed by at least one "#N " line.
    m = re.search(r"###\s*Attempts\b(.*?)(?=\n###|\n##|$)", raw, re.DOTALL)
    if not m:
        return False
    return bool(re.search(r"^\s*#\d+\s", m.group(1), re.MULTILINE))


def render_for_prompt(agent_name: str) -> str:
    """Return the summary markdown, or '' if none exists."""
    raw = load_raw(agent_name).strip()
    if not raw:
        return ""
    if len(raw) > MAX_SUMMARY_CHARS:
        raw = raw[:MAX_SUMMARY_CHARS] + "\n\n...[summary truncated]"
    return raw


# ── Meta (last-summarised message id) ────────────────────────────────
def _last_summarised_id(agent_name: str) -> int:
    p = _meta_path(agent_name)
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return int(data.get("last_message_id", 0))
    except Exception:
        return 0


def _write_meta(agent_name: str, last_id: int) -> None:
    p = _meta_path(agent_name)
    p.write_text(
        json.dumps({
            "last_message_id": last_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )


# ── Prompt ───────────────────────────────────────────────────────────
def _build_summariser_prompt(old_summary: str, new_messages: str) -> str:
    return f"""You maintain a cumulative session summary for an AI agent.

This is NOT a log and NOT an audit trail. It is the minimum the agent
needs to resume the CURRENT conversation fluidly: what is being worked
on, what has been decided, what has already been tried, what is still
open. Anything from earlier that is no longer relevant to the current
work should be dropped, not preserved.

You are given:
  A. The current summary (may be empty for the first run).
  B. Recent conversation turns that may contain new information.

Output: the updated summary. Same structure, next revision.

── CORE RULES ──────────────────────────────────────────────────────

1. TASK WORKING SET — PROTECTED
   While a task is ACTIVE, its Attempts, Findings, and Dead Ends are
   the most important content in the summary. Preserve every attempt
   as a discrete numbered record with its result and conclusion. Do
   NOT drop, merge, or compress attempts while the task is live.
   This is exactly why the summary exists — after 30 turns on the
   same bug, the raw message window no longer shows what was tried.

2. TASK COMPLETION → ONE-PASS COMPRESSION
   When a task ends, replace the ENTIRE Active Task section (header,
   attempts, findings, dead ends) with a single bullet at the top of
   Facts in the SAME output:
       resolved: <one-line outcome>
   Do not leave the task section in place with status="done" and
   promise to compress it next cycle. Compress it now.

3. RELEVANCE FILTER (session sections only)
   The following applies to top-level Facts, Decisions, and Open
   Questions — NOT to the active task's Attempts.
     - A fact that has been contradicted → replace with the current
       value.
     - A decision that has been reversed → drop or replace.
     - A question that has been answered → drop.
     - A narrative ("we tried X then Y") whose lesson is captured as
       a decision or fact → drop the narrative, keep the lesson.

4. SIZE DISCIPLINE
   Target under {MAX_SUMMARY_CHARS} characters. If your output would
   exceed this, compress in THIS ORDER:
     a. Compress narrative and Recent Actions.
     b. Collapse related facts into grouped sub-sections.
     c. Compress each attempt to one line: "#N <action> → <result>".
     d. If still over, DROP Dead Ends and Findings first — the
        attempts themselves carry most of the information.
   NEVER drop or merge attempts while the task is live. Over-budget
   is preferable to losing the task's working set.

── TASK BOUNDARIES ─────────────────────────────────────────────────

A task stays ACTIVE until one of these signals appears in (B):

  CLOSE the current task when:
    • The user explicitly says it's done / resolved / moving on.
    • The user requests something clearly unrelated to the task.
    • The task's next_step has been executed and its outcome
      resolves the task.

  DO NOT close a task because:
    • It has been open a long time.
    • The last few turns mention something else briefly.
    • The user is silent about it for a while.

  If unsure whether to close, KEEP the task open. Preserving an extra
  turn of state is far cheaper than re-doing 20 turns of work.

  If a task hasn't been referenced in the last 40 turns and its
  next_step hasn't advanced, mark it status="stalled" but KEEP all
  attempts. Stalled tasks are not dropped — only explicit closure
  drops the attempts.

── OUTPUT STRUCTURE ─────────────────────────────────────────────────

  {SUMMARY_HEADER}
  Last updated: <ISO timestamp>
  Turns covered: <first_id>–<last_id>
  Current focus: <one short phrase>

  ## Active Task
  <1–2 line description>
  Started: <ISO timestamp> (<N> turns ago)
  Status: investigating | implementing | waiting_on_user | stalled
  Next step: <one line>

  ### Attempts
  <numbered list, oldest first. Each entry:>
      #N  <short description>
          Result: <what happened>
          Conclusion: <what this tells us> OR "inconclusive"

  ### Findings
  <bullets — facts discovered about THIS specific problem>

  ### Dead Ends
  <bullets — things ruled out, so they are not retried>

  ## Facts
  <session-level atomic facts, one per bullet>
  <"key = value" style where possible>
  <stale facts may be marked "(last verified N turns ago)">

  ## Decisions
  <bullets: "decision — one-line rationale" — only decisions still in force>

  ## Open Questions
  <bullets — questions waiting for a human answer>

  ## Recent Actions
  <bullets — last ~10 state-changing actions across the session>

── CONSTRAINTS ──────────────────────────────────────────────────────

- If (A) is empty and (B) doesn't cover much, keep the output small.
  Do not invent structure that isn't there yet.
- NEVER include passwords, API keys, tokens, or anything that looks
  like a secret. Write "[REDACTED]" if such a value would appear.
- Do not invent facts. If it isn't in (A) or (B), it isn't in the
  output.
- Do not include meta-commentary ("this is taking a while", "the
  user seems frustrated").
- Output only the markdown. No code fences, no prose before or after.

--- CURRENT SUMMARY (A) ---
{old_summary or "(empty — this is the first update)"}

--- RECENT TURNS (B) ---
{new_messages}

--- END ---
"""


def _build_shrink_prompt(summary: str) -> str:
    return f"""The session summary below is {len(summary)} characters.
It must be under {MAX_SUMMARY_CHARS}.

Rewrite it shorter. Apply this drop order:
  1. Compress narrative and Recent Actions.
  2. Collapse related facts into groups.
  3. Compress each attempt to one line "#N <action> → <result>".
  4. Drop Dead Ends and Findings before touching attempts.

DO NOT drop: the Active Task, any Attempts, Open Questions, or
current Decisions. If you must choose between over-budget and
losing the working set, stay over-budget.

Same structure. Output only the shorter markdown.

--- SUMMARY ---
{summary}"""


# ── LLM call ─────────────────────────────────────────────────────────
def _call_summariser(prompt: str) -> Optional[str]:
    """Call the small model. Returns the new summary or None on failure."""
    try:
        from llm import NativeLLM
        llm = NativeLLM(
            api_key=os.getenv(_SUMMARISER_API_KEY_ENV),
            temperature=0.2,
        )
        raw = llm.call(messages=[{"role": "user", "content": prompt}])
    except Exception:
        logger.exception("summariser LLM call failed")
        return None

    if not raw:
        return None

    text = raw.strip()
    # Strip code fences if the model added them anyway.
    text = re.sub(r"^```(?:markdown|md)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text).strip()

    if not text.startswith(SUMMARY_HEADER):
        logger.warning(
            "summariser output missing expected header — discarding "
            "to avoid corrupting the existing summary"
        )
        return None

    return text


# ── Formatting for the summariser ────────────────────────────────────
def _format_messages(rows: list[dict]) -> str:
    out = []
    for m in rows:
        sender = m.get("sender", "?")
        body = (m.get("body") or "").strip()
        if len(body) > 800:
            body = body[:800] + " ...[truncated]"
        ts = (m.get("created_at") or "")[:19]
        out.append(f"[#{m.get('id')}] {ts} {sender}:\n{body}\n")
    return "\n".join(out)


# ── Update ───────────────────────────────────────────────────────────
def _do_update(agent_name: str) -> None:
    """One summariser cycle. Runs in a background thread."""
    from orchestration import inbox

    try:
        last_id = _last_summarised_id(agent_name)

        rows = inbox.recent(agent_name, limit=WINDOW_MESSAGES * 3) or []
        fresh = [m for m in rows if int(m.get("id") or 0) > last_id]

        if len(fresh) < MIN_NEW_SINCE_UPDATE:
            return

        window = fresh[-WINDOW_MESSAGES:]
        if not window:
            return

        old = load_raw(agent_name)
        transcript = _format_messages(window)

        new_summary = _call_summariser(
            _build_summariser_prompt(old, transcript)
        )
        if not new_summary:
            return

        # Hard-cap pass. Rare. Only fires if the model ignored the
        # size rule.
        if len(new_summary) > MAX_SUMMARY_CHARS:
            shrunk = _call_summariser(_build_shrink_prompt(new_summary))
            if shrunk and len(shrunk) < len(new_summary):
                new_summary = shrunk

        # Redact secrets that slipped past the prompt rule.
        new_summary = _redact(new_summary)

        # Atomic write.
        path = _summary_path(agent_name)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(new_summary, encoding="utf-8")
        os.replace(tmp, path)

        newest_id = int(window[-1].get("id") or 0)
        _write_meta(agent_name, newest_id)

        logger.info(
            f"session summary updated for {agent_name} "
            f"(msg #{last_id}→#{newest_id}, "
            f"{len(new_summary):,} chars)"
        )
    except Exception:
        logger.exception(f"session summary update failed for {agent_name}")


def maybe_update(agent_name: str) -> None:
    """
    Called after every completed turn. Fires a background summariser
    update if enough new messages have accumulated. Never blocks.
    """
    def _run():
        lock = _lock_for(agent_name)
        if not lock.acquire(blocking=False):
            return
        try:
            _do_update(agent_name)
        finally:
            lock.release()

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"summary-{agent_name}",
    ).start()


def force_update(agent_name: str) -> str:
    """Synchronous update. For debugging or a manual tool."""
    lock = _lock_for(agent_name)
    with lock:
        _do_update(agent_name)
    return load_raw(agent_name) or "(still empty)"
