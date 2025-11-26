import logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ChatMemberHandler, ConversationHandler
)
import sqlite3
import os
from threading import Lock
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DB_NAME = "group_stats.db"

# Initialize logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Thread-safe SQLite connection
db_lock = Lock()

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    return conn

def execute_db(query, params=()):
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        result = c.fetchall()
        conn.close()
        return result

# Load banned words
def load_banned_words():
    with open("banned_words.txt", "r") as f:
        return [word.strip().lower() for word in f.readlines()]

banned_words = load_banned_words()

# Database setup
execute_db("""CREATE TABLE IF NOT EXISTS groups (
    group_id INTEGER PRIMARY KEY,
    group_name TEXT
)""")

execute_db("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER,
    group_id INTEGER,
    username TEXT,
    PRIMARY KEY(user_id, group_id)
)""")

execute_db("""CREATE TABLE IF NOT EXISTS deleted_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    user_id INTEGER,
    content TEXT,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

execute_db("""CREATE TABLE IF NOT EXISTS join_leave_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    user_id INTEGER,
    action TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

# Scheduler for broadcasting
scheduler = BackgroundScheduler()
scheduler.start()

# ====================== HELPER FUNCTIONS ======================
def is_admin_user(user_id):
    return user_id == ADMIN_ID

def log_event(group_id, user_id, content, reason):
    execute_db("""INSERT INTO deleted_messages
              (group_id, user_id, content, reason)
              VALUES (?, ?, ?, ?)""",
              (group_id, user_id, content, reason))

async def send_to_admin(context, message):
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=message)
    except Exception as e:
        logger.error(f"Failed to send to admin: {e}")

# ====================== WELCOME MESSAGE FUNCTIONALITY ======================
async def welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for user in update.message.new_chat_members:
        chat = update.effective_chat
        username = user.username or "No Username"
        first_name = user.first_name or "No First Name"
        last_name = user.last_name or "No Last Name"
        user_id = user.id
        join_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        welcome_text = (
            f"👋 Welcome, {first_name} {last_name}!\n\n"
            f"📝 User Details:\n"
            f"• Username: @{username}\n"
            f"• User ID: {user_id}\n"
            f"• Join Date: {join_date}\n\n"
            "🤖 Rules:\n"
            "1. 🚫 No spam or banned words.\n"
            "2. 🔗 No URLs allowed.\n"
            "3. 👀 Be respectful to others!"
        )

        welcome_msg = await update.message.reply_text(welcome_text)

        context.job_queue.run_once(
            delete_message,
            when=30,
            data={
                "chat_id": chat.id,
                "message_id": welcome_msg.message_id
            }
        )

# ====================== START COMMAND ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    username = user.username or "No Username"
    first_name = user.first_name or "No First Name"
    last_name = user.last_name or "No Last Name"
    user_id = user.id
    join_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    start_message = (
        f"👋 Welcome, {first_name} {last_name}!\n\n"
        f"📝 User Details:\n"
        f"• Username: @{username}\n"
        f"• User ID: {user_id}\n"
        f"• Join Date: {join_date}\n\n"
        "🤖 Bot Features:\n"
        "1. 🚫 Auto-delete URLs and banned words.\n"
        "2. 📊 Provide live group analytics.\n"
        "3. 📢 Broadcast messages to all groups.\n"
        "4. 👋 Greet new members with their details!"
    )
    await update.message.reply_text(start_message)

# ====================== MESSAGE HANDLER ======================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.chat:
        return

    user = update.effective_user
    chat = update.effective_chat
    
    if chat.type == "private":
        return
    
    execute_db("""INSERT OR IGNORE INTO groups
              (group_id, group_name) VALUES (?, ?)""",
              (chat.id, chat.title))
    
    execute_db("""INSERT OR REPLACE INTO users
              (user_id, group_id, username)
              VALUES (?, ?, ?)""",
              (user.id, chat.id, user.username))
    
    # Check for URLs
    url_deleted = False
    if message.entities:
        if any(entity.type == "url" for entity in message.entities):
            if not is_admin_user(user.id):
                await message.delete()
                url_deleted = True
                
                warning_text = (
                    f"⚠️ Hi {user.first_name}, URLs are not allowed in this group. "
                    "Please refrain from sharing links. Thank you! 😊"
                )
                try:
                    warning_msg = await context.bot.send_message(
                        chat_id=chat.id,
                        text=warning_text
                    )
                    
                    context.job_queue.run_once(
                        delete_message,
                        when=30,
                        data={
                            "chat_id": chat.id,
                            "message_id": warning_msg.message_id
                        }
                    )
                except:
                    pass
                
                log_event(chat.id, user.id, message.text or "URL in media", "URL")
                
                admin_report = (
                    f"🚫 DELETED URL MESSAGE\n"
                    f"👤 From: {user.first_name} (@{user.username or 'no_username'})\n"
                    f"🆔 User ID: {user.id}\n"
                    f"💬 Group: {chat.title}\n"
                    f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"📝 Content:\n{message.text or 'URL in media'}"
                )
                await send_to_admin(context, admin_report)
    
    # Check banned words
    if not url_deleted and message.text:
        text = message.text.lower()
        if any(word in text for word in banned_words):
            if not is_admin_user(user.id):
                await message.delete()
                
                warning_text = (
                    f"⚠️ Hi {user.first_name}, the message contained a banned word. "
                    "Please be mindful of the group rules. Thank you! 😊"
                )
                try:
                    warning_msg = await context.bot.send_message(
                        chat_id=chat.id,
                        text=warning_text
                    )
                    
                    context.job_queue.run_once(
                        delete_message,
                        when=30,
                        data={
                            "chat_id": chat.id,
                            "message_id": warning_msg.message_id
                        }
                    )
                except:
                    pass
                
                log_event(chat.id, user.id, message.text, "Banned word")
                
                admin_report = (
                    f"🚫 DELETED BANNED WORD MESSAGE\n"
                    f"👤 From: {user.first_name} (@{user.username or 'no_username'})\n"
                    f"🆔 User ID: {user.id}\n"
                    f"💬 Group: {chat.title}\n"
                    f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"📝 Content:\n{message.text}"
                )
                await send_to_admin(context, admin_report)

# ====================== STATS COMMAND ======================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_deleted = execute_db("SELECT COUNT(*) FROM deleted_messages")[0][0] or 0
    total_groups = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
    total_users = execute_db("SELECT COUNT(DISTINCT user_id) FROM users")[0][0] or 0
    
    reason_stats = execute_db("""SELECT reason, COUNT(*) FROM deleted_messages 
                              GROUP BY reason""")
    breakdown = "\n".join([f"• {row[0]}: {row[1]}" for row in reason_stats]) if reason_stats else "• No deletions yet"
    
    stats_text = (
        f"📊 Live Stats\n\n"
        f"👥 Groups: {total_groups}\n"
        f"👤 Total Users: {total_users}\n"
        f"🗑️ Total Deleted: {total_deleted}\n\n"
        f"Breakdown:\n{breakdown}"
    )
    
    await update.message.reply_text(stats_text)

# ====================== RELOAD BANNED WORDS ======================
async def reload_banned_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin_user(user.id):
        await update.message.reply_text("❌ This command is for admin only.")
        return
    
    global banned_words
    banned_words = load_banned_words()
    await update.message.reply_text(f"✅ Banned words reloaded! Loaded {len(banned_words)} words.")

# ====================== DELETE MESSAGE FUNCTION ======================
async def delete_message(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

# ====================== ERROR HANDLER ======================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")

# ====================== MAIN ======================
def main():
    application = Application.builder().token(TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("reload", reload_banned_words))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_message))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))
    application.add_error_handler(error_handler)
    
    application.run_polling()

if __name__ == "__main__":
    # Add Flask for Render
    from flask import Flask
    app = Flask(__name__)

    @app.route('/')
    def home():
        return "Bot is running!", 200

    import threading
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False), daemon=True).start()
    main()