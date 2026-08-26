import os
import re
import json
import random
import logging
from datetime import timedelta

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
PORT = int(os.environ.get("PORT", "10000"))
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

IMPORT_MAX_CHARS = 1_000_000
QUIZ_LIFETIME_DAYS = 30
REATTEMPT_DELAY_HOURS = 24
MAX_QUESTIONS = 200

POOL = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=5,
    kwargs={"row_factory": dict_row},
    open=False,
)

def db():
    return POOL.connection()

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            exam TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            topic TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days')
        );
        ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS exam TEXT DEFAULT '';
        ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS subject TEXT DEFAULT '';
        ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS topic TEXT DEFAULT '';

        CREATE TABLE IF NOT EXISTS questions (
            id BIGSERIAL PRIMARY KEY,
            quiz_id BIGINT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
            q_no INTEGER NOT NULL,
            question TEXT NOT NULL,
            options JSONB NOT NULL,
            answer INTEGER NOT NULL,
            explanation TEXT DEFAULT '',
            UNIQUE (quiz_id, q_no)
        );

        CREATE TABLE IF NOT EXISTS attempts (
            user_id BIGINT PRIMARY KEY,
            quiz_id BIGINT REFERENCES quizzes(id) ON DELETE SET NULL,
            question_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            position INTEGER NOT NULL DEFAULT 0,
            score INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'quiz',
            explanation_on BOOLEAN NOT NULL DEFAULT TRUE,
            timer_seconds INTEGER NOT NULL DEFAULT 0,
            deadline_epoch DOUBLE PRECISION DEFAULT 0
        );
        ALTER TABLE attempts ADD COLUMN IF NOT EXISTS explanation_on BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE attempts ADD COLUMN IF NOT EXISTS timer_seconds INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE attempts ADD COLUMN IF NOT EXISTS deadline_epoch DOUBLE PRECISION DEFAULT 0;

        CREATE TABLE IF NOT EXISTS wrong_answers (
            user_id BIGINT NOT NULL,
            question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            wrong_count INTEGER NOT NULL DEFAULT 1,
            last_wrong TIMESTAMPTZ NOT NULL DEFAULT now(),
            retry_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours'),
            retry_notified BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY (user_id, question_id)
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id BIGINT PRIMARY KEY,
            timer_seconds INTEGER NOT NULL DEFAULT 0,
            explanation_on BOOLEAN NOT NULL DEFAULT TRUE,
            random_questions BOOLEAN NOT NULL DEFAULT FALSE,
            random_options BOOLEAN NOT NULL DEFAULT FALSE
        );

        CREATE INDEX IF NOT EXISTS idx_quizzes_expires ON quizzes(expires_at);
        CREATE INDEX IF NOT EXISTS idx_wrong_retry ON wrong_answers(retry_at, retry_notified);
        """)
        conn.commit()

def cleanup_expired():
    with db() as conn:
        conn.execute("DELETE FROM quizzes WHERE expires_at <= now()")
        conn.commit()

def allowed(update: Update) -> bool:
    return bool(update.effective_user) and (
        ADMIN_USER_ID == 0 or update.effective_user.id == ADMIN_USER_ID
    )

async def guard(update: Update) -> bool:
    if allowed(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "यह निजी bot है। पहले /id भेजें और अपना Telegram ID देखें।"
        )
    return False

def menu():
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

def get_settings(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id=%s", (user_id,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO user_settings(user_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (user_id,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM user_settings WHERE user_id=%s", (user_id,)
            ).fetchone()
    return row

def update_setting(user_id, field, value):
    if field not in {"timer_seconds", "explanation_on", "random_questions", "random_options"}:
        return
    with db() as conn:
        conn.execute(
            f"INSERT INTO user_settings(user_id,{field}) VALUES(%s,%s) "
            f"ON CONFLICT(user_id) DO UPDATE SET {field}=EXCLUDED.{field}",
            (user_id, value),
        )
        conn.commit()

# ---------- Flexible parser ----------

QUESTION_RE = re.compile(
    r"(?im)^\s*(?:(?:Q|QUESTION|प्रश्न)\s*(\d+)\s*[\.\)\:\-]\s*|"
    r"(\d+)\s*[\.\)\:\-]\s*)(.+?)\s*$"
)
OPTION_RE = re.compile(r"(?im)^\s*([ABCD])\s*[\.\)\:\-]\s+(.+?)\s*$")

def clean_line(s):
    return re.sub(r"\s+", " ", s.strip())

def extract_meta(text):
    def val(pattern):
        m = re.search(pattern, text, re.I | re.M)
        return clean_line(m.group(1)) if m else ""
    title = val(r"^\s*(?:QUIZ|TITLE|शीर्षक)\s*[:\-]\s*(.+)$") or "My Quiz"
    exam = val(r"^\s*(?:EXAM|परीक्षा)\s*[:\-]\s*(.+)$")
    subject = val(r"^\s*(?:SUBJECT|विषय)\s*[:\-]\s*(.+)$")
    topic = val(r"^\s*(?:TOPIC|अध्याय|टॉपिक)\s*[:\-]\s*(.+)$")
    return title, exam, subject, topic

def extract_answer_key(text):
    answers = {}
    patterns = [
        r"(?im)^\s*(?:ANSWER\s*KEY|ANSWERS?|उत्तर\s*कुंजी|उत्तर)\s*[:\-]\s*(.+?)\s*$",
        r"(?im)^\s*(?:KEY)\s*[:\-]\s*(.+?)\s*$",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            chunk = m.group(1)
            for q, a in re.findall(r"(?i)(?:Q\s*)?(\d+)\s*[\-:\.\)]\s*([ABCD])", chunk):
                answers[int(q)] = ord(a.upper()) - 65
            for q, a in re.findall(r"(?i)(\d+)\s*=\s*([ABCD])", chunk):
                answers[int(q)] = ord(a.upper()) - 65
    return answers

def extract_inline_answer(block):
    pats = [
        r"(?im)^\s*(?:ANSWER|ANS|CORRECT\s*ANSWER|RIGHT\s*ANSWER|सही\s*उत्तर|उत्तर)\s*[:=\-]?\s*([ABCD])\b",
        r"(?im)^\s*(?:ANSWER|ANS)\s*[:=\-]?\s*(?:OPTION\s*)?([1-4])\b",
    ]
    for pat in pats:
        m = re.search(pat, block)
        if m:
            token = m.group(1).upper()
            return (ord(token) - 65) if token in "ABCD" else int(token) - 1
    return None

def extract_explanation(block):
    m = re.search(
        r"(?ims)^\s*(?:EXPLANATION|WHY|व्याख्या|कारण|विवरण)\s*[:\-]\s*(.*?)(?="
        r"^\s*(?:ANSWER|ANS|CORRECT\s*ANSWER|सही\s*उत्तर|उत्तर)\s*[:=\-]|"
        r"^\s*(?:EXPLANATION|WHY|व्याख्या|कारण)\s*[:\-]|\Z)",
        block,
    )
    return clean_line(m.group(1)) if m else ""

def parse_quiz(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    title, exam, subject, topic = extract_meta(text)
    key = extract_answer_key(text)

    matches = list(QUESTION_RE.finditer(text))
    if not matches:
        # fallback: numbered questions with no space after marker
        fallback = re.compile(r"(?im)^\s*(?:(?:Q|QUESTION|प्रश्न)\s*)?(\d+)\s*[\.\)\:\-]\s*(.+?)\s*$")
        matches = list(fallback.finditer(text))
    if not matches:
        raise ValueError("सवाल नहीं मिले। Q1., Q1), 1., 1), प्रश्न 1: जैसे format रखें।")

    questions = []
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[m.end():end]
        q_no = int(m.group(1) or m.group(2) or (idx + 1))
        qtext = clean_line(m.group(3) if m.lastindex and m.lastindex >= 3 else m.group(2))

        options = {}
        for letter, value in OPTION_RE.findall(block):
            options[letter.upper()] = clean_line(value)

        if set(options) != {"A", "B", "C", "D"}:
            raise ValueError(
                f"प्रश्न {idx+1} में A, B, C, D चारों options नहीं मिले।"
            )

        answer = extract_inline_answer(block)
        if answer is None:
            answer = key.get(q_no)
        if answer is None:
            # support a simple "Correct: 2" / "Ans: 2"
            mnum = re.search(r"(?im)^\s*(?:ANSWER|ANS|उत्तर)\s*[:=\-]?\s*([1-4])\b", block)
            if mnum:
                answer = int(mnum.group(1)) - 1
        if answer not in range(4):
            raise ValueError(
                f"प्रश्न {idx+1} का सही उत्तर नहीं मिला। ANSWER: B या Answer Key: 1-B जैसे format रखें।"
            )

        questions.append({
            "q_no": len(questions) + 1,
            "question": qtext,
            "options": [options["A"], options["B"], options["C"], options["D"]],
            "answer": answer,
            "explanation": extract_explanation(block),
        })

    if len(questions) > MAX_QUESTIONS:
        raise ValueError(f"एक Quiz में अधिकतम {MAX_QUESTIONS} सवाल रखें।")
    return {
        "title": title,
        "exam": exam,
        "subject": subject,
        "topic": topic,
        "questions": questions,
    }

# ---------- Commands / import ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired()
    get_settings(update.effective_user.id)
    await update.message.reply_text(
        "📚 My Revision Quiz\n\n"
        "/import — Quiz paste करें\n"
        "/done — import पूरा करें\n"
        "/quiz — Quiz शुरू करें\n"
        "/revision — ReAttempt\n"
        "/stats — Progress\n"
        "/categories — Categories\n"
        "/settings — Settings\n"
        "/cancel — current session रद्द करें\n\n"
        "🗑️ Quiz 30 दिन बाद auto-delete होते हैं।\n"
        "⏰ गलत सवाल 24 घंटे बाद ReAttempt होते हैं।",
        reply_markup=menu(),
    )

async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"आपका Telegram ID:\n{update.effective_user.id}")

async def import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data["importing"] = True
    context.user_data["import_text"] = ""
    await update.message.reply_text(
        "📥 Quiz paste mode शुरू।\n"
        "लंबा Quiz कई messages में भेज सकते हैं।\n"
        "सब भेजने के बाद /done लिखें।\n"
        "कुल सीमा: 10 लाख characters।"
    )

async def collect_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.user_data.get("importing"):
        await update.message.reply_text("पहले /import दबाएँ।")
        return
    text = update.message.text or ""
    total = context.user_data.get("import_text", "") + "\n" + text
    if len(total) > IMPORT_MAX_CHARS:
        await update.message.reply_text("❌ Import limit पार हो गई।")
        return
    context.user_data["import_text"] = total
    await update.message.reply_text(f"✅ हिस्सा मिल गया ({len(total):,} characters)।")

async def import_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.user_data.get("importing"):
        await update.message.reply_text("अभी कोई import चालू नहीं है।")
        return
    text = context.user_data.get("import_text", "")
    try:
        data = parse_quiz(text)
    except ValueError as e:
        await update.message.reply_text(f"❌ Format error:\n{e}")
        return

    context.user_data.pop("importing", None)
    context.user_data.pop("import_text", None)
    cleanup_expired()

    with db() as conn:
        quiz_id = conn.execute(
            """INSERT INTO quizzes(title,exam,subject,topic,expires_at)
               VALUES(%s,%s,%s,%s,now()+interval '30 days') RETURNING id""",
            (data["title"], data["exam"], data["subject"], data["topic"]),
        ).fetchone()["id"]
        for q in data["questions"]:
            conn.execute(
                """INSERT INTO questions(quiz_id,q_no,question,options,answer,explanation)
                   VALUES(%s,%s,%s,%s,%s,%s)""",
                (quiz_id, q["q_no"], q["question"], json.dumps(q["options"]),
                 q["answer"], q["explanation"]),
            )
        conn.commit()

    await update.message.reply_text(
        f"🎉 Quiz save हो गया!\n\n"
        f"📚 {data['title']}\n"
        f"❓ {len(data['questions'])} सवाल\n"
        f"🎯 {data['exam'] or 'Exam नहीं दिया'}\n"
        f"📖 {data['subject'] or 'Subject नहीं दिया'}\n"
        f"🗂️ {data['topic'] or 'Topic नहीं दिया'}\n\n"
        f"🗑️ 30 दिन बाद auto-delete होगा।\n"
        f"/quiz से खेलें।"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data.clear()
    await update.message.reply_text("ठीक है, current session cancel कर दिया।", reply_markup=menu())

# ---------- Settings ----------

def settings_keyboard(s):
    timer = s["timer_seconds"]
    timer_text = "OFF" if timer == 0 else f"{timer}s"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏱️ Timer: {timer_text}", callback_data="set:timer")],
        [InlineKeyboardButton(f"💡 Explanation: {'ON' if s['explanation_on'] else 'OFF'}", callback_data="set:exp")],
        [InlineKeyboardButton(f"🔀 Questions: {'ON' if s['random_questions'] else 'OFF'}", callback_data="set:rq")],
        [InlineKeyboardButton(f"🔀 Options: {'ON' if s['random_options'] else 'OFF'}", callback_data="set:ro")],
    ])

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    s = get_settings(update.effective_user.id)
    await update.message.reply_text("⚙️ Quiz Settings", reply_markup=settings_keyboard(s))

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    uid = update.effective_user.id
    s = get_settings(uid)
    if q.data == "set:timer":
        await q.edit_message_text(
            "⏱️ Timer चुनें",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("OFF", callback_data="timer:0")],
                [InlineKeyboardButton("10 sec", callback_data="timer:10"),
                 InlineKeyboardButton("20 sec", callback_data="timer:20")],
                [InlineKeyboardButton("30 sec", callback_data="timer:30"),
                 InlineKeyboardButton("60 sec", callback_data="timer:60")],
                [InlineKeyboardButton("90 sec", callback_data="timer:90")],
                [InlineKeyboardButton("↩️ Back", callback_data="settings:back")],
            ]),
        )
        return
    if q.data == "set:exp":
        update_setting(uid, "explanation_on", not s["explanation_on"])
    elif q.data == "set:rq":
        update_setting(uid, "random_questions", not s["random_questions"])
    elif q.data == "set:ro":
        update_setting(uid, "random_options", not s["random_options"])
    s = get_settings(uid)
    await q.edit_message_text("⚙️ Quiz Settings", reply_markup=settings_keyboard(s))

async def timer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    seconds = int(q.data.split(":")[1])
    update_setting(update.effective_user.id, "timer_seconds", seconds)
    s = get_settings(update.effective_user.id)
    await q.edit_message_text("⚙️ Quiz Settings", reply_markup=settings_keyboard(s))

async def settings_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_settings(update.effective_user.id)
    await q.edit_message_text("⚙️ Quiz Settings", reply_markup=settings_keyboard(s))

# ---------- Quiz engine ----------

QUESTION_CACHE = {}

def get_question(question_id):
    if question_id in QUESTION_CACHE:
        return QUESTION_CACHE[question_id]
    with db() as conn:
        q = conn.execute("SELECT * FROM questions WHERE id=%s", (question_id,)).fetchone()
    if q:
        QUESTION_CACHE[question_id] = q
        if len(QUESTION_CACHE) > 1000:
            QUESTION_CACHE.pop(next(iter(QUESTION_CACHE)))
    return q

def latest_quiz():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM quizzes WHERE expires_at>now() ORDER BY id DESC LIMIT 1"
        ).fetchone()

def quiz_question_ids(quiz_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM questions WHERE quiz_id=%s ORDER BY q_no", (quiz_id,)
        ).fetchall()
    return [r["id"] for r in rows]

def schedule_timer(application, user_id, deadline, chat_id):
    # Old timer jobs are harmless because the deadline is checked from DB.
    if deadline <= 0:
        return
    delay = max(0.5, deadline - __import__("time").time())
    application.job_queue.run_once(
        timer_expired_job, when=delay, data={"user_id": user_id, "chat_id": chat_id}
    )

async def timer_expired_job(context):
    data = context.job.data
    uid = data["user_id"]
    with db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE user_id=%s", (uid,)).fetchone()
    if not attempt or not attempt["timer_seconds"] or attempt["deadline_epoch"] <= 0:
        return
    import time
    if time.time() + 0.2 < attempt["deadline_epoch"]:
        return
    await advance_after_timeout(context.application, uid, data["chat_id"])

async def advance_after_timeout(application, user_id, chat_id):
    with db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE user_id=%s", (user_id,)).fetchone()
    if not attempt:
        return
    pos = attempt["position"]
    ids = attempt["question_ids"]
    if pos >= len(ids):
        return
    qid = int(ids[pos])
    q = get_question(qid)
    if not q:
        return
    with db() as conn:
        conn.execute(
            """INSERT INTO wrong_answers(user_id,question_id,wrong_count,last_wrong,retry_at,retry_notified)
               VALUES(%s,%s,1,now(),now()+interval '24 hours',FALSE)
               ON CONFLICT(user_id,question_id) DO UPDATE SET
               wrong_count=wrong_answers.wrong_count+1,last_wrong=now(),
               retry_at=now()+interval '24 hours',retry_notified=FALSE""",
            (user_id, qid),
        )
        conn.execute(
            "UPDATE attempts SET position=%s, deadline_epoch=0 WHERE user_id=%s",
            (pos + 1, user_id),
        )
        conn.commit()
    await application.bot.send_message(
        chat_id=chat_id,
        text="⏰ समय समाप्त!\n\n❌ यह सवाल ReAttempt में save हो गया।",
    )
    await send_current(user_id, application, chat_id)

async def begin_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired()
    quiz = latest_quiz()
    if not quiz:
        await update.message.reply_text("पहले /import से Quiz डालें।")
        return
    ids = quiz_question_ids(quiz["id"])
    if not ids:
        await update.message.reply_text("इस Quiz में सवाल नहीं हैं।")
        return
    s = get_settings(update.effective_user.id)
    if s["random_questions"]:
        random.shuffle(ids)
    with db() as conn:
        conn.execute(
            """INSERT INTO attempts(user_id,quiz_id,question_ids,position,score,mode,explanation_on,timer_seconds,deadline_epoch)
               VALUES(%s,%s,%s,0,0,'quiz',%s,%s,0)
               ON CONFLICT(user_id) DO UPDATE SET quiz_id=EXCLUDED.quiz_id,
               question_ids=EXCLUDED.question_ids,position=0,score=0,mode='quiz',
               explanation_on=EXCLUDED.explanation_on,timer_seconds=EXCLUDED.timer_seconds,deadline_epoch=0""",
            (update.effective_user.id, quiz["id"], json.dumps(ids),
             s["explanation_on"], s["timer_seconds"]),
        )
        conn.commit()
    await send_current(update.effective_user.id, context, update.effective_chat.id)

async def begin_revision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired()
    with db() as conn:
        rows = conn.execute(
            """SELECT q.id FROM questions q
               JOIN wrong_answers w ON w.question_id=q.id
               JOIN quizzes z ON z.id=q.quiz_id
               WHERE w.user_id=%s AND w.retry_at<=now() AND z.expires_at>now()
               ORDER BY w.retry_at LIMIT 10""",
            (update.effective_user.id,),
        ).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        await update.message.reply_text("अभी कोई ReAttempt due नहीं है।")
        return
    s = get_settings(update.effective_user.id)
    if s["random_questions"]:
        random.shuffle(ids)
    with db() as conn:
        conn.execute(
            """INSERT INTO attempts(user_id,quiz_id,question_ids,position,score,mode,explanation_on,timer_seconds,deadline_epoch)
               VALUES(%s,NULL,%s,0,0,'revision',%s,%s,0)
               ON CONFLICT(user_id) DO UPDATE SET quiz_id=NULL,question_ids=EXCLUDED.question_ids,
               position=0,score=0,mode='revision',explanation_on=EXCLUDED.explanation_on,
               timer_seconds=EXCLUDED.timer_seconds,deadline_epoch=0""",
            (update.effective_user.id, json.dumps(ids), s["explanation_on"], s["timer_seconds"]),
        )
        conn.commit()
    await send_current(update.effective_user.id, context, update.effective_chat.id)

async def send_current(user_id, context_or_app, chat_id):
    application = context_or_app.application if hasattr(context_or_app, "application") else context_or_app
    with db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE user_id=%s", (user_id,)).fetchone()
    if not attempt:
        return
    ids = attempt["question_ids"]
    pos = attempt["position"]
    if pos >= len(ids):
        await finish(chat_id, user_id, application)
        return
    q = get_question(int(ids[pos]))
    if not q:
        await advance_after_timeout(application, user_id, chat_id)
        return

    options = list(q["options"])
    if attempt["mode"] == "quiz" and get_settings(user_id)["random_options"]:
        pairs = list(enumerate(options))
        random.shuffle(pairs)
        display = [(letter, text, original) for letter, (original, text) in zip(["A","B","C","D"], pairs)]
    else:
        display = [("ABCD"[i], options[i], i) for i in range(4)]

    keyboard = [
        [InlineKeyboardButton(f"{letter}. {text}", callback_data=f"ans:{q['id']}:{original}")]
        for letter, text, original in display
    ]
    mode = "🔄 ReAttempt" if attempt["mode"] == "revision" else "🧠 Quiz"
    timer = attempt["timer_seconds"]
    timer_line = f"\n⏱️ {timer} सेकंड" if timer else ""
    msg = f"{mode}\n\n❓ {pos+1}/{len(ids)}\n\n{q['question']}{timer_line}"
    sent = await application.bot.send_message(
        chat_id=chat_id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard)
    )
    if timer:
        import time
        deadline = time.time() + timer
        with db() as conn:
            conn.execute(
                "UPDATE attempts SET deadline_epoch=%s WHERE user_id=%s",
                (deadline, user_id),
            )
            conn.commit()
        schedule_timer(application, user_id, deadline, chat_id)

async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        return
    _, qid_text, opt_text = query.data.split(":")
    qid, selected = int(qid_text), int(opt_text)
    uid = update.effective_user.id
    with db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE user_id=%s", (uid,)).fetchone()
    if not attempt:
        await query.edit_message_text("यह Quiz session खत्म हो चुका है। /quiz से फिर शुरू करें।")
        return
    ids, pos = attempt["question_ids"], attempt["position"]
    if pos >= len(ids) or int(ids[pos]) != qid:
        await query.edit_message_text("यह सवाल अब active नहीं है।")
        return
    q = get_question(qid)
    correct = selected == q["answer"]
    new_score = attempt["score"] + (1 if correct else 0)
    with db() as conn:
        if correct:
            conn.execute("DELETE FROM wrong_answers WHERE user_id=%s AND question_id=%s", (uid, qid))
        else:
            conn.execute(
                """INSERT INTO wrong_answers(user_id,question_id,wrong_count,last_wrong,retry_at,retry_notified)
                   VALUES(%s,%s,1,now(),now()+interval '24 hours',FALSE)
                   ON CONFLICT(user_id,question_id) DO UPDATE SET
                   wrong_count=wrong_answers.wrong_count+1,last_wrong=now(),
                   retry_at=now()+interval '24 hours',retry_notified=FALSE""",
                (uid, qid),
            )
        conn.execute(
            "UPDATE attempts SET position=%s,score=%s,deadline_epoch=0 WHERE user_id=%s",
            (pos + 1, new_score, uid),
        )
        conn.commit()

    letters = "ABCD"
    text = "✅ सही!" if correct else f"❌ गलत!\n\nसही उत्तर: {letters[q['answer']]}"
    if attempt["explanation_on"] and q["explanation"]:
        text += f"\n\n💡 {q['explanation']}"
    if not correct:
        text += "\n\n🔄 यह सवाल 24 घंटे बाद ReAttempt होगा।"
    await query.edit_message_text(text)
    await send_current(uid, context, update.effective_chat.id)

async def reattempt_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        return
    _, qid_text, opt_text = query.data.split(":")
    qid, selected = int(qid_text), int(opt_text)
    uid = update.effective_user.id
    q = get_question(qid)
    if not q:
        await query.edit_message_text("यह सवाल अब उपलब्ध नहीं है।")
        return
    correct = selected == q["answer"]
    with db() as conn:
        if correct:
            conn.execute(
                "DELETE FROM wrong_answers WHERE user_id=%s AND question_id=%s",
                (uid, qid),
            )
        else:
            conn.execute(
                """UPDATE wrong_answers
                   SET wrong_count=wrong_count+1,last_wrong=now(),
                       retry_at=now()+interval '24 hours',retry_notified=FALSE
                   WHERE user_id=%s AND question_id=%s""",
                (uid, qid),
            )
        conn.commit()
    s = get_settings(uid)
    text = "✅ सही! यह सवाल ReAttempt list से हट गया।"
    if not correct:
        text = "❌ फिर गलत। यह सवाल 24 घंटे बाद फिर आएगा।"
    if s["explanation_on"] and q["explanation"]:
        text += f"\n\n💡 {q['explanation']}"
    await query.edit_message_text(text)

async def finish(chat_id, uid, application):
    with db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE user_id=%s", (uid,)).fetchone()
    if not attempt:
        return
    total = len(attempt["question_ids"])
    score = attempt["score"]
    pct = round(score * 100 / total) if total else 0
    await application.bot.send_message(
        chat_id=chat_id,
        text=f"🏁 Quiz पूरा!\n\n📊 Score: {score}/{total}\n📈 प्रतिशत: {pct}%\n\n"
             f"🔄 गलत सवाल 24 घंटे बाद ReAttempt होंगे।",
    )

# ---------- Automatic ReAttempt ----------

async def send_due_reattempts(application):
    cleanup_expired()
    with db() as conn:
        rows = conn.execute(
            """SELECT w.user_id,w.question_id,q.question,q.options
               FROM wrong_answers w
               JOIN questions q ON q.id=w.question_id
               JOIN quizzes z ON z.id=q.quiz_id
               WHERE w.retry_at<=now() AND w.retry_notified=FALSE AND z.expires_at>now()
               ORDER BY w.retry_at LIMIT 50"""
        ).fetchall()
        for r in rows:
            try:
                keyboard = [
                    [InlineKeyboardButton(f"{'ABCD'[i]}. {opt}", callback_data=f"retry:{r['question_id']}:{i}")]
                    for i, opt in enumerate(r["options"])
                ]
                await application.bot.send_message(
                    chat_id=r["user_id"],
                    text=f"🔄 ReAttempt — 24 घंटे बाद\n\n❓ {r['question']}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                conn.execute(
                    "UPDATE wrong_answers SET retry_notified=TRUE WHERE user_id=%s AND question_id=%s",
                    (r["user_id"], r["question_id"]),
                )
                conn.commit()
            except Exception:
                log.exception("ReAttempt send failed")

async def maintenance_job(context):
    await send_due_reattempts(context.application)

# ---------- Stats / Categories ----------

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired()
    with db() as conn:
        qcount = conn.execute("SELECT COUNT(*) AS n FROM quizzes WHERE expires_at>now()").fetchone()["n"]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM questions q JOIN quizzes z ON z.id=q.quiz_id WHERE z.expires_at>now()"
        ).fetchone()["n"]
        wrong = conn.execute(
            """SELECT COUNT(*) AS n FROM wrong_answers w JOIN questions q ON q.id=w.question_id
               JOIN quizzes z ON z.id=q.quiz_id WHERE w.user_id=%s AND z.expires_at>now()""",
            (update.effective_user.id,),
        ).fetchone()["n"]
    await update.message.reply_text(
        f"📊 Progress\n\n📚 Active Quizzes: {qcount}\n❓ Questions: {total}\n"
        f"❌ ReAttempt: {wrong}\n\n🗑️ 30 दिन बाद auto-delete"
    )

async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired()
    with db() as conn:
        rows = conn.execute(
            """SELECT COALESCE(NULLIF(exam,''),'General') AS exam,
                      COALESCE(NULLIF(subject,''),'General') AS subject,
                      COUNT(*) AS n
               FROM quizzes q
               JOIN questions x ON x.quiz_id=q.id
               WHERE q.expires_at>now()
               GROUP BY 1,2 ORDER BY 1,2"""
        ).fetchall()
    if not rows:
        await update.message.reply_text("अभी कोई category नहीं है।")
        return
    lines = ["📚 Categories\n"]
    for r in rows:
        lines.append(f"🎯 {r['exam']} → 📖 {r['subject']} ({r['n']} सवाल)")
    await update.message.reply_text("\n".join(lines))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ---------- Text menu ----------

async def text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    t = (update.message.text or "").strip()
    mapping = {
        "📝 Quiz": begin_quiz,
        "➕ Add Quiz": import_start,
        "🔄 ReAttempt": begin_revision,
        "📊 Stats": stats,
        "📚 Categories": categories,
        "⚙️ Settings": settings,
        "❓ Help": help_cmd,
    }
    fn = mapping.get(t)
    if fn:
        await fn(update, context)
    elif not context.user_data.get("importing"):
        await update.message.reply_text("नीचे menu से विकल्प चुनें या /help देखें।")

# ---------- App ----------

async def post_init(application):
    POOL.open(wait=True)
    init_db()
    cleanup_expired()
    if application.job_queue:
        application.job_queue.run_repeating(maintenance_job, interval=60, first=10)

async def post_shutdown(application):
    try:
        POOL.close()
    except Exception:
        log.exception("Pool close failed")

def main():
    if not PUBLIC_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing.")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("import", import_start))
    app.add_handler(CommandHandler("done", import_done))
    app.add_handler(CommandHandler("quiz", begin_quiz))
    app.add_handler(CommandHandler("revision", begin_revision))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^set:(timer|exp|rq|ro)$"))
    app.add_handler(CallbackQueryHandler(timer_callback, pattern=r"^timer:(0|10|20|30|60|90)$"))
    app.add_handler(CallbackQueryHandler(settings_back, pattern=r"^settings:back$"))
    app.add_handler(CallbackQueryHandler(reattempt_answer, pattern=r"^retry:\d+:[0-3]$"))
    app.add_handler(CallbackQueryHandler(answer, pattern=r"^ans:\d+:[0-3]$"))

    # Menu handler must come before the generic text collector.
    app.add_handler(MessageHandler(filters.Regex(r"^(📝 Quiz|➕ Add Quiz|🔄 ReAttempt|📊 Stats|📚 Categories|⚙️ Settings|❓ Help)$"), text_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{PUBLIC_URL}/telegram",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
