import os
import re
import json
import random
import logging
import asyncio
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# My Revision Quiz Bot - Final integrated version
# Features:
# - Universal-ish quiz parser with many common formats
# - Multi-message import + visible Done/Cancel buttons
# - Categories + subcategories
# - Bottom reply keyboard
# - Manual quiz delete
# - 30-day automatic quiz expiry
# - Revision only after 24 hours
# - Timer ON/OFF
# - Random questions ON/OFF
# - Random options ON/OFF
# - Explanations
# - PostgreSQL connection pool for faster DB access
# ============================================================

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
REVISION_DELAY_HOURS = 24
CLEANUP_INTERVAL_SECONDS = 3600

# Pooling avoids opening a brand-new PostgreSQL connection for every action.
DB_POOL_MIN = 1
DB_POOL_MAX = 5
pool = None

CATEGORIES = [
    ("🇮🇳 सामान्य ज्ञान", "सामान्य ज्ञान"),
    ("🏛️ इतिहास", "इतिहास"),
    ("🌍 भूगोल", "भूगोल"),
    ("⚖️ संविधान / राजव्यवस्था", "राजव्यवस्था"),
    ("🔬 सामान्य विज्ञान", "विज्ञान"),
    ("🌳 पर्यावरण", "पर्यावरण"),
    ("💰 अर्थव्यवस्था", "अर्थव्यवस्था"),
    ("📰 Current Affairs", "Current Affairs"),
    ("🧠 मनोविज्ञान", "मनोविज्ञान"),
    ("📖 हिंदी", "हिंदी"),
    ("🇬🇧 English", "English"),
    ("🎯 RO/ARO", "RO/ARO"),
    ("📝 अन्य", "अन्य"),
]
CATEGORY_NAMES = {name for _, name in CATEGORIES}


def db():
    """Pooled PostgreSQL connection."""
    if pool is not None:
        return pool.connection()
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    global pool

    if pool is None:
        pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=DB_POOL_MIN,
            max_size=DB_POOL_MAX,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        pool.open(wait=True)

    with db() as conn:
        conn.execute(
            """
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
                option_order JSONB NOT NULL DEFAULT '[]'::jsonb
            );

            CREATE TABLE IF NOT EXISTS wrong_answers (
                user_id BIGINT NOT NULL,
                question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                wrong_count INTEGER NOT NULL DEFAULT 1,
                last_wrong TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, question_id)
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                timer_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                timer_seconds INTEGER NOT NULL DEFAULT 30,
                random_questions BOOLEAN NOT NULL DEFAULT FALSE,
                random_options BOOLEAN NOT NULL DEFAULT FALSE
            );

            ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS category TEXT;
            ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS subcategory TEXT;
            ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

            ALTER TABLE attempts ADD COLUMN IF NOT EXISTS option_order JSONB NOT NULL DEFAULT '[]'::jsonb;

            UPDATE quizzes
            SET category = COALESCE(NULLIF(category, ''), 'अन्य'),
                subcategory = COALESCE(subcategory, ''),
                expires_at = COALESCE(expires_at, created_at + interval '30 days');

            CREATE INDEX IF NOT EXISTS idx_quizzes_expiry
                ON quizzes (expires_at);

            CREATE INDEX IF NOT EXISTS idx_quizzes_category
                ON quizzes (category);

            CREATE INDEX IF NOT EXISTS idx_questions_quiz
                ON questions (quiz_id, q_no);

            CREATE INDEX IF NOT EXISTS idx_wrong_user_time
                ON wrong_answers (user_id, last_wrong);

            CREATE INDEX IF NOT EXISTS idx_wrong_question
                ON wrong_answers (question_id);
            """
        )
        conn.commit()


def cleanup_expired_quizzes():
    with db() as conn:
        result = conn.execute(
            "DELETE FROM quizzes WHERE expires_at <= now()"
        )
        conn.commit()
        if result.rowcount:
            log.info("Deleted %s expired quiz(es).", result.rowcount)


async def cleanup_loop():
    while True:
        try:
            cleanup_expired_quizzes()
        except Exception:
            log.exception("Expired quiz cleanup failed.")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


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


def clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _normalize_answer_token(token: str):
    token = token.strip().upper()
    token = re.sub(r"^[^A-D1-4]*", "", token)
    token = re.sub(r"[^A-D1-4]*$", "", token)

    if token in {"A", "B", "C", "D"}:
        return ord(token) - 65
    if token in {"1", "2", "3", "4"}:
        return int(token) - 1
    return None


def extract_meta(text: str):
    def val(pattern):
        m = re.search(pattern, text, re.I | re.M)
        return clean_line(m.group(1)) if m else ""

    title = (
        val(r"^\s*(?:QUIZ|TITLE|शीर्षक|क्विज़|क्विज)\s*[:\-]\s*(.+)$")
        or "My Quiz"
    )
    exam = val(r"^\s*(?:EXAM|परीक्षा)\s*[:\-]\s*(.+)$")
    subject = val(r"^\s*(?:SUBJECT|विषय)\s*[:\-]\s*(.+)$")
    topic = val(r"^\s*(?:TOPIC|अध्याय|टॉपिक)\s*[:\-]\s*(.+)$")
    category = val(
        r"^\s*(?:CATEGORY|CAT|श्रेणी|कैटेगरी|वर्ग)\s*[:\-]\s*(.+)$"
    )
    subcategory = val(
        r"^\s*(?:SUBCATEGORY|SUB-CATEGORY|उपश्रेणी|उप-श्रेणी|उपवर्ग)\s*[:\-]\s*(.+)$"
    )

    # Useful automatic fallback:
    # EXAM: RO/ARO can become the category if CATEGORY wasn't supplied.
    if not category and exam:
        if exam.strip().upper().replace(" ", "") in {"RO/ARO", "ROARO"}:
            category = "RO/ARO"

    return title, exam, subject, topic, category, subcategory


