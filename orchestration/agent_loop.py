# orchestration/agent_loop.py
"""
The one loop. CEO and every worker run this via their own dispatcher.

Entry points:
    dispatcher(agent_name, start_from=None)   — poll inbox, call run_agent_turn
    run_agent_turn(agent_name, msg)           — handle one message start-to-finish

Logging:
    Three log files per agent under agents/<agent>/logs/:
      • tools.log     — two lines per tool call.
      • thinking.log  — one structured reasoning block per turn.

    The CEO reads a worker's tool log with read_agent_log.

Tool availability:
    Every registered tool is available to every agent. The only gate is
    delegation: `send_message` is CEO-only.

Thinking box:
    Before every action, the agent must produce a structured reasoning
    block. Validated for structure. Logged to thinking.log.

    A soft cross-check warns on CEO replies that assert things the
    thinking block listed as unknown.

    The CEO also sees:
      • its own previous thinking block ("YOUR LAST REASONING")
      • its own recent replies to whoever it is currently replying to
        ("YOUR RECENT REPLIES") — covering both user and worker threads,
        so it does not contradict itself across turns.

Conversation graph (CEO only):
    Every message has a parent_message_id pointing at the message it
    responds to. That graph crosses threads — the CEO's delegation to
    a worker has the user message as its parent, the worker's report
    has the delegation as its parent, the CEO's reply to the user has
    the report as its parent. build_conversations() walks the graph
    and renders the whole arc per conversation, with a derived status
    (open / stalled / resolved). This is what stops the CEO from
    re-delegating a task it has already completed — the resolved
    conversation shows the delegation, the worker's report, and the
    CEO's confirmation all in one block.

Session summary (CEO only):
    A cumulative markdown summary of earlier turns, maintained by a
    background summariser (orchestration/session_summary.py). Covers
    turns that have aged out of the 12-message inbox window and the
    90-minute conversation graph. Injected into the CEO prompt as
    "EARLIER SESSION CONTEXT".

    When the summary contains an active task with an Attempts list,
    the thinking box requires a `checked_attempts` field so the CEO
    declares which prior attempts it has considered before acting.

    Updates are fire-and-forget: after every completed turn, a
    background thread checks whether enough new messages have
    accumulated to justify a summariser call. Never blocks the
    dispatcher.

Delegation dedup:
    Before the CEO's send_message to a worker goes through, the
    delegation body is compared against the CEO's last few delegations
    to that same worker. If it's a near-duplicate, the send is blocked
    and the CEO gets a system note. This is a second line of defence
    behind the conversation graph.

Reply contract:
    Every CEO SEND_REPLY to a user carries a machine-readable claim
    (claimed_actions + evidence_ids) validated against a turn ledger.
    Unbacked claims are rejected. Workers bypass this contract.

Grace turn:
    One last LLM call before giving up, bypassing contract and thinking.

Tool-arg aliases:
    A tool parameter that has been renamed will still be called by its
    old name for a while — the LLM imitates its own recent tool calls,
    which are fed back into the prompt from tools.log. _TOOL_ARG_ALIASES
    remaps old names to new ones before dispatch so a rename takes
    effect immediately without waiting for history to age out.
"""
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

import empire_tools
from orchestration import agents, inbox, messenger, conversations
from orchestration import session_summary
from orchestration.turn_ledger import TurnLedger
from orchestration.reply_contract import ClaimedAction, validate as validate_reply
from orchestration.thinking import (
    parse_and_validate as parse_thinking,
    check_reply_consistency,
    write_thinking_log,
)
import ceo_prompter


logger = logging.getLogger(__name__)

MAX_TURNS = 60
MAX_IDENTICAL_FAILURES = 2
HARD_STOP_FAILURES = 3
TOOL_HISTORY_LINES = 12
REASONING_MAX_CHARS = 2500
REPLIES_HISTORY_LIMIT = 6
REPLIES_BODY_CHARS = 240

# How similar two delegations must be (word overlap ratio) to count
# as a duplicate. Lower to 0.60 if the CEO keeps sneaking through
# with minor rephrasings.
DELEGATION_DUPLICATE_THRESHOLD = 0.65

# How many recent delegations to the same worker to check against.
DELEGATION_LOOKBACK = 4

