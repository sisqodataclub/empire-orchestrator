#!/usr/bin/env python3
"""
Single-organisation Telegram bot worker.

Runs a fast bot for one organisation by using the internal engine directly.
Expects environment variables:
  ORG_ID           - organisation UUID (used to locate workspace)
  BOT_TOKEN        - Telegram bot token
  ALLOWED_USER_IDS - comma-separated Telegram user IDs allowed to control the bot
"""

import os
import sys
import time
import threading
import telebot
from dotenv import load_dotenv

load_dotenv()

# --- Read environment ---
ORG_ID = os.environ.get("ORG_ID")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ALLOWED_USER_IDS_STR = os.environ.get("ALLOWED_USER_IDS", "")

if not ORG_ID or not BOT_TOKEN or not ALLOWED_USER_IDS_STR:
    print("❌ Missing required environment variables (ORG_ID, BOT_TOKEN, ALLOWED_USER_IDS).")
    sys.exit(1)

ALLOWED_USER_IDS = [int(x.strip()) for x in ALLOWED_USER_IDS_STR.split(',') if x.strip()]

# --- Change to the organisation's workspace directory ---
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/app/data/workspaces")
ORG_WORKSPACE = os.path.join(WORKSPACE_ROOT, f"org_{ORG_ID}")
os.chdir(ORG_WORKSPACE)

# --- Now import the engine modules (they rely on cwd) ---
import gm
from gm import manager, get_population, build_mission_prompt, start_chat_mission
from orchestration.inbox_db import InboxDB

# --- Setup inbox for this org (uses cwd) ---
INBOX_DB_PATH = os.path.join(os.getcwd(), "ai_civilization", "inbox.db")
inbox_db = InboxDB(INBOX_DB_PATH)

# --- Telegram bot ---
bot = telebot.TeleBot(BOT_TOKEN)

# Thread ID for this bot's conversations
THREAD_ID = f"tg_{ORG_ID}"

def wait_and_send_reply(task, chat_id):
    """Wait for the mission to complete and send only the final reply."""
    while not task.is_complete:
        time.sleep(0.5)
    if task.result:
        reply_text = task.result
        if len(reply_text) > 4000:
            reply_text = reply_text[:4000] + "\n... (truncated)"
        try:
            bot.send_message(chat_id, reply_text, parse_mode="Markdown")
        except Exception:
            try:
                bot.send_message(chat_id, reply_text)
            except Exception:
                pass
    else:
        bot.send_message(chat_id, "✅ Mission complete (no reply).")

@bot.message_handler(commands=['start'])
def handle_start(message):
    if message.from_user.id not in ALLOWED_USER_IDS:
        bot.reply_to(message, "⛔ ACCESS DENIED.")
        return
    bot.reply_to(message, "⚔️ **EMPIRE CONTROL ACTIVE**\nSend me a message, and the CEO will reply.")

@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if message.from_user.id not in ALLOWED_USER_IDS:
        bot.reply_to(message, "⛔ ACCESS DENIED.")
        return

    text = message.text.strip()
    chat_id = message.chat.id

    bot.reply_to(message, "🧠 CEO is processing your message...")

    # Start the chat mission using the internal engine
    task_id = start_chat_mission(
        raw_mission=text,
        priority="normal",
        mcp_tools=[],
        thread_id=THREAD_ID
    )

    task = manager.get_task(task_id)
    threading.Thread(target=wait_and_send_reply, args=(task, chat_id), daemon=True).start()

if __name__ == "__main__":
    print(f"✅ Bot worker started for org {ORG_ID}")
    bot.infinity_polling()
