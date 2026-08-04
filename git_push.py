#!/usr/bin/env python3
import os
import subprocess
import sys
from datetime import datetime

def run(cmd):
    """Run a shell command and exit on failure."""
    print(f"▶️  {cmd}")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print(f"❌ Command failed: {cmd}")
        sys.exit(1)

def main():
    # 1. Ensure we are in the correct directory
    project_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_dir)
    print(f"📂 Working directory: {project_dir}")

    # 2. Make sure .env is not tracked (safety first)
    env_path = os.path.join(project_dir, ".env")
    if os.path.exists(env_path):
        with open(".gitignore", "a+") as f:
            f.seek(0)
            if ".env" not in f.read():
                f.write("\n.env\n")
        run("git rm --cached .env 2>/dev/null || true")   # remove from tracking if it was added

    # 3. Add all changes
    run("git add .")

    # 4. Commit with timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_msg = f"Update – {timestamp}"
    run(f'git commit -m "{commit_msg}" || echo "Nothing to commit"')

    # 5. Push to main
    run("git push origin main")

    print("✅ Successfully pushed to GitHub!")

if __name__ == "__main__":
    main()