# Backward-compatibility aliases for tool parameters that have been
# renamed. The LLM imitates its own tool history from tools.log, so
# after a rename it keeps sending the old name for a while. Remap
# here so the rename takes effect without breaking the LLM.
_TOOL_ARG_ALIASES: dict[str, dict[str, str]] = {
    "internet_search": {"raw_query": "query"},
    # Future renames go here: "tool_key": {"old_name": "new_name"},
}

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        from llm import NativeLLM
        _llm = NativeLLM(api_key=os.getenv("deepseek"), temperature=0.7)
    return _llm


# ── Tool logging ─────────────────────────────────────────────────────
def log_tool_event(agent_name: str, kind: str, detail: str) -> None:
    """Append a line to agents/<agent_name>/logs/tools.log."""
    d = agents.logs_dir(agent_name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "tools.log")
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts}  {kind:<10}  {detail}\n")
    except Exception:
        logger.exception(f"Failed to append to {path}")


# ── Recent activity (for prompt context) ─────────────────────────────
def recent_tool_activity(agent_name: str, limit: int = TOOL_HISTORY_LINES) -> str:
    """
    Return the last `limit` lines of the agent's own tools.log.
    Fed into the prompt as "YOUR RECENT TOOL CALLS".
    """
    path = os.path.join(agents.logs_dir(agent_name), "tools.log")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except Exception:
        return ""

    out = []
    for line in lines:
        stripped = re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\s+", "", line.rstrip())
        if stripped:
            out.append("  " + stripped)
    return "\n".join(out)


# ── Last reasoning (for prompt context) ──────────────────────────────
def recent_reasoning(agent_name: str, max_chars: int = REASONING_MAX_CHARS) -> str:
    """
    Read the LAST thinking block from thinking.log and return it for
    prompt injection. Blocks are separated by ─── header lines.
    """
    path = os.path.join(agents.logs_dir(agent_name), "thinking.log")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""

    header_re = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\s+turn=\d+\s+action=",
        re.MULTILINE,
    )
    matches = list(header_re.finditer(content))
    if not matches:
        return ""

    block = content[matches[-1].start():].rstrip()

    if len(block) > max_chars:
        block = block[:max_chars] + "\n...[truncated]"
    return block


# ── Recent replies (for prompt context) ──────────────────────────────
def recent_replies(
    agent_name: str,
    msg: dict,
    limit: int = REPLIES_HISTORY_LIMIT,
) -> str:
    """
    Return the agent's own recent replies to whoever it is CURRENTLY
    replying to.

    For the CEO:
      • If the incoming message is from a worker, the CEO's replies
        to that worker went to the worker's thread. Read it.
      • Otherwise, the reply target is the user thread from
        inbox.reply_target(). Read it.

    For a worker:
      • Its replies go to the CEO thread. Read "ceo".

    Purpose: let the agent see what it already told this counterpart,
    so it does not re-answer, re-delegate, or contradict itself.
    """
    if agent_name == "ceo":
        sender = msg.get("sender", "")
        if sender.startswith("worker_"):
            target_thread = sender
        else:
            target_thread = inbox.reply_target(agent_name, msg) or ""
    else:
        target_thread = "ceo"

    if not target_thread:
        return ""

    try:
        rows = inbox.recent(target_thread, limit=limit * 4) or []
    except Exception:
        return ""

    own = [m for m in rows if m.get("sender") == agent_name]
    own = own[-limit:]
    if not own:
        return ""

    out = []
    for m in own:
        ts   = (m.get("created_at") or "")[:16]
        body = (m.get("body") or "").strip().replace("\n", " ")
        if len(body) > REPLIES_BODY_CHARS:
            body = body[: REPLIES_BODY_CHARS - 3] + "..."
        out.append(f"  [{ts}] {body}")
    return "\n".join(out)


# ── Conversation graph (for CEO prompt context) ──────────────────────
def _build_conversation_block() -> str:
    """
    Walk the inbox parent_message_id graph and render every active
    conversation. Returns the rendered block, or "" on failure. Never
    raises — a graph failure must not block the turn.
    """
    try:
        convos = conversations.build_conversations()
        if not convos:
            return ""
        return conversations.render(convos, viewer="ceo")
    except Exception:
        logger.exception("conversation graph build failed")
        return ""


# ── Delegation dedup helper ──────────────────────────────────────────
def _similar_text(a: str, b: str, threshold: float = 0.75) -> bool:
    """
    Cheap word-overlap check. True if two message bodies are
    substantially the same. Used to block duplicate delegations.
    """
    a_words = set(re.findall(r"\b\w{4,}\b", (a or "").lower()))
    b_words = set(re.findall(r"\b\w{4,}\b", (b or "").lower()))
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words) / max(len(a_words | b_words), 1)
    return overlap >= threshold


