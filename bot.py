import os
import re
import sqlite3
import logging
import asyncio
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# SIMPLE QUIZ BOT - NEW VERSION
# No PostgreSQL / psycopg. SQLite only.
# ============================================================

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
DB_FILE = os.getenv("QUIZ_DB", "quiz.db")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("quizbot")

# -------------------- DATABASE --------------------

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")

db.executescript("""
CREATE TABLE IF NOT EXISTS quizzes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    exam TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    category TEXT DEFAULT '',
    subcategory TEXT DEFAULT '',
    topic TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    question TEXT NOT NULL,
    option_a TEXT NOT NULL,
    option_b TEXT NOT NULL,
    option_c TEXT NOT NULL,
    option_d TEXT NOT NULL,
    answer TEXT NOT NULL,
    explanation TEXT DEFAULT '',
    FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    quiz_id INTEGER NOT NULL,
    score INTEGER NOT NULL,
    total INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wrong_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    quiz_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    timer_enabled INTEGER DEFAULT 0,
    timer_seconds INTEGER DEFAULT 30,
    random_enabled INTEGER DEFAULT 0
);
""")
db.commit()

# -------------------- HELPERS --------------------

def now_text():
    return datetime.now(timezone.utc).isoformat()

def get_settings(user_id):
    row = db.execute(
        "SELECT * FROM user_settings WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if row:
        return dict(row)
    db.execute(
        "INSERT INTO user_settings(user_id) VALUES(?)",
        (user_id,),
    )
    db.commit()
    return {
        "user_id": user_id,
        "timer_enabled": 0,
        "timer_seconds": 30,
        "random_enabled": 0,
    }

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Quiz", callback_data="menu_quiz"),
            InlineKeyboardButton("➕ Add Quiz", callback_data="menu_add"),
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="menu_stats"),
            InlineKeyboardButton("📚 Categories", callback_data="menu_categories"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"),
            InlineKeyboardButton("🔄 ReAttempt", callback_data="menu_reattempt"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="menu_help"),
        ],
    ])

def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_main")]
    ])

def categories_keyboard():
    rows = []
    cats = db.execute("""
        SELECT category, COUNT(*) AS n
        FROM quizzes
        WHERE TRIM(category) <> ''
        GROUP BY category
        ORDER BY category
    """).fetchall()
    for r in cats:
        rows.append([
            InlineKeyboardButton(
                f"📚 {r['category']} ({r['n']})",
                callback_data=f"cat:{r['category'][:45]}",
            )
        ])
    rows.append([InlineKeyboardButton("📚 सभी Topics", callback_data="all_topics")])
    rows.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def quiz_list_keyboard(category=None):
    if category:
        rows_data = db.execute("""
            SELECT id, title, topic, category
            FROM quizzes
            WHERE category=?
            ORDER BY id DESC
        """, (category,)).fetchall()
    else:
        rows_data = db.execute("""
            SELECT id, title, topic, category
            FROM quizzes
            ORDER BY id DESC
        """).fetchall()

    rows = []
    for r in rows_data:
        label = r["title"]
        if r["topic"]:
            label += f" • {r['topic']}"
        rows.append([
            InlineKeyboardButton(
                f"▶️ {label[:55]}",
                callback_data=f"play:{r['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Categories", callback_data="menu_categories")])
    return InlineKeyboardMarkup(rows)

def format_quiz_text(quiz):
    return (
        f"📘 <b>{quiz['title']}</b>\n"
        f"🎯 Exam: {quiz['exam'] or '-'}\n"
        f"📚 Subject: {quiz['subject'] or '-'}\n"
        f"🏷 Category: {quiz['category'] or '-'}\n"
        f"📌 Topic: {quiz['topic'] or '-'}"
    )

