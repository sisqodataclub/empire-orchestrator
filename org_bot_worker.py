############################################################################
#!/usr/bin/env python3
"""
Single-organisation Telegram bot worker.

Architecture:
  • User messages are queued into the CEO's inbox via gm.start_chat_mission().
  • The CEO's dispatcher (started by gm on import) picks them up, runs a
    turn, and writes its reply to the user thread: user_<THREAD_ID>.
  • The poller below drains user_<THREAD_ID> for rows where sender='ceo'
    and delivers them to the Telegram chat.
  • Delegation is a tool call: the CEO writes to a worker's inbox. The
    worker's dispatcher picks it up, runs its own turn, and replies to the
    CEO's inbox. The CEO's dispatcher picks that up and writes the final
    summary to the user thread. No notifier needed — the inbox IS the bus.

Env:
  ORG_ID             — organisation identifier
  BOT_TOKEN          — Telegram bot token
  ALLOWED_USER_IDS   — comma-separated Telegram user IDs
  WORKSPACE_ROOT     — parent dir; each org runs in <WORKSPACE_ROOT>/org_<id>
"""

import logging
import os
import re
import sys
import threading
import time

import telebot
from telebot.apihelper import ApiTelegramException
from dotenv import load_dotenv

load_dotenv()

ORG_ID               = os.environ.get("ORG_ID")
BOT_TOKEN            = os.environ.get("BOT_TOKEN")
ALLOWED_USER_IDS_STR = os.environ.get("ALLOWED_USER_IDS", "")

if not ORG_ID or not BOT_TOKEN or not ALLOWED_USER_IDS_STR:
    print("❌ Missing required env vars: ORG_ID, BOT_TOKEN, ALLOWED_USER_IDS.")
    sys.exit(1)

ALLOWED_USER_IDS = [
    int(x.strip()) for x in ALLOWED_USER_IDS_STR.split(",") if x.strip()
]

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/app/data/workspaces")
ORG_WORKSPACE  = os.path.join(WORKSPACE_ROOT, f"org_{ORG_ID}")
os.makedirs(ORG_WORKSPACE, exist_ok=True)
os.chdir(ORG_WORKSPACE)


# ── Logging ──────────────────────────────────────────────────────────
from logger import setup_logging

setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