def normalize_category(category: str) -> str:
    category = clean_line(category)
    if not category:
        return "अन्य"

    low = category.lower()
    aliases = {
        "gk": "सामान्य ज्ञान",
        "general knowledge": "सामान्य ज्ञान",
        "history": "इतिहास",
        "geography": "भूगोल",
        "polity": "राजव्यवस्था",
        "constitution": "राजव्यवस्था",
        "science": "विज्ञान",
        "environment": "पर्यावरण",
        "economics": "अर्थव्यवस्था",
        "economy": "अर्थव्यवस्था",
        "current affairs": "Current Affairs",
        "psychology": "मनोविज्ञान",
        "hindi": "हिंदी",
        "english": "English",
        "ro/aro": "RO/ARO",
        "roaro": "RO/ARO",
    }
    return aliases.get(low, category)


def _is_answer_key_line(line: str) -> bool:
    x = line.strip()
    if not x:
        return False

    return bool(
        re.match(
            r"(?i)^(?:[✅✔️☑️✓👉🟢\s]*)?"
            r"(?:ANSWER\s*KEY|ANSWERS?|उत्तर\s*कुंजी|उत्तर\s*तालिका|KEY)"
            r"\s*[:\-]?",
            x,
        )
    ) or bool(
        re.match(r"(?i)^[\s\dABCD|,:;=\-\.]+$", x)
        and re.search(r"\d\s*[-=:]\s*[ABCD1-4]", x, re.I)
    )


def extract_answer_key(text: str):
    answers = {}

    patterns = [
        r"(?im)^\s*(?:[✅✔️☑️✓👉🟢\s]*)?"
        r"(?:ANSWER\s*KEY|KEY|ANSWERS?|उत्तर\s*कुंजी|उत्तर\s*तालिका)"
        r"\s*[:\-]?\s*(.+?)\s*$"
    ]

    for pat in patterns:
        for m in re.finditer(pat, text):
            chunk = m.group(1)

            for q, a in re.findall(
                r"(?i)(?:Q(?:UESTION)?\s*)?"
                r"(\d+)\s*[\-:=\.\)]\s*([ABCD1-4])\b",
                chunk,
            ):
                ans = _normalize_answer_token(a)
                if ans is not None:
                    answers[int(q)] = ans

            for q, a in re.findall(
                r"(?i)(\d+)\s*=\s*([ABCD1-4])\b",
                chunk,
            ):
                ans = _normalize_answer_token(a)
                if ans is not None:
                    answers[int(q)] = ans

    return answers


def extract_inline_answer(block: str):
    prefix = r"^[\s\u200b]*(?:[\W_]*?)?"
    labels = (
        r"(?:ANSWER|ANS|CORRECT\s*ANSWER|RIGHT\s*ANSWER|CORRECT|"
        r"सही\s*उत्तर|उत्तर|सही\s*विकल्प)"
    )

    patterns = [
        rf"(?im){prefix}{labels}\s*[:=\-]?\s*"
        rf"(?:OPTION|विकल्प)?\s*[\(\[]?\s*([ABCD])\b",
        rf"(?im){prefix}{labels}\s*[:=\-]?\s*"
        rf"(?:OPTION|विकल्प)?\s*[\(\[]?\s*([1-4])\b",
    ]

    for pat in patterns:
        m = re.search(pat, block)
        if m:
            return _normalize_answer_token(m.group(1))

    return None


def extract_explanation(block: str):
    m = re.search(
        r"(?ims)^\s*(?:[💡📝📌👉\s]*)?"
        r"(?:EXPLANATION|WHY|व्याख्या|कारण|विवरण|स्पष्टीकरण)"
        r"\s*[:\-]?\s*(.*?)"
        r"(?=^\s*(?:[\W_]*)(?:ANSWER|ANS|CORRECT\s*ANSWER|"
        r"RIGHT\s*ANSWER|सही\s*उत्तर|उत्तर)\s*[:=\-]?|\Z)",
        block,
    )
    return clean_line(m.group(1)) if m else ""


def _question_matches(text: str):
    # Supports:
    # Q1. / Q1) / Q1: / Q1 -
    # QUESTION 1:
    # प्रश्न 1:
    # 1. / 1) / 1: / 1 -
    # Q: / QUESTION: / प्रश्न:
    question_re = re.compile(
        r"(?im)^\s*(?:"
        r"Q(?:UESTION)?\s*(\d+)?"
        r"|प्रश्न\s*(\d+)"
        r"|(\d+)"
        r")\s*[\.\:\)\-]\s*(.+?)\s*$"
    )

    matches = []
    for m in question_re.finditer(text):
        line = m.group(0).strip()
        if _is_answer_key_line(line):
            continue
        matches.append(m)

    return matches


