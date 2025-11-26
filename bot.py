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

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Thread-safe DB
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

# load banned words
def load_banned_words():
    if not os.path.exists("banned_words.txt"):
        open("banned_words.txt", "a").close()
    with open("banned_words.txt", "r") as f:
        return [w.strip().lower() for w in f.readlines() if w.strip()]

banned_words = load_banned_words()

# DB tables
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

execute_db("""CREATE TABLE IF NOT EXISTS failed_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    target_name TEXT,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

# Scheduler with pytz timezone (fixes error)
scheduler = BackgroundScheduler(timezone=pytz.UTC)
scheduler.start()

# Helpers
def is_admin(update: Update):
    user = update.effective_user
    return user and getattr(user, "id", None) == ADMIN_ID

def log_event(group_id, user_id, content, reason):
    execute_db("""INSERT INTO deleted_messages (group_id, user_id, content, reason) VALUES (?, ?, ?, ?)""",
               (group_id, user_id, content, reason))

def send_to_admin_text(text):
    try:
        Bot(TOKEN).send_message(chat_id=ADMIN_ID, text=text, parse_mode=None)
    except Exception as e:
        logger.error(f"Failed to send admin text: {e}")

def mention_md(user):
    if not user:
        return "Unknown"
    uid = getattr(user, "id", None)
    name = (user.first_name or "") + (" " + user.last_name if getattr(user, "last_name", None) else "")
    if uid:
        return f"[{name.strip()}](tg://user?id={uid})"
    return name.strip()

def safe_username(user):
    if not user:
        return "Unknown"
    return f"@{user.username}" if getattr(user, "username", None) else f"{user.first_name or 'User'} ({user.id})"

def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return getattr(member, "status", "") in ("administrator", "creator")
    except Exception:
        return False

# ================= WELCOME =================
def welcome_message(update: Update, context: CallbackContext):
    if not update.message or not update.message.new_chat_members:
        return
    chat = update.effective_chat
    for user in update.message.new_chat_members:
        if not user:
            continue
        username = user.username or "NoUsername"
        first = user.first_name or ""
        last = user.last_name or ""
        uid = getattr(user, "id", None)
        join_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        welcome_text = (
            f"👋 Welcome, {first} {last}!\n\n"
            f"📝 User Details:\n"
            f"• Username: @{username}\n"
            f"• User ID: {uid}\n"
            f"• Join Date: {join_date}\n\n"
            "🤖 Rules:\n"
            "1. 🚫 No spam or banned words.\n"
            "2. 🔗 No URLs allowed.\n"
            "3. 👀 Be respectful to others!"
        )
        try:
            sent = update.message.reply_text(welcome_text)
        except Exception as e:
            logger.error(f"Welcome send failed: {e}")
            sent = None

        # schedule deletion only for groups (not private)
        if getattr(chat, "type", "") != "private" and sent:
            try:
                scheduler.add_job(
                    func=lambda chat_id=chat.id, msg_id=sent.message_id: Bot(TOKEN).delete_message(chat_id=chat_id, message_id=msg_id),
                    next_run_time=datetime.now(pytz.UTC) + pytz.UTC.localize(datetime.utcnow()).utcoffset(),
                    trigger='date',
                )
            except Exception:
                # fallback to job_queue if available in context
                try:
                    context.job_queue.run_once(delete_message, when=20,
                                               context={"chat_id": chat.id, "message_id": sent.message_id})
                except Exception:
                    pass

# ================= START =================
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    first = user.first_name or "No"
    last = user.last_name or "Name"
    uname = user.username or "NoUsername"
    uid = getattr(user, "id", None)
    msg = (
        f"👋 Welcome, {first} {last}!\n\n"
        f"📝 User Details:\n"
        f"• Username: @{uname}\n"
        f"• User ID: {uid}\n\n"
        "🤖 Bot Features:\n"
        "1. 🚫 Auto-delete URLs and banned words.\n"
        "2. 📊 Provide live group analytics.\n"
        "3. 📢 Broadcast messages to all groups.\n"
        "4. 👋 Greet new members with their details!"
    )
    update.message.reply_text(msg)

# ================= JOIN/LEAVE =================
def track_join_leave(update: Update, context: CallbackContext):
    event = update.chat_member
    if not event or not event.chat or not getattr(event, "new_chat_member", None):
        return
    new_user = event.new_chat_member.user
    if not new_user or not getattr(new_user, "id", None):
        return
    group_id = event.chat.id
    user_id = new_user.id
    username = new_user.username or "Unknown"
    action = "join" if event.new_chat_member.status == "member" else "leave"
    try:
        execute_db("INSERT INTO join_leave_events (group_id, user_id, action) VALUES (?, ?, ?)",
                   (group_id, user_id, action))
    except Exception as e:
        logger.error(f"DB insert join_leave failed: {e}")
    try:
        Bot(TOKEN).send_message(chat_id=ADMIN_ID,
                                text=f"👤 {username} {action}ed group {event.chat.title}")
    except Exception:
        pass

# ================= MESSAGE HANDLER =================
def message_handler(update: Update, context: CallbackContext):
    message = update.effective_message
    if not message or not message.chat:
        return

    user = update.effective_user
    chat = update.effective_chat

    if not user or not getattr(user, "id", None):
        logger.warning("No user info — skipping")
        return

    # do not process messages in private for group logic
    if getattr(chat, "type", "") == "private":
        return

    # Avoid stealing messages during active admin broadcast conversation
    if context.user_data.get("broadcast_type"):
        return

    # Always save group+user info so counts work
    try:
        execute_db("INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?, ?)", (chat.id, chat.title))
        execute_db("INSERT OR REPLACE INTO users (user_id, group_id, username) VALUES (?, ?, ?)",
                   (user.id, chat.id, user.username))
    except Exception as e:
        logger.error(f"DB save error: {e}")

    # Check URLs
    try:
        entities = getattr(message, "entities", []) or []
        if any(getattr(ent, "type", None) == MessageEntity.URL for ent in entities):
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

                # schedule delete only in groups and if we have sent message
                if getattr(chat, "type", "") != "private" and warning_msg:
                    try:
                        context.job_queue.run_once(delete_message, when=20,
                                                   context={"chat_id": chat.id, "message_id": warning_msg.message_id})
                    except Exception:
                        pass

                # log & admin report (include username + id)
                log_event(chat.id, user.id, message.text or "URL in media", "URL")
                admin_report = (f"🚫 Deleted URL from {safe_username(user)} in '{chat.title}' (id:{chat.id}):\n"
                                f"Content: {message.text or 'URL in media'}")
                send_to_admin_text(admin_report)
                return
    except Exception as e:
        logger.error(f"URL check error: {e}")

    # Check banned words
    try:
        txt = getattr(message, "text", "") or ""
        if txt and any(word in txt.lower() for word in banned_words):
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
                        context.job_queue.run_once(delete_message, when=20,
                                                   context={"chat_id": chat.id, "message_id": warning_msg.message_id})
                    except Exception:
                        pass

                log_event(chat.id, user.id, txt, "Banned word")
                admin_report = (f"🚫 Deleted banned word from {safe_username(user)} in '{chat.title}' (id:{chat.id}):\n"
                                f"Content: {txt}")
                send_to_admin_text(admin_report)
                return
    except Exception as e:
        logger.error(f"Banned-word check error: {e}")

# ================= STATS =================
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

# ================= RELOAD BANNED =================
def reload_banned_words(update: Update, context: CallbackContext):
    if not is_admin(update):
        update.message.reply_text("❌ This command is for admin only.")
        return
    global banned_words
    banned_words = load_banned_words()
    update.message.reply_text(f"✅ Banned words reloaded! Loaded {len(banned_words)} words.")

# ================= BROADCAST =================
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
        update.message.reply_text(f"✅ Selected: All Bot Users ({users_count} users)\n\nSend the message you want to broadcast.")
        return BROADCAST_MESSAGE
    elif choice == "2":
        context.user_data["broadcast_type"] = "all_groups"
        groups_count = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
        update.message.reply_text(f"✅ Selected: All Groups ({groups_count} groups)\n\nSend the message you want to broadcast.")
        return BROADCAST_MESSAGE
    elif choice == "3":
        context.user_data["broadcast_type"] = "specific_group"
        groups = execute_db("SELECT group_id, group_name FROM groups")
        if not groups:
            update.message.reply_text("❌ No groups found in database.")
            return ConversationHandler.END
        group_list = "\n".join([f"{i+1}. {row[1]} (id:{row[0]})" for i, row in enumerate(groups)])
        update.message.reply_text(f"📋 **Available Groups:**\n\n{group_list}\n\nPlease reply with group number:")
        context.user_data["groups_list"] = groups
        return BROADCAST_MESSAGE
    else:
        update.message.reply_text("❌ Invalid choice. Enter 1, 2, or 3:")
        return BROADCAST_TYPE

def broadcast_message(update: Update, context: CallbackContext):
    btype = context.user_data.get("broadcast_type")
    if not btype:
        update.message.reply_text("❌ No broadcast type selected. Run /broadcast.")
        return ConversationHandler.END
    # group selection
    if btype == "specific_group" and not context.user_data.get("selected_group"):
        text = update.message.text or ""
        try:
            idx = int(text.strip()) - 1
            groups = context.user_data.get("groups_list", [])
            if 0 <= idx < len(groups):
                selected_group = groups[idx]
                context.user_data["selected_group"] = selected_group
                update.message.reply_text(f"✅ Selected group: {selected_group[1]}\nSend the message for this group.")
                return BROADCAST_MESSAGE
            else:
                update.message.reply_text("❌ Invalid group number. Try again:")
                return BROADCAST_MESSAGE
        except ValueError:
            update.message.reply_text("❌ Reply with the group number:")
            return BROADCAST_MESSAGE
    # treat incoming as broadcast content
    context.user_data["broadcast_message"] = update.message
    # preview and confirm
    if btype == "all_users":
        target_info = f"All Bot Users ({execute_db('SELECT COUNT(DISTINCT user_id) FROM users')[0][0] or 0})"
    elif btype == "all_groups":
        target_info = f"All Groups ({execute_db('SELECT COUNT(*) FROM groups')[0][0] or 0})"
    else:
        sel = context.user_data.get("selected_group")
        target_info = f"Specific Group: {sel[1]} (id:{sel[0]})"
    m = update.message
    if m.text:
        preview = m.text[:300] + ("..." if len(m.text) > 300 else "")
        message_preview = f"Text: {preview}"
    elif m.photo:
        message_preview = "Photo with caption" if m.caption else "Photo"
    elif m.document:
        message_preview = f"Document: {m.document.file_name}"
    else:
        message_preview = "Media"
    confirm_text = (f"📢 **Broadcast Confirmation**\n\n"
                    f"🎯 Target: {target_info}\n"
                    f"📝 Message Type: {message_preview}\n\n"
                    "Type 'confirm' to send or 'cancel' to abort:")
    update.message.reply_text(confirm_text, parse_mode=ParseMode.MARKDOWN)
    return BROADCAST_CONFIRM

def _serialize_message_for_broadcast(msg):
    if not msg:
        return {}
    if msg.text:
        return {"type": "text", "text": msg.text}
    if msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption}
    if msg.document:
        return {"type": "document", "file_id": msg.document.file_id, "caption": msg.caption}
    return {"type": "text", "text": getattr(msg, "caption", "") or " "}

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
            # small throttle to avoid hitting limits
            time.sleep(0.05)
    except Exception as e:
        try:
            bot.send_message(chat_id=admin_id or ADMIN_ID, text=f"❌ Broadcast job crashed: {type(e).__name__}: {e}")
        except Exception:
            pass
        return
    success_rate = (success / total * 100) if total else 0.0
    # full report (limited sample of failed targets to avoid giant message)
    report = (f"✅ Broadcast Finished\n\nTarget type: {btype}\nTotal: {total}\nSuccess: {success}\nFailed: {fail}\nSuccess rate: {success_rate:.1f}%\n")
    if failed_targets:
        report += "\nFailed examples (first 50):\n" + "\n".join(failed_targets[:50])
    try:
        bot.send_message(chat_id=admin_id or ADMIN_ID, text=report)
    except Exception:
        pass

def broadcast_confirm(update: Update, context: CallbackContext):
    if not update.message or not update.message.text or update.message.text.lower() != 'confirm':
        update.message.reply_text("❌ Broadcast canceled.")
        return ConversationHandler.END
    btype = context.user_data.get("broadcast_type")
    message = context.user_data.get("broadcast_message")
    if not btype or not message:
        update.message.reply_text("❌ Missing broadcast info. Try again.")
        return ConversationHandler.END
    msg_data = _serialize_message_for_broadcast(message)
    selected_group = context.user_data.get("selected_group")
    selected_group_id = selected_group[0] if selected_group else None
    # schedule immediate job with pytz-aware time
    try:
        scheduler.add_job(run_broadcast_job, args=[btype, msg_data, selected_group_id, update.effective_user.id],
                          next_run_time=datetime.now(pytz.UTC))
    except Exception as e:
        logger.error(f"Failed to schedule broadcast job: {e}")
        update.message.reply_text("❌ Failed to queue broadcast. Try again later.")
        return ConversationHandler.END
    update.message.reply_text("✅ Broadcast queued; report will be sent to admin when finished.")
    # cleanup
    for k in ("broadcast_type", "broadcast_message", "groups_list", "selected_group"):
        context.user_data.pop(k, None)
    return ConversationHandler.END

# ================= DELETE MESSAGE =================
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

# ================= ERROR =================
def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Error: {context.error}")

# ================= MAIN =================
def main():
    # remove webhook if exists to avoid conflict with polling
    try:
        Bot(TOKEN).delete_webhook()
    except Exception:
        pass

    updater = Updater(TOKEN)
    dp = updater.dispatcher

    # handlers before generic
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stats", stats_command))
    dp.add_handler(CommandHandler("reload", reload_banned_words))
    dp.add_handler(ChatMemberHandler(track_join_leave))

    # broadcast conv handler
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

    # welcome handler
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_message))

    # generic catch-all (last)
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