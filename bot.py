import os
import re
import sqlite3
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DB_FILE = "quizbot.sqlite3"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)


# ================= DATABASE =================

def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        con.execute("""
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


def get_setting(key):
    with db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with db() as con:
        con.execute("""
            INSERT INTO settings(key,value)
            VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def is_admin(user_id):
    return get_setting("admin_id") == str(user_id)


def add_question(question, options, correct):
    with db() as con:
        con.execute("""
            INSERT INTO questions
            (question,option_a,option_b,option_c,option_d,correct)
            VALUES (?,?,?,?,?,?)
        """, (
            question,
            options[0],
            options[1],
            options[2],
            options[3],
            correct
        ))


def get_questions():
    with db() as con:
        return con.execute(
            "SELECT * FROM questions ORDER BY id"
        ).fetchall()


def delete_all_questions():
    with db() as con:
        con.execute("DELETE FROM questions")


# ================= ADMIN UI =================

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
            )
        ],
        [
            InlineKeyboardButton(
                "🎯 Send Quiz",
                callback_data="quiz"
            ),
            InlineKeyboardButton(
                "🗑 Delete All",
                callback_data="delete"
            )
        ]
    ])


# ================= PARSER =================

def parse_questions(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    starts = list(re.finditer(
        r"(?im)^\s*(?:"
        r"Q(?:uestion)?\s*\d*\s*[:.)-]"
        r"|\d+\s*[).:-]"
        r")\s*",
        text
    ))

    if starts:
        blocks = []
        for i, match in enumerate(starts):
            end = (
                starts[i + 1].start()
                if i + 1 < len(starts)
                else len(text)
            )
            blocks.append(text[match.start():end])
    else:
        blocks = [text]

    answer_map = {
        "A": 0, "А": 0, "1": 0,
        "B": 1, "В": 1, "2": 1,
        "C": 2, "С": 2, "3": 2,
        "D": 3, "Д": 3, "4": 3
    }

    results = []

    for block in blocks:
        lines = [
            line.strip()
            for line in block.split("\n")
            if line.strip()
        ]

        if len(lines) < 5:
            continue

        question = re.sub(
            r"^\s*(?:"
            r"Q(?:uestion)?\s*\d*\s*[:.)-]"
            r"|\d+\s*[).:-]"
            r")\s*",
            "",
            lines[0],
            flags=re.I
        ).strip()

        options = [None, None, None, None]
        correct = None

        patterns = [
            r"^\s*[AaАа]\s*[).:-]\s*(.+)$",
            r"^\s*[BbВв]\s*[).:-]\s*(.+)$",
            r"^\s*[CcСс]\s*[).:-]\s*(.+)$",
            r"^\s*[DdДд]\s*[).:-]\s*(.+)$"
        ]

        for line in lines[1:]:
            for index, pattern in enumerate(patterns):
                match = re.match(pattern, line)
                if match:
                    options[index] = match.group(1).strip()
                    break

            match = re.search(
                r"(?:answer|ans|correct|correct\s*answer|"
                r"सही\s*उत्तर|उत्तर)"
                r"\s*[:=\-]?\s*([A-DА-Д1-4])\b",
                line,
                flags=re.I
            )

            if match:
                correct = answer_map.get(
                    match.group(1).upper()
                )

        if question and all(options) and correct is not None:
            results.append(
                (question, options, correct)
            )

    return results


# ================= COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # First user becomes admin.
    if get_setting("admin_id") is None:
        set_setting("admin_id", user.id)

        await update.message.reply_text(
            "👑 Setup complete!\n\n"
            "यह Telegram account अब Bot Admin है।\n\n"
            "नीचे से काम शुरू करें:",
            reply_markup=admin_keyboard()
        )
        return

    if is_admin(user.id):
        await update.message.reply_text(
            "🤖 Quiz Bot Admin Panel",
            reply_markup=admin_keyboard()
        )
    else:
        await update.message.reply_text(
            "👋 Quiz Bot में स्वागत है।\n\n"
            "Quiz खेलने के लिए /quiz भेजें।"
        )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin access नहीं है।"
        )
        return

    await update.message.reply_text(
        "🤖 Quiz Bot Admin Panel",
        reply_markup=admin_keyboard()
    )


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_quiz(
        update.effective_chat.id,
        context
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin access नहीं है।"
        )
        return

    context.user_data["mode"] = "add"

    await update.message.reply_text(
        "➕ Add Question\n\n"
        "एक या कई questions paste करें।\n\n"
        "Example:\n\n"
        "Q: भारत की राजधानी क्या है?\n"
        "A) मुंबई\n"
        "B) दिल्ली\n"
        "C) चेन्नई\n"
        "D) जयपुर\n"
        "Answer: B"
    )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    rows = get_questions()

    if not rows:
        await update.message.reply_text(
            "📚 अभी कोई Question नहीं है।"
        )
        return

    text = "📚 Saved Questions\n\n"

    for row in rows[:50]:
        text += f"{row['id']}. {row['question'][:100]}\n"

    if len(rows) > 50:
        text += f"\nऔर {len(rows) - 50} questions हैं।"

    await update.message.reply_text(text)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    delete_all_questions()

    await update.message.reply_text(
        "🗑 सभी Questions delete हो गए।",
        reply_markup=admin_keyboard()
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if context.user_data.get("mode") != "add":
        return

    parsed = parse_questions(
        update.message.text or ""
    )

    if not parsed:
        await update.message.reply_text(
            "❌ Format समझ नहीं आया।\n\n"
            "Q: सवाल?\n"
            "A) विकल्प A\n"
            "B) विकल्प B\n"
            "C) विकल्प C\n"
            "D) विकल्प D\n"
            "Answer: B"
        )
        return

    for question, options, correct in parsed:
        add_question(
            question,
            options,
            correct
        )

    context.user_data["mode"] = None

    await update.message.reply_text(
        f"✅ {len(parsed)} Question save हो गए।",
        reply_markup=admin_keyboard()
    )


# ================= BUTTONS =================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text(
            "❌ Admin access नहीं है।"
        )
        return

    data = query.data

    if data == "add":
        context.user_data["mode"] = "add"

        await query.message.reply_text(
            "➕ Add Question\n\n"
            "एक या कई questions paste करें।\n\n"
            "Q: भारत की राजधानी क्या है?\n"
            "A) मुंबई\n"
            "B) दिल्ली\n"
            "C) चेन्नई\n"
            "D) जयपुर\n"
            "Answer: B"
        )

    elif data == "list":
        rows = get_questions()

        if not rows:
            await query.message.reply_text(
                "📚 अभी कोई Question नहीं है।"
            )
            return

        text = "📚 Saved Questions\n\n"

        for row in rows[:50]:
            text += f"{row['id']}. {row['question'][:100]}\n"

        await query.message.reply_text(text)

    elif data == "delete":
        delete_all_questions()

        await query.message.reply_text(
            "🗑 सभी Questions delete हो गए।",
            reply_markup=admin_keyboard()
        )

    elif data == "quiz":
        await send_quiz(
            query.message.chat_id,
            context
        )


