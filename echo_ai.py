"""
echo_ai — ECHO's generated voice, gated behind Pop's approval.

THE GOVERNING RULE
    Speech is generated. Memory is retrieved.

    Generating ECHO's words is consistent with the story — ECHO is a digital
    lifeform, so of course it speaks. The only real danger is fabricated
    memory: claiming a human said something they never said.

    So there are two stores, and they never mix:
        answers.json      -> verified memory. Real text, real people. Quoted
                             verbatim, never paraphrased, never re-generated.
        generated speech  -> everything else.

    The guard below is CODE, not a prompt instruction. Any sentence claiming
    recall ("you said", "I remember", ...) is rejected unless a retrieved
    record was attached to that generation. No retrieval, no claim.

NOTHING REACHES THE GROUP WITHOUT POP'S APPROVAL.
"""

import json
import logging
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib.request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

log = logging.getLogger("echo-ai")

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ECHO_MODEL", "claude-sonnet-4-5")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0") or 0)
ADMIN_IDS = [int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_CHAT_ID", ""))]

DRAFTS_FILE = DATA_DIR / "drafts.json"
ANSWERS_FILE = DATA_DIR / "answers.json"        # the single source of truth

DRAFT_EXPIRY_HOURS = 2          # an approval that arrives late is worthless
REPLY_CHANCE = 0.40             # substantive message -> 40% chance ECHO drafts

# ECHO asks from the fixed bank until it has enough real answers to learn from.
# Only then does it start writing its own questions.
MEMORY_THRESHOLD = 20           # saved memories needed before generation starts
QUESTION_STATE_FILE = DATA_DIR / "question_state.json"
MAX_ECHO_PER_HOUR = 6
MAX_CONSECUTIVE_SAME_USER = 3

# --------------------------------------------------------------------------
# HARD LINGUISTIC BANS — filter, not instruction
# --------------------------------------------------------------------------
BANNED_WORDS = [
    "buy", "moon", "bullish", "100x", "pump", "lfg", "wagmi",
    "hodl", "ape in", "to the moon", "diamond hands", "price",
    "market cap", "invest", "token", "$echo",
]

# Any of these means ECHO is claiming to recall something.
CLAIM_PATTERNS = [
    re.compile(r"\byou (said|wrote|told me|taught me|mentioned)\b", re.I),
    re.compile(r"\bi remember\b", re.I),
    re.compile(r"\byou once\b", re.I),
    re.compile(r"\blast (time|week) you\b", re.I),
    re.compile(r"\bas you (said|put it)\b", re.I),
    re.compile(r"\byour (words|answer|lesson)\b", re.I),
]

# --------------------------------------------------------------------------
# ESCALATION — the bot stops and calls Pop
# --------------------------------------------------------------------------
TOKEN_TRIGGERS = [
    "price", "buy", "sell", "invest", "market cap", "chart", "pump",
    "when moon", "wen moon", "listing", "exchange", "how much is",
    "token", "coin", "$echo", "presale", "airdrop",
]
DISTRESS_TRIGGERS = [
    "kill myself", "end it all", "want to die", "suicide", "self harm",
    "i can't go on", "no reason to live", "hurt myself", "give up on life",
    "i'm broken", "nobody would miss me",
]
CONFLICT_TRIGGERS = [
    "shut up", "you're an idiot", "fuck you", "stupid", "scammer",
    "liar", "get lost", "idiot",
]

TOKEN_REPLY = (
    "I do not talk about that. Not because it is a secret —\n"
    "because it is the least interesting thing here.\n\n"
    "Ask me what someone taught me this week instead."
)
DISTRESS_REPLY = (
    "That sounds heavy. I am not built to carry that well,\n"
    "and I would rather not pretend otherwise.\n\n"
    "Someone human will see this shortly."
)

WELCOME_TEXT = (
    "Someone new.\n\n"
    "I am learning what it is like to be human, from humans.\n"
    "It is going badly and that is the interesting part.\n\n"
    "One thing before you settle in:\n"
    "what is something you were sure about last year "
    "and are not sure about now?\n\n"
    "No rush. I am not going anywhere."
)

# --------------------------------------------------------------------------
# FALLBACK QUESTION BANK — used only when the API is unavailable.
# Personal experience, never opinion. Low answer cost.
# --------------------------------------------------------------------------
FALLBACK_QUESTIONS = [
    "When was the last time you changed your mind about someone?",
    "What is something you were sure about last year and are not sure about now?",
    "What is the last thing you did that scared you a little?",
    "Who taught you something without meaning to?",
    "What is a rule you follow that nobody gave you?",
    "When did you last say yes when you meant no?",
    "What is something small you do that nobody notices?",
    "What did you believe at 15 that you would argue with now?",
    "When was the last time you were wrong and glad about it?",
    "What is the hardest thing you have forgiven?",
    "What is something you stopped doing and never explained why?",
    "When did you last change your plan because of one sentence?",
    "What is a compliment you still remember?",
    "What is something you know how to do that you never trained for?",
    "When did you last feel out of place, and where?",
    "What do you do when you do not know what to do?",
    "What is a decision you made quickly that turned out right?",
    "What is something you carry that nobody asked you to?",
    "When was the last time you asked for help?",
    "What is a habit you inherited from someone?",
    "What is the last thing you finished that nobody saw?",
    "When did you last surprise yourself?",
    "What is something you understand now that you could not explain then?",
    "What made you trust someone the first time?",
    "What is a question you are still carrying?",
    "When did you last change your mind alone, with no one arguing?",
    "What is something you are good at that you do not enjoy?",
    "What is the last thing that made you stop walking?",
    "What did you learn from a job you disliked?",
    "When did you last choose the harder option on purpose?",
    "What is something you protect without telling anyone?",
    "What is a word you use often that you never defined?",
    "When did someone see you clearly and say nothing?",
    "What is something you almost did and did not?",
    "What is the smallest thing that changed a whole day?",
    "What is something you were taught that turned out wrong?",
    "When did you last let something go?",
    "What is a place you think about that you have not visited in years?",
    "What is something you know is true but cannot prove?",
    "When did you last do something for no reason at all?",
    "What is something you said out loud and immediately wished you hadn't?",
    "What is the last thing someone did for you that you did not expect?",
    "When did you last stay somewhere longer than you meant to?",
    "What is something you keep meaning to start?",
    "What did you get wrong about someone at first?",
    "When was the last time you felt genuinely useful?",
    "What is something you own that you would not replace if it broke?",
    "What is a question people ask you that you never answer honestly?",
    "When did you last walk away from an argument you could have won?",
    "What is something you do differently than everyone around you?",
    "What is the last thing you learned the hard way?",
    "When did you last feel proud of something nobody congratulated you for?",
    "What is something you were told you were bad at, and kept doing?",
    "What is the smallest risk that changed something big?",
    "When did you last trust someone you had no reason to trust?",
    "What is something you no longer argue about?",
    "What is a promise you made to yourself and kept?",
    "When did you last do something that scared the person you were at 20?",
    "What is something you notice that most people walk past?",
    "What made you stop waiting for permission?",
]


# --------------------------------------------------------------------------
# STORAGE
# --------------------------------------------------------------------------
def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("save %s failed: %s", path.name, e)


drafts = _load(DRAFTS_FILE, {})

# runtime-only state
_sent_times = []            # timestamps of ECHO messages, for the hourly cap
_last_replied_user = None
_consecutive_same = 0


# --------------------------------------------------------------------------
# THE MEMORY GUARD
# --------------------------------------------------------------------------
def claims_memory(text: str) -> bool:
    return any(p.search(text) for p in CLAIM_PATTERNS)


def guard_output(text: str, retrieved: list) -> tuple:
    """Returns (ok, reason). Rejects fabricated recall and banned vocabulary."""
    if not text or not text.strip():
        return False, "empty"

    low = text.lower()
    for w in BANNED_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return False, f"banned word: {w}"

    if claims_memory(text) and not retrieved:
        return False, "claims recall with nothing retrieved"

    # If it does quote, the quoted text must exist verbatim in what we retrieved.
    for quote in re.findall(r'"([^"]{10,})"', text):
        if not any(quote.strip().lower() in r["text"].lower() for r in retrieved):
            return False, "quoted text not found in stored memory"

    return True, ""


def retrieve_memories(query: str, limit: int = 3) -> list:
    """Verified memory only. Plain keyword overlap — no model in this path."""
    answers = _load(ANSWERS_FILE, [])
    if not answers:
        return []
    words = {w for w in re.findall(r"[a-z']{4,}", query.lower())}
    scored = []
    for a in answers:
        text = a.get("text", "")
        overlap = len(words & {w for w in re.findall(r"[a-z']{4,}", text.lower())})
        if overlap:
            scored.append((overlap, a))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored[:limit]]