def parse_quiz(text):
    """
    FIXED IMPORT FORMAT:

    QUIZ: Title
    EXAM: RO/ARO
    SUBJECT: सामान्य ज्ञान
    CATEGORY: RO/ARO
    SUBCATEGORY: सामान्य ज्ञान
    TOPIC: भारत

    Q1: सवाल?
    A) विकल्प
    B) विकल्प
    C) विकल्प
    D) विकल्प
    ANSWER: B
    EXPLANATION: कारण

    Q2: ...
    """

    lines = [x.strip() for x in text.replace("\r", "").split("\n")]
    lines = [x for x in lines if x != ""]

    header = {
        "title": "",
        "exam": "",
        "subject": "",
        "category": "",
        "subcategory": "",
        "topic": "",
    }

    questions = []
    current = None
    state = None

    header_map = {
        "QUIZ": "title",
        "EXAM": "exam",
        "SUBJECT": "subject",
        "CATEGORY": "category",
        "SUBCATEGORY": "subcategory",
        "TOPIC": "topic",
    }

    q_pattern = re.compile(r"^Q\s*(\d+)\s*[:.)-]\s*(.+)$", re.I)
    option_pattern = re.compile(r"^([ABCD])\s*[\):.-]\s*(.+)$", re.I)
    answer_pattern = re.compile(r"^(?:ANSWER|CORRECT\s+ANSWER)\s*:\s*([ABCD])\s*$", re.I)
    exp_pattern = re.compile(r"^(?:EXPLANATION|WHY)\s*:\s*(.*)$", re.I)

    for line in lines:
        matched_header = False
        for key, dest in header_map.items():
            m = re.match(rf"^{re.escape(key)}\s*:\s*(.*)$", line, re.I)
            if m:
                header[dest] = m.group(1).strip()
                matched_header = True
                state = None
                break
        if matched_header:
            continue

        qm = q_pattern.match(line)
        if qm:
            if current:
                questions.append(current)
            current = {
                "number": int(qm.group(1)),
                "question": qm.group(2).strip(),
                "A": "",
                "B": "",
                "C": "",
                "D": "",
                "answer": "",
                "explanation": "",
            }
            state = "question"
            continue

        if not current:
            continue

        om = option_pattern.match(line)
        if om:
            letter = om.group(1).upper()
            current[letter] = om.group(2).strip()
            state = letter
            continue

        am = answer_pattern.match(line)
        if am:
            current["answer"] = am.group(1).upper()
            state = "answer"
            continue

        em = exp_pattern.match(line)
        if em:
            current["explanation"] = em.group(1).strip()
            state = "explanation"
            continue

        # Do not silently merge arbitrary lines into options.
        # Only allow continuation of explanation.
        if state == "explanation":
            current["explanation"] += " " + line

    if current:
        questions.append(current)

    if not header["title"]:
        raise ValueError("QUIZ: Title missing है।")
    if not questions:
        raise ValueError("कोई Q1/Q2... सवाल नहीं मिला।")

    errors = []
    expected_number = 1

    for q in questions:
        if q["number"] != expected_number:
            errors.append(
                f"सवाल क्रम गलत है: Q{expected_number} के बाद Q{q['number']} मिला।"
            )
            expected_number = q["number"] + 1
        else:
            expected_number += 1

        if not q["question"]:
            errors.append(f"Q{q['number']}: सवाल खाली है।")

        for letter in "ABCD":
            if not q[letter]:
                errors.append(f"Q{q['number']}: {letter}) option missing है।")

        if q["answer"] not in "ABCD":
            errors.append(
                f"Q{q['number']}: ANSWER केवल A/B/C/D होना चाहिए।"
            )

    if errors:
        raise ValueError("\n".join(errors[:15]))

    return header, questions

