# app/routers/missions.py
import subprocess
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app.dependencies import get_current_org
from app.db.models import Organization
from app.config import settings
from app.services.workspace import ensure_tenant_workspace

router = APIRouter()

class MissionRequest(BaseModel):
    mission: str

@router.post("/missions/start")
async def start_mission(
    req: MissionRequest,
    org: Organization = Depends(get_current_org)
):
    # Ensure the tenant root folder exists
    tenant_root = ensure_tenant_workspace(str(org.id))

    # Log file will be created by gm.py, but we can also capture its stdout/stderr
    log_file_path = tenant_root / "logs" / "execution.log"
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    # Absolute path to gm.py (shared code in project root)
    gm_path = Path(__file__).parent.parent.parent / "gm.py"

    with open(log_file_path, "a") as log_file:
        subprocess.Popen(
            ["python", str(gm_path), req.mission],
            cwd=str(tenant_root),      # all relative paths write into tenant folder
            stdout=log_file,
            stderr=log_file
        )

    return {
        "status": "Mission started",
        "organization": org.name,
        "log_file": str(log_file_path)
    }

@router.get("/missions/{task_id}/artifacts/{filename}")
async def get_artifact(
    task_id: str,
    filename: str,
    org: Organization = Depends(get_current_org)
):
    safe_filename = Path(filename).name
    file_path = (
        Path(settings.WORKSPACE_ROOT)
        / f"org_{org.id}"
        / "ai_civilization"
        / "scratch"
        / f"mission_{task_id}"
        / safe_filename
    )
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(file_path)

@router.put("/manifest")
async def update_manifest(
    manifest_content: str = Body(..., media_type="text/plain"),
    org: Organization = Depends(get_current_org)
):
    manifest_path = Path(settings.WORKSPACE_ROOT) / f"org_{org.id}" / "domain_manifest.md"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest_content, encoding="utf-8")
    return {"status": "Manifest updated"}