# ── Grace turn — final honest reply before giving up ─────────────────
def _grace_turn(agent_name: str, msg: dict,
                prompt: str, tail: str, reason: str) -> bool:
    """
    One last LLM call when the loop is about to give up. Bypasses both
    the reply contract and the thinking box.
    """
    directive = f"""
━━━ OUT OF TURNS — FINAL REPLY REQUIRED ━━━
You've run out of turns without completing the task.

Internal reason (do NOT quote this to the user): {reason}

Do NOT call any more tools. Instead, SEND_REPLY to the user with a
plain-language explanation. Tell them:

  1. What you were trying to do (one line)
  2. What blocked you (one line — no stack traces, no error codes,
     no tool names, no JSON fragments)
  3. What they can try next (one line, or "nothing — this needs
     attention from the system owner")

Keep it short and human. The user is not a developer.

Do NOT include a thinking block for this reply. Reply with ONE JSON
object:
  {{"action_type": "SEND_REPLY",
    "action_payload": {{"body": "<your reply>", "attachments": []}}}}
"""
    try:
        raw = _get_llm().call(
            messages=[{"role": "user", "content": prompt + tail + directive}]
        )
    except Exception:
        logger.exception(f"[{agent_name}] grace-turn LLM call failed")
        return False

    plan = _parse_json(raw)
    if (plan.get("action_type") or "").strip() != "SEND_REPLY":
        return False

    reply_body = ((plan.get("action_payload") or {}).get("body") or "").strip()
    if not reply_body:
        return False

    target = inbox.reply_target(agent_name, msg) or msg.get("sender", "ceo")
    inbox.send(
        thread=target,
        sender=agent_name,
        body=reply_body,
        parent_id=msg.get("id"),
    )
    log_tool_event(agent_name, "grace_reply", f"to={target}")
    logger.info(f"[{agent_name}] grace reply -> {target}: {reply_body[:120]}")
    return True


# ── Dispatcher ───────────────────────────────────────────────────────
def dispatcher(agent_name: str, start_from: Optional[int] = None) -> None:
    """
    Poll `agent_name`'s inbox every second. For every new row whose sender
    isn't the agent itself, run one turn.
    """
    if start_from is None:
        last_seen = inbox.max_id(agent_name)
    else:
        last_seen = int(start_from)

    logger.info(f"[{agent_name}] dispatcher running (last_seen={last_seen})")

    while True:
        try:
            for msg in inbox.since(agent_name, last_seen):
                if msg.get("sender") != agent_name:
                    try:
                        run_agent_turn(agent_name, msg)
                    except Exception:
                        logger.exception(f"[{agent_name}] turn crashed")
                        _write_error(agent_name, msg, "Internal error during turn.")
                    finally:
                        # Fire-and-forget session summary update.
                        # Runs in its own thread; never blocks the
                        # dispatcher. Dedups against the last
                        # summarised message ID internally.
                        try:
                            session_summary.maybe_update(agent_name)
                        except Exception:
                            logger.exception(
                                f"[{agent_name}] session summary trigger failed"
                            )
                last_seen = max(last_seen, int(msg.get("id") or 0))
        except Exception:
            logger.exception(f"[{agent_name}] dispatcher iteration failed")
        time.sleep(1)