def save_quiz(header, questions):
    cur = db.execute("""
        INSERT INTO quizzes
        (title, exam, subject, category, subcategory, topic, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        header["title"],
        header["exam"],
        header["subject"],
        header["category"],
        header["subcategory"],
        header["topic"],
        now_text(),
    ))
    quiz_id = cur.lastrowid

    for q in questions:
        db.execute("""
            INSERT INTO questions
            (quiz_id, number, question, option_a, option_b, option_c, option_d,
             answer, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            quiz_id,
            q["number"],
            q["question"],
            q["A"],
            q["B"],
            q["C"],
            q["D"],
            q["answer"],
            q["explanation"],
        ))
    db.commit()
    return quiz_id

# -------------------- COMMANDS --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "📚 <b>My Quiz Bot</b>\n\n"
        "Simple Quiz System\n"
        "नीचे से विकल्प चुनें।",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>Help</b>\n\n"
        "📝 Quiz — उपलब्ध Quiz खेलें\n"
        "➕ Add Quiz — नया Quiz import करें\n"
        "📚 Categories — Category/Topic से Quiz चुनें\n"
        "⚙️ Settings — Timer और Random\n"
        "🔄 ReAttempt — पिछले 24 घंटे के गलत सवाल\n"
        "📊 Stats — आपका score\n\n"
        "<b>Quiz Import का fixed format:</b>\n"
        "<code>QUIZ: भारत सामान्य ज्ञान टेस्ट\n"
        "EXAM: RO/ARO\n"
        "SUBJECT: सामान्य ज्ञान\n"
        "CATEGORY: RO/ARO\n"
        "SUBCATEGORY: सामान्य ज्ञान\n"
        "TOPIC: भारत\n\n"
        "Q1: भारत की राजधानी क्या है?\n"
        "A) मुंबई\n"
        "B) नई दिल्ली\n"
        "C) कोलकाता\n"
        "D) चेन्नई\n"
        "ANSWER: B\n"
        "EXPLANATION: नई दिल्ली भारत की राजधानी है।</code>",
        parse_mode="HTML",
        reply_markup=back_button(),
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Current action cancel कर दिया गया।",
        reply_markup=main_menu(),
    )

async def deletequiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "उदाहरण: /deletequiz 12\n\n"
            "Quiz ID देखने के लिए Categories खोलें।",
            reply_markup=back_button(),
        )
        return
    try:
        quiz_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Quiz ID number होना चाहिए।")
        return

    row = db.execute("SELECT title FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if not row:
        await update.message.reply_text("❌ ऐसा Quiz नहीं मिला।")
        return

    db.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
    db.commit()
    await update.message.reply_text(
        f"✅ Quiz delete हो गया:\n{row['title']}",
        reply_markup=main_menu(),
    )

# -------------------- ADD QUIZ --------------------

async def begin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "import"
    await update.message.reply_text(
        "➕ <b>New Quiz Import</b>\n\n"
        "पूरा Quiz एक ही message में भेजें।\n"
        "सिर्फ नीचे वाला fixed format इस्तेमाल करें।\n\n"
        "<code>QUIZ: भारत सामान्य ज्ञान टेस्ट\n"
        "EXAM: RO/ARO\n"
        "SUBJECT: सामान्य ज्ञान\n"
        "CATEGORY: RO/ARO\n"
        "SUBCATEGORY: सामान्य ज्ञान\n"
        "TOPIC: भारत\n\n"
        "Q1: भारत की राजधानी क्या है?\n"
        "A) मुंबई\n"
        "B) नई दिल्ली\n"
        "C) कोलकाता\n"
        "D) चेन्नई\n"
        "ANSWER: B\n"
        "EXPLANATION: नई दिल्ली भारत की राजधानी है।</code>\n\n"
        "❌ Cancel: /cancel",
        parse_mode="HTML",
    )

# -------------------- QUIZ PLAY --------------------

async def show_quiz_start(query, quiz_id, context):
    quiz = db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    if not quiz:
        await query.edit_message_text("❌ Quiz नहीं मिला।", reply_markup=back_button())
        return

    count = db.execute(
        "SELECT COUNT(*) AS n FROM questions WHERE quiz_id=?",
        (quiz_id,),
    ).fetchone()["n"]

    context.user_data.clear()
    context.user_data["play"] = {
        "quiz_id": quiz_id,
        "index": 0,
        "score": 0,
        "total": count,
        "question_ids": [],
        "answers": {},
        "started_at": now_text(),
    }

    settings = get_settings(query.from_user.id)

    await query.edit_message_text(
        format_quiz_text(quiz)
        + f"\n\n📝 Questions: {count}\n"
        + f"⏱ Timer: {'ON' if settings['timer_enabled'] else 'OFF'}\n"
        + f"🔀 Random: {'ON' if settings['random_enabled'] else 'OFF'}\n\n"
        + "शुरू करें?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start Quiz", callback_data=f"go:{quiz_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu_categories")],
        ]),
    )

