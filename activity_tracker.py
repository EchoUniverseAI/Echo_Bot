"""
activity_tracker — silent daily activity logging for the ECHO Guardian bot.

Answers one question: how much of what gets said here is actually substance?

Wire it up with a single line in bot.py:

    import activity_tracker
    activity_tracker.register(app)

Storage: $DATA_DIR/activity.json, written atomically so a Railway restart
mid-write can't corrupt it.
"""

import json
import logging
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

log = logging.getLogger("activity")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
ACTIVITY_FILE = DATA_DIR / "activity.json"

GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0") or 0)
ADMIN_IDS = [int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_CHAT_ID", ""))]

TRACKER_GROUP = 9          # own handler group so nothing else is displaced
MAX_DAYS = 30
TOP_NAMES_SINGLE = 5
TOP_NAMES_RANGE = 8


# --------------------------------------------------------------------------
# STORAGE
# --------------------------------------------------------------------------
def _load() -> dict:
    try:
        with open(ACTIVITY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("activity.json unreadable (%s) — starting fresh", e)
        return {}


def _save(data: dict) -> None:
    """Write to a temp file in the same directory, then swap it in."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".activity-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ACTIVITY_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        log.warning("could not save activity: %s", e)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# CLASSIFICATION
# --------------------------------------------------------------------------
def _normalise(text: str) -> str:
    """Lowercase, strip emoji and punctuation, keep Arabic letters and digits."""
    text = text.lower().strip()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "So":                       # emoji / pictographs
            continue
        if ch.isalnum() or ch.isspace() or "\u0600" <= ch <= "\u06ff":
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


_RAW_FILLER = [
    # greetings
    "gm", "gn", "ga", "gm gm", "gm all", "gm y'all", "gm everyone", "gm fam",
    "gm bro", "gn all", "good morning", "good night", "good evening",
    "good afternoon", "morning", "evening", "night", "hi", "hii", "hello",
    "hey", "hey bro", "hey all", "hey guys", "hey there", "yo", "yoo", "sup",
    "wassup", "what's up", "what's good", "hola", "salam", "assalamualaikum",
    #状态 / how-are-you
    "how are you", "how are you doing", "how r u", "hru", "how is everyone",
    "how you doing", "wyd", "wbu", "wby", "and you", "i'm good", "i'm good bro",
    "i'm fine", "good", "great", "fine", "ok", "okay", "alright", "all good",
    "not bad", "same here",
    # courtesies / reactions
    "thanks", "thank you", "thx", "ty", "welcome", "you're welcome", "np",
    "cheers", "congrats", "nice", "cool", "awesome", "well done", "let's go",
    "lfg", "based", "true", "facts", "agreed", "exactly", "yes", "yeah", "yep",
    "no", "nope", "lol", "lmao", "haha", "wow", "omg", "bro", "ser", "fam", "gg",
    # Arabic
    "صباح الخير", "مساء الخير", "السلام عليكم", "وعليكم السلام", "اهلا",
    "مرحبا", "شكرا", "تمام", "حلو", "كويس", "الحمدلله", "ازيك", "ازيكم",
    "عامل ايه", "عاملين ايه",
]
# normalise the list itself so "what's good" matches the normalised "whats good"
FILLER_PHRASES = {_normalise(p) for p in _RAW_FILLER}

FILLER_PATTERNS = [
    re.compile(r"^g[mn]\b.{0,20}$"),
    re.compile(r"^(good\s+(morning|night|evening|afternoon))\b.{0,20}$"),
    re.compile(r"^(hi|hey|hello|yo)\b.{0,15}$"),
    re.compile(r"^how\s+(are|r|is|you|u)\b.{0,20}$"),
    re.compile(r"^(thanks|thank\s+you|thx)\b.{0,20}$"),
    re.compile(r"^(welcome)\b.{0,25}$"),
]


def classify(text: str) -> str:
    """'real' or 'filler'. Deliberately conservative — undercounts 'real'."""
    if not text:
        return "filler"

    has_question = "?" in text or "؟" in text
    norm = _normalise(text)

    if not norm:                      # emoji-only, e.g. 🔥🔥🔥
        return "filler"
    if norm in FILLER_PHRASES:
        return "filler"
    for pat in FILLER_PATTERNS:
        if pat.match(norm):
            return "filler"
    if has_question:                  # a short question still carries value
        return "real"
    if len(norm.split()) < 4:
        return "filler"
    return "real"


# --------------------------------------------------------------------------
# TRACKING
# --------------------------------------------------------------------------
async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Silent. Records nothing to the chat, replies to no one."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return
    if user.is_bot:
        return
    if GROUP_CHAT_ID and chat.id != GROUP_CHAT_ID:
        return

    text = msg.text or msg.caption or ""
    if text.startswith("/"):
        return

    kind = classify(text)
    day = _today()

    data = _load()
    entry = data.setdefault(
        day, {"total": 0, "real": 0, "filler": 0, "replies_to_bot": 0, "users": {}}
    )
    entry["total"] += 1
    entry[kind] += 1

    reply = msg.reply_to_message
    if reply and reply.from_user and reply.from_user.is_bot:
        entry["replies_to_bot"] += 1

    urec = entry["users"].setdefault(str(user.id), {"name": "", "total": 0, "real": 0})
    urec["name"] = user.full_name
    urec["total"] += 1
    if kind == "real":
        urec["real"] += 1

    _save(data)


# --------------------------------------------------------------------------
# /activity
# --------------------------------------------------------------------------
def _pct(part: int, whole: int) -> int:
    return round(part / whole * 100) if whole else 0


def _top(users: dict, limit: int) -> list:
    ranked = sorted(users.values(), key=lambda u: (-u.get("real", 0), -u.get("total", 0)))
    return [u for u in ranked if u.get("total")][:limit]


def _render_day(day: str, e: dict) -> str:
    speakers = len(e.get("users", {}))
    with_substance = sum(1 for u in e["users"].values() if u.get("real", 0) > 0)
    lines = [
        f"📅 {day}",
        f"الرسائل: {e['total']}  |  حقيقية: {e['real']} ({_pct(e['real'], e['total'])}%)"
        f"  |  تحيات: {e['filler']}",
        f"ردود على البوت: {e.get('replies_to_bot', 0)}",
        f"متحدثون: {speakers}  |  منهم بمضمون: {with_substance}",
    ]
    top = _top(e.get("users", {}), TOP_NAMES_SINGLE)
    if top:
        lines.append("")
        lines.append("الأنشط:")
        lines += [f"- {u['name']} — {u.get('real', 0)} حقيقية / {u['total']}" for u in top]
    return "\n".join(lines)


def _render_range(days: int, data: dict) -> str:
    today = datetime.now(timezone.utc).date()
    wanted = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)][::-1]
    present = [(d, data[d]) for d in wanted if d in data]

    if not present:
        return f"📊 آخر {days} يوم — لا يوجد نشاط مسجّل بعد."

    total = sum(e["total"] for _, e in present)
    real = sum(e["real"] for _, e in present)
    filler = sum(e["filler"] for _, e in present)
    replies = sum(e.get("replies_to_bot", 0) for _, e in present)

    merged: dict = {}
    for _, e in present:
        for uid, u in e.get("users", {}).items():
            m = merged.setdefault(uid, {"name": u.get("name", ""), "total": 0, "real": 0})
            m["name"] = u.get("name") or m["name"]
            m["total"] += u.get("total", 0)
            m["real"] += u.get("real", 0)

    n = len(present)
    lines = [
        f"📊 آخر {days} يوم ({n} يوم فيها نشاط)",
        f"إجمالي الرسائل: {total}",
        f"حقيقية: {real} ({_pct(real, total)}%)  |  تحيات: {filler}",
        f"ردود على البوت: {replies}",
        f"متوسط يومي: {total / n:.1f} رسالة ({real / n:.1f} حقيقية)",
        f"أعضاء متحدثون: {len(merged)}",
        "",
    ]
    for d, e in present:
        lines.append(
            f"{d}: {e['total']} رسالة · {e['real']} حقيقية ({_pct(e['real'], e['total'])}%)"
        )

    top = _top(merged, TOP_NAMES_RANGE)
    if top:
        lines.append("")
        lines.append("الأنشط:")
        lines += [f"- {u['name']} — {u.get('real', 0)} حقيقية / {u['total']}" for u in top]
    return "\n".join(lines)


async def cmd_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or (ADMIN_IDS and user.id not in ADMIN_IDS):
        return                                    # silent for everyone else

    days = 1
    if context.args:
        try:
            days = max(1, min(MAX_DAYS, int(context.args[0])))
        except ValueError:
            days = 1

    data = _load()
    if days == 1:
        day = _today()
        body = _render_day(day, data[day]) if day in data else f"📅 {day}\nلا رسائل بعد اليوم."
    else:
        body = _render_range(days, data)

    # keep admin output out of the group
    chat = update.effective_chat
    if chat and chat.type != "private":
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        try:
            await context.bot.send_message(user.id, body)
            return
        except Exception:
            pass
    await update.effective_message.reply_text(body)


# --------------------------------------------------------------------------
def register(application: Application) -> None:
    """Adds both handlers. Tracking sits in its own group so it never
    competes with moderation, link filtering or sticker replies."""
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, track_message),
        group=TRACKER_GROUP,
    )
    application.add_handler(CommandHandler("activity", cmd_activity))
    log.info("activity tracker registered (group=%d)", TRACKER_GROUP)
