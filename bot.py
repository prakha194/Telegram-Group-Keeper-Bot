import logging
from telegram import Update, MessageEntity
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackContext, ChatMemberHandler, ConversationHandler
)
import sqlite3
import os
from threading import Lock
from datetime import datetime
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
def is_admin_user(user_id):
    return user_id == ADMIN_ID

def log_event(group_id, user_id, content, reason):
    execute_db("""INSERT INTO deleted_messages
              (group_id, user_id, content, reason)
              VALUES (?, ?, ?, ?)""",
              (group_id, user_id, content, reason))

def send_to_admin(context, message):
    try:
        context.bot.send_message(chat_id=ADMIN_ID, text=message)
    except Exception as e:
        logger.error(f"Failed to send to admin: {e}")

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

        # Welcome message with user details (NO MARKDOWN to avoid parsing errors)
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

        # Send the welcome message (without markdown)
        welcome_msg = update.message.reply_text(welcome_text)

        # Delete the welcome message after 30 seconds
        context.job_queue.run_once(
            delete_message,
            when=30,
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
    else:
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
    update.message.reply_text(start_message)

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
    
    # Skip private chats for message filtering
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
    url_deleted = False
    if message.entities:
        if any(entity.type == MessageEntity.URL for entity in message.entities):
            if not is_admin_user(user.id):
                # Delete the message
                message.delete()
                url_deleted = True
                
                # Send a kind warning to the user
                warning_text = (
                    f"⚠️ Hi {user.first_name}, URLs are not allowed in this group. "
                    "Please refrain from sharing links. Thank you! 😊"
                )
                try:
                    warning_msg = context.bot.send_message(
                        chat_id=chat.id,
                        text=warning_text
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
                except:
                    pass
                
                # Log the event and report to admin
                log_event(chat.id, user.id, message.text or "URL in media", "URL")
                
                # Forward deleted message to admin
                admin_report = (
                    f"🚫 DELETED URL MESSAGE\n"
                    f"👤 From: {user.first_name} (@{user.username or 'no_username'})\n"
                    f"🆔 User ID: {user.id}\n"
                    f"💬 Group: {chat.title}\n"
                    f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"📝 Content:\n{message.text or 'URL in media'}"
                )
                send_to_admin(context, admin_report)
    
    # Check banned words (only if URL wasn't already deleted)
    if not url_deleted and message.text:
        text = message.text.lower()
        if any(word in text for word in banned_words):
            if not is_admin_user(user.id):
                # Delete the message
                message.delete()
                
                # Send a kind warning to the user
                warning_text = (
                    f"⚠️ Hi {user.first_name}, the message contained a banned word. "
                    "Please be mindful of the group rules. Thank you! 😊"
                )
                try:
                    warning_msg = context.bot.send_message(
                        chat_id=chat.id,
                        text=warning_text
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
                except:
                    pass
                
                # Log the event and report to admin
                log_event(chat.id, user.id, message.text, "Banned word")
                
                # Forward deleted message to admin
                admin_report = (
                    f"🚫 DELETED BANNED WORD MESSAGE\n"
                    f"👤 From: {user.first_name} (@{user.username or 'no_username'})\n"
                    f"🆔 User ID: {user.id}\n"
                    f"💬 Group: {chat.title}\n"
                    f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"📝 Content:\n{message.text}"
                )
                send_to_admin(context, admin_report)

# ====================== STATS COMMAND ======================
def stats_command(update: Update, context: CallbackContext):
    # Available to ALL users
    user = update.effective_user
    
    # Get accurate stats
    total_deleted = execute_db("SELECT COUNT(*) FROM deleted_messages")[0][0] or 0
    total_groups = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
    total_users = execute_db("SELECT COUNT(DISTINCT user_id) FROM users")[0][0] or 0
    
    # Get breakdown by reason
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
    
    update.message.reply_text(stats_text)

# ====================== RELOAD BANNED WORDS ======================
def reload_banned_words(update: Update, context: CallbackContext):
    # ADMIN ONLY
    user = update.effective_user
    if not is_admin_user(user.id):
        update.message.reply_text("❌ This command is for admin only.")
        return
    
    global banned_words
    banned_words = load_banned_words()
    update.message.reply_text(f"✅ Banned words reloaded! Loaded {len(banned_words)} words.")

# ====================== ENHANCED BROADCAST FUNCTIONALITY ======================
BROADCAST_TYPE, BROADCAST_MESSAGE, BROADCAST_CONFIRM = range(3)

def broadcast(update: Update, context: CallbackContext):
    # ADMIN ONLY
    user = update.effective_user
    if not is_admin_user(user.id):
        update.message.reply_text("❌ This command is for admin only.")
        return ConversationHandler.END
    
    # Show broadcast options
    options_text = (
        "📢 Broadcast Options:\n\n"
        "1. 🎯 DM to all users - Send to all users in private\n"
        "2. 👥 All groups - Send to all groups\n"
        "3. 📋 Specific group - Choose specific group\n\n"
        "Please reply with number (1, 2, or 3):"
    )
    
    update.message.reply_text(options_text)
    return BROADCAST_TYPE

def broadcast_type(update: Update, context: CallbackContext):
    choice = update.message.text.strip()
    
    if choice == "1":
        context.user_data["broadcast_type"] = "dm"
        broadcast_type_name = "DM to all users"
    elif choice == "2":
        context.user_data["broadcast_type"] = "all_groups"
        broadcast_type_name = "All groups"
    elif choice == "3":
        context.user_data["broadcast_type"] = "specific_group"
        # Show group list
        groups = execute_db("SELECT group_id, group_name FROM groups")
        if not groups:
            update.message.reply_text("❌ No groups found in database.")
            return ConversationHandler.END
        
        group_list = "\n".join([f"{i+1}. {row[1]} (ID: {row[0]})" for i, row in enumerate(groups)])
        update.message.reply_text(f"📋 Available groups:\n{group_list}\n\nPlease reply with group number:")
        context.user_data["groups_list"] = groups
        return BROADCAST_TYPE
    
    else:
        update.message.reply_text("❌ Invalid choice. Please enter 1, 2, or 3:")
        return BROADCAST_TYPE
    
    update.message.reply_text(
        f"✅ Selected: {broadcast_type_name}\n\n"
        "📝 Now please send the message you want to broadcast (text, photo, document, etc.):"
    )
    return BROADCAST_MESSAGE

def broadcast_message(update: Update, context: CallbackContext):
    # Store the message
    context.user_data["broadcast_message"] = update.message
    
    # Get target info based on type
    broadcast_type = context.user_data.get("broadcast_type")
    
    if broadcast_type == "dm":
        users = execute_db("SELECT DISTINCT user_id FROM users")
        count = len(users)
        target_info = f"DM to {count} users"
    elif broadcast_type == "all_groups":
        groups = execute_db("SELECT COUNT(*) FROM groups")[0][0]
        target_info = f"All {groups} groups"
    elif broadcast_type == "specific_group":
        groups_list = context.user_data.get("groups_list", [])
        selected_group = None
        
        try:
            group_num = int(update.message.text) - 1
            if 0 <= group_num < len(groups_list):
                selected_group = groups_list[group_num]
                context.user_data["selected_group"] = selected_group
                target_info = f"Group: {selected_group[1]}"
            else:
                update.message.reply_text("❌ Invalid group number. Please try again:")
                return BROADCAST_TYPE
        except ValueError:
            update.message.reply_text("❌ Please enter a valid number:")
            return BROADCAST_TYPE
    
    # Ask for confirmation
    confirm_text = (
        f"📢 Broadcast Confirmation\n\n"
        f"🎯 Target: {target_info}\n"
        f"📝 Message: {update.message.text if update.message.text else 'Media file'}\n\n"
        "Type 'confirm' to send or 'cancel' to abort:"
    )
    
    update.message.reply_text(confirm_text)
    return BROADCAST_CONFIRM

def broadcast_confirm(update: Update, context: CallbackContext):
    if update.message.text.lower() != 'confirm':
        update.message.reply_text("❌ Broadcast canceled.")
        return ConversationHandler.END
    
    # Send broadcast
    broadcast_type = context.user_data.get("broadcast_type")
    message = context.user_data.get("broadcast_message")
    
    success_count = 0
    fail_count = 0
    
    if broadcast_type == "dm":
        users = execute_db("SELECT DISTINCT user_id FROM users")
        for user in users:
            try:
                if message.text:
                    context.bot.send_message(chat_id=user[0], text=message.text)
                elif message.photo:
                    context.bot.send_photo(chat_id=user[0], photo=message.photo[-1].file_id)
                elif message.document:
                    context.bot.send_document(chat_id=user[0], document=message.document.file_id)
                success_count += 1
            except:
                fail_count += 1
    
    elif broadcast_type == "all_groups":
        groups = execute_db("SELECT group_id FROM groups")
        for group in groups:
            try:
                if message.text:
                    context.bot.send_message(chat_id=group[0], text=message.text)
                elif message.photo:
                    context.bot.send_photo(chat_id=group[0], photo=message.photo[-1].file_id)
                elif message.document:
                    context.bot.send_document(chat_id=group[0], document=message.document.file_id)
                success_count += 1
            except:
                fail_count += 1
    
    elif broadcast_type == "specific_group":
        group = context.user_data.get("selected_group")
        try:
            if message.text:
                context.bot.send_message(chat_id=group[0], text=message.text)
            elif message.photo:
                context.bot.send_photo(chat_id=group[0], photo=message.photo[-1].file_id)
            elif message.document:
                context.bot.send_document(chat_id=group[0], document=message.document.file_id)
            success_count = 1
        except:
            fail_count = 1
    
    result_text = (
        f"✅ Broadcast Completed!\n\n"
        f"✅ Success: {success_count}\n"
        f"❌ Failed: {fail_count}"
    )
    
    update.message.reply_text(result_text)
    return ConversationHandler.END

# ====================== DELETE MESSAGE FUNCTION ======================
def delete_message(context: CallbackContext):
    job = context.job
    chat_id = job.context["chat_id"]
    message_id = job.context["message_id"]

    try:
        context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

# ====================== ERROR HANDLER ======================
def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Error: {context.error}")

# ====================== MAIN ======================
def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher
    
    # Handlers - available to all users
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stats", stats_command))  # Available to all
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_message))
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command, message_handler))
    dp.add_handler(ChatMemberHandler(track_join_leave))
    
    # Admin-only handlers
    dp.add_handler(CommandHandler("reload", reload_banned_words))  # Admin only
    
    # Enhanced broadcast conversation handler (Admin only)
    broadcast_handler = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast)],
        states={
            BROADCAST_TYPE: [MessageHandler(Filters.text & ~Filters.command, broadcast_type)],
            BROADCAST_MESSAGE: [MessageHandler(Filters.all & ~Filters.command, broadcast_message)],
            BROADCAST_CONFIRM: [MessageHandler(Filters.text & ~Filters.command, broadcast_confirm)],
        },
        fallbacks=[]
    )
    dp.add_handler(broadcast_handler)
    
    dp.add_error_handler(error_handler)
    
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    # Add Flask for Render port binding
    from flask import Flask
    app = Flask(__name__)

    @app.route('/')
    def home():
        return "Bot is running!", 200

    import threading
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=5000, debug=False), daemon=True).start()
    main()