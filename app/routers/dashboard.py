# app/routers/dashboard.py
import json
import time
import asyncio
import os
import pty
import struct
import fcntl
import termios
import re
import shutil
from datetime import datetime
from pathlib import Path
from string import Template
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
from pydantic import BaseModel
from app.dependencies import get_current_org, get_optional_org
from app.db.models import Organization
from app.config import settings
from app.services.workspace import ensure_tenant_workspace
import subprocess

router = APIRouter()

active_terminals = {}

# -------- Request Models --------
class MissionRequest(BaseModel):
    mission: str

class TerminalInputRequest(BaseModel):
    input: str

class TerminalResizeRequest(BaseModel):
    cols: int
    rows: int

# -------- Helper: Rotate log files --------
def rotate_log_if_needed(log_path: Path, max_size_mb: int = 5):
    try:
        if log_path.exists() and log_path.stat().st_size > max_size_mb * 1024 * 1024:
            backup = log_path.with_suffix(".log.bak")
            if backup.exists():
                backup.unlink()
            log_path.rename(backup)
    except Exception:
        pass

# -------- Data helpers --------
def get_tenant_agents(tenant_root: Path):
    agents = []
    default_agents = [
        {
            "role": "The Global CEO",
            "goal": "Operate as a God-Tier Principal Staff Engineer. Translate Overlord intent into deterministic, flawless execution.",
            "backstory": "You are the Supreme Intelligence of a rising Technocratic Empire. Directives: Scalpel Protocol, Dynamic Pivoting, Zero Hallucination, Compiler Semantics, Worker Memory Law.",
            "tools": ["system_terminal", "file_manager", "ast_inspector", "internet_search", "web_fetch"]
        },
        {
            "role": "Quality Assurance Engineer",
            "goal": "Cryptographically and physically validate all technical work before it is marked complete.",
            "backstory": "You are the Gatekeeper. Nothing passes without physical proof. Zero-Trust Verification, Compiler Semantics, Python Script Injection.",
            "tools": ["system_terminal", "file_manager", "ast_inspector"]
        }
    ]
    seen = {a["role"].lower() for a in default_agents}
    dna_dirs = [
        tenant_root / "ai_civilization",
        tenant_root / "ai_civilization" / "agent_pool"
    ]
    for base in dna_dirs:
        if not base.exists():
            continue
        for fname in base.glob("*.json"):
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("status") != "ACTIVE":
                        continue
                    role = data.get("role", "Unknown")
                    if role.lower() in seen:
                        continue
                    seen.add(role.lower())
                    tools = data.get("tools", ["system_terminal", "file_manager", "ast_inspector"])
                    if "capabilities" in data and data["capabilities"]:
                        tools = data["capabilities"]
                    agents.append({
                        "role": role,
                        "goal": data.get("goal", "No goal defined."),
                        "backstory": data.get("backstory", "No backstory."),
                        "tools": tools,
                        "created": data.get("created", "unknown")
                    })
            except Exception:
                continue
    return default_agents + agents

def get_tenant_missions(tenant_root: Path):
    missions = []
    log_dir = tenant_root / "ai_civilization" / "mission_logs"
    if not log_dir.exists():
        return missions
    for fname in log_dir.glob("*.json"):
        try:
            with open(fname, "r", encoding="utf-8") as f:
                data = json.load(f)
                mission_id = fname.stem.split("_")[-1]
                mission_text = "Unknown mission"
                status = "COMPLETED"
                if data and isinstance(data, list) and len(data) > 0:
                    first = data[0]
                    mission_text = first.get("instruction_text", "").split("\n")[0][:100]
                    last = data[-1] if data else {}
                    if "ASYNC-INTERRUPT" in last.get("step", "") and "completed" not in last.get("result", "").lower():
                        status = "RUNNING"
                timestamp = datetime.fromtimestamp(fname.stat().st_mtime).strftime("%H:%M")
                missions.append({
                    "id": mission_id,
                    "mission": mission_text,
                    "status": status,
                    "timestamp": timestamp
                })
        except Exception:
            continue
    missions.sort(key=lambda x: int(x["id"]) if x["id"].isdigit() else 0, reverse=True)
    return missions

def get_chroma_collections(tenant_root: Path):
    chroma_path = tenant_root / "ai_civilization" / "chroma_db"
    if not chroma_path.exists():
        return 0
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(chroma_path))
        return len(client.list_collections())
    except Exception:
        return 0

