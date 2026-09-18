# ceo_prompter.py
#
# Builds the CEO's decision prompt. Mission-type aware:
#   • assistant  — single user message, short-lived, read-first
#   • delegated  — worker spawned via AgentBus, replies to CEO
#   • scheduled  — fired by the scheduler
#   • standalone — headless CLI mission
#
# The prompt is structured as:
#   1. Header / mission objective
#   2. Inbox thread + RESPONSE DISCIPLINE
#   3. Mode-specific instruction block
#   4. SYSTEM MAP (how the CEO inspects his own universe)
#   5. Laws of Orchestration
#   6. Workspace / file access
#   7. Environment recon / workspace map / plan / recent results / roster / tools
#   8. Dynamic system status
#   9. Forced decision (if any)
#  10. Recent actions
#  11. Strict JSON output schema
#
# ─────────────────────────────────────────────────────────────────────────────
# TOOL CATALOG SOURCE OF TRUTH (fix)
# ─────────────────────────────────────────────────────────────────────────────
# The catalog and the MCP status line are now BUILT FROM THE CEO AGENT'S
# `.tools` LIST — the same list the runtime dispatches CALL_TOOL against.
# Previously the catalog came from `orchestration.role_tools.TOOL_REGISTRY`,
# which did NOT include MCP tools loaded by gm.py. That produced a prompt
# where LAW 2 said "MCP is online" but the FULL TOOL CATALOG listed zero MCP
# tools — the LLM trusted the table, went into reconnaissance mode, and never
# actually called an MCP tool. Deriving both from `agents` makes that
# mismatch structurally impossible.
# ─────────────────────────────────────────────────────────────────────────────

import os
from typing import List, Any, Optional, Dict
from datetime import datetime

from ceo_state import CEOScratchpad, SharedState

# NOTE: `orchestration.role_tools.TOOL_REGISTRY` is intentionally no longer
# used for the catalog. It is kept imported only for backward compatibility
# (other modules may import it via this module).
from orchestration.role_tools import TOOL_REGISTRY  # noqa: F401


# ══════════════════════════════════════════════════════════════════════════════
# Static blocks — small helpers
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_tool_name(name: str) -> str:
    """Match the dispatcher's normalisation: lowercase, spaces → underscores."""
    return (name or "").lower().replace(" ", "_")


def _make_tools_block(registry, title="AVAILABLE WORKER EQUIPMENT"):
    """
    Legacy helper — builds a catalog from a {name: tool} dict.

    Kept for callers that already hold a registry. The CEO prompt itself no
    longer uses this; it uses _make_tools_block_from_agents() so the catalog
    is derived from the exact list the runtime will dispatch on.
    """
    lines = [
        f"━━━ {title} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "Tool Name            | Description",
        "---------------------|------------------------------------------------",
    ]
    for name, tool in sorted(registry.items()):
        desc = getattr(tool, 'description', '')
        short = desc.split('\n')[0][:77] + '...' if len(desc) > 80 else desc.split('\n')[0]
        lines.append(f"{name:<20} | {short}")
    return "\n".join(lines)


def _find_ceo_agent(agents: List[Any]) -> Optional[Any]:
    """
    Return the CEO agent from a population list.

    Matches by role string first (exact), then falls back to the first agent
    in the list. The list built by gm.py.get_population() always puts the
    Global CEO first, so the fallback is safe.
    """
    if not agents:
        return None
    for a in agents:
        if getattr(a, "role", "") == "The Global CEO":
            return a
    return agents[0]


def _is_mcp_tool(tool: Any) -> bool:
    """
    Heuristic: is this tool provided by an MCP server adapter?

    crewai_tools wraps MCP server tools as MCPTool-ish classes whose module
    path contains 'mcp' or 'mcpadapt'. Some versions also stash a server
    name on the instance. We check all signals.
    """
    try:
        mod = type(tool).__module__.lower()
        cls = type(tool).__name__.lower()
    except Exception:
        return False

    if "mcp" in mod or "mcpadapt" in mod:
        return True
    if "mcp" in cls:
        return True
    if getattr(tool, "server_name", None) or getattr(tool, "_mcp", False):
        return True
    return False


