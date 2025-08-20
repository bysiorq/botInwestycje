# ────────────────────────── etapy_bot.py (2025-08) ──────────────────────────
# Bot do prowadzenia etapów inwestycji (elektryka / automatyka smart dom)
# - /start pokazuje listę inwestycji (z możliwością dodania nowej) + wybór daty
# - Wejście w inwestycję → kafelki: Etap 1..6, Prace dodatkowe (7), Czy skończone? (8)
# - Wejście w Etap → kafelki: "Jakie prace należy dokończyć?", "Na ile %", "💾 Zapisz"
# - Edycja dzisiejszych wpisów dla inwestycji (jak w raporcie dziennym)
# - Excel z blokadą, backupami, miesięczne arkusze, opcjonalny upload do SharePoint
# - Eksport całego miesiąca (admin) i użytkownika

import os
import re
import json
import logging
import shutil
import calendar as cal
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet

# ───────────── SharePoint (opcjonalny upload) ─────────────
try:
    from office365.sharepoint.client_context import ClientContext
    from office365.runtime.auth.client_credential import ClientCredential
except ModuleNotFoundError:
    ClientContext = ClientCredential = None

# ───────────── Telegram ─────────────
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from telegram.error import BadRequest

# ───────────── File locking & atomic save ─────────────
import tempfile
import portalocker

# ──────────────────── konfiguracja ────────────────────
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # MUST HAVE
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", 8080))

DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "20"))

# opcjonalne SharePoint
SHAREPOINT_SITE = os.getenv("SHAREPOINT_SITE")
SHAREPOINT_DOC_LIB = os.getenv("SHAREPOINT_DOC_LIB")
SHAREPOINT_CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID")
SHAREPOINT_CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET")

EXCEL_FILE = os.path.join(DATA_DIR, "projects.xlsx")
MAPPING_FILE = os.path.join(DATA_DIR, "project_msgs.json")
PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")
LOCK_FILE = os.path.join(DATA_DIR, "projects.lock")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

# ──────────────────── stałe excela ────────────────────
HEADERS = [
    "ID",              # {user_id}_{dd.mm.YYYY}_{idx}
    "Data",
    "Imię",
    "Inwestycja",
    "Etap",            # "Etap 1"..."Etap 6", "Prace dodatkowe", "Zakończenie"
    "% ukończenia",    # 0..100
    "Do dokończenia",  # tekst
    "Skończone?",      # Tak/Nie/-
]
COLS = {name: i + 1 for i, name in enumerate(HEADERS)}

# ──────────────────── stany konwersacji ────────────────────
(
    DATE_PICK,              # wybór daty
    PROJECT_LIST,           # lista inwestycji
    ADD_PROJECT,            # wpisanie nazwy inwestycji
    SELECT_STAGE,           # wybór etapu w inwestycji
    STAGE_MENU,             # submenu etapu
    TODO_INPUT,             # wpisanie "co dokończyć"
    PERCENT_INPUT,          # wpisanie % ręcznie
    FINISHED_DECIDE,        # tak/nie
    EDIT_PICK_ENTRY,        # wybór wpisu do edycji
    EDIT_PICK_FIELD,        # wybór pola do edycji
    EDIT_VALUE,             # wartość nowa
) = range(11)

# ──────────────────── helpers: excel/lock/backup ────────────────────
def _atomic_save_wb(wb: Workbook, path: str) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    os.close(fd)
    wb.save(tmp_path)
    os.replace(tmp_path, path)

def _with_lock(fn, *args, **kwargs):
    with portalocker.Lock(LOCK_FILE, timeout=30):
        return fn(*args, **kwargs)

def _backup_file():
    if not os.path.exists(EXCEL_FILE):
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"projects_{ts}.xlsx")
    try:
        shutil.copy2(EXCEL_FILE, dst)
    except Exception as e:
        logging.warning("Backup failed: %s", e)
    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("projects_") and f.endswith(".xlsx")])
    if len(files) > BACKUP_KEEP:
        for old in files[: len(files) - BACKUP_KEEP]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except Exception:
                pass

def open_wb() -> Workbook:
    if os.path.exists(EXCEL_FILE):
        return load_workbook(EXCEL_FILE)
    return Workbook()

def month_key_from_date(date_str: str) -> str:
    d = datetime.strptime(date_str, "%d.%m.%Y")
    return f"{d.year:04d}-{d.month:02d}"

