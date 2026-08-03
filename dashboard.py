#!/usr/bin/env python3
"""
dashboard.py
CEO‑friendly web dashboard for the Empire system.
Displays agent roster, mission history, library stats, and system health.
"""

import os
import json
import glob
from datetime import datetime
from flask import Flask, render_template_string, jsonify
from empire_tools import chroma_client, library_collection, docs_collection

app = Flask(__name__)

# ─── HTML TEMPLATE ──────────────────────────────────────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🏛️ Empire Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #30363d;
            padding-bottom: 15px;
            margin-bottom: 30px;
        }
        header h1 {
            font-size: 2.2rem;
            font-weight: 300;
            color: #f0f6fc;
        }
        header h1 span { color: #ff7b72; }
        .last-updated {
            font-size: 0.9rem;
            color: #8b949e;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 20px;
            text-align: center;
        }
        .stat-card .number {
            font-size: 2.5rem;
            font-weight: 600;
            color: #f0f6fc;
        }
        .stat-card .label {
            font-size: 0.9rem;
            color: #8b949e;
            margin-top: 5px;
        }
        .section {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 30px;
        }
        .section h2 {
            font-size: 1.3rem;
            font-weight: 400;
            color: #f0f6fc;
            margin-bottom: 15px;
            border-bottom: 1px solid #30363d;
            padding-bottom: 10px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
        }
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
        }
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
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }
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
            max-width: 300px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: block;
        }
        @media (max-width: 600px) {
            .stats-grid { grid-template-columns: 1fr 1fr; }
            .memory-stats { grid-template-columns: 1fr; }
            .backstory-preview { max-width: 150px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🏛️ Empire <span>Dashboard</span></h1>
            <div class="last-updated">🔄 Last updated: {{ now }}</div>
        </header>

        <!-- Stats -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="number">{{ agents|length }}</div>
                <div class="label">👥 Agents</div>
            </div>
            <div class="stat-card">
                <div class="number">{{ missions|length }}</div>
                <div class="label">📋 Missions</div>
            </div>
            <div class="stat-card">
                <div class="number">{{ lessons }}</div>
                <div class="label">📚 Library Lessons</div>
            </div>
            <div class="stat-card">
                <div class="number">{{ docs }}</div>
                <div class="label">📖 Official Docs</div>
            </div>
        </div>

        <!-- Agent Roster -->
        <div class="section">
            <h2>👥 Agent Roster</h2>
            <table>
                <thead>
                    <tr>
                        <th>Role</th>
                        <th>Goal</th>
                        <th>Backstory</th>
                        <th>Tools</th>
                    </tr>
                </thead>
                <tbody>
                    {% for agent in agents %}
                    <tr>
                        <td><strong>{{ agent.role }}</strong></td>
                        <td>{{ agent.goal[:80] }}{% if agent.goal|length > 80 %}…{% endif %}</td>
                        <td>
                            <span class="backstory-preview" title="{{ agent.backstory }}">
                                {{ agent.backstory[:80] }}{% if agent.backstory|length > 80 %}…{% endif %}
                            </span>
                        </td>
                        <td>
                            <div class="tools-list">
                                {% for tool in agent.tools[:5] %}
                                <span class="tool-tag">{{ tool }}</span>
                                {% endfor %}
                                {% if agent.tools|length > 5 %}
                                <span class="tool-tag">+{{ agent.tools|length - 5 }}</span>
                                {% endif %}
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <!-- Recent Missions -->
        <div class="section">
            <h2>📋 Recent Missions</h2>
            <table>
                <thead>
                    <tr><th>ID</th><th>Mission</th><th>Status</th><th>Timestamp</th></tr>
                </thead>
                <tbody>
                    {% for mission in missions[-10:]|reverse %}
                    <tr>
                        <td><strong>#{{ mission.id }}</strong></td>
                        <td>{{ mission.mission[:80] }}{% if mission.mission|length > 80 %}…{% endif %}</td>
                        <td><span class="status-badge status-{{ mission.status.lower() }}">{{ mission.status }}</span></td>
                        <td>{{ mission.timestamp }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <!-- Memory Stats -->
        <div class="section">
            <h2>🧠 Knowledge Store</h2>
            <div class="memory-stats">
                <div class="memory-item">
                    <div class="key">📚 Lessons</div>
                    <div class="value">{{ lessons }}</div>
                </div>
                <div class="memory-item">
                    <div class="key">📖 Docs</div>
                    <div class="value">{{ docs }}</div>
                </div>
                <div class="memory-item">
                    <div class="key">🧬 Agent DNA files</div>
                    <div class="value">{{ agent_dna_files }}</div>
                </div>
                <div class="memory-item">
                    <div class="key">📁 ChromaDB collections</div>
                    <div class="value">{{ collections }}</div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

# ─── DATA FETCHING ──────────────────────────────────────────────────────────

def get_default_agents():
    """Return the built‑in CEO and QA agents (hardcoded)."""
    return [
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

def get_json_agents():
    """Read DNA files from ai_civilization/ and agent_pool/."""
    agents = []
    dna_dirs = [
        "ai_civilization",
        "ai_civilization/agent_pool"
    ]
    seen_roles = set()
    for base in dna_dirs:
        if not os.path.exists(base):
            continue
        for fname in glob.glob(os.path.join(base, "*.json")):
            try:
                with open(fname, "r") as f:
                    data = json.load(f)
                    if data.get("status") != "ACTIVE":
                        continue
                    role = data.get("role", "Unknown")
                    if role in seen_roles:
                        continue
                    seen_roles.add(role)
                    tools = data.get("tools", ["system_terminal", "file_manager", "ast_inspector"])
                    # If the JSON has a 'capabilities' list, use those as tools
                    if "capabilities" in data and data["capabilities"]:
                        tools = data["capabilities"]
                    agents.append({
                        "role": role,
                        "goal": data.get("goal", "No goal defined."),
                        "backstory": data.get("backstory", "No backstory."),
                        "tools": tools
                    })
            except Exception:
                continue
    return agents

def get_agent_roster():
    """Combine default agents and JSON-loaded agents, deduplicate."""
    default_agents = get_default_agents()
    json_agents = get_json_agents()
    # Combine: default agents first, then JSON agents (if not already present)
    seen = {a["role"].lower() for a in default_agents}
    for agent in json_agents:
        if agent["role"].lower() not in seen:
            seen.add(agent["role"].lower())
            default_agents.append(agent)
    return default_agents

def get_mission_logs():
    """Read mission logs from ai_civilization/mission_logs/."""
    missions = []
    log_dir = "ai_civilization/mission_logs"
    if not os.path.exists(log_dir):
        return missions
    for fname in glob.glob(os.path.join(log_dir, "*.json")):
        try:
            with open(fname, "r") as f:
                data = json.load(f)
                # extract mission info from the conversation history
                mission_id = fname.split("_")[-1].replace(".json", "")
                mission_text = "Unknown mission"
                if data and isinstance(data, list) and len(data) > 0:
                    first = data[0]
                    mission_text = first.get("instruction_text", "").split("\n")[0][:100]
                status = "COMPLETED"
                timestamp = datetime.fromtimestamp(os.path.getmtime(fname)).strftime("%H:%M")
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

# ─── FLASK ROUTES ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    agents = get_agent_roster()
    missions = get_mission_logs()
    lessons = library_collection.count()
    docs = docs_collection.count()
    dna_files = len(glob.glob("ai_civilization/*.json")) + len(glob.glob("ai_civilization/agent_pool/*.json"))
    collections = len(chroma_client.list_collections())

    return render_template_string(
        HTML_TEMPLATE,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        agents=agents,
        missions=missions,
        lessons=lessons,
        docs=docs,
        agent_dna_files=dna_files,
        collections=collections
    )

# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🏛️  Empire Dashboard starting on http://localhost:5000")
    print("📊 Press Ctrl+C to stop.")
    app.run(host="0.0.0.0", port=5000, debug=False)
