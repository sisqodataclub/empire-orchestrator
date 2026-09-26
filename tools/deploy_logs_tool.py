# tools/deploy_logs_tool.py
#
# Read-only access to deployment logs written by /opt/deploy-wrapper.sh.
#
# Logs live inside each organisation's own workspace, at:
#     <org_workspace>/deploy_logs/deploy-<service>-<ts>.log
#
# The org is discovered from, in order:
#   1. $ORG_ID env var (set by telegram_bridge_empire.py on the worker)
#   2. Current working directory (worker does os.chdir(ORG_WORKSPACE))
#   3. If exactly one org_* exists under /app/data/workspaces, use it
#
# This makes the tool work both inside the worker (which chdir's to
# the org) and in `docker compose exec` tests (which don't).
#
# Fallback locations (used only if the primary path is empty):
#   - /app/data/workspaces/deploy_logs  (legacy shared location)
#   - /host_tmp                          (legacy /tmp bind mount)
#
# Deliberately read-only: no docker socket, no exec, no rebuild trigger.
import os
import re
from datetime import datetime
from pathlib import Path

from crewai.tools import tool


_WORKSPACES_ROOT = Path("/app/data/workspaces")

# Pattern that matches the wrapper's log filenames:
#     deploy-<service>-YYYYMMDD-HHMMSS.log
_FILENAME_RE = re.compile(r"^deploy-.+-\d{8}-\d{6}\.log$")

# Lines that mean something went wrong during a build/deploy.
_FAILURE_RE = re.compile(
    r"(ERROR|failed|error during build|"
    r"did not complete successfully|exit code: [1-9]|"
    r"ERR_PNPM|Transform failed|SyntaxError|Traceback|"
    r"cannot execute binary file)",
    re.IGNORECASE,
)


def _log(tool_name: str, detail: str) -> None:
    try:
        from empire_tools import log_agent_action
        log_agent_action(tool_name, detail)
    except Exception:
        pass


def _current_org_workspace() -> Path | None:
    """
    Return the current org's workspace directory, or None if it can't
    be determined.
    """
    # 1. Explicit env var — the worker process has this.
    org_id = os.getenv("ORG_ID", "").strip()
    if org_id:
        candidate = _WORKSPACES_ROOT / f"org_{org_id}"
        if candidate.is_dir():
            return candidate

    # 2. Current working directory — the worker chdir's here.
    try:
        cwd = Path.cwd()
        if cwd.name.startswith("org_") and cwd.is_dir():
            return cwd
    except Exception:
        pass

    # 3. Single-org fallback — handy for docker compose exec tests.
    if _WORKSPACES_ROOT.is_dir():
        try:
            orgs = [
                p for p in _WORKSPACES_ROOT.iterdir()
                if p.is_dir() and p.name.startswith("org_")
            ]
        except Exception:
            orgs = []
        if len(orgs) == 1:
            return orgs[0]

    return None


def _candidate_dirs() -> list[Path]:
    """
    Directories to search for deploy logs, in priority order.
    First entry is the current org's own deploy_logs. The rest are
    legacy fallbacks.
    """
    dirs: list[Path] = []

    org_ws = _current_org_workspace()
    if org_ws:
        dirs.append(org_ws / "deploy_logs")

    # Legacy shared locations.
    dirs.append(_WORKSPACES_ROOT / "deploy_logs")
    dirs.append(Path("/host_tmp"))

    return dirs


def _find_deploy_logs() -> list[Path]:
    """
    Return every deploy-*.log, newest first. Reads from the first
    candidate directory that actually contains any matching files,
    so a stale legacy folder can't shadow the current org's logs.
    """
    for d in _candidate_dirs():
        if not d.exists():
            continue
        found: list[Path] = []
        try:
            for p in d.iterdir():
                if p.is_file() and _FILENAME_RE.match(p.name):
                    found.append(p)
        except Exception:
            continue
        if found:
            found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return found
    return []


def _active_dir() -> Path | None:
    """Return the directory that _find_deploy_logs() read from."""
    for d in _candidate_dirs():
        if not d.exists():
            continue
        try:
            for p in d.iterdir():
                if p.is_file() and _FILENAME_RE.match(p.name):
                    return d
        except Exception:
            continue
    return None


# ──────────────────────────────────────────────────────────────────────
@tool("List Deploy Logs")
def list_deploy_logs():
    """
    List this organisation's deployment log files, newest first.
    Returns the filename, size, and last-modified time.

    Use this when the user asks about a recent deploy, or when a
    container's own logs point at a build that failed before it started.
    """
    _log("List Deploy Logs", "")
    logs = _find_deploy_logs()
    if not logs:
        searched = ", ".join(str(d) for d in _candidate_dirs())
        return (
            f"📭 No deploy logs found for this organisation.\n"
            f"Looked in: {searched}"
        )

    active = _active_dir()
    lines = [f"📦 {len(logs)} deploy log(s) in {active}:"]
    for p in logs:
        try:
            stat = p.stat()
            ts = datetime.fromtimestamp(stat.st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            lines.append(f"  • {p.name}  ({stat.st_size:,} B, {ts})")
        except Exception:
            lines.append(f"  • {p.name}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
@tool("Read Deploy Log")
def read_deploy_log(filename: str = "", tail: int = 200):
    """
    Read a deployment log by filename.

    Args:
      filename: Exact filename from List Deploy Logs, e.g.
                'deploy-forms-20260925-185129.log'. If empty, the most
                recent log is used.
      tail:     Lines from the end (default 200, capped at 2000).

    Returns the matching lines, oldest first.
    """
    _log("Read Deploy Log", f"{filename or '(newest)'} tail={tail}")

    logs = _find_deploy_logs()
    if not logs:
        return "📭 No deploy logs found for this organisation."

    path = None
    if filename:
        for p in logs:
            if p.name == filename:
                path = p
                break
        if path is None:
            names = ", ".join(p.name for p in logs[:5])
            return f"❌ No deploy log named '{filename}'. Available: {names}"
    else:
        path = logs[0]

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"❌ Could not read {path.name}: {e}"

    tail = max(1, min(int(tail or 200), 2000))
    chunk = lines[-tail:]
    body = "".join(chunk).rstrip()
    header = f"📋 {path.name} — last {len(chunk)} line(s):\n"
    return header + body


# ──────────────────────────────────────────────────────────────────────
@tool("Scan Deploy Failures")
def scan_deploy_failures(lookback_logs: int = 5):
    """
    Sweep this organisation's recent deploy logs for failure lines.

    Use this after a container starts reporting "connection refused"
    or "no such host" on its own errors — the cause is often a build
    that failed before the container even existed.

    Args:
      lookback_logs: How many of the most recent logs to scan
                     (default 5, capped at 20).
    """
    _log("Scan Deploy Failures", f"lookback={lookback_logs}")

    logs = _find_deploy_logs()
    if not logs:
        return "📭 No deploy logs found for this organisation."

    lookback_logs = max(1, min(int(lookback_logs or 5), 20))
    findings: list[tuple[str, list[str]]] = []

    for path in logs[:lookback_logs]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            continue
        matches = [
            line.rstrip()
            for line in content.splitlines()
            if _FAILURE_RE.search(line)
        ]
        if matches:
            findings.append((path.name, matches[:25]))

    if not findings:
        return (
            f"✅ No failure lines in the last {lookback_logs} deploy log(s)."
        )

    lines = [f"🚨 Failures in {len(findings)} deploy log(s):\n"]
    for name, matches in findings:
        lines.append(f"\n━━━ {name} ({len(matches)} line(s)) ━━━")
        lines.extend(matches)
    return "\n".join(lines)
