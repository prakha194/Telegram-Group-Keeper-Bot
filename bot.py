# bot.py
# Complete replacement - drop this file in place of your current bot.py

import logging
import os
import sqlite3
import time
from threading import Lock
from datetime import datetime
import pytz

from telegram import Update, MessageEntity, ParseMode, Bot, ChatMember
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    CallbackContext, ChatMemberHandler, ConversationHandler
)
from telegram.error import Unauthorized, BadRequest, TimedOut, TelegramError
from apscheduler.schedulers.background import BackgroundScheduler

# --- CONFIG
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))  # numeric admin id
DB_NAME = "group_stats.db"

# --- LOGGING
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DB helpers
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
        rows = c.fetchall()
        conn.close()
        return rows

# --- load banned words
def load_banned_words():
    if not os.path.exists("banned_words.txt"):
        open("banned_words.txt", "a").close()
    with open("banned_words.txt", "r") as f:
        return [w.strip().lower() for w in f.readlines() if w.strip()]

banned_words = load_banned_words()

# --- create tables
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

# store users who started bot (for direct broadcasts)
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

# --- scheduler with pytz.UTC (prevents timezone error)
scheduler = BackgroundScheduler(timezone=pytz.UTC)
scheduler.start()

# --- helper functions
def safe_username(user):
    if not user:
        return "Unknown"
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = (user.first_name or "").strip()
    return f"{name} ({getattr(user, 'id', 'id?')})" if name else f"{getattr(user, 'id', 'id?')}"

def mention_md(user):
    if not user:
        return "Unknown"
    uid = getattr(user, "id", None)
    name = (user.first_name or "") + (" " + user.last_name if getattr(user, "last_name", None) else "")
    name = name.strip() or f"user_{uid}"
    if uid:
        return f"[{name}](tg://user?id={uid})"
    return name

def is_admin(update: Update):
    user = update.effective_user
    return user and getattr(user, "id", None) == ADMIN_ID

def log_event(group_id, user_id, content, reason):
    execute_db("""INSERT INTO deleted_messages (group_id, user_id, content, reason) VALUES (?, ?, ?, ?)""",
               (group_id, user_id, content, reason))

def send_admin(text):
    try:
        Bot(TOKEN).send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error("Failed sending admin message: %s", e)

# --- ensure no leftover webhook (delete and drop pending updates)
def ensure_no_webhook():
    try:
        Bot(TOKEN).delete_webhook(drop_pending_updates=True)
        logger.info("Deleted any existing webhook (drop_pending_updates=True).")
    except Exception as e:
        logger.warning("Could not delete webhook (may be none): %s", e)

# ================= WELCOME & BOT-ADDED TRACKER =================
def track_my_chat_member(update: Update, context: CallbackContext):
    """
    Called when bot's status in a chat changes (added/removed).
    Records the group when bot is present so option 3 shows groups.
    """
    try:
        if not getattr(update, "chat_member", None):
            return
        member_update = update.chat_member
        # in v13 the ChatMemberHandler provides .chat and .new_chat_member
        chat = getattr(member_update, "chat", None)
        new = getattr(member_update, "new_chat_member", None)
        # If bot was added or promoted, record the group
        if chat and new and getattr(new, "user", None):
            # check if the user in new is the bot
            if getattr(new.user, "id", None) == Bot(TOKEN).get_me().id:
                # bot changed status in chat
                # If bot is member/administrator, store the group
                status = getattr(new, "status", "")
                if status in ("member", "administrator", "creator"):
                    try:
                        execute_db("INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?, ?)",
                                   (chat.id, chat.title or "No Title"))
                        logger.info("Recorded group %s (%s)", chat.title, chat.id)
                    except Exception as e:
                        logger.error("Failed to insert group on bot add: %s", e)
                else:
                    # If bot removed, optionally remove group? We'll keep it for history.
                    logger.info("Bot status in chat %s changed to %s", chat.id, status)
    except Exception as e:
        logger.error("track_my_chat_member error: %s", e)

