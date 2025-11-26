import logging
from telegram import Update, MessageEntity, ParseMode
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackContext, ChatMemberHandler, ConversationHandler
)
import sqlite3
import os
from threading import Lock
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))  # Your Telegram ID
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
def is_admin(update: Update):
    user = update.effective_user
    chat = update.effective_chat
    if user and chat.get_member(user.id).status in ["administrator", "creator"]:
        return True
    return False

def log_event(group_id, user_id, content, reason):
    execute_db("""INSERT INTO deleted_messages
              (group_id, user_id, content, reason)
              VALUES (?, ?, ?, ?)""",
              (group_id, user_id, content, reason))

def send_to_admin(context, message):
    context.bot.send_message(chat_id=ADMIN_ID, text=message)

# ====================== WELCOME MESSAGE FUNCTIONALITY ======================
def welcome_message(update: Update, context: CallbackContext):
    # Get the new member who joined
    for user in update.message.new_chat_members:
        chat = update.effective_chat
        username = user.username or "No Username"
        first_name = user.first_name or "No First Name"
        last_name = user.last_name or "No Last Name"
        user_id = user.id
        join_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Welcome message with user details
        welcome_text = (
            f"👋 Welcome, {first_name} {last_name}!\n\n"
            f"📝 **User Details:**\n"
            f"• Username: @{username}\n"
            f"• User ID: `{user_id}`\n"
            f"• Join Date: {join_date}\n\n"
            "🤖 **Rules:**\n"
            "1. 🚫 No spam or banned words.\n"
            "2. 🔗 No URLs allowed.\n"
            "3. 👀 Be respectful to others!"
        )

        # Send the welcome message
        welcome_msg = update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN
        )

        # Delete the welcome message after 30 seconds
        context.job_queue.run_once(
            delete_message,  # Function to delete the message
            when=30,  # Delay in seconds
            context={
                "chat_id": chat.id,
                "message_id": welcome_msg.message_id
            }
        )

# ====================== START COMMAND ======================
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    chat = update.effective_chat
    username = user.username or "No Username"
    first_name = user.first_name or "No First Name"
    last_name = user.last_name or "No Last Name"
    user_id = user.id
    join_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if chat.type == "private":
        start_message = (
            f"👋 Welcome, {first_name} {last_name}!\n\n"
            f"📝 **User Details:**\n"
            f"• Username: @{username}\n"
            f"• User ID: `{user_id}`\n"
            f"• Join Date: {join_date}\n\n"
            "🤖 **Bot Features:**\n"
            "1. 🚫 Auto-delete URLs and banned words.\n"
            "2. 📊 Provide live group analytics.\n"
            "3. 📢 Broadcast messages to all groups.\n"
            "4. 👋 Greet new members with their details!"
        )
    else:
        # Handle group messages
        start_message = (
            f"👋 Welcome, {first_name} {last_name}!\n\n"
            f"📝 **User Details:**\n"
            f"• Username: @{username}\n"
            f"• User ID: `{user_id}`\n"
            f"• Join Date: {join_date}\n\n"
            "🤖 **Bot Features:**\n"
            "1. 🚫 Auto-delete URLs and banned words.\n"
            "2. 📊 Provide live group analytics.\n"
            "3. 📢 Broadcast messages to all groups.\n"
            "4. 👋 Greet new members with their details!"
        )
    update.message.reply_text(start_message, parse_mode=ParseMode.MARKDOWN)

# ====================== TRACK JOIN/LEAVE EVENTS ======================
def track_join_leave(update: Update, context: CallbackContext):
    event = update.chat_member
    if not event or not event.chat:
        return

    group_id = event.chat.id
    user_id = event.new_chat_member.user.id
    username = event.new_chat_member.user.username or "Unknown"
    
    action = "join" if event.new_chat_member.status == "member" else "leave"
    
    # Update database
    execute_db("""INSERT INTO join_leave_events
              (group_id, user_id, action)
              VALUES (?, ?, ?)""",
              (group_id, user_id, action))
    
    # Send DM to admin
    msg = f"👤 {username} {action}ed group {event.chat.title}"
    context.bot.send_message(chat_id=ADMIN_ID, text=msg)

