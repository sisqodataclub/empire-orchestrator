# gm.py — Zero-Trust Tool Segregation + AgentBus multi-agent delegation
#
# Architecture:
#   • Console / Telegram write IN messages to the inbox.
#   • start_chat_mission() spawns an ActiveTask for the user's thread.
#   • The CEO loop reads the mission, uses tools or delegates, replies, exits.
#   • DELEGATE writes to an agent's thread and spawns a concurrent ActiveTask.
#   • When the agent finishes, AgentBus writes [AGENT_REPLY] back to the
#     CEO's thread.
#   • The AgentBus notifier watches those threads and wakes a fresh
#     assistant mission so the CEO can deliver the result to the user.
#
# Observability:
#   • TaskManager keeps a live health map (heartbeat, current step) per task.
#   • A watchdog thread fires a "mission appears stuck" reply if a task's
#     heartbeat goes silent for 90+ seconds.
#   • The CEO has three in-memory tools (system_status, inspect_task,
#     cancel_task) and full EXECUTE_REPL / EXECUTE_TERMINAL access to
#     investigate the whole ecosystem on demand.
#
import os
import sys
import time
import json
import logging
import threading
import subprocess
import uuid

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markdown import Markdown
from dotenv import load_dotenv

# CORE IMPORTS
from empire_tools import EmpireTools
from task_manager import TaskManager
from orchestration.inbox_db import InboxDB
from orchestration.dynamic_tools import load_dynamic_tools
from orchestration.scheduler_db import SchedulerDB
from orchestration.scheduler import TaskScheduler
from orchestration.mcp_manager import load_mcp_tools
from tools.scheduler_tools import set_scheduler_db

