#
import threading, time, subprocess
from datetime import datetime
from .scheduler_db import SchedulerDB

MAX_CONCURRENT_MISSIONS = 3

class TaskScheduler:
    def __init__(self, db_path: str, thread_id: str = "scheduler"):
        self.db = SchedulerDB(db_path)
        self.thread_id = thread_id
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            now = datetime.now().isoformat()
            running_count = self._count_running_missions()
            while running_count < MAX_CONCURRENT_MISSIONS:
                task = self.db.claim_next_task(now)
                if not task:
                    break
                try:
                    if task.get('task_type') == 'SCRIPT':
                        self._execute_script_task(task)
                    elif task.get('task_type') == 'AGENT_WAKE':
                        self._execute_agent_wake(task)
                    elif task.get('task_type') == 'NOTIFY_USER':
                        self._execute_notify_user(task)
                    else:
                        self._trigger_ceo_wake(task)
                    running_count += 1
                except Exception as e:
                    print(f"❌ Scheduler error for task #{task['id']}: {e}", flush=True)
                    self.db.mark_task_failed(task['id'], str(e))
            time.sleep(5)

    def _count_running_missions(self) -> int:
        try:
            from gm import manager
            return sum(1 for t in manager.list_tasks() if t.status == "RUNNING")
        except Exception:
            return 0

    # ---------- Script Task ----------
    def _execute_script_task(self, task):
        self.db.mark_task_in_progress(task['id'])
        script_code = task.get('script_code')
        script_path = task.get('script_path')
        result = None
        try:
            if script_code:
                from gm import run_repl_code
                result = run_repl_code(script_code)
            elif script_path:
                result = subprocess.getoutput(f"python {script_path}")
            else:
                result = "Error: No script_code or script_path provided."
            if "Error" in result or "Traceback" in result:
                raise Exception(result)
            self.db.mark_task_completed(task['id'])
            print(f"✅ Script task #{task['id']} completed.", flush=True)
        except Exception as e:
            self.db.mark_task_failed(task['id'], str(e))
            print(f"❌ Script task #{task['id']} failed: {str(e)[:200]}", flush=True)

    # ---------- CEO Wake ----------
    def _trigger_ceo_wake(self, task):
        self.db.mark_task_in_progress(task['id'])
        mission_text = (
            f"[SCHEDULED_TASK_ID:{task['id']}]\n"
            f"Scheduled task triggered:\n"
            f"Title: {task['title']}\n"
            f"Description: {task.get('description','')}\n"
            f"Please handle this task now. You may delegate it or complete it yourself."
        )
        try:
            from gm import start_chat_mission
            start_chat_mission(
                mission_text,
                thread_id=task.get('thread_id') or self.thread_id
            )
            print(f"✅ CEO wake task #{task['id']} triggered.", flush=True)
        except Exception as e:
            print(f"❌ Failed to start mission for task #{task['id']}: {e}", flush=True)
            self.db.mark_task_failed(task['id'], str(e))

    # ---------- Agent Wake ----------
    def _execute_agent_wake(self, task):
        self.db.mark_task_in_progress(task['id'])
        agent_role = task.get('agent_role', '')
        instruction = task.get('agent_instruction', task.get('description', ''))
        mission_text = (
            f"[SCHEDULED_TASK_ID:{task['id']}]\n"
            f"AGENT_WAKE task. You must delegate this entire task to the {agent_role} agent.\n"
            f"Task: {instruction}\n"
            f"After delegation, immediately call FINISH. Do not perform any other actions."
        )
        try:
            from gm import start_chat_mission
            start_chat_mission(
                mission_text,
                thread_id=task.get('thread_id') or self.thread_id
            )
            print(f"✅ Agent wake task #{task['id']} triggered for {agent_role}.", flush=True)
        except Exception as e:
            print(f"❌ Failed to start agent wake mission #{task['id']}: {e}", flush=True)
            self.db.mark_task_failed(task['id'], str(e))

    # ---------- Notify User ----------
    def _execute_notify_user(self, task):
        """Starts a CEO mission to reply to the user using the completed task's result."""
        self.db.mark_task_in_progress(task['id'])
        mission_text = (
            f"[SCHEDULED_TASK_ID:{task['id']}]\n"
            f"NOTIFY_USER task. You need to reply to the user about the completed task.\n"
            f"Task description: {task.get('description','')}\n"
            f"Use the RECENT TASK OUTCOMES section to get the result and answer accurately.\n"
            f"Send a natural, helpful reply via SEND_REPLY."
        )
        try:
            from gm import start_chat_mission
            start_chat_mission(
                mission_text,
                thread_id=task.get('thread_id') or self.thread_id
            )
            print(f"✅ Notify user task #{task['id']} triggered.", flush=True)
        except Exception as e:
            print(f"❌ Failed to start notify mission #{task['id']}: {e}", flush=True)
            self.db.mark_task_failed(task['id'], str(e))
