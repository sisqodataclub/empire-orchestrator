# agent.py
"""
Shared agent class used by gm.py, task_manager.py, and agent_spawner.py.
"""

class NativeAgent:
    def __init__(self, role: str, goal: str, backstory: str, tools: list = None):
        self.role = role
        self.goal = goal
        self.backstory = f"{goal}\n\n{backstory}"
        self.tools = tools or []
        self.step_callback = None  # attached by TaskManager
