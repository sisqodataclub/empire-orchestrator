################################################################


#
from crewai.tools import tool

@tool("List Empire Tools")
def list_empire_tools():
    """
    Return a list of all tools available to the CEO, with short descriptions.
    This tool is used when the user asks something like "list your tools" or "what tools do you have".
    """
    try:
        # Try to import TOOL_REGISTRY from role_tools and gm (if available)
        from orchestration.role_tools import TOOL_REGISTRY as role_registry
    except:
        role_registry = {}

    try:
        import gm
        gm_registry = gm.TOOL_REGISTRY
    except:
        gm_registry = {}

    # Combine both registries
    all_tools = {}
    all_tools.update(role_registry)
    all_tools.update(gm_registry)

    if not all_tools:
        return "No tools available."

    lines = ["Available Empire Tools:", "─────────────────────────"]
    for name in sorted(all_tools.keys()):
        tool = all_tools[name]
        desc = getattr(tool, 'description', '') or 'No description'
        # Take first line or first 100 chars for brevity
        short_desc = desc.split('\n')[0].strip()
        if len(short_desc) > 100:
            short_desc = short_desc[:97] + "..."
        lines.append(f"• {name}: {short_desc}")

    return "\n".join(lines)



























#########################################################