def welcome_message(update: Update, context: CallbackContext):
    if not update.message or not update.message.new_chat_members:
        return
    chat = update.effective_chat
    for u in update.message.new_chat_members:
        if not u:
            continue
        text = (f"👋 Welcome, {u.first_name or ''} {u.last_name or ''}!\n\n"
                f"• Username: @{u.username or 'NoUsername'}\n"
                f"• ID: {getattr(u, 'id', '')}\n\n"
                "Rules:\n1. No spam\n2. No links\n3. Be respectful")
        try:
            sent = update.message.reply_text(text)
        except Exception as e:
            logger.error("Failed sending welcome: %s", e)
            sent = None
        # schedule delete in groups only
        if getattr(chat, "type", "") != "private" and sent:
            try:
                # reliable scheduling via job_queue
                context.job_queue.run_once(delete_message, when=20, context={"chat_id": chat.id, "message_id": sent.message_id})
            except Exception:
                logger.exception("Failed to schedule welcome deletion")

# ================= COMMANDS =================
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    try:
        # record as bot_user for direct broadcasts
        execute_db("INSERT OR REPLACE INTO bot_users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
                   (user.id, user.username or None, user.first_name or None, user.last_name or None))
    except Exception as e:
        logger.error("Failed to insert bot_user: %s", e)

    msg = (f"Hi {user.first_name or ''}! Bot started. Your ID: {user.id}\n"
           "You will receive broadcasts if admin sends them.")
    update.message.reply_text(msg)

# ================= JOIN/LEAVE =================
def track_join_leave(update: Update, context: CallbackContext):
    event = update.chat_member
    if not event or not event.chat or not getattr(event, "new_chat_member", None):
        return
    new_user = event.new_chat_member.user
    if not new_user or not getattr(new_user, "id", None):
        return
    gid = event.chat.id
    try:
        execute_db("INSERT INTO join_leave_events (group_id, user_id, action) VALUES (?, ?, ?)",
                   (gid, new_user.id, "join" if getattr(event.new_chat_member, "status", "") == "member" else "leave"))
    except Exception:
        pass
    # notify admin
    try:
        Bot(TOKEN).send_message(chat_id=ADMIN_ID, text=f"User {safe_username(new_user)} changed status in {event.chat.title}")
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
        logger.warning("Skipping message - no user info")
        return

    # Don't handle private chats here (group-specific logic only)
    if getattr(chat, "type", "") == "private":
        return

    # If admin is currently in broadcast conversation, let conversation handlers handle it
    if context.user_data.get("broadcast_type"):
        return

    # Always insert groups & users (fixes 0 count)
    try:
        execute_db("INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?, ?)", (chat.id, chat.title or ""))
        execute_db("INSERT OR REPLACE INTO users (user_id, group_id, username) VALUES (?, ?, ?)",
                   (user.id, chat.id, user.username or None))
    except Exception as e:
        logger.error("DB insert error: %s", e)

    # check for URLs
    try:
        ents = getattr(message, "entities", []) or []
        if any(getattr(ent, "type", None) == MessageEntity.URL for ent in ents):
            if not is_admin(update):
                try:
                    message.delete()
                except Exception:
                    pass
                warning_msg = None
                try:
                    warning_msg = context.bot.send_message(chat_id=chat.id, text=f"⚠️ {user.first_name}, links are not allowed.")
                except Exception:
                    pass
                # schedule deletion in groups only
                if warning_msg and getattr(chat, "type", "") != "private":
                    try:
                        context.job_queue.run_once(delete_message, when=20, context={"chat_id": chat.id, "message_id": warning_msg.message_id})
                    except Exception:
                        logger.exception("Failed to schedule delete for URL warning")
                # log + admin report (include username and chat info)
                log_event(chat.id, user.id, message.text or "URL in media", "URL")
                admin_text = (f"🚫 *Deleted URL*\nUser: {safe_username(user)}\nGroup: {chat.title or 'NoTitle'} (id:{chat.id})\n"
                              f"Content: {message.text or 'URL in media'}")
                send_admin(admin_text)
                return
    except Exception:
        logger.exception("URL check failed")

    # check banned words
    try:
        txt = getattr(message, "text", "") or ""
        if txt and any(w in txt.lower() for w in banned_words):
            if not is_admin(update):
                try:
                    message.delete()
                except Exception:
                    pass
                warning_msg = None
                try:
                    warning_msg = context.bot.send_message(chat_id=chat.id, text=f"⚠️ {user.first_name}, your message contains banned content.")
                except Exception:
                    pass
                if warning_msg and getattr(chat, "type", "") != "private":
                    try:
                        context.job_queue.run_once(delete_message, when=20, context={"chat_id": chat.id, "message_id": warning_msg.message_id})
                    except Exception:
                        logger.exception("Failed scheduling ban-warning deletion")
                log_event(chat.id, user.id, txt, "Banned word")
                admin_text = (f"🚫 *Deleted Banned Message*\nUser: {safe_username(user)}\nGroup: {chat.title or 'NoTitle'} (id:{chat.id})\n"
                              f"Content: {txt}")
                send_admin(admin_text)
                return
    except Exception:
        logger.exception("Banned-word processing failed")