def parse_quiz(text: str):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not text:
        raise ValueError("Quiz text खाली है।")

    title, exam, subject, topic, category, subcategory = extract_meta(text)
    answer_key = extract_answer_key(text)
    matches = _question_matches(text)

    if not matches:
        raise ValueError(
            "सवाल नहीं मिले। Q1., Q1), Q1:, 1., 1), "
            "प्रश्न 1: या Q: जैसे format रखें।"
        )

    questions = []

    # More tolerant option matcher:
    # A) text / A. text / A: text / A- text / A - text / (A) text
    option_re = re.compile(
        r"(?im)^\s*(?:\(?([ABCD])\)?)[\.\:\)\-]\s*(.+?)\s*$"
    )

    for idx, match in enumerate(matches):
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[match.end():block_end]

        groups = match.groups()
        q_no = next((int(g) for g in groups[:3] if g), idx + 1)
        question_text = clean_line(groups[3])

        # Preserve wrapped question text before option A.
        pre_option = re.split(
            r"(?im)^\s*(?:\(?[A-D]\)?)[\.\:\)\-]\s+",
            block,
            maxsplit=1,
        )[0]

        if pre_option.strip():
            question_text = clean_line(question_text + " " + pre_option)

        options = {}
        for letter, value in option_re.findall(block):
            options[letter.upper()] = clean_line(value)

        if set(options) != {"A", "B", "C", "D"}:
            raise ValueError(
                f"प्रश्न {idx + 1} में A, B, C, D चारों options नहीं मिले।"
            )

        answer = extract_inline_answer(block)

        if answer is None:
            answer = answer_key.get(q_no)

        if answer is None:
            mnum = re.search(
                r"(?im)^\s*(?:[\W_]*)(?:ANSWER|ANS|CORRECT|"
                r"उत्तर|सही\s*उत्तर)\s*[:=\-]?\s*([1-4ABCD])\b",
                block,
            )
            if mnum:
                answer = _normalize_answer_token(mnum.group(1))

        if answer not in range(4):
            raise ValueError(
                f"प्रश्न {idx + 1} का सही उत्तर नहीं मिला। "
                "उदाहरण: ANSWER: B, सही उत्तर: B, Correct Answer: 2, "
                "या Answer Key: 1-B | 2-C"
            )

        questions.append(
            {
                "q_no": len(questions) + 1,
                "question": question_text,
                "options": [
                    options["A"],
                    options["B"],
                    options["C"],
                    options["D"],
                ],
                "answer": answer,
                "explanation": extract_explanation(block),
            }
        )

    if len(questions) > 100:
        raise ValueError("एक Quiz में अधिकतम 100 सवाल रखें।")

    return {
        "title": title,
        "exam": exam,
        "subject": subject,
        "topic": topic,
        "category": normalize_category(category),
        "subcategory": subcategory,
        "questions": questions,
    }


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
        [
            ["✅ Quiz Done", "❌ Cancel Import"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_keyboard(settings):
    timer_text = "ON" if settings["timer_enabled"] else "OFF"
    rq_text = "ON" if settings["random_questions"] else "OFF"
    ro_text = "ON" if settings["random_options"] else "OFF"

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"⏱ Timer: {timer_text}", callback_data="set:timer")],
            [InlineKeyboardButton(f"🔀 Random Questions: {rq_text}", callback_data="set:rq")],
            [InlineKeyboardButton(f"🔀 Random Options: {ro_text}", callback_data="set:ro")],
            [InlineKeyboardButton(f"⏱ Timer Time: {settings['timer_seconds']} sec", callback_data="set:time")],
        ]
    )


def get_settings(user_id):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id)
            VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id,),
        )
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        conn.commit()
        return row


