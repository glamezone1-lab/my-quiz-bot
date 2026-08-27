import os
import re
import sqlite3
import logging
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_ID_RAW = os.getenv("ADMIN_USER_ID", "").strip()
ADMIN_ID: Optional[int] = None

if ADMIN_ID_RAW:
    try:
        ADMIN_ID = int(ADMIN_ID_RAW)
    except ValueError:
        ADMIN_ID = None

PORT = int(os.getenv("PORT", "10000"))

# Render automatically provides this
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()

DB_FILE = "quizbot.sqlite3"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            correct INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    con.commit()
    con.close()


def save_question(question, options, correct):
    con = db()
    cur = con.cursor()

    cur.execute("""
        INSERT INTO questions
        (question, option_a, option_b, option_c, option_d, correct)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        question,
        options[0],
        options[1],
        options[2],
        options[3],
        correct
    ))

    con.commit()
    qid = cur.lastrowid
    con.close()

    return qid


def get_questions():
    con = db()
    cur = con.cursor()

    cur.execute("""
        SELECT id, question, option_a, option_b,
               option_c, option_d, correct
        FROM questions
        ORDER BY id
    """)

    rows = cur.fetchall()
    con.close()

    return rows


def delete_question(qid):
    con = db()
    cur = con.cursor()

    cur.execute(
        "DELETE FROM questions WHERE id = ?",
        (qid,)
    )

    con.commit()
    deleted = cur.rowcount
    con.close()

    return deleted


def delete_all_questions():
    con = db()
    cur = con.cursor()

    cur.execute("DELETE FROM questions")

    con.commit()
    con.close()


def set_setting(key, value):
    con = db()
    cur = con.cursor()

    cur.execute("""
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, (key, value))

    con.commit()
    con.close()


def get_setting(key):
    con = db()
    cur = con.cursor()

    cur.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    )

    row = cur.fetchone()
    con.close()

    return row[0] if row else None


# =========================================================
# ADMIN
# =========================================================

def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Add Question",
                callback_data="add"
            ),
            InlineKeyboardButton(
                "📚 Questions",
                callback_data="list"
            ),
        ],
        [
            InlineKeyboardButton(
                "🗑 Delete All",
                callback_data="delete_all"
            ),
            InlineKeyboardButton(
                "🎯 Start Quiz",
                callback_data="quiz"
            ),
        ],
        [
            InlineKeyboardButton(
                "📌 Set Chat",
                callback_data="setchat"
            ),
        ],
    ])


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    if ADMIN_ID is None:
        await update.message.reply_text(
            "⚠️ ADMIN_USER_ID अभी Render में सेट नहीं है.\n\n"
            f"आपका Telegram User ID है:\n"
            f"`{user.id}`\n\n"
            "इसे Render → Environment Variables में "
            "ADMIN_USER_ID के नाम से डालें।",
            parse_mode="Markdown"
        )
        return

    if is_admin(user.id):

        await update.message.reply_text(
            "🤖 *Quiz Bot Admin Panel*\n\n"
            "नीचे से काम चुनें:",
            parse_mode="Markdown",
            reply_markup=admin_keyboard()
        )

    else:

        await update.message.reply_text(
            "👋 Quiz Bot में आपका स्वागत है!\n\n"
            "Quiz शुरू करने के लिए /quiz भेजें।"
        )


# =========================================================
# ADMIN COMMAND
# =========================================================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ आपको Admin access नहीं है।")
        return

    await update.message.reply_text(
        "🤖 *Admin Panel*",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )


# =========================================================
# SET CHAT
# =========================================================

async def setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    chat = update.effective_chat

    set_setting("quiz_chat_id", str(chat.id))

    await update.message.reply_text(
        "✅ यह chat Quiz भेजने के लिए set कर दी गई है।\n\n"
        f"Chat ID: `{chat.id}`",
        parse_mode="Markdown"
    )


# =========================================================
# PARSER
# =========================================================