# ── The turn ─────────────────────────────────────────────────────────
def run_agent_turn(agent_name: str, msg: dict) -> None:
    cfg = agents.get(agent_name)
    if cfg is None:
        logger.warning(f"run_agent_turn: unknown agent '{agent_name}'")
        return

    sender = msg.get("sender", "?")
    body   = (msg.get("body") or "").strip()
    msg_id = msg.get("id")

    if not body:
        return

    logger.info(f"[{agent_name}] <- {sender}: {body[:120]}")
    log_tool_event(agent_name, "receive", f"from={sender}")

    messenger.set_context(agent_name, msg_id)
    empire_tools.set_log_context(agent_name)

    ledger = TurnLedger()

    history = inbox.format_history(agent_name, agent_name, limit=12)
    from_worker = sender.startswith("worker_")
    worker_is_asking = from_worker and body.startswith("[QUESTION]")

    agent_tools = agents.all_tools()
    tool_history = recent_tool_activity(agent_name)
    last_reasoning = recent_reasoning(agent_name)

    # The agent's own recent replies to whoever it is currently
    # replying to — user thread or worker thread for the CEO, CEO
    # thread for workers.
    replies_history = recent_replies(agent_name, msg)

    # Conversation graph — CEO only. Walks the parent_message_id chain
    # across all threads to give a single coherent view of every
    # active conversation, with derived status. Empty for workers.
    conversations_block = _build_conversation_block() if cfg["is_ceo"] else ""

    # Cumulative session summary — CEO only. Covers turns that have
    # aged out of the 12-message inbox window and the 90-minute
    # conversation graph. Empty on early turns. When the summary
    # contains an active task with an Attempts list, the thinking
    # box requires the `checked_attempts` field this turn.
    session_summary_block = ""
    require_checked_attempts = False
    if cfg["is_ceo"]:
        try:
            session_summary_block = session_summary.render_for_prompt(agent_name)
            require_checked_attempts = session_summary.has_attempts(agent_name)
        except Exception:
            logger.exception(f"[{agent_name}] session summary read failed")

    if cfg["is_ceo"]:
        prompt = ceo_prompter.build_ceo_prompt(
            user_message=body,
            inbox_history=history,
            conversations_block=conversations_block,
            cwd=agents.WORKSPACE_DIR,
            thread_id=agent_name,
            tools=agent_tools,
            available_agents=agents.worker_roster(),
            from_worker=from_worker,
            worker_report=body if from_worker else "",
            delegated_role=sender if from_worker else "",
            worker_is_asking=worker_is_asking,
            tool_history=tool_history,
            last_reasoning=last_reasoning,
            replies_history=replies_history,
            session_summary=session_summary_block,
        )
    else:
        prompt = ceo_prompter.build_worker_prompt(
            role=cfg["role"],
            instruction=body,
            cwd=agents.WORKSPACE_DIR,
            tools=agent_tools,
            thread_id=agent_name,
            inbox_history=history,
            tool_history=tool_history,
            replies_history=replies_history,
        )

    failure_counts: dict = {}
    tools_called_this_turn = 0
    thinking_consistency_warned = False

    tail = ""
    for turn in range(1, MAX_TURNS + 1):
        try:
            raw = _get_llm().call(
                messages=[{"role": "user", "content": prompt + tail}]
            )
        except Exception as e:
            logger.exception(f"[{agent_name}] LLM call failed")
            reason = f"LLM call failed: {e}"
            if _grace_turn(agent_name, msg, prompt, tail, reason):
                return
            _write_error(
                agent_name, msg,
                "I couldn't reach my reasoning service for this request. "
                "Please try again in a moment."
            )
            return

        plan = _parse_json(raw)
        action  = (plan.get("action_type") or "").strip()
        payload = plan.get("action_payload") or {}

        # ── THINKING BOX ─────────────────────────────────────────────
        think, think_error = parse_thinking(
            plan,
            require_checked_attempts=require_checked_attempts,
        )
        if think_error:
            logger.info(f"[{agent_name}] thinking box rejected: {think_error}")
            tail += f"\n\n[SYSTEM] {think_error}"
            continue

        write_thinking_log(agent_name, turn, think, action, payload)
        # ── END THINKING BOX ─────────────────────────────────────────

        # ── CALL_TOOL ────────────────────────────────────────────────
        if action == "CALL_TOOL":
            tool_name = (payload.get("tool_name") or "").strip()
            tool_args = payload.get("tool_args") or {}
            if not tool_name:
                tail += "\n\nCALL_TOOL requires `tool_name`."
                continue

            # ── DELEGATION DEDUP ─────────────────────────────────────
            # Block near-duplicate delegations to the same worker
            # before they go out. Two checks:
            #   A. Worker's most recent messages say "already reported"
            #      / "no new work" → block outright.
            #   B. Near-duplicate of a recent delegation body → block.
            _deleg_blocked = False
            if tool_name == "send_message":
                to_raw   = str((tool_args or {}).get("to", "") or "")
                new_body = str((tool_args or {}).get("body", "") or "")

                if to_raw and new_body:
                    try:
                        target_thread = messenger._resolve_target(to_raw)
                    except Exception:
                        target_thread = to_raw

                    if target_thread.startswith("worker_"):
                        try:
                            worker_thread = inbox.recent(target_thread, limit=30) or []
                        except Exception:
                            worker_thread = []

                        # Check A — worker says done.
                        worker_recent = [
                            m for m in worker_thread
                            if m.get("sender") == target_thread
                        ][-2:]
                        for wm in worker_recent:
                            wbody = (wm.get("body") or "").lower()
                            if ("already reported" in wbody
                                    or "no new work" in wbody
                                    or "already done" in wbody):
                                logger.warning(
                                    f"[{agent_name}] delegation suppressed — "
                                    f"{target_thread} already reported done"
                                )
                                log_tool_event(
                                    agent_name, "delegate_suppressed",
                                    f"worker_says_done to={target_thread}"
                                )
                                tail += (
                                    f"\n\n[SYSTEM] DELEGATION BLOCKED — "
                                    f"{target_thread} already told you the "
                                    f"task is done:\n"
                                    f"  \"{(wm.get('body') or '')[:200]}\"\n\n"
                                    f"Do NOT delegate this task again. The "
                                    f"worker has completed it. If you "
                                    f"haven't already, SEND_REPLY to the "
                                    f"user confirming the task is done. "
                                    f"Then STOP."
                                )
                                _deleg_blocked = True
                                break

                        # Check B — near-duplicate of a recent
                        # delegation body.
                        if not _deleg_blocked:
                            my_prior = [
                                m for m in worker_thread
                                if m.get("sender") == agent_name
                            ][-DELEGATION_LOOKBACK:]

                            for prev in my_prior:
                                if _similar_text(
                                    prev.get("body") or "",
                                    new_body,
                                    threshold=DELEGATION_DUPLICATE_THRESHOLD,
                                ):
                                    logger.warning(
                                        f"[{agent_name}] delegation suppressed — "
                                        f"near-duplicate of msg #{prev.get('id')} "
                                        f"to {target_thread}"
                                    )
                                    log_tool_event(
                                        agent_name, "delegate_suppressed",
                                        f"dup of #{prev.get('id')} to={target_thread}"
                                    )
                                    tail += (
                                        f"\n\n[SYSTEM] DUPLICATE DELEGATION "
                                        f"BLOCKED.\n"
                                        f"You already sent a near-identical "
                                        f"task to {target_thread} "
                                        f"(msg #{prev.get('id')}). The worker "
                                        f"has it.\n\n"
                                        f"Do NOT re-delegate the same task. "
                                        f"Instead:\n"
                                        f"  • If the worker has reported back, "
                                        f"SEND_REPLY to the user confirming "
                                        f"what was done.\n"
                                        f"  • If the worker hasn't reported "
                                        f"yet, SEND_REPLY to the user saying "
                                        f"it's in progress.\n"
                                        f"  • If this is genuinely new work, "
                                        f"rephrase the body so it isn't a "
                                        f"duplicate of msg #{prev.get('id')}."
                                    )
                                    _deleg_blocked = True
                                    break

            if _deleg_blocked:
                continue
            # ── END DELEGATION DEDUP ─────────────────────────────────

            result = _run_tool(agent_name, tool_name, tool_args)
            result_str = str(result)
            is_error = _looks_like_tool_error(result_str)

            rec = ledger.record(
                tool_name=tool_name,
                args=tool_args if isinstance(tool_args, dict) else {},
                ok=not is_error,
            )

            tools_called_this_turn += 1
            log_tool_event(
                agent_name, "tool",
                f"#{rec.call_id} {tool_name}({_short(tool_args)})"
            )

            try:
                sig = f"{tool_name}|{json.dumps(tool_args, sort_keys=True)}"
            except (TypeError, ValueError):
                sig = f"{tool_name}|{tool_args!r}"

            if is_error:
                failure_counts[sig] = failure_counts.get(sig, 0) + 1

                if failure_counts[sig] >= HARD_STOP_FAILURES:
                    logger.warning(
                        f"[{agent_name}] breaking loop — "
                        f"'{tool_name}' failed {failure_counts[sig]}× "
                        f"with the same args"
                    )
                    reason = (
                        f"tool '{tool_name}' kept failing with the same "
                        f"arguments. Last error: {result_str[:200]}"
                    )
                    if _grace_turn(agent_name, msg, prompt, tail, reason):
                        return
                    _write_error(
                        agent_name, msg,
                        "I hit a wall on this one — I kept trying the same "
                        "approach and it kept failing. Let me know if you'd "
                        "like me to try something different."
                    )
                    return

                if failure_counts[sig] >= MAX_IDENTICAL_FAILURES:
                    tail += (
                        f"\n\n--- TOOL RESULT #{rec.call_id} ({tool_name}) ---\n"
                        f"{result_str[:4000]}\n"
                        f"\n[STOP] This exact call has failed "
                        f"{failure_counts[sig]} times. Do NOT retry it. "
                        f"Either call a DIFFERENT tool, fix the arguments, "
                        f"or SEND_REPLY / ASK_CEO explaining the problem."
                    )
                    continue
            else:
                failure_counts.pop(sig, None)

            tail += (
                f"\n\n--- TOOL RESULT #{rec.call_id} ({tool_name}) ---\n"
                f"{result_str[:4000]}\n"
            )
            continue

        # ── SEND_REPLY ───────────────────────────────────────────────
        if action == "SEND_REPLY":
            reply_body  = (payload.get("body") or "").strip()
            attachments = payload.get("attachments") or []
            if not reply_body:
                tail += "\n\nSEND_REPLY needs a non-empty `body`."
                continue

            target = inbox.reply_target(agent_name, msg) or sender

            # Thinking-vs-reply consistency (soft, fires once)
            if not thinking_consistency_warned:
                consistency_warning = check_reply_consistency(
                    think, reply_body
                )
                if consistency_warning:
                    thinking_consistency_warned = True
                    logger.info(
                        f"[{agent_name}] reply consistency warning issued"
                    )
                    tail += f"\n\n{consistency_warning}"
                    continue

            # Reply contract (CEO → user only)
            if cfg["is_ceo"] and target.startswith("user_"):
                raw_claims   = payload.get("claimed_actions") or []
                raw_evidence = payload.get("evidence_ids") or []

                if not isinstance(raw_claims, list):
                    raw_claims = [raw_claims]
                if not isinstance(raw_evidence, list):
                    raw_evidence = [raw_evidence]

                try:
                    claims = [ClaimedAction(c) for c in raw_claims]
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"[{agent_name}] invalid claimed_actions: {e}"
                    )
                    tail += (
                        f"\n\n[SYSTEM] Invalid claimed_actions: {e}\n"
                        f"Valid values: "
                        f"{[c.value for c in ClaimedAction]}\n\n"
                        f"Actual calls this turn:\n{ledger.summary()}"
                    )
                    continue

                ok, reason = validate_reply(
                    claims, list(raw_evidence), ledger
                )
                if not ok:
                    logger.warning(
                        f"[{agent_name}] reply rejected: {reason}"
                    )
                    tail += (
                        f"\n\n[SYSTEM] SEND_REPLY REJECTED.\n"
                        f"{reason}\n\n"
                        f"Rewrite the reply so claimed_actions and "
                        f"evidence_ids match reality:\n"
                        f"  • If you delegated, claimed_actions=['delegate'].\n"
                        f"  • If you haven't started, "
                        f"claimed_actions=['incomplete'].\n"
                        f"  • If you read the log, claimed_actions=['read'] "
                        f"with the read call's ID in evidence_ids.\n"
                        f"  • Do NOT claim work you did not do."
                    )
                    continue

            inbox.send(
                thread=target,
                sender=agent_name,
                body=reply_body,
                attachments=attachments,
                parent_id=msg_id,
            )
            log_tool_event(agent_name, "reply", f"to={target}")
            logger.info(f"[{agent_name}] -> {target}: {reply_body[:120]}")
            return

        # ── ASK_CEO ──────────────────────────────────────────────────
        if action == "ASK_CEO":
            if cfg["is_ceo"]:
                tail += (
                    "\n\nASK_CEO is not available to the CEO. "
                    "Use ASK_USER to ask the user, or SEND_REPLY to answer."
                )
                continue

            question = (payload.get("question") or "").strip()
            if not question:
                tail += "\n\nASK_CEO needs a non-empty `question`."
                continue

            inbox.send(
                thread="ceo",
                sender=agent_name,
                body=f"[QUESTION] {question}",
                parent_id=msg_id,
            )
            log_tool_event(agent_name, "ask_ceo", question[:120])
            logger.info(f"[{agent_name}] ASK_CEO: {question[:120]}")
            return

        # ── ASK_USER ─────────────────────────────────────────────────
        if action == "ASK_USER":
            if not cfg["is_ceo"]:
                tail += (
                    "\n\nASK_USER is not available to workers. "
                    "Use ASK_CEO to ask the CEO."
                )
                continue

            question = (payload.get("question") or "").strip()
            if not question:
                tail += "\n\nASK_USER needs a non-empty `question`."
                continue

            target = inbox.reply_target(agent_name, msg) or sender
            inbox.send(
                thread=target,
                sender=agent_name,
                body=question,
                parent_id=msg_id,
            )
            log_tool_event(agent_name, "ask_user", f"to={target}")
            logger.info(f"[{agent_name}] ASK_USER -> {target}: {question[:120]}")
            return

        # ── FINISH ───────────────────────────────────────────────────
        if action == "FINISH":
            logger.info(f"[{agent_name}] finished without reply (turn {turn})")
            log_tool_event(agent_name, "finish", f"turn={turn}")
            return

        # ── Unknown ──────────────────────────────────────────────────
        tail += (
            f"\n\nUnrecognised action_type '{action}'. "
            f"Use ONE of: CALL_TOOL, SEND_REPLY, ASK_CEO, ASK_USER, FINISH."
        )

    logger.warning(f"[{agent_name}] hit MAX_TURNS without finishing")

    if _grace_turn(agent_name, msg, prompt, tail,
                   "ran out of turns before finishing"):
        return

    _write_error(
        agent_name, msg,
        "I wasn't able to finish this one. Please try again, or rephrase "
        "the request into something more specific."
    )


