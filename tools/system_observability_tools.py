# tools/system_observability_tools.py
"""
System awareness tools for the CEO.

The old version of this file read live state from TaskManager (heartbeats,
ActiveTask objects, scheduler queue). None of that exists in the new
architecture. The new sources of truth are:

    orchestration/agents.py         — the agent registry + tool registry
    agents/<name>/logs/tools.log    — per-agent tool trail
    ai_civilization/inbox.db        — every message the system has seen

Three tools give the CEO a 360 view of runtime state:

    system_status()             — composite: agents + inbox + errors
    list_agents()               — roster with last-activity per agent
    think(thought)              — record reasoning into the tool log

Note: `read_inbox` is NOT here. That name belongs to tools/inbox_tools.py,
which is the CEO's tool for reading user-facing inbox threads. Two
modules exporting the same name would clobber each other in the
EmpireTools registry.

`set_observability_context()` is kept as a no-op so any old caller still
importing it doesn't crash. The new system wires nothing through it —
the tools read from the sources above directly.
"""
import os
import sqlite3
from datetime import datetime
from typing import Optional

from crewai.tools import tool


# ══════════════════════════════════════════════════════════════════════
# Legacy context hook (kept as a no-op for backwards compatibility)
# ══════════════════════════════════════════════════════════════════════
_inbox_db_path: Optional[str] = None
_log_path: Optional[str] = None


def set_observability_context(
    task_manager=None,
    log_path: Optional[str] = None,
    inbox_db_path: Optional[str] = None,
    scheduler_db_path: Optional[str] = None,
    **_ignored,
) -> None:
    """No-op. The new system doesn't need external wiring."""
    global _inbox_db_path, _log_path
    if inbox_db_path:
        _inbox_db_path = inbox_db_path
    if log_path:
        _log_path = log_path


def _inbox_path() -> str:
    return _inbox_db_path or os.path.join(
        os.getcwd(), "ai_civilization", "inbox.db"
    )


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════
def _safe_sqlite(db_path: str, query: str, params: tuple = ()) -> list:
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(query, params).fetchall()
        con.close()
        return rows
    except Exception:
        return []


def _agent_registry() -> dict:
    """Lazy import of the live registry."""
    try:
        from orchestration import agents as _agents
        return _agents.AGENTS
    except Exception:
        return {}


def _tool_count() -> int:
    """How many tools are in the shared registry."""
    try:
        from orchestration import agents as _agents
        return len(_agents.TOOL_REGISTRY)
    except Exception:
        return 0


def _logs_dir(agent_name: str) -> str:
    try:
        from orchestration import agents as _agents
        return _agents.logs_dir(agent_name)
    except Exception:
        return os.path.join(
            os.getcwd(), "ai_civilization", "agents", agent_name, "logs"
        )


def _last_activity(agent_name: str) -> tuple:
    """
    Return (human_str, seconds_ago or None) for the agent's tools.log.
    """
    path = os.path.join(_logs_dir(agent_name), "tools.log")
    if not os.path.exists(path):
        return ("never", None)
    try:
        mtime = os.path.getmtime(path)
        delta = datetime.now().timestamp() - mtime
        if delta < 60:
            return (f"{int(delta)}s ago", int(delta))
        if delta < 3600:
            return (f"{int(delta/60)}m ago", int(delta))
        return (f"{int(delta/3600)}h ago", int(delta))
    except Exception:
        return ("?", None)


def _recent_errors(limit: int = 5) -> list:
    """Scan every agent's tools.log for lines with kind='error'."""
    errors = []
    for name in _agent_registry():
        path = os.path.join(_logs_dir(name), "tools.log")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f.readlines()[-200:]:
                    if "  error     " in line:
                        errors.append((line.rstrip(), name))
        except Exception:
            pass
    return errors[-limit:]


