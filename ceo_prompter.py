# ceo_prompter.py
"""
Builds the CEO system prompt dynamically.
- Dynamic state‑enforced tool schema (allowed action_types)
- ID‑based plan with live snapshot injection
- Graceful redirect coaching instead of hard blocks
- FORBIDDEN: CEO uses WRITE_FILE – delegation is mandatory
- AUTO-COMPLETION AWARE: CEO knows not to manually verify or mark tasks done
"""

import os
from typing import List, Any, Optional, Dict
from datetime import datetime
from ceo_state import CEOScratchpad, SharedState


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
    mission_db = None,               # 🆕 for live plan snapshot
    allowed_actions: List[str] = None, # 🆕 state‑enforced tool schema
) -> str:

    # ── Project Handbook ──
    manifest_path = os.path.join(cwd, "domain_manifest.md")
    handbook = ""
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            handbook = f.read()

    # ── Agent Roster ──
    agent_roster = "\n".join(
        f"• {a.role}  → Tools: {', '.join({getattr(t, 'name', getattr(t, '__name__', str(t))) for t in a.tools})}"
        for a in agents
    )

    # ── Operational Directives ──
    operational_directives = f"""
{f"=== DOMAIN KNOWLEDGE & PROJECT HANDBOOK ===\n{handbook}\n===========================================" if handbook else ""}

=== OPERATIONAL DIRECTIVES ===
1. ZERO HALLUCINATION: Anchor all work to the Project Handbook.
2. STRICT DELEGATION: You are an **orchestrator**, not an individual contributor.
   - NEVER write files yourself. Use DELEGATE to assign file creation to a specialist.
   - NEVER run compilers, installers, or data‑modifying terminal commands.
   - Use TERMINAL only for read‑only reconnaissance (ls, cat, find).
3. PLAN AS SOURCE OF TRUTH: Always refer to the LIVE MISSION PLAN below for task IDs and statuses.
"""

    # ── Dynamic Planning Directive ──
    dynamic_planning_directive = f"""
━━━ DYNAMIC DELIVERABLE‑DRIVEN PLANNING (FIRST PRINCIPLES) ━━━━━━━━━━━━━━━━━━━━
You are an autonomous Chief Architect. On your FIRST turn, define the final product via DEFINE_PRODUCT.
Then use UPDATE_PLAN to build a custom roadmap with clear, measurable outputs.
"""

    # ── Plan Directive ──
    plan_directive = f"""
━━━ ULTRA-DETAILED PLAN MAINTENANCE & PROJECT MANAGEMENT ━━━━━━━━━━━━━━━━━━━━━
The mission plan is stored in a secure SQLite database and rendered below as the LIVE MISSION PLAN.
You are FORBIDDEN from using WRITE_FILE on plan.md. Instead, use UPDATE_PLAN (structured JSON) for all plan changes.
"""

    # ── UPDATE_PLAN Tool Definition ──
    update_plan_tool = f"""
━━━ UPDATING THE PLAN (STRUCTURED JSON ONLY) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
To modify the roadmap, emit an UPDATE_PLAN action with this schema:
{{
  "action_type": "UPDATE_PLAN",
  "action_payload": {{
    "mutation": "ADD_PHASE | ADD_TASK | MARK_TASK_DONE",
    "phase_title": "...",
    "task_description": "...",
    "assigned_role": "...",
    "deliverable_file": "/path/file",   // optional
    "tools_allowed": "tool1,tool2",     // optional
    "task_id": 1                        // required for MARK_TASK_DONE
  }}
}}
"""

    # ── Verification Rule (✅ LOOP FIX APPLIED HERE) ──
    verification_protocol = f"""
━━━ NO TERMINAL AFTER WORKER OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Once a worker finishes, the system AUTOMATICALLY marks their task as COMPLETED.
You are PERMANENTLY FORBIDDEN from using TERMINAL to verify their work—the data is already in the timeline.
Do NOT try to manually mark tasks done. Simply review the LIVE MISSION PLAN and move to the next task/phase, or use FINISH.
"""

    # ── Delegation & File Creation Policy ──
    delegation_policy = f"""
━━━ DELEGATION & FILE CREATION POLICY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
As Chief Architect, you are strictly an orchestrator. Your hands do not touch the keyboard.
You are FORBIDDEN from using WRITE_FILE to author reports, code, or deliverables.
If a file needs to be written, you MUST use the DELEGATE tool to assign it to a specialist.

When delegating, always instruct the worker to write files to the mission scratch directory:
`{scratch_dir}`
"""

    # ── Live Plan Snapshot (from mission_db) ──
    live_plan = ""
    if mission_db:
        phases = mission_db.conn.execute("SELECT id, title, status FROM phases ORDER BY id").fetchall()
        if phases:
            live_plan = "━━━ LIVE MISSION PLAN (SINGLE SOURCE OF TRUTH) ━━━\n"
            for pid, title, status in phases:
                icon = "✅" if status == "COMPLETED" else "⏳"
                live_plan += f"[P{pid}] {title} - {icon} [{status}]\n"
                tasks = mission_db.conn.execute(
                    "SELECT id, description, status FROM tasks WHERE phase_id = ? ORDER BY id", (pid,)
                ).fetchall()
                for tid, desc, tstatus in tasks:
                    ticon = "[x]" if tstatus == "COMPLETED" else "[ ]"
                    live_plan += f"  - {ticon} (Task #{tid}) {desc}\n"
            live_plan += "\nNOTE: Only mutate tasks/phases listed above. Do not invent non‑existent phases."
        else:
            live_plan = "No phases defined yet. Use UPDATE_PLAN ADD_PHASE to create the first phase."

    # ── Active Task Block ──
    active_task_block = ""
    if active_task:
        active_task_block = f"""
━━━ YOUR ACTIVE TASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Description: {active_task.get('description','')}
Acceptance Criteria: {active_task.get('acceptance_criteria','')}
Assigned Role: {active_task.get('assigned_role','')}
Tools Allowed: {active_task.get('tools_allowed','Any')}
Deliverable: {active_task.get('deliverable_file','Not specified')}
"""

    # ── Context Strategy ──
    context_strategy = f"""
━━━ CONTEXT STRATEGY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Global Lessons: {global_lessons}
- Web Intelligence: {web_intelligence}
- Active Worker Status: {worker_status}
- Compute Budget: ${compute_budget:.2f}
"""

    # ── Timeline ──
    timeline_str = ""
    for entry in conversation_history[-4:]:
        step = entry.get("step", "?")
        agent = entry.get("agent", "?")
        instr = str(entry.get("instruction_text", ""))[:150]
        result = str(entry.get("result", ""))[:300]
        timeline_str += f"[{step}] {agent}: {instr}\n  → {result}\n\n"
    if not timeline_str:
        timeline_str = "No previous turns recorded."

    # ── Forced Decision ──
    forced_block = ""
    if forced_decision:
        forced_block = f"""
━━━ 🚨 FORCED DECISION REQUIRED 🚨 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{forced_decision}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

    # ── Environment Recon ──
    env_block = ""
    if environment_recon:
        env_block = f"""
