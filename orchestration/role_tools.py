# orchestration/role_tools.py
from empire_tools import EmpireTools

def _build_tool_registry():
    all_tools = EmpireTools()
    registry = {}
    for method_name in dir(all_tools):
        if method_name.startswith("_"):
            continue
        obj = getattr(all_tools, method_name)
        if hasattr(obj, 'name') and hasattr(obj, 'description'):
            key = getattr(obj, 'name', method_name).lower().replace(' ', '_')
            registry[key] = obj
            registry[method_name.lower()] = obj
    return registry

TOOL_REGISTRY = _build_tool_registry()

def get_tools_for_role(role: str) -> list:
    """
    Every agent gets file_manager and ast_inspector as a minimum.
    Additional role‑specific tools can be added here later, but never remove the basics.
    """
    base_tools = {"file_manager", "ast_inspector"}

    tools = []
    for name in base_tools:
        tool = TOOL_REGISTRY.get(name)
        if tool:
            tools.append(tool)
    return tools
