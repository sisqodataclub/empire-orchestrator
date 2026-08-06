# app/routers/auth.py
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import Organization

router = APIRouter()

LOGIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Login – Empire Orchestrator</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
        }}
        .login-box {{
            background: #161b22;
            padding: 40px;
            border-radius: 8px;
            border: 1px solid #30363d;
            width: 400px;
        }}
        h1 {{ font-weight: 300; font-size: 2rem; color: #f0f6fc; margin-bottom: 10px; }}
        h1 span {{ color: #ff7b72; }}
        .sub {{ color: #8b949e; margin-bottom: 30px; }}
        label {{ display: block; margin-top: 20px; margin-bottom: 6px; color: #8b949e; font-size: 0.9rem; }}
        input[type="text"] {{
            width: 100%;
            padding: 10px;
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 4px;
            color: #c9d1d9;
            font-size: 1rem;
        }}
        button {{
            margin-top: 25px;
            width: 100%;
            padding: 10px;
            background: #2ea043;
            border: none;
            border-radius: 4px;
            color: #fff;
            font-weight: bold;
            font-size: 1rem;
            cursor: pointer;
        }}
        button:hover {{ background: #3fb950; }}
        .error {{
            color: #f85149;
            margin-top: 12px;
            font-size: 0.9rem;
        }}
        .note {{
            margin-top: 20px;
            font-size: 0.8rem;
            color: #8b949e;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="login-box">
        <h1>🏛️ Empire <span>Orchestrator</span></h1>
        <div class="sub">Multi-tenant AI workspace</div>
        <form method="post">
            <label for="api_key">API Key</label>
            <input type="text" id="api_key" name="api_key" placeholder="emp_live_..." required autofocus>
            {error_html}
            <button type="submit">Login</button>
            <div class="note">Enter your organization's API key.</div>
        </form>
    </div>
</body>
</html>
"""

def render_login(error: str = "") -> str:
    error_html = f'<div class="error">{error}</div>' if error else ""
    return LOGIN_PAGE.format(error_html=error_html)

@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(render_login())

@router.post("/login")
async def login_post(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    api_key = form.get("api_key")
    if not api_key:
        return HTMLResponse(render_login("API key is required."))

    org = db.query(Organization).filter(Organization.api_key == api_key).first()
    if not org:
        return HTMLResponse(render_login("Invalid API key."))

    # Attach cookie directly to the redirect response instance with path="/"
    response = RedirectResponse(url="/api/v1/dashboard", status_code=302)
    response.set_cookie(
        key="api_key",
        value=api_key,
        httponly=True,
        max_age=60*60*24*30,
        samesite="lax",
        path="/"  # <--- THIS IS CRITICAL
    )
    return response

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("api_key", path="/")
    return response