def clean_text(text):
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def parse_questions(text):

    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # Separate questions by Q:, Question:, or numbered questions
    blocks = re.split(
        r"(?im)(?=^\s*(?:Q(?:uestion)?\s*\d*[:.)-]|"
        r"\d+\s*[\).:-])\s*)",
        text
    )

    results = []

    for block in blocks:

        block = block.strip()

        if not block:
            continue

        lines = [
            x.strip()
            for x in block.split("\n")
            if x.strip()
        ]

        if len(lines) < 5:
            continue

        question = None
        options = [None, None, None, None]
        answer = None

        # -------------------------
        # QUESTION
        # -------------------------

        for line in lines:

            m = re.match(
                r"^\s*(?:Q(?:uestion)?\s*\d*\s*[:.)-]?)\s*(.+)$",
                line,
                re.I
            )

            if m:
                question = clean_text(m.group(1))
                break

        if question is None:

            m = re.match(
                r"^\s*\d+\s*[\).:-]\s*(.+)$",
                lines[0]
            )

            if m:
                question = clean_text(m.group(1))
            else:
                question = clean_text(lines[0])

        # -------------------------
        # OPTIONS
        # -------------------------

        for line in lines:

            m = re.match(
                r"^\s*[AА]\s*[\).:-]\s*(.+)$",
                line,
                re.I
            )

            if m:
                options[0] = clean_text(m.group(1))
                continue

            m = re.match(
                r"^\s*[BВ]\s*[\).:-]\s*(.+)$",
                line,
                re.I
            )

            if m:
                options[1] = clean_text(m.group(1))
                continue

            m = re.match(
                r"^\s*[CС]\s*[\).:-]\s*(.+)$",
                line,
                re.I
            )

            if m:
                options[2] = clean_text(m.group(1))
                continue

            m = re.match(
                r"^\s*[DД]\s*[\).:-]\s*(.+)$",
                line,
                re.I
            )

            if m:
                options[3] = clean_text(m.group(1))
                continue

        # -------------------------
        # ANSWER
        # -------------------------

        for line in lines:

            m = re.search(
                r"(?:answer|ans|correct|सही\s*उत्तर|उत्तर)"
                r"\s*[:=\-]?\s*([A-DА-Д1-4])",
                line,
                re.I
            )

            if m:
                answer = m.group(1).upper()
                break

        # -------------------------
        # NORMALIZE ANSWER
        # -------------------------

        if answer:

            answer_map = {
                "A": 0,
                "А": 0,
                "B": 1,
                "В": 1,
                "C": 2,
                "С": 2,
                "D": 3,
                "Д": 3,
                "1": 0,
                "2": 1,
                "3": 2,
                "4": 3,
            }

            correct = answer_map.get(answer)

        else:
            correct = None

        # -------------------------
        # VALIDATE
        # -------------------------

        if (
            question
            and all(options)
            and correct is not None
            and 0 <= correct <= 3
        ):

            results.append({
                "question": question,
                "options": options,
                "correct": correct
            })

    return results


# =========================================================
# ADD QUESTION MESSAGE
# =========================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    if not is_admin(user.id):
        return

    text = update.message.text or ""

    mode = context.user_data.get("mode")

    if mode != "add":
        return

    questions = parse_questions(text)

    if not questions:

        await update.message.reply_text(
            "❌ Question समझ नहीं आया।\n\n"
            "इस format में भेजें:\n\n"
            "Q: भारत की राजधानी क्या है?\n"
            "A) मुंबई\n"
            "B) दिल्ली\n"
            "C) कोलकाता\n"
            "D) चेन्नई\n"
            "Answer: B"
        )
        return

    saved = 0

    for q in questions:

        save_question(
            q["question"],
            q["options"],
            q["correct"]
        )

        saved += 1

    context.user_data["mode"] = None

    await update.message.reply_text(
        f"✅ {saved} Question save हो गए।\n\n"
        "अब /quiz से Quiz शुरू कर सकते हैं।",
        reply_markup=admin_keyboard()
    )


