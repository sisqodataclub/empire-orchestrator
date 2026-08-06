# run_mission.py
import sys
import os
import subprocess
from pathlib import Path

def main():
    if len(sys.argv) < 3:
        print("Usage: python run_mission.py <org_id> <mission_prompt>")
        sys.exit(1)

    org_id = sys.argv[1]
    mission_prompt = sys.argv[2]

    project_root = Path(__file__).parent
    sys.path.insert(0, str(project_root))

    from app.services.workspace import ensure_tenant_workspace
    tenant_root = ensure_tenant_workspace(org_id)

    print(f"🚀 Running mission for org {org_id} in {tenant_root}")

    # Change to tenant's data directory
    os.chdir(tenant_root)

    # Call gm.py headless from the project root
    cmd = ["python", str(project_root / "gm.py"), mission_prompt]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Mission failed with exit code {e.returncode}")
        sys.exit(e.returncode)

if __name__ == "__main__":
    main()
