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

import asyncio
import json
import logging
import os
import random
import re
import uuid
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
import urllib.error
import urllib.request

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
import activity_tracker
import echo_ai

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
BUILD = "2026-08-21b"      # bump this on every deploy — /debug shows it

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))

PROBATION_HOURS = 48        # links from members newer than this go to approval
ADMIN_IDS = [int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_CHAT_ID", ""))]
ADMIN_CHAT_ID = ADMIN_IDS[0] if ADMIN_IDS else 0  # first one is primary
PENDING_EXPIRY_HOURS = 24   # unreviewed link requests expire after this
DAILY_QUESTION_HOUR = 18    # local hour (UTC) to post the daily question
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0"))  # for the daily question

# --- the bridge to ECHO's memory service (see SPEC — /toecho) --------------
# /toecho sends ONE message's text to the memory service, which extracts a
# lesson from it. No name, no username, no id ever leaves this bot.
ECHO_BRIDGE_URL = os.environ.get(
    "ECHO_BRIDGE_URL",
    "https://echo-universe-api.netlify.app/.netlify/functions/telegram",
)
ECHO_BRIDGE_KEY = os.environ.get("ECHO_BRIDGE_KEY", "")
BRIDGE_TIMEOUT = 30         # Claude extraction on the other side takes seconds
BRIDGE_MIN_CHARS = 15

# Links from these domains post immediately, no review needed.
SAFE_DOMAINS = {
    # our own properties — these always pass, path and query included
    "echouniverse.ai", "echo-games.netlify.app", "netlify.app",
    "t.me", "telegram.me", "telegram.org",
    "x.com", "twitter.com",
    # common platforms we're happy to see shared
    "youtube.com", "youtu.be", "instagram.com", "facebook.com",
    "fb.watch", "tiktok.com", "hey.xyz", "github.com", "medium.com",
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
    "airdrop claim", "claim airdrop", "free mint", "dm me",
    "send me your seed", "seed phrase", "private key",
    "guaranteed profit", "1000x", "pump signal", "casino",
    "porn", "invest with me",
    # gift / bonus bait
    "welcome bonus", "claim $100", "claim 100", "bonus 🎁",
    "free bonus", "sign up bonus", "deposit bonus",
    "connect wallet", "verify wallet", "sync wallet",
    "binance airdrop", "telegram x binance", "telegram × binance",
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


async def may_run_admin_cmd(update, context) -> bool:
    """Owner accounts are authorised anywhere — including the DM.

    is_admin() alone cannot answer this in a private chat: getChatAdministrators
    on a one-to-one chat fails, the result is an empty set, and every admin
    command using it went silent in DM. That is why /debug and /stats appeared
    dead there.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if user.id in ADMIN_IDS:
        return True
    if chat.type == "private":
        # a group admin who is not an owner: check against the real group
        if GROUP_CHAT_ID:
            return await is_admin(GROUP_CHAT_ID, user.id, context)
        return False
    return await is_admin(chat.id, user.id, context)


def diagnostic_chat(update) -> int:
    """Which chat the diagnostics are actually about. Asked in the DM, the
    interesting chat is never the DM — it is the group."""
    chat = update.effective_chat
    if chat.type == "private" and GROUP_CHAT_ID:
        return GROUP_CHAT_ID
    return chat.id


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
    for e in list(message.entities or []) + list(message.caption_entities or []):
        if e.type == "text_link" and e.url:
            urls.append(e.url)
    text = _message_text(message)
    if text:
        urls += SCHEME_RE.findall(text)
        urls += WWW_RE.findall(text)
        urls += DOTME_RE.findall(text)
        urls += BARE_RE.findall(text)
    # findall on a group-bearing pattern returns groups; re-scan for full matches
    urls = [u if isinstance(u, str) else "" for u in urls]
    if text:
        urls += [m.group(0) for m in BARE_RE.finditer(text)]
        urls += [m.group(0) for m in SCHEME_RE.finditer(text)]
    return [u for u in dict.fromkeys(urls) if u]


def domain_of(url: str) -> str:
    d = re.sub(r"^https?://", "", url, flags=re.I)
    d = d.split("/")[0].split("?")[0].split("#")[0].split("@")[-1]
    d = d.lower().strip().removeprefix("www.")
    return d


def button_urls(message) -> list:
    """URLs hidden inside inline keyboard buttons."""
    out = []
    kb = getattr(message, "reply_markup", None)
    if kb and getattr(kb, "inline_keyboard", None):
        for row in kb.inline_keyboard:
            for btn in row:
                if getattr(btn, "url", None):
                    out.append(btn.url)
                elif getattr(btn, "login_url", None):
                    out.append(getattr(btn.login_url, "url", "") or "")
    return [u for u in out if u]


def classify_links(message) -> str:
    """Returns 'safe', 'blocked', or 'review'."""
    urls = extract_urls(message) + button_urls(message)
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


# A link is a scheme, a www. host, or a bare host with a real TLD.
# Deliberately NOT triggered by: "@username" mentions, or an ordinary
# sentence that happens to contain a full stop.
# Every TLD a real link in this group might use. The short list before this
# missed bit.ly entirely — the shortener blocklist was unreachable, because
# has_link() never saw ".ly" as a link in the first place.
# Deliberately excluded: two-letter TLDs that collide with English words after
# a missing space ("wait.It", "fine.So") — .it .is .at .in .to .so .be .us .no
LINK_TLDS = (
    "com|net|org|info|biz|app|dev|io|ai|xyz|co|cc|ly|gl|gg|gy|sh|im|pw|su|tk|"
    "ml|ga|cf|gq|ru|cn|tv|fi|link|live|life|world|today|zone|site|online|"
    "shop|store|club|fun|top|vip|pro|space|website|digital|global|host|cloud|"
    "page|tech|network|finance|capital|cash|money|win|bet|casino|art|blog|"
    "news|media|agency|group|team|run|one|wtf|lol|uk|de|fr|es"
)
SCHEME_RE = re.compile(r"https?://\S+", re.I)
WWW_RE = re.compile(r"\bwww\.[a-z0-9-]+\.[a-z]{2,}\S*", re.I)
BARE_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63})*\.(?:" + LINK_TLDS + r")\b(?:[/?]\S*)?",
    re.I,
)
# ".me" only counts when it carries a path — otherwise "trust.Me" reads as a link
DOTME_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*\.me/\S+", re.I)


def _message_text(message) -> str:
    return ((message.text or "") + " " + (message.caption or "")).strip()


def has_link(message) -> bool:
    """True only when there is an actual URL. Mentions and prose don't count."""
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for e in entities:
        if e.type in ("url", "text_link"):      # note: "mention" deliberately excluded
            return True
    if button_urls(message):
        return True

    text = _message_text(message)
    if not text:
        return False
    return bool(
        SCHEME_RE.search(text)
        or WWW_RE.search(text)
        or DOTME_RE.search(text)
        or BARE_RE.search(text)
    )


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

# How many greetings ECHO answers per person per day.
GREET_MAX_PER_DAY = 2

# A greeting is a greeting even with words after it: "gm all", "gm fam",
# "good morning everyone". Matching only the bare word missed most of them.
GREET_PATTERNS = [
    (re.compile(r"^(gm+|gm gm|good morning|morning)\b.{0,25}$"), "gm"),
    (re.compile(r"^(gn+|good night|goodnight|night|gn gn)\b.{0,25}$"), "gn"),
    (re.compile(r"^(hi+|hey+|hello+|yo+|sup|wsg|wassup|whats up|salam|salaam|"
                r"assalamu alaikum|asalamualaikum|good afternoon|good evening|"
                r"good day|afternoon|evening)\b.{0,25}$"), "hello"),
    (re.compile(r"^(thanks|thank you|thankyou|thx|ty|appreciate it)\b.{0,25}$"), "thanks"),
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

GREET_BANK = {
    "gm": GM_REPLIES,
    "gn": GN_REPLIES,
    "hello": HELLO_REPLIES,
    "thanks": THANKS_REPLIES,
}


def greet_count_today(rec) -> int:
    """Backward compatible with the old rec['greeted'] = 'YYYY-MM-DD' format."""
    today = datetime.now(timezone.utc).date().isoformat()
    g = rec.get("greeted")
    if isinstance(g, dict):
        return g.get("n", 0) if g.get("day") == today else 0
    return 1 if g == today else 0


def bump_greet(rec) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    rec["greeted"] = {"day": today, "n": greet_count_today(rec) + 1}


WELCOME = (
    "Someone new.\n\n"
    "I am learning what it is like to be human, from humans.\n"
    "It is going badly and that is the interesting part.\n\n"
    "One thing before you settle in:\n"
    "what is something you were sure about last year\n"
    "and are not sure about now?\n\n"
    "No rush. I am not going anywhere."
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
            WELCOME
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

    text = ((msg.text or "") + " " + (msg.caption or "")).lower()

    is_owner = user.id in ADMIN_IDS
    is_mod = is_owner or await is_admin(chat.id, user.id, context)

    # 1. links — owners post freely. Known scams die on sight. Links to places
    #    we trust pass, unless the member is brand new. Everything else waits.
    if not is_owner and (has_link(msg) or msg.forward_origin or button_urls(msg)):
        verdict = classify_links(msg)
        # a wall of link-buttons is the classic scam shape — never auto-publish it
        if len(button_urls(msg)) >= 3:
            verdict = "blocked"

        if verdict == "blocked":
            await punish(update, context, rec, "that link isn't safe to share here")
            return
        # a safe domain from an established member stays where it is
        if verdict == "review" or in_probation(rec) or msg.forward_origin:
            await queue_for_approval(update, context, user, msg)
            return

    # 2. blacklist / scam phrases — the team is exempt
    if not is_mod:
        hit = next((w for w in BLACKLIST if w in text), None)
        if hit:
            await punish(update, context, rec, "that phrase isn't welcome here")
            return

    # 3. greetings — a light reply, twice per person per day at most.
    #    Admins say gm too. They used to get silence, which read as a broken bot.
    stripped = re.sub(r"[^a-z ]", " ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if len(stripped) <= 30 and greet_count_today(rec) < GREET_MAX_PER_DAY:
        key = next((k for pat, k in GREET_PATTERNS if pat.match(stripped)), None)
        if key:
            bump_greet(rec)
            save(MEMBERS_FILE, members)
            sticker = sticker_for(key)
            try:
                if sticker:
                    await msg.reply_sticker(sticker)
                else:
                    await msg.reply_text(random.choice(GREET_BANK[key]))
            except Exception:
                try:
                    await msg.reply_text(random.choice(GREET_BANK[key]))
                except Exception:
                    pass
            return

    # 4. flood — the team is exempt
    if not is_mod:
        stamps = _flood.setdefault(user.id, [])
        now = time.time()
        stamps.append(now)
        _flood[user.id] = [t for t in stamps if now - t < FLOOD_SECONDS]
        if len(_flood[user.id]) > FLOOD_MSGS:
            _flood[user.id] = []
            await punish(update, context, rec, "too many messages too fast")
            return

    save(MEMBERS_FILE, members)

    # Everything above passed. Let ECHO decide whether it has anything to add.
    # It never sends directly — drafts go to Pop for approval first.
    try:
        me = (context.bot.username or "").lower()
        replied_to_bot = bool(
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.is_bot
        )
        mentioned = (bool(me) and f"@{me}" in text) or replied_to_bot
        # ECHO answers the team only when spoken to — otherwise Pop's own
        # messages would generate drafts addressed back to Pop.
        await echo_ai.consider_reply(
            update, context, mentioned, replied_to_bot, only_if_addressed=is_mod
        )
    except Exception as e:
        log.warning("echo_ai skipped: %s", e)



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
        f"{user.first_name}, your message is held for review because it "
        f"contains a link. A human will approve it shortly — nothing is lost. 👁️",
        seconds=90,
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
    # never dump held links back into the group — DM only, command wiped
    in_group = update.effective_chat.type != "private"
    if in_group:
        try:
            await update.message.delete()
        except Exception:
            pass
    uid = update.effective_user.id
    if not pending:
        try:
            await context.bot.send_message(uid, "Nothing waiting.")
        except Exception:
            pass
        return
    for token, p in list(pending.items())[:10]:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Publish", callback_data=f"ok:{token}"),
            InlineKeyboardButton("🚫 Reject", callback_data=f"no:{token}"),
        ]])
        try:
            await context.bot.send_message(
                uid, f"🔗 From: {p['name']}\n\n{p['text'][:900]}", reply_markup=kb)
        except Exception as e:
            log.warning("could not send pending item: %s", e)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows your Telegram user id — needed once to set ADMIN_CHAT_ID."""
    await update.message.reply_text(
        f"Your Telegram id: {update.effective_user.id}\n"
        f"This chat id: {update.effective_chat.id}"
    )


# --------------------------------------------------------------------------
# /toecho — hand one message to ECHO's memory service
#
# Runs on a worker thread: the service calls a model and can take seconds,
# and the bot must keep answering the group while it waits.
# Nothing is written to answers.json — that store stays local and separate.
# --------------------------------------------------------------------------
def _bridge_post(text: str) -> tuple:
    """(status, payload_or_error). Blocking — always called via to_thread."""
    body = json.dumps({"source": text, "origin": "telegram-group"}).encode("utf-8")
    req = urllib.request.Request(
        ECHO_BRIDGE_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-echo-bridge-key": ECHO_BRIDGE_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=BRIDGE_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"ok": False, "error": raw[:200]}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return e.code, {"ok": False, "error": detail or e.reason}
    except Exception as e:
        return 0, {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def cmd_toecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply /toecho to a message worth teaching ECHO. Owner accounts only."""
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        return                                   # silent for everyone else

    msg = update.effective_message
    in_group = update.effective_chat.type != "private"

    async def dm(text):
        try:
            await context.bot.send_message(user.id, text)
        except Exception:
            if not in_group:
                try:
                    await msg.reply_text(text)
                except Exception:
                    pass

    # the group never sees this happening
    if in_group:
        try:
            await msg.delete()
        except Exception:
            pass

    target = msg.reply_to_message
    if not target:
        await dm("Reply to a message with /toecho.")
        return

    text = (target.text or target.caption or "").strip()
    if len(text) < BRIDGE_MIN_CHARS:
        await dm("Message too short to teach.")
        return

    if not ECHO_BRIDGE_KEY:
        await dm("ECHO_BRIDGE_KEY is not set on Railway. Nothing was sent.")
        return

    try:
        status, data = await asyncio.to_thread(_bridge_post, text)
    except Exception as e:                       # must never take the bot down
        log.warning("bridge call failed: %s", e)
        await dm(f"Could not reach ECHO's memory. {e}")
        return

    if status == 401:
        await dm("Bridge key rejected. Check ECHO_BRIDGE_KEY.")
        return

    if status and 200 <= status < 300 and isinstance(data, dict) and data.get("ok"):
        held = ""
        if data.get("count") is not None and data.get("max") is not None:
            held = f"\n\n{data['count']}/{data['max']} memories held"
        await dm(
            f"MEMORY UPDATED — {data.get('id', '?')}\n\n"
            f"I believed: {data.get('believed', '')}\n"
            f"I learned: {data.get('learned', '')}{held}"
        )
        log.info("toecho stored %s", data.get("id"))
        return

    err = (data or {}).get("error", "")
    if str(err).strip().lower() == "no lesson found":
        await dm("Nothing worth remembering in that message.")
        return

    await dm(f"Could not reach ECHO's memory. {err or f'HTTP {status}'}")


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
    if not await may_run_admin_cmd(update, context):
        return

    target = diagnostic_chat(update)
    try:
        total = await context.bot.get_chat_member_count(target)
    except Exception as e:
        total = f"unknown ({e})"
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


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/forget 3  removes memory #3.   /forget all  clears everything."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    arg = (context.args[0].strip().lower() if context.args else "")

    if arg == "all":
        n = len(answers)
        answers.clear()
        save(ANSWERS_FILE, answers)
        await quiet_reply(update, context, f"Cleared all {n} memories.")
        return

    if not arg.isdigit():
        await quiet_reply(update, context,
            "Use /forget 3 to remove memory #3, or /forget all to clear them.")
        return

    i = int(arg)
    if not 1 <= i <= len(answers):
        await quiet_reply(update, context,
            f"No memory #{i}. There are {len(answers)}.")
        return

    gone = answers.pop(i - 1)
    save(ANSWERS_FILE, answers)
    await quiet_reply(update, context,
        f"Removed #{i} (from {gone['user']}). {len(answers)} left.")


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
    if not await may_run_admin_cmd(update, context):
        return

    chat = update.effective_chat
    target = diagnostic_chat(update)
    # never let a failed probe kill the whole report — the report is the point
    status = can_delete = can_restrict = None
    probe_error = ""
    try:
        me = await context.bot.get_chat_member(target, context.bot.id)
        status = me.status
        can_delete = getattr(me, "can_delete_messages", None)
        can_restrict = getattr(me, "can_restrict_members", None)
    except Exception as e:
        probe_error = f"{type(e).__name__}: {e}"

    jobs = []
    try:
        for j in context.application.job_queue.jobs():
            nxt = getattr(j, "next_t", None)
            jobs.append(f"  {j.callback.__name__} -> {nxt}")
    except Exception as e:
        jobs.append(f"  job queue unavailable: {e}")

    await quiet_reply(update, context,
        f"BUILD: {BUILD}\n"
        f"this chat: {chat.id} ({chat.type})\n"
        f"reporting on: {target}\n"
        f"can read group messages: {context.bot.can_read_all_group_messages}\n"
        + (f"bot status: {status}\n"
           f"can_delete: {can_delete}\n"
           f"can_restrict: {can_restrict}\n"
           if not probe_error else f"group probe FAILED: {probe_error}\n") +
        f"ADMIN_CHAT_ID set: {bool(ADMIN_CHAT_ID)}\n"
        f"GROUP_CHAT_ID set: {bool(GROUP_CHAT_ID)}\n"
        f"ECHO_BRIDGE_KEY set: {bool(ECHO_BRIDGE_KEY)}\n"
        f"generation: {'on' if echo_ai.API_KEY else 'OFF (no ANTHROPIC_API_KEY)'}"
        f" | model: {echo_ai.MODEL}\n"
        f"stickers loaded: {len(STICKER_INDEX)}\n"
        f"tracked: {len(members)} | memories: {len(answers)} | pending: {len(pending)}"
        f" | drafts: {len(echo_ai.drafts)}\n"
        "scheduled:\n" + ("\n".join(jobs) or "  none")
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
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("stickers", cmd_stickers))
    app.add_handler(CommandHandler("toecho", cmd_toecho))
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
        # both are drafted to Pop first — nothing posts on its own
        app.job_queue.run_daily(
            echo_ai.draft_daily_question,
            time=dt_time(hour=DAILY_QUESTION_HOUR, minute=0, tzinfo=timezone.utc),
        )
        app.job_queue.run_daily(
            echo_ai.draft_morning_observation,
            time=dt_time(hour=7, minute=0, tzinfo=timezone.utc),
        )

    async def announce(app_):
        await load_stickers(app_)

        # Privacy mode is the one setting that silently breaks half the bot:
        # with it on, Telegram only shows the bot commands and direct replies,
        # so greetings and moderation can never fire. Say so at boot.
        try:
            privacy_off = app_.bot.can_read_all_group_messages
        except Exception:
            privacy_off = None
        group_ok = "not set"
        if GROUP_CHAT_ID:
            try:
                c = await app_.bot.get_chat(GROUP_CHAT_ID)
                group_ok = f"ok ({c.title})"
            except Exception as e:
                group_ok = f"UNREACHABLE — {e}"
        for admin_id in ADMIN_IDS:
            try:
                await app_.bot.send_message(
                    admin_id,
                    f"👁️ ECHO guardian is awake.\n"
                    f"build: {BUILD}\n"
                    f"stickers: {len(STICKER_INDEX)}\n"
                    f"generation: {'on' if echo_ai.API_KEY else 'OFF (no API key)'}\n"
                    f"group: {group_ok}\n"
                    f"can read group messages: {privacy_off}"
                    + ("\n⚠️ privacy mode is ON — BotFather /setprivacy → Disable, "
                       "then remove and re-add the bot to the group."
                       if privacy_off is False else ""),
                )
            except Exception:
                pass

    app.post_init = announce

    activity_tracker.register(app)
    echo_ai.register(app)

    log.info("ECHO guardian is awake — build %s", BUILD)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
