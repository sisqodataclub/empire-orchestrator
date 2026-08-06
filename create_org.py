# create_org.py
import sys
from pathlib import Path

# Ensure project root is in sys.path so we can import app modules
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from app.db.session import SessionLocal
from app.db.models import Organization
from app.services.workspace import ensure_tenant_workspace

db = SessionLocal()
name = input("Organization name: ")
org = Organization(name=name, api_key=Organization.generate_api_key())
db.add(org)
db.commit()

# Provision the workspace folder and seed initial files (domain_manifest.md, .env)
tenant_root = ensure_tenant_workspace(str(org.id))

print(f"✅ Organization created!")
print(f"ID: {org.id}")
print(f"API Key: {org.api_key}")
print(f"Workspace provisioned at: {tenant_root}")
db.close()
