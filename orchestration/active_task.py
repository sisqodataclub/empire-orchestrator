#
# orchestration/active_task.py
#
# ActiveTask — the runtime container for a single mission.
#
# Responsibilities:
#   • Parse the mission text to determine mission type
#       - ASSISTANT: user message via inbox thread (short-lived)
#       - DELEGATED: worker spawned via AgentBus (replies to CEO, not user)
#       - SCHEDULED: fired by the scheduler with [SCHEDULED_TASK_ID]
#       - STANDALONE: headless CLI mission
#   • Load the appropriate context (frameworks, inbox history)
#   • Run the CEO loop via CoreLoopMixin
#   • Persist results
#
# Observability:
#   • Registers itself with TaskManager on init so the health watchdog can
#     track its heartbeat.
#   • Unregisters on completion.
#
# Recent changes:
#   1. [DELEGATED_TASK] detection — extracts ParentThread, AgentThread, etc.
#   2. Inbox history TRIMMED to last 3 messages with the current one marked.
#   3. _save_task_result() adapted for delegated tasks.
#   4. Library query EAGER-LOAD removed — the SentenceTransformer is now
#      loaded lazily only when the CEO actually uses the search_library tool.
#   5. Registers with TaskManager for heartbeat tracking.
#   6. Tool-first enforcement state: _tools_executed, _actionable_rejections.
#   7. Verification-wake state: _active_worker_report, _delegated_files —
#      populated from the inbox when the mission is a worker-finished wake.
#

import os
import re
import json
import hashlib
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from helpers import _preload_file_context
from logger import setup_logging
from ceo_state import CEOScratchpad, SharedState
from agent_spawner import AgentSpawner
from mental_framework import detect_domains, load_framework

from .database import MissionDB

# Mixins
from .mixins.helpers_mixin import HelpersMixin
from .mixins.agent_management_mixin import AgentManagementMixin
from .mixins.worker_dispatch_mixin import WorkerDispatchMixin
from .mixins.action_handlers_mixin import ActionHandlersMixin
from .mixins.core_loop_mixin import CoreLoopMixin

# Secrets manager
from .secrets_manager import SecretsManager


setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()


# ── Tools the AgentSpawner may hand out as a *baseline* ──
_BASELINE_READONLY_TOOL_KEYS = {
    "list_directory",
    "ast_inspector",
    "internet_search",
    "search_empire_library",
    "query_official_docs",
    "consult_mission_history",
}