def start_import_prompt_text():
    return (
        "📥 Quiz paste mode शुरू।\n\n"
        "बहुत लंबा Quiz हो तो कई messages में लगातार भेजें। "
        "मैं सभी हिस्से जोड़ दूँगा।\n\n"
        "सब भेजने के बाद नीचे ✅ Quiz Done दबाएँ।\n"
        "या /done लिख सकते हैं।\n\n"
        "❌ Cancel Import से session रद्द कर सकते हैं।\n"
        "अधिकतम कुल import: 10 लाख characters।"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    await update.message.reply_text(
        "📚 My Revision Quiz\n\n"
        "📝 Quiz — उपलब्ध Quiz खेलें\n"
        "➕ Add Quiz — नया Quiz import करें\n"
        "🔄 ReAttempt — 24 घंटे पुराने गलत सवाल\n"
        "📊 Stats — आपका progress\n"
        "📚 Categories — विषय/श्रेणी के अनुसार Quiz\n"
        "⚙️ Settings — Timer और Random options\n"
        "❓ Help — सहायता\n\n"
        "/id — Telegram ID\n"
        "/deletequiz — Quiz manually delete करें",
        reply_markup=main_menu(),
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    await update.message.reply_text(
        f"आपका Telegram ID:\n{update.effective_user.id}",
        reply_markup=main_menu(),
    )


async def import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    context.user_data["importing"] = True
    context.user_data["import_text"] = ""

    await update.message.reply_text(
        start_import_prompt_text(),
        reply_markup=import_menu(),
    )


async def collect_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    if not context.user_data.get("importing"):
        # Normal menu button text is handled here.
        text = (update.message.text or "").strip()

        if text == "📝 Quiz":
            await begin_quiz(update, context)
            return
        if text == "➕ Add Quiz":
            await import_start(update, context)
            return
        if text == "🔄 ReAttempt":
            await begin_revision(update, context)
            return
        if text == "📊 Stats":
            await stats(update, context)
            return
        if text == "📚 Categories":
            await categories(update, context)
            return
        if text == "⚙️ Settings":
            await settings(update, context)
            return
        if text == "❓ Help":
            await help_command(update, context)
            return

        await update.message.reply_text(
            "पहले नीचे से कोई option चुनें या /import लिखें।",
            reply_markup=main_menu(),
        )
        return

    text = update.message.text or ""
    total = context.user_data.get("import_text", "") + "\n" + text

    if len(total) > IMPORT_MAX_CHARS:
        await update.message.reply_text(
            "❌ Import limit पार हो गई। अधिकतम कुल 10 लाख characters हैं।",
            reply_markup=import_menu(),
        )
        return

    context.user_data["import_text"] = total

    await update.message.reply_text(
        "✅ हिस्सा मिल गया। और भेजें या नीचे ✅ Quiz Done दबाएँ।",
        reply_markup=import_menu(),
    )


def category_keyboard(prefix="setcat"):
    rows = []
    row = []

    for label, name in CATEGORIES:
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    return InlineKeyboardMarkup(rows)


async def import_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    if not context.user_data.get("importing"):
        await update.message.reply_text(
            "अभी कोई import चालू नहीं है।",
            reply_markup=main_menu(),
        )
        return

    text = context.user_data.get("import_text", "")

    try:
        parsed = parse_quiz(text)
    except ValueError as error:
        await update.message.reply_text(
            f"❌ Format error:\n{error}\n\n"
            "Import अभी बंद नहीं हुआ है। Text ठीक करके फिर भेजें "
            "या ❌ Cancel Import दबाएँ।",
            reply_markup=import_menu(),
        )
        return

    category = normalize_category(parsed.get("category", "अन्य"))

    # Keep parsed data in memory until the user chooses a category.
    context.user_data["pending_quiz"] = parsed

    if category and category != "अन्य":
        await save_pending_quiz(update, context, category)
        return

    await update.message.reply_text(
        f"📚 Quiz तैयार है: {parsed['title']}\n"
        f"❓ {len(parsed['questions'])} सवाल\n\n"
        "अब Category चुनें:",
        reply_markup=category_keyboard(),
    )


async def save_pending_quiz(update, context, category):
    parsed = context.user_data.get("pending_quiz")

    if not parsed:
        await update.effective_message.reply_text(
            "❌ Pending Quiz नहीं मिला। /import से फिर शुरू करें।",
            reply_markup=main_menu(),
        )
        return

    category = normalize_category(category)
    subcategory = clean_line(parsed.get("subcategory", ""))

    with db() as conn:
        quiz_row = conn.execute(
            """
            INSERT INTO quizzes (title, category, subcategory, expires_at)
            VALUES (%s, %s, %s, now() + interval '30 days')
            RETURNING id
            """,
            (parsed["title"], category, subcategory),
        ).fetchone()

        quiz_id = quiz_row["id"]

        for question in parsed["questions"]:
            conn.execute(
                """
                INSERT INTO questions
                (quiz_id, q_no, question, options, answer, explanation)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    quiz_id,
                    question["q_no"],
                    question["question"],
                    json.dumps(question["options"], ensure_ascii=False),
                    question["answer"],
                    question["explanation"],
                ),
            )

        conn.commit()

    context.user_data.pop("importing", None)
    context.user_data.pop("import_text", None)
    context.user_data.pop("pending_quiz", None)

    await update.effective_message.reply_text(
        f"🎉 Quiz save हो गया!\n\n"
        f"📚 {parsed['title']}\n"
        f"🏷 Category: {category}\n"
        f"❓ {len(parsed['questions'])} सवाल\n\n"
        "📝 Quiz button से खेलें।",
        reply_markup=main_menu(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Current session cancel कर दिया गया।",
        reply_markup=main_menu(),
    )


async def delete_quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT z.id, z.title, z.category, z.created_at,
                   COUNT(q.id) AS question_count
            FROM quizzes z
            LEFT JOIN questions q ON q.quiz_id = z.id
            WHERE z.expires_at > now()
            GROUP BY z.id
            ORDER BY z.id DESC
            LIMIT 20
            """
        ).fetchall()

    if not rows:
        await update.message.reply_text(
            "🗑 अभी delete करने के लिए कोई Quiz नहीं है।",
            reply_markup=main_menu(),
        )
        return

    keyboard = []
    for row in rows:
        title = clean_line(row["title"])
        if len(title) > 34:
            title = title[:31] + "..."
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"🗑 {title} ({row['question_count']})",
                    callback_data=f"deletequiz:ask:{row['id']}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="deletequiz:cancel")]
    )

    await update.message.reply_text(
        "🗑 Quiz Delete\n\n"
        "जिस Quiz को हटाना है उसे चुनें।\n"
        "Delete करने पर उसके सवाल भी हमेशा के लिए हट जाएंगे।",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await query.edit_message_text("❌ Quiz delete cancel कर दिया गया।")
        return

    if action == "list":
        cleanup_expired_quizzes()

        with db() as conn:
            rows = conn.execute(
                """
                SELECT z.id, z.title, z.category, COUNT(q.id) AS question_count
                FROM quizzes z
                LEFT JOIN questions q ON q.quiz_id = z.id
                WHERE z.expires_at > now()
                GROUP BY z.id
                ORDER BY z.id DESC
                LIMIT 20
                """
            ).fetchall()

        if not rows:
            await query.edit_message_text(
                "🗑 अभी delete करने के लिए कोई Quiz नहीं है।"
            )
            return

        keyboard = []
        for row in rows:
            title = clean_line(row["title"])
            if len(title) > 34:
                title = title[:31] + "..."
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"🗑 {title} ({row['question_count']})",
                        callback_data=f"deletequiz:ask:{row['id']}",
                    )
                ]
            )

        keyboard.append(
            [InlineKeyboardButton("❌ Cancel", callback_data="deletequiz:cancel")]
        )

        await query.edit_message_text(
            "🗑 Quiz Delete\n\nजिस Quiz को हटाना है उसे चुनें।",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action == "ask" and len(parts) == 3:
        quiz_id = int(parts[2])

        with db() as conn:
            row = conn.execute(
                """
                SELECT z.id, z.title, z.category, COUNT(q.id) AS question_count
                FROM quizzes z
                LEFT JOIN questions q ON q.quiz_id = z.id
                WHERE z.id = %s
                GROUP BY z.id
                """,
                (quiz_id,),
            ).fetchone()

        if not row:
            await query.edit_message_text("❌ यह Quiz अब मौजूद नहीं है।")
            return

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⚠️ हाँ, Delete",
                        callback_data=f"deletequiz:confirm:{quiz_id}",
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="deletequiz:cancel",
                    ),
                ]
            ]
        )

        await query.edit_message_text(
            f"⚠️ क्या यह Quiz delete करना है?\n\n"
            f"📚 {row['title']}\n"
            f"🏷 {row['category']}\n"
            f"❓ {row['question_count']} सवाल\n\n"
            "Delete करने के बाद इसे वापस नहीं लाया जा सकेगा।",
            reply_markup=keyboard,
        )
        return

    if action == "confirm" and len(parts) == 3:
        quiz_id = int(parts[2])

        with db() as conn:
            row = conn.execute(
                "SELECT id, title FROM quizzes WHERE id = %s",
                (quiz_id,),
            ).fetchone()

            if not row:
                await query.edit_message_text(
                    "❌ यह Quiz पहले ही delete हो चुका है।"
                )
                return

            conn.execute("DELETE FROM quizzes WHERE id = %s", (quiz_id,))
            conn.commit()

        await query.edit_message_text(
            f"✅ Quiz delete हो गया।\n\n📚 {row['title']}\n\n"
            "उसके सभी questions भी delete हो गए हैं।"
        )
        return

    await query.edit_message_text("❌ Invalid delete action।")


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS n
            FROM quizzes
            WHERE expires_at > now()
            GROUP BY category
            ORDER BY category
            """
        ).fetchall()

    counts = {row["category"]: row["n"] for row in rows}

    keyboard = []
    row = []

    for label, name in CATEGORIES:
        row.append(
            InlineKeyboardButton(
                f"{label} ({counts.get(name, 0)})",
                callback_data=f"cat:{name}",
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "📚 Categories\n\n"
        "Category चुनें। उसके अंदर के Quiz दिखाई देंगे।\n"
        "RO/ARO के लिए Category: 🎯 RO/ARO रख सकते हैं और "
        "Subcategory में History / Polity / Geography आदि लिख सकते हैं।",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    category = query.data.split(":", 1)[1]

    with db() as conn:
        rows = conn.execute(
            """
            SELECT z.id, z.title, z.subcategory, COUNT(q.id) AS question_count
            FROM quizzes z
            LEFT JOIN questions q ON q.quiz_id = z.id
            WHERE z.category = %s
              AND z.expires_at > now()
            GROUP BY z.id
            ORDER BY z.id DESC
            LIMIT 20
            """,
            (category,),
        ).fetchall()

    if not rows:
        await query.edit_message_text(
            f"📚 {category}\n\nइस Category में अभी कोई Quiz नहीं है।"
        )
        return

    keyboard = []

    for row in rows:
        title = clean_line(row["title"])
        if len(title) > 35:
            title = title[:32] + "..."

        sub = f" • {row['subcategory']}" if row["subcategory"] else ""

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"▶️ {title}{sub} ({row['question_count']})",
                    callback_data=f"playquiz:{row['id']}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("⬅️ Categories", callback_data="catlist")]
    )

    await query.edit_message_text(
        f"📚 {category}\n\nQuiz चुनें:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def category_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    keyboard = []
    row = []

    for label, name in CATEGORIES:
        row.append(
            InlineKeyboardButton(label, callback_data=f"cat:{name}")
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    await query.edit_message_text(
        "📚 Category चुनें:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def get_quiz_ids(quiz_id=None, limit=100):
    with db() as conn:
        if quiz_id:
            rows = conn.execute(
                """
                SELECT id
                FROM questions
                WHERE quiz_id = %s
                ORDER BY q_no
                LIMIT %s
                """,
                (quiz_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT q.id
                FROM questions q
                JOIN quizzes z ON z.id = q.quiz_id
                WHERE z.expires_at > now()
                ORDER BY q.quiz_id DESC, q.q_no
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

        return [row["id"] for row in rows]


def get_question(question_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT q.*, z.expires_at
            FROM questions q
            JOIN quizzes z ON z.id = q.quiz_id
            WHERE q.id = %s
              AND z.expires_at > now()
            """,
            (question_id,),
        ).fetchone()