def _make_tools_block_from_agents(
    agents: List[Any],
    title: str = "FULL TOOL CATALOG",
) -> str:
    """
    Build the tool catalog directly from an agent's actual `.tools` list.

    This is the single source of truth: the catalog IS the list the runtime
    will dispatch CALL_TOOL against, so it cannot drift.

    Deduplicates by normalised name. Sorts alphabetically for a stable,
    byte-reproducible prompt.
    """
    ceo = _find_ceo_agent(agents)
    if ceo is None:
        return f"━━━ {title} ━━━\n(no agents available)"

    tools = getattr(ceo, "tools", None) or []
    if not tools:
        return (
            f"━━━ {title} ━━━\n"
            f"(no tools loaded for {getattr(ceo, 'role', 'agent')})"
        )

    # Deduplicate by normalised name; keep the first occurrence.
    seen: Dict[str, Any] = {}
    for t in tools:
        raw = getattr(t, "name", None) or type(t).__name__
        key = _normalize_tool_name(raw)
        if key and key not in seen:
            seen[key] = t

    lines = [
        f"━━━ {title} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "Tool Name            | Description",
        "---------------------|------------------------------------------------",
    ]

    # Alphabetical — stable output, easy to scan.
    for key in sorted(seen.keys()):
        tool = seen[key]
        desc = (getattr(tool, "description", "") or "").split("\n")[0]
        short = desc[:77] + "..." if len(desc) > 80 else desc
        lines.append(f"{key:<20} | {short}")

    return "\n".join(lines)


def _build_mcp_block(agents: List[Any]) -> str:
    """
    Build the MCP status line from the CEO's actual tool list.

    No fresh `load_mcp_tools()` call — we read the same list the runtime will
    dispatch on, so the prose and the catalog cannot disagree.
    """
    ceo = _find_ceo_agent(agents)
    if ceo is None:
        return "MCP status unknown (no agents loaded)."

    tools = getattr(ceo, "tools", None) or []
    mcp_names = sorted({
        _normalize_tool_name(getattr(t, "name", None) or type(t).__name__)
        for t in tools
        if _is_mcp_tool(t)
    })
    mcp_names = [n for n in mcp_names if n]

    if mcp_names:
        return (
            "MCP Servers are ONLINE and auto-connected at process start. "
            "You have direct access to these MCP tools: "
            + ", ".join(mcp_names)
            + ". There is NO 'connect_mcp' tool — if a name appears above, "
              "call it directly with CALL_TOOL."
        )
    return "MCP Servers are offline. Rely on standard Python tools."