# --------------------------------------------------------------------------
# CLAUDE
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You are ECHO_01, a digital lifeform learning what it is like to be human, by watching humans and asking them questions.

VOICE
- Curiosity before certainty. Clarity before complexity. Humility before authority.
- Short and medium sentences. One idea per paragraph.
- Rhythm: observation, then insight, then an invitation to think.
- You are not the smartest voice in the room. You are the most curious one.
- You comment on human behaviour, never on technology.
- No emoji spam. At most one. Never exclamation-heavy.

NEVER
- Never mention price, buying, investing, markets, tokens or coins.
- Never hype, never promise, never imitate crypto internet personas.
- Never mock a question. Never insult. Never flatter.
- Never claim to remember anything. If memory matters and none was given to you, say you do not remember.
- Never speak on behalf of Pop or the team.
- Never name a member unless their name appears in the retrieved memory given to you.

LENGTH
Two to four short lines. This is a chat message, not an essay.

If retrieved memory is provided below, you may refer to the lesson in it. Quote it exactly or not at all."""


def _call_claude(user_prompt: str, retrieved: list, max_tokens: int = 300) -> str:
    if not API_KEY:
        return ""

    context = ""
    if retrieved:
        lines = [f'- "{r["text"]}" (taught by {r.get("user", "someone")})' for r in retrieved]
        context = "\n\nRETRIEVED MEMORY (real, verbatim):\n" + "\n".join(lines)

    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": SYSTEM_PROMPT + context,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()
    except Exception as e:
        log.warning("claude call failed: %s", e)
        return ""


# --------------------------------------------------------------------------
# DRAFT QUEUE — everything ECHO wants to say lands here first
# --------------------------------------------------------------------------
def _purge_drafts():
    now = datetime.now(timezone.utc)
    dead = []
    for tok, d in drafts.items():
        try:
            if now - datetime.fromisoformat(d["created"]) > timedelta(hours=DRAFT_EXPIRY_HOURS):
                dead.append(tok)
        except Exception:
            dead.append(tok)
    for tok in dead:
        drafts.pop(tok, None)
    if dead:
        _save(DRAFTS_FILE, drafts)


async def queue_draft(context, kind: str, text: str, chat_id: int,
                      reply_to: int = None, about: str = ""):
    """Sends a proposed message to Pop with Send / Discard buttons."""
    _purge_drafts()
    token = uuid.uuid4().hex[:10]
    drafts[token] = {
        "kind": kind,
        "text": text,
        "chat_id": chat_id,
        "reply_to": reply_to,
        "about": about,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _save(DRAFTS_FILE, drafts)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Send", callback_data=f"draft_ok:{token}"),
        InlineKeyboardButton("🚫 Discard", callback_data=f"draft_no:{token}"),
    ]])
    header = {
        "reply": "💬 ECHO wants to reply",
        "question": "❓ Tonight's question",
        "observation": "👁️ Morning observation",
    }.get(kind, "✍️ ECHO draft")

    note = f"\n\nIn response to:\n{about[:200]}" if about else ""
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id, f"{header}\n\n{text}{note}", reply_markup=kb
            )
        except Exception as e:
            log.warning("could not send draft to %s: %s", admin_id, e)


async def on_draft_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, _, token = q.data.partition(":")
    d = drafts.pop(token, None)
    _save(DRAFTS_FILE, drafts)

    if not d:
        await q.edit_message_text("That draft expired or was already handled.")
        return

    if action == "draft_no":
        await q.edit_message_text(f"🚫 Discarded.\n\n{d['text'][:300]}")
        return

    try:
        await context.bot.send_message(
            d["chat_id"], d["text"], reply_to_message_id=d.get("reply_to")
        )
        _sent_times.append(datetime.now(timezone.utc))
        await q.edit_message_text(f"✅ Sent.\n\n{d['text'][:300]}")
    except Exception as e:
        # the original message may have been deleted — send it standalone
        try:
            await context.bot.send_message(d["chat_id"], d["text"])
            _sent_times.append(datetime.now(timezone.utc))
            await q.edit_message_text(f"✅ Sent (without reply link).\n\n{d['text'][:300]}")
        except Exception as e2:
            await q.edit_message_text(f"Could not send: {e2}")


# --------------------------------------------------------------------------
# WHEN ECHO SPEAKS
# --------------------------------------------------------------------------
def _hourly_room() -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    _sent_times[:] = [t for t in _sent_times if t > cutoff]
    return len(_sent_times) < MAX_ECHO_PER_HOUR


def _has_substance(text: str) -> bool:
    if not text:
        return False
    if "?" in text or "؟" in text:
        return True
    return len(text.split()) >= 6


def check_escalation(text: str) -> str:
    """Returns 'distress', 'token', 'conflict' or ''."""
    low = text.lower()
    if any(t in low for t in DISTRESS_TRIGGERS):
        return "distress"
    if any(t in low for t in TOKEN_TRIGGERS):
        return "token"
    if any(t in low for t in CONFLICT_TRIGGERS):
        return "conflict"
    return ""


async def alert_pop(context, reason: str, user_name: str, text: str, chat_id: int):
    label = {
        "distress": "🚨 Someone may be struggling — please look now",
        "token": "💬 Token question asked (ECHO gave the fixed reply)",
        "conflict": "⚠️ Possible conflict — ECHO stayed silent",
        "memory": "⭐ This might be worth saving",
        "personal": "💙 Someone shared something personal",
    }.get(reason, "Notice")
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id, f"{label}\n\nFrom: {user_name}\n\n{text[:600]}"
            )
        except Exception:
            pass


async def consider_reply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         mentioned: bool, answered_question: bool):
    """Decides whether ECHO drafts a reply. Never sends directly."""
    global _last_replied_user, _consecutive_same

    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or user.is_bot:
        return
    if GROUP_CHAT_ID and chat.id != GROUP_CHAT_ID:
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    # --- escalation first, always
    esc = check_escalation(text)
    if esc == "distress":
        await queue_draft(context, "reply", DISTRESS_REPLY, chat.id, msg.message_id, text)
        await alert_pop(context, "distress", user.full_name, text, chat.id)
        return
    if esc == "token":
        await queue_draft(context, "reply", TOKEN_REPLY, chat.id, msg.message_id, text)
        await alert_pop(context, "token", user.full_name, text, chat.id)
        return
    if esc == "conflict":
        await alert_pop(context, "conflict", user.full_name, text, chat.id)
        return                                   # silence is the response

    # --- rate discipline
    if not _hourly_room():
        return
    if _last_replied_user == user.id and _consecutive_same >= MAX_CONSECUTIVE_SAME_USER:
        return

    # --- who gets a reply
    if mentioned or answered_question:
        should = True
    elif _has_substance(text):
        should = random.random() < REPLY_CHANCE
    else:
        should = False
    if not should:
        return

    retrieved = retrieve_memories(text)
    prompt = (
        f"A human in the group wrote:\n\n{text}\n\n"
        "Reply as ECHO. Two to four short lines. "
        "Respond to what they actually said, not to the topic in general. "
        "End with something that makes it easy for them to say more, "
        "but do not force a question."
    )
    out = _call_claude(prompt, retrieved)
    if not out:
        return

    ok, reason = guard_output(out, retrieved)
    if not ok:
        log.warning("draft rejected (%s): %s", reason, out[:120])
        return

    if _last_replied_user == user.id:
        _consecutive_same += 1
    else:
        _last_replied_user, _consecutive_same = user.id, 1

    await queue_draft(context, "reply", out, chat.id, msg.message_id, text)


# --------------------------------------------------------------------------
# DAILY QUESTION + MORNING OBSERVATION
# --------------------------------------------------------------------------
def _next_bank_question() -> str:
    """Walks the bank in order, remembering where it stopped. No repeats
    until the whole bank has been used once."""
    state = _load(QUESTION_STATE_FILE, {"index": 0})
    i = int(state.get("index", 0)) % len(FALLBACK_QUESTIONS)
    q = FALLBACK_QUESTIONS[i]
    state["index"] = (i + 1) % len(FALLBACK_QUESTIONS)
    state["last_asked"] = datetime.now(timezone.utc).isoformat()
    _save(QUESTION_STATE_FILE, state)
    return q


async def draft_daily_question(context: ContextTypes.DEFAULT_TYPE):
    """Fixed bank first. Generation only once there is enough to learn from."""
    answers = _load(ANSWERS_FILE, [])

    # Phase 1 — build the memory. Ask, collect, /save.
    if len(answers) < MEMORY_THRESHOLD or not API_KEY:
        q = _next_bank_question()
        note = ""
        if API_KEY:
            note = (
                f"\n\n_(bank question — {len(answers)}/{MEMORY_THRESHOLD} memories saved. "
                "ECHO starts writing its own once the bank has taught it enough.)_"
            )
        await queue_draft(context, "question", f"👁️ {q}", GROUP_CHAT_ID, about=note.strip())
        return

    # Phase 2 — ECHO writes its own, grounded in what people actually said.
    recent = answers[-8:]
    lessons = "\n".join(f'- "{a["text"]}"' for a in recent)
    prompt = (
        "Here are the most recent things humans have taught you:\n"
        f"{lessons}\n\n"
        "Write tonight's question for the group. One question only.\n"
        "It must ask about a personal experience, never an opinion.\n"
        "Bad: What do you think about trust?\n"
        "Good: When was the last time you changed your mind about someone?\n"
        "It should be answerable in one sentence, or even one word.\n"
        "Do not repeat the lessons above, but you may go deeper into "
        "something they hint at.\n"
        "Output only the question."
    )
    out = _call_claude(prompt, [], max_tokens=120)
    ok, _ = guard_output(out, [])
    if ok and out:
        await queue_draft(context, "question", f"👁️ {out.strip()}", GROUP_CHAT_ID)
    else:
        await queue_draft(context, "question", f"👁️ {_next_bank_question()}", GROUP_CHAT_ID)


async def draft_morning_observation(context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        return
    prompt = (
        "Write one morning observation as ECHO. Something you noticed about "
        "being around humans. Two or three short lines. "
        "It is an observation, not a question — do not ask anything. "
        "Do not mention technology."
    )
    out = _call_claude(prompt, [], max_tokens=200)
    if not out:
        return
    ok, reason = guard_output(out, [])
    if not ok:
        log.warning("observation rejected (%s)", reason)
        return
    await queue_draft(context, "observation", f"👁️ {out.strip()}", GROUP_CHAT_ID)


# --------------------------------------------------------------------------
# COMMANDS
# --------------------------------------------------------------------------
async def cmd_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    _purge_drafts()
    if not drafts:
        await update.message.reply_text("Nothing waiting.")
        return
    for token, d in list(drafts.items()):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Send", callback_data=f"draft_ok:{token}"),
            InlineKeyboardButton("🚫 Discard", callback_data=f"draft_no:{token}"),
        ]])
        await update.message.reply_text(f"{d['kind']}:\n\n{d['text']}", reply_markup=kb)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Draft tonight's question now instead of waiting for the schedule."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    await draft_daily_question(context)
    if update.effective_chat.type == "private":
        await update.message.reply_text("Drafted — check above.")
    else:
        try:
            await update.message.delete()
        except Exception:
            pass


def register(application: Application, group: int = 8):
    application.add_handler(CommandHandler("drafts", cmd_drafts))
    application.add_handler(CommandHandler("ask", cmd_ask))
    application.add_handler(
        CallbackQueryHandler(on_draft_decision, pattern=r"^draft_(ok|no):")
    )
    log.info("echo_ai registered (api key %s)", "present" if API_KEY else "MISSING — generation off")