async def begin_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS n
            FROM quizzes
            WHERE expires_at > now()
            GROUP BY category
            ORDER BY category
            """
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "पहले ➕ Add Quiz से Quiz डालें।",
            reply_markup=main_menu(),
        )
        return

    counts = {row["category"]: row["n"] for row in rows}

    keyboard = []
    row = []

    for label, name in CATEGORIES:
        if counts.get(name, 0):
            row.append(
                InlineKeyboardButton(
                    f"{label} ({counts[name]})",
                    callback_data=f"cat:{name}",
                )
            )
            if len(row) == 2:
                keyboard.append(row)
                row = []

    if row:
        keyboard.append(row)

    await update.effective_message.reply_text(
        "📝 Quiz खेलने के लिए Category चुनें:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def play_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    quiz_id = int(query.data.split(":")[1])

    cleanup_expired_quizzes()

    with db() as conn:
        quiz = conn.execute(
            """
            SELECT id, title, category, subcategory
            FROM quizzes
            WHERE id = %s AND expires_at > now()
            """,
            (quiz_id,),
        ).fetchone()

    if not quiz:
        await query.edit_message_text("❌ यह Quiz अब उपलब्ध नहीं है।")
        return

    await start_quiz_for_user(
        update.effective_user.id,
        update.effective_chat.id,
        context,
        quiz_id,
    )

    await query.edit_message_text(
        f"▶️ {quiz['title']}\n"
        f"🏷 {quiz['category']}"
        f"{' • ' + quiz['subcategory'] if quiz['subcategory'] else ''}\n\n"
        "Quiz शुरू हो गया।"
    )


async def start_quiz_for_user(user_id, chat_id, context, quiz_id):
    settings = get_settings(user_id)

    question_ids = get_quiz_ids(quiz_id)

    if not question_ids:
        await context.bot.send_message(
            chat_id=chat_id,
            text="इस Quiz में सवाल नहीं हैं।",
        )
        return

    if settings["random_questions"]:
        random.shuffle(question_ids)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO attempts
            (user_id, quiz_id, question_ids, position, score, mode, option_order)
            VALUES (%s, %s, %s, 0, 0, 'quiz', %s)
            ON CONFLICT (user_id) DO UPDATE SET
                quiz_id = EXCLUDED.quiz_id,
                question_ids = EXCLUDED.question_ids,
                position = 0,
                score = 0,
                mode = 'quiz',
                option_order = '[]'::jsonb
            """,
            (
                user_id,
                quiz_id,
                json.dumps(question_ids),
                json.dumps([]),
            ),
        )
        conn.commit()

    cancel_timer(context)
    await send_current(user_id, context, chat_id)


