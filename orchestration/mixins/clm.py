import threading, time, os, re, hashlib, subprocess, json
from typing import List, Dict, Any

# External imports used in the core loop
from ceo_prompter import build_ceo_prompt
from mental_framework import render_framework_block
from empire_tools import EmpireTools

# Mixin imports for method resolution
from .helpers_mixin import HelpersMixin
from .action_handlers_mixin import ActionHandlersMixin
from .worker_dispatch_mixin import WorkerDispatchMixin
from .agent_management_mixin import AgentManagementMixin


class CoreLoopMixin:
    """Contains the main mission loop and forced‑decision injection."""

    def _run_loop(self):
        try:
            self._run_loop_inner()
        except Exception as _fatal:
            import traceback as _tb
            _tb_str = _tb.format_exc()
            self.logs.append(f"[bold red]💥 FATAL LOOP CRASH: {_fatal}[/bold red]")
            self.conversation_history.append({
                "step": "FATAL-CRASH", "agent": "🔧 System", "instruction_text": "",
                "result": f"💥 The mission loop crashed fatally:\n{_tb_str[-1000:]}",
                "raw_tool_outputs": [], "structured_results": []
            })
            if not self.is_complete:
                self.status = "COMPLETED"; self.is_complete = True; self.save_history_to_disk()

    def _inject_forced_decision_prompt(self) -> str:
        pending_phase_id = self.mission_db.get_pending_phase_id()
        if pending_phase_id is None:
            self.ceo_scratchpad.blockers.insert(0,
                "[ALL PHASES COMPLETE] All planned work is done. You MUST now call FINISH or, if something is missing, use UPDATE_PLAN to add new phases."
            )
            return ""
        phase_status = self.mission_db.get_phase_status(pending_phase_id)
        if phase_status == "COMPLETED":
            forced_msg = (
                f"🚨 FORCED DECISION: Phase ID {pending_phase_id} has been completed by workers. "
                f"As CEO, you must now respond with exactly ONE of these actions:\n"
                f"  • MARK_COMPLETED (to advance to the next phase)\n"
                f"  • REQUEST_REWORK (to send the phase back with specific feedback)\n"
                f"No other actions are allowed until you make this decision."
            )
            return forced_msg
        return ""

    def _run_loop_inner(self):
        self.status = "RUNNING"
        domain_file = os.path.join(os.getcwd(), "domain_manifest.md")
        if not os.path.exists(domain_file):
            self.ceo_scratchpad.active_domain = "MISSING_MANIFEST"
            self.ceo_scratchpad.blockers.insert(0,
                "[DOMAIN MISSING] domain_manifest.md not found. ..."
            )
            self._clarify_question = "Please create domain_manifest.md ..."
            self.status = "AWAITING_OVERLORD"; return

        subordinates = [a for a in self.agents if a.role != "The Global CEO"]
        available_roles_str = "\n".join(
            f"• {a.role}\n  Tools: [{', '.join(dict.fromkeys(getattr(t,'name',str(t)) for t in a.tools))}]" for a in subordinates
        )
        try:
            recon_output = EmpireTools().list_directory(self.scratch_dir)
        except: recon_output = "Directory empty or inaccessible."
        self._initial_recon = f"--- ENVIRONMENT RECONNAISSANCE ---\nWorkspace Path: {self.scratch_dir}\n...\n{recon_output}\n"

        turn = 0
        _EMERGENCY_TURN_CEILING = 300
        while turn < _EMERGENCY_TURN_CEILING:
            turn += 1
            if self.is_complete: break

            if self.compute_budget <= 0:
                self.result = self.generate_final_report(); self.status = "COMPLETED"; self.is_complete = True
                self.save_history_to_disk()
                if self.task_manager: self.task_manager.trigger_dream_state()
                return

            # Process async events
            if self._async_events:
                for event in self._async_events:
                    self.conversation_history.append({
                        "step": f"{turn} (ASYNC-INTERRUPT)", "agent": "⚡ ASYNC INTERRUPT", "instruction_text": "",
                        "result": event, "raw_tool_outputs": [], "structured_results": []
                    })
                    self._index_turn(self.conversation_history[-1])
                for role_name, full_result in list(self._async_full_results.items()):
                    self._handle_worker_completion(role_name, str(full_result))
                self._verification_turns_allowed = 0
                self._seen_worker_output = True
                self.ceo_scratchpad.blockers = [b for b in self.ceo_scratchpad.blockers if "VERIFICATION LIMIT" not in b]
                self.vision_streak.clear(); self._consecutive_duplicate_blocks = 0
                self._async_full_results.clear(); self._async_events.clear()
                self.save_history_to_disk()
            else:
                if not self._seen_worker_output and self._verification_turns_allowed < 3:
                    self._verification_turns_allowed = 3

            forced_decision = self._inject_forced_decision_prompt()

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
                    dead_end_warnings += f"\n🚨 DEAD END: Strategy '{strat}' attempted {count} times. PIVOT NOW."

            framework_blocker_hint = self._check_framework_on_blockers(turn)

            # CEO search trigger
            blockers = self.ceo_scratchpad.blockers
            searchable_blockers = [
                b for b in blockers
                if not b.startswith(("[ALL PHASES COMPLETE]", "🚨 FORCED DECISION",
                                     "🚫 FINAL DELIVERABLE GATE", "🚫 PREMATURE FINISH BLOCKED",
                                     "🚨 DUPLICATE COMMAND BLOCKED",
                                     "You already have the worker's output"))
            ]
            err_hashes   = [hashlib.md5(b.encode()).hexdigest()[:8] for b in blockers]
            repeated_err = len(err_hashes) >= 2 and len(set(err_hashes)) < len(err_hashes)
            cooldown_ok  = (turn - self.web_search_turn) >= 4
            should_search = (repeated_err or self.pivot_count >= 2) and cooldown_ok

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
                        self.web_intelligence  = new_intel
                        self.web_search_turn   = turn
                        self.last_search_query = clean_q

            # spatial anchor, worker status, token banner
            try:
                _cwd = os.getcwd()
                _project_scan = subprocess.getoutput(
                    "find /home /srv /opt /var/www /root -maxdepth 5 "
                    r"\( -name 'package.json' -o -name 'main.py' -o -name 'app.py' "
                    r"-o -name 'pyproject.toml' -o -name 'Makefile' -o -name 'go.mod' \) "
                    "! -path '*/node_modules/*' ! -path '*/.git/*' 2>/dev/null | head -20"
                )
                _live_procs = subprocess.getoutput(
                    "ps aux | grep -E 'node|python|ruby|go|rust|java|php|next|nuxt|nest|uvicorn|flask|django|fastapi' "
                    "| grep -v grep | awk '{print $1, $2, $11, $12, $13}' | head -15"
                )
                _open_ports = subprocess.getoutput(
                    "ss -tlnp 2>/dev/null | grep LISTEN | awk '{print $4, $6}' | head -15"
                )
                spatial_anchor = (
                    f"\n🌍 DYNAMIC SPATIAL ANCHOR (live system snapshot):\n"
                    f"  CWD: {_cwd}\n"
                    f"  Project files found:\n"
                    + "".join(f"    • {l}\n" for l in _project_scan.splitlines() if l.strip())
                    + f"  Live processes:\n"
                    + "".join(f"    • {l}\n" for l in _live_procs.splitlines() if l.strip())
                    + f"  Open ports:\n"
                    + "".join(f"    • {l}\n" for l in _open_ports.splitlines() if l.strip())
                )
            except Exception:
                spatial_anchor = ""

            _live_workers_now = {r: t for r, t in self._async_workers.items() if t.is_alive()}
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
                    f"✅ NO WORKERS RUNNING — you have full terminal access.\n"
                    f"   → Do NOT set WAIT. Use TERMINAL, DELEGATE, or HIRE."
                )

            _timeline_chars = sum(len(str(i)) for i in self.conversation_history)
            if _timeline_chars > 80000:
                token_banner = "🚨 TOKEN EXHAUSTION RISK: CRITICAL — Timeline massive. Write master_plan NOW and delegate.\n"
            elif _timeline_chars > 40000:
                token_banner = "⚠️  TOKEN EXHAUSTION RISK: MEDIUM — Timeline growing. Shift to PLANNING soon.\n"
            else:
                token_banner = ""

            stagnation_warning = self.stagnation_warning

            env_recon = ""
            if turn == 1 and hasattr(self, '_initial_recon'):
                env_recon = self._initial_recon
                del self._initial_recon

            active_task = self.mission_db.get_active_task()
            allowed_actions = self._get_allowed_actions()

            prompt = build_ceo_prompt(
                mission=self.mission, turn=turn, conversation_history=last_turns,
                relevant_context=relevant_context, ceo_scratchpad=self.ceo_scratchpad,
                shared_state=self.shared_state, master_plan=self.master_plan,
                scratch_dir=self.scratch_dir, cwd=os.getcwd(), global_lessons=self.global_lessons,
                web_intelligence=self.web_intelligence, stagnation_warning=stagnation_warning,
                framework_block=framework_block, dead_end_warnings=dead_end_warnings,
                available_roles_str=available_roles_str, agents=self.agents,
                spatial_anchor=spatial_anchor, ceo_playbook=self.ceo_playbook,
                compute_budget=self.compute_budget, timeline_chars=_timeline_chars,
                active_schemas=self._active_schemas, async_workers_status="",
                worker_status=worker_status, framework_hint_block=framework_blocker_hint,
                token_banner=token_banner, environment_recon=env_recon,
                forced_decision=forced_decision, active_task=active_task,
                mission_db=self.mission_db, allowed_actions=allowed_actions,
                inbox_history=getattr(self, 'inbox_history_text', "")
            )

            try:
                response = self.director_llm.call(messages=[{"role": "user", "content": prompt}])
            except Exception as _llm_err:
                self.logs.append(f"[bold red]🌐 LLM CALL FAILED (turn {turn}): {str(_llm_err)[:120]}[/bold red]")
                self.conversation_history.append({
                    "step": f"{turn} (LLM-TIMEOUT)", "agent": "🔧 System", "instruction_text": "",
                    "result": f"⚠️ LLM API call failed: {str(_llm_err)[:200]}", "raw_tool_outputs": [], "structured_results": []
                })
                self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                time.sleep(3); continue

            plan = self._parse_json_response(response)
            action_type = plan.get("action_type")
            payload = plan.get("action_payload", {})

            should_continue = self._execute_action(action_type, payload, turn)
            if not should_continue:
                break

        if not self.is_complete:
            self.result = self.generate_final_report()
            self.status = "COMPLETED"; self.is_complete = True
            self.save_history_to_disk()
