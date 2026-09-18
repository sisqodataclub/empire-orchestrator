# chat_handler.py

# chat_handler.py
import os, json, re
from orchestration.inbox_db import InboxDB
from orchestration.scheduler_db import SchedulerDB

class ChatHandler:
    def __init__(self, director_llm, manager, inbox_db, thread_id, scheduler_db=None):
        self.director_llm = director_llm
        self.manager = manager
        self.inbox_db = inbox_db
        self.thread_id = thread_id
        if scheduler_db is None:
            db_path = os.path.join(os.getcwd(), "ai_civilization", "scheduler.db")
            self.scheduler_db = SchedulerDB(db_path)
        else:
            self.scheduler_db = scheduler_db

    def process_message(self, user_message: str) -> str:
        # Record incoming message
        msg_id = self.inbox_db.add_message(
            thread_id=self.thread_id,
            direction="IN",
            body=user_message,
            sender="user",
            recipient="CEO",
            status="NEW"
        )

        # Quick deterministic check for workspace listing requests
        if any(kw in user_message.lower() for kw in ["what folders", "list folders", "show folders", "what files", "list files", "show files", "what's in", "what is in"]):
            return self._handle_workspace_query(user_message)

        # Build context for the router (last 4 messages)
        history = self.inbox_db.get_thread_history(self.thread_id, limit=4)
        history_text = "\n".join(
            f"[{'CEO' if msg['direction']=='OUT' else 'User'}] {msg['body']}"
            for msg in history
        )

        # Improved triage prompt
        classification_prompt = f"""
You are the Chief of Staff routing an incoming message.
You have FULL access to the user's workspace directory and filesystem. You can use tools like `list_directory` to view folders and files.
User Message: "{user_message}"
Recent Context: {history_text}

Classify the required action into ONE of these categories:
- CASUAL_CHAT: Only greetings, thanks, or simple conversational replies that do NOT request information about the workspace, files, folders, code, or data.
- ACTION_REQUIRED: Any request that requires reading the filesystem, listing folders/files, fetching data, writing code, analyzing files, or performing an action.

Examples:
"Hi" → CASUAL_CHAT
"What folders are there?" → ACTION_REQUIRED (needs list_directory)
"List my files" → ACTION_REQUIRED
"Create a file called test.txt" → ACTION_REQUIRED

Output strictly valid JSON:
{{"category": "CASUAL_CHAT or ACTION_REQUIRED", "quick_reply": "A brief natural response acknowledging the user."}}
"""
        try:
            raw = self.director_llm.call(messages=[{"role": "user", "content": classification_prompt}]).strip()
            raw_clean = re.sub(r'```(?:json)?\s*', '', raw).strip("`")
            decision = json.loads(raw_clean)
        except Exception:
            decision = {"category": "ACTION_REQUIRED", "quick_reply": "I've received your request. Spinning up a background workspace to handle this now."}

        if decision.get("category") == "CASUAL_CHAT":
            reply_text = decision.get("quick_reply", "Understood.")
            self._send_reply(reply_text)
            return reply_text
        else:
            ack_text = decision.get("quick_reply", "I am initializing a background mission to handle this.")
            self._send_reply(ack_text)

            # Queue background mission
            mission_objective = f"Process inbox message #{msg_id} in thread {self.thread_id}. User Request: {user_message}"
            self.scheduler_db.add_task(
                title=f"Inbox Request #{msg_id}",
                due_at=None,
                recurrence=None,
                project_id=None,
                depends_on_task_id=None,
                description=mission_objective,
                task_type="CEO_WAKE"
            )
            return ack_text

    def _handle_workspace_query(self, user_message: str) -> str:
        """Directly list the workspace directory and reply with the results."""
        try:
            import gm
            tool = gm.TOOL_REGISTRY.get("list_directory")
            if tool:
                if hasattr(tool, 'run'):
                    result = tool.run(path=os.getcwd())
                elif hasattr(tool, 'func'):
                    result = tool.func(path=os.getcwd())
                else:
                    result = tool(path=os.getcwd())
            else:
                # Fallback: use os.listdir
                result = "\n".join(os.listdir(os.getcwd()))
        except Exception as e:
            result = f"Could not list directory: {e}"

        reply_prompt = f"""
The user asked: "{user_message}"

Here is the directory listing for the workspace (current directory {os.getcwd()}):
{result}

Create a concise, friendly reply that lists the folders. If the result is empty, say the workspace is empty. Do not mention that you used a tool; just provide the answer.
"""
        reply = self.director_llm.call(messages=[{"role": "user", "content": reply_prompt}]).strip()
        self._send_reply(reply)
        return reply

    def _send_reply(self, text: str):
        self.inbox_db.add_message(
            thread_id=self.thread_id,
            direction="OUT",
            body=text,
            sender="CEO",
            recipient="user",
            status="PENDING_DELIVERY"
        )