def _extract_user_message(mission: str) -> str:
    """
    Return the user's actual message, stripped of inbox-mission boilerplate
    ("Process inbox message #N in thread X. User message: ...\nInstructions:...").
    Falls back to the full mission string if the pattern isn't present.
    """
    if not mission:
        return ""
    m = re.search(r'User message:\s*(.+?)(?:\nInstructions:|\Z)',
                  mission, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return mission.strip()


class ActiveTask(
    HelpersMixin,
    AgentManagementMixin,
    WorkerDispatchMixin,
    ActionHandlersMixin,
    CoreLoopMixin,
):
    def __init__(self, task_id, mission, agents, director_llm, task_manager=None):
        self.id = task_id
        self.mission = mission
        self.agents = agents
        self.director_llm = director_llm
        self.task_manager = task_manager
        self.status = "STARTING"

        logger.info(f"ActiveTask {task_id} initialised with mission: {mission[:100]}")

        # ── Extract the user's real intent once, reuse everywhere ──
        self.user_message = _extract_user_message(mission)

        # ── Parse mission type and extract metadata ──
        self._parse_mission_metadata(mission)

        # Preload file context (adds logs)
        self.mission, _preload_logs = _preload_file_context(mission)
        self.logs: List[str] = []
        self.result = None
        self.timestamp = datetime.now().strftime("%H:%M:%S")
        self.is_complete = False
        self.conversation_history: List[dict] = []

        for line in _preload_logs:
            self.logs.append(line)

        # ── Domain / framework detection (uses the user's message) ──
        self._active_domains = detect_domains(self.user_message or mission)
        self._active_schemas = [load_framework(d) for d in self._active_domains]
        self._framework_turn = -1

        # ── Scratchpad / plan state ──
        self.ceo_scratchpad = CEOScratchpad()
        self.strategy_attempts: Dict[str, int] = {}
        self.pivot_count = 0
        self.consecutive_timeouts: Dict[str, int] = {}
        self.scratch_dir = os.path.abspath(
            os.path.join("ai_civilization", "scratch", f"mission_{task_id}")
        )
        os.makedirs(self.scratch_dir, exist_ok=True)
        self.mission_db = MissionDB(self.scratch_dir)

        # ── CEO playbook ──
        self.ceo_playbook_path = os.path.abspath(
            os.path.join("ai_civilization", "ceo_playbook.json")
        )
        self.ceo_playbook: List[Any] = []
        if os.path.exists(self.ceo_playbook_path):
            try:
                with open(self.ceo_playbook_path, "r", encoding="utf-8") as f:
                    self.ceo_playbook = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load ceo_playbook: {e}")

        # ── Runtime / model bookkeeping ──
        self.web_intelligence = ""
        self.web_search_turn = -99
        self.last_search_query = ""
        self.compute_budget = 100.00
        self.vision_streak: List[str] = []
        self._last_files_count = 0
        self._stagnation_turns = 0
        self.stagnation_warning = ""
        self.master_plan: List[str] = []
        self.verification_script = ""
        self._verification_run = True

        # ── Async worker state (legacy in-mission workers) ──
        self._async_workers: Dict[str, Any] = {}
        self._async_dispatch_times: Dict[str, float] = {}
        self._async_results: Dict[str, Any] = {}
        self._async_full_results: Dict[str, Any] = {}
        self._async_events: List[str] = []
        self._async_fail_counts: Dict[str, int] = {}
        self._sync_fail_counts: Dict[str, int] = {}
        self._post_release_waits = 0
        self._consecutive_blocks = 0
        self._idle_turns = 0
        self._playbook_strikes: Dict[str, int] = {}
        self._clarify_count = 0
        self._json_parse_errors = 0
        self._consecutive_duplicate_blocks = 0
        self._verification_turns_allowed = 3
        self._seen_worker_output = False

        self.shared_state = SharedState()
        self.agent_memories = {agent.role: [] for agent in self.agents}
        self.global_lessons = "No relevant past lessons loaded at init. Use 'search_library' when you need institutional knowledge."

        # ── Fact-checking & evidence ledger ──
        self.evidence_ledger: Dict[str, str] = {}
        self.valid_citations: set = set()
        self._last_request_type = "conversational"
        self._last_verification_method = "none"

        # ── Anti-corruption / reflection gate ──
        self.audit_fail_count = 0
        self.max_audit_failures = 3

        # ── Inbox reply tracking ──
        self._reply_sent = False
        self._has_tool_executed = False

        # ── NEW: tool-first enforcement state ──
        # _tools_executed records the *specific* CALL_TOOL names that have
        # run. Unlike _has_tool_executed (which also flips for EXECUTE_REPL
        # and TERMINAL), this set is what the actionable-request gate checks.
        # Reconnaissance does NOT count as answering the user.
        self._tools_executed: set = set()
        self._actionable_rejections: int = 0

        # ── NEW: verification-wake state ──
        # Populated below (after _attach_inbox) when this mission is a
        # worker-finished wake from AgentBus. The assistant prompt uses
        # these to render the VERIFICATION MODE block.
        self._active_worker_report: str = ""
        self._delegated_files: list = []

        # ── Scheduler DB (for task result persistence) ──
        self.scheduler_db = None
        try:
            from orchestration.scheduler_db import SchedulerDB
            self.scheduler_db = SchedulerDB(
                os.path.join(os.getcwd(), "ai_civilization", "scheduler.db")
            )
        except Exception as e:
            logger.warning(f"Failed to initialise scheduler_db: {e}")

        # ── Inbox integration (thread + trimmed history) ──
        self.inbox_db = None
        self.inbox_thread_id = None
        self.inbox_history_text = ""
        self._attach_inbox()

        # ── NEW: detect worker-finished wake and load the worker's report ──
        # When AgentBus wakes the CEO with a delegated worker's result, the
        # mission text starts with "A worker you delegated to has finished."
        # We pull the [AGENT_REPLY] message out of the parent thread so the
        # assistant prompt can render VERIFICATION MODE.
        if "worker you delegated" in (mission or "").lower():
            try:
                if self.inbox_db and self.inbox_thread_id:
                    history = self.inbox_db.get_thread_history(
                        self.inbox_thread_id, limit=5
                    )
                    for msg in reversed(history or []):
                        body = (msg.get("body") or "")
                        if msg.get("direction") == "IN" and "AGENT_REPLY" in body:
                            self._active_worker_report = body
                            try:
                                import json as _json
                                self._delegated_files = _json.loads(
                                    msg.get("attachments") or "[]"
                                )
                            except Exception:
                                self._delegated_files = []
                            logger.info(
                                f"Verification wake: loaded worker report "
                                f"({len(body)} chars, "
                                f"{len(self._delegated_files)} files)"
                            )
                            break
            except Exception as e:
                logger.warning(f"Failed to load worker report: {e}")

        # ── Secrets manager ──
        import base64
        from cryptography.fernet import Fernet

        org_api_key = os.getenv("ORG_API_KEY")
        if org_api_key:
            key = base64.urlsafe_b64encode(
                hashlib.sha256(org_api_key.encode()).digest()
            )
        else:
            key = Fernet.generate_key()
        self.secrets_manager = SecretsManager(os.getcwd(), key.decode())

        from empire_tools import set_secrets_manager
        set_secrets_manager(self.secrets_manager)

        # ── Register with TaskManager for health tracking ──
        if self.task_manager:
            try:
                self.task_manager.register_task(
                    task_id=task_id,
                    mission=mission,
                    thread_id=self.inbox_thread_id or "console",
                )
            except Exception as e:
                logger.warning(f"Could not register task with TaskManager: {e}")

        # Persist initial history
        self.save_history_to_disk()

        # Wire step callbacks for logs
        colors = ["bold cyan", "bold magenta", "bold blue", "bold green", "bold yellow"]
        for i, agent in enumerate(self.agents):
            agent.step_callback = self.create_logger(agent.role, colors[i % len(colors)])

        # ── AgentSpawner: seed with a read-only baseline ──
        ceo_tools = self.agents[0].tools if self.agents else []
        baseline_tools = [
            t for t in ceo_tools
            if getattr(t, 'name', '').lower().replace(' ', '_')
               in _BASELINE_READONLY_TOOL_KEYS
        ]

        tenant_pool_dir = os.path.abspath(
            os.path.join("ai_civilization", "agent_pool")
        )
        self.spawner = AgentSpawner(
            director_llm=self.director_llm,
            logger=self.logs.append,
            tools=baseline_tools,
            pool_dir=tenant_pool_dir,
        )

        logger.info(
            f"ActiveTask {task_id} ready "
            f"(type={'delegated' if self.is_delegated_task else 'assistant'}, "
            f"thread={self.inbox_thread_id})"
        )

    # ------------------------------------------------------------------
    # Mission metadata parsing
    # ------------------------------------------------------------------
    def _parse_mission_metadata(self, mission: str) -> None:
        """Detect mission type and extract metadata from the mission text."""
        # Defaults
        self.is_delegated_task = False
        self.is_scheduled_task = False
        self.delegated_role: Optional[str] = None
        self.parent_thread_id: Optional[str] = None
        self.parent_message_id: Optional[int] = None
        self.agent_thread_id: Optional[str] = None
        self.agent_message_id: Optional[int] = None
        self.assigned_tools: List[str] = []
        self.scheduled_task_id: Optional[int] = None

        # ── DELEGATED TASK ──
        if "[DELEGATED_TASK]" in mission:
            self.is_delegated_task = True

            def _grab(pattern: str, cast=str, default=None):
                m = re.search(pattern, mission)
                if not m:
                    return default
                v = m.group(1).strip()
                if v in ("None", "none", ""):
                    return default
                try:
                    return cast(v)
                except (ValueError, TypeError):
                    return default

            self.parent_thread_id  = _grab(r'ParentThread:\s*(\S+)')
            self.parent_message_id = _grab(r'ParentMessageId:\s*(\d+)', int)
            self.agent_thread_id   = _grab(r'AgentThread:\s*(\S+)')
            self.agent_message_id  = _grab(r'AgentMessageId:\s*(\d+)', int)
            self.delegated_role    = _grab(r'Role:\s*(.+)')

            tools_str = _grab(r'AssignedTools:\s*(.+)')
            if tools_str:
                self.assigned_tools = [
                    t.strip() for t in tools_str.split(',') if t.strip()
                ]

            logger.info(
                f"Delegated task detected: role='{self.delegated_role}' "
                f"parent_thread='{self.parent_thread_id}' "
                f"agent_thread='{self.agent_thread_id}'"
            )

        # ── SCHEDULED TASK ──
        sched_match = re.search(r'\[SCHEDULED_TASK_ID:(\d+)\]', mission)
        if sched_match:
            self.is_scheduled_task = True
            self.scheduled_task_id = int(sched_match.group(1))
            logger.info(
                f"Scheduled task detected: id={self.scheduled_task_id}"
            )

    # ------------------------------------------------------------------
    # Inbox attachment (thread + trimmed history)
    # ------------------------------------------------------------------
    def _attach_inbox(self) -> None:
        """
        Attach to the inbox DB and load a *trimmed* history.

        For delegated tasks, prefer the agent thread (self.agent_thread_id).
        For assistant tasks, use the user thread parsed from "thread <id>".
        History is capped at 3 messages, with the current message marked so the
        CEO replies ONLY to it (not to every historical message).
        """
        # Determine which thread we belong to
        if self.is_delegated_task and self.agent_thread_id:
            self.inbox_thread_id = self.agent_thread_id
        else:
            m = re.search(r'thread\s+([A-Za-z0-9\-_]+)', self.mission, re.IGNORECASE)
            self.inbox_thread_id = m.group(1) if m else None

        if not self.inbox_thread_id:
            return

        inbox_db_path = os.path.join(os.getcwd(), "ai_civilization", "inbox.db")
        try:
            from .inbox_db import InboxDB
            self.inbox_db = InboxDB(inbox_db_path)
            logger.info(
                f"ActiveTask {self.id} attached to inbox thread "
                f"'{self.inbox_thread_id}'"
            )
        except Exception as e:
            self.logs.append(f"[dim red]InboxDB init error: {e}[/dim red]")
            logger.warning(f"InboxDB init error: {e}")
            return

        # Determine which message we're replying to
        current_msg_id: Optional[int] = None
        if self.is_delegated_task and self.agent_message_id:
            current_msg_id = self.agent_message_id
        else:
            m = re.search(r'message #(\d+)', self.mission)
            if m:
                current_msg_id = int(m.group(1))

        # Trim history to last 3 messages, mark the current one
        try:
            history = self.inbox_db.get_thread_history(
                self.inbox_thread_id, limit=3
            )
            if history:
                lines: List[str] = []
                for msg in history:
                    direction = msg.get('direction', '?')
                    body = (msg.get('body') or '')[:300]
                    marker = ""
                    # Mark the message we're replying to
                    if current_msg_id is not None and msg.get('id') == current_msg_id:
                        marker = "  ◀—— reply ONLY to this one"
                    elif current_msg_id is None and direction == "IN":
                        # Fallback: mark the most recent IN if we don't have an id
                        marker = "  ◀—— reply ONLY to this one"
                    lines.append(f"[{direction}] {body}{marker}")
                self.inbox_history_text = "\n".join(lines)
        except Exception as e:
            self.inbox_history_text = f"Error retrieving history: {e}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        logger.info(f"ActiveTask {self.id} started")
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

    # ------------------------------------------------------------------
    # Task result persistence
    # ------------------------------------------------------------------
    def _save_task_result(self) -> None:
        """
        Store the final mission result so it can be cited later via a
        `task_<id>` token in RECENT TASK OUTCOMES.

        For delegated tasks, we do NOT queue a NOTIFY_USER mission here —
        the AgentBus notifier handles that via [AGENT_REPLY] messages.
        """
        if not self.scheduler_db:
            return

        task_id = getattr(self, 'scheduled_task_id', None)
        citation_token = f"task_{task_id}" if task_id else None

        try:
            try:
                self.scheduler_db.add_task_result(
                    task_id=task_id,
                    mission=self.user_message or self.mission,
                    result=self.result or "",
                    status="COMPLETED",
                    citation_token=citation_token,
                )
            except TypeError:
                # Older SchedulerDB signature without citation_token
                self.scheduler_db.add_task_result(
                    task_id=task_id,
                    mission=self.user_message or self.mission,
                    result=self.result or "",
                    status="COMPLETED",
                )
            logger.info(f"Task result saved for mission {self.id}")
        except Exception as e:
            logger.warning(f"Failed to save task result: {e}")

    def _mark_scheduled_task_failed(self, reason: str) -> None:
        """Mark the scheduled task as failed (used for retry logic)."""
        if not self.scheduler_db or not getattr(self, 'scheduled_task_id', None):
            return
        try:
            self.scheduler_db.mark_task_failed(self.scheduled_task_id, reason)
            logger.info(
                f"Scheduled task {self.scheduled_task_id} marked failed: {reason}"
            )
        except Exception as e:
            logger.warning(f"Failed to mark scheduled task failed: {e}")

    # ------------------------------------------------------------------
    # Health — unregister on completion
    # ------------------------------------------------------------------
    def mark_finished(self) -> None:
        """
        Called by the core loop (and by cancel_task) when the mission is
        truly done. Removes the task from TaskManager's health map so the
        watchdog stops caring about it.
        """
        if not self.task_manager:
            return
        try:
            self.task_manager.unregister_task(self.id)
        except Exception as e:
            logger.warning(f"Could not unregister task {self.id}: {e}")
