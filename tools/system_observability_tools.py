# tools/system_observability_tools.py
"""
Minimal system observability tools for the CEO.

Design principle: the CEO already has EXECUTE_REPL and EXECUTE_TERMINAL,
so he can read any SQLite DB and grep any log file himself. The only state
that SQL/shell CANNOT reach is *in-memory Python state* — heartbeats,
live task objects, running threads.

This module exposes exactly three tools for that in-memory layer:

    system_status()          → composite: tasks + inbox + scheduler + recent errors
    inspect_task(task_id)    → deep dive on one running mission
    cancel_task(task_id)     → mutate a live task object to stop its loop

Everything else — inbox queries, scheduler queries, log greps — the CEO
does via EXECUTE_REPL / EXECUTE_TERMINAL. The ceo_prompter teaches him the
schema so he knows what to query.
"""
import os
import json
import sqlite3
import subprocess
from typing import Optional

from crewai.tools import tool


# ══════════════════════════════════════════════════════════════════════
# Module-level context — injected by gm.py / org_bot_worker.py after init
# ══════════════════════════════════════════════════════════════════════
_task_manager = None
_log_path: Optional[str] = None
_inbox_db_path: Optional[str] = None
_scheduler_db_path: Optional[str] = None


def set_observability_context(
    task_manager=None,
    log_path: Optional[str] = None,
    inbox_db_path: Optional[str] = None,
    scheduler_db_path: Optional[str] = None,
    **_ignored,
) -> None:
    """
    Wire the live singletons that the in-memory tools need.

    Only `task_manager` is required. The path arguments default to the
    standard locations under ./ai_civilization/ and ./logs/.
    """
    global _task_manager, _log_path, _inbox_db_path, _scheduler_db_path
    _task_manager = task_manager
    _log_path = log_path
    _inbox_db_path = inbox_db_path
    _scheduler_db_path = scheduler_db_path


def _inbox_path() -> str:
    return _inbox_db_path or os.path.join(
        os.getcwd(), "ai_civilization", "inbox.db"
    )


def _sched_path() -> str:
    return _scheduler_db_path or os.path.join(
        os.getcwd(), "ai_civilization", "scheduler.db"
    )


def _log_file() -> str:
    return _log_path or os.path.join(os.getcwd(), "logs", "empire.log")


def _safe_sqlite(db_path: str, query: str, params: tuple = ()) -> list:
    """Read-only SQLite helper. Returns [] on any failure."""
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(query, params).fetchall()
        con.close()
        return rows
    except Exception:
        return []


def _safe_shell(cmd: str) -> str:
    """Run a shell command, return stdout (or error string) truncated."""
    try:
        out = subprocess.getoutput(cmd)
        return out[:4000] if out else ""
    except Exception as e:
        return f"(shell error: {e})"


# ══════════════════════════════════════════════════════════════════════
# TOOL 1 — Composite status snapshot
# ══════════════════════════════════════════════════════════════════════

@tool("System Status")
def system_status() -> str:
    """
    One-shot composite overview of the entire ecosystem:
    active missions, inbox backlog, scheduler queue, recent log warnings.

    Use this FIRST whenever the user asks:
      • "what's happening?"
      • "any updates?"
      • "is anything stuck?"
      • "is the system okay?"

    For deeper queries (specific messages, custom SQL, log greps), follow
    up with EXECUTE_REPL / EXECUTE_TERMINAL — you have direct DB and log
    access.
    """
    parts = []

    # ── Missions (from in-memory TaskManager) ──
    parts.append("═══ ACTIVE MISSIONS ═══")
    if _task_manager is None:
        parts.append("  (TaskManager not wired — call set_observability_context)")
    else:
        snapshot = _task_manager.health_snapshot()
        if not snapshot:
            parts.append("  No tasks registered.")
        else:
            running = [t for t in snapshot if t["status"] == "RUNNING"]
            stuck   = [t for t in snapshot if t["is_stuck"]]
            parts.append(
                f"  Running: {len(running)}   Stuck: {len(stuck)}   "
                f"Tracked total: {len(snapshot)}"
            )
            # Show all running tasks (up to 10)
            for t in running[:10]:
                flag = "  ⚠️ STUCK" if t["is_stuck"] else ""
                parts.append(
                    f"    #{t['id']:<3} age={t['seconds_since_heartbeat']:>4}s "
                    f"runtime={t['runtime_sec']:>4}s  "
                    f"step='{t['step'][:45]}'{flag}"
                )
            # If any stuck, show them even if not in running[:10]
            for t in stuck:
                if t not in running[:10]:
                    parts.append(
                        f"    #{t['id']:<3} [STUCK] age={t['seconds_since_heartbeat']}s "
                        f"step='{t['step'][:45]}'"
                    )

    # ── Inbox (per-thread summary) ──
    parts.append("\n═══ INBOX (per-thread) ═══")
    rows = _safe_sqlite(
        _inbox_path(),
        "SELECT thread_id, direction, status, COUNT(*) "
        "FROM inbox GROUP BY thread_id, direction, status "
        "ORDER BY thread_id",
    )
    if not rows:
        parts.append("  Empty or unreadable.")
    else:
        by_thread: dict = {}
        for thread, direction, status, count in rows:
            by_thread.setdefault(thread, []).append((direction, status, count))
        for thread, entries in by_thread.items():
            summary = ", ".join(f"{d}/{s}={c}" for d, s, c in entries)
            parts.append(f"  {thread}: {summary}")
            pending = sum(c for d, s, c in entries if s == "PENDING_DELIVERY")
            if pending > 5:
                parts.append(
                    f"    ⚠️ {pending} undelivered — poller may be behind"
                )

    # ── Scheduler ──
    parts.append("\n═══ SCHEDULER ═══")
    rows = _safe_sqlite(
        _sched_path(),
        "SELECT status, COUNT(*) FROM scheduled_tasks GROUP BY status",
    )
    if not rows:
        parts.append("  No scheduled tasks.")
    else:
        for status, count in rows:
            parts.append(f"  {status}: {count}")

    # ── Recent warnings in the log ──
    parts.append("\n═══ RECENT WARNINGS ═══")
    log_path = _log_file()
    if not os.path.exists(log_path):
        parts.append(f"  (log not found at {log_path})")
    else:
        warnings = _safe_shell(
            f"tail -200 {log_path} | grep -iE 'error|stuck|failed|timeout' | tail -8"
        )
        parts.append(f"  {warnings}" if warnings.strip() else "  None in last 200 lines.")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════