# ================= STATS =================
def stats_command(update: Update, context: CallbackContext):
    total_deleted = execute_db("SELECT COUNT(*) FROM deleted_messages")[0][0] or 0
    total_groups = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
    total_users = execute_db("SELECT COUNT(DISTINCT user_id) FROM users")[0][0] or 0
    reason_stats = execute_db("SELECT reason, COUNT(*) FROM deleted_messages GROUP BY reason")
    breakdown = "\n".join([f"• {r[0]}: {r[1]}" for r in reason_stats]) if reason_stats else "• No deletions yet"
    text = (f"📊 Live Stats\n\n👥 Groups: {total_groups}\n👤 Total Users: {total_users}\n🗑️ Total Deleted: {total_deleted}\n\nBreakdown:\n{breakdown}")
    update.message.reply_text(text)

# ================= RELOAD banned words =================
def reload_banned_words(update: Update, context: CallbackContext):
    if not is_admin(update):
        update.message.reply_text("❌ Admin only.")
        return
    global banned_words
    banned_words = load_banned_words()
    update.message.reply_text(f"✅ Banned words reloaded ({len(banned_words)})")

# ================= BROADCAST (conversation handlers) =================
BROADCAST_TYPE, BROADCAST_MESSAGE, BROADCAST_CONFIRM = range(3)

def broadcast(update: Update, context: CallbackContext):
    if not is_admin(update):
        update.message.reply_text("❌ Admin only.")
        return ConversationHandler.END
    update.message.reply_text("Broadcast options:\n1) All bot users\n2) All groups\n3) Specific group\nReply with 1/2/3")
    return BROADCAST_TYPE

def broadcast_type(update: Update, context: CallbackContext):
    choice = (update.message.text or "").strip()
    if choice == "1":
        context.user_data["broadcast_type"] = "all_bot_users"
        count = execute_db("SELECT COUNT(*) FROM bot_users")[0][0] or 0
        update.message.reply_text(f"Selected all bot users ({count}) — send the message to broadcast.")
        return BROADCAST_MESSAGE
    if choice == "2":
        context.user_data["broadcast_type"] = "all_groups"
        count = execute_db("SELECT COUNT(*) FROM groups")[0][0] or 0
        update.message.reply_text(f"Selected all groups ({count}) — send the message to broadcast.")
        return BROADCAST_MESSAGE
    if choice == "3":
        context.user_data["broadcast_type"] = "specific_group"
        groups = execute_db("SELECT group_id, group_name FROM groups")
        if not groups:
            update.message.reply_text("No known groups. Add the bot to a group or send a message in a group with bot present.")
            return ConversationHandler.END
        lines = [f"{i+1}. {g[1] or 'NoTitle'} (id:{g[0]})" for i,g in enumerate(groups)]
        update.message.reply_text("Available groups:\n" + "\n".join(lines) + "\nReply with group number")
        context.user_data["groups_list"] = groups
        return BROADCAST_MESSAGE
    update.message.reply_text("Invalid option. Reply 1, 2 or 3")
    return BROADCAST_TYPE