def ensure_month_sheet(wb: Workbook, month_key: str) -> Worksheet:
    ws: Optional[Worksheet] = wb[month_key] if month_key in wb.sheetnames else None
    if ws is None:
        ws = wb.create_sheet(title=month_key, index=0)
        ws.append(HEADERS)
        if "Sheet" in wb.sheetnames and wb["Sheet"].max_row == 1 and wb["Sheet"].max_column == 1:
            wb.remove(wb["Sheet"])
    else:
        idx = wb.sheetnames.index(month_key)
        if idx != 0:
            wb.move_sheet(ws, offset=-idx)
    return ws

def get_month_sheet_if_exists(wb: Workbook, month_key: str) -> Optional[Worksheet]:
    return wb[month_key] if month_key in wb.sheetnames else None

def save_entry(user_id: int, date_str: str, name: str, project: str, etap: str,
               percent: Optional[int], todo: str, finished: Optional[str]) -> None:
    """Append nowy wpis."""
    def _save():
        wb = open_wb()
        ws = ensure_month_sheet(wb, month_key_from_date(date_str))

        prefix = f"{user_id}_{date_str}_"
        existing_idxs: List[int] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            rid = str(row[0]) if row and row[0] is not None else ""
            if rid.startswith(prefix):
                try:
                    existing_idxs.append(int(rid.split("_")[-1]))
                except Exception:
                    pass
        next_idx = (max(existing_idxs) + 1) if existing_idxs else 1

        ws.append([
            f"{user_id}_{date_str}_{next_idx}",
            date_str,
            name,
            project,
            etap,
            percent if percent is not None else "",
            todo or "",
            finished or "-",
        ])
        _backup_file()
        _atomic_save_wb(wb, EXCEL_FILE)
    _with_lock(_save)
    _maybe_upload_sharepoint()

def read_entries_for_day_project(user_id: int, date_str: str, project: str) -> List[Dict[str, str]]:
    if not os.path.exists(EXCEL_FILE):
        return []
    def _read():
        wb = load_workbook(EXCEL_FILE)
        ws = get_month_sheet_if_exists(wb, month_key_from_date(date_str))
        if not ws:
            return []
        prefix = f"{user_id}_{date_str}_"
        out = []
        for row in ws.iter_rows(min_row=2, values_only=False):
            rid = str(row[0].value) if row and row[0] is not None else ""
            if rid and rid.startswith(prefix) and (row[COLS["Inwestycja"] - 1].value or "") == project:
                out.append({
                    "rid": rid,
                    "row": row[0].row,
                    "date": row[COLS["Data"] - 1].value,
                    "name": row[COLS["Imię"] - 1].value,
                    "project": row[COLS["Inwestycja"] - 1].value,
                    "etap": row[COLS["Etap"] - 1].value,
                    "percent": row[COLS["% ukończenia"] - 1].value or "",
                    "todo": row[COLS["Do dokończenia"] - 1].value or "",
                    "finished": row[COLS["Skończone?"] - 1].value or "-",
                })
        # sort by idx in RID
        out.sort(key=lambda e: int(e["rid"].split("_")[-1]))
        return out
    return _with_lock(_read)

def update_entry_field(date_str: str, rid: str, field: str, new_value: str) -> None:
    def _upd():
        wb = load_workbook(EXCEL_FILE)
        ws = ensure_month_sheet(wb, month_key_from_date(date_str))
        col_name_map = {
            "etap": "Etap",
            "percent": "% ukończenia",
            "todo": "Do dokończenia",
            "finished": "Skończone?",
        }
        target_col = COLS[col_name_map[field]]
        target_row = None
        for row in ws.iter_rows(min_row=2, values_only=False):
            if str(row[0].value) == rid:
                target_row = row[0].row
                break
        if not target_row:
            raise RuntimeError("Nie znaleziono wiersza do edycji.")
        ws.cell(row=target_row, column=target_col, value=new_value)
        _backup_file()
        _atomic_save_wb(wb, EXCEL_FILE)
    _with_lock(_upd)
    _maybe_upload_sharepoint()

def _maybe_upload_sharepoint() -> None:
    if all([ClientContext, SHAREPOINT_SITE, SHAREPOINT_DOC_LIB, SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET]):
        try:
            ctx = ClientContext(SHAREPOINT_SITE).with_credentials(
                ClientCredential(SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET)
            )
            folder = ctx.web.get_folder_by_server_relative_url(SHAREPOINT_DOC_LIB)
            with open(EXCEL_FILE, "rb") as f:
                folder.upload_file(os.path.basename(EXCEL_FILE), f).execute_query()
        except Exception as e:
            logging.warning("SharePoint upload failed: %s", e)