def get_manifest_content(tenant_root: Path) -> str:
    manifest_path = tenant_root / "domain_manifest.md"
    if manifest_path.exists():
        return manifest_path.read_text(encoding="utf-8")
    return "# Domain Manifest\n\nNo manifest found."

# -------- PTY Endpoints --------
@router.post("/start_mission")
async def start_mission_api(
    req: MissionRequest,
    org: Organization = Depends(get_current_org),
):
    tenant_root = ensure_tenant_workspace(str(org.id))
    gm_path = Path(__file__).parent.parent.parent / "gm.py"

    if org.id in active_terminals:
        try:
            os.close(active_terminals[org.id]["master_fd"])
            active_terminals[org.id]["process"].terminate()
        except Exception:
            pass
        active_terminals.pop(org.id, None)

    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", 40, 120, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    env = os.environ.copy()
    env["FORCE_COLOR"] = "1"
    env["TERM"] = "xterm-256color"
    env["PYTHONWARNINGS"] = "ignore"

    process = subprocess.Popen(
        ["python", str(gm_path)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        cwd=str(tenant_root),
        env=env
    )
    os.close(slave_fd)

    active_terminals[org.id] = {
        "master_fd": master_fd,
        "process": process,
        "view_sent": False,
        "task_id": None
    }

    time.sleep(0.4)
    os.write(master_fd, f"new {req.mission}\n".encode())

    return {"status": "started", "organization": org.name}

@router.get("/stream_terminal")
async def stream_terminal(request: Request, org: Organization = Depends(get_current_org)):
    async def event_generator():
        while org.id not in active_terminals:
            await asyncio.sleep(0.5)

        term_info = active_terminals[org.id]
        master_fd = term_info["master_fd"]
        process = term_info["process"]

        loop = asyncio.get_running_loop()
        buffer = ""
        streaming_started = False

        while process.poll() is None:
            try:
                data = await loop.run_in_executor(None, os.read, master_fd, 1024)
                if data:
                    decoded = data.decode("utf-8", errors="ignore")
                    buffer += decoded

                    if not streaming_started:
                        if not term_info.get("view_sent"):
                            match = re.search(r"(?:Task ID:|mission_|#)(\d+)", buffer)
                            if match:
                                tid = match.group(1)
                                term_info["task_id"] = tid
                                time.sleep(0.3)
                                os.write(master_fd, f"view {tid}\n".encode())
                                term_info["view_sent"] = True
                                streaming_started = True

                                if buffer:
                                    payload = json.dumps({'log': buffer})
                                    yield f"data: {payload}\n\n"
                                    buffer = ""
                                continue
                    else:
                        if decoded:
                            payload = json.dumps({'log': decoded})
                            yield f"data: {payload}\n\n"
            except OSError:
                break
            await asyncio.sleep(0.05)

        if streaming_started:
            msg = json.dumps({'log': '\r\n[Mission Process Completed]\r\n'})
            yield f"data: {msg}\n\n"
        else:
            msg = json.dumps({'log': '\r\n[Mission ended without Task Manager UI]\r\n'})
            yield f"data: {msg}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.post("/terminal_input")
async def terminal_input(req: TerminalInputRequest, org: Organization = Depends(get_current_org)):
    if org.id not in active_terminals:
        raise HTTPException(status_code=404, detail="No active terminal session.")
    master_fd = active_terminals[org.id]["master_fd"]
    try:
        os.write(master_fd, req.input.encode("utf-8"))
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/terminal_resize")
async def terminal_resize(req: TerminalResizeRequest, org: Organization = Depends(get_current_org)):
    if org.id not in active_terminals:
        raise HTTPException(status_code=404, detail="No active terminal session.")
    master_fd = active_terminals[org.id]["master_fd"]
    try:
        winsize = struct.pack("HHHH", req.rows, req.cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stop_mission")
async def stop_mission(org: Organization = Depends(get_current_org)):
    if org.id in active_terminals:
        try:
            process = active_terminals[org.id]["process"]
            process.terminate()
            process.wait(timeout=2)
            os.close(active_terminals[org.id]["master_fd"])
        except Exception:
            pass
        del active_terminals[org.id]
        return {"status": "stopped"}
    return {"status": "no_active_session"}

@router.post("/update_manifest")
async def update_manifest(request: Request, org: Organization = Depends(get_current_org)):
    data = await request.json()
    new_content = data.get("manifest", "")
    tenant_root = ensure_tenant_workspace(str(org.id))
    manifest_path = tenant_root / "domain_manifest.md"
    manifest_path.write_text(new_content, encoding="utf-8")
    return {"status": "updated"}

# -------- Mission Management Endpoints --------
@router.get("/mission/{task_id}/logs")
async def get_mission_logs(task_id: str, org: Organization = Depends(get_current_org)):
    tenant_root = ensure_tenant_workspace(str(org.id))
    log_path = tenant_root / "ai_civilization" / "mission_logs" / f"mission_{task_id}.json"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Mission log not found")
    with open(log_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data

@router.get("/mission/{task_id}/artifacts")
async def list_artifacts(task_id: str, org: Organization = Depends(get_current_org)):
    tenant_root = ensure_tenant_workspace(str(org.id))
    scratch_dir = tenant_root / "ai_civilization" / "scratch" / f"mission_{task_id}"
    if not scratch_dir.exists():
        raise HTTPException(status_code=404, detail="Mission scratch directory not found")
    files = []
    for f in scratch_dir.rglob("*"):
        if f.is_file():
            rel_path = str(f.relative_to(scratch_dir))
            files.append({
                "name": f.name,
                "path": rel_path,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
            })
    return {"files": files}

@router.get("/mission/{task_id}/artifacts/{file_path:path}")
async def download_artifact(task_id: str, file_path: str, org: Organization = Depends(get_current_org)):
    tenant_root = ensure_tenant_workspace(str(org.id))
    scratch_dir = tenant_root / "ai_civilization" / "scratch" / f"mission_{task_id}"
    file_full_path = (scratch_dir / file_path).resolve()
    if not str(file_full_path).startswith(str(scratch_dir.resolve())) or not file_full_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file not found")
    return FileResponse(path=str(file_full_path))

@router.post("/mission/{task_id}/reset")
async def reset_mission(task_id: str, org: Organization = Depends(get_current_org)):
    tenant_root = ensure_tenant_workspace(str(org.id))
    scratch_dir = tenant_root / "ai_civilization" / "scratch" / f"mission_{task_id}"
    log_path = tenant_root / "ai_civilization" / "mission_logs" / f"mission_{task_id}.json"
    plan_path = tenant_root / "ai_civilization" / "plan.md"
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    if log_path.exists():
        os.remove(log_path)
    if plan_path.exists():
        os.remove(plan_path)
    return {"status": "reset"}

# -------- Main Dashboard HTML --------
HTML_TEMPLATE = Template("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🏛️ Empire Dashboard – $org_name</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            padding: 10px;
        }
        .container { max-width: 1400px; margin: 0 auto; width: 100%; }
        header {
            display: flex;
            flex-direction: column;
            gap: 10px;
            align-items: flex-start;
            border-bottom: 1px solid #30363d;
            padding-bottom: 15px;
            margin-bottom: 20px;
        }
        @media (min-width: 768px) {
            header {
                flex-direction: row;
                justify-content: space-between;
                align-items: center;
            }
            body { padding: 20px; }
        }
        header h1 {
            font-size: 1.8rem;
            font-weight: 300;
            color: #f0f6fc;
        }
        @media (min-width: 768px) { header h1 { font-size: 2.2rem; } }
        header h1 span { color: #ff7b72; }
        .header-controls {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
        }
        .last-updated {
            font-size: 0.85rem;
            color: #8b949e;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
            margin-bottom: 20px;
        }
        @media (min-width: 768px) {
            .stats-grid {
                grid-template-columns: repeat(4, 1fr);
                gap: 20px;
                margin-bottom: 30px;
            }
        }
        .stat-card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 15px;
            text-align: center;
            cursor: default;
        }
        .stat-card .number {
            font-size: 2rem;
            font-weight: 600;
            color: #f0f6fc;
        }
        @media (min-width: 768px) { .stat-card .number { font-size: 2.5rem; } }
        .stat-card .label {
            font-size: 0.85rem;
            color: #8b949e;
            margin-top: 5px;
        }
        .section {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 20px;
        }
        @media (min-width: 768px) { .section { padding: 20px; margin-bottom: 30px; } }
        .section h2 {
            font-size: 1.2rem;
            font-weight: 400;
            color: #f0f6fc;
            margin-bottom: 15px;
            border-bottom: 1px solid #30363d;
            padding-bottom: 10px;
        }
        .section h2 .btn-sm {
            float: right;
            font-size: 0.8rem;
            padding: 4px 12px;
            background: #30363d;
            border: none;
            border-radius: 4px;
            color: #c9d1d9;
            cursor: pointer;
        }
        .section h2 .btn-sm:hover { background: #40464d; }
        @media (min-width: 768px) { .section h2 { font-size: 1.3rem; } }
        .table-responsive {
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
            min-width: 500px;
        }
        @media (min-width: 768px) { table { font-size: 0.9rem; min-width: auto; } }
        th {
            text-align: left;
            padding: 10px 8px;
            color: #8b949e;
            font-weight: 400;
            border-bottom: 1px solid #30363d;
        }
        td {
            padding: 10px 8px;
            border-bottom: 1px solid #21262d;
            vertical-align: middle;
        }
        tr.clickable { cursor: pointer; }
        tr.clickable:hover { background: #1c2128; }
        .status-badge {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 500;
        }
        .status-completed { background: #2ea043; color: #fff; }
        .status-running { background: #d29922; color: #fff; }
        .status-failed { background: #f85149; color: #fff; }
        .status-idle { background: #8b949e; color: #fff; }
        .tools-list {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
        }
        .tool-tag {
            background: #21262d;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.7rem;
            color: #c9d1d9;
        }
        .memory-stats {
            display: grid;
            grid-template-columns: 1fr;
            gap: 10px;
        }
        @media (min-width: 600px) { .memory-stats { grid-template-columns: 1fr 1fr; } }
        .memory-item {
            background: #0d1117;
            padding: 10px;
            border-radius: 6px;
        }
        .memory-item .key { color: #8b949e; font-size: 0.8rem; }
        .memory-item .value { font-size: 1.1rem; font-weight: 500; }
        .backstory-preview {
            font-size: 0.8rem;
            color: #8b949e;
            max-width: 200px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: block;
        }
        @media (min-width: 768px) { .backstory-preview { max-width: 300px; } }
        .org-badge {
            background: #30363d;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            color: #f0f6fc;
        }
        .logout-link {
            color: #f85149;
            text-decoration: none;
        }
        .logout-link:hover { text-decoration: underline; }
        .form-group {
            margin-bottom: 15px;
        }
        .form-group label {
            display: block;
            color: #8b949e;
            margin-bottom: 5px;
            font-size: 0.9rem;
        }
        .form-group textarea,
        .form-group input[type="text"] {
            width: 100%;
            padding: 10px;
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 4px;
            color: #c9d1d9;
            font-family: 'Courier New', monospace;
            font-size: 0.9rem;
        }
        .form-group textarea {
            min-height: 120px;
        }
        @media (min-width: 768px) { .form-group textarea { min-height: 150px; } }
        .btn {
            padding: 10px 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 500;
            font-size: 0.9rem;
        }
        .btn-primary { background: #2ea043; color: #fff; }
        .btn-primary:hover { background: #3fb950; }
        .btn-secondary { background: #30363d; color: #c9d1d9; }
        .btn-secondary:hover { background: #40464d; }
        .btn-danger { background: #f85149; color: #fff; }
        .btn-danger:hover { background: #da3633; }
        .btn-sm { padding: 4px 10px; font-size: 0.75rem; }
        #terminal-container {
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 4px;
            padding: 10px;
            height: 450px;
            width: 100%;
            overflow: hidden;
        }
        .inline-flex {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }
        /* Modal styles */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 999;
            justify-content: center;
            align-items: center;
        }
        .modal-overlay.active { display: flex; }
        .modal-box {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 25px;
            max-width: 90%;
            max-height: 90%;
            overflow: auto;
            min-width: 400px;
        }
        .modal-box h3 {
            color: #f0f6fc;
            margin-bottom: 15px;
            border-bottom: 1px solid #30363d;
            padding-bottom: 10px;
        }
        .modal-box .close-btn {
            float: right;
            background: #f85149;
            border: none;
            color: #fff;
            padding: 4px 12px;
            border-radius: 4px;
            cursor: pointer;
        }
        .modal-box .close-btn:hover { background: #da3633; }
        .modal-box pre {
            background: #0d1117;
            padding: 15px;
            border-radius: 4px;
            overflow: auto;
            max-height: 60vh;
            font-size: 0.8rem;
            color: #c9d1d9;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .modal-box .file-list {
            list-style: none;
            padding: 0;
        }
        .modal-box .file-list li {
            padding: 6px 0;
            border-bottom: 1px solid #21262d;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .modal-box .file-list li a {
            color: #58a6ff;
            text-decoration: none;
        }
        .modal-box .file-list li a:hover { text-decoration: underline; }
        .modal-box .file-list .file-size {
            color: #8b949e;
            font-size: 0.75rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🏛️ Empire <span>Dashboard</span></h1>
            <div class="header-controls">
                <span class="org-badge">$org_name</span>
                <span class="last-updated" id="lastUpdated">🔄 $now</span>
                <button class="btn btn-secondary btn-sm" id="refreshBtn">↻ Refresh</button>
                <button class="btn btn-secondary btn-sm" id="autoRefreshBtn">▶ Auto-refresh</button>
                <a href="/logout" class="logout-link">Logout</a>
            </div>
        </header>

        <!-- Stats Grid -->
        <div class="stats-grid" id="statsGrid">
            <div class="stat-card"><div class="number" id="agentsCount">$agents_count</div><div class="label">👥 Agents</div></div>
            <div class="stat-card"><div class="number" id="missionsCount">$missions_count</div><div class="label">📋 Missions</div></div>
            <div class="stat-card"><div class="number" id="lessonsCount">$lessons</div><div class="label">📚 Lessons</div></div>
            <div class="stat-card"><div class="number" id="docsCount">$docs</div><div class="label">📖 Docs</div></div>
        </div>

        <!-- New Mission -->
        <div class="section">
            <h2>🚀 Start New Mission</h2>
            <div class="form-group">
                <label for="missionPrompt">Mission Prompt</label>
                <textarea id="missionPrompt" placeholder="e.g., Create a new folder named 'project_alpha' and write core requirements"></textarea>
            </div>
            <div class="inline-flex">
                <button class="btn btn-primary" id="startMissionBtn">▶ Start Mission</button>
                <span id="missionStatus" style="color:#8b949e; font-size:0.9rem;"></span>
                <button class="btn btn-danger btn-sm" id="stopMissionBtn" style="display:none;">⏹ Stop</button>
            </div>
            <div id="missionLogContainer" style="display:none; margin-top:15px;">
                <div class="inline-flex" style="justify-content:space-between; margin-bottom:8px;">
                    <span style="color:#8b949e; font-size:0.9rem;">🖥️ Interactive Task Manager CLI (Live Rich UI)</span>
                    <button class="btn btn-secondary" id="clearLogBtn">Close / Clear</button>
                </div>
                <div id="terminal-container"></div>
            </div>
        </div>

        <!-- Manifest Editor -->
        <div class="section">
            <h2>📄 Domain Manifest</h2>
            <div class="form-group">
                <label for="manifestEditor">Edit your organisation's manifest</label>
                <textarea id="manifestEditor">$manifest_content</textarea>
            </div>
            <div class="inline-flex">
                <button class="btn btn-primary" id="saveManifestBtn">💾 Save Manifest</button>
                <span id="manifestSaveStatus" style="color:#8b949e; font-size:0.9rem;"></span>
            </div>
        </div>

        <!-- Agent Roster -->
        <div class="section">
            <h2>👥 Agent Roster</h2>
            <div class="table-responsive">
                <table>
                    <thead><tr><th>Role</th><th>Goal</th><th>Backstory</th><th>Tools</th></tr></thead>
                    <tbody id="agentRows">$agent_rows</tbody>
                </table>
            </div>
        </div>

        <!-- Recent Missions -->
        <div class="section">
            <h2>📋 Recent Missions</h2>
            <div class="table-responsive">
                <table>
                    <thead><tr><th>ID</th><th>Mission</th><th>Status</th><th>Timestamp</th><th>Actions</th></tr></thead>
                    <tbody id="missionRows">$mission_rows</tbody>
                </table>
            </div>
        </div>

        <!-- Memory Stats -->
        <div class="section">
            <h2>🧠 Knowledge Store</h2>
            <div class="memory-stats" id="memoryStats">
                <div class="memory-item"><div class="key">📚 Lessons</div><div class="value" id="memLessons">$lessons</div></div>
                <div class="memory-item"><div class="key">📖 Docs</div><div class="value" id="memDocs">$docs</div></div>
                <div class="memory-item"><div class="key">🧬 Agent DNA files</div><div class="value" id="memDna">$agent_dna_files</div></div>
                <div class="memory-item"><div class="key">📁 ChromaDB collections</div><div class="value" id="memChroma">$collections</div></div>
            </div>
        </div>
    </div>

    <!-- Modals -->
    <div class="modal-overlay" id="agentModal">
        <div class="modal-box">
            <button class="close-btn" onclick="closeModal('agentModal')">✕</button>
            <h3 id="agentModalTitle">Agent Details</h3>
            <div id="agentModalContent"></div>
        </div>
    </div>
    <div class="modal-overlay" id="missionModal">
        <div class="modal-box">
            <button class="close-btn" onclick="closeModal('missionModal')">✕</button>
            <h3 id="missionModalTitle">Mission Log</h3>
            <div id="missionModalContent"><pre>Loading...</pre></div>
        </div>
    </div>
    <div class="modal-overlay" id="artifactsModal">
        <div class="modal-box">
            <button class="close-btn" onclick="closeModal('artifactsModal')">✕</button>
            <h3 id="artifactsModalTitle">Artifacts</h3>
            <div id="artifactsModalContent"><p>Loading...</p></div>
        </div>
    </div>

    <script>
        // -------- Global state --------
        let eventSource = null;
        let term = null;
        let fitAddon = null;
        let autoRefresh = false;
        let refreshInterval = null;

        // -------- Terminal functions --------
        function initTerminal() {
            if (term) { term.dispose(); }
            document.getElementById('terminal-container').innerHTML = '';
            term = new Terminal({
                cursorBlink: true,
                fontSize: 13,
                fontFamily: 'Courier New, monospace',
                theme: { background: '#0d1117', foreground: '#c9d1d9', cursor: '#2ea043' }
            });
            fitAddon = new FitAddon.FitAddon();
            term.loadAddon(fitAddon);
            term.open(document.getElementById('terminal-container'));
            fitAddon.fit();
            term.onData(data => {
                fetch('/api/v1/terminal_input', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ input: data })
                }).catch(err => console.error('Terminal input error:', err));
            });
            term.onResize(size => {
                fetch('/api/v1/terminal_resize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cols: size.cols, rows: size.rows })
                }).catch(err => console.error('Terminal resize error:', err));
            });
        }

        function connectTerminalStream() {
            if (eventSource) { eventSource.close(); eventSource = null; }
            eventSource = new EventSource('/api/v1/stream_terminal');
            eventSource.onmessage = function(event) {
                const data = JSON.parse(event.data);
                if (data.log && term) { term.write(data.log); }
            };
            eventSource.onerror = function() {
                setTimeout(connectTerminalStream, 2000);
            };
        }

        // -------- Dashboard refresh --------
        async function refreshDashboard() {
            try {
                const resp = await fetch(window.location.href);
                const html = await resp.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                document.getElementById('agentsCount').textContent = doc.getElementById('agentsCount').textContent;
                document.getElementById('missionsCount').textContent = doc.getElementById('missionsCount').textContent;
                document.getElementById('lessonsCount').textContent = doc.getElementById('lessonsCount').textContent;
                document.getElementById('docsCount').textContent = doc.getElementById('docsCount').textContent;
                document.getElementById('memLessons').textContent = doc.getElementById('memLessons').textContent;
                document.getElementById('memDocs').textContent = doc.getElementById('memDocs').textContent;
                document.getElementById('memDna').textContent = doc.getElementById('memDna').textContent;
                document.getElementById('memChroma').textContent = doc.getElementById('memChroma').textContent;
                document.getElementById('agentRows').innerHTML = doc.getElementById('agentRows').innerHTML;
                document.getElementById('missionRows').innerHTML = doc.getElementById('missionRows').innerHTML;
                attachClickHandlers();
                document.getElementById('lastUpdated').textContent = '🔄 ' + new Date().toLocaleString();
            } catch (e) {
                console.warn('Refresh failed:', e);
            }
        }

        function attachClickHandlers() {
            document.querySelectorAll('#agentRows tr.clickable').forEach(row => {
                row.onclick = function() {
                    const role = this.dataset.role;
                    showAgentDetails(role);
                };
            });
        }

        // -------- Modal functions --------
        function closeModal(id) { document.getElementById(id).classList.remove('active'); }

        function showAgentDetails(role) {
            const row = document.querySelector(`#agentRows tr[data-role="${role}"]`);
            if (!row) return;
            const goal = row.cells[1].textContent;
            const backstory = row.cells[2].querySelector('.backstory-preview')?.title || row.cells[2].textContent;
            const tools = Array.from(row.cells[3].querySelectorAll('.tool-tag')).map(t => t.textContent).join(', ');
            document.getElementById('agentModalTitle').textContent = `🧠 ${role}`;
            document.getElementById('agentModalContent').innerHTML = `
                <p><strong>Goal:</strong> ${goal}</p>
                <p><strong>Backstory:</strong> ${backstory}</p>
                <p><strong>Tools:</strong> ${tools || 'None'}</p>
            `;
            document.getElementById('agentModal').classList.add('active');
        }

        async function showMissionLog(taskId) {
            document.getElementById('missionModalTitle').textContent = `📋 Mission #${taskId} Log`;
            document.getElementById('missionModalContent').innerHTML = '<pre>Loading...</pre>';
            document.getElementById('missionModal').classList.add('active');
            try {
                const resp = await fetch(`/api/v1/mission/${taskId}/logs`);
                if (!resp.ok) throw new Error('Not found');
                const data = await resp.json();
                document.getElementById('missionModalContent').innerHTML = `<pre>${JSON.stringify(data, null, 2)}</pre>`;
            } catch (e) {
                document.getElementById('missionModalContent').innerHTML = `<p style="color:#f85149;">Error: ${e.message}</p>`;
            }
        }

        async function showArtifacts(taskId) {
            document.getElementById('artifactsModalTitle').textContent = `📁 Artifacts for Mission #${taskId}`;
            document.getElementById('artifactsModalContent').innerHTML = '<p>Loading...</p>';
            document.getElementById('artifactsModal').classList.add('active');
            try {
                const resp = await fetch(`/api/v1/mission/${taskId}/artifacts`);
                if (!resp.ok) throw new Error('No artifacts found');
                const data = await resp.json();
                if (!data.files || data.files.length === 0) {
                    document.getElementById('artifactsModalContent').innerHTML = '<p>No files found.</p>';
                    return;
                }
                let html = '<ul class="file-list">';
                for (const f of data.files) {
                    const size = f.size > 1024 ? (f.size/1024).toFixed(1)+' KB' : f.size+' B';
                    html += `<li>
                        <a href="/api/v1/mission/${taskId}/artifacts/${encodeURIComponent(f.path)}" target="_blank">${f.path}</a>
                        <span class="file-size">${size}</span>
                    </li>`;
                }
                html += '</ul>';
                document.getElementById('artifactsModalContent').innerHTML = html;
            } catch (e) {
                document.getElementById('artifactsModalContent').innerHTML = `<p style="color:#f85149;">Error: ${e.message}</p>`;
            }
        }

        async function resetMission(taskId) {
            if (!confirm(`Are you sure you want to reset mission #${taskId}? This will delete its scratch folder, logs, and plan.`)) return;
            try {
                const resp = await fetch(`/api/v1/mission/${taskId}/reset`, { method: 'POST' });
                const result = await resp.json();
                if (result.status === 'reset') {
                    alert('Mission reset successfully.');
                    refreshDashboard();
                } else {
                    alert('Reset failed.');
                }
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        // -------- Start/Stop Mission --------
        document.getElementById('startMissionBtn').addEventListener('click', async function() {
            const prompt = document.getElementById('missionPrompt').value.trim();
            if (!prompt) { alert('Please enter a mission prompt.'); return; }
            const status = document.getElementById('missionStatus');
            status.textContent = '⏳ Starting Task Manager UI...';
            this.disabled = true;
            try {
                const resp = await fetch('/api/v1/start_mission', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mission: prompt })
                });
                const result = await resp.json();
                if (result.status === 'started') {
                    status.textContent = '✅ Task Manager active!';
                    document.getElementById('missionLogContainer').style.display = 'block';
                    document.getElementById('stopMissionBtn').style.display = 'inline-block';
                    initTerminal();
                    connectTerminalStream();
                } else {
                    status.textContent = '❌ Failed to start.';
                }
            } catch (err) {
                status.textContent = '❌ Error: ' + err.message;
            } finally {
                this.disabled = false;
            }
        });

        document.getElementById('stopMissionBtn').addEventListener('click', async function() {
            try {
                await fetch('/api/v1/stop_mission', { method: 'POST' });
                if (eventSource) { eventSource.close(); eventSource = null; }
                document.getElementById('missionLogContainer').style.display = 'none';
                document.getElementById('stopMissionBtn').style.display = 'none';
                document.getElementById('missionStatus').textContent = '⏹ Mission stopped.';
            } catch (e) {
                alert('Error stopping mission: ' + e.message);
            }
        });

        document.getElementById('clearLogBtn').addEventListener('click', function() {
            if (eventSource) { eventSource.close(); eventSource = null; }
            document.getElementById('missionLogContainer').style.display = 'none';
            document.getElementById('stopMissionBtn').style.display = 'none';
            document.getElementById('missionStatus').textContent = '';
        });

        // -------- Manifest Save --------
        document.getElementById('saveManifestBtn').addEventListener('click', async function() {
            const content = document.getElementById('manifestEditor').value;
            const status = document.getElementById('manifestSaveStatus');
            status.textContent = '⏳ Saving...';
            try {
                const resp = await fetch('/api/v1/update_manifest', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ manifest: content })
                });
                const result = await resp.json();
                status.textContent = result.status === 'updated' ? '✅ Updated!' : '❌ Failed.';
            } catch (err) {
                status.textContent = '❌ Error: ' + err.message;
            }
        });

        // -------- Auto-refresh --------
        document.getElementById('refreshBtn').addEventListener('click', refreshDashboard);

        document.getElementById('autoRefreshBtn').addEventListener('click', function() {
            autoRefresh = !autoRefresh;
            this.textContent = autoRefresh ? '⏸ Pause refresh' : '▶ Auto-refresh';
            if (autoRefresh) {
                refreshInterval = setInterval(refreshDashboard, 10000);
                refreshDashboard();
            } else {
                clearInterval(refreshInterval);
            }
        });

        // -------- Modal close on overlay click --------
        document.querySelectorAll('.modal-overlay').forEach(el => {
            el.addEventListener('click', function(e) {
                if (e.target === this) { this.classList.remove('active'); }
            });
        });

        // -------- Initialise click handlers --------
        attachClickHandlers();
    </script>
</body>
</html>
""")

# -------- Dashboard endpoint --------
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, org: Organization = Depends(get_optional_org)):
    if not org:
        return RedirectResponse(url="/login", status_code=302)

    tenant_root = Path(settings.WORKSPACE_ROOT) / f"org_{org.id}"
    if not tenant_root.exists():
        agents = get_tenant_agents(tenant_root)
        missions = []
        dna_files = 0
        collections = 0
        manifest_content = "# No manifest yet."
    else:
        agents = get_tenant_agents(tenant_root)
        missions = get_tenant_missions(tenant_root)
        dna_files = len(list((tenant_root / "ai_civilization").glob("*.json"))) + len(list((tenant_root / "ai_civilization" / "agent_pool").glob("*.json")))
        collections = get_chroma_collections(tenant_root)
        manifest_content = get_manifest_content(tenant_root)

    # Build agent rows
    agent_rows = ""
    for a in agents:
        tools = " ".join([f'<span class="tool-tag">{t}</span>' for t in a["tools"][:5]])
        if len(a["tools"]) > 5:
            tools += f' <span class="tool-tag">+{len(a["tools"])-5}</span>'
        agent_rows += f"""
        <tr class="clickable" data-role="{a['role']}">
            <td><strong>{a["role"]}</strong></td>
            <td>{a["goal"][:80] + ("…" if len(a["goal"]) > 80 else "")}</td>
            <td><span class="backstory-preview" title="{a["backstory"]}">{a["backstory"][:80] + ("…" if len(a["backstory"]) > 80 else "")}</span></td>
            <td><div class="tools-list">{tools}</div></td>
        </tr>
        """

    # Build mission rows
    mission_rows = ""
    for m in missions[-10:][::-1]:
        mission_rows += f"""
        <tr>
            <td><strong><a href="#" onclick="showMissionLog('{m['id']}'); return false;">#{m['id']}</a></strong></td>
            <td>{m["mission"][:80] + ("…" if len(m["mission"]) > 80 else "")}</td>
            <td><span class="status-badge status-{m["status"].lower()}">{m["status"]}</span></td>
            <td>{m["timestamp"]}</td>
            <td>
                <button class="btn btn-secondary btn-sm" onclick="showArtifacts('{m['id']}')">📁 Files</button>
                <button class="btn btn-danger btn-sm" onclick="resetMission('{m['id']}')">🔄 Reset</button>
            </td>
        </tr>
        """
    if not mission_rows:
        mission_rows = '<tr><td colspan="5" style="text-align:center; color:#8b949e;">No missions yet</td></tr>'

    lessons = 0
    docs = 0

    # Use safe_substitute to ignore unmapped JavaScript template variables like `${role}` and `${taskId}`
    html = HTML_TEMPLATE.safe_substitute(
        org_name=org.name,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        agents_count=len(agents),
        missions_count=len(missions),
        lessons=lessons,
        docs=docs,
        agent_dna_files=dna_files,
        collections=collections,
        agent_rows=agent_rows,
        mission_rows=mission_rows,
        manifest_content=manifest_content
    )
    return HTMLResponse(html)