━━━ ENVIRONMENT RECONNAISSANCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{environment_recon}
"""

    # ── Allowed Actions Schema ──
    if allowed_actions is None:
        allowed_actions = [
            "DEFINE_PRODUCT", "TERMINAL", "DELEGATE", "HIRE", "WAIT",
            "FINISH", "CLARIFY", "MARK_COMPLETED", "REQUEST_REWORK",
            "UPDATE_PLAN", "ROLLBACK"
        ]
    action_type_list = " | ".join(allowed_actions)

    # ── Main Prompt ──
    prompt = f"""
╔═══════════════════════════════════════════════════════════════════════════╗
║  👑 GLOBAL CEO – Autonomous System Orchestrator                         ║
║  Mission #{turn}   |   {datetime.now().strftime("%Y-%m-%d %H:%M")}                                       ║
╚═══════════════════════════════════════════════════════════════════════════╝

MISSION: {mission}

{env_block}

{operational_directives}

{dynamic_planning_directive}

{plan_directive}

{update_plan_tool}

{verification_protocol}

{delegation_policy}

{live_plan}

{active_task_block}

{context_strategy}

━━━ RECENT TIMELINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{timeline_str}

{forced_block}

━━━ AVAILABLE AGENTS & TOOLS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{agent_roster if agent_roster else "None (hire agents with HIRE action)"}

━━━ CELEBRATE TURN {turn} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond ONLY in valid JSON. Use this compact schema:
{{
  "reasoning_engine": {{
    "current_phase": "DIAGNOSTIC | DISCOVERY | ARCHITECTURE | EXECUTION | VERIFICATION | RESEARCHING",
    "verified_state": "What exists on disk RIGHT NOW — verified, not assumed",
    "hypothesis_and_risk": "Root cause + what could go wrong next",
    "optimal_next_step": "Cheapest route to advance"
  }},
  "thought": "One sentence: what I know, what I'm doing, why.",
  "action_type": "{action_type_list}",
  "action_payload": {{
    // If DEFINE_PRODUCT: {{"description": "...", "deliverable_files": ["/path/file"]}}
    // If TERMINAL: {{"commands": ["cmd1", "cmd2"]}} (max 3 read-only commands)
    // If DELEGATE: {{"role": "exact role name", "instruction": "Detailed task."}}
    // If HIRE: {{"role": "new role", "goal": "...", "backstory": "...", "initial_instruction": "..." (optional)}}
    // If WAIT: {{"reason": "why waiting"}}
    // If FINISH: {{"report": "summary of what was accomplished"}}
    // If CLARIFY: {{"question": "specific question for Overlord"}}
    // If MARK_COMPLETED: {{}}  (empty payload)
    // If REQUEST_REWORK: {{"feedback": "specific description of what needs fixing"}}
    // If UPDATE_PLAN: {{"mutation": "ADD_PHASE|ADD_TASK|MARK_TASK_DONE", "phase_title": "...", "task_description": "...", "assigned_role": "...", "deliverable_file": "/path/file", "tools_allowed": "tool1,tool2", "task_id": 1}}
    // If ROLLBACK: {{"checkpoint_id": 1}}
  }}
}}
"""
    return prompt
