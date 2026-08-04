# orchestration/active_task.py
import threading, json, os, re, sqlite3, subprocess, hashlib, time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from llm import NativeLLM
from helpers import _preload_file_context, _build_post_execution_report
from logger import setup_logging
import logging
from ceo_state import CEOScratchpad, SharedState
from ceo_prompter import build_ceo_prompt
from worker_dispatcher import dispatch_sync_workers, check_async_workers
from agent_spawner import AgentSpawner
from cognitive_wrapper import (
    cognitive_agent_wrapper, classify_task, report_agent_wrapper,
    _strip_verification_from_instruction,
)
from mental_framework import (
    detect_domains, render_framework_block, build_worker_brief,
    query_on_blocker, load_framework,
)
from framework_writer import (
    record_observation, record_failure, record_structural,
    evolve_framework, build_from_research,
)
from empire_tools import library_collection, logs_collection, pure_duckduckgo_scrape, ast_inspector, EmpireTools

from .database import MissionDB
from .role_tools import TOOL_REGISTRY, get_tools_for_role

setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()


class ActiveTask:
    def __init__(self, task_id, mission, agents, director_llm, task_manager=None):
        self.id = task_id
        self.mission = mission
        self.agents = agents
        self.director_llm = director_llm
        self.task_manager = task_manager
        self.status = "STARTING"

        self.mission, _preload_logs = _preload_file_context(mission)
        self.logs = []
        self.result = None
        self.timestamp = datetime.now().strftime("%H:%M:%S")
        self.is_complete = False
        self.conversation_history = []

        for line in _preload_logs:
            self.logs.append(line)

        self._active_domains = detect_domains(mission)
        self._active_schemas = [load_framework(d) for d in self._active_domains]
        self._framework_turn = -1
        self.ceo_scratchpad = CEOScratchpad()
        self.strategy_attempts = {}
        self.pivot_count = 0
        self.consecutive_timeouts = {}
        self.scratch_dir = os.path.abspath(
            os.path.join("ai_civilization", "scratch", f"mission_{task_id}")
        )
        os.makedirs(self.scratch_dir, exist_ok=True)
        self.mission_db = MissionDB(self.scratch_dir)
        self.ceo_playbook_path = os.path.abspath(os.path.join("ai_civilization", "ceo_playbook.json"))
        self.ceo_playbook = []
        if os.path.exists(self.ceo_playbook_path):
            try:
                with open(self.ceo_playbook_path, "r", encoding="utf-8") as f:
                    self.ceo_playbook = json.load(f)
            except Exception:
                pass
        self.web_intelligence = ""
        self.web_search_turn = -99
        self.last_search_query = ""
        self.compute_budget = 100.00
        self.vision_streak = []
        self._last_files_count = 0
        self._stagnation_turns = 0
        self.stagnation_warning = ""
        self.master_plan = []
        self.verification_script = ""
        self._verification_run = True
        self._async_workers = {}
        self._async_dispatch_times = {}
        self._async_results = {}
        self._async_full_results = {}
        self._async_events = []
        self._async_fail_counts = {}
        self._sync_fail_counts = {}
        self._post_release_waits = 0
        self._consecutive_blocks = 0
        self._idle_turns = 0
        self._playbook_strikes = {}
        self._clarify_count = 0
        self._json_parse_errors = 0
        self._consecutive_duplicate_blocks = 0
        self._verification_turns_allowed = 3
        self._seen_worker_output = False
        self.shared_state = SharedState()
        self.agent_memories = {agent.role: [] for agent in self.agents}
        self.global_lessons = "No relevant past lessons found."
        try:
            res = library_collection.query(
                query_texts=[self.mission],
                n_results=5,
                include=["documents", "distances", "metadatas"]
            )
            if res['documents'] and res['documents'][0]:
                relevant_docs = []
                for doc, dist, meta in zip(
                    res['documents'][0], res['distances'][0], res['metadatas'][0]
                ):
                    if (dist < 0.55
                            and meta.get('type') not in ('intelligence_report', 'MASTER_DOC', 'documentation')
                            and meta.get('trust_score', 1.0) >= 0.4
                            and not meta.get('concept', '').startswith('Auto-Report')):
                        relevant_docs.append(f"• {doc[:300]}")
                    if len(relevant_docs) >= 2:
                        break
                if relevant_docs:
                    self.global_lessons = "\n".join(relevant_docs)
        except Exception as e:
            self.global_lessons = f"Library Access Error: {e}"

        self.save_history_to_disk()

        colors = ["bold cyan", "bold magenta", "bold blue", "bold green", "bold yellow"]
        for i, agent in enumerate(self.agents):
            agent.step_callback = self.create_logger(agent.role, colors[i % len(colors)])

        self.spawner = AgentSpawner(
            director_llm=self.director_llm,
            logger=self.logs.append,
            tools=self.agents[0].tools if self.agents else [],
            pool_dir="ai_civilization/agent_pool"
        )

    # ════════════════════════════════════════════════════════════
    # ALL ORIGINAL HELPER METHODS (unchanged)
    # ════════════════════════════════════════════════════════════
    def create_logger(self, agent_name, role_color):
        def log_step(text, is_tool=False):
            if is_tool:
                self.logs.append(f"    ↳ [bold yellow]🛠️  {text}[/bold yellow]")
            else:
                if len(text) > 300:
                    text = text[:297] + "..."
                self.logs.append(f"[{role_color}]{agent_name}[/]: [dim]💭 {text}[/dim]")
        return log_step

    def save_history_to_disk(self):
        directory = os.path.join(os.getcwd(), "ai_civilization", "mission_logs")
        os.makedirs(directory, exist_ok=True)
        file_path = os.path.join(directory, f"mission_{self.id}.json")
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(self.conversation_history, f, indent=4)

    def inject_learned_directive(self, agent_role: str, coaching_tip: str) -> None:
        if not agent_role or not coaching_tip:
            return
        filename = os.path.join("ai_civilization", agent_role.lower().replace(" ", "_") + ".json")
        try:
            if os.path.exists(filename):
                with open(filename, "r", encoding="utf-8") as f:
                    dna = json.load(f)
            else:
                dna = {}
            directives = dna.get("learned_directives", [])
            if coaching_tip not in directives:
                directives.append(coaching_tip)
            dna["learned_directives"] = directives[-10:]
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(dna, f, indent=4)
            self.logs.append(f"  [bold green]🧬 DNA INJECTED → '{agent_role}': {coaching_tip[:80]}...[/bold green]")
        except Exception as e:
            self.logs.append(f"[dim red]DNA Injection error for '{agent_role}': {e}[/dim red]")

    def generate_final_report(self):
        summary_prompt = (
            f"Analyze MISSION: {self.mission}\n"
            f"FULL HISTORY: {json.dumps(self.conversation_history, indent=2)}\n\n"
            "INSTRUCTION: Write a high-density 'Empire Intelligence Report'. ...\n"
            "```json\n[...]\n```"
        )
        try:
            report = self.director_llm.call(messages=[{"role": "user", "content": summary_prompt}])
            return report
        except Exception as e:
            return f"Mission Complete. (Report Generation Failed: {e})"

    def intervene(self, message):
        self.logs.append(f"\n[bold red]🚨 OVERLORD INTERVENTION:[/bold red] {message}\n")
        self._clarify_count = max(0, self._clarify_count - 1)
        self.conversation_history.append({
            "step": "INTERVENTION", "agent": "OVERLORD (USER)", "instruction_text": "CRITICAL OVERRIDE",
            "result": f"✅ OVERLORD HAS ANSWERED. ...\n{message}"
        })
        self.save_history_to_disk()
        if self.status == "AWAITING_OVERLORD":
            self.status = "RUNNING"
            threading.Thread(target=self._run_loop, daemon=True).start()

    def evolve_agent_dna(self, agent_role, critic_feedback):
        try:
            filename = os.path.join("ai_civilization", agent_role.lower().replace(" ", "_") + ".json")
            if not os.path.exists(filename): return
            with open(filename, "r", encoding="utf-8") as f:
                dna = json.load(f)
            evolution_prompt = f"""... Rewrite backstory to prevent failure ..."""
            new_backstory = self.director_llm.call(messages=[{"role": "user", "content": evolution_prompt}]).strip()
            if new_backstory.startswith("```"):
                new_backstory = "\n".join(new_backstory.split("\n")[1:-1])
            dna['backstory'] = new_backstory.strip()
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(dna, f, indent=4)
            self.logs.append(f"[bold green]🧬 EVOLUTION COMPLETE: '{agent_role}' ...[/bold green]")
        except Exception as e:
            self.logs.append(f"[dim red]Geneticist error: {e}[/dim red]")

    def refine_ceo_strategy(self):
        self.pivot_count += 1
        self.logs.append(f"\n[bold red]📉 CONFIDENCE CRITICAL. PIVOT #{self.pivot_count}...[/bold red]")
        if self.pivot_count >= 3:
            self.logs.append("[bold red]🆘 CEO exhausted all strategies. Escalating to Overlord.[/bold red]")
            self.status = "AWAITING_OVERLORD"; return
        reflection_prompt = f"""... Write a NEW STRATEGY ..."""
        try:
            reflection = self.director_llm.call(messages=[{"role": "user", "content": reflection_prompt}])
            self.ceo_scratchpad.hypothesis = f"[PIVOT #{self.pivot_count}] {reflection}"
            self.ceo_scratchpad.confidence = max(40, 75 - (self.pivot_count * 10))
        except Exception as e:
            self.logs.append(f"[dim red]CEO Refinement Error: {e}[/dim red]")

    def _ceo_web_search(self, query: str) -> str:
        try:
            from empire_tools import pure_duckduckgo_scrape
            self.logs.append(f"\n[bold cyan]🌐 CEO AUTO-SEARCH: '{query[:80]}'[/bold cyan]")
            results = pure_duckduckgo_scrape(query)
            if not results: return ""
            lines = [f"🌐 WEB INTELLIGENCE ({len(results)} results) ..."]
            for i, r in enumerate(results[:4], 1):
                snippet = r.get('Snippet', '')[:300].replace('\n', ' ')
                lines.append(f"  [{i}] {r.get('Title','?')}\n       {r.get('Link','?')}\n       {snippet}")
            return "\n".join(lines)
        except Exception as e:
            return ""

    def _inject_jit_knowledge(self, instruction_text: str) -> str:
        jit_header = ""
        try:
            jit_results = library_collection.query(query_texts=[instruction_text], n_results=3,
                                                   include=["documents", "metadatas", "distances"])
            if jit_results['documents'] and jit_results['documents'][0]:
                for i in range(len(jit_results['documents'][0])):
                    distance = jit_results['distances'][0][i]; meta = jit_results['metadatas'][0][i]
                    doc = jit_results['documents'][0][i]; concept = meta.get('concept', '')
                    if distance < 0.6 and meta.get('type') != 'intelligence_report' and not concept.startswith('Auto-Report'):
                        jit_header += f"\n\n### 📚 JIT GROUND TRUTH ({concept}):\n{doc}\n"
        except Exception: pass
        if self._active_schemas:
            worker_brief = build_worker_brief(self._active_schemas, "worker", instruction_text)
            if worker_brief:
                jit_header = f"\n{worker_brief}\n" + jit_header
        return jit_header + instruction_text

    def _compress_shared_state(self):
        hot, warm = self.shared_state.verified_facts, self.shared_state.warm_facts
        if len(hot) > 10:
            to_compress = hot[:-5]; hot = hot[-5:]
            warm.extend(f[:80] for f in to_compress); warm = warm[-20:]
            self.shared_state.verified_facts, self.shared_state.warm_facts = hot, warm

    def _hash_strategy(self, role: str, instruction) -> str:
        cmd_text = str(instruction)
        shell_cmds = re.findall(r'`([^`]+)`|npx\s+\S+[^\n]*|npm\s+\S+[^\n]*|python3?\s+\S+[^\n]*|cd\s+\S+[^\n]*', cmd_text)
        core = ''.join(shell_cmds[:3])[:80] if shell_cmds else cmd_text[:100]
        return f"{role}_{hashlib.md5(core.encode()).hexdigest()[:8]}"

    def _check_framework_on_blockers(self, turn: int) -> str:
        if not self._active_schemas or (turn - self._framework_turn) < 2: return ""
        current_blockers = self.ceo_scratchpad.blockers
        if not current_blockers: return ""
        result = query_on_blocker(self._active_schemas, str(current_blockers[-1]))
        if result: self._framework_turn = turn
        return result

    def _run_completion_gate(self) -> tuple[bool, str]:
        return False, ""

    def _index_turn(self, entry: dict):
        try:
            agent = entry.get('agent', ''); instr = str(entry.get('instruction_text', ''))[:500]
            result = str(entry.get('result', ''))[:500]; full_text = f"{agent}: {instr}\n{result}"
            metadata = {"turn": entry.get('step', ''), "agent": agent, "mission_id": str(self.id),
                        "timestamp": datetime.now().isoformat()}
            doc_id = f"turn_{self.id}_{len(self.conversation_history)}"
            logs_collection.add(documents=[full_text], metadatas=[metadata], ids=[doc_id])
        except Exception: pass

    def _query_relevant_context(self, query: str, top_k: int = 5) -> str:
        try:
            results = logs_collection.query(query_texts=[query], n_results=top_k,
                                            include=["documents", "metadatas", "distances"])
            if not results['documents'] or not results['documents'][0]: return ""
            lines = []
            for doc, meta, dist in zip(results['documents'][0], results['metadatas'][0], results['distances'][0]):
                if dist > 0.6: continue
                lines.append(f"[Turn {meta.get('turn','?')} | {meta.get('agent','?')}]\n{doc[:400]}")
            return "\n\n".join(lines)
        except Exception: return ""

    def _run_single_terminal(self, cmd: str) -> str:
        try:
            result = subprocess.getoutput(f"cd {os.getcwd()} && {cmd}")
            return result[:25000] if len(result) > 25000 else result or "✅ ok (no output)"
        except Exception as e:
            return f"❌ Terminal Error: {e}"

    def _parse_json_response(self, raw_output: str) -> dict:
        try: return json.loads(raw_output)
        except: pass
        cleaned = re.sub(r'```(?:json)?\s*', '', raw_output).strip("`").strip()
        match = re.search(r'(\{.*\})', cleaned, re.DOTALL)
        if match:
            try: return json.loads(match.group(1))
            except:
                json_str = re.sub(r',\s*}', '}', match.group(1)); json_str = re.sub(r',\s*]', ']', json_str)
                try: return json.loads(json_str)
                except: pass
        self.logs.append("[yellow]⚠️ JSON parser fallback used – generating safe action.[/yellow]")
        return {"action_type": "TERMINAL", "action_payload": {"commands": ["ls -la"]}}

    # ═════════════════════════════════════════════════════════════════
    # 🆕 UPDATED _spawn_agent WITH ROLE‑BASED TOOL INJECTION
    # ═════════════════════════════════════════════════════════════════
    def _spawn_agent(self, role: str, goal: str, backstory: str) -> Optional[Any]:
        agent = self.spawner.ensure_agent(role, goal, backstory)
        if agent:
            role_tools = get_tools_for_role(role)
            if role_tools:
                agent.tools = role_tools
            else:
                agent.tools = [TOOL_REGISTRY[name] for name in ("file_manager", "ast_inspector") if name in TOOL_REGISTRY]
            colors = ["bold cyan", "bold magenta", "bold blue", "bold green", "bold yellow"]
            color_index = len(self.agents) % len(colors)
            agent.step_callback = self.create_logger(role, colors[color_index])
            self.agents.append(agent)
            self.logs.append(f"[bold green]🧬 AGENT READY: '{role}' (tools: {[t.name for t in agent.tools]})[/bold green]")
            return agent
        try:
            from gm import NativeAgent
        except ImportError:
            NativeAgent = None
        if NativeAgent:
            role_tools = get_tools_for_role(role)
            new_agent = NativeAgent(role, goal, backstory, tools=role_tools)
            colors = ["bold cyan", "bold magenta", "bold blue", "bold green", "bold yellow"]
            color_index = len(self.agents) % len(colors)
            new_agent.step_callback = self.create_logger(role, colors[color_index])
            self.agents.append(new_agent)
            self.logs.append(f"[bold green]🧬 AGENT READY (spawned): '{role}'[/bold green]")
            return new_agent
        return None

    # ═════════════════════════════════════════════════════════════════
    # 🆕 NEW HELPER: _spawn_agent_with_tools (dynamic provisioning)
    # ═════════════════════════════════════════════════════════════════
    def _spawn_agent_with_tools(self, role: str, goal: str, backstory: str, tool_names: list):
        from gm import NativeAgent
        tools = [TOOL_REGISTRY[name] for name in tool_names if name in TOOL_REGISTRY]
        new_agent = NativeAgent(role, goal, backstory, tools=tools)
        colors = ["bold cyan", "bold magenta", "bold blue", "bold green", "bold yellow"]
        color_index = len(self.agents) % len(colors)
        new_agent.step_callback = self.create_logger(role, colors[color_index])
        self.agents.append(new_agent)
        self.logs.append(f"[bold green]🧬 AGENT READY: '{role}' (tools: {tool_names})[/bold green]")
        return new_agent

    # ═════════════════════════════════════════════════════════════════
    # 🆕 STATE‑ENFORCED TOOL SCHEMAS
    # ═════════════════════════════════════════════════════════════════
    def _get_allowed_actions(self) -> List[str]:
        if not self.ceo_scratchpad.final_product_defined:
            return ["DEFINE_PRODUCT", "CLARIFY", "TERMINAL"]
        if self._async_events or self._async_full_results:
            return ["UPDATE_PLAN", "REQUEST_REWORK", "FINISH", "CLARIFY", "MARK_COMPLETED"]
        if self._async_workers:
            return ["WAIT", "CLARIFY"]
        return [
            "DEFINE_PRODUCT", "TERMINAL", "DELEGATE", "HIRE", "WAIT",
            "FINISH", "CLARIFY", "MARK_COMPLETED", "REQUEST_REWORK",
            "UPDATE_PLAN", "ROLLBACK"
        ]

    # ═════════════════════════════════════════════════════════════════
    # DISPATCH WORKER (FIXED: now updates shared_state.files_modified)
    # ═════════════════════════════════════════════════════════════════
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

        # ── Capture pre‑existing files in scratch_dir ──
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

            # ── Detect new files in scratch_dir ──
            if os.path.isdir(self.scratch_dir):
                current_files = set(os.listdir(self.scratch_dir))
                new_files = current_files - pre_files
                for fname in new_files:
                    fpath = os.path.join(self.scratch_dir, fname)
                    if os.path.isfile(fpath):
                        self.shared_state.files_modified[fpath] = rn

            # ── Existing extraction of files_written from structured result ──
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

    # ═════════════════════════════════════════════════════════════════
    # DETERMINISTIC WORKER COMPLETION HANDLER (unchanged)
    # ═════════════════════════════════════════════════════════════════
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

    # ═════════════════════════════════════════════════════════════════
    # FORCED‑DECISION GATE INJECTION (unchanged)
    # ═════════════════════════════════════════════════════════════════
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

    # ═════════════════════════════════════════════════════════════════
    # 🚀 CORE MISSION LOOP (FULLY UPDATED)
    # ═════════════════════════════════════════════════════════════════
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

            # Process async events (with permanent lock after first worker output)
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

            # ── CEO search trigger (FIXED exclusion list) ──
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
                mission_db=self.mission_db, allowed_actions=allowed_actions
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
                continue

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
                        continue
                    _live = {r: t for r, t in self._async_workers.items() if t.is_alive()}
                    if _live:
                        redirect_msg = f"Workers {list(_live.keys())} are still running. Set action_type='WAIT'."
                        self.logs.append(f"  [bold yellow]🛑 {redirect_msg}[/bold yellow]")
                        self.conversation_history.append({
                            "step": f"{turn} (SYSTEM-REDIRECT)", "agent": "🔧 System", "instruction_text": str(cmds),
                            "result": redirect_msg, "raw_tool_outputs": [], "structured_results": []
                        })
                        continue
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
                        continue
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
                    continue

            # ── UPDATED UPDATE_PLAN HANDLER (MARK_TASK_DONE only needs task_id) ──
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
                        continue
                    # Proceed with only task_id – no phase_title/task_description needed
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
                    continue
                # Other mutations still require phase_title and task_description
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
                continue

            # ── All other action handlers (unchanged except DELEGATE) ──
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
                continue

            if action_type in ("HIRE", "DELEGATE") and not self.ceo_scratchpad.final_product_defined:
                self.ceo_scratchpad.blockers.insert(0,
                    "[NO PRODUCT DEFINED] You must first define the final product using DEFINE_PRODUCT. "
                    "Provide a description and the list of deliverable files that must exist for the mission to be complete."
                )
                self.logs.append("[bold red]🚫 BLOCKED: Product not defined yet.[/bold red]")
                continue

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
                continue

            # 🆕 DYNAMIC TOOL PROVISIONING IN DELEGATE
            if action_type == "DELEGATE":
                role = payload.get("role", "").strip()
                instruction = payload.get("instruction", "").strip()
                assigned_tools = payload.get("assigned_tools", ["file_manager", "ast_inspector"])

                # Dynamic validation – automatically includes every registered tool
                valid_tools = set(TOOL_REGISTRY.keys())
                safe_tools = [t for t in assigned_tools if t in valid_tools]
                if "file_manager" not in safe_tools:
                    safe_tools.append("file_manager")   # failsafe

                if role and instruction:
                    if not any(role.lower() in a.role.lower() for a in self.agents):
                        self.logs.append(f"[bold yellow]⚠️ Agent '{role}' not found. Auto-hiring with tools: {safe_tools}[/bold yellow]")
                        new_agent = self._spawn_agent_with_tools(role, f"Execute tasks related to {role}.", f"Expert in {role}.", safe_tools)
                        if not new_agent:
                            self.logs.append(f"[bold red]❌ Failed to auto-hire '{role}'. Skipping delegation.[/bold red]")
                            continue
                    else:
                        agent_obj = next(a for a in self.agents if role.lower() in a.role.lower())
                        agent_obj.tools = [TOOL_REGISTRY[name] for name in safe_tools if name in TOOL_REGISTRY]

                    subordinates = [a for a in self.agents if a.role != "The Global CEO"]
                    self._dispatch_worker(role, instruction, turn, subordinates)
                    continue

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
                    continue
                else:
                    self.logs.append("  [dim]No workers running. CEO should act.[/dim]")
                    continue

            if action_type == "MARK_COMPLETED":
                pending_phase_id = self.mission_db.get_pending_phase_id()
                if pending_phase_id:
                    self.ceo_scratchpad.blockers = [
                        b for b in self.ceo_scratchpad.blockers if "FORCED DECISION" not in b
                    ]
                    self.logs.append(f"  [bold green]✅ Phase {pending_phase_id} acknowledged. Advancing.[/bold green]")
                continue

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
                continue

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
                continue

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
                    continue

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
                        continue

                _gate_blocked, _gate_msg = self._run_completion_gate()
                if _gate_blocked:
                    self.ceo_scratchpad.blockers = [b for b in self.ceo_scratchpad.blockers if "COMPLETION GATE BLOCKED" not in b]
                    self.ceo_scratchpad.blockers.append(_gate_msg)
                    self.conversation_history.append({
                        "step": f"{turn} (BUILD-GATE-FAIL)", "agent": "🔧 Completion Gate", "instruction_text": "completion gate",
                        "result": _gate_msg, "raw_tool_outputs": [], "structured_results": []
                    })
                    self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                    continue

                self.logs.append(f"  [bold green]✅ CEO declared FINISHED — mission complete[/bold green]")
                self.mission_db.create_checkpoint("mission_complete", self.scratch_dir)
                self.result = report if report else self.generate_final_report()
                self.status = "COMPLETED"; self.is_complete = True
                self.save_history_to_disk()
                if self.task_manager: self.task_manager.trigger_dream_state()
                return

            if action_type == "CLARIFY":
                self._consecutive_duplicate_blocks = 0
                question = payload.get("question", "").strip()
                if not question:
                    self.logs.append("[bold red]❌ CLARIFY requires a question[/bold red]")
                    continue
                self._clarify_count += 1
                if self._clarify_count > 2:
                    self.logs.append(f"\n[bold red]🚫 CLARIFY BLOCKED (#{self._clarify_count-1})[/bold red]")
                    self.conversation_history.append({
                        "step": f"{turn} (CLARIFY-BLOCKED)", "agent": "🔧 System Enforcement", "instruction_text": question[:120],
                        "result": "🚨 CLARIFICATION LIMIT REACHED. Make reasonable assumptions and proceed.",
                        "raw_tool_outputs": [], "structured_results": []
                    })
                    self._index_turn(self.conversation_history[-1]); self.save_history_to_disk()
                    continue
                self.logs.append(f"\n[bold yellow]🤔 CEO REQUESTS CLARIFICATION:[/bold yellow]\n[italic]{question}[/italic]\n")
                self.status = "AWAITING_OVERLORD"
                self._clarify_question = question
                return

            # Unrecognised action
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
                continue

        if not self.is_complete:
            self.result = self.generate_final_report()
            self.status = "COMPLETED"; self.is_complete = True
            self.save_history_to_disk()

    def start(self):
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()
