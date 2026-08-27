import os
import sqlite3
import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# Telegram Quiz Bot - clean replacement
# Works with Render/Webhook + SQLite
# Environment variables:
# BOT_TOKEN      = Telegram BotFather token
# ADMIN_USER_ID  = your Telegram numeric user ID
# PUBLIC_URL     = Render public URL, e.g. https://my-quiz-bot.onrender.com
# PORT           = provided by Render (default 10000)
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

DB = "quizbot.sqlite3"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("quizbot")


# ------------------------- Database -------------------------

def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                option1 TEXT NOT NULL,
                option2 TEXT NOT NULL,
                option3 TEXT NOT NULL,
                option4 TEXT NOT NULL,
                answer INTEGER NOT NULL CHECK(answer BETWEEN 1 AND 4),
                explanation TEXT DEFAULT '',
                FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
            )
        """)


def create_quiz(title: str) -> int:
    with db() as con:
        cur = con.execute("INSERT INTO quizzes(title) VALUES (?)", (title,))
        return cur.lastrowid


def add_question(
    quiz_id: int,
    question: str,
    options: list[str],
    answer: int,
    explanation: str,
):
    with db() as con:
        con.execute(
            """
            INSERT INTO questions
            (quiz_id, question, option1, option2, option3, option4, answer, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quiz_id,
                question,
                options[0],
                options[1],
                options[2],
                options[3],
                answer,
                explanation,
            ),
        )


def get_quizzes():
    with db() as con:
        return con.execute(
            "SELECT * FROM quizzes ORDER BY id DESC"
        ).fetchall()


def get_quiz(quiz_id: int):
    with db() as con:
        return con.execute(
            "SELECT * FROM quizzes WHERE id=?", (quiz_id,)
        ).fetchone()


def get_questions(quiz_id: int):
    with db() as con:
        return con.execute(
            "SELECT * FROM questions WHERE quiz_id=? ORDER BY id",
            (quiz_id,),
        ).fetchall()


def delete_quiz(quiz_id: int) -> bool:
    with db() as con:
        cur = con.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
        return cur.rowcount > 0


# ------------------------- Helpers --------------------------

def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


async def deny(update: Update):
    if update.message:
        await update.message.reply_text("⛔ यह कमांड सिर्फ admin इस्तेमाल कर सकता है।")
    elif update.callback_query:
        await update.callback_query.answer(
            "⛔ सिर्फ admin के लिए।", show_alert=True
        )


def reset_state(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "creating_quiz_id",
        "creating_title",
        "new_question",
        "new_options",
        "new_answer",
    ):
        context.user_data.pop(key, None)


# ------------------------- User commands --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎯 *Quiz Bot में आपका स्वागत है!*\n\n"
        "Quiz खेलने के लिए नीचे बटन दबाएँ।"
    )
    keyboard = [[InlineKeyboardButton("📚 Quizzes देखें", callback_data="list_quizzes")]]
    if is_admin(update):
        keyboard.append([
            InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")
        ])
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def quizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_quizzes(update, context)


async def show_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_quizzes()
    if not rows:
        text = "अभी कोई quiz उपलब्ध नहीं है।"
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    keyboard = [
        [InlineKeyboardButton(f"📝 {r['title']}", callback_data=f"play:{r['id']}")]
        for r in rows
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "📚 *Quiz चुनें:*",
            parse_mode="Markdown",
            reply_markup=markup,
        )
    else:
        await update.message.reply_text(
            "📚 *Quiz चुनें:*",
            parse_mode="Markdown",
            reply_markup=markup,
        )


# ------------------------- Admin panel ----------------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    await send_admin_panel(update)


async def send_admin_panel(update: Update):
    keyboard = [
        [InlineKeyboardButton("➕ नया Quiz", callback_data="admin_new")],
        [InlineKeyboardButton("🗑 Quiz Delete", callback_data="admin_delete")],
        [InlineKeyboardButton("📋 Quiz List", callback_data="admin_list")],
    ]
    text = "⚙️ *Admin Panel*\n\nयहाँ से quiz बनाएं या delete करें।"

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def newquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)

    reset_state(context)
    context.user_data["creating_quiz"] = True
    await update.message.reply_text(
        "➕ *नया Quiz*\n\n"
        "पहले quiz का नाम भेजें।\n\n"
        "उदाहरण: `इंसानी फितरत Quiz`",
        parse_mode="Markdown",
    )


