# bot.py
import logging
from telegram import Update, MessageEntity, ParseMode, Bot
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackContext, ChatMemberHandler, ConversationHandler
)
import sqlite3
import os
from threading import Lock
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from telegram.error import Unauthorized, BadRequest, TimedOut, TelegramError

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
    if not os.path.exists("banned_words.txt"):
        open("banned_words.txt", "a").close()
    with open("banned_words.txt", "r") as f:
        return [word.strip().lower() for word in f.readlines() if word.strip()]

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

# Table for failed broadcast deliveries
execute_db("""CREATE TABLE IF NOT EXISTS failed_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    target_name TEXT,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

# Scheduler for broadcasting
scheduler = BackgroundScheduler()
scheduler.start()

# ====================== HELPER FUNCTIONS ======================
def is_admin(update: Update):
    user = update.effective_user
    return user and getattr(user, "id", None) == ADMIN_ID

def log_event(group_id, user_id, content, reason):
    execute_db("""INSERT INTO deleted_messages
              (group_id, user_id, content, reason)
              VALUES (?, ?, ?, ?)""",
              (group_id, user_id, content, reason))

def send_to_admin(context, message):
    # Avoid crashing if context missing: use Bot fallback
    try:
        context.bot.send_message(chat_id=ADMIN_ID, text=message)
    except Exception:
        try:
            Bot(TOKEN).send_message(chat_id=ADMIN_ID, text=message)
        except Exception as e:
            logger.error(f"Failed to send admin message: {e}")

def mention_md(user):
    """Return markdown mention for a user usable in group messages."""
    if not user:
        return "Unknown"
    uid = getattr(user, "id", None)
    name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
    if uid:
        return f"[{name.strip()}](tg://user?id={uid})"
    return name.strip()

def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Return True if user is admin or creator in a chat. Safe guards applied."""
    try:
        member = bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return getattr(member, "status", "") in ("administrator", "creator")
    except Exception:
        return False

def mention_group_admin(bot: Bot, chat_id: int):
    """Return a mention for the first admin found in a chat."""
    try:
        admins = bot.get_chat_administrators(chat_id)
        if admins:
            admin_user = admins[0].user
            return mention_md(admin_user)
    except Exception:
        pass
    return "Admin"

# ====================== WELCOME MESSAGE FUNCTIONALITY ======================
def welcome_message(update: Update, context: CallbackContext):
    # safety guards
    if not update.message or not update.message.new_chat_members:
        return

    for user in update.message.new_chat_members:
        chat = update.effective_chat
        username = user.username or "No Username"
        first_name = user.first_name or "No First Name"
        last_name = user.last_name or "No Last Name"
        user_id = getattr(user, "id", None)
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

        try:
            welcome_msg = update.message.reply_text(welcome_text)
        except Exception as e:
            logger.error(f"Failed to send welcome message: {e}")
            continue

        # schedule deletion only for groups (not private chats)
        if chat and getattr(chat, "type", "") != "private":
            try:
                context.job_queue.run_once(
                    delete_message,
                    when=20,
                    context={
                        "chat_id": chat.id,
                        "message_id": welcome_msg.message_id
                    }
                )
            except Exception:
                pass

# ====================== START COMMAND ======================
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    chat = update.effective_chat
    username = user.username or "No Username"
    first_name = user.first_name or "No First Name"
    last_name = user.last_name or "No Last Name"
    user_id = getattr(user, "id", None)
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
    update.message.reply_text(start_message)

# ====================== TRACK JOIN/LEAVE EVENTS ======================
def track_join_leave(update: Update, context: CallbackContext):
    event = update.chat_member
    if not event or not event.chat or not event.new_chat_member:
        return

    new_user = event.new_chat_member.user
    if not new_user or not getattr(new_user, "id", None):
        return

    group_id = event.chat.id
    user_id = new_user.id
    username = new_user.username or "Unknown"

    action = "join" if event.new_chat_member.status == "member" else "leave"

    execute_db("""INSERT INTO join_leave_events
              (group_id, user_id, action)
              VALUES (?, ?, ?)""",
              (group_id, user_id, action))

    msg = f"👤 {username} {action}ed group {event.chat.title}"
    try:
        context.bot.send_message(chat_id=ADMIN_ID, text=msg)
    except Exception:
        pass

# ====================== MESSAGE HANDLER ======================
def message_handler(update: Update, context: CallbackContext):
    message = update.effective_message
    if not message or not message.chat:
        return

    user = update.effective_user
    chat = update.effective_chat

    # Guard against missing user
    if not user or not getattr(user, "id", None):
        logger.warning("Skipping message: no effective user or user id")
        return

    # Don't process private chats here
    if getattr(chat, "type", "") == "private":
        return

    # If a broadcast conversation is active for this admin, avoid stealing messages
    if context.user_data.get("broadcast_type"):
        # let conversation handlers process these messages
        return

    # Save group and user info
    try:
        execute_db("""INSERT OR IGNORE INTO groups
                  (group_id, group_name) VALUES (?, ?)""",
                  (chat.id, chat.title))
        execute_db("""INSERT OR REPLACE INTO users
                  (user_id, group_id, username)
                  VALUES (?, ?, ?)""",
                  (user.id, chat.id, user.username))
    except Exception as e:
        logger.error(f"DB write error: {e}")

    # Check for URLs
    if message.entities:
        if any(getattr(entity, "type", None) == MessageEntity.URL for entity in message.entities):
            if not is_admin(update):
                try:
                    message.delete()
                except Exception:
                    pass

                warning_text = f"⚠️ Hi {user.first_name}, URLs are not allowed in this group. Please refrain from sharing links. Thank you! 😊"
                try:
                    warning_msg = context.bot.send_message(chat_id=chat.id, text=warning_text)
                except Exception:
                    warning_msg = None

                # schedule delete only in groups
                if getattr(chat, "type", "") != "private" and warning_msg:
                    try:
                        context.job_queue.run_once(
                            delete_message,
                            when=20,
                            context={
                                "chat_id": chat.id,
                                "message_id": warning_msg.message_id
                            }
                        )
                    except Exception:
                        pass

                log_event(chat.id, user.id, message.text or "URL in media", "URL")
                admin_report = f"🚫 Deleted URL from {user.username or user.first_name} in {chat.title}:\nContent: {message.text or 'URL in media'}"
                send_to_admin(context, admin_report)

    # Check banned words
    if message.text:
        text = message.text.lower()
        if any(word in text for word in banned_words):
            if not is_admin(update):
                try:
                    message.delete()
                except Exception:
                    pass

                warning_text = f"⚠️ Hi {user.first_name}, the message contained a banned word. Please be mindful of the group rules. Thank you! 😊"
                try:
                    warning_msg = context.bot.send_message(chat_id=chat.id, text=warning_text)
                except Exception:
                    warning_msg = None

                if getattr(chat, "type", "") != "private" and warning_msg:
                    try:
                        context.job_queue.run_once(
                            delete_message,
                            when=20,
                            context={
                                "chat_id": chat.id,
                                "message_id": warning_msg.message_id
                            }
                        )
                    except Exception:
                        pass

                log_event(chat.id, user.id, message.text, "Banned word")
                admin_report = f"🚫 Deleted banned word from {user.username or user.first_name} in {chat.title}:\nContent: {message.text}"
                send_to_admin(context, admin_report)

# ====================== STATS COMMAND ======================
def stats_command(update: Update, context: CallbackContext):
    total_deleted = execute_db("SELECT COUNT(*) FROM deleted_messages")[0][0] or 0
    total_groups = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
    total_users = execute_db("SELECT COUNT(DISTINCT user_id) FROM users")[0][0] or 0

    reason_stats = execute_db("SELECT reason, COUNT(*) FROM deleted_messages GROUP BY reason")
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
    if not is_admin(update):
        update.message.reply_text("❌ This command is for admin only.")
        return

    global banned_words
    banned_words = load_banned_words()
    update.message.reply_text(f"✅ Banned words reloaded! Loaded {len(banned_words)} words.")

# ====================== ENHANCED BROADCAST FUNCTIONALITY ======================
BROADCAST_TYPE, BROADCAST_MESSAGE, BROADCAST_CONFIRM = range(3)

def broadcast(update: Update, context: CallbackContext):
    if not is_admin(update):
        update.message.reply_text("❌ This command is for admin only.")
        return ConversationHandler.END

    options_text = (
        "📢 **Broadcast Options:**\n\n"
        "1. 👤 All Bot Users - Send to all users who interacted with bot\n"
        "2. 👥 All Groups - Send to all groups where bot is added\n"
        "3. 🎯 Specific Group - Choose specific group from list\n\n"
        "Please reply with number (1, 2, or 3):"
    )
    update.message.reply_text(options_text, parse_mode=ParseMode.MARKDOWN)
    return BROADCAST_TYPE

def broadcast_type(update: Update, context: CallbackContext):
    choice = (update.message.text or "").strip()

    if choice == "1":
        context.user_data["broadcast_type"] = "all_users"
        users_count = execute_db("SELECT COUNT(DISTINCT user_id) FROM users")[0][0] or 0
        broadcast_type_name = f"All Bot Users ({users_count} users)"

        update.message.reply_text(
            f"✅ Selected: {broadcast_type_name}\n\n"
            "📝 Now please send the message you want to broadcast:\n"
            "• Text message\n"
            "• Photo with caption\n"
            "• Document/file\n"
            "• Or any media"
        )
        return BROADCAST_MESSAGE

    elif choice == "2":
        context.user_data["broadcast_type"] = "all_groups"
        groups_count = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
        broadcast_type_name = f"All Groups ({groups_count} groups)"

        update.message.reply_text(
            f"✅ Selected: {broadcast_type_name}\n\n"
            "📝 Now please send the message you want to broadcast:\n"
            "• Text message\n"
            "• Photo with caption\n"
            "• Document/file\n"
            "• Or any media"
        )
        return BROADCAST_MESSAGE

    elif choice == "3":
        context.user_data["broadcast_type"] = "specific_group"
        groups = execute_db("SELECT group_id, group_name FROM groups")
        if not groups:
            update.message.reply_text("❌ No groups found in database.")
            return ConversationHandler.END

        group_list = "\n".join([f"{i+1}. {row[1]}" for i, row in enumerate(groups)])
        update.message.reply_text(
            f"📋 **Available Groups:**\n\n{group_list}\n\n"
            "Please reply with group number to choose the target group:"
        )
        context.user_data["groups_list"] = groups
        return BROADCAST_MESSAGE

    else:
        update.message.reply_text("❌ Invalid choice. Please enter 1, 2, or 3:")
        return BROADCAST_TYPE

def broadcast_message(update: Update, context: CallbackContext):
    btype = context.user_data.get("broadcast_type")

    if not btype:
        update.message.reply_text("❌ No broadcast type selected. Please run /broadcast again.")
        return ConversationHandler.END

    # If expecting group selection
    if btype == "specific_group" and not context.user_data.get("selected_group"):
        text = update.message.text or ""
        try:
            idx = int(text.strip()) - 1
            groups = context.user_data.get("groups_list", [])
            if 0 <= idx < len(groups):
                selected_group = groups[idx]
                context.user_data["selected_group"] = selected_group
                update.message.reply_text(
                    f"✅ Selected group: {selected_group[1]}\n\n"
                    "📝 Now send the message you want to broadcast to this group (text/photo/document/etc):"
                )
                return BROADCAST_MESSAGE
            else:
                update.message.reply_text("❌ Invalid group number. Please enter a valid number from the list:")
                return BROADCAST_MESSAGE
        except ValueError:
            update.message.reply_text("❌ Please reply with a valid number for the group selection:")
            return BROADCAST_MESSAGE

    # Otherwise treat as broadcast content
    context.user_data["broadcast_message"] = update.message  # store full message

    if btype == "all_users":
        users_count = execute_db("SELECT COUNT(DISTINCT user_id) FROM users")[0][0] or 0
        target_info = f"All Bot Users ({users_count} users)"
    elif btype == "all_groups":
        groups_count = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
        target_info = f"All Groups ({groups_count} groups)"
    else:
        selected_group = context.user_data.get("selected_group")
        target_info = f"Specific Group: {selected_group[1]}"

    # Message preview
    message_preview = ""
    m = update.message
    if m.text:
        preview = m.text[:200] + ("..." if len(m.text) > 200 else "")
        message_preview = f"Text: {preview}"
    elif m.photo:
        message_preview = "Photo with caption" if m.caption else "Photo"
    elif m.document:
        message_preview = f"Document: {m.document.file_name}"
    else:
        message_preview = "Media message"

    confirm_text = (
        f"📢 **Broadcast Confirmation**\n\n"
        f"🎯 **Target:** {target_info}\n"
        f"📝 **Message Type:** {message_preview}\n\n"
        "Type 'confirm' to send or 'cancel' to abort:"
    )

    update.message.reply_text(confirm_text, parse_mode=ParseMode.MARKDOWN)
    return BROADCAST_CONFIRM

# Helper: serialize message to simple dict safe for scheduler
def _serialize_message_for_broadcast(msg):
    if not msg:
        return {}
    data = {}
    if msg.text:
        data["type"] = "text"
        data["text"] = msg.text
    elif msg.photo:
        data["type"] = "photo"
        data["file_id"] = msg.photo[-1].file_id
        data["caption"] = msg.caption
    elif msg.document:
        data["type"] = "document"
        data["file_id"] = msg.document.file_id
        data["caption"] = msg.caption
    else:
        data["type"] = "text"
        data["text"] = getattr(msg, "caption", "") or " "
    return data

def _send_serialized(bot: Bot, chat_id: int, msg_data: dict):
    typ = msg_data.get("type")
    if typ == "text":
        bot.send_message(chat_id=chat_id, text=msg_data.get("text"))
    elif typ == "photo":
        bot.send_photo(chat_id=chat_id, photo=msg_data.get("file_id"), caption=msg_data.get("caption"))
    elif typ == "document":
        bot.send_document(chat_id=chat_id, document=msg_data.get("file_id"), caption=msg_data.get("caption"))
    else:
        text = msg_data.get("text") or msg_data.get("caption") or " "
        bot.send_message(chat_id=chat_id, text=text)

def run_broadcast_job(btype, msg_data, selected_group_id=None, admin_id=None):
    """Runs in background via APScheduler."""
    bot = Bot(TOKEN)
    success = 0
    fail = 0
    failed_targets = []
    try:
        if btype == "all_users":
            rows = execute_db("SELECT DISTINCT user_id FROM users")
            targets = [r[0] for r in rows]
        elif btype == "all_groups":
            rows = execute_db("SELECT group_id FROM groups")
            targets = [r[0] for r in rows]
        else:
            targets = [selected_group_id] if selected_group_id else []

        total = len(targets)
        for tgt in targets:
            try:
                _send_serialized(bot, tgt, msg_data)
                success += 1
            except Unauthorized:
                fail += 1
                failed_targets.append(f"{tgt} (blocked/unauthorized)")
                execute_db("INSERT INTO failed_deliveries (target_id, target_name, reason) VALUES (?, ?, ?)",
                           (tgt, str(tgt), "Unauthorized"))
            except TimedOut:
                fail += 1
                failed_targets.append(f"{tgt} (timeout)")
                execute_db("INSERT INTO failed_deliveries (target_id, target_name, reason) VALUES (?, ?, ?)",
                           (tgt, str(tgt), "TimedOut"))
            except TelegramError as e:
                fail += 1
                failed_targets.append(f"{tgt} ({type(e).__name__})")
                execute_db("INSERT INTO failed_deliveries (target_id, target_name, reason) VALUES (?, ?, ?)",
                           (tgt, str(tgt), type(e).__name__))
            except Exception as e:
                fail += 1
                failed_targets.append(f"{tgt} ({type(e).__name__})")
                execute_db("INSERT INTO failed_deliveries (target_id, target_name, reason) VALUES (?, ?, ?)",
                           (tgt, str(tgt), type(e).__name__))
    except Exception as e:
        try:
            bot.send_message(chat_id=admin_id or ADMIN_ID, text=f"❌ Broadcast job crashed: {type(e).__name__}: {e}")
        except Exception:
            pass
        return

    success_rate = (success / total * 100) if total else 0.0
    report = (
        f"✅ Broadcast Finished\n\n"
        f"Target type: {btype}\n"
        f"Total: {total}\n"
        f"Success: {success}\n"
        f"Failed: {fail}\n"
        f"Success rate: {success_rate:.1f}%\n"
    )
    if failed_targets:
        report += "\nFailed examples:\n" + "\n".join(failed_targets[:10])
    try:
        bot.send_message(chat_id=admin_id or ADMIN_ID, text=report)
    except Exception:
        pass

def broadcast_confirm(update: Update, context: CallbackContext):
    if update.message.text is None or update.message.text.lower() != 'confirm':
        update.message.reply_text("❌ Broadcast canceled.")
        return ConversationHandler.END

    btype = context.user_data.get("broadcast_type")
    message = context.user_data.get("broadcast_message")
    if not btype or not message:
        update.message.reply_text("❌ Missing broadcast info. Please try again.")
        return ConversationHandler.END

    msg_data = _serialize_message_for_broadcast(message)
    selected_group = context.user_data.get("selected_group")
    selected_group_id = selected_group[0] if selected_group else None

    # Schedule broadcast in background
    try:
        scheduler.add_job(run_broadcast_job, args=[btype, msg_data, selected_group_id, update.effective_user.id])
    except Exception as e:
        logger.error(f"Failed to schedule broadcast job: {e}")
        update.message.reply_text("❌ Failed to queue broadcast. Try again later.")
        return ConversationHandler.END

    update.message.reply_text("✅ Broadcast queued and will run in background. You will receive a report when it's finished.")

    # cleanup conversation data
    for k in ("broadcast_type", "broadcast_message", "groups_list", "selected_group"):
        if k in context.user_data:
            del context.user_data[k]

    return ConversationHandler.END

# ====================== DELETE MESSAGE FUNCTION ======================
def delete_message(context: CallbackContext):
    job = context.job
    ctx = getattr(job, "context", {}) or {}
    chat_id = ctx.get("chat_id")
    message_id = ctx.get("message_id")
    if not chat_id or not message_id:
        return
    try:
        context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

# ====================== ERROR HANDLER ======================
def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Error: {context.error}")

# ====================== MAIN ======================
def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    # Handlers that should run before the generic message handler
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stats", stats_command))
    dp.add_handler(CommandHandler("reload", reload_banned_words))
    dp.add_handler(ChatMemberHandler(track_join_leave))

    # Broadcast conversation handler - must be added BEFORE the generic catch-all handler
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

    # Welcome/new members handler
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_message))

    # Generic message handler (catch-all) must be last so conversations can work
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command, message_handler))

    dp.add_error_handler(error_handler)

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    from flask import Flask
    app = Flask(__name__)

    @app.route('/')
    def home():
        return "Bot is running!", 200

    import threading
    PORT = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=PORT, debug=False), daemon=True).start()
    main()