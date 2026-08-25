"""
Telegram-бот записи клиентов к частному мастеру (парикмахер / массажист / репетитор).
Стек: Python 3.11+, pyTelegramBotAPI (telebot), SQLite.
Запуск: TOKEN="8756700491:AAE6gA2oCcRMJE0d8hQnxIS20aLcDK1_Myk" ADMIN_ID="8636659463" python main.py   (или через файл .env рядом с main.py)
"""

import os
import re
import html
import time
import sqlite3
import logging
import threading
import schedule
import pytz
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types
last_portfolio_msg = {}  # Здесь будем хранить ID сообщений портфолио для каждого чата

# ==========================================================
#                    0.ЗАГРУЗКА НАСТРОЕК
# ==========================================================

def load_env_file(path: str = ".env") -> None:
    """Простая загрузка переменных из файла .env (без сторонних библиотек).
    Формат строки: KEY=VALUE. Уже существующие переменные окружения не перезатираются."""
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
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Не удалось прочитать {path}: {exc}")


load_env_file()

# ---------- логирование ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("booking-bot")


def env_int(name: str, default: int = 0) -> int:
    """Безопасное чтение целочисленной переменной окружения."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Переменная %s='%s' не является числом, использую %s", name, raw, default)
        return default


TOKEN = ("8756700491:AAE6gA2oCcRMJE0d8hQnxIS20aLcDK1_Myk")
if not TOKEN:
    raise SystemExit(
        "\n❌ ОШИБКА ЗАПУСКА: не задана переменная окружения TOKEN.\n"
        "   Windows (CMD):        set TOKEN=123456:ABC... && python main.py\n"
        "   Windows (PowerShell): $env:TOKEN='123456:ABC...'; python main.py\n"
        "   Linux/Mac:            TOKEN='123456:ABC...' python3 main.py\n"
        "   Либо создай файл .env рядом с main.py:\n"
        "       TOKEN=123456:ABC...\n"
        "       _ID=123456789\n"
    )

ADMIN_ID = env_int("ADMIN_ID",8636659463)
if ADMIN_ID == 0:
    logger.warning("ADMIN_ID не задан - админ-панель и уведомления мастеру будут отключены!")

DB_PATH = os.getenv("DB_PATH", "bookings.db")
TZ_OFFSET = env_int("TZ_OFFSET", 3)          # часовой пояс мастера (3 = Москва)
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))
MASTER_NAME = os.getenv("MASTER_NAME", "")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True)

# ---------- тексты кнопок главного меню ----------
BTN_BOOK = "📅 Записаться"
BTN_MY = "📋 Мои записи"
BTN_CANCEL = "❌ Отменить запись"
BTN_ADMIN = "⚙️ Админ-панель"

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MAX_MSG_LEN = 3500


# ==========================================================
#                    1. РАБОТА С БАЗОЙ SQLITE
# ==========================================================

DB_LOCK = threading.RLock()  # SQLite + многопоточный polling → сериализуем доступ


@contextmanager
def get_conn():
    """Контекстный менеджер соединения: коммит при успехе, откат при ошибке, всегда close()."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def q_all(sql: str, params: tuple = ()) -> list:
    """SELECT-запрос: список строк (при ошибке - пустой список)."""
    try:
        with get_conn() as conn:
            return conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка q_all: %s | SQL=%s", exc, sql)
        return []


def q_one(sql: str, params: tuple = ()):

    """SELECT-запрос: одна строка или None."""
    try:
        with get_conn() as conn:
            return conn.execute(sql, params).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка q_one: %s | SQL=%s", exc, sql)
        return None


def q_exec(sql: str, params: tuple = ()) -> int:
    """INSERT/UPDATE/DELETE: возвращает rowcount, при ошибке -1."""
    try:
        with get_conn() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка q_exec: %s | SQL=%s", exc, sql)
        return -1


def init_db() -> None:
    """Создаёт таблицы и индексы, если их ещё нет."""
    try:
        with get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS services (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    name     TEXT    NOT NULL,
                    duration INTEGER NOT NULL DEFAULT 60,
                    price    INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS slots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id  INTEGER NOT NULL,
                    date        TEXT    NOT NULL,           -- YYYY-MM-DD
                    time        TEXT    NOT NULL,           -- HH:MM
                    is_booked   INTEGER NOT NULL DEFAULT 0,
                    client_id   INTEGER,
                    client_name TEXT,
                    FOREIGN KEY (service_id) REFERENCES services (id)
                )
            """)
            # вспомогательная таблица: запоминаем имя клиента, чтобы не спрашивать каждый раз
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    user_id  INTEGER PRIMARY KEY,
                    name     TEXT,
                    username TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    master_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    caption TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    remind_time TEXT NOT NULL,
                    is_sent INTEGER NOT NULL DEFAULT 0
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    question_text TEXT NOT NULL,
                    answer_text TEXT,
                    is_answered INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            # Сразу добавляем стандартный текст FAQ в базу, если его ещё нет
            conn.execute("""
                INSERT OR IGNORE INTO config (key, value) VALUES ('faq_text', 
                    '💬 Вопросы и связь\n\nНапишите прямо в этот чат, и мастер ответит, как только освободится.\n\n<b>Часто спрашивают:</b>\n• можно ли с дизайном? — да\n• можно ли раньше/позже? — если есть окно, бот покажет\n• можно ли перенести запись? — да, через «Мои записи» или сообщением мастеру'
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_lookup ON slots (service_id, date, is_booked)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_client ON slots (client_id, is_booked)")
        # уникальность слота (одна услуга не может иметь два одинаковых времени)
        try:
            with get_conn() as conn:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_slots_unique ON slots (service_id, date, time)"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось создать уникальный индекс (возможно, есть дубли слотов): %s", exc)
        logger.info("База данных готова: %s", os.path.abspath(DB_PATH))
    except Exception as exc:  # noqa: BLE001
        logger.exception("КРИТИЧЕСКАЯ ошибка init_db: %s", exc)
        raise


# ==========================================================
#                    2. УТИЛИТЫ
# ==========================================================

def esc(text) -> str:
    """Экранирование текста для HTML-разметки Telegram."""
    return html.escape(str(text if text is not None else ""))


def now_local() -> datetime:
    """Текущее время в часовом поясе мастера."""
    return datetime.now(LOCAL_TZ)


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def time_str() -> str:
    return now_local().strftime("%H:%M")


def fmt_date(date_iso: str) -> str:
    """'2025-03-15' -> '15.03.2025 (Сб)'."""
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        return f"{d.strftime('%d.%m.%Y')} ({WEEKDAYS[d.weekday()]})"
    except Exception:  # noqa: BLE001
        return str(date_iso)


def fmt_date_short(date_iso: str) -> str:
    """'2025-03-15' -> '15.03 Сб'."""
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        return f"{d.strftime('%d.%m')} {WEEKDAYS[d.weekday()]}"
    except Exception:  # noqa: BLE001
        return str(date_iso)


def parse_date_input(text: str):
    """Понимает 'сегодня', 'завтра', '15.03', '15.03.2025', '2025-03-15'. Возвращает 'YYYY-MM-DD' или None."""
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
            if parsed < today:  # если дата уже прошла - считаем, что это следующий год
                parsed = parsed.replace(year=today.year + 1)
        return parsed.isoformat()
    return None


def parse_times_input(text: str):
    """'10:00, 11.30 13-00' -> ['10:00', '11:30', '13:00']. Возвращает (список_ок, список_ошибок)."""
    parts = [p for p in re.split(r"[,;\s]+", (text or "").strip()) if p]
    good, bad = [], []
    for part in parts:
        candidate = part.replace(".", ":").replace("-", ":")
        try:
            good.append(datetime.strptime(candidate, "%H:%M").strftime("%H:%M"))
        except ValueError:
            bad.append(part)
    # убираем дубли, сохраняя порядок
    unique = list(dict.fromkeys(good))
    return unique, bad
MAX_GEN_SLOTS = 400          # предохранитель: не даём создать 5000 слотов одной кнопкой


def norm_time(part: str):
    """'9' -> '09:00', '10.30' -> '10:30'. None, если мусор."""
    p = (part or "").strip().replace(".", ":")
    if p.isdigit():
        p = f"{int(p):02d}:00" if len(p) <= 2 else p
    try:
        return datetime.strptime(p, "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def parse_hours_range(text: str):
    """'10:00-19:00' / '10-19' / '10 до 19' -> ('10:00', '19:00'). None при ошибке."""
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
                   step_min: int, days: int, only_weekdays: bool = False):
    """Массовая генерация слотов.
    Возвращает (создано, пропущено_дублей, дней_задействовано, достигнут_лимит)."""
    t_start = datetime.strptime(start_hm, "%H:%M")
    t_end = datetime.strptime(end_hm, "%H:%M")
    today = now_local().date()
    now_hm = time_str()

    rows, days_used, capped = [], 0, False
    for offset in range(days):
        day = today + timedelta(days=offset)
        if only_weekdays and day.weekday() >= 5:      # 5=Сб, 6=Вс
            continue
        day_iso = day.isoformat()
        added_today = False
        cur = t_start
        # слот влезает, только если целиком укладывается до конца рабочего дня
        while cur + timedelta(minutes=step_min) <= t_end:
            hm = cur.strftime("%H:%M")
            cur += timedelta(minutes=step_min)
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

    # какие слоты уже есть — чтобы не плодить дубли
    dates = sorted({r[1] for r in rows})
    marks = ",".join("?" * len(dates))     # безопасно: подставляем только знаки ?, не данные
    existing = {
        (r["date"], r["time"])
        for r in q_all(f"SELECT date, time FROM slots WHERE service_id = ? AND date IN ({marks})",
                       (service_id, *dates))
    }
    fresh = [r for r in rows if (r[1], r[2]) not in existing]
    skipped = len(rows) - len(fresh)
    if not fresh:
        return 0, skipped, days_used, capped
    try:
        with get_conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO slots (service_id, date, time, is_booked) VALUES (?,?,?,0)",
                fresh,
            )
        logger.info("Генерация: сервис=%s создано=%s пропущено=%s", service_id, len(fresh), skipped)
        return len(fresh), skipped, days_used, capped
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка генерации слотов: %s", exc)
        return 0, skipped, days_used, capped


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def safe_send(chat_id: int, text: str, reply_markup=None):
    """Отправка сообщения с защитой от падений и авто-разбивкой длинного текста."""
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
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось отправить сообщение в %s: %s", chat_id, exc)
        return None


def safe_edit(call, text: str, reply_markup=None):
    """Редактирование сообщения по callback; если не вышло - отправляем новое."""
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("edit_message_text не удался (%s), отправляю новое сообщение", exc)
        safe_send(call.message.chat.id, text, reply_markup)


def notify_admin(text: str) -> None:
    """Уведомление мастера (если ADMIN_ID задан)."""
    if ADMIN_ID:
        safe_send(ADMIN_ID, text)


# ==========================================================
#                3. FSM (пошаговые диалоги)
# ==========================================================

user_states: dict = {}          # {user_id: {"step": str, "data": dict}}
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
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_BOOK)
    kb.row(BTN_MY, BTN_CANCEL)
    if is_admin(user_id):
        kb.row(BTN_ADMIN)
    # Новая кнопка для всех
    kb.row("📁 Портфолио")
    return kb


def services_kb(prefix: str) -> types.InlineKeyboardMarkup:
    """Список услуг inline-кнопками. prefix: 'svc' (клиент) или 'aslot' (админ)."""
    kb = types.InlineKeyboardMarkup()
    rows = q_all("SELECT id, name, duration, price FROM services ORDER BY id")
    for row in rows:
        kb.add(types.InlineKeyboardButton(
            text=f"{row['name']} - {row['price']}₽ · {row['duration']} мин",
            callback_data=f"{prefix}:{row['id']}",
        ))
    return kb


def admin_menu_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Добавить слот", callback_data="adm:add_slot"))
    kb.add(types.InlineKeyboardButton("🕳 Свободные слоты", callback_data="adm:free"))
    kb.add(types.InlineKeyboardButton("📆 Записи на сегодня", callback_data="adm:today"))
    kb.add(types.InlineKeyboardButton("📅 Расписание на неделю", callback_data="adm:gen_week"))
    kb.add(types.InlineKeyboardButton("📚 Все записи", callback_data="adm:all"))
    kb.add(types.InlineKeyboardButton("🧾 Добавить услугу", callback_data="adm:add_service"))
    kb.add(types.InlineKeyboardButton("❌ Удалить услугу", callback_data="adm:del_service"))  
    kb.add(types.InlineKeyboardButton("📊 Статистика", callback_data="adm:stats")) 
    kb.add(types.InlineKeyboardButton("📖 Команды", callback_data="adm:commands"))
    return kb


# ==========================================================
#              5. ЗАПРОСЫ К БД (бизнес-логика)
# ==========================================================

def get_service(service_id: int):
    return q_one("SELECT id, name, duration, price FROM services WHERE id = ?", (service_id,))


def get_slot(slot_id: int):
    return q_one("""
        SELECT s.id, s.service_id, s.date, s.time, s.is_booked, s.client_id, s.client_name,
               sv.name AS service_name, sv.price AS price, sv.duration AS duration
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.id = ?
    """, (slot_id,))


def free_dates(service_id: int) -> list:
    """Даты, где есть свободные будущие слоты для услуги."""
    return q_all("""
        SELECT date, COUNT(*) AS cnt
        FROM slots
        WHERE service_id = ? AND is_booked = 0
          AND (date > ? OR (date = ? AND time >= ?))
        GROUP BY date
        ORDER BY date
        LIMIT 30
    """, (service_id, today_str(), today_str(), time_str()))


def free_slots_on_date(service_id: int, date_iso: str) -> list:
    return q_all("""
        SELECT id, time
        FROM slots
        WHERE service_id = ? AND date = ? AND is_booked = 0
          AND (date > ? OR (date = ? AND time >= ?))
        ORDER BY time
    """, (service_id, date_iso, today_str(), today_str(), time_str()))


def my_bookings(user_id: int) -> list:
    return q_all("""
        SELECT s.id, s.date, s.time, sv.name AS service_name, sv.price AS price, sv.duration AS duration
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1 AND s.client_id = ?
          AND (s.date > ? OR (s.date = ? AND s.time >= ?))
        ORDER BY s.date, s.time
    """, (user_id, today_str(), today_str(), time_str()))


def all_bookings(only_today: bool = False) -> list:
    if only_today:
        return q_all("""
            SELECT s.id, s.date, s.time, s.client_id, s.client_name,
                   sv.name AS service_name, sv.price AS price
            FROM slots s LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 1 AND s.date = ?
            ORDER BY s.time
        """, (today_str(),))
    return q_all("""
        SELECT s.id, s.date, s.time, s.client_id, s.client_name,
               sv.name AS service_name, sv.price AS price
        FROM slots s LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1
          AND (s.date > ? OR (s.date = ? AND s.time >= ?))
        ORDER BY s.date, s.time
        LIMIT 100
    """, (today_str(), today_str(), time_str()))


def book_slot(slot_id: int, user_id: int, client_name: str) -> bool:
    """Атомарное бронирование: сработает только если слот всё ещё свободен."""
    try:
        with get_conn() as conn:
            cur = conn.execute("""
                UPDATE slots
                SET is_booked = 1, client_id = ?, client_name = ?
                WHERE id = ? AND is_booked = 0
            """, (user_id, client_name[:100], slot_id))
            return cur.rowcount == 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка бронирования слота %s: %s", slot_id, exc)
        return False


def unbook_slot(slot_id: int, user_id: int, force: bool = False) -> bool:
    """Отмена записи. force=True - админ может отменить любую."""
    try:
        with get_conn() as conn:
            if force:
                cur = conn.execute(
                    "UPDATE slots SET is_booked = 0, client_id = NULL, client_name = NULL "
                    "WHERE id = ? AND is_booked = 1", (slot_id,))
            else:
                cur = conn.execute(
                    "UPDATE slots SET is_booked = 0, client_id = NULL, client_name = NULL "
                    "WHERE id = ? AND is_booked = 1 AND client_id = ?", (slot_id, user_id))
            return cur.rowcount == 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка отмены слота %s: %s", slot_id, exc)
        return False


def save_client(user_id: int, name: str, username: str = "") -> None:
    q_exec("""
        INSERT INTO clients (user_id, name, username) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, username = excluded.username
    """, (user_id, (name or "").strip()[:100], (username or "")[:64]))


def get_client_name(user_id: int):
    row = q_one("SELECT name FROM clients WHERE user_id = ?", (user_id,))
    return row["name"] if row and row["name"] else None


# ==========================================================
#                 6. КОМАНДЫ И ГЛАВНОЕ МЕНЮ
# ==========================================================

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    clear_state(message.from_user.id)
    text = (
        f"🌸 Привет, {esc(message.from_user.first_name)}!\n\n"
        f"Я помощник мастера <b>{esc(MASTER_NAME)}</b> ✂️\n"
        "Помогу записаться на стрижку за 30 секунд — без звонков и переписок.\n\n"
        "✧ <b>Что я умею:</b>\n"
        f" {BTN_BOOK} — выбрать услугу и свободное окошко\n"
        f" {BTN_MY} — посмотреть свои записи\n"
        f" {BTN_CANCEL} — отменить, если планы поменялись\n\n"
        "📍 Адрес: ул. Ленина, 15, каб. 3\n"
        "📞 Связь: @Yumiii3_3"
    )
    safe_send(message.chat.id, text, main_menu(message.from_user.id))
    
    # Отдельное сообщение с Inline-кнопкой
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❓ Вопросы и связь", callback_data="faq_start"))
    safe_send(
        message.chat.id, 
        "Если у вас есть вопросы к мастеру или вы хотите с ним связаться, нажмите «Вопросы и связь».",
        reply_markup=kb
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
    safe_send(message.chat.id, "⚙️ <b>Админ-панель</b>\nВыбери действие:", admin_menu_kb())


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
    q_exec("UPDATE config SET value=? WHERE key='faq_text'", (new_text,))
    safe_send(message.chat.id, "✅ Текст FAQ обновлён! Теперь клиенты будут видеть его при нажатии на кнопку вопросов.")

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
    
    # 👇 ИЗМЕНЕНИЕ: используем enumerate, чтобы сделать сквозную нумерацию 1, 2, 3...
    for index, row in enumerate(rows, start=1):
        lines.append(f"🕒 {row['created_at']}\n💬 {row['question_text']}\n")
        # В тексте кнопки теперь (вопрос №1), (вопрос №2)...
        # Но в callback_data мы передаём реальный row['id'], чтобы бот знал, что именно удалять!
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
        "🔹 <b>/my_reminders</b> — посмотреть активные напоминания\n"
        "🔹 <b>/myid</b> — узнать свой Telegram ID\n"
        "🔹 <b>/admin</b> — открыть админ-панель\n"
        "🔹 <b>/info</b> — показать эту шпаргалку\n\n"
        "🔹 <b>/edit_faq</b> — позволяет поменять часто задаваемые вопросы в «Вопросы и связь»\n\n"
		"🔹 <b>//my_q</b> — посмотреть все новые вопросы»\n\n"
        "📍 <i>Все команды работают только для мастера.</i>"
    )
    safe_send(message.chat.id, text, main_menu(message.from_user.id))

@bot.message_handler(content_types=["photo"], func=lambda m: is_admin(m.from_user.id))
def catch_file_id(message):
    """Админ шлёт фото — бот возвращает его file_id для вставки в .env"""
    file_id = message.photo[-1].file_id  # [-1] = самое большое разрешение
    safe_send(message.chat.id,
              f"<b>file_id получен:</b>\n<code>{file_id}</code>")

@bot.message_handler(func=lambda m: m.text == BTN_ADMIN)
def on_admin_button(message):
    clear_state(message.from_user.id)
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Раздел доступен только мастеру.", main_menu(message.from_user.id))
        return
    safe_send(message.chat.id, "⚙️ <b>Админ-панель</b>\nВыбери действие:", admin_menu_kb())


@bot.message_handler(func=lambda m: m.text == BTN_BOOK)
def on_book(message):
    clear_state(message.from_user.id)
    kb = services_kb("svc")
    if not kb.keyboard:
        safe_send(message.chat.id, "😔 Пока нет доступных услуг. Мастер скоро их добавит.",
                  main_menu(message.from_user.id))
        return
    safe_send(message.chat.id, "💅 <b>Выбери услугу:</b>", kb)


@bot.message_handler(func=lambda m: m.text == BTN_MY)
def on_my(message):
    clear_state(message.from_user.id)
    rows = my_bookings(message.from_user.id)
    if not rows:
        safe_send(message.chat.id, "📭 У тебя пока нет активных записей.", main_menu(message.from_user.id))
        return
    lines = ["📋 <b>Твои записи:</b>", ""]
    for row in rows:
        lines.append(f"• <b>{esc(row['service_name'] or 'услуга')}</b>\n"
            f"  🗓 {fmt_date(row['date'])} в {esc(row['time'])}\n"
            f"  ⏱ {row['duration'] or 0} мин · 💰 {row['price'] or 0}₽")
    safe_send(message.chat.id, "\n".join(lines), main_menu(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == BTN_CANCEL)
def on_cancel_booking(message):
    clear_state(message.from_user.id)
    rows = my_bookings(message.from_user.id)
    if not rows:
        safe_send(message.chat.id, "📭 Нет записей, которые можно отменить.", main_menu(message.from_user.id))
        return
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        kb.add(types.InlineKeyboardButton(text=f"🗑 {fmt_date_short(row['date'])} {row['time']} - {row['service_name'] or 'услуга'}",
            callback_data=f"cxl:{row['id']}",))
    safe_send(message.chat.id, "❌ <b>Какую запись отменить?</b>", kb)

@bot.message_handler(func=lambda m: m.text == "💬 Вопросы и связь")
def faq_contact(message):
    # 1. Загружаем текущий FAQ-текст из базы (мы добавили таблицу config)
    row = q_one("SELECT value FROM config WHERE key='faq_text'")
    faq_text = row["value"] if row else "Текст FAQ не найден."
    
    # 2. Отправляем FAQ клиенту
    safe_send(message.chat.id, faq_text)
    
    # 3. Просим клиента написать вопрос
    msg = bot.send_message(message.chat.id, "✍️ Если у тебя есть вопрос, напиши его прямо сейчас. Мастер ответит, как только освободится.")
    bot.register_next_step_handler(msg, save_client_question)

def save_client_question(message):
    # Если пользователь передумал и написал /cancel, это обработается в on_text
    question = message.text.strip()
    if len(question) < 3:
        safe_send(message.chat.id, "❓ Вопрос слишком короткий. Напиши подробнее.")
        return
    
    # Сохраняем вопрос в базу данных (таблицу questions)
    q_exec("INSERT INTO questions (user_id, question_text, created_at) VALUES (?, ?, ?)",
           (message.from_user.id, question, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    
    # Отправляем подтверждение и ВОЗВРАЩАЕМ ГЛАВНОЕ МЕНЮ
    safe_send(message.chat.id, "✅ Твой вопрос отправлен мастеру! Он ответит в ближайшее время.", reply_markup=main_menu(message.from_user.id))

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Статистика доступна только мастеру.")
        return

    # ---------- Статистика за сегодня ----------
    today = today_str()
    rows_today = q_all("""
        SELECT s.time, s.client_name, sv.name AS service_name, sv.price
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1 AND s.date = ?
        ORDER BY s.time
    """, (today,))

    count_today = len(rows_today)
    revenue_today = sum(row["price"] or 0 for row in rows_today)

    # ---------- Статистика за неделю ----------
    week_start = (now_local() - timedelta(days=7)).strftime("%Y-%m-%d")
    rows_week = q_all("""
        SELECT COUNT(*) AS cnt, SUM(sv.price) AS total_revenue
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1 AND s.date >= ? AND s.date <= ?
    """, (week_start, today))

    count_week = rows_week[0]["cnt"] if rows_week else 0
    revenue_week = rows_week[0]["total_revenue"] or 0 if rows_week else 0

    # ---------- Самая популярная услуга ----------
    popular = q_one("""
        SELECT sv.name, COUNT(*) AS cnt
        FROM slots s
        LEFT JOIN services sv ON sv.id = s.service_id
        WHERE s.is_booked = 1
        GROUP BY sv.name
        ORDER BY cnt DESC
        LIMIT 1
    """)

    # ---------- Формируем сообщение ----------
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

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "⛔️ Рассылку может делать только мастер.")
        return
    
    # Отделяем команду от текста сообщения
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        safe_send(message.chat.id, "📝 Напиши текст рассылки. Например: /broadcast Скидка 20% на этой неделе!")
        return
        
    text = parts[1]
    safe_send(message.chat.id, "⏳ Начинаю рассылку... Это может занять несколько секунд.")
    
    # Получаем всех клиентов, которые когда-либо писали боту
    rows = q_all("SELECT user_id FROM clients")
    
    if not rows:
        safe_send(message.chat.id, "📭 У тебя пока нет клиентов для рассылки.")
        return
        
    success = 0
    fail = 0
    
    for row in rows:
        try:
            bot.send_message(row["user_id"], 
                f"📢 <b>Рассылка от мастера:</b>\n\n{text}", 
                parse_mode='HTML'
            )
            success += 1
            # Задержка, чтобы Telegram не заблокировал бота за спам
            time.sleep(0.05) 
        except Exception:
            fail += 1
            
    safe_send(message.chat.id, 
        f"✅ Рассылка завершена!\n"
        f"📨 Отправлено: {success}\n"
        f"❌ Не доставлено (пользователь удалил бота): {fail}"
    )

@bot.message_handler(func=lambda m: m.text == "📁 Портфолио")
def show_portfolio(message):
    chat_id = message.chat.id
    
    # Удаляем старые сообщения портфолио, если они есть
    if chat_id in last_portfolio_msg:
        for msg_id in last_portfolio_msg[chat_id]:
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
    
    rows = q_all("SELECT file_id, caption FROM portfolio")
    if not rows:
        safe_send(message.chat.id, "📭 У мастера пока нет фото в портфолио.", main_menu(message.from_user.id))
        return
    
    chunk_size = 9
    chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
    
    # Отправляем первую группу и получаем ID отправленных сообщений
    msg_ids = send_portfolio_chunk(message.chat.id, chunks[0], 0, len(chunks))
    last_portfolio_msg[chat_id] = msg_ids

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

    rows = q_all("SELECT id, file_id, caption FROM portfolio WHERE master_id=?", (message.from_user.id,))
    if not rows:
        safe_send(message.chat.id, "📭 В портфолио пока нет фото для удаления.", main_menu(message.from_user.id))
        return

    chunk_size = 9
    chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]

    send_del_portfolio_chunk(message.chat.id, chunks[0], 0, len(chunks))
	
def send_del_portfolio_chunk(chat_id, chunk, chunk_index, total_chunks):
    # Группируем фото в альбом
    media_group = []
    kb_list = []
    for index, row in enumerate(chunk):
        media_group.append(types.InputMediaPhoto(media=row["file_id"]))
        # Создаём кнопку удаления с порядковым номером
        kb_list.append(types.InlineKeyboardButton(
            f"🗑️ Удалить фото {index + 1}",
            callback_data=f"delport:{row['id']}"
        ))

    # Отправляем альбом
    bot.send_media_group(chat_id, media_group)

    # Отправляем кнопки под альбомом
    kb = types.InlineKeyboardMarkup(row_width=1)
    for btn in kb_list:
        kb.add(btn)
    
    # Добавляем кнопки навигации (Назад / Вперёд)
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
    q_exec("INSERT INTO portfolio (master_id, file_id, caption) VALUES (?, ?, ?)",
           (message.from_user.id, file_id, caption))
    safe_send(message.chat.id, "✅ Фото добавлено в портфолио!", main_menu(message.from_user.id))
# ==========================================================
#
# 7. CALLBACK-ОБРАБОТЧИК (единая точка)
# ==========================================================

@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:  # noqa: BLE001
        pass

    data = call.data or ""
    user_id = call.from_user.id

    try:
        # ---------- клиент: выбрал услугу ----------
        if data.startswith("svc:"):
            show_dates(call, int(data.split(":")[1]))
        # ---------- клиент: выбрал дату ----------
        elif data.startswith("day:"):
            _, sid, date_iso = data.split(":", 2)
            show_times(call, int(sid), date_iso)
        # ---------- клиент: выбрал время ----------
        elif data.startswith("slot:"):
            start_confirm(call, int(data.split(":")[1]))
        # ---------- клиент: подтвердил ----------
        elif data.startswith("conf:"):
            do_book(call, int(data.split(":")[1]))
        # ---------- клиент: назад к списку услуг ----------
        elif data == "back:services":
            # Снова показываем список услуг
            kb = services_kb("svc")
            if not kb.keyboard:
                safe_edit(call, "😔 Пока нет доступных услуг.", main_menu(user_id))
                return
            safe_edit(call, "💅 <b>Выбери услугу:</b>", kb)
               # ---------- клиент: добавить в календарь ----------
        elif data.startswith("cal:"):
            slot_id = int(data.split(":")[1])
            slot = get_slot(slot_id)
            if not slot:
                safe_edit(call, "😔 Запись не найдена.")
                return
            
            import urllib.parse
            import datetime as dt
            
            title = f"Запись: {slot['service_name']}"
            start_dt = f"{slot['date']}T{slot['time']}:00"
            # Длительность 1 час (или можно взять из slot['duration'])
            end_dt = (dt.datetime.strptime(f"{slot['date']} {slot['time']}", "%Y-%m-%d %H:%M") + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:00")
            
            # Специальная ссылка для Telegram (открывает нативный календарь)
            # Работает и на Android, и на iOS без регистрации
            ical_string = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:{title}
DTSTART:{start_dt}
DTEND:{end_dt}
END:VEVENT
END:VCALENDAR"""
            
            # Кодируем в ссылку
            encoded = urllib.parse.quote(ical_string)
            calendar_url = f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={urllib.parse.quote(title)}&dates={start_dt}/{end_dt}"
            
            bot.answer_callback_query(call.id, "📅 Открываю календарь...")
            
            kb = types.InlineKeyboardMarkup()
            btn_cal = types.InlineKeyboardButton(
                "📅 Добавить в календарь (в телефоне)",
                url=calendar_url
            )
            kb.add(btn_cal)
            
            safe_send(call.message.chat.id, 
                "📅 <b>Добавь запись в календарь:</b>\n\n"
                "Нажми на кнопку ниже, и событие появится в твоём календаре.\n"
                "Никаких регистраций — просто нажми и сохрани.",
                reply_markup=kb
            )
        # ---------- мастер: удалить фото из портфолио ----------
        elif data.startswith("delport:"):
            photo_id = int(data.split(":")[1])
            q_exec("DELETE FROM portfolio WHERE id=?", (photo_id,))
            bot.answer_callback_query(call.id, "🗑️ Фото удалено. Обновляю список...")
            # Заново вызываем команду, чтобы показать новый список с новой нумерацией
            cmd_del_photo(call.message)

        # ---------- мастер: листать портфолио для удаления ----------
        elif data.startswith("delport_nav:"):
            chunk_index = int(data.split(":")[1])
            rows = q_all("SELECT id, file_id, caption FROM portfolio")
            chunk_size = 9
            chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
            
            send_del_portfolio_chunk(call.message.chat.id, chunks[chunk_index], chunk_index, len(chunks))
            bot.answer_callback_query(call.id, "📂 Загружаю следующую партию...")

                # ---------- клиент: листать портфолио ----------
		        elif data.startswith("port:"):
            chunk_index = int(data.split(":")[1])
            rows = q_all("SELECT file_id, caption FROM portfolio")
            chunk_size = 9
            chunks = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
            
            # Защита: если страницы не существует, переходим на последнюю
            if chunk_index >= len(chunks):
                chunk_index = len(chunks) - 1
                
            send_portfolio_chunk(call.message.chat.id, chunks[chunk_index], chunk_index, len(chunks))
            bot.answer_callback_query(call.id, "📁 Загружаю следующее...")
		
        # ---------- мастер: ответить на вопрос клиента ----------
        elif data.startswith("ans_q:"):
            q_id = int(data.split(":")[1])
            row = q_one("SELECT user_id, question_text FROM questions WHERE id=?", (q_id,))
            if not row:
                safe_edit(call, "❌ Вопрос не найден.", admin_menu_kb())
                return
            # Сохраняем ID вопроса в состояние, чтобы потом знать, кому ответить
            set_state(call.from_user.id, "reply_to_q", q_id=q_id, client_id=row["user_id"])
            msg = bot.send_message(call.from_user.id, f"✍️ Напиши ответ на вопрос клиента:\n\n💬 {row['question_text']}")
            bot.register_next_step_handler(msg, save_master_reply)

        # ---------- клиент: открыть Вопросы и связь ----------
        elif data == "faq_start":
            row = q_one("SELECT value FROM config WHERE key='faq_text'")
            faq_text = row["value"] if row else "Текст FAQ не найден."
            
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⬅️ Вернуться", callback_data="back_to_main"))
            
            safe_edit(call, faq_text, reply_markup=kb)
            
            set_state(call.from_user.id, "await_client_question")
            safe_send(call.message.chat.id, "✍️ Если у тебя есть вопрос, напиши его прямо сейчас. Мастер ответит, как только освободится.")
        # ---------- клиент: вернуться к приветствию ----------
        elif data == "back_to_main":
            clear_state(call.from_user.id)
            # Мы не будем отправлять /start заново, а просто покажем главное меню
            # Это аккуратнее, чем снова городить весь текст приветствия.
            safe_edit(call, "🏠 Главное меню", main_menu(call.from_user.id))

        # ---------- клиент: отказался ----------
        elif data == "abort":
            clear_state(user_id)
            safe_edit(call, "🚫 Запись отменена. Можешь выбрать другое время: " + BTN_BOOK)
        # ---------- клиент: отмена существующей записи ----------
        elif data.startswith("cxl:"):
            do_unbook(call, int(data.split(":")[1]))
        # ---------- админ ----------
        elif data.startswith("adm:") or data.startswith("aslot:") or data.startswith("adel:") or data.startswith("gsvc:") or data.startswith("delsvc:"):
            if not is_admin(user_id):
                safe_edit(call, "⛔️ Доступно только мастеру.")
                return
            handle_admin_callback(call, data)
        else:
            safe_edit(call, "🤔 Не понял кнопку. Открой меню командой /start")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка в callback '%s': %s", data, exc)
        safe_send(user_id, "⚠️ Что-то пошло не так. Попробуй ещё раз или напиши /start")


# --------------- шаги клиентского сценария ----------------
def save_master_reply(message):
    state = get_state(message.from_user.id)
    if not state or state.get("step") != "reply_to_q":
        return
    q_id = state["data"]["q_id"]
    client_id = state["data"]["client_id"]
    answer = message.text.strip()

    if len(answer) < 2:
        safe_send(message.chat.id, "Ответ слишком короткий. Попробуй ещё раз через /my_q.")
        return

    # Сохраняем ответ в базу и помечаем как отвеченный
    q_exec("UPDATE questions SET answer_text=?, is_answered=1 WHERE id=?", (answer, q_id))
    clear_state(message.from_user.id)
    safe_send(message.chat.id, "✅ Ответ сохранён! Он отправлен клиенту.")

    # Отправляем ответ клиенту
    try:
        q = q_one("SELECT question_text FROM questions WHERE id=?", (q_id,))
        if q:
            bot.send_message(client_id, f"📬 <b>Ответ от мастера:</b>\n\n💬 Ваш вопрос: {q['question_text']}\n✍️ Ответ: {answer}")
        else:
            logger.warning("Попытка ответить на уже удалённый вопрос.")
    except Exception as e:
        logger.error(f"Не удалось отправить ответ клиенту: {e}")

def show_dates(call, service_id: int) -> None:
    """Шаг 2: свободные даты для выбранной услуги."""
    service = get_service(service_id)
    if not service:
        safe_edit(call, "😔 Услуга не найдена. Нажми «📅 Записаться» ещё раз.")
        return
    dates = free_dates(service_id)
    if not dates:
        safe_edit(call, f"😔 На услугу <b>{esc(service['name'])}</b> пока нет свободного времени.\n"
                        f"Загляни позже или напиши мастеру.")
        return
    kb = types.InlineKeyboardMarkup()
    for row in dates:
        kb.add(types.InlineKeyboardButton(text=f"🗓 {fmt_date_short(row['date'])} · свободно: {row['cnt']}",
            callback_data=f"day:{service_id}:{row['date']}",))

    safe_edit(call, f"💅 Услуга: <b>{esc(service['name'])}</b> - {service['price']}₽ "
                    f"({service['duration']} мин)\n\n🗓 <b>Выбери дату:</b>", kb)
    kb.add(types.InlineKeyboardButton("◀️ К услугам", callback_data="back:services"))

def show_times(call, service_id: int, date_iso: str) -> None:
    """Шаг 3: свободное время на выбранную дату."""
    service = get_service(service_id)
    slots = free_slots_on_date(service_id, date_iso)
    if not service or not slots:
        safe_edit(call, "😔 На эту дату время уже разобрали. Выбери другую дату.")
        return
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[types.InlineKeyboardButton(text=f"🕒 {row['time']}", callback_data=f"slot:{row['id']}")
             for row in slots])
    kb.add(types.InlineKeyboardButton("◀️ К датам", callback_data=f"svc:{service_id}"))
    safe_edit(call, f"💅 <b>{esc(service['name'])}</b>\n🗓 {fmt_date(date_iso)}\n\n"
                    f"🕒 <b>Выбери время:</b>", kb)


def confirm_text(slot, client_name: str) -> str:
    return ("🧾 <b>Проверь запись:</b>\n\n"
        f"👤 Имя: <b>{esc(client_name)}</b>\n"
        f"💅 Услуга: <b>{esc(slot['service_name'] or 'услуга')}</b>\n"
        f"🗓 Дата: <b>{fmt_date(slot['date'])}</b>\n"
        f"🕒 Время: <b>{esc(slot['time'])}</b>\n"
        f"⏱ Длительность: {slot['duration'] or 0} мин\n"
        f"💰 Стоимость: {slot['price'] or 0}₽\n\n"
        "Всё верно?")


def confirm_kb(slot_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    
    # Получаем данные о слоте, чтобы потом отправить их в календарь
    slot = get_slot(slot_id)
    service_name = slot['service_name'] if slot else 'Услуга'
    date_iso = slot['date'] if slot else ''
    time_str = slot['time'] if slot else ''
    
    # Основная кнопка подтверждения
    btn_confirm = types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"conf:{slot_id}")
    
    # Новая кнопка календаря (Telegram сам откроет календарь у клиента)
    btn_calendar = types.InlineKeyboardButton("📅 Добавить в календарь", callback_data=f"cal:{slot_id}")
    
    # Кнопка отмены
    btn_cancel = types.InlineKeyboardButton("🚫 Отмена", callback_data="abort")
    
    # Добавляем кнопки в два ряда
    kb.row(btn_confirm, btn_calendar)
    kb.row(btn_cancel)
    
    return kb


def start_confirm(call, slot_id: int) -> None:
    """Шаг 4: если имя неизвестно - спрашиваем; иначе сразу подтверждение."""
    slot = get_slot(slot_id)
    if not slot or slot["is_booked"]:
        safe_edit(call, "😔 Это время только что заняли. Выбери, пожалуйста, другое.")
        return
    user_id = call.from_user.id
    name = get_client_name(user_id)
    if not name:
        set_state(user_id, "await_name", slot_id=slot_id)
        safe_edit(call, "✍️ Напиши, пожалуйста, <b>своё имя</b> (как к тебе обращаться):")
        return
    if name:
        set_state(user_id, "await_comment", slot_id=slot_id)
        # Создаём временную клавиатуру с одной кнопкой
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("➡️ Далее")
        safe_edit(call, 
            "✍️ Есть ли пожелания к записи? (Например, дизайн, время).\n"
            "Если нет — просто нажми кнопку <b>«➡️ Далее»</b> внизу.",
            reply_markup=kb
        )
        return  
    safe_edit(call, confirm_text(slot, name), confirm_kb(slot_id))


def do_book(call, slot_id: int) -> None:
    """Шаг 5: бронируем, уведомляем клиента и мастера."""
    user_id = call.from_user.id
    clear_state(user_id)
    name = get_client_name(user_id) or (call.from_user.first_name or "Клиент")
    slot = get_slot(slot_id)
    if not slot:
        safe_edit(call, "😔 Слот не найден. Попробуй записаться заново.")
        return
    if not book_slot(slot_id, user_id, name):
        safe_edit(call, "😔 Увы, это время только что забронировали. Выбери другое: " + BTN_BOOK)
        return

    safe_edit(call, "✅ <b>Готово! Ты записан(а) к</b> "
                    f"{esc(MASTER_NAME)}.\n\n"
                    f"💅 {esc(slot['service_name'] or 'услуга')}\n"
                    f"🗓 {fmt_date(slot['date'])} в {esc(slot['time'])}\n"
                    f"💰 {slot['price'] or 0}₽\n\n"
                    "Если планы изменятся — нажми «❌ Отменить запись».")
    safe_send(call.message.chat.id, "Ждём тебя! 💫", main_menu(user_id))

    # Автоматически создаём напоминание за 1 час до записи
    from datetime import timedelta

    booking_time = datetime.strptime(f"{slot['date']} {slot['time']}", "%Y-%m-%d %H:%M")
    remind_time = booking_time - timedelta(hours=1)
    remind_time_str = remind_time.strftime("%H:%M")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO reminders (user_id, text, remind_time) VALUES (?, ?, ?)", 
                    (user_id, f"Напоминаем: через час у вас запись на {slot['service_name']} к {MASTER_NAME}", remind_time_str))
        conn.commit()

    username = f"@{call.from_user.username}" if call.from_user.username else "—"
    notify_admin("🔔 <b>НОВАЯ ЗАПИСЬ</b>\n\n"
        f"👤 {esc(name)} ({esc(username)}, id {user_id})\n"
        f"💅 {esc(slot['service_name'] or 'услуга')}\n"
        f"🗓 {fmt_date(slot['date'])} в {esc(slot['time'])}\n"
        f"💰 {slot['price'] or 0}₽ · ⏱ {slot['duration'] or 0} мин")
    logger.info("Запись: slot=%s user=%s", slot_id, user_id)


def do_unbook(call, slot_id: int) -> None:
    """Отмена записи клиентом."""
    user_id = call.from_user.id
    slot = get_slot(slot_id)
    if not slot or not slot["is_booked"] or slot["client_id"] != user_id:
        safe_edit(call, "🤔 Эта запись уже неактивна.")
        return
    if not unbook_slot(slot_id, user_id):
        safe_edit(call, "⚠️ Не удалось отменить запись. Попробуй ещё раз.")
        return
    safe_edit(call, "✅ Запись отменена. Спасибо, что предупредили! 🙏\n"
                    f"💅 {esc(slot['service_name'] or 'услуга')}\n"
                    f"🗓 {fmt_date(slot['date'])} в {esc(slot['time'])}\n\n"
                    "Если захотите вернуться — просто нажмите «📅 Записаться».")
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
        safe_edit(call, "⚙️ <b>Админ-панель</b>\nВыбери действие:", admin_menu_kb())

    elif data == "adm:add_service":
        set_state(user_id, "svc_name")
        safe_edit(call, "🧾 <b>Новая услуга</b>\n\nШаг 1/3. Введи название (например: «Стрижка мужская»):\n"
                        "Отмена — /cancel")

    elif data == "adm:add_slot":
        kb = services_kb("aslot")
        if not kb.keyboard:
            safe_edit(call, "Сначала добавь хотя бы одну услугу.", admin_menu_kb())
            return
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
        safe_edit(call, "➕ <b>Новый слот</b>\n\nДля какой услуги?", kb)

    elif data == "adm:today":
        rows = all_bookings(only_today=True)
        if not rows:
            safe_edit(call, "📆 На сегодня записей нет. Отдыхай 😎", admin_menu_kb())
            return
        lines = [f"📆 <b>Записи на {fmt_date(today_str())}:</b>", ""]
        total = 0
        for row in rows:
            total += row["price"] or 0
            lines.append(f"🕒 <b>{esc(row['time'])}</b> — {esc(row['client_name'] or '—')} · "
                         f"{esc(row['service_name'] or 'услуга')} · {row['price'] or 0}₽")
        lines.append(f"\n💰 Итого за день: <b>{total}₽</b>")
        safe_edit(call, "\n".join(lines), admin_menu_kb())

    elif data == "adm:all":
        rows = all_bookings()
        if not rows:
            safe_edit(call, "📚 Активных записей нет.", admin_menu_kb())
            return
        lines = ["📚 <b>Все активные записи:</b>", ""]
        current_date = None
        for row in rows:
            if row["date"] != current_date:
                current_date = row["date"]
                lines.append(f"\n<b>🗓 {fmt_date(current_date)}</b>")
            lines.append(f"  🕒 {esc(row['time'])} — {esc(row['client_name'] or '—')} · "
                         f"{esc(row['service_name'] or 'услуга')} · {row['price'] or 0}₽")
        safe_send(call.message.chat.id, "\n".join(lines))
        safe_send(call.message.chat.id, "⚙️ Меню:", admin_menu_kb())

    elif data == "adm:free":
        rows = q_all("""
            SELECT s.id, s.date, s.time, sv.name AS service_name
            FROM slots s LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 0 AND (s.date > ? OR (s.date = ? AND s.time >= ?))
            ORDER BY s.date, s.time LIMIT 40
        """, (today_str(), today_str(), time_str()))
        if not rows:
            safe_edit(call, "🕳 Свободных слотов нет — добавь новые.", admin_menu_kb())
            return
        kb = types.InlineKeyboardMarkup()
        for row in rows:
            kb.add(types.InlineKeyboardButton(text=f"🗑 {fmt_date_short(row['date'])} {row['time']} — {row['service_name'] or 'услуга'}",
                callback_data=f"adel:{row['id']}",))
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
        safe_edit(call, "🕳 <b>Свободные слоты</b>\nНажми, чтобы удалить слот:", kb)

    elif data == "adm:del_service":
        rows = q_all("SELECT id, name FROM services ORDER BY id")
        if not rows:
            safe_edit(call, "📭 Нет услуг для удаления.", admin_menu_kb())
            return
        kb = types.InlineKeyboardMarkup()
        for row in rows:
            kb.add(types.InlineKeyboardButton(text=f"❌ {row['name']}", callback_data=f"delsvc:{row['id']}"))
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
        safe_edit(call, "❌ <b>Какую услугу удалить?</b>", kb)
    elif data == "adm:stats":
        user_id = call.from_user.id
        today = today_str()
        rows_today = q_all("""
            SELECT s.time, s.client_name, sv.name AS service_name, sv.price
            FROM slots s
            LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 1 AND s.date = ?
            ORDER BY s.time
        """, (today,))

        count_today = len(rows_today)
        revenue_today = sum(row["price"] or 0 for row in rows_today)

        week_start = (now_local() - timedelta(days=7)).strftime("%Y-%m-%d")
        rows_week = q_all("""
            SELECT COUNT(*) AS cnt, SUM(sv.price) AS total_revenue
            FROM slots s
            LEFT JOIN services sv ON sv.id = s.service_id
            WHERE s.is_booked = 1 AND s.date >= ? AND s.date <= ?
        """, (week_start, today))

        count_week = rows_week[0]["cnt"] if rows_week else 0
        revenue_week = rows_week[0]["total_revenue"] or 0 if rows_week else 0

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

        safe_edit(call, "\n".join(lines), admin_menu_kb())

    elif data.startswith("delsvc:"):
        service_id = int(data.split(":")[1])
        q_exec("DELETE FROM slots WHERE service_id=?", (service_id,))
        q_exec("DELETE FROM services WHERE id=?", (service_id,))
        safe_edit(call, "🗑 Услуга и все её слоты удалены.", admin_menu_kb())
       
    elif data.startswith("adel:"):
        slot_id = int(data.split(":")[1])
        rowcount = q_exec("DELETE FROM slots WHERE id = ? AND is_booked = 0", (slot_id,))
        if rowcount == 1:
            safe_edit(call, "🗑 Слот удалён.", admin_menu_kb())
        else:
            safe_edit(call, "⚠️ Не удалось удалить (возможно, слот уже занят).", admin_menu_kb())
    elif data == "adm:gen_week":
        kb = services_kb("gsvc")
        if not kb.keyboard:
            safe_edit(call, "Сначала добавь хотя бы одну услугу.", admin_menu_kb())
            return
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:menu"))
        safe_edit(call, "📅 <b>Расписание на неделе</b>\n\n"
                        "Создаём все слоты сразу на несколько дней вперёд.\n"
                        "Для какой услуги?", kb)

    elif data.startswith("gsvc:"):
        service_id = int(data.split(":")[1])
        service = get_service(service_id)
        if not service:
            safe_edit(call, "Услуга не найдена.", admin_menu_kb())
            return
        set_state(user_id, "gen_hours", service_id=service_id)
        safe_edit(call, f"💅 Услуга: <b>{esc(service['name'])}</b>\n\n"
                        "Шаг 1/3. Твои рабочие часы:\n"
                        "<code>10:00-19:00</code>\n\n"
                        "(можно короче: 10-19)\n\n"
                        "📅 <b>На сколько дней вперёд создать слоты?</b>\n"
                        "Шаг 2/3. Введи количество дней (например: 7):\n"
                        "Отмена — /cancel")

    elif data == "adm:commands":
        text = (
            "📖 <b>Команды мастера</b>\n\n"
            "✧ <b>Фото:</b>\n"
            "📸 /add_photo — добавить фото в портфолио\n"
            "🗑️ /del_photo — открыть портфолио для удаления\n\n"
            "✧ <b>Рассылка:</b>\n"
            "📢 /broadcast — отправить сообщение всем клиентам\n\n"
            "✧ <b>Статистика:</b>\n"
            "📊 /stats — посмотреть записи и выручку\n\n"
            "✧ <b>Настройки:</b>\n"
            "⚙️ /admin — открыть админ-панель\n"
            "🆔 /myid — узнать свой Telegram ID\n\n"
            "❓/edit_faq — изменить текст «Вопросы и связь»\n\n"
			"💬/my_q — посмотреть все новые вопросы»\n\n"
            "📍 <i>Нажми на любую команду, чтобы выполнить её.</i>"
		)
        safe_edit(call, text, admin_menu_kb())


    elif data.startswith("aslot:"):
        service_id = int(data.split(":")[1])
        service = get_service(service_id)
        if not service:
            safe_edit(call, "Услуга не найдена.", admin_menu_kb())
            return
        set_state(user_id, "slot_date", service_id=service_id)
        safe_edit(call, f"➕ Услуга: <b>{esc(service['name'])}</b>\n\n"
                        "Шаг 1/2. Введи дату:\n"
                        "сегодня / завтра / 15.03 / 15.03.2025 / 2025-03-15\n"
                        "Отмена — /cancel")


# ==========================================================
#
# 8. ТЕКСТОВЫЙ ОБРАБОТЧИК ДЛЯ ПОШАГОВЫХ ДИАЛОГОВ
# ==========================================================

@bot.message_handler(content_types=["text"])
def on_text(message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    state = get_state(user_id)

    # --- нет активного диалога ---
    if not state:
        safe_send(message.chat.id, "🤖 Выбери действие в меню 👇", main_menu(user_id))
        return

    step = state["step"]
    data = state["data"]

    try:
        # ---------- клиент вводит своё имя ----------
        if step == "await_name":
            if len(text) < 2 or len(text) > 60:
                safe_send(message.chat.id, "✍️ Имя должно быть от 2 до 60 символов. Попробуй ещё раз:")
                return
            save_client(user_id, text, message.from_user.username or "")
            clear_state(user_id)
            slot = get_slot(int(data.get("slot_id", 0)))
            if not slot or slot["is_booked"]:
                safe_send(message.chat.id, "😔 Это время уже заняли. Выбери другое 👇", main_menu(user_id))
                return
            set_state(user_id, "await_comment", slot_id=slot_id)
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add("➡️ Далее")
            safe_send(message.chat.id, 
                "✍️ Есть ли пожелания к записи? (Например, дизайн, время).\n"
                "Если нет — просто нажми кнопку <b>«➡️ Далее»</b> внизу.",
                reply_markup=kb
            )
            return

        # ---------- клиент: вводит комментарий ----------
        if step == "await_comment":
            # Если нажали кнопку "Далее" или отправили "-" — пропускаем комментарий
            if text == "➡️ Далее" or text == "-":
                comment = "-"
            else:
                comment = text
            
            set_state(user_id, "await_confirm", slot_id=data["slot_id"], comment=comment)
            slot = get_slot(data["slot_id"])
            name = get_client_name(user_id) or "Клиент"
            # Показываем подтверждение с комментарием
            safe_send(message.chat.id, confirm_text_with_comment(slot, name, comment), confirm_kb(slot["id"]))
            return

                        # ---------- клиент: отправляет вопрос ----------
        if step == "await_client_question":
            question = text.strip()
            if len(question) < 3:
                safe_send(message.chat.id, "❓ Вопрос слишком короткий. Напиши подробнее.")
                return
            # Сохраняем вопрос в базу
            q_exec("INSERT INTO questions (user_id, question_text, created_at) VALUES (?, ?, ?)",
                   (message.from_user.id, question, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            clear_state(user_id)
            
            # 👇 МГНОВЕННОЕ УВЕДОМЛЕНИЕ МАСТЕРУ
            notify_admin(
                f"📩 <b>Новый вопрос от клиента!</b>\n\n"
                f"👤 От: {esc(message.from_user.first_name or 'Клиент')}\n"
                f"💬 Вопрос: {esc(question)}\n\n"
                f"Чтобы ответить, используйте команду /my_q."
            )
            
            safe_send(message.chat.id, "✅ Твой вопрос отправлен мастеру! Он ответит в ближайшее время.", main_menu(user_id))
            return

        # ---------- админ: новая услуга ----------
        if step == "svc_name":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            if len(text) < 2 or len(text) > 80:
                safe_send(message.chat.id, "Название должно быть 2–80 символов. Введи ещё раз:")
                return
            set_state(user_id, "svc_duration", name=text)
            safe_send(message.chat.id, "Шаг 2/3. Длительность в минутах (например 60):")
            return

        if step == "svc_duration":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            if not text.isdigit() or not (5 <= int(text) <= 600):
                safe_send(message.chat.id, "Нужно число от 5 до 600. Введи длительность в минутах:")
                return
            set_state(user_id, "svc_price", name=data["name"], duration=int(text))
            safe_send(message.chat.id, "Шаг 3/3. Цена в рублях (например 1500):")
            return

        if step == "svc_price":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            digits = text.replace(" ", "").replace("₽", "")
            if not digits.isdigit() or int(digits) > 1_000_000:
                safe_send(message.chat.id, "Нужно целое число (например 1500). Введи цену:")
                return
            price = int(digits)
            q_exec("INSERT INTO services (name, duration, price) VALUES (?,?,?)",
                   (data["name"], data["duration"], price))
            clear_state(user_id)
            safe_send(message.chat.id,
                      f"✅ Услуга добавлена:\n💅 <b>{esc(data['name'])}</b>\n"
                      f"⏱ {data['duration']} мин · 💰 {price}₽")
            safe_send(message.chat.id, "⚙️ Меню:", admin_menu_kb())
            return

        # ---------- админ: новый слот ----------
        if step == "slot_date":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            date_iso = parse_date_input(text)
            if not date_iso:
                safe_send(message.chat.id, "❗️ Не понял дату. Примеры: завтра, 15.03, 2025-03-15")
                return
            if date_iso < today_str():
                safe_send(message.chat.id, "❗️ Дата в прошлом. Введи другую:")
                return
            set_state(user_id, "slot_time", service_id=data["service_id"], date=date_iso)
            safe_send(message.chat.id,
                      f"🗓 Дата: <b>{fmt_date(date_iso)}</b>\n\n"
                      "Шаг 2/2. Введи время. Можно сразу несколько через запятую:\n"
                      "10:00, 11:30, 13:00")
            return

        if step == "slot_time":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            times, bad = parse_times_input(text)
            if not times:
                safe_send(message.chat.id, "❗️ Не понял время. Формат ЧЧ:ММ, например 10:00, 14:30")
                return
            service_id = int(data["service_id"])
            date_iso = data["date"]
            added, skipped = [], []
            for t in times:
                exists = q_one("SELECT id FROM slots WHERE service_id = ? AND date = ? AND time = ?",
                               (service_id, date_iso, t))
                if exists:
                    skipped.append(t)
                    continue
                if q_exec("INSERT INTO slots (service_id, date, time, is_booked) VALUES (?,?,?, 0)",
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
                report.append("⏭ Пропущено (уже есть): " + ", ".join(skipped))
            if bad:
                report.append("⚠️ Не распознано: " + ", ".join(bad))
            safe_send(message.chat.id, "\n".join(report))
            safe_send(message.chat.id, "⚙️ Меню:", admin_menu_kb())
            return

        # ---------- админ: генератор расписания ----------
        if step == "gen_hours":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            rng = parse_hours_range(text)
            if not rng:
                safe_send(message.chat.id, "❗️ Формат: <code>10:00-19:00</code>. Попробуй ещё раз:")
                return
            set_state(user_id, "gen_step", service_id=data["service_id"], start=rng[0], end=rng[1])
            safe_send(message.chat.id,
                      f"🕒 Часы: <b>{rng[0]}–{rng[1]}</b>\n\n"
                      "Шаг 2/3. Интервал между записями в минутах:\n"
                      "<code>30</code> · <code>45</code> · <code>60</code> · <code>90</code>")
            return

        if step == "gen_step":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            if not text.isdigit() or not (10 <= int(text) <= 480):
                safe_send(message.chat.id, "Нужно число от 10 до 480. Введи интервал:")
                return
            set_state(user_id, "gen_days", service_id=data["service_id"],
                      start=data["start"], end=data["end"], interval=int(text))
            safe_send(message.chat.id,
                      "Шаг 3/3. На сколько дней вперёд создать? (1–60)\n\n"
                      "Добавь слово <b>будни</b>, чтобы пропустить Сб и Вс:\n"
                      "<code>7</code> · <code>14</code> · <code>14 будни</code>")
            return

        if step == "gen_days":
            if not is_admin(user_id):
                clear_state(user_id)
                return
            only_weekdays = "будн" in text.lower()
            digits = re.sub(r"\D", "", text)
            if not digits or not (1 <= int(digits) <= 60):
                safe_send(message.chat.id, "Нужно число от 1 до 60. Например: <code>14 будни</code>")
                return
            clear_state(user_id)
            service_id = int(data["service_id"])
            service = get_service(service_id)
          
            added, skipped, days_used, capped = generate_slots(service_id, data["start"], data["end"],
                int(data["interval"]), int(digits), only_weekdays)

            report = [f"📅 <b>{esc(service['name'] if service else 'услуга')}</b>",
                      f"🕒 {data['start']}–{data['end']} · шаг {data['interval']} мин",
                      f"🗓 Дней задействовано: {days_used}" + (" (только будни)" if only_weekdays else ""),
                      ""]
            report.append(f"✅ Создано слотов: <b>{added}</b>")
            if skipped:
                report.append(f"⏭ Пропущено (уже были): {skipped}")
            if capped:
                report.append(f"⚠️ Лимит {MAX_GEN_SLOTS} слотов за раз — запусти генератор ещё раз.")
            if not added and not skipped:
                report.append("😕 Ничего не создано: интервал больше рабочего дня?")
            safe_send(message.chat.id, "\n".join(report))
            safe_send(message.chat.id, "⚙️ Меню:", admin_menu_kb())
            return

                # ---------- неизвестное состояние ----------
        clear_state(user_id)
        safe_send(message.chat.id, "🔄 Диалог сброшен. Выбери действие 👇", main_menu(user_id))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка в шаге '%s': %s", step, exc)
        clear_state(user_id)
        safe_send(message.chat.id, "⚠️ Ошибка обработки. Начни заново: /start", main_menu(user_id))


@bot.message_handler(content_types=["photo", "sticker", "voice", "video", "document",
                                    "audio", "location", "contact", "animation"])
def on_other_content(message):
    safe_send(message.chat.id, "🤖 Я понимаю только кнопки и текст 👇", main_menu(message.from_user.id))

# ---------- Планировщик ----------
def check_reminders():
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz).strftime("%H:%M")
    # Используем get_conn для доступа к базе
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, user_id, text FROM reminders WHERE remind_time=? AND is_sent=0", (now,))
        due = cur.fetchall()
        for r_id, user_id, text in due:
            try:
                bot.send_message(user_id, 
                    f"⏰ *Напоминание:* {text}\n\n"
                    f"📞 Связь с мастером: @nickname", 
                    parse_mode='Markdown')
            except Exception as e:
                print(f"Ошибка отправки: {e}")
            cur.execute("UPDATE reminders SET is_sent=1 WHERE id=?", (r_id,))
        conn.commit()

def run_scheduler():
    schedule.every(1).minutes.do(check_reminders)
    while True:
        schedule.run_pending()
        time.sleep(1)

# Запускаем планировщик в отдельном потоке
threading.Thread(target=run_scheduler, daemon=True).start()

# ==========================================================
#
# 9. FLASK-ЗАГЛУШКА (health-check для Render)
# ==========================================================

def start_web_server() -> None:
    """Открывает порт с health-check endpoint — нужно для Render Web Service.
    Запускается, только если задана переменная PORT."""
    port_raw = (os.getenv("PORT") or "").strip()
    if not port_raw:
        logger.info("PORT не задан — веб-сервер не запускаю (локальный/PythonAnywhere режим).")
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
        except Exception as exc:  # noqa: BLE001
            logger.error("Веб-сервер упал: %s", exc)

    threading.Thread(target=run, daemon=True, name="flask-health").start()
    logger.info("Health-check сервер запущен на порту %s", port_raw)


# ==========================================================
#
# 10. ТОЧКА ВХОДА
# ==========================================================

def main() -> None:
    init_db()
    start_web_server()

    try:
        me = bot.get_me()
        logger.info("Бот @%s запущен. ADMIN_ID=%s, TZ=UTC+%s", me.username, ADMIN_ID, TZ_OFFSET)
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось проверить токен (get_me): %s", exc)

    while True:
        try:
            bot.infinity_polling(none_stop=True, timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Polling прервался: %s. Перезапуск через 10 сек...", exc)
            time.sleep(10)
# Функция генерации слотов (расписания)
def generate_slots(service_id: int, start_time: str, end_time: str,
                   step_min: int, days_ahead: int, only_weekdays: bool):
    """Создаёт слоты для указанной услуги на N дней вперёд."""
    today = now_local().date()
    added = 0
    skipped = 0
    days_used = 0
    capped = False

    for day_offset in range(days_ahead):
        current_date = today + timedelta(days=day_offset)
        # Если нужны только будни (пн-пт), пропускаем сб/вс
        if only_weekdays and current_date.weekday() >= 5:
            continue

        date_iso = current_date.isoformat()
        # Генерируем время от start до end с шагом step_min
        cur_time = datetime.strptime(start_time, "%H:%M")
        end_dt = datetime.strptime(end_time, "%H:%M")

        while cur_time < end_dt:
            time_str = cur_time.strftime("%H:%M")
            exists = q_one("SELECT id FROM slots WHERE service_id=? AND date=? AND time=?",
                           (service_id, date_iso, time_str))
            if exists:
                skipped += 1
            else:
                if added >= MAX_GEN_SLOTS:
                    capped = True
                    return added, skipped, days_used, capped
                q_exec("INSERT INTO slots (service_id, date, time, is_booked) VALUES (?,?,?, 0)",
                       (service_id, date_iso, time_str))
                added += 1

            cur_time += timedelta(minutes=step_min)
        days_used += 1

    return added, skipped, days_used, capped


if __name__ == "__main__":
    main()
