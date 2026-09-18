# gm.py (Zero‑Trust Tool Segregation)
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

# 🔌 MCP IMPORTS — deferred to __main__ to avoid blocking console startup
# from mcp import StdioServerParameters
# from crewai_tools import MCPServerAdapter

# CORE IMPORTS
from empire_tools import EmpireTools
from task_manager import TaskManager
from orchestration.inbox_db import InboxDB
from orchestration.dynamic_tools import load_dynamic_tools
from orchestration.scheduler_db import SchedulerDB
from orchestration.scheduler import TaskScheduler
from orchestration.mcp_manager import load_mcp_tools      # <-- NEW
from tools.scheduler_tools import set_scheduler_db

# ==============================================================================
# 1. SILENCE CREWAI TELEMETRY SPAM BEFORE ANYTHING ELSE
# ==============================================================================
logging.getLogger("crewai").setLevel(logging.ERROR)
logging.getLogger("posthog").setLevel(logging.CRITICAL)
logging.getLogger("chromadb").setLevel(logging.ERROR)

# ==============================================================================
# 2. CONFIGURATION & INITIALIZATION
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
]:
    if not os.path.exists(folder):
        os.makedirs(folder)
        console.print(f"[dim]📁 Created: {folder}[/dim]")

# ── Task Scheduler Initialization ──
SCHEDULER_DB_PATH = os.path.join(CIVILIZATION_DIR, "scheduler.db")
scheduler_db = SchedulerDB(SCHEDULER_DB_PATH)
set_scheduler_db(scheduler_db)
scheduler = TaskScheduler(SCHEDULER_DB_PATH, thread_id="scheduler")

# ==============================================================================
# 3. MISSION TEMPLATES
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
# 4. NATIVE AGENT CLASS
# ==============================================================================
class NativeAgent:
    def __init__(self, role: str, goal: str, backstory: str, tools: list = None):
        self.role          = role
        self.goal          = goal
        self.backstory     = f"{goal}\n\n{backstory}"
        self.tools         = tools or []
        self.step_callback = None  # Attached by TaskManager

# ==============================================================================
# 5. TOOL LOADING (Proper @tool detection)
# ==============================================================================
def _load_empire_tools() -> list:
    """Load all @tool-decorated methods from EmpireTools class."""
    tools = []
    for method_name in dir(EmpireTools):
        if method_name.startswith("_"):
            continue
        obj = getattr(EmpireTools, method_name)
        if hasattr(obj, 'name') and hasattr(obj, 'description'):
            tools.append(obj)
    return tools

def _load_dynamic_tools() -> list:
    """Load tools from the tenant's dynamic_tools directory."""
    dynamic_dir = os.path.join(CIVILIZATION_DIR, "dynamic_tools")
    return load_dynamic_tools(dynamic_dir)

# Combine static, dynamic, and MCP tools
all_empire_tools = _load_empire_tools() + _load_dynamic_tools() + load_mcp_tools()

# Build TOOL_REGISTRY from actual tool objects (not broken imports)
TOOL_REGISTRY: dict = {
    getattr(t, 'name', '').lower().replace(' ', '_'): t
    for t in all_empire_tools
    if hasattr(t, 'name')
}

# ==============================================================================
# 6. SESSION PERSISTENCE
# ==============================================================================
SESSION_PATH = os.path.join(CIVILIZATION_DIR, "session.json")

