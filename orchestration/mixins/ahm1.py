import os, re, json, time
from .helpers_mixin import HelpersMixin
from worker_dispatcher import check_async_workers
from ..role_tools import TOOL_REGISTRY

class ActionHandlersMixin:
    """Contains the logic for each possible CEO action."""

    def _execute_action(self, action_type: str, payload: dict, turn: int) -> bool:
        """
        Execute the action specified by action_type.
        Returns True if the loop should continue, False if it should stop (mission complete or awaiting user).
        """
        # ── GRACEFUL REDIRECT INTERCEPTORS ──
        if action_type == "WRITE_FILE":
            file_path = payload.get("path", "")
            redirect_msg = (
                f"ACTION REJECTED: As Chief Architect, you attempted to write `{file_path}` directly. "
                "You are an orchestrator, not an individual contributor.\n\n"
                "SYSTEM HINT: Do not write files yourself. Use the DELEGATE tool to assign this work."
            )
            self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
            self.conversation_history.append({
                "step": f"{turn} (SYSTEM-REDIRECT)", "agent": "🔧 System", "instruction_text": file_path,
                "result": redirect_msg, "raw_tool_outputs": [], "structured_results": []
            })
            self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
            return True

        # ── TERMINAL ──
        if action_type == "TERMINAL":
            cmds = payload.get("commands", [])
            if isinstance(cmds, str): cmds = [cmds]
            if cmds:
                _FORBIDDEN = ['npm','npx','pip','python','rustc','cargo','go ','sed','echo','mkdir','touch','rm ','mv ','cp ','git ']
                if any(bad in str(cmds) for bad in _FORBIDDEN):
                    redirect_msg = "As Chief Architect, you cannot run compilers, installers, or write data. Use DELEGATE."
                    self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
                    self.conversation_history.append({
                        "step": f"{turn} (SYSTEM-REDIRECT)", "agent": "🔧 System", "instruction_text": str(cmds),
                        "result": redirect_msg, "raw_tool_outputs": [], "structured_results": []
                    })
                    self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                    return True
                _live = {r: t for r, t in self._async_workers.items() if t.is_alive()}
                if _live:
                    redirect_msg = f"Workers {list(_live.keys())} are still running. Set action_type='WAIT'."
                    self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
                    self.conversation_history.append({
                        "step": f"{turn} (SYSTEM-REDIRECT)", "agent": "🔧 System", "instruction_text": str(cmds),
                        "result": redirect_msg, "raw_tool_outputs": [], "structured_results": []
                    })
                    self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                    return True
                if self._seen_worker_output or self._verification_turns_allowed <= 0:
                    active_task = self.mission_db.get_active_task()
                    tid_hint = ""
                    if active_task:
                        tid_hint = f" The active task is **Task #{active_task['id']}** (see LIVE MISSION PLAN above). Use `task_id: {active_task['id']}`."
                    redirect_msg = (
                        "You already have the worker's output." + tid_hint +
                        " Use UPDATE_PLAN with mutation=MARK_TASK_DONE and the task_id."
                    )
                    self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
                    self.conversation_history.append({
                        "step": f"{turn} (SYSTEM-REDIRECT)", "agent": "🔧 System", "instruction_text": str(cmds),
                        "result": redirect_msg, "raw_tool_outputs": [], "structured_results": []
                    })
                    self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                    return True
                self.logs.append(f"  [bold cyan]👁️ TERMINAL: {cmds} (batch of {len(cmds)})[/bold cyan]")
                outputs = [f"$ {cmd}\n{self._run_single_terminal(cmd)}" for cmd in cmds]
                vision_out = "\n\n---\n\n".join(outputs)[:25000]
                self.vision_streak.extend(cmds)
                self.compute_budget -= 0.50 * len(cmds)
                self._idle_turns = 0; self._consecutive_blocks = 0
                self.conversation_history.append({
                    "step": f"{turn} (TERMINAL)", "agent": "👑 CEO", "instruction_text": str(cmds),
                    "result": vision_out, "raw_tool_outputs": [], "structured_results": []
                })
                self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
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
                        tid_hint = f" For example, the current pending task is **Task #{active_task['id']}**. Use `task_id: {active_task['id']}`."
                    redirect_msg = (
                        "You must provide a `task_id` from the LIVE MISSION PLAN." + tid_hint
                    )
                    self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
                    self.conversation_history.append({
                        "step": f"{turn} (SYSTEM-REDIRECT)", "agent": "🔧 System", "instruction_text": str(payload),
                        "result": redirect_msg, "raw_tool_outputs": [], "structured_results": []
                    })
                    self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                    return True
                result_msg = self.mission_db.handle_plan_mutation(
                    mutation, "", "", "", "", "", task_id=task_id
                )
                plan_path = os.path.join(self.scratch_dir, "plan.md")
                self.mission_db.render_markdown(plan_path)
                self.logs.append(f"  [bold green]🛠️ UPDATE_PLAN ({mutation}): {result_msg}[/bold green]")
                self.conversation_history.append({
                    "step": f"{turn} (UPDATE_PLAN)", "agent": "👑 CEO",
                    "instruction_text": f"{mutation} on task {task_id}",
                    "result": result_msg, "raw_tool_outputs": [], "structured_results": []
                })
                self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                return True
            # Other mutations
            phase_title = payload.get("phase_title", "").strip()
            task_description = payload.get("task_description", "").strip()
            if phase_title and task_description:
                result_msg = self.mission_db.handle_plan_mutation(
                    mutation, phase_title, task_description,
                    payload.get("assigned_role", "").strip(),
                    payload.get("deliverable_file", "").strip(),
                    payload.get("tools_allowed", "").strip(),
                    task_id=payload.get("task_id")
                )
                plan_path = os.path.join(self.scratch_dir, "plan.md")
                self.mission_db.render_markdown(plan_path)
                self.logs.append(f"  [bold green]🛠️ UPDATE_PLAN ({mutation}): {result_msg}[/bold green]")
                self.conversation_history.append({
                    "step": f"{turn} (UPDATE_PLAN)", "agent": "👑 CEO",
                    "instruction_text": f"{mutation} on {phase_title}",
                    "result": result_msg, "raw_tool_outputs": [], "structured_results": []
                })
                self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
            else:
                self.logs.append("[bold red]❌ UPDATE_PLAN requires phase_title and task_description[/bold red]")
            return True

        # ── DEFINE_PRODUCT ──
        if action_type == "DEFINE_PRODUCT":
            description = payload.get("description", "")
            files = payload.get("deliverable_files", [])
            if description and files:
                self.ceo_scratchpad.final_product_defined = True
                self.ceo_scratchpad.final_product_description = description
                self.ceo_scratchpad.required_deliverable_files = files
                self.logs.append(f"  [bold green]✅ Final product defined: {description[:80]}...[/bold green]")
                self.conversation_history.append({
                    "step": f"{turn} (DEFINE_PRODUCT)", "agent": "👑 CEO", "instruction_text": "Final product definition",
                    "result": f"Description: {description[:200]} | Files: {files}",
                    "raw_tool_outputs": [], "structured_results": []
                })
                self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
            else:
                self.logs.append("[bold red]❌ DEFINE_PRODUCT requires description and deliverable_files[/bold red]")
            return True

        # ── BLOCKED HIRE/DELEGATE if product not defined ──
        if action_type in ("HIRE", "DELEGATE") and not self.ceo_scratchpad.final_product_defined:
            self.ceo_scratchpad.blockers.insert(0,
                "[NO PRODUCT DEFINED] You must first define the final product using DEFINE_PRODUCT. "
                "Provide a description and the list of deliverable files that must exist for the mission to be complete."
            )
            self.logs.append("[bold red]🚫 BLOCKED: Product not defined yet.[/bold red]")
            return True

        # ── HIRE ──
        if action_type == "HIRE":
            role = payload.get("role", "").strip()
            goal = payload.get("goal", "").strip()
            backstory = payload.get("backstory", "").strip()
            initial_instruction = payload.get("initial_instruction", "").strip()
            if role and goal and backstory:
                new_agent = self._spawn_agent(role, goal, backstory)
                if new_agent:
                    self.logs.append(f"[bold green]✅ HIRED: {role}[/bold green]")
                    self.conversation_history.append({
                        "step": f"{turn} (HIRE)", "agent": "👑 CEO", "instruction_text": f"Hired {role}",
                        "result": f"✅ Agent '{role}' created with goal: {goal[:100]}...",
                        "raw_tool_outputs": [], "structured_results": []
                    })
                    subordinates = [a for a in self.agents if a.role != "The Global CEO"]
                    if initial_instruction:
                        self.logs.append(f"  [bold yellow]⚡ Initial instruction provided – dispatching {role} immediately.[/bold yellow]")
                        self._dispatch_worker(role, initial_instruction, turn, subordinates)
                        self._post_release_waits = 0
                    else:
                        self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                        self._consecutive_duplicate_blocks = 0
                else:
                    self.logs.append(f"[bold red]❌ Failed to hire {role}[/bold red]")
            else:
                self.logs.append("[bold red]❌ HIRE requires role, goal, backstory[/bold red]")
            return True

        # ── DELEGATE ──
        if action_type == "DELEGATE":
            role = payload.get("role", "").strip()
            instruction = payload.get("instruction", "").strip()
            assigned_tools = payload.get("assigned_tools", ["file_manager", "ast_inspector"])
            valid_tools = set(TOOL_REGISTRY.keys())
            safe_tools = [t for t in assigned_tools if t in valid_tools]
            if "file_manager" not in safe_tools:
                safe_tools.append("file_manager")
            if role and instruction:
                if not any(role.lower() in a.role.lower() for a in self.agents):
                    self.logs.append(f"[bold yellow]⚠️ Agent '{role}' not found. Auto-hiring with tools: {safe_tools}[/bold yellow]")
                    new_agent = self._spawn_agent_with_tools(role, f"Execute tasks related to {role}.", f"Expert in {role}.", safe_tools)
                    if not new_agent:
                        self.logs.append(f"[bold red]❌ Failed to auto-hire '{role}'. Skipping delegation.[/bold red]")
                        return True
                else:
                    agent_obj = next(a for a in self.agents if role.lower() in a.role.lower())
                    agent_obj.tools = [TOOL_REGISTRY[name] for name in safe_tools if name in TOOL_REGISTRY]
                subordinates = [a for a in self.agents if a.role != "The Global CEO"]
                self._dispatch_worker(role, instruction, turn, subordinates)
            return True

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
                return True
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
                self.logs.append(f"  [bold green]✅ Phase {pending_phase_id} acknowledged. Advancing.[/bold green]")
            return True

        # ── REQUEST_REWORK ──
        if action_type == "REQUEST_REWORK":
            feedback = payload.get("feedback", "")
            pending_phase_id = self.mission_db.get_pending_phase_id()
            if pending_phase_id:
                tasks = self.mission_db.conn.execute(
                    "SELECT id FROM tasks WHERE phase_id = ? ORDER BY id DESC LIMIT 1", (pending_phase_id,)
                ).fetchone()
                if tasks:
                    task_id = tasks[0]
                    self.mission_db.record_rework_feedback(task_id, feedback)
                    self.mission_db.update_task_status(task_id, "NEEDS_REWORK", feedback)
                self.ceo_scratchpad.blockers = [
                    b for b in self.ceo_scratchpad.blockers if "FORCED DECISION" not in b
                ]
                self.logs.append(f"  [bold yellow]🔄 Rework feedback recorded for task {task_id}[/bold yellow]")
            return True

        # ── ROLLBACK ──
        if action_type == "ROLLBACK":
            checkpoint_id = payload.get("checkpoint_id")
            if checkpoint_id:
                success = self.mission_db.rollback_to_checkpoint(int(checkpoint_id), self.scratch_dir)
                if success:
                    self.logs.append(f"[bold green]⏪ Rolled back to checkpoint {checkpoint_id}.[/bold green]")
                    self.ceo_scratchpad.blockers = []
                    self.ceo_scratchpad.confidence = 80
                else:
                    self.logs.append(f"[red]❌ Rollback failed.[/red]")
            return True

        # ── FINISH ──
        if action_type == "FINISH":
            self._consecutive_duplicate_blocks = 0
            report = payload.get("report", "")
            pending_phase = self.mission_db.get_pending_phase_id()
            if pending_phase is not None:
                phase_row = self.mission_db.conn.execute("SELECT title, status FROM phases WHERE id = ?", (pending_phase,)).fetchone()
                phase_title = phase_row[0] if phase_row else f"ID {pending_phase}"
                _finish_block_msg = (
                    f"🚫 PREMATURE FINISH BLOCKED: You still have uncompleted phases in your plan (Pending: '{phase_title}'). "
                    f"You cannot call FINISH until every phase in plan.md is fully executed and marked [x] COMPLETED."
                )
                self.ceo_scratchpad.blockers = [b for b in self.ceo_scratchpad.blockers if "PREMATURE FINISH BLOCKED" not in b]
                self.ceo_scratchpad.blockers.append(_finish_block_msg)
                self.logs.append(f"[bold red]{_finish_block_msg}[/bold red]")
                self.conversation_history.append({
                    "step": f"{turn} (FINISH-BLOCKED)", "agent": "🔧 System Enforcement", "instruction_text": "Attempted premature FINISH",
                    "result": _finish_block_msg, "raw_tool_outputs": [], "structured_results": []
                })
                self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                return True

            if self.ceo_scratchpad.final_product_defined:
                missing = [f for f in self.ceo_scratchpad.required_deliverable_files if not os.path.exists(f)]
                if missing:
                    _gate_msg = (
                        "🚫 FINAL DELIVERABLE GATE: The following required files are missing:\n"
                        + "\n".join(f"  • {f}" for f in missing)
                        + "\n\nTo fix this you can either:\n"
                        "  1. DELEGATE a worker to create exactly that file, OR\n"
                        "  2. Use DEFINE_PRODUCT again with the path of the file that already exists "
                        "(e.g., the one just created by the worker).\n"
                        "Do NOT re‑run any terminal commands."
                    )
                    self.ceo_scratchpad.blockers = [b for b in self.ceo_scratchpad.blockers if "FINAL DELIVERABLE GATE" not in b]
                    self.ceo_scratchpad.blockers.append(_gate_msg)
                    self.conversation_history.append({
                        "step": f"{turn} (DELIVERABLE-GATE-FAIL)", "agent": "🔧 Final Deliverable Gate", "instruction_text": "pre‑FINISH check",
                        "result": _gate_msg, "raw_tool_outputs": [], "structured_results": []
                    })
                    self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                    return True

            _gate_blocked, _gate_msg = self._run_completion_gate()
            if _gate_blocked:
                self.ceo_scratchpad.blockers = [b for b in self.ceo_scratchpad.blockers if "COMPLETION GATE BLOCKED" not in b]
                self.ceo_scratchpad.blockers.append(_gate_msg)
                self.conversation_history.append({
                    "step": f"{turn} (BUILD-GATE-FAIL)", "agent": "🔧 Completion Gate", "instruction_text": "completion gate",
                    "result": _gate_msg, "raw_tool_outputs": [], "structured_results": []
                })
                self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                return True

            self.logs.append(f"  [bold green]✅ CEO declared FINISHED — mission complete[/bold green]")
            self.mission_db.create_checkpoint("mission_complete", self.scratch_dir)
            self.result = report if report else self.generate_final_report()
            self.status = "COMPLETED"; self.is_complete = True
            self.save_history_to_disk()
            if self.task_manager: self.task_manager.trigger_dream_state()
            return False  # stop loop

        # ── CLARIFY ──
        if action_type == "CLARIFY":
            self._consecutive_duplicate_blocks = 0
            question = payload.get("question", "").strip()
            if not question:
                self.logs.append("[bold red]❌ CLARIFY requires a question[/bold red]")
                return True
            self._clarify_count += 1
            if self._clarify_count > 2:
                self.logs.append(f"\n[bold red]🚫 CLARIFY BLOCKED (#{self._clarify_count-1})[/bold red]")
                self.conversation_history.append({
                    "step": f"{turn} (CLARIFY-BLOCKED)", "agent": "🔧 System Enforcement", "instruction_text": question[:120],
                    "result": "🚨 CLARIFICATION LIMIT REACHED. Make reasonable assumptions and proceed.",
                    "raw_tool_outputs": [], "structured_results": []
                })
                self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                return True
            self.logs.append(f"\n[bold yellow]🤔 CEO REQUESTS CLARIFICATION:[/bold yellow]\n[italic]{question}[/italic]\n")
            self.status = "AWAITING_OVERLORD"
            self._clarify_question = question
            return False  # stop loop

        # ── SEND_REPLY (new) ──
        if action_type == "SEND_REPLY":
            message_id = payload.get("message_id")
            body = payload.get("body", "")
            attachments = payload.get("attachments", [])
            if not message_id or not body:
                self.logs.append("[bold red]❌ SEND_REPLY requires message_id and body[/bold red]")
                return True
            if hasattr(self, 'inbox_db') and self.inbox_db:
                self.inbox_db.mark_replied(message_id, body)
            self.result = body
            self.status = "COMPLETED"
            self.is_complete = True
            self.save_history_to_disk()
            return False  # stop the loop

        # ── ASK_USER (new) ──
        if action_type == "ASK_USER":
            thread_id = payload.get("thread_id")
            question = payload.get("question", "")
            if not thread_id or not question:
                self.logs.append("[bold red]❌ ASK_USER requires thread_id and question[/bold red]")
                return True
            if hasattr(self, 'inbox_db') and self.inbox_db:
                self.inbox_db.add_message(
                    thread_id=thread_id,
                    direction="OUT",
                    body=question,
                    sender="CEO",
                    recipient="user",
                    status="PENDING_DELIVERY"
                )
            self.status = "AWAITING_USER_REPLY"
            self._clarify_question = question
            return False  # stop loop until user replies

        # ── Unrecognised action ──
        self.logs.append(f"  [dim]⚠️ Unrecognised or empty action: {action_type}[/dim]")
        self._idle_turns += 1
        if self._idle_turns >= 3:
            self.logs.append(f"  [bold red]🚫 IDLE LOOP #{self._idle_turns}: CEO took no action.[/bold red]")
            self.conversation_history.append({
                "step": f"{turn} (IDLE-ERROR)", "agent": "🔧 System Enforcement", "instruction_text": "Empty action",
                "result": "🚨 IDLE LOOP: Write master_plan and assign a worker IMMEDIATELY.",
                "raw_tool_outputs": [], "structured_results": []
            })
            self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
        return True
