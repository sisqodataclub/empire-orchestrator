# tools/dynamic_tools_tool.py
#
# Agent-facing tools for creating, inspecting, and activating new tools
# at runtime.
#
# Lifecycle:
#   propose_tool         — writes a new tool file to staging/
#   list_pending_tools   — shows what's staged
#   read_pending_tool    — shows the code of a staged tool
#   activate_tool        — promotes staging → active, registers live
#   deactivate_tool      — removes from registry and archives
#   list_dynamic_tools   — lists active dynamic tools
#   reject_pending_tool  — declines a staged tool
#
# The agent must obtain explicit user approval before calling
# activate_tool. That's enforced by the CEO prompt, not by code.
from crewai.tools import tool

from orchestration import dynamic_tools as dt


def _log(tool_name: str, detail: str) -> None:
    try:
        from empire_tools import log_agent_action
        log_agent_action(tool_name, detail)
    except Exception:
        pass


@tool("Propose Tool")
def propose_tool(name: str, source: str):
    """
    Write a new tool to the staging area. Does NOT activate it.

    Args:
      name:   Lowercase identifier for the tool file (e.g. 'check_quote').
              Must be unique. 2-41 chars, letters/digits/underscores.
      source: The full Python source of the tool file. Must define at
              least one function decorated with @tool("Display Name").

    Written to:
      ai_civilization/dynamic_tools/staging/<name>.py

    After proposing, ask the user for approval via ASK_USER before
    calling activate_tool.

    Example source:

        from crewai.tools import tool

        @tool("Check Quote")
        def check_quote(quote_id: str):
            \"\"\"Looks up a quote ID in the booking database.\"\"\"
            import sqlite3
            con = sqlite3.connect('/app/data/workspaces/.../bookings.db')
            ...
            return result
    """
    _log("Propose Tool", name)
    result = dt.write_staged(name, source)
    if result.startswith("✅"):
        return (
            f"{result}\n\n"
            f"Next: call list_pending_tools() to confirm, then use "
            f"ASK_USER to get approval before activate_tool()."
        )
    return result


@tool("List Pending Tools")
def list_pending_tools():
    """
    List every tool currently in the staging area (proposed by you,
    not yet approved by the user).
    """
    _log("List Pending Tools", "")
    staged = dt.list_staged()
    if not staged:
        return "📭 No tools staged."
    lines = [f"📦 {len(staged)} pending tool(s):"]
    for s in staged:
        if "error" in s:
            lines.append(f"  • {s['file']}  ⚠️ {s['error']}")
        else:
            lines.append(
                f'  • {s["name"]}  → @tool("{s["tool_name"]}")  '
                f'({s["size"]:,} B, {s["mtime"]})'
            )
            if s["doc"]:
                lines.append(f"      {s['doc']}")
    return "\n".join(lines)


@tool("Read Pending Tool")
def read_pending_tool(name: str):
    """
    Show the full source of a staged tool.
    Args:
      name: the staging filename stem (e.g. 'check_quote').
    """
    _log("Read Pending Tool", name)
    src = dt.read_staged(name)
    if src is None:
        return f"❌ No staged tool named '{name}'. Use list_pending_tools()."
    return f"📄 staging/{name}.py\n{'='*50}\n{src}"


@tool("Activate Tool")
def activate_tool(name: str):
    """
    Promote a staged tool to active and register it live. No restart
    required. Every agent can immediately call the new tool.

    ONLY call this AFTER the user has explicitly approved the tool
    via ASK_USER.

    Args:
      name: the staging filename stem.
    """
    _log("Activate Tool", name)
    ok, msg = dt.activate(name)
    return msg


@tool("Deactivate Tool")
def deactivate_tool(name: str, reason: str = ""):
    """
    Remove a tool from the registry and archive its file. Use when a
    dynamic tool misbehaves or is no longer needed.

    Args:
      name:   the active filename stem.
      reason: one-line note for the audit trail.
    """
    _log("Deactivate Tool", f"{name} ({reason})")
    ok, msg = dt.deactivate(name, reason=reason)
    return msg


@tool("List Dynamic Tools")
def list_dynamic_tools():
    """
    List every active dynamic tool. These are the ones that were
    proposed by the agent, approved, and are now callable by every
    agent just like a built-in tool.
    """
    _log("List Dynamic Tools", "")
    active = dt.list_active()
    if not active:
        return "📭 No dynamic tools are active."
    lines = [f"🔧 {len(active)} active dynamic tool(s):"]
    for a in active:
        lines.append(
            f"  • {a['name']}  ({a['size']:,} B, activated {a['mtime']})"
        )
    return "\n".join(lines)


@tool("Reject Pending Tool")
def reject_pending_tool(name: str, reason: str = ""):
    """
    Decline a staged tool. The file is moved to rejected/ with the
    reason appended.

    Args:
      name:   the staging filename stem.
      reason: one-line explanation.
    """
    _log("Reject Pending Tool", f"{name} ({reason})")
    ok, msg = dt.reject(name, reason=reason)
    return msg
