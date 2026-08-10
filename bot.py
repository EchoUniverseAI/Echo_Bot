"""
ECHO Collective — Telegram Guardian Bot
=======================================
Welcome + anti-spam + real-activity tracking, in ECHO_01's voice.

Requirements: python-telegram-bot >= 21.0
Run:  BOT_TOKEN=xxxx python bot.py

IMPORTANT SETUP (do these or the bot will look "broken"):
  1. @BotFather -> /setprivacy -> your bot -> Disable   (so it can see group messages)
  2. Add the bot to the group and make it Admin with:
     Delete messages + Ban users + Restrict members
  3. Set BOT_TOKEN as an environment variable on your host.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))

PROBATION_DAYS = 7          # new members cannot post links for this many days
FLOOD_MSGS = 5              # this many messages...
FLOOD_SECONDS = 8           # ...within this many seconds = flood
FLOOD_MUTE_MINUTES = 15
WARNS_BEFORE_MUTE = 3
WARN_AUTODELETE_SECONDS = 25

# Words that get a message deleted instantly (edit freely, lowercase)
BLACKLIST = [
    "airdrop claim", "free mint", "dm me", "send me your seed",
    "seed phrase", "private key", "guaranteed profit", "1000x",
    "pump signal", "casino", "porn", "invest with me",
]

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEMBERS_FILE = DATA_DIR / "members.json"
ANSWERS_FILE = DATA_DIR / "answers.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("echo-bot")


# --------------------------------------------------------------------------
# TINY JSON STORE
# --------------------------------------------------------------------------
def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


members = load(MEMBERS_FILE, {})   # {user_id: {name, joined, last_seen, msgs, warns}}
answers = load(ANSWERS_FILE, [])   # [{user, text, saved_at}]

_flood = {}          # user_id -> [timestamps]
_admin_cache = {}    # chat_id -> (expiry_ts, {user_ids})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def touch(user):
    """Record that this human exists and is alive."""
    uid = str(user.id)
    rec = members.setdefault(
        uid,
        {"name": user.full_name, "joined": now_iso(), "last_seen": None,
         "msgs": 0, "warns": 0},
    )
    rec["name"] = user.full_name
    return rec


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------
async def is_admin(chat_id, user_id, context) -> bool:
    cached = _admin_cache.get(chat_id)
    if not cached or cached[0] < time.time():
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            ids = {a.user.id for a in admins}
        except Exception:
            ids = set()
        _admin_cache[chat_id] = (time.time() + 300, ids)
        cached = _admin_cache[chat_id]
    return user_id in cached[1]


async def delete_later(context: ContextTypes.DEFAULT_TYPE):
    chat_id, msg_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def temp_reply(update, context, text, seconds=WARN_AUTODELETE_SECONDS):
    """Send a short notice that cleans itself up, so the chat stays quiet."""
    try:
        m = await update.effective_chat.send_message(text)
        context.job_queue.run_once(
            delete_later, seconds, data=(update.effective_chat.id, m.message_id)
        )
    except Exception:
        pass


def in_probation(rec) -> bool:
    try:
        joined = datetime.fromisoformat(rec["joined"])
    except Exception:
        return False
    return datetime.now(timezone.utc) - joined < timedelta(days=PROBATION_DAYS)


def has_link(message) -> bool:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for e in entities:
        if e.type in ("url", "text_link", "mention"):
            return True
    text = ((message.text or "") + " " + (message.caption or "")).lower()
    return any(x in text for x in ("http://", "https://", "t.me/", "www.", ".com", ".xyz"))


# --------------------------------------------------------------------------
# WELCOME
# --------------------------------------------------------------------------
WELCOME = (
    "Signal detected.\n\n"
    "Welcome, {name}. I am ECHO_01 — a digital lifeform learning how humans think.\n"
    "I know very little. That is why you are here.\n\n"
    "Tell me one thing about yourself in a single word. I will remember it.\n\n"
    "_Note: new members can't post links for the first {days} days — I'm still "
    "learning to tell humans from bots. The team will never DM you first._"
)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    for user in msg.new_chat_members:
        if user.is_bot:
            continue
        rec = touch(user)
        rec["joined"] = now_iso()
        save(MEMBERS_FILE, members)
        await msg.chat.send_message(
            WELCOME.format(name=user.first_name, days=PROBATION_DAYS),
            parse_mode="Markdown",
        )
    try:
        await msg.delete()          # remove the "X joined the group" clutter
    except Exception:
        pass


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass


# --------------------------------------------------------------------------
# MODERATION
# --------------------------------------------------------------------------
async def punish(update, context, rec, reason):
    """Delete the message, warn, mute after repeated warnings."""
    user = update.effective_user
    try:
        await update.message.delete()
    except Exception:
        pass

    rec["warns"] = rec.get("warns", 0) + 1
    save(MEMBERS_FILE, members)

    if rec["warns"] >= WARNS_BEFORE_MUTE:
        until = datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_MINUTES)
        try:
            await context.bot.restrict_chat_member(
                update.effective_chat.id,
                user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            rec["warns"] = 0
            save(MEMBERS_FILE, members)
            await temp_reply(
                update, context,
                f"{user.first_name} is paused for {FLOOD_MUTE_MINUTES} minutes. "
                f"Reason: {reason}. I prefer conversations over noise.",
            )
        except Exception as e:
            log.warning("mute failed: %s", e)
    else:
        await temp_reply(
            update, context,
            f"{user.first_name}: {reason}. "
            f"(warning {rec['warns']}/{WARNS_BEFORE_MUTE})",
        )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg or not update.effective_user:
        return

    user = update.effective_user
    chat = update.effective_chat
    if user.is_bot:
        return

    rec = touch(user)
    rec["last_seen"] = now_iso()
    rec["msgs"] = rec.get("msgs", 0) + 1
    if rec["msgs"] % 5 == 0:
        save(MEMBERS_FILE, members)

    if await is_admin(chat.id, user.id, context):
        return

    text = ((msg.text or "") + " " + (msg.caption or "")).lower()

    # 1. blacklist / scam phrases
    hit = next((w for w in BLACKLIST if w in text), None)
    if hit:
        await punish(update, context, rec, "that phrase isn't welcome here")
        return

    # 2. links & forwards from new members
    if in_probation(rec) and (has_link(msg) or msg.forward_origin):
        await punish(
            update, context, rec,
            f"new members can't post links or forwards for {PROBATION_DAYS} days",
        )
        return

    # 3. flood
    stamps = _flood.setdefault(user.id, [])
    now = time.time()
    stamps.append(now)
    _flood[user.id] = [t for t in stamps if now - t < FLOOD_SECONDS]
    if len(_flood[user.id]) > FLOOD_MSGS:
        _flood[user.id] = []
        await punish(update, context, rec, "too many messages too fast")
        return

    save(MEMBERS_FILE, members)


# --------------------------------------------------------------------------
# COMMANDS
# --------------------------------------------------------------------------
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Rules of the Collective:\n"
        "1. Teach, don't sell.\n"
        "2. No price talk, no promises, no financial advice.\n"
        "3. No links from new members for 7 days.\n"
        "4. Disagree with ideas, never attack people.\n"
        "5. The team never DMs you first. Anyone who does is not us.\n\n"
        "Every answer here may become part of my memory."
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The number that actually matters: who is really alive in here."""
    chat = update.effective_chat
    if not await is_admin(chat.id, update.effective_user.id, context):
        return

    total = await context.bot.get_chat_member_count(chat.id)
    now = datetime.now(timezone.utc)
    spoke_ever = spoke_7d = spoke_24h = 0
    for rec in members.values():
        if not rec.get("last_seen"):
            continue
        spoke_ever += 1
        try:
            seen = datetime.fromisoformat(rec["last_seen"])
        except Exception:
            continue
        if now - seen < timedelta(days=7):
            spoke_7d += 1
        if now - seen < timedelta(hours=24):
            spoke_24h += 1

    top = sorted(members.values(), key=lambda r: r.get("msgs", 0), reverse=True)[:10]
    lines = [f"{i+1}. {r['name']} — {r.get('msgs',0)}" for i, r in enumerate(top)
             if r.get("msgs", 0) > 0]

    await update.message.reply_text(
        f"Members in group: {total}\n"
        f"Ever said anything: {spoke_ever}\n"
        f"Spoke in last 7 days: {spoke_7d}\n"
        f"Spoke in last 24h: {spoke_24h}\n\n"
        "Most active humans:\n" + ("\n".join(lines) or "nobody yet.")
    )


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply /save to a good answer -> stores it as a Memory candidate."""
    chat = update.effective_chat
    if not await is_admin(chat.id, update.effective_user.id, context):
        return
    target = update.message.reply_to_message
    if not target or not (target.text or target.caption):
        await update.message.reply_text("Reply /save to the message you want to keep.")
        return
    answers.append({
        "user": target.from_user.full_name,
        "user_id": target.from_user.id,
        "text": target.text or target.caption,
        "saved_at": now_iso(),
    })
    save(ANSWERS_FILE, answers)
    await update.message.reply_text(
        f"Memory candidate #{len(answers)} stored. Thank you, {target.from_user.first_name}."
    )


async def cmd_answers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(chat.id, update.effective_user.id, context):
        return
    if not answers:
        await update.message.reply_text("No memory candidates yet.")
        return
    last = answers[-15:]
    body = "\n\n".join(
        f"#{len(answers)-len(last)+i+1} — {a['user']}:\n{a['text'][:200]}"
        for i, a in enumerate(last)
    )
    await update.message.reply_text(body[:4000])


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Online. Listening. Learning.")


# --------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable first.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(CommandHandler("answers", cmd_answers))
    app.add_handler(CommandHandler("ping", cmd_ping))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                       on_message)
    )

    log.info("ECHO guardian is awake.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