# ──────────────────── helpers: projekty (lista) ────────────────────
def load_projects() -> List[Dict[str, str]]:
    if os.path.exists(PROJECTS_FILE):
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            try:
                arr = json.load(f)
                if isinstance(arr, list):
                    return arr
            except Exception:
                pass
    return []

def save_projects(projects: List[Dict[str, str]]) -> None:
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)

def add_project(name: str) -> None:
    def _upd():
        projects = load_projects()
        names = {p["name"] for p in projects}
        if name not in names:
            projects.insert(0, {"name": name, "created_at": datetime.now().isoformat(), "active": True})
            save_projects(projects)
    _with_lock(_upd)

# ──────────────────── helpers: Telegram (sticky/safe_answer/UI) ────────────────────
async def sticky_set(update_or_ctx, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    chat = update_or_ctx.effective_chat if isinstance(update_or_ctx, Update) else None
    chat_id = chat.id if chat else update_or_ctx.callback_query.message.chat.id
    sticky_id = context.user_data.get("sticky_id")
    if sticky_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=sticky_id, text=text, reply_markup=reply_markup)
            return
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
        except Exception:
            pass
    m = await context.bot.send_message(chat_id, text, reply_markup=reply_markup)
    context.user_data["sticky_id"] = m.message_id

async def sticky_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    sticky_id = context.user_data.get("sticky_id")
    if sticky_id:
        try:
            await context.bot.delete_message(chat_id, sticky_id)
        except Exception:
            pass
        context.user_data.pop("sticky_id", None)

async def safe_answer(q):
    try:
        await q.answer()
    except BadRequest:
        pass
    except Exception:
        pass

def today_str() -> str:
    return datetime.now().strftime("%d.%m.%Y")

def to_ddmmyyyy(d: date) -> str:
    return d.strftime("%d.%m.%Y")

def month_kb(year: int, month: int) -> InlineKeyboardMarkup:
    month_name = cal.month_name[month]
    days = cal.monthcalendar(year, month)
    rows = []
    rows.append([InlineKeyboardButton(f"{month_name} {year}", callback_data="noop")])
    rows.append([InlineKeyboardButton(x, callback_data="noop") for x in ["Pn","Wt","Śr","Cz","Pt","So","Nd"]])
    for week in days:
        r = []
        for d in week:
            if d == 0:
                r.append(InlineKeyboardButton(" ", callback_data="noop"))
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
    return InlineKeyboardMarkup(rows)

def build_projects_menu(date_str: str) -> InlineKeyboardMarkup:
    projects = load_projects()
    rows = [[InlineKeyboardButton(f"📅 Data: {date_str}", callback_data="change_date")]]
    if projects:
        for i, p in enumerate(projects):
            if p.get("active", True):
                rows.append([InlineKeyboardButton(p["name"], callback_data=f"proj:{i}")])
    rows.append([InlineKeyboardButton("➕ Dodaj inwestycję", callback_data="add_project")])
    rows.append([InlineKeyboardButton("📥 Eksport", callback_data="export"),
                 InlineKeyboardButton("📥 Mój eksport", callback_data="myexport")])
    return InlineKeyboardMarkup(rows)

def build_project_panel(project_name: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Etap 1", callback_data="stage:Etap 1"),
         InlineKeyboardButton("Etap 2", callback_data="stage:Etap 2")],
        [InlineKeyboardButton("Etap 3", callback_data="stage:Etap 3"),
         InlineKeyboardButton("Etap 4", callback_data="stage:Etap 4")],
        [InlineKeyboardButton("Etap 5", callback_data="stage:Etap 5"),
         InlineKeyboardButton("Etap 6", callback_data="stage:Etap 6")],
        [InlineKeyboardButton("Prace dodatkowe (7)", callback_data="stage:Prace dodatkowe")],
        [InlineKeyboardButton("✅ Czy inwestycja skończona?", callback_data="finished")],
        [InlineKeyboardButton("📝 Edytuj dzisiejsze wpisy", callback_data="edit_today")],
        [InlineKeyboardButton("↩️ Powrót", callback_data="back_projects")],
    ]
    return InlineKeyboardMarkup(rows)

def build_stage_menu(etap: str, percent: Optional[int], todo: Optional[str]) -> InlineKeyboardMarkup:
    shown_percent = "-" if percent is None else f"{percent}%"
    shown_todo = "-" if not todo else (todo if len(todo) <= 40 else todo[:37] + "…")
    rows = [
        [InlineKeyboardButton(f"🔧 Do dokończenia: {shown_todo}", callback_data="set_todo")],
        [InlineKeyboardButton(f"📊 Na ile %: {shown_percent}", callback_data="set_percent")],
        [InlineKeyboardButton("💾 Zapisz wpis", callback_data="save_stage")],
        [InlineKeyboardButton("↩️ Powrót", callback_data="back_project")],
    ]
    return InlineKeyboardMarkup(rows)

def build_percent_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("0%", callback_data="pct:0"),
         InlineKeyboardButton("25%", callback_data="pct:25"),
         InlineKeyboardButton("50%", callback_data="pct:50")],
        [InlineKeyboardButton("75%", callback_data="pct:75"),
         InlineKeyboardButton("90%", callback_data="pct:90"),
         InlineKeyboardButton("100%", callback_data="pct:100")],
        [InlineKeyboardButton("✍️ Wpisz ręcznie", callback_data="pct:manual")],
        [InlineKeyboardButton("↩️ Wróć", callback_data="pct:back")],
    ]
    return InlineKeyboardMarkup(rows)

def format_summary(entries: List[Dict[str, str]], date_str: str, project: str, name: str) -> str:
    lines = [f"🏗️ Inwestycja: {project}", f"📅 Data: {date_str}", f"👤 Imię: {name}", ""]
    if not entries:
        lines.append("Brak wpisów.")
        return "\n".join(lines)
    for i, e in enumerate(entries, start=1):
        lines.extend([
            f"#{i} — {e['etap']}",
            f"   📊 %: {e['percent'] if e['percent'] != '' else '-'}",
            f"   🔧 Do dokończenia: {e['todo'] if e['todo'] else '-'}",
            f"   ✅ Skończone?: {e['finished'] or '-'}",
            "",
        ])
    return "\n".join(lines)

def load_mapping() -> Dict[str, int]:
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_mapping(mapping: Dict[str, int]) -> None:
    with open(MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f)

# ──────────────────── EXPORT ────────────────────
def export_month(month_key: str, user_id: Optional[int] = None) -> Optional[str]:
    if not os.path.exists(EXCEL_FILE):
        return None
    def _exp() -> Optional[str]:
        wb = load_workbook(EXCEL_FILE)
        if month_key not in wb.sheetnames:
            return None
        ws = wb[month_key]
        out = Workbook()
        wso = out.active
        wso.title = month_key
        wso.append(HEADERS)
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[0]:
                continue
            if user_id and not str(row[0]).startswith(f"{user_id}_"):
                continue
            wso.append(list(row))
        tmpf = os.path.join(DATA_DIR, f"export_{month_key}_{user_id or 'ALL'}.xlsx")
        _atomic_save_wb(out, tmpf)
        return tmpf
    return _with_lock(_exp)

# ──────────────────── HANDLERY ────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    sel_date = today_str()
    context.user_data["date"] = sel_date
    await sticky_set(update, context, "Wybierz inwestycję lub dodaj nową:", build_projects_menu(sel_date))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "Użyj /start: wybierz datę, inwestycję, etap i uzupełnij pola. "
        "Wpisy zapisują się do Excela. Dostępne: eksporty, edycja dzisiejszych wpisów."
    )

# --- zmiana daty (kalendarz) ---
async def change_date_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    now = datetime.now()
    await sticky_set(update, context, "📅 Wybierz datę:", month_kb(now.year, now.month))
    return DATE_PICK

async def calendar_nav_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = q.data
    if data.startswith("cal:"):
        y, m = map(int, data.split(":")[1].split("-"))
        await sticky_set(update, context, "📅 Wybierz datę:", month_kb(y, m))
    elif data.startswith("day:"):
        ds = data.split(":")[1]
        context.user_data["date"] = ds
        await sticky_set(update, context, "Wybierz inwestycję lub dodaj nową:", build_projects_menu(ds))
        return ConversationHandler.END
    return DATE_PICK

