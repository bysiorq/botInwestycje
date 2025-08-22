# ────────────────────────── etapy_bot.py (2025-08 • Storage: SQLite, stable sticky UI) ──────────────────────────
# Single-message UI jak BotFather. Stabilne na webhooku:
# • Trwały stan per user w /data/state/<uid>.json (date, project, stage_code, await, sticky_id)
# • Dane biznesowe w SQLite: /data/invest.db (projects, stages)
# • Callbacki niosą stage_code (S1..S7) → brak zależności od ulotnego user_data
# • Każda zmiana zapisuje do DB i od razu renderuje widok. Bez duplikowania wiadomości.
# • Zdjęcia: przechowujemy telegramowe file_id rozdzielone spacją (max ~200/sesję)

import os
import re
import json
import sqlite3
import logging
import calendar as cal
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, ConversationHandler,
)
from telegram.error import BadRequest

# ──────────────────── konfiguracja ────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
if WEBHOOK_URL and not WEBHOOK_URL.startswith("http"):
    WEBHOOK_URL = "https://" + WEBHOOK_URL
PORT = int(os.getenv("PORT", 8080))
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.path.join(DATA_DIR, "invest.db")
STATE_DIR = os.path.join(DATA_DIR, "state")
os.makedirs(STATE_DIR, exist_ok=True)

# Stałe etapy
STAGES = [
    {"code": "S1", "name": "Etap 1"},
    {"code": "S2", "name": "Etap 2"},
    {"code": "S3", "name": "Etap 3"},
    {"code": "S4", "name": "Etap 4"},
    {"code": "S5", "name": "Etap 5"},
    {"code": "S6", "name": "Etap 6"},
    {"code": "S7", "name": "Prace dodatkowe"},
]
CODE2NAME = {x["code"]: x["name"] for x in STAGES}
NAME2CODE = {x["name"]: x["code"] for x in STAGES}

DATE_PICK = 10

# ──────────────────── helpers: czas, stan ────────────────────
def today_str() -> str: return datetime.now().strftime("%d.%m.%Y")
def to_ddmmyyyy(d: date) -> str: return d.strftime("%d.%m.%Y")

def _state_path(uid: int) -> str:
    return os.path.join(STATE_DIR, f"{uid}.json")

def load_user_state(uid: int) -> dict:
    path = _state_path(uid)
    if not os.path.exists(path): return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_user_state(uid: int, data: dict) -> None:
    tmp = _state_path(uid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _state_path(uid))

