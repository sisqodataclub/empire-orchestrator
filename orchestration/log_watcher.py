# orchestration/log_watcher.py
#
# Background watcher. Every `interval_seconds`, sweeps container logs
# for new error lines and queues one aggregated [AUTO-ALERT] message
# into the CEO's inbox. The CEO's normal loop picks it up and
# investigates.
#
# Sent from the user thread — the CEO sees it as a message from the
# user and replies to the user thread, which the Telegram poller
# delivers.
#
# Dedup: a signature of (container, first 80 chars of message) is
# remembered so the same error doesn't fire repeatedly. The set is
# bounded.
#
# Container whitelist: pass `containers=[...]` to watch only those.
# Pass `containers=None` (or []) to watch every accessible container.
# Names must match `docker ps` exactly. A typo means the container is
# silently skipped.
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from orchestration import inbox

logger = logging.getLogger(__name__)


def _signature(container: str, msg: str) -> str:
    return f"{container}::{msg[:80]}"


def start_log_watcher(
    user_thread: str,
    interval_seconds: int = 300,
    lookback_minutes: int = 6,
    containers: list[str] | None = None,
) -> None:
    """
    Poll container logs. When new error lines appear, queue one
    aggregated [AUTO-ALERT] message for the CEO.

    Args:
      user_thread:      The user_<id> thread name. Alerts are written
                        as if sent by this thread, so the CEO replies
                        to the user.
      interval_seconds: Poll interval (default 300 = 5 min).
      lookback_minutes: How far back each poll looks (default 6,
                        slightly larger than interval to avoid gaps).
      containers:       Optional list of container names to watch.
                        If None or empty, every container is watched.
                        Matches `docker ps` names exactly.
    """
    from tools.container_logs_tool import (
        _container_map, _iter_log_entries, _ERROR_RE,
    )

    allowed = set(containers) if containers else None
    seen: set[str] = set()

    def _watch():
        if allowed:
            logger.info(
                f"log watcher started (every {interval_seconds}s, "
                f"watching {len(allowed)} container(s): "
                f"{', '.join(sorted(allowed))})"
            )
        else:
            logger.info(
                f"log watcher started (every {interval_seconds}s, "
                f"watching ALL containers)"
            )

        while True:
            try:
                cmap = _container_map()
                cutoff = datetime.now(timezone.utc) - timedelta(
                    minutes=lookback_minutes
                )
                new: list[tuple[str, str, str]] = []

                for name, path in cmap.items():
                    if allowed is not None and name not in allowed:
                        continue

                    for ts, _stream, msg in _iter_log_entries(path, max_lines=500):
                        if ts:
                            try:
                                entry_ts = datetime.fromisoformat(
                                    ts.replace("Z", "+00:00")
                                )
                                if entry_ts < cutoff:
                                    continue
                            except Exception:
                                pass

                        if not _ERROR_RE.search(msg):
                            continue

                        sig = _signature(name, msg)
                        if sig in seen:
                            continue
                        seen.add(sig)
                        new.append((name, ts, msg))

                # Bound the dedup set so it can't grow forever.
                if len(seen) > 5000:
                    seen.clear()

                if new:
                    containers_hit = {c for c, _, _ in new}
                    lines = [
                        f"🚨 [AUTO-ALERT] {len(new)} new error line(s) "
                        f"across {len(containers_hit)} container(s).",
                        "",
                        "Investigate and report to the user. Do NOT take "
                        "corrective action without approval.",
                        "",
                    ]
                    for container, ts, msg in new[:15]:
                        lines.append(
                            f"  • {container} @ {ts[:19]}: {msg[:180]}"
                        )
                    if len(new) > 15:
                        lines.append(f"  ...and {len(new) - 15} more.")

                    inbox.send(
                        thread="ceo",
                        sender=user_thread,
                        body="\n".join(lines),
                    )
                    logger.warning(
                        f"log watcher queued alert: {len(new)} line(s) "
                        f"across {len(containers_hit)} container(s)"
                    )

            except Exception:
                logger.exception("log watcher iteration failed")

            time.sleep(interval_seconds)

    threading.Thread(
        target=_watch, daemon=True, name="log-watcher"
    ).start()
