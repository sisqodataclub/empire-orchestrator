# mixins/core_loop_mixin.py
#
# Main mission loop, forced-decision injection, and fatal-crash handling.
#
# Mission types (detected in ActiveTask._parse_mission_metadata):
#
#   ASSISTANT  — user message via an inbox thread. Fast: gather info → reply → exit.
#                Turn ceiling: 6.
#
#   DELEGATED  — worker spawned via AgentBus. Runs in its own ActiveTask thread.
#                Does the work, then SEND_REPLY routes back to the CEO via AgentBus.
#                Turn ceiling: 40.
#
#   SCHEDULED  — fired by the scheduler. Behaviour depends on task_type.
#                Turn ceiling: 300.
#
#   STANDALONE — headless CLI mission. Turn ceiling: 300.
#
# Observability:
#   • Emits heartbeats to TaskManager 3 times per turn (before thinking,
#     before the LLM call, before executing the action). If a mission
#     freezes inside any of these steps, the watchdog fires at 90s.
#   • Calls self.mark_finished() on every exit path so TaskManager stops
#     tracking the task.
#
# Recent changes (assistant-mode fix):
#   1. mission_kind / delegated_role / parent_thread are now passed through
#      to build_ceo_prompt. Previously they were omitted, so every prompt
#      rendered as STANDALONE MODE — the CEO never saw the assistant-mode
#      instructions ("ANSWER, DON'T DESCRIBE", MCP is auto-connected, etc.).
#   2. _ensure_final_reply() no longer calls the LLM to fabricate a reply
#      when the loop exits without one. It sends an honest failure instead.
#      The old fallback prompt ("Reply to the user now. Return ONLY the reply
#      text.") had no tools and no action schema, so the LLM honestly
#      reported "I have no tools" — which looked authoritative but was
#      meaningless.
#   3. _emergency_reply() bypasses the tool-first gate (turn=9999) so the
#      user still receives a graceful message if the budget runs out before
#      any tool call.
#

import time
import os
import re
import logging
import hashlib
import subprocess
from typing import List, Dict, Any

from ceo_prompter import build_ceo_prompt
from mental_framework import render_framework_block
from empire_tools import EmpireTools

from .helpers_mixin import HelpersMixin
from .action_handlers_mixin import ActionHandlersMixin
from .worker_dispatch_mixin import WorkerDispatchMixin
from .agent_management_mixin import AgentManagementMixin


logger = logging.getLogger(__name__)


# Turn ceilings per mission type
_TURNS_DELEGATED  = 40
_TURNS_ASSISTANT  = 6
_TURNS_SCHEDULED  = 300
_TURNS_STANDALONE = 300


