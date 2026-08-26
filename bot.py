import os
import re
import json
import random
import asyncio
import logging
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

# ============================================================
# My Quiz Bot - clean rebuild
# ============================================================
# Required Render environment variables:
# BOT_TOKEN
# DATABASE_URL
# ADMIN_USER_ID   (optional; 0 = everyone allowed)
# RENDER_EXTERNAL_URL (Render provides this automatically)
# PORT (optional)
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("quizbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
PORT = int(os.environ.get("PORT", "10000"))
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

QUIZ_DAYS = 30
REATTEMPT_HOURS = 24
IMPORT_LIMIT = 1_000_000

POOL = None

BUILTIN_CATEGORIES = [
    "सामान्य ज्ञान", "इतिहास", "भूगोल", "राजव्यवस्था", "विज्ञान",
    "पर्यावरण", "अर्थव्यवस्था", "Current Affairs", "मनोविज्ञान",
    "हिंदी", "English", "RO/ARO", "अन्य",
]


def db():
    if POOL is not None:
        return POOL.connection()
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def allowed(update: Update) -> bool:
    return bool(update.effective_user) and (ADMIN_USER_ID == 0 or update.effective_user.id == ADMIN_USER_ID)


async def guard(update: Update) -> bool:
    if allowed(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "यह private bot है। पहले /id भेजें और अपना Telegram ID देखें।"
        )
    return False


def clean(s):
    return re.sub(r"\s+", " ", str(s or "").strip())


def normalize_text(s):
    return clean(s).replace("\ufe0f", "").replace("\u200d", "")


def init_db():
    global POOL
    if POOL is None:
        POOL = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        POOL.open(wait=True)

    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'अन्य',
                subcategory TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days')
            );
            CREATE TABLE IF NOT EXISTS questions (
                id BIGSERIAL PRIMARY KEY,
                quiz_id BIGINT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
                q_no INTEGER NOT NULL,
                question TEXT NOT NULL,
                options JSONB NOT NULL,
                answer INTEGER NOT NULL,
                explanation TEXT NOT NULL DEFAULT '',
                UNIQUE(quiz_id, q_no)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                user_id BIGINT PRIMARY KEY,
                quiz_id BIGINT REFERENCES quizzes(id) ON DELETE SET NULL,
                question_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                position INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'quiz',
                option_order JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS wrong_answers (
                user_id BIGINT NOT NULL,
                question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                wrong_count INTEGER NOT NULL DEFAULT 1,
                last_wrong TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(user_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                timer_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                timer_seconds INTEGER NOT NULL DEFAULT 30,
                random_questions BOOLEAN NOT NULL DEFAULT FALSE,
                random_options BOOLEAN NOT NULL DEFAULT FALSE,
                explanation_enabled BOOLEAN NOT NULL DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS quiz_categories (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS quiz_topics (
                id BIGSERIAL PRIMARY KEY,
                category_id BIGINT NOT NULL REFERENCES quiz_categories(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE(category_id, name)
            );

            ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS category TEXT;
            ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS subcategory TEXT;
            ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
            ALTER TABLE attempts ADD COLUMN IF NOT EXISTS option_order JSONB NOT NULL DEFAULT '[]'::jsonb;
            ALTER TABLE attempts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
            ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS timer_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS timer_seconds INTEGER NOT NULL DEFAULT 30;
            ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS random_questions BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS random_options BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS explanation_enabled BOOLEAN NOT NULL DEFAULT TRUE;

            UPDATE quizzes SET category = COALESCE(NULLIF(category,''),'अन्य');
            UPDATE quizzes SET subcategory = COALESCE(subcategory,'');
            UPDATE quizzes SET expires_at = COALESCE(expires_at, created_at + interval '30 days');

            CREATE INDEX IF NOT EXISTS idx_quizzes_expiry ON quizzes(expires_at);
            CREATE INDEX IF NOT EXISTS idx_quizzes_category ON quizzes(category);
            CREATE INDEX IF NOT EXISTS idx_questions_quiz ON questions(quiz_id,q_no);
            CREATE INDEX IF NOT EXISTS idx_wrong_user ON wrong_answers(user_id,last_wrong);
        """)
        conn.commit()

    # Seed built-in categories, without overwriting user-created categories.
    with db() as conn:
        for name in BUILTIN_CATEGORIES:
            conn.execute(
                "INSERT INTO quiz_categories(name) VALUES(%s) ON CONFLICT(name) DO NOTHING",
                (name,),
            )
        conn.commit()


def cleanup_expired():
    with db() as conn:
        conn.execute("DELETE FROM quizzes WHERE expires_at <= now()")
        conn.commit()


def get_settings(uid):
    with db() as conn:
        conn.execute(
            "INSERT INTO user_settings(user_id) VALUES(%s) ON CONFLICT(user_id) DO NOTHING",
            (uid,),
        )
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=%s", (uid,)).fetchone()
        conn.commit()
        return row


def update_setting(uid, field, value):
    if field not in {"timer_enabled", "timer_seconds", "random_questions", "random_options", "explanation_enabled"}:
        return
    with db() as conn:
        conn.execute(
            f"INSERT INTO user_settings(user_id,{field}) VALUES(%s,%s) "
            f"ON CONFLICT(user_id) DO UPDATE SET {field}=EXCLUDED.{field}",
            (uid, value),
        )
        conn.commit()


def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["📝 Quiz", "➕ Add Quiz"],
            ["🔄 ReAttempt", "📊 Stats"],
            ["📚 Categories", "⚙️ Settings"],
            ["❓ Help"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def import_menu():
    return ReplyKeyboardMarkup(
        [["✅ Quiz Done", "❌ Cancel Import"]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ---------------- Parser ----------------

def answer_index(token):
    t = clean(token).upper()
    t = {"क":"A", "ख":"B", "ग":"C", "घ":"D"}.get(t, t)
    m = re.search(r"[ABCD1-4]", t)
    if not m:
        return None
    t = m.group(0)
    if t in "ABCD":
        return ord(t) - 65
    return int(t) - 1


def meta(text, label, default=""):
    m = re.search(rf"(?im)^\s*{label}\s*[:=-]\s*(.+?)\s*$", text)
    return clean(m.group(1)) if m else default


def normalize_category(value):
    v = clean(value)
    aliases = {
        "gk":"सामान्य ज्ञान", "general knowledge":"सामान्य ज्ञान",
        "history":"इतिहास", "geography":"भूगोल", "polity":"राजव्यवस्था",
        "constitution":"राजव्यवस्था", "science":"विज्ञान", "environment":"पर्यावरण",
        "economics":"अर्थव्यवस्था", "economy":"अर्थव्यवस्था", "psychology":"मनोविज्ञान",
        "hindi":"हिंदी", "english":"English", "roaro":"RO/ARO", "ro/aro":"RO/ARO",
    }
    return aliases.get(v.lower(), v or "अन्य")


def parse_quiz(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("Quiz text खाली है।")

    title = meta(text, r"(?:QUIZ|TITLE|शीर्षक|क्विज)", "My Quiz")
    exam = meta(text, r"(?:EXAM|परीक्षा)")
    category = normalize_category(meta(text, r"(?:CATEGORY|CAT|श्रेणी|कैटेगरी|वर्ग)"))
    subcategory = meta(text, r"(?:SUBCATEGORY|SUB-CATEGORY|उपश्रेणी|उप-श्रेणी|उपवर्ग)")
    if category == "अन्य" and exam.upper().replace(" ", "") in {"RO/ARO", "ROARO"}:
        category = "RO/ARO"

    qre = re.compile(
        r"(?im)^\s*(?:Q(?:UESTION)?\s*(\d+)?|प्रश्न\s*(\d+)|(?<!\w)(\d+))\s*[\.:)\-]\s*(.+?)\s*$"
    )
    matches = list(qre.finditer(text))
    matches = [m for m in matches if not re.match(r"(?i)^\s*(?:answer key|answers?|उत्तर कुंजी)", m.group(0))]
    if not matches:
        raise ValueError("सवाल नहीं मिले। Q1., Q1), 1., 1), प्रश्न 1: जैसे format रखें।")

    answer_key = {}
    for m in re.finditer(r"(?im)(?:ANSWER\s*KEY|उत्तर\s*कुंजी)\s*[:\-]?\s*(.+)$", text):
        for q, a in re.findall(r"(\d+)\s*[-=:]\s*([ABCD1-4कखगघ])", m.group(1), re.I):
            answer_key[int(q)] = answer_index(a)

    option_re = re.compile(
        r"(?im)^\s*(?:[•▪️🔹🔸➡️👉✔️✓]\s*)?\(?([ABCDकखगघ])\)?\s*[\.\:)\-–—]\s*(.+?)\s*$"
    )
    answer_re = re.compile(
        r"(?im)^\s*(?:ANSWER|ANS|CORRECT\s*ANSWER|RIGHT\s*ANSWER|CORRECT|सही\s*उत्तर|उत्तर|सही\s*विकल्प)\s*[:=\-–—]?\s*(?:OPTION|विकल्प)?\s*([ABCD1-4कखगघ])\b"
    )
    explanation_re = re.compile(
        r"(?ims)^\s*(?:EXPLANATION|WHY|व्याख्या|कारण|विवरण|स्पष्टीकरण)\s*[:\-]?\s*(.*?)(?=^\s*(?:ANSWER|ANS|CORRECT\s*ANSWER|RIGHT\s*ANSWER|सही\s*उत्तर|उत्तर)\s*[:=\-]|\Z)"
    )

    questions = []
    for i, m in enumerate(matches):
        end = matches[i+1].start() if i + 1 < len(matches) else len(text)
        block = text[m.end():end]
        qno = next((int(x) for x in m.groups()[:3] if x), i + 1)
        qtext = clean(m.group(4))

        opts = {}
        for letter, value in option_re.findall(block):
            letter = {"क":"A", "ख":"B", "ग":"C", "घ":"D"}.get(letter, letter.upper())
            opts[letter] = clean(value)
        if set(opts) != {"A","B","C","D"}:
            raise ValueError(f"प्रश्न {qno} में A/B/C/D चारों options नहीं मिले।")

        # Text between question line and first option belongs to question.
        first_opt = option_re.search(block)
        if first_opt and clean(block[:first_opt.start()]):
            qtext = clean(qtext + " " + block[:first_opt.start()])

        ans = None
        am = answer_re.search(block)
        if am:
            ans = answer_index(am.group(1))
        if ans is None:
            ans = answer_key.get(qno)
        if ans not in {0,1,2,3}:
            raise ValueError(f"प्रश्न {qno} का सही उत्तर नहीं मिला।")

        em = explanation_re.search(block)
        explanation = clean(em.group(1)) if em else ""
        questions.append({
            "q_no": len(questions)+1,
            "question": qtext,
            "options": [opts["A"],opts["B"],opts["C"],opts["D"]],
            "answer": ans,
            "explanation": explanation,
        })

    if len(questions) > 100:
        raise ValueError("एक Quiz में अधिकतम 100 सवाल रखें।")

    return {
        "title": title,
        "category": category,
        "subcategory": subcategory,
        "questions": questions,
    }


# ---------------- Import ----------------

async def import_start(update, context):
    if not await guard(update): return
    context.user_data.clear()
    context.user_data["importing"] = True
    context.user_data["import_text"] = ""
    await update.effective_message.reply_text(
        "📥 Quiz paste mode शुरू।\n\n"
        "पूरा Quiz एक या कई messages में भेजें।\n"
        "सब भेजने के बाद नीचे ✅ Quiz Done दबाएँ।\n"
        "❌ Cancel Import से रद्द करें।",
        reply_markup=import_menu(),
    )


async def import_done(update, context):
    if not await guard(update): return
    if not context.user_data.get("importing"):
        await update.effective_message.reply_text("अभी कोई Quiz import नहीं चल रहा है।", reply_markup=main_menu())
        return
    raw = context.user_data.get("import_text", "").strip()
    try:
        parsed = parse_quiz(raw)
    except Exception as e:
        await update.effective_message.reply_text(
            f"❌ Quiz format में समस्या:\n{e}\n\nText ठीक करके भेजें या Cancel करें।",
            reply_markup=import_menu(),
        )
        return

    context.user_data["pending_quiz"] = parsed
    cat = parsed["category"]
    if cat and cat != "अन्य":
        await save_pending_quiz(update, context, cat)
        return

    await update.effective_message.reply_text(
        f"📚 {parsed['title']}\n❓ {len(parsed['questions'])} सवाल\n\nCategory चुनें:",
        reply_markup=category_keyboard("savecat"),
    )


def category_keyboard(prefix="cat"):
    with db() as conn:
        rows = conn.execute("SELECT name FROM quiz_categories ORDER BY name").fetchall()
    names = []
    seen = set()
    for n in BUILTIN_CATEGORIES + [r["name"] for r in rows]:
        if n not in seen:
            seen.add(n); names.append(n)
    kb=[]; row=[]
    for n in names:
        row.append(InlineKeyboardButton(n, callback_data=f"{prefix}:{n}"))
        if len(row)==2:
            kb.append(row); row=[]
    if row: kb.append(row)
    return InlineKeyboardMarkup(kb)


async def save_pending_quiz(update, context, category):
    parsed = context.user_data.get("pending_quiz")
    if not parsed:
        await update.effective_message.reply_text("❌ Pending Quiz नहीं मिला। /import से फिर शुरू करें।", reply_markup=main_menu())
        return
    category = normalize_category(category)
    subcategory = clean(parsed.get("subcategory"))
    with db() as conn:
        conn.execute("INSERT INTO quiz_categories(name) VALUES(%s) ON CONFLICT(name) DO NOTHING", (category,))
        if subcategory:
            cid = conn.execute("SELECT id FROM quiz_categories WHERE name=%s", (category,)).fetchone()["id"]
            conn.execute("INSERT INTO quiz_topics(category_id,name) VALUES(%s,%s) ON CONFLICT(category_id,name) DO NOTHING", (cid, subcategory))
        qz = conn.execute(
            "INSERT INTO quizzes(title,category,subcategory,expires_at) VALUES(%s,%s,%s,now()+interval '30 days') RETURNING id",
            (parsed["title"], category, subcategory),
        ).fetchone()
        for q in parsed["questions"]:
            conn.execute(
                "INSERT INTO questions(quiz_id,q_no,question,options,answer,explanation) VALUES(%s,%s,%s,%s,%s,%s)",
                (qz["id"], q["q_no"], q["question"], json.dumps(q["options"], ensure_ascii=False), q["answer"], q["explanation"]),
            )
        conn.commit()
    context.user_data.clear()
    await update.effective_message.reply_text(
        f"🎉 Quiz save हो गया!\n\n📚 {parsed['title']}\n🏷 {category}\n❓ {len(parsed['questions'])} सवाल",
        reply_markup=main_menu(),
    )


# ---------------- Categories / Quiz selection ----------------

async def categories(update, context):
    if not await guard(update): return
    cleanup_expired()
    with db() as conn:
        rows = conn.execute("SELECT category,COUNT(*) n FROM quizzes WHERE expires_at>now() GROUP BY category ORDER BY category").fetchall()
    if not rows:
        await update.effective_message.reply_text("अभी कोई Quiz नहीं है। पहले ➕ Add Quiz करें।", reply_markup=main_menu())
        return
    kb=[]; row=[]
    for r in rows:
        row.append(InlineKeyboardButton(f"📚 {r['category']} ({r['n']})", callback_data=f"cat:{r['category']}"))
        if len(row)==2: kb.append(row); row=[]
    if row: kb.append(row)
    await update.effective_message.reply_text("📚 Category चुनें:", reply_markup=InlineKeyboardMarkup(kb))


async def category_callback(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    category=q.data.split(":",1)[1]
    with db() as conn:
        topics=conn.execute("SELECT DISTINCT subcategory FROM quizzes WHERE category=%s AND expires_at>now() AND subcategory<>'' ORDER BY subcategory", (category,)).fetchall()
    kb=[[InlineKeyboardButton("📚 सभी Topics", callback_data=f"topic:{category}:__ALL__")]]
    row=[]
    for t in topics:
        row.append(InlineKeyboardButton(f"📖 {t['subcategory']}", callback_data=f"topic:{category}:{t['subcategory']}"))
        if len(row)==2: kb.append(row); row=[]
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("⬅️ Categories", callback_data="catlist")])
    await q.edit_message_text(f"📚 {category}\n\nTopic चुनें:", reply_markup=InlineKeyboardMarkup(kb))


async def topic_callback(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    _, category, topic = q.data.split(":",2)
    with db() as conn:
        if topic == "__ALL__":
            rows=conn.execute("SELECT z.id,z.title,z.subcategory,COUNT(q.id) n FROM quizzes z LEFT JOIN questions q ON q.quiz_id=z.id WHERE z.category=%s AND z.expires_at>now() GROUP BY z.id ORDER BY z.id DESC", (category,)).fetchall()
        else:
            rows=conn.execute("SELECT z.id,z.title,z.subcategory,COUNT(q.id) n FROM quizzes z LEFT JOIN questions q ON q.quiz_id=z.id WHERE z.category=%s AND z.subcategory=%s AND z.expires_at>now() GROUP BY z.id ORDER BY z.id DESC", (category,topic)).fetchall()
    if not rows:
        await q.edit_message_text("इस Topic में कोई Quiz नहीं है।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Topics",callback_data=f"cat:{category}")]]))
        return
    kb=[]
    for r in rows:
        title=clean(r["title"]); title=title[:35]+"..." if len(title)>38 else title
        extra=f" • {r['subcategory']}" if topic=="__ALL__" and r["subcategory"] else ""
        kb.append([InlineKeyboardButton(f"▶️ {title}{extra} ({r['n']})", callback_data=f"play:{r['id']}")])
    kb.append([InlineKeyboardButton("⬅️ Topics",callback_data=f"cat:{category}")])
    await q.edit_message_text("📝 Quiz चुनें:", reply_markup=InlineKeyboardMarkup(kb))


async def catlist_callback(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    await q.edit_message_text("📚 Category चुनें:", reply_markup=category_keyboard("cat"))


# ---------------- Quiz engine ----------------

async def start_selected_quiz(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    quiz_id=int(q.data.split(":")[1])
    uid=update.effective_user.id
    chat_id=update.effective_chat.id
    with db() as conn:
        quiz=conn.execute("SELECT * FROM quizzes WHERE id=%s AND expires_at>now()", (quiz_id,)).fetchone()
        qs=conn.execute("SELECT id FROM questions WHERE quiz_id=%s ORDER BY q_no", (quiz_id,)).fetchall()
    if not quiz or not qs:
        await q.edit_message_text("❌ यह Quiz अब उपलब्ध नहीं है।")
        return
    ids=[r["id"] for r in qs]
    settings=get_settings(uid)
    if settings["random_questions"]:
        random.shuffle(ids)
    with db() as conn:
        conn.execute(
            "INSERT INTO attempts(user_id,quiz_id,question_ids,position,score,mode,option_order,updated_at) VALUES(%s,%s,%s,0,0,'quiz','[]',now()) "
            "ON CONFLICT(user_id) DO UPDATE SET quiz_id=EXCLUDED.quiz_id,question_ids=EXCLUDED.question_ids,position=0,score=0,mode='quiz',option_order='[]',updated_at=now()",
            (uid, quiz_id, json.dumps(ids)),
        )
        conn.commit()
    await q.edit_message_text(f"▶️ {quiz['title']}\n\n🧠 Quiz शुरू हो रहा है...")
    await send_current(uid, chat_id, context)


async def send_current(uid, chat_id, context):
    cancel_timer(context)
    with db() as conn:
        attempt=conn.execute("SELECT * FROM attempts WHERE user_id=%s",(uid,)).fetchone()
    if not attempt: return
    ids=[int(x) for x in (attempt["question_ids"] or [])]
    pos=int(attempt["position"])
    if pos>=len(ids):
        await finish(uid,chat_id,context)
        return
    qid=ids[pos]
    with db() as conn:
        q=conn.execute("SELECT q.*,z.title quiz_title,z.category,z.subcategory FROM questions q JOIN quizzes z ON z.id=q.quiz_id WHERE q.id=%s AND z.expires_at>now()",(qid,)).fetchone()
    if not q:
        await finish(uid,chat_id,context); return
    opts=list(q["options"])
    order=list(range(4))
    settings=get_settings(uid)
    if settings["random_options"]: random.shuffle(order)
    with db() as conn:
        conn.execute("UPDATE attempts SET option_order=%s,updated_at=now() WHERE user_id=%s",(json.dumps(order),uid)); conn.commit()
    kb=[]
    for display, actual in enumerate(order):
        kb.append([InlineKeyboardButton(f"{'ABCD'[display]}. {opts[actual]}",callback_data=f"ans:{qid}:{display}")])
    text=(f"{'🔄 ReAttempt' if attempt['mode']=='revision' else '🧠 Quiz'}\n"
          f"📚 {q['quiz_title']}\n🏷 {q['category']}{' • '+q['subcategory'] if q['subcategory'] else ''}\n\n"
          f"❓ {pos+1}/{len(ids)}\n\n{q['question']}")
    if settings["timer_enabled"]: text+=f"\n\n⏱ समय: {settings['timer_seconds']} सेकंड"
    await context.bot.send_message(chat_id=chat_id,text=text,reply_markup=InlineKeyboardMarkup(kb))
    if settings["timer_enabled"]:
        context.user_data["timer_task"]=asyncio.create_task(timer_task(uid,chat_id,qid,context,int(settings["timer_seconds"])))


def cancel_timer(context):
    task=context.user_data.pop("timer_task",None)
    if task and not task.done(): task.cancel()


async def timer_task(uid,chat_id,qid,context,seconds):
    try:
        await asyncio.sleep(seconds)
        with db() as conn:
            a=conn.execute("SELECT * FROM attempts WHERE user_id=%s FOR UPDATE",(uid,)).fetchone()
            if not a: return
            ids=[int(x) for x in (a["question_ids"] or [])]
            pos=int(a["position"])
            if pos>=len(ids) or ids[pos]!=qid: return
            conn.execute("UPDATE attempts SET position=position+1,updated_at=now() WHERE user_id=%s",(uid,))
            conn.execute("INSERT INTO wrong_answers(user_id,question_id,wrong_count,last_wrong) VALUES(%s,%s,1,now()) ON CONFLICT(user_id,question_id) DO UPDATE SET wrong_count=wrong_answers.wrong_count+1,last_wrong=now()",(uid,qid))
            conn.commit()
        await context.bot.send_message(chat_id=chat_id,text="⏰ समय समाप्त! यह सवाल गलत में जोड़ दिया गया है।")
        await send_current(uid,chat_id,context)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("timer failed")


async def answer_callback(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    try:
        _,qid_s,display_s=q.data.split(":")
        qid=int(qid_s); display=int(display_s)
    except Exception:
        await q.edit_message_text("❌ Answer invalid है।"); return
    uid=update.effective_user.id; chat_id=update.effective_chat.id
    cancel_timer(context)
    with db() as conn:
        attempt=conn.execute("SELECT * FROM attempts WHERE user_id=%s FOR UPDATE",(uid,)).fetchone()
        if not attempt:
            await q.edit_message_text("Quiz session खत्म हो चुका है। /quiz से फिर शुरू करें।"); return
        ids=[int(x) for x in (attempt["question_ids"] or [])]
        pos=int(attempt["position"])
        if pos>=len(ids) or ids[pos]!=qid:
            await q.edit_message_text("यह सवाल अब active नहीं है।"); return
        order=[int(x) for x in (attempt["option_order"] or [0,1,2,3])]
        if display not in range(4) or len(order)!=4:
            await q.edit_message_text("❌ Answer invalid है।"); return
        selected=order[display]
        question=conn.execute("SELECT * FROM questions WHERE id=%s",(qid,)).fetchone()
        if not question:
            await q.edit_message_text("❌ सवाल नहीं मिला।"); return
        correct=selected==int(question["answer"])
        score=int(attempt["score"])+(1 if correct else 0)
        conn.execute("UPDATE attempts SET position=position+1,score=%s,updated_at=now() WHERE user_id=%s",(score,uid))
        if correct:
            conn.execute("DELETE FROM wrong_answers WHERE user_id=%s AND question_id=%s",(uid,qid))
        else:
            conn.execute("INSERT INTO wrong_answers(user_id,question_id,wrong_count,last_wrong) VALUES(%s,%s,1,now()) ON CONFLICT(user_id,question_id) DO UPDATE SET wrong_count=wrong_answers.wrong_count+1,last_wrong=now()",(uid,qid))
        conn.commit()
    result="✅ सही!" if correct else f"❌ गलत! सही उत्तर: {'ABCD'[int(question['answer'])]}"
    settings=get_settings(uid)
    text=result
    if settings["explanation_enabled"] and question["explanation"]:
        text+=f"\n\n💡 {question['explanation']}"
    await q.edit_message_text(text)
    await send_current(uid,chat_id,context)


async def finish(uid,chat_id,context):
    cancel_timer(context)
    with db() as conn:
        a=conn.execute("SELECT * FROM attempts WHERE user_id=%s",(uid,)).fetchone()
    if not a: return
    total=len(a["question_ids"] or [])
    score=int(a["score"])
    pct=round(score*100/total) if total else 0
    await context.bot.send_message(chat_id=chat_id,text=f"🏁 Quiz पूरा!\n\n📊 Score: {score}/{total}\n📈 प्रतिशत: {pct}%\n\n🔄 गलत सवाल 24 घंटे बाद ReAttempt में आएंगे।",reply_markup=main_menu())
    with db() as conn:
        conn.execute("DELETE FROM attempts WHERE user_id=%s",(uid,)); conn.commit()


# ---------------- ReAttempt ----------------

async def revision(update, context):
    if not await guard(update): return
    uid=update.effective_user.id
    with db() as conn:
        rows=conn.execute("SELECT w.question_id FROM wrong_answers w JOIN questions q ON q.id=w.question_id JOIN quizzes z ON z.id=q.quiz_id WHERE w.user_id=%s AND z.expires_at>now() AND w.last_wrong<=now()-interval '24 hours' ORDER BY w.last_wrong LIMIT 50",(uid,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("🔄 अभी कोई 24 घंटे पुराना गलत सवाल नहीं है।",reply_markup=main_menu()); return
    ids=[r["question_id"] for r in rows]
    settings=get_settings(uid)
    if settings["random_questions"]: random.shuffle(ids)
    with db() as conn:
        conn.execute("INSERT INTO attempts(user_id,quiz_id,question_ids,position,score,mode,option_order,updated_at) VALUES(%s,NULL,%s,0,0,'revision','[]',now()) ON CONFLICT(user_id) DO UPDATE SET quiz_id=NULL,question_ids=EXCLUDED.question_ids,position=0,score=0,mode='revision',option_order='[]',updated_at=now()",(uid,json.dumps(ids))); conn.commit()
    await update.effective_message.reply_text(f"🔄 ReAttempt शुरू।\n\n❓ {len(ids)} गलत सवाल तैयार हैं।")
    await send_current(uid,update.effective_chat.id,context)


# ---------------- Settings ----------------

def settings_markup(s):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏱ Timer: {'ON' if s['timer_enabled'] else 'OFF'}",callback_data="set:timer")],
        [InlineKeyboardButton(f"🔀 Random Questions: {'ON' if s['random_questions'] else 'OFF'}",callback_data="set:rq")],
        [InlineKeyboardButton(f"🔀 Random Options: {'ON' if s['random_options'] else 'OFF'}",callback_data="set:ro")],
        [InlineKeyboardButton(f"💡 Explanation: {'ON' if s['explanation_enabled'] else 'OFF'}",callback_data="set:ex")],
        [InlineKeyboardButton(f"⏱ Timer Time: {s['timer_seconds']} sec",callback_data="set:time")],
    ])


async def settings(update, context):
    if not await guard(update): return
    s=get_settings(update.effective_user.id)
    await update.effective_message.reply_text(
        "⚙️ Quiz Settings\n\n"
        "⏱ Timer = हर सवाल की समय सीमा\n"
        "🔀 Random Questions = सवालों का क्रम बदलना\n"
        "🔀 Random Options = A/B/C/D का क्रम बदलना\n"
        "💡 Explanation = सही/गलत के बाद explanation दिखाना",
        reply_markup=settings_markup(s),
    )


async def settings_callback(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    uid=update.effective_user.id
    action=q.data.split(":",1)[1]
    if action=="time":
        kb=[]; row=[]
        for sec in [10,20,30,45,60,90]:
            row.append(InlineKeyboardButton(f"{sec}s",callback_data=f"timer:{sec}"))
            if len(row)==3: kb.append(row); row=[]
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("⬅️ Settings",callback_data="settings:back")])
        await q.edit_message_text("⏱ Timer Time चुनें:",reply_markup=InlineKeyboardMarkup(kb)); return
    fields={"timer":"timer_enabled","rq":"random_questions","ro":"random_options","ex":"explanation_enabled"}
    if action in fields:
        s=get_settings(uid); field=fields[action]; update_setting(uid,field,not bool(s[field]))
    await q.edit_message_text("⚙️ Settings updated.",reply_markup=settings_markup(get_settings(uid)))


async def timer_callback(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    sec=int(q.data.split(":")[1]); update_setting(update.effective_user.id,"timer_seconds",sec)
    await q.edit_message_text(f"⏱ Timer अब {sec} सेकंड है।",reply_markup=settings_markup(get_settings(update.effective_user.id)))


async def settings_back(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    await q.edit_message_text("⚙️ Quiz Settings",reply_markup=settings_markup(get_settings(update.effective_user.id)))


# ---------------- Start ----------------

async def start(update, context):
    if not await guard(update):
        return
    cancel_timer(context)
    context.user_data.clear()
    await update.effective_message.reply_text(
        "👋 Welcome to My Quiz Bot!\n\n"
        "नीचे menu से Quiz चुनें या ➕ Add Quiz से नया Quiz डालें।",
        reply_markup=main_menu(),
    )


# ---------------- Stats / Help / Delete ----------------

async def stats(update, context):
    if not await guard(update): return
    cleanup_expired(); uid=update.effective_user.id
    with db() as conn:
        quizzes=conn.execute("SELECT COUNT(*) n FROM quizzes WHERE expires_at>now()").fetchone()["n"]
        questions=conn.execute("SELECT COUNT(*) n FROM questions q JOIN quizzes z ON z.id=q.quiz_id WHERE z.expires_at>now()").fetchone()["n"]
        wrong=conn.execute("SELECT COUNT(*) n FROM wrong_answers w JOIN questions q ON q.id=w.question_id JOIN quizzes z ON z.id=q.quiz_id WHERE w.user_id=%s AND z.expires_at>now()",(uid,)).fetchone()["n"]
        ready=conn.execute("SELECT COUNT(*) n FROM wrong_answers w JOIN questions q ON q.id=w.question_id JOIN quizzes z ON z.id=q.quiz_id WHERE w.user_id=%s AND z.expires_at>now() AND w.last_wrong<=now()-interval '24 hours'",(uid,)).fetchone()["n"]
    await update.effective_message.reply_text(f"📊 Progress\n\n📚 Quizzes: {quizzes}\n❓ Questions: {questions}\n❌ गलत सवाल: {wrong}\n🔄 ReAttempt ready: {ready}",reply_markup=main_menu())


async def help_command(update, context):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "❓ Help\n\n"
        "/start — Main menu\n/import — Quiz डालें\n/done — Import पूरा करें\n/quiz — Quiz खेलें\n/revision — 24 घंटे पुराने गलत सवाल\n/stats — Progress\n/categories — Categories\n/settings — Settings\n/deletequiz — Quiz delete\n/cancel — Current session cancel\n/id — Telegram ID",
        reply_markup=main_menu(),
    )


async def show_id(update, context):
    await update.effective_message.reply_text(f"आपका Telegram ID: {update.effective_user.id}",reply_markup=main_menu())


async def cancel(update, context):
    if not await guard(update): return
    cancel_timer(context); context.user_data.clear()
    await update.effective_message.reply_text("❌ Session cancel कर दिया गया।",reply_markup=main_menu())


async def delete_quiz(update, context):
    if not await guard(update): return
    with db() as conn:
        rows=conn.execute("SELECT z.id,z.title,COUNT(q.id) n FROM quizzes z LEFT JOIN questions q ON q.quiz_id=z.id WHERE z.expires_at>now() GROUP BY z.id ORDER BY z.id DESC LIMIT 30").fetchall()
    if not rows:
        await update.effective_message.reply_text("🗑 कोई Quiz नहीं है।",reply_markup=main_menu()); return
    kb=[]
    for r in rows:
        title=clean(r["title"]); title=title[:32]+"..." if len(title)>35 else title
        kb.append([InlineKeyboardButton(f"🗑 {title} ({r['n']})",callback_data=f"delask:{r['id']}")])
    await update.effective_message.reply_text("🗑 Delete करने वाला Quiz चुनें:",reply_markup=InlineKeyboardMarkup(kb))


async def delete_callback(update, context):
    q=update.callback_query; await q.answer()
    if not allowed(update): return
    action, sid=q.data.split(":"); quiz_id=int(sid)
    if action=="delcancel":
        await q.edit_message_text("❌ Delete cancel कर दिया गया।", reply_markup=main_menu())
        return
    if action=="delask":
        with db() as conn: row=conn.execute("SELECT title FROM quizzes WHERE id=%s",(quiz_id,)).fetchone()
        if not row: await q.edit_message_text("Quiz नहीं मिला।"); return
        await q.edit_message_text(f"⚠️ Delete करें?\n\n📚 {row['title']}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("हाँ, Delete",callback_data=f"delconfirm:{quiz_id}"),InlineKeyboardButton("❌ Cancel",callback_data="delcancel:0")]]))
    else:
        with db() as conn: conn.execute("DELETE FROM quizzes WHERE id=%s",(quiz_id,)); conn.commit()
        await q.edit_message_text("✅ Quiz delete हो गया।")


# ---------------- Text router ----------------

async def text_router(update, context):
    if not await guard(update): return
    text=normalize_text(update.effective_message.text)

    if text=="📝 Quiz":
        await categories(update,context); return
    if text=="➕ Add Quiz":
        await import_start(update,context); return
    if text=="🔄 ReAttempt":
        await revision(update,context); return
    if text=="📊 Stats":
        await stats(update,context); return
    if text=="📚 Categories":
        await categories(update,context); return
    if text in {"⚙️ Settings","⚙ Settings","Settings"}:
        await settings(update,context); return
    if text=="❓ Help":
        await help_command(update,context); return
    if text=="✅ Quiz Done":
        await import_done(update,context); return
    if text=="❌ Cancel Import":
        await cancel(update,context); return

    if context.user_data.get("importing"):
        current=context.user_data.get("import_text","")
        total=(current+"\n"+update.effective_message.text).strip()
        if len(total)>IMPORT_LIMIT:
            await update.effective_message.reply_text("❌ Import limit 10 लाख characters है।",reply_markup=import_menu()); return
        context.user_data["import_text"]=total
        await update.effective_message.reply_text("✅ हिस्सा मिल गया। और भेजें या Quiz Done दबाएँ।",reply_markup=import_menu())
        return

    await update.effective_message.reply_text("नीचे menu से option चुनें।",reply_markup=main_menu())


# ---------------- Startup ----------------

async def post_init(app):
    init_db(); cleanup_expired()


def main():
    if not PUBLIC_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL missing है।")
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("help",help_command))
    app.add_handler(CommandHandler("id",show_id))
    app.add_handler(CommandHandler("import",import_start))
    app.add_handler(CommandHandler("done",import_done))
    app.add_handler(CommandHandler("quiz",categories))
    app.add_handler(CommandHandler("revision",revision))
    app.add_handler(CommandHandler("stats",stats))
    app.add_handler(CommandHandler("categories",categories))
    app.add_handler(CommandHandler("settings",settings))
    app.add_handler(CommandHandler("deletequiz",delete_quiz))
    app.add_handler(CommandHandler("cancel",cancel))

    # Inline callbacks — specific patterns first.
    app.add_handler(CallbackQueryHandler(answer_callback,pattern=r"^ans:\d+:[0-3]$"))
    app.add_handler(CallbackQueryHandler(start_selected_quiz,pattern=r"^play:\d+$"))
    app.add_handler(CallbackQueryHandler(topic_callback,pattern=r"^topic:.+:.+$"))
    app.add_handler(CallbackQueryHandler(category_callback,pattern=r"^cat:.+$"))
    app.add_handler(CallbackQueryHandler(catlist_callback,pattern=r"^catlist$"))
    app.add_handler(CallbackQueryHandler(settings_callback,pattern=r"^set:(timer|rq|ro|ex)$"))
    app.add_handler(CallbackQueryHandler(timer_callback,pattern=r"^timer:\d+$"))
    app.add_handler(CallbackQueryHandler(settings_back,pattern=r"^settings:back$"))
    app.add_handler(CallbackQueryHandler(delete_callback,pattern=r"^(delask|delconfirm|delcancel):\d+$"))

    # Category save callback used after import.
    async def savecat_cb(update, context):
        q=update.callback_query; await q.answer()
        if not allowed(update): return
        await save_pending_quiz(update,context,q.data.split(":",1)[1])
    app.add_handler(CallbackQueryHandler(savecat_cb,pattern=r"^savecat:.+$"))

    # Bottom keyboard / import text router MUST be last.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_router))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{PUBLIC_URL}/telegram",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__=="__main__":
    main()