# TOOL 2 — Inspect a single mission (live in-memory state)
# ══════════════════════════════════════════════════════════════════════

@tool("Inspect Task")
def inspect_task(task_id: str) -> str:
    """
    Deep dive on a single running mission. Returns:
      • status, complete flag, mission text
      • live health: current step, last heartbeat, runtime
      • last 5 turns from its conversation history

    Use when the user asks:
      • "what is task #N doing?"
      • "why is task #N stuck?"
      • "show me the last steps of #N"

    For the *conversation* the mission is part of, use EXECUTE_REPL to
    query inbox.db directly.
    """
    if _task_manager is None:
        return "TaskManager not wired."
    task = _task_manager.get_task(str(task_id))
    if not task:
        return f"Task #{task_id} not found."

    # Pull live health entry
    health = None
    for h in _task_manager.health_snapshot():
        if str(h["id"]) == str(task_id):
            health = h
            break

    parts = [f"═══ TASK #{task_id} ═══"]
    parts.append(f"  status:       {task.status}")
    parts.append(f"  complete:     {task.is_complete}")
    parts.append(f"  mission:      {task.mission[:200]}")

    if health:
        parts.append(f"  thread_id:    {health['thread_id']}")
        parts.append(f"  current step: {health['step']}")
        parts.append(
            f"  last beat:    {health['seconds_since_heartbeat']}s ago"
            + ("  ⚠️ STUCK" if health["is_stuck"] else "")
        )
        parts.append(f"  runtime:      {health['runtime_sec']}s")

    history = getattr(task, "conversation_history", [])
    if history:
        parts.append("\n  Recent turns (last 5):")
        for entry in history[-5:]:
            step = entry.get("step", "?")
            result = str(entry.get("result", ""))[:140].replace("\n", " ")
            parts.append(f"    [{step}] {result}")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════
# TOOL 3 — Cancel a live mission (mutates in-memory state)
# ══════════════════════════════════════════════════════════════════════

@tool("Cancel Task")
def cancel_task(task_id: str, reason: str = "") -> str:
    """
    Force-terminate a running mission. Sets its status to INTERRUPTED,
    marks it complete, and unregisters it from the health monitor.

    Use when:
      • the user explicitly says "stop that task" / "kill #N"
      • Diagnose has identified a stuck task and the user authorises it

    This mutates a *live Python object* — a plain SQL UPDATE would not
    stop the loop. That's why this tool exists.
    """
    if _task_manager is None:
        return "TaskManager not wired."
    task = _task_manager.get_task(str(task_id))
    if not task:
        return f"Task #{task_id} not found."
    try:
        task.status = "INTERRUPTED"
        task.is_complete = True
        task.save_history_to_disk()
        _task_manager.unregister_task(str(task_id))
        return (
            f"✅ Task #{task_id} cancelled."
            + (f"  Reason: {reason}" if reason else "")
        )
    except Exception as e:
        return f"Cancel failed: {e}"


# ══════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════
__all__ = [
    "system_status",
    "inspect_task",
    "cancel_task",
    "set_observability_context",
]