# --- lista inwestycji / dodanie nowej ---
async def projects_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = q.data

    if data == "add_project":
        await sticky_set(update, context, "🏷 Podaj nazwę inwestycji (miejscowość/nazwa):\n\n(tekst)")
        return ADD_PROJECT

    if data.startswith("proj:"):
        idx = int(data.split(":")[1])
        projects = load_projects()
        if idx < 0 or idx >= len(projects):
            await sticky_set(update, context, "Nie znaleziono inwestycji.", build_projects_menu(context.user_data.get("date", today_str())))
            return ConversationHandler.END
        proj = projects[idx]["name"]
        context.user_data["project"] = proj
        await sticky_set(update, context, f"🏗️ {proj}\nWybierz etap:", build_project_panel(proj))
        return SELECT_STAGE

    # eksporty
    if data in {"export", "myexport"}:
        # obsłużą dedykowane handlery
        return ConversationHandler.END

    if data == "change_date":
        return await change_date_cb(update, context)

    return ConversationHandler.END

async def add_project_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # kasuj wiadomość użytkownika
    try:
        await update.message.delete()
    except Exception:
        pass
    name = (update.message.text or "").strip()
    if not name:
        await sticky_set(update, context, "Nazwa nie może być pusta. Podaj nazwę inwestycji:")
        return ADD_PROJECT
    add_project(name)
    await sticky_set(update, context, "Dodano. Wybierz inwestycję lub dodaj kolejną:", build_projects_menu(context.user_data.get("date", today_str())))
    return ConversationHandler.END

# --- panel projektu (etapy) ---
async def project_panel_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = q.data
    project = context.user_data.get("project")
    if not project:
        await sticky_set(update, context, "Brak kontekstu inwestycji.", build_projects_menu(context.user_data.get("date", today_str())))
        return ConversationHandler.END

    if data == "back_projects":
        await sticky_set(update, context, "Wybierz inwestycję lub dodaj nową:", build_projects_menu(context.user_data.get("date", today_str())))
        return ConversationHandler.END

    if data == "edit_today":
        entries = read_entries_for_day_project(update.effective_user.id, context.user_data.get("date", today_str()), project)
        if not entries:
            await sticky_set(update, context, "Brak dzisiejszych wpisów dla tej inwestycji.", build_project_panel(project))
            return SELECT_STAGE
        context.user_data["edit_entries"] = entries
        kb = [[InlineKeyboardButton(f"#{i+1} {e['etap']} ({e['percent']}%)", callback_data=f"edit:{i}")] for i, e in enumerate(entries)]
        kb.append([InlineKeyboardButton("↩️ Powrót", callback_data="back_project")])
        await sticky_set(update, context, "Wybierz wpis do edycji:", InlineKeyboardMarkup(kb))
        return EDIT_PICK_ENTRY

    if data == "finished":
        kb = [
            [InlineKeyboardButton("Tak", callback_data="fin:Tak"),
             InlineKeyboardButton("Nie", callback_data="fin:Nie")],
            [InlineKeyboardButton("↩️ Powrót", callback_data="back_project")],
        ]
        await sticky_set(update, context, "Czy inwestycja skończona?", InlineKeyboardMarkup(kb))
        return FINISHED_DECIDE

    if data.startswith("stage:"):
        etap = data.split(":", 1)[1]
        context.user_data["etap"] = etap
        # inicjalne wartości
        context.user_data["stage_todo"] = context.user_data.get("stage_todo", "")
        context.user_data["stage_percent"] = context.user_data.get("stage_percent", None)
        await sticky_set(update, context, f"{project} → {etap}\nUzupełnij:", build_stage_menu(etap, context.user_data["stage_percent"], context.user_data["stage_todo"]))
        return STAGE_MENU

    if data == "back_project":
        await sticky_set(update, context, f"🏗️ {project}\nWybierz etap:", build_project_panel(project))
        return SELECT_STAGE

    return SELECT_STAGE