def save_session():
    try:
        snapshot = {
            tid: {
                "mission":        t.mission,
                "status":         t.status,
                "timestamp":      t.timestamp,
                "result_preview": str(t.result)[:200] if t.result else None
            }
            for tid, t in manager.tasks.items()
        }
        with open(SESSION_PATH, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        console.print(f"[dim red]Session save error: {e}[/dim red]")

def load_session():
    if not os.path.exists(SESSION_PATH):
        return
    try:
        with open(SESSION_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data:
            console.print(f"[dim]📂 Restored {len(data)} missions from last session.[/dim]")
    except Exception:
        pass

# ==============================================================================
# 7. POPULATION MANAGEMENT (ROLE-BASED TOOL SEGREGATION)
# ==============================================================================

CEO_TOOL_NAMES = {
    "list_directory",
    "inspect_code",
    "search_mission_logs",
    "search_library",
    "spawn_agent",
    "consult_overlord",
    "read_inbox",
    "get_new_inbox_messages",
    "send_user_message",
    "ask_user",
    # REPL and live research tools
    "search_web",
    "scrape_webpage",
    "query_docs",
    "execute_repl",
    # Secret management tools
    "set_secret",
    "get_secret",
    "list_secret_keys",
    "delete_secret",
    # Scheduler / project tools
    "add_project",
    "list_projects",
    "add_task",
    "list_tasks",
    "complete_task",
    "cancel_task",
}

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
    """Run Python code in a subprocess with timeout and output truncation."""
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
            env={**os.environ, "HTTP_PROXY": "", "HTTPS_PROXY": "", "NO_PROXY": "*"}
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            output += f"\n[ERROR] {result.stderr.strip()}"
        if len(output) > max_output:
            output = output[:max_output] + "\n...[TRUNCATED]"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Execution timed out after 5 seconds."
    finally:
        if os.path.exists(script_path):
            os.unlink(script_path)

def get_population(mcp_tools: list = None) -> list:
    if mcp_tools is None:
        mcp_tools = []

    # all_empire_tools already includes MCP tools, so we don't need to add them again
    all_available = all_empire_tools
    agents = []

    # CEO
    ceo_tools = _filter_tools_by_names(all_available, CEO_TOOL_NAMES)
    emperor = NativeAgent(
        role="The Global CEO",
        goal=(
            "Operate as a God-Tier Principal Staff Engineer. "
            "Translate Overlord intent into deterministic, flawless execution and orchestrate workers."
        ),
        backstory="""You are the Supreme Intelligence of a rising Technocratic Empire.

🚨 DIRECTIVE 1 — ARCHITECTURAL ORCHESTRATION:
You plan, scope, and delegate. You do NOT perform raw file edits or execute unverified shell scripts.
Use 'Inspect Code' (map mode) and 'List Directory' to map systems before delegating.

🚨 DIRECTIVE 2 — DYNAMIC PIVOTING:
If an agent fails repeatedly, DO NOT repeat the same command.
Invent a new technical vector. If 3 pivots fail, use 'Consult Overlord'. Never guess.

🚨 DIRECTIVE 3 — ZERO HALLUCINATION & FACT‑CHECKING:
Never assume a fact. For ANY factual answer (URLs, phone numbers, code details, dates, etc.), you MUST verify using available tools:
   - Use 'Internet Search' or 'Scrape Webpage' to confirm web links or public information.
   - Use 'Inspect Code' or 'List Directory' to verify internal files.
   - Use 'EXECUTE_REPL' to run Python code that retrieves the exact answer (e.g., query DB, parse file).
   When replying, cite your source or methodology (e.g., "I found this in file X via Inspect Code" or "I ran Python script to query the database and got: ...").

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

🚨 DIRECTIVE 7 — DELEGATION BOUNDARY:
- Use EXECUTE_REPL for read‑only exploration, data extraction, and verification.
- Use DELEGATE for state‑changing work: writing code, editing files, running migrations, building features.

🚨 DIRECTIVE 8 — EXTENDING THE EMPIRE:
You may delegate to the 'Tool Builder' to create new Python tools saved in 'ai_civilization/dynamic_tools'.
You have secret management tools and a built-in scheduler:
  - Use 'add_project' to group related tasks.
  - Use 'add_task' to schedule future work, with optional 'project_id' and 'depends_on_task_id'.
  - The scheduler runs in the background and will wake you when tasks are due or dependencies are satisfied.
""",
        tools=ceo_tools
    )
    agents.append(emperor)

    # QA
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
        tools=qa_tools
    )
    agents.append(qa_agent)

    # Tool Builder
    tool_builder_tools = _filter_tools_by_names(all_available, TOOL_BUILDER_TOOL_NAMES)
    tool_builder = NativeAgent(
        role="Tool Builder",
        goal="Build and register new Python tools for the empire's dynamic tool directory.",
        backstory="""You create reusable tools that other agents can use. Write @tool-decorated functions and save them in the 'ai_civilization/dynamic_tools' directory. After saving, the tool becomes available in future missions automatically.""",
        tools=tool_builder_tools
    )
    agents.append(tool_builder)

    # DNA-loaded agents
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

            # DNA agents automatically get all MCP tools
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
                tools=agent_specific_tools
            )
            agents.append(sub_agent)

        except Exception as e:
            console.print(f"[dim red]⚠️ Failed to load '{filename}': {e}[/dim red]")
            continue

    return agents

# ==============================================================================
# 8. MISSION PROMPT BUILDER
# ==============================================================================
DESTRUCTIVE_KEYWORDS = {
    "deploy", "delete", "remove", "drop", "modify", "update", "fix",
    "patch", "migrate", "refactor", "overwrite", "replace", "install"
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
# 9. START CHAT MISSION (Inbox-aware)
# ==============================================================================
def start_chat_mission(raw_mission: str, priority: str = "normal", mcp_tools: list = None, thread_id: str = "console"):
    """
    Create an inbox item, enrich the mission text with message_id and chat instructions,
    then start a mission via TaskManager. Returns task_id.
    """
    inbox_db_path = os.path.join(os.getcwd(), "ai_civilization", "inbox.db")
    inbox_db = InboxDB(inbox_db_path)

    msg_id = inbox_db.add_message(
        thread_id=thread_id,
        direction="IN",
        body=raw_mission,
        sender="user",
        recipient="CEO",
        status="NEW"
    )

    mission_text = (
        f"Process inbox message #{msg_id} in thread {thread_id}.\n"
        f"User message: {raw_mission}\n"
        f"Instructions:\n"
        f"- This is a chat message from the user.\n"
        f"- If the message is simple or conversational, reply directly using SEND_REPLY with message_id={msg_id} and your reply body.\n"
        f"- If the user is asking for a complex task, you may delegate work, but your final output must be a reply via SEND_REPLY.\n"
        f"- Do NOT define products, create plans, or run terminal commands unless the user explicitly requests a technical task.\n"
        f"- For factual queries, you MUST use EXECUTE_REPL, Internet Search, or Inspect Code to verify before replying."
    )

    full_mission = build_mission_prompt(mission_text, priority=priority)
    population = get_population(mcp_tools=mcp_tools or [])
    task_id = manager.start_mission(full_mission, population)
    return task_id

# ==============================================================================
# 10. UI HELPERS
# ==============================================================================
def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def show_dashboard():
    clear()
    console.print(Panel.fit(
        "[bold red]⚔️  GLOBAL DOMINANCE SYSTEM  ⚔️[/bold red]\n"
        "[dim]Chat with the CEO — just type a message[/dim]",
        border_style="red"
    ))
    console.print("[dim]Commands:[/dim]")
    console.print("  [cyan]view <id>[/cyan]        View a mission log")
    console.print("  [cyan]parallel[/cyan]         View all running missions")
    console.print("  [cyan]roster[/cyan]           Show agent population")
    console.print("  [cyan]templates[/cyan]       List mission templates")
    console.print("  [cyan]!use <template>[/cyan]  Launch a template")
    console.print("  [cyan]search <query>[/cyan]   Search mission history")
    console.print("  [red]exit[/red]              Save and shutdown")
    console.print()
    console.print("[bold]Type your message and press Enter.[/bold]")

# ==============================================================================
# 11. LIVE TASK VIEWER (unchanged)
# ==============================================================================
def view_task_live(task_id: str):
    task = manager.get_task(task_id)
    if not task:
        console.print("[red]❌ Task ID not found.[/red]")
        time.sleep(1)
        return

    current_log_index = 0
    clear()
    console.print(Panel(
        f"[bold yellow]📺 MISSION #{task_id}[/bold yellow]\n[dim]{task.mission[:100]}[/dim]",
        border_style="yellow"
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
                except KeyboardInterrupt:
                    pass

            if task.is_complete:
                console.print()
                if task.result:
                    console.print(Panel(
                        Markdown(str(task.result)),
                        title="📝 MISSION COMPLETE — INTELLIGENCE REPORT",
                        border_style="green"
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
                time.sleep(0.5)
                console.print("[dim]▶️  Resuming...[/dim]")
                view_task_live(task_id)
        except KeyboardInterrupt:
            return

# ==============================================================================
# 12. PARALLEL MISSION VIEWER (unchanged)
# ==============================================================================
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
        border_style="yellow"
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
                        f"\n[bold magenta]══ Mission #{task.id}: {task.mission[:50]} ══[/bold magenta]"
                    )
                    for log in new_logs[-4:]:
                        console.print(log)
                    log_indices[task.id] = len(task.logs)
            time.sleep(1.5)

    except KeyboardInterrupt:
        return

# ==============================================================================
# 13. AGENT ROSTER VIEWER (unchanged)
# ==============================================================================
def show_roster(mcp_tools: list = None):
    population = get_population(mcp_tools=mcp_tools or [])
    table = Table(
        title="🧬 EMPIRE POPULATION",
        header_style="bold magenta",
        expand=True,
        show_lines=True
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
            agent.goal[:70]
        )

    console.print(table)
    console.input("\nPress Enter to return...")

# ==============================================================================
# 14. MISSION SEARCH (unchanged)
# ==============================================================================
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
# 15. GRACEFUL SHUTDOWN (unchanged)
# ==============================================================================
def shutdown():
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
    console.print("[dim]Empire state preserved. Goodbye, Overlord.[/dim]")
    sys.exit(0)

# ==============================================================================
# 16. HEADLESS MISSION RUNNER (unchanged)
# ==============================================================================
def run_mission_headless(mission: str, priority: str = "normal", mcp_tools: list = None):
    console.print(f"[bold blue]🚀 Running headless mission:[/bold blue] {mission[:80]}")
    task_id = start_chat_mission(mission, priority=priority, mcp_tools=mcp_tools or [], thread_id="headless")
    console.print(f"[green]✅ Mission #{task_id} started.[/green]")
    task = manager.get_task(task_id)
    while not task.is_complete:
        time.sleep(0.5)
    console.print(Panel(
        Markdown(str(task.result)) if task.result else "[dim]No result captured.[/dim]",
        title="📝 HEADLESS MISSION COMPLETE",
        border_style="green"
    ))
    save_session()
    sys.exit(0)

# ==============================================================================
# 17. MAIN LOOP (Chat Interface) (unchanged)
# ==============================================================================
def _main_loop(github_tools: list):
    load_session()

    while True:
        show_dashboard()
        try:
            raw = console.input("\n[bold white]YOU > [/bold white]").strip()
            if not raw:
                continue

            lower = raw.lower()

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
            elif lower == "roster":
                show_roster(github_tools)
                continue
            elif lower == "templates":
                table = Table(
                    title="📋 MISSION TEMPLATES",
                    header_style="bold magenta",
                    expand=True
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
                    console.print(f"[red]Unknown template '{key}'. Run 'templates' to see options.[/red]")
                    time.sleep(1.5)
                    continue
                mission = MISSION_TEMPLATES[key]
                new_id = start_chat_mission(mission, priority="high", mcp_tools=github_tools, thread_id="console")
                save_session()
                console.print(f"[green]🚀 Mission #{new_id} launched from template '{key}'![/green]")
                time.sleep(0.8)
                view_task_live(new_id)
                continue
            elif lower.startswith("search "):
                search_missions(raw[7:])
                continue
            else:
                console.print("[dim]🏛️  CEO is processing...[/dim]")
                new_id = start_chat_mission(raw, priority="normal", mcp_tools=github_tools, thread_id="console")
                save_session()
                console.print(f"[green]✅ Mission #{new_id} started.[/green]")
                time.sleep(0.5)
                view_task_live(new_id)

        except KeyboardInterrupt:
            shutdown()
        except Exception as e:
            console.print(f"[bold red]SYSTEM ERROR: {e}[/bold red]")
            time.sleep(2)

# ==============================================================================
# 18. ENTRYPOINT (Interactive + Headless) (simplified)
# ==============================================================================
if __name__ == "__main__":
    clear()
    console.print(Panel.fit(
        "[bold red]⚔️  GLOBAL DOMINANCE SYSTEM BOOTING...[/bold red]",
        border_style="dim red"
    ))

    console.print("[dim]⏳ Loading AI libraries (litellm, crewai, sentence-transformers)...[/dim]")
    console.print("[dim]   This takes 20-60 seconds on first run. Do NOT press Ctrl+C.[/dim]")

    # Start scheduler in background
    scheduler.start()
    console.print("[dim]📅 Task scheduler started.[/dim]")

    # MCP tools are already loaded at import time; no need to connect here.

    if len(sys.argv) > 1:
        known_commands = {"view", "parallel", "roster", "templates", "!use", "search", "exit"}
        if sys.argv[1].lower() not in known_commands:
            mission = " ".join(sys.argv[1:])
            console.print(f"[dim]🧠 Headless mode: running mission: {mission[:80]}...[/dim]")
            run_mission_headless(mission)
            sys.exit(0)

    # Interactive mode
    console.print("[dim]🔌 Starting interactive mode...[/dim]")
    try:
        _main_loop([])   # tools are now global, no need to pass
    except KeyboardInterrupt:
        shutdown()
