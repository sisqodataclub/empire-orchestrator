# orchestration/dynamic_tools.py
import importlib.util
import os
import sys
from pathlib import Path
from typing import List

def load_dynamic_tools(directory: str) -> List:
    """Scan a directory for Python files containing @tool decorated functions and import them."""
    tools = []
    dir_path = Path(directory)
    if not dir_path.exists():
        return tools

    for file_path in dir_path.glob("*.py"):
        module_name = file_path.stem
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
                for attr_name in dir(module):
                    obj = getattr(module, attr_name)
                    if hasattr(obj, 'name') and hasattr(obj, 'description'):
                        tools.append(obj)
            except Exception as e:
                print(f"[red]Error loading dynamic tool {module_name}: {e}[/red]")
    return tools
