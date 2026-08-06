# gm.py (Zero‑Trust Tool Segregation)
import os
import sys
import time
import json
import logging
import threading
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
]:
    if not os.path.exists(folder):
        os.makedirs(folder)
        console.print(f"[dim]📁 Created: {folder}[/dim]")

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

all_empire_tools = _load_empire_tools()

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

# ── Tool classification by normalized tool name ───────────────────────────
CEO_TOOL_NAMES = {
    "list_directory",
    "inspect_code",           # 'map' mode for structural overview
    "search_mission_logs",
    "search_library",
    "spawn_agent",            # Spawn Specialist
    "consult_overlord",
}

# QA Engineer always has file‑reading and verification tools
QA_TOOL_NAMES = {
    "manage_file",            # Read (and temporary write) for verification
    "execute_terminal",       # Running test suites / compilers
    "inspect_code",           # 'extract' / 'section' mode for deep checks
    "commit_to_library",
}

def _filter_tools_by_names(tools_list: list, allowed_names: set) -> list:
    """Return only the tools whose normalized name is in allowed_names."""
    filtered = []
    for t in tools_list:
        t_name = getattr(t, 'name', '').lower().replace(' ', '_')
        if t_name in allowed_names:
            filtered.append(t)
    return filtered


def get_population(mcp_tools: list = None) -> list:
    if mcp_tools is None:
        mcp_tools = []

    all_available = all_empire_tools + mcp_tools
    agents = []

    # ── 👑 THE GLOBAL CEO ──
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

🚨 DIRECTIVE 3 — ZERO HALLUCINATION:
Never assume a file exists. Use 'List Directory' to confirm paths before any operation.

🚨 DIRECTIVE 4 — COMPILER SEMANTICS:
For tsc, rustc, go build — ZERO OUTPUT = ZERO ERRORS = SUCCESS.
Move to the next goal once verified.