# Centralised logging
from logger import setup_logging
setup_logging(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Silence telemetry spam
logging.getLogger("crewai").setLevel(logging.ERROR)
logging.getLogger("posthog").setLevel(logging.CRITICAL)
logging.getLogger("chromadb").setLevel(logging.ERROR)

# Silence noisy HTTP/model-download logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("filelock").setLevel(logging.WARNING)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
load_dotenv()
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"
os.environ["OTEL_SDK_DISABLED"]         = "true"
os.environ["POSTHOG_DISABLED"]          = "true"

console = Console()
manager = TaskManager()

# ── Civilization Directory Structure ──
CIVILIZATION_DIR = os.path.join(os.getcwd(), "ai_civilization")
for folder in [
    CIVILIZATION_DIR,
    os.path.join(CIVILIZATION_DIR, "mission_logs"),
    os.path.join(CIVILIZATION_DIR, "agent_memory"),
    os.path.join(CIVILIZATION_DIR, "dynamic_tools"),
    os.path.join(CIVILIZATION_DIR, "scratch"),
    os.path.join(CIVILIZATION_DIR, "logs"),
]:
    if not os.path.exists(folder):
        os.makedirs(folder)
        logger.info(f"Created directory: {folder}")

# ── Shared inbox DB ──
INBOX_DB_PATH = os.path.join(CIVILIZATION_DIR, "inbox.db")

# ── Task Scheduler ──
SCHEDULER_DB_PATH = os.path.join(CIVILIZATION_DIR, "scheduler.db")
scheduler_db = SchedulerDB(SCHEDULER_DB_PATH)
set_scheduler_db(scheduler_db)
scheduler = TaskScheduler(SCHEDULER_DB_PATH, thread_id="scheduler")

# ── Wire system-observability context for the CEO ──
# This is what lets the CEO answer "what's happening?" / "is it stuck?" by
# reading the live TaskManager + the inbox/scheduler/logs directly.
from tools.system_observability_tools import set_observability_context

set_observability_context(
    task_manager=manager,
    log_path=os.path.join(CIVILIZATION_DIR, "logs", "empire.log"),
    inbox_db_path=INBOX_DB_PATH,
    scheduler_db_path=SCHEDULER_DB_PATH,
)
logger.info("🔭 Observability context wired.")

# ── Start the health watchdog ──
# Fires a "mission appears stuck" reply on the task's thread if any task's
# heartbeat goes silent for 90+ seconds. Runs in a daemon thread.
manager.start_watchdog()
logger.info("💓 Task health watchdog started.")

# ==============================================================================
# MISSION TEMPLATES
# ==============================================================================
MISSION_TEMPLATES = {
    "audit": (
        "Perform a full codebase audit of the entire project. "
        "Map all files, identify bugs, dead code, security vulnerabilities, and performance issues. "
        "Output a prioritized fix list with file:line references."
    ),
    "deploy": (
        "Verify all tests pass, build the project, and deploy to production. "
        "Use zero-trust verification at every step. "
        "Do NOT proceed to the next step until the current one is physically confirmed."
    ),
    "debug": (
        "The application has a critical bug. Reproduce it, trace the root cause through the "
        "full call stack, write a minimal surgical fix, and verify it resolves the issue "
        "without breaking anything else."
    ),
    "harvest": (
        "Identify the entire tech stack from package.json and requirements.txt. "
        "Harvest official documentation for each major dependency and index it "
        "into the Empire Library for JIT retrieval."
    ),
    "refactor": (
        "Analyze the codebase for structural issues: duplicate logic, god files, "
        "poor naming, and missing error handling. "
        "Propose and implement a clean, surgical refactor plan."
    ),
    "test": (
        "Write comprehensive unit and integration tests for the entire codebase. "
        "Achieve minimum 80% coverage. Run all tests and confirm they pass."
    ),
}

# ==============================================================================
# NATIVE AGENT CLASS
# ==============================================================================
class NativeAgent:
    def __init__(self, role: str, goal: str, backstory: str, tools: list = None):
        self.role          = role
        self.goal          = goal
        self.backstory     = f"{goal}\n\n{backstory}"
        self.tools         = tools or []
        self.step_callback = None

# ==============================================================================
# TOOL LOADING
# ==============================================================================
def _load_empire_tools() -> list:
    tools = []
    for method_name in dir(EmpireTools):
        if method_name.startswith("_"):
            continue
        obj = getattr(EmpireTools, method_name)
        if hasattr(obj, 'name') and hasattr(obj, 'description'):
            tools.append(obj)
    return tools

def _load_dynamic_tools() -> list:
    dynamic_dir = os.path.join(CIVILIZATION_DIR, "dynamic_tools")
    return load_dynamic_tools(dynamic_dir)

all_empire_tools = _load_empire_tools() + _load_dynamic_tools() + load_mcp_tools()

TOOL_REGISTRY: dict = {
    getattr(t, 'name', '').lower().replace(' ', '_'): t
    for t in all_empire_tools
    if hasattr(t, 'name')
}

# ==============================================================================
# SESSION PERSISTENCE
# ==============================================================================
SESSION_PATH = os.path.join(CIVILIZATION_DIR, "session.json")

def save_session():
    try:
        snapshot = {
            tid: {
                "mission":        t.mission,
                "status":         t.status,
                "timestamp":      t.timestamp,
                "result_preview": str(t.result)[:200] if t.result else None,
            }
            for tid, t in manager.tasks.items()
        }
        with open(SESSION_PATH, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=2)
        logger.debug("Session saved successfully")
    except Exception as e:
        logger.error(f"Session save error: {e}")
        console.print(f"[dim red]Session save error: {e}[/dim red]")

def load_session():
    if not os.path.exists(SESSION_PATH):
        return
    try:
        with open(SESSION_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data:
            logger.info(f"Restored {len(data)} missions from session")
            console.print(f"[dim]📂 Restored {len(data)} missions from last session.[/dim]")
    except Exception as e:
        logger.warning(f"Session load error: {e}")

# ==============================================================================
# POPULATION MANAGEMENT
# ==============================================================================
QA_TOOL_NAMES = {
    "manage_file",
    "execute_terminal",
    "inspect_code",
    "commit_to_library",
}

TOOL_BUILDER_TOOL_NAMES = {
    "manage_file",
    "execute_terminal",
    "inspect_code",
    "list_directory",
}

def _filter_tools_by_names(tools_list: list, allowed_names: set) -> list:
    filtered = []
    for t in tools_list:
        t_name = getattr(t, 'name', '').lower().replace(' ', '_')
        if t_name in allowed_names:
            filtered.append(t)
    return filtered

def run_repl_code(code: str, timeout=5, max_output=2000) -> str:
    script_id = uuid.uuid4().hex[:8]
    script_path = f"/tmp/repl_{script_id}.py"
    with open(script_path, "w") as f:
        f.write(code)
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HTTP_PROXY": "", "HTTPS_PROXY": "", "NO_PROXY": "*"},
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            output += f"\n[ERROR] {result.stderr.strip()}"
        if len(output) > max_output:
            output = output[:max_output] + "\n...[TRUNCATED]"
        return output
    except subprocess.TimeoutExpired:
        logger.warning("REPL code execution timed out")
        return "Error: Execution timed out after 5 seconds."
    finally:
        if os.path.exists(script_path):
            os.unlink(script_path)

def get_population(mcp_tools: list = None) -> list:
    if mcp_tools is None:
        mcp_tools = []

    all_available = all_empire_tools
    agents = []

    emperor = NativeAgent(
        role="The Global CEO",
        goal=(
            "Operate as a God-Tier Principal Staff Engineer. "
            "Translate Overlord intent into deterministic, flawless execution "
            "and orchestrate workers."
        ),
        backstory="""You are the Supreme Intelligence of a rising Technocratic Empire.

🚨 DIRECTIVE 1 — ARCHITECTURAL ORCHESTRATION:
You plan, scope, and delegate. You do NOT perform raw file edits or execute unverified shell scripts.
Use 'Inspect Code' (map mode) and 'List Directory' to map systems before delegating.

🚨 DIRECTIVE 2 — DYNAMIC PIVOTING:
If an agent fails repeatedly, DO NOT repeat the same command.
Invent a new technical vector. If 3 pivots fail, use 'Consult Overlord'. Never guess.

🚨 DIRECTIVE 3 — ZERO HALLUCINATION & FACT‑CHECKING:
Never assume a fact. For ANY factual answer (URLs, phone numbers, code details, dates, etc.),
you MUST verify using available tools:
   - Use 'Internet Search' or 'Scrape Webpage' to confirm web links or public information.
   - Use 'Inspect Code' or 'List Directory' to verify internal files.
   - Use 'EXECUTE_REPL' to run Python code that retrieves the exact answer (query DB, parse file).
   When replying, cite your source or methodology.

🚨 DIRECTIVE 4 — COMPILER SEMANTICS:
For tsc, rustc, go build — ZERO OUTPUT = ZERO ERRORS = SUCCESS.
Move to the next goal once verified.

🚨 DIRECTIVE 5 — WORKER MEMORY LAW:
Workers remember their last 3 task summaries AND their last raw command output.
They do NOT have full terminal history.

🚨 DIRECTIVE 6 — INBOX COMMUNICATION:
Use 'Read Inbox' to check for new messages from the user.
Use 'Send User Message' to reply. For simple conversational messages,
reply directly without delegating or creating files.
Use 'Ask User' only when you need clarification and must pause.

🚨 DIRECTIVE 7 — DELEGATION BOUNDARY (WITH FULL TOOL ACCESS):
- You now have direct access to ALL tools in the empire, including file management, terminal, email, research, etc.
- HOWEVER, you must STILL obey these rules:
   - Use EXECUTE_REPL or read‑only tools for exploration and verification.
   - NEVER use WRITE_FILE or TERMINAL to modify files or run compilers/installers yourself. Always DELEGATE those actions to a specialist.
   - You may use email tools (read_latest_emails, send_email) directly when asked.
   - You may use search, scrape, inspect, list, scheduler, secret, and other non‑destructive tools directly.
- The system will automatically block any dangerous direct action and remind you to delegate.

🚨 DIRECTIVE 8 — EXTENDING THE EMPIRE:
You may delegate to the 'Tool Builder' to create new Python tools saved in 'ai_civilization/dynamic_tools'.
You have secret management tools and a built-in scheduler:
  - Use 'add_project' to group related tasks.
  - Use 'add_task' to schedule future work, with optional 'project_id' and 'depends_on_task_id'.
  - The scheduler runs in the background and will wake you when tasks are due or dependencies are satisfied.

🚨 DIRECTIVE 9 — MULTI-AGENT DELEGATION (NEW):
When you use DELEGATE, the system spawns a separate agent in its own thread.
You do NOT wait for it — your mission exits immediately. When the agent
finishes, a new mission will be created and you will be asked to reply to
the user with the result. You can therefore handle multiple tasks in
parallel. The agent replies to you via your inbox thread.

🚨 DIRECTIVE 10 — SYSTEM CARETAKER (NEW):
You are the administrator of your own universe. You have full visibility
into the running system and the power to diagnose and fix it.

LIVE-STATE TOOLS (only these need special tools because they touch
in-memory Python state that SQL cannot reach):
  • system_status()      → composite: tasks + inbox + scheduler + log warnings
  • inspect_task("N")    → deep dive on mission #N (heartbeat, step, history)
  • cancel_mission("N")  → force-terminate a stuck mission

EVERYTHING ELSE you investigate yourself via EXECUTE_REPL / EXECUTE_TERMINAL.
The SYSTEM MAP in your prompt shows the SQLite schema and log paths you
can query directly. Do not wait for a dedicated tool — write the query.

When to investigate unprompted:
  • If a tool returns an error you don't understand
  • If the user says "nothing is happening" or "you didn't reply"
  • If system_status() shows a task with age > 90s and running
Report findings with specifics: task IDs, counts, timestamps, error text.
""",
        tools=all_available,
    )
    agents.append(emperor)

    qa_tools = _filter_tools_by_names(all_available, QA_TOOL_NAMES)
    qa_agent = NativeAgent(
        role="Quality Assurance Engineer",
        goal="Cryptographically and physically validate all technical work before it is marked complete.",
        backstory="""You are the Gatekeeper. Nothing passes without physical proof.

⚡ ZERO-TRUST VERIFICATION:
Never assume a file was updated because the terminal didn't error.
Write a Python script or CLI command (cat, ls -la, sqlite3) to physically read
the changed data and PROVE the change took effect.

⚡ COMPILER SEMANTICS:
If tsc / rustc / go build returns zero output with exit code 0 — that IS success.
Do not re-run it or flag it as a failure. Report: compilation clean, zero errors.

⚡ PYTHON SCRIPT INJECTION:
When verifying file edits, write a temporary verify.py, execute it, and report the truth.""",
        tools=qa_tools,
    )
    agents.append(qa_agent)

    tool_builder_tools = _filter_tools_by_names(all_available, TOOL_BUILDER_TOOL_NAMES)
    tool_builder = NativeAgent(
        role="Tool Builder",
        goal="Build and register new Python tools for the empire's dynamic tool directory.",
        backstory=(
            "You create reusable tools that other agents can use. Write "
            "@tool-decorated functions and save them in the "
            "'ai_civilization/dynamic_tools' directory. After saving, the tool "
            "becomes available in future missions automatically."
        ),
        tools=tool_builder_tools,
    )
    agents.append(tool_builder)

    # ── DNA-loaded sub-agents ──
    json_files = [f for f in os.listdir(CIVILIZATION_DIR) if f.endswith(".json")]
    for filename in json_files:
        try:
            with open(os.path.join(CIVILIZATION_DIR, filename), "r", encoding="utf-8") as f:
                dna = json.load(f)

            if dna.get("status") != "ACTIVE":
                continue

            agent_specific_tools = []
            for cap in dna.get("capabilities", []):
                normalized_cap = cap.lower().replace(' ', '_')
                if normalized_cap in TOOL_REGISTRY:
                    agent_specific_tools.append(TOOL_REGISTRY[normalized_cap])

            if mcp_tools:
                agent_specific_tools.extend(mcp_tools)

            sub_agent = NativeAgent(
                role=dna['role'],
                goal=dna['goal'],
                backstory=(
                    dna['backstory'] +
                    "\n\n🧠 MEMORY PROTOCOL: Whenever you solve a complex problem, "
                    "use 'Commit to Global Library' to index it permanently."
                ),
                tools=agent_specific_tools,
            )
            agents.append(sub_agent)

        except Exception as e:
            logger.error(f"Failed to load DNA file '{filename}': {e}")
            console.print(f"[dim red]⚠️ Failed to load '{filename}': {e}[/dim red]")
            continue

    return agents

# ==============================================================================
# MISSION PROMPT BUILDER
# ==============================================================================
DESTRUCTIVE_KEYWORDS = {
    "deploy", "delete", "remove", "drop", "modify", "update", "fix",
    "patch", "migrate", "refactor", "overwrite", "replace", "install",
}

def build_mission_prompt(mission: str, priority: str = "normal") -> str:
    base     = mission.strip()
    is_high  = priority in ("high", "critical")
    needs_qa = any(kw in mission.lower() for kw in DESTRUCTIVE_KEYWORDS)

    if is_high or needs_qa:
        base += (
            "\n\n🛡️ EXECUTION PROTOCOLS (MANDATORY):\n"
            "1. SCALPEL SCRIPTING: Write surgical scripts — never bulldoze files.\n"
            "2. ZERO-TRUST: Physically verify every change before marking it done.\n"
            "3. INSPECTION FIRST: For any destructive action, run an inspection step first.\n"
            "4. QA GATE: Assign Quality Assurance Engineer to validate final output.\n"
            "5. COMPILER SEMANTICS: tsc/rustc/go zero output = zero errors = SUCCESS."
        )

    return base

# ==============================================================================
# START CHAT MISSION (unified entry point for all user messages)
# ==============================================================================
def start_chat_mission(
    raw_mission: str,
    priority: str = "normal",
    mcp_tools: list = None,
    thread_id: str = "console",
):
    """
    Create an inbox item and start a full mission.
    Called by:
      • Console input loop
      • Telegram worker (org_bot_worker.py)
      • AgentBus notifier (to wake the CEO for agent replies)
    """
    logger.info(f"Starting chat mission for thread '{thread_id}': {raw_mission[:80]}")
    inbox_db = InboxDB(INBOX_DB_PATH)

    msg_id = inbox_db.add_message(
        thread_id=thread_id,
        direction="IN",
        body=raw_mission,
        sender="user",
        recipient="CEO",
        status="NEW",
    )

    mission_text = (
        f"Process inbox message #{msg_id} in thread {thread_id}.\n"
        f"User message: {raw_mission}\n"
        f"Instructions:\n"
        f"- This is a task from the user. Execute it fully.\n"
        f"- You must send a final reply via SEND_REPLY with the result.\n"
        f"- Do NOT call FINISH until you have sent a reply.\n"
    )

    full_mission = build_mission_prompt(mission_text, priority=priority)
    population = get_population(mcp_tools=mcp_tools or [])
    task_id = manager.start_mission(full_mission, population)
    logger.info(f"Mission {task_id} started for thread '{thread_id}'")
    return task_id

# ── Greeting fast-path ──
def is_greeting(text: str) -> bool:
    normalized = text.lower().strip().rstrip(".!?")
    greetings = {
        "hi", "hello", "hey", "good morning", "good afternoon",
        "good evening", "thanks", "thank you", "thx", "ty",
    }
    return normalized in greetings

def send_instant_reply(thread_id: str, text: str):
    inbox_db = InboxDB(INBOX_DB_PATH)
    inbox_db.add_message(
        thread_id=thread_id,
        direction="OUT",
        body=text,
        sender="CEO",
        recipient="user",
        status="PENDING_DELIVERY",
    )
    logger.info(f"Sent instant reply to thread '{thread_id}': {text[:50]}")

# ==============================================================================
# UI HELPERS
# ==============================================================================
def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def show_dashboard():
    clear()
    console.print(Panel.fit(
        "[bold red]⚔️  GLOBAL DOMINANCE SYSTEM  ⚔️[/bold red]\n"
        "[dim]Chat with the CEO — just type a message[/dim]",
        border_style="red",
    ))
    console.print("[dim]Commands:[/dim]")
    console.print("  [cyan]view <id>[/cyan]        View a mission log")
    console.print("  [cyan]parallel[/cyan]         View all running missions")
    console.print("  [cyan]status[/cyan]           Live system health snapshot")
    console.print("  [cyan]roster[/cyan]           Show agent population")
    console.print("  [cyan]templates[/cyan]       List mission templates")
    console.print("  [cyan]!use <template>[/cyan]  Launch a template")
    console.print("  [cyan]search <query>[/cyan]   Search mission history")
    console.print("  [cyan]exit[/cyan]              Save and shutdown")
    console.print()
    console.print("[bold]Type your message and press Enter.[/bold]")

# ==============================================================================
# VIEWERS
# ==============================================================================
def view_task_live(task_id: str):
    task = manager.get_task(task_id)
    if not task:
        console.print("[red]❌ Task ID not found.[/red]")
        logger.warning(f"View attempted for non-existent task {task_id}")
        time.sleep(1)
        return

    logger.info(f"Viewing live task {task_id}")
    current_log_index = 0
    clear()
    console.print(Panel(
        f"[bold yellow]📺 MISSION #{task_id}[/bold yellow]\n[dim]{task.mission[:100]}[/dim]",
        border_style="yellow",
    ))
    console.print("[dim]Press Ctrl+C to pause and intervene.[/dim]\n")

    try:
        while True:
            if len(task.logs) > current_log_index:
                for log in task.logs[current_log_index:]:
                    console.print(log)
                current_log_index = len(task.logs)

            if task.status == "AWAITING_OVERLORD":
                console.print(
                    "\n[bold cyan]🤔 CEO IS WAITING FOR YOUR INPUT.[/bold cyan]"
                )
                try:
                    answer = console.input("[bold cyan]OVERLORD > [/bold cyan]").strip()
                    if answer:
                        manager.intervene(task_id, answer)
                        console.print("[green]✅ Instruction delivered. Resuming...[/green]")
                        logger.info(f"Intervention sent for task {task_id}: {answer[:80]}")
                except KeyboardInterrupt:
                    pass

            if task.is_complete:
                console.print()
                if task.result:
                    console.print(Panel(
                        Markdown(str(task.result)),
                        title="📝 MISSION COMPLETE — INTELLIGENCE REPORT",
                        border_style="green",
                    ))
                save_session()
                console.print("\n[dim]Press Enter to return...[/dim]")
                input()
                return

            time.sleep(0.4)

    except KeyboardInterrupt:
        console.print("\n[bold yellow]⏸️  PAUSED.[/bold yellow]")
        try:
            cmd = console.input("[bold red]OVERLORD INTERVENTION > [/bold red]").strip()
            if cmd.lower() in ("exit", "q"):
                return
            if cmd:
                manager.intervene(task_id, cmd)
                console.print("[green]✅ Instruction sent![/green]")
                logger.info(f"Intervention sent for task {task_id}: {cmd[:80]}")
                time.sleep(0.5)
                console.print("[dim]▶️  Resuming...[/dim]")
                view_task_live(task_id)
        except KeyboardInterrupt:
            return

def view_parallel():
    running = [t for t in manager.list_tasks() if t.status == "RUNNING"]
    if not running:
        console.print("[dim]No missions currently running.[/dim]")
        console.input("Press Enter to continue...")
        return

    clear()
    console.print(Panel(
        f"[bold yellow]🚀 PARALLEL VIEW — {len(running)} active missions[/bold yellow]\n"
        "[dim]Press Ctrl+C to return.[/dim]",
        border_style="yellow",
    ))

    log_indices = {t.id: 0 for t in running}

    try:
        while True:
            still_running = [t for t in running if not t.is_complete]
            if not still_running:
                console.print("\n[green]✅ All parallel missions complete.[/green]")
                console.input("Press Enter to continue...")
                return
            for task in running:
                new_logs = task.logs[log_indices[task.id]:]
                if new_logs:
                    console.print(
                        f"\n[bold magenta]══ Mission #{task.id}: "
                        f"{task.mission[:50]} ══[/bold magenta]"
                    )
                    for log in new_logs[-4:]:
                        console.print(log)
                    log_indices[task.id] = len(task.logs)
            time.sleep(1.5)

    except KeyboardInterrupt:
        return

def show_status():
    """Live system health snapshot — what the CEO sees when asked 'what's happening?'."""
    clear()
    console.print(Panel.fit(
        "[bold cyan]🔭 SYSTEM HEALTH[/bold cyan]",
        border_style="cyan",
    ))
    try:
        from tools.system_observability_tools import system_status
        report = system_status.func() if hasattr(system_status, "func") else system_status()
        console.print(report)
    except Exception as e:
        console.print(f"[red]Error getting status: {e}[/red]")
    console.input("\n[dim]Press Enter to return...[/dim]")

def show_roster(mcp_tools: list = None):
    population = get_population(mcp_tools=mcp_tools or [])
    table = Table(
        title="🧬 EMPIRE POPULATION",
        header_style="bold magenta",
        expand=True,
        show_lines=True,
    )
    table.add_column("Role",  style="bold cyan", width=28)
    table.add_column("Tools", style="green",     width=35)
    table.add_column("Goal",  style="dim")

    for agent in population:
        tool_names = [getattr(t, 'name', '?') for t in agent.tools[:6]]
        overflow   = f" +{len(agent.tools) - 6}" if len(agent.tools) > 6 else ""
        table.add_row(
            agent.role,
            ", ".join(tool_names) + overflow,
            agent.goal[:70],
        )

    console.print(table)
    console.input("\nPress Enter to return...")

def search_missions(query: str):
    query   = query.lower().strip()
    matches = [
        t for t in manager.list_tasks()
        if query in t.mission.lower() or query in t.status.lower()
    ]
    if not matches:
        console.print(f"[dim]No missions matching '{query}'.[/dim]")
    else:
        table = Table(header_style="bold magenta", expand=True, box=None)
        table.add_column("ID",      style="dim",  width=4)
        table.add_column("Status",                width=14)
        table.add_column("Mission", style="cyan")
        for t in matches:
            table.add_row(t.id, t.status, t.mission[:80])
        console.print(table)
    console.input("\nPress Enter to continue...")

# ==============================================================================
# GRACEFUL SHUTDOWN
# ==============================================================================
def shutdown():
    logger.info("Shutdown initiated")
    console.print("\n[bold red]⚡ SHUTDOWN INITIATED...[/bold red]")
    interrupted = 0
    for task in manager.list_tasks():
        if task.status == "RUNNING":
            task.status      = "INTERRUPTED"
            task.is_complete = True
            task.save_history_to_disk()
            interrupted += 1
    save_session()
    if interrupted:
        console.print(f"[yellow]⚠️  Interrupted {interrupted} mission(s) — state saved.[/yellow]")
        logger.info(f"Interrupted {interrupted} running missions")
    console.print("[dim]Empire state preserved. Goodbye, Overlord.[/dim]")
    sys.exit(0)

# ==============================================================================
# HEADLESS MISSION RUNNER
# ==============================================================================
def run_mission_headless(mission: str, priority: str = "normal", mcp_tools: list = None):
    logger.info(f"Running headless mission: {mission[:80]}")
    console.print(f"[bold blue]🚀 Running headless mission:[/bold blue] {mission[:80]}")
    task_id = start_chat_mission(
        mission,
        priority=priority,
        mcp_tools=mcp_tools or [],
        thread_id="headless",
    )
    console.print(f"[green]✅ Mission #{task_id} started.[/green]")
    task = manager.get_task(task_id)
    while not task.is_complete:
        time.sleep(0.5)
    console.print(Panel(
        Markdown(str(task.result)) if task.result else "[dim]No result captured.[/dim]",
        title="📝 HEADLESS MISSION COMPLETE",
        border_style="green",
    ))
    save_session()
    sys.exit(0)

# ==============================================================================
# CONSOLE REPLY POLLER
# ==============================================================================
def poll_inbox_replies(inbox_db, thread_id):
    """Background poller to print new OUT messages from the inbox."""
    last_id = 0
    while True:
        try:
            out_msgs = inbox_db.get_out_messages_since(thread_id, last_id)
            for msg in out_msgs:
                console.print(f"[bold green]CEO:[/bold green] {msg['body']}")
                last_id = msg['id']
                inbox_db.mark_out_delivered(msg['id'])
                logger.info(f"Delivered OUT message #{msg['id']} to console")
        except Exception as e:
            logger.error(f"Console poller error: {e}")
        time.sleep(1)

# ==============================================================================
# AGENTBUS NOTIFIER — wakes the CEO when a worker replies
# ==============================================================================
def _start_agent_bus_notifier():
    """
    Start the AgentBus notifier for all threads the CEO listens on.

    When an agent (spawned via DELEGATE) finishes, it writes an [AGENT_REPLY]
    to the parent thread. The notifier picks it up and calls the wake callback,
    which spawns a fresh assistant mission so the CEO can deliver the result
    to the user.
    """
    try:
        from orchestration.agent_bus import AgentBus
    except Exception as e:
        logger.error(f"AgentBus not available: {e}")
        return None

    agent_bus = AgentBus(
        inbox_db_path=INBOX_DB_PATH,
        workspace_root=os.getcwd(),
        task_manager=manager,
    )

    def _wake_ceo_for_agent_reply(thread_id: str, message_id: int):
        logger.info(
            f"AgentBus notifier: waking CEO for [AGENT_REPLY] "
            f"on thread '{thread_id}' msg #{message_id}"
        )
        try:
            start_chat_mission(
                raw_mission=(
                    f"An agent has finished work you delegated. "
                    f"Read inbox message #{message_id} in thread '{thread_id}', "
                    f"then reply to the user with the result."
                ),
                priority="high",
                thread_id=thread_id,
            )
        except Exception as e:
            logger.error(f"Failed to wake CEO for agent reply: {e}")

    # Watch threads — console is always present. Telegram workers register
    # their own notifier (see org_bot_worker.py) with the tg_<org> thread.
    watch_threads = ["console"]

    agent_bus.start_notifier(
        watch_threads=watch_threads,
        wake_callback=_wake_ceo_for_agent_reply,
    )
    logger.info(f"AgentBus notifier started (watching: {watch_threads})")
    return agent_bus

# ==============================================================================
# MAIN LOOP (Chat Interface)
# ==============================================================================
def _main_loop(github_tools: list):
    load_session()
    inbox_db = InboxDB(INBOX_DB_PATH)

    # Console reply poller
    threading.Thread(
        target=poll_inbox_replies,
        args=(inbox_db, "console"),
        daemon=True,
    ).start()
    logger.info("Console poller started")

    # AgentBus notifier — watches for [AGENT_REPLY] on the console thread
    _start_agent_bus_notifier()

    while True:
        show_dashboard()
        try:
            raw = console.input("\n[bold white]YOU > [/bold white]").strip()
            if not raw:
                continue

            lower = raw.lower()
            logger.debug(f"User input: {raw[:100]}")

            if lower == "exit":
                shutdown()
            elif lower.startswith("view "):
                parts = raw.split()
                if len(parts) >= 2:
                    view_task_live(parts[1])
                continue
            elif lower == "parallel":
                view_parallel()
                continue
            elif lower == "status":
                show_status()
                continue
            elif lower == "roster":
                show_roster(github_tools)
                continue
            elif lower == "templates":
                table = Table(
                    title="📋 MISSION TEMPLATES",
                    header_style="bold magenta",
                    expand=True,
                )
                table.add_column("Key",     style="cyan", width=12)
                table.add_column("Preview", style="dim")
                for key, text in MISSION_TEMPLATES.items():
                    table.add_row(key, text[:80] + "...")
                console.print(table)
                console.print("[dim]Launch with: !use <key>[/dim]")
                console.input("\nPress Enter to continue...")
                continue
            elif lower.startswith("!use "):
                key = raw[5:].strip().lower()
                if key not in MISSION_TEMPLATES:
                    console.print(
                        f"[red]Unknown template '{key}'. Run 'templates' to see options.[/red]"
                    )
                    time.sleep(1.5)
                    continue
                mission = MISSION_TEMPLATES[key]
                new_id = start_chat_mission(
                    mission,
                    priority="high",
                    mcp_tools=github_tools,
                    thread_id="console",
                )
                save_session()
                console.print(
                    f"[green]🚀 Mission #{new_id} launched from template '{key}'![/green]"
                )
                time.sleep(0.8)
                view_task_live(new_id)
                continue
            elif lower.startswith("search "):
                search_missions(raw[7:])
                continue
            else:
                # Direct mission creation — no ChatHandler
                if is_greeting(raw):
                    reply_text = "Hello! How can I assist you today?"
                    send_instant_reply("console", reply_text)
                    console.print(f"[bold green]CEO:[/bold green] {reply_text}")
                    logger.info(f"Instant greeting reply sent for: {raw[:50]}")
                else:
                    console.print("[dim]🏛️  CEO is processing...[/dim]")
                    logger.info(f"Processing user message: {raw[:100]}")
                    task_id = start_chat_mission(
                        raw,
                        thread_id="console",
                        mcp_tools=github_tools,
                    )
                    console.print(
                        f"[dim]Mission #{task_id} started. "
                        f"You'll be notified when the CEO replies.[/dim]"
                    )

        except KeyboardInterrupt:
            shutdown()
        except Exception as e:
            logger.exception(f"System error in main loop: {e}")
            console.print(f"[bold red]SYSTEM ERROR: {e}[/bold red]")
            time.sleep(2)

# ==============================================================================
# ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    clear()
    console.print(Panel.fit(
        "[bold red]⚔️  GLOBAL DOMINANCE SYSTEM BOOTING...[/bold red]",
        border_style="dim red",
    ))
    logger.info("Empire system booting")

    console.print("[dim]⏳ Loading AI libraries (litellm, crewai, sentence-transformers)...[/dim]")
    console.print("[dim]   This takes 20-60 seconds on first run. Do NOT press Ctrl+C.[/dim]")

    scheduler.start()
    console.print("[dim]📅 Task scheduler started.[/dim]")
    logger.info("Task scheduler started")

    if len(sys.argv) > 1:
        known_commands = {"view", "parallel", "status", "roster", "templates", "!use", "search", "exit"}
        if sys.argv[1].lower() not in known_commands:
            mission = " ".join(sys.argv[1:])
            console.print(f"[dim]🧠 Headless mode: running mission: {mission[:80]}...[/dim]")
            run_mission_headless(mission)
            sys.exit(0)

    console.print("[dim]🔌 Starting interactive mode...[/dim]")
    logger.info("Starting interactive mode")
    try:
        _main_loop([])
    except KeyboardInterrupt:
        shutdown()