# ══════════════════════════════════════════════════════════════════════
# TOOL 1 — composite system status
# ══════════════════════════════════════════════════════════════════════
@tool("System Status")
def system_status() -> str:
    """
    One-shot overview of the whole system: registered agents, shared
    tool count, recent inbox traffic, recent errors.

    Use this FIRST when the user asks anything about the state of the
    system itself:
      • "what are you working on?"
      • "is anything running?"
      • "what happened recently?"
      • "is the system okay?"

    For deeper digging use read_inbox(), read_agent_log(), or
    execute_terminal directly.
    """
    parts = []
    parts.append("═══ SYSTEM STATUS ═══")
    parts.append(f"Time:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        from orchestration import agents as _agents
        parts.append(f"Workspace: {_agents.WORKSPACE_DIR}")
    except Exception:
        parts.append(f"Workspace: {os.getcwd()}")
    parts.append(f"Tools:     {_tool_count()} registered (shared by all agents)")
    parts.append("")

    # ── Agents ──
    registry = _agent_registry()
    parts.append("── AGENTS ──")
    if not registry:
        parts.append("  (no agents registered)")
    else:
        for name, cfg in sorted(registry.items()):
            role = cfg.get("role", "?")
            tag = " (CEO)" if cfg.get("is_ceo") else ""
            last, secs = _last_activity(name)
            active = " *active*" if (secs is not None and secs < 60) else ""
            parts.append(f"  {name}{tag} — {role}")
            parts.append(f"    last: {last}{active}")
    parts.append("")

    # ── Inbox: last 10 rows across all threads ──
    parts.append("── INBOX (last 10 rows) ──")
    rows = _safe_sqlite(
        _inbox_path(),
        "SELECT id, thread_id, sender, substr(body, 1, 90) "
        "FROM inbox ORDER BY id DESC LIMIT 10",
    )
    if not rows:
        parts.append("  (inbox empty or unreadable)")
    else:
        for row_id, thread, sender, body in rows:
            body = (body or "").replace("\n", " ")
            parts.append(f"  #{row_id:>4}  [{thread}]  {sender}:  {body}")
    parts.append("")

    # ── Recent errors ──
    parts.append("── RECENT ERRORS ──")
    errs = _recent_errors(limit=5)
    if not errs:
        parts.append("  (none)")
    else:
        for line, name in errs:
            parts.append(f"  [{name}]  {line}")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════
# TOOL 2 — detailed agent roster
# ══════════════════════════════════════════════════════════════════════
@tool("List Agents")
def list_agents() -> str:
    """
    Full roster: every registered agent with role and last activity.
    Use before delegating to see who's available, or when the user asks
    "what agents do you have?".

    Every agent shares the same tool set, so the roster does not list
    individual tools — the count is the same across the board.
    """
    registry = _agent_registry()
    if not registry:
        return "(no agents registered)"

    shared_tools = _tool_count()

    lines = ["═══ AGENT ROSTER ═══"]
    lines.append(f"Shared toolset: {shared_tools} tools available to every agent")
    for name, cfg in sorted(registry.items()):
        role = cfg.get("role", "?")
        tag = " (CEO)" if cfg.get("is_ceo") else ""
        last, secs = _last_activity(name)
        active = " *active*" if (secs is not None and secs < 60) else ""

        lines.append("")
        lines.append(f"  {name}{tag}")
        lines.append(f"    role:  {role}")
        lines.append(f"    last:  {last}{active}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# TOOL 3 — explicit reasoning step
# ══════════════════════════════════════════════════════════════════════
@tool("Think")
def think(thought: str) -> str:
    """
    Record an explicit reasoning step. The thought lands in your tool
    log so it's auditable — no other side effects.

    Use for multi-step decisions: lay out the plan before acting. For
    example, before a complex delegation:

        think("User wants X. This needs a worker because Y. I'll send
               a message to Python Dev with instructions to build Z and
               verify with `python z.py`.")

    Then act. The reasoning and the action both end up in the log, in
    order, so a later audit can see what you were thinking.
    """
    if not thought or not thought.strip():
        return "think: nothing to record."

    # Find which agent is calling (set by agent_loop via empire_tools).
    agent_name = None
    try:
        import empire_tools
        agent_name = empire_tools.get_log_context()
    except Exception:
        pass

    if not agent_name:
        return "think: no active agent context; reasoning not recorded."

    try:
        path = os.path.join(_logs_dir(agent_name), "tools.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        one_line = thought.replace("\n", " ").strip()[:400]
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts}  think       {one_line}\n")
    except Exception:
        pass

    return f"Reasoning recorded ({len(thought)} chars)."


# ══════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════
# Note: `read_inbox` is NOT exported from here. The tools/inbox_tools.py
# module owns that name — it's the CEO's tool for reading user-facing
# inbox threads. Two modules exporting the same name would collide in
# the EmpireTools registry.
__all__ = [
    "system_status",
    "list_agents",
    "think",
    "set_observability_context",
]
