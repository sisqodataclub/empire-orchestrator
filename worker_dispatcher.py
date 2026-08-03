"""
worker_dispatcher.py
Handles execution of sync/async workers, including threading, timeout, result collection,
and state updates. Extracted from task_manager.py to reduce code clutter.
"""

import threading
import time
import os
import json
import subprocess
import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable

# Import from core modules
from cognitive_wrapper import cognitive_agent_wrapper, classify_task, report_agent_wrapper
from mental_framework import detect_domains, load_framework, build_worker_brief   # <-- FIXED import
from framework_writer import record_observation, record_failure, record_structural


def execute_worker(
    role_name: str,
    cmd_text: str,
    turn: int,
    task_context: Any,          # ActiveTask instance (or an object with required attrs)
    director_llm: Any,
    logger: Callable,
    shared_state: Dict[str, Any],
    agent_memories: Dict[str, List],
    scratch_dir: str,
    sync_fail_counts: Dict[str, int],
    conversation_history: List[dict],
    mission: str,
    active_schemas: List[dict],
    mission_id: str,
    parallel_results: Dict[str, str],
) -> None:
    """
    Executes a single worker (sync) – this is the exact same logic as the inner
    `execute_worker` function from the original task_manager.py.
    It writes results directly into `parallel_results` dict.
    """
    # Find the agent by role (search among subordinates)
    subordinates = [a for a in task_context.agents if a.role != "The Global CEO"]
    agent = next((a for a in subordinates if role_name.lower() in a.role.lower()), None)

    if not agent:
        parallel_results[role_name] = f"❌ No agent found for role: {role_name}"
        return

    logger(f"    ↳ Command @{agent.role}: [cyan]{str(cmd_text)[:120]}[/cyan]")

    worker_brief = build_worker_brief(active_schemas, agent.role, str(cmd_text))

    # ── Live project context injection ────────────────────────────────────────
    try:
        _proj_cwd = os.getcwd()
        _src_files = subprocess.getoutput(
            f"find {_proj_cwd} -maxdepth 5 "
            r"\( -name 'package.json' -o -name 'pyproject.toml' -o -name 'tasks.md' "
            r"-o -name 'main.py' -o -name 'app.py' \) "
            "! -path '*/node_modules/*' ! -path '*/.git/*' ! -path '*/dist/*' "
            "2>/dev/null | sort | head -20"
        ).strip()
        _active_dir = _proj_cwd
        for _line in (task_context.master_plan or []):
            _m = re.search(r'(/[\w./\-]+(?:apps|src|frontend|backend|packages)/[\w.\-/]+)', str(_line))
            if _m and os.path.exists(_m.group(1)):
                _active_dir = _m.group(1).rstrip('/')
                break
        _active_ls = ""
        if os.path.isdir(_active_dir):
            _active_ls = subprocess.getoutput(f"ls -la {_active_dir} 2>/dev/null | head -20").strip()

        # Extract literal paths from master plan
        _plan_paths = []
        for _step in (task_context.master_plan or []):
            for _match in re.findall(r'(/[\w./\-$\[\]\(\)\+]+\.(?:tsx?|jsx?|json|css|html|py|svelte|vue|md|sh))', str(_step)):
                if _match not in _plan_paths:
                    _plan_paths.append(_match)

        _target_files_str = ""
        if _plan_paths:
            _target_files_str = (
                "\n  🎯 TARGET FILES FROM CEO (WORK ON THESE EXACT PATHS):\n"
                + "".join(f"    ⚑ {p}\n" for p in _plan_paths)
                + "  → Do NOT discover alternative files. These are the verified targets.\n"
            )

        _project_context = (
            f"\n📍 PROJECT CONTEXT:\n"
            f"  CWD: {_proj_cwd}\n"
            f"  Active project dir: {_active_dir}\n"
            f"  Contents:\n"
            + "".join(f"    {l}\n" for l in _active_ls.splitlines())
            + f"\n  Key files:\n"
            + "".join(f"    {l}\n" for l in _src_files.splitlines())
            + _target_files_str
            + f"\n  → Use absolute paths for all file operations.\n"
        )
    except Exception:
        _project_context = f"\n📍 CWD: {os.getcwd()}\n"

    _clarification_layer = (
        "BEFORE YOU START:\n"
        "  1. Do I know exactly WHERE to work?\n"
        "  2. Do I know exactly WHAT to build or change?\n"
        "  3. Does something relevant already exist?\n"
        "  4. Am I guessing about something important?\n\n"
        "If any answer is 'I don't know' — respond with clarification_needed.\n"
        "─────────────────────────────────────────────────────────────────────\n"
    )

    # ── Wrap the instruction ─────────────────────────────────────────────────
    from empire_tools import library_collection  # for JIT knowledge injection (if needed)

    def inject_jit(instruction_text: str) -> str:
        # This is the _inject_jit_knowledge function from task_manager.
        # We'll inline a simplified version, or just use the original method.
        # In the interest of brevity, we'll call the original method if available.
        if hasattr(task_context, '_inject_jit_knowledge'):
            return task_context._inject_jit_knowledge(instruction_text)
        return instruction_text

    enriched_cmd = inject_jit(
        f"🚨 STRICT DIRECTIVE FROM CEO 🚨\n"
        f"IGNORE your default backstory and past goals.\n"
        + (f"\n{worker_brief}\n" if worker_brief else "")
        + _project_context
        + f"\n{_clarification_layer}"
        + f"YOUR EXACT MISSION NOW:\n{cmd_text}\n"
    )

    # ── Execute the agent ─────────────────────────────────────────────────────
    task_tier = classify_task(enriched_cmd)

    if task_tier == "REPORT":
        result = report_agent_wrapper(
            agent=agent, instruction=enriched_cmd,
            critic_llm=director_llm, logger=logger,
            cwd=os.getcwd(), shared_state=shared_state
        )
    else:
        result = cognitive_agent_wrapper(
            agent=agent, instruction=enriched_cmd,
            project_state="", private_history="",
            critic_llm=director_llm, logger=logger,
            scratch_dir=scratch_dir, agent_role=agent.role,
            shared_state=shared_state
        )

    # ── Unpack result ────────────────────────────────────────────────────────
    if len(result) == 3:
        safe_res, artifacts, structured_result = result
    else:
        safe_res, artifacts = result
        structured_result = {}

    parallel_results[agent.role] = safe_res

    # ── Record observations into framework ───────────────────────────────────
    for schema in active_schemas:
        record_observation(schema, str(safe_res), mission_id, agent.role, turn)
        if structured_result.get("status") == "success":
            for err in structured_result.get("errors_resolved", [])[:2]:
                if err:
                    record_failure(
                        schema, error_text=err[:150],
                        root_cause="Resolved during mission",
                        fix=str(safe_res)[:200],
                        mission_id=mission_id, agent=agent.role, confidence=0.75,
                    )
        for f in structured_result.get("files_written", []):
            record_structural(
                schema, key=f"file_written:{os.path.basename(f)}",
                value=f, kind="path", mission_id=mission_id,
                agent=agent.role, confidence=0.80,
            )

    # ── Handle timeouts ──────────────────────────────────────────────────────
    role_slug = agent.role.lower().replace(" ", "_")[:20]
    if structured_result.get("status") == "timeout":
        task_context.consecutive_timeouts[role_slug] = (
            task_context.consecutive_timeouts.get(role_slug, 0) + 1
        )
        if task_context.consecutive_timeouts[role_slug] >= 2:
            logger(f"\n[bold yellow]⚡ {agent.role} timed out twice. CEO synthesizing...[/bold yellow]")
            all_outputs = []
            for mem in agent_memories.get(agent.role, []):
                all_outputs.extend(mem.get("tool_outputs", []))
            if all_outputs:
                synthesis_prompt = (
                    f"You are the Global CEO. A worker timed out twice. "
                    f"Synthesize the report from raw tool outputs.\n\n"
                    f"MISSION: {mission}\n\n"
                    f"DATA ({len(all_outputs)} tool calls):\n"
                    + "\n\n".join(
                        f"[{o['tool']}({o['args_summary']})]\n{o['output']}"
                        for o in all_outputs[-12:]
                    )[:4000]
                    + "\n\nWrite a complete report."
                )
                try:
                    synthesized = director_llm.call(
                        messages=[{"role": "user", "content": synthesis_prompt}]
                    )
                    parallel_results[agent.role] = synthesized
                    task_context.consecutive_timeouts[role_slug] = 0
                except Exception as syn_err:
                    logger(f"[dim red]Synthesis error: {syn_err}[/dim red]")
    else:
        task_context.consecutive_timeouts[role_slug] = 0

    # ── Handle clarification ─────────────────────────────────────────────────
    if structured_result.get("status") == "clarification":
        question = structured_result.get("clarification_needed", safe_res.replace("[CLARIFICATION NEEDED]", "").strip())
        blocker = f"[WORKER QUESTION from {agent.role}]: {question}"
        if blocker not in shared_state.get("blockers", []):
            shared_state.setdefault("blockers", []).append(blocker)
        ceo_blocker = f"❓ {agent.role} needs clarification: {question}"
        # ceo_scratchpad is a dataclass object – use attribute access
        if ceo_blocker not in task_context.ceo_scratchpad.blockers:
            task_context.ceo_scratchpad.blockers.append(ceo_blocker)
        logger(f"\n  [bold yellow]❓ CLARIFICATION from {agent.role}:[/bold yellow]\n  {question[:200]}")

    # ── Handle success ──────────────────────────────────────────────────────
    elif structured_result.get("status") == "success":
        for f in structured_result.get("files_written", []):
            shared_state["files_modified"][f] = agent.role
        verified = structured_result.get("verified_by")
        if verified:
            fact = f"{agent.role} verified {structured_result.get('primary_technology','task')} via {verified}"
            if fact not in shared_state.get("verified_facts", []):
                shared_state.setdefault("verified_facts", []).append(fact)
        resolved = structured_result.get("errors_resolved", [])
        shared_state["blockers"] = [
            b for b in shared_state.get("blockers", [])
            if not any(r[:40] in b for r in resolved)
        ]
        coaching = structured_result.get("coaching_tip", "")
        if coaching:
            # Inject learned directive to agent's DNA
            if hasattr(task_context, 'inject_learned_directive'):
                task_context.inject_learned_directive(agent.role, coaching)
        sync_fail_counts[agent.role] = 0

    elif structured_result.get("status") in ("fail", "timeout"):
        for err in structured_result.get("errors_encountered", [])[:2]:
            if err and err not in shared_state.get("blockers", []):
                shared_state.setdefault("blockers", []).append(err[:120])

    # ── Update agent memory ──────────────────────────────────────────────────
    if agent.role in agent_memories:
        agent_memories[agent.role].append({
            "assigned_task":       cmd_text,
            "your_result":         safe_res,
            "last_command_output": artifacts.get("last_command_output", ""),
            "files_written":       artifacts.get("files_written", []),
            "tool_outputs":        artifacts.get("tool_outputs", []),
            "structured_result":   structured_result
        })

    # ── Handle rejection / genetic evolution ──────────────────────────────────
    if "❌" in safe_res or "Rejected by Mentor" in safe_res:
        logger(f"\n[bold red]🚨 FAILURE on {agent.role}. Awakening Geneticist...[/bold red]")
        if hasattr(task_context, 'evolve_agent_dna'):
            task_context.evolve_agent_dna(agent.role, safe_res)

        sync_fail_counts[agent.role] = sync_fail_counts.get(agent.role, 0) + 1
        if sync_fail_counts[agent.role] >= 2:
            takeover_msg = (
                f"[DIRECT CONTROL MANDATE]: Worker '{agent.role}' failed {sync_fail_counts[agent.role]}x. "
                f"Take direct terminal control and fix the blocker yourself."
            )
            # ceo_scratchpad is a dataclass – use attribute access
            # Ensure blockers list exists (it should, but we'll be safe)
            if not hasattr(task_context.ceo_scratchpad, 'blockers'):
                task_context.ceo_scratchpad.blockers = []
            # Remove any existing takeover messages for this agent
            task_context.ceo_scratchpad.blockers = [
                b for b in task_context.ceo_scratchpad.blockers
                if agent.role not in b or "DIRECT CONTROL MANDATE" not in b
            ]
            task_context.ceo_scratchpad.blockers.append(takeover_msg)
            logger(f"  [bold red]🚨 TAKEOVER MANDATE: {agent.role} failed {sync_fail_counts[agent.role]}x[/bold red]")

        # ── Check playbook failures ──────────────────────────────────────────
        PLAYBOOK_DIR = os.path.abspath(os.path.join("ai_civilization", "cot_playbooks"))
        if os.path.isdir(PLAYBOOK_DIR):
            for pb_file in os.listdir(PLAYBOOK_DIR):
                if not pb_file.endswith("_cot.md"):
                    continue
                tech = pb_file.replace("_cot.md", "")
                if tech.lower() in safe_res.lower() or tech.lower() in str(cmd_text).lower():
                    task_context._playbook_strikes[tech] = task_context._playbook_strikes.get(tech, 0) + 1
                    if task_context._playbook_strikes[tech] >= 3:
                        # ceo_scratchpad.blockers is a list – append
                        task_context.ceo_scratchpad.blockers.append(
                            f"📖 PLAYBOOK OVERRIDE: '{tech}' caused {task_context._playbook_strikes[tech]} failures. Rebuild."
                        )
                        task_context._playbook_strikes[tech] = 0