# ================= SEND QUIZ =================

async def send_quiz(chat_id, context):
    rows = get_questions()

    if not rows:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ पहले Add Question से questions डालें।"
        )
        return

    sent = 0

    for row in rows:
        try:
            await context.bot.send_poll(
                chat_id=chat_id,
                question=row["question"],
                options=[
                    row["option_a"],
                    row["option_b"],
                    row["option_c"],
                    row["option_d"]
                ],
                type="quiz",
                correct_option_id=row["correct"],
                is_anonymous=True
            )
            sent += 1

        except Exception:
            log.exception(
                "Failed to send question %s",
                row["id"]
            )

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🎯 {sent} Quiz question भेजे गए।"
    )


# ================= RENDER HEALTH SERVER =================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()
        self.wfile.write(
            b"Quiz Bot is running"
        )

    def log_message(self, format, *args):
        return


def health_server():
    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )
    log.info(
        "Health server listening on port %s",
        PORT
    )
    server.serve_forever()


# ================= MAIN =================

def main():

    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing."
        )

    init_db()

    # Render health check
    Thread(
        target=health_server,
        daemon=True
    ).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("admin", admin)
    )

    app.add_handler(
        CommandHandler("quiz", quiz_command)
    )

    app.add_handler(
        CommandHandler("add", add_command)
    )

    app.add_handler(
        CommandHandler("list", list_command)
    )

    app.add_handler(
        CommandHandler("deleteall", delete_command)
    )

    app.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    log.info("Starting Telegram polling...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