# --- submenu etapu ---
async def stage_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = q.data
    project = context.user_data.get("project")
    etap = context.user_data.get("etap")

    if data == "set_todo":
        await sticky_set(update, context, "🔧 Wpisz: Jakie prace należy dokończyć?\n\n(tekst)")
        return TODO_INPUT

    if data == "set_percent":
        await sticky_set(update, context, "📊 Wybierz % lub wpisz ręcznie:", build_percent_kb())
        return PERCENT_INPUT

    if data == "save_stage":
        uid = update.effective_user.id
        name = update.effective_user.first_name
        date_str = context.user_data.get("date", today_str())
        percent = context.user_data.get("stage_percent")
        todo = context.user_data.get("stage_todo", "")
        save_entry(uid, date_str, name, project, etap, percent, todo, None)
        # czyścimy bufor etapu (nie resetujemy nazwy projektu)
        context.user_data.pop("stage_todo", None)
        context.user_data.pop("stage_percent", None)

        # pokaż podsumowanie dzisiejszych wpisów dla tej inwestycji
        entries = read_entries_for_day_project(uid, date_str, project)
        summary = format_summary(entries, date_str, project, name)

        await sticky_delete(context, q.message.chat.id)
        msg = await q.message.chat.send_message(summary)
        mapping = load_mapping()
        mapping[f"{uid}_{date_str}_{project}"] = msg.message_id
        save_mapping(mapping)

        # wróć do panelu projektu
        await sticky_set(update, context, f"🏗️ {project}\nWybierz etap:", build_project_panel(project))
        return SELECT_STAGE

    if data == "back_project":
        await sticky_set(update, context, f"🏗️ {project}\nWybierz etap:", build_project_panel(project))
        return SELECT_STAGE

    return STAGE_MENU

async def todo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    txt = (update.message.text or "").strip()
    if not txt:
        await sticky_set(update, context, "Pole nie może być puste. Wpisz co należy dokończyć:")
        return TODO_INPUT
    context.user_data["stage_todo"] = txt
    etap = context.user_data.get("etap")
    await sticky_set(update, context, "Zapisano. Co dalej?", build_stage_menu(etap, context.user_data.get("stage_percent"), txt))
    return STAGE_MENU

async def percent_input_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = q.data

    if data == "pct:back":
        etap = context.user_data.get("etap")
        await sticky_set(update, context, "Wrócono.", build_stage_menu(etap, context.user_data.get("stage_percent"), context.user_data.get("stage_todo")))
        return STAGE_MENU

    if data == "pct:manual":
        await sticky_set(update, context, "Wpisz wartość % (0-100):")
        return PERCENT_INPUT

    if data.startswith("pct:"):
        pct = int(data.split(":")[1])
        context.user_data["stage_percent"] = pct
        etap = context.user_data.get("etap")
        await sticky_set(update, context, "Ustawiono %.", build_stage_menu(etap, pct, context.user_data.get("stage_todo")))
        return STAGE_MENU

    return PERCENT_INPUT

async def percent_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    t = (update.message.text or "").strip()
    if not re.fullmatch(r"\d{1,3}", t):
        await sticky_set(update, context, "Podaj liczbę 0-100:")
        return PERCENT_INPUT
    val = int(t)
    if not (0 <= val <= 100):
        await sticky_set(update, context, "Zakres 0-100. Podaj ponownie:")
        return PERCENT_INPUT
    context.user_data["stage_percent"] = val
    etap = context.user_data.get("etap")
    await sticky_set(update, context, "Ustawiono %.", build_stage_menu(etap, val, context.user_data.get("stage_todo")))
    return STAGE_MENU

# --- zakończenie inwestycji ---
async def finished_decide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    project = context.user_data.get("project")
    if q.data.startswith("fin:"):
        choice = q.data.split(":")[1]
        uid = q.from_user.id
        name = q.from_user.first_name
        date_str = context.user_data.get("date", today_str())
        # zapis "Zakończenie"
        percent = 100 if choice == "Tak" else ""
        save_entry(uid, date_str, name, project, "Zakończenie", percent if percent != "" else None, "", choice)
        entries = read_entries_for_day_project(uid, date_str, project)
        summary = format_summary(entries, date_str, project, name)

        await sticky_delete(context, q.message.chat.id)
        msg = await q.message.chat.send_message(summary)
        mapping = load_mapping()
        mapping[f"{uid}_{date_str}_{project}"] = msg.message_id
        save_mapping(mapping)

        await sticky_set(update, context, f"🏗️ {project}\nWybierz etap:", build_project_panel(project))
        return SELECT_STAGE

    if q.data == "back_project":
        await sticky_set(update, context, f"🏗️ {project}\nWybierz etap:", build_project_panel(project))
        return SELECT_STAGE

    return FINISHED_DECIDE