async def deletequiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    await show_delete_quizzes(update)


async def show_delete_quizzes(update: Update):
    rows = get_quizzes()
    if not rows:
        text = "Delete करने के लिए कोई quiz नहीं है।"
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"🗑 {r['title']}", callback_data=f"delete:{r['id']}"
            )
        ]
        for r in rows
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "🗑 *Delete करने वाला Quiz चुनें:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await update.message.reply_text(
            "🗑 *Delete करने वाला Quiz चुनें:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def list_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)
    rows = get_quizzes()
    if not rows:
        await update.message.reply_text("अभी कोई quiz नहीं है।")
        return

    parts = ["📋 *Quiz List*\n"]
    for r in rows:
        count = len(get_questions(r["id"]))
        parts.append(f"• `{r['id']}` — {r['title']} ({count} questions)")
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


# --------------------- Quiz creation flow -------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_admin(update):
        return

    # Creating a quiz: title
    if context.user_data.get("creating_quiz"):
        title = update.message.text.strip()
        if not title:
            await update.message.reply_text("Quiz का नाम खाली नहीं हो सकता। फिर से भेजें।")
            return

        quiz_id = create_quiz(title)
        context.user_data["creating_quiz_id"] = quiz_id
        context.user_data["creating_quiz"] = False
        context.user_data["adding_question"] = True

        await update.message.reply_text(
            f"✅ Quiz बनाया गया: *{title}*\n\n"
            "अब पहला question भेजें।\n"
            "जब सारे questions हो जाएँ तो /done लिखें।",
            parse_mode="Markdown",
        )
        return

    # Adding question: question text
    if context.user_data.get("adding_question") and "new_question" not in context.user_data:
        q = update.message.text.strip()
        if not q:
            await update.message.reply_text("Question खाली नहीं हो सकता।")
            return

        context.user_data["new_question"] = q
        await update.message.reply_text(
            "अब 4 options *एक ही लाइन में* भेजें।\n\n"
            "Format:\n"
            "`Option 1 | Option 2 | Option 3 | Option 4`",
            parse_mode="Markdown",
        )
        return

    # Adding question: options
    if context.user_data.get("adding_question") and "new_options" not in context.user_data:
        raw = update.message.text.strip()
        options = [x.strip() for x in raw.split("|")]

        if len(options) != 4 or any(not x for x in options):
            await update.message.reply_text(
                "❌ ठीक 4 options चाहिए।\n\n"
                "उदाहरण:\n"
                "`भारत | नेपाल | चीन | जापान`",
                parse_mode="Markdown",
            )
            return

        context.user_data["new_options"] = options
        await update.message.reply_text(
            "सही answer का नंबर भेजें:\n\n"
            "1️⃣ Option 1\n"
            "2️⃣ Option 2\n"
            "3️⃣ Option 3\n"
            "4️⃣ Option 4\n\n"
            "उदाहरण: `2`",
            parse_mode="Markdown",
        )
        return

    # Adding question: answer
    if context.user_data.get("adding_question") and "new_answer" not in context.user_data:
        raw = update.message.text.strip()
        if raw not in ("1", "2", "3", "4"):
            await update.message.reply_text("❌ सिर्फ 1, 2, 3 या 4 भेजें।")
            return

        context.user_data["new_answer"] = int(raw)
        await update.message.reply_text(
            "अब explanation भेजें।\n\n"
            "अगर explanation नहीं चाहिए तो `skip` लिखें।",
            parse_mode="Markdown",
        )
        return

    # Adding question: explanation
    if context.user_data.get("adding_question") and "new_answer" in context.user_data:
        explanation = update.message.text.strip()
        if explanation.lower() == "skip":
            explanation = ""

        quiz_id = context.user_data["creating_quiz_id"]
        add_question(
            quiz_id,
            context.user_data["new_question"],
            context.user_data["new_options"],
            context.user_data["new_answer"],
            explanation,
        )

        # Keep adding questions
        context.user_data.pop("new_question", None)
        context.user_data.pop("new_options", None)
        context.user_data.pop("new_answer", None)

        total = len(get_questions(quiz_id))
        await update.message.reply_text(
            f"✅ Question #{total} save हो गया।\n\n"
            "अगला question भेजें।\n"
            "या quiz पूरा करने के लिए /done लिखें।"
        )
        return


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny(update)

    quiz_id = context.user_data.get("creating_quiz_id")
    if not quiz_id:
        await update.message.reply_text("अभी कोई quiz बनाने की प्रक्रिया नहीं चल रही।")
        return

    quiz = get_quiz(quiz_id)
    count = len(get_questions(quiz_id))

    if count == 0:
        delete_quiz(quiz_id)
        reset_state(context)
        await update.message.reply_text(
            "Quiz में कोई question नहीं था, इसलिए quiz हटा दिया गया।"
        )
        return

    reset_state(context)
    await update.message.reply_text(
        f"🎉 *Quiz तैयार है!*\n\n"
        f"📚 {quiz['title']}\n"
        f"📝 Questions: {count}\n\n"
        "Users `/quiz` लिखकर इसे खेल सकते हैं।",
        parse_mode="Markdown",
    )


