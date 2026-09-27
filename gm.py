# gm.py — Boot file for the agent system.
#
# Architecture
# ────────────
# One workspace. One inbox. Every agent — CEO and workers — is the same
# shape: LLM + tools + shared workspace + an inbox thread + a dispatcher.
#
# Delegation is a tool call: send_message(to="React Dev", body="...").
# Verification is a tool call: read_agent_log(agent="worker_react_dev").
# Reply is a terminal action: SEND_REPLY.
#
# The code that runs the loop lives in:
#
#   orchestration/agents.py       — registry + scaffolding + dispatcher start
#   orchestration/agent_loop.py   — run_agent_turn + dispatcher
#   orchestration/messenger.py    — send_message + read_agent_log
#   orchestration/inbox.py        — thin helpers over InboxDB
#   orchestration/inbox_db.py     — the SQLite table
#   orchestration/dynamic_tools.py — staging/active/rejected/archived loader
#   ceo_prompter.py               — build_ceo_prompt + build_worker_prompt
#
# This file only wires them together and exposes the entry points that
# org_bot_worker.py, the console, and any external code needs.
#
# Threads
# ───────
#   ceo                     ← user and workers write here
#   worker_<name>           ← CEO writes here
#   user_<chat_id>          ← CEO writes here; Telegram poller drains it
#
# Every message row is:  (thread, sender, body, attachments, created_at)
# `thread` is the recipient. `sender` is the writer.

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

# ── Core imports ──────────────────────────────────────────────────────
from empire_tools import EmpireTools
from orchestration import agents, agent_loop, inbox, messenger
from orchestration import dynamic_tools as dt
from orchestration.mcp_manager import load_mcp_tools
from logger import setup_logging


# ══════════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════════
setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

