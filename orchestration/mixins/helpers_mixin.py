import os, re, json, hashlib, subprocess, threading, time
from datetime import datetime
from typing import List, Dict, Any

from dotenv import load_dotenv
from ceo_state import CEOScratchpad, SharedState
from empire_tools import library_collection, logs_collection, pure_duckduckgo_scrape
from mental_framework import detect_domains, render_framework_block, build_worker_brief, query_on_blocker, load_framework

class HelpersMixin:
    """Utility methods used across the ActiveTask class."""

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
