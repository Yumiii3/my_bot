"""
Telegram-бот записи клиентов к частному мастеру (парикмахер / массажист / репетитор).
Стек: Python 3.11+, pyTelegramBotAPI (telebot), PostgreSQL (Neon.tech).
Запуск: 
TOKEN="ТВОЙ_ТОКЕН_ОТ_BOTFATHER" ADMIN_ID="ТВОЙ_TELEGRAM_ID" python main.py   (или через файл .env рядом с main.py)
"""

import os
import re
import html
import time
import logging
import threading
import schedule
import pytz
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

import telebot
from telebot import types
last_portfolio_msg = {}

# ==========================================================
#                    0. ЗАГРУЗКА НАСТРОЕК
# ==========================================================

def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as exc:
        print(f"[WARN] Не удалось прочитать {path}: {exc}")


load_env_file()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("booking-bot")


def env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Переменная %s='%s' не является числом, использую %s", name, raw, default)
        return default


TOKEN = os.getenv("TOKEN", "ВАШ_ТОКЕН")
if not TOKEN or TOKEN == "ВАШ_ТОКЕН":
    raise SystemExit(
        "\n❌ ОШИБКА ЗАПУСКА: не задан TOKEN.\n"
        "   Добавь переменную окружения TOKEN на Render или в файл .env\n"
    )

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise SystemExit(
        "\n❌ ОШИБКА ЗАПУСКА: не задан DATABASE_URL.\n"
        "   Получи строку подключения на neon.tech и добавь её в переменные Render.\n"
    )

ADMIN_ID = env_int("ADMIN_ID", 0)
if ADMIN_ID == 0:
    logger.warning("ADMIN_ID не задан — админ-панель и уведомления мастеру будут отключены!")

TZ_OFFSET = env_int("TZ_OFFSET", 3)
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))
MASTER_NAME = os.getenv("MASTER_NAME", "")
MASTER_CONTACT = os.getenv("MASTER_CONTACT", "").strip()

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True)

BTN_BOOK = "📅 Записаться"
BTN_MY = "📋 Мои записи"
BTN_CANCEL = "❌ Отменить запись"
BTN_ADMIN = "⚙️ Админ-панель"
BTN_PORTFOLIO = "📁 Портфолио"
BTN_FAQ = "💬 Вопросы и связь"

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MAX_MSG_LEN = 3500

STUDIO_NAME = (os.getenv("STUDIO_NAME") or "SHARLI").strip() or "SHARLI"
STUDIO_ADDRESS = (os.getenv("STUDIO_ADDRESS") or "Махачкала, просп. Имама Шамиля, 93").strip()
STUDIO_HOURS = (os.getenv("STUDIO_HOURS") or "ежедневно 9:00–19:00").strip()
STUDIO_CONTACT = (os.getenv("STUDIO_CONTACT") or "@sharli_beauty").strip()
COVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cover.jpg")


# ==========================================================
#                    1. РАБОТА С БАЗОЙ PostgreSQL
# ==========================================================

@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q_all(sql: str, params: tuple = ()) -> list:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    except Exception as exc:
        logger.exception("Ошибка q_all: %s | SQL=%s", exc, sql)
        return []


def q_one(sql: str, params: tuple = ()):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return cur.fetchone()
    except Exception as exc:
        logger.exception("Ошибка q_one: %s | SQL=%s", exc, sql)
        return None


def q_exec(sql: str, params: tuple = ()) -> int:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount
    except Exception as exc:
        logger.exception("Ошибка q_exec: %s | SQL=%s", exc, sql)
        return -1


def init_db() -> None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS services (
                        id       SERIAL PRIMARY KEY,
                        name     TEXT    NOT NULL,
                        duration INTEGER NOT NULL DEFAULT 60,
                        price    INTEGER NOT NULL DEFAULT 0,
                        break_min INTEGER NOT NULL DEFAULT 0
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS slots (
                        id          SERIAL PRIMARY KEY,
                        service_id  INTEGER NOT NULL,
                        date        TEXT    NOT NULL,
                        time        TEXT    NOT NULL,
                        is_booked   INTEGER NOT NULL DEFAULT 0,
                        client_id   BIGINT,
                        client_name TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS clients (
                        user_id  BIGINT PRIMARY KEY,
                        name     TEXT,
                        username TEXT,
                        notes    TEXT NOT NULL DEFAULT ''
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS portfolio (
                        id SERIAL PRIMARY KEY,
                        master_id BIGINT NOT NULL,
                        file_id TEXT NOT NULL,
                        caption TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reminders (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        text TEXT NOT NULL,
                        remind_time TEXT NOT NULL,
                        is_sent INTEGER NOT NULL DEFAULT 0
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS config (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS questions (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        question_text TEXT NOT NULL,
                        answer_text TEXT,
                        is_answered INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                """)
                cur.execute("""
                    INSERT INTO config (key, value) VALUES ('faq_text',
                        '💬 Вопросы и связь\n\nНапишите прямо в этот чат, и мастер ответит, как только освободится.\n\n<b>Часто спрашивают:</b>\n• можно ли с дизайном? — да\n• можно ли раньше/позже? — если есть окно, бот покажет\n• можно ли перенести запись? — да, через «Мои записи» или сообщением мастеру')
                    ON CONFLICT (key) DO NOTHING
                """)
                for table, column in (
                    ("slots", "client_id"),
                    ("clients", "user_id"),
                    ("portfolio", "master_id"),
                    ("reminders", "user_id"),
                    ("questions", "user_id"),
                ):
                    cur.execute("SAVEPOINT migr_id")
                    try:
                        cur.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT")
                        cur.execute("RELEASE SAVEPOINT migr_id")
                        logger.info("Миграция %s.%s → BIGINT: ок", table, column)
                    except Exception as exc:
                        cur.execute("ROLLBACK TO SAVEPOINT migr_id")
                        logger.warning("Миграция %s.%s → BIGINT: %s", table, column, exc)
                cur.execute("SAVEPOINT migr_break")
                try:
                    cur.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS break_min INTEGER NOT NULL DEFAULT 0")
                    cur.execute("RELEASE SAVEPOINT migr_break")
                    logger.info("Миграция services.break_min: ок")
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT migr_break")
                    logger.warning("Миграция services.break_min: %s", exc)
                cur.execute("SAVEPOINT migr_notes")
                try:
                    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT ''")
                    cur.execute("RELEASE SAVEPOINT migr_notes")
                    logger.info("Миграция clients.notes: ок")
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT migr_notes")
                    logger.warning("Миграция clients.notes: %s", exc)
                cur.execute("DELETE FROM reminders WHERE is_sent = 0 AND length(remind_time) <> 16")
                if cur.rowcount:
                    logger.info("Очищено старых напоминаний: %s", cur.rowcount)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_slots_lookup ON slots (service_id, date, is_booked)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_slots_client ON slots (client_id, is_booked)")
                try:
                    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_slots_unique ON slots (service_id, date, time)")
                except Exception as exc:
                    logger.warning("Не удалось создать уникальный индекс: %s", exc)
        logger.info("База данных PostgreSQL готова")
    except Exception as exc:
        logger.exception("КРИТИЧЕСКАЯ ошибка init_db: %s", exc)
        alert_admin_error("Инициализация базы", exc)
        raise


# ==========================================================
#                    2. УТИЛИТЫ
# ==========================================================

def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def time_str() -> str:
    return now_local().strftime("%H:%M")


def hm_to_min(hm) -> int:
    parts = str(hm or "0:0").split(":")
    return int(parts[0]) * 60 + int(parts[1])


def fmt_date(date_iso: str) -> str:
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        return f"{d.strftime('%d.%m.%Y')} ({WEEKDAYS[d.weekday()]})"
    except Exception:
        return str(date_iso)


def fmt_date_short(date_iso: str) -> str:
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        return f"{d.strftime('%d.%m')} {WEEKDAYS[d.weekday()]}"
    except Exception:
        return str(date_iso)


def fmt_price(value) -> str:
    try:
        amount = int(value or 0)
    except (TypeError, ValueError):
        amount = 0
    return f"{amount:,}".replace(",", " ") + " ₽"


def plural_ru(n, one: str, few: str, many: str) -> str:
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        n = 0
    tail = n % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def btn_text(value, limit: int = 60) -> str:
    return str(value or "").replace("\n", " ").replace("\r", " ").strip()[:limit]


def booking_status(date_iso: str, time_hm: str) -> str:
    try:
        moment = datetime.strptime(f"{date_iso} {time_hm}", "%Y-%m-%d %H:%M")
        delta = moment - now_local().replace(tzinfo=None)
        if timedelta(0) <= delta <= timedelta(hours=24):
            return "⏳ Скоро"
    except (TypeError, ValueError):
        pass
    return "✅ Подтверждена"


def date_button_label(date_iso: str, count: int) -> str:
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return f"{date_iso} · {count}"
    tomorrow = (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    if date_iso == today_str():
        prefix = "Сегодня"
    elif date_iso == tomorrow:
        prefix = "Завтра"
    else:
        prefix = f"{WEEKDAYS[d.weekday()]}, {d.strftime('%d.%m')}"
    return f"{prefix} · {count}"


def parse_date_input(text: str):
    raw = (text or "").strip().lower().replace("/", ".")
    today = now_local().date()
    if raw in ("сегодня", "today"):
        return today.isoformat()
    if raw in ("завтра", "tomorrow"):
        return (today + timedelta(days=1)).isoformat()
    if raw in ("послезавтра",):
        return (today + timedelta(days=2)).isoformat()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if fmt == "%d.%m":
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
        return parsed.isoformat()
    return None


def parse_times_input(text: str):
    parts = [p for p in re.split(r"[,;\s]+", (text or "").strip()) if p]
    good, bad = [], []
    for part in parts:
        candidate = part.replace(".", ":").replace("-", ":")
        try:
            good.append(datetime.strptime(candidate, "%H:%M").strftime("%H:%M"))
        except ValueError:
            bad.append(part)
    unique = list(dict.fromkeys(good))
    return unique, bad


MAX_GEN_SLOTS = 400


def norm_time(part: str):
    p = (part or "").strip().replace(".", ":")
    if p.isdigit():
        p = f"{int(p):02d}:00" if len(p) <= 2 else p
    try:
        return datetime.strptime(p, "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def parse_hours_range(text: str):
    raw = (text or "").strip().lower().replace(" ", "")
    for dash in ("—", "–", "..", "до"):
        raw = raw.replace(dash, "-")
    parts = [p for p in raw.split("-") if p]
    if len(parts) != 2:
        return None
    start, end = norm_time(parts[0]), norm_time(parts[1])
    if not start or not end or start >= end:
        return None
    return start, end


def generate_slots(service_id: int, start_hm: str, end_hm: str,
                   break_min: int, days: int, only_weekdays: bool = False):
    service = get_service(service_id)
    duration = int((service or {}).get("duration") or 0)
    if duration <= 0:
        return 0, 0, 0, False
    step_min = duration + max(0, int(break_min))
    t_start = datetime.strptime(start_hm, "%H:%M")
    t_end = datetime.strptime(end_hm, "%H:%M")
    today = now_local().date()
    now_hm = time_str()

    rows, days_used, capped = [], 0, False
    for offset in range(days):
        day = today + timedelta(days=offset)
        if only_weekdays and day.weekday() >= 5:
            continue
        day_iso = day.isoformat()
        added_today = False
        times = []
        cur = t_start
        while cur + timedelta(minutes=duration) <= t_end:
            times.append(cur.strftime("%H:%M"))
            cur += timedelta(minutes=step_min)
        final_slot = t_end - timedelta(minutes=duration)
        if final_slot >= t_start:
            final_hm = final_slot.strftime("%H:%M")
            if final_hm not in times:
                times.append(final_hm)
        for hm in times:
            if day == today and hm <= now_hm:
                continue
            if len(rows) >= MAX_GEN_SLOTS:
                capped = True
                break
            rows.append((service_id, day_iso, hm))
            added_today = True
        if added_today:
            days_used += 1
        if capped:
            break

    if not rows:
        return 0, 0, 0, capped

    dates = sorted({r[1] for r in rows})
    marks = ",".join(["%s"] * len(dates))
    existing = {
        (r["date"], r["time"])
        for r in q_all(f"SELECT date, time FROM slots WHERE service_id = %s AND date IN ({marks})",
                       (service_id, *dates))
    }
    fresh = [r for r in rows if (r[1], r[2]) not in existing]
    skipped = len(rows) - len(fresh)
    if not fresh:
        return 0, skipped, days_used, capped
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO slots (service_id, date, time, is_booked) VALUES (%s,%s,%s,0) "
                    "ON CONFLICT DO NOTHING",
                    fresh,
                )
        logger.info("Генерация: сервис=%s длительность=%s перерыв=%s создано=%s пропущено=%s",
                    service_id, duration, break_min, len(fresh), skipped)
        return len(fresh), skipped, days_used, capped
    except Exception as exc:
        logger.exception("Ошибка генерации слотов: %s", exc)
        return 0, skipped, days_used, capped


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def safe_send(chat_id: int, text: str, reply_markup=None):
    try:
        if len(text) <= MAX_MSG_LEN:
            return bot.send_message(chat_id, text, reply_markup=reply_markup)
        chunks, buf = [], ""
        for line in text.split("\n"):
            if len(buf) + len(line) + 1 > MAX_MSG_LEN:
                chunks.append(buf)
                buf = ""
            buf += line + "\n"
        if buf:
            chunks.append(buf)
        last = None
        for i, chunk in enumerate(chunks):
            last = bot.send_message(chat_id, chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)
        return last
    except Exception as exc:
        logger.error("Не удалось отправить сообщение в %s: %s", chat_id, exc)
        return None


_screens = {}


def _remember_screen(chat_id: int, message) -> None:
    if message is not None:
        _screens[chat_id] = message.message_id


def send_screen(chat_id: int, text: str, reply_markup=None):
    if isinstance(reply_markup, (types.ReplyKeyboardMarkup, types.ReplyKeyboardRemove, types.ForceReply)):
        message = safe_send(chat_id, text, reply_markup)
        _remember_screen(chat_id, message)
        return message
    message_id = _screens.get(chat_id)
    if message_id:
        try:
            message = bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            _remember_screen(chat_id, message)
            return message
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return None
            logger.debug("Не удалось изменить экран (%s), отправляю новое сообщение", exc)
    message = safe_send(chat_id, text, reply_markup)
    _remember_screen(chat_id, message)
    return message


def safe_edit(call, text: str, reply_markup=None):
    try:
        message = bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=reply_markup,
        )
        _remember_screen(call.message.chat.id, message)
        return message
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return None
        logger.debug("edit_message_text не удался (%s), отправляю новое сообщение", exc)
        message = safe_send(call.message.chat.id, text, reply_markup)
        _remember_screen(call.message.chat.id, message)
        return message


def notify_admin(text: str, reply_markup=None) -> None:
    if ADMIN_ID:
        safe_send(ADMIN_ID, text, reply_markup)


_error_alert_times: dict = {}


def alert_admin_error(context: str, exc: Exception = None) -> None:
    if not ADMIN_ID:
        return
    now_ts = time.time()
    if now_ts - _error_alert_times.get("__global__", 0) < 60:
        return
    if now_ts - _error_alert_times.get(context, 0) < 300:
        return
    _error_alert_times["__global__"] = now_ts
    _error_alert_times[context] = now_ts
    if len(_error_alert_times) > 200:
        _error_alert_times.clear()
        _error_alert_times["__global__"] = now_ts
        _error_alert_times[context] = now_ts
    detail = ""
    if exc is not None:
        detail = f"{type(exc).__name__}: {exc}"[:700]
        for secret in (TOKEN, DATABASE_URL):
            if secret:
                detail = detail.replace(secret, "***")
        detail = re.sub(r"(?i)(password|pwd)=\S+", r"\1=***", detail)
        detail = re.sub(r"postgres(?:ql)?://\S+", "postgres://***", detail)
    text = (f"⚠️ <b>Ошибка в боте</b>\n\n"
            f"🕒 {now_local().strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"📍 {esc(context)}")
    if detail:
        text += f"\n\n<code>{esc(detail)}</code>"
    safe_send(ADMIN_ID, text)


# ==========================================================
#                3. FSM
# ==========================================================

user_states: dict = {}
STATE_LOCK = threading.Lock()


def set_state(user_id: int, step: str, **data) -> None:
    with STATE_LOCK:
        user_states[user_id] = {"step": step, "data": data}


def get_state(user_id: int):
    with STATE_LOCK:
        return user_states.get(user_id)


def clear_state(user_id: int) -> None:
    with STATE_LOCK:
        user_states.pop(user_id, None)


# ==========================================================
#                    4. КЛАВИАТУРЫ
# ==========================================================

def main_menu(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row(BTN_BOOK)
    kb.row(BTN_MY, BTN_PORTFOLIO)
    kb.row(BTN_FAQ)
    if is_admin(user_id):
        kb.row(BTN_ADMIN)
    return kb


def services_kb(prefix: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    rows = q_all("SELECT id, name, duration, price FROM services ORDER BY id")
    for row in rows:
        name = btn_text(row["name"], 40)
        kb.add(types.InlineKeyboardButton(
            text=f"{name} · {fmt_price(row['price'])}",
            callback_data=f"{prefix}:{row['id']}",
        ))
    return kb


def admin_menu_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("📅 Записи", callback_data="adm:bookings"),
        types.InlineKeyboardButton("🗓 Расписание", callback_data="adm:schedule"),
    )
    kb.row(
        types.InlineKeyboardButton("🧾 Услуги", callback_data="adm:services"),
        types.InlineKeyboardButton("👥 Клиенты", callback_data="adm:clients"),
    )
    kb.row(
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm:stats"),
        types.InlineKeyboardButton("⚙️ Ещё", callback_data="adm:more"),
    )
    return kb


def admin_bookings_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("📆 На сегодня", callback_data="adm:today"),
        types.InlineKeyboardButton("📚 Все записи", callback_data="adm:all"),
    )
    kb.row(types.InlineKeyboardButton("🕳 Свободные слоты", callback_data="adm:free"))
    kb.row(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
    return kb


def admin_schedule_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("➕ Добавить слот", callback_data="adm:add_slot"),
        types.InlineKeyboardButton("📅 На неделю", callback_data="adm:gen_week"),
    )
    kb.row(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
    return kb


def admin_services_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("➕ Добавить", callback_data="adm:add_service"),
        types.InlineKeyboardButton("❌ Удалить", callback_data="adm:del_service"),
    )
    kb.row(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
    return kb


def admin_more_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📖 Команды", callback_data="adm:commands"))
    kb.add(types.InlineKeyboardButton("❓ Текст FAQ", callback_data="adm:faq"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
    return kb


def show_services_screen(chat_id: int, user_id: int) -> None:
    kb = services_kb("svc")
    if not kb.keyboard:
        safe_send(chat_id, "😔 Пока нет доступных услуг. Загляни чуть позже.", main_menu(user_id))
        return
    kb.row(types.InlineKeyboardButton("◀️ В меню", callback_data="back_to_main"))
    send_screen(
        chat_id,
        "Шаг 1 из 4\n\n<b>Выбери услугу</b>\nПокажу свободное время и цену.",
        kb,
    )


def bookings_view(user_id: int):
    rows = my_bookings(user_id)
    if not rows:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📅 Записаться", callback_data="start:book"))
        kb.add(types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_main"))
        return (
            "📋 <b>Мои записи</b>\n\n"
            "Пока пусто. Выбери время для себя — это займёт меньше минуты.",
            kb,
        )

    lines = ["📋 <b>Мои записи</b>", f"Активных: <b>{len(rows)}</b>", ""]
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        service = esc(row["service_name"] or "услуга")
        lines.append(f"<b>{service}</b> · {fmt_price(row['price'])}")
        lines.append(f"📅 {fmt_date(row['date'])} · {esc(row['time'])}")
        lines.append(booking_status(row["date"], row["time"]))
        lines.append("")
        label = f"🚫 Отменить · {fmt_date_short(row['date'])} {row['time']}"
        kb.add(types.InlineKeyboardButton(btn_text(label), callback_data=f"cxl:{row['id']}"))
    kb.add(types.InlineKeyboardButton("➕ Записаться ещё", callback_data="start:book"))
    kb.add(types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_main"))
    return "\n".join(lines).strip(), kb


def show_my_bookings(chat_id: int, user_id: int, call=None) -> None:
    text, kb = bookings_view(user_id)
    if call is not None:
        safe_edit(call, text, kb)
    else:
        send_screen(chat_id, text, kb)


def calendar_link(slot) -> str:
    title = f"Запись: {slot['service_name'] or 'услуга'}"
    try:
        start_dt = datetime.strptime(f"{slot['date']} {slot['time']}", "%Y-%m-%d %H:%M")
        duration = int(slot["duration"] or 60)
    except (TypeError, ValueError):
        return "https://calendar.google.com/calendar/render"
    end_dt = start_dt + timedelta(minutes=duration)
    dates = f"{start_dt.strftime('%Y%m%dT%H%M%S')}/{end_dt.strftime('%Y%m%dT%H%M%S')}"
    return (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={urllib.parse.quote(title)}&dates={dates}"
    )


# ==========================================================
#              5. ЗАПРОСЫ К БД
# ==========================================================

def get_service(service_id: int):
    return q_one("SELECT id, name, duration, price, break_min FROM services WHERE id = %s", (service_id,))


def get_slot(slot_id: int):
    return q_one("""
        SELECT s.id, s.service_id, s.date, s.time, s.is_booked, s.client_id, s.client_name,
               sv.name AS service_name, sv.price AS price, sv.duration AS duration
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.id = %s
    """, (slot_id,))


def _windows_for(rows, length: int) -> list:
    windows = []
    for row in rows:
        start = hm_to_min(row["time"])
        windows.append((start, start + length))
    return windows


def _time_free(hm: str, length: int, windows: list) -> bool:
    start = hm_to_min(hm)
    end = start + length
    for win_start, win_end in windows:
        if start < win_end and win_start < end:
            return False
    return True


def free_dates(service_id: int) -> list:
    service = get_service(service_id)
    if not service:
        return []
    length = int(service["duration"] or 0) + int(service["break_min"] or 0)
    if length <= 0:
        return []
    free = q_all("""
        SELECT date, time
        FROM slots
        WHERE service_id = %s AND is_booked = 0
          AND (date > %s OR (date = %s AND time >= %s))
        ORDER BY date, time
    """, (service_id, today_str(), today_str(), time_str()))
    booked = q_all("""
        SELECT date, time
        FROM slots
        WHERE service_id = %s AND is_booked = 1 AND date >= %s
    """, (service_id, today_str()))
    busy = {}
    for row in booked:
        busy.setdefault(row["date"], []).append(row)
    counts = {}
    for row in free:
        day = row["date"]
        if _time_free(row["time"], length, _windows_for(busy.get(day, []), length)):
            counts[day] = counts.get(day, 0) + 1
    result = [{"date": day, "cnt": cnt} for day, cnt in sorted(counts.items())]
    return result[:30]


def free_slots_on_date(service_id: int, date_iso: str) -> list:
    service = get_service(service_id)
    if not service:
        return []
    length = int(service["duration"] or 0) + int(service["break_min"] or 0)
    if length <= 0:
        return []
    rows = q_all("""
        SELECT id, time
        FROM slots
        WHERE service_id = %s AND date = %s AND is_booked = 0
          AND (date > %s OR (date = %s AND time >= %s))
        ORDER BY time
    """, (service_id, date_iso, today_str(), today_str(), time_str()))
    booked = q_all("""
        SELECT time FROM slots
        WHERE service_id = %s AND date = %s AND is_booked = 1
    """, (service_id, date_iso))
    windows = _windows_for(booked, length)
    return [row for row in rows if _time_free(row["time"], length, windows)]


def my_bookings(user_id: int) -> list:
    return q_all("""
        SELECT s.id, s.date, s.time, sv.name AS service_name, sv.price AS price, sv.duration AS duration
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1 AND s.client_id = %s
          AND (s.date > %s OR (s.date = %s AND s.time >= %s))
        ORDER BY s.date, s.time
    """, (user_id, today_str(), today_str(), time_str()))


def all_bookings(only_today: bool = False) -> list:
    if only_today:
        return q_all("""
            SELECT s.id, s.date, s.time, s.client_id, s.client_name,
                   sv.name AS service_name, sv.price AS price
            FROM slots s LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 1 AND s.date = %s
            ORDER BY s.time
        """, (today_str(),))
    return q_all("""
        SELECT s.id, s.date, s.time, s.client_id, s.client_name,
               sv.name AS service_name, sv.price AS price
        FROM slots s LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1
          AND (s.date > %s OR (s.date = %s AND s.time >= %s))
        ORDER BY s.date, s.time
        LIMIT 100
    """, (today_str(), today_str(), time_str()))


def book_slot(slot_id: int, user_id: int, client_name: str):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.service_id, s.date, sv.duration, sv.break_min
                    FROM slots s
                    JOIN services sv ON sv.id = s.service_id
                    WHERE s.id = %s
                """, (slot_id,))
                slot = cur.fetchone()
                if not slot:
                    logger.warning("Бронь %s: слот не найден", slot_id)
                    return False

                cur.execute("""
                    SELECT id, time, is_booked, client_id
                    FROM slots
                    WHERE service_id = %s AND date = %s
                    ORDER BY id
                    FOR UPDATE
                """, (slot["service_id"], slot["date"]))
                rows = cur.fetchall()
                current = next((row for row in rows if row["id"] == slot_id), None)
                if current is None:
                    logger.warning("Бронь %s: слот не найден", slot_id)
                    return False

                if current["is_booked"]:
                    if current["client_id"] == user_id:
                        logger.info("Бронь %s: слот уже принадлежит %s (дубль)", slot_id, user_id)
                        return True
                    if current["client_id"] is None:
                        logger.warning("Бронь %s: слот был занят без клиента, освобождаю "
                                       "и записываю %s", slot_id, user_id)
                        cur.execute("""
                            UPDATE slots
                            SET client_id = %s, client_name = %s
                            WHERE id = %s
                        """, (user_id, client_name[:100], slot_id))
                        return cur.rowcount == 1
                    logger.warning("Бронь %s: слот занят пользователем %s",
                                   slot_id, current["client_id"])
                    return False

                length = int(slot["duration"] or 0) + int(slot["break_min"] or 0)
                start = hm_to_min(current["time"])
                end = start + length
                for row in rows:
                    if row["id"] == slot_id or not row["is_booked"]:
                        continue
                    busy_start = hm_to_min(row["time"])
                    if start < busy_start + length and busy_start < end:
                        logger.warning("Бронь %s: пересечение с записью %s (%s)",
                                       slot_id, row["id"], row["time"])
                        return False

                cur.execute("""
                    UPDATE slots
                    SET is_booked = 1, client_id = %s, client_name = %s
                    WHERE id = %s AND is_booked = 0
                """, (user_id, client_name[:100], slot_id))
                return cur.rowcount == 1
    except Exception as exc:
        logger.exception("Ошибка бронирования слота %s: %s", slot_id, exc)
        alert_admin_error(f"Бронирование слота {slot_id}", exc)
        return None


def unbook_slot(slot_id: int, user_id: int, force: bool = False) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if force:
                    cur.execute(
                        "UPDATE slots SET is_booked = 0, client_id = NULL, client_name = NULL "
                        "WHERE id = %s AND is_booked = 1", (slot_id,))
                else:
                    cur.execute(
                        "UPDATE slots SET is_booked = 0, client_id = NULL, client_name = NULL "
                        "WHERE id = %s AND is_booked = 1 AND client_id = %s", (slot_id, user_id))
                return cur.rowcount == 1
    except Exception as exc:
        logger.exception("Ошибка отмены слота %s: %s", slot_id, exc)
        return False


def save_client(user_id: int, name: str, username: str = "") -> None:
    q_exec("""
        INSERT INTO clients (user_id, name, username) VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET name = excluded.name, username = excluded.username
    """, (user_id, (name or "").strip()[:100], (username or "")[:64]))


def get_client_name(user_id: int):
    row = q_one("SELECT name FROM clients WHERE user_id = %s", (user_id,))
    return row["name"] if row and row["name"] else None


def get_client(user_id: int):
    return q_one("SELECT user_id, name, username, notes FROM clients WHERE user_id = %s", (user_id,))


def set_client_note(user_id: int, note: str) -> None:
    q_exec("""
        INSERT INTO clients (user_id, notes) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET notes = excluded.notes
    """, (user_id, (note or "").strip()[:1000]))


def client_stats(user_id: int):
    today = today_str()
    return q_one("""
        SELECT COUNT(*) AS visits,
               MAX(s.date) FILTER (WHERE s.date <= %s) AS last_date,
               MIN(s.date) FILTER (WHERE s.date >= %s) AS next_date,
               COALESCE(SUM(sv.price), 0) AS total
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1 AND s.client_id = %s
    """, (today, today, user_id))


def client_card_text(client) -> str:
    user_id = client["user_id"]
    name = (client["name"] or "").strip() or "Клиент"
    username = f"@{client['username']}" if client.get("username") else "—"
    stats = client_stats(user_id) or {}
    visits = stats.get("visits") or 0
    last_date = fmt_date(stats["last_date"]) if stats.get("last_date") else "—"
    next_date = fmt_date(stats["next_date"]) if stats.get("next_date") else "—"
    total = stats.get("total") or 0
    note = (client.get("notes") or "").strip()
    return (f"👤 <b>{esc(name)}</b>\n"
            f"{esc(username)} · id {user_id}\n\n"
            f"📅 Записей: <b>{visits}</b>\n"
            f"🕓 Последний визит: {esc(last_date)}\n"
            f"⏭ Ближайшая запись: {esc(next_date)}\n"
            f"💰 Всего: <b>{total} ₽</b>\n\n"
            f"🗒 <b>Заметка</b>\n{esc(note) if note else '—'}")


def client_card_kb(user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✏️ Изменить заметку", callback_data=f"clnote:{user_id}"))
    kb.add(types.InlineKeyboardButton("◀️ К клиентам", callback_data="adm:clients"))
    kb.add(types.InlineKeyboardButton("⚙️ Меню", callback_data="adm:menu"))
    return kb


def show_client_card(call, user_id: int) -> None:
    client = get_client(user_id)
    if not client:
        safe_edit(call, "🤔 Клиент не найден.", admin_menu_kb())
        return
    safe_edit(call, client_card_text(client), client_card_kb(user_id))


def clients_list_kb(rows) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        name = (row["name"] or "").strip() or f"Клиент {row['user_id']}"
        mark = " 📝" if (row["notes"] or "").strip() else ""
        label = f"{name}{mark}".replace("\n", " ").replace("\r", " ")[:60]
        kb.add(types.InlineKeyboardButton(label, callback_data=f"cl:{row['user_id']}"))
    kb.add(types.InlineKeyboardButton("🔎 Найти по имени", callback_data="adm:clients_find"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
    return kb


# ==========================================================
#                 6. КОМАНДЫ И ГЛАВНОЕ МЕНЮ
# ==========================================================

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    clear_state(message.from_user.id)
    master_line = f"👩 Мастер: <b>{esc(MASTER_NAME)}</b>\n" if MASTER_NAME else ""
    text = (
        f"<b>{esc(STUDIO_NAME)}</b>\n"
        "Красота в каждой детали.\n\n"
        "Помогу выбрать услугу и свободное время — без звонков и переписок.\n\n"
        f"📍 {esc(STUDIO_ADDRESS)}\n"
        f"🕰 {esc(STUDIO_HOURS)}\n"
        f"{master_line}"
        f"💬 {esc(STUDIO_CONTACT)}\n\n"
        "Выбери, с чего начнём 👇"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton("📅 Записаться", callback_data="start:book"))
    kb.row(
        types.InlineKeyboardButton("📁 Портфолио", callback_data="start:portfolio"),
        types.InlineKeyboardButton("💬 Вопросы", callback_data="faq_start"),
    )

    photo_sent = False
    if os.path.exists(COVER_PATH):
        try:
            with open(COVER_PATH, "rb") as photo:
                bot.send_photo(
                    message.chat.id,
                    photo,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            photo_sent = True
        except Exception as exc:
            logger.warning("Не удалось отправить обложку: %s", exc)
    if not photo_sent:
        safe_send(message.chat.id, text, kb)

    send_screen(
        message.chat.id,
        "Меню всегда под рукой 👇",
        main_menu(message.from_user.id),
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel_state(message):
    clear_state(message.from_user.id)
    safe_send(message.chat.id, "🔄 Действие отменено. Выбери пункт меню 👇", main_menu(message.from_user.id))


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    clear_state(message.from_user.id)
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Раздел доступен только мастеру.", main_menu(message.from_user.id))
        return
    safe_send(message.chat.id, "⚙️ <b>Админ-панель</b>\n\nВыбери раздел.", admin_menu_kb())


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    safe_send(message.chat.id, f"🆔 Твой Telegram ID: <code>{message.from_user.id}</code>")


@bot.message_handler(commands=["edit_faq"])
def cmd_edit_faq(message):
    if not is_admin(message.from_user.id):
        return
    row = q_one("SELECT value FROM config WHERE key='faq_text'")
    current_text = row["value"] if row else "Текст не найден."
    safe_send(message.chat.id, f"📖 <b>Текущий текст FAQ:</b>\n\n{current_text}\n\n✏️ Отправь новый текст для FAQ (одним сообщением):")
    bot.register_next_step_handler(message, save_new_faq)


def save_new_faq(message):
    new_text = message.text.strip()
    if len(new_text) < 10:
        safe_send(message.chat.id, "Текст слишком короткий. Попробуй ещё раз через /edit_faq.")
        return
    q_exec("UPDATE config SET value=%s WHERE key='faq_text'", (new_text,))
    safe_send(message.chat.id, "✅ Текст FAQ обновлён!")


@bot.message_handler(commands=["my_q"])
def cmd_my_questions(message):
    if not is_admin(message.from_user.id):
        return
    rows = q_all("SELECT id, user_id, question_text, created_at FROM questions WHERE is_answered=0 ORDER BY created_at ASC")
    if not rows:
        safe_send(message.chat.id, "🎉 У тебя пока нет новых вопросов от клиентов.")
        return

    lines = ["📋 <b>Новые вопросы от клиентов:</b>\n"]
    kb = types.InlineKeyboardMarkup()
    for index, row in enumerate(rows, start=1):
        lines.append(f"🕒 {row['created_at']}\n💬 {row['question_text']}\n")
        kb.add(types.InlineKeyboardButton(
            f"✍️ Ответить (вопрос №{index})",
            callback_data=f"ans_q:{row['id']}"
        ))
    safe_send(message.chat.id, "\n".join(lines), reply_markup=kb)


@bot.message_handler(commands=["info"])
def cmd_info(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Эта команда только для мастера.")
        return
    text = (
        "📖 <b>Шпаргалка по командам мастера</b>\n\n"
        "🔹 <b>/start</b> — перезапустить бота и обновить меню\n"
        "🔹 <b>/add_photo</b> — добавить фото в портфолио\n"
        "🔹 <b>/del_photo</b> — открыть портфолио для удаления фото\n"
        "🔹 <b>/broadcast</b> — сделать рассылку всем клиентам\n"
        "🔹 <b>/stats</b> — посмотреть статистику записей\n"
        "🔹 <b>/reminders</b> — посмотреть активные напоминания\n"
        "🔹 <b>/myid</b> — узнать свой Telegram ID\n"
        "🔹 <b>/admin</b> — открыть админ-панель\n"
        "🔹 <b>/info</b> — показать эту шпаргалку\n\n"
        "🔹 <b>/edit_faq</b> — изменить текст «Вопросы и связь»\n\n"
        "🔹 <b>/my_q</b> — посмотреть все новые вопросы\n\n"
        "📍 <i>Все команды работают только для мастера.</i>"
    )
    safe_send(message.chat.id, text, main_menu(message.from_user.id))


@bot.message_handler(content_types=["photo"], func=lambda m: is_admin(m.from_user.id))
def catch_file_id(message):
    file_id = message.photo[-1].file_id
    safe_send(message.chat.id, f"<b>file_id получен:</b>\n<code>{file_id}</code>")


@bot.message_handler(func=lambda m: m.text == BTN_ADMIN)
def on_admin_button(message):
    clear_state(message.from_user.id)
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Раздел доступен только мастеру.", main_menu(message.from_user.id))
        return
    safe_send(message.chat.id, "⚙️ <b>Админ-панель</b>\n\nВыбери раздел.", admin_menu_kb())


@bot.message_handler(func=lambda m: m.text == BTN_BOOK)
def on_book(message):
    clear_state(message.from_user.id)
    show_services_screen(message.chat.id, message.from_user.id)


@bot.message_handler(func=lambda m: m.text == BTN_MY)
def on_my(message):
    clear_state(message.from_user.id)
    show_my_bookings(message.chat.id, message.from_user.id)


@bot.message_handler(func=lambda m: m.text == BTN_CANCEL)
def on_cancel_booking(message):
    clear_state(message.from_user.id)
    rows = my_bookings(message.from_user.id)
    if not rows:
        safe_send(
            message.chat.id,
            "📭 Нет записей, которые можно отменить.\n\nСамое время выбрать время для себя.",
            main_menu(message.from_user.id),
        )
        return
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        label = f"🚫 {fmt_date_short(row['date'])} {row['time']} · {row['service_name'] or 'услуга'}"
        kb.add(types.InlineKeyboardButton(
            text=btn_text(label),
            callback_data=f"cxl:{row['id']}",
        ))
    kb.add(types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_main"))
    send_screen(message.chat.id, "❌ <b>Какую запись отменить?</b>", kb)


@bot.message_handler(func=lambda m: m.text == BTN_FAQ)
def faq_contact(message):
    row = q_one("SELECT value FROM config WHERE key='faq_text'")
    faq_text = row["value"] if row else "Текст FAQ не найден."
    safe_send(message.chat.id, faq_text)
    msg = bot.send_message(message.chat.id, "✍️ Если у тебя есть вопрос, напиши его прямо сейчас. Мастер ответит, как только освободится.")
    bot.register_next_step_handler(msg, save_client_question)


def save_client_question(message):
    question = message.text.strip()
    if len(question) < 3:
        safe_send(message.chat.id, "❓ Вопрос слишком короткий. Напиши подробнее.")
        return
    q_exec("INSERT INTO questions (user_id, question_text, created_at) VALUES (%s, %s, %s)",
           (message.from_user.id, question, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    safe_send(message.chat.id, "✅ Твой вопрос отправлен мастеру!", reply_markup=main_menu(message.from_user.id))


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Статистика доступна только мастеру.")
        return

    today = today_str()
    rows_today = q_all("""
        SELECT s.time, s.client_name, sv.name AS service_name, sv.price
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1 AND s.date = %s
        ORDER BY s.time
    """, (today,))

    count_today = len(rows_today)
    revenue_today = sum(row["price"] or 0 for row in rows_today)

    week_start = (now_local() - timedelta(days=7)).strftime("%Y-%m-%d")
    rows_week = q_all("""
        SELECT COUNT(*) AS cnt, SUM(sv.price) AS total_revenue
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1 AND s.date >= %s AND s.date <= %s
    """, (week_start, today))

    count_week = rows_week[0]["cnt"] if rows_week else 0

    popular = q_one("""
        SELECT sv.name, COUNT(*) AS cnt
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1
        GROUP BY sv.name
        ORDER BY cnt DESC
        LIMIT 1
    """)

    lines = [
        "📊 <b>Статистика мастера</b>\n",
        f"📅 Сегодня: {count_today} записей",
        f"💰 Выручка: {revenue_today} ₽\n",
        f"📈 За неделю: {count_week} записей",
    ]
    if popular and popular["name"]:
        lines.append(f"🏆 Самая популярная: {popular['name']} ({popular['cnt']} раз)\n")

    if rows_today:
        lines.append("📍 <b>Детали по записям:</b>")
        for row in rows_today:
            lines.append(
                f"  • {row['time']} — {row['client_name'] or '—'} "
                f"({row['service_name'] or 'услуга'}) — {row['price'] or 0} ₽"
            )
    else:
        lines.append("\n📭 На сегодня записей нет.")

    safe_send(message.chat.id, "\n".join(lines), main_menu(message.from_user.id))


@bot.message_handler(commands=["reminders"])
def cmd_reminders(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Доступно только мастеру.")
        return
    rows = q_all("""
        SELECT user_id, text, remind_time
        FROM reminders
        WHERE is_sent = 0
        ORDER BY remind_time
        LIMIT 50
    """)
    if not rows:
        safe_send(message.chat.id, "📭 Нет ожидающих напоминаний.")
        return
    lines = ["⏰ <b>Ожидающие напоминания:</b>\n"]
    for row in rows:
        lines.append(f"• {esc(row['remind_time'])} — {esc(row['text'])} (id {row['user_id']})")
    safe_send(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Рассылку может делать только мастер.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        safe_send(message.chat.id, "📝 Напиши текст рассылки. Например: /broadcast Скидка 20%!")
        return
    text = parts[1]
    safe_send(message.chat.id, "⏳ Начинаю рассылку...")
    rows = q_all("SELECT user_id FROM clients")
    if not rows:
        safe_send(message.chat.id, "📭 У тебя пока нет клиентов для рассылки.")
        return
    success = 0
    fail = 0
    for row in rows:
        try:
            bot.send_message(row["user_id"], f"📢 <b>Рассылка от мастера:</b>\n\n{text}", parse_mode='HTML')
            success += 1
            time.sleep(0.05)
        except Exception:
            fail += 1
    safe_send(message.chat.id, f"✅ Рассылка завершена!\n📨 Отправлено: {success}\n❌ Не доставлено: {fail}")


@bot.message_handler(commands=["dbfree"])
def cmd_dbfree(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Доступно только мастеру.")
        return
    rows = q_all("""
        SELECT s.id, s.date, s.time, s.client_id, s.client_name, sv.name AS service_name
        FROM slots s LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1
        ORDER BY s.date, s.time
        LIMIT 60
    """)
    if not rows:
        safe_send(message.chat.id, "📭 В базе нет ни одной занятой записи. Всё чисто.")
        return
    lines = [f"📋 Занято слотов: <b>{len(rows)}</b> (показаны первые 60)\n"]
    for row in rows:
        who = row["client_name"] or row["client_id"] or "—"
        lines.append(f"• {fmt_date_short(row['date'])} {row['time']} — {esc(who)} · {esc(row['service_name'] or 'услуга')}")
    lines.append("\nЧтобы освободить ВСЕ эти слоты, отправь: /dbfree да")
    safe_send(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["dbfree_yes"])
def cmd_dbfree_yes(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Доступно только мастеру.")
        return
    rowcount = q_exec("UPDATE slots SET is_booked = 0, client_id = NULL, client_name = NULL WHERE is_booked = 1")
    safe_send(message.chat.id, f"✅ Освобождено слотов: {rowcount}. Теперь все времена свободны.")


def send_portfolio_to(chat_id: int, user_id: int) -> None:
    rows = q_all("SELECT file_id, caption FROM portfolio")
    if not rows:
        send_screen(chat_id, "📭 В портфолио пока нет фото. Совсем скоро здесь появятся работы.", main_menu(user_id))
        return
    media_group = [types.InputMediaPhoto(media=row["file_id"]) for row in rows[:10]]
    bot.send_media_group(chat_id, media_group)
    send_screen(chat_id, "📁 Работы мастера ✨", main_menu(user_id))


@bot.message_handler(func=lambda m: m.text == BTN_PORTFOLIO)
def show_portfolio(message):
    send_portfolio_to(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["add_photo"])
def cmd_add_photo(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Добавлять фото может только мастер.")
        return
    msg = bot.send_message(message.chat.id, "📸 Отправь фото, которое хочешь добавить в портфолио. Можно с подписью.")
    bot.register_next_step_handler(msg, save_portfolio_photo)


@bot.message_handler(commands=["del_photo"])
def cmd_del_photo(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "🌷 Фото удалено.")
        return
    rows = q_all("SELECT id, file_id, caption FROM portfolio WHERE master_id=%s", (message.from_user.id,))
    if not rows:
        safe_send(message.chat.id, "📭 В портфолио пока нет фото для удаления.", main_menu(message.from_user.id))
        return
    chunk_size = 9
    chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
    send_del_portfolio_chunk(message.chat.id, chunks[0], 0, len(chunks))


def send_portfolio_chunk(chat_id, chunk, chunk_index, total_chunks):
    media_group = [types.InputMediaPhoto(media=row["file_id"]) for row in chunk]
    bot.send_media_group(chat_id, media_group)
    if chunk_index + 1 < total_chunks:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➡️ Дальше", callback_data=f"port:{chunk_index + 1}"))
        safe_send(chat_id, "📁 Ещё фото:", reply_markup=kb)


def send_del_portfolio_chunk(chat_id, chunk, chunk_index, total_chunks):
    media_group = []
    kb_list = []
    for index, row in enumerate(chunk):
        media_group.append(types.InputMediaPhoto(media=row["file_id"]))
        kb_list.append(types.InlineKeyboardButton(
            f"🗑️ Удалить фото {index + 1}",
            callback_data=f"delport:{row['id']}"
        ))
    bot.send_media_group(chat_id, media_group)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for btn in kb_list:
        kb.add(btn)
    if chunk_index > 0:
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data=f"delport_nav:{chunk_index - 1}"))
    if chunk_index + 1 < total_chunks:
        kb.add(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"delport_nav:{chunk_index + 1}"))
    safe_send(chat_id, "📁 Нажми на кнопку под фото, которое хочешь удалить.", reply_markup=kb)


def save_portfolio_photo(message):
    if not message.photo:
        safe_send(message.chat.id, "❌ Пожалуйста, отправь фото.", main_menu(message.from_user.id))
        return
    file_id = message.photo[-1].file_id
    caption = message.caption or ""
    q_exec("INSERT INTO portfolio (master_id, file_id, caption) VALUES (%s, %s, %s)",
           (message.from_user.id, file_id, caption))
    safe_send(message.chat.id, "✅ Фото добавлено в портфолио!", main_menu(message.from_user.id))


# ==========================================================
# 7. CALLBACK-ОБРАБОТЧИК
# ==========================================================

@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    data = call.data or ""
    user_id = call.from_user.id

    try:
        if data == "start:book":
            clear_state(user_id)
            show_services_screen(call.message.chat.id, user_id)
        elif data == "start:portfolio":
            send_portfolio_to(call.message.chat.id, user_id)
        elif data == "my:list":
            show_my_bookings(call.message.chat.id, user_id, call=call)
        elif data.startswith("svc:"):
            show_dates(call, int(data.split(":")[1]))
        elif data.startswith("dts:"):
            _, sid, offset = data.split(":", 2)
            show_dates(call, int(sid), int(offset))
        elif data.startswith("day:"):
            _, sid, date_iso = data.split(":", 2)
            show_times(call, int(sid), date_iso)
        elif data.startswith("slot:"):
            start_confirm(call, int(data.split(":")[1]))
        elif data.startswith("skip:"):
            slot_id = int(data.split(":")[1])
            slot = get_slot(slot_id)
            if not slot or slot["is_booked"]:
                clear_state(user_id)
                safe_edit(call, "😔 Это время уже заняли. Нажми «📅 Записаться».")
                return
            set_state(user_id, "await_confirm", slot_id=slot_id, comment="-")
            name = get_client_name(user_id) or "Клиент"
            safe_edit(call, confirm_text(slot, name, "-"), confirm_kb(slot_id))
        elif data.startswith("conf:"):
            do_book(call, int(data.split(":")[1]))
        elif data.startswith("chg:"):
            slot = get_slot(int(data.split(":")[1]))
            if not slot:
                safe_edit(call, "😔 Запись не найдена. Начни заново: «📅 Записаться».")
                return
            clear_state(user_id)
            show_times(call, slot["service_id"], slot["date"])
        elif data == "back:services":
            kb = services_kb("svc")
            if not kb.keyboard:
                safe_edit(call, "😔 Пока нет доступных услуг.", main_menu(user_id))
                return
            kb.row(types.InlineKeyboardButton("◀️ В меню", callback_data="back_to_main"))
            safe_edit(call, "Шаг 1 из 4\n\n<b>Выбери услугу</b>\nПокажу свободное время и цену.", kb)
        elif data.startswith("cal:"):
            slot_id = int(data.split(":")[1])
            slot = get_slot(slot_id)
            if not slot:
                safe_edit(call, "😔 Запись не найдена.")
                return
            calendar_url = calendar_link(slot)
            bot.answer_callback_query(call.id, "📅 Открываю календарь...")
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("📅 Добавить в календарь", url=calendar_url))
            safe_send(
                call.message.chat.id,
                "📅 <b>Добавь запись в календарь</b>\n\nНажми на кнопку ниже.",
                reply_markup=kb,
            )
        elif data.startswith("delport:"):
            photo_id = int(data.split(":")[1])
            q_exec("DELETE FROM portfolio WHERE id=%s", (photo_id,))
            bot.answer_callback_query(call.id, "🗑️ Фото удалено. Обновляю список...")
            cmd_del_photo(call.message)
        elif data.startswith("delport_nav:"):
            chunk_index = int(data.split(":")[1])
            rows = q_all("SELECT id, file_id, caption FROM portfolio")
            chunk_size = 9
            chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
            send_del_portfolio_chunk(call.message.chat.id, chunks[chunk_index], chunk_index, len(chunks))
            bot.answer_callback_query(call.id, "📂 Загружаю следующую партию...")
        elif data.startswith("ans_q:"):
            q_id = int(data.split(":")[1])
            row = q_one("SELECT user_id, question_text FROM questions WHERE id=%s", (q_id,))
            if not row:
                safe_edit(call, "❌ Вопрос не найден.", admin_menu_kb())
                return
            set_state(call.from_user.id, "reply_to_q", q_id=q_id, client_id=row["user_id"])
            msg = bot.send_message(call.from_user.id, f"✍️ Напиши ответ на вопрос клиента:\n\n💬 {row['question_text']}")
            bot.register_next_step_handler(msg, save_master_reply)
        elif data == "faq_start":
            row = q_one("SELECT value FROM config WHERE key='faq_text'")
            faq_text = row["value"] if row else "Текст FAQ не найден."
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⬅️ Вернуться", callback_data="back_to_main"))
            safe_edit(call, faq_text, reply_markup=kb)
            set_state(call.from_user.id, "await_client_question")
            safe_send(call.message.chat.id, "✍️ Если у тебя есть вопрос, напиши его прямо сейчас. Мастер ответит, как только освободится.")
        elif data == "back_to_main":
            clear_state(call.from_user.id)
            send_screen(
                call.message.chat.id,
                "🏠 <b>Главное меню</b>\n\nВыбери действие 👇",
                main_menu(call.from_user.id),
            )
        elif data == "abort":
            clear_state(user_id)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("📅 Выбрать время", callback_data="start:book"))
            kb.add(types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_main"))
            safe_edit(
                call,
                "🚫 <b>Запись не подтверждена</b>\n\nВсё в порядке — можно выбрать другое время.",
                kb,
            )
        elif data.startswith("cxl:"):
            do_unbook(call, int(data.split(":")[1]))
        elif (data.startswith("adm:") or data.startswith("aslot:") or data.startswith("adel:")
              or data.startswith("gsvc:") or data.startswith("delsvc:")
              or data.startswith("cl:") or data.startswith("clnote:")):
            if not is_admin(user_id):
                safe_edit(call, "⛔️ Доступно только мастеру.")
                return
            handle_admin_callback(call, data)
        elif data.startswith("port:"):
            chunk_index = int(data.split(":")[1])
            rows = q_all("SELECT file_id, caption FROM portfolio")
            chunk_size = 9
            chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
            if chunk_index >= len(chunks):
                chunk_index = len(chunks) - 1
            send_portfolio_chunk(call.message.chat.id, chunks[chunk_index], chunk_index, len(chunks))
            bot.answer_callback_query(call.id, "📁 Загружаю следующее...")
        else:
            safe_edit(call, "🤔 Не понял кнопку. Открой меню командой /start")

    except Exception as exc:
        logger.exception("Ошибка в callback '%s': %s", data, exc)
        alert_admin_error(f"Кнопка: {data[:40]}", exc)
        safe_send(user_id, "⚠️ Что-то пошло не так. Попробуй ещё раз или напиши /start")


def save_master_reply(message):
    state = get_state(message.from_user.id)
    if not state or state.get("step") != "reply_to_q":
        return
    q_id = state["data"]["q_id"]
    client_id = state["data"]["client_id"]
    answer = message.text.strip()
    if len(answer) < 2:
        safe_send(message.chat.id, "Ответ слишком короткий.")
        return
    q_exec("UPDATE questions SET answer_text=%s, is_answered=1 WHERE id=%s", (answer, q_id))
    clear_state(message.from_user.id)
    safe_send(message.chat.id, "✅ Ответ сохранён! Он отправлен клиенту.")
    try:
        q = q_one("SELECT question_text FROM questions WHERE id=%s", (q_id,))
        if q:
            bot.send_message(client_id, f"📬 <b>Ответ от мастера:</b>\n\n💬 Ваш вопрос: {q['question_text']}\n✍️ Ответ: {answer}")
    except Exception as e:
        logger.error(f"Не удалось отправить ответ клиенту: {e}")


def confirm_text(slot, client_name: str, comment: str = "-") -> str:
    lines = [
        "Шаг 4 из 4",
        "",
        "<b>Проверь запись</b>",
        "",
        f"👤 {esc(client_name)}",
        f"💅 {esc(slot['service_name'] or 'услуга')}",
        f"📅 {fmt_date(slot['date'])} · {esc(slot['time'])}",
        f"⏱ {slot['duration'] or 0} мин",
        f"💰 <b>{fmt_price(slot['price'])}</b>",
    ]
    if comment and comment != "-":
        lines += ["", f"📝 {esc(comment)}"]
    lines += ["", "Всё верно?"]
    return "\n".join(lines)


def confirm_text_with_comment(slot, client_name: str, comment: str) -> str:
    return confirm_text(slot, client_name, comment)


def show_dates(call, service_id: int, offset: int = 0) -> None:
    service = get_service(service_id)
    if not service:
        safe_edit(call, "😔 Услуга не найдена. Начни запись заново: «📅 Записаться».")
        return
    dates = free_dates(service_id)
    if not dates:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("◀️ К услугам", callback_data="back:services"))
        safe_edit(call, f"😔 На услугу <b>{esc(service['name'])}</b> пока нет свободного времени.", kb)
        return

    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    page_size = 6
    max_offset = max(0, ((len(dates) - 1) // page_size) * page_size)
    offset = min(max(0, offset), max_offset)
    page = dates[offset:offset + page_size]

    buttons = [
        types.InlineKeyboardButton(
            text=date_button_label(row["date"], row["cnt"]),
            callback_data=f"day:{service_id}:{row['date']}",
        )
        for row in page
    ]
    kb = types.InlineKeyboardMarkup()
    for i in range(0, len(buttons), 2):
        kb.row(*buttons[i:i + 2])

    nav = []
    if offset > 0:
        nav.append(types.InlineKeyboardButton("‹ Раньше", callback_data=f"dts:{service_id}:{max(0, offset - page_size)}"))
    if offset + page_size < len(dates):
        nav.append(types.InlineKeyboardButton("Позже ›", callback_data=f"dts:{service_id}:{offset + page_size}"))
    if nav:
        kb.row(*nav)
    kb.row(types.InlineKeyboardButton("◀️ К услугам", callback_data="back:services"))

    safe_edit(
        call,
        (f"Шаг 2 из 4\n\n<b>{esc(service['name'])}</b> · {fmt_price(service['price'])}\n"
         f"{service['duration']} мин\n\n"
         "Выбери день — покажу свободное время.\n"
         "Цифра на кнопке — число окон."),
        kb,
    )


def show_times(call, service_id: int, date_iso: str) -> None:
    service = get_service(service_id)
    slots = free_slots_on_date(service_id, date_iso)
    if not service:
        safe_edit(call, "😔 Услуга не найдена. Начни запись заново: «📅 Записаться».")
        return
    kb = types.InlineKeyboardMarkup()
    if not slots:
        kb.add(types.InlineKeyboardButton("◀️ К датам", callback_data=f"svc:{service_id}"))
        safe_edit(
            call,
            f"😔 На {fmt_date(date_iso)} свободного времени уже нет.\n\nВыбери другой день — я рядом.",
            kb,
        )
        return

    time_buttons = [
        types.InlineKeyboardButton(text=row["time"], callback_data=f"slot:{row['id']}")
        for row in slots
    ]
    for i in range(0, len(time_buttons), 3):
        kb.row(*time_buttons[i:i + 3])
    kb.row(types.InlineKeyboardButton("◀️ К датам", callback_data=f"svc:{service_id}"))
    safe_edit(
        call,
        (f"Шаг 3 из 4\n\n<b>{esc(service['name'])}</b> · {fmt_price(service['price'])}\n"
         f"📅 {fmt_date(date_iso)}\n\n"
         "Выбери удобное время:"),
        kb,
    )


def confirm_kb(slot_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"conf:{slot_id}"))
    kb.row(
        types.InlineKeyboardButton("✏️ Изменить", callback_data=f"chg:{slot_id}"),
        types.InlineKeyboardButton("🚫 Отмена", callback_data="abort"),
    )
    return kb


def start_confirm(call, slot_id: int) -> None:
    slot = get_slot(slot_id)
    if not slot or slot["is_booked"]:
        safe_edit(call, "😔 Это время только что заняли. Выбери другое.")
        return
    user_id = call.from_user.id
    name = get_client_name(user_id)
    if not name:
        set_state(user_id, "await_name", slot_id=slot_id)
        safe_edit(
            call,
            "Шаг 4 из 4\n\n<b>Как тебя зовут?</b>\n"
            "Напиши имя — сохраню его для следующих записей.",
        )
        return
    set_state(user_id, "await_comment", slot_id=slot_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➡️ Пропустить", callback_data=f"skip:{slot_id}"))
    safe_edit(
        call,
        "Шаг 4 из 4\n\n<b>Пожелания к записи</b>\n"
        "Напиши, что важно учесть, или пропусти этот шаг.",
        reply_markup=kb,
    )


_confirm_locks = {}


def do_book(call, slot_id: int) -> None:
    user_id = call.from_user.id
    key = (user_id, slot_id)
    lock = _confirm_locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        # Параллельный дубль того же подтверждения — молча игнорируем.
        logger.info("Пропущен дубль confirm: user=%s slot=%s", user_id, slot_id)
        return
    try:
        state = get_state(user_id)
        comment = "-"
        if state and state.get("step") == "await_confirm":
            saved = state.get("data") or {}
            try:
                saved_slot = int(saved.get("slot_id", 0))
            except (TypeError, ValueError):
                saved_slot = 0
            if saved_slot == int(slot_id):
                comment = (saved.get("comment") or "-").strip() or "-"
        clear_state(user_id)
        name = get_client_name(user_id) or (call.from_user.first_name or "Клиент")
        slot = get_slot(slot_id)
        if not slot:
            safe_edit(call, "😔 Слот не найден. Попробуй записаться заново.")
            return
        book_result = book_slot(slot_id, user_id, name)
        if book_result is None:
            safe_edit(call, "⚠️ Техническая ошибка при записи. Попробуй ещё раз через минуту.")
            return
        if book_result is False:
            safe_edit(call, "😔 Это время занял другой клиент. Выбери другое: " + BTN_BOOK)
            return

        booking_code = f"AB-{int(slot_id):04d}"
        kb_success = types.InlineKeyboardMarkup(row_width=2)
        kb_success.row(
            types.InlineKeyboardButton("📋 Мои записи", callback_data="my:list"),
            types.InlineKeyboardButton("📅 В календарь", url=calendar_link(slot)),
        )
        kb_success.add(types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_main"))
        safe_edit(
            call,
            ("✅ <b>Готово! Ты записан(а)</b>\n\n"
             f"💅 <b>{esc(slot['service_name'] or 'услуга')}</b>\n"
             f"📅 {fmt_date(slot['date'])} · {esc(slot['time'])}\n"
             f"💰 {fmt_price(slot['price'])}\n"
             f"🔖 Код записи: <code>{booking_code}</code>\n\n"
             "Ждём тебя! Если планы изменятся — открой «Мои записи»."),
            kb_success,
        )

        booking_time = datetime.strptime(f"{slot['date']} {slot['time']}", "%Y-%m-%d %H:%M")
        remind_time = booking_time - timedelta(hours=1)
        remind_time_str = remind_time.strftime("%Y-%m-%d %H:%M")

        if remind_time > now_local().replace(tzinfo=None):
            q_exec("INSERT INTO reminders (user_id, text, remind_time) VALUES (%s, %s, %s)",
                   (user_id, f"У вас запись на {esc(slot['service_name'])} к {esc(MASTER_NAME)}", remind_time_str))

        username = f"@{call.from_user.username}" if call.from_user.username else "—"
        admin_text = ("🔔 <b>НОВАЯ ЗАПИСЬ</b>\n\n"
            f"👤 {esc(name)} ({esc(username)}, id {user_id})\n"
            f"💅 {esc(slot['service_name'] or 'услуга')}\n"
            f"🗓 {fmt_date(slot['date'])} в {esc(slot['time'])}\n")
        if comment and comment != "-":
            admin_text += f"📝 Пожелание: {esc(comment)}\n"
        try:
            client = get_client(user_id)
            note = (client["notes"] or "").strip() if client else ""
        except Exception as exc:
            logger.warning("Не удалось прочитать заметку клиента %s: %s", user_id, exc)
            note = ""
        if note:
            admin_text += f"🗒 Заметка: {esc(note)}\n"
        admin_text += f"💰 {fmt_price(slot['price'])} · ⏱ {slot['duration'] or 0} мин"
        note_kb = types.InlineKeyboardMarkup()
        note_kb.add(types.InlineKeyboardButton("🗒 Заметка", callback_data=f"clnote:{user_id}"))
        notify_admin(admin_text, note_kb)
        logger.info("Запись: slot=%s user=%s comment=%s", slot_id, user_id, comment)
    finally:
        try:
            lock.release()
        finally:
            _confirm_locks.pop(key, None)


def do_unbook(call, slot_id: int) -> None:
    user_id = call.from_user.id
    slot = get_slot(slot_id)
    if not slot or not slot["is_booked"] or slot["client_id"] != user_id:
        safe_edit(call, "🤔 Эта запись уже неактивна.")
        return
    if not unbook_slot(slot_id, user_id):
        safe_edit(call, "⚠️ Не удалось отменить запись.")
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📋 Мои записи", callback_data="my:list"))
    kb.row(
        types.InlineKeyboardButton("📅 Записаться", callback_data="start:book"),
        types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_main"),
    )
    safe_edit(
        call,
        ("✅ <b>Запись отменена</b>\n\n"
         f"💅 {esc(slot['service_name'] or 'услуга')}\n"
         f"📅 {fmt_date(slot['date'])} · {esc(slot['time'])}\n\n"
         "Спасибо, что предупредили. Слот снова свободен."),
        kb,
    )
    notify_admin("⚠️ <b>ОТМЕНА ЗАПИСИ</b>\n\n"
        f"👤 {esc(slot['client_name'] or call.from_user.first_name)} (id {user_id})\n"
        f"💅 {esc(slot['service_name'] or 'услуга')}\n"
        f"🗓 {fmt_date(slot['date'])} в {esc(slot['time'])}\n"
        "🟢 Слот снова свободен.")
    logger.info("Отмена: slot=%s user=%s", slot_id, user_id)


# --------------- админские callback-и ----------------

def handle_admin_callback(call, data: str) -> None:
    user_id = call.from_user.id

    if data == "adm:menu":
        clear_state(user_id)
        safe_edit(call, "⚙️ <b>Админ-панель</b>\n\nВыбери раздел.", admin_menu_kb())

    elif data == "adm:bookings":
        clear_state(user_id)
        safe_edit(call, "📅 <b>Записи</b>\n\nЧто показать?", admin_bookings_kb())

    elif data == "adm:schedule":
        clear_state(user_id)
        safe_edit(call, "🗓 <b>Расписание</b>\n\nУправление слотами.", admin_schedule_kb())

    elif data == "adm:services":
        clear_state(user_id)
        safe_edit(call, "🧾 <b>Услуги</b>\n\nДобавить или удалить позицию.", admin_services_kb())

    elif data == "adm:more":
        clear_state(user_id)
        safe_edit(call, "⚙️ <b>Ещё</b>\n\nПолезные разделы.", admin_more_kb())

    elif data == "adm:faq":
        row = q_one("SELECT value FROM config WHERE key='faq_text'")
        faq_text = row["value"] if row else "Текст FAQ не найден."
        safe_edit(call, f"❓ <b>Текст FAQ</b>\n\n{faq_text}", admin_more_kb())

    elif data == "adm:add_service":
        set_state(user_id, "svc_name")
        safe_edit(call, "🧾 <b>Новая услуга</b>\n\nШаг 1/3. Введи название:\nОтмена — /cancel")

    elif data == "adm:add_slot":
        kb = services_kb("aslot")
        if not kb.keyboard:
            safe_edit(call, "Сначала добавь хотя бы одну услугу.", admin_schedule_kb())
            return
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:schedule"))
        safe_edit(call, "➕ <b>Новый слот</b>\n\nДля какой услуги?", kb)

    elif data == "adm:today":
        rows = all_bookings(only_today=True)
        if not rows:
            safe_edit(call, "📆 На сегодня записей нет. Отдыхай 😎", admin_bookings_kb())
            return
        lines = [f"📆 <b>Записи на {fmt_date(today_str())}</b>", ""]
        total = 0
        for row in rows:
            total += row["price"] or 0
            lines.append(f"🕒 <b>{esc(row['time'])}</b> — {esc(row['client_name'] or '—')} · "
                         f"{esc(row['service_name'] or 'услуга')} · {fmt_price(row['price'])}")
        lines.append(f"\n💰 Итого за день: <b>{fmt_price(total)}</b>")
        safe_edit(call, "\n".join(lines), admin_bookings_kb())

    elif data == "adm:all":
        rows = all_bookings()
        if not rows:
            safe_edit(call, "📚 Активных записей нет.", admin_bookings_kb())
            return
        lines = ["📚 <b>Все активные записи</b>", ""]
        current_date = None
        for row in rows:
            if row["date"] != current_date:
                current_date = row["date"]
                lines.append(f"\n<b>🗓 {fmt_date(current_date)}</b>")
            lines.append(f"  🕒 {esc(row['time'])} — {esc(row['client_name'] or '—')} · "
                         f"{esc(row['service_name'] or 'услуга')} · {fmt_price(row['price'])}")
        safe_send(call.message.chat.id, "\n".join(lines))
        safe_send(call.message.chat.id, "📅 <b>Записи</b>", admin_bookings_kb())

    elif data == "adm:clients":
        clear_state(user_id)
        rows = q_all("""
            SELECT c.user_id, c.name, c.username, c.notes,
                   MAX(s.date) AS last_date
            FROM clients c
            LEFT JOIN slots s ON s.client_id = c.user_id AND s.is_booked = 1
            GROUP BY c.user_id, c.name, c.username, c.notes
            ORDER BY MAX(s.date) DESC NULLS LAST, c.name NULLS LAST, c.user_id
            LIMIT 30
        """)
        total_row = q_one("SELECT COUNT(*) AS n FROM clients")
        total = int(total_row["n"]) if total_row else 0
        if not rows:
            safe_edit(call, "👥 <b>Клиенты</b>\n\nПока никого нет — клиенты появятся после первой записи.",
                      admin_menu_kb())
            return
        text = ("👥 <b>Клиенты</b>\n\n"
                "Выбери клиента, чтобы открыть карточку и заметку.")
        if total > len(rows):
            text += (f"\n\nПоказаны {len(rows)} из {total}. "
                     "Если нужного нет — «🔎 Найти по имени».")
        safe_edit(call, text, clients_list_kb(rows))

    elif data == "adm:clients_find":
        set_state(user_id, "client_search")
        safe_edit(call, "🔎 <b>Поиск клиента</b>\n\nНапиши имя или его часть:")

    elif data.startswith("clnote:"):
        try:
            client_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            safe_edit(call, "🤔 Клиент не найден.", admin_menu_kb())
            return
        client = get_client(client_id)
        if not client:
            safe_edit(call, "🤔 Клиент не найден.", admin_menu_kb())
            return
        name = (client["name"] or "").strip() or "Клиент"
        set_state(user_id, "client_note", client_id=client_id)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("◀️ К карточке", callback_data=f"cl:{client_id}"))
        prompt = safe_send(call.message.chat.id,
                           f"🗒 <b>Заметка о клиенте</b>\n\n"
                           f"👤 {esc(name)}\n\n"
                           "Напиши текст заметки одним сообщением. Чтобы удалить заметку — отправь «-».\n"
                           "Отмена — /cancel",
                           kb)
        if prompt:
            _remember_screen(call.message.chat.id, prompt)

    elif data.startswith("cl:"):
        try:
            client_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            safe_edit(call, "🤔 Клиент не найден.", admin_menu_kb())
            return
        clear_state(user_id)
        show_client_card(call, client_id)

    elif data == "adm:free":
        rows = q_all("""
            SELECT s.id, s.date, s.time, sv.name AS service_name
            FROM slots s LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 0 AND (s.date > %s OR (s.date = %s AND s.time >= %s))
            ORDER BY s.date, s.time LIMIT 40
        """, (today_str(), today_str(), time_str()))
        if not rows:
            safe_edit(call, "🕳 Свободных слотов нет — добавь новые.", admin_bookings_kb())
            return
        kb = types.InlineKeyboardMarkup()
        for row in rows:
            label = f"🗑 {fmt_date_short(row['date'])} {row['time']} · {row['service_name'] or 'услуга'}"
            kb.add(types.InlineKeyboardButton(text=btn_text(label), callback_data=f"adel:{row['id']}"))
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:bookings"))
        safe_edit(call, "🕳 <b>Свободные слоты</b>\nНажми, чтобы удалить слот:", kb)

    elif data == "adm:del_service":
        rows = q_all("SELECT id, name FROM services ORDER BY id")
        if not rows:
            safe_edit(call, "📭 Нет услуг для удаления.", admin_services_kb())
            return
        kb = types.InlineKeyboardMarkup()
        for row in rows:
            kb.add(types.InlineKeyboardButton(
                text=btn_text(f"❌ {row['name']}"),
                callback_data=f"delsvc:{row['id']}",
            ))
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:services"))
        safe_edit(call, "❌ <b>Какую услугу удалить?</b>", kb)

    elif data == "adm:stats":
        user_id = call.from_user.id
        today = today_str()
        rows_today = q_all("""
            SELECT s.time, s.client_name, sv.name AS service_name, sv.price
            FROM slots s
            LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 1 AND s.date = %s
            ORDER BY s.time
        """, (today,))
        count_today = len(rows_today)
        revenue_today = sum(row["price"] or 0 for row in rows_today)

        week_start = (now_local() - timedelta(days=7)).strftime("%Y-%m-%d")
        rows_week = q_all("""
            SELECT COUNT(*) AS cnt, SUM(sv.price) AS total_revenue
            FROM slots s
            LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 1 AND s.date >= %s AND s.date <= %s
        """, (week_start, today))
        count_week = rows_week[0]["cnt"] if rows_week else 0

        popular = q_one("""
            SELECT sv.name, COUNT(*) AS cnt
            FROM slots s
            LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 1
            GROUP BY sv.name
            ORDER BY cnt DESC
            LIMIT 1
        """)

        lines = [
            "📊 <b>Статистика мастера</b>\n",
            f"📅 Сегодня: {count_today} записей",
            f"💰 Выручка: {revenue_today} ₽\n",
            f"📈 За неделю: {count_week} записей",
        ]
        if popular and popular["name"]:
            lines.append(f"🏆 Самая популярная: {popular['name']} ({popular['cnt']} раз)\n")
        if rows_today:
            lines.append("📍 <b>Детали по записям:</b>")
            for row in rows_today:
                lines.append(f"  • {row['time']} — {row['client_name'] or '—'} "
                             f"({row['service_name'] or 'услуга'}) — {row['price'] or 0} ₽")
        else:
            lines.append("\n📭 На сегодня записей нет.")
        safe_edit(call, "\n".join(lines), admin_menu_kb())

    elif data.startswith("delsvc:"):
        service_id = int(data.split(":")[1])
        q_exec("DELETE FROM slots WHERE service_id=%s", (service_id,))
        q_exec("DELETE FROM services WHERE id=%s", (service_id,))
        safe_edit(call, "🗑 Услуга и все её слоты удалены.", admin_menu_kb())

    elif data.startswith("adel:"):
        slot_id = int(data.split(":")[1])
        rowcount = q_exec("DELETE FROM slots WHERE id = %s AND is_booked = 0", (slot_id,))
        if rowcount == 1:
            safe_edit(call, "🗑 Слот удалён.", admin_menu_kb())
        else:
            safe_edit(call, "⚠️ Не удалось удалить (возможно, слот уже занят).", admin_menu_kb())

    elif data == "adm:gen_week":
        kb = services_kb("gsvc")
        if not kb.keyboard:
            safe_edit(call, "Сначала добавь хотя бы одну услугу.", admin_schedule_kb())
            return
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:schedule"))
        safe_edit(call, "📅 <b>Расписание на неделе</b>\n\nДля какой услуги?", kb)

    elif data == "adm:gen_go":
        state = get_state(user_id)
        if not state or state.get("step") != "gen_confirm":
            safe_edit(call, "⚠️ Данные устарели. Запусти расписание заново.", admin_menu_kb())
            return
        d = state["data"]
        clear_state(user_id)
        service_id = int(d["service_id"])
        service = get_service(service_id)
        if not service:
            safe_edit(call, "Услуга не найдена.", admin_menu_kb())
            return
        break_min = int(d["break_min"])
        q_exec("UPDATE services SET break_min=%s WHERE id=%s", (break_min, service_id))
        booked = q_one("SELECT COUNT(*) AS cnt FROM slots WHERE service_id=%s AND is_booked=1",
                       (service_id,))
        booked_count = int(booked["cnt"]) if booked else 0
        deleted = q_exec("DELETE FROM slots WHERE service_id=%s AND is_booked=0", (service_id,))
        added, skipped, days_used, capped = generate_slots(
            service_id, d["start"], d["end"], break_min, int(d["days"]),
            bool(d["only_weekdays"]))
        report = [f"📅 <b>Расписание обновлено: {esc(service['name'])}</b>",
                  f"🕒 {d['start']}–{d['end']} · услуга {service['duration']} мин · перерыв {break_min} мин",
                  f"🗓 Дней: {days_used}" + (" (только будни)" if d["only_weekdays"] else ""),
                  ""]
        if deleted >= 0:
            report.append(f"🗑 Удалено старых свободных слотов: {deleted}")
        report.append(f"✅ Создано слотов: <b>{added}</b>")
        if booked_count:
            report.append(f"💾 Сохранено записей клиентов: {booked_count}")
        if skipped:
            report.append(f"⏭ Не создано (совпало с записями): {skipped}")
        if capped:
            report.append(f"⚠️ Лимит {MAX_GEN_SLOTS} слотов за раз.")
        if not added and not skipped:
            report.append("😕 Слоты не созданы — проверь рабочие часы и длительность услуги.")
        safe_edit(call, "\n".join(report), admin_menu_kb())

    elif data == "adm:gen_cancel":
        clear_state(user_id)
        safe_edit(call, "🚫 Расписание не изменено.", admin_menu_kb())

    elif data.startswith("gsvc:"):
        service_id = int(data.split(":")[1])
        service = get_service(service_id)
        if not service:
            safe_edit(call, "Услуга не найдена.", admin_menu_kb())
            return
        set_state(user_id, "gen_hours", service_id=service_id)
        safe_edit(call, f"💅 Услуга: <b>{esc(service['name'])}</b>\n\n"
                        "Шаг 1/3. Твои рабочие часы:\n<code>10:00-19:00</code>")

    elif data == "adm:commands":
        text = (
            "📖 <b>Команды мастера</b>\n\n"
            "✧ <b>Фото:</b>\n"
            "📸 /add_photo — добавить фото\n"
            "🗑️ /del_photo — удалить фото\n\n"
            "✧ <b>Рассылка:</b>\n"
            "📢 /broadcast — рассылка\n\n"
            "✧ <b>Статистика:</b>\n"
            "📊 /stats — статистика\n\n"
            "✧ <b>Настройки:</b>\n"
            "⚙️ /admin — админ-панель\n"
            "🆔 /myid — свой ID\n"
            "🗓️ /reminders — ожидающие напоминания\n\n"
            "❓ /edit_faq — изменить FAQ\n\n"
            "💬 /my_q — посмотреть вопросы"
        )
        safe_edit(call, text, admin_more_kb())

    elif data.startswith("aslot:"):
        service_id = int(data.split(":")[1])
        service = get_service(service_id)
        if not service:
            safe_edit(call, "Услуга не найдена.", admin_menu_kb())
            return
        set_state(user_id, "slot_date", service_id=service_id)
        safe_edit(call, f"➕ Услуга: <b>{esc(service['name'])}</b>\n\n"
                        "Шаг 1/2. Введи дату:\nсегодня / завтра / 15.03 / 2025-03-15")


# ==========================================================
# 8. ТЕКСТОВЫЙ ОБРАБОТЧИК
# ==========================================================

@bot.message_handler(content_types=["text"])
def on_text(message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    state = get_state(user_id)

    if not state:
        safe_send(message.chat.id, "🤖 Выбери действие в меню 👇", main_menu(user_id))
        return

    step = state["step"]
    data = state["data"]

    try:
        if step == "await_name":
            if len(text) < 2 or len(text) > 60:
                send_screen(message.chat.id, "✍️ Имя должно быть от 2 до 60 символов. Попробуй ещё раз:")
                return
            save_client(user_id, text, message.from_user.username or "")
            clear_state(user_id)
            slot = get_slot(int(data.get("slot_id", 0)))
            if not slot or slot["is_booked"]:
                safe_send(message.chat.id, "😔 Это время уже заняли.", main_menu(user_id))
                return
            set_state(user_id, "await_comment", slot_id=data["slot_id"])
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("➡️ Пропустить", callback_data=f"skip:{data['slot_id']}"))
            send_screen(
                message.chat.id,
                "Шаг 4 из 4\n\n<b>Пожелания к записи</b>\n"
                "Напиши, что важно учесть, или пропусти этот шаг.",
                reply_markup=kb,
            )
            return

        if step == "await_comment":
            if text == "➡️ Далее" or text == "-":
                comment = "-"
            else:
                comment = text
            slot = get_slot(data["slot_id"])
            if not slot:
                clear_state(user_id)
                send_screen(message.chat.id, "😔 Слот не найден. Начни запись заново: /start",
                            main_menu(user_id))
                return
            set_state(user_id, "await_confirm", slot_id=data["slot_id"], comment=comment)
            name = get_client_name(user_id) or "Клиент"
            send_screen(message.chat.id, confirm_text(slot, name, comment), confirm_kb(slot["id"]))
            return

        if step == "await_client_question":
            question = text.strip()
            if len(question) < 3:
                safe_send(message.chat.id, "❓ Вопрос слишком короткий.")
                return
            q_exec("INSERT INTO questions (user_id, question_text, created_at) VALUES (%s, %s, %s)",
                   (message.from_user.id, question, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            clear_state(user_id)
            notify_admin(
                f"📩 <b>Новый вопрос от клиента!</b>\n\n"
                f"👤 От: {esc(message.from_user.first_name or 'Клиент')}\n"
                f"💬 Вопрос: {esc(question)}\n\n"
                f"Чтобы ответить, используйте команду /my_q."
            )
            safe_send(message.chat.id, "✅ Твой вопрос отправлен мастеру!", main_menu(user_id))
            return

        if step == "client_note":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            if not text:
                send_screen(message.chat.id, "Напиши текст заметки или «-», чтобы удалить:")
                return
            if len(text) > 1000:
                send_screen(message.chat.id, "Заметка слишком длинная (максимум 1000 символов). Сократи:")
                return
            try:
                client_id = int(data.get("client_id", 0))
            except (TypeError, ValueError):
                clear_state(user_id)
                send_screen(message.chat.id, "⚠️ Данные устарели, открой клиента заново.", admin_menu_kb())
                return
            set_client_note(client_id, "" if text == "-" else text)
            clear_state(user_id)
            client = get_client(client_id)
            if client:
                send_screen(message.chat.id,
                            "✅ Заметка сохранена.\n\n" + client_card_text(client),
                            client_card_kb(client_id))
            else:
                send_screen(message.chat.id, "✅ Заметка сохранена.", admin_menu_kb())
            return

        if step == "client_search":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            if len(text) < 2:
                send_screen(message.chat.id, "Введи хотя бы 2 символа имени:")
                return
            rows = q_all("""
                SELECT user_id, name, username, notes
                FROM clients
                WHERE name ILIKE %s
                ORDER BY name NULLS LAST, user_id
                LIMIT 30
            """, (f"%{text}%",))
            clear_state(user_id)
            if not rows:
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("🔎 Искать снова", callback_data="adm:clients_find"))
                kb.add(types.InlineKeyboardButton("👥 Все клиенты", callback_data="adm:clients"))
                send_screen(message.chat.id, f"🔎 По запросу «{esc(text)}» никого не нашёл.", kb)
                return
            send_screen(message.chat.id, f"🔎 <b>Найдено: {len(rows)}</b>\nВыбери клиента:",
                        clients_list_kb(rows))
            return

        if step == "svc_name":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            if len(text) < 2 or len(text) > 80:
                send_screen(message.chat.id, "Название должно быть 2–80 символов:")
                return
            set_state(user_id, "svc_duration", name=text)
            send_screen(message.chat.id, "Шаг 2/3. Длительность в минутах:")
            return

        if step == "svc_duration":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            if not text.isdigit() or not (5 <= int(text) <= 600):
                send_screen(message.chat.id, "Нужно число от 5 до 600:")
                return
            set_state(user_id, "svc_price", name=data["name"], duration=int(text))
            send_screen(message.chat.id, "Шаг 3/3. Цена в рублях:")
            return

        if step == "svc_price":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            digits = text.replace(" ", "").replace("₽", "")
            if not digits.isdigit() or int(digits) > 1_000_000:
                send_screen(message.chat.id, "Нужно целое число:")
                return
            price = int(digits)
            q_exec("INSERT INTO services (name, duration, price) VALUES (%s,%s,%s)",
                   (data["name"], data["duration"], price))
            clear_state(user_id)
            safe_send(message.chat.id,
                      f"✅ Услуга добавлена:\n💅 <b>{esc(data['name'])}</b>\n"
                      f"⏱ {data['duration']} мин · 💰 {price}₽")
            safe_send(message.chat.id, "⚙️ Меню:", admin_menu_kb())
            return

        if step == "slot_date":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            date_iso = parse_date_input(text)
            if not date_iso:
                send_screen(message.chat.id, "❗️ Не понял дату. Примеры: завтра, 15.03, 2025-03-15")
                return
            if date_iso < today_str():
                send_screen(message.chat.id, "❗️ Дата в прошлом.")
                return
            set_state(user_id, "slot_time", service_id=data["service_id"], date=date_iso)
            send_screen(message.chat.id,
                      f"🗓 Дата: <b>{fmt_date(date_iso)}</b>\n\n"
                      "Шаг 2/2. Введи время (можно несколько через запятую):\n10:00, 11:30, 13:00")
            return

        if step == "slot_time":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            times, bad = parse_times_input(text)
            if not times:
                send_screen(message.chat.id, "❗️ Не понял время. Формат ЧЧ:ММ")
                return
            service_id = int(data["service_id"])
            date_iso = data["date"]
            added, skipped = [], []
            for t in times:
                exists = q_one("SELECT id FROM slots WHERE service_id = %s AND date = %s AND time = %s",
                               (service_id, date_iso, t))
                if exists:
                    skipped.append(t)
                    continue
                if q_exec("INSERT INTO slots (service_id, date, time, is_booked) VALUES (%s,%s,%s, 0)",
                          (service_id, date_iso, t)) == 1:
                    added.append(t)
                else:
                    skipped.append(t)
            clear_state(user_id)
            service = get_service(service_id)
            report = [f"💅 <b>{esc(service['name'] if service else 'услуга')}</b>\n🗓 {fmt_date(date_iso)}"]
            if added:
                report.append("✅ Добавлено: " + ", ".join(added))
            if skipped:
                report.append("⏭ Пропущено: " + ", ".join(skipped))
            if bad:
                report.append("⚠️ Не распознано: " + ", ".join(bad))
            safe_send(message.chat.id, "\n".join(report))
            safe_send(message.chat.id, "⚙️ Меню:", admin_menu_kb())
            return

        if step == "gen_hours":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            rng = parse_hours_range(text)
            if not rng:
                send_screen(message.chat.id, "❗️ Формат: <code>10:00-19:00</code>")
                return
            set_state(user_id, "gen_step", service_id=data["service_id"], start=rng[0], end=rng[1])
            send_screen(message.chat.id,
                      f"🕒 Часы: <b>{rng[0]}–{rng[1]}</b>\n\n"
                      "Шаг 2/3. Перерыв между записями, мин:\n<code>0</code> · <code>10</code> · <code>15</code> · <code>20</code>")
            return

        if step == "gen_step":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            if not text.isdigit() or not (0 <= int(text) <= 120):
                send_screen(message.chat.id, "Нужно число от 0 до 120:")
                return
            set_state(user_id, "gen_days", service_id=data["service_id"],
                      start=data["start"], end=data["end"], break_min=int(text))
            send_screen(message.chat.id,
                      "Шаг 3/3. На сколько дней вперёд? (1–60)\n\n"
                      "Добавь <b>будни</b>, чтобы пропустить Сб и Вс:\n"
                      "<code>7</code> · <code>14</code> · <code>14 будни</code>")
            return

        if step == "gen_days":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            only_weekdays = "будн" in text.lower()
            digits = re.sub(r"\D", "", text)
            if not digits or not (1 <= int(digits) <= 60):
                send_screen(message.chat.id, "Нужно число от 1 до 60.")
                return
            service_id = int(data["service_id"])
            service = get_service(service_id)
            if not service:
                clear_state(user_id)
                send_screen(message.chat.id, "Услуга не найдена.")
                return
            set_state(user_id, "gen_confirm", service_id=service_id,
                      start=data["start"], end=data["end"],
                      break_min=int(data["break_min"]), days=int(digits),
                      only_weekdays=only_weekdays)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Продолжить", callback_data="adm:gen_go"))
            kb.add(types.InlineKeyboardButton("🚫 Отмена", callback_data="adm:gen_cancel"))
            days_line = f"🗓 Дней: {int(digits)}"
            if only_weekdays:
                days_line += " (только будни)"
            send_screen(message.chat.id,
                "⚠️ <b>Пересоздание расписания</b>\n\n"
                f"💅 {esc(service['name'])}\n"
                f"🕒 {data['start']}–{data['end']}\n"
                f"⏱ Услуга: {service['duration']} мин\n"
                f"☕️ Перерыв: {int(data['break_min'])} мин\n"
                f"{days_line}\n\n"
                "Все старые свободные слоты этой услуги будут удалены и созданы заново.\n"
                "Записи клиентов сохранятся.\n\n"
                "Продолжить?",
                reply_markup=kb)
            return

        clear_state(user_id)
        safe_send(message.chat.id, "🔄 Диалог сброшен.", main_menu(user_id))

    except Exception as exc:
        logger.exception("Ошибка в шаге '%s': %s", step, exc)
        alert_admin_error(f"Шаг: {step}", exc)
        clear_state(user_id)
        safe_send(message.chat.id, "⚠️ Ошибка обработки. Начни заново: /start", main_menu(user_id))


@bot.message_handler(content_types=["photo", "sticker", "voice", "video", "document",
                                    "audio", "location", "contact", "animation"])
def on_other_content(message):
    safe_send(message.chat.id, "🤖 Я понимаю только кнопки и текст 👇", main_menu(message.from_user.id))


# ---------- Планировщик ----------
def check_reminders():
    now = now_local().replace(tzinfo=None)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    contact = f"\n\n📞 Связь с мастером: {esc(MASTER_CONTACT)}" if MASTER_CONTACT else ""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, user_id, text, remind_time
                FROM reminders
                WHERE is_sent = 0 AND remind_time <= %s
                ORDER BY remind_time
            """, (now_str,))
            due = cur.fetchall()
            for row in due:
                r_id = row["id"]
                user_id = row["user_id"]
                text = row["text"]
                try:
                    remind_dt = datetime.strptime(row["remind_time"], "%Y-%m-%d %H:%M")
                except (TypeError, ValueError):
                    cur.execute("UPDATE reminders SET is_sent=1 WHERE id=%s", (r_id,))
                    continue
                if now >= remind_dt + timedelta(minutes=15):
                    cur.execute("UPDATE reminders SET is_sent=1 WHERE id=%s", (r_id,))
                    continue
                try:
                    bot.send_message(user_id,
                        f"⏰ <b>Напоминание:</b> {text}{contact}",
                        parse_mode='HTML')
                except Exception as e:
                    print(f"Ошибка отправки: {e}")
                cur.execute("UPDATE reminders SET is_sent=1 WHERE id=%s", (r_id,))
        conn.commit()


def run_scheduler():
    schedule.every(1).minutes.do(check_reminders)
    while True:
        try:
            schedule.run_pending()
        except Exception as exc:
            logger.exception("Планировщик: %s", exc)
            alert_admin_error("Планировщик напоминаний", exc)
        time.sleep(1)


threading.Thread(target=run_scheduler, daemon=True).start()


# ==========================================================
# 9. FLASK-ЗАГЛУШКА (health-check для Render)
# ==========================================================

def start_web_server() -> None:
    port_raw = (os.getenv("PORT") or "").strip()
    if not port_raw:
        logger.info("PORT не задан — веб-сервер не запускаю.")
        return
    try:
        from flask import Flask
    except ImportError:
        logger.warning("Flask не установлен — health-check пропущен.")
        return

    app = Flask(__name__)

    @app.route("/")
    @app.route("/health")
    def health():
        return "Bot is alive", 200

    def run():
        try:
            app.run(host="0.0.0.0", port=int(port_raw), debug=False, use_reloader=False)
        except Exception as exc:
            logger.error("Веб-сервер упал: %s", exc)
            alert_admin_error("Веб-сервер (health-check)", exc)

    threading.Thread(target=run, daemon=True, name="flask-health").start()
    logger.info("Health-check сервер запущен на порту %s", port_raw)


# ==========================================================
# 10. ТОЧКА ВХОДА
# ==========================================================

def main() -> None:
    init_db()
    start_web_server()

    try:
        me = bot.get_me()
        logger.info("Бот @%s запущен. ADMIN_ID=%s, TZ=UTC+%s", me.username, ADMIN_ID, TZ_OFFSET)
    except Exception as exc:
        logger.error("Не удалось проверить токен (get_me): %s", exc)
        alert_admin_error("Проверка токена (get_me)", exc)

    while True:
        try:
            bot.delete_webhook(drop_pending_updates=True)
            bot.infinity_polling(none_stop=True, timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as exc:
            logger.exception("Polling прервался: %s. Перезапуск через 10 сек...", exc)
            alert_admin_error("Polling (связь с Telegram)", exc)
            time.sleep(10)


if __name__ == "__main__":
    main()