async def begin_revision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT q.id
            FROM questions q
            JOIN wrong_answers w
              ON w.question_id = q.id
            JOIN quizzes z
              ON z.id = q.quiz_id
            WHERE w.user_id = %s
              AND z.expires_at > now()
              AND w.last_wrong <= now() - interval '24 hours'
            ORDER BY w.last_wrong ASC
            LIMIT 10
            """,
            (update.effective_user.id,),
        ).fetchall()

    question_ids = [row["id"] for row in rows]

    if not question_ids:
        await update.effective_message.reply_text(
            "🔄 अभी कोई 24 घंटे पुराने गलत सवाल नहीं हैं।\n\n"
            "गलत सवाल को दोबारा ReAttempt करने से पहले 24 घंटे पूरे होने चाहिए।",
            reply_markup=main_menu(),
        )
        return

    with db() as conn:
        conn.execute(
            """
            INSERT INTO attempts
            (user_id, quiz_id, question_ids, position, score, mode, option_order)
            VALUES (%s, NULL, %s, 0, 0, 'revision', %s)
            ON CONFLICT (user_id) DO UPDATE SET
                quiz_id = NULL,
                question_ids = EXCLUDED.question_ids,
                position = 0,
                score = 0,
                mode = 'revision',
                option_order = '[]'::jsonb
            """,
            (
                update.effective_user.id,
                json.dumps(question_ids),
                json.dumps([]),
            ),
        )
        conn.commit()

    cancel_timer(context)
    await send_current(
        update.effective_user.id,
        context,
        update.effective_chat.id,
    )


def cancel_timer(context):
    task = context.user_data.pop("quiz_timer_task", None)
    if task and not task.done():
        task.cancel()


async def timeout_question(user_id, chat_id, question_id, context, seconds):
    try:
        await asyncio.sleep(seconds)

        with db() as conn:
            attempt = conn.execute(
                "SELECT * FROM attempts WHERE user_id = %s",
                (user_id,),
            ).fetchone()

            if not attempt:
                return

            ids = [int(x) for x in attempt["question_ids"]]
            position = int(attempt["position"])

            if position >= len(ids) or ids[position] != question_id:
                return

            # Timeout is treated as an incorrect attempt.
            conn.execute(
                """
                INSERT INTO wrong_answers
                (user_id, question_id, wrong_count, last_wrong)
                VALUES (%s, %s, 1, now())
                ON CONFLICT (user_id, question_id)
                DO UPDATE SET
                    wrong_count = wrong_answers.wrong_count + 1,
                    last_wrong = now()
                """,
                (user_id, question_id),
            )

            conn.execute(
                """
                UPDATE attempts
                SET position = position + 1
                WHERE user_id = %s
                """,
                (user_id,),
            )
            conn.commit()

        await context.bot.send_message(
            chat_id=chat_id,
            text="⏰ समय समाप्त! इसे गलत सवालों में जोड़ दिया गया।",
        )
        await send_current(user_id, context, chat_id)

    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("Timer task failed.")


async def send_current(user_id, context, chat_id):
    cancel_timer(context)

    with db() as conn:
        row = conn.execute(
            """
            SELECT
                a.*,
                q.id AS question_id,
                q.question,
                q.options,
                q.answer,
                q.explanation,
                z.title AS quiz_title,
                z.category,
                z.subcategory
            FROM attempts a
            JOIN questions q
              ON q.id = (a.question_ids ->> a.position)::bigint
            JOIN quizzes z
              ON z.id = q.quiz_id
            WHERE a.user_id = %s
              AND z.expires_at > now()
            """,
            (user_id,),
        ).fetchone()

    if not row:
        await finish(chat_id, user_id, context)
        return

    options = list(row["options"])
    actual_indices = list(range(4))

    settings = get_settings(user_id)

    if settings["random_options"]:
        random.shuffle(actual_indices)

    # Store displayed-index -> actual-index mapping.
    with db() as conn:
        conn.execute(
            """
            UPDATE attempts
            SET option_order = %s
            WHERE user_id = %s
            """,
            (json.dumps(actual_indices), user_id),
        )
        conn.commit()

    letters = ["A", "B", "C", "D"]

    keyboard = []
    for displayed_index, actual_index in enumerate(actual_indices):
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{letters[displayed_index]}. {options[actual_index]}",
                    callback_data=(
                        f"ans:{row['question_id']}:{displayed_index}"
                    ),
                )
            ]
        )

    mode = "🔄 Revision" if row["mode"] == "revision" else "🧠 Quiz"

    text = (
        f"{mode}\n"
        f"📚 {row['quiz_title']}\n"
        f"🏷 {row['category']}"
        f"{' • ' + row['subcategory'] if row['subcategory'] else ''}\n\n"
        f"❓ {int(row['position']) + 1}/{len(row['question_ids'])}\n\n"
        f"{row['question']}"
    )

    if settings["timer_enabled"]:
        text += f"\n\n⏱ समय: {settings['timer_seconds']} सेकंड"

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    if settings["timer_enabled"]:
        context.user_data["quiz_timer_task"] = asyncio.create_task(
            timeout_question(
                user_id,
                chat_id,
                int(row["question_id"]),
                context,
                int(settings["timer_seconds"]),
            )
        )


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    _, question_id_text, displayed_index_text = query.data.split(":")

    question_id = int(question_id_text)
    displayed_index = int(displayed_index_text)
    user_id = update.effective_user.id

    cancel_timer(context)

    with db() as conn:
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE user_id = %s",
            (user_id,),
        ).fetchone()

        if not attempt:
            await query.edit_message_text(
                "यह Quiz session खत्म हो चुका है। /quiz से फिर शुरू करें।"
            )
            return

        question_ids = [int(x) for x in attempt["question_ids"]]
        position = int(attempt["position"])

        if (
            position >= len(question_ids)
            or question_ids[position] != question_id
        ):
            await query.edit_message_text("यह सवाल अब active नहीं है।")
            return

        option_order = attempt["option_order"] or list(range(4))
        if len(option_order) != 4:
            option_order = list(range(4))

        actual_selected_option = int(option_order[displayed_index])

        question = conn.execute(
            """
            SELECT q.*, z.expires_at
            FROM questions q
            JOIN quizzes z ON z.id = q.quiz_id
            WHERE q.id = %s AND z.expires_at > now()
            """,
            (question_id,),
        ).fetchone()

        if not question:
            await query.edit_message_text("❌ यह सवाल अब उपलब्ध नहीं है।")
            return

        correct = actual_selected_option == int(question["answer"])
        new_score = int(attempt["score"]) + (1 if correct else 0)

        if correct:
            conn.execute(
                """
                DELETE FROM wrong_answers
                WHERE user_id = %s AND question_id = %s
                """,
                (user_id, question_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO wrong_answers
                (user_id, question_id, wrong_count, last_wrong)
                VALUES (%s, %s, 1, now())
                ON CONFLICT (user_id, question_id)
                DO UPDATE SET
                    wrong_count = wrong_answers.wrong_count + 1,
                    last_wrong = now()
                """,
                (user_id, question_id),
            )

        conn.execute(
            """
            UPDATE attempts
            SET position = position + 1,
                score = %s,
                option_order = '[]'::jsonb
            WHERE user_id = %s
            """,
            (new_score, user_id),
        )
        conn.commit()

    letters = ["A", "B", "C", "D"]

    if correct:
        result = "✅ सही!"
    else:
        result = (
            f"❌ गलत। सही उत्तर: "
            f"{letters[int(question['answer'])]}."
        )

    text = f"{result}\n\n"

    if question["explanation"]:
        text += f"💡 {question['explanation']}\n\n"

    text += "अगला सवाल नीचे है।"

    await query.edit_message_text(text)

    await send_current(
        user_id,
        context,
        update.effective_chat.id,
    )