# ====================== MESSAGE HANDLER ======================
def message_handler(update: Update, context: CallbackContext):
    message = update.effective_message
    if not message or not message.chat:
        return  # Skip invalid messages

    user = update.effective_user
    chat = update.effective_chat
    
    # Skip private chats
    if chat.type == "private":
        return
    
    # Update groups table
    execute_db("""INSERT OR IGNORE INTO groups
              (group_id, group_name) VALUES (?, ?)""",
              (chat.id, chat.title))
    
    # Update users table
    execute_db("""INSERT OR REPLACE INTO users
              (user_id, group_id, username)
              VALUES (?, ?, ?)""",
              (user.id, chat.id, user.username))
    
    # Check for URLs
    if any(entity.type == MessageEntity.URL for entity in message.entities):
        if not is_admin(update):
            # Delete the message
            message.delete()
            
            # Send a kind warning to the user
            warning_text = (
                f"⚠️ Hi {user.first_name}, URLs are not allowed in this group. "
                "Please refrain from sharing links. Thank you! 😊"
            )
            warning_msg = context.bot.send_message(
                chat_id=chat.id,
                text=warning_text,
                reply_to_message_id=message.message_id
            )
            
            # Delete the warning message after 30 seconds
            context.job_queue.run_once(
                delete_message,
                when=30,
                context={
                    "chat_id": chat.id,
                    "message_id": warning_msg.message_id
                }
            )
            
            # Log the event and report to admin
            log_event(chat.id, user.id, message.text, "URL")
            admin_report = (
                f"🚫 Deleted URL from {user.username or user.first_name} in {chat.title}:\n"
                f"• User ID: `{user.id}`\n"
                f"• Content: `{message.text}`\n"
                f"• Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            send_to_admin(context, admin_report)
    
    # Check banned words
    text = message.text.lower() if message.text else ""
    if any(word in text for word in banned_words):
        if not is_admin(update):
            # Delete the message
            message.delete()
            
            # Send a kind warning to the user
            warning_text = (
                f"⚠️ Hi {user.first_name}, the message contained a banned word. "
                "Please be mindful of the group rules. Thank you! 😊"
            )
            warning_msg = context.bot.send_message(
                chat_id=chat.id,
                text=warning_text,
                reply_to_message_id=message.message_id
            )
            
            # Delete the warning message after 30 seconds
            context.job_queue.run_once(
                delete_message,
                when=30,
                context={
                    "chat_id": chat.id,
                    "message_id": warning_msg.message_id
                }
            )
            
            # Log the event and report to admin
            log_event(chat.id, user.id, message.text, "Banned word")
            admin_report = (
                f"🚫 Deleted banned word from {user.username or user.first_name} in {chat.title}:\n"
                f"• User ID: `{user.id}`\n"
                f"• Content: `{message.text}`\n"
                f"• Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            send_to_admin(context, admin_report)

# ====================== STATS COMMAND ======================
def stats_command(update: Update, context: CallbackContext):
    if update.effective_chat.type != "private" or update.effective_user.id != ADMIN_ID:
        return
    
    # Get stats
    total_deleted = execute_db("SELECT COUNT(*) FROM deleted_messages")[0][0]
    reason_stats = execute_db("""SELECT reason, COUNT(*) FROM deleted_messages
                              GROUP BY reason""")
    reason_stats = "\n".join([f"{row[0]}: {row[1]}" for row in reason_stats])
    total_groups = execute_db("SELECT COUNT(DISTINCT group_id) FROM groups")[0][0]
    
    update.message.reply_text(
        f"📊 Live Stats:\n"
        f"• Total Groups: {total_groups}\n"
        f"• Total Deleted: {total_deleted}\n"
        f"Breakdown:\n{reason_stats}"
    )

# ====================== RELOAD BANNED WORDS ======================
def reload_banned_words(update: Update, context: CallbackContext):
    global banned_words
    banned_words = load_banned_words()
    update.message.reply_text("✅ Banned words reloaded!")

# ====================== BROADCAST FUNCTIONALITY ======================
BROADCAST_MESSAGE, BROADCAST_TIME, BROADCAST_CONFIRM = range(3)

def broadcast(update: Update, context: CallbackContext):
    if update.effective_user.id != ADMIN_ID:
        return
    
    update.message.reply_text("📢 Please enter the message you want to broadcast:")
    return BROADCAST_MESSAGE

def broadcast_message(update: Update, context: CallbackContext):
    context.user_data["broadcast_message"] = update.message.text
    update.message.reply_text("🕒 Please enter the date and time for the broadcast (format: YYYY-MM-DD HH:MM):")
    return BROADCAST_TIME

def broadcast_time(update: Update, context: CallbackContext):
    try:
        broadcast_time = datetime.strptime(update.message.text, "%Y-%m-%d %H:%M")
        context.user_data["broadcast_time"] = broadcast_time
        groups = execute_db("SELECT group_id, group_name FROM groups")
        group_list = "\n".join([f"{row[0]} - {row[1]}" for row in groups])
        update.message.reply_text(
            f"📋 Groups/Channels:\n{group_list}\n\n"
            "Please confirm the broadcast by typing 'confirm' or cancel by typing 'cancel'."
        )
        return BROADCAST_CONFIRM
    except ValueError:
        update.message.reply_text("❌ Invalid date format. Please use YYYY-MM-DD HH:MM.")
        return BROADCAST_TIME

def broadcast_confirm(update: Update, context: CallbackContext):
    if update.message.text.lower() == "confirm":
        broadcast_time = context.user_data["broadcast_time"]
        broadcast_message = context.user_data["broadcast_message"]
        groups = execute_db("SELECT group_id FROM groups")
        
        # Schedule the broadcast
        scheduler.add_job(
            send_broadcast,
            "date",
            run_date=broadcast_time,
            args=[context, broadcast_message, groups]
        )
        
        update.message.reply_text(
            f"✅ Broadcast scheduled for {broadcast_time.strftime('%Y-%m-%d %H:%M')}."
        )
    else:
        update.message.reply_text("❌ Broadcast canceled.")
    return ConversationHandler.END

def send_broadcast(context: CallbackContext, message, groups):
    for group in groups:
        try:
            context.bot.send_message(chat_id=group[0], text=message)
        except Exception as e:
            logger.error(f"Failed to send to group {group[0]}: {e}")

# ====================== DELETE MESSAGE FUNCTION ======================
def delete_message(context: CallbackContext):
    job = context.job
    chat_id = job.context["chat_id"]
    message_id = job.context["message_id"]

    # Delete the message
    context.bot.delete_message(chat_id=chat_id, message_id=message_id)

# ====================== ERROR HANDLER ======================
def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Error: {context.error}")
    context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Error occurred: {context.error}")

# ====================== HEALTH CHECK FOR RENDER ======================
from flask import Flask
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

# ====================== MAIN ======================
def main():
    # Start health check server in background for Render
    import threading
    def run_flask():
        app.run(host='0.0.0.0', port=5000)
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    updater = Updater(TOKEN)
    dp = updater.dispatcher
    
    # Handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stats", stats_command))
    dp.add_handler(CommandHandler("reload", reload_banned_words))
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_message))
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command, message_handler))
    dp.add_handler(ChatMemberHandler(track_join_leave))
    dp.add_error_handler(error_handler)
    
    # Broadcast conversation handler
    broadcast_handler = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast)],
        states={
            BROADCAST_MESSAGE: [MessageHandler(Filters.text & ~Filters.command, broadcast_message)],
            BROADCAST_TIME: [MessageHandler(Filters.text & ~Filters.command, broadcast_time)],
            BROADCAST_CONFIRM: [MessageHandler(Filters.text & ~Filters.command, broadcast_confirm)],
        },
        fallbacks=[]
    )
    dp.add_handler(broadcast_handler)
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