# ── Error detection ──────────────────────────────────────────────────
def _looks_like_tool_error(result_str: str) -> bool:
    """Return True if the tool result looks like a failure."""
    if not result_str:
        return False

    head = result_str[:300]

    return (
        "arguments validation failed" in result_str
        or "validation error for" in result_str
        or "Field required" in result_str
        or "is not permitted for this agent" in result_str
        or result_str.startswith("Unknown tool:")
        or (result_str.startswith("Tool '") and "raised" in head)
        or (result_str.startswith("Tool '") and "is not callable" in head)
    )


# ── Tool execution ───────────────────────────────────────────────────
def _run_tool(agent_name: str, tool_name: str, tool_args: dict) -> Any:
    """
    Run a tool by name for `agent_name`. The only gate is delegation:
    `send_message` is CEO-only.

    The turn ledger is NOT updated here — the caller records after
    computing `is_error`, so `ok` reflects validation failures, not
    just exceptions.
    """
    cfg = agents.get(agent_name) or {}
    key = (tool_name or "").lower().replace(" ", "_")

    # Remap renamed args so an LLM that learned the old name from
    # its own tool history still works.
    if isinstance(tool_args, dict):
        aliases = _TOOL_ARG_ALIASES.get(key)
        if aliases:
            tool_args = {aliases.get(k, k): v for k, v in tool_args.items()}

    tool = agents.TOOL_REGISTRY.get(key)
    if tool is None:
        return f"Unknown tool: {tool_name}"

    if key == "send_message" and not cfg.get("can_delegate"):
        return (
            "Tool 'send_message' is not permitted for this agent. "
            "Workers do the work directly; only the CEO delegates."
        )

    if isinstance(tool_args, dict):
        clean_args = {k: v for k, v in tool_args.items() if v is not None}
    else:
        clean_args = tool_args

    try:
        if hasattr(tool, "run"):
            return tool.run(**clean_args)
        if hasattr(tool, "func"):
            return tool.func(**clean_args)
        if callable(tool):
            return tool(**clean_args)
        return f"Tool '{tool_name}' is not callable."
    except Exception as e:
        logger.exception(f"[{agent_name}] tool '{tool_name}' raised")
        return f"Tool '{tool_name}' raised: {e}"


# ── Helpers ──────────────────────────────────────────────────────────
def _parse_json(text: str) -> dict:
    if not text:
        return {}
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip("`").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {}


def _short(obj: Any, n: int = 80) -> str:
    s = str(obj).replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _write_error(agent_name: str, msg: dict, text: str) -> None:
    target = inbox.reply_target(agent_name, msg) or msg.get("sender", "ceo")
    if target.startswith("user_"):
        body = text
    else:
        body = f"[ERROR from {agent_name}] {text}"

    inbox.send(
        thread=target,
        sender=agent_name,
        body=body,
        parent_id=msg.get("id"),
    )
    log_tool_event(agent_name, "error", text[:120])