async def finish(chat_id, user_id, context):
    cancel_timer(context)

    with db() as conn:
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE user_id = %s",
            (user_id,),
        ).fetchone()

    if not attempt:
        return

    total = len(attempt["question_ids"])
    score = int(attempt["score"])

    percentage = round(score * 100 / total) if total else 0

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🏁 Quiz पूरा!\n\n"
            f"स्कोर: {score}/{total}\n"
            f"प्रतिशत: {percentage}%\n\n"
            "🔄 24 घंटे बाद गलत सवाल ReAttempt में आएंगे।"
        ),
        reply_markup=main_menu(),
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    with db() as conn:
        total_questions = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM questions q
            JOIN quizzes z ON z.id = q.quiz_id
            WHERE z.expires_at > now()
            """
        ).fetchone()["n"]

        wrong_questions = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM wrong_answers w
            JOIN questions q ON q.id = w.question_id
            JOIN quizzes z ON z.id = q.quiz_id
            WHERE w.user_id = %s
              AND z.expires_at > now()
            """,
            (update.effective_user.id,),
        ).fetchone()["n"]

        ready_revision = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM wrong_answers w
            JOIN questions q ON q.id = w.question_id
            JOIN quizzes z ON z.id = q.quiz_id
            WHERE w.user_id = %s
              AND z.expires_at > now()
              AND w.last_wrong <= now() - interval '24 hours'
            """,
            (update.effective_user.id,),
        ).fetchone()["n"]

        quiz_count = conn.execute(
            "SELECT COUNT(*) AS n FROM quizzes WHERE expires_at > now()"
        ).fetchone()["n"]

    await update.message.reply_text(
        f"📊 Progress\n\n"
        f"📚 Quizzes: {quiz_count}\n"
        f"❓ Questions: {total_questions}\n"
        f"❌ गलत सवाल: {wrong_questions}\n"
        f"🔄 ReAttempt के लिए तैयार: {ready_revision}",
        reply_markup=main_menu(),
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    row = get_settings(update.effective_user.id)

    await update.message.reply_text(
        "⚙️ Quiz Settings\n\n"
        "⏱ Timer: हर सवाल के लिए समय सीमा।\n"
        "🔀 Random Questions: सवालों का क्रम बदलता है।\n"
        "🔀 Random Options: A/B/C/D options का क्रम बदलता है।\n\n"
        "इन settings को कभी भी ON/OFF कर सकते हैं।",
        reply_markup=settings_keyboard(row),
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    action = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    if action == "timer":
        with db() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id)
                VALUES (%s)
                ON CONFLICT (user_id) DO UPDATE
                SET timer_enabled = NOT user_settings.timer_enabled
                """,
                (user_id,),
            )
            conn.commit()

    elif action == "rq":
        with db() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id)
                VALUES (%s)
                ON CONFLICT (user_id) DO UPDATE
                SET random_questions = NOT user_settings.random_questions
                """,
                (user_id,),
            )
            conn.commit()

    elif action == "ro":
        with db() as conn:
            conn.execute(
                """
                INSERT INTO user_settings (user_id)
                VALUES (%s)
                ON CONFLICT (user_id) DO UPDATE
                SET random_options = NOT user_settings.random_options
                """,
                (user_id,),
            )
            conn.commit()

    elif action == "time":
        current = get_settings(user_id)
        choices = [10, 20, 30, 45, 60, 90]

        buttons = []
        row = []

        for seconds in choices:
            row.append(
                InlineKeyboardButton(
                    f"{seconds}s",
                    callback_data=f"timersec:{seconds}",
                )
            )
            if len(row) == 3:
                buttons.append(row)
                row = []

        if row:
            buttons.append(row)

        await query.edit_message_text(
            f"⏱ Timer Time\n\n"
            f"अभी: {current['timer_seconds']} सेकंड\n"
            "नया समय चुनें:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    row = get_settings(user_id)

    await query.edit_message_text(
        "⚙️ Settings updated.",
        reply_markup=settings_keyboard(row),
    )


async def timer_seconds_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    seconds = int(query.data.split(":")[1])
    user_id = update.effective_user.id

    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, timer_seconds)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE
            SET timer_seconds = EXCLUDED.timer_seconds
            """,
            (user_id, seconds),
        )
        conn.commit()

    row = get_settings(user_id)

    await query.edit_message_text(
        f"⏱ Timer अब {seconds} सेकंड है।",
        reply_markup=settings_keyboard(row),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    await update.effective_message.reply_text(
        "❓ Help\n\n"
        "/import — Quiz paste करें\n"
        "/done — Import पूरा करें\n"
        "/quiz — Quiz खेलें\n"
        "/revision — 24 घंटे पुराने गलत सवाल\n"
        "/stats — Progress\n"
        "/categories — Categories\n"
        "/settings — Timer/Random settings\n"
        "/deletequiz — Quiz manually delete करें\n"
        "/cancel — Current session cancel\n"
        "/id — Telegram ID\n\n"
        "Quiz के लिए सामान्य formats जैसे Q1., Q1), Q1:, "
        "1., 1), प्रश्न 1:, Q: और A/B/C/D options स्वीकार हैं।\n\n"
        "30 दिन पूरे होने पर Quiz अपने-आप delete हो जाता है।",
        reply_markup=main_menu(),
    )


async def post_init(application):
    init_db()
    cleanup_expired_quizzes()
    application.create_task(cleanup_loop())


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(CommandHandler("import", import_start))
    application.add_handler(CommandHandler("done", import_done))
    application.add_handler(CommandHandler("quiz", begin_quiz))
    application.add_handler(CommandHandler("revision", begin_revision))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("categories", categories))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("deletequiz", delete_quiz_start))

    # Import buttons
    application.add_handler(
        MessageHandler(
            filters.Regex(r"^✅ Quiz Done$"),
            import_done,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(r"^❌ Cancel Import$"),
            cancel,
        )
    )

    # Main bottom keyboard buttons are handled before generic text collection.
    application.add_handler(
        MessageHandler(
            filters.Regex(
                r"^(📝 Quiz|➕ Add Quiz|🔄 ReAttempt|📊 Stats|"
                r"📚 Categories|⚙️ Settings|❓ Help)$"
            ),
            collect_text,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            delete_quiz_callback,
            pattern=r"^deletequiz:(?:list|ask|confirm|cancel)(?::\d+)?$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            category_callback,
            pattern=r"^cat:.+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            category_list_callback,
            pattern=r"^catlist$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            play_quiz_callback,
            pattern=r"^playquiz:\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            settings_callback,
            pattern=r"^set:(?:timer|rq|ro|time)$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            timer_seconds_callback,
            pattern=r"^timersec:\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            save_category_callback,
            pattern=r"^setcat:.+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            answer,
            pattern=r"^ans:\d+:[0-3]$",
        )
    )

    # Generic text must be last.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            collect_text,
        )
    )

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{PUBLIC_URL}/telegram",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


async def save_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    category = query.data.split(":", 1)[1]
    await save_pending_quiz(update, context, category)


if __name__ == "__main__":
    main()