# --- eksporty ---
async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_arg = None
    if update.callback_query and update.callback_query.data == "export":
        month_arg = month_key_from_date(context.user_data.get("date", today_str()))
    else:
        args = getattr(context, "args", []) or []
        month_arg = args[0] if args else month_key_from_date(today_str())

    if ADMIN_IDS and update.effective_user.id not in ADMIN_IDS:
        await sticky_set(update, context, "Brak uprawnień do eksportu (tylko admini). Użyj /myexport <YYYY-MM>.")
        return ConversationHandler.END

    path = export_month(month_arg)
    if not path:
        await sticky_set(update, context, f"Brak danych dla {month_arg}.")
        return ConversationHandler.END

    with open(path, "rb") as f:
        await update.effective_chat.send_document(f, filename=os.path.basename(path), caption=f"Eksport {month_arg}")
    try:
        os.remove(path)
    except Exception:
        pass
    return ConversationHandler.END

async def myexport_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_arg = None
    if update.callback_query and update.callback_query.data == "myexport":
        month_arg = month_key_from_date(context.user_data.get("date", today_str()))
    else:
        args = getattr(context, "args", []) or []
        month_arg = args[0] if args else month_key_from_date(today_str())

    path = export_month(month_arg, user_id=update.effective_user.id)
    if not path:
        await sticky_set(update, context, f"Brak danych dla {month_arg}.")
        return ConversationHandler.END

    with open(path, "rb") as f:
        await update.effective_chat.send_document(f, filename=os.path.basename(path), caption=f"Mój eksport {month_arg}")
    try:
        os.remove(path)
    except Exception:
        pass
    return ConversationHandler.END

# --- edycja dzisiejszych wpisów ---
async def edit_pick_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = q.data
    if data == "back_project":
        await sticky_set(update, context, f"🏗️ {context.user_data.get('project')}\nWybierz etap:", build_project_panel(context.user_data.get("project")))
        return SELECT_STAGE

    idx = int(data.split(":")[1])
    entries = context.user_data.get("edit_entries", [])
    if idx < 0 or idx >= len(entries):
        await sticky_set(update, context, "Nieprawidłowy wybór.", build_project_panel(context.user_data.get("project")))
        return SELECT_STAGE
    context.user_data["edit_idx"] = idx
    e = entries[idx]
    kb = [
        [InlineKeyboardButton("Etap", callback_data="ef:etap")],
        [InlineKeyboardButton("% ukończenia", callback_data="ef:percent")],
        [InlineKeyboardButton("Do dokończenia", callback_data="ef:todo")],
        [InlineKeyboardButton("Skończone?", callback_data="ef:finished")],
        [InlineKeyboardButton("↩️ Inny wpis", callback_data="back_edit_list")],
    ]
    await sticky_set(update, context, f"Wybrano: #{idx+1} {e['etap']} ({e['percent']}%)\nCo edytować?", InlineKeyboardMarkup(kb))
    return EDIT_PICK_FIELD

async def edit_pick_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = q.data

    if data == "back_edit_list":
        entries = context.user_data.get("edit_entries", [])
        kb = [[InlineKeyboardButton(f"#{i+1} {e['etap']} ({e['percent']}%)", callback_data=f"edit:{i}")] for i, e in enumerate(entries)]
        kb.append([InlineKeyboardButton("↩️ Powrót", callback_data="back_project")])
        await sticky_set(update, context, "Wybierz wpis:", InlineKeyboardMarkup(kb))
        return EDIT_PICK_ENTRY

    field = data.split(":")[1]
    context.user_data["edit_field"] = field

    prompts = {
        "etap": "Podaj nową nazwę etapu (np. Etap 3 / Prace dodatkowe / Zakończenie):",
        "percent": "Podaj nowy % (0-100):",
        "todo": "Podaj nową wartość pola 'Do dokończenia':",
        "finished": "Podaj wartość 'Skończone?' (Tak/Nie/-):",
    }
    await sticky_set(update, context, prompts[field])
    return EDIT_VALUE

async def edit_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    val = (update.message.text or "").strip()
    field = context.user_data.get("edit_field")
    idx = context.user_data.get("edit_idx")
    date_str = context.user_data.get("date", today_str())

    e = context.user_data.get("edit_entries", [])[idx]

    # walidacje
    if field == "percent":
        if not re.fullmatch(r"\d{1,3}", val):
            await sticky_set(update, context, "Wpisz liczbę 0-100:")
            return EDIT_VALUE
        iv = int(val)
        if not (0 <= iv <= 100):
            await sticky_set(update, context, "Zakres 0-100. Spróbuj ponownie:")
            return EDIT_VALUE
    elif field == "finished":
        if val not in {"Tak", "Nie", "-"}:
            await sticky_set(update, context, "Dozwolone: Tak / Nie / - . Wpisz ponownie:")
            return EDIT_VALUE

    try:
        update_entry_field(date_str, e["rid"], field, val)
    except Exception as ex:
        await sticky_set(update, context, f"❌ Błąd zapisu: {ex}")
        return EDIT_VALUE

    # odśwież listę edytowalną
    project = context.user_data.get("project")
    context.user_data["edit_entries"] = read_entries_for_day_project(update.effective_user.id, date_str, project)

    kb = [
        [InlineKeyboardButton("Edytuj inne pole", callback_data=f"edit:{idx}")],
        [InlineKeyboardButton("Edytuj inny wpis", callback_data="back_edit_list")],
        [InlineKeyboardButton("Pokaż podsumowanie i wyjdź", callback_data="finish_edit")],
    ]
    await sticky_set(update, context, "Zmieniono. Co dalej?", InlineKeyboardMarkup(kb))
    return EDIT_PICK_FIELD