def dispatch_sync_workers(
    assigned_roles: List[str],
    raw_instructions: Any,
    turn: int,
    task_context: Any,
    director_llm: Any,
    logger: Callable,
    shared_state: Dict[str, Any],
    agent_memories: Dict[str, List],
    scratch_dir: str,
    sync_fail_counts: Dict[str, int],
    conversation_history: List[dict],
    mission: str,
    active_schemas: List[dict],
    mission_id: str,
) -> Dict[str, str]:
    """
    Runs sync workers in parallel threads, collects results.
    Returns a dict {agent_role: result_text}.
    """
    parallel_results = {}
    worker_threads = []

    for role in assigned_roles:
        cmd = raw_instructions.get(role, raw_instructions) if isinstance(raw_instructions, dict) else raw_instructions
        t = threading.Thread(
            target=execute_worker,
            args=(role, cmd, turn, task_context, director_llm, logger, shared_state,
                  agent_memories, scratch_dir, sync_fail_counts, conversation_history,
                  mission, active_schemas, mission_id, parallel_results)
        )
        worker_threads.append(t)
        t.start()

    for t in worker_threads:
        t.join(timeout=1500)  # 25 minutes
        if t.is_alive():
            logger(f"  [bold red]⏰ SYNC WORKER THREAD TIMEOUT after 25min.[/bold red]")
            conversation_history.append({
                "step": f"{turn} (SYNC-THREAD-TIMEOUT)",
                "agent": "🔧 System",
                "instruction_text": "",
                "result": "⚠️ Sync worker did not complete in 25min. Check work with ls, then proceed.",
                "raw_tool_outputs": [],
                "structured_results": []
            })

    return parallel_results


def check_async_workers(
    async_workers: Dict[str, threading.Thread],
    async_dispatch_times: Dict[str, float],
    async_results: Dict[str, str],
    async_events: List[str],
    async_fail_counts: Dict[str, int],
    ceo_scratchpad: Any,
    logger: Callable,
    conversation_history: List[dict],
    turn: int,
) -> bool:
    """
    Checks the status of running async workers.
    Returns True if the CEO should wait (WAIT_FOR_ASYNC), False if the CEO should act.
    Also handles timeouts and stale workers.
    """
    still_running = {r: t for r, t in async_workers.items() if t.is_alive()}

    # ── If workers are running, CEO must wait ──────────────────────────────
    if still_running:
        return True

    # ── If no workers, process results and clear ────────────────────────────
    if async_results:
        for role, result in async_results.items():
            async_events.append(f"🚨 [ASYNC EVENT]: {role} completed. Result: {str(result)[:120]}")
            logger(f"  [bold green]✅ ASYNC COMPLETE: {role}[/bold green]")
        async_results.clear()
        async_workers.clear()
        async_dispatch_times.clear()

    return False  # No workers running, CEO can act
