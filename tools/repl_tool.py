# tools/repl_tool.py
import os
import sys
import subprocess
import uuid
from crewai.tools import tool

@tool("Execute REPL")
def execute_repl(code: str) -> str:
    """
    Executes Python code in a sandboxed subprocess and returns the output.
    Use for read-only data extraction, verification, and exploration.
    """
    return run_repl_code(code)

def run_repl_code(code: str, timeout=5, max_output=2000) -> str:
    """Run Python code in a subprocess with timeout and output truncation."""
    script_id = uuid.uuid4().hex[:8]
    script_path = f"/tmp/repl_{script_id}.py"
    with open(script_path, "w") as f:
        f.write(code)
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HTTP_PROXY": "", "HTTPS_PROXY": "", "NO_PROXY": "*"}
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            output += f"\n[ERROR] {result.stderr.strip()}"
        if len(output) > max_output:
            output = output[:max_output] + "\n...[TRUNCATED]"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Execution timed out after 5 seconds."
    finally:
        if os.path.exists(script_path):
            os.unlink(script_path)