async def finish_edit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    project = context.user_data.get("project")
    date_str = context.user_data.get("date", today_str())
    uid = q.from_user.id
    name = q.from_user.first_name

    entries = read_entries_for_day_project(uid, date_str, project)
    summary = format_summary(entries, date_str, project, name)

    await sticky_delete(context, q.message.chat.id)
    msg = await q.message.chat.send_message(summary)
    mapping = load_mapping()
    mapping[f"{uid}_{date_str}_{project}"] = msg.message_id
    save_mapping(mapping)
    context.user_data.clear()
    return ConversationHandler.END

# --- eksport CB via inline ---
async def export_cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = q.data
    if data == "export":
        return await export_handler(update, context)
    if data == "myexport":
        return await myexport_handler(update, context)
    return ConversationHandler.END

# --- error handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, BadRequest) and "query is too old" in str(err).lower():
        return
    logging.exception("Unhandled exception: %s", err)

# ──────────────────── PTB Application ────────────────────
async def on_startup(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Otwórz panel inwestycji"),
        BotCommand("export", "Eksport (admin): /export YYYY-MM"),
        BotCommand("myexport", "Mój eksport: /myexport YYYY-MM"),
        BotCommand("help", "Pomoc"),
    ])

def build_app() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    # komendy
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("export", export_handler))
    app.add_handler(CommandHandler("myexport", myexport_handler))
    app.add_handler(CommandHandler("help", help_cmd))

    # global: kalendarz + eksport inline
    app.add_handler(CallbackQueryHandler(change_date_cb, pattern=r"^change_date$"))
    app.add_handler(CallbackQueryHandler(calendar_nav_cb, pattern=r"^(cal:\d{4}-\d{2}|day:\d{2}\.\d{2}\.\d{4})$"))
    app.add_handler(CallbackQueryHandler(export_cb_router, pattern=r"^(export|myexport)$"))

    # lista inwestycji
    app.add_handler(CallbackQueryHandler(projects_router, pattern=r"^(add_project|proj:\d+|change_date)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_project_input), ADD_PROJECT)

    # panel projektu
    app.add_handler(CallbackQueryHandler(project_panel_router, pattern=r"^(back_projects|edit_today|finished|stage:.*|back_project)$"))

    # submenu etapu
    app.add_handler(CallbackQueryHandler(stage_menu_router, pattern=r"^(set_todo|set_percent|save_stage|back_project)$"))
    app.add_handler(CallbackQueryHandler(percent_input_router, pattern=r"^(pct:(?:\d+|manual|back))$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, todo_input), TODO_INPUT)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, percent_manual), PERCENT_INPUT)

    # zakończenie inwestycji
    app.add_handler(CallbackQueryHandler(finished_decide, pattern=r"^(fin:(Tak|Nie)|back_project)$"))

    # edycja dzisiejszych wpisów
    app.add_handler(CallbackQueryHandler(edit_pick_entry, pattern=r"^edit:\d+$"))
    app.add_handler(CallbackQueryHandler(edit_pick_field, pattern=r"^(ef:(etap|percent|todo|finished)|back_edit_list)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_input), EDIT_VALUE)
    app.add_handler(CallbackQueryHandler(finish_edit_cb, pattern=r"^finish_edit$"))

    app.add_error_handler(error_handler)
    return app

# ──────────────────── main ────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    if not TELEGRAM_TOKEN:
        raise SystemExit("Brak TELEGRAM_TOKEN w env.")

    bot_app = build_app()

    if WEBHOOK_URL:
        bot_app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
        )
    else:
        bot_app.run_polling(allowed_updates=Update.ALL_TYPES)
