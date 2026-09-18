# agent_spawner.py
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from agent import NativeAgent

class AgentSpawner:
    def __init__(self, director_llm: Any, logger: Any, tools: List[Any], pool_dir: str = ""):
        self.llm = director_llm
        self.logger = logger
        self.tools = tools
        self.pool: Dict[str, Dict] = {}

        # 🔥 FIX: Respect passed pool_dir; fallback only if empty
        self._pool_dir = pool_dir if pool_dir else os.path.join(os.getcwd(), "ai_civilization", "agent_pool")
        os.makedirs(self._pool_dir, exist_ok=True)
        self._load_pool()

        # DEBUG: confirm the path in logs
        print(f"[DEBUG] AgentSpawner initialized with pool_dir: {self._pool_dir}", flush=True)

    def _load_pool(self) -> None:
        if not os.path.exists(self._pool_dir):
            return
        for fname in os.listdir(self._pool_dir):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(self._pool_dir, fname), "r", encoding="utf-8") as f:
                        data = json.load(f)
                        role_lower = data.get("role", "").lower()
                        if role_lower:
                            self.pool[role_lower] = data
                except Exception:
                    pass

    def _generate_persona(self, role: str) -> Dict[str, Any]:
        prompt = f"""
Create a JSON persona for a {role}.
Available tools: ["system_terminal", "file_manager", "ast_inspector", "web_search", "web_fetch"]

Format: {{ "goal": "...", "backstory": "...", "tools": ["tool_1", "tool_2"] }}
Make the goal specific and actionable.
CRITICAL: Assign ONLY the tools strictly necessary for this role (e.g., a Copywriter does not need system_terminal).
Return ONLY valid JSON.
"""
        try:
            response = self.llm.call(messages=[{"role": "user", "content": prompt}])
            clean = re.sub(r"```(?:json)?", "", response).strip().strip("`").strip()
            start = clean.find("{")
            end = clean.rfind("}") + 1
            if start != -1 and end != -1:
                data = json.loads(clean[start:end])
                return {
                    "goal": data.get("goal", ""),
                    "backstory": data.get("backstory", ""),
                    "tools": data.get("tools", ["web_search", "web_fetch"])
                }
        except Exception:
            pass
        return {
            "goal": f"Complete tasks as a {role} efficiently and accurately.",
            "backstory": f"You are an experienced {role} with a track record of delivering high-quality work.",
            "tools": ["web_search", "file_manager"]
        }

    def _save_agent_dna(self, role: str, goal: str, backstory: str, tools: List[str]) -> None:
        # DEBUG: confirm save is called
        print(f"[DEBUG] _save_agent_dna called for role: {role}", flush=True)

        os.makedirs(self._pool_dir, exist_ok=True)
        safe_name = role.replace(" ", "_").replace("/", "_") + ".json"
        filepath = os.path.join(self._pool_dir, safe_name)
        dna = {
            "role": role,
            "goal": goal,
            "backstory": backstory,
            "authorized_tools": tools,    # internal
            "tools": tools,               # for dashboard compatibility
            "status": "ACTIVE",
            "created": datetime.now().isoformat(),
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(dna, f, indent=4)
        self.pool[role.lower()] = dna
        self.logger(f"[system]🧬 Persisted Agent DNA to {filepath}")

    def _map_tool_names_to_objects(self, tool_names: List[str]) -> List[Any]:
        agent_tools = []
        for t_name in tool_names:
            matched_custom = next(
                (t for t in self.tools if getattr(t, 'name', getattr(t, '__name__', str(t)))
                 .lower().replace(" ", "_") == t_name.lower()),
                None
            )
            if matched_custom:
                agent_tools.append(matched_custom)
            else:
                agent_tools.append(t_name.lower())
        return agent_tools

    def create_agent(self, role: str, goal: Optional[str] = None, backstory: Optional[str] = None, tool_names: Optional[List[str]] = None) -> NativeAgent:
        role = role.strip()
        if not role:
            raise ValueError("Role name cannot be empty")

        if goal is None or backstory is None or tool_names is None:
            generated = self._generate_persona(role)
            goal = goal or generated.get("goal", f"Execute high-quality work as a {role}.")
            backstory = backstory or generated.get("backstory", f"You are an expert {role} with deep domain knowledge.")
            tool_names = tool_names or generated.get("tools", ["web_search"])

        self._save_agent_dna(role, goal, backstory, tool_names)
        return NativeAgent(
            role=role,
            goal=goal,
            backstory=backstory,
            tools=self._map_tool_names_to_objects(tool_names),
        )

    def ensure_agent(self, role: str, goal: Optional[str] = None, backstory: Optional[str] = None) -> NativeAgent:
        role_lower = role.lower()
        if role_lower in self.pool:
            data = self.pool[role_lower]
            tool_names = data.get("authorized_tools", data.get("tools", ["web_search", "web_fetch"]))
            return NativeAgent(
                role=data["role"],
                goal=data.get("goal", f"Complete tasks as {data['role']}"),
                backstory=data.get("backstory", "Experienced specialist."),
                tools=self._map_tool_names_to_objects(tool_names),
            )
        self.logger(f"[system]🧬 Spawning new agent: {role}")
        return self.create_agent(role, goal, backstory)
