#!/usr/bin/env python3
"""
Dynamic Telegram Bot Manager (subprocess-based).

Reads bot configurations from telegram_db and spawns a separate worker process
for each organisation. Each worker runs the fast internal engine for its own
workspace and handles Telegram messages quickly.

The manager monitors the database every 60 seconds and starts/stops workers
as configurations change.

Recent changes:
  • Worker stdout/stderr is now forwarded via a single logger thread per worker
    and prefixed `[worker <org_id>]`, so log lines from all orgs remain
    distinguishable even when many are running.
  • Graceful shutdown on SIGTERM/SIGINT: all child workers are terminated
    before the manager exits.
  • Config changes (new / removed orgs) are picked up on the 60s tick.
"""

import os
import sys
import time
import signal
import threading
import subprocess

from dotenv import load_dotenv

from telegram_db import get_all_active_bots

load_dotenv()

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/app/data/workspaces")
WORKER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "org_bot_worker.py"
)

# Dictionary to track running workers: org_id -> subprocess.Popen
running_workers: dict = {}
_shutdown = threading.Event()


# ══════════════════════════════════════════════════════════════════════════
# Worker lifecycle
# ══════════════════════════════════════════════════════════════════════════
def start_worker(config: dict) -> None:
    org_id = config["organization_id"]
    bot_token = config["bot_token"]
    allowed_user_ids = config["allowed_user_ids"]

    env = os.environ.copy()
    env.update({
        "ORG_ID": org_id,
        "BOT_TOKEN": bot_token,
        "ALLOWED_USER_IDS": allowed_user_ids,
        "WORKSPACE_ROOT": WORKSPACE_ROOT,
    })

    try:
        proc = subprocess.Popen(
            [sys.executable, WORKER_SCRIPT],
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        running_workers[org_id] = proc
        print(f"✅ Started worker for org {org_id} (PID {proc.pid})", flush=True)

        # Forward worker output with a prefix so logs stay readable.
        def _forward(proc: subprocess.Popen, org_id: str) -> None:
            try:
                for line in proc.stdout:
                    print(f"[worker {org_id}] {line.rstrip()}", flush=True)
            except Exception as e:
                print(f"[worker {org_id}] forward error: {e}", flush=True)

        threading.Thread(
            target=_forward, args=(proc, org_id), daemon=True
        ).start()

    except Exception as e:
        print(f"❌ Failed to start worker for org {org_id}: {e}", flush=True)


def stop_worker(org_id: str) -> None:
    proc = running_workers.pop(org_id, None)
    if not proc:
        return
    print(f"🛑 Stopping worker for org {org_id}", flush=True)
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except Exception as e:
        print(f"⚠️ Error stopping worker {org_id}: {e}", flush=True)
    print(f"✅ Worker {org_id} stopped", flush=True)


def stop_all_workers() -> None:
    for org_id in list(running_workers.keys()):
        stop_worker(org_id)


# ══════════════════════════════════════════════════════════════════════════
# Shutdown handling
# ══════════════════════════════════════════════════════════════════════════
def _handle_signal(signum, _frame):
    print(f"\n⚠️ Received signal {signum} — shutting down workers…", flush=True)
    _shutdown.set()
    stop_all_workers()
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ══════════════════════════════════════════════════════════════════════════
# Main monitor loop
# ══════════════════════════════════════════════════════════════════════════
def main() -> None:
    print("✅ EMPIRE TELEGRAM BOT MANAGER ONLINE (subprocess mode).", flush=True)

    # Initial start
    try:
        configs = get_all_active_bots()
    except Exception as e:
        print(f"❌ Could not load initial bot configs: {e}", flush=True)
        configs = []
    for config in configs:
        start_worker(config)

    # Monitor loop
    while not _shutdown.is_set():
        time.sleep(60)
        try:
            current_configs = get_all_active_bots()
        except Exception as e:
            print(f"⚠️ Bot config refresh failed: {e}", flush=True)
            continue

        current_org_ids = {c["organization_id"] for c in current_configs}
        running_org_ids = set(running_workers.keys())

        # Detect crashed workers and remove them from the tracked set so the
        # start-loop below can respawn them.
        for org_id, proc in list(running_workers.items()):
            if proc.poll() is not None:
                print(
                    f"⚠️ Worker for org {org_id} exited "
                    f"(code {proc.returncode}); will restart.",
                    flush=True,
                )
                running_workers.pop(org_id, None)
                running_org_ids.discard(org_id)

        # Start new workers
        for config in current_configs:
            if config["organization_id"] not in running_org_ids:
                start_worker(config)

        # Stop removed workers
        for org_id in running_org_ids - current_org_ids:
            stop_worker(org_id)


if __name__ == "__main__":
    main()