class CoreLoopMixin:
    """Contains the main mission loop and forced‑decision injection."""

    # ------------------------------------------------------------------
    # Heartbeat helper — safe no-op if TaskManager isn't wired
    # ------------------------------------------------------------------
    def _beat(self, step: str) -> None:
        if self.task_manager:
            try:
                self.task_manager.heartbeat(self.id, step=step)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Top-level entry point — wraps the inner loop and handles fatals.
    # ------------------------------------------------------------------
    def _run_loop(self):
        try:
            self._run_loop_inner()
        except Exception as _fatal:
            import traceback as _tb
            _tb_str = _tb.format_exc()
            self.logs.append(f"[bold red]💥 FATAL LOOP CRASH: {_fatal}[/bold red]")
            self.conversation_history.append({
                "step": "FATAL-CRASH",
                "agent": "🔧 System",
                "instruction_text": "",
                "result": f"💥 The mission loop crashed fatally:\n{_tb_str[-1000:]}",
                "raw_tool_outputs": [],
                "structured_results": [],
            })

            # If this mission was launched by the scheduler, tell the DB so
            # retry / backoff logic can kick in.
            if getattr(self, 'scheduled_task_id', None):
                try:
                    self._mark_scheduled_task_failed(f"fatal: {str(_fatal)[:200]}")
                except Exception:
                    pass

            if not self.is_complete:
                self.status = "COMPLETED"
                self.is_complete = True
                self.save_history_to_disk()

            # Ensure TaskManager stops tracking this task on fatal crash.
            try:
                self.mark_finished()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Forced-decision injector (used when a phase completes)
    # ------------------------------------------------------------------
    def _inject_forced_decision_prompt(self) -> str:
        pending_phase_id = self.mission_db.get_pending_phase_id()
        if pending_phase_id is None:
            self.ceo_scratchpad.blockers.insert(
                0,
                "[ALL PHASES COMPLETE] All planned work is done. "
                "You MUST now call FINISH or, if something is missing, "
                "use UPDATE_PLAN to add new phases.",
            )
            return ""

        phase_status = self.mission_db.get_phase_status(pending_phase_id)
        if phase_status == "COMPLETED":
            return (
                f"🚨 FORCED DECISION: Phase ID {pending_phase_id} has been completed by workers. "
                f"As CEO, you must now respond with exactly ONE of these actions:\n"
                f"  • MARK_COMPLETED (to advance to the next phase)\n"
                f"  • REQUEST_REWORK (to send the phase back with specific feedback)\n"
                f"No other actions are allowed until you make this decision."
            )
        return ""

    # ------------------------------------------------------------------
    # Mission-type helpers
    # ------------------------------------------------------------------
    def _mission_kind(self) -> str:
        """Return 'delegated' | 'assistant' | 'scheduled' | 'standalone'."""
        if getattr(self, 'is_delegated_task', False):
            return "delegated"
        if getattr(self, 'is_scheduled_task', False):
            return "scheduled"
        if getattr(self, 'inbox_thread_id', None):
            return "assistant"
        return "standalone"

    def _turn_ceiling(self) -> int:
        kind = self._mission_kind()
        return {
            "delegated":  _TURNS_DELEGATED,
            "assistant":  _TURNS_ASSISTANT,
            "scheduled":  _TURNS_SCHEDULED,
            "standalone": _TURNS_STANDALONE,
        }[kind]

    # ------------------------------------------------------------------
    # The inner mission loop
    # ------------------------------------------------------------------
    def _run_loop_inner(self):
        self.status = "RUNNING"
        mission_kind = self._mission_kind()

        # ── domain_manifest gate: required only for scheduled/standalone ──
        # Assistant and delegated missions must NOT be blocked by a missing
        # manifest — that gate was causing simple queries to bounce.
        if mission_kind in ("scheduled", "standalone"):
            domain_file = os.path.join(os.getcwd(), "domain_manifest.md")
            if not os.path.exists(domain_file):
                self.ceo_scratchpad.active_domain = "MISSING_MANIFEST"
                self.ceo_scratchpad.blockers.insert(
                    0, "[DOMAIN MISSING] domain_manifest.md not found. ..."
                )
                self._clarify_question = "Please create domain_manifest.md ..."
                self.status = "AWAITING_OVERLORD"
                self.mark_finished()
                return

        # ── Agent roster (subordinates only) ──
        subordinates = [a for a in self.agents if a.role != "The Global CEO"]
        available_roles_str = "\n".join(
            f"• {a.role}\n  Tools: ["
            f"{', '.join(dict.fromkeys(getattr(t, 'name', str(t)) for t in a.tools))}]"
            for a in subordinates
        )

        # ── Initial recon of scratch dir (used in first-turn prompt) ──
        try:
            recon_output = EmpireTools().list_directory(self.scratch_dir)
        except Exception:
            recon_output = "Directory empty or inaccessible."
        self._initial_recon = (
            f"--- ENVIRONMENT RECONNAISSANCE ---\n"
            f"Workspace Path: {self.scratch_dir}\n...\n{recon_output}\n"
        )

        turn = 0
        _EMERGENCY_TURN_CEILING = self._turn_ceiling()
        logger.info(
            f"Mission '{self.id}' kind={mission_kind} "
            f"turn_ceiling={_EMERGENCY_TURN_CEILING}"
        )

        while turn < _EMERGENCY_TURN_CEILING:
            turn += 1
            if self.is_complete:
                break

            # ── HEARTBEAT: thinking phase ──
            self._beat(f"turn {turn}: thinking")

            # ── COMPUTE BUDGET EXHAUSTION ──
            if self.compute_budget <= 0:
                self._emergency_reply(turn)
                self.result = self.generate_final_report()
                self.status = "COMPLETED"
                self.is_complete = True
                self.save_history_to_disk()
                if self.task_manager:
                    self.task_manager.trigger_dream_state()
                self.mark_finished()
                return

            # ── Drain legacy async worker events ──
            if self._async_events:
                for event in self._async_events:
                    self.conversation_history.append({
                        "step": f"{turn} (ASYNC-INTERRUPT)",
                        "agent": "⚡ ASYNC INTERRUPT",
                        "instruction_text": "",
                        "result": event,
                        "raw_tool_outputs": [],
                        "structured_results": [],
                    })
                    self._index_turn(self.conversation_history[-1])
                for role_name, full_result in list(self._async_full_results.items()):
                    self._handle_worker_completion(role_name, str(full_result))
                self._verification_turns_allowed = 0
                self._seen_worker_output = True
                self.ceo_scratchpad.blockers = [
                    b for b in self.ceo_scratchpad.blockers if "VERIFICATION LIMIT" not in b
                ]
                self.vision_streak.clear()
                self._consecutive_duplicate_blocks = 0
                self._async_full_results.clear()
                self._async_events.clear()
                self.save_history_to_disk()
            else:
                if not self._seen_worker_output and self._verification_turns_allowed < 3:
                    self._verification_turns_allowed = 3

            forced_decision = self._inject_forced_decision_prompt()

            # ── Context pieces for the prompt ──
            query_text = f"{self.mission} {' '.join(self.ceo_scratchpad.blockers[-2:])}"
            relevant_context = self._query_relevant_context(query_text, top_k=3)
            last_turns = self.conversation_history[-2:] or []

            if self._active_schemas:
                framework_block, _ = render_framework_block(self._active_domains, self.mission)
            else:
                framework_block = ""

            dead_end_warnings = ""
            for strat, count in self.strategy_attempts.items():
                if count >= 2:
                    dead_end_warnings += (
                        f"\n🚨 DEAD END: Strategy '{strat}' attempted {count} times. PIVOT NOW."
                    )

            framework_blocker_hint = self._check_framework_on_blockers(turn)

            # ── CEO search trigger (disabled until first reply on assistant) ──
            blockers = self.ceo_scratchpad.blockers
            searchable_blockers = [
                b for b in blockers
                if not b.startswith((
                    "[ALL PHASES COMPLETE]",
                    "🚨 FORCED DECISION",
                    "🚫 FINAL DELIVERABLE GATE",
                    "🚫 PREMATURE FINISH BLOCKED",
                    "🚨 DUPLICATE COMMAND BLOCKED",
                    "You already have the worker's output",
                ))
            ]
            err_hashes = [hashlib.md5(b.encode()).hexdigest()[:8] for b in blockers]
            repeated_err = len(err_hashes) >= 2 and len(set(err_hashes)) < len(err_hashes)
            cooldown_ok = (turn - self.web_search_turn) >= 4
            should_search = (repeated_err or self.pivot_count >= 2) and cooldown_ok

            if mission_kind == "assistant" and not getattr(self, '_reply_sent', False):
                should_search = False

            if should_search:
                dead_ends = list(self.strategy_attempts.keys())
                if searchable_blockers:
                    raw_q = f"{searchable_blockers[0]} {self.mission[:50]}"
                elif dead_ends:
                    raw_q = f"solve: {dead_ends[-1][:60]} {self.mission[:40]}"
                else:
                    raw_q = self.mission[:100]
                clean_q = re.sub(r'[A-Za-z0-9]{8}_[a-f0-9]{8}', '', raw_q).strip()[:120]
                if clean_q != self.last_search_query:
                    new_intel = self._ceo_web_search(clean_q)
                    if new_intel:
                        self.web_intelligence = new_intel
                        self.web_search_turn = turn
                        self.last_search_query = clean_q

            # ── Workspace map (skip for short assistant missions — too expensive) ──
            if mission_kind in ("scheduled", "standalone", "delegated"):
                spatial_anchor = self._build_workspace_map()
            else:
                spatial_anchor = ""

            # ── Legacy worker status ──
            _live_workers_now = {
                r: t for r, t in self._async_workers.items() if t.is_alive()
            }
            _elapsed_by_worker = {}
            for _rn, _rt in _live_workers_now.items():
                _dispatched = self._async_dispatch_times.get(_rn, time.time())
                _elapsed_by_worker[_rn] = int((time.time() - _dispatched) // 60)

            if _live_workers_now:
                worker_status = (
                    f"⏳ WORKERS RUNNING: {list(_live_workers_now.keys())} "
                    + ", ".join(f"({r}: {m}min)" for r, m in _elapsed_by_worker.items())
                    + "\n   → Terminal and AST commands are BLOCKED until they finish."
                    + "\n   → Set action_type='WAIT' and nothing else."
                )
            else:
                worker_status = (
                    "✅ NO WORKERS RUNNING — you have full terminal access.\n"
                    "   → Do NOT set WAIT. Use TERMINAL, DELEGATE, or HIRE."
                )

            # ── Token banner ──
            _timeline_chars = sum(len(str(i)) for i in self.conversation_history)
            if _timeline_chars > 80000:
                token_banner = (
                    "🚨 TOKEN EXHAUSTION RISK: CRITICAL — Timeline massive. "
                    "Write master_plan NOW and delegate.\n"
                )
            elif _timeline_chars > 40000:
                token_banner = (
                    "⚠️  TOKEN EXHAUSTION RISK: MEDIUM — Timeline growing. "
                    "Shift to PLANNING soon.\n"
                )
            else:
                token_banner = ""

            env_recon = ""
            if turn == 1 and hasattr(self, '_initial_recon'):
                env_recon = self._initial_recon
                del self._initial_recon

            active_task = self.mission_db.get_active_task()
            allowed_actions = self._allowed_actions_for(mission_kind)

            # ── Recent task results for citation ──
            recent_task_results = []
            try:
                from orchestration.scheduler_db import SchedulerDB
                sched = SchedulerDB(
                    os.path.join(os.getcwd(), "ai_civilization", "scheduler.db")
                )
                recent_task_results = sched.get_recent_task_results(limit=5)
                for r in recent_task_results:
                    token = r.get('citation_token')
                    if token:
                        if not hasattr(self, 'valid_citations'):
                            self.valid_citations = set()
                        self.valid_citations.add(token)
            except Exception:
                pass

            # ── Build prompt ──
            # ────────────────────────────────────────────────────────────
            # FIX: pass mission_kind, delegated_role, parent_thread.
            # Without these, build_ceo_prompt defaulted to kind="standalone"
            # for every mission — including Telegram threads. The CEO never
            # saw the assistant-mode block ("ANSWER, DON'T DESCRIBE", MCP is
            # auto-connected, etc.) and treated every user message as a
            # headless CLI task.
            # ────────────────────────────────────────────────────────────

            prompt = build_ceo_prompt(
                mission=self.mission,
                turn=turn,
                conversation_history=last_turns,
                relevant_context=relevant_context,
                ceo_scratchpad=self.ceo_scratchpad,
                shared_state=self.shared_state,
                master_plan=self.master_plan,
                scratch_dir=self.scratch_dir,
                cwd=os.getcwd(),
                global_lessons=self.global_lessons,
                web_intelligence=self.web_intelligence,
                stagnation_warning=self.stagnation_warning,
                framework_block=framework_block,
                dead_end_warnings=dead_end_warnings,
                available_roles_str=available_roles_str,
                agents=self.agents,
                spatial_anchor=spatial_anchor,
                ceo_playbook=self.ceo_playbook,
                compute_budget=self.compute_budget,
                timeline_chars=_timeline_chars,
                active_schemas=self._active_schemas,
                async_workers_status="",
                worker_status=worker_status,
                framework_hint_block=framework_blocker_hint,
                token_banner=token_banner,
                environment_recon=env_recon,
                forced_decision=forced_decision,
                active_task=active_task,
                mission_db=self.mission_db,
                allowed_actions=allowed_actions,
                inbox_history=getattr(self, 'inbox_history_text', ""),
                recent_task_results=recent_task_results,
                # ── Mission kind + delegation context ──

                mission_kind=mission_kind,
                delegated_role=getattr(self, 'delegated_role', None),
                parent_thread=getattr(self, 'parent_thread_id', None),
                # ── Assistant-flow params ──
                thread_id=getattr(self, 'inbox_thread_id', '') or "",
                user_message=getattr(self, 'user_message', '') or "",
                active_worker_report=getattr(self, '_active_worker_report', '') or "",
                delegated_files=getattr(self, '_delegated_files', None),
            )












            # ── HEARTBEAT: before the LLM call ──
            self._beat(f"turn {turn}: calling LLM")

            # ── LLM call ──
            try:
                response = self.director_llm.call(
                    messages=[{"role": "user", "content": prompt}]
                )
            except Exception as _llm_err:
                self.logs.append(
                    f"[bold red]🌐 LLM CALL FAILED (turn {turn}): "
                    f"{str(_llm_err)[:120]}[/bold red]"
                )
                self.conversation_history.append({
                    "step": f"{turn} (LLM-TIMEOUT)",
                    "agent": "🔧 System",
                    "instruction_text": "",
                    "result": f"⚠️ LLM API call failed: {str(_llm_err)[:200]}",
                    "raw_tool_outputs": [],
                    "structured_results": [],
                })
                self._index_turn(self.conversation_history[-1])
                self.save_history_to_disk()
                time.sleep(3)
                continue

            plan = self._parse_json_response(response)
            action_type = plan.get("action_type")
            payload = plan.get("action_payload", {})

            reasoning = plan.get("reasoning_engine", {})
            self._last_request_type = reasoning.get("request_type", "conversational")
            self._last_verification_method = reasoning.get("verification_method", "none")

            # ── HEARTBEAT: before executing the action ──
            self._beat(f"turn {turn}: executing {action_type}")

            should_continue = self._execute_action(action_type, payload, turn)
            if not should_continue:
                break

        # ── FALLBACK: ensure the mission always ends with a reply ──
        self._ensure_final_reply(turn)

        if not self.is_complete:
            if not (
                getattr(self, 'inbox_thread_id', None)
                and getattr(self, '_reply_sent', False)
            ):
                self.result = self.generate_final_report()
            self.status = "COMPLETED"
            self.is_complete = True
            self.save_history_to_disk()

        # ── Notify TaskManager so the watchdog stops tracking this task ──
        self.mark_finished()

    # ------------------------------------------------------------------
    # Helpers used by the loop
    # ------------------------------------------------------------------
    def _allowed_actions_for(self, mission_kind: str) -> List[str]:
        """Return the action set for this mission type / phase."""
        # Delegated worker: full access to work, then a single SEND_REPLY.
        if mission_kind == "delegated":
            return [
                "CALL_TOOL", "EXECUTE_REPL", "TERMINAL",
                "SEND_REPLY", "FINISH",
            ]

        # Assistant (user inbox): first turn is read-only + reply.
        if mission_kind == "assistant":
            if not getattr(self, '_reply_sent', False):
                return [
                    "CALL_TOOL",      # read-only tools + MCP
                    "EXECUTE_REPL",
                    "SEND_REPLY",     # the actual reply to the user
                    "DELEGATE",       # allow it in one shot: ack + delegate
                    "ASK_USER",
                ]
            return [
                "CALL_TOOL", "EXECUTE_REPL", "SEND_REPLY",
                "DELEGATE", "ASK_USER", "FINISH",
            ]

        # Scheduled / standalone: full set.
        return [
            "CALL_TOOL", "EXECUTE_REPL", "TERMINAL",
            "UPDATE_PLAN", "MODIFY_PLAN", "DEFINE_PRODUCT",
            "HIRE", "DELEGATE", "WAIT",
            "MARK_COMPLETED", "REQUEST_REWORK", "ROLLBACK",
            "SEND_REPLY", "ASK_USER", "FINISH",
        ]

    def _build_workspace_map(self) -> str:
        """Produce a small environment summary for the prompt."""
        try:
            _cwd = os.getcwd()
            _workspace_tree = subprocess.getoutput(
                "find . -maxdepth 4 -type f "
                "-not -path './node_modules/*' "
                "-not -path './.git/*' "
                "-not -path './ai_civilization/scratch/*' "
                "-not -path './ai_civilization/logs/*' "
                "-not -path './ai_civilization/mission_logs/*' "
                "| sort | head -200"
            )
            _live_procs = subprocess.getoutput(
                "ps aux | grep -E 'node|python|ruby|go|rust|java|php|next|nuxt|nest|uvicorn|flask|django|fastapi' "
                "| grep -v grep | awk '{print $1, $2, $11, $12, $13}' | head -15"
            )
            _open_ports = subprocess.getoutput(
                "ss -tlnp 2>/dev/null | grep LISTEN | awk '{print $4, $6}' | head -15"
            )
            return (
                f"\n🌍 WORKSPACE MAP (current directory {_cwd}):\n"
                f"{_workspace_tree}\n"
                f"  Live processes:\n"
                + "".join(f"    • {l}\n" for l in _live_procs.splitlines() if l.strip())
                + f"  Open ports:\n"
                + "".join(f"    • {l}\n" for l in _open_ports.splitlines() if l.strip())
            )
        except Exception:
            return ""

    def _emergency_reply(self, turn: int) -> None:
        """Send a final reply if the budget runs out before the mission finished."""
        if not getattr(self, 'inbox_thread_id', None):
            return
        if getattr(self, '_reply_sent', False):
            return

        if getattr(self, 'is_delegated_task', False):
            # Let SEND_REPLY route back to the CEO via AgentBus.
            # turn=9999 bypasses the tool-first gate.
            self._execute_action(
                "SEND_REPLY",
                {"body": "Compute budget exhausted before completion."},
                turn=9999,
            )
            return

        msg_id_match = re.search(r'message #(\d+)', self.mission)
        message_id = int(msg_id_match.group(1)) if msg_id_match else None
        if message_id is None:
            return

        # turn=9999 bypasses the tool-first gate: emergency messages must
        # reach the user even if no tool has been executed.
        self._execute_action(
            "SEND_REPLY",
            {
                "message_id": message_id,
                "body": (
                    "I'm sorry, but I've run out of compute budget for this task. "
                    "Please try again later."
                ),
                "citations": [],
            },
            turn=9999,
        )

    # ------------------------------------------------------------------
    # Final-reply fallback — HONEST, no fabrication
    # ------------------------------------------------------------------
    # The previous version of this method called the LLM with a prompt of
    # the form:
    #
    #   "You are the CEO … The user sent this message: …
    #    Reply to the user now. Return ONLY the reply text as a plain string."
    #
    # That prompt contains no tools and no action schema, so the LLM
    # correctly reported "I have no tools / I have no SEND_REPLY function"
    # — which then went out to the user as an authoritative-looking reply.
    # It was pure fabrication, produced by a summariser that had no access
    # to the actual mission state.
    #
    # The replacement below never calls the LLM. It sends a short, honest
    # failure that tells the user the system did not complete the request.
    # Combined with the tool-first gate in action_handlers_mixin, this path
    # should now be unreachable for normal assistant missions.
    # ------------------------------------------------------------------
    def _ensure_final_reply(self, turn: int) -> None:
        """
        If the loop exited without a reply, send an honest failure.
        Do NOT call the LLM again with no tools — that fabricates a reply.
        """
        if not getattr(self, 'inbox_thread_id', None):
            return
        if getattr(self, '_reply_sent', False):
            return

        self.logs.append(
            "[yellow]⚠️ Loop exited without a reply. Sending honest failure "
            "(no fabrication).[/yellow]"
        )

        if getattr(self, 'is_delegated_task', False):
            self._execute_action(
                "SEND_REPLY",
                {"body": "Task ended before producing a verified result."},
                turn=9999,
            )
            return

        msg_id_match = re.search(r'message #(\d+)', self.mission)
        message_id = int(msg_id_match.group(1)) if msg_id_match else None
        if message_id is None:
            return

        self._execute_action(
            "SEND_REPLY",
            {
                "message_id": message_id,
                "body": (
                    "I wasn't able to complete this request — my mission ended "
                    "before I produced a verified result. Please try again, or "
                    "rephrase the request."
                ),
                "citations": [],
            },
            turn=9999,
        )
