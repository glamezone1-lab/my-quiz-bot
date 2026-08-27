import os
import re
import json
import html
import sqlite3
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# ============================================================
# TELEGRAM QUIZ BOT - PERSONAL QUIZ + REVISION EDITION
# Render: polling mode (NO PUBLIC_URL / webhook required)
# ============================================================
# Required Render Environment Variables:
#   BOT_TOKEN       = BotFather token
#   ADMIN_USER_ID   = Your Telegram numeric user ID
# Optional:
#   DB_FILE         = quizbot.sqlite3
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_USER_ID", "0") or "0")
DB_FILE = os.getenv("DB_FILE", "quizbot.sqlite3")
MAX_IMPORT_CHARS = 300_000
MAX_QUESTIONS_PER_IMPORT = 500

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("quizbot")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_conn():
    con = sqlite3.connect(DB_FILE, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db_conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            options_json TEXT NOT NULL,
            correct TEXT NOT NULL,
            explanation TEXT DEFAULT '',
            FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            quiz_id INTEGER NOT NULL,
            total INTEGER NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            chosen TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            answered_at TEXT NOT NULL,
            FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
            FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS wrong_answers (
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            wrong_count INTEGER NOT NULL DEFAULT 1,
            last_wrong_at TEXT NOT NULL,
            PRIMARY KEY(user_id, question_id),
            FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('question_count','10')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('random_questions','1')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('random_options','0')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('show_explanation','1')")
        con.commit()


def setting(key, default=""):
    with db_conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db_conn() as con:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        con.commit()


def is_admin(user_id):
    return ADMIN_ID > 0 and int(user_id) == ADMIN_ID


def ensure_admin(update):
    user = update.effective_user
    return bool(user and is_admin(user.id))


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Quiz", callback_data="menu:quiz"), InlineKeyboardButton("➕ Add Quiz", callback_data="menu:add")],
        [InlineKeyboardButton("🔄 ReAttempt", callback_data="menu:retry"), InlineKeyboardButton("📊 Stats", callback_data="menu:stats")],
        [InlineKeyboardButton("📚 Categories", callback_data="menu:categories"), InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings")],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ])


def back_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="menu:home")]])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Quiz", callback_data="admin:add"), InlineKeyboardButton("📋 Quiz List", callback_data="admin:list")],
        [InlineKeyboardButton("✏️ Edit Quiz", callback_data="admin:edit"), InlineKeyboardButton("🗑️ Delete Quiz", callback_data="admin:delete")],
        [InlineKeyboardButton("📥 Import Quiz", callback_data="admin:import"), InlineKeyboardButton("🔀 Move Quiz", callback_data="admin:move")],
        [InlineKeyboardButton("📚 Categories", callback_data="menu:categories"), InlineKeyboardButton("📊 Stats", callback_data="menu:stats")],
        [InlineKeyboardButton("↩️ Back", callback_data="menu:home")],
    ])


