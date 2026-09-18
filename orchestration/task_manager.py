#
# orchestration/task_manager.py
#
# TaskManager — owns the set of currently-running ActiveTasks and
# orchestrates spawning, listing, intervening, and dream-state consolidation.
#
# Supports concurrent missions: each call to start_mission() or
# spawn_delegated_task() creates a new ActiveTask in its own thread.
#
# Observability (new):
#   • Keeps a live `_health` map: task_id → {heartbeat, step, mission_snippet,
#     thread_id, started_at}.
#   • Heartbeats are updated by ActiveTask each turn via `heartbeat()`.
#   • A watchdog thread runs every 15s. If any RUNNING task's heartbeat is
#     older than `_stuck_threshold_sec` (90s), it fires a "mission appears
#     stuck" reply on the task's thread and marks the task as STUCK.
#   • `health_snapshot()` returns a list of dicts that the CEO's
#     system_status() / inspect_task() tools consume.
#

import os
import time
import threading
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
import logging

from llm import NativeLLM
from .active_task import ActiveTask


load_dotenv()
logger = logging.getLogger(__name__)


# Watchdog defaults
_DEFAULT_STUCK_THRESHOLD_SEC = 90
_WATCHDOG_POLL_INTERVAL_SEC  = 15


class TaskManager:
    def __init__(self):
        self.tasks: dict = {}
        # Simple monotonic counter — avoids collisions if a task is removed.
        self._next_task_num = 1
        self._lock = threading.Lock()

        # ── Health / observability ──
        # task_id → {
        #     "heartbeat":   float  (unix ts of last heartbeat),
        #     "step":        str    (human-readable current step),
        #     "mission_snippet": str,
        #     "thread_id":   str,
        #     "started_at":  float,
        #     "stuck_reported": bool,  # set once the watchdog has already fired
        # }
        self._health: dict = {}
        self._stuck_threshold_sec = _DEFAULT_STUCK_THRESHOLD_SEC
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_watchdog = threading.Event()

        self.director_llm = NativeLLM(
            api_key=os.getenv("deepseek"),
            temperature=0.7,
        )
        logger.info("TaskManager initialised")

    # ------------------------------------------------------------------
    # Task creation
    # ------------------------------------------------------------------
    def _next_id(self) -> str:
        with self._lock:
            tid = str(self._next_task_num)
            self._next_task_num += 1
            return tid

    def start_mission(self, mission: str, agents: list) -> str:
        """Create a new ActiveTask, register it, and start its loop thread."""
        task_id = self._next_id()
        logger.info(f"Starting mission {task_id}: {mission[:100]}")

        new_task = ActiveTask(
            task_id,
            mission,
            agents,
            self.director_llm,
            task_manager=self,
        )
        self.tasks[task_id] = new_task
        new_task.start()
        return task_id

    def spawn_delegated_task(self, mission_text: str, priority: str = "normal") -> str:
        """
        Spawn a concurrent ActiveTask for a delegated worker.

        Called by AgentBus.delegate(). The worker runs in its own thread and
        replies to the CEO via AgentBus.agent_reply_to_ceo() when done.

        Uses lazy imports of gm to avoid a circular import at module load
        (gm.py imports task_manager at the top level).
        """
        # Lazy import — gm imports task_manager, so we can't import at top level.
        from gm import get_population, build_mission_prompt

        full_mission = build_mission_prompt(mission_text, priority=priority)
        population = get_population(mcp_tools=[])

        task_id = self.start_mission(full_mission, population)
        logger.info(f"Spawned delegated task {task_id} (mission len={len(full_mission)})")
        return task_id

    # ------------------------------------------------------------------
    # Task access
    # ------------------------------------------------------------------
    def list_tasks(self):
        return list(self.tasks.values())

    def get_task(self, tid: str):
        return self.tasks.get(tid)

    def active_count(self) -> int:
        """Number of tasks currently not complete."""
        return sum(
            1 for t in self.tasks.values()
            if not getattr(t, "is_complete", False)
        )

    # ------------------------------------------------------------------
    # Intervention
    # ------------------------------------------------------------------
    def intervene(self, tid: str, instruction: str) -> bool:
        task = self.get_task(tid)
        if task:
            logger.info(f"Intervening in task {tid} with: {instruction[:100]}")
            task.intervene(instruction)
            return True
        logger.warning(f"Intervention attempted for non-existent task {tid}")
        return False

    # ==================================================================
    # HEALTH / OBSERVABILITY API
    # ==================================================================

    def register_task(self, task_id: str, mission: str, thread_id: str) -> None:
        """
        Called by ActiveTask.__init__ so the manager starts tracking a task's
        heartbeat. thread_id is used by the watchdog to route the "stuck"
        notification back to the right inbox thread.
        """
        now = time.time()
        with self._lock:
            self._health[task_id] = {
                "heartbeat":       now,
                "step":            "initialising",
                "mission_snippet": (mission or "")[:120],
                "thread_id":       thread_id or "console",
                "started_at":      now,
                "stuck_reported":  False,
            }
        logger.debug(f"Registered task #{task_id} on thread '{thread_id}'")

    def unregister_task(self, task_id: str) -> None:
        """Called when a task completes or is cancelled."""
        with self._lock:
            self._health.pop(str(task_id), None)
        logger.debug(f"Unregistered task #{task_id}")

    def heartbeat(self, task_id: str, step: str = "") -> None:
        """
        Called by ActiveTask once per turn (and at key sub-steps like before
        the LLM call). Updates the last-seen timestamp and current step.
        """
        with self._lock:
            entry = self._health.get(str(task_id))
            if entry is None:
                # Late registration — create a minimal entry so we don't lose it.
                now = time.time()
                self._health[str(task_id)] = {
                    "heartbeat":       now,
                    "step":            step or "?",
                    "mission_snippet": "",
                    "thread_id":       "console",
                    "started_at":      now,
                    "stuck_reported":  False,
                }
            else:
                entry["heartbeat"] = time.time()
                if step:
                    entry["step"] = step

    def health_snapshot(self) -> list:
        """
        Return a list of dicts describing every tracked task. Consumed by the
        CEO's system_status() and inspect_task() tools.

        Fields per entry:
            id                       str
            status                   str  (task.status)
            mission                  str
            thread_id                str
            step                     str
            seconds_since_heartbeat  int
            runtime_sec              int
            is_stuck                 bool
            is_complete              bool
        """
        now = time.time()
        with self._lock:
            entries = list(self._health.items())

        out = []
        for tid, info in entries:
            task = self.tasks.get(tid)
            if task is None:
                continue
            age = int(now - info["heartbeat"])
            out.append({
                "id":                      tid,
                "status":                  task.status,
                "mission":                 info.get("mission_snippet", "") or task.mission[:120],
                "thread_id":               info.get("thread_id", "?"),
                "step":                    info.get("step", "?"),
                "seconds_since_heartbeat": age,
                "runtime_sec":             int(now - info.get("started_at", now)),
                "is_stuck": (
                    task.status == "RUNNING"
                    and age > self._stuck_threshold_sec
                ),
                "is_complete":             task.is_complete,
            })
        return out

    # ==================================================================
    # WATCHDOG — proactive stuck-mission alerting
    # ==================================================================

    def start_watchdog(self) -> None:
        """Start the background watchdog thread (idempotent)."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._stop_watchdog.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True
        )
        self._watchdog_thread.start()
        logger.info(
            f"TaskManager watchdog started "
            f"(threshold={self._stuck_threshold_sec}s, "
            f"poll={_WATCHDOG_POLL_INTERVAL_SEC}s)"
        )

    def stop_watchdog(self) -> None:
        self._stop_watchdog.set()

    def _watchdog_loop(self) -> None:
        while not self._stop_watchdog.is_set():
            try:
                self._check_stuck_tasks()
            except Exception:
                logger.exception("Watchdog iteration failed")
            self._stop_watchdog.wait(_WATCHDOG_POLL_INTERVAL_SEC)

    def _check_stuck_tasks(self) -> None:
        """Scan health entries; for any stuck task, fire the escape hatch."""
        now = time.time()
        with self._lock:
            entries = list(self._health.items())

        for tid, info in entries:
            task = self.tasks.get(tid)
            if task is None or task.is_complete:
                continue
            if task.status != "RUNNING":
                continue

            age = now - info["heartbeat"]
            if age <= self._stuck_threshold_sec:
                continue

            logger.error(
                f"Task #{tid} STUCK for {int(age)}s "
                f"(last step: {info.get('step','?')})"
            )
            try:
                self._handle_stuck_task(tid, task, info, age)
            except Exception:
                logger.exception(f"Failed to handle stuck task #{tid}")

    def _handle_stuck_task(self, tid: str, task, info: dict, age: float) -> None:
        """
        Force a "mission is stuck" reply on the task's thread, then mark the
        task as STUCK. Idempotent — the `stuck_reported` flag prevents the
        watchdog from firing more than once per task.
        """
        thread_id = info.get("thread_id")
        if not thread_id:
            return

        # Only fire the escape hatch once per task
        with self._lock:
            entry = self._health.get(tid)
            if not entry or entry.get("stuck_reported"):
                return
            entry["stuck_reported"] = True

        # Compose and post a stuck notification
        body = (
            f"⚠️ Mission #{tid} appears to be stuck. "
            f"Last activity {int(age)}s ago "
            f"(step: {info.get('step', '?')}). "
            f"I've logged the issue — please try again or ask "
            f"for a fresh attempt."
        )

        try:
            from orchestration.inbox_db import InboxDB
            inbox = InboxDB(
                os.path.join(os.getcwd(), "ai_civilization", "inbox.db")
            )
            inbox.add_message(
                thread_id=thread_id,
                direction="OUT",
                body=body,
                sender="CEO",
                recipient="user",
                status="PENDING_DELIVERY",
            )
            logger.info(
                f"Emitted stuck notification for task #{tid} on thread '{thread_id}'"
            )
        except Exception:
            logger.exception(
                f"Could not deliver stuck notification for #{tid}"
            )

        # Mark the task as STUCK so it stops counting as RUNNING.
        # Leave the thread alive so the user can still inspect or cancel.
        try:
            task.status = "STUCK"
            task.save_history_to_disk()
        except Exception:
            logger.exception(f"Could not mark task #{tid} as STUCK")

    # ------------------------------------------------------------------
    # Dream state (unchanged)
    # ------------------------------------------------------------------
    def trigger_dream_state(self) -> None:
        active = [
            t for t in self.tasks.values()
            if t.status not in ("COMPLETED", "AWAITING_OVERLORD")
        ]
        if active:
            logger.debug("Dream state skipped – active missions present")
            return
        logger.info("Triggering dream state consolidation")
        threading.Thread(target=self._dream_state_worker, daemon=True).start()

    def _dream_state_worker(self) -> None:
        try:
            from empire_tools import library_collection

            logger.debug("Dream state worker started")
            all_docs = library_collection.get(
                include=["documents", "metadatas", "ids"]
            )
            if not all_docs or not all_docs.get("documents"):
                logger.info("Dream state: no documents to consolidate")
                return

            docs      = all_docs["documents"]
            metadatas = all_docs["metadatas"]
            ids       = all_docs["ids"]

            report_ids: list = []
            lesson_texts: list = []
            for doc, meta, doc_id in zip(docs, metadatas, ids):
                if meta.get("type") == "intelligence_report":
                    report_ids.append(doc_id)
                else:
                    lesson_texts.append(doc[:600])

            if not lesson_texts:
                logger.info("Dream state: no lesson fragments to consolidate")
                return

            logger.info(
                f"Dream state: consolidating {len(lesson_texts)} fragments"
            )
            consolidation_prompt = (
                f"You are the Imperial Archivist. Consolidate "
                f"{len(lesson_texts)} knowledge fragments into a "
                f"Master Architecture File.\n\n"
                f"FRAGMENTS:\n"
                + "\n---\n".join(lesson_texts[:30])
                + "\n\nINSTRUCTIONS:\n"
                "1. Discard: outdated facts, contradictions, vague generalities.\n"
                "2. Merge: repeated patterns into authoritative rules.\n"
                "3. Output: Architecture Patterns | Known Bugs & Fixes | "
                "API Contracts | Deployment Rules | Anti-Patterns. "
                "Be specific. Max 2000 words."
            )

            master_doc = self.director_llm.call(
                messages=[{"role": "user", "content": consolidation_prompt}]
            )

            if report_ids:
                logger.debug(
                    f"Dream state: deleting {len(report_ids)} stale reports"
                )
                library_collection.delete(ids=report_ids[:50])

            master_id = (
                f"MASTER_DOC_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            library_collection.upsert(
                documents=[master_doc],
                ids=[master_id],
                metadatas=[{
                    "type":        "MASTER_DOC",
                    "concept":     "Master Architecture File",
                    "created_at":  datetime.now().isoformat(),
                    "trust_score": 1.0,
                }],
            )
            logger.info(f"Dream state: Master doc {master_id} created")

            dream_path = os.path.join(
                "ai_civilization",
                "dream_state",
                f"master_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
            )
            os.makedirs(os.path.dirname(dream_path), exist_ok=True)
            with open(dream_path, "w", encoding="utf-8") as f:
                f.write(
                    f"# Master Architecture File\n"
                    f"_Consolidated: {datetime.now().isoformat()}_\n\n"
                )
                f.write(
                    f"_Merged {len(lesson_texts)} fragments. "
                    f"Deleted {len(report_ids)} stale reports._\n\n"
                )
                f.write(master_doc)
            logger.info(f"Dream state: Master doc saved to {dream_path}")

        except Exception:
            logger.exception("Dream state worker failed")
