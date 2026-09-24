# tools/list_tools.py
"""
List Empire Tools — returns the catalog of every tool available to the
caller, with short descriptions and an [MCP] tag on tools that come
from a Model Context Protocol server.

Reads from orchestration.agents.TOOL_REGISTRY — the single live registry
that gm.py populates at boot. The old role_tools/gm dual-read is gone.
"""
from crewai.tools import tool


# ── MCP detection ────────────────────────────────────────────────────
def _is_mcp(name: str) -> bool:
    """True if the given tool name came from an MCP server."""
    try:
        from orchestration.mcp_manager import is_mcp_tool
        return is_mcp_tool(name)
    except Exception:
        return False


# ── Registry access ──────────────────────────────────────────────────
def _live_registry() -> dict:
    """Read the single live registry that gm.py populates."""
    try:
        from orchestration import agents
        return agents.TOOL_REGISTRY
    except Exception:
        return {}


@tool("List Empire Tools")
def list_empire_tools():
    """
    Return the complete catalog of tools available to you, with short
    descriptions. Tools marked [MCP] are served by a live Model Context
    Protocol server (e.g. GitHub MCP) running alongside you.

    Call this when the user asks "what tools do you have?", "list your
    tools", or "do we have MCP?".

    After seeing a name here, call describe_tool(name) for full
    documentation: arguments, examples, and usage notes.
    """
    registry = _live_registry()

    if not registry:
        return "No tools available."

    lines = [
        f"Available Tools ({len(registry)} total):",
        "──────────────────────────────────────",
    ]
    mcp_count = 0
    for name in sorted(registry.keys()):
        tool_obj = registry[name]
        desc = getattr(tool_obj, "description", "") or "No description"
        short_desc = desc.split("\n")[0].strip()
        if len(short_desc) > 100:
            short_desc = short_desc[:97] + "..."

        tag = ""
        if _is_mcp(name):
            tag = "  [MCP]"
            mcp_count += 1

        lines.append(f"• {name}: {short_desc}{tag}")

    if mcp_count:
        lines.append("")
        lines.append(
            f"[MCP] = provided by a live Model Context Protocol server "
            f"({mcp_count} tools)."
        )

    return "\n".join(lines)