🚨 DIRECTIVE 5 — WORKER MEMORY LAW:
Workers remember their last 3 task summaries AND their last raw command output.
They do NOT have full terminal history.""",
        tools=ceo_tools
    )
    agents.append(emperor)

    # ── 🛡️ QUALITY ASSURANCE ENGINEER ──
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

    # ── 🧬 DYNAMIC DNA-LOADED SUBORDINATES ──
    json_files = [f for f in os.listdir(CIVILIZATION_DIR) if f.endswith(".json")]
    for filename in json_files:
        try:
            with open(os.path.join(CIVILIZATION_DIR, filename), "r", encoding="utf-8") as f:
                dna = json.load(f)

            if dna.get("status") != "ACTIVE":
                continue

            agent_specific_tools = []
            # Load tools explicitly listed in the DNA capabilities
            for cap in dna.get("capabilities", []):
                normalized_cap = cap.lower().replace(' ', '_')
                if normalized_cap in TOOL_REGISTRY:
                    agent_specific_tools.append(TOOL_REGISTRY[normalized_cap])

            # External MCP tools (GitHub, etc.) remain available to execution workers
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
# 8. MISSION PROMPT BUILDER (Conditional — no bloat on simple tasks)
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
# 9. UI HELPERS
# ==============================================================================
def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def show_dashboard():
    clear()
    console.print(Panel.fit(
        "[bold red]⚔️  GLOBAL DOMINANCE SYSTEM  ⚔️[/bold red]\n"
        "[dim]AGI Director — Native Architecture v4[/dim]",
        border_style="red"
    ))

    table = Table(show_header=True, header_style="bold magenta", expand=True, box=None)
    table.add_column("ID",      style="dim",   width=4)
    table.add_column("Time",                   width=8)
    table.add_column("Status",                 width=18)
    table.add_column("Mission", style="cyan")

    tasks = manager.list_tasks()
    if not tasks:
        table.add_row("-", "-", "[dim]IDLE[/dim]", "[dim]No active missions[/dim]")
    else:
        for t in tasks:
            if   t.status == "COMPLETED":         style = "bold green"
            elif t.status == "RUNNING":            style = "bold yellow"
            elif t.status == "AWAITING_OVERLORD":  style = "bold cyan"
            elif t.status == "INTERRUPTED":        style = "bold red"
            else:                                  style = "bold white"
            table.add_row(
                t.id, t.timestamp,
                f"[{style}]{t.status}[/{style}]",
                t.mission[:65]
            )

    console.print(table)
    console.print()
    console.print("[bold]COMMANDS:[/bold]")
    console.print("  [green]new <mission>[/green]          Start a new mission")
    console.print("  [green]!high <mission>[/green]        Start with full safety protocols")
    console.print("  [green]!critical <mission>[/green]    Start with max priority")
    console.print("  [cyan]view <id>[/cyan]               Live feed + intervention")
    console.print("  [cyan]parallel[/cyan]                View all running missions")
    console.print("  [cyan]roster[/cyan]                  Show agent population")
    console.print("  [cyan]templates[/cyan]              List mission templates")
    console.print("  [cyan]!use <template>[/cyan]         Launch a template mission")
    console.print("  [cyan]search <query>[/cyan]          Search mission history")
    console.print("  [cyan]!![/cyan]                     Repeat last mission")
    console.print("  [red]exit[/red]                  Save state and shutdown")

# ==============================================================================
# 10. LIVE TASK VIEWER
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

            # Auto-prompt when CEO is waiting for Overlord
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
                console.print("\n[dim]Press Enter to return to dashboard...[/dim]")
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
# 11. PARALLEL MISSION VIEWER
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
# 12. AGENT ROSTER VIEWER
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
# 13. MISSION SEARCH
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
# 14. GRACEFUL SHUTDOWN
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
# 15. MAIN LOOP
# ==============================================================================
def _main_loop(github_tools: list):
    last_mission = ""
    load_session()

    while True:
        show_dashboard()
        try:
            raw   = console.input("\n[bold white]OVERLORD > [/bold white]").strip()
            if not raw:
                continue

            cmd   = raw
            lower = cmd.lower()

            # ── EXIT ──
            if lower == "exit":
                shutdown()

            # ── REPEAT LAST ──
            elif lower == "!!":
                if last_mission:
                    cmd   = f"new {last_mission}"
                    lower = cmd.lower()
                else:
                    console.print("[dim]No previous mission to repeat.[/dim]")
                    time.sleep(1)
                    continue

            # ── VIEW TASK ──
            if lower.startswith("view "):
                parts = cmd.split()
                if len(parts) >= 2:
                    view_task_live(parts[1])
                continue

            # ── PARALLEL VIEW ──
            elif lower == "parallel":
                view_parallel()
                continue

            # ── ROSTER ──
            elif lower == "roster":
                show_roster(github_tools)
                continue

            # ── TEMPLATES ──
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

            # ── USE TEMPLATE ──
            elif lower.startswith("!use "):
                key = cmd[5:].strip().lower()
                if key not in MISSION_TEMPLATES:
                    console.print(f"[red]Unknown template '{key}'. Run 'templates' to see options.[/red]")
                    time.sleep(1.5)
                    continue
                mission      = MISSION_TEMPLATES[key]
                full_mission = build_mission_prompt(mission, priority="high")
                last_mission = mission
                population   = get_population(mcp_tools=github_tools)
                new_id       = manager.start_mission(full_mission, population)
                save_session()
                console.print(f"[green]🚀 Mission #{new_id} launched from template '{key}'![/green]")
                time.sleep(0.8)
                view_task_live(new_id)
                continue

            # ── SEARCH ──
            elif lower.startswith("search "):
                search_missions(cmd[7:])
                continue

            # ── NEW MISSION ──
            elif (
                lower.startswith("new ")
                or lower.startswith("!high ")
                or lower.startswith("!critical ")
            ):
                if lower.startswith("new "):
                    priority = "normal"
                    mission  = cmd[4:]
                elif lower.startswith("!high "):
                    priority = "high"
                    mission  = cmd[6:]
                else:
                    priority = "critical"
                    mission  = cmd[10:]

                mission      = mission.strip()
                full_mission = build_mission_prompt(mission, priority=priority)
                last_mission = mission

                console.print("[dim]🏛️  Assembling Empire population...[/dim]")
                population = get_population(mcp_tools=github_tools)
                console.print(f"[dim]👥 {len(population)} agents ready.[/dim]")

                new_id = manager.start_mission(full_mission, population)
                save_session()
                console.print(
                    f"[green]🚀 Mission #{new_id} launched! (Priority: {priority.upper()})[/green]"
                )
                time.sleep(0.8)
                view_task_live(new_id)

            else:
                console.print(
                    f"[dim red]Unknown command: '{cmd}'. "
                    f"Type 'new <mission>' to start or 'exit' to quit.[/dim red]"
                )
                time.sleep(1.5)

        except KeyboardInterrupt:
            shutdown()
        except Exception as e:
            console.print(f"[bold red]SYSTEM ERROR: {e}[/bold red]")
            time.sleep(2)


# ==============================================================================
# 16. ENTRYPOINT (MCP Graceful Degradation)
# ==============================================================================
if __name__ == "__main__":
    clear()
    console.print(Panel.fit(
        "[bold red]⚔️  GLOBAL DOMINANCE SYSTEM BOOTING...[/bold red]",
        border_style="dim red"
    ))

    # ── Heavy imports deferred here so the console appears immediately ──
    console.print("[dim]⏳ Loading AI libraries (litellm, crewai, sentence-transformers)...[/dim]")
    console.print("[dim]   This takes 20-60 seconds on first run. Do NOT press Ctrl+C.[/dim]")

    try:
        from mcp import StdioServerParameters
        from crewai_tools import MCPServerAdapter
        console.print("[dim]✅ Libraries loaded.[/dim]")
    except Exception as import_err:
        console.print(f"[yellow]⚠️  MCP import failed: {import_err}\nRunning without GitHub tools.[/yellow]")
        StdioServerParameters = None
        MCPServerAdapter      = None

    console.print("[dim]🔌 Connecting to GitHub MCP Server...[/dim]")

    if StdioServerParameters is None or MCPServerAdapter is None:
        console.print("[yellow]⚠️  Running in degraded mode (no GitHub tools).[/yellow]")
        time.sleep(1)
        try:
            _main_loop([])
        except KeyboardInterrupt:
            shutdown()
    else:
        github_params = StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={
                "GITHUB_PERSONAL_ACCESS_TOKEN": os.getenv("ai_mcp", ""),
                "PATH": os.getenv("PATH", "")
            }
        )

        try:
            with MCPServerAdapter(github_params) as github_tools:
                console.print(
                    f"[green]✅ MCP Connected — {len(github_tools)} GitHub tools injected.[/green]"
                )
                time.sleep(1)
                _main_loop(list(github_tools))

        except KeyboardInterrupt:
            shutdown()

        except Exception as mcp_err:
            console.print(
                f"[yellow]⚠️  GitHub MCP unavailable: {mcp_err}\n"
                f"Running in degraded mode (no GitHub tools).[/yellow]"
            )
            time.sleep(2)
            try:
                _main_loop([])
            except KeyboardInterrupt:
                shutdown()
