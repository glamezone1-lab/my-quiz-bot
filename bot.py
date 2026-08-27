
import os, json, sqlite3, logging, threading, random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN","").strip()
ADMIN = int(os.getenv("ADMIN_USER_ID","0") or 0)
PORT = int(os.getenv("PORT","10000"))
DB = "quizbot.sqlite3"
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

CAT,TITLE,TYPE,QTEXT,QIMAGE,OPTIONS,ANSWER,EXPLAIN,MORE = range(9)
play, admin = {}, {}

def db():
    c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); return c

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS quizzes(id INTEGER PRIMARY KEY AUTOINCREMENT,category_id INTEGER NOT NULL,title TEXT NOT NULL,
          FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY AUTOINCREMENT,quiz_id INTEGER NOT NULL,position INTEGER,
          qtype TEXT,question TEXT,image_url TEXT DEFAULT '',options TEXT DEFAULT '[]',answers TEXT DEFAULT '[]',
          explanation TEXT DEFAULT '',FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,quiz_id INTEGER,score INTEGER,total INTEGER,percent INTEGER);
        CREATE TABLE IF NOT EXISTS settings(user_id INTEGER PRIMARY KEY,explanation INTEGER DEFAULT 1,random_q INTEGER DEFAULT 0,
          random_o INTEGER DEFAULT 0,show_score INTEGER DEFAULT 1);
        """)

def seed():
    with db() as c:
        if c.execute("SELECT COUNT(*) n FROM quizzes").fetchone()["n"]: return
        cid=c.execute("INSERT INTO categories(name) VALUES(?)",("General Knowledge",)).lastrowid
        qid=c.execute("INSERT INTO quizzes(category_id,title) VALUES(?,?)",(cid,"भारत सामान्य ज्ञान")).lastrowid
        data=[
            ("MCQ","भारत का राष्ट्रीय फूल कौन सा है?",["गुलाब","कमल","गेंदा","चमेली"],[1],"कमल भारत का राष्ट्रीय फूल है।"),
            ("TRUE_FALSE","भारत का संविधान 26 जनवरी 1950 को लागू हुआ था?",["सही","गलत"],[0],"26 जनवरी 1950 को लागू हुआ।")]
        for i,(t,q,o,a,e) in enumerate(data,1):
            c.execute("INSERT INTO questions(quiz_id,position,qtype,question,options,answers,explanation) VALUES(?,?,?,?,?,?,?)",
                      (qid,i,t,q,json.dumps(o,ensure_ascii=False),json.dumps(a),e))

def isadmin(uid): return ADMIN and uid==ADMIN
def categories():
    with db() as c:return c.execute("SELECT * FROM categories ORDER BY name").fetchall()
def category(cid):
    with db() as c:return c.execute("SELECT * FROM categories WHERE id=?",(cid,)).fetchone()
def quizzes(cid=None):
    with db() as c:
        if cid:return c.execute("""SELECT q.*,c.name cat FROM quizzes q JOIN categories c ON c.id=q.category_id
                                    WHERE q.category_id=? ORDER BY q.id DESC""",(cid,)).fetchall()
        return c.execute("""SELECT q.*,c.name cat FROM quizzes q JOIN categories c ON c.id=q.category_id
                            ORDER BY q.id DESC""").fetchall()
def quiz(qid):
    with db() as c:
        q=c.execute("SELECT q.*,c.name cat FROM quizzes q JOIN categories c ON c.id=q.category_id WHERE q.id=?",(qid,)).fetchone()
        if not q:return None
        return q,c.execute("SELECT * FROM questions WHERE quiz_id=? ORDER BY position",(qid,)).fetchall()
def settings(uid):
    with db() as c:
        r=c.execute("SELECT * FROM settings WHERE user_id=?",(uid,)).fetchone()
        if not r:
            c.execute("INSERT INTO settings(user_id) VALUES(?)",(uid,)); c.commit()
            r=c.execute("SELECT * FROM settings WHERE user_id=?",(uid,)).fetchone()
        return r
def toggle(uid,key):
    if key not in ("explanation","random_q","random_o","show_score"):return
    with db() as c:c.execute(f"UPDATE settings SET {key}=1-{key} WHERE user_id=?",(uid,));c.commit()

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        b=b"OK";self.send_response(200);self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    def log_message(self,*a):pass
def health(): ThreadingHTTPServer(("0.0.0.0",PORT),Health).serve_forever()


def reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 Quiz", "📚 Categories"],
            ["📊 Stats", "🔄 ReAttempt"],
            ["⚙️ Settings", "❓ Help"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

async def send_home(update, uid):
    await update.message.reply_text(
        "👋 *My Quiz Bot*\n\nनीचे से विकल्प चुनें।",
        parse_mode="Markdown",
        reply_markup=home_k(uid),
    )
    await update.message.reply_text(
        "Quick Menu:",
        reply_markup=reply_keyboard(),
    )

def home_k(uid):
    r=[
        [InlineKeyboardButton("📝 Quiz",callback_data="menu:quiz")],
        [InlineKeyboardButton("📚 Categories",callback_data="menu:cat")],
        [InlineKeyboardButton("📊 Stats",callback_data="menu:stats")],
        [InlineKeyboardButton("🔄 ReAttempt",callback_data="menu:reattempt")],
        [InlineKeyboardButton("⚙️ Settings",callback_data="menu:set")],
        [InlineKeyboardButton("❓ Help",callback_data="menu:help")]]
    if isadmin(uid): r[1:1]=[
        [InlineKeyboardButton("➕ Add Quiz",callback_data="addquiz")],
        [InlineKeyboardButton("🛠️ Manage",callback_data="manage")]]
    return InlineKeyboardMarkup(r)
def home_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu",callback_data="home")]])
def cat_k(admin_mode=False):
    r=[[InlineKeyboardButton("📚 "+c["name"],callback_data=f"cat:{c['id']}")] for c in categories()]
    if admin_mode:r += [
        [InlineKeyboardButton("➕ Add Category",callback_data="addcat")],
        [InlineKeyboardButton("✏️ Rename Category",callback_data="renamecat")],
        [InlineKeyboardButton("🗑️ Delete Category",callback_data="deletecat")]]
    r.append([InlineKeyboardButton("🏠 Main Menu",callback_data="home")]);return InlineKeyboardMarkup(r)
def quiz_k(cid):
    r=[[InlineKeyboardButton("▶️ "+q["title"],callback_data=f"play:{q['id']}")] for q in quizzes(cid)]
    r += [[InlineKeyboardButton("⬅️ Categories",callback_data="menu:cat")],[InlineKeyboardButton("🏠 Main Menu",callback_data="home")]]
    return InlineKeyboardMarkup(r)
def manage_k():return InlineKeyboardMarkup([
    [InlineKeyboardButton("📚 Categories",callback_data="managecat")],
    [InlineKeyboardButton("📝 Quiz List",callback_data="managequiz")],
    [InlineKeyboardButton("➕ Add Quiz",callback_data="addquiz")],
    [InlineKeyboardButton("✏️ Edit Quiz",callback_data="editquiz")],
    [InlineKeyboardButton("🗑️ Delete Quiz",callback_data="deletequiz")],
    [InlineKeyboardButton("🏠 Main Menu",callback_data="home")]])

async def start(u,c):await send_home(u,u.effective_user.id)
async def userid(u,c):await u.message.reply_text(f"Your Telegram User ID: `{u.effective_user.id}`",parse_mode="Markdown")
async def cancel(u,c):admin.pop(u.effective_user.id,None);c.user_data.clear();await u.message.reply_text("❌ Cancelled.",reply_markup=home_k(u.effective_user.id));return ConversationHandler.END

async def callbacks(u,c):
    q=u.callback_query;await q.answer();d=q.data;uid=q.from_user.id
    if d=="home":await q.edit_message_text("👋 *My Quiz Bot*",parse_mode="Markdown",reply_markup=home_k(uid));return
    if d=="menu:quiz":await q.edit_message_text("📚 *Category चुनें:*",parse_mode="Markdown",reply_markup=cat_k());return
    if d=="menu:cat":await q.edit_message_text("📚 *Categories*",parse_mode="Markdown",reply_markup=cat_k(isadmin(uid)));return
    if d.startswith("cat:"):
        x=category(int(d.split(":")[1]));await q.edit_message_text(f"📚 *{x['name']}*\n\nQuiz चुनें:",parse_mode="Markdown",reply_markup=quiz_k(x["id"]));return
    if d=="menu:help":
        t="❓ *Help*\n\nQuiz → Category → Quiz → Answer → Score."
        if isadmin(uid):t+="\n\nAdmin: /addcategory, /addquiz, /importquiz"
        await q.edit_message_text(t,parse_mode="Markdown",reply_markup=home_kb());return
    if d=="menu:stats":
        with db() as x:
            a=x.execute("SELECT COUNT(*) n FROM attempts WHERE user_id=?",(uid,)).fetchone()["n"]
            av=x.execute("SELECT COALESCE(ROUND(AVG(percent)),0) n FROM attempts WHERE user_id=?",(uid,)).fetchone()["n"]
            z=x.execute("SELECT COUNT(*) n FROM quizzes").fetchone()["n"];n=x.execute("SELECT COUNT(*) n FROM questions").fetchone()["n"]
        await q.edit_message_text(f"📊 *Stats*\n\n📝 Quizzes: {z}\n❓ Questions: {n}\n🎯 Attempts: {a}\n📈 Average: {av}%",parse_mode="Markdown",reply_markup=home_kb());return
    if d=="menu:reattempt":
        with db() as x:r=x.execute("SELECT quiz_id FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 1",(uid,)).fetchone()
        if not r:await q.edit_message_text("अभी कोई attempt नहीं है.",reply_markup=home_kb());return
        await q.edit_message_text("🔄 फिर से खेलें?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start",callback_data=f"play:{r['quiz_id']}")],[InlineKeyboardButton("🏠 Main Menu",callback_data="home")]]));return
    if d=="menu:set":
        await show_settings(q,uid);return
    if d.startswith("set:"):
        toggle(uid,d.split(":")[1]);await show_settings(q,uid);return
    if d=="manage" and isadmin(uid):await q.edit_message_text("🛠️ *Manage*",parse_mode="Markdown",reply_markup=manage_k());return
    if d=="managecat" and isadmin(uid):await q.edit_message_text("📚 *Category Management*",parse_mode="Markdown",reply_markup=cat_k(True));return
    if d=="managequiz" and isadmin(uid):
        t="📝 *Quiz List*\n\n"+"\n".join(f"{i+1}. {x['title']} — {x['cat']}" for i,x in enumerate(quizzes())) or "No quizzes."
        await q.edit_message_text(t,parse_mode="Markdown",reply_markup=manage_k());return
    if d=="addcat" and isadmin(uid):await q.edit_message_text("➕ /addcategory भेजें.");return
    if d=="renamecat" and isadmin(uid):
        r=[[InlineKeyboardButton("✏️ "+x["name"],callback_data=f"rename:{x['id']}")] for x in categories()];r.append([InlineKeyboardButton("⬅️ Manage",callback_data="manage")])
        await q.edit_message_text("Category चुनें:",reply_markup=InlineKeyboardMarkup(r));return
    if d.startswith("rename:") and isadmin(uid):admin[uid]={"rename":int(d.split(":")[1])};await q.edit_message_text("नया नाम भेजें. /cancel");return
    if d=="deletecat" and isadmin(uid):
        r=[[InlineKeyboardButton("🗑️ "+x["name"],callback_data=f"dc:{x['id']}")] for x in categories()];r.append([InlineKeyboardButton("⬅️ Manage",callback_data="manage")])
        await q.edit_message_text("⚠️ Category के साथ उसके quizzes भी delete होंगे.\n\nचुनें:",reply_markup=InlineKeyboardMarkup(r));return
    if d.startswith("dc:") and isadmin(uid):
        with db() as x:x.execute("DELETE FROM categories WHERE id=?",(int(d.split(":")[1]),));x.commit()
        await q.edit_message_text("✅ Category deleted.",reply_markup=manage_k());return
    if d=="editquiz" and isadmin(uid):
        r=[[InlineKeyboardButton("✏️ "+x["title"],callback_data=f"eq:{x['id']}")] for x in quizzes()]
        r.append([InlineKeyboardButton("⬅️ Manage",callback_data="manage")])
        await q.edit_message_text("Edit करने वाला quiz चुनें:",reply_markup=InlineKeyboardMarkup(r));return
    if d.startswith("eq:") and isadmin(uid):
        admin[uid]={"edit_quiz":int(d.split(":")[1])}
        await q.edit_message_text("नया Quiz Title भेजें. /cancel");return
    if d=="deletequiz" and isadmin(uid):
        r=[[InlineKeyboardButton("🗑️ "+x["title"],callback_data=f"dq:{x['id']}")] for x in quizzes()];r.append([InlineKeyboardButton("⬅️ Manage",callback_data="manage")])
        await q.edit_message_text("Quiz चुनें:",reply_markup=InlineKeyboardMarkup(r));return
    if d.startswith("dq:") and isadmin(uid):
        with db() as x:x.execute("DELETE FROM quizzes WHERE id=?",(int(d.split(":")[1]),));x.commit()
        await q.edit_message_text("✅ Quiz deleted.",reply_markup=manage_k());return
    if d.startswith("play:"):await startplay(q,uid,int(d.split(":")[1]));return
    if d=="next":await sendq(q,uid);return
    if d.startswith("ans:"):await answer(q,uid,*map(int,d.split(":")[1:]));return
    if d.startswith("mul:"):await mult(q,uid,*map(int,d.split(":")[1:]));return
    if d.startswith("ms:"):await multsubmit(q,uid,int(d.split(":")[1]));return

async def show_settings(q,uid):
    s=settings(uid);yn=lambda v:"ON" if v else "OFF"
    k=InlineKeyboardMarkup([
        [InlineKeyboardButton("💡 Explanation "+yn(s["explanation"]),callback_data="set:explanation")],
        [InlineKeyboardButton("🔀 Random Questions "+yn(s["random_q"]),callback_data="set:random_q")],
        [InlineKeyboardButton("🔀 Random Options "+yn(s["random_o"]),callback_data="set:random_o")],
        [InlineKeyboardButton("📊 Show Score "+yn(s["show_score"]),callback_data="set:show_score")],
        [InlineKeyboardButton("🏠 Main Menu",callback_data="home")]])
    await q.edit_message_text("⚙️ *Settings*",parse_mode="Markdown",reply_markup=k)

def qkb(q,opts):return InlineKeyboardMarkup([[InlineKeyboardButton(f"{chr(65+i)}. {v}",callback_data=f"ans:{q['id']}:{i}")] for i,v in enumerate(opts)])
def mkb(q,opts,sel):
    r=[[InlineKeyboardButton(("☑️ " if i in sel else "⬜ ")+f"{chr(65+i)}. {v}",callback_data=f"mul:{q['id']}:{i}")] for i,v in enumerate(opts)]
    r.append([InlineKeyboardButton("✅ Submit",callback_data=f"ms:{q['id']}")]);return InlineKeyboardMarkup(r)

async def startplay(q,uid,qid):
    z=quiz(qid)
    if not z:await q.edit_message_text("Quiz नहीं मिला.",reply_markup=home_kb());return
    qz,qs=z
    if not qs:await q.edit_message_text("इस quiz में questions नहीं हैं.",reply_markup=home_kb());return
    ids=[x["id"] for x in qs]
    if settings(uid)["random_q"]:random.shuffle(ids)
    play[uid]={"quiz":qid,"ids":ids,"i":0,"score":0,"sel":set()};await sendq(q,uid)

async def sendq(q,uid):
    s=play.get(uid)
    if not s:return
    z=quiz(s["quiz"]);qz,qs=z
    if s["i"]>=len(s["ids"]):
        score,total=s["score"],len(s["ids"]);pct=round(score*100/total)
        with db() as x:x.execute("INSERT INTO attempts(user_id,quiz_id,score,total,percent) VALUES(?,?,?,?,?)",(uid,s["quiz"],score,total,pct));x.commit()
        play.pop(uid,None);txt="🏁 *Quiz Complete!*"
        if settings(uid)["show_score"]:txt+=f"\n\n🎯 Score: *{score}/{total}*\n📈 {pct}%"
        await q.edit_message_text(txt,parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 ReAttempt",callback_data=f"play:{qz['id']}")],[InlineKeyboardButton("🏠 Main Menu",callback_data="home")]]));return
    row=next(x for x in qs if x["id"]==s["ids"][s["i"]]);opts=json.loads(row["options"]);ans=json.loads(row["answers"])
    if settings(uid)["random_o"] and row["qtype"] in ("MCQ","MULTI","IMAGE_MCQ"):
        p=list(enumerate(opts));random.shuffle(p);mp={old:new for new,(old,_) in enumerate(p)};opts=[v for _,v in p];ans=[mp[a] for a in ans]
    s.update(qid=row["id"],opts=opts,ans=ans,waiting=row["qtype"] in ("TEXT","FILL_BLANK","ORDERING"))
    text=f"❓ *Question {s['i']+1}/{len(s['ids'])}*\n📚 {qz['title']}\n\n{row['question']}"
    if row["image_url"]:
        try:await q.message.reply_photo(row["image_url"])
        except Exception:pass
    if row["qtype"] in ("TEXT","FILL_BLANK","ORDERING"):await q.edit_message_text(text+"\n\n✍️ अपना उत्तर message में लिखें.",parse_mode="Markdown");return
    if row["qtype"]=="MULTI":s["sel"]=set();await q.edit_message_text(text+"\n\nएक या अधिक चुनें:",parse_mode="Markdown",reply_markup=mkb(row,opts,set()));return
    await q.edit_message_text(text,parse_mode="Markdown",reply_markup=qkb(row,opts))

async def answer(q,uid,qid,sel):
    s=play.get(uid)
    if not s or s["qid"]!=qid:return
    ok=sel in s["ans"];s["score"]+=ok
    row=next(x for x in quiz(s["quiz"])[1] if x["id"]==qid)
    text="✅ *सही उत्तर!*" if ok else "❌ *गलत उत्तर!*\nसही: "+", ".join(f"{chr(65+i)}. {s['opts'][i]}" for i in s["ans"])
    if settings(uid)["explanation"] and row["explanation"]:text+="\n\n💡 "+row["explanation"]
    s["i"]+=1;s["waiting"]=False;await q.edit_message_text(text,parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Next",callback_data="next")]]))
async def mult(q,uid,qid,sel):
    s=play.get(uid)
    if not s or s["qid"]!=qid:return
    s.setdefault("sel",set());s["sel"].remove(sel) if sel in s["sel"] else s["sel"].add(sel);await q.edit_message_reply_markup(reply_markup=mkb({"id":qid},s["opts"],s["sel"]))
async def multsubmit(q,uid,qid):
    s=play.get(uid)
    if not s or s["qid"]!=qid:return
    ok=set(s["sel"])==set(s["ans"]);s["score"]+=ok;row=next(x for x in quiz(s["quiz"])[1] if x["id"]==qid)
    text="✅ *सही उत्तर!*" if ok else "❌ *गलत उत्तर!*\nसही: "+", ".join(f"{chr(65+i)}. {s['opts'][i]}" for i in s["ans"])
    if settings(uid)["explanation"] and row["explanation"]:text+="\n\n💡 "+row["explanation"]
    s["i"]+=1;s["sel"]=set();await q.edit_message_text(text,parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Next",callback_data="next")]]))
async def text_answer(u,c):
    uid=u.effective_user.id;s=play.get(uid)
    if not s or not s.get("waiting"):return
    row=next(x for x in quiz(s["quiz"])[1] if x["id"]==s["qid"]);expected=json.loads(row["answers"]);ok=u.message.text.strip().casefold() in [str(x).casefold() for x in expected];s["score"]+=ok
    text="✅ *सही उत्तर!*" if ok else f"❌ *गलत उत्तर!*\nसही: {expected[0]}"
    if settings(uid)["explanation"] and row["explanation"]:text+="\n\n💡 "+row["explanation"]
    s["i"]+=1;s["waiting"]=False;await u.message.reply_text(text,parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Next",callback_data="next")]]))

async def addcategory(u,c):
    if not isadmin(u.effective_user.id):return ConversationHandler.END
    await u.message.reply_text("➕ Category name भेजें. /cancel");return CAT
async def savecategory(u,c):
    try:
        with db() as x:x.execute("INSERT INTO categories(name) VALUES(?)",(u.message.text.strip(),));x.commit()
        await u.message.reply_text("✅ Category created.",reply_markup=home_k(u.effective_user.id));return ConversationHandler.END
    except sqlite3.IntegrityError:await u.message.reply_text("⚠️ यह category पहले से है.");return CAT

async def addquiz(u,c):
    uid=u.effective_user.id
    if not isadmin(uid):return ConversationHandler.END
    if not categories():await u.message.reply_text("पहले /addcategory करें.");return ConversationHandler.END
    admin[uid]={"quiz":{"questions":[]}}
    k=[[InlineKeyboardButton(x["name"],callback_data=f"pick:{x['id']}")] for x in categories()]
    if u.callback_query:
        await u.callback_query.answer();await u.callback_query.edit_message_text("➕ *New Quiz*\n\nCategory चुनें:",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(k))
    else:await u.message.reply_text("➕ *New Quiz*\n\nCategory चुनें:",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(k))
    return CAT
async def pick(u,c):
    q=u.callback_query;await q.answer();uid=q.from_user.id;admin[uid]["quiz"]["cat"]=int(q.data.split(":")[1]);await q.edit_message_text("Quiz Title भेजें.");return TITLE
async def title(u,c):admin[u.effective_user.id]["quiz"]["title"]=u.message.text.strip();await u.message.reply_text("Type: 1=MCQ, 2=TRUE_FALSE, 3=MULTI, 4=TEXT, 5=IMAGE_MCQ, 6=FILL_BLANK, 7=ORDERING");return TYPE
async def typ(u,c):
    m={"1":"MCQ","2":"TRUE_FALSE","3":"MULTI","4":"TEXT","5":"IMAGE_MCQ","6":"FILL_BLANK","7":"ORDERING"};v=u.message.text.strip().upper()
    if v not in m:await u.message.reply_text("सिर्फ 1-5.");return TYPE
    admin[u.effective_user.id]["cur"]={"qtype":m[v],"options":[],"answers":[],"image_url":"","explanation":""};await u.message.reply_text("Question text भेजें.");return QTEXT
async def qtext(u,c):
    x=admin[u.effective_user.id]["cur"];x["question"]=u.message.text.strip()
    if x["qtype"]=="IMAGE_MCQ":await u.message.reply_text("Image URL भेजें या -");return QIMAGE
    if x["qtype"]=="TRUE_FALSE":x["options"]=["सही","गलत"];await u.message.reply_text("Answer: सही या गलत");return ANSWER
    if x["qtype"] in ("TEXT","FILL_BLANK"):
        await u.message.reply_text("सही text answer भेजें.");return ANSWER
    if x["qtype"]=="ORDERING":
        await u.message.reply_text("सही क्रम comma से भेजें. उदाहरण: पहला, दूसरा, तीसरा");return ANSWER
    await u.message.reply_text("Options नई lines में: A. पहला\\nB. दूसरा\\nC. तीसरा\\nD. चौथा\\n(2-12)");return OPTIONS
async def qimage(u,c):admin[u.effective_user.id]["cur"]["image_url"]="" if u.message.text.strip()=="-" else u.message.text.strip();await u.message.reply_text("Options नई lines में भेजें.");return OPTIONS
def parseopts(t):
    a=[]
    for x in [z.strip() for z in t.splitlines() if z.strip()]:
        a.append(x[2:].strip() if len(x)>2 and x[0].upper() in "ABCDEFGHIJKL" and x[1] in ".):" else x)
    return a if 2<=len(a)<=12 else None
async def opts(u,c):
    a=parseopts(u.message.text)
    if not a:await u.message.reply_text("⚠️ 2-12 options चाहिए.");return OPTIONS
    admin[u.effective_user.id]["cur"]["options"]=a;await u.message.reply_text("Answer: MCQ में A, MULTI में A,C");return ANSWER
async def ans(u,c):
    x=admin[u.effective_user.id]["cur"];v=u.message.text.strip()
    if x["qtype"]=="TRUE_FALSE":
        if v.casefold() not in ("सही","गलत","true","false"):await u.message.reply_text("सिर्फ सही/गलत.");return ANSWER
        x["answers"]=[0 if v.casefold() in ("सही","true") else 1]
    elif x["qtype"] in ("TEXT","FILL_BLANK","ORDERING"):x["answers"]=[v]
    else:
        try:a=sorted(set(ord(z.strip().upper())-65 for z in v.split(",") if z.strip()))
        except:a=[]
        if not a or any(i<0 or i>=len(x["options"]) for i in a) or (x["qtype"]!="MULTI" and len(a)!=1):await u.message.reply_text("⚠️ Answer गलत है.");return ANSWER
        x["answers"]=a
    await u.message.reply_text("Explanation भेजें या -");return EXPLAIN
async def explain(u,c):admin[u.effective_user.id]["cur"]["explanation"]="" if u.message.text.strip()=="-" else u.message.text.strip();await u.message.reply_text("और question? हाँ/नहीं");return MORE
async def more(u,c):
    uid=u.effective_user.id;v=u.message.text.strip().casefold();admin[uid]["quiz"]["questions"].append(admin[uid]["cur"].copy())
    if v in ("हाँ","हां","yes","y"):await u.message.reply_text("Next type: 1=MCQ, 2=TRUE_FALSE, 3=MULTI, 4=TEXT, 5=IMAGE_MCQ, 6=FILL_BLANK, 7=ORDERING");return TYPE
    if v not in ("नहीं","नही","no","n"):admin[uid]["quiz"]["questions"].pop();await u.message.reply_text("सिर्फ हाँ/नहीं.");return MORE
    d=admin.pop(uid)["quiz"]
    with db() as x:
        qz=x.execute("INSERT INTO quizzes(category_id,title) VALUES(?,?)",(d["cat"],d["title"])).lastrowid
        for i,z in enumerate(d["questions"],1):x.execute("INSERT INTO questions(quiz_id,position,qtype,question,image_url,options,answers,explanation) VALUES(?,?,?,?,?,?,?,?)",(qz,i,z["qtype"],z["question"],z["image_url"],json.dumps(z["options"],ensure_ascii=False),json.dumps(z["answers"],ensure_ascii=False),z["explanation"]))
    await u.message.reply_text(f"✅ Quiz created: {d['title']}\nQuestions: {len(d['questions'])}",reply_markup=home_k(uid));return ConversationHandler.END

IMPORT_EXAMPLE="""[QUIZ]
Title: मानव मनोविज्ञान
Category: Psychology

[QUESTION]
Type: MCQ
Question: सवाल यहाँ
A: पहला
B: दूसरा
C: तीसरा
D: चौथा
Answer: B
Explanation: कारण

[QUESTION]
Type: TRUE_FALSE
Question: कथन यहाँ
Answer: True
Explanation: कारण

[QUESTION]
Type: MULTI
Question: कई सही हो सकते हैं
A: पहला
B: दूसरा
C: तीसरा
D: चौथा
Answer: A,C
Explanation: कारण

[QUESTION]
Type: TEXT
Question: भारत की राजधानी?
Answer: नई दिल्ली
Explanation: कारण

[QUESTION]
Type: FILL_BLANK
Question: भारत की राजधानी ______ है।
Answer: नई दिल्ली
Explanation: कारण

[QUESTION]
Type: ORDERING
Question: सही क्रम लिखें।
Answer: पहला, दूसरा, तीसरा
Explanation: कारण"""
def parse_import(s):
    title=catname="";qs=[];cur=None
    for raw in s.splitlines():
        x=raw.strip()
        if not x:continue
        if x.upper()=="[QUIZ]":cur=None;continue
        if x.upper()=="[QUESTION]":
            if cur:qs.append(cur)
            cur={"qtype":"MCQ","question":"","image_url":"","options":[],"answers":[],"explanation":""};continue
        if ":" not in x:continue
        k,v=x.split(":",1);k=k.strip().lower();v=v.strip()
        if cur is None:
            if k=="title":title=v
            elif k=="category":catname=v
        elif k=="type":cur["qtype"]=v.upper()
        elif k=="question":cur["question"]=v
        elif k in "abcdefghijkl":cur["options"].append(v)
        elif k=="image":cur["image_url"]=v
        elif k=="explanation":cur["explanation"]=v
        elif k=="answer":
            if cur["qtype"] in ("TEXT","FILL_BLANK","ORDERING"):cur["answers"]=[v]
            elif cur["qtype"]=="TRUE_FALSE":cur["options"]=["सही","गलत"];cur["answers"]=[0 if v.casefold() in ("true","सही","yes") else 1]
            else:cur["answers"]=[ord(z.strip().upper())-65 for z in v.split(",") if z.strip()]
    if cur:qs.append(cur)
    if not title or not catname or not qs:raise ValueError("Title, Category और Question जरूरी हैं.")
    for i,z in enumerate(qs,1):
        if z["qtype"] not in ("MCQ","TRUE_FALSE","MULTI","TEXT","IMAGE_MCQ","FILL_BLANK","ORDERING"):raise ValueError(f"Question {i}: Type गलत.")
        if not z["question"]:raise ValueError(f"Question {i}: text missing.")
        if z["qtype"] in ("MCQ","MULTI","IMAGE_MCQ") and (not 2<=len(z["options"])<=12 or not z["answers"]):raise ValueError(f"Question {i}: options/answer गलत.")
    return title,catname,qs
async def importcmd(u,c):
    if not isadmin(u.effective_user.id):return
    c.user_data["import"]=1;await u.message.reply_text("📥 पूरा import text एक message में भेजें:\n\n"+IMPORT_EXAMPLE+"\n\n/cancel")
async def importtext(u,c):
    if not isadmin(u.effective_user.id) or not c.user_data.get("import"):return
    try:title_,cat_,qs=parse_import(u.message.text)
    except Exception as e:await u.message.reply_text("❌ Format error: "+str(e));return
    with db() as x:
        r=x.execute("SELECT id FROM categories WHERE name=?",(cat_,)).fetchone();cid=r["id"] if r else x.execute("INSERT INTO categories(name) VALUES(?)",(cat_,)).lastrowid
        qz=x.execute("INSERT INTO quizzes(category_id,title) VALUES(?,?)",(cid,title_)).lastrowid
        for i,z in enumerate(qs,1):x.execute("INSERT INTO questions(quiz_id,position,qtype,question,image_url,options,answers,explanation) VALUES(?,?,?,?,?,?,?,?)",(qz,i,z["qtype"],z["question"],z["image_url"],json.dumps(z["options"],ensure_ascii=False),json.dumps(z["answers"]),z["explanation"]))
    c.user_data.pop("import",None);await u.message.reply_text("✅ Import successful.",reply_markup=home_k(u.effective_user.id))

async def reply_menu_router(u,c):
    text = (u.message.text or "").strip()
    uid = u.effective_user.id
    if text == "📝 Quiz":
        cats = categories()
        if not cats:
            await u.message.reply_text("📚 अभी कोई category नहीं है.", reply_markup=reply_keyboard())
            return
        rows = [[InlineKeyboardButton("📚 "+x["name"], callback_data=f"cat:{x['id']}")] for x in cats]
        rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
        await u.message.reply_text("📚 *Category चुनें:*", parse_mode="Markdown",
                                   reply_markup=InlineKeyboardMarkup(rows))
    elif text == "📚 Categories":
        await u.message.reply_text("📚 *Categories*", parse_mode="Markdown",
                                   reply_markup=cat_k(isadmin(uid)))
    elif text == "📊 Stats":
        with db() as x:
            a=x.execute("SELECT COUNT(*) n FROM attempts WHERE user_id=?",(uid,)).fetchone()["n"]
            av=x.execute("SELECT COALESCE(ROUND(AVG(percent)),0) n FROM attempts WHERE user_id=?",(uid,)).fetchone()["n"]
            z=x.execute("SELECT COUNT(*) n FROM quizzes").fetchone()["n"]
            n=x.execute("SELECT COUNT(*) n FROM questions").fetchone()["n"]
        await u.message.reply_text(
            f"📊 *Stats*\n\n📝 Quizzes: {z}\n❓ Questions: {n}\n🎯 Attempts: {a}\n📈 Average: {av}%",
            parse_mode="Markdown", reply_markup=reply_keyboard()
        )
    elif text == "🔄 ReAttempt":
        with db() as x:
            r=x.execute("SELECT quiz_id FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 1",(uid,)).fetchone()
        if not r:
            await u.message.reply_text("अभी कोई attempt नहीं है.", reply_markup=reply_keyboard())
        else:
            await u.message.reply_text(
                "🔄 फिर से खेलें?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ Start", callback_data=f"play:{r['quiz_id']}")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="home")]
                ])
            )
    elif text == "⚙️ Settings":
        # Reply-keyboard version of settings.
        s=settings(uid); yn=lambda v:"ON" if v else "OFF"
        k=InlineKeyboardMarkup([
            [InlineKeyboardButton("💡 Explanation "+yn(s["explanation"]),callback_data="set:explanation")],
            [InlineKeyboardButton("🔀 Random Questions "+yn(s["random_q"]),callback_data="set:random_q")],
            [InlineKeyboardButton("🔀 Random Options "+yn(s["random_o"]),callback_data="set:random_o")],
            [InlineKeyboardButton("📊 Show Score "+yn(s["show_score"]),callback_data="set:show_score")],
            [InlineKeyboardButton("🏠 Main Menu",callback_data="home")]
        ])
        await u.message.reply_text("⚙️ *Settings*",parse_mode="Markdown",reply_markup=k)
    elif text == "❓ Help":
        t="❓ *Help*\n\nQuiz → Category → Quiz → Answer → Score."
        if isadmin(uid): t+="\n\nAdmin: /addcategory, /addquiz, /importquiz"
        await u.message.reply_text(t,parse_mode="Markdown",reply_markup=reply_keyboard())

async def admintext(u,c):
    uid=u.effective_user.id
    if not isadmin(uid):return
    if "rename" in admin:
        try:
            with db() as x:
                x.execute("UPDATE categories SET name=? WHERE id=?",(u.message.text.strip(),admin[uid]["rename"]))
                x.commit()
            admin.pop(uid);await u.message.reply_text("✅ Category renamed.",reply_markup=home_k(uid))
        except sqlite3.IntegrityError:await u.message.reply_text("⚠️ यह नाम पहले से है.")
        return
    if "edit_quiz" in admin:
        title=u.message.text.strip()
        if not title:
            await u.message.reply_text("Title खाली नहीं हो सकता.");return
        with db() as x:
            x.execute("UPDATE quizzes SET title=? WHERE id=?",(title,admin[uid]["edit_quiz"]))
            x.commit()
        admin.pop(uid);await u.message.reply_text("✅ Quiz title updated.",reply_markup=home_k(uid))

def main():
    if not TOKEN:raise RuntimeError("BOT_TOKEN missing in Render Environment.")
    init();seed();threading.Thread(target=health,daemon=True).start()
    app=ApplicationBuilder().token(TOKEN).build()
    catconv=ConversationHandler(entry_points=[CommandHandler("addcategory",addcategory)],states={CAT:[MessageHandler(filters.TEXT&~filters.COMMAND,savecategory)]},fallbacks=[CommandHandler("cancel",cancel)],allow_reentry=True)
    quizconv=ConversationHandler(entry_points=[CommandHandler("addquiz",addquiz),CallbackQueryHandler(addquiz,pattern="^addquiz$")],states={
        CAT:[CallbackQueryHandler(pick,pattern=r"^pick:\d+$")],TITLE:[MessageHandler(filters.TEXT&~filters.COMMAND,title)],
        TYPE:[MessageHandler(filters.TEXT&~filters.COMMAND,typ)],QTEXT:[MessageHandler(filters.TEXT&~filters.COMMAND,qtext)],
        QIMAGE:[MessageHandler(filters.TEXT&~filters.COMMAND,qimage)],OPTIONS:[MessageHandler(filters.TEXT&~filters.COMMAND,opts)],
        ANSWER:[MessageHandler(filters.TEXT&~filters.COMMAND,ans)],EXPLAIN:[MessageHandler(filters.TEXT&~filters.COMMAND,explain)],
        MORE:[MessageHandler(filters.TEXT&~filters.COMMAND,more)]},fallbacks=[CommandHandler("cancel",cancel)],allow_reentry=True)
    app.add_handler(CommandHandler("start",start));app.add_handler(CommandHandler("id",userid));app.add_handler(CommandHandler("importquiz",importcmd))
    app.add_handler(catconv);app.add_handler(quizconv)
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,reply_menu_router),group=0)
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,importtext),group=1)
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,admintext),group=2)
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,text_answer),group=3)
    app.add_handler(CallbackQueryHandler(callbacks),group=0)
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":main()