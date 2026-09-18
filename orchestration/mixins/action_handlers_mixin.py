##############################################

# mixins/action_handlers_mixin.py
#
# Contains the logic for each possible CEO action.
#
# Key architectural note (multi-agent delegation):
#
#   • DELEGATE is FIRE-AND-FORGET. It calls AgentBus.delegate() which spawns
#     a brand-new ActiveTask running in its own thread. The current mission
#     then exits immediately — the CEO does not wait for the worker.
#
#   • SEND_REPLY has two modes:
#       - Assistant / scheduled / standalone → write OUT to the user's thread.
#       - Delegated task (is_delegated_task=True) → write back to the CEO
#         via AgentBus.agent_reply_to_ceo(). The current mission exits.
#
#   • A duplicate-reply guard prevents the same body from being queued twice
#     on the same thread, which was a symptom of the earlier loop bugs.
#
#   • Every exit path that truly ends the mission calls self.mark_finished()
#     so TaskManager's health watchdog stops tracking the task.
#

import os
import re
import json
import time
import uuid

from .helpers_mixin import HelpersMixin
from worker_dispatcher import check_async_workers
from ..role_tools import TOOL_REGISTRY


class ActionHandlersMixin:
    """Contains the logic for each possible CEO action."""

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------
    def _block_ceo(self, reason: str) -> bool:
        """Utility to block a reply and add feedback to CEO blockers."""
        self.logs.append(f"  [bold red]🛑 {reason}[/bold red]")
        self.ceo_scratchpad.blockers.append(reason)
        return True

    def _complete_scheduled_task(self) -> None:
        """Mark the scheduled task that triggered this mission as COMPLETED."""
        if getattr(self, 'scheduled_task_id', None):
            from orchestration.scheduler_db import SchedulerDB
            sched = SchedulerDB(
                os.path.join(os.getcwd(), "ai_civilization", "scheduler.db")
            )
            sched.mark_task_completed(self.scheduled_task_id)

    def _extract_commitments_and_schedule(self, reply_text: str) -> None:
        """
        Detect promises/commitments in the CEO's reply and schedule them.
        DISABLED by default — kept for optional use.
        """
        if not reply_text.strip():
            return

        extraction_prompt = f"""
You are the Chief of Staff. The CEO just sent this message to the user:

"{reply_text}"

Did the CEO promise to do something, investigate something, or dispatch someone?
If yes, extract the promises into concrete technical tasks.
If it's just casual chat, return contains_promises=false.

Return ONLY valid JSON:
{{"contains_promises": true/false, "tasks_to_create": [{{"task_title": "...", "assignee_role": "...", "description": "..."}}]}}
"""
        try:
            raw = self.director_llm.call(
                messages=[{"role": "user", "content": extraction_prompt}]
            )
            cleaned = re.sub(r'```(?:json)?\s*', '', raw).strip("`").strip()
            analysis = json.loads(cleaned)
        except Exception as e:
            self.logs.append(f"⚠️ Commitment extraction failed: {e}")
            return

        if not analysis.get("contains_promises", False):
            return

        tasks = analysis.get("tasks_to_create", [])
        if not tasks:
            return

        from orchestration.scheduler_db import SchedulerDB
        scheduler = SchedulerDB(
            os.path.join(os.getcwd(), "ai_civilization", "scheduler.db")
        )

        for task in tasks:
            title = task.get("task_title", "Follow-up task")
            role = task.get("assignee_role", "")
            desc = task.get("description", "")
            scheduler.add_task(
                title=title,
                due_at=None,
                recurrence=None,
                project_id=None,
                depends_on_task_id=None,
                description=f"Role: {role}. {desc}",
            )
            self.logs.append(
                f"🤖 [Chief of Staff] Queued: {title} for {role or 'general'}"
            )

    # ------------------------------------------------------------------
    # Main dispatcher
    # ------------------------------------------------------------------
    def _execute_action(self, action_type: str, payload: dict, turn: int) -> bool:
        """
        Execute the action specified by action_type.
        Returns True if the loop should continue, False if it should stop
        (mission complete, awaiting user, or delegated task finished).
        """

        # ── WRITE_FILE — blocked, must delegate ──
        if action_type == "WRITE_FILE":
            file_path = payload.get("path", "")
            redirect_msg = (
                f"ACTION REJECTED: As Chief Architect, you attempted to write "
                f"`{file_path}` directly. You are an orchestrator, not an "
                f"individual contributor.\n\n"
                f"SYSTEM HINT: Do not write files yourself. Use DELEGATE to assign "
                f"this work."
            )
            self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
            self.conversation_history.append({
                "step": f"{turn} (SYSTEM-REDIRECT)",
                "agent": "🔧 System",
                "instruction_text": file_path,
                "result": redirect_msg,
                "raw_tool_outputs": [],
                "structured_results": [],
            })
            self._index_turn(self.conversation_history[-1])
            self.save_history_to_disk()
            return True

        # ── CALL_TOOL ──
        if action_type == "CALL_TOOL":
            tool_name = payload.get("tool_name", "").strip()
            tool_args = payload.get("tool_args", {})

            tool = TOOL_REGISTRY.get(tool_name.lower().replace(' ', '_'))
            if not tool:
                try:
                    import gm
                    tool = gm.TOOL_REGISTRY.get(tool_name.lower().replace(' ', '_'))
                except Exception:
                    pass

            if not tool:
                self.logs.append(
                    f"[bold red]❌ Tool '{tool_name}' not found in any registry.[/bold red]"
                )
                return True

            try:
                if hasattr(tool, 'run'):
                    result = tool.run(**tool_args)
                elif hasattr(tool, 'func'):
                    result = tool.func(**tool_args)
                else:
                    result = tool(**tool_args)

                evidence_id = f"ev_{uuid.uuid4().hex[:6]}"
                if not hasattr(self, 'evidence_ledger'):
                    self.evidence_ledger = {}
                self.evidence_ledger[evidence_id] = str(result)

                self._has_tool_executed = True
                if not hasattr(self, 'valid_citations'):
                    self.valid_citations = set()
                self.valid_citations.add(evidence_id)

                self.logs.append(f"  [bold cyan]🔧 CALL_TOOL: {tool_name}[/bold cyan]")
                self.conversation_history.append({
                    "step": f"{turn} (CALL_TOOL)",
                    "agent": "👑 CEO",
                    "instruction_text": f"Tool: {tool_name}, Args: {tool_args}",
                    "result": str(result),
                    "raw_tool_outputs": [str(result)],
                    "structured_results": {"evidence_id": evidence_id},
                })
                self._index_turn(self.conversation_history[-1])
                self.save_history_to_disk()
                return True

            except Exception as e:
                self.logs.append(
                    f"[bold red]❌ Tool call error for {tool_name}: {e}[/bold red]"
                )
                return True

        # ── EXECUTE_REPL ──
        if action_type == "EXECUTE_REPL":
            code = payload.get("code", "")
            if not code:
                self.logs.append("[bold red]❌ EXECUTE_REPL requires code[/bold red]")
                return True

            from gm import run_repl_code
            output = run_repl_code(code)

            evidence_id = f"ev_{uuid.uuid4().hex[:6]}"
            if not hasattr(self, 'evidence_ledger'):
                self.evidence_ledger = {}
            self.evidence_ledger[evidence_id] = output

            self._has_tool_executed = True
            if not hasattr(self, 'valid_citations'):
                self.valid_citations = set()
            self.valid_citations.add(evidence_id)

            self.logs.append(
                f"  [bold cyan]🔁 REPL OUTPUT (Evidence ID: {evidence_id}):"
                f"[/bold cyan]\n{output[:1000]}"
            )
            self.conversation_history.append({
                "step": f"{turn} (EXECUTE_REPL)",
                "agent": "👑 CEO",
                "instruction_text": code,
                "result": output,
                "raw_tool_outputs": [output],
                "structured_results": {"evidence_id": evidence_id},
            })
            self._index_turn(self.conversation_history[-1])
            self.save_history_to_disk()
            return True

        # ── TERMINAL ──
        if action_type == "TERMINAL":
            cmds = payload.get("commands", [])
            if isinstance(cmds, str):
                cmds = [cmds]
            if not cmds:
                return True

            # Delegated workers are the ones allowed to run compilers/installers.
            is_delegated = getattr(self, 'is_delegated_task', False)

            if not is_delegated:
                _FORBIDDEN = [
                    'npm','npx','pip','python','rustc','cargo','go ','sed',
                    'echo','mkdir','touch','rm ','mv ','cp ','git ',
                ]
                if any(bad in str(cmds) for bad in _FORBIDDEN):
                    redirect_msg = (
                        "As Chief Architect, you cannot run compilers, installers, "
                        "or write data. Use DELEGATE."
                    )
                    self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
                    self.conversation_history.append({
                        "step": f"{turn} (SYSTEM-REDIRECT)",
                        "agent": "🔧 System",
                        "instruction_text": str(cmds),
                        "result": redirect_msg,
                        "raw_tool_outputs": [],
                        "structured_results": [],
                    })
                    self._index_turn(self.conversation_history[-1])
                    self.save_history_to_disk()
                    return True

            _live = {r: t for r, t in self._async_workers.items() if t.is_alive()}
            if _live:
                redirect_msg = (
                    f"Workers {list(_live.keys())} are still running. "
                    f"Set action_type='WAIT'."
                )
                self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
                self.conversation_history.append({
                    "step": f"{turn} (SYSTEM-REDIRECT)",
                    "agent": "🔧 System",
                    "instruction_text": str(cmds),
                    "result": redirect_msg,
                    "raw_tool_outputs": [],
                    "structured_results": [],
                })
                self._index_turn(self.conversation_history[-1])
                self.save_history_to_disk()
                return True

            self.logs.append(
                f"  [bold cyan]👁️ TERMINAL: {cmds} (batch of {len(cmds)})[/bold cyan]"
            )
            outputs = [f"$ {cmd}\n{self._run_single_terminal(cmd)}" for cmd in cmds]
            vision_out = "\n\n---\n\n".join(outputs)[:25000]
            self.vision_streak.extend(cmds)
            self.compute_budget -= 0.50 * len(cmds)
            self._idle_turns = 0
            self._consecutive_blocks = 0
            self.conversation_history.append({
                "step": f"{turn} (TERMINAL)",
                "agent": "👑 CEO",
                "instruction_text": str(cmds),
                "result": vision_out,
                "raw_tool_outputs": [],
                "structured_results": [],
            })
            self._index_turn(self.conversation_history[-1])
            self.save_history_to_disk()
            return True

        # ── UPDATE_PLAN / MODIFY_PLAN ──
        if action_type in ("UPDATE_PLAN", "MODIFY_PLAN"):
            mutation = payload.get("mutation", "").upper()

            if mutation == "MARK_TASK_DONE":
                task_id = payload.get("task_id")
                if not task_id:
                    active_task = self.mission_db.get_active_task()
                    tid_hint = ""
                    if active_task:
                        tid_hint = (
                            f" For example, the current pending task is "
                            f"**Task #{active_task['id']}**. Use `task_id: {active_task['id']}`."
                        )
                    redirect_msg = (
                        "You must provide a `task_id` from the LIVE MISSION PLAN."
                        + tid_hint
                    )
                    self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
                    self.conversation_history.append({
                        "step": f"{turn} (SYSTEM-REDIRECT)",
                        "agent": "🔧 System",
                        "instruction_text": str(payload),
                        "result": redirect_msg,
                        "raw_tool_outputs": [],
                        "structured_results": [],
                    })
                    self._index_turn(self.conversation_history[-1])
                    self.save_history_to_disk()
                    return True

                result_msg = self.mission_db.handle_plan_mutation(
                    mutation, "", "", "", "", "", task_id=task_id
                )
                plan_path = os.path.join(self.scratch_dir, "plan.md")
                self.mission_db.render_markdown(plan_path)
                self.logs.append(
                    f"  [bold green]🛠️ UPDATE_PLAN ({mutation}): {result_msg}[/bold green]"
                )
                self.conversation_history.append({
                    "step": f"{turn} (UPDATE_PLAN)",
                    "agent": "👑 CEO",
                    "instruction_text": f"{mutation} on task {task_id}",
                    "result": result_msg,
                    "raw_tool_outputs": [],
                    "structured_results": [],
                })
                self._index_turn(self.conversation_history[-1])
                self.save_history_to_disk()
                return True

            phase_title = payload.get("phase_title", "").strip()
            task_description = payload.get("task_description", "").strip()
            if phase_title and task_description:
                result_msg = self.mission_db.handle_plan_mutation(
                    mutation,
                    phase_title,
                    task_description,
                    payload.get("assigned_role", "").strip(),
                    payload.get("deliverable_file", "").strip(),
                    payload.get("tools_allowed", "").strip(),
                    task_id=payload.get("task_id"),
                )
                plan_path = os.path.join(self.scratch_dir, "plan.md")
                self.mission_db.render_markdown(plan_path)
                self.logs.append(
                    f"  [bold green]🛠️ UPDATE_PLAN ({mutation}): {result_msg}[/bold green]"
                )
                self.conversation_history.append({
                    "step": f"{turn} (UPDATE_PLAN)",
                    "agent": "👑 CEO",
                    "instruction_text": f"{mutation} on {phase_title}",
                    "result": result_msg,
                    "raw_tool_outputs": [],
                    "structured_results": [],
                })
                self._index_turn(self.conversation_history[-1])
                self.save_history_to_disk()
            else:
                self.logs.append(
                    "[bold red]❌ UPDATE_PLAN requires phase_title and task_description[/bold red]"
                )
            return True

        # ── DEFINE_PRODUCT ──
        if action_type == "DEFINE_PRODUCT":
            description = payload.get("description", "")
            files = payload.get("deliverable_files", [])
            if description and files:
                self.ceo_scratchpad.final_product_defined = True
                self.ceo_scratchpad.final_product_description = description
                self.ceo_scratchpad.required_deliverable_files = files
                self.logs.append(
                    f"  [bold green]✅ Final product defined: "
                    f"{description[:80]}...[/bold green]"
                )
                self.conversation_history.append({
                    "step": f"{turn} (DEFINE_PRODUCT)",
                    "agent": "👑 CEO",
                    "instruction_text": "Final product definition",
                    "result": f"Description: {description[:200]} | Files: {files}",
                    "raw_tool_outputs": [],
                    "structured_results": [],
                })
                self._index_turn(self.conversation_history[-1])
                self.save_history_to_disk()
            else:
                self.logs.append(
                    "[bold red]❌ DEFINE_PRODUCT requires description and deliverable_files[/bold red]"
                )
            return True

        # ── HIRE / DELEGATE gating (product must be defined for standalone) ──
        is_inbox_task = getattr(self, 'inbox_thread_id', None) is not None
        if (
            action_type in ("HIRE", "DELEGATE")
            and not self.ceo_scratchpad.final_product_defined
            and not is_inbox_task
        ):
            self.ceo_scratchpad.blockers.insert(
                0,
                "[NO PRODUCT DEFINED] You must first define the final product "
                "using DEFINE_PRODUCT. Provide a description and the list of "
                "deliverable files that must exist for the mission to be complete.",
            )
            self.logs.append("[bold red]🚫 BLOCKED: Product not defined yet.[/bold red]")
            return True

        # ── HIRE ──
        if action_type == "HIRE":
            role = payload.get("role", "").strip()
            goal = payload.get("goal", "").strip()
            backstory = payload.get("backstory", "").strip()
            initial_instruction = payload.get("initial_instruction", "").strip()
            if not (role and goal and backstory):
                self.logs.append(
                    "[bold red]❌ HIRE requires role, goal, backstory[/bold red]"
                )
                return True

            new_agent = self._spawn_agent(role, goal, backstory)
            if not new_agent:
                self.logs.append(f"[bold red]❌ Failed to hire {role}[/bold red]")
                return True

            self.logs.append(f"[bold green]✅ HIRED: {role}[/bold green]")
            self.conversation_history.append({
                "step": f"{turn} (HIRE)",
                "agent": "👑 CEO",
                "instruction_text": f"Hired {role}",
                "result": f"✅ Agent '{role}' created with goal: {goal[:100]}...",
                "raw_tool_outputs": [],
                "structured_results": [],
            })
            self._index_turn(self.conversation_history[-1])
            self.save_history_to_disk()
            return True

        # ── DELEGATE (fire-and-forget via AgentBus) ──
        if action_type == "DELEGATE":
            # No recursive delegation from a delegated worker.
            if getattr(self, 'is_delegated_task', False):
                self.logs.append(
                    "[bold yellow]🛑 DELEGATE blocked inside a delegated task[/bold yellow]"
                )
                return True

            role = payload.get("role", "").strip()
            instruction = payload.get("instruction", "").strip()
            assigned_tools = payload.get("assigned_tools", ["file_manager", "ast_inspector"])

            if not role or not instruction:
                self.logs.append(
                    "[bold red]❌ DELEGATE requires role and instruction[/bold red]"
                )
                return True

            # Filter assigned_tools against registries, guarantee file_manager.
            valid_tools = set(TOOL_REGISTRY.keys())
            try:
                import gm
                valid_tools.update(gm.TOOL_REGISTRY.keys())
            except Exception:
                pass
            safe_tools = [t for t in assigned_tools if t in valid_tools]
            if "file_manager" not in safe_tools:
                safe_tools.append("file_manager")

            # Determine the parent thread and message id for the reply route.
            parent_thread = getattr(self, 'inbox_thread_id', None) or "console"
            m = re.search(r'message #(\d+)', self.mission)
            parent_msg_id = int(m.group(1)) if m else None

            try:
                from orchestration.agent_bus import AgentBus
                bus = AgentBus(
                    inbox_db_path=os.path.join(
                        os.getcwd(), "ai_civilization", "inbox.db"
                    ),
                    workspace_root=os.getcwd(),
                    task_manager=self.task_manager,
                )
                result = bus.delegate(
                    role=role,
                    instruction=instruction,
                    assigned_tools=safe_tools,
                    parent_thread=parent_thread,
                    parent_message_id=parent_msg_id,
                )
                self.logs.append(
                    f"[bold green]✅ Delegated '{role}' → thread "
                    f"{result['thread_id']} (task {result['task_id']})[/bold green]"
                )
            except Exception as e:
                self.logs.append(f"[bold red]❌ Delegation failed: {e}[/bold red]")
                self.logs.append(
                    "[dim]Falling back to in-mission worker dispatch…[/dim]"
                )
                # Fallback: legacy in-mission dispatch (blocking behaviour).
                subordinates = [a for a in self.agents if a.role != "The Global CEO"]
                self._dispatch_worker(role, instruction, turn, subordinates)
                return True

            # Fire-and-forget: exit THIS mission. The agent runs independently.
            self.result = f"Delegated to {role}: {instruction[:150]}"
            self.status = "COMPLETED"
            self.is_complete = True
            self.save_history_to_disk()
            self.mark_finished()
            return False

        # ── WAIT ──
        if action_type == "WAIT":
            should_wait = check_async_workers(
                async_workers=self._async_workers,
                async_dispatch_times=self._async_dispatch_times,
                async_results=self._async_results,
                async_events=self._async_events,
                async_fail_counts=self._async_fail_counts,
                ceo_scratchpad=self.ceo_scratchpad,
                logger=self.logs.append,
                conversation_history=self.conversation_history,
                turn=turn,
            )
            if should_wait:
                self._consecutive_duplicate_blocks = 0
                time.sleep(10)
            else:
                self.logs.append("  [dim]No workers running. CEO should act.[/dim]")
            return True

        # ── MARK_COMPLETED ──
        if action_type == "MARK_COMPLETED":
            pending_phase_id = self.mission_db.get_pending_phase_id()
            if pending_phase_id:
                self.ceo_scratchpad.blockers = [
                    b for b in self.ceo_scratchpad.blockers if "FORCED DECISION" not in b
                ]
                self.logs.append(
                    f"  [bold green]✅ Phase {pending_phase_id} acknowledged. Advancing.[/bold green]"
                )
            return True

        # ── REQUEST_REWORK ──
        if action_type == "REQUEST_REWORK":
            feedback = payload.get("feedback", "")
            pending_phase_id = self.mission_db.get_pending_phase_id()
            if pending_phase_id:
                tasks = self.mission_db.conn.execute(
                    "SELECT id FROM tasks WHERE phase_id = ? ORDER BY id DESC LIMIT 1",
                    (pending_phase_id,),
                ).fetchone()
                if tasks:
                    task_id = tasks[0]
                    self.mission_db.record_rework_feedback(task_id, feedback)
                    self.mission_db.update_task_status(task_id, "NEEDS_REWORK", feedback)
                self.ceo_scratchpad.blockers = [
                    b for b in self.ceo_scratchpad.blockers if "FORCED DECISION" not in b
                ]
                self.logs.append(
                    f"  [bold yellow]🔄 Rework feedback recorded for task {task_id}[/bold yellow]"
                )
            return True

        # ── ROLLBACK ──
        if action_type == "ROLLBACK":
            checkpoint_id = payload.get("checkpoint_id")
            if checkpoint_id:
                success = self.mission_db.rollback_to_checkpoint(
                    int(checkpoint_id), self.scratch_dir
                )
                if success:
                    self.logs.append(
                        f"[bold green]⏪ Rolled back to checkpoint {checkpoint_id}.[/bold green]"
                    )
                    self.ceo_scratchpad.blockers = []
                    self.ceo_scratchpad.confidence = 80
                else:
                    self.logs.append("[red]❌ Rollback failed.[/red]")
            return True

        # ── FINISH ──
        if action_type == "FINISH":
            self._consecutive_duplicate_blocks = 0
            report = payload.get("report", "")

            # Ensure a reply was sent before finishing.
            if getattr(self, 'inbox_thread_id', None) and not getattr(self, '_reply_sent', False):
                if getattr(self, 'is_delegated_task', False):
                    # Route to AgentBus via SEND_REPLY.
                    self._execute_action(
                        "SEND_REPLY",
                        {"body": report or "Task completed."},
                        turn,
                    )
                else:
                    msg_id_match = re.search(r'message #(\d+)', self.mission)
                    message_id = int(msg_id_match.group(1)) if msg_id_match else None
                    if message_id is not None:
                        default_reply = report or (
                            "I have completed the requested task. "
                            "Let me know if you need anything else."
                        )
                        self._execute_action(
                            "SEND_REPLY",
                            {
                                "message_id": message_id,
                                "body": default_reply,
                                "citations": [],
                            },
                            turn,
                        )

            # For assistant missions, FINISH is immediate — no plan gates.
            mission_kind = self._mission_kind()
            if mission_kind in ("scheduled", "standalone"):
                pending_phase = self.mission_db.get_pending_phase_id()
                if pending_phase is not None:
                    phase_row = self.mission_db.conn.execute(
                        "SELECT title, status FROM phases WHERE id = ?",
                        (pending_phase,),
                    ).fetchone()
                    phase_title = phase_row[0] if phase_row else f"ID {pending_phase}"
                    _finish_block_msg = (
                        f"🚫 PREMATURE FINISH BLOCKED: You still have uncompleted "
                        f"phases in your plan (Pending: '{phase_title}'). "
                        f"You cannot call FINISH until every phase is marked COMPLETED."
                    )
                    self.ceo_scratchpad.blockers = [
                        b for b in self.ceo_scratchpad.blockers
                        if "PREMATURE FINISH BLOCKED" not in b
                    ]
                    self.ceo_scratchpad.blockers.append(_finish_block_msg)
                    self.logs.append(f"[bold red]{_finish_block_msg}[/bold red]")
                    self.conversation_history.append({
                        "step": f"{turn} (FINISH-BLOCKED)",
                        "agent": "🔧 System Enforcement",
                        "instruction_text": "Attempted premature FINISH",
                        "result": _finish_block_msg,
                        "raw_tool_outputs": [],
                        "structured_results": [],
                    })
                    self._index_turn(self.conversation_history[-1])
                    self.save_history_to_disk()
                    return True

                if self.ceo_scratchpad.final_product_defined:
                    missing = [
                        f for f in self.ceo_scratchpad.required_deliverable_files
                        if not os.path.exists(f)
                    ]
                    if missing:
                        _gate_msg = (
                            "🚫 FINAL DELIVERABLE GATE: The following required files "
                            "are missing:\n"
                            + "\n".join(f"  • {f}" for f in missing)
                            + "\n\nDELEGATE a worker to create them."
                        )
                        self.ceo_scratchpad.blockers = [
                            b for b in self.ceo_scratchpad.blockers
                            if "FINAL DELIVERABLE GATE" not in b
                        ]
                        self.ceo_scratchpad.blockers.append(_gate_msg)
                        self.conversation_history.append({
                            "step": f"{turn} (DELIVERABLE-GATE-FAIL)",
                            "agent": "🔧 Final Deliverable Gate",
                            "instruction_text": "pre‑FINISH check",
                            "result": _gate_msg,
                            "raw_tool_outputs": [],
                            "structured_results": [],
                        })
                        self._index_turn(self.conversation_history[-1])
                        self.save_history_to_disk()
                        return True

                _gate_blocked, _gate_msg = self._run_completion_gate()
                if _gate_blocked:
                    self.ceo_scratchpad.blockers = [
                        b for b in self.ceo_scratchpad.blockers
                        if "COMPLETION GATE BLOCKED" not in b
                    ]
                    self.ceo_scratchpad.blockers.append(_gate_msg)
                    self.conversation_history.append({
                        "step": f"{turn} (BUILD-GATE-FAIL)",
                        "agent": "🔧 Completion Gate",
                        "instruction_text": "completion gate",
                        "result": _gate_msg,
                        "raw_tool_outputs": [],
                        "structured_results": [],
                    })
                    self._index_turn(self.conversation_history[-1])
                    self.save_history_to_disk()
                    return True

            # Preserve the reply as the final result if it exists.
            if getattr(self, '_reply_sent', False):
                final_result = self.result
            else:
                final_result = report if report else self.generate_final_report()

            self.logs.append(
                "  [bold green]✅ CEO declared FINISHED — mission complete[/bold green]"
            )
            try:
                self.mission_db.create_checkpoint("mission_complete", self.scratch_dir)
            except Exception:
                pass
            self.result = final_result

            self._save_task_result()

            self.status = "COMPLETED"
            self.is_complete = True
            self.save_history_to_disk()
            if self.task_manager:
                self.task_manager.trigger_dream_state()
            self._complete_scheduled_task()

            self.mark_finished()

            return False

        # ── CLARIFY ──
        if action_type == "CLARIFY":
            self._consecutive_duplicate_blocks = 0
            question = payload.get("question", "").strip()
            if not question:
                self.logs.append("[bold red]❌ CLARIFY requires a question[/bold red]")
                return True
            self._clarify_count += 1
            if self._clarify_count > 2:
                self.logs.append(
                    f"\n[bold red]🚫 CLARIFY BLOCKED (#{self._clarify_count-1})[/bold red]"
                )
                self.conversation_history.append({
                    "step": f"{turn} (CLARIFY-BLOCKED)",
                    "agent": "🔧 System Enforcement",
                    "instruction_text": question[:120],
                    "result": "🚨 CLARIFICATION LIMIT REACHED. Proceed with assumptions.",
                    "raw_tool_outputs": [],
                    "structured_results": [],
                })
                self._index_turn(self.conversation_history[-1])
                self.save_history_to_disk()
                return True
            self.logs.append(
                f"\n[bold yellow]🤔 CEO REQUESTS CLARIFICATION:[/bold yellow]\n"
                f"[italic]{question}[/italic]\n"
            )
            self.status = "AWAITING_OVERLORD"
            self._clarify_question = question
            return False

        # ── SEND_REPLY ──
        if action_type == "SEND_REPLY":
            body = payload.get("body", "")
            citations = payload.get("citations", [])
            attachments = payload.get("attachments", [])

            # ── DELEGATED TASK: route reply back to CEO via AgentBus ──
            if getattr(self, 'is_delegated_task', False):
                if not body.strip():
                    self.logs.append("[bold red]❌ SEND_REPLY requires a body[/bold red]")
                    return True
                try:
                    from orchestration.agent_bus import AgentBus
                    bus = AgentBus(
                        inbox_db_path=os.path.join(
                            os.getcwd(), "ai_civilization", "inbox.db"
                        ),
                        workspace_root=os.getcwd(),
                        task_manager=self.task_manager,
                    )
                    bus.agent_reply_to_ceo(
                        agent_thread=self.agent_thread_id,
                        parent_thread=self.parent_thread_id,
                        parent_message_id=self.parent_message_id,
                        result=body,
                        files=attachments,
                    )
                    self._reply_sent = True
                    self.result = body
                    self.logs.append(
                        "[green]📤 Result posted to CEO's inbox[/green]"
                    )
                    self.status = "COMPLETED"
                    self.is_complete = True
                    self.save_history_to_disk()

                    self.mark_finished()
                    return False
                except Exception as e:
                    self.logs.append(
                        f"[bold red]❌ Failed to route reply to CEO: {e}[/bold red]"
                    )
                    return True

            # ── USER-FACING reply ──
            message_id = payload.get("message_id")
            request_type = getattr(self, '_last_request_type', "conversational")

            # Hard gate: factual/coding replies require citations.
            if request_type in ("factual", "coding"):
                if not citations:
                    return self._block_ceo(
                        "🛑 BLOCKED: Factual replies require at least one citation ID."
                    )
                for cid in citations:
                    if cid not in getattr(self, 'valid_citations', set()):
                        return self._block_ceo(
                            f"🛑 BLOCKED: Invalid citation '{cid}'. Use only "
                            f"evidence IDs or task tokens shown in your context."
                        )

            if not message_id or not body:
                self.logs.append(
                    "[bold red]❌ SEND_REPLY requires message_id and body[/bold red]"
                )
                return True

            # ── Deduplicate: never send the same body twice on the same thread ──
            if getattr(self, 'inbox_db', None) and getattr(self, 'inbox_thread_id', None):
                try:
                    recent = self.inbox_db.get_thread_history(
                        self.inbox_thread_id, limit=5
                    )
                    for msg in recent:
                        if (
                            msg.get("direction") == "OUT"
                            and (msg.get("body") or "").strip() == body.strip()
                        ):
                            self.logs.append(
                                "[yellow]🛑 Duplicate reply suppressed "
                                "(same body already sent).[/yellow]"
                            )
                            self._reply_sent = True
                            self.result = body
                            return True
                except Exception:
                    pass

            if hasattr(self, 'inbox_db') and self.inbox_db:
                try:
                    self.inbox_db.mark_replied(message_id, body)
                except Exception:
                    pass

            # Queue the OUT message for delivery.
            if (
                getattr(self, 'inbox_thread_id', None)
                and getattr(self, 'inbox_db', None)
            ):
                self.inbox_db.add_message(
                    thread_id=self.inbox_thread_id,
                    direction="OUT",
                    body=body,
                    sender="CEO",
                    recipient="user",
                    status="PENDING_DELIVERY",
                )
                self.logs.append(
                    f"  [green]📤 OUT message queued for thread "
                    f"{self.inbox_thread_id}[/green]"
                )

            self._reply_sent = True
            self.result = body
            self.save_history_to_disk()
            # Continue the loop; mission ends with FINISH (or the fallback).
            return True

        # ── ASK_USER ──
        if action_type == "ASK_USER":
            thread_id = payload.get("thread_id")
            question = payload.get("question", "")
            if not thread_id or not question:
                self.logs.append(
                    "[bold red]❌ ASK_USER requires thread_id and question[/bold red]"
                )
                return True
            if hasattr(self, 'inbox_db') and self.inbox_db:
                self.inbox_db.add_message(
                    thread_id=thread_id,
                    direction="OUT",
                    body=question,
                    sender="CEO",
                    recipient="user",
                    status="PENDING_DELIVERY",
                )
            self.status = "AWAITING_USER_REPLY"
            self._clarify_question = question
            return False

        # ── Unrecognised action ──
        self.logs.append(f"  [dim]⚠️ Unrecognised or empty action: {action_type}[/dim]")
        self._idle_turns += 1
        if self._idle_turns >= 3:
            self.logs.append(
                f"  [bold red]🚫 IDLE LOOP #{self._idle_turns}: CEO took no action.[/bold red]"
            )
            self.conversation_history.append({
                "step": f"{turn} (IDLE-ERROR)",
                "agent": "🔧 System Enforcement",
                "instruction_text": "Empty action",
                "result": "🚨 IDLE LOOP: Take an action.",
                "raw_tool_outputs": [],
                "structured_results": [],
            })
            self._index_turn(self.conversation_history[-1])
            self.save_history_to_disk()
        return True
