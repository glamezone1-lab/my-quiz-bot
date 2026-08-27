
import os
import re
import json
import sqlite3
import logging
import random
import asyncio
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_USER_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DB_FILE = os.getenv("QUIZ_DB_FILE", "quizbot.sqlite3")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

MAIN_BUTTONS = [
    ["📝 Quiz", "➕ Add Quiz"],
    ["🔄 ReAttempt", "📊 Stats"],
    ["📚 Categories", "⚙️ Settings"],
    ["❓ Help"],
]

# ================= DATABASE =================

def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS categories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS questions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                options TEXT NOT NULL,
                correct TEXT NOT NULL,
                explanation TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(category_id) REFERENCES categories(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL,
                selected INTEGER NOT NULL,
                correct INTEGER NOT NULL,
                attempted_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS revisions(
                question_id INTEGER PRIMARY KEY,
                due_at TEXT NOT NULL,
                wrong_count INTEGER NOT NULL DEFAULT 1
            )
        """)

def is_admin(uid):
    return bool(ADMIN_ID) and str(uid) == ADMIN_ID

def get_category(name):
    with db() as con:
        return con.execute(
            "SELECT * FROM categories WHERE lower(name)=lower(?)",
            (name.strip(),)
        ).fetchone()

def create_category(name):
    name = name.strip()
    if not name:
        return None
    with db() as con:
        con.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))
        return con.execute(
            "SELECT * FROM categories WHERE lower(name)=lower(?)",
            (name,)
        ).fetchone()

def get_categories():
    with db() as con:
        return con.execute("SELECT * FROM categories ORDER BY name").fetchall()

def add_question(category_id, q, options, correct, explanation):
    with db() as con:
        con.execute("""
            INSERT INTO questions
            (category_id,question,options,correct,explanation,created_at)
            VALUES(?,?,?,?,?,?)
        """, (
            category_id, q, json.dumps(options, ensure_ascii=False),
            json.dumps(correct), explanation,
            datetime.now(timezone.utc).isoformat()
        ))

def get_questions(category_id=None):
    with db() as con:
        if category_id is None:
            return con.execute("""
                SELECT q.*, c.name AS category_name
                FROM questions q JOIN categories c ON c.id=q.category_id
                ORDER BY q.id
            """).fetchall()
        return con.execute("""
            SELECT q.*, c.name AS category_name
            FROM questions q JOIN categories c ON c.id=q.category_id
            WHERE q.category_id=?
            ORDER BY q.id
        """, (category_id,)).fetchall()

def get_question(qid):
    with db() as con:
        return con.execute("""
            SELECT q.*, c.name AS category_name
            FROM questions q JOIN categories c ON c.id=q.category_id
            WHERE q.id=?
        """, (qid,)).fetchone()

def save_attempt(qid, selected, correct):
    with db() as con:
        con.execute("""
            INSERT INTO attempts(question_id,selected,correct,attempted_at)
            VALUES(?,?,?,?)
        """, (
            qid, selected, int(correct),
            datetime.now(timezone.utc).isoformat()
        ))

def mark_revision(qid):
    due = datetime.now(timezone.utc) + timedelta(hours=24)
    with db() as con:
        row = con.execute(
            "SELECT wrong_count FROM revisions WHERE question_id=?",
            (qid,)
        ).fetchone()
        count = (row["wrong_count"] + 1) if row else 1
        con.execute("""
            INSERT INTO revisions(question_id,due_at,wrong_count)
            VALUES(?,?,?)
            ON CONFLICT(question_id)
            DO UPDATE SET due_at=excluded.due_at,
                          wrong_count=excluded.wrong_count
        """, (qid, due.isoformat(), count))

def clear_revision(qid):
    with db() as con:
        con.execute("DELETE FROM revisions WHERE question_id=?", (qid,))

def due_revisions():
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        return con.execute("""
            SELECT q.*, c.name AS category_name, r.due_at, r.wrong_count
            FROM revisions r
            JOIN questions q ON q.id=r.question_id
            JOIN categories c ON c.id=q.category_id
            WHERE r.due_at <= ?
            ORDER BY r.due_at
        """, (now,)).fetchall()

def all_wrong_count():
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]

def stats():
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        right = con.execute(
            "SELECT COUNT(*) FROM attempts WHERE correct=1"
        ).fetchone()[0]
    return total, right

# ================= PARSER =================

def parse_quiz(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    starts = list(re.finditer(
        r"(?im)^\s*(?:(?:Q(?:uestion)?|प्रश्न)\s*\d*\s*[:.)-]|\d+\s*[).:-])\s*",
        text
    ))

    if starts:
        blocks = []
        for i, m in enumerate(starts):
            end = starts[i+1].start() if i+1 < len(starts) else len(text)
            blocks.append(text[m.start():end].strip())
    else:
        blocks = [text]

    amap = {
        "A":0, "А":0, "1":0,
        "B":1, "В":1, "2":1,
        "C":2, "С":2, "3":2,
        "D":3, "Д":3, "4":3,
        "E":4, "Е":4, "5":4,
        "F":5, "Ф":5, "6":5,
        "G":6, "Г":6, "7":6,
        "H":7, "Н":7, "8":7,
        "I":8, "И":8, "9":8,
        "J":9, "Й":9, "10":9,
        "K":10, "К":10, "11":10,
        "L":11, "Л":11, "12":11,
    }

    results = []
    for block in blocks:
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        if len(lines) < 3:
            continue

        q = re.sub(
            r"^\s*(?:(?:Q(?:uestion)?|प्रश्न)\s*\d*\s*[:.)-]|\d+\s*[).:-])\s*",
            "", lines[0], flags=re.I
        ).strip()

        options = []
        answer_raw = ""
        explanation = ""

        for line in lines[1:]:
            om = re.match(
                r"^\s*(10|11|12|[A-LА-Л])\s*[).:-]\s*(.+)$",
                line, flags=re.I
            )
            if om:
                options.append(om.group(2).strip())
                continue

            am = re.search(
                r"(?:answer|ans|correct(?:\s+answer)?|सही\s*उत्तर|उत्तर)"
                r"\s*[:=\-]\s*(.+)$",
                line, flags=re.I
            )
            if am:
                answer_raw = am.group(1).upper()
                continue

            em = re.search(
                r"^(?:explanation|व्याख्या)\s*[:=\-]\s*(.+)$",
                line, flags=re.I
            )
            if em:
                explanation = em.group(1).strip()

        tokens = re.findall(r"10|11|12|[A-LА-Л]|[1-9]", answer_raw)
        correct = []
        for token in tokens:
            if token in amap and amap[token] < len(options):
                correct.append(amap[token])

        correct = sorted(set(correct))
        if q and 2 <= len(options) <= 12 and correct:
            results.append({
                "question": q,
                "options": options,
                "correct": correct,
                "explanation": explanation,
            })

    return results

# ================= UI =================

def main_keyboard():
    return ReplyKeyboardMarkup(
        MAIN_BUTTONS,
        resize_keyboard=True,
        is_persistent=True
    )

def category_markup(prefix):
    cats = get_categories()
    rows = []
    for i in range(0, len(cats), 2):
        row = []
        for c in cats[i:i+2]:
            row.append(
                InlineKeyboardButton(
                    f"{c['name']} ({len(get_questions(c['id']))})",
                    callback_data=f"{prefix}:{c['id']}"
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID:
        await update.message.reply_text(
            "⚠️ Render में ADMIN_USER_ID सेट करें।"
        )
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ यह private quiz bot है।"
        )
        return

    await update.message.reply_text(
        "🤖 *Quiz Bot Ready*\n\nनीचे से कोई option चुनें।",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ================= ADD QUIZ =================

async def add_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "category"
    await update.message.reply_text(
        "➕ *Add Quiz*\n\nपहले Quiz की Category/Name भेजें।\n\n"
        "उदाहरण: `General Knowledge`",
        parse_mode="Markdown"
    )

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("mode") != "import":
        await update.message.reply_text(
            "अभी कोई Quiz import नहीं चल रहा है।",
            reply_markup=main_keyboard()
        )
        return

    count = context.user_data.get("import_count", 0)
    category = context.user_data.get("category_name", "Quiz")
    context.user_data.clear()

    await update.message.reply_text(
        f"🎉 *Quiz तैयार है!*\n\n"
        f"📚 Category: {category}\n"
        f"📝 Questions: {count}\n\n"
        "अब 📝 Quiz दबाकर खेलें।",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ================= QUIZ =================

async def quiz_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cats = get_categories()
    if not cats:
        await update.message.reply_text(
            "❌ पहले ➕ Add Quiz से questions डालें।",
            reply_markup=main_keyboard()
        )
        return

    await update.message.reply_text(
        "📝 Quiz के लिए Category चुनें:",
        reply_markup=category_markup("play")
    )

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id):
    rows = [dict(r) for r in get_questions(category_id)]
    if not rows:
        await update.callback_query.message.reply_text(
            "❌ इस Category में questions नहीं हैं。",
            reply_markup=main_keyboard()
        )
        return

    random.shuffle(rows)
    rows = rows[:20]

    context.user_data["session"] = {
        "type": "quiz",
        "questions": rows,
        "index": 0,
        "score": 0,
        "total": len(rows),
        "poll_question_id": None,
    }

    await update.callback_query.message.reply_text(
        f"🎯 Quiz शुरू!\nQuestions: {len(rows)}",
        reply_markup=main_keyboard()
    )
    await send_current_poll(update.effective_user.id, context)

async def send_current_poll(user_id, context):
    s = context.user_data.get("session")
    if not s or s["index"] >= s["total"]:
        return await finish_session(user_id, context)

    q = s["questions"][s["index"]]
    options = json.loads(q["options"])

    try:
        msg = await context.bot.send_poll(
            chat_id=user_id,
            question=f"{s['index']+1}/{s['total']}  {q['question']}",
            options=options,
            type="quiz",
            correct_option_id=int(json.loads(q["correct"])[0]),
            is_anonymous=False
        )
        s["poll_id"] = msg.poll.id
        s["poll_question_id"] = q["id"]
    except Exception:
        log.exception("send_poll failed")

async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or str(update.poll_answer.user.id) != ADMIN_ID:
        return

    s = context.user_data.get("session")
    if not s:
        return

    answer = update.poll_answer
    if not answer.option_ids:
        return

    q = s["questions"][s["index"]]
    selected = answer.option_ids[0]
    correct_ids = json.loads(q["correct"])
    is_correct = selected in correct_ids

    save_attempt(q["id"], selected, is_correct)

    if is_correct:
        s["score"] += 1
        clear_revision(q["id"])
        feedback = "✅ सही!"
    else:
        mark_revision(q["id"])
        feedback = "❌ गलत — 24 घंटे बाद Revision के लिए आएगा।"

    s["index"] += 1

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=feedback + (
            f"\n💡 {q['explanation']}" if q["explanation"] else ""
        )
    )

    if s["index"] < s["total"]:
        await send_current_poll(ADMIN_ID, context)
    else:
        await finish_session(ADMIN_ID, context)

async def finish_session(user_id, context):
    s = context.user_data.get("session")
    if not s:
        return

    total = s["total"]
    score = s["score"]
    percent = round(score * 100 / total, 1) if total else 0

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "🏆 *Quiz Complete!*\n\n"
            f"✅ सही: {score}\n"
            f"❌ गलत: {total-score}\n"
            f"📊 Score: {percent}%\n\n"
            f"🔄 ReAttempt में अभी {all_wrong_count()} गलत questions हैं।"
        ),
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

    context.user_data.pop("session", None)

# ================= REATTEMPT =================

async def reattempt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = due_revisions()

    # If nothing is due yet, show next pending count.
    if not rows:
        count = all_wrong_count()
        if count:
            await update.message.reply_text(
                f"⏳ अभी कोई Revision due नहीं है।\n"
                f"❌ Pending wrong questions: {count}\n\n"
                "गलत सवाल 24 घंटे बाद अपने-आप आएगा।",
                reply_markup=main_keyboard()
            )
        else:
            await update.message.reply_text(
                "🎉 अभी कोई गलत question नहीं है।",
                reply_markup=main_keyboard()
            )
        return

    random.shuffle(rows)
    rows = [dict(r) for r in rows[:20]]

    context.user_data["session"] = {
        "type": "retry",
        "questions": rows,
        "index": 0,
        "score": 0,
        "total": len(rows),
    }

    await update.message.reply_text(
        f"🔄 ReAttempt शुरू!\nDue questions: {len(rows)}",
        reply_markup=main_keyboard()
    )
    await send_current_poll(update.effective_user.id, context)

# ================= STATS =================

async def stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, right = stats()
    wrong = total - right
    accuracy = round(right * 100 / total, 1) if total else 0

    await update.message.reply_text(
        f"📊 *Stats*\n\n"
        f"कुल attempts: {total}\n"
        f"✅ सही: {right}\n"
        f"❌ गलत: {wrong}\n"
        f"🎯 Accuracy: {accuracy}%\n"
        f"🔄 Revision pending: {all_wrong_count()}",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ================= CATEGORIES =================

async def categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cats = get_categories()
    if not cats:
        await update.message.reply_text(
            "📚 अभी कोई Category नहीं है।",
            reply_markup=main_keyboard()
        )
        return

    text = "📚 *Categories*\n\n" + "\n".join(
        f"• {c['name']} — {len(get_questions(c['id']))} questions"
        for c in cats
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=category_markup("play")
    )

# ================= SETTINGS / HELP =================

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ *Settings*\n\n"
        "• Quiz: random questions\n"
        "• प्रति Quiz: अधिकतम 20 questions\n"
        "• Wrong question: 24 घंटे बाद Revision\n"
        "• सही होने पर Revision से हटेगा\n"
        "• Main keyboard: 7 buttons",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Help*\n\n"
        "➕ Add Quiz → Category का नाम → 100–200 questions paste करें → `/done`\n\n"
        "📝 Quiz → Category चुनें → Quiz खेलें\n\n"
        "🔄 ReAttempt → Due गलत questions दोबारा\n\n"
        "गलत question 24 घंटे बाद Revision के लिए due होगा।\n\n"
        "Format:\n"
        "Q1. भारत की राजधानी क्या है?\n"
        "A) मुंबई\nB) दिल्ली\nC) चेन्नई\nD) जयपुर\n"
        "Answer: B\n"
        "Explanation: दिल्ली भारत की राजधानी है।",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ================= TEXT ROUTER =================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or not is_admin(update.effective_user.id):
        return

    text = (update.message.text or "").strip()

    if text == "📝 Quiz":
        return await quiz_menu(update, context)
    if text == "➕ Add Quiz":
        return await add_quiz(update, context)
    if text == "🔄 ReAttempt":
        return await reattempt(update, context)
    if text == "📊 Stats":
        return await stats_menu(update, context)
    if text == "📚 Categories":
        return await categories_menu(update, context)
    if text == "⚙️ Settings":
        return await settings_menu(update, context)
    if text == "❓ Help":
        return await help_menu(update, context)
    if text == "↩️ Back":
        context.user_data.clear()
        return await update.message.reply_text(
            "🏠 Main Menu",
            reply_markup=main_keyboard()
        )

    mode = context.user_data.get("mode")

    if mode == "category":
        cat = create_category(text)
        context.user_data["mode"] = "import"
        context.user_data["category_id"] = cat["id"]
        context.user_data["category_name"] = cat["name"]
        context.user_data["import_count"] = 0

        await update.message.reply_text(
            f"✅ Category: {cat['name']}\n\n"
            "अब 100–200 questions paste करें।\n"
            "जरूरत हो तो कई messages में भेजें।\n\n"
            "पूरा होने पर `/done` भेजें।"
        )
        return

    if mode == "import":
        parsed = parse_quiz(text)
        if not parsed:
            await update.message.reply_text(
                "❌ इस message में valid questions नहीं मिले।"
            )
            return

        cid = context.user_data["category_id"]
        for item in parsed:
            add_question(
                cid,
                item["question"],
                item["options"],
                item["correct"],
                item["explanation"]
            )

        context.user_data["import_count"] += len(parsed)

        await update.message.reply_text(
            f"✅ {len(parsed)} questions save हुए।\n"
            f"📥 Total: {context.user_data['import_count']}\n\n"
            "और questions भेजें या पूरा होने पर `/done` भेजें।"
        )
        return

    await update.message.reply_text(
        "नीचे से कोई button चुनें।",
        reply_markup=main_keyboard()
    )

# ================= CALLBACKS =================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("❌ Access denied.")
        return

    if query.data == "home":
        context.user_data.clear()
        await query.message.reply_text(
            "🏠 Main Menu",
            reply_markup=main_keyboard()
        )
        return

    if query.data.startswith("play:"):
        await start_quiz(
            update,
            context,
            int(query.data.split(":")[1])
        )

# ================= 24-HOUR REVISION WORKER =================

async def revision_worker(app):
    while True:
        try:
            if ADMIN_ID:
                rows = due_revisions()
                for row in rows[:10]:
                    await app.bot.send_message(
                        chat_id=int(ADMIN_ID),
                        text=(
                            "🔔 *24-Hour Revision*\n\n"
                            "कल यह question गलत हुआ था:\n\n"
                            f"❓ {row['question']}\n\n"
                            "इसे दोबारा करने के लिए 🔄 ReAttempt दबाएँ।"
                        ),
                        parse_mode="Markdown",
                        reply_markup=main_keyboard()
                    )
                    # Move due time forward by 24h so it is not sent every loop.
                    with db() as con:
                        next_due = datetime.now(timezone.utc) + timedelta(hours=24)
                        con.execute(
                            "UPDATE revisions SET due_at=? WHERE question_id=?",
                            (next_due.isoformat(), row["id"])
                        )
        except Exception:
            log.exception("Revision worker error")

        await asyncio.sleep(60)

# ================= RENDER HEALTH =================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Quiz Bot is running")

    def log_message(self, format, *args):
        return

def health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()

# ================= MAIN =================

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing.")

    if not ADMIN_ID:
        raise RuntimeError("ADMIN_USER_ID environment variable missing.")

    init_db()

    Thread(target=health_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("done", done))

    app.add_handler(CallbackQueryHandler(callbacks))

    app.add_handler(
        MessageHandler(filters.POLL_ANSWER, poll_answer)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    async def post_init(application):
        application.create_task(revision_worker(application))

    app.post_init = post_init

    log.info("Starting Telegram polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
