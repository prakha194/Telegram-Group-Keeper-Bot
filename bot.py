# bot.py
import logging
import os
import sqlite3
import time
from threading import Lock
from datetime import datetime
import pytz

from telegram import Update, MessageEntity, ParseMode, Bot
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackContext, ChatMemberHandler, ConversationHandler
)
from telegram.error import Unauthorized, BadRequest, TimedOut, TelegramError
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

# Keep track of users who started the bot (for direct broadcasts)
execute_db("""CREATE TABLE IF NOT EXISTS bot_users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

execute_db("""CREATE TABLE IF NOT EXISTS failed_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    target_name TEXT,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

# Scheduler for broadcasting (use pytz.UTC)
scheduler = BackgroundScheduler(timezone=pytz.UTC)
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

def send_to_admin_text(text):
    try:
        Bot(TOKEN).send_message(chat_id=ADMIN_ID, text=text, parse_mode=None)
    except Exception as e:
        logger.error(f"Failed to send admin text: {e}")

def safe_user_ident(user):
    """Return @username if available else first_name (id)"""
    if not user:
        return "Unknown"
    if getattr(user, "username", None):
        return f"@{user.username}"
    return f"{(user.first_name or 'User')} ({getattr(user,'id', 'id?')})"

# Remove any webhook / pending updates to avoid getUpdates conflict
def remove_webhook_if_any():
    try:
        Bot(TOKEN).delete_webhook(drop_pending_updates=True)
        logger.info("Deleted webhook (drop_pending_updates=True).")
    except Exception as e:
        logger.debug(f"delete_webhook: {e}")

# ====================== WELCOME MESSAGE FUNCTIONALITY ======================
def welcome_message(update: Update, context: CallbackContext):
    if not update.message or not getattr(update.message, "new_chat_members", None):
        return

    for user in update.message.new_chat_members:
        chat = update.effective_chat
        username = user.username or "No Username"
        first_name = user.first_name or "No First Name"
        last_name = user.last_name or "No Last Name"
        user_id = getattr(user, "id", None)
        join_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # EXACT original welcome text preserved
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
            welcome_msg = None

        # schedule delete only in groups (original 30 seconds)
        if welcome_msg and getattr(chat, "type", "") != "private":
            try:
                context.job_queue.run_once(
                    delete_message,
                    when=30,
                    context={
                        "chat_id": chat.id,
                        "message_id": welcome_msg.message_id
                    }
                )
            except Exception as e:
                logger.error(f"Failed to schedule welcome deletion: {e}")

# ====================== START COMMAND ======================
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    username = user.username or "No Username"
    first_name = user.first_name or "No First Name"
    last_name = user.last_name or "No Last Name"
    user_id = getattr(user, "id", None)
    join_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # record as bot_user (so broadcasts to bot users work)
    try:
        execute_db("""INSERT OR REPLACE INTO bot_users (user_id, username, first_name, last_name)
                      VALUES (?, ?, ?, ?)""", (user_id, user.username or None, user.first_name or None, user.last_name or None))
    except Exception as e:
        logger.error(f"Failed to record bot_user: {e}")

    # EXACT original start message preserved
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
    if not event or not getattr(event, "chat", None) or not getattr(event, "new_chat_member", None):
        return

    group_id = event.chat.id
    new_user = event.new_chat_member.user
    if not new_user:
        return
    user_id = getattr(new_user, "id", None)
    username = new_user.username or "Unknown"

    action = "join" if getattr(event.new_chat_member, "status", "") == "member" else "leave"

    try:
        execute_db("""INSERT INTO join_leave_events
                  (group_id, user_id, action)
                  VALUES (?, ?, ?)""",
                  (group_id, user_id, action))
    except Exception as e:
        logger.error(f"Failed to insert join_leave_events: {e}")

    msg = f"👤 {username} {action}ed group {event.chat.title}"
    try:
        context.bot.send_message(chat_id=ADMIN_ID, text=msg)
    except Exception:
        pass

# Track when bot is added to a chat (so we know groups)
def track_my_chat_member(update: Update, context: CallbackContext):
    # handles bot's own status changes in chats (MY_CHAT_MEMBER)
    try:
        cm = update.my_chat_member
    except Exception:
        cm = getattr(update, "chat_member", None)
    if not cm or not getattr(cm, "chat", None) or not getattr(cm, "new_chat_member", None):
        return
    chat = cm.chat
    new = cm.new_chat_member
    # if bot is in chat now, record it
    try:
        me = Bot(TOKEN).get_me()
        if getattr(new, "user", None) and getattr(new.user, "id", None) == me.id:
            status = getattr(new, "status", "")
            if status in ("member", "administrator", "creator"):
                try:
                    execute_db("INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?, ?)", (chat.id, chat.title or ""))
                    logger.info(f"Recorded group {chat.title} ({chat.id}) because bot was added/promoted.")
                except Exception as e:
                    logger.error(f"Failed to record group on bot add: {e}")
    except Exception:
        pass

# ====================== MESSAGE HANDLER ======================
def message_handler(update: Update, context: CallbackContext):
    message = update.effective_message
    if not message or not getattr(message, "chat", None):
        return

    user = update.effective_user
    chat = update.effective_chat

    # If chat is private, skip group logic
    if getattr(chat, "type", "") == "private":
        return

    # Prevent global handler from stealing admin broadcast conversation replies
    # (context.user_data is per-user; if admin is in conversation, handlers will run first)
    if context.user_data.get("broadcast_type"):
        return

    # Save group and user safely
    try:
        execute_db("""INSERT OR IGNORE INTO groups
                  (group_id, group_name) VALUES (?, ?)""",
                  (chat.id, chat.title))
        execute_db("""INSERT OR REPLACE INTO users
                  (user_id, group_id, username)
                  VALUES (?, ?, ?)""",
                  (getattr(user, "id", None), chat.id, getattr(user, "username", None)))
    except Exception as e:
        logger.error(f"DB write error: {e}")

    # Check for URLs
    try:
        if getattr(message, "entities", None):
            if any(getattr(entity, "type", None) == MessageEntity.URL for entity in message.entities):
                if not is_admin(update):
                    try:
                        message.delete()
                    except Exception:
                        pass

                    # preserve original warning text exactly
                    warning_text = f"⚠️ Hi {user.first_name}, URLs are not allowed in this group. Please refrain from sharing links. Thank you! 😊"
                    try:
                        warning_msg = context.bot.send_message(chat_id=chat.id, text=warning_text)
                    except Exception:
                        warning_msg = None

                    # schedule deletion only in groups (original 30 seconds)
                    if warning_msg and getattr(chat, "type", "") != "private":
                        try:
                            context.job_queue.run_once(
                                delete_message,
                                when=30,
                                context={
                                    "chat_id": chat.id,
                                    "message_id": warning_msg.message_id
                                }
                            )
                        except Exception as e:
                            logger.error(f"Failed to schedule warning delete: {e}")

                    log_event(chat.id, getattr(user, "id", None), message.text or "URL in media", "URL")
                    admin_report = f"🚫 Deleted URL from {user.username or user.first_name} in {chat.title}:\nContent: {message.text or 'URL in media'}"
                    try:
                        send_to_admin_text(admin_report)
                    except Exception:
                        pass
                    return
    except Exception as e:
        logger.error(f"URL processing error: {e}")

    # Check banned words
    try:
        if getattr(message, "text", None):
            text = message.text or ""
            if any(word in text.lower() for word in banned_words):
                if not is_admin(update):
                    try:
                        message.delete()
                    except Exception:
                        pass

                    # preserve original banned warning text exactly
                    warning_text = f"⚠️ Hi {user.first_name}, the message contained a banned word. Please be mindful of the group rules. Thank you! 😊"
                    try:
                        warning_msg = context.bot.send_message(chat_id=chat.id, text=warning_text)
                    except Exception:
                        warning_msg = None

                    if warning_msg and getattr(chat, "type", "") != "private":
                        try:
                            context.job_queue.run_once(
                                delete_message,
                                when=30,
                                context={
                                    "chat_id": chat.id,
                                    "message_id": warning_msg.message_id
                                }
                            )
                        except Exception as e:
                            logger.error(f"Failed to schedule banned-word warning delete: {e}")

                    log_event(chat.id, getattr(user, "id", None), message.text, "Banned word")
                    admin_report = f"🚫 Deleted banned word from {user.username or user.first_name} in {chat.title}:\nContent: {message.text}"
                    try:
                        send_to_admin_text(admin_report)
                    except Exception:
                        pass
                    return
    except Exception as e:
        logger.error(f"Banned-word processing error: {e}")

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

        group_list = "\n".join([f"{i+1}. {row[1]} (id:{row[0]})" for i, row in enumerate(groups)])
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

    # Safety: ensure there's a broadcast type selected
    if not btype:
        update.message.reply_text("❌ No broadcast type selected. Please run /broadcast again.")
        return ConversationHandler.END

    # If specific_group and selected_group not set yet => expect a numeric selection
    if btype == "specific_group" and not context.user_data.get("selected_group"):
        # Expect the user to reply with a number
        text = update.message.text or ""
        try:
            idx = int(text.strip()) - 1
            groups = context.user_data.get("groups_list", [])
            if 0 <= idx < len(groups):
                selected_group = groups[idx]
                context.user_data["selected_group"] = selected_group
                # Ask for the message to send now (exact original prompt preserved)
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

    # At this point, we treat update.message as the actual broadcast content
    context.user_data["broadcast_message"] = update.message  # store the full message object

    # Prepare target_info for confirmation
    if btype == "all_users":
        users_count = execute_db("SELECT COUNT(DISTINCT user_id) FROM users")[0][0] or 0
        target_info = f"All Bot Users ({users_count} users)"
    elif btype == "all_groups":
        groups_count = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
        target_info = f"All Groups ({groups_count} groups)"
    else:  # specific_group
        selected_group = context.user_data.get("selected_group")
        target_info = f"Specific Group: {selected_group[1]} (id:{selected_group[0]})"

    # Message preview
    message_preview = ""
    m = update.message
    if getattr(m, "text", None):
        preview = m.text[:200] + ("..." if len(m.text) > 200 else "")
        message_preview = f"Text: {preview}"
    elif getattr(m, "photo", None):
        message_preview = "Photo with caption" if m.caption else "Photo"
    elif getattr(m, "document", None):
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

# Background broadcast runner (non-blocking)
def _serialize_message_for_broadcast(msg):
    if not msg:
        return {}
    data = {}
    if getattr(msg, "text", None):
        data["type"] = "text"
        data["text"] = msg.text
    elif getattr(msg, "photo", None):
        data["type"] = "photo"
        data["file_id"] = msg.photo[-1].file_id
        data["caption"] = msg.caption
    elif getattr(msg, "document", None):
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
        bot.send_message(chat_id=chat_id, text=msg_data.get("text") or " ")

def run_broadcast_job(btype, msg_data, selected_group_id=None, admin_id=None):
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
            time.sleep(0.06)  # small throttle to avoid rate limits
    except Exception as e:
        try:
            bot.send_message(chat_id=admin_id or ADMIN_ID, text=f"❌ Broadcast job crashed: {type(e).__name__}: {e}")
        except Exception:
            pass
        return

    success_rate = (success / total * 100) if total else 0.0
    report = (
        f"✅ **Broadcast Finished**\n\n"
        f"📊 Results:\n"
        f"• ✅ Successfully sent: {success}\n"
        f"• ❌ Failed: {fail}\n"
        f"• 📈 Success rate: {success_rate:.1f}%\n\n"
    )
    if failed_targets:
        report += "Failed examples:\n" + "\n".join(failed_targets[:50])

    try:
        bot.send_message(chat_id=admin_id or ADMIN_ID, text=report)
    except Exception:
        pass

def broadcast_confirm(update: Update, context: CallbackContext):
    if update.message.text is None or update.message.text.lower() != 'confirm':
        update.message.reply_text("❌ Broadcast canceled.")
        # clear conversation data
        for k in ("broadcast_type", "broadcast_message", "groups_list", "selected_group"):
            context.user_data.pop(k, None)
        return ConversationHandler.END

    btype = context.user_data.get("broadcast_type")
    message = context.user_data.get("broadcast_message")
    if not btype or not message:
        update.message.reply_text("❌ Missing broadcast info. Please try again.")
        return ConversationHandler.END

    msg_data = _serialize_message_for_broadcast(message)
    selected_group = context.user_data.get("selected_group")
    selected_group_id = selected_group[0] if selected_group else None

    # Schedule broadcast in background immediately (UTC-aware)
    try:
        scheduler.add_job(run_broadcast_job, args=[btype, msg_data, selected_group_id, update.effective_user.id],
                          next_run_time=datetime.now(pytz.UTC))
    except Exception as e:
        logger.error(f"Failed to schedule broadcast job: {e}")
        update.message.reply_text("❌ Failed to queue broadcast. Try again later.")
        return ConversationHandler.END

    update.message.reply_text("✅ Broadcast queued and will run in background. You will receive a report when it's finished.")

    # cleanup conversation data
    for k in ("broadcast_type", "broadcast_message", "groups_list", "selected_group"):
        context.user_data.pop(k, None)

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
    # remove webhook if exists to avoid getUpdates conflict
    remove_webhook_if_any()

    updater = Updater(TOKEN)
    dp = updater.dispatcher

    # Handlers that should run before the generic message handler
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stats", stats_command))
    dp.add_handler(CommandHandler("reload", reload_banned_words))
    dp.add_handler(ChatMemberHandler(track_join_leave))

    # Track bot membership changes to record groups
    dp.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

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
    logger.info("Bot started polling.")
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