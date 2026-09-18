############
###############

#!/usr/bin/env python3
"""
Single-organisation Telegram bot worker.

Architecture:
  • Every user message becomes an IN row in the inbox DB, then a fresh
    ActiveTask (assistant mission) is spawned via gm.start_chat_mission().
  • The CEO replies via SEND_REPLY; the poller below sends those OUT messages
    to the Telegram chat.
  • When the CEO delegates to an agent, AgentBus spawns a concurrent
    ActiveTask. When that agent finishes, it writes [AGENT_REPLY] back to
    THIS thread (THREAD_ID). The AgentBus notifier picks it up and wakes a
    fresh assistant mission so the CEO can deliver the result to the user.

Observability:
  • TaskManager keeps a live health map (heartbeat, current step) per task.
  • A watchdog fires a "mission appears stuck" reply if a task's heartbeat
    goes silent for 90+ seconds.
  • The CEO has three in-memory tools (system_status, inspect_task,
    cancel_mission) and full EXECUTE_REPL / EXECUTE_TERMINAL access to
    investigate the whole ecosystem on demand.

Recent changes:
  • AgentBus notifier wired to THREAD_ID (delegated work now auto-reports).
  • OUT-message poller seeds last_out_id from the highest existing message
    on boot — prevents replaying chat history and hitting 429.
  • 429-aware send helper honours `retry_after` and gives up cleanly.
  • Observability context wired + watchdog started.
  • HTTP/model-download log spam silenced.
"""

import os
import re
import sys
import time
import threading
import logging

import telebot
from telebot.apihelper import ApiTelegramException
from dotenv import load_dotenv

load_dotenv()

ORG_ID = os.environ.get("ORG_ID")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ALLOWED_USER_IDS_STR = os.environ.get("ALLOWED_USER_IDS", "")

if not ORG_ID or not BOT_TOKEN or not ALLOWED_USER_IDS_STR:
    print("❌ Missing required environment variables (ORG_ID, BOT_TOKEN, ALLOWED_USER_IDS).")
    sys.exit(1)

ALLOWED_USER_IDS = [
    int(x.strip()) for x in ALLOWED_USER_IDS_STR.split(",") if x.strip()
]

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/app/data/workspaces")
ORG_WORKSPACE = os.path.join(WORKSPACE_ROOT, f"org_{ORG_ID}")
os.makedirs(ORG_WORKSPACE, exist_ok=True)
os.chdir(ORG_WORKSPACE)

# ── Per-org logging ────────────────────────────────────────────────────────
from logger import setup_logging
setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

# Silence noisy HTTP/model-download logs from Hugging Face and httpx.
# Without this, the mission-start path emits hundreds of DEBUG lines.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("filelock").setLevel(logging.WARNING)

# ── Engine imports (must be AFTER chdir) ──────────────────────────────────
import gm
from gm import manager
from orchestration.inbox_db import InboxDB

# Start scheduler
gm.scheduler.start()
logger.info(f"📅 Scheduler started for org {ORG_ID}.")

# Inbox DB path (needed by observability wiring below)
INBOX_DB_PATH = os.path.join(os.getcwd(), "ai_civilization", "inbox.db")
inbox_db = InboxDB(INBOX_DB_PATH)

# ── Wire system-observability context for this org ──
# This is what lets the CEO answer "what's happening?" in Telegram by
# reading the live TaskManager + this org's inbox/scheduler/logs.
from tools.system_observability_tools import set_observability_context

set_observability_context(
    task_manager=gm.manager,
    log_path=os.path.join(os.getcwd(), "logs", "empire.log"),
    inbox_db_path=INBOX_DB_PATH,
    scheduler_db_path=os.path.join(
        os.getcwd(), "ai_civilization", "scheduler.db"
    ),
)
logger.info("🔭 Observability context wired.")

# ── Start the health watchdog ──
# Fires a "mission appears stuck" reply on the user's thread if any task's
# heartbeat goes silent for 90+ seconds. Runs in a daemon thread.
gm.manager.start_watchdog()
logger.info("💓 Task health watchdog started.")