for noisy in (
    "httpx", "httpcore", "urllib3", "huggingface_hub",
    "sentence_transformers", "filelock",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Engine (import AFTER chdir — gm.py uses cwd for paths) ───────────
import gm                                              # noqa: E402
from orchestration import inbox as inbox_mod           # noqa: E402

# gm.py has already:
#   • created the CEO dispatcher thread
#   • registered the CEO in the agent registry
#   • wired inbox.init() to <cwd>/ai_civilization/inbox.db
#
# Nothing else to start. No scheduler, no watchdog, no notifier.

THREAD_ID  = f"tg_{ORG_ID}"          # logical user identifier
USER_THREAD = f"user_{THREAD_ID}"    # inbox thread the CEO writes to

# Telegram bot
bot = telebot.TeleBot(BOT_TOKEN)


# ══════════════════════════════════════════════════════════════════════
# Send helpers — 429-aware
# ══════════════════════════════════════════════════════════════════════
def _send_with_backoff(chat_id: int, text: str, max_attempts: int = 3) -> bool:
    """Send a Telegram message, honouring server `retry_after` on 429."""
    for attempt in range(1, max_attempts + 1):
        try:
            bot.send_message(chat_id, text)
            return True
        except ApiTelegramException as e:
            if e.error_code == 429:
                m = re.search(r"retry after (\d+)", e.description or "")
                wait = min(int(m.group(1)) if m else 5, 30)
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


# ══════════════════════════════════════════════════════════════════════
# Poller — CEO replies on the user thread → Telegram
# ══════════════════════════════════════════════════════════════════════
def poll_user_thread() -> None:
    """
    Watch `user_<THREAD_ID>` for rows where sender='ceo' and id > last_seen.
    Deliver each to the Telegram chat.

    No direction filter — the new inbox writes every row as direction='IN'.
    Filtering on sender='ceo' is what distinguishes CEO replies.
    """
    # Seed from current max so we don't replay history on boot.
    last_seen = inbox_mod.max_id(USER_THREAD)
    logger.info(
        f"📤 user-thread poller seeded at last_seen={last_seen} "
        f"(thread={USER_THREAD})"
    )

    while True:
        try:
            for msg in inbox_mod.since(USER_THREAD, last_seen):
                last_seen = max(last_seen, int(msg.get("id") or 0))

                if msg.get("sender") != "ceo":
                    continue

                chat_id = getattr(poll_user_thread, "last_chat_id", None)
                if not chat_id:
                    logger.debug(
                        f"No chat_id yet; skipping msg #{msg.get('id')}."
                    )
                    continue

                body = msg.get("body") or ""
                if not body:
                    continue

                delivered = True
                for chunk in _chunk_text(body, 4000):
                    if not _send_with_backoff(chat_id, chunk):
                        delivered = False
                        break

                if delivered:
                    logger.info(
                        f"✅ Sent msg #{msg.get('id')} to chat {chat_id} "
                        f"({len(body)} chars)"
                    )
                else:
                    logger.error(
                        f"❌ Giving up on msg #{msg.get('id')} "
                        f"(chat {chat_id})"
                    )
        except Exception as e:
            logger.error(f"Poller error: {e}")
        time.sleep(1)


poll_user_thread.last_chat_id = None  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════
# Telegram handlers
# ══════════════════════════════════════════════════════════════════════
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
    logger.info(f"/start from user {message.from_user.id}")


@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if message.from_user.id not in ALLOWED_USER_IDS:
        bot.reply_to(message, "⛔ ACCESS DENIED.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    text    = (message.text or "").strip()

    # Register the chat_id so the poller can deliver replies.
    poll_user_thread.last_chat_id = chat_id  # type: ignore[attr-defined]

    if not text:
        return

    logger.info(f"📥 From user {user_id}: {text[:100]}")

    try:
        # Fast-path for bare greetings — saves one LLM round-trip.
        if gm.is_greeting(text):
            gm.send_instant_reply(THREAD_ID, "Hello! How can I assist you today?")
            logger.info(f"Instant greeting reply queued for user {user_id}")
            return

        # Queue a mission into the CEO's inbox.
        msg_id = gm.start_chat_mission(text, thread_id=THREAD_ID)
        logger.info(
            f"Queued CEO mission (msg #{msg_id}) for user {user_id} "
            f"(text={text[:60]})"
        )
    except Exception:
        logger.exception(f"Error processing message from user {user_id}")
        try:
            bot.reply_to(
                message,
                "❌ An error occurred while processing your request. "
                "Please try again.",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# Boot
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    threading.Thread(target=poll_user_thread, daemon=True).start()
    logger.info(f"📤 user-thread poller started (thread={USER_THREAD})")
    logger.info(f"✅ Bot worker ready for org {ORG_ID}")

    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down worker.")
    except Exception as e:
        logger.exception(f"Fatal bot polling error: {e}")#!/usr/bin/env python3
"""
Single-organisation Telegram bot worker.

Architecture:
  • User messages are queued into the CEO's inbox via gm.start_chat_mission().
  • The CEO's dispatcher (started by gm on import) picks them up, runs a
    turn, and writes its reply to the user thread: user_<THREAD_ID>.
  • The poller below drains user_<THREAD_ID> for rows where sender='ceo'
    and delivers them to the Telegram chat.
  • Delegation is a tool call: the CEO writes to a worker's inbox. The
    worker's dispatcher picks it up, runs its own turn, and replies to the
    CEO's inbox. The CEO's dispatcher picks that up and writes the final
    summary to the user thread. No notifier needed — the inbox IS the bus.

Env:
  ORG_ID             — organisation identifier
  BOT_TOKEN          — Telegram bot token
  ALLOWED_USER_IDS   — comma-separated Telegram user IDs
  WORKSPACE_ROOT     — parent dir; each org runs in <WORKSPACE_ROOT>/org_<id>
"""

import logging
import os
import re
import sys
import threading
import time

import telebot
from telebot.apihelper import ApiTelegramException
from dotenv import load_dotenv

load_dotenv()

ORG_ID               = os.environ.get("ORG_ID")
BOT_TOKEN            = os.environ.get("BOT_TOKEN")
ALLOWED_USER_IDS_STR = os.environ.get("ALLOWED_USER_IDS", "")

if not ORG_ID or not BOT_TOKEN or not ALLOWED_USER_IDS_STR:
    print("❌ Missing required env vars: ORG_ID, BOT_TOKEN, ALLOWED_USER_IDS.")
    sys.exit(1)

ALLOWED_USER_IDS = [
    int(x.strip()) for x in ALLOWED_USER_IDS_STR.split(",") if x.strip()
]

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/app/data/workspaces")
ORG_WORKSPACE  = os.path.join(WORKSPACE_ROOT, f"org_{ORG_ID}")
os.makedirs(ORG_WORKSPACE, exist_ok=True)
os.chdir(ORG_WORKSPACE)


# ── Logging ──────────────────────────────────────────────────────────
from logger import setup_logging

setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

for noisy in (
    "httpx", "httpcore", "urllib3", "huggingface_hub",
    "sentence_transformers", "filelock",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Engine (import AFTER chdir — gm.py uses cwd for paths) ───────────
import gm                                              # noqa: E402
from orchestration import inbox as inbox_mod           # noqa: E402

# gm.py has already:
#   • created the CEO dispatcher thread
#   • registered the CEO in the agent registry
#   • wired inbox.init() to <cwd>/ai_civilization/inbox.db
#
# Nothing else to start. No scheduler, no watchdog, no notifier.

THREAD_ID  = f"tg_{ORG_ID}"          # logical user identifier
USER_THREAD = f"user_{THREAD_ID}"    # inbox thread the CEO writes to

# Telegram bot
bot = telebot.TeleBot(BOT_TOKEN)


# ══════════════════════════════════════════════════════════════════════
# Send helpers — 429-aware
# ══════════════════════════════════════════════════════════════════════
def _send_with_backoff(chat_id: int, text: str, max_attempts: int = 3) -> bool:
    """Send a Telegram message, honouring server `retry_after` on 429."""
    for attempt in range(1, max_attempts + 1):
        try:
            bot.send_message(chat_id, text)
            return True
        except ApiTelegramException as e:
            if e.error_code == 429:
                m = re.search(r"retry after (\d+)", e.description or "")
                wait = min(int(m.group(1)) if m else 5, 30)
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


# ══════════════════════════════════════════════════════════════════════
# Poller — CEO replies on the user thread → Telegram
# ══════════════════════════════════════════════════════════════════════
def poll_user_thread() -> None:
    """
    Watch `user_<THREAD_ID>` for rows where sender='ceo' and id > last_seen.
    Deliver each to the Telegram chat.

    No direction filter — the new inbox writes every row as direction='IN'.
    Filtering on sender='ceo' is what distinguishes CEO replies.
    """
    # Seed from current max so we don't replay history on boot.
    last_seen = inbox_mod.max_id(USER_THREAD)
    logger.info(
        f"📤 user-thread poller seeded at last_seen={last_seen} "
        f"(thread={USER_THREAD})"
    )

    while True:
        try:
            for msg in inbox_mod.since(USER_THREAD, last_seen):
                last_seen = max(last_seen, int(msg.get("id") or 0))

                if msg.get("sender") != "ceo":
                    continue

                chat_id = getattr(poll_user_thread, "last_chat_id", None)
                if not chat_id:
                    logger.debug(
                        f"No chat_id yet; skipping msg #{msg.get('id')}."
                    )
                    continue

                body = msg.get("body") or ""
                if not body:
                    continue

                delivered = True
                for chunk in _chunk_text(body, 4000):
                    if not _send_with_backoff(chat_id, chunk):
                        delivered = False
                        break

                if delivered:
                    logger.info(
                        f"✅ Sent msg #{msg.get('id')} to chat {chat_id} "
                        f"({len(body)} chars)"
                    )
                else:
                    logger.error(
                        f"❌ Giving up on msg #{msg.get('id')} "
                        f"(chat {chat_id})"
                    )
        except Exception as e:
            logger.error(f"Poller error: {e}")
        time.sleep(1)


poll_user_thread.last_chat_id = None  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════
# Telegram handlers
# ══════════════════════════════════════════════════════════════════════
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
    logger.info(f"/start from user {message.from_user.id}")


@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if message.from_user.id not in ALLOWED_USER_IDS:
        bot.reply_to(message, "⛔ ACCESS DENIED.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    text    = (message.text or "").strip()

    # Register the chat_id so the poller can deliver replies.
    poll_user_thread.last_chat_id = chat_id  # type: ignore[attr-defined]

    if not text:
        return

    logger.info(f"📥 From user {user_id}: {text[:100]}")

    try:
        # Fast-path for bare greetings — saves one LLM round-trip.
        if gm.is_greeting(text):
            gm.send_instant_reply(THREAD_ID, "Hello! How can I assist you today?")
            logger.info(f"Instant greeting reply queued for user {user_id}")
            return

        # Queue a mission into the CEO's inbox.
        msg_id = gm.start_chat_mission(text, thread_id=THREAD_ID)
        logger.info(
            f"Queued CEO mission (msg #{msg_id}) for user {user_id} "
            f"(text={text[:60]})"
        )
    except Exception:
        logger.exception(f"Error processing message from user {user_id}")
        try:
            bot.reply_to(
                message,
                "❌ An error occurred while processing your request. "
                "Please try again.",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# Boot
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    threading.Thread(target=poll_user_thread, daemon=True).start()
    logger.info(f"📤 user-thread poller started (thread={USER_THREAD})")
    logger.info(f"✅ Bot worker ready for org {ORG_ID}")

    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down worker.")
    except Exception as e:
        logger.exception(f"Fatal bot polling error: {e}")









































################################################################################################
