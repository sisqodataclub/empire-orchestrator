# app/services/workspace.py
import shutil
from pathlib import Path
from app.config import settings

def ensure_tenant_workspace(org_id: str) -> Path:
    tenant_root = Path(settings.WORKSPACE_ROOT) / f"org_{org_id}"
    if not tenant_root.exists():
        tenant_root.mkdir(parents=True, exist_ok=True)
        
        project_root = Path(__file__).resolve().parent.parent.parent
        
        # Seed individual organization manifest/config files on creation
        for filename in ["domain_manifest.md", ".env"]:
            src = project_root / filename
            if src.exists():
                shutil.copy(src, tenant_root / filename)
                
    return tenant_root
