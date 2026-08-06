# app/dependencies.py
from fastapi import Security, HTTPException, Depends, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session
from typing import Optional
from app.db.session import get_db
from app.db.models import Organization

# auto_error=False so we can try header, then fallback to cookie
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def get_api_key_from_request(
    request: Request,
    api_key_header: str = Security(api_key_header)
) -> Optional[str]:
    # Try header first
    if api_key_header:
        print(f"🔑 Header API key found: {api_key_header[:12]}...")
        return api_key_header
        
    # Then try cookie
    api_key_cookie = request.cookies.get("api_key")
    if api_key_cookie:
        print(f"🍪 Cookie API key found: {api_key_cookie[:12]}...")
        return api_key_cookie
        
    print("❌ No API key found in headers or cookies!")
    return None

async def get_current_org(
    api_key: str = Depends(get_api_key_from_request),
    db: Session = Depends(get_db)
) -> Organization:
    if not api_key:
        raise HTTPException(status_code=401, detail="Not authenticated")
    org = db.query(Organization).filter(Organization.api_key == api_key).first()
    if not org:
        raise HTTPException(status_code=401, detail="Invalid or revoked API Key")
    return org

# Optional: for routes that can handle anonymous users gracefully
async def get_optional_org(
    api_key: str = Depends(get_api_key_from_request),
    db: Session = Depends(get_db)
) -> Optional[Organization]:
    if not api_key:
        return None
    return db.query(Organization).filter(Organization.api_key == api_key).first()
