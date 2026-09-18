import threading, time, os, re
from .helpers_mixin import HelpersMixin
from cognitive_wrapper import _strip_verification_from_instruction
from framework_writer import record_observation
from ..role_tools import TOOL_REGISTRY








class WorkerDispatchMixin:
    """Methods for dispatching work to subordinate agents and handling completion."""

    def _dispatch_worker(self, role: str, instruction: str, turn: int, subordinates: list):
        if role in self._async_workers and self._async_workers[role].is_alive():
            self.logs.append(f"  [dim yellow]⏭ '{role}' already running — skipped[/dim yellow]")
            return
        _fail_count = self._async_fail_counts.get(role, 0)
        if _fail_count >= 3:
            _last_fail = str(self._async_results.get(role, "unknown"))[:300]
            self.logs.append(f"  [bold red]🚫 DISPATCH BLOCKED: {role} failed {_fail_count}x[/bold red]")
            self.ceo_scratchpad.blockers.append(
                f"[DIRECT CONTROL MANDATE — ASYNC]: '{role}' failed {_fail_count}x. "
                f"Fix it yourself via TERMINAL commands before re-dispatching."
            )
            self.conversation_history.append({
                "step": f"{turn} (DISPATCH-BLOCKED)", "agent": "🔧 System Enforcement",
                "instruction_text": f"Blocked re-dispatch of {role}",
                "result": f"🚨 '{role}' failed {_fail_count}x. Take direct control. Last: {_last_fail}",
                "raw_tool_outputs": [], "structured_results": []
            })
            self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
            return

        agent_obj = next((a for a in subordinates if role.lower() in a.role.lower()), None)
        if not agent_obj:
            self.logs.append(f"[bold red]❌ Agent '{role}' not found.[/bold red]"); return

        plan_content = ""
        plan_path = os.path.join(self.scratch_dir, "plan.md")
        if os.path.exists(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8") as f: plan_content = f.read()
                plan_content = _strip_verification_from_instruction(plan_content)
            except: pass

        # Tool scoping & rework injection
        pending_phase_id = self.mission_db.get_pending_phase_id()
        task_row = None
        if pending_phase_id is not None:
            rows = self.mission_db.conn.execute(
                "SELECT id, tools_allowed, current_iteration FROM tasks WHERE phase_id=? AND assigned_role=? AND status='PENDING' ORDER BY id LIMIT 1",
                (pending_phase_id, role)
            ).fetchall()
            if rows:
                task_row = {"id": rows[0][0], "tools_allowed": rows[0][1], "current_iteration": rows[0][2]}
        if task_row and task_row["tools_allowed"]:
            allowed = [t.strip() for t in task_row["tools_allowed"].split(",") if t.strip()]
            original_tools = agent_obj.tools[:]
            agent_obj.tools = [t for t in agent_obj.tools if getattr(t, 'name', '') in allowed]
            if not agent_obj.tools:
                agent_obj.tools = original_tools
        if task_row and task_row["current_iteration"] > 0:
            feedback = self.mission_db.get_rework_feedback(task_row["id"])
            if feedback:
                instruction = (
                    f"🚨 PREVIOUS ATTEMPTS FAILED BECAUSE:\n{feedback}\n\n---\n"
                    f"NEW INSTRUCTION (do NOT repeat those mistakes):\n{instruction}"
                )

        # Capture pre-existing files
        pre_files = set()
        if os.path.isdir(self.scratch_dir):
            pre_files = set(os.listdir(self.scratch_dir))

        def _async_execute(rn=role, ct=instruction, plan=plan_content):
            agent_obj = next((a for a in self.agents if rn.lower() in a.role.lower()), None)
            if not agent_obj:
                self._async_results[rn] = f"❌ No agent found for role: {rn}"; return
            plan_block = f"\n📋 PLAN (execute exactly, do not verify):\n{plan}\n\n" if plan else ""
            strict_ct = (
                f"🚨 EXECUTIVE ORDER FROM CEO 🚨\n"
                f"Follow the plan EXACTLY. ...\n{plan_block}YOUR EXACT MISSION NOW:\n{ct}\n"
            )
            from cognitive_wrapper import cognitive_agent_wrapper
            res = cognitive_agent_wrapper(agent=agent_obj, instruction=strict_ct, project_state="", private_history="",
                                          critic_llm=self.director_llm, logger=agent_obj.step_callback,
                                          scratch_dir=self.scratch_dir, agent_role=agent_obj.role,
                                          shared_state=self.shared_state.__dict__)
            safe_res = res[0] if res else "No result"

            # Detect new files
            if os.path.isdir(self.scratch_dir):
                current_files = set(os.listdir(self.scratch_dir))
                new_files = current_files - pre_files
                for fname in new_files:
                    fpath = os.path.join(self.scratch_dir, fname)
                    if os.path.isfile(fpath):
                        self.shared_state.files_modified[fpath] = rn

            # Extract files_written from structured result
            files_written = []
            if len(res) >= 3 and isinstance(res[2], dict):
                files_written = res[2].get("files_written", [])
            elif len(res) >= 2 and isinstance(res[1], dict):
                files_written = res[1].get("files_written", [])
            for fp in files_written:
                if fp and os.path.exists(fp):
                    self.shared_state.files_modified[fp] = rn

            self._async_results[rn] = safe_res
            auto_dump = []
            for fpath, _agent in self.shared_state.files_modified.items():
                if os.path.exists(fpath):
                    try:
                        with open(fpath, 'r', encoding='utf-8') as fx: content = fx.read(4000)
                        auto_dump.append(f"### 📄 FILE: {os.path.basename(fpath)}\nPath: `{fpath}`\n```\n{content}\n```")
                    except: pass
            artifact_dump_str = "\n\n".join(auto_dump) if auto_dump else "No files were written by this worker."
            self._async_events.append(
                f"🚨 [AUTO‑VERIFY]: {agent_obj.role} completed.\n"
                f"   Result summary: {str(safe_res)[:120]}\n"
                f"   [ASYNC‑RESULT:FULL — read (ASYNC‑RESULT) timeline entry for complete output]\n\n"
                f"📂 AUTOMATED ARTIFACT INSPECTION — files created / modified:\n{artifact_dump_str}\n\n"
                f"MANDATE: You have all evidence. Do NOT run terminal. Update plan.md (UPDATE_PLAN) or finish (FINISH) now."
            )
            self._async_full_results[rn] = safe_res
            self.logs.append(f"  [bold green]✅ ASYNC COMPLETE: {agent_obj.role}[/bold green]")
            for schema in self._active_schemas:
                record_observation(schema, str(safe_res), str(self.id), agent_obj.role, turn)
            if "CRITICAL FAILURE" in str(safe_res) or "❌" in str(safe_res)[:50]:
                self._async_fail_counts[rn] = self._async_fail_counts.get(rn, 0) + 1
                if self._async_fail_counts[rn] >= 2:
                    self.ceo_scratchpad.blockers.append(f"[DIRECT CONTROL MANDATE]: Worker '{agent_obj.role}' failed {self._async_fail_counts[rn]}x. Take direct control.")
            else:
                self._async_fail_counts[rn] = 0
            self._async_workers.pop(rn, None)
            self.compute_budget = round(self.compute_budget - 2.50, 2)

        t = threading.Thread(target=_async_execute, daemon=True)
        self._async_workers[role] = t; self._async_dispatch_times[role] = time.time(); t.start()

        self.conversation_history.append({
            "step": f"{turn} (DELEGATE)", "agent": "👑 CEO",
            "instruction_text": f"Delegated to {role} with plan injection",
            "result": f"Worker '{role}' dispatched with task and plan.",
            "raw_tool_outputs": [], "structured_results": []
        })
        self._index_turn(self.conversation_history[-1]); self._post_release_waits = 0; self.save_history_to_disk()

    def _handle_worker_completion(self, role_name: str, safe_res: str):
        pending_phase_id = self.mission_db.get_pending_phase_id()
        if pending_phase_id is not None:
            success = "CRITICAL FAILURE" not in safe_res and "❌" not in safe_res[:50]
            status = "COMPLETED" if success else "NEEDS_REWORK"
            feedback = safe_res[:300] if not success else ""
            tasks = self.mission_db.conn.execute(
                "SELECT id, acceptance_criteria, description, tools_allowed FROM tasks WHERE phase_id = ? AND assigned_role = ? AND status = 'PENDING'",
                (pending_phase_id, role_name)
            ).fetchall()
            if tasks:
                task_id = tasks[0][0]
                for tid, criteria, desc, tools_allowed in tasks:
                    paths = []; dl_match = re.search(r"-\s*\*\*Deliverable File:\*\*\s*`?([^\s`]+)`?", desc)
                    if dl_match: paths.append(dl_match.group(1).strip())
                    paths.extend(re.findall(r"([/\w.\-]+\.(?:md|html|py|json|txt|xml|csv))", criteria))
                    paths.extend(re.findall(r"([/\w.\-]+\.(?:md|html|py|json|txt|xml|csv))", desc))
                    for fp in paths:
                        if not os.path.exists(fp):
                            success = False; feedback = f"Required deliverable file '{fp}' was not created by the worker."; break
                    if not success: break
                self.mission_db.update_task_status(task_id, status, feedback)
                for fpath, _ in self.shared_state.files_modified.items():
                    if os.path.exists(fpath):
                        artifact_type = "report" if fpath.endswith(".md") else "code_source" if fpath.endswith((".py",".js",".ts")) else "data" if fpath.endswith((".csv",".json")) else "output"
                        self.mission_db.register_artifact(task_id, artifact_type, fpath)
                phase_completed = self.mission_db.mark_phase_complete_if_all_done(pending_phase_id)
                if phase_completed:
                    checkpoint_label = f"phase_{pending_phase_id}_complete"
                    self.mission_db.create_checkpoint(checkpoint_label, self.scratch_dir)
                    self.logs.append(f"  [bold cyan]🔒 Checkpoint '{checkpoint_label}' created.[/bold cyan]")
        plan_path = os.path.join(self.scratch_dir, "plan.md")
        self.mission_db.render_markdown(plan_path)