# ------------------------- Playing ---------------------------

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int):
    quiz = get_quiz(quiz_id)
    questions = get_questions(quiz_id)

    if not quiz or not questions:
        await update.callback_query.answer("यह quiz उपलब्ध नहीं है।", show_alert=True)
        return

    context.user_data["playing_quiz_id"] = quiz_id
    context.user_data["question_index"] = 0
    context.user_data["score"] = 0
    context.user_data["total"] = len(questions)

    await update.callback_query.answer()
    await send_question(update, context)


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz_id = context.user_data.get("playing_quiz_id")
    index = context.user_data.get("question_index", 0)
    questions = get_questions(quiz_id)

    if index >= len(questions):
        return await finish_quiz(update, context)

    q = questions[index]
    keyboard = [
        [InlineKeyboardButton(f"1️⃣ {q['option1']}", callback_data=f"ans:{q['id']}:1")],
        [InlineKeyboardButton(f"2️⃣ {q['option2']}", callback_data=f"ans:{q['id']}:2")],
        [InlineKeyboardButton(f"3️⃣ {q['option3']}", callback_data=f"ans:{q['id']}:3")],
        [InlineKeyboardButton(f"4️⃣ {q['option4']}", callback_data=f"ans:{q['id']}:4")],
    ]

    text = (
        f"❓ *Question {index + 1}/{len(questions)}*\n\n"
        f"{q['question']}"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        return

    qid = int(parts[1])
    chosen = int(parts[2])

    with db() as con:
        q = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()

    if not q:
        await query.answer("Question नहीं मिला।", show_alert=True)
        return

    # Prevent accidental answers after quiz state changed.
    quiz_id = context.user_data.get("playing_quiz_id")
    if not quiz_id:
        await query.edit_message_text("यह quiz session खत्म हो चुका है। `/quiz` से फिर शुरू करें।")
        return

    correct = chosen == q["answer"]
    if correct:
        context.user_data["score"] = context.user_data.get("score", 0) + 1

    result = "✅ *सही जवाब!*" if correct else f"❌ *गलत जवाब!*\nसही उत्तर: {q['answer']}"

    if q["explanation"]:
        result += f"\n\n💡 {q['explanation']}"

    await query.edit_message_text(result, parse_mode="Markdown")

    context.user_data["question_index"] = context.user_data.get("question_index", 0) + 1

    # Small pause is unnecessary; next button keeps flow reliable.
    keyboard = [[InlineKeyboardButton("➡️ अगला सवाल", callback_data="next_question")]]
    await query.message.reply_text(
        "अगले सवाल के लिए:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def next_question_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await send_question(update, context)


async def finish_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    score = context.user_data.get("score", 0)
    total = context.user_data.get("total", 0)
    quiz_id = context.user_data.get("playing_quiz_id")
    quiz = get_quiz(quiz_id) if quiz_id else None

    percent = round((score / total) * 100) if total else 0

    if percent >= 80:
        emoji = "🏆"
    elif percent >= 50:
        emoji = "👍"
    else:
        emoji = "💪"

    text = (
        f"{emoji} *Quiz पूरा हुआ!*\n\n"
        f"📚 {quiz['title'] if quiz else 'Quiz'}\n"
        f"🎯 Score: *{score}/{total}*\n"
        f"📊 Percentage: *{percent}%*\n\n"
        "फिर से खेलने के लिए `/quiz` लिखें।"
    )

    keyboard = [[InlineKeyboardButton("📚 दूसरे Quiz", callback_data="list_quizzes")]]

    await update.effective_chat.send_message(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    context.user_data.pop("playing_quiz_id", None)
    context.user_data.pop("question_index", None)
    context.user_data.pop("score", None)
    context.user_data.pop("total", None)


# ----------------------- Callbacks ---------------------------

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if data == "list_quizzes":
        await update.callback_query.answer()
        await show_quizzes(update, context)
        return

    if data == "admin_panel":
        if not is_admin(update):
            return await deny(update)
        await update.callback_query.answer()
        await send_admin_panel(update)
        return

    if data == "admin_new":
        if not is_admin(update):
            return await deny(update)
        await update.callback_query.answer()
        reset_state(context)
        context.user_data["creating_quiz"] = True
        await update.callback_query.edit_message_text(
            "➕ *नया Quiz*\n\nQuiz का नाम भेजें:",
            parse_mode="Markdown",
        )
        return

    if data == "admin_delete":
        if not is_admin(update):
            return await deny(update)
        await update.callback_query.answer()
        await show_delete_quizzes(update)
        return

    if data == "admin_list":
        if not is_admin(update):
            return await deny(update)
        await update.callback_query.answer()
        rows = get_quizzes()
        if not rows:
            await update.callback_query.edit_message_text("अभी कोई quiz नहीं है।")
            return
        text = "📋 *Quiz List*\n\n"
        for r in rows:
            text += f"• `{r['id']}` — {r['title']} ({len(get_questions(r['id']))} questions)\n"
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
        return

    if data.startswith("play:"):
        quiz_id = int(data.split(":")[1])
        await start_quiz(update, context, quiz_id)
        return

    if data.startswith("ans:"):
        await answer_callback(update, context)
        return

    if data == "next_question":
        await next_question_callback(update, context)
        return

    if data.startswith("delete:"):
        if not is_admin(update):
            return await deny(update)

        quiz_id = int(data.split(":")[1])
        quiz = get_quiz(quiz_id)
        if not quiz:
            await update.callback_query.answer("Quiz नहीं मिला।", show_alert=True)
            return

        delete_quiz(quiz_id)
        await update.callback_query.answer("Quiz delete हो गया।")
        await show_delete_quizzes(update)
        return


# -------------------------- Main -----------------------------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing.")
    if not ADMIN_ID:
        raise RuntimeError("ADMIN_USER_ID environment variable missing.")
    if not PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL environment variable missing.")

    init_db()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("quiz", quizzes_command))
    application.add_handler(CommandHandler("quizzes", quizzes_command))

    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("newquiz", newquiz_command))
    application.add_handler(CommandHandler("deletequiz", deletequiz_command))
    application.add_handler(CommandHandler("listquiz", list_admin_command))
    application.add_handler(CommandHandler("done", done_command))

    application.add_handler(CallbackQueryHandler(callback_router))

    # Text messages are used only by admin while creating a quiz.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    log.info("Starting Quiz Bot on port %s", PORT)
    log.info("Webhook URL: %s/telegram", PUBLIC_URL)

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