async def load_questions(quiz_id, random_enabled=False):
    rows = db.execute(
        "SELECT * FROM questions WHERE quiz_id=? ORDER BY number",
        (quiz_id,),
    ).fetchall()
    rows = [dict(r) for r in rows]
    if random_enabled:
        import random
        random.shuffle(rows)
    return rows

async def send_current_question(chat_id, user_id, context, bot):
    play = context.user_data.get("play")
    if not play:
        await bot.send_message(chat_id, "❌ Quiz session नहीं मिली।", reply_markup=main_menu())
        return

    if "questions" not in play:
        settings = get_settings(user_id)
        play["questions"] = await load_questions(
            play["quiz_id"],
            bool(settings["random_enabled"]),
        )

    idx = play["index"]
    questions = play["questions"]

    if idx >= len(questions):
        await finish_quiz(chat_id, user_id, context, bot)
        return

    q = questions[idx]
    play["current_question_id"] = q["id"]

    text = (
        f"📝 <b>Q{idx + 1}/{len(questions)}</b>\n\n"
        f"{q['question']}\n\n"
        f"A) {q['option_a']}\n"
        f"B) {q['option_b']}\n"
        f"C) {q['option_c']}\n"
        f"D) {q['option_d']}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("A", callback_data="ans:A"),
            InlineKeyboardButton("B", callback_data="ans:B"),
        ],
        [
            InlineKeyboardButton("C", callback_data="ans:C"),
            InlineKeyboardButton("D", callback_data="ans:D"),
        ],
    ])

    settings = get_settings(user_id)
    if settings["timer_enabled"]:
        text += f"\n\n⏱ {settings['timer_seconds']} सेकंड"
        # We intentionally do not run a background timer here.
        # This keeps the basic quiz flow reliable on Render free instances.

    await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )

async def go_quiz(query, context):
    quiz_id = int(query.data.split(":")[1])
    play = context.user_data.get("play")
    if not play or play["quiz_id"] != quiz_id:
        await query.edit_message_text("❌ Quiz session expired. फिर से Quiz चुनें।", reply_markup=main_menu())
        return
    await query.edit_message_text("🧠 Quiz शुरू हो रहा है...")
    await send_current_question(query.message.chat_id, query.from_user.id, context, context.bot)

