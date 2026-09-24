# orchestration/agents.py
"""Agent registry, scaffolding, and dispatcher activation.

No tool allowlists. Every registered tool is available to every agent.
The only distinction is a single boolean: `can_delegate`.

  ceo         → can_delegate = True
  worker_*    → can_delegate = False

Tool discovery happens at runtime via `list_empire_tools()` and
`describe_tool(name)` — both registered in EmpireTools.
"""
import json
import logging
import os
import re
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Paths (set by gm.py via configure() at boot) ─────────────────────
BASE_DIR      = os.path.abspath(os.getcwd())
WORKSPACE_DIR = BASE_DIR
AGENTS_DIR    = os.path.join(BASE_DIR, "ai_civilization", "agents")


def configure(base_dir=None, workspace_dir=None, agents_dir=None):
    global BASE_DIR, WORKSPACE_DIR, AGENTS_DIR
    if base_dir:      BASE_DIR      = os.path.abspath(base_dir)
    if workspace_dir: WORKSPACE_DIR = os.path.abspath(workspace_dir)
    if agents_dir:    AGENTS_DIR    = os.path.abspath(agents_dir)
    for d in (WORKSPACE_DIR, AGENTS_DIR):
        os.makedirs(d, exist_ok=True)


# ── Tool registry (populated by gm.py) ───────────────────────────────
TOOL_REGISTRY: Dict[str, object] = {}


# ── Live registry ─────────────────────────────────────────────────────
AGENTS: Dict[str, dict] = {}
_registry_lock = threading.Lock()
_dispatch_started: Dict[str, bool] = {}


# ── Naming / paths ────────────────────────────────────────────────────
def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "worker"


def worker_thread_name(role: str) -> str:
    if role.startswith("worker_"):
        return role
    return f"worker_{slug(role)}"


def agent_dir(agent_name: str) -> str:
    return os.path.join(AGENTS_DIR, agent_name)


def logs_dir(agent_name: str) -> str:
    return os.path.join(agent_dir(agent_name), "logs")


# ── Scaffolding ───────────────────────────────────────────────────────
def scaffold_agent(agent_name: str) -> None:
    os.makedirs(agent_dir(agent_name), exist_ok=True)
    os.makedirs(logs_dir(agent_name), exist_ok=True)


def save_config(cfg: dict) -> None:
    scaffold_agent(cfg["name"])
    path = os.path.join(agent_dir(cfg["name"]), "agent.json")
    payload = {
        "name":         cfg["name"],
        "role":         cfg["role"],
        "is_ceo":       bool(cfg.get("is_ceo", False)),
        "can_delegate": bool(cfg.get("can_delegate", False)),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        logger.exception(f"Failed to write agent.json for {cfg['name']}")


# ── Access ────────────────────────────────────────────────────────────
def get(agent_name: str) -> Optional[dict]:
    return AGENTS.get(agent_name)


def worker_roster() -> List[str]:
    return [n for n, c in AGENTS.items() if not c.get("is_ceo")]


def all_tools() -> list:
    """Every registered tool object. Used by agent_loop to build the
    agent's runtime tool list — not stored on the agent config."""
    return list(TOOL_REGISTRY.values())


# ── CEO registration ──────────────────────────────────────────────────
def register_ceo() -> None:
    cfg = {
        "name":         "ceo",
        "role":         "The Global CEO",
        "workspace":    WORKSPACE_DIR,
        "is_ceo":       True,
        "can_delegate": True,
    }
    AGENTS["ceo"] = cfg
    save_config(cfg)


# ── Worker registration ───────────────────────────────────────────────
def ensure_worker(role_or_thread: str) -> str:
    """
    Register a worker for `role` if not already registered. Returns the
    worker's inbox thread name. The dispatcher is started separately by
    the messenger after the first message row is written.
    """
    thread = worker_thread_name(role_or_thread)

    with _registry_lock:
        if thread not in AGENTS:
            display_role = role_or_thread
            if display_role.startswith("worker_"):
                display_role = display_role.replace("worker_", "", 1).replace("_", " ")
            cfg = {
                "name":         thread,
                "role":         display_role,
                "workspace":    WORKSPACE_DIR,
                "is_ceo":       False,
                "can_delegate": False,
            }
            AGENTS[thread] = cfg
            save_config(cfg)
            logger.info(f"Worker '{thread}' registered (role='{display_role}')")

    return thread


# ── Dispatcher activation ─────────────────────────────────────────────
def ensure_dispatcher(agent_name: str, start_from: Optional[int] = None) -> None:
    from orchestration import agent_loop

    with _registry_lock:
        if _dispatch_started.get(agent_name):
            return
        t = threading.Thread(
            target=agent_loop.dispatcher,
            args=(agent_name, start_from),
            daemon=True,
            name=f"dispatcher-{agent_name}",
        )
        t.start()
        _dispatch_started[agent_name] = True
        logger.info(f"Dispatcher started for '{agent_name}' (start_from={start_from})")


def start_ceo_dispatcher() -> None:
    ensure_dispatcher("ceo", start_from=None)