def parse_quiz_text(text):
    """Parse common ChatGPT/manual quiz formats. Returns (questions, errors)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return [], ["Text खाली है।"]
    if len(text) > MAX_IMPORT_CHARS:
        return [], [f"Import बहुत बड़ा है। अधिकतम {MAX_IMPORT_CHARS:,} characters रखें।"]

    lines = [x.strip() for x in text.split("\n")]
    qpat = re.compile(
        r"^(?:Q(?:uestion)?\s*#?\s*\d*\s*[:.)-]|प्रश्न\s*#?\s*\d*\s*[:.)-]|\d+\s*[.)-])\s*(.+)$",
        re.I,
    )
    starts = []
    for i, line in enumerate(lines):
        m = qpat.match(line)
        if m and m.group(1).strip():
            starts.append((i, m.group(1).strip()))

    if not starts:
        return [], ["सवाल नहीं मिला। Q1., 1., Question 1: या प्रश्न 1: जैसा format रखें।"]

    blocks = []
    for n, (idx, question) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        blocks.append((question, lines[idx + 1:end]))

    optpat = re.compile(r"^\(?([A-L])\)?\s*[).:-]\s*(.+)$", re.I)
    ans_pat = re.compile(r"^(?:answer|ans|correct\s*answer|correct|सही\s*उत्तर|उत्तर)\s*[:=-]\s*(.+)$", re.I)
    exp_pat = re.compile(r"^(?:explanation|व्याख्या|समझाइए|स्पष्टीकरण)\s*[:=-]\s*(.*)$", re.I)

    good, errors = [], []
    for number, (question, rest) in enumerate(blocks, 1):
        options = []
        answer_raw = None
        explanation = []
        mode = "options"

        for line in rest:
            if not line:
                continue
            am = ans_pat.match(line)
            if am:
                answer_raw = am.group(1).strip()
                mode = "after_answer"
                continue
            em = exp_pat.match(line)
            if em:
                explanation.append(em.group(1).strip())
                mode = "explanation"
                continue
            om = optpat.match(line)
            if om and mode != "explanation":
                options.append((om.group(1).upper(), om.group(2).strip()))
                mode = "options"
                continue
            if mode == "explanation":
                explanation.append(line)
            elif options and mode != "after_answer":
                # continuation of the previous option
                letter, value = options[-1]
                options[-1] = (letter, value + " " + line)

        if len(options) < 2:
            errors.append(f"Q{number}: कम से कम 2 options नहीं मिले।")
            continue
        if len(options) > 12:
            errors.append(f"Q{number}: 12 से ज्यादा options हैं।")
            continue
        if not answer_raw:
            errors.append(f"Q{number}: Answer/उत्तर नहीं मिला।")
            continue

        letters = [letter for letter, _ in options]
        correct_letters = []
        for part in re.split(r"[,/\s]+", answer_raw.upper()):
            part = part.strip(".()[]")
            if not part:
                continue
            m = re.search(r"[A-L]", part)
            if m and m.group(0) in letters:
                correct_letters.append(m.group(0))
            elif part.isdigit():
                pos = int(part) - 1
                if 0 <= pos < len(letters):
                    correct_letters.append(letters[pos])

        correct_letters = list(dict.fromkeys(correct_letters))
        if not correct_letters:
            errors.append(f"Q{number}: Answer options से match नहीं हुआ।")
            continue

        good.append({
            "question": question,
            "options": [value for _, value in options],
            "correct": ",".join(correct_letters),
            "explanation": " ".join(x for x in explanation if x).strip(),
        })

    if len(good) > MAX_QUESTIONS_PER_IMPORT:
        good = good[:MAX_QUESTIONS_PER_IMPORT]
        errors.append(f"पहले {MAX_QUESTIONS_PER_IMPORT} सवाल ही सेव किए गए।")
    return good, errors


def get_categories():
    with db_conn() as con:
        return con.execute("SELECT * FROM categories ORDER BY name COLLATE NOCASE").fetchall()


def get_or_create_category(name):
    name = (name or "General").strip()[:80] or "General"
    with db_conn() as con:
        con.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))
        row = con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        con.commit()
        return row["id"]


def create_quiz(title, category_name, parsed):
    cat_id = get_or_create_category(category_name)
    with db_conn() as con:
        cur = con.execute("INSERT INTO quizzes(title,category_id,created_at) VALUES(?,?,?)", (title[:120], cat_id, now_iso()))
        quiz_id = cur.lastrowid
        for item in parsed:
            con.execute("INSERT INTO questions(quiz_id,question,options_json,correct,explanation) VALUES(?,?,?,?,?)", (
                quiz_id, item["question"], json.dumps(item["options"], ensure_ascii=False), item["correct"], item.get("explanation", "")
            ))
        con.commit()
        return quiz_id


def quiz_rows():
    with db_conn() as con:
        return con.execute("""SELECT q.id,q.title,COALESCE(c.name,'General') category,COUNT(x.id) n
        FROM quizzes q LEFT JOIN categories c ON c.id=q.category_id LEFT JOIN questions x ON x.quiz_id=q.id
        GROUP BY q.id ORDER BY q.id DESC""").fetchall()


def questions_for_quiz(quiz_id):
    with db_conn() as con:
        return con.execute("SELECT * FROM questions WHERE quiz_id=? ORDER BY id", (quiz_id,)).fetchall()


def get_quiz(quiz_id):
    with db_conn() as con:
        return con.execute("SELECT q.*,COALESCE(c.name,'General') category FROM quizzes q LEFT JOIN categories c ON c.id=q.category_id WHERE q.id=?", (quiz_id,)).fetchone()


def delete_quiz(quiz_id):
    with db_conn() as con:
        con.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
        con.commit()


def wrong_count(user_id):
    with db_conn() as con:
        return con.execute("SELECT COUNT(*) n FROM wrong_answers WHERE user_id=?", (user_id,)).fetchone()["n"]


def stats(user_id):
    with db_conn() as con:
        attempts = con.execute("SELECT COUNT(*) n,COALESCE(SUM(score),0) score,COALESCE(SUM(total),0) total FROM attempts WHERE user_id=?", (user_id,)).fetchone()
        quizzes = con.execute("SELECT COUNT(*) n FROM quizzes").fetchone()["n"]
        questions = con.execute("SELECT COUNT(*) n FROM questions").fetchone()["n"]
        users = con.execute("SELECT COUNT(DISTINCT user_id) n FROM attempts").fetchone()["n"]
        return attempts, quizzes, questions, users


def load_quiz_session(quiz_id, count, random_questions=True):
    rows = questions_for_quiz(quiz_id)
    if random_questions:
        random.shuffle(rows)
    return rows[:max(1, min(count, len(rows)))]


def format_question(row, number, total, random_options=False):
    options = json.loads(row["options_json"])
    pairs = list(enumerate(options))
    if random_options:
        random.shuffle(pairs)
    letters = [chr(ord("A") + i) for i in range(len(pairs))]
    text = f"❓ <b>Q{number}/{total}</b>\n\n{html.escape(row['question'])}\n\n"
    for letter, (orig_idx, value) in zip(letters, pairs):
        text += f"<b>{letter})</b> {html.escape(value)}\n"
    # Store mapping for this question in context; caller handles it.
    return text, pairs, letters


def answer_is_correct(row, selected_letter, pairs, letters):
    options = json.loads(row["options_json"])
    selected_index = pairs[letters.index(selected_letter)][0]
    actual_letter = chr(ord("A") + selected_index)
    correct = {x.strip().upper() for x in row["correct"].split(",") if x.strip()}
    return actual_letter in correct


def question_keyboard(question_id, pairs, letters):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{letter}", callback_data=f"ans:{question_id}:{letter}") for letter in letters],
        [InlineKeyboardButton("⏭️ Skip", callback_data=f"skip:{question_id}")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_admin(update):
        await update.message.reply_text("🔒 यह Personal Quiz Bot है। Access owner के लिए है।")
        return
    context.user_data.clear()
    await update.message.reply_text("🤖 <b>Quiz Bot</b>\n\nअपना विकल्प चुनें:", parse_mode="HTML", reply_markup=main_menu())


async def home(update, context):
    await update.callback_query.answer()
    context.user_data.pop("mode", None)
    await update.callback_query.edit_message_text("🤖 <b>Quiz Bot</b>\n\nअपना विकल्प चुनें:", parse_mode="HTML", reply_markup=main_menu())


async def show_quiz_list(update, context):
    rows = quiz_rows()
    if not rows:
        await update.callback_query.edit_message_text("📭 अभी कोई Quiz सेव नहीं है।", reply_markup=main_menu())
        return
    buttons = []
    for r in rows[:50]:
        buttons.append([InlineKeyboardButton(f"📝 {r['title']} ({r['n']})", callback_data=f"play:{r['id']}")])
    buttons.append([InlineKeyboardButton("↩️ Back", callback_data="menu:home")])
    await update.callback_query.edit_message_text("📝 <b>Quiz चुनें</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def show_add_start(update, context):
    context.user_data.clear()
    context.user_data["mode"] = "add_title"
    await update.callback_query.edit_message_text("➕ <b>Add Quiz</b>\n\nपहली line में इस तरह भेजें:\n<code>Title | Category</code>\n\nउदाहरण:\n<code>Indian History | History</code>\n\nफिर Bot सवाल paste करने को कहेगा।", parse_mode="HTML", reply_markup=back_menu())


async def begin_import(update, context):
    context.user_data["mode"] = "importing"
    context.user_data["import_text"] = ""
    await update.callback_query.edit_message_text(
        "📥 <b>Quiz Import</b>\n\nअब 100–200 सवाल भी भेज सकते हैं। Telegram message छोटा पड़ने पर कई messages में भेजें।\n\nहर message जुड़ता जाएगा। अंत में <b>✅ Finish Import</b> दबाएँ।\n\n<code>/cancel</code> से रद्द कर सकते हैं।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="import:cancel")]])
    )


async def finish_import(update, context):
    raw = context.user_data.get("import_text", "")
    if not raw.strip():
        await update.callback_query.answer("पहले सवाल भेजें।", show_alert=True)
        return
    parsed, errors = parse_quiz_text(raw)
    if not parsed:
        await update.callback_query.edit_message_text("❌ कोई valid सवाल नहीं मिला।\n\n" + "\n".join(errors[:12]), reply_markup=main_menu())
        context.user_data.clear()
        return
    title = context.user_data.get("pending_title", "Imported Quiz")
    category = context.user_data.get("pending_category", "General")
    quiz_id = create_quiz(title, category, parsed)
    msg = f"✅ <b>Quiz Saved</b>\n\n📚 {html.escape(title)}\n📁 {html.escape(category)}\n❓ {len(parsed)} सवाल सेव हुए।"
    if errors:
        msg += "\n\n⚠️ कुछ lines skip हुईं:\n" + "\n".join(html.escape(x) for x in errors[:10])
    context.user_data.clear()
    await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=main_menu())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_admin(update):
        return
    mode = context.user_data.get("mode")
    text = update.message.text.strip()

    if mode == "add_title":
        parts = [x.strip() for x in text.split("|", 1)]
        title = parts[0][:120] if parts and parts[0] else "Imported Quiz"
        category = parts[1][:80] if len(parts) > 1 and parts[1] else "General"
        context.user_data.update(mode="importing", import_text="", pending_title=title, pending_category=category)
        await update.message.reply_text(
            f"📚 <b>{html.escape(title)}</b>\n📁 Category: <b>{html.escape(category)}</b>\n\nअब सारे सवाल paste/send करें।\n100–200 सवाल कई messages में भी भेज सकते हैं।\n\nसभी भेजने के बाद नीचे <b>Finish Import</b> दबाएँ।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish Import", callback_data="import:finish"), InlineKeyboardButton("❌ Cancel", callback_data="import:cancel")]])
        )
        return

    if mode == "importing":
        old = context.user_data.get("import_text", "")
        if len(old) + len(text) > MAX_IMPORT_CHARS:
            await update.message.reply_text("⚠️ Import limit पहुँच गया। अब Finish Import दबाएँ या कम सवाल भेजें।")
            return
        context.user_data["import_text"] = old + "\n" + text
        # Don't parse incomplete chunks as errors; just acknowledge.
        count = len(re.findall(r"^(?:Q(?:uestion)?\s*#?\s*\d+|प्रश्न\s*#?\s*\d+|\d+)\s*[.)-:]", context.user_data["import_text"], re.I | re.M))
        await update.message.reply_text(f"📥 जोड़ दिया गया। अभी लगभग <b>{count}</b> सवाल मिले।\n\nऔर भेजें या Finish Import दबाएँ।", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish Import", callback_data="import:finish"), InlineKeyboardButton("❌ Cancel", callback_data="import:cancel")]]))
        return

    if mode == "cat_add":
        name = text[:80].strip()
        if name:
            get_or_create_category(name)
            context.user_data.clear()
            await update.message.reply_text(f"✅ Category <b>{html.escape(name)}</b> added.", parse_mode="HTML", reply_markup=main_menu())
        else:
            await update.message.reply_text("❌ Category name खाली है।")
        return

    if mode == "cat_rename":
        parts = [x.strip() for x in text.split("|", 1)]
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1]:
            await update.message.reply_text("Format: <code>ID | New Name</code>", parse_mode="HTML")
            return
        with db_conn() as con:
            con.execute("UPDATE categories SET name=? WHERE id=?", (parts[1][:80], int(parts[0])))
            con.commit()
        context.user_data.clear()
        await update.message.reply_text("✅ Category renamed.", reply_markup=main_menu())
        return

    if mode == "cat_delete":
        if not text.isdigit():
            await update.message.reply_text("Category ID भेजें।")
            return
        with db_conn() as con:
            con.execute("UPDATE quizzes SET category_id=NULL WHERE category_id=?", (int(text),))
            con.execute("DELETE FROM categories WHERE id=?", (int(text),))
            con.commit()
        context.user_data.clear()
        await update.message.reply_text("✅ Category deleted. Quizzes को General में रखा गया।", reply_markup=main_menu())
        return

    if mode == "edit_title":
        quiz_id = context.user_data.get("edit_quiz_id")
        if quiz_id:
            with db_conn() as con:
                con.execute("UPDATE quizzes SET title=? WHERE id=?", (text[:120], quiz_id)); con.commit()
            context.user_data.clear()
            await update.message.reply_text("✅ Quiz title updated.", reply_markup=main_menu())
        return


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_admin(update):
        return
    if context.user_data.get("mode") != "importing":
        await update.message.reply_text("पहले ➕ Add Quiz से Import शुरू करें।")
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 2_000_000:
        await update.message.reply_text("❌ File बहुत बड़ी है। 2 MB से छोटी .txt file दें।")
        return
    f = await doc.get_file()
    data = await f.download_as_bytearray()
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        text = bytes(data).decode("utf-8", errors="replace")
    old = context.user_data.get("import_text", "")
    if len(old) + len(text) > MAX_IMPORT_CHARS:
        await update.message.reply_text("❌ Import text limit से बड़ा है।")
        return
    context.user_data["import_text"] = old + "\n" + text
    await update.message.reply_text("📥 File import में जोड़ दी गई। अब और file/text भेजें या Finish Import दबाएँ।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish Import", callback_data="import:finish"), InlineKeyboardButton("❌ Cancel", callback_data="import:cancel")]]))


async def show_categories(update, context):
    cats = get_categories()
    buttons = [[InlineKeyboardButton(f"📁 {c['name']}", callback_data=f"cat:view:{c['id']}")] for c in cats]
    buttons += [[InlineKeyboardButton("➕ Add Category", callback_data="cat:add"), InlineKeyboardButton("✏️ Rename", callback_data="cat:rename")], [InlineKeyboardButton("🗑️ Delete", callback_data="cat:delete")], [InlineKeyboardButton("↩️ Back", callback_data="menu:home")]]
    await update.callback_query.edit_message_text("📚 <b>Categories</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def show_settings(update, context):
    await update.callback_query.edit_message_text(
        "⚙️ <b>Settings</b>\n\n"
        f"Questions/Quiz: <b>{setting('question_count','10')}</b>\n"
        f"Random Questions: <b>{'ON' if setting('random_questions','1')=='1' else 'OFF'}</b>\n"
        f"Random Options: <b>{'ON' if setting('random_options','0')=='1' else 'OFF'}</b>\n"
        f"Explanation: <b>{'ON' if setting('show_explanation','1')=='1' else 'OFF'}</b>\n\n"
        "नीचे विकल्प बदलें:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("5 Questions", callback_data="set:count:5"), InlineKeyboardButton("10 Questions", callback_data="set:count:10")],
            [InlineKeyboardButton("20 Questions", callback_data="set:count:20"), InlineKeyboardButton("All", callback_data="set:count:all")],
            [InlineKeyboardButton("🔀 Random Questions", callback_data="set:randomq"), InlineKeyboardButton("🔀 Random Options", callback_data="set:randomopt")],
            [InlineKeyboardButton("💡 Explanation", callback_data="set:explain")],
            [InlineKeyboardButton("↩️ Back", callback_data="menu:home")],
        ]))


async def show_stats(update, context):
    attempts, quiz_n, question_n, users = stats(update.effective_user.id)
    accuracy = (attempts["score"] / attempts["total"] * 100) if attempts["total"] else 0
    text = ("📊 <b>Stats</b>\n\n"
            f"👤 Users: {users}\n📚 Quizzes: {quiz_n}\n❓ Questions: {question_n}\n"
            f"🎮 Attempts: {attempts['n']}\n✅ Correct: {attempts['score']}\n"
            f"🎯 Accuracy: {accuracy:.1f}%\n❌ ReAttempt pending: {wrong_count(update.effective_user.id)}")
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu())


async def show_help(update, context):
    text = ("❓ <b>Help</b>\n\n"
            "📝 Quiz — saved quizzes खेलें\n"
            "➕ Add Quiz — 100–200 सवाल एक साथ import करें\n"
            "🔄 ReAttempt — जिन सवालों में गलती हुई, वही दोबारा\n"
            "📊 Stats — score/accuracy देखें\n"
            "📚 Categories — quiz को categories में रखें\n"
            "⚙️ Settings — question count/random/explanation\n\n"
            "Import में एक message छोटा पड़ जाए तो कई messages भेजें। आखिर में Finish Import दबाएँ।")
    await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu())


async def play_quiz(update, context, quiz_id):
    q = get_quiz(quiz_id)
    if not q:
        await update.callback_query.edit_message_text("❌ Quiz नहीं मिला।", reply_markup=main_menu()); return
    count_raw = setting("question_count", "10")
    count = 9999 if count_raw == "all" else int(count_raw)
    rows = load_quiz_session(quiz_id, count, setting("random_questions","1") == "1")
    if not rows:
        await update.callback_query.edit_message_text("❌ इस Quiz में सवाल नहीं हैं।", reply_markup=main_menu()); return
    with db_conn() as con:
        cur = con.execute("INSERT INTO attempts(user_id,quiz_id,total,score,started_at) VALUES(?,?,?,?,?)", (update.effective_user.id, quiz_id, len(rows), 0, now_iso()))
        attempt_id = cur.lastrowid
    context.user_data["quiz"] = {"quiz_id": quiz_id, "attempt_id": attempt_id, "rows": [r["id"] for r in rows], "pos": 0, "score": 0, "total": len(rows), "answered": set(), "maps": {}}
    await send_current_question(update.callback_query, context, edit=True)


async def send_current_question(query, context, edit=False):
    session = context.user_data.get("quiz")
    if not session or session["pos"] >= session["total"]:
        await finish_quiz(query, context); return
    qid = session["rows"][session["pos"]]
    with db_conn() as con:
        row = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    text, pairs, letters = format_question(row, session["pos"] + 1, session["total"], setting("random_options","0") == "1")
    session["maps"][qid] = {"pairs": pairs, "letters": letters}
    markup = question_keyboard(qid, pairs, letters)
    if edit:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_answer(query, context, qid, letter):
    session = context.user_data.get("quiz")
    if not session or qid not in session["rows"]:
        await query.answer("यह Quiz session खत्म हो गया है।", show_alert=True); return
    if session["rows"][session["pos"]] != qid:
        await query.answer("यह सवाल पहले ही जा चुका है।", show_alert=True); return
    if qid in session["answered"]:
        await query.answer("Already answered."); return
    with db_conn() as con:
        row = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    mapping = session["maps"].get(qid)
    correct = answer_is_correct(row, letter, mapping["pairs"], mapping["letters"])
    session["answered"].add(qid)
    if correct:
        session["score"] += 1
        with db_conn() as con:
            con.execute("DELETE FROM wrong_answers WHERE user_id=? AND question_id=?", (query.from_user.id, qid)); con.commit()
        result = "✅ <b>सही!</b>"
    else:
        with db_conn() as con:
            con.execute("""INSERT INTO wrong_answers(user_id,question_id,wrong_count,last_wrong_at) VALUES(?,?,1,?)
            ON CONFLICT(user_id,question_id) DO UPDATE SET wrong_count=wrong_count+1,last_wrong_at=excluded.last_wrong_at""", (query.from_user.id, qid, now_iso())); con.commit()
        result = "❌ <b>गलत!</b>"
    explanation = row["explanation"] if setting("show_explanation","1") == "1" else ""
    await query.answer("सही" if correct else "गलत")
    msg = result
    if explanation:
        msg += "\n💡 " + html.escape(explanation)
    await query.edit_message_text(msg, parse_mode="HTML")
    session["pos"] += 1
    if session["pos"] < session["total"]:
        await send_current_question(query, context, edit=False)
    else:
        await finish_quiz(query, context)


async def skip_answer(query, context, qid):
    session = context.user_data.get("quiz")
    if not session or session["rows"][session["pos"]] != qid:
        await query.answer("Session खत्म हो गया है।", show_alert=True); return
    session["answered"].add(qid)
    with db_conn() as con:
        con.execute("""INSERT INTO wrong_answers(user_id,question_id,wrong_count,last_wrong_at) VALUES(?,?,1,?)
        ON CONFLICT(user_id,question_id) DO UPDATE SET wrong_count=wrong_count+1,last_wrong_at=excluded.last_wrong_at""", (query.from_user.id, qid, now_iso())); con.commit()
    await query.answer("Revision में जोड़ दिया")
    session["pos"] += 1
    await query.edit_message_text("⏭️ Skip किया गया — Revision में जोड़ दिया।")
    if session["pos"] < session["total"]:
        await send_current_question(query, context, edit=False)
    else:
        await finish_quiz(query, context)


async def finish_quiz(query, context):
    session = context.user_data.get("quiz")
    if not session: return
    with db_conn() as con:
        con.execute("UPDATE attempts SET score=?,finished_at=? WHERE id=?", (session["score"], now_iso(), session["attempt_id"])); con.commit()
    score, total = session["score"], session["total"]
    acc = score / total * 100 if total else 0
    pending = wrong_count(query.from_user.id)
    context.user_data.pop("quiz", None)
    await query.message.reply_text(
        f"🎉 <b>Quiz Complete!</b>\n\n🎯 Score: <b>{score}/{total}</b>\n📊 Accuracy: <b>{acc:.1f}%</b>\n❌ Revision में: <b>{pending}</b> सवाल\n\nगलत सवाल दोबारा करने के लिए 🔄 ReAttempt दबाएँ।",
        parse_mode="HTML", reply_markup=main_menu()
    )


async def show_retry(update, context):
    with db_conn() as con:
        rows = con.execute("""SELECT w.question_id,w.wrong_count,q.question,qu.title
        FROM wrong_answers w JOIN questions q ON q.id=w.question_id JOIN quizzes qu ON qu.id=q.quiz_id
        WHERE w.user_id=? ORDER BY w.wrong_count DESC,w.last_wrong_at DESC""", (update.effective_user.id,)).fetchall()
    if not rows:
        await update.callback_query.edit_message_text("🎉 अभी कोई गलत/Revision सवाल नहीं है।", reply_markup=main_menu()); return
    buttons = [[InlineKeyboardButton(f"🔄 {i+1}. {r['question'][:45]}", callback_data=f"retryq:{r['question_id']}")] for i,r in enumerate(rows[:50])]
    buttons.append([InlineKeyboardButton("▶️ सभी गलत सवाल", callback_data="retry:all")])
    buttons.append([InlineKeyboardButton("🧹 Revision List साफ करें", callback_data="retry:clear")])
    buttons.append([InlineKeyboardButton("↩️ Back", callback_data="menu:home")])
    await update.callback_query.edit_message_text(f"🔄 <b>ReAttempt</b>\n\nकुल pending: {len(rows)}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def retry_all(update, context):
    with db_conn() as con:
        ids = [r["question_id"] for r in con.execute("SELECT question_id FROM wrong_answers WHERE user_id=? ORDER BY wrong_count DESC,last_wrong_at DESC", (update.effective_user.id,)).fetchall()]
    if not ids:
        await update.callback_query.edit_message_text("🎉 Revision list खाली है।", reply_markup=main_menu()); return
    ids = ids[:50]
    context.user_data["retry"] = {"rows": ids, "pos": 0, "score": 0, "total": len(ids), "maps": {}}
    await send_retry_question(update.callback_query, context)


async def send_retry_question(query, context):
    s = context.user_data.get("retry")
    if not s or s["pos"] >= s["total"]:
        score,total=s["score"],s["total"]
        context.user_data.pop("retry",None)
        await query.message.reply_text(f"🔄 <b>Revision Complete</b>\n\n🎯 {score}/{total}",parse_mode="HTML",reply_markup=main_menu()); return
    qid=s["rows"][s["pos"]]
    with db_conn() as con: row=con.execute("SELECT * FROM questions WHERE id=?",(qid,)).fetchone()
    text,pairs,letters=format_question(row,s["pos"]+1,s["total"],setting("random_options","0")=="1")
    s["maps"][qid]={"pairs":pairs,"letters":letters}
    await query.edit_message_text("🔄 <b>Revision</b>\n\n"+text,parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{x}",callback_data=f"retryans:{qid}:{x}") for x in letters]]))


async def handle_retry_answer(query,context,qid,letter):
    s=context.user_data.get("retry")
    if not s or s["rows"][s["pos"]]!=qid: await query.answer("Session खत्म।",show_alert=True); return
    with db_conn() as con: row=con.execute("SELECT * FROM questions WHERE id=?",(qid,)).fetchone()
    mp=s["maps"][qid]; correct=answer_is_correct(row,letter,mp["pairs"],mp["letters"])
    if correct:
        s["score"]+=1
        with db_conn() as con: con.execute("DELETE FROM wrong_answers WHERE user_id=? AND question_id=?",(query.from_user.id,qid)); con.commit()
        msg="✅ सही — सवाल Revision list से हट गया।"
    else:
        with db_conn() as con: con.execute("UPDATE wrong_answers SET wrong_count=wrong_count+1,last_wrong_at=? WHERE user_id=? AND question_id=?",(now_iso(),query.from_user.id,qid)); con.commit()
        msg="❌ फिर गलत — सवाल Revision में रहेगा।"
    await query.answer("सही" if correct else "गलत")
    await query.edit_message_text(msg,parse_mode="HTML")
    s["pos"]+=1
    await send_retry_question(query,context)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if not ensure_admin(update): await query.answer("Access denied",show_alert=True); return
    data=query.data or ""
    if data=="menu:home": return await home(update,context)
    if data=="menu:quiz": await query.answer(); return await show_quiz_list(update,context)
    if data=="menu:add": await query.answer(); return await show_add_start(update,context)
    if data=="menu:retry": await query.answer(); return await show_retry(update,context)
    if data=="menu:stats": await query.answer(); return await show_stats(update,context)
    if data=="menu:categories": await query.answer(); return await show_categories(update,context)
    if data=="menu:settings": await query.answer(); return await show_settings(update,context)
    if data=="menu:help": await query.answer(); return await show_help(update,context)
    if data=="admin:add": return await show_add_start(update,context)
    if data=="admin:import": return await begin_import(update,context)
    if data=="import:finish": await query.answer(); return await finish_import(update,context)
    if data=="import:cancel": context.user_data.clear(); await query.answer("Cancelled"); return await query.edit_message_text("❌ Import cancel कर दिया गया।",reply_markup=main_menu())
    if data.startswith("play:"): await query.answer(); return await play_quiz(update,context,int(data.split(":")[1]))
    if data.startswith("ans:"):
        _,qid,letter=data.split(":",2); return await handle_answer(query,context,int(qid),letter)
    if data.startswith("skip:"): return await skip_answer(query,context,int(data.split(":")[1]))
    if data=="retry:all": await query.answer(); return await retry_all(update,context)
    if data.startswith("retryans:"):
        _,qid,letter=data.split(":",2); return await handle_retry_answer(query,context,int(qid),letter)
    if data.startswith("retryq:"):
        qid=int(data.split(":")[1]); context.user_data["retry"]={"rows":[qid],"pos":0,"score":0,"total":1,"maps":{}}; await query.answer(); return await send_retry_question(query,context)
    if data=="retry:clear":
        with db_conn() as con: con.execute("DELETE FROM wrong_answers WHERE user_id=?",(query.from_user.id,)); con.commit()
        await query.answer("Revision list cleared"); return await show_retry(update,context)
    if data.startswith("set:"):
        parts=data.split(":")
        if parts[1]=="count": set_setting("question_count",parts[2])
        elif parts[1]=="randomq": set_setting("random_questions","0" if setting("random_questions","1")=="1" else "1")
        elif parts[1]=="randomopt": set_setting("random_options","0" if setting("random_options","0")=="1" else "1")
        elif parts[1]=="explain": set_setting("show_explanation","0" if setting("show_explanation","1")=="1" else "1")
        await query.answer("Updated"); return await show_settings(update,context)
    if data=="admin:list":
        rows=quiz_rows(); text="📋 <b>Quiz List</b>\n\n"+"\n".join(f"{r['id']}. {html.escape(r['title'])} — {r['n']} Q — {html.escape(r['category'])}" for r in rows[:100]) if rows else "📭 कोई quiz नहीं।"; return await query.edit_message_text(text,parse_mode="HTML",reply_markup=admin_menu())
    if data=="admin:edit":
        rows=quiz_rows(); buttons=[[InlineKeyboardButton(f"✏️ {r['title']}",callback_data=f"edit:{r['id']}")] for r in rows[:50]]; buttons.append([InlineKeyboardButton("↩️ Back",callback_data="menu:home")]); return await query.edit_message_text("✏️ Quiz चुनें:",reply_markup=InlineKeyboardMarkup(buttons))
    if data.startswith("edit:"):
        qid=int(data.split(":")[1]); context.user_data.update(mode="edit_title",edit_quiz_id=qid); await query.answer(); return await query.edit_message_text("✏️ नया Quiz title भेजें:\n/cancel से रद्द करें।")
    if data=="admin:delete":
        rows=quiz_rows(); buttons=[[InlineKeyboardButton(f"🗑️ {r['title']}",callback_data=f"del:{r['id']}")] for r in rows[:50]]; buttons.append([InlineKeyboardButton("↩️ Back",callback_data="menu:home")]); return await query.edit_message_text("🗑️ Delete करने वाला Quiz चुनें:",reply_markup=InlineKeyboardMarkup(buttons))
    if data.startswith("del:"):
        delete_quiz(int(data.split(":")[1])); await query.answer("Deleted"); return await query.edit_message_text("✅ Quiz delete हो गया।",reply_markup=admin_menu())
    if data=="admin:move": return await query.edit_message_text("🔀 Move Quiz अभी category बदलने के लिए तैयार है। पहले Quiz चुनें।",reply_markup=admin_menu())
    if data=="cat:add": context.user_data["mode"]="cat_add"; return await query.edit_message_text("📚 Category का नाम भेजें:")
    if data=="cat:rename": context.user_data["mode"]="cat_rename"; return await query.edit_message_text("✏️ पहले इस format में भेजें:\n<code>ID | New Name</code>",parse_mode="HTML")
    if data=="cat:delete": context.user_data["mode"]="cat_delete"; return await query.edit_message_text("🗑️ Category ID भेजें।")
    if data.startswith("cat:view:"):
        cid=int(data.split(":")[2])
        with db_conn() as con: rows=con.execute("SELECT title,(SELECT COUNT(*) FROM questions x WHERE x.quiz_id=q.id) n FROM quizzes q WHERE category_id=?",(cid,)).fetchall()
        text="📁 <b>Category</b>\n\n"+"\n".join(f"• {html.escape(r['title'])} — {r['n']} Q" for r in rows) if rows else "इस category में quiz नहीं है।"; return await query.edit_message_text(text,parse_mode="HTML",reply_markup=back_menu())


async def cancel(update,context):
    if ensure_admin(update):
        context.user_data.clear(); await update.message.reply_text("❌ Cancel कर दिया गया।",reply_markup=main_menu())


def main():
    if not TOKEN: raise RuntimeError("BOT_TOKEN environment variable missing.")
    if not ADMIN_ID: raise RuntimeError("ADMIN_USER_ID environment variable missing.")
    init_db()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("cancel",cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.Document.ALL,handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    log.info("Starting Telegram polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