def sync_in(update_or_ctx, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = (update_or_ctx.effective_user.id if isinstance(update_or_ctx, Update)
           else update_or_ctx.callback_query.from_user.id)
    state = load_user_state(uid)
    if state:
        for k in ["date", "project", "stage_code", "await", "sticky_id"]:
            if k in state:
                context.user_data[k] = state[k]
    return uid

def sync_out(uid: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = {}
    for k in ["date", "project", "stage_code", "await", "sticky_id"]:
        if k in context.user_data:
            data[k] = context.user_data[k]
    save_user_state(uid, data)

# ──────────────────── SQLite ────────────────────
def _conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        finished INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        percent INTEGER,            -- NULL → brak
        to_finish TEXT,             -- "Do dokończenia"
        notes TEXT,                 -- "Notatki"
        finished TEXT,              -- "-" / "✓" (opcjonalnie)
        last_updated TEXT,
        photos TEXT,                -- "fileid fileid ..."
        last_editor TEXT,
        last_editor_id TEXT,
        UNIQUE(project_id, code)
    );
    """)
    conn.commit()
    conn.close()

def list_projects(active_only: bool = True) -> List[Dict[str, str]]:
    conn = _conn(); cur = conn.cursor()
    if active_only:
        cur.execute("SELECT name, active, finished, created_at FROM projects WHERE active=1 ORDER BY created_at ASC;")
    else:
        cur.execute("SELECT name, active, finished, created_at FROM projects ORDER BY active DESC, created_at ASC;")
    out = []
    for r in cur.fetchall():
        out.append({
            "name": r["name"],
            "active": bool(r["active"]),
            "finished": bool(r["finished"]),
            "created": r["created_at"],
        })
    conn.close()
    return out

def _get_project_id(name: str) -> Optional[int]:
    conn = _conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM projects WHERE name=?;", (name,))
    row = cur.fetchone()
    conn.close()
    return row["id"] if row else None

def _ensure_default_stages(pid: int):
    # Dodaj S1..S7 jeśli brak
    conn = _conn(); cur = conn.cursor()
    cur.execute("SELECT code FROM stages WHERE project_id=?;", (pid,))
    have = {r["code"] for r in cur.fetchall()}
    for st in STAGES:
        if st["code"] not in have:
            cur.execute("""
                INSERT INTO stages (project_id, code, name, percent, to_finish, notes, finished, last_updated, photos, last_editor, last_editor_id)
                VALUES (?, ?, ?, NULL, '', '', '-', '', '', '', '');
            """, (pid, st["code"], st["name"]))
    conn.commit(); conn.close()

def add_project(name: str) -> None:
    name = name.strip()
    if not name:
        return
    pid = _get_project_id(name)
    if pid:
        return
    conn = _conn(); cur = conn.cursor()
    cur.execute("INSERT INTO projects(name, active, finished, created_at) VALUES(?, 1, 0, ?);", (name, datetime.now().isoformat()))
    conn.commit(); conn.close()
    pid = _get_project_id(name)
    if pid:
        _ensure_default_stages(pid)

def set_project_active(name: str, active: bool) -> None:
    conn = _conn(); cur = conn.cursor()
    cur.execute("UPDATE projects SET active=? WHERE name=?;", (1 if active else 0, name))
    conn.commit(); conn.close()

def set_project_finished(name: str, finished: bool) -> None:
    conn = _conn(); cur = conn.cursor()
    cur.execute("UPDATE projects SET finished=? WHERE name=?;", (1 if finished else 0, name))
    conn.commit(); conn.close()

def read_stage(project: str, stage_name: str) -> Dict[str, str]:
    pid = _get_project_id(project)
    if not pid:
        add_project(project)
        pid = _get_project_id(project)
    _ensure_default_stages(pid)
    conn = _conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM stages WHERE project_id=? AND name=?;", (pid, stage_name))
    r = cur.fetchone()
    if not r:
        # fallback – utwórz brakujący etap po nazwie
        code = NAME2CODE.get(stage_name, "S?")
        cur.execute("""
            INSERT INTO stages (project_id, code, name, percent, to_finish, notes, finished, last_updated, photos, last_editor, last_editor_id)
            VALUES (?, ?, ?, NULL, '', '', '-', '', '', '', '');
        """, (pid, code, stage_name))
        conn.commit()
        cur.execute("SELECT * FROM stages WHERE project_id=? AND name=?;", (pid, stage_name))
        r = cur.fetchone()
    conn.close()
    return {
        "Stage": r["name"],
        "Percent": ("" if r["percent"] is None else r["percent"]),
        "ToFinish": r["to_finish"] or "",
        "Notes": r["notes"] or "",
        "Finished": r["finished"] or "-",
        "LastUpdated": r["last_updated"] or "",
        "Photos": r["photos"] or "",
        "LastEditor": r["last_editor"] or "",
        "LastEditorId": r["last_editor_id"] or "",
    }

def update_stage(project: str, stage_name: str, updates: Dict[str, str], editor_name: str, editor_id: int) -> None:
    pid = _get_project_id(project)
    if not pid:
        add_project(project)
        pid = _get_project_id(project)
    _ensure_default_stages(pid)
    # mapowanie kolumn
    colmap = {
        "Percent": "percent",
        "ToFinish": "to_finish",
        "Notes": "notes",
        "Finished": "finished",
        "LastUpdated": "last_updated",
        "Photos": "photos",
        "LastEditor": "last_editor",
        "LastEditorId": "last_editor_id",
    }
    sets = []
    vals = []
    for k, v in updates.items():
        if k not in colmap:
            raise ValueError(f"Unsupported field: {k}")
        sets.append(f"{colmap[k]}=?")
        vals.append(v)
    # meta
    sets.extend(["last_updated=?", "last_editor=?", "last_editor_id=?"])
    vals.extend([datetime.now().strftime("%d.%m.%Y %H:%M:%S"), editor_name or "", str(editor_id or "")])
    vals.extend([pid, stage_name])
    sql = f"UPDATE stages SET {', '.join(sets)} WHERE project_id=? AND name=?;"
    conn = _conn(); cur = conn.cursor()
    cur.execute(sql, tuple(vals))
    if cur.rowcount == 0:
        # jeśli brak – wstaw
        code = NAME2CODE.get(stage_name, "S?")
        fields = {"percent": None, "to_finish": "", "notes": "", "finished": "-",
                  "last_updated": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                  "photos": "", "last_editor": editor_name or "", "last_editor_id": str(editor_id or "")}
        for k, v in updates.items():
            fields[colmap[k]] = v
        cur.execute("""
            INSERT INTO stages(project_id, code, name, percent, to_finish, notes, finished, last_updated, photos, last_editor, last_editor_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?);
        """, (pid, code, stage_name, fields["percent"], fields["to_finish"], fields["notes"], fields["finished"],
              fields["last_updated"], fields["photos"], fields["last_editor"], fields["last_editor_id"]))
    conn.commit(); conn.close()

def _percent_preview_for_project(project: str) -> str:
    pid = _get_project_id(project)
    if not pid:
        return "-"
    conn = _conn(); cur = conn.cursor()
    cur.execute("SELECT code, name, percent FROM stages WHERE project_id=?;", (pid,))
    rows = {r["name"]: ( "-" if r["percent"] is None else (f"{int(r['percent'])}%" if str(r["percent"]).isdigit() else str(r["percent"])) ) for r in cur.fetchall()}
    conn.close()
    parts = []
    for st in STAGES:
        parts.append(f"{st['name'].split()[-1]} {rows.get(st['name'], '-')}")
    return " | ".join(parts)

# ──────────────────── UI helpers ────────────────────
async def safe_answer(q, text: Optional[str] = None, show_alert: bool = False):
    try:
        if text is not None: await q.answer(text=text, show_alert=show_alert)
        else: await q.answer()
    except BadRequest:
        pass
    except Exception:
        pass

async def sticky_set(update_or_ctx, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    """Edytuje istniejący panel; nową wiadomość wysyła tylko jeśli edycja jest niemożliwa."""
    chat = update_or_ctx.effective_chat if isinstance(update_or_ctx, Update) else update_or_ctx.callback_query.message.chat
    chat_id = chat.id
    sticky_id = context.user_data.get("sticky_id")
    if sticky_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=sticky_id, text=text,
                reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True
            )
            return
        except BadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                return
            if not any(s in msg for s in [
                "message to edit not found",
                "message identifier is not specified",
                "chat not found",
                "message can't be edited",
            ]):
                # inny błąd – nie próbuj wysyłać nowego
                return
        except Exception:
            return
    m = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)
    context.user_data["sticky_id"] = m.message_id
    try:
        uid = (update_or_ctx.effective_user.id if isinstance(update_or_ctx, Update)
               else update_or_ctx.callback_query.from_user.id)
        sync_out(uid, context)
    except Exception:
        pass

def banner_await(context: ContextTypes.DEFAULT_TYPE) -> str:
    aw = context.user_data.get("await") or {}
    if not aw: return ""
    names = {"project_name": "Nazwa inwestycji", "todo": "Do dokończenia", "notes": "Notatki", "percent": "% ukończenia", "photo": "Zdjęcie"}
    proj = context.user_data.get("project") or ""
    scode = context.user_data.get("stage_code") or ""
    sname = CODE2NAME.get(scode, "")
    where = f" (inwestycja: {proj}" + (f" | {sname}" if sname else "") + ")"
    return f"✍️ *Oczekuję na:* {names.get(aw.get('field'), aw.get('field'))}{where}. Wyślij teraz.\n"

def projects_menu_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    ds = context.user_data.get("date", today_str())
    out = []
    b = banner_await(context); 
    if b: out.append(b)
    out.append(f"🏗️ *Inwestycje*  |  📅 {ds}\n")
    if not list_projects(active_only=True):
        out.append("Brak inwestycji. Dodaj pierwszą 👇")
    return "\n".join(out)

def projects_menu_kb(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    ds = context.user_data.get("date", today_str())
    projs = list_projects(active_only=True)
    aw = context.user_data.get("await") or {}
    adding = (aw.get("mode") == "text" and aw.get("field") == "project_name")
    def mark(lbl, on): return f"{'●' if on else '○'} {lbl}"
    rows = [[InlineKeyboardButton(f"📅 Data: {ds}", callback_data="date:open")]]
    for i, p in enumerate(projs):
        rows.append([InlineKeyboardButton(f"🏗️ {p['name']}", callback_data=f"proj:open:{i}")])
    rows.append([InlineKeyboardButton(mark("➕ Dodaj inwestycję", adding), callback_data="proj:add")])
    rows.append([InlineKeyboardButton("🗄 Archiwum", callback_data="proj:arch")])
    return InlineKeyboardMarkup(rows)

def project_panel_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    proj = context.user_data.get("project")
    out = []
    b = banner_await(context)
    if b: out.append(b)
    out.append(f"🏗️ *{proj}*")
    out.append(f"📊 Postęp etapów: {_percent_preview_for_project(proj)}\n")
    out.append("👇 Wybierz etap. Otwarte zadania:")
    for st in STAGES:
        data = read_stage(proj, st["name"])
        tf = (data["ToFinish"] or "").strip()
        p = data["Percent"]
        ptxt = f" (📊 {int(p)}%)" if str(p).isdigit() else ""
        if tf:
            prev = tf if len(tf) <= 60 else tf[:57] + "…"
            out.append(f"• {st['name']}{ptxt}: 🔧 {prev}")
    return "\n".join(out)

def project_panel_kb(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Etap 1", callback_data="stage:open:S1"),
         InlineKeyboardButton("Etap 2", callback_data="stage:open:S2")],
        [InlineKeyboardButton("Etap 3", callback_data="stage:open:S3"),
         InlineKeyboardButton("Etap 4", callback_data="stage:open:S4")],
        [InlineKeyboardButton("Etap 5", callback_data="stage:open:S5"),
         InlineKeyboardButton("Etap 6", callback_data="stage:open:S6")],
        [InlineKeyboardButton("Prace dodatkowe", callback_data="stage:open:S7")],
        [InlineKeyboardButton("✅ Oznacz zakończoną", callback_data="proj:finish"),
         InlineKeyboardButton("📦 Archiwizuj/Przywróć", callback_data="proj:toggle_active")],
        [InlineKeyboardButton("↩️ Wstecz", callback_data="nav:home")],
    ]
    return InlineKeyboardMarkup(rows)

def stage_panel_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    proj = context.user_data.get("project")
    scode = context.user_data.get("stage_code")
    sname = CODE2NAME.get(scode, "")
    data = read_stage(proj, sname)
    out = []
    b = banner_await(context)
    if b: out.append(b)
    out.extend([
        f"🏗️ *{proj}*  →  {sname}",
        "",
        f"📊 % ukończenia: {data['Percent'] if data['Percent'] != '' else '-'}",
        f"🔧 Do dokończenia:\n{data['ToFinish'] or '-'}",
        f"📝 Notatki:\n{data['Notes'] or '-'}",
        f"🖼 Zdjęcia: {len((data['Photos'] or '').split()) if (data['Photos'] or '').strip() else 0}",
        f"⏱ Ostatnia zmiana: {data['LastUpdated'] or '-'}  |  👤 {data['LastEditor'] or '-'}",
        "",
        "Wybierz działanie poniżej 👇",
    ])
    return "\n".join(out)

def stage_panel_kb(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    aw = context.user_data.get("await") or {}
    active_key = None
    if aw:
        if aw.get("mode") == "text" and aw.get("field") in {"todo", "notes", "percent"}: active_key = aw.get("field")
        if aw.get("mode") == "photo": active_key = "photo"
    scode = context.user_data.get("stage_code", "")
    def mark(lbl, key): return f"{'●' if active_key == key else '○'} {lbl}"
    rows = [
        [InlineKeyboardButton(mark("🔧 Do dokończenia", "todo"), callback_data="stage:set:todo"),
         InlineKeyboardButton(mark("📝 Notatki", "notes"), callback_data="stage:set:notes")],
        [InlineKeyboardButton(mark("📊 % (0/25/50/75/90/100)", "percent"), callback_data=f"stage:set:percent:{scode}"),
        InlineKeyboardButton(mark("📸 Dodaj zdjęcie", "photo"), callback_data="stage:add_photo")],
        [InlineKeyboardButton("🧹 Wyczyść Do dokończenia", callback_data=f"stage:clear:todo:{scode}"),
         InlineKeyboardButton("🧹 Wyczyść Notatki", callback_data=f"stage:clear:notes:{scode}")],
        [InlineKeyboardButton("💾 Zapisz zmiany", callback_data=f"stage:save:{scode}")],
        [InlineKeyboardButton("↩️ Wstecz", callback_data="proj:back")],
    ]
    return InlineKeyboardMarkup(rows)

def percent_kb(stage_code: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("0%", callback_data=f"pct:{stage_code}:0"),
         InlineKeyboardButton("25%", callback_data=f"pct:{stage_code}:25"),
         InlineKeyboardButton("50%", callback_data=f"pct:{stage_code}:50")],
        [InlineKeyboardButton("75%", callback_data=f"pct:{stage_code}:75"),
         InlineKeyboardButton("90%", callback_data=f"pct:{stage_code}:90"),
         InlineKeyboardButton("100%", callback_data=f"pct:{stage_code}:100")],
        [InlineKeyboardButton("✍️ Wpisz ręcznie", callback_data=f"pct:{stage_code}:manual")],
        [InlineKeyboardButton("↩️ Wróć", callback_data="pct:back")],
    ]
    return InlineKeyboardMarkup(rows)

def month_kb(year: int, month: int) -> InlineKeyboardMarkup:
    month_name = cal.month_name[month]; days = cal.monthcalendar(year, month)
    rows = [[InlineKeyboardButton(f"{month_name} {year}", callback_data="noop")]]
    rows.append([InlineKeyboardButton(x, callback_data="noop") for x in ["Pn","Wt","Śr","Cz","Pt","So","Nd"]])
    for week in days:
        r = []
        for d in week:
            if d == 0: r.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                ds = to_ddmmyyyy(date(year, month, d))
                r.append(InlineKeyboardButton(str(d), callback_data=f"day:{ds}"))
        rows.append(r)
    prev_month = (date(year, month, 1) - timedelta(days=1))
    next_month = (date(year, month, cal.monthrange(year, month)[1]) + timedelta(days=1))
    rows.append([
        InlineKeyboardButton("« Poprzedni", callback_data=f"cal:{prev_month.year}-{prev_month.month:02d}"),
        InlineKeyboardButton("Dziś", callback_data=f"day:{today_str()}"),
        InlineKeyboardButton("Następny »", callback_data=f"cal:{next_month.year}-{next_month.month:02d}"),
    ])
    rows.append([InlineKeyboardButton("↩️ Wstecz", callback_data="nav:home")])
    return InlineKeyboardMarkup(rows)

# ──────────────────── renderery ────────────────────
async def render_home(update_or_ctx, context: ContextTypes.DEFAULT_TYPE):
    sync_in(update_or_ctx, context)
    await sticky_set(update_or_ctx, context, projects_menu_text(context), projects_menu_kb(context))

async def render_project(update_or_ctx, context: ContextTypes.DEFAULT_TYPE):
    sync_in(update_or_ctx, context)
    await sticky_set(update_or_ctx, context, project_panel_text(context), project_panel_kb(context))

async def render_stage(update_or_ctx, context: ContextTypes.DEFAULT_TYPE):
    sync_in(update_or_ctx, context)
    await sticky_set(update_or_ctx, context, stage_panel_text(context), stage_panel_kb(context))

# ──────────────────── Handlers ────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context)
    # NIE czyścimy sticky_id
    sticky_id = context.user_data.get("sticky_id")
    context.user_data.clear()
    if sticky_id:
        context.user_data["sticky_id"] = sticky_id
    context.user_data["date"] = today_str()
    sync_out(uid, context)
    await render_home(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context); sync_out(uid, context)
    text = (
        "🤖 *Pomoc – Inwestycje*\n"
        "• /start – lista inwestycji, dodawanie, archiwum.\n"
        "• W projekcie → Etap → edytuj pola. Zmiany zapisują się do *SQLite* i od razu widać je w panelu.\n"
        "• Kropki ○/● pokazują, że czekam na tekst/zdjęcie.\n"
        "• Stan sesji (w tym sticky_id) jest trwały.\n"
    )
    await sticky_set(update, context, text, InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Wstecz", callback_data="nav:home")]]))

# --- data ---
async def date_open_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context); await safe_answer(update.callback_query)
    now = datetime.now()
    await sticky_set(update, context, "📅 Wybierz datę (informacyjnie):", month_kb(now.year, now.month))
    sync_out(uid, context)
    return DATE_PICK

async def calendar_nav_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context); q = update.callback_query; await safe_answer(q); data = q.data
    if data.startswith("cal:"):
        y, m = map(int, data.split(":")[1].split("-"))
        await sticky_set(update, context, "📅 Wybierz datę:", month_kb(y, m)); sync_out(uid, context); return DATE_PICK
    if data.startswith("day:"):
        ds = data.split(":")[1]; context.user_data["date"] = ds; sync_out(uid, context)
        await render_home(update, context); return ConversationHandler.END
    sync_out(uid, context); return DATE_PICK

# --- projekty / archiwum ---
def _render_archive_kb(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    projs = list_projects(active_only=False)
    context.user_data["arch_names"] = [p["name"] for p in projs]
    rows = []
    for i, p in enumerate(projs):
        state = "🟢" if p["active"] else "⚪️"
        rows.append([InlineKeyboardButton(f"{state} {p['name']}", callback_data=f"arch:tog:{i}")])
    rows.append([InlineKeyboardButton("↩️ Wstecz", callback_data="nav:home")])
    return InlineKeyboardMarkup(rows)

async def projects_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context); q = update.callback_query; await safe_answer(q); data = q.data

    if data == "nav:home":
        sync_out(uid, context); await render_home(update, context); return

    if data == "proj:add":
        context.user_data["await"] = {"mode": "text", "field": "project_name"}; sync_out(uid, context)
        await render_home(update, context); return

    if data == "proj:arch":
        await sticky_set(update, context, "🗄 *Archiwum / Aktywne* (kliknij aby przełączyć):", _render_archive_kb(context)); sync_out(uid, context); return

    if data.startswith("arch:tog:"):
        idx = int(data.split(":")[2]); names = context.user_data.get("arch_names", [])
        if 0 <= idx < len(names):
            # toggle active
            allp = {p["name"]: p for p in list_projects(active_only=False)}
            cur = allp.get(names[idx])
            if cur: set_project_active(names[idx], not cur["active"])
        await sticky_set(update, context, "🗄 *Archiwum / Aktywne* (kliknij aby przełączyć):", _render_archive_kb(context)); sync_out(uid, context); return

    if data.startswith("proj:open:"):
        idx = int(data.split(":")[2]); projs = list_projects(active_only=True)
        if 0 <= idx < len(projs):
            context.user_data["project"] = projs[idx]["name"]; context.user_data.pop("await", None); sync_out(uid, context)
            await render_project(update, context)
        else:
            await render_home(update, context)
        return

    if data == "proj:finish":
        proj = context.user_data.get("project")
        if not proj: sync_out(uid, context); await render_home(update, context); return
        set_project_finished(proj, True)
        await sticky_set(update, context, f"🎉 *{proj}* oznaczono jako zakończoną. 💪", InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Wróć", callback_data="nav:home")]]))
        sync_out(uid, context); return

    if data == "proj:toggle_active":
        proj = context.user_data.get("project")
        if proj:
            allp = {p["name"]: p for p in list_projects(active_only=False)}
            cur = allp.get(proj)
            if cur: set_project_active(proj, not cur["active"])
        sync_out(uid, context); await render_home(update, context); return

# --- panel etapu ---
async def stage_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context); q = update.callback_query; await safe_answer(q); data = q.data
    proj = context.user_data.get("project")

    if data.startswith("stage:open:"):
        scode = data.split(":")[2]
        if scode not in CODE2NAME: sync_out(uid, context); await render_project(update, context); return
        context.user_data["stage_code"] = scode; context.user_data.pop("await", None); sync_out(uid, context)
        await render_stage(update, context); return

    if data == "stage:set:todo":
        context.user_data["await"] = {"mode": "text", "field": "todo"}; sync_out(uid, context)
        await render_stage(update, context); return
    if data == "stage:set:notes":
        context.user_data["await"] = {"mode": "text", "field": "notes"}; sync_out(uid, context)
        await render_stage(update, context); return

    if data.startswith("stage:set:percent:"):
        scode = data.split(":")[3]
        await sticky_set(update, context, "📊 Ustaw % ukończenia:", percent_kb(scode)); sync_out(uid, context); return

    if data.startswith("stage:clear:"):
        _, _, field, scode = data.split(":")
        sname = CODE2NAME.get(scode, "")
        if proj and sname:
            update_stage(proj, sname, {"ToFinish" if field == "todo" else "Notes": ""}, q.from_user.first_name, q.from_user.id)
        await safe_answer(q, "Wyczyszczono ✅"); sync_out(uid, context); await render_stage(update, context); return

    if data.startswith("stage:save:"):
        scode = data.split(":")[2]; sname = CODE2NAME.get(scode, "")
        if proj and sname:
            update_stage(proj, sname, {}, q.from_user.first_name, q.from_user.id)
        await safe_answer(q, "Zapisano ✅"); sync_out(uid, context); await render_stage(update, context); return

    if data == "proj:back":
        context.user_data.pop("await", None); sync_out(uid, context); await render_project(update, context); return

    if data == "stage:add_photo":
        context.user_data["await"] = {"mode": "photo", "field": "photo"}; sync_out(uid, context)
        await render_stage(update, context); return

# --- procenty ---
async def percent_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context); q = update.callback_query; await safe_answer(q)
    data = q.data
    proj = context.user_data.get("project")

    if data == "pct:back":
        sync_out(uid, context); await render_stage(update, context); return

    if data.startswith("pct:"):
        try:
            _, scode, val = data.split(":")
        except ValueError:
            return
        sname = CODE2NAME.get(scode, "")
        if val == "manual":
            context.user_data["await"] = {"mode": "text", "field": "percent"}; context.user_data["stage_code"] = scode; sync_out(uid, context)
            await render_stage(update, context); return
        pct = None
        try: pct = int(val)
        except Exception: pass
        if proj and sname and pct is not None:
            update_stage(proj, sname, {"Percent": pct}, q.from_user.first_name, q.from_user.id)
            await safe_answer(q, "Ustawiono % ✅")
        sync_out(uid, context); await render_stage(update, context); return

# --- tekstowe wejścia ---
async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context)
    txt = (update.message.text or "").strip()
    try: await update.message.delete()
    except Exception: pass

    aw = context.user_data.get("await") or {}
    mode = aw.get("mode"); field = aw.get("field")

    if mode == "text" and field == "project_name":
        if txt: add_project(txt)
        context.user_data.pop("await", None); sync_out(uid, context)
        await render_home(update, context); return

    if mode != "text":
        sync_out(uid, context); return

    proj = context.user_data.get("project")
    scode = context.user_data.get("stage_code")
    sname = CODE2NAME.get(scode or "", "")
    if not proj or not sname:
        context.user_data.pop("await", None); sync_out(uid, context)
        await render_home(update, context); return

    if field == "todo":
        update_stage(proj, sname, {"ToFinish": txt}, update.effective_user.first_name, update.effective_user.id)
    elif field == "notes":
        update_stage(proj, sname, {"Notes": txt}, update.effective_user.first_name, update.effective_user.id)
    elif field == "percent":
        if not re.fullmatch(r"\d{1,3}", txt):
            await sticky_set(update, context, "📊 Wpisz liczbę 0-100:", percent_kb(scode)); return
        val = int(txt)
        if not (0 <= val <= 100):
            await sticky_set(update, context, "📊 Zakres 0-100:", percent_kb(scode)); return
        update_stage(proj, sname, {"Percent": val}, update.effective_user.first_name, update.effective_user.id)

    context.user_data.pop("await", None); sync_out(uid, context)
    await render_stage(update, context)

# --- zdjęcia ---
async def photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = sync_in(update, context)
    aw = context.user_data.get("await") or {}
    if aw.get("mode") != "photo":
        sync_out(uid, context); return
    proj = context.user_data.get("project")
    scode = context.user_data.get("stage_code"); sname = CODE2NAME.get(scode or "", "")
    if not proj or not sname:
        context.user_data.pop("await", None); sync_out(uid, context); return
    try:
        file_id = update.message.photo[-1].file_id
    except Exception:
        try: await update.message.delete()
        except Exception: pass
        return
    data = read_stage(proj, sname)
    photos = (data["Photos"] or "").split(); photos.append(file_id); photos = photos[-200:]
    update_stage(proj, sname, {"Photos": " ".join(photos)}, update.effective_user.first_name, update.effective_user.id)
    try: await update.message.delete()
    except Exception: pass
    context.user_data.pop("await", None); sync_out(uid, context)
    await render_stage(update, context)

# --- cancel / errors ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if context.user_data.get("sticky_id"):
            await context.bot.delete_message(update.effective_chat.id, context.user_data.get("sticky_id"))
    except Exception:
        pass
    await update.effective_chat.send_message("Anulowano.")
    context.user_data.clear()
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, BadRequest) and ("query is too old" in str(err).lower() or "query is not found" in str(err).lower()):
        return
    logging.exception("Unhandled exception: %s", err)

# ──────────────────── PTB Application ────────────────────
async def on_startup(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Otwórz panel inwestycji"),
        BotCommand("help", "Pomoc"),
    ])

def build_app() -> Application:
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))

    # data
    app.add_handler(CallbackQueryHandler(date_open_cb, pattern=r"^date:open$"))
    app.add_handler(CallbackQueryHandler(calendar_nav_cb, pattern=r"^(cal:\d{4}-\d{2}|day:\d{2}\.\d{2}\.\d{4})$"))

    # projekty / archiwum
    app.add_handler(CallbackQueryHandler(projects_router, pattern=r"^(nav:home|proj:add|proj:arch|arch:tog:\d+|proj:open:\d+|proj:finish|proj:toggle_active)$"))

    # panel etapu + procenty
    app.add_handler(CallbackQueryHandler(stage_router, pattern=r"^(stage:open:S[1-7]|stage:set:(todo|notes)|stage:set:percent:S[1-7]|stage:clear:(todo|notes):S[1-7]|stage:save:S[1-7]|proj:back|stage:add_photo)$"))
    app.add_handler(CallbackQueryHandler(percent_cb, pattern=r"^(pct:(S[1-7]):(\d+|manual)|pct:back)$"))

    # wejścia
    app.add_handler(MessageHandler(filters.PHOTO, photo_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))

    app.add_error_handler(error_handler)
    return app

# ──────────────────── main ────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    if not TELEGRAM_TOKEN:
        raise SystemExit("Brak TELEGRAM_TOKEN w env.")
    bot_app = build_app()
    if WEBHOOK_URL:
        bot_app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TELEGRAM_TOKEN, webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    else:
        bot_app.run_polling(allowed_updates=Update.ALL_TYPES)