# Telegram bot
bot = telebot.TeleBot(BOT_TOKEN)
THREAD_ID = f"tg_{ORG_ID}"


# ══════════════════════════════════════════════════════════════════════════
# Send helpers — 429-aware
# ══════════════════════════════════════════════════════════════════════════
def _send_with_backoff(chat_id: int, text: str, max_attempts: int = 3) -> bool:
    """
    Send a Telegram message, honouring the server's `retry_after` on 429.

    Returns True if delivered, False if the send gave up after `max_attempts`.
    Caps the wait at 30 s so we don't sleep for minutes on a nasty backoff.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            bot.send_message(chat_id, text)
            return True
        except ApiTelegramException as e:
            if e.error_code == 429:
                m = re.search(r'retry after (\d+)', e.description or "")
                wait = int(m.group(1)) if m else 5
                wait = min(wait, 30)
                logger.warning(
                    f"Telegram 429 (attempt {attempt}/{max_attempts}), "
                    f"sleeping {wait}s"
                )
                time.sleep(wait + 1)
                continue
            logger.error(f"Telegram API error: {e}")
            return False
        except Exception as e:
            logger.error(f"Send error: {e}")
            return False
    logger.error(f"Giving up on send after {max_attempts} attempts")
    return False


def _chunk_text(text: str, size: int):
    """Yield `text` in chunks of at most `size` chars, splitting on newlines."""
    if len(text) <= size:
        yield text
        return
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > size:
            if buf:
                yield buf
            buf = line
        else:
            buf += line
    if buf:
        yield buf


# ══════════════════════════════════════════════════════════════════════════
# OUT-message poller (CEO → user)
# ══════════════════════════════════════════════════════════════════════════
def poll_and_send_out_messages() -> None:
    """Background poller that sends new OUT messages to the user via Telegram."""
    # ── Seed last_out_id from the highest existing message ────────────────
    # This is critical: on restart we MUST NOT replay the entire chat
    # history, otherwise we hit Telegram's 429 rate limit immediately.
    last_out_id = 0
    try:
        history = inbox_db.get_thread_history(THREAD_ID, limit=2000)
        if history:
            last_out_id = max(int(m.get("id", 0)) for m in history)
            logger.info(
                f"OUT poller seeded at last_out_id={last_out_id} "
                f"({len(history)} historical messages skipped)"
            )
    except Exception as e:
        logger.warning(f"Could not seed last_out_id, starting from 0: {e}")

    # Main poll loop
    while True:
        try:
            out_msgs = inbox_db.get_out_messages_since(THREAD_ID, last_out_id)
            for msg in out_msgs:
                chat_id = getattr(poll_and_send_out_messages, "last_chat_id", None)
                if not chat_id:
                    # No chat_id yet — advance past this message so we don't
                    # re-process it every second. When the user sends their
                    # first message, chat_id will be set and future OUT
                    # messages will deliver normally.
                    logger.debug(
                        f"No chat_id yet; skipping OUT #{msg['id']} permanently."
                    )
                    last_out_id = msg["id"]
                    continue

                body = msg.get("body", "") or ""
                delivered = True
                for chunk in _chunk_text(body, 4000):
                    if not _send_with_backoff(chat_id, chunk):
                        delivered = False
                        break

                if delivered:
                    try:
                        inbox_db.mark_out_delivered(msg["id"])
                    except Exception as e:
                        logger.warning(f"mark_out_delivered failed: {e}")
                    logger.info(f"✅ Sent OUT message #{msg['id']} to chat {chat_id}")
                else:
                    # Do NOT spin on this message. Advance past it so the
                    # poller continues. The user simply misses this reply.
                    logger.error(
                        f"❌ Giving up on OUT message #{msg['id']} "
                        f"(chat {chat_id}); advancing."
                    )

                # Advance in both cases.
                last_out_id = msg["id"]

        except Exception as e:
            logger.error(f"Poller error: {e}")

        time.sleep(1)


poll_and_send_out_messages.last_chat_id = None  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════════
# AgentBus notifier — wakes the CEO when a delegated worker finishes
# ══════════════════════════════════════════════════════════════════════════
def _start_agent_bus_notifier() -> None:
    try:
        from orchestration.agent_bus import AgentBus
    except Exception as e:
        logger.error(f"AgentBus import failed, delegation reports disabled: {e}")
        return

    def _wake_ceo_for_agent_reply(thread_id: str, message_id: int) -> None:
        logger.info(
            f"AgentBus: [AGENT_REPLY] on '{thread_id}' msg #{message_id} — "
            f"waking CEO to deliver result."
        )
        try:
            gm.start_chat_mission(
                raw_mission=(
                    f"An agent has finished work you delegated. "
                    f"Read inbox message #{message_id} in thread '{thread_id}', "
                    f"then reply to the user with the result."
                ),
                priority="high",
                thread_id=thread_id,
            )
        except Exception as e:
            logger.error(f"Failed to wake CEO for agent reply: {e}")

    try:
        agent_bus = AgentBus(
            inbox_db_path=INBOX_DB_PATH,
            workspace_root=os.getcwd(),
            task_manager=manager,
        )
        agent_bus.start_notifier(
            watch_threads=[THREAD_ID],
            wake_callback=_wake_ceo_for_agent_reply,
        )
        logger.info(f"📡 AgentBus notifier started for thread {THREAD_ID}")
    except Exception as e:
        logger.error(f"Failed to start AgentBus notifier: {e}")


# ══════════════════════════════════════════════════════════════════════════
# Telegram handlers
# ══════════════════════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def handle_start(message):
    if message.from_user.id not in ALLOWED_USER_IDS:
        bot.reply_to(message, "⛔ ACCESS DENIED.")
        return
    bot.reply_to(
        message,
        "⚔️ *EMPIRE CONTROL ACTIVE*\nSend me a message, and the CEO will reply.",
        parse_mode="Markdown",
    )
    logger.info(f"Start command from user {message.from_user.id}")


@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if message.from_user.id not in ALLOWED_USER_IDS:
        bot.reply_to(message, "⛔ ACCESS DENIED.")
        return

    chat_id = message.chat.id
    text = (message.text or "").strip()
    user_id = message.from_user.id

    # Update poller's chat_id
    poll_and_send_out_messages.last_chat_id = chat_id  # type: ignore[attr-defined]

    if not text:
        return

    logger.info(f"Received message from user {user_id}: {text[:100]}")

    try:
        # Simple greeting: instant reply, no mission
        if gm.is_greeting(text):
            reply_text = "Hello! How can I assist you today?"
            gm.send_instant_reply(THREAD_ID, reply_text)
            logger.info(f"Sent instant greeting reply to user {user_id}")
            return

        # Otherwise start a CEO mission for this user's thread
        task_id = gm.start_chat_mission(text, thread_id=THREAD_ID)
        logger.info(
            f"Started CEO mission {task_id} for user {user_id} "
            f"(text={text[:60]})"
        )
    except Exception as e:
        logger.exception(f"Error processing message from user {user_id}")
        try:
            bot.reply_to(
                message,
                "❌ An error occurred while processing your request. "
                "Please try again.",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# Boot
# ══════════════════════════════════════════════════════════════════════════
def _boot_background_threads() -> None:
    """Start the poller and the AgentBus notifier."""
    # OUT-message poller (CEO → Telegram)
    threading.Thread(target=poll_and_send_out_messages, daemon=True).start()
    logger.info("📤 OUT-message poller started.")

    # AgentBus notifier ([AGENT_REPLY] → CEO wake)
    _start_agent_bus_notifier()


if __name__ == "__main__":
    _boot_background_threads()
    logger.info(f"✅ Bot worker started for org {ORG_ID}")
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down worker.")
    except Exception as e:
        logger.exception(f"Fatal bot polling error: {e}")
