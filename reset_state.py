# reset_state.py
import os
import shutil
from pathlib import Path

def reset_all_mission_state(project_root: str = "."):
    """
    Removes every dynamic file and folder that the Empire orchestrator
    creates during runtime. Source code (.py files, configs) is untouched.
    After running this, the project is back to a fresh‑install state.
    """
    root = Path(project_root)

    # ── Directories to remove entirely ──────────────────────────────
    dirs_to_nuke = [
        "ai_civilization",
        "agent_workspace",
        "logs",
        "orchestration/__pycache__",          # if you want to be thorough
        "__pycache__",
    ]

    for rel in dirs_to_nuke:
        target = root / rel
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
                print(f"🗑️  Removed directory: {target}")
            else:
                target.unlink()
                print(f"🗑️  Removed file: {target}")

    # ── Individual files to remove (safety: only known names) ───────
    files_to_nuke = [
        "ai_civilization/session.json",
        "ai_civilization/empire_graph.db",
        "ai_civilization/failed_commits.jsonl",
        "ai_civilization/dream_state",          # folder, but handled as dir above if exists
    ]

    for rel in files_to_nuke:
        target = root / rel
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
                print(f"🗑️  Removed directory: {target}")
            else:
                target.unlink()
                print(f"🗑️  Removed file: {target}")

    # ── Re‑create the essential directory skeleton (optional) ───────
    essential_dirs = [
        "ai_civilization/mission_logs",
        "ai_civilization/scratch",
        "ai_civilization/agent_memory",
        "ai_civilization/agent_pool",
        "ai_civilization/chroma_db",
        "ai_civilization/mental_frameworks",
        "ai_civilization/cot_playbooks",
        "ai_civilization/dream_state",
    ]
    for rel in essential_dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)

    print("✅ All mission state cleared. Project is ready for a fresh start.")

if __name__ == "__main__":
    import sys
    # Optional confirmation prompt when run directly
    if len(sys.argv) > 1 and sys.argv[1] == "--force":
        reset_all_mission_state()
    else:
        confirm = input("This will delete ALL mission data and logs. Continue? (yes/no): ")
        if confirm.lower() in ("yes", "y"):
            reset_all_mission_state()
        else:
            print("Aborted.")
