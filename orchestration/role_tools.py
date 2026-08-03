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

ROLE_TOOLS = {
    "quality assurance engineer": {"file_manager", "execute_terminal", "ast_inspector", "commit_to_library"},
    "seo specialist":              {"web_search", "web_fetch", "file_manager", "ast_inspector"},
    "web developer":               {"file_manager", "execute_terminal", "ast_inspector"},
    "data analyst":                {"file_manager", "execute_terminal", "ast_inspector", "python_repl"},
    "research specialist":         {"web_search", "web_fetch", "file_manager", "ast_inspector"},
}

def get_tools_for_role(role: str) -> list:
    role_lower = role.lower()
    allowed = None
    if role_lower in ROLE_TOOLS:
        allowed = ROLE_TOOLS[role_lower]
    else:
        for known_role, tools in ROLE_TOOLS.items():
            if known_role in role_lower:
                allowed = tools
                break
    if not allowed:
        allowed = {"file_manager", "ast_inspector"}
    tools = []
    for name in allowed:
        tool = TOOL_REGISTRY.get(name)
        if tool:
            tools.append(tool)
    return tools