# =========================================================
# CALLBACKS
# =========================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    if not is_admin(user.id):
        await query.message.reply_text(
            "❌ Admin access required."
        )
        return

    data = query.data

    # -------------------------
    # ADD
    # -------------------------

    if data == "add":

        context.user_data["mode"] = "add"

        await query.message.reply_text(
            "➕ *Add Question*\n\n"
            "एक या कई questions paste करें।\n\n"
            "Example:\n\n"
            "Q: भारत की राजधानी क्या है?\n"
            "A) मुंबई\n"
            "B) दिल्ली\n"
            "C) चेन्नई\n"
            "D) जयपुर\n"
            "Answer: B\n\n"
            "कई questions भी एक साथ भेज सकते हैं।",
            parse_mode="Markdown"
        )

    # -------------------------
    # LIST
    # -------------------------

    elif data == "list":

        rows = get_questions()

        if not rows:

            await query.message.reply_text(
                "📚 अभी कोई question नहीं है।"
            )
            return

        text = "📚 *Saved Questions*\n\n"

        for row in rows[:30]:

            qid = row[0]
            question = row[1]

            text += f"*{qid}.* {question[:100]}\n"

        if len(rows) > 30:
            text += f"\n...और {len(rows)-30} questions"

        await query.message.reply_text(
            text,
            parse_mode="Markdown"
        )

    # -------------------------
    # DELETE ALL
    # -------------------------

    elif data == "delete_all":

        await query.message.reply_text(
            "⚠️ सभी questions delete करने के लिए:\n\n"
            "/deleteall"
        )

    # -------------------------
    # QUIZ
    # -------------------------

    elif data == "quiz":

        await send_quiz(update, context)

    # -------------------------
    # SET CHAT
    # -------------------------

    elif data == "setchat":

        chat = query.message.chat

        set_setting(
            "quiz_chat_id",
            str(chat.id)
        )

        await query.message.reply_text(
            "✅ यह chat Quiz destination के रूप में set हो गई।"
        )


# =========================================================
# DELETE ALL COMMAND
# =========================================================

async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    delete_all_questions()

    await update.message.reply_text(
        "🗑 सभी questions delete कर दिए गए।"
    )


# =========================================================
# QUIZ
# =========================================================

async def send_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if user and not is_admin(user.id):

        await update.message.reply_text(
            "❌ Admin access required."
        )
        return

    rows = get_questions()

    if not rows:

        if update.callback_query:
            await update.callback_query.message.reply_text(
                "❌ पहले questions add करें।"
            )
        else:
            await update.message.reply_text(
                "❌ पहले questions add करें।"
            )

        return

    chat_id = get_setting("quiz_chat_id")

    if not chat_id:

        if update.callback_query:
            await update.callback_query.message.reply_text(
                "❌ पहले उस chat में /setchat भेजें "
                "जहाँ Quiz भेजना है।"
            )
        else:
            await update.message.reply_text(
                "❌ पहले उस chat में /setchat भेजें "
                "जहाँ Quiz भेजना है।"
            )

        return

    chat_id = int(chat_id)

    sent = 0

    for row in rows:

        qid = row[0]
        question = row[1]

        options = [
            row[2],
            row[3],
            row[4],
            row[5]
        ]

        correct = row[6]

        try:

            await context.bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=options,
                type="quiz",
                correct_option_id=correct,
                is_anonymous=True
            )

            sent += 1

        except Exception as e:

            logger.exception(
                "Quiz send failed for question %s",
                qid
            )

    if update.callback_query:

        await update.callback_query.message.reply_text(
            f"🎯 {sent} Quiz भेजे गए।"
        )

    else:

        await update.message.reply_text(
            f"🎯 {sent} Quiz भेजे गए।"
        )


# =========================================================
# HELP
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 *Quiz Bot Commands*\n\n"
        "/start - Start\n"
        "/admin - Admin Panel\n"
        "/setchat - Quiz destination set करें\n"
        "/quiz - Quiz भेजें\n"
        "/deleteall - सभी questions हटाएँ",
        parse_mode="Markdown"
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):

    logger.exception(
        "Telegram error: %s",
        context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    init_db()

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable missing."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("admin", admin)
    )

    app.add_handler(
        CommandHandler("setchat", setchat)
    )

    app.add_handler(
        CommandHandler("quiz", send_quiz)
    )

    app.add_handler(
        CommandHandler("deleteall", deleteall)
    )

    app.add_handler(
        CommandHandler("help", help_command)
    )

    # Buttons
    app.add_handler(
        CallbackQueryHandler(callbacks)
    )

    # Admin text input
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    app.add_error_handler(error_handler)

    # =====================================================
    # RENDER WEBHOOK
    # =====================================================

    if RENDER_URL:

        webhook_path = "telegram-webhook"

        webhook_url = (
            RENDER_URL.rstrip("/")
            + "/"
            + webhook_path
        )

        logger.info(
            "Starting webhook: %s",
            webhook_url
        )

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )

    else:

        # Local fallback
        logger.info("RENDER_EXTERNAL_URL not found.")
        logger.info("Starting polling mode.")

        app.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()
