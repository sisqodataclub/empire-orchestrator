# orchestration/dynamic_tools.py
#
# Loader and lifecycle manager for agent-authored tools.
#
# Directory layout, all under <workspace>/ai_civilization/dynamic_tools/:
#
#   staging/    — agent-written, not loaded. Pending user approval.
#   active/     — approved, loaded into agents.TOOL_REGISTRY at boot
#                 and on activation. Callable by every agent.
#   rejected/   — explicitly declined. Kept for audit.
#   archived/   — deactivated. Not loaded.
#
# A valid tool file must:
#   • Parse as Python (AST).
#   • Contain at least one function decorated with @tool("...").
#   • Have a name distinct from every existing registry key.
#
# The loader does not enforce that the agent obtained user approval
# before activating. That's a prompt-level rule.
import ast
import importlib.util
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _root() -> Path:
    return Path("ai_civilization") / "dynamic_tools"


def _dir(kind: str) -> Path:
    d = _root() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate_name(name: str) -> Optional[str]:
    """Return None if valid, or an error message if not."""
    if not name:
        return "name is required"
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        return (
            "name must be lowercase, start with a letter, contain only "
            "letters/digits/underscores, and be 2-41 chars long"
        )
    return None


def _validate_source(source: str) -> Optional[str]:
    """Return None if the source parses and contains a @tool, else an error."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"SyntaxError on line {e.lineno}: {e.msg}"

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and getattr(dec.func, "id", "") == "tool"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                found.append(dec.args[0].value)
    if not found:
        return (
            'no @tool("Name") decorated function found — the file must '
            "define at least one tool"
        )
    return None


def _registry_keys() -> set:
    """Every tool key currently registered."""
    from orchestration import agents
    return set(agents.TOOL_REGISTRY.keys())


def write_staged(name: str, source: str) -> str:
    """Write a new tool file to staging/. Returns a status string."""
    err = _validate_name(name)
    if err:
        return f"❌ {err}"

    err = _validate_source(source)
    if err:
        return f"❌ {err}"

    path = _dir("staging") / f"{name}.py"
    if path.exists():
        return f"⚠️ staging/{name}.py already exists — delete it first."

    if name in _registry_keys():
        return f"❌ '{name}' collides with an existing tool key."

    path.write_text(source, encoding="utf-8")
    return f"✅ staged {name} at {path}"


def list_staged() -> list[dict]:
    """Metadata for every staged tool."""
    out = []
    for p in sorted(_dir("staging").glob("*.py")):
        try:
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
            doc = ast.get_docstring(tree) or ""
            tool_name = ""
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    for dec in node.decorator_list:
                        if (
                            isinstance(dec, ast.Call)
                            and getattr(dec.func, "id", "") == "tool"
                            and dec.args
                            and isinstance(dec.args[0], ast.Constant)
                        ):
                            tool_name = dec.args[0].value
                            break
                if tool_name:
                    break
            out.append({
                "file": p.name,
                "name": p.stem,
                "tool_name": tool_name,
                "doc": doc.strip().split("\n")[0][:120] if doc else "",
                "size": p.stat().st_size,
                "mtime": datetime.fromtimestamp(
                    p.stat().st_mtime
                ).isoformat(timespec="seconds"),
            })
        except Exception as e:
            out.append({"file": p.name, "name": p.stem, "error": str(e)})
    return out


def read_staged(name: str) -> Optional[str]:
    p = _dir("staging") / f"{name}.py"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def activate(name: str) -> tuple[bool, str]:
    """
    Move staging/<name>.py to active/<name>.py, import it, and register
    its @tool-decorated function(s) in agents.TOOL_REGISTRY live.
    Returns (ok, message).
    """
    err = _validate_name(name)
    if err:
        return False, err

    staged = _dir("staging") / f"{name}.py"
    if not staged.exists():
        return False, f"❌ no staged tool named '{name}'"

    active = _dir("active") / f"{name}.py"
    source = staged.read_text(encoding="utf-8")
    err = _validate_source(source)
    if err:
        return False, f"❌ not activatable: {err}"

    shutil.move(str(staged), str(active))

    try:
        spec = importlib.util.spec_from_file_location(
            f"dynamic_tools.{name}", active
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        shutil.move(str(active), str(staged))
        return False, f"❌ import failed: {type(e).__name__}: {e}"

    from orchestration import agents

    registered = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if not hasattr(obj, "name") or not hasattr(obj, "description"):
            continue
        key = getattr(obj, "name", "").lower().replace(" ", "_")
        if not key:
            continue
        if key in agents.TOOL_REGISTRY:
            logger.warning(
                f"dynamic tool '{key}' collides with existing key — skipped"
            )
            continue
        agents.TOOL_REGISTRY[key] = obj
        registered.append(key)

    if not registered:
        shutil.move(str(active), str(staged))
        return False, (
            "❌ file imported but no new tools registered "
            "(every @tool name collided with an existing key)"
        )

    return True, (
        f"✅ activated '{name}' — registered "
        f"{len(registered)} tool(s): {', '.join(registered)}"
    )


def deactivate(name: str, reason: str = "") -> tuple[bool, str]:
    """Remove a tool from the registry and archive its file."""
    err = _validate_name(name)
    if err:
        return False, err

    active = _dir("active") / f"{name}.py"
    if not active.exists():
        return False, f"❌ no active tool named '{name}'"

    from orchestration import agents
    source = active.read_text(encoding="utf-8")
    removed = []
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if (
                        isinstance(dec, ast.Call)
                        and getattr(dec.func, "id", "") == "tool"
                        and dec.args
                        and isinstance(dec.args[0], ast.Constant)
                    ):
                        key = dec.args[0].value.lower().replace(" ", "_")
                        if key in agents.TOOL_REGISTRY:
                            agents.TOOL_REGISTRY.pop(key, None)
                            removed.append(key)
    except Exception:
        pass

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.move(str(active), str(_dir("archived") / f"{name}.{ts}.py"))

    note = f" (reason: {reason})" if reason else ""
    return True, (
        f"✅ deactivated '{name}' — removed {len(removed)} tool(s): "
        f"{', '.join(removed) or 'none'}{note}"
    )


def list_active() -> list[dict]:
    out = []
    for p in sorted(_dir("active").glob("*.py")):
        out.append({
            "file": p.name,
            "name": p.stem,
            "size": p.stat().st_size,
            "mtime": datetime.fromtimestamp(
                p.stat().st_mtime
            ).isoformat(timespec="seconds"),
        })
    return out


def reject(name: str, reason: str = "") -> tuple[bool, str]:
    staged = _dir("staging") / f"{name}.py"
    if not staged.exists():
        return False, f"❌ no staged tool named '{name}'"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    note = f"\n# rejected {ts}: {reason}" if reason else f"\n# rejected {ts}"
    content = staged.read_text(encoding="utf-8") + note
    staged.write_text(content, encoding="utf-8")
    shutil.move(str(staged), str(_dir("rejected") / f"{name}.py"))
    return True, f"✅ rejected '{name}' → rejected/{name}.py"


def load_active_tools() -> list:
    """
    Import every active tool file and return the @tool-decorated
    objects. Called once at boot from gm._load_dynamic().
    """
    tools = []
    for p in sorted(_dir("active").glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(
                f"dynamic_tools.{p.stem}", p
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for attr_name in dir(module):
                obj = getattr(module, attr_name)
                if hasattr(obj, "name") and hasattr(obj, "description"):
                    tools.append(obj)
        except Exception:
            logger.exception(f"failed to load dynamic tool {p.name}")
    return tools


# ── Backwards compatibility ─────────────────────────────────────────
# The old gm.py called load_dynamic_tools(directory) against a single
# flat directory. That path is no longer used, but keep the name
# working as an alias so nothing that still imports it breaks.
def load_dynamic_tools(directory: str) -> list:
    """Deprecated. Use load_active_tools()."""
    return load_active_tools()
