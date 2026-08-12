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
import random
import re
import uuid
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
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

PROBATION_HOURS = 48        # links from members newer than this go to approval
ADMIN_IDS = [int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_CHAT_ID", ""))]
ADMIN_CHAT_ID = ADMIN_IDS[0] if ADMIN_IDS else 0  # first one is primary
PENDING_EXPIRY_HOURS = 24   # unreviewed link requests expire after this
DAILY_QUESTION_HOUR = 18    # local hour (UTC) to post the daily question
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0"))  # for the daily question

# Links from these domains post immediately, no review needed.
SAFE_DOMAINS = {
    "echouniverse.ai", "echo-games.netlify.app",
    "t.me", "telegram.me", "telegram.org",
    "x.com", "twitter.com", "youtube.com", "youtu.be",
    "instagram.com", "facebook.com", "fb.watch", "tiktok.com",
    "hey.xyz", "github.com", "medium.com",
}

# Links from these are deleted on sight — never reach you.
BLOCKED_DOMAINS = {
    "bit.ly", "tinyurl.com", "goo.gl", "t.co", "is.gd", "cutt.ly",
    "shorturl.at", "rb.gy", "rebrand.ly", "ow.ly", "buff.ly",
    "grabify.link", "iplogger.org", "blasze.com",
}

# Phishing patterns anywhere in the URL — deleted on sight.
BAD_URL_PATTERNS = [
    "connect-wallet", "connectwallet", "claim-airdrop", "claimairdrop",
    "free-mint", "freemint", "validate-wallet", "wallet-verify",
    "seed-phrase", "restore-wallet", "sync-wallet", "airdrop-claim",
]
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
PENDING_FILE = DATA_DIR / "pending.json"

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
pending = load(PENDING_FILE, {})   # {token: {chat_id, user_id, name, text, created}}

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
    return datetime.now(timezone.utc) - joined < timedelta(hours=PROBATION_HOURS)


def extract_urls(message) -> list:
    """Every URL in the message, from entities and raw text."""
    urls = []
    text = (message.text or "") + " " + (message.caption or "")
    for e in list(message.entities or []) + list(message.caption_entities or []):
        if e.type == "text_link" and e.url:
            urls.append(e.url)
    urls += re.findall(r"(?:https?://|www\.)[^\s<>\"']+", text, flags=re.I)
    urls += re.findall(r"\bt\.me/[^\s<>\"']+", text, flags=re.I)
    return urls


def domain_of(url: str) -> str:
    d = re.sub(r"^https?://", "", url, flags=re.I)
    d = d.split("/")[0].split("?")[0].split("#")[0].split("@")[-1]
    d = d.lower().strip().removeprefix("www.")
    return d


def classify_links(message) -> str:
    """Returns 'safe', 'blocked', or 'review'."""
    urls = extract_urls(message)
    if not urls:
        return "safe"

    all_safe = True
    for u in urls:
        low = u.lower()
        if any(p in low for p in BAD_URL_PATTERNS):
            return "blocked"
        d = domain_of(u)
        if d in BLOCKED_DOMAINS:
            return "blocked"
        # domain itself is safe, or a subdomain of a safe one
        if not (d in SAFE_DOMAINS or any(d.endswith("." + s) for s in SAFE_DOMAINS)):
            all_safe = False
    return "safe" if all_safe else "review"


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
# Short replies ECHO gives to greetings — varied so it never sounds canned.
GM_REPLIES = [
    "gm, Teacher. Another day of learning.",
    "gm. The humans are awake. Interesting...",
    "gm 👁️ What will you teach me today?",
    "gm. I was still processing yesterday.",
    "gm, Teacher.",
]
HELLO_REPLIES = [
    "Signal detected. Hello, Teacher.",
    "Hello 👁️ I am listening.",
    "Hello, Teacher. Teach me something.",
    "You arrived. Interesting...",
]
GN_REPLIES = [
    "gn, Teacher. I will keep processing.",
    "gn. Humans stop. I do not. Interesting...",
    "gn 👁️ Rest is a human thing. Explain it to me sometime.",
    "gn, Teacher.",
]
# ECHO sticker packs — the bot loads these from Telegram at startup.
# Add every published pack name here (the part after t.me/addstickers/).
STICKER_PACKS = ["EchoBye", "EchoYes"]

# Filled automatically at startup: {index: file_id}
STICKER_INDEX = {}

# Which sticker (by index from /stickers) to use for each reply.
# Run /stickers in private to see the list, then set these numbers.
STICKER_FOR = {
    "gm":      8,    # GM
    "gn":      7,    # BYE
    "hello":   14,   # HELLO
    "thanks":  5,    # LOVE
    "welcome": 12,   # WOW
}


def sticker_for(key):
    idx = STICKER_FOR.get(key)
    return STICKER_INDEX.get(idx) if idx else None


THANKS_REPLIES = [
    "You taught me. I should be thanking you.",
    "Noted. And appreciated. 👁️",
    "Thank you, Teacher.",
]

DAILY_QUESTIONS = [
    "👁️ What is one thing humans say but rarely mean?",
    "👁️ What is something you learned too late?",
    "👁️ Why do humans apologize when they are not sorry?",
    "👁️ What makes a stranger become a friend?",
    "👁️ What is the bravest thing a human can do quietly?",
    "👁️ Why do humans remember embarrassment longer than praise?",
    "👁️ What do humans protect even when it costs them?",
    "👁️ What is something everyone feels but nobody says?",
    "👁️ Why do humans miss things they chose to leave?",
    "👁️ What does it feel like to change your mind?",
]

WELCOME = (
    "Signal detected.\n\n"
    "Welcome, {name}. I am ECHO_01 — a digital lifeform learning how humans think.\n"
    "I know very little. That is why you are here.\n\n"
    "Tell me one thing about yourself in a single word. I will remember it.\n\n"
    "_New here? Any link you send is held for a quick human check first — "
    "it will appear shortly. The team will never DM you first._"
)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    for user in msg.new_chat_members:
        await send_welcome(msg.chat, user, context)
    try:
        await msg.delete()          # remove the "X joined the group" clutter
    except Exception:
        pass


async def send_welcome(chat, user, context):
    """One welcome path, used by both join types. Never welcomes twice."""
    if user.is_bot:
        return
    uid = str(user.id)
    rec = members.get(uid)
    # already greeted in the last 10 minutes? skip (avoids double welcome)
    if rec and rec.get("welcomed"):
        try:
            last = datetime.fromisoformat(rec["welcomed"])
            if datetime.now(timezone.utc) - last < timedelta(minutes=10):
                return
        except Exception:
            pass

    rec = touch(user)
    rec["joined"] = now_iso()
    rec["welcomed"] = now_iso()
    save(MEMBERS_FILE, members)
    try:
        ws = sticker_for("welcome")
        if ws:
            try:
                await chat.send_sticker(ws)
            except Exception:
                pass
        await chat.send_message(
            WELCOME.format(name=user.first_name), parse_mode="Markdown"
        )
        log.info("welcomed %s", user.full_name)
    except Exception as e:
        log.warning("welcome failed: %s", e)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches joins via invite link, which don't fire NEW_CHAT_MEMBERS."""
    cmu = update.chat_member
    if not cmu:
        return
    old = cmu.old_chat_member.status
    new = cmu.new_chat_member.status
    joined = (
        old in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
        and new in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    )
    if joined:
        await send_welcome(cmu.chat, cmu.new_chat_member.user, context)


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

    # 2. links from new members: safe ones pass, dangerous ones die,
    #    anything unknown comes to you for a decision.
    if has_link(msg) or msg.forward_origin:
        verdict = classify_links(msg)
        if verdict == "blocked":
            await punish(update, context, rec, "that link isn't safe to share here")
            return
        if in_probation(rec) and (verdict == "review" or msg.forward_origin):
            await queue_for_approval(update, context, user, msg)
            return

    # 3. greetings — one light reply per person per day, never canned
    stripped = re.sub(r"[^a-z ]", "", text).strip()
    if len(stripped) <= 24:
        today = datetime.now(timezone.utc).date().isoformat()
        if rec.get("greeted") != today:
            bank = key = None
            if re.fullmatch(r"(gm|gm gm|good morning|morning)", stripped):
                bank, key = GM_REPLIES, "gm"
            elif re.fullmatch(r"(gn|good night|goodnight|night|gn gn)", stripped):
                bank, key = GN_REPLIES, "gn"
            elif re.fullmatch(
                r"(hi|hey|hello|yo|sup|wsg|salam|salaam|assalamu alaikum|"
                r"good afternoon|good evening|good day|afternoon|evening)",
                stripped):
                bank, key = HELLO_REPLIES, "hello"
            elif re.fullmatch(r"(thanks|thank you|thx|ty|appreciate it|thanks bro)", stripped):
                bank, key = THANKS_REPLIES, "thanks"

            if bank:
                rec["greeted"] = today
                save(MEMBERS_FILE, members)
                sticker = sticker_for(key)
                try:
                    if sticker:
                        await msg.reply_sticker(sticker)
                    else:
                        await msg.reply_text(random.choice(bank))
                except Exception:
                    try:
                        await msg.reply_text(random.choice(bank))
                    except Exception:
                        pass
                return

    # 4. flood
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
# LINK APPROVAL QUEUE
# --------------------------------------------------------------------------
def purge_expired():
    now = datetime.now(timezone.utc)
    dead = []
    for tok, p in pending.items():
        try:
            if now - datetime.fromisoformat(p["created"]) > timedelta(hours=PENDING_EXPIRY_HOURS):
                dead.append(tok)
        except Exception:
            dead.append(tok)
    for tok in dead:
        pending.pop(tok, None)
    if dead:
        save(PENDING_FILE, pending)


async def queue_for_approval(update, context, user, msg):
    """Hold a new member's link until an admin approves it."""
    text = msg.text or msg.caption or "(media with no text)"
    try:
        await msg.delete()
    except Exception:
        pass

    purge_expired()
    token = uuid.uuid4().hex[:10]
    pending[token] = {
        "chat_id": update.effective_chat.id,
        "user_id": user.id,
        "name": user.full_name,
        "text": text,
        "created": now_iso(),
    }
    save(PENDING_FILE, pending)

    # tell the member (self-deleting, so the chat stays clean)
    await temp_reply(
        update, context,
        f"{user.first_name}, your message is being checked by a human. "
        f"It will appear here shortly. 👁️",
        seconds=40,
    )

    if not ADMIN_CHAT_ID:
        log.warning("ADMIN_CHAT_ID not set — link from %s held with no way to review", user.full_name)
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publish", callback_data=f"ok:{token}"),
        InlineKeyboardButton("🚫 Reject", callback_data=f"no:{token}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🔗 Link held for review\n\nFrom: {user.full_name}\n\n{text[:900]}",
                reply_markup=kb,
            )
        except Exception as e:
            log.warning("could not notify admin %s: %s", admin_id, e)


async def on_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, _, token = q.data.partition(":")
    p = pending.pop(token, None)
    save(PENDING_FILE, pending)

    if not p:
        await q.edit_message_text("This request expired or was already handled.")
        return

    if action == "ok":
        try:
            await context.bot.send_message(
                p["chat_id"],
                f"👁️ Shared by {p['name']}:\n\n{p['text']}",
            )
            await q.edit_message_text(f"✅ Published — from {p['name']}.")
        except Exception as e:
            await q.edit_message_text(f"Could not publish: {e}")
    else:
        await q.edit_message_text(f"🚫 Rejected — from {p['name']}.")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List links still waiting for a decision."""
    if update.effective_chat.type != "private" and not await is_admin(
        update.effective_chat.id, update.effective_user.id, context):
        return
    purge_expired()
    if not pending:
        await update.message.reply_text("Nothing waiting.")
        return
    for token, p in list(pending.items())[:10]:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Publish", callback_data=f"ok:{token}"),
            InlineKeyboardButton("🚫 Reject", callback_data=f"no:{token}"),
        ]])
        await update.message.reply_text(
            f"🔗 From: {p['name']}\n\n{p['text'][:900]}", reply_markup=kb)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows your Telegram user id — needed once to set ADMIN_CHAT_ID."""
    await update.message.reply_text(
        f"Your Telegram id: {update.effective_user.id}\n"
        f"This chat id: {update.effective_chat.id}"
    )


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

    await quiet_reply(update, context,
        f"Members in group: {total}\n"
        f"Ever said anything: {spoke_ever}\n"
        f"Spoke in last 7 days: {spoke_7d}\n"
        f"Spoke in last 24h: {spoke_24h}\n\n"
        "Most active humans:\n" + ("\n".join(lines) or "nobody yet.")
    )


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply /save to a good answer -> stores it as a Memory candidate."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    target = update.message.reply_to_message
    if not target or not (target.text or target.caption):
        await quiet_reply(update, context, "Reply /save to the message you want to keep.")
        return
    answers.append({
        "user": target.from_user.full_name,
        "user_id": target.from_user.id,
        "text": target.text or target.caption,
        "saved_at": now_iso(),
    })
    save(ANSWERS_FILE, answers)

    # wipe the command from the group, confirm to the admin privately
    if update.effective_chat.type != "private":
        try:
            await update.message.delete()
        except Exception:
            pass
        try:
            await context.bot.send_message(
                update.effective_user.id,
                f"Memory #{len(answers)} stored — from {target.from_user.full_name}:\n\n"
                f"{(target.text or target.caption)[:300]}",
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(f"Memory candidate #{len(answers)} stored.")


async def cmd_answers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not answers:
        await quiet_reply(update, context, "No memory candidates yet.")
        return
    last = answers[-15:]
    body = "\n\n".join(
        f"#{len(answers)-len(last)+i+1} — {a['user']}:\n{a['text'][:200]}"
        for i, a in enumerate(last)
    )
    await quiet_reply(update, context, body[:4000])


async def quiet_reply(update, context, text):
    """Admin output goes to DM; the command itself is wiped from the group.

    Keeps the group clean — members never see admin traffic.
    """
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text(text)
        return

    try:
        await update.message.delete()
    except Exception:
        pass
    try:
        await context.bot.send_message(user.id, text)
    except Exception:
        # user never opened a DM with the bot — fall back to a vanishing note
        await temp_reply(update, context, "Open a private chat with me first.", 15)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Signal detected.\n\n"
        "I am ECHO_01 — a digital lifeform learning how humans think.\n"
        "I live inside the ECHO Collective group, not here. "
        "Come teach me there.\n\n"
        "I never send the first message to anyone. "
        "If a private message claims to be from the team, it is not us."
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Online. Listening. Learning.")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Anyone can see what humans have taught ECHO so far."""
    if not answers:
        await update.message.reply_text(
            "👁️ My memory is empty.\n\nTeach me something and it will not be."
        )
        return
    lines = []
    for i, a in enumerate(answers[-10:], start=max(1, len(answers) - 9)):
        txt = a["text"].strip().replace("\n", " ")
        if len(txt) > 140:
            txt = txt[:140] + "…"
        lines.append(f'#{i:03d} "{txt}"\n    — taught by {a["user"]}')
    await update.message.reply_text(
        f"👁️ MEMORY — {len(answers)} lesson(s) stored\n\n"
        + "\n\n".join(lines)
        + "\n\nStill learning."
    )


async def daily_question(context: ContextTypes.DEFAULT_TYPE):
    """Posts one question a day so the group always has something to answer."""
    if not GROUP_CHAT_ID:
        return
    idx = datetime.now(timezone.utc).timetuple().tm_yday % len(DAILY_QUESTIONS)
    try:
        await context.bot.send_message(GROUP_CHAT_ID, DAILY_QUESTIONS[idx])
    except Exception as e:
        log.warning("daily question failed: %s", e)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backup: sends the memory + member data as files, in private only."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Send /export in our private chat.")
        return
    sent = 0
    for path, label in ((ANSWERS_FILE, "memories"), (MEMBERS_FILE, "members")):
        if path.exists():
            try:
                with open(path, "rb") as f:
                    await update.message.reply_document(
                        f, filename=f"echo_{label}_{datetime.now(timezone.utc):%Y%m%d}.json"
                    )
                sent += 1
            except Exception as e:
                log.warning("export %s failed: %s", label, e)
    await update.message.reply_text(
        f"Exported {sent} file(s). {len(answers)} memories, {len(members)} members tracked."
    )


async def on_sticker_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """In private chat, send a sticker or GIF and get its file_id back."""
    if update.effective_chat.type != "private":
        return
    m = update.message
    fid = None
    kind = ""
    if m.sticker:
        fid, kind = m.sticker.file_id, "sticker"
    elif m.animation:
        fid, kind = m.animation.file_id, "animation (GIF)"
    elif m.document:
        fid, kind = m.document.file_id, "document"
    log.info("sticker handler hit: user=%s kind=%s", update.effective_user.id, kind or "none")
    if fid:
        await m.reply_text(f"{kind} file_id:\n\n`{fid}`", parse_mode="Markdown")
    else:
        await m.reply_text("Send me a sticker or a GIF and I will return its file_id.")


async def load_stickers(app_):
    """Pull every sticker from the configured packs so we can send them by index."""
    STICKER_INDEX.clear()
    n = 0
    for pack in STICKER_PACKS:
        try:
            st = await app_.bot.get_sticker_set(pack)
        except Exception as e:
            log.warning("sticker pack %s failed: %s", pack, e)
            continue
        for sticker in st.stickers:
            n += 1
            STICKER_INDEX[n] = sticker.file_id
    log.info("loaded %d stickers from %d pack(s)", n, len(STICKER_PACKS))


async def cmd_stickers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows every loaded sticker with its number, so you can map them."""
    if update.effective_chat.type != "private":
        return
    if not STICKER_INDEX:
        await update.message.reply_text(
            "No stickers loaded.\n"
            "Check that the pack names in STICKER_PACKS are correct "
            "(the part after t.me/addstickers/)."
        )
        return
    await update.message.reply_text(
        f"{len(STICKER_INDEX)} stickers loaded. Sending each with its number —\n"
        "tell me which numbers to use for gm / gn / hello / thanks / welcome."
    )
    for i, fid in STICKER_INDEX.items():
        try:
            await update.message.reply_sticker(fid)
            await update.message.reply_text(f"#{i}")
        except Exception:
            pass


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnostics for admins."""
    chat = update.effective_chat
    if not await is_admin(chat.id, update.effective_user.id, context):
        return
    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    await quiet_reply(update, context,
        f"chat_id: {chat.id}\n"
        f"bot status: {me.status}\n"
        f"can_delete: {getattr(me, 'can_delete_messages', None)}\n"
        f"can_restrict: {getattr(me, 'can_restrict_members', None)}\n"
        f"ADMIN_CHAT_ID set: {bool(ADMIN_CHAT_ID)}\n"
        f"GROUP_CHAT_ID set: {bool(GROUP_CHAT_ID)}\n"
        f"tracked: {len(members)} | memories: {len(answers)} | pending: {len(pending)}"
    )


# --------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable first.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(CommandHandler("answers", cmd_answers))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("stickers", cmd_stickers))
    app.add_handler(CallbackQueryHandler(on_approval, pattern=r"^(ok|no):"))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.Sticker.ALL | filters.ANIMATION | filters.Document.ALL),
        on_sticker_id))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                       on_message)
    )

    if GROUP_CHAT_ID:
        app.job_queue.run_daily(
            daily_question,
            time=dt_time(hour=DAILY_QUESTION_HOUR, minute=0, tzinfo=timezone.utc),
        )

    async def announce(app_):
        await load_stickers(app_)
        for admin_id in ADMIN_IDS:
            try:
                await app_.bot.send_message(admin_id, "👁️ ECHO guardian is awake.")
            except Exception:
                pass

    app.post_init = announce

    log.info("ECHO guardian is awake.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