for noisy in (
    "httpx", "httpcore", "urllib3", "huggingface_hub",
    "sentence_transformers", "filelock", "crewai", "chromadb",
    "posthog",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════
load_dotenv()
os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("POSTHOG_DISABLED", "true")

console = Console()

BASE_DIR         = os.path.abspath(os.getcwd())
CIVILIZATION_DIR = os.path.join(BASE_DIR, "ai_civilization")

# The workspace that every agent shares. Agents read and write here.
# Change to BASE_DIR if you want them to operate on the repo itself
# (not recommended — they'd be able to overwrite gm.py).
WORKSPACE_DIR    = BASE_DIR

AGENTS_DIR       = os.path.join(CIVILIZATION_DIR, "agents")
INBOX_DB_PATH    = os.path.join(CIVILIZATION_DIR, "inbox.db")

for d in (CIVILIZATION_DIR, WORKSPACE_DIR, AGENTS_DIR):
    os.makedirs(d, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════
# Wire the orchestration package
# ══════════════════════════════════════════════════════════════════════
inbox.init(INBOX_DB_PATH)

agents.configure(
    base_dir=BASE_DIR,
    workspace_dir=WORKSPACE_DIR,
    agents_dir=AGENTS_DIR,
)


# ══════════════════════════════════════════════════════════════════════
# Tool loading
# ══════════════════════════════════════════════════════════════════════
def _load_empire_tools() -> list:
    tools = []
    for method_name in dir(EmpireTools):
        if method_name.startswith("_"):
            continue
        obj = getattr(EmpireTools, method_name)
        if hasattr(obj, "name") and hasattr(obj, "description"):
            tools.append(obj)
    return tools


def _load_dynamic() -> list:
    """
    Import every agent-authored tool from
        ai_civilization/dynamic_tools/active/

    The staging/active/rejected/archived lifecycle is managed by
    orchestration/dynamic_tools.py. This call only reads `active/`
    and returns the tool objects for registration.
    """
    return dt.load_active_tools()


_all_tools: list = []
try:
    _all_tools = _load_empire_tools() + _load_dynamic() + load_mcp_tools()
except Exception as e:
    logger.warning(f"Tool load partial failure: {e}")

# Messenger tools (send_message, read_agent_log) are not part of
# EmpireTools — they live in the orchestration package.
_all_tools += messenger.MESSENGER_TOOLS

# Public alias kept for anything that imports it from gm.
all_empire_tools = _all_tools

# Populate the registry that agents.py + agent_loop.py read from.
for t in _all_tools:
    key = getattr(t, "name", "").lower().replace(" ", "_")
    if key:
        agents.TOOL_REGISTRY[key] = t

logger.info(f"Loaded {len(agents.TOOL_REGISTRY)} tools into registry")

# Warm the loop guard's embedding model so the first send_message
# doesn't stall for 1–2s on the cold start. Safe if the model fails
# to load — the guard degrades to exact-match only.
try:
    from orchestration.loop_guard import warm_model
    warm_model()
except Exception:
    logger.exception("loop_guard: warm_model failed (guard still functional)")

# Note: agent-authored tools are not present in the count above until
# activate_tool() registers them. After activation, the registry grows
# in place and every subsequent turn sees the new tool. See
# tools/dynamic_tools_tool.py for the runtime activation path.


# ══════════════════════════════════════════════════════════════════════
# Register CEO and start its dispatcher
# ══════════════════════════════════════════════════════════════════════
agents.register_ceo()
agents.start_ceo_dispatcher()


# ══════════════════════════════════════════════════════════════════════
# Public entry points
# (called by org_bot_worker.py, the console loop, and external code)
# ══════════════════════════════════════════════════════════════════════
def start_chat_mission(
    raw_mission: str,
    priority: str = "normal",
    mcp_tools: list = None,
    thread_id: str = "console",
) -> str:
    """
    Queue a user message into the CEO's inbox. Returns the message id.
    The CEO's dispatcher picks it up within ~1s.
    """
    user_thread = f"user_{thread_id}"
    msg_id = inbox.send(thread="ceo", sender=user_thread, body=raw_mission)
    logger.info(
        f"Queued user message #{msg_id} from {user_thread} to ceo: "
        f"{raw_mission[:80]}"
    )
    return str(msg_id)


def is_greeting(text: str) -> bool:
    normalized = text.lower().strip().rstrip(".!?")
    return normalized in {
        "hi", "hello", "hey", "good morning", "good afternoon",
        "good evening", "thanks", "thank you", "thx", "ty",
    }


def send_instant_reply(thread_id: str, text: str) -> None:
    """Queue a reply directly, bypassing the CEO. Used for greetings."""
    inbox.send(thread=f"user_{thread_id}", sender="ceo", body=text)
    logger.info(f"Instant reply queued to {thread_id}: {text[:50]}")


def get_population(mcp_tools: list = None) -> list:
    """
    Kept for backwards compatibility with any caller that still expects
    a population list. Returns a single stub for the CEO.
    """
    ceo = agents.get("ceo") or {}
    class _Stub:
        pass
    s = _Stub()
    s.role = ceo.get("role", "The Global CEO")
    s.tools = ceo.get("tools", [])
    return [s]


# ══════════════════════════════════════════════════════════════════════
# Compat shims (safe no-ops for old imports)
# ══════════════════════════════════════════════════════════════════════
class _NullManager:
    """Stand-in for the old TaskManager. Methods are no-ops."""
    tasks = {}
    def start_watchdog(self):    pass
    def stop_watchdog(self):     pass
    def heartbeat(self, *a, **k): pass
    def register_task(self, *a, **k): pass
    def unregister_task(self, *a, **k): pass
    def list_tasks(self):        return []
    def get_task(self, *a, **k): return None
    def active_count(self):      return 0
    def trigger_dream_state(self): pass


class _NullScheduler:
    def start(self):  pass
    def stop(self):   pass


manager   = _NullManager()
scheduler = _NullScheduler()


def run_repl_code(code: str, timeout: int = 5, max_output: int = 2000) -> str:
    """
    Kept for backwards compatibility with anything that still calls it
    (e.g. an execute_repl path in empire_tools). Runs a Python snippet
    in a temp file and returns its stdout/stderr.
    """
    script_id = uuid.uuid4().hex[:8]
    script_path = os.path.join("/tmp", f"repl_{script_id}.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(code)
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=timeout,
            env={
                **os.environ,
                "HTTP_PROXY": "", "HTTPS_PROXY": "", "NO_PROXY": "*",
            },
        )
        out = result.stdout.strip()
        if result.returncode != 0:
            out += f"\n[ERROR] {result.stderr.strip()}"
        if len(out) > max_output:
            out = out[:max_output] + "\n...[TRUNCATED]"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Execution timed out after {timeout}s."
    except Exception as e:
        return f"Error: {e}"
    finally:
        if os.path.exists(script_path):
            try:
                os.unlink(script_path)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
# Console-side user-thread watcher
# ══════════════════════════════════════════════════════════════════════
def _start_user_thread_poller() -> None:
    """
    Watch every user_* thread for new CEO replies and print them to the
    console. The Telegram poller in org_bot_worker.py does the actual
    sending — this is just a local debug aid.
    """
    def _watch():
        seen: Dict[str, int] = {}
        while True:
            try:
                for thread in inbox.list_user_threads():
                    last = seen.get(thread, 0)
                    for m in inbox.since(thread, last):
                        sender = m.get("sender", "")
                        body = (m.get("body") or "")
                        if sender == "ceo":
                            console.print(
                                f"[bold green]CEO → {thread}:[/bold green] "
                                f"{body[:200]}"
                            )
                        seen[thread] = max(last, int(m.get("id", last)))
            except Exception:
                logger.exception("user-thread poller iteration failed")
            time.sleep(1)

    threading.Thread(target=_watch, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════
# Shutdown
# ══════════════════════════════════════════════════════════════════════
def _shutdown(*_) -> None:
    logger.info("shutdown requested")
    sys.exit(0)


# ══════════════════════════════════════════════════════════════════════
# Entry point (console mode)
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    console.print(Panel.fit(
        "[bold red]EMPIRE — agents + inbox[/bold red]",
        border_style="red",
    ))
    console.print(
        f"[dim]Workspace: {WORKSPACE_DIR}[/dim]\n"
        f"[dim]Inbox DB:  {INBOX_DB_PATH}[/dim]"
    )

    _start_user_thread_poller()
    logger.info("CEO dispatcher + console poller running")

    console.print("[dim]Type a message for the CEO. Ctrl+C to exit.[/dim]")
    while True:
        try:
            raw = console.input("\n[bold white]YOU > [/bold white]").strip()
            if not raw:
                continue
            if is_greeting(raw):
                send_instant_reply("console", "Hello! How can I assist you today?")
                continue
            start_chat_mission(raw, thread_id="console")
            console.print("[dim](queued)[/dim]")
        except KeyboardInterrupt:
            _shutdown()
        except Exception:
            logger.exception("console loop error")