async def answer_question(query, context):
    play = context.user_data.get("play")
    if not play or "questions" not in play:
        await query.answer("Quiz session नहीं मिली।", show_alert=True)
        return

    answer = query.data.split(":")[1]
    idx = play["index"]
    questions = play["questions"]

    if idx >= len(questions):
        await query.answer("Quiz पूरा हो चुका है।")
        return

    q = questions[idx]
    correct = q["answer"]

    # Prevent duplicate answer on same question.
    if play.get("answered_for") == q["id"]:
        await query.answer("इस सवाल का जवाब पहले ही दे चुके हैं।")
        return

    play["answered_for"] = q["id"]

    if answer == correct:
        play["score"] += 1
        result = "✅ सही उत्तर!"
    else:
        result = f"❌ गलत उत्तर\nसही उत्तर: <b>{correct}</b>"

        db.execute("""
            INSERT INTO wrong_answers(user_id, quiz_id, question_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            query.from_user.id,
            play["quiz_id"],
            q["id"],
            now_text(),
        ))
        db.commit()

    explanation = q["explanation"]
    text = result
    if explanation:
        text += f"\n\n💡 {explanation}"

    await query.edit_message_text(text, parse_mode="HTML")
    await asyncio.sleep(0.25)

    play["index"] += 1
    play["answered_for"] = None

    await send_current_question(
        query.message.chat_id,
        query.from_user.id,
        context,
        context.bot,
    )

async def finish_quiz(chat_id, user_id, context, bot):
    play = context.user_data.get("play")
    if not play:
        return

    score = play["score"]
    total = len(play["questions"])
    quiz_id = play["quiz_id"]

    db.execute("""
        INSERT INTO attempts
        (user_id, quiz_id, score, total, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        quiz_id,
        score,
        total,
        play["started_at"],
        now_text(),
    ))
    db.commit()

    percent = round((score / total) * 100) if total else 0

    await bot.send_message(
        chat_id,
        f"🏁 <b>Quiz Complete!</b>\n\n"
        f"🎯 Score: <b>{score}/{total}</b>\n"
        f"📊 Percentage: <b>{percent}%</b>",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )

    context.user_data.clear()

# -------------------- SETTINGS --------------------

async def settings_text(user_id):
    s = get_settings(user_id)
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"⏱ Timer: <b>{'ON' if s['timer_enabled'] else 'OFF'}</b>\n"
        f"⏰ Time: <b>{s['timer_seconds']} sec</b>\n"
        f"🔀 Random Questions: <b>{'ON' if s['random_enabled'] else 'OFF'}</b>"
    )

def settings_keyboard(s):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"⏱ Timer {'ON' if s['timer_enabled'] else 'OFF'}",
            callback_data="set:timer",
        )],
        [
            InlineKeyboardButton("15 sec", callback_data="set:time:15"),
            InlineKeyboardButton("30 sec", callback_data="set:time:30"),
            InlineKeyboardButton("60 sec", callback_data="set:time:60"),
        ],
        [InlineKeyboardButton(
            f"🔀 Random {'ON' if s['random_enabled'] else 'OFF'}",
            callback_data="set:random",
        )],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_main")],
    ])

async def show_settings(query):
    s = get_settings(query.from_user.id)
    await query.edit_message_text(
        await settings_text(query.from_user.id),
        parse_mode="HTML",
        reply_markup=settings_keyboard(s),
    )

async def change_setting(query):
    parts = query.data.split(":")
    user_id = query.from_user.id
    s = get_settings(user_id)

    if parts[1] == "timer":
        new_value = 0 if s["timer_enabled"] else 1
        db.execute(
            "UPDATE user_settings SET timer_enabled=? WHERE user_id=?",
            (new_value, user_id),
        )
    elif parts[1] == "random":
        new_value = 0 if s["random_enabled"] else 1
        db.execute(
            "UPDATE user_settings SET random_enabled=? WHERE user_id=?",
            (new_value, user_id),
        )
    elif parts[1] == "time":
        seconds = int(parts[2])
        db.execute(
            "UPDATE user_settings SET timer_seconds=? WHERE user_id=?",
            (seconds, user_id),
        )

    db.commit()
    await show_settings(query)

# -------------------- STATS / REATTEMPT --------------------

