################################

# orchestration/agent_bus.py
"""
AgentBus — multi-agent delegation over the inbox DB.

Each agent has its own thread (agent_<role_slug>). Delegation writes an IN
message there and spawns a new ActiveTask whose mission is tagged
[DELEGATED_TASK]. The agent's SEND_REPLY routes back to the CEO via
agent_reply_to_ceo(), which writes an [AGENT_REPLY] IN to the parent
(user) thread. A background notifier picks these up and wakes a fresh
CEO mission to deliver the result.
"""

import os
import re
import json
import time
import logging
import threading
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class AgentBus:
    def __init__(self, inbox_db_path: str, workspace_root: str, task_manager):
        # Late import so this module can be imported before inbox_db is set up
        from orchestration.inbox_db import InboxDB
        self.inbox_db = InboxDB(inbox_db_path)
        self.workspace_root = workspace_root
        self.task_manager = task_manager
        self._notifier_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ──────────────────────────────────────────────────────────────────────
    # Thread naming
    # ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def agent_thread_id(role: str) -> str:
        slug = re.sub(r'[^a-z0-9_]', '_', role.lower().strip())[:30].strip('_')
        return f"agent_{slug}"

    # ──────────────────────────────────────────────────────────────────────
    # Delegation: CEO → agent
    # ──────────────────────────────────────────────────────────────────────
    def delegate(
        self,
        role: str,
        instruction: str,
        assigned_tools: list,
        parent_thread: str,
        parent_message_id: Optional[int] = None,
    ) -> dict:
        """Write to agent's inbox and spawn a new ActiveTask."""
        if not self.task_manager:
            raise RuntimeError("AgentBus.delegate requires a task_manager")

        thread_id = self.agent_thread_id(role)

        # 1. IN message on the agent's own thread
        msg_id = self.inbox_db.add_message(
            thread_id=thread_id,
            direction="IN",
            body=instruction,
            sender="CEO",
            recipient=role,
            status="NEW",
        )

        # 2. Mission text carries everything the agent needs to reply back.
        mission = (
            f"[DELEGATED_TASK]\n"
            f"ParentThread: {parent_thread}\n"
            f"ParentMessageId: {parent_message_id if parent_message_id is not None else 'None'}\n"
            f"AgentThread: {thread_id}\n"
            f"AgentMessageId: {msg_id}\n"
            f"Role: {role}\n"
            f"AssignedTools: {','.join(assigned_tools) if assigned_tools else 'file_manager'}\n"
            f"Instruction: {instruction}\n"
        )

        # 3. Spawn a new concurrent ActiveTask
        task_id = self.task_manager.spawn_delegated_task(mission)
        logger.info(
            f"Delegated role='{role}' to thread='{thread_id}' "
            f"message=#{msg_id} task='{task_id}'"
        )

        return {"thread_id": thread_id, "message_id": msg_id, "task_id": task_id}

    # ──────────────────────────────────────────────────────────────────────
    # Reply: agent → CEO
    # ──────────────────────────────────────────────────────────────────────
    def agent_reply_to_ceo(
        self,
        agent_thread: str,
        parent_thread: str,
        parent_message_id: Optional[int],
        result: str,
        files: Optional[list] = None,
    ) -> None:
        """Post the agent's result and notify the CEO's thread."""
        agent_name = agent_thread.replace("agent_", "")

        # OUT to agent's own thread — this is its audit trail.
        self.inbox_db.add_message(
            thread_id=agent_thread,
            direction="OUT",
            body=result,
            sender=agent_name,
            recipient="CEO",
            status="DELIVERED",
        )

        # IN to CEO's thread — triggers a fresh assistant mission.
        payload = {
            "agent": agent_name,
            "parent_message_id": parent_message_id,
            "result": result[:4000],
            "files": files or [],
        }
        notification = "[AGENT_REPLY]\n" + json.dumps(payload, ensure_ascii=False)

        self.inbox_db.add_message(
            thread_id=parent_thread,
            direction="IN",
            body=notification,
            sender=agent_name,
            recipient="CEO",
            status="NEW",
        )
        logger.info(
            f"Agent '{agent_name}' replied to {parent_thread} "
            f"(result {len(result)} chars)"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Notifier: watch for [AGENT_REPLY] on user threads
    # ──────────────────────────────────────────────────────────────────────
    def start_notifier(
        self,
        watch_threads: list,
        wake_callback: Callable[[str, int], None],
    ) -> None:
        """
        Background loop. For each new IN on a watched thread that starts
        with [AGENT_REPLY], call wake_callback(thread_id, message_id) so the
        caller can spin a fresh CEO mission.
        """
        def _run():
            # Seed last_seen with the current tail so we don't replay history.
            last_seen = {}
            for t in watch_threads:
                try:
                    msgs = self.inbox_db.get_thread_history(t, limit=1)
                    last_seen[t] = msgs[-1]["id"] if msgs else 0
                except Exception:
                    last_seen[t] = 0

            while not self._stop.is_set():
                try:
                    for thread in watch_threads:
                        msgs = self.inbox_db.get_thread_history(thread, limit=30)
                        for m in msgs:
                            mid = m.get("id", 0)
                            if mid <= last_seen.get(thread, 0):
                                continue
                            last_seen[thread] = mid
                            body = m.get("body", "") or ""
                            direction = m.get("direction")
                            if direction == "IN" and body.startswith("[AGENT_REPLY]"):
                                logger.info(
                                    f"Notifier: [AGENT_REPLY] detected on "
                                    f"'{thread}' msg #{mid}"
                                )
                                try:
                                    wake_callback(thread, mid)
                                except Exception as e:
                                    logger.error(f"wake_callback failed: {e}")
                except Exception as e:
                    logger.error(f"Notifier loop error: {e}")
                time.sleep(2)

        self._notifier_thread = threading.Thread(target=_run, daemon=True)
        self._notifier_thread.start()
        logger.info(f"AgentBus notifier started on threads: {watch_threads}")

    def stop(self) -> None:
        self._stop.set()