def broadcast_message(update: Update, context: CallbackContext):
    btype = context.user_data.get("broadcast_type")
    if not btype:
        update.message.reply_text("No selection. Run /broadcast.")
        return ConversationHandler.END
    # if specific group selection expected
    if btype == "specific_group" and not context.user_data.get("selected_group"):
        try:
            idx = int((update.message.text or "").strip()) - 1
            groups = context.user_data.get("groups_list", [])
            if 0 <= idx < len(groups):
                sel = groups[idx]
                context.user_data["selected_group"] = sel
                update.message.reply_text(f"Selected: {sel[1]} (id:{sel[0]}). Now send the message to broadcast.")
                return BROADCAST_MESSAGE
            else:
                update.message.reply_text("Invalid number. Try again.")
                return BROADCAST_MESSAGE
        except ValueError:
            update.message.reply_text("Please reply with the group number.")
            return BROADCAST_MESSAGE

    # treat current message as the content
    context.user_data["broadcast_message"] = update.message
    # preview
    m = update.message
    if m.text:
        preview = m.text[:200] + ("..." if len(m.text)>200 else "")
        typ = f"Text: {preview}"
    elif m.photo:
        typ = "Photo"
    elif m.document:
        typ = f"Document: {m.document.file_name}"
    else:
        typ = "Media"
    # prepare target info
    if btype == "all_bot_users":
        target_info = f"All bot users ({execute_db('SELECT COUNT(*) FROM bot_users')[0][0] or 0})"
    elif btype == "all_groups":
        target_info = f"All groups ({execute_db('SELECT COUNT(*) FROM groups')[0][0] or 0})"
    else:
        sel = context.user_data.get("selected_group")
        target_info = f"{sel[1]} (id:{sel[0]})"
    update.message.reply_text(f"Preview:\nTarget: {target_info}\nType: {typ}\n\nType 'confirm' to send or 'cancel'")
    return BROADCAST_CONFIRM

def _serialize_message_for_broadcast(msg):
    if not msg:
        return {}
    if getattr(msg, "text", None):
        return {"type":"text","text":msg.text}
    if getattr(msg, "photo", None):
        return {"type":"photo","file_id":msg.photo[-1].file_id,"caption":msg.caption}
    if getattr(msg, "document", None):
        return {"type":"document","file_id":msg.document.file_id,"caption":msg.caption}
    return {"type":"text","text":getattr(msg,"caption","") or " "}

def _send_serialized(bot, chat_id, data):
    t = data.get("type")
    if t == "text":
        bot.send_message(chat_id=chat_id, text=data.get("text"))
    elif t == "photo":
        bot.send_photo(chat_id=chat_id, photo=data.get("file_id"), caption=data.get("caption"))
    elif t == "document":
        bot.send_document(chat_id=chat_id, document=data.get("file_id"), caption=data.get("caption"))
    else:
        bot.send_message(chat_id=chat_id, text=data.get("text") or " ")

