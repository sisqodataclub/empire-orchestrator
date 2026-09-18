#
import threading, json, os, re, sqlite3, subprocess, hashlib, time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from llm import NativeLLM
from helpers import _preload_file_context, _build_post_execution_report
from logger import setup_logging
import logging
from ceo_state import CEOScratchpad, SharedState
from ceo_prompter import build_ceo_prompt
from worker_dispatcher import dispatch_sync_workers, check_async_workers
from agent_spawner import AgentSpawner
from cognitive_wrapper import (
    cognitive_agent_wrapper, classify_task, report_agent_wrapper,
    _strip_verification_from_instruction,
)
from mental_framework import (
    detect_domains, render_framework_block, build_worker_brief,
    query_on_blocker, load_framework,
)
from framework_writer import (
    record_observation, record_failure, record_structural,
    evolve_framework, build_from_research,
)
from empire_tools import library_collection, logs_collection, pure_duckduckgo_scrape, ast_inspector, EmpireTools

from .database import MissionDB
from .role_tools import TOOL_REGISTRY, get_tools_for_role

# Import mixins
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


class ActiveTask(
    HelpersMixin,
    AgentManagementMixin,
    WorkerDispatchMixin,
    ActionHandlersMixin,
    CoreLoopMixin
):
    def __init__(self, task_id, mission, agents, director_llm, task_manager=None):
        self.id = task_id
        self.mission = mission
        self.agents = agents
        self.director_llm = director_llm
        self.task_manager = task_manager
        self.status = "STARTING"

        self.mission, _preload_logs = _preload_file_context(mission)
        self.logs = []
        self.result = None
        self.timestamp = datetime.now().strftime("%H:%M:%S")
        self.is_complete = False
        self.conversation_history = []

        for line in _preload_logs:
            self.logs.append(line)

        self._active_domains = detect_domains(mission)
        self._active_schemas = [load_framework(d) for d in self._active_domains]
        self._framework_turn = -1
        self.ceo_scratchpad = CEOScratchpad()
        self.strategy_attempts = {}
        self.pivot_count = 0
        self.consecutive_timeouts = {}
        self.scratch_dir = os.path.abspath(
            os.path.join("ai_civilization", "scratch", f"mission_{task_id}")
        )
        os.makedirs(self.scratch_dir, exist_ok=True)
        self.mission_db = MissionDB(self.scratch_dir)
        self.ceo_playbook_path = os.path.abspath(os.path.join("ai_civilization", "ceo_playbook.json"))
        self.ceo_playbook = []
        if os.path.exists(self.ceo_playbook_path):
            try:
                with open(self.ceo_playbook_path, "r", encoding="utf-8") as f:
                    self.ceo_playbook = json.load(f)
            except Exception:
                pass
        self.web_intelligence = ""
        self.web_search_turn = -99
        self.last_search_query = ""
        self.compute_budget = 100.00
        self.vision_streak = []
        self._last_files_count = 0
        self._stagnation_turns = 0
        self.stagnation_warning = ""
        self.master_plan = []
        self.verification_script = ""
        self._verification_run = True
        self._async_workers = {}
        self._async_dispatch_times = {}
        self._async_results = {}
        self._async_full_results = {}
        self._async_events = []
        self._async_fail_counts = {}
        self._sync_fail_counts = {}
        self._post_release_waits = 0
        self._consecutive_blocks = 0
        self._idle_turns = 0
        self._playbook_strikes = {}
        self._clarify_count = 0
        self._json_parse_errors = 0
        self._consecutive_duplicate_blocks = 0
        self._verification_turns_allowed = 3
        self._seen_worker_output = False
        self.shared_state = SharedState()
        self.agent_memories = {agent.role: [] for agent in self.agents}
        self.global_lessons = "No relevant past lessons found."

        # ---------- FACT‑CHECKING & EVIDENCE LEDGER ----------
        self.evidence_ledger = {}          # maps evidence_id -> output
        self._last_request_type = "conversational"
        self._last_verification_method = "none"
        # ------------------------------------------------------

        # ---------- ANTI‑CORRUPTION / REFLECTION GATE ----------
        self.audit_fail_count = 0
        self.max_audit_failures = 3
        # ------------------------------------------------------

        try:
            res = library_collection.query(
                query_texts=[self.mission],
                n_results=5,
                include=["documents", "distances", "metadatas"]
            )
            if res['documents'] and res['documents'][0]:
                relevant_docs = []
                for doc, dist, meta in zip(
                    res['documents'][0], res['distances'][0], res['metadatas'][0]
                ):
                    if (dist < 0.55
                            and meta.get('type') not in ('intelligence_report', 'MASTER_DOC', 'documentation')
                            and meta.get('trust_score', 1.0) >= 0.4
                            and not meta.get('concept', '').startswith('Auto-Report')):
                        relevant_docs.append(f"• {doc[:300]}")
                    if len(relevant_docs) >= 2:
                        break
                if relevant_docs:
                    self.global_lessons = "\n".join(relevant_docs)
        except Exception as e:
            self.global_lessons = f"Library Access Error: {e}"

        # ---------- INBOX INTEGRATION (chat/email style) ----------
        self.inbox_db = None
        self.inbox_thread_id = None
        self.inbox_history_text = ""
        # Try to extract thread_id from mission text, e.g. "Process inbox thread <thread_id>"
        match = re.search(r'thread\s+([A-Za-z0-9\-_]+)', self.mission, re.IGNORECASE)
        if match:
            self.inbox_thread_id = match.group(1)
            # Initialize InboxDB using a shared file in tenant workspace root
            inbox_db_path = os.path.join(os.getcwd(), "ai_civilization", "inbox.db")
            try:
                from .inbox_db import InboxDB  # relative import
                self.inbox_db = InboxDB(inbox_db_path)
            except Exception as e:
                self.logs.append(f"[dim red]InboxDB init error: {e}[/dim red]")
            # Fetch thread history for CEO context
            if self.inbox_db:
                try:
                    history = self.inbox_db.get_thread_history(self.inbox_thread_id, limit=20)
                    if history:
                        lines = []
                        for msg in history:
                            direction = msg.get('direction', '?')
                            sender = msg.get('sender', 'Unknown')
                            body = msg.get('body', '')
                            lines.append(f"[{direction}] {sender}: {body}")
                        self.inbox_history_text = "\n".join(lines)
                except Exception as e:
                    self.inbox_history_text = f"Error retrieving history: {e}"
        # ----------------------------------------------------------

        # ---------- SCHEDULED TASK INTEGRATION ----------
        self.scheduled_task_id = None
        sched_match = re.search(r'\[SCHEDULED_TASK_ID:(\d+)\]', self.mission)
        if sched_match:
            self.scheduled_task_id = int(sched_match.group(1))
        # -------------------------------------------------

        # ---------- SECRETS MANAGER INITIALIZATION ----------
        # Derive encryption key from org API key (or use dedicated env)
        import base64
        from cryptography.fernet import Fernet

        org_api_key = os.getenv("ORG_API_KEY")
        if org_api_key:
            key = base64.urlsafe_b64encode(hashlib.sha256(org_api_key.encode()).digest())
        else:
            key = Fernet.generate_key()
        self.secrets_manager = SecretsManager(os.getcwd(), key.decode())
        # Set global for tool access
        from empire_tools import set_secrets_manager
        set_secrets_manager(self.secrets_manager)
        # -----------------------------------------------------

        self.save_history_to_disk()

        colors = ["bold cyan", "bold magenta", "bold blue", "bold green", "bold yellow"]
        for i, agent in enumerate(self.agents):
            agent.step_callback = self.create_logger(agent.role, colors[i % len(colors)])
        tenant_pool_dir = os.path.abspath(os.path.join("ai_civilization", "agent_pool"))

        self.spawner = AgentSpawner(
            director_llm=self.director_llm,
            logger=self.logs.append,
            tools=self.agents[0].tools if self.agents else [],
            pool_dir=tenant_pool_dir
        )

    def start(self):
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()
