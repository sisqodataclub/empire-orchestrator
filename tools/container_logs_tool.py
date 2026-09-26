# tools/container_logs_tool.py
#
# Read-only access to the host's Docker container logs.
#
# The host's /var/lib/docker/containers directory is bind-mounted at
# /host_containers (read-only). The tool walks that tree, reads each
# container's config.v2.json to map hash → name, and parses the
# JSON-line log files Docker writes for each container.
#
# Deliberately read-only: no docker socket, no exec, no restart, no
# container control. The agent observes; the user decides.
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crewai.tools import tool


CONTAINERS_ROOT = "/host_containers"

# Patterns that suggest something is wrong. Case-insensitive.
_ERROR_PATTERNS = [
    r"\bERROR\b",
    r"\bCRITICAL\b",
    r"\bFATAL\b",
    r"\bpanic\b",
    r"Traceback \(most recent call last\)",
    r"\bException\b",
    r"connect\(\) failed",
    r"upstream timed out",
    r"\b502\b", r"\b503\b", r"\b504\b",
    r"Bad Gateway", r"Service Unavailable", r"Gateway Time-out",
    r"Connection refused",
    r"permission denied",
    r"\bsegfault\b",
    r"Out of memory",
    r"\bOOMKilled\b",
]

_ERROR_RE = re.compile("|".join(_ERROR_PATTERNS), re.IGNORECASE)


def _log(tool_name: str, detail: str) -> None:
    try:
        from empire_tools import log_agent_action
        log_agent_action(tool_name, detail)
    except Exception:
        pass


def _container_map() -> dict[str, Path]:
    """
    Walk /host_containers/*/config.v2.json and return
    {container_name: path_to_json_log}.
    """
    out = {}
    root = Path(CONTAINERS_ROOT)
    if not root.exists():
        return out

    for cid_dir in root.iterdir():
        if not cid_dir.is_dir():
            continue
        cfg_path = cid_dir / "config.v2.json"
        log_path = cid_dir / f"{cid_dir.name}-json.log"
        if not cfg_path.exists() or not log_path.exists():
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = cfg.get("Name", "").lstrip("/")
        if name:
            out[name] = log_path
    return out


def _iter_log_entries(path: Path, max_lines: int | None = None):
    """Yield (timestamp, stream, message) from a Docker json-file log."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return

    if max_lines:
        lines = lines[-max_lines:]

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        yield (
            entry.get("time", ""),
            entry.get("stream", ""),
            entry.get("log", "").rstrip("\n"),
        )


def _within(ts: str, cutoff: datetime) -> bool:
    if not ts:
        return True
    try:
        entry_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return entry_ts >= cutoff
    except Exception:
        return True


# ──────────────────────────────────────────────────────────────────────
@tool("List Containers")
def list_containers():
    """
    List every container whose logs are accessible, by name. Use this
    first to find the exact container name before calling
    read_container_logs.

    The agent can READ container logs but cannot start, stop, exec
    into, or modify containers. Observation only.
    """
    _log("List Containers", "")
    cmap = _container_map()
    if not cmap:
        return (
            "❌ No container logs accessible. The host's "
            "/var/lib/docker/containers must be bind-mounted read-only "
            f"at {CONTAINERS_ROOT}."
        )
    lines = [f"📦 {len(cmap)} containers with accessible logs:"]
    for name in sorted(cmap):
        lines.append(f"  • {name}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
@tool("Read Container Logs")
def read_container_logs(
    container: str,
    tail: int = 100,
    grep: str = "",
    since_minutes: int = 0,
):
    """
    Read the most recent log lines for one container.

    Args:
      container:      Exact container name (e.g. 'ddeep-forms').
                      Get the exact name from List Containers.
      tail:           Lines to return from the end (default 100,
                      capped at 1000).
      grep:           Optional regex. Only matching lines are
                      returned. Use this to focus on one error
                      keyword.
      since_minutes:  If > 0, only entries newer than this many
                      minutes.

    Returns the matching lines, oldest first.
    """
    _log("Read Container Logs",
         f"{container} tail={tail} grep={grep!r} since={since_minutes}m")

    cmap = _container_map()
    path = cmap.get(container)
    if not path:
        avail = ", ".join(sorted(cmap.keys())[:10]) or "(none)"
        return f"❌ No logs found for '{container}'. Available: {avail}"

    tail = max(1, min(int(tail or 100), 1000))
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=int(since_minutes))
        if since_minutes and since_minutes > 0 else None
    )

    grep_re = None
    if grep:
        try:
            grep_re = re.compile(grep, re.IGNORECASE)
        except re.error as e:
            return f"❌ Invalid grep pattern: {e}"

    out = []
    for ts, stream, msg in _iter_log_entries(path, max_lines=tail * 5):
        if cutoff and not _within(ts, cutoff):
            continue
        if grep_re and not grep_re.search(msg):
            continue
        prefix = "stderr" if stream == "stderr" else "stdout"
        out.append(f"[{ts}] [{prefix}] {msg}")
        if len(out) >= tail:
            break

    if not out:
        return f"📭 No matching log lines in '{container}'."

    return (
        f"📋 Last {len(out)} line(s) from {container}:\n"
        + "\n".join(out[-tail:])
    )


# ──────────────────────────────────────────────────────────────────────
@tool("Scan For Errors")
def scan_for_errors(since_minutes: int = 15, max_per_container: int = 20):
    """
    Sweep every accessible container's recent logs for error-looking
    lines — exceptions, 5xx upstream failures, connection refused,
    OOM, and so on.

    Use this for a broad health check, or when an [AUTO-ALERT] arrives
    and you want the full picture across the stack.

    Args:
      since_minutes:      Look-back window (default 15).
      max_per_container:  Cap on matched lines per container
                          (default 20).

    Returns a grouped summary.
    """
    _log("Scan For Errors", f"since={since_minutes}m")

    cmap = _container_map()
    if not cmap:
        return "❌ No container logs accessible."

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=int(since_minutes))
    findings: dict[str, list[str]] = {}

    for name, path in cmap.items():
        matches = []
        for ts, stream, msg in _iter_log_entries(path, max_lines=2000):
            if not _within(ts, cutoff):
                continue
            if _ERROR_RE.search(msg):
                matches.append(f"[{ts}] [{stream}] {msg}")
                if len(matches) >= max_per_container:
                    break
        if matches:
            findings[name] = matches

    if not findings:
        return f"✅ No error-looking lines in the last {since_minutes} minutes."

    lines = [
        f"🚨 Errors found in {len(findings)} container(s) "
        f"in the last {since_minutes} minutes:\n"
    ]
    for name, matches in sorted(findings.items()):
        lines.append(f"\n━━━ {name} ({len(matches)} line(s)) ━━━")
        lines.extend(matches)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
@tool("Container Health")
def container_health():
    """
    Compact health snapshot of every accessible container: last log
    timestamp and a count of error-looking lines in the most recent
    200 entries. Use this for a quick 'is everything ok?' check
    before a deeper investigation.
    """
    _log("Container Health", "")

    cmap = _container_map()
    if not cmap:
        return "❌ No container logs accessible."

    rows = []
    for name, path in sorted(cmap.items()):
        last_ts = "—"
        err_count = 0
        for ts, _stream, msg in _iter_log_entries(path, max_lines=200):
            last_ts = ts or last_ts
            if _ERROR_RE.search(msg):
                err_count += 1
        flag = "🟢" if err_count == 0 else "🔴"
        rows.append(
            f"  {flag} {name:30}  last={last_ts[:19]}  errors={err_count}"
        )

    return "🩺 Container health (last 200 log lines each):\n" + "\n".join(rows)