async def show_stats(query):
    uid = query.from_user.id
    attempts = db.execute("""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(score),0) AS score,
               COALESCE(SUM(total),0) AS total
        FROM attempts
        WHERE user_id=?
    """, (uid,)).fetchone()

    wrong = db.execute("""
        SELECT COUNT(*) AS n
        FROM wrong_answers
        WHERE user_id=?
    """, (uid,)).fetchone()["n"]

    pct = round(attempts["score"] * 100 / attempts["total"]) if attempts["total"] else 0

    await query.edit_message_text(
        "📊 <b>Your Stats</b>\n\n"
        f"📝 Attempts: <b>{attempts['n']}</b>\n"
        f"✅ Correct: <b>{attempts['score']}</b>\n"
        f"📚 Questions: <b>{attempts['total']}</b>\n"
        f"📈 Accuracy: <b>{pct}%</b>\n"
        f"❌ Wrong answers saved: <b>{wrong}</b>",
        parse_mode="HTML",
        reply_markup=back_button(),
    )

async def show_reattempt(query, context):
    uid = query.from_user.id
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    rows = db.execute("""
        SELECT DISTINCT q.quiz_id, z.title
        FROM wrong_answers q
        JOIN quizzes z ON z.id=q.quiz_id
        WHERE q.user_id=? AND q.created_at>=?
        ORDER BY z.id DESC
    """, (uid, cutoff)).fetchall()

    if not rows:
        await query.edit_message_text(
            "🔄 पिछले 24 घंटे में कोई गलत सवाल नहीं है।",
            reply_markup=back_button(),
        )
        return

    keyboard = []
    for r in rows:
        keyboard.append([
            InlineKeyboardButton(
                f"🔄 {r['title'][:55]}",
                callback_data=f"reattempt:{r['quiz_id']}",
            )
        ])
    keyboard.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_main")])

    await query.edit_message_text(
        "🔄 <b>ReAttempt</b>\n\nजिस Quiz के गलत सवाल दोबारा करने हैं, चुनें:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def start_reattempt(query, context):
    quiz_id = int(query.data.split(":")[1])
    uid = query.from_user.id
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    rows = db.execute("""
        SELECT DISTINCT q.*
        FROM questions q
        JOIN wrong_answers w ON w.question_id=q.id
        WHERE w.user_id=? AND w.quiz_id=? AND w.created_at>=?
        ORDER BY q.number
    """, (uid, quiz_id, cutoff)).fetchall()

    if not rows:
        await query.edit_message_text(
            "❌ अभी कोई eligible गलत सवाल नहीं है।",
            reply_markup=back_button(),
        )
        return

    context.user_data.clear()
    context.user_data["play"] = {
        "quiz_id": quiz_id,
        "index": 0,
        "score": 0,
        "total": len(rows),
        "started_at": now_text(),
        "questions": [dict(r) for r in rows],
        "reattempt": True,
    }

    await query.edit_message_text("🔄 ReAttempt शुरू हो रहा है...")
    await send_current_question(query.message.chat_id, uid, context, context.bot)

# -------------------- CALLBACK ROUTER --------------------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_main":
        context.user_data.clear()
        await query.edit_message_text(
            "📚 <b>My Quiz Bot</b>\n\nनीचे से विकल्प चुनें।",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "menu_help":
        await query.edit_message_text(
            "❓ <b>Help</b>\n\n"
            "📝 Quiz — Quiz खेलें\n"
            "➕ Add Quiz — नया Quiz import करें\n"
            "📚 Categories — Category/Topic\n"
            "⚙️ Settings — Timer/Random\n"
            "🔄 ReAttempt — 24 घंटे के गलत सवाल\n"
            "📊 Stats — Progress",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        return

    if data == "menu_add":
        context.user_data.clear()
        context.user_data["mode"] = "import"
        await query.edit_message_text(
            "➕ <b>Add Quiz</b>\n\n"
            "पूरा Quiz एक message में भेजें।\n\n"
            "Fixed format:\n\n"
            "<code>QUIZ: भारत सामान्य ज्ञान टेस्ट\n"
            "EXAM: RO/ARO\n"
            "SUBJECT: सामान्य ज्ञान\n"
            "CATEGORY: RO/ARO\n"
            "SUBCATEGORY: सामान्य ज्ञान\n"
            "TOPIC: भारत\n\n"
            "Q1: भारत की राजधानी क्या है?\n"
            "A) मुंबई\n"
            "B) नई दिल्ली\n"
            "C) कोलकाता\n"
            "D) चेन्नई\n"
            "ANSWER: B\n"
            "EXPLANATION: नई दिल्ली भारत की राजधानी है।</code>\n\n"
            "/cancel = Cancel",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        return

    if data == "menu_categories":
        await query.edit_message_text(
            "📚 <b>Categories</b>\n\nQuiz चुनें:",
            parse_mode="HTML",
            reply_markup=categories_keyboard(),
        )
        return

    if data == "all_topics":
        await query.edit_message_text(
            "📚 <b>सभी Quiz</b>",
            parse_mode="HTML",
            reply_markup=quiz_list_keyboard(),
        )
        return

    if data.startswith("cat:"):
        category = data[4:]
        await query.edit_message_text(
            f"📚 <b>{category}</b>\n\nQuiz चुनें:",
            parse_mode="HTML",
            reply_markup=quiz_list_keyboard(category),
        )
        return

    if data == "menu_quiz":
        await query.edit_message_text(
            "📝 <b>Quiz</b>\n\nCategory से Quiz चुनें:",
            parse_mode="HTML",
            reply_markup=categories_keyboard(),
        )
        return

    if data.startswith("play:"):
        await show_quiz_start(query, int(data.split(":")[1]), context)
        return

    if data.startswith("go:"):
        await go_quiz(query, context)
        return

    if data.startswith("ans:"):
        await answer_question(query, context)
        return

    if data == "menu_settings":
        await show_settings(query)
        return

    if data.startswith("set:"):
        await change_setting(query)
        return

    if data == "menu_stats":
        await show_stats(query)
        return

    if data == "menu_reattempt":
        await show_reattempt(query, context)
        return

    if data.startswith("reattempt:"):
        await start_reattempt(query, context)
        return

# -------------------- TEXT IMPORT --------------------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if context.user_data.get("mode") != "import":
        await update.message.reply_text(
            "नीचे से कोई विकल्प चुनें।",
            reply_markup=main_menu(),
        )
        return

    try:
        header, questions = parse_quiz(text)
        quiz_id = save_quiz(header, questions)

        context.user_data.clear()

        await update.message.reply_text(
            "✅ <b>Quiz Save हो गया!</b>\n\n"
            f"🆔 Quiz ID: <b>{quiz_id}</b>\n"
            f"📘 {header['title']}\n"
            f"❓ Questions: <b>{len(questions)}</b>\n"
            f"📚 Category: {header['category'] or '-'}\n"
            f"📌 Topic: {header['topic'] or '-'}\n\n"
            "अब <b>📝 Quiz</b> दबाकर इसे खेल सकते हैं।",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    except ValueError as e:
        await update.message.reply_text(
            "❌ <b>Format Error</b>\n\n"
            f"{str(e)}\n\n"
            "Quiz save नहीं हुआ।\n"
            "Text ठीक करके फिर भेजें।\n\n"
            "⚠️ हर सवाल में A), B), C), D) और ANSWER: A/B/C/D जरूरी है।",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Import failed")
        await update.message.reply_text(
            "❌ Quiz save करते समय unexpected error आया।\n"
            "Text को fixed format में दोबारा भेजें।",
        )

# -------------------- HEALTH SERVER FOR RENDER --------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"OK - My Quiz Bot is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("Health server running on port %s", PORT)
    server.serve_forever()

# -------------------- ERROR HANDLER --------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled Telegram error", exc_info=context.error)

# -------------------- MAIN --------------------

def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing. "
            "Render Environment में BOT_TOKEN सेट करें।"
        )

    Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("deletequiz", deletequiz_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    logger.info("My Quiz Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
