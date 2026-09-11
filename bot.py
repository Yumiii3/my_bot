import os
import re
import html
import time
import sqlite3
import logging
import threading
import urllib.parse

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types


# ==========================================================
#                    НАСТРОЙКИ
# ==========================================================

def load_env_file(path: str = ".env") -> None:
    """
    Загружает переменные из .env.
    Уже существующие переменные окружения не перезаписываются.
    """
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()

                if not line:
                    continue

                if line.startswith("#"):
                    continue

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)

                key = key.strip()
                value = value.strip().strip('"').strip("'")

                if key and key not in os.environ:
                    os.environ[key] = value

    except Exception as exc:
        print(f"Не удалось прочитать .env: {exc}")


load_env_file()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("booking-bot")


def env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "").strip()

    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        logger.warning(
            "Переменная %s имеет неправильное значение: %s",
            name,
            value,
        )
        return default


TOKEN = os.getenv("TOKEN", "").strip()

if not TOKEN:
    raise SystemExit(
        "Не задан TOKEN.\n"
        "Добавьте переменную TOKEN в Render Environment Variables."
    )


ADMIN_ID = env_int("ADMIN_ID", 0)

if ADMIN_ID == 0:
    logger.warning(
        "ADMIN_ID не задан. Админ-панель будет недоступна."
    )


DB_PATH = os.getenv("DB_PATH", "bookings.db").strip()

db_directory = os.path.dirname(DB_PATH)

if db_directory:
    os.makedirs(db_directory, exist_ok=True)


BACKUP_DIR = os.getenv("BACKUP_DIR", "backups").strip()

MASTER_NAME = os.getenv("MASTER_NAME", "мастер")

MASTER_ADDRESS = os.getenv(
    "MASTER_ADDRESS",
    "Адрес уточняйте у мастера",
)

MASTER_CONTACT = os.getenv(
    "MASTER_CONTACT",
    "@nickname",
)

TZ_OFFSET = env_int("TZ_OFFSET", 3)

LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))


bot = telebot.TeleBot(
    TOKEN,
    parse_mode="HTML",
    threaded=True,
)


LOCK_FILE = "bot.lock"

MAX_MSG_LEN = 3500

MAX_GEN_SLOTS = 400


BTN_BOOK = "📅 Записаться"
BTN_MY = "📋 Мои записи"
BTN_CANCEL = "❌ Отменить запись"
BTN_PORTFOLIO = "📁 Портфолио"
BTN_FAQ = "❓ Вопросы и связь"
BTN_SHARE = "📤 Поделиться ботом"
BTN_ADMIN = "⚙️ Админ-панель"


WEEKDAYS = [
    "Пн",
    "Вт",
    "Ср",
    "Чт",
    "Пт",
    "Сб",
    "Вс",
]


# ==========================================================
#              ЗАЩИТА ОТ ДВУХ ЭКЗЕМПЛЯРОВ
# ==========================================================

def acquire_single_instance() -> None:
    try:
        lock = open(LOCK_FILE, "x", encoding="utf-8")
        lock.write(str(os.getpid()))
        lock.close()

        logger.info("Lock-файл создан")

    except FileExistsError:
        logger.error(
            "Похоже, бот уже запущен. "
            "Если это не так, удалите файл bot.lock."
        )
        raise SystemExit(1)

    except Exception as exc:
        logger.exception("Не удалось создать lock-файл: %s", exc)
        raise SystemExit(1)


def release_single_instance() -> None:
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            logger.info("Lock-файл удалён")

    except Exception as exc:
        logger.warning(
            "Не удалось удалить lock-файл: %s",
            exc,
        )


# ==========================================================
#                    УТИЛИТЫ
# ==========================================================

def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def current_time_str() -> str:
    return now_local().strftime("%H:%M")


def fmt_date(date_iso: str) -> str:
    try:
        date = datetime.strptime(date_iso, "%Y-%m-%d")

        return (
            f"{date.strftime('%d.%m.%Y')} "
            f"({WEEKDAYS[date.weekday()]})"
        )

    except Exception:
        return str(date_iso)


def fmt_date_short(date_iso: str) -> str:
    try:
        date = datetime.strptime(date_iso, "%Y-%m-%d")

        return (
            f"{date.strftime('%d.%m')} "
            f"{WEEKDAYS[date.weekday()]}"
        )

    except Exception:
        return str(date_iso)


def parse_date_input(text: str):
    raw = (text or "").strip().lower().replace("/", ".")

    today = now_local().date()

    if raw in ("сегодня", "today"):
        return today.isoformat()

    if raw in ("завтра", "tomorrow"):
        return (today + timedelta(days=1)).isoformat()

    if raw == "послезавтра":
        return (today + timedelta(days=2)).isoformat()

    for date_format in (
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d.%m.%y",
        "%d.%m",
    ):
        try:
            parsed = datetime.strptime(
                raw,
                date_format,
            ).date()

        except ValueError:
            continue

        if date_format == "%d.%m":
            parsed = parsed.replace(year=today.year)

            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)

        return parsed.isoformat()

    return None


def normalize_time(value: str):
    value = (value or "").strip()
    value = value.replace(".", ":")

    if value.isdigit():
        value = f"{int(value):02d}:00"

    try:
        return datetime.strptime(
            value,
            "%H:%M",
        ).strftime("%H:%M")

    except ValueError:
        return None


def parse_times_input(text: str):
    parts = [
        item
        for item in re.split(
            r"[,;\s]+",
            (text or "").strip(),
        )
        if item
    ]

    good = []
    bad = []

    for part in parts:
        normalized = normalize_time(part)

        if normalized:
            good.append(normalized)
        else:
            bad.append(part)

    good = list(dict.fromkeys(good))

    return good, bad


def parse_hours_range(text: str):
    raw = (text or "").strip().lower()
    raw = raw.replace(" ", "")
    raw = raw.replace("—", "-")
    raw = raw.replace("–", "-")
    raw = raw.replace("до", "-")
    raw = raw.replace("..", "-")

    parts = [part for part in raw.split("-") if part]

    if len(parts) != 2:
        return None

    start = normalize_time(parts[0])
    end = normalize_time(parts[1])

    if not start or not end:
        return None

    if start >= end:
        return None

    return start, end


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


# ==========================================================
#                    SQLITE
# ==========================================================

DB_LOCK = threading.RLock()


@contextmanager
def get_conn():
    with DB_LOCK:
        connection = sqlite3.connect(
            DB_PATH,
            timeout=30,
        )

        connection.row_factory = sqlite3.Row

        try:
            connection.execute(
                "PRAGMA foreign_keys = ON"
            )

            connection.execute(
                "PRAGMA busy_timeout = 30000"
            )

            yield connection
            connection.commit()

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()


def q_all(sql: str, params: tuple = ()):
    try:
        with get_conn() as connection:
            return connection.execute(
                sql,
                params,
            ).fetchall()

    except Exception as exc:
        logger.exception(
            "Ошибка q_all: %s | SQL=%s",
            exc,
            sql,
        )
        return []


def q_one(sql: str, params: tuple = ()):
    try:
        with get_conn() as connection:
            return connection.execute(
                sql,
                params,
            ).fetchone()

    except Exception as exc:
        logger.exception(
            "Ошибка q_one: %s | SQL=%s",
            exc,
            sql,
        )
        return None


def q_exec(sql: str, params: tuple = ()) -> int:
    try:
        with get_conn() as connection:
            cursor = connection.execute(
                sql,
                params,
            )

            return cursor.rowcount

    except Exception as exc:
        logger.exception(
            "Ошибка q_exec: %s | SQL=%s",
            exc,
            sql,
        )
        return -1


def table_columns(connection, table_name: str) -> set:
    rows = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return {row[1] for row in rows}


def add_column_if_missing(
    connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    columns = table_columns(
        connection,
        table_name,
    )

    if column_name not in columns:
        connection.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} "
            f"{column_definition}"
        )

        logger.info(
            "Добавлена колонка %s.%s",
            table_name,
            column_name,
        )


def init_db() -> None:
    with get_conn() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                duration INTEGER NOT NULL DEFAULT 60,
                price INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                is_booked INTEGER NOT NULL DEFAULT 0,
                client_id INTEGER,
                client_name TEXT,
                client_comment TEXT,
                FOREIGN KEY(service_id)
                    REFERENCES services(id)
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                created_at TEXT
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                caption TEXT,
                created_at TEXT
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                remind_time TEXT NOT NULL,
                is_sent INTEGER NOT NULL DEFAULT 0
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS reminders_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                slot_id INTEGER NOT NULL,
                remind_at TEXT NOT NULL,
                is_sent INTEGER NOT NULL DEFAULT 0
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                answer_text TEXT,
                is_answered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                preferred_date TEXT,
                created_at TEXT NOT NULL,
                is_notified INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Миграции уже существующей базы
        add_column_if_missing(
            connection,
            "services",
            "is_active",
            "INTEGER NOT NULL DEFAULT 1",
        )

        add_column_if_missing(
            connection,
            "slots",
            "client_comment",
            "TEXT",
        )

        add_column_if_missing(
            connection,
            "clients",
            "created_at",
            "TEXT",
        )

        add_column_if_missing(
            connection,
            "portfolio",
            "created_at",
            "TEXT",
        )

        default_faq = (
            "💬 <b>Вопросы и связь</b>\n\n"
            "Если у вас есть вопрос, напишите его прямо в этот чат. "
            "Мастер ответит, как только освободится."
        )

        connection.execute("""
            INSERT OR IGNORE INTO config(key, value)
            VALUES(?, ?)
        """, (
            "faq_text",
             default_faq,
        ))


        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_slots_lookup
            ON slots(service_id, date, is_booked)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_slots_client
            ON slots(client_id, is_booked)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_v2
            ON reminders_v2(remind_at, is_sent)
        """)

        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_slots_unique
            ON slots(service_id, date, time)
        """)

    logger.info(
        "База данных готова: %s",
        os.path.abspath(DB_PATH),
    )


# ==========================================================
#              РЕЗЕРВНОЕ КОПИРОВАНИЕ
# ==========================================================

def create_db_backup():
    """
    Создаёт корректную копию SQLite-базы.
    """
    try:
        if not os.path.exists(DB_PATH):
            logger.warning(
                "База для резервного копирования не найдена"
            )
            return None

        os.makedirs(
            BACKUP_DIR,
            exist_ok=True,
        )

        filename = (
            "bookings_"
            f"{now_local().strftime('%Y-%m-%d_%H-%M-%S')}.db"
        )

        backup_path = os.path.join(
            BACKUP_DIR,
            filename,
        )

        with sqlite3.connect(DB_PATH) as source:
            with sqlite3.connect(backup_path) as destination:
                source.backup(destination)

        logger.info(
            "Создана резервная копия: %s",
            backup_path,
        )

        return backup_path

    except Exception as exc:
        logger.exception(
            "Ошибка создания резервной копии: %s",
            exc,
        )
        return None


def send_db_backup(chat_id: int) -> bool:
    backup_path = create_db_backup()

    if not backup_path:
        safe_send(
            chat_id,
            "❌ Не удалось создать резервную копию базы.",
        )
        return False

    try:
        with open(backup_path, "rb") as backup_file:
            bot.send_document(
                chat_id,
                backup_file,
                caption=(
                    "🗄 <b>Резервная копия базы</b>\n\n"
                    f"Дата: "
                    f"{now_local().strftime('%d.%m.%Y %H:%M')}\n"
                    "Сохрани этот файл отдельно."
                ),
                parse_mode="HTML",
            )

        return True

    except Exception as exc:
        logger.exception(
            "Ошибка отправки backup: %s",
            exc,
        )

        safe_send(
            chat_id,
            "❌ Не удалось отправить резервную копию.",
        )

        return False


# ==========================================================
#                    ОТПРАВКА СООБЩЕНИЙ
# ==========================================================

def safe_send(
    chat_id: int,
    text: str,
    reply_markup=None,
):
    try:
        if len(text) <= MAX_MSG_LEN:
            return bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
            )

        chunks = []
        current = ""

        for line in text.split("\n"):
            if len(current) + len(line) + 1 > MAX_MSG_LEN:
                chunks.append(current)
                current = ""

            current += line + "\n"

        if current:
            chunks.append(current)

        last_message = None

        for index, chunk in enumerate(chunks):
            last_message = bot.send_message(
                chat_id,
                chunk,
                reply_markup=(
                    reply_markup
                    if index == len(chunks) - 1
                    else None
                ),
            )

        return last_message

    except Exception as exc:
        logger.error(
            "Не удалось отправить сообщение: %s",
            exc,
        )
        return None


def safe_edit(
    call,
    text: str,
    reply_markup=None,
):
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

    except Exception:
        safe_send(
            call.message.chat.id,
            text,
            reply_markup,
        )


def notify_admin(text: str) -> None:
    if ADMIN_ID:
        safe_send(
            ADMIN_ID,
            text,
        )


# ==========================================================
#                    СОСТОЯНИЯ
# ==========================================================

user_states = {}

STATE_LOCK = threading.RLock()


def set_state(
    user_id: int,
    step: str,
    **data,
) -> None:
    with STATE_LOCK:
        user_states[user_id] = {
            "step": step,
            "data": data,
        }


def get_state(user_id: int):
    with STATE_LOCK:
        return user_states.get(user_id)


def clear_state(user_id: int) -> None:
    with STATE_LOCK:
        user_states.pop(user_id, None)


# ==========================================================
#                    КЛАВИАТУРЫ
# ==========================================================

def main_menu(user_id: int):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
    )

    keyboard.row(BTN_BOOK)
    keyboard.row(BTN_MY, BTN_CANCEL)
    keyboard.row(BTN_PORTFOLIO)
    keyboard.row(BTN_FAQ, BTN_SHARE)

    if is_admin(user_id):
        keyboard.row(BTN_ADMIN)

    return keyboard


def services_kb(prefix: str):
    keyboard = types.InlineKeyboardMarkup()

    rows = q_all("""
        SELECT id, name, duration, price
        FROM services
        WHERE is_active = 1
        ORDER BY id
    """)

    for row in rows:
        keyboard.add(
            types.InlineKeyboardButton(
                text=(
                    f"{row['name']} — "
                    f"{row['price']}₽ · "
                    f"{row['duration']} мин"
                ),
                callback_data=f"{prefix}:{row['id']}",
            )
        )

    return keyboard


def admin_menu_kb():
    keyboard = types.InlineKeyboardMarkup()

    buttons = [
        (
            "➕ Добавить слот",
            "adm:add_slot",
        ),
        (
            "🕳 Свободные слоты",
            "adm:free",
        ),
        (
            "📆 Записи на сегодня",
            "adm:today",
        ),
        (
            "📅 Создать расписание",
            "adm:gen_week",
        ),
        (
            "📚 Все записи",
            "adm:all",
        ),
        (
            "🧾 Добавить услугу",
            "adm:add_service",
        ),
        (
            "❌ Скрыть услугу",
            "adm:del_service",
        ),
        (
            "📊 Статистика",
            "adm:stats",
        ),
        (
            "📖 Команды",
            "adm:commands",
        ),
    ]

    for text, callback_data in buttons:
        keyboard.add(
            types.InlineKeyboardButton(
                text=text,
                callback_data=callback_data,
            )
        )

    return keyboard


# ==========================================================
#                    ДАННЫЕ КЛИЕНТА
# ==========================================================

def save_client(
    user_id: int,
    name: str,
    username: str = "",
) -> None:
    q_exec("""
        INSERT INTO clients(
            user_id,
            name,
            username,
            created_at
        )
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            name = excluded.name,
            username = excluded.username
    """, (
        user_id,
        (name or "").strip()[:100],
        (username or "").strip()[:64],
        now_local().isoformat(),
    ))


def get_client_name(user_id: int):
    row = q_one("""
        SELECT name
        FROM clients
        WHERE user_id = ?
    """, (user_id,))

    if row and row["name"]:
        return row["name"]

    return None


# ==========================================================
#                    УСЛУГИ И СЛОТЫ
# ==========================================================

def get_service(service_id: int):
    return q_one("""
        SELECT id, name, duration, price, is_active
        FROM services
        WHERE id = ?
    """, (service_id,))


def get_slot(slot_id: int):
    return q_one("""
        SELECT
            s.id,
            s.service_id,
            s.date,
            s.time,
            s.is_booked,
            s.client_id,
            s.client_name,
            s.client_comment,
            sv.name AS service_name,
            sv.price AS price,
            sv.duration AS duration
        FROM slots s
        LEFT JOIN services sv
            ON sv.id = s.service_id
        WHERE s.id = ?
    """, (slot_id,))


def free_dates(service_id: int):
    return q_all("""
        SELECT date, COUNT(*) AS cnt
        FROM slots
        WHERE service_id = ?
          AND is_booked = 0
          AND (
              date > ?
              OR (
                  date = ?
                  AND time >= ?
              )
          )
        GROUP BY date
        ORDER BY date
        LIMIT 30
    """, (
        service_id,
        today_str(),
        today_str(),
        current_time_str(),
    ))


def free_slots_on_date(
    service_id: int,
    date_iso: str,
):
    return q_all("""
        SELECT id, time
        FROM slots
        WHERE service_id = ?
          AND date = ?
          AND is_booked = 0
          AND (
              date > ?
              OR (
                  date = ?
                  AND time >= ?
              )
          )
        ORDER BY time
    """, (
        service_id,
        date_iso,
        today_str(),
        today_str(),
        current_time_str(),
    ))


def my_bookings(user_id: int):
    return q_all("""
        SELECT
            s.id,
            s.date,
            s.time,
            s.client_comment,
            sv.name AS service_name,
            sv.price AS price,
            sv.duration AS duration
        FROM slots s
        LEFT JOIN services sv
            ON sv.id = s.service_id
        WHERE s.is_booked = 1
          AND s.client_id = ?
          AND (
              s.date > ?
              OR (
                  s.date = ?
                  AND s.time >= ?
              )
          )
        ORDER BY s.date, s.time
    """, (
        user_id,
        today_str(),
        today_str(),
        current_time_str(),
    ))


def all_bookings(only_today: bool = False):
    if only_today:
        return q_all("""
            SELECT
                s.id,
                s.date,
                s.time,
                s.client_id,
                s.client_name,
                s.client_comment,
                sv.name AS service_name,
                sv.price AS price
            FROM slots s
            LEFT JOIN services sv
                ON sv.id = s.service_id
            WHERE s.is_booked = 1
              AND s.date = ?
            ORDER BY s.time
        """, (today_str(),))

    return q_all("""
        SELECT
            s.id,
            s.date,
            s.time,
            s.client_id,
            s.client_name,
            s.client_comment,
            sv.name AS service_name,
            sv.price AS price
        FROM slots s
        LEFT JOIN services sv
            ON sv.id = s.service_id
        WHERE s.is_booked = 1
          AND (
              s.date > ?
              OR (
                  s.date = ?
                  AND s.time >= ?
              )
          )
        ORDER BY s.date, s.time
        LIMIT 300
    """, (
        today_str(),
        today_str(),
        current_time_str(),
    ))


def book_slot(
    slot_id: int,
    user_id: int,
    client_name: str,
    client_comment: str = "",
) -> bool:
    """
    Атомарное бронирование.
    Два клиента не смогут занять один слот.
    """
    try:
        with get_conn() as connection:
            cursor = connection.execute("""
                UPDATE slots
                SET
                    is_booked = 1,
                    client_id = ?,
                    client_name = ?,
                    client_comment = ?
                WHERE id = ?
                  AND is_booked = 0
            """, (
                user_id,
                client_name[:100],
                client_comment[:1000],
                slot_id,
            ))

            return cursor.rowcount == 1

    except Exception as exc:
        logger.exception(
            "Ошибка бронирования: %s",
            exc,
        )
        return False


def unbook_slot(
    slot_id: int,
    user_id: int,
    force: bool = False,
) -> bool:
    try:
        with get_conn() as connection:
            if force:
                cursor = connection.execute("""
                    UPDATE slots
                    SET
                        is_booked = 0,
                        client_id = NULL,
                        client_name = NULL,
                        client_comment = NULL
                    WHERE id = ?
                      AND is_booked = 1
                """, (slot_id,))

            else:
                cursor = connection.execute("""
                    UPDATE slots
                    SET
                        is_booked = 0,
                        client_id = NULL,
                        client_name = NULL,
                        client_comment = NULL
                    WHERE id = ?
                      AND is_booked = 1
                      AND client_id = ?
                """, (
                    slot_id,
                    user_id,
                ))

            return cursor.rowcount == 1

    except Exception as exc:
        logger.exception(
            "Ошибка отмены записи: %s",
            exc,
        )
        return False


def generate_slots(
    service_id: int,
    start_hm: str,
    end_hm: str,
    step_min: int,
    days: int,
    only_weekdays: bool = False,
):
    start = datetime.strptime(
        start_hm,
        "%H:%M",
    )

    end = datetime.strptime(
        end_hm,
        "%H:%M",
    )

    today = now_local().date()
    current_hm = current_time_str()

    rows = []
    days_used = 0
    capped = False

    for offset in range(days):
        day = today + timedelta(days=offset)

        if only_weekdays and day.weekday() >= 5:
            continue

        current = start
        added_today = False

        while current + timedelta(minutes=step_min) <= end:
            time_value = current.strftime("%H:%M")
            current += timedelta(minutes=step_min)

            if day == today and time_value <= current_hm:
                continue

            if len(rows) >= MAX_GEN_SLOTS:
                capped = True
                break

            rows.append((
                service_id,
                day.isoformat(),
                time_value,
            ))

            added_today = True

        if added_today:
            days_used += 1

        if capped:
            break

    if not rows:
        return 0, 0, days_used, capped

    dates = sorted(
        {row[1] for row in rows}
    )

    marks = ",".join(
        ["?"] * len(dates)
    )

    existing_rows = q_all(
        f"""
        SELECT date, time
        FROM slots
        WHERE service_id = ?
          AND date IN ({marks})
        """,
        (
            service_id,
            *dates,
        ),
    )

    existing = {
        (
            row["date"],
            row["time"],
        )
        for row in existing_rows
    }

    fresh = [
        row
        for row in rows
        if (row[1], row[2]) not in existing
    ]

    skipped = len(rows) - len(fresh)

    if not fresh:
        return 0, skipped, days_used, capped

    try:
        with get_conn() as connection:
            connection.executemany("""
                INSERT OR IGNORE INTO slots(
                    service_id,
                    date,
                    time,
                    is_booked
                )
                VALUES(?, ?, ?, 0)
            """, fresh)

        return len(fresh), skipped, days_used, capped

    except Exception as exc:
        logger.exception(
            "Ошибка генерации слотов: %s",
            exc,
        )
        return 0, skipped, days_used, capped


# ==========================================================
#                    НАПОМИНАНИЯ
# ==========================================================

def create_reminder(
    user_id: int,
    slot_id: int,
    date_iso: str,
    time_hm: str,
) -> None:
    try:
        booking_time = datetime.strptime(
            f"{date_iso} {time_hm}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=LOCAL_TZ)

        remind_at = booking_time - timedelta(hours=1)

        q_exec("""
            INSERT INTO reminders_v2(
                user_id,
                slot_id,
                remind_at,
                is_sent
            )
            VALUES(?, ?, ?, 0)
        """, (
            user_id,
            slot_id,
            remind_at.isoformat(),
        ))

    except Exception as exc:
        logger.exception(
            "Ошибка создания напоминания: %s",
            exc,
        )


def cancel_reminder(slot_id: int) -> None:
    q_exec("""
        UPDATE reminders_v2
        SET is_sent = 1
        WHERE slot_id = ?
          AND is_sent = 0
    """, (slot_id,))


def check_reminders() -> None:
    """
    Отправляет все напоминания, срок которых наступил.
    """
    now_iso = now_local().isoformat()

    try:
        with get_conn() as connection:
            rows = connection.execute("""
                SELECT
                    r.id,
                    r.user_id,
                    s.date,
                    s.time,
                    sv.name AS service_name
                FROM reminders_v2 r
                JOIN slots s
                    ON s.id = r.slot_id
                LEFT JOIN services sv
                    ON sv.id = s.service_id
                WHERE r.is_sent = 0
                  AND r.remind_at <= ?
                  AND s.is_booked = 1
            """, (now_iso,)).fetchall()

            for row in rows:
                try:
                    bot.send_message(
                        row["user_id"],
                        "⏰ <b>Напоминание о записи</b>\n\n"
                        f"💅 {esc(row['service_name'] or 'услуга')}\n"
                        f"🗓 {fmt_date(row['date'])}\n"
                        f"🕒 {esc(row['time'])}\n\n"
                        f"📍 {esc(MASTER_ADDRESS)}\n"
                        f"📞 {esc(MASTER_CONTACT)}",
                        parse_mode="HTML",
                    )

                    connection.execute("""
                        UPDATE reminders_v2
                        SET is_sent = 1
                        WHERE id = ?
                    """, (row["id"],))

                except Exception as exc:
                    logger.exception(
                        "Ошибка отправки напоминания: %s",
                        exc,
                    )

    except Exception as exc:
        logger.exception(
            "Ошибка проверки напоминаний: %s",
            exc,
        )


def reminder_loop():
    while True:
        try:
            check_reminders()

        except Exception as exc:
            logger.exception(
                "Ошибка в потоке напоминаний: %s",
                exc,
            )

        time.sleep(60)


# ==========================================================
#                    ТЕКСТЫ ЗАПИСИ
# ==========================================================

def confirm_text(
    slot,
    client_name: str,
    comment: str = "",
) -> str:
    comment_text = comment or "Без пожеланий"

    return (
        "🧾 <b>Проверь запись:</b>\n\n"
        f"👤 Имя: <b>{esc(client_name)}</b>\n"
        f"💅 Услуга: "
        f"<b>{esc(slot['service_name'] or 'услуга')}</b>\n"
        f"🗓 Дата: <b>{fmt_date(slot['date'])}</b>\n"
        f"🕒 Время: <b>{esc(slot['time'])}</b>\n"
        f"⏱ Длительность: "
        f"{slot['duration'] or 0} мин\n"
        f"💰 Стоимость: "
        f"{slot['price'] or 0}₽\n"
        f"💬 Пожелание: {esc(comment_text)}\n\n"
        "Всё верно?"
    )


def confirm_kb(slot_id: int):
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    keyboard.row(
        types.InlineKeyboardButton(
            "✅ Подтвердить",
            callback_data=f"conf:{slot_id}",
        ),
        types.InlineKeyboardButton(
            "📅 В календарь",
            callback_data=f"cal:{slot_id}",
        ),
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "🚫 Отмена",
            callback_data="abort",
        )
    )

    return keyboard


# ==========================================================
#                    /START И КОМАНДЫ
# ==========================================================

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    user_id = message.from_user.id

    clear_state(user_id)

    save_client(
        user_id,
        message.from_user.first_name or "Клиент",
        message.from_user.username or "",
    )

    text = (
        f"🌸 Привет, "
        f"{esc(message.from_user.first_name or 'клиент')}!\n\n"
        f"Я помощник мастера "
        f"<b>{esc(MASTER_NAME)}</b> 💅\n\n"
        "Здесь можно быстро:\n"
        "• выбрать услугу;\n"
        "• найти свободное время;\n"
        "• записаться без звонка;\n"
        "• посмотреть свои записи;\n"
        "• перенести или отменить запись.\n\n"
        f"📍 {esc(MASTER_ADDRESS)}\n"
        f"📞 {esc(MASTER_CONTACT)}"
    )

    safe_send(
        message.chat.id,
        text,
        main_menu(user_id),
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    clear_state(
        message.from_user.id,
    )

    safe_send(
        message.chat.id,
        "🔄 Действие отменено.",
        main_menu(message.from_user.id),
    )


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    clear_state(
        message.from_user.id,
    )

    if not is_admin(message.from_user.id):
        safe_send(
            message.chat.id,
            "⛔️ Раздел доступен только мастеру.",
            main_menu(message.from_user.id),
        )
        return

    safe_send(
        message.chat.id,
        "⚙️ <b>Админ-панель</b>\nВыбери действие:",
        admin_menu_kb(),
    )


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    safe_send(
        message.chat.id,
        f"🆔 Ваш Telegram ID: "
        f"<code>{message.from_user.id}</code>",
    )


@bot.message_handler(commands=["backup"])
def cmd_backup(message):
    if not is_admin(message.from_user.id):
        safe_send(
            message.chat.id,
            "⛔️ Резервную копию может создавать только мастер.",
        )
        return

    safe_send(
        message.chat.id,
        "⏳ Создаю резервную копию базы...",
    )

    if send_db_backup(message.chat.id):
        safe_send(
            message.chat.id,
            "✅ Резервная копия отправлена.\n\n"
            "Перед обновлением кода всегда используй /backup.",
        )


@bot.message_handler(commands=["info"])
def cmd_info(message):
    if not is_admin(message.from_user.id):
        safe_send(
            message.chat.id,
            "⛔️ Команда доступна только мастеру.",
        )
        return

    text = (
        "📖 <b>Команды мастера</b>\n\n"
        "/admin — админ-панель\n"
        "/add_photo — добавить фото\n"
        "/del_photo — удалить фото\n"
        "/stats — статистика\n"
        "/backup — резервная копия базы\n"
        "/broadcast текст — рассылка\n"
        "/edit_faq — изменить FAQ\n"
        "/my_q — вопросы клиентов\n"
        "/myid — узнать ID\n"
    )

    safe_send(
        message.chat.id,
        text,
        main_menu(message.from_user.id),
    )


# ==========================================================
#                    ГЛАВНОЕ МЕНЮ
# ==========================================================

@bot.message_handler(func=lambda m: m.text == BTN_BOOK)
def on_book(message):
    clear_state(
        message.from_user.id,
    )

    keyboard = services_kb("svc")

    if not keyboard.keyboard:
        safe_send(
            message.chat.id,
            "😔 Пока нет доступных услуг.",
            main_menu(message.from_user.id),
        )
        return

    safe_send(
        message.chat.id,
        "💅 <b>Выбери услугу:</b>",
        keyboard,
    )


@bot.message_handler(func=lambda m: m.text == BTN_MY)
def on_my_bookings(message):
    clear_state(
        message.from_user.id,
    )

    rows = my_bookings(
        message.from_user.id,
    )

    if not rows:
        safe_send(
            message.chat.id,
            "📭 У тебя пока нет активных записей.",
            main_menu(message.from_user.id),
        )
        return

    lines = [
        "📋 <b>Твои записи:</b>",
        "",
    ]

    for row in rows:
        comment = row["client_comment"] or "Без пожеланий"

        lines.append(
            f"• <b>{esc(row['service_name'] or 'услуга')}</b>\n"
            f"  🗓 {fmt_date(row['date'])} "
            f"в {esc(row['time'])}\n"
            f"  ⏱ {row['duration'] or 0} мин · "
            f"💰 {row['price'] or 0}₽\n"
            f"  💬 {esc(comment)}"
        )

    safe_send(
        message.chat.id,
        "\n".join(lines),
        main_menu(message.from_user.id),
    )


@bot.message_handler(func=lambda m: m.text == BTN_CANCEL)
def on_cancel_booking(message):
    clear_state(
        message.from_user.id,
    )

    rows = my_bookings(
        message.from_user.id,
    )

    if not rows:
        safe_send(
            message.chat.id,
            "📭 Нет записей, которые можно отменить.",
            main_menu(message.from_user.id),
        )
        return

    keyboard = types.InlineKeyboardMarkup()

    for row in rows:
        keyboard.add(
            types.InlineKeyboardButton(
                text=(
                    f"🗑 "
                    f"{fmt_date_short(row['date'])} "
                    f"{row['time']} — "
                    f"{row['service_name'] or 'услуга'}"
                ),
                callback_data=f"cxl:{row['id']}",
            )
        )

    safe_send(
        message.chat.id,
        "❌ <b>Какую запись отменить?</b>",
        keyboard,
    )


@bot.message_handler(func=lambda m: m.text == BTN_SHARE)
def on_share(message):
    try:
        username = bot.get_me().username

        share_url = (
            "https://t.me/share/url"
            f"?url=https://t.me/{username}"
            "&text=Запишитесь онлайн без звонков ✨"
        )

        keyboard = types.InlineKeyboardMarkup()

        keyboard.add(
            types.InlineKeyboardButton(
                "📤 Поделиться",
                url=share_url,
            )
        )

        safe_send(
            message.chat.id,
            "📤 Поделитесь ботом с подругой:",
            keyboard,
        )

    except Exception:
        safe_send(
            message.chat.id,
            "Не удалось создать ссылку для отправки.",
        )


# ==========================================================
#                    FAQ И ВОПРОСЫ
# ==========================================================

def get_faq_text() -> str:
    row = q_one("""
        SELECT value
        FROM config
        WHERE key = 'faq_text'
    """)

    if row:
        return row["value"]

    return (
        "💬 Напишите вопрос мастеру. "
        "Он ответит, как только освободится."
    )


@bot.message_handler(func=lambda m: m.text == BTN_FAQ)
def on_faq(message):
    clear_state(
        message.from_user.id,
    )

    safe_send(
        message.chat.id,
        get_faq_text(),
    )

    set_state(
        message.from_user.id,
        "await_question",
    )

    safe_send(
        message.chat.id,
        "✍️ Напиши вопрос одним сообщением:",
        main_menu(message.from_user.id),
    )


@bot.message_handler(commands=["edit_faq"])
def cmd_edit_faq(message):
    if not is_admin(message.from_user.id):
        return

    set_state(
        message.from_user.id,
        "edit_faq",
    )

    safe_send(
        message.chat.id,
        "✍️ Отправь новый текст FAQ одним сообщением.",
    )


@bot.message_handler(commands=["my_q"])
def cmd_my_questions(message):
    if not is_admin(message.from_user.id):
        return

    rows = q_all("""
        SELECT id, user_id, question_text, created_at
        FROM questions
        WHERE is_answered = 0
        ORDER BY created_at
    """)

    if not rows:
        safe_send(
            message.chat.id,
            "🎉 Новых вопросов нет.",
        )
        return

    keyboard = types.InlineKeyboardMarkup()

    lines = [
        "📋 <b>Новые вопросы:</b>",
        "",
    ]

    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. {esc(row['question_text'])}"
        )

        keyboard.add(
            types.InlineKeyboardButton(
                f"✍️ Ответить №{index}",
                callback_data=f"ans_q:{row['id']}",
            )
        )

    safe_send(
        message.chat.id,
        "\n".join(lines),
        keyboard,
    )


# ==========================================================
#                    ПОРТФОЛИО
# ==========================================================

def get_portfolio():
    return q_all("""
        SELECT id, file_id, caption
        FROM portfolio
        WHERE master_id = ?
        ORDER BY id DESC
    """, (ADMIN_ID,))


@bot.message_handler(func=lambda m: m.text == BTN_PORTFOLIO)
def show_portfolio(message):
    rows = get_portfolio()

    if not rows:
        safe_send(
            message.chat.id,
            "📭 В портфолио пока нет фотографий.",
            main_menu(message.from_user.id),
        )
        return

    for start in range(0, len(rows), 10):
        chunk = rows[start:start + 10]

        media = []

        for row in chunk:
            media.append(
                types.InputMediaPhoto(
                    media=row["file_id"],
                    caption=row["caption"] or "",
                )
            )

        try:
            bot.send_media_group(
                message.chat.id,
                media,
            )

        except Exception as exc:
            logger.exception(
                "Ошибка отправки портфолио: %s",
                exc,
            )

    safe_send(
        message.chat.id,
        "📁 Работы мастера ✨",
        main_menu(message.from_user.id),
    )


@bot.message_handler(commands=["add_photo"])
def cmd_add_photo(message):
    if not is_admin(message.from_user.id):
        return

    set_state(
        message.from_user.id,
        "add_photo",
    )

    safe_send(
        message.chat.id,
        "📸 Отправь фотографию с подписью или без неё.",
    )


@bot.message_handler(commands=["del_photo"])
def cmd_del_photo(message):
    if not is_admin(message.from_user.id):
        return

    rows = get_portfolio()

    if not rows:
        safe_send(
            message.chat.id,
            "📭 В портфолио нет фотографий.",
        )
        return

    keyboard = types.InlineKeyboardMarkup()

    for row in rows[:100]:
        keyboard.add(
            types.InlineKeyboardButton(
                f"🗑 Удалить фото №{row['id']}",
                callback_data=f"delport:{row['id']}",
            )
        )

    safe_send(
        message.chat.id,
        "📁 Выбери фотографию для удаления:",
        keyboard,
    )


# ==========================================================
#                    СТАТИСТИКА И РАССЫЛКА
# ==========================================================

def get_statistics_text() -> str:
    today = today_str()

    today_rows = q_all("""
        SELECT
            s.time,
            s.client_name,
            s.client_comment,
            sv.name AS service_name,
            sv.price
        FROM slots s
        LEFT JOIN services sv
            ON sv.id = s.service_id
        WHERE s.is_booked = 1
          AND s.date = ?
        ORDER BY s.time
    """, (today,))

    week_start = (
        now_local() - timedelta(days=7)
    ).strftime("%Y-%m-%d")

    week_row = q_one("""
        SELECT
            COUNT(*) AS cnt,
            COALESCE(SUM(sv.price), 0) AS revenue
        FROM slots s
        LEFT JOIN services sv
            ON sv.id = s.service_id
        WHERE s.is_booked = 1
          AND s.date >= ?
          AND s.date <= ?
    """, (
        week_start,
        today,
    ))

    popular = q_one("""
        SELECT
            sv.name,
            COUNT(*) AS cnt
        FROM slots s
        LEFT JOIN services sv
            ON sv.id = s.service_id
        WHERE s.is_booked = 1
        GROUP BY sv.name
        ORDER BY cnt DESC
        LIMIT 1
    """)

    today_revenue = sum(
        row["price"] or 0
        for row in today_rows
    )

    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"📅 Сегодня записей: {len(today_rows)}",
        f"💰 Выручка сегодня: {today_revenue}₽",
        f"📈 Записей за неделю: "
        f"{week_row['cnt'] if week_row else 0}",
        f"💰 Выручка за неделю: "
        f"{week_row['revenue'] if week_row else 0}₽",
    ]

    if popular and popular["name"]:
        lines.append(
            f"🏆 Популярная услуга: "
            f"{esc(popular['name'])} "
            f"({popular['cnt']})"
        )

    if today_rows:
        lines.extend([
            "",
            "📍 <b>Сегодня:</b>",
        ])

        for row in today_rows:
            comment = row["client_comment"] or ""

            lines.append(
                f"🕒 {row['time']} — "
                f"{esc(row['client_name'] or 'клиент')} — "
                f"{esc(row['service_name'] or 'услуга')} — "
                f"{row['price'] or 0}₽"
            )

            if comment:
                lines.append(
                    f"   💬 {esc(comment)}"
                )

    return "\n".join(lines)


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not is_admin(message.from_user.id):
        return

    safe_send(
        message.chat.id,
        get_statistics_text(),
        main_menu(message.from_user.id),
    )


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(
        maxsplit=1,
    )

    if len(parts) < 2:
        safe_send(
            message.chat.id,
            "Пример:\n"
            "<code>/broadcast Скидка 20% на этой неделе</code>",
        )
        return

    text = parts[1]

    clients = q_all("""
        SELECT user_id
        FROM clients
    """)

    success = 0
    failed = 0

    safe_send(
        message.chat.id,
        f"⏳ Начинаю рассылку для {len(clients)} клиентов.",
    )

    for client in clients:
        try:
            bot.send_message(
                client["user_id"],
                f"📢 <b>Сообщение от мастера</b>\n\n"
                f"{esc(text)}",
                parse_mode="HTML",
            )

            success += 1
            time.sleep(0.05)

        except Exception:
            failed += 1

    safe_send(
        message.chat.id,
        f"✅ Рассылка завершена.\n\n"
        f"📨 Отправлено: {success}\n"
        f"❌ Не доставлено: {failed}",
    )


# ==========================================================
#                    CALLBACK-ОБРАБОТЧИК
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
        if data.startswith("svc:"):
            show_dates(
                call,
                int(data.split(":")[1]),
            )
            return

        if data.startswith("day:"):
            _, service_id, date_iso = data.split(":", 2)

            show_times(
                call,
                int(service_id),
                date_iso,
            )
            return

        if data.startswith("slot:"):
            start_confirm(
                call,
                int(data.split(":")[1]),
            )
            return

        if data.startswith("conf:"):
            do_book(
                call,
                int(data.split(":")[1]),
            )
            return

        if data.startswith("cxl:"):
            do_unbook(
                call,
                int(data.split(":")[1]),
            )
            return

        if data == "abort":
            clear_state(user_id)

            safe_edit(
                call,
                "🚫 Запись отменена.",
                main_menu(user_id),
            )
            return

        if data == "back:services":
            keyboard = services_kb("svc")

            safe_edit(
                call,
                "💅 <b>Выбери услугу:</b>",
                keyboard,
            )
            return

        if data == "back:main":
            clear_state(user_id)

            safe_edit(
                call,
                "🏠 Главное меню",
            )

            safe_send(
                call.message.chat.id,
                "Выбери действие:",
                main_menu(user_id),
            )
            return

        if data.startswith("cal:"):
            send_calendar_link(
                call,
                int(data.split(":")[1]),
            )
            return

        if data.startswith("delport:"):
            if not is_admin(user_id):
                return

            photo_id = int(data.split(":")[1])

            deleted = q_exec("""
                DELETE FROM portfolio
                WHERE id = ?
                  AND master_id = ?
            """, (
                photo_id,
                ADMIN_ID,
            ))

            if deleted == 1:
                safe_edit(
                    call,
                    "✅ Фото удалено.",
                    admin_menu_kb(),
                )
            else:
                safe_edit(
                    call,
                    "Фото не найдено.",
                    admin_menu_kb(),
                )

            return

        if data.startswith("ans_q:"):
            if not is_admin(user_id):
                return

            question_id = int(data.split(":")[1])

            row = q_one("""
                SELECT user_id, question_text
                FROM questions
                WHERE id = ?
                  AND is_answered = 0
            """, (question_id,))

            if not row:
                safe_edit(
                    call,
                    "Вопрос уже обработан.",
                    admin_menu_kb(),
                )
                return

            set_state(
                user_id,
                "reply_question",
                question_id=question_id,
                client_id=row["user_id"],
            )

            safe_send(
                call.message.chat.id,
                "✍️ Напиши ответ клиенту:\n\n"
                f"Вопрос: {esc(row['question_text'])}",
            )
            return

        if data.startswith("adm:"):
            if not is_admin(user_id):
                safe_edit(
                    call,
                    "⛔️ Доступ только для мастера.",
                )
                return

            handle_admin_callback(
                call,
                data,
            )
            return

        safe_edit(
            call,
            "🤔 Кнопка устарела. Открой меню заново.",
        )

    except Exception as exc:
        logger.exception(
            "Ошибка callback %s: %s",
            data,
            exc,
        )

        safe_send(
            user_id,
            "⚠️ Произошла ошибка. Попробуй ещё раз.",
            main_menu(user_id),
        )


# ==========================================================
#                    СЦЕНАРИЙ ЗАПИСИ
# ==========================================================

def show_dates(
    call,
    service_id: int,
):
    service = get_service(service_id)

    if not service or not service["is_active"]:
        safe_edit(
            call,
            "😔 Услуга больше недоступна.",
        )
        return

    dates = free_dates(service_id)

    if not dates:
        safe_edit(
            call,
            "😔 Свободного времени пока нет.\n\n"
            "Напиши мастеру, чтобы попасть в лист ожидания.",
        )
        return

    keyboard = types.InlineKeyboardMarkup()

    for row in dates:
        keyboard.add(
            types.InlineKeyboardButton(
                text=(
                    f"🗓 {fmt_date_short(row['date'])} · "
                    f"свободно: {row['cnt']}"
                ),
                callback_data=(
                    f"day:{service_id}:{row['date']}"
                ),
            )
        )

    keyboard.add(
        types.InlineKeyboardButton(
            "◀️ К услугам",
            callback_data="back:services",
        )
    )

    safe_edit(
        call,
        f"💅 Услуга: "
        f"<b>{esc(service['name'])}</b>\n"
        f"💰 {service['price']}₽ · "
        f"⏱ {service['duration']} мин\n\n"
        "🗓 <b>Выбери дату:</b>",
        keyboard,
    )


def show_times(
    call,
    service_id: int,
    date_iso: str,
):
    service = get_service(service_id)

    slots = free_slots_on_date(
        service_id,
        date_iso,
    )

    if not service or not slots:
        safe_edit(
            call,
            "😔 На эту дату свободного времени нет.",
        )
        return

    keyboard = types.InlineKeyboardMarkup(
        row_width=3,
    )

    buttons = []

    for row in slots:
        buttons.append(
            types.InlineKeyboardButton(
                text=f"🕒 {row['time']}",
                callback_data=f"slot:{row['id']}",
            )
        )

    keyboard.add(*buttons)

    keyboard.add(
        types.InlineKeyboardButton(
            "◀️ К датам",
            callback_data=f"svc:{service_id}",
        )
    )

    safe_edit(
        call,
        f"💅 <b>{esc(service['name'])}</b>\n"
        f"🗓 {fmt_date(date_iso)}\n\n"
        "🕒 <b>Выбери время:</b>",
        keyboard,
    )


def start_confirm(
    call,
    slot_id: int,
):
    slot = get_slot(slot_id)

    if not slot or slot["is_booked"]:
        safe_edit(
            call,
            "😔 Это время уже заняли.",
        )
        return

    user_id = call.from_user.id

    name = get_client_name(user_id)

    if not name:
        set_state(
            user_id,
            "await_name",
            slot_id=slot_id,
        )

        safe_edit(
            call,
            "✍️ Напиши своё имя:",
        )
        return

    set_state(
        user_id,
        "await_comment",
        slot_id=slot_id,
    )

    safe_edit(
        call,
        "💬 Есть ли пожелания к записи?\n\n"
        "Например: дизайн, длина волос, особые пожелания.\n\n"
        "Если пожеланий нет, напиши <code>-</code>.",
    )


def do_book(
    call,
    slot_id: int,
):
    user_id = call.from_user.id

    state = get_state(user_id) or {}
    state_data = state.get("data", {})

    comment = state_data.get("comment", "")

    name = get_client_name(user_id)

    if not name:
        name = call.from_user.first_name or "Клиент"

    slot = get_slot(slot_id)

    if not slot or slot["is_booked"]:
        clear_state(user_id)

        safe_edit(
            call,
            "😔 Это время уже заняли.",
        )
        return

    success = book_slot(
        slot_id=slot_id,
        user_id=user_id,
        client_name=name,
        client_comment=comment,
    )

    if not success:
        clear_state(user_id)

        safe_edit(
            call,
            "😔 Это время только что заняли. "
            "Выбери другое.",
        )
        return

    clear_state(user_id)

    create_reminder(
        user_id=user_id,
        slot_id=slot_id,
        date_iso=slot["date"],
        time_hm=slot["time"],
    )

    safe_edit(
        call,
        "✅ <b>Запись подтверждена!</b>\n\n"
        f"💅 {esc(slot['service_name'] or 'услуга')}\n"
        f"🗓 {fmt_date(slot['date'])}\n"
        f"🕒 {esc(slot['time'])}\n"
        f"💰 {slot['price'] or 0}₽\n\n"
        "За час до записи я пришлю напоминание.",
    )

    safe_send(
        call.message.chat.id,
        "Ждём тебя! ✨",
        main_menu(user_id),
    )

    username = (
        f"@{call.from_user.username}"
        if call.from_user.username
        else "без username"
    )

    comment_text = comment or "Без пожеланий"

    notify_admin(
        "🔔 <b>НОВАЯ ЗАПИСЬ</b>\n\n"
        f"👤 {esc(name)}\n"
        f"📱 {esc(username)}\n"
        f"🆔 {user_id}\n"
        f"💅 {esc(slot['service_name'] or 'услуга')}\n"
        f"🗓 {fmt_date(slot['date'])} "
        f"в {esc(slot['time'])}\n"
        f"💰 {slot['price'] or 0}₽\n"
        f"⏱ {slot['duration'] or 0} мин\n"
        f"💬 Пожелание: {esc(comment_text)}",
    )


def do_unbook(
    call,
    slot_id: int,
):
    user_id = call.from_user.id

    slot = get_slot(slot_id)

    if not slot:
        safe_edit(
            call,
            "Запись не найдена.",
        )
        return

    if not slot["is_booked"]:
        safe_edit(
            call,
            "Эта запись уже отменена.",
        )
        return

    if slot["client_id"] != user_id and not is_admin(user_id):
        safe_edit(
            call,
            "⛔️ Нельзя отменить чужую запись.",
        )
        return

    success = unbook_slot(
        slot_id=slot_id,
        user_id=user_id,
        force=is_admin(user_id),
    )

    if not success:
        safe_edit(
            call,
            "⚠️ Не удалось отменить запись.",
        )
        return

    cancel_reminder(slot_id)

    safe_edit(
        call,
        "✅ Запись отменена.\n\n"
        f"💅 {esc(slot['service_name'] or 'услуга')}\n"
        f"🗓 {fmt_date(slot['date'])} "
        f"в {esc(slot['time'])}",
    )

    safe_send(
        call.message.chat.id,
        "Если захочешь записаться снова, выбери "
        f"«{BTN_BOOK}».",
        main_menu(user_id),
    )

    notify_admin(
        "⚠️ <b>ЗАПИСЬ ОТМЕНЕНА</b>\n\n"
        f"👤 {esc(slot['client_name'] or 'клиент')}\n"
        f"💅 {esc(slot['service_name'] or 'услуга')}\n"
        f"🗓 {fmt_date(slot['date'])} "
        f"в {esc(slot['time'])}",
    )


def send_calendar_link(
    call,
    slot_id: int,
):
    slot = get_slot(slot_id)

    if not slot:
        safe_edit(
            call,
            "Запись не найдена.",
        )
        return

    start = datetime.strptime(
        f"{slot['date']} {slot['time']}",
        "%Y-%m-%d %H:%M",
    )

    duration = slot["duration"] or 60

    end = start + timedelta(
        minutes=duration,
    )

    start_value = start.strftime(
        "%Y%m%dT%H%M%S",
    )

    end_value = end.strftime(
        "%Y%m%dT%H%M%S",
    )

    title = (
        f"{slot['service_name'] or 'Запись'} "
        f"к {MASTER_NAME}"
    )

    calendar_url = (
        "https://calendar.google.com/calendar/render"
        "?action=TEMPLATE"
        f"&text={urllib.parse.quote(title)}"
        f"&dates={start_value}/{end_value}"
        f"&details={urllib.parse.quote(MASTER_ADDRESS)}"
    )

    keyboard = types.InlineKeyboardMarkup()

    keyboard.add(
        types.InlineKeyboardButton(
            "📅 Добавить в календарь",
            url=calendar_url,
        )
    )

    safe_send(
        call.message.chat.id,
        "📅 Нажми кнопку ниже, чтобы добавить "
        "запись в календарь.",
        keyboard,
    )


# ==========================================================
#                    АДМИН-ПАНЕЛЬ
# ==========================================================

def handle_admin_callback(
    call,
    data: str,
):
    user_id = call.from_user.id

    if data == "adm:menu":
        clear_state(user_id)

        safe_edit(
            call,
            "⚙️ <b>Админ-панель</b>",
            admin_menu_kb(),
        )
        return

    if data == "adm:add_service":
        set_state(
            user_id,
            "service_name",
        )

        safe_edit(
            call,
            "🧾 <b>Новая услуга</b>\n\n"
            "Шаг 1/3. Напиши название услуги:",
        )
        return

    if data == "adm:add_slot":
        keyboard = services_kb("aslot")

        if not keyboard.keyboard:
            safe_edit(
                call,
                "Сначала добавь услугу.",
                admin_menu_kb(),
            )
            return

        keyboard.add(
            types.InlineKeyboardButton(
                "◀️ Назад",
                callback_data="adm:menu",
            )
        )

        safe_edit(
            call,
            "Для какой услуги добавить слот?",
            keyboard,
        )
        return

    if data == "adm:gen_week":
        keyboard = services_kb("gsvc")

        if not keyboard.keyboard:
            safe_edit(
                call,
                "Сначала добавь услугу.",
                admin_menu_kb(),
            )
            return

        safe_edit(
            call,
            "Для какой услуги создать расписание?",
            keyboard,
        )
        return

    if data == "adm:today":
        rows = all_bookings(
            only_today=True,
        )

        if not rows:
            safe_edit(
                call,
                "На сегодня записей нет.",
                admin_menu_kb(),
            )
            return

        lines = [
            f"📆 <b>Записи на "
            f"{fmt_date(today_str())}</b>",
            "",
        ]

        total = 0

        for row in rows:
            total += row["price"] or 0

            lines.append(
                f"🕒 <b>{esc(row['time'])}</b> — "
                f"{esc(row['client_name'] or 'клиент')}\n"
                f"💅 {esc(row['service_name'] or 'услуга')} — "
                f"{row['price'] or 0}₽"
            )

            if row["client_comment"]:
                lines.append(
                    f"💬 {esc(row['client_comment'])}"
                )

        lines.append(
            f"\n💰 Итого: <b>{total}₽</b>"
        )

        safe_edit(
            call,
            "\n".join(lines),
            admin_menu_kb(),
        )
        return

    if data == "adm:all":
        rows = all_bookings()

        if not rows:
            safe_edit(
                call,
                "Активных записей нет.",
                admin_menu_kb(),
            )
            return

        lines = [
            "📚 <b>Активные записи:</b>",
            "",
        ]

        current_date = None

        for row in rows:
            if row["date"] != current_date:
                current_date = row["date"]

                lines.append(
                    f"\n<b>{fmt_date(current_date)}</b>"
                )

            lines.append(
                f"🕒 {row['time']} — "
                f"{esc(row['client_name'] or 'клиент')} — "
                f"{esc(row['service_name'] or 'услуга')} — "
                f"{row['price'] or 0}₽"
            )

            if row["client_comment"]:
                lines.append(
                    f"   💬 {esc(row['client_comment'])}"
                )

        safe_send(
            call.message.chat.id,
            "\n".join(lines),
        )

        safe_send(
            call.message.chat.id,
            "⚙️ Меню:",
            admin_menu_kb(),
        )
        return

    if data == "adm:free":
        rows = q_all("""
            SELECT
                s.id,
                s.date,
                s.time,
                sv.name AS service_name
            FROM slots s
            LEFT JOIN services sv
                ON sv.id = s.service_id
            WHERE s.is_booked = 0
              AND (
                  s.date > ?
                  OR (
                      s.date = ?
                      AND s.time >= ?
                  )
              )
            ORDER BY s.date, s.time
            LIMIT 100
        """, (
            today_str(),
            today_str(),
            current_time_str(),
        ))

        if not rows:
            safe_edit(
                call,
                "Свободных слотов нет.",
                admin_menu_kb(),
            )
            return

        keyboard = types.InlineKeyboardMarkup()

        for row in rows:
            keyboard.add(
                types.InlineKeyboardButton(
                    (
                        f"🗑 {fmt_date_short(row['date'])} "
                        f"{row['time']} — "
                        f"{row['service_name'] or 'услуга'}"
                    ),
                    callback_data=f"adel:{row['id']}",
                )
            )

        safe_edit(
            call,
            "🕳 Выбери слот для удаления:",
            keyboard,
        )
        return

    if data == "adm:del_service":
        rows = q_all("""
            SELECT id, name
            FROM services
            WHERE is_active = 1
            ORDER BY id
        """)

        if not rows:
            safe_edit(
                call,
                "Активных услуг нет.",
                admin_menu_kb(),
            )
            return

        keyboard = types.InlineKeyboardMarkup()

        for row in rows:
            keyboard.add(
                types.InlineKeyboardButton(
                    f"❌ Скрыть {row['name']}",
                    callback_data=f"delsvc:{row['id']}",
                )
            )

        safe_edit(
            call,
            "Выбери услугу. Она будет скрыта для новых записей, "
            "но старые данные останутся.",
            keyboard,
        )
        return

    if data == "adm:stats":
        safe_edit(
            call,
            get_statistics_text(),
            admin_menu_kb(),
        )
        return

    if data == "adm:commands":
        safe_edit(
            call,
            "📖 <b>Команды мастера</b>\n\n"
            "/admin — панель управления\n"
            "/add_photo — добавить фото\n"
            "/del_photo — удалить фото\n"
            "/stats — статистика\n"
            "/backup — резервная копия\n"
            "/broadcast текст — рассылка\n"
            "/edit_faq — изменить FAQ\n"
            "/my_q — вопросы клиентов\n"
            "/info — справка",
            admin_menu_kb(),
        )
        return

    if data.startswith("aslot:"):
        service_id = int(data.split(":")[1])
        service = get_service(service_id)

        if not service:
            safe_edit(
                call,
                "Услуга не найдена.",
                admin_menu_kb(),
            )
            return

        set_state(
            user_id,
            "slot_date",
            service_id=service_id,
        )

        safe_edit(
            call,
            f"💅 Услуга: "
            f"<b>{esc(service['name'])}</b>\n\n"
            "Введи дату:\n"
            "сегодня, завтра, 15.03 "
            "или 2026-09-15",
        )
        return

    if data.startswith("gsvc:"):
        service_id = int(data.split(":")[1])
        service = get_service(service_id)

        if not service:
            safe_edit(
                call,
                "Услуга не найдена.",
                admin_menu_kb(),
            )
            return

        set_state(
            user_id,
            "gen_hours",
            service_id=service_id,
        )

        safe_edit(
            call,
            f"💅 Услуга: "
            f"<b>{esc(service['name'])}</b>\n\n"
            "Введи рабочие часы:\n"
            "<code>10:00-19:00</code>",
        )
        return

    if data.startswith("adel:"):
        slot_id = int(data.split(":")[1])

        deleted = q_exec("""
            DELETE FROM slots
            WHERE id = ?
              AND is_booked = 0
        """, (slot_id,))

        safe_edit(
            call,
            (
                "✅ Слот удалён."
                if deleted == 1
                else "⚠️ Слот уже занят или не найден."
            ),
            admin_menu_kb(),
        )
        return

    if data.startswith("delsvc:"):
        service_id = int(data.split(":")[1])

        q_exec("""
            UPDATE services
            SET is_active = 0
            WHERE id = ?
        """, (service_id,))

        safe_edit(
            call,
            "✅ Услуга скрыта.\n"
            "Старые записи и данные сохранены.",
            admin_menu_kb(),
        )
        return


# ==========================================================
#                    ФОТО АДМИНИСТРАТОРА
# ==========================================================

@bot.message_handler(
    content_types=["photo"],
)
def on_photo(message):
    if not is_admin(message.from_user.id):
        return

    state = get_state(message.from_user.id)

    if not state:
        safe_send(
            message.chat.id,
            "Чтобы добавить фото, сначала используй /add_photo.",
        )
        return

    if state["step"] != "add_photo":
        return

    file_id = message.photo[-1].file_id
    caption = message.caption or ""

    q_exec("""
        INSERT INTO portfolio(
            master_id,
            file_id,
            caption,
            created_at
        )
        VALUES(?, ?, ?, ?)
    """, (
        ADMIN_ID,
        file_id,
        caption[:1000],
        now_local().isoformat(),
    ))

    clear_state(message.from_user.id)

    safe_send(
        message.chat.id,
        "✅ Фото добавлено в портфолио.",
        main_menu(message.from_user.id),
    )


# ==========================================================
#                    ТЕКСТОВЫЙ ОБРАБОТЧИК
# ==========================================================

@bot.message_handler(content_types=["text"])
def on_text(message):
    user_id = message.from_user.id
    text = (message.text or "").strip()

    state = get_state(user_id)

    if not state:
        safe_send(
            message.chat.id,
            "🤖 Выбери действие в меню.",
            main_menu(user_id),
        )
        return

    step = state["step"]
    data = state["data"]

    try:
        if step == "await_name":
            if len(text) < 2 or len(text) > 60:
                safe_send(
                    message.chat.id,
                    "Имя должно содержать от 2 до 60 символов.",
                )
                return

            save_client(
                user_id,
                text,
                message.from_user.username or "",
            )

            set_state(
                user_id,
                "await_comment",
                slot_id=data["slot_id"],
            )

            safe_send(
                message.chat.id,
                "💬 Есть ли пожелания к записи?\n\n"
                "Если нет, напиши <code>-</code>.",
            )
            return

        if step == "await_comment":
            comment = ""

            if text != "-":
                comment = text[:1000]

            slot_id = int(data["slot_id"])
            slot = get_slot(slot_id)
            name = get_client_name(user_id) or "Клиент"

            if not slot or slot["is_booked"]:
                clear_state(user_id)

                safe_send(
                    message.chat.id,
                    "😔 Это время уже заняли.",
                    main_menu(user_id),
                )
                return

            set_state(
                user_id,
                "await_confirm",
                slot_id=slot_id,
                comment=comment,
            )

            keyboard = confirm_kb(slot_id)

            safe_send(
                message.chat.id,
                confirm_text(
                    slot,
                    name,
                    comment or "Без пожеланий",
                ),
                keyboard,
            )
            return

        if step == "await_question":
            if len(text) < 3:
                safe_send(
                    message.chat.id,
                    "Вопрос слишком короткий.",
                )
                return

            q_exec("""
                INSERT INTO questions(
                    user_id,
                    question_text,
                    created_at
                )
                VALUES(?, ?, ?)
            """, (
                user_id,
                text[:2000],
                now_local().isoformat(),
            ))

            clear_state(user_id)

            notify_admin(
                "📩 <b>Новый вопрос</b>\n\n"
                f"👤 {esc(message.from_user.first_name or 'клиент')}\n"
                f"🆔 {user_id}\n"
                f"💬 {esc(text)}\n\n"
                "Ответить можно через /my_q.",
            )

            safe_send(
                message.chat.id,
                "✅ Вопрос отправлен мастеру.",
                main_menu(user_id),
            )
            return

        if step == "edit_faq":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            if len(text) < 10:
                safe_send(
                    message.chat.id,
                    "Текст слишком короткий.",
                )
                return

            q_exec("""
                INSERT INTO config(key, value)
                VALUES('faq_text', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value
            """, (text,))

            clear_state(user_id)

            safe_send(
                message.chat.id,
                "✅ FAQ обновлён.",
                main_menu(user_id),
            )
            return

        if step == "reply_question":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            question_id = data["question_id"]
            client_id = data["client_id"]

            q_exec("""
                UPDATE questions
                SET
                    answer_text = ?,
                    is_answered = 1
                WHERE id = ?
            """, (
                text[:3000],
                question_id,
            ))

            clear_state(user_id)

            try:
                bot.send_message(
                    client_id,
                    "📬 <b>Ответ от мастера</b>\n\n"
                    f"{esc(text)}",
                    parse_mode="HTML",
                )

            except Exception as exc:
                logger.warning(
                    "Не удалось отправить ответ клиенту: %s",
                    exc,
                )

            safe_send(
                message.chat.id,
                "✅ Ответ отправлен клиенту.",
                main_menu(user_id),
            )
            return

        if step == "service_name":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            if len(text) < 2 or len(text) > 80:
                safe_send(
                    message.chat.id,
                    "Название должно быть от 2 до 80 символов.",
                )
                return

            set_state(
                user_id,
                "service_duration",
                name=text,
            )

            safe_send(
                message.chat.id,
                "Введи длительность услуги в минутах.\n"
                "Например: <code>60</code>",
            )
            return

        if step == "service_duration":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            if not text.isdigit():
                safe_send(
                    message.chat.id,
                    "Введи число от 5 до 600.",
                )
                return

            duration = int(text)

            if not 5 <= duration <= 600:
                safe_send(
                    message.chat.id,
                    "Введи число от 5 до 600.",
                )
                return

            set_state(
                user_id,
                "service_price",
                name=data["name"],
                duration=duration,
            )

            safe_send(
                message.chat.id,
                "Введи стоимость в рублях.\n"
                "Например: <code>1500</code>",
            )
            return

        if step == "service_price":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            price_text = (
                text
                .replace("₽", "")
                .replace(" ", "")
            )

            if not price_text.isdigit():
                safe_send(
                    message.chat.id,
                    "Введи стоимость целым числом.",
                )
                return

            price = int(price_text)

            if price < 0 or price > 1_000_000:
                safe_send(
                    message.chat.id,
                    "Стоимость должна быть от 0 до 1 000 000.",
                )
                return

            q_exec("""
                INSERT INTO services(
                    name,
                    duration,
                    price,
                    is_active
                )
                VALUES(?, ?, ?, 1)
            """, (
                data["name"],
                data["duration"],
                price,
            ))

            clear_state(user_id)

            safe_send(
                message.chat.id,
                "✅ Услуга добавлена:\n\n"
                f"💅 {esc(data['name'])}\n"
                f"⏱ {data['duration']} мин\n"
                f"💰 {price}₽",
                main_menu(user_id),
            )
            return

        if step == "slot_date":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            date_iso = parse_date_input(text)

            if not date_iso:
                safe_send(
                    message.chat.id,
                    "Не понял дату. Примеры:\n"
                    "завтра\n"
                    "15.03\n"
                    "2026-09-15",
                )
                return

            if date_iso < today_str():
                safe_send(
                    message.chat.id,
                    "Нельзя добавить слот в прошлом.",
                )
                return

            set_state(
                user_id,
                "slot_time",
                service_id=data["service_id"],
                date=date_iso,
            )

            safe_send(
                message.chat.id,
                f"Дата: <b>{fmt_date(date_iso)}</b>\n\n"
                "Введи время.\n"
                "Можно несколько:\n"
                "<code>10:00, 12:30, 15:00</code>",
            )
            return

        if step == "slot_time":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            times, bad = parse_times_input(text)

            if not times:
                safe_send(
                    message.chat.id,
                    "Не понял время. Пример: 10:00, 14:30",
                )
                return

            added = []
            skipped = []

            for time_value in times:
                try:
                    inserted = q_exec("""
                        INSERT OR IGNORE INTO slots(
                            service_id,
                            date,
                            time,
                            is_booked
                        )
                        VALUES(?, ?, ?, 0)
                    """, (
                        data["service_id"],
                        data["date"],
                        time_value,
                    ))

                    if inserted == 1:
                        added.append(time_value)
                    else:
                        skipped.append(time_value)

                except Exception:
                    skipped.append(time_value)

            clear_state(user_id)

            report = [
                f"🗓 {fmt_date(data['date'])}",
            ]

            if added:
                report.append(
                    "✅ Добавлено: "
                    + ", ".join(added)
                )

            if skipped:
                report.append(
                    "⏭ Уже существовали: "
                    + ", ".join(skipped)
                )

            if bad:
                report.append(
                    "⚠️ Не распознано: "
                    + ", ".join(bad)
                )

            safe_send(
                message.chat.id,
                "\n".join(report),
                main_menu(user_id),
            )
            return

        if step == "gen_hours":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            hours = parse_hours_range(text)

            if not hours:
                safe_send(
                    message.chat.id,
                    "Пример правильного формата:\n"
                    "<code>10:00-19:00</code>",
                )
                return

            set_state(
                user_id,
                "gen_interval",
                service_id=data["service_id"],
                start=hours[0],
                end=hours[1],
            )

            safe_send(
                message.chat.id,
                "Введи интервал между записями в минутах.\n"
                "Например: <code>60</code>",
            )
            return

        if step == "gen_interval":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            if not text.isdigit():
                safe_send(
                    message.chat.id,
                    "Введи число от 10 до 480.",
                )
                return

            interval = int(text)

            if not 10 <= interval <= 480:
                safe_send(
                    message.chat.id,
                    "Введи число от 10 до 480.",
                )
                return

            set_state(
                user_id,
                "gen_days",
                service_id=data["service_id"],
                start=data["start"],
                end=data["end"],
                interval=interval,
            )

            safe_send(
                message.chat.id,
                "На сколько дней создать расписание?\n"
                "Например: <code>14</code>\n"
                "Чтобы пропустить выходные: "
                "<code>14 будни</code>",
            )
            return

        if step == "gen_days":
            if not is_admin(user_id):
                clear_state(user_id)
                return

            only_weekdays = "будн" in text.lower()
            digits = re.sub(r"\D", "", text)

            if not digits:
                safe_send(
                    message.chat.id,
                    "Введи количество дней от 1 до 60.",
                )
                return

            days = int(digits)

            if not 1 <= days <= 60:
                safe_send(
                    message.chat.id,
                    "Введи количество дней от 1 до 60.",
                )
                return

            result = generate_slots(
                service_id=data["service_id"],
                start_hm=data["start"],
                end_hm=data["end"],
                step_min=data["interval"],
                days=days,
                only_weekdays=only_weekdays,
            )

            added, skipped, days_used, capped = result

            clear_state(user_id)

            text_result = (
                "📅 <b>Расписание создано</b>\n\n"
                f"✅ Новых слотов: {added}\n"
                f"⏭ Уже существовало: {skipped}\n"
                f"🗓 Дней: {days_used}"
            )

            if capped:
                text_result += (
                    f"\n\n⚠️ Достигнут лимит "
                    f"{MAX_GEN_SLOTS} слотов."
                )

            safe_send(
                message.chat.id,
                text_result,
                main_menu(user_id),
            )
            return

        clear_state(user_id)

        safe_send(
            message.chat.id,
            "🔄 Диалог сброшен.",
            main_menu(user_id),
        )

    except Exception as exc:
        logger.exception(
            "Ошибка обработки шага %s: %s",
            step,
            exc,
        )

        clear_state(user_id)

        safe_send(
            message.chat.id,
            "⚠️ Произошла ошибка. Начни заново.",
            main_menu(user_id),
        )


# ==========================================================
#                    НЕПОДДЕРЖИВАЕМЫЕ ФАЙЛЫ
# ==========================================================

@bot.message_handler(
    content_types=[
        "sticker",
        "voice",
        "video",
        "document",
        "audio",
        "location",
        "contact",
        "animation",
    ]
)
def on_other_content(message):
    safe_send(
        message.chat.id,
        "🤖 Сейчас я понимаю текст, кнопки и фотографии.",
        main_menu(message.from_user.id),
    )


# ==========================================================
#                    HEALTH CHECK ДЛЯ RENDER
# ==========================================================

def start_web_server():
    port_raw = os.getenv("PORT", "").strip()

    if not port_raw:
        return

    try:
        from flask import Flask

    except ImportError:
        logger.warning(
            "Flask не установлен. Health-check отключён."
        )
        return

    app = Flask(__name__)

    @app.route("/")
    @app.route("/health")
    def health():
        return "Bot is alive", 200

    def run_flask():
        try:
            app.run(
                host="0.0.0.0",
                port=int(port_raw),
                debug=False,
                use_reloader=False,
            )

        except Exception as exc:
            logger.exception(
                "Flask завершился: %s",
                exc,
            )

    threading.Thread(
        target=run_flask,
        daemon=True,
        name="flask-health",
    ).start()


# ==========================================================
#                    ЗАПУСК
# ==========================================================

def main():
    acquire_single_instance()

    try:
        init_db()

        start_web_server()

        threading.Thread(
            target=reminder_loop,
            daemon=True,
            name="reminders",
        ).start()

        try:
            bot_info = bot.get_me()

            logger.info(
                "Бот @%s запущен. ADMIN_ID=%s",
                bot_info.username,
                ADMIN_ID,
            )

        except Exception as exc:
            logger.exception(
                "Не удалось проверить бота: %s",
                exc,
            )

        while True:
            try:
                bot.delete_webhook()

                bot.infinity_polling(
                    timeout=30,
                    long_polling_timeout=30,
                    skip_pending=True,
                )

            except Exception as exc:
                logger.exception(
                    "Polling прервался: %s",
                    exc,
                )

                time.sleep(10)

    finally:
        release_single_instance()


if __name__ == "__main__":
    main()