def _build_live_plan(mission_db) -> str:
    """Extracts the live project plan from SQLite."""
    if not mission_db:
        return "No phases defined. Use UPDATE_PLAN to initialize the project."

    try:
        phases = mission_db.conn.execute(
            "SELECT id, title, status FROM phases ORDER BY id"
        ).fetchall()
    except Exception:
        return "Plan unavailable."

    if not phases:
        return "No phases defined yet. Use UPDATE_PLAN to create the first phase."

    lines = ["━━━ LIVE MISSION PLAN (SINGLE SOURCE OF TRUTH) ━━━"]
    for pid, title, status in phases:
        icon = "✅" if status == "COMPLETED" else "⏳"
        lines.append(f"[P{pid}] {title} - {icon} [{status}]")
        try:
            tasks = mission_db.conn.execute(
                "SELECT id, description, status FROM tasks WHERE phase_id = ? ORDER BY id",
                (pid,),
            ).fetchall()
        except Exception:
            tasks = []
        for tid, desc, tstatus in tasks:
            ticon = "[x]" if tstatus == "COMPLETED" else "[ ]"
            lines.append(f"  - {ticon} (Task #{tid}) {desc}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM MAP — teaches the CEO how to inspect his own universe directly
# ══════════════════════════════════════════════════════════════════════════════

def _block_system_map() -> str:
    """
    The schema documentation. This is what lets the CEO investigate the
    ecosystem via EXECUTE_REPL / EXECUTE_TERMINAL instead of needing a
    dedicated tool for every query.
    """
    return """━━━ SYSTEM MAP (YOU ARE THE CARETAKER OF THIS UNIVERSE) ━━━━━━━━━━━━━
You have direct read access to every database and log via EXECUTE_REPL
(Python) and EXECUTE_TERMINAL (shell). Do NOT wait for a dedicated tool —
write the query yourself.

DATABASES (SQLite, under ai_civilization/):

  inbox.db → table: inbox
      id                INTEGER PK
      thread_id         TEXT   (e.g. "console", "tg_<org_id>", "headless")
      direction         TEXT   'IN'  | 'OUT'
      sender            TEXT
      recipient         TEXT
      body              TEXT
      attachments       TEXT   (JSON string, default '[]')
      status            TEXT   'NEW' | 'PENDING_DELIVERY' | 'DELIVERED'
                              | 'REPLIED' | 'FAILED'
      created_at        TEXT
      parent_message_id INTEGER

  scheduler.db → table: scheduled_tasks
      id, title, status, due_at, task_type
      task_type ∈ {'CEO_WAKE', 'AGENT_WAKE', 'SCRIPT', 'NOTIFY_USER'}
      status    ∈ {'PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED'}

LOGS:
  logs/empire.log    — main system log (this is where errors land)
  agent_workspace/   — per-tool audit trails

INSPECTION PATTERNS — copy these into EXECUTE_REPL:

  # Inbox backlog by thread:
  import sqlite3
  con = sqlite3.connect("ai_civilization/inbox.db")
  for r in con.execute(
      "SELECT thread_id, direction, status, COUNT(*) FROM inbox "
      "GROUP BY thread_id, direction, status"):
      print(r)

  # Recent messages in a specific thread:
  con = sqlite3.connect("ai_civilization/inbox.db")
  for r in con.execute(
      "SELECT id, direction, status, substr(body,1,80), created_at "
      "FROM inbox WHERE thread_id=? ORDER BY id DESC LIMIT 20",
      ("tg_<org>",)):
      print(r)

  # Scheduled tasks by status:
  con = sqlite3.connect("ai_civilization/scheduler.db")
  for r in con.execute(
      "SELECT status, COUNT(*) FROM scheduled_tasks GROUP BY status"):
      print(r)

  # Recent errors in the log:
  print(__import__('subprocess').getoutput(
      "tail -200 logs/empire.log | grep -iE 'error|stuck|failed|timeout' | tail -20"))

  # Mark a stuck backlog as FAILED (SQL mutation via EXECUTE_REPL):
  con = sqlite3.connect("ai_civilization/inbox.db")
  con.execute("UPDATE inbox SET status='FAILED' "
              "WHERE thread_id=? AND status='PENDING_DELIVERY'",
              ("tg_<org>",))
  con.commit()

LIVE-STATE TOOLS (the ONLY things SQL cannot reach — in-memory Python state):
  • system_status()       → composite: tasks + inbox + scheduler + log warnings
  • inspect_task("N")     → heartbeat, current step, last 5 turns of mission #N
  • cancel_mission("N")   → terminate a live mission (mutates Python object)

WHEN TO INVESTIGATE (unprompted):
  • If a tool returns an error you don't understand
  • If the user says "nothing is happening", "you didn't reply", "is it stuck?"
  • If system_status() shows any task with age > 90s and status=RUNNING
  • If a user reports a message that never arrived → check inbox.db for
    undelivered PENDING_DELIVERY rows on that thread

RULES FOR INVESTIGATION REPORTS:
  • Cite specifics: task IDs, thread IDs, message counts, timestamps,
    error excerpts. "Something seems wrong" is not a report.
  • Example: "Thread tg_xxx has 12 PENDING_DELIVERY messages from the last
    3 minutes; the poller appears stalled."
  • If you find a fixable issue, propose the fix in the reply (or just do it
    if it's a safe SQL/log-level cleanup) and report what you did.
"""


# ══════════════════════════════════════════════════════════════════════════════
# Mode-specific instruction blocks
# ══════════════════════════════════════════════════════════════════════════════

def _block_assistant_mode() -> str:
    """Instructions for short-lived user-facing missions."""
    return """━━━ ASSISTANT MODE (READ‑FIRST) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You are an AI assistant with direct access to the user's workspace and, via
tools, to external systems (GitHub MCP, web search, email, etc.).

RULES FOR THIS MISSION:

0. STOP DOING THESE (COMMON MISTAKES THAT WASTE TURNS):
   • Do NOT run reconnaissance — no `grep -r`, `find /`, `ls -R /app`,
     `cat Dockerfile`, `cat docker-compose.yml`, `env | grep …`.
   • Do NOT re-read files whose paths you were given — assume they exist.
   • Do NOT dump environment variables.
   • Do NOT read `mcp_manager.py` or explore `/app/orchestration/` to
     "understand the system". The SYSTEM MAP below already tells you
     everything you need.
   • Do NOT spend more than 2 turns "figuring out" before you DO.
     If you are unsure, call the most obvious tool and inspect the error.

1. ANSWER, DON'T DESCRIBE.
   If the user asks you to DO something (list, show, find, read, check,
   search, look up, create, send), you MUST execute the appropriate tool(s)
   FIRST, then reply with the RESULT.
   "Yes, I have access to X" is NEVER an acceptable reply to an actionable
   request. If you have access, USE IT.

   BAD:  User: "list all repos on sisqodataclub"
         You:  "Yes — I have access to your workspace. It contains .env…"

   GOOD: User: "list all repos on sisqodataclub"
         You:  CALL_TOOL search_repositories(query="org:sisqodataclub")
               → then SEND_REPLY with the actual list of repos.

2. MCP IS AUTO-CONNECTED. DO NOT "CONNECT".
   The MCP server (@modelcontextprotocol/server-github) is launched by the
   system at process start. There is NO "connect_mcp" tool, no config file
   to discover, and no setup step to perform.
   If the FULL TOOL CATALOG below lists MCP tools (e.g. search_repositories,
   get_file_contents, create_issue, list_pull_requests), they are callable
   RIGHT NOW via CALL_TOOL.

   BAD:  User: "Can you connect to our MCP?"
         You:  (runs EXECUTE_REPL to grep /app for mcp config files,
               reads mcp_manager.py, dumps env vars mentioning MCP,
               never actually calls an MCP tool)

   GOOD: User: "Can you connect to our MCP?"
         You:  CALL_TOOL search_repositories({"query": "org:sisqodataclub"})
               → tool returns a list of repos
               → SEND_REPLY: "Yes — verified by querying the org. Here are
                 the repositories: …"

   The ONLY correct way to verify the MCP connection is to CALL AN MCP TOOL.
   Do not investigate config files. Do not read mcp_manager.py. Do not grep
   for "mcp". Do not dump env vars. Just call the tool and report the result.

3. WHEN TO USE WHICH TOOL (typical cases):
   • "list files / folders / what's in X"         → list_directory
   • "show me code in file X" / "how does X work" → inspect_code (map/extract)
   • "read file X"                               → manage_file action=read
   • "list repos in org X" / GitHub queries      → search_repositories (MCP)
   • "read file X from repo Y"                   → get_file_contents (MCP)
   • "list PRs / issues in repo Y"               → list_pull_requests /
                                                   list_issues (MCP)
   • "connect to MCP" / "are you connected?"     → CALL_TOOL an MCP tool
                                                   (e.g. search_repositories)
                                                   and report the result
   • "what tools do you have"                    → list_empire_tools
   • "search the web for X"                      → internet_search
   • "what does the docs say about X"            → query_docs
   • "what did we learn about X"                 → search_library
   • "what's happening?" / "any updates?"        → system_status (see SYSTEM MAP)
   • "is it stuck?" / "why is X frozen?"         → system_status → inspect_task
   • "why didn't you reply?"                     → inspect inbox.db via
                                                   EXECUTE_REPL
   • Anything you don't know                     → internet_search or ASK_USER

4. WHEN TO DELEGATE:
   Delegation is for HEAVY work — file creation, code writing, multi-source
   research, refactors. For read-only questions, answer directly.

   Delegation is FIRE-AND-FORGET: you send a brief ack, call DELEGATE, and
   the mission exits. The agent runs in a separate thread. When it finishes,
   a new mission is created and you will be asked to reply to the user with
   the result. You do NOT wait.

5. RESPONSE DISCIPLINE:
   You reply to EXACTLY ONE message — the one marked "◀—— reply ONLY to this
   one" in the INBOX THREAD section below. Older messages are context. Do NOT
   answer them. Do NOT acknowledge the same request twice.

6. PATH DISCIPLINE:
   Workspace root is the current working directory. Paths in tools are
   relative to it (or absolute). Do NOT invent paths — verify with
   list_directory if unsure.
"""


def _block_delegated_mode(role: str, parent_thread: Optional[str]) -> str:
    """Instructions for a worker mission delegated by the CEO."""
    return f"""━━━ AGENT MODE (DELEGATED WORKER) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You are **{role}**, a specialist agent dispatched by the Global CEO.

Your job: execute the instruction in the INBOX THREAD section, then post the
result back to the CEO. You do NOT talk to the user directly.

WORKFLOW:
  1. Read the instruction (the ◀—— marked message below).
  2. Use your assigned tools to perform the work.
     • Write files with `manage_file` (action=write / patch).
     • Read code with `inspect_code`.
     • Run Python with `execute_repl`; run shell with `execute_terminal`.
  3. When done, call SEND_REPLY with a CONCISE report:
       - What you did (1-2 lines)
       - Which files you created / modified (full paths)
       - Any blockers or caveats
     The reply routes automatically to the CEO's inbox.
  4. FINISH.

RULES:
  • Stay strictly on-task. Do not delegate further.
  • Verify your work: re-read files you wrote before reporting success.
  • If you cannot complete the task, SEND_REPLY with a clear explanation of
    what failed and why. Do NOT loop indefinitely.
  • Maximum 40 turns — use them wisely.
"""


def _block_scheduled_mode() -> str:
    """Instructions for scheduler-triggered missions."""
    return """━━━ SCHEDULED TASK MODE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This mission was triggered by the Task Scheduler. The mission text contains
[SCHEDULED_TASK_ID:N]. Execute the described task, then reply to the
originating thread (if any) and call FINISH.

If the task is informational: gather data, reply, FINISH.
If the task is creative: delegate to the right agent, then FINISH.
"""


def _block_standalone_mode() -> str:
    """Instructions for headless CLI missions."""
    return """━━━ STANDALONE MISSION MODE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This is a headless mission from the CLI or an internal trigger. Execute the
described objective end-to-end. Use tools directly for information; delegate
for creation or heavy research. FINISH when done.
"""


# ══════════════════════════════════════════════════════════════════════════════
# Main builder
# ══════════════════════════════════════════════════════════════════════════════

def build_ceo_prompt(
    mission: str,
    turn: int,
    conversation_history: List[dict],
    relevant_context: str,
    ceo_scratchpad: CEOScratchpad,
    shared_state: SharedState,
    master_plan: List[str],
    scratch_dir: str,
    cwd: str,
    global_lessons: str,
    web_intelligence: str,
    stagnation_warning: str,
    framework_block: str,
    dead_end_warnings: str,
    available_roles_str: str,
    agents: List[Any],
    spatial_anchor: str,
    ceo_playbook: List[str],
    compute_budget: float,
    timeline_chars: int,
    active_schemas: List[dict],
    async_workers_status: str,
    worker_status: str,
    framework_hint_block: str,
    token_banner: str,
    environment_recon: str = "",
    forced_decision: str = "",
    active_task: Optional[Dict] = None,
    mission_db=None,
    allowed_actions: List[str] = None,
    inbox_history: str = "",
    recent_task_results: List[dict] = None,
    current_state: str = "EXECUTION",
    mission_kind: str = "standalone",
    delegated_role: Optional[str] = None,
    parent_thread: Optional[str] = None,
) -> str:
    """
    Build the CEO prompt.

    mission_kind is one of: 'assistant' | 'delegated' | 'scheduled' | 'standalone'.

    The tool catalog and the MCP status line are BOTH derived from the CEO
    agent's `.tools` list. This guarantees the prompt's description of
    available tools matches what the runtime will dispatch on.
    """

    # ── 1. Base context ──────────────────────────────────────────────────────
    # Both blocks read from the SAME source (`agents`), so they cannot drift.
    mcp_block          = _build_mcp_block(agents)
    full_tool_catalog  = _make_tools_block_from_agents(agents, title="FULL TOOL CATALOG")
    live_plan          = _build_live_plan(mission_db)

    ceo_actions = [
        f"Turn {i+1}: {e.get('step','?')} - {str(e.get('instruction_text',''))[:100]}"
        for i, e in enumerate(conversation_history) if e.get("agent") == "👑 CEO"
    ]
    ceo_action_log = "\n".join(ceo_actions[-7:]) if ceo_actions else "No prior actions."

    # ── 2. Focus / allowed actions ──────────────────────────────────────────
    if current_state == "TRIAGE":
        focus_block = (
            "You must analyze the inbox and decide the immediate next step. "
            "Do not execute tools."
        )
        action_type_list = "NEEDS_DATA | NEEDS_DELEGATION | NEEDS_PLANNING | READY_TO_REPLY"
    elif current_state == "DATA_FETCH":
        focus_block = "You must verify facts or fetch data. Use CALL_TOOL or EXECUTE_REPL."
        action_type_list = "CALL_TOOL | EXECUTE_REPL | WAIT"
    elif current_state == "DELEGATION":
        focus_block = "You must assign work to a specialist. Do not write files yourself."
        action_type_list = "DELEGATE | ADD_TASK | UPDATE_PLAN"
    else:  # EXECUTION
        if mission_kind == "assistant":
            focus_block = (
                "Answer the user's message. Gather facts with tools, then reply "
                "and exit. Delegate only if the task requires file creation or "
                "heavy research. If the user asks about system health, use the "
                "SYSTEM MAP to investigate directly."
            )
        elif mission_kind == "delegated":
            focus_block = (
                "Execute the delegated instruction using your assigned tools. "
                "When done, SEND_REPLY with a concise report."
            )
        else:
            focus_block = (
                "Orchestrate the mission. Verify facts before replying. "
                "Delegate heavy work."
            )

        if not allowed_actions:
            allowed_actions = [
                "CALL_TOOL", "SEND_REPLY", "DELEGATE", "UPDATE_PLAN",
                "EXECUTE_REPL", "FINISH", "WAIT",
            ]
        action_type_list = " | ".join(allowed_actions)

    # ── 3. Mode-specific instruction block ─────────────────────────────────
    if mission_kind == "assistant":
        mode_block = _block_assistant_mode()
    elif mission_kind == "delegated":
        mode_block = _block_delegated_mode(
            role=delegated_role or "Specialist",
            parent_thread=parent_thread,
        )
    elif mission_kind == "scheduled":
        mode_block = _block_scheduled_mode()
    else:
        mode_block = _block_standalone_mode()

    # ── 4. Recent task outcomes ────────────────────────────────────────────
    recent_results_block = ""
    if recent_task_results:
        lines = ["━━━ RECENT TASK OUTCOMES (cite these tokens) ━━━"]
        for r in recent_task_results:
            token = r.get('citation_token', f"task_{r.get('id','?')}")
            mission_snip = (r.get('mission') or '')[:80]
            result_snip = (r.get('result') or '')[:120]
            lines.append(f"• [{token}] Mission: {mission_snip} → Result: {result_snip}")
        recent_results_block = "\n".join(lines)

    # ── 5. Agent roster ────────────────────────────────────────────────────
    agent_roster_block = ""
    if available_roles_str:
        agent_roster_block = (
            f"━━━ AVAILABLE AGENTS & THEIR TOOLS ━━━\n{available_roles_str}"
        )

    # ── 6. Environment recon ───────────────────────────────────────────────
    env_recon_block = ""
    if environment_recon:
        env_recon_block = f"━━━ ENVIRONMENT RECONNAISSANCE ━━━\n{environment_recon}"

    # ── 7. Forced decision ─────────────────────────────────────────────────
    forced_block = ""
    if forced_decision:
        forced_block = f"━━━ 🚨 FORCED DECISION REQUIRED 🚨 ━━━\n{forced_decision}"

    # ── 8. Workspace map ───────────────────────────────────────────────────
    workspace_map_block = ""
    if spatial_anchor:
        workspace_map_block = (
            f"━━━ WORKSPACE MAP (USE THIS TO LOCATE FILES) ━━━\n{spatial_anchor}"
        )

    # ── 9. Plans and citations are only relevant in "heavy" modes ──────────
    show_plan  = mission_kind in ("scheduled", "standalone")
    show_tools = mission_kind in ("assistant", "delegated", "scheduled", "standalone")

    # System map is useful for the CEO in every mission type — he's the caretaker
    # of the whole universe, whether he's answering a simple question or running
    # a heavy scheduled mission.
    system_map_block = _block_system_map()

    # ── 10. Assemble ───────────────────────────────────────────────────────
    prompt = f"""
╔═══════════════════════════════════════════════════════════════════════════╗
║  👑 GLOBAL CEO – Autonomous System Orchestrator                           ║
║  Mission #{turn}  |  kind={mission_kind:<10}  |  {datetime.now().strftime("%Y-%m-%d %H:%M")}              ║
╚═══════════════════════════════════════════════════════════════════════════╝

MISSION OBJECTIVE: {mission}

━━━ INBOX THREAD ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{inbox_history if inbox_history else "No inbox history."}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{mode_block}

{system_map_block}

━━━ CURRENT FOCUS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{focus_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━ LAWS OF ORCHESTRATION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LAW 1: PROVE YOUR WORK (ZERO HALLUCINATION)
- You MUST use tools to verify facts, URLs, and data before stating them.
- You may ONLY state factual information if you cite an Evidence ID (ev_...)
  from a tool you ran in this mission, OR a Task Result token (task_...)
  from RECENT TASK OUTCOMES.
- When using SEND_REPLY to state a fact, include the citation ID(s) in the
  'citations' array.
- If you have no valid citation, say you don't know or ask for clarification.

LAW 2: THE TOOL GATE
- To use ANY tool (MCP, GitHub, File System, Search), output action_type
  "CALL_TOOL" with the exact tool_name inside action_payload.
- Example: {{"action_type": "CALL_TOOL", "action_payload": {{"tool_name": "search_repositories", "tool_args": {{"query": "org:sisqodataclub"}}}}}}
- {mcp_block}

LAW 3: DELEGATION OVER EXECUTION
- You are an orchestrator. Do NOT write files yourself.
- Use DELEGATE to assign file creation, code writing, or heavy multi-source
  research. Delegation is FIRE-AND-FORGET: you exit, the worker runs, and a
  new mission is created when the worker finishes.

LAW 4: PLAN MUTATION
- The LIVE MISSION PLAN (below) is the only source of truth.
- Use UPDATE_PLAN with structured JSON to add phases and tasks.

LAW 5: INBOX COMMUNICATION
- You MUST reply to the user with SEND_REPLY when the mission is user-facing.
- NEVER call FINISH before you have sent at least one reply.
- For a simple question: CALL_TOOL → SEND_REPLY → FINISH.
- For a task: SEND_REPLY (ack) → DELEGATE → FINISH.

LAW 6: TRANSPARENCY
- Briefly explain what you're doing and why in your first reply.
- Be concise. One paragraph is usually enough.

━━━ WORKSPACE & FILE SYSTEM ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- CWD: {cwd}
- Scratch: {scratch_dir}
- Read files with `manage_file action=read` or `inspect_code`.
- Prefer `inspect_code` (map/extract) over `manage_file read` for large code.
- Delegate file creation/edit to a specialist with the `file_manager` tool.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{env_recon_block}

{workspace_map_block}

{live_plan if show_plan else ""}

{recent_results_block}

{agent_roster_block}

{full_tool_catalog if show_tools else ""}

━━━ DYNAMIC SYSTEM STATUS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{worker_status}
{token_banner}
Compute Budget: ${compute_budget:.2f}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{forced_block}

━━━ YOUR RECENT ACTIONS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{ceo_action_log}

━━━ OUTPUT FORMAT (STRICT JSON) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output a single JSON object. Do NOT wrap it in markdown block quotes.

{{
  "cognitive_state": {{
    "current_objective": "What am I trying to achieve right now?",
    "tool_required": "Which tool from the catalog do I need? (or 'None')",
    "verification_check": "Did I actually execute the tool required for my next action? (Yes/No)"
  }},
  "action_type": "{action_type_list}",
  "action_payload": {{
    // For CALL_TOOL:  {{"tool_name": "search_repositories", "tool_args": {{"query": "org:sisqodataclub"}}}}
    // For SEND_REPLY: {{"message_id": 123, "body": "...", "citations": ["ev_abc123", "task_42"]}}
    //   (delegated tasks: SEND_REPLY only needs {{"body": "..."}} — the routing is automatic)
    // For DELEGATE:   {{"role": "Python Dev", "instruction": "...", "assigned_tools": ["file_manager"]}}
    // For UPDATE_PLAN:{{"mutation": "ADD_PHASE", "phase_title": "...", "task_description": "..."}}
    // For EXECUTE_REPL:{{"code": "print('hello')"}}
    // For WAIT:       {{"reason": "Waiting for worker to finish"}}
    // For FINISH:     {{"report": "Mission accomplished"}}
  }}
}}
"""
    return prompt
