from typing import Any, List, Optional
from .helpers_mixin import HelpersMixin
from ..role_tools import TOOL_REGISTRY, get_tools_for_role



class AgentManagementMixin:
    """Methods for spawning and managing agents."""

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

    def _get_allowed_actions(self) -> List[str]:
        if not self.ceo_scratchpad.final_product_defined:
            return ["DEFINE_PRODUCT", "CLARIFY", "TERMINAL", "SEND_REPLY", "ASK_USER"]
        if self._async_events or self._async_full_results:
            return ["UPDATE_PLAN", "REQUEST_REWORK", "FINISH", "CLARIFY", "MARK_COMPLETED", "SEND_REPLY", "ASK_USER"]
        if self._async_workers:
            return ["WAIT", "CLARIFY", "SEND_REPLY", "ASK_USER"]
        return [
            "DEFINE_PRODUCT", "TERMINAL", "DELEGATE", "HIRE", "WAIT",
            "FINISH", "CLARIFY", "MARK_COMPLETED", "REQUEST_REWORK",
            "UPDATE_PLAN", "ROLLBACK", "SEND_REPLY", "ASK_USER"
        ]
