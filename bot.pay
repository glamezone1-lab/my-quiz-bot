import os
import re
import json
import logging
import asyncio

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
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
REATTEMPT_DELAY_HOURS = 24
CLEANUP_INTERVAL_SECONDS = 3600

if not PUBLIC_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is missing. This bot is designed to run on Render.")

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days')
            );

            ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
            UPDATE quizzes
            SET expires_at = created_at + interval '30 days'
            WHERE expires_at IS NULL;

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
                retry_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours'),
                retry_notified BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (user_id, question_id)
            );

            ALTER TABLE wrong_answers ADD COLUMN IF NOT EXISTS retry_at TIMESTAMPTZ;
            ALTER TABLE wrong_answers ADD COLUMN IF NOT EXISTS retry_notified BOOLEAN;
            UPDATE wrong_answers
            SET retry_at = COALESCE(retry_at, last_wrong + interval '24 hours'),
                retry_notified = COALESCE(retry_notified, FALSE)
            WHERE retry_at IS NULL OR retry_notified IS NULL;

            CREATE INDEX IF NOT EXISTS idx_quizzes_expires_at
                ON quizzes (expires_at);
            CREATE INDEX IF NOT EXISTS idx_questions_quiz_id_q_no
                ON questions (quiz_id, q_no);
            CREATE INDEX IF NOT EXISTS idx_wrong_answers_retry
                ON wrong_answers (retry_at, retry_notified);
            CREATE INDEX IF NOT EXISTS idx_wrong_answers_user_last_wrong
                ON wrong_answers (user_id, last_wrong DESC);
            """
        )
        conn.commit()


def cleanup_expired_quizzes():
    with db() as conn:
        result = conn.execute("DELETE FROM quizzes WHERE expires_at <= now()")
        conn.commit()
        if result.rowcount:
            log.info("Deleted %s expired quiz(es).", result.rowcount)


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


def parse_quiz(text: str):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    title_match = re.search(
        r"(?im)^\s*(?:QUIZ|TITLE|शीर्षक)\s*:\s*(.+?)\s*$",
        text,
    )
    title = title_match.group(1).strip() if title_match else "My Quiz"

    q_pattern = re.compile(
        r"(?im)^\s*(?:(?:Q\s*\d*|प्रश्न\s*\d+|\d+)\s*[\:\.\)\-])\s*(.+?)\s*$"
    )
    matches = list(q_pattern.finditer(text))

    if not matches:
        raise ValueError(
            "Question markers नहीं मिले। Q1:, Q1., Q1), 1., 1), या प्रश्न 1: जैसे format रखें।"
        )

    questions = []
    for i, match in enumerate(matches):
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[match.end():block_end]
        question_text = match.group(1).strip()

        options = {}
        for letter, value in re.findall(
            r"(?im)^\s*([ABCD])\s*[\)\.\:\-]\s*(.+?)\s*$", block
        ):
            options[letter.upper()] = value.strip()

        answer_match = re.search(
            r"(?im)^\s*(?:ANSWER|ANS|उत्तर)\s*[\:\-]?\s*([ABCD])\s*$",
            block,
        )

        explanation_match = re.search(
            r"(?ims)^\s*(?:EXPLANATION|WHY|व्याख्या|कारण)\s*[\:\-]\s*(.*?)(?=^\s*(?:Q\s*\d*|प्रश्न\s*\d+|\d+)\s*[\:\.\)\-]|^\s*(?:ANSWER|ANS|उत्तर)\s*[\:\-]|\Z)",
            block,
        )

        if set(options) != {"A", "B", "C", "D"}:
            raise ValueError(f"प्रश्न {i + 1} में A, B, C, D चारों options नहीं मिले।")
        if not answer_match:
            raise ValueError(f"प्रश्न {i + 1} का ANSWER नहीं मिला।")

        questions.append({
            "q_no": i + 1,
            "question": question_text,
            "options": [options["A"], options["B"], options["C"], options["D"]],
            "answer": ord(answer_match.group(1).upper()) - 65,
            "explanation": explanation_match.group(1).strip() if explanation_match else "",
        })

    return title, questions


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired_quizzes()
    await update.message.reply_text(
        "📚 My Revision Quiz\n\n"
        "/import — ChatGPT से Quiz paste करें\n"
        "/done — import पूरा करें\n"
        "/quiz — नया Quiz खेलें\n"
        "/revision — गलत हुए सवाल दोबारा\n"
        "/stats — आपका progress\n"
        "/id — Telegram ID\n"
        "/cancel — current session रद्द करें\n\n"
        "🗑️ Quiz 30 दिन बाद अपने-आप delete होते हैं।\n"
        "⏰ गलत सवाल 24 घंटे बाद ReAttempt के लिए आते हैं।"
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        await update.message.reply_text(f"आपका Telegram ID:\n{update.effective_user.id}")


async def import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data["importing"] = True
    context.user_data["import_text"] = ""
    await update.message.reply_text(
        "📥 Quiz paste mode शुरू।\n\n"
        "Quiz बहुत लंबा हो तो कई messages में लगातार भेजें। मैं सभी हिस्से जोड़ दूँगा।\n"
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
        await update.message.reply_text("❌ Import limit पार हो गई। अधिकतम कुल 10 लाख characters हैं।")
        return

    context.user_data["import_text"] = total
    await update.message.reply_text(
        f"✅ हिस्सा मिल गया। कुल लगभग {len(total):,} characters जमा हैं।\nऔर भेजें या /done लिखें।"
    )


async def import_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if not context.user_data.get("importing"):
        await update.message.reply_text("अभी कोई import चालू नहीं है।")
        return

    text = context.user_data.get("import_text", "")
    try:
        title, questions = parse_quiz(text)
    except ValueError as error:
        await update.message.reply_text(
            f"❌ Format error:\n{error}\n\nText ठीक करके फिर भेजें या /cancel करें।"
        )
        return

    context.user_data.pop("importing", None)
    context.user_data.pop("import_text", None)
    cleanup_expired_quizzes()

    with db() as conn:
        quiz_id = conn.execute(
            "INSERT INTO quizzes (title, expires_at) VALUES (%s, now() + interval '30 days') RETURNING id",
            (title,),
        ).fetchone()["id"]
        for question in questions:
            conn.execute(
                """
                INSERT INTO questions (quiz_id, q_no, question, options, answer, explanation)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (quiz_id, question["q_no"], question["question"], json.dumps(question["options"]), question["answer"], question["explanation"]),
            )
        conn.commit()

    await update.message.reply_text(
        f"🎉 Quiz save हो गया!\n\n📚 {title}\n❓ {len(questions)} सवाल\n🗑️ 30 दिन बाद auto-delete\n\n/quiz से खेलें।"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data.clear()
    await update.message.reply_text("ठीक है, current session cancel कर दिया।")


def get_quiz_ids(quiz_id=None, limit=100):
    with db() as conn:
        if quiz_id:
            rows = conn.execute(
                """SELECT q.id FROM questions q JOIN quizzes z ON z.id=q.quiz_id
                   WHERE q.quiz_id=%s AND z.expires_at>now() ORDER BY q.q_no LIMIT %s""",
                (quiz_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT q.id FROM questions q JOIN quizzes z ON z.id=q.quiz_id
                   WHERE z.expires_at>now() ORDER BY q.quiz_id DESC, q.q_no LIMIT %s""",
                (limit,),
            ).fetchall()
    return [row["id"] for row in rows]


QUESTION_CACHE = {}


def get_question(question_id):
    if question_id in QUESTION_CACHE:
        return QUESTION_CACHE[question_id]
    with db() as conn:
        question = conn.execute("SELECT * FROM questions WHERE id=%s", (question_id,)).fetchone()
    if question:
        QUESTION_CACHE[question_id] = question
        if len(QUESTION_CACHE) > 500:
            QUESTION_CACHE.pop(next(iter(QUESTION_CACHE)))
    return question


async def begin_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired_quizzes()
    with db() as conn:
        quiz = conn.execute(
            "SELECT id,title FROM quizzes WHERE expires_at>now() ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not quiz:
        await update.message.reply_text("पहले /import से Quiz डालें।")
        return
    question_ids = get_quiz_ids(quiz["id"])
    if not question_ids:
        await update.message.reply_text("इस Quiz में सवाल नहीं हैं।")
        return
    with db() as conn:
        conn.execute(
            """INSERT INTO attempts (user_id,quiz_id,question_ids,position,score,mode)
               VALUES (%s,%s,%s,0,0,'quiz')
               ON CONFLICT (user_id) DO UPDATE SET quiz_id=EXCLUDED.quiz_id,
               question_ids=EXCLUDED.question_ids,position=0,score=0,mode='quiz'""",
            (update.effective_user.id, quiz["id"], json.dumps(question_ids)),
        )
        conn.commit()
    await send_current(update.effective_user.id, context, update.effective_chat.id)


async def begin_revision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired_quizzes()
    with db() as conn:
        rows = conn.execute(
            """SELECT q.id FROM questions q JOIN wrong_answers w ON w.question_id=q.id
               JOIN quizzes z ON z.id=q.quiz_id
               WHERE w.user_id=%s AND z.expires_at>now()
               ORDER BY w.last_wrong DESC LIMIT 10""",
            (update.effective_user.id,),
        ).fetchall()
    question_ids = [row["id"] for row in rows]
    if not question_ids:
        await update.message.reply_text("अभी कोई गलत सवाल save नहीं है। पहले /quiz खेलें।")
        return
    with db() as conn:
        conn.execute(
            """INSERT INTO attempts (user_id,quiz_id,question_ids,position,score,mode)
               VALUES (%s,NULL,%s,0,0,'revision')
               ON CONFLICT (user_id) DO UPDATE SET quiz_id=NULL,
               question_ids=EXCLUDED.question_ids,position=0,score=0,mode='revision'""",
            (update.effective_user.id, json.dumps(question_ids)),
        )
        conn.commit()
    await send_current(update.effective_user.id, context, update.effective_chat.id)


async def send_current(user_id, context, chat_id):
    with db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE user_id=%s", (user_id,)).fetchone()
    if not attempt:
        return
    question_ids = attempt["question_ids"]
    position = attempt["position"]
    if position >= len(question_ids):
        await finish(chat_id, user_id, context)
        return
    question = get_question(question_ids[position])
    if not question:
        await context.bot.send_message(chat_id=chat_id, text="यह सवाल अब उपलब्ध नहीं है। /quiz से फिर शुरू करें।")
        return
    letters = ["A", "B", "C", "D"]
    keyboard = [[InlineKeyboardButton(f"{letters[i]}. {option}", callback_data=f"ans:{question['id']}:{i}")] for i, option in enumerate(question["options"])]
    mode = "🔄 Revision" if attempt["mode"] == "revision" else "🧠 Quiz"
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{mode}\n\n❓ {position + 1}/{len(question_ids)}\n\n{question['question']}",
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
        attempt = conn.execute("SELECT * FROM attempts WHERE user_id=%s", (user_id,)).fetchone()
    if not attempt:
        await query.edit_message_text("यह Quiz session खत्म हो चुका है। /quiz से फिर शुरू करें।")
        return

    question_ids = attempt["question_ids"]
    position = attempt["position"]
    if position >= len(question_ids) or int(question_ids[position]) != question_id:
        await query.edit_message_text("यह सवाल अब active नहीं है।")
        return

    question = get_question(question_id)
    if not question:
        await query.edit_message_text("यह सवाल अब उपलब्ध नहीं है।")
        return
    correct = selected_option == question["answer"]
    new_score = attempt["score"] + (1 if correct else 0)

    with db() as conn:
        if correct:
            conn.execute("DELETE FROM wrong_answers WHERE user_id=%s AND question_id=%s", (user_id, question_id))
        else:
            conn.execute(
                """INSERT INTO wrong_answers (user_id,question_id,wrong_count,last_wrong,retry_at,retry_notified)
                   VALUES (%s,%s,1,now(),now()+interval '24 hours',FALSE)
                   ON CONFLICT (user_id,question_id) DO UPDATE SET
                   wrong_count=wrong_answers.wrong_count+1,last_wrong=now(),
                   retry_at=now()+interval '24 hours',retry_notified=FALSE""",
                (user_id, question_id),
            )
        conn.execute("UPDATE attempts SET position=%s,score=%s WHERE user_id=%s", (position + 1, new_score, user_id))
        conn.commit()

    letters = ["A", "B", "C", "D"]
    result = "✅ सही!" if correct else f"❌ गलत। सही उत्तर: {letters[question['answer']]} ."
    text = result + "\n\n"
    if question["explanation"]:
        text += f"💡 {question['explanation']}\n\n"
    text += "अगला सवाल नीचे है।"
    await query.edit_message_text(text)
    await send_current(user_id, context, update.effective_chat.id)


async def reattempt_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        return
    _, question_id_text, option_text = query.data.split(":")
    question_id = int(question_id_text)
    selected_option = int(option_text)
    user_id = update.effective_user.id
    question = get_question(question_id)
    if not question:
        await query.edit_message_text("यह सवाल अब उपलब्ध नहीं है।")
        return

    correct = selected_option == question["answer"]
    with db() as conn:
        if correct:
            conn.execute("DELETE FROM wrong_answers WHERE user_id=%s AND question_id=%s", (user_id, question_id))
        else:
            conn.execute(
                """UPDATE wrong_answers SET wrong_count=wrong_count+1,last_wrong=now(),
                   retry_at=now()+interval '24 hours',retry_notified=FALSE
                   WHERE user_id=%s AND question_id=%s""",
                (user_id, question_id),
            )
        conn.commit()

    if correct:
        await query.edit_message_text("✅ सही! यह सवाल ReAttempt list से हट गया।")
    else:
        await query.edit_message_text("❌ फिर गलत। यह सवाल 24 घंटे बाद फिर आएगा।")


async def send_due_reattempts(application):
    cleanup_expired_quizzes()
    with db() as conn:
        rows = conn.execute(
            """SELECT w.user_id,w.question_id,q.question,q.options
               FROM wrong_answers w JOIN questions q ON q.id=w.question_id
               JOIN quizzes z ON z.id=q.quiz_id
               WHERE w.retry_at<=now() AND w.retry_notified=FALSE AND z.expires_at>now()
               ORDER BY w.retry_at LIMIT 20"""
        ).fetchall()

        for row in rows:
            keyboard = [
                [InlineKeyboardButton(f"{['A','B','C','D'][i]}. {option}", callback_data=f"retry:{row['question_id']}:{i}")]
                for i, option in enumerate(row["options"])
            ]
            try:
                await application.bot.send_message(
                    chat_id=row["user_id"],
                    text=f"🔄 ReAttempt (24 घंटे बाद)\n\n❓ {row['question']}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                conn.execute(
                    "UPDATE wrong_answers SET retry_notified=TRUE WHERE user_id=%s AND question_id=%s",
                    (row["user_id"], row["question_id"]),
                )
                conn.commit()
            except Exception:
                log.exception("Failed to send reattempt for user %s question %s", row["user_id"], row["question_id"])


async def background_loop(application):
    while True:
        try:
            cleanup_expired_quizzes()
            await send_due_reattempts(application)
        except Exception:
            log.exception("Background task failed")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def finish(chat_id, user_id, context):
    with db() as conn:
        attempt = conn.execute("SELECT * FROM attempts WHERE user_id=%s", (user_id,)).fetchone()
    if not attempt:
        return
    total = len(attempt["question_ids"])
    score = attempt["score"]
    percentage = round(score * 100 / total) if total else 0
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🏁 Quiz पूरा!\n\nस्कोर: {score}/{total}\nप्रतिशत: {percentage}%\n\n❌ गलत सवालों के लिए /revision\n⏰ गलत सवाल 24 घंटे बाद ReAttempt होंगे।",
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    cleanup_expired_quizzes()
    with db() as conn:
        quiz_count = conn.execute("SELECT COUNT(*) AS n FROM quizzes WHERE expires_at>now()").fetchone()["n"]
        total_questions = conn.execute(
            "SELECT COUNT(*) AS n FROM questions q JOIN quizzes z ON z.id=q.quiz_id WHERE z.expires_at>now()"
        ).fetchone()["n"]
        wrong_questions = conn.execute(
            """SELECT COUNT(*) AS n FROM wrong_answers w JOIN questions q ON q.id=w.question_id
               JOIN quizzes z ON z.id=q.quiz_id WHERE w.user_id=%s AND z.expires_at>now()""",
            (update.effective_user.id,),
        ).fetchone()["n"]
    await update.message.reply_text(
        f"📊 Progress\n\n📚 Active Quizzes: {quiz_count}\n❓ Questions: {total_questions}\n❌ ReAttempt questions: {wrong_questions}\n\n🗑️ Quiz 30 दिन बाद auto-delete होता है।"
    )


async def post_init(application):
    POOL.open(wait=True)
    init_db()
    cleanup_expired_quizzes()
    application.create_task(background_loop(application))


async def post_shutdown(application):
    try:
        POOL.close()
    except Exception:
        log.exception("Database pool close failed")


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(CommandHandler("import", import_start))
    application.add_handler(CommandHandler("done", import_done))
    application.add_handler(CommandHandler("quiz", begin_quiz))
    application.add_handler(CommandHandler("revision", begin_revision))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("cancel", cancel))

    application.add_handler(CallbackQueryHandler(reattempt_answer, pattern=r"^retry:\d+:[0-3]$"))
    application.add_handler(CallbackQueryHandler(answer, pattern=r"^ans:\d+:[0-3]$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_text))

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
