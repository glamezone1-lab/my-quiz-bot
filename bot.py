import os
import re
import json
import logging
import asyncio

import psycopg
from psycopg.rows import dict_row
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
CLEANUP_INTERVAL_SECONDS = 3600

if not PUBLIC_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is missing. This bot is designed to run on Render.")


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
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
                mode TEXT NOT NULL DEFAULT 'quiz'
            );

            CREATE TABLE IF NOT EXISTS wrong_answers (
                user_id BIGINT NOT NULL,
                question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                wrong_count INTEGER NOT NULL DEFAULT 1,
                last_wrong TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, question_id)
            );
            """
        )
        conn.execute(
            "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"
        )
        conn.execute(
            "UPDATE quizzes SET expires_at = created_at + interval '30 days' WHERE expires_at IS NULL"
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
    # Remove only surrounding whitespace; keep normal punctuation/emoji.
    return re.sub(r"\s+", " ", value.strip())


def _normalize_answer_token(token: str):
    token = token.strip().upper()
    # Remove common decoration around the answer, e.g. "✅ B" / "(B)".
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
        val(r"^\s*(?:QUIZ|TITLE|शीर्षक)\s*[:\-]\s*(.+)$")
        or "My Quiz"
    )
    exam = val(r"^\s*(?:EXAM|परीक्षा)\s*[:\-]\s*(.+)$")
    subject = val(r"^\s*(?:SUBJECT|विषय)\s*[:\-]\s*(.+)$")
    topic = val(r"^\s*(?:TOPIC|अध्याय|टॉपिक)\s*[:\-]\s*(.+)$")
    return title, exam, subject, topic


def _is_answer_key_line(line: str) -> bool:
    x = line.strip()
    if not x:
        return False
    return bool(
        re.match(
            r"(?i)^(?:[✅✔️☑️✓👉🟢\s]*)?(?:ANSWER\s*KEY|ANSWERS?|उत्तर\s*कुंजी|उत्तर\s*तालिका|KEY)\s*[:\-]?",
            x,
        )
    ) or bool(
        re.match(
            r"(?i)^[\s\dABCD|,:;=\-\.]+$", x
        ) and re.search(r"\d\s*[-=:]\s*[ABCD1-4]", x, re.I)
    )


def extract_answer_key(text: str):
    answers = {}
    patterns = [
        r"(?im)^\s*(?:[✅✔️☑️✓👉🟢\s]*)?(?:ANSWER\s*KEY|KEY|ANSWERS?|उत्तर\s*कुंजी|उत्तर\s*तालिका)\s*[:\-]?\s*(.+?)\s*$",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            chunk = m.group(1)
            # 1-B / Q1-B / 1: B / 1=B / 1)B / 1. B
            for q, a in re.findall(
                r"(?i)(?:Q(?:UESTION)?\s*)?(\d+)\s*[\-:=\.\)]\s*([ABCD1-4])\b",
                chunk,
            ):
                ans = _normalize_answer_token(a)
                if ans is not None:
                    answers[int(q)] = ans
            # 1=B style, including comma/pipe separated keys
            for q, a in re.findall(r"(?i)(\d+)\s*=\s*([ABCD1-4])\b", chunk):
                ans = _normalize_answer_token(a)
                if ans is not None:
                    answers[int(q)] = ans
    return answers


def extract_inline_answer(block: str):
    # Deliberately allow emoji/symbols before ANSWER and optional words such as
    # "OPTION" or "विकल्प". Also accept numeric answers 1-4.
    prefix = r"^[\s\u200b]*(?:[\W_]*?)?"
    labels = r"(?:ANSWER|ANS|CORRECT\s*ANSWER|RIGHT\s*ANSWER|CORRECT|सही\s*उत्तर|उत्तर|सही\s*विकल्प)"

    patterns = [
        rf"(?im){prefix}{labels}\s*[:=\-]?\s*(?:OPTION|विकल्प)?\s*[\(\[]?\s*([ABCD])\b",
        rf"(?im){prefix}{labels}\s*[:=\-]?\s*(?:OPTION|विकल्प)?\s*[\(\[]?\s*([1-4])\b",
    ]
    for pat in patterns:
        m = re.search(pat, block)
        if m:
            return _normalize_answer_token(m.group(1))
    return None


def extract_explanation(block: str):
    m = re.search(
        r"(?ims)^\s*(?:[💡📝📌👉\s]*)?(?:EXPLANATION|WHY|व्याख्या|कारण|विवरण|स्पष्टीकरण)\s*[:\-]?\s*(.*?)(?="
        r"^\s*(?:[\W_]*)(?:ANSWER|ANS|CORRECT\s*ANSWER|RIGHT\s*ANSWER|सही\s*उत्तर|उत्तर)\s*[:=\-]?|\Z)",
        block,
    )
    return clean_line(m.group(1)) if m else ""


def _question_matches(text: str):
    # Support Q1., Q1), Q1:, QUESTION 1:, प्रश्न 1:, 1., 1), etc.
    # Q: / QUESTION: without a number is also accepted; it gets a sequential number.
    question_re = re.compile(
        r"(?im)^\s*(?:"
        r"Q(?:UESTION)?\s*(\d+)?"
        r"|प्रश्न\s*(\d+)"
        r"|(\d+)"
        r")\s*[\.:\)\-]\s*(.+?)\s*$"
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

    title, exam, subject, topic = extract_meta(text)
    answer_key = extract_answer_key(text)
    matches = _question_matches(text)

    if not matches:
        raise ValueError(
            "सवाल नहीं मिले। Q1., Q1), Q1:, 1., 1), प्रश्न 1: या Q: जैसे format रखें।"
        )

    questions = []

    for idx, match in enumerate(matches):
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[match.end():block_end]

        groups = match.groups()
        q_no = next((int(g) for g in groups[:3] if g), idx + 1)
        question_text = clean_line(groups[3])

        # If the question wraps to additional lines before option A, preserve them.
        pre_option = re.split(
            r"(?im)^\s*[A-D][\)\.\:\-]\s+",
            block,
            maxsplit=1,
        )[0]
        if pre_option.strip():
            question_text = clean_line(question_text + " " + pre_option)

        options = {}
        for letter, value in re.findall(
            r"(?im)^\s*([ABCD])\s*[\)\.\:\-]\s*(.+?)\s*$",
            block,
        ):
            options[letter.upper()] = clean_line(value)

        if set(options) != {"A", "B", "C", "D"}:
            raise ValueError(
                f"प्रश्न {idx + 1} में A, B, C, D चारों options नहीं मिले।"
            )

        answer = extract_inline_answer(block)
        if answer is None:
            answer = answer_key.get(q_no)

        # Fallback for variants like "Correct: 2" / "Ans - C".
        if answer is None:
            mnum = re.search(
                r"(?im)^\s*(?:[\W_]*)(?:ANSWER|ANS|CORRECT|उत्तर|सही\s*उत्तर)\s*[:=\-]?\s*([1-4ABCD])\b",
                block,
            )
            if mnum:
                answer = _normalize_answer_token(mnum.group(1))

        if answer not in range(4):
            raise ValueError(
                f"प्रश्न {idx + 1} का सही उत्तर नहीं मिला। "
                "ये formats स्वीकार हैं: ANSWER: B, ✅ सही उत्तर: B, Correct Answer: 2, "
                "या Answer Key: 1-B | 2-C"
            )

        questions.append(
            {
                "q_no": len(questions) + 1,
                "question": question_text,
                "options": [options["A"], options["B"], options["C"], options["D"]],
                "answer": answer,
                "explanation": extract_explanation(block),
            }
        )

    if len(questions) > 100:
        raise ValueError("एक Quiz में अधिकतम 100 सवाल रखें।")

    return title, questions


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Delete Quiz", callback_data="deletequiz:list")],
    ])

    await update.message.reply_text(
        "📚 My Revision Quiz\n\n"
        "/import — ChatGPT से Quiz paste करें\n"
        "/done — import पूरा करें\n"
        "/quiz — नया Quiz खेलें\n"
        "/revision — गलत हुए सवाल दोबारा\n"
        "/stats — आपका progress\n"
        "/id — Telegram ID\n"
        "/deletequiz — Quiz manually delete करें\n"
        "/cancel — current session रद्द करें",
        reply_markup=keyboard,
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    await update.message.reply_text(
        f"आपका Telegram ID:\n{update.effective_user.id}"
    )


async def import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    context.user_data["importing"] = True
    context.user_data["import_text"] = ""

    await update.message.reply_text(
        "📥 Quiz paste mode शुरू।\n\n"
        "बहुत लंबा Quiz हो तो कई messages में लगातार भेजें। "
        "मैं सभी हिस्से जोड़ दूँगा।\n\n"
        "सब भेजने के बाद /done लिखें।\n"
        "अधिकतम कुल import: 10 लाख characters।"
    )


async def collect_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    if not context.user_data.get("importing"):
        await update.message.reply_text("पहले /import लिखें।")
        return

    text = update.message.text or ""
    total = context.user_data.get("import_text", "") + "\n" + text

    if len(total) > IMPORT_MAX_CHARS:
        await update.message.reply_text(
            "❌ Import limit पार हो गई। अधिकतम कुल 10 लाख characters हैं।"
        )
        return

    context.user_data["import_text"] = total

    await update.message.reply_text(
        "✅ हिस्सा मिल गया। और भेजें या /done लिखें।"
    )


async def import_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    if not context.user_data.get("importing"):
        await update.message.reply_text(
            "अभी कोई import चालू नहीं है।"
        )
        return

    text = context.user_data.get("import_text", "")

    context.user_data.pop("importing", None)
    context.user_data.pop("import_text", None)

    try:
        title, questions = parse_quiz(text)
    except ValueError as error:
        context.user_data["importing"] = True
        context.user_data["import_text"] = text
        await update.message.reply_text(
            f"❌ Format error:\n{error}\n\n"
            "Import अभी बंद नहीं हुआ है। Text ठीक करके फिर भेजें या /cancel करें।"
        )
        return

    with db() as conn:
        quiz_row = conn.execute(
            """
            INSERT INTO quizzes (title, expires_at)
            VALUES (%s, now() + interval '30 days')
            RETURNING id
            """,
            (title,),
        ).fetchone()

        quiz_id = quiz_row["id"]

        for question in questions:
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
                    json.dumps(question["options"]),
                    question["answer"],
                    question["explanation"],
                ),
            )

        conn.commit()

    await update.message.reply_text(
        f"🎉 Quiz save हो गया!\n\n"
        f"📚 {title}\n"
        f"❓ {len(questions)} सवाल\n\n"
        f"/quiz से खेलें।"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    context.user_data.clear()

    await update.message.reply_text(
        "ठीक है, current session cancel कर दिया।"
    )


async def delete_quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT z.id, z.title, z.created_at, COUNT(q.id) AS question_count
            FROM quizzes z
            LEFT JOIN questions q ON q.quiz_id = z.id
            WHERE z.expires_at > now()
            GROUP BY z.id
            ORDER BY z.id DESC
            LIMIT 20
            """
        ).fetchall()

    if not rows:
        await update.message.reply_text("🗑 अभी delete करने के लिए कोई Quiz नहीं है।")
        return

    keyboard = []
    for row in rows:
        title = clean_line(row["title"])
        if len(title) > 42:
            title = title[:39] + "..."
        keyboard.append([
            InlineKeyboardButton(
                f"🗑 {title} ({row['question_count']})",
                callback_data=f"deletequiz:ask:{row['id']}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton("❌ Cancel", callback_data="deletequiz:cancel")
    ])

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
        # Reuse the command-like flow for the inline button.
        cleanup_expired_quizzes()
        with db() as conn:
            rows = conn.execute(
                """
                SELECT z.id, z.title, COUNT(q.id) AS question_count
                FROM quizzes z
                LEFT JOIN questions q ON q.quiz_id = z.id
                WHERE z.expires_at > now()
                GROUP BY z.id
                ORDER BY z.id DESC
                LIMIT 20
                """
            ).fetchall()

        if not rows:
            await query.edit_message_text("🗑 अभी delete करने के लिए कोई Quiz नहीं है।")
            return

        keyboard = []
        for row in rows:
            title = clean_line(row["title"])
            if len(title) > 42:
                title = title[:39] + "..."
            keyboard.append([
                InlineKeyboardButton(
                    f"🗑 {title} ({row['question_count']})",
                    callback_data=f"deletequiz:ask:{row['id']}",
                )
            ])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="deletequiz:cancel")])
        await query.edit_message_text(
            "🗑 Quiz Delete\n\nजिस Quiz को हटाना है उसे चुनें।\n"
            "Delete करने पर उसके सवाल भी हमेशा के लिए हट जाएंगे।",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action == "ask" and len(parts) == 3:
        quiz_id = int(parts[2])
        with db() as conn:
            row = conn.execute(
                """
                SELECT z.id, z.title, COUNT(q.id) AS question_count
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

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚠️ हाँ, Delete", callback_data=f"deletequiz:confirm:{quiz_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="deletequiz:cancel"),
            ]
        ])
        await query.edit_message_text(
            f"⚠️ क्या यह Quiz delete करना है?\n\n"
            f"📚 {row['title']}\n"
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
                await query.edit_message_text("❌ यह Quiz पहले ही delete हो चुका है।")
                return

            conn.execute("DELETE FROM quizzes WHERE id = %s", (quiz_id,))
            conn.commit()

        await query.edit_message_text(
            f"✅ Quiz delete हो गया।\n\n📚 {row['title']}\n\n"
            "उसके सभी questions भी delete हो गए हैं।"
        )
        return

    await query.edit_message_text("❌ Invalid delete action।")


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
            "SELECT * FROM questions WHERE id = %s",
            (question_id,),
        ).fetchone()


async def begin_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    cleanup_expired_quizzes()

    with db() as conn:
        quiz = conn.execute(
            """
            SELECT id, title
            FROM quizzes
            WHERE expires_at > now()
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if not quiz:
        await update.message.reply_text(
            "पहले /import से Quiz डालें।"
        )
        return

    question_ids = get_quiz_ids(quiz["id"])

    if not question_ids:
        await update.message.reply_text(
            "इस Quiz में सवाल नहीं हैं।"
        )
        return

    with db() as conn:
        conn.execute(
            """
            INSERT INTO attempts
            (user_id, quiz_id, question_ids, position, score, mode)
            VALUES (%s, %s, %s, 0, 0, 'quiz')
            ON CONFLICT (user_id) DO UPDATE SET
                quiz_id = EXCLUDED.quiz_id,
                question_ids = EXCLUDED.question_ids,
                position = 0,
                score = 0,
                mode = 'quiz'
            """,
            (
                update.effective_user.id,
                quiz["id"],
                json.dumps(question_ids),
            ),
        )
        conn.commit()

    await send_current(
        update.effective_user.id,
        context,
        update.effective_chat.id,
    )


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
            ORDER BY w.last_wrong DESC
            LIMIT 10
            """,
            (update.effective_user.id,),
        ).fetchall()

    question_ids = [row["id"] for row in rows]

    if not question_ids:
        await update.message.reply_text(
            "अभी कोई गलत सवाल save नहीं है। पहले /quiz खेलें।"
        )
        return

    with db() as conn:
        conn.execute(
            """
            INSERT INTO attempts
            (user_id, quiz_id, question_ids, position, score, mode)
            VALUES (%s, NULL, %s, 0, 0, 'revision')
            ON CONFLICT (user_id) DO UPDATE SET
                quiz_id = NULL,
                question_ids = EXCLUDED.question_ids,
                position = 0,
                score = 0,
                mode = 'revision'
            """,
            (
                update.effective_user.id,
                json.dumps(question_ids),
            ),
        )
        conn.commit()

    await send_current(
        update.effective_user.id,
        context,
        update.effective_chat.id,
    )


async def send_current(user_id, context, chat_id):
    with db() as conn:
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE user_id = %s",
            (user_id,),
        ).fetchone()

    question_ids = attempt["question_ids"]
    position = attempt["position"]

    if position >= len(question_ids):
        await finish(chat_id, user_id, context)
        return

    question = get_question(question_ids[position])

    letters = ["A", "B", "C", "D"]

    keyboard = [
        [
            InlineKeyboardButton(
                f"{letters[i]}. {option}",
                callback_data=f"ans:{question['id']}:{i}",
            )
        ]
        for i, option in enumerate(question["options"])
    ]

    mode = (
        "🔄 Revision"
        if attempt["mode"] == "revision"
        else "🧠 Quiz"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{mode}\n\n"
            f"❓ {position + 1}/{len(question_ids)}\n\n"
            f"{question['question']}"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not allowed(update):
        return

    _, question_id_text, option_text = query.data.split(":")

    question_id = int(question_id_text)
    selected_option = int(option_text)
    user_id = update.effective_user.id

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

    question_ids = attempt["question_ids"]
    position = attempt["position"]

    if (
        position >= len(question_ids)
        or int(question_ids[position]) != question_id
    ):
        await query.edit_message_text(
            "यह सवाल अब active नहीं है।"
        )
        return

    question = get_question(question_id)
    correct = selected_option == question["answer"]

    new_score = attempt["score"] + (1 if correct else 0)

    with db() as conn:
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
            SET position = %s, score = %s
            WHERE user_id = %s
            """,
            (position + 1, new_score, user_id),
        )

        conn.commit()

    letters = ["A", "B", "C", "D"]

    if correct:
        result = "✅ सही!"
    else:
        result = (
            f"❌ गलत। सही उत्तर: "
            f"{letters[question['answer']]}."
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
    with db() as conn:
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE user_id = %s",
            (user_id,),
        ).fetchone()

    total = len(attempt["question_ids"])
    score = attempt["score"]

    percentage = (
        round(score * 100 / total)
        if total
        else 0
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🏁 Quiz पूरा!\n\n"
            f"स्कोर: {score}/{total}\n"
            f"प्रतिशत: {percentage}%\n\n"
            "❌ गलत सवालों के लिए /revision"
        ),
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

        quiz_count = conn.execute(
            "SELECT COUNT(*) AS n FROM quizzes WHERE expires_at > now()"
        ).fetchone()["n"]

    await update.message.reply_text(
        f"📊 Progress\n\n"
        f"📚 Quizzes: {quiz_count}\n"
        f"❓ Questions: {total_questions}\n"
        f"❌ Revision questions: {wrong_questions}"
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

    application.add_handler(
        CommandHandler("start", start)
    )
    application.add_handler(
        CommandHandler("help", start)
    )
    application.add_handler(
        CommandHandler("id", show_id)
    )
    application.add_handler(
        CommandHandler("import", import_start)
    )
    application.add_handler(
        CommandHandler("done", import_done)
    )
    application.add_handler(
        CommandHandler("quiz", begin_quiz)
    )
    application.add_handler(
        CommandHandler("revision", begin_revision)
    )
    application.add_handler(
        CommandHandler("stats", stats)
    )
    application.add_handler(
        CommandHandler("cancel", cancel)
    )
    application.add_handler(
        CommandHandler("deletequiz", delete_quiz_start)
    )


    application.add_handler(
        CallbackQueryHandler(
            delete_quiz_callback,
            pattern=r"^deletequiz:(?:list|ask|confirm|cancel)(?::\d+)?$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            answer,
            pattern=r"^ans:\d+:[0-3]$",
        )
    )

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


if __name__ == "__main__":
    main()