def run_broadcast_job(btype, msg_data, selected_group_id=None, admin_id=None):
    bot = Bot(TOKEN)
    success = 0
    fail = 0
    failures = []
    try:
        if btype == "all_bot_users":
            rows = execute_db("SELECT user_id FROM bot_users")
            targets = [r[0] for r in rows]
        elif btype == "all_groups":
            rows = execute_db("SELECT group_id FROM groups")
            targets = [r[0] for r in rows]
        elif btype == "specific_group":
            targets = [selected_group_id] if selected_group_id else []
        else:
            targets = []
        total = len(targets)
        for t in targets:
            try:
                _send_serialized(bot, t, msg_data)
                success += 1
            except Unauthorized:
                fail += 1
                failures.append(f"{t} (blocked/unauthorized)")
                execute_db("INSERT INTO failed_deliveries (target_id, target_name, reason) VALUES (?, ?, ?)",
                           (t, str(t), "Unauthorized"))
            except TimedOut:
                fail += 1
                failures.append(f"{t} (timeout)")
                execute_db("INSERT INTO failed_deliveries (target_id, target_name, reason) VALUES (?, ?, ?)",
                           (t, str(t), "TimedOut"))
            except TelegramError as e:
                fail += 1
                failures.append(f"{t} ({type(e).__name__})")
                execute_db("INSERT INTO failed_deliveries (target_id, target_name, reason) VALUES (?, ?, ?)",
                           (t, str(t), type(e).__name__))
            except Exception as e:
                fail += 1
                failures.append(f"{t} ({type(e).__name__})")
                execute_db("INSERT INTO failed_deliveries (target_id, target_name, reason) VALUES (?, ?, ?)",
                           (t, str(t), type(e).__name__))
            time.sleep(0.08)  # small throttle
    except Exception as e:
        try:
            Bot(TOKEN).send_message(chat_id=admin_id or ADMIN_ID, text=f"❌ Broadcast crashed: {type(e).__name__}: {e}")
        except Exception:
            pass
        return
    success_rate = (success/total*100) if total else 0.0
    report = (f"✅ Broadcast Finished\nType: {btype}\nTotal: {total}\nSuccess: {success}\nFailed: {fail}\nSuccess rate: {success_rate:.1f}%")
    if failures:
        report += "\n\nFailed sample:\n" + "\n".join(failures[:50])
    try:
        Bot(TOKEN).send_message(chat_id=admin_id or ADMIN_ID, text=report)
    except Exception:
        pass

def broadcast_confirm(update: Update, context: CallbackContext):
    if not update.message or not getattr(update.message, "text", "") or update.message.text.lower() != "confirm":
        update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    btype = context.user_data.get("broadcast_type")
    message = context.user_data.get("broadcast_message")
    if not btype or not message:
        update.message.reply_text("Missing data. Try again.")
        return ConversationHandler.END
    data = _serialize_message_for_broadcast(message)
    sel = context.user_data.get("selected_group")
    selected_group_id = sel[0] if sel else None
    try:
        # schedule immediately (UTC)
        scheduler.add_job(run_broadcast_job, args=[btype, data, selected_group_id, update.effective_user.id],
                          next_run_time=datetime.now(pytz.UTC))
    except Exception as e:
        logger.error("Failed to schedule broadcast: %s", e)
        update.message.reply_text("Failed to queue broadcast; try later.")
        return ConversationHandler.END
    update.message.reply_text("Queued. Admin will get a report when finished.")
    for k in ("broadcast_type","broadcast_message","groups_list","selected_group"):
        context.user_data.pop(k, None)
    return ConversationHandler.END

# ================= delete message =================
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

# ================= errors =================
def error_handler(update: Update, context: CallbackContext):
    logger.error("Error: %s", context.error)

# ================= main =================
def main():
    # Remove any webhook + drop pending updates (fixes getUpdates conflict)
    ensure_no_webhook()

    updater = Updater(TOKEN)
    dp = updater.dispatcher

    # core handlers (order matters)
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stats", stats_command))
    dp.add_handler(CommandHandler("reload", reload_banned_words))
    dp.add_handler(ChatMemberHandler(track_join_leave))
    # track bot's membership changes
    dp.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # broadcast conv handler
    conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast)],
        states={
            BROADCAST_TYPE: [MessageHandler(Filters.text & ~Filters.command, broadcast_type)],
            BROADCAST_MESSAGE: [MessageHandler(Filters.all & ~Filters.command, broadcast_message)],
            BROADCAST_CONFIRM: [MessageHandler(Filters.text & ~Filters.command, broadcast_confirm)],
        },
        fallbacks=[]
    )
    dp.add_handler(conv)

    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_message))
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
        return "Bot is running", 200
    import threading
    PORT = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=PORT, debug=False), daemon=True).start()
    main()