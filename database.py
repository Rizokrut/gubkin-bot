"""
Модуль работы с базой данных — теперь Turso (облачная SQLite-совместимая
база на движке libSQL) вместо локального файла SQLite.

ПОЧЕМУ: Render на бесплатном плане не гарантирует, что файл bot.db
переживёт деплой — при пересборке диск может обнулиться, и все
зарегистрированные пользователи с их группами пропадают. Turso хранит
данные в облаке отдельно от Render, поэтому переживает любой деплой.

Схема курс -> группа (без отдельного "факультета") и group_id для
запроса к API реального сайта — как и раньше, не менялось.

ВАЖНО (исправление): раньше пары в get_schedule_for_day/_week
сортировались по text-полю time_slot ("10:10-11:40" встаёт раньше
"8:30-9:15", потому что "1" < "8" как текст). Теперь при сохранении
считается числовой sort_key (минуты от полуночи) и сортировка идёт
по нему — пары идут по-настоящему хронологически.

Все публичные функции и их сигнатуры сохранены как в прошлой версии,
плюс добавлены новые (список пользователей, напоминания, настройки
пользователя) — реализованы с нуля под ту же схему.
"""

import libsql_client

from config import TURSO_DATABASE_URL, TURSO_AUTH_TOKEN

_client: "libsql_client.Client | None" = None


def _get_client() -> "libsql_client.Client":
    global _client
    if _client is None:
        _client = libsql_client.create_client(
            url=TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN,
        )
    return _client


def _row_to_dict(columns, row) -> dict:
    return dict(zip(columns, row))


def _rows_to_dicts(result_set) -> list[dict]:
    return [_row_to_dict(result_set.columns, row) for row in result_set.rows]


def _time_to_sort_key(time_slot: str | None) -> int:
    """'HH:MM-HH:MM' -> минуты от полуночи, для правильной хронологической
    сортировки (не текстовой). Если строка кривая — кладём в самый конец."""
    if not time_slot:
        return 9999
    try:
        start = time_slot.split("-")[0].strip()
        hours, minutes = start.split(":")
        return int(hours) * 60 + int(minutes)
    except Exception:
        return 9999


# ---------- Инициализация ----------

async def init_db() -> None:
    client = _get_client()
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            course TEXT,
            group_name TEXT,
            group_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS structure (
            course TEXT NOT NULL,
            group_name TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            PRIMARY KEY (course, group_id)
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule (
            group_id INTEGER NOT NULL,
            weekday INTEGER NOT NULL,
            time_slot TEXT,
            subject TEXT,
            room TEXT,
            teacher TEXT,
            lesson_type TEXT,
            week_parity TEXT,
            sort_key INTEGER
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS user_prefs (
            telegram_id INTEGER PRIMARY KEY,
            reminders_on INTEGER DEFAULT 1,
            remind_minutes INTEGER DEFAULT 30
        )
        """
    )
    await client.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders_sent (
            telegram_id INTEGER NOT NULL,
            reminder_key TEXT NOT NULL,
            sent_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (telegram_id, reminder_key)
        )
        """
    )


# ---------- Пользователи ----------

async def get_user(telegram_id: int):
    client = _get_client()
    rs = await client.execute("SELECT * FROM users WHERE telegram_id = ?", [telegram_id])
    if not rs.rows:
        return None
    return _row_to_dict(rs.columns, rs.rows[0])


async def save_user(telegram_id: int, course: str, group_name: str, group_id: int) -> None:
    client = _get_client()
    await client.execute(
        """
        INSERT INTO users (telegram_id, course, group_name, group_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            course = excluded.course,
            group_name = excluded.group_name,
            group_id = excluded.group_id
        """,
        [telegram_id, course, group_name, group_id],
    )


async def count_users() -> int:
    client = _get_client()
    rs = await client.execute("SELECT COUNT(*) FROM users")
    return rs.rows[0][0] if rs.rows else 0


async def get_all_users() -> list[dict]:
    """Все пользователи, как есть, без сортировки по курсу/группе."""
    client = _get_client()
    rs = await client.execute("SELECT * FROM users ORDER BY created_at")
    return _rows_to_dicts(rs)


async def list_users_detailed() -> list[dict]:
    """Все пользователи, отсортированные для удобного чтения — по курсу и группе."""
    client = _get_client()
    rs = await client.execute(
        "SELECT telegram_id, course, group_name, group_id, created_at "
        "FROM users ORDER BY course, group_name, telegram_id"
    )
    return _rows_to_dicts(rs)


async def users_by_course(course: str) -> list[dict]:
    client = _get_client()
    rs = await client.execute(
        "SELECT * FROM users WHERE course = ? ORDER BY group_name, telegram_id",
        [course],
    )
    return _rows_to_dicts(rs)


async def users_by_group(group_name: str) -> list[dict]:
    client = _get_client()
    rs = await client.execute(
        "SELECT * FROM users WHERE group_name = ? ORDER BY telegram_id",
        [group_name],
    )
    return _rows_to_dicts(rs)


# ---------- Напоминания ----------

async def reminder_was_sent(telegram_id: int, reminder_key: str) -> bool:
    """reminder_key — произвольная строка-идентификатор конкретного напоминания,
    например '2026-09-15|monday' — чтобы не отправить одно и то же дважды."""
    client = _get_client()
    rs = await client.execute(
        "SELECT 1 FROM reminders_sent WHERE telegram_id = ? AND reminder_key = ?",
        [telegram_id, reminder_key],
    )
    return len(rs.rows) > 0


async def mark_reminder_sent(telegram_id: int, reminder_key: str) -> None:
    client = _get_client()
    await client.execute(
        "INSERT OR IGNORE INTO reminders_sent (telegram_id, reminder_key) VALUES (?, ?)",
        [telegram_id, reminder_key],
    )


# ---------- Настройки пользователя (напоминания вкл/выкл, за сколько минут) ----------

async def get_user_prefs(telegram_id: int) -> dict:
    client = _get_client()
    rs = await client.execute(
        "SELECT reminders_on, remind_minutes FROM user_prefs WHERE telegram_id = ?",
        [telegram_id],
    )
    if not rs.rows:
        return {"reminders_on": True, "remind_minutes": 30}
    row = _row_to_dict(rs.columns, rs.rows[0])
    return {
        "reminders_on": bool(row["reminders_on"]),
        "remind_minutes": row["remind_minutes"],
    }


async def set_reminders_on(telegram_id: int, value: bool) -> None:
    client = _get_client()
    await client.execute(
        """
        INSERT INTO user_prefs (telegram_id, reminders_on) VALUES (?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET reminders_on = excluded.reminders_on
        """,
        [telegram_id, 1 if value else 0],
    )


async def set_remind_minutes(telegram_id: int, minutes: int) -> None:
    client = _get_client()
    await client.execute(
        """
        INSERT INTO user_prefs (telegram_id, remind_minutes) VALUES (?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET remind_minutes = excluded.remind_minutes
        """,
        [telegram_id, minutes],
    )


# ---------- Структура курс -> группа ----------

async def get_courses() -> list[str]:
    client = _get_client()
    rs = await client.execute("SELECT DISTINCT course FROM structure ORDER BY course")
    return [row[0] for row in rs.rows]


async def get_groups(course: str) -> list[tuple[str, int]]:
    """Возвращает список (group_name, group_id) для курса."""
    client = _get_client()
    rs = await client.execute(
        "SELECT group_name, group_id FROM structure WHERE course = ? ORDER BY group_name",
        [course],
    )
    return [(row[0], row[1]) for row in rs.rows]


async def save_structure(rows: list[tuple[str, str, int]]) -> None:
    """rows: список (course, group_name, group_id). Полностью перезаписывает таблицу."""
    client = _get_client()
    statements = [libsql_client.Statement("DELETE FROM structure")]
    for course, group_name, group_id in rows:
        statements.append(
            libsql_client.Statement(
                "INSERT OR IGNORE INTO structure (course, group_name, group_id) VALUES (?, ?, ?)",
                [course, group_name, group_id],
            )
        )
    await client.batch(statements)


# ---------- Расписание ----------

async def save_schedule_for_group(group_id: int, lessons: list[dict]) -> None:
    client = _get_client()
    statements = [
        libsql_client.Statement("DELETE FROM schedule WHERE group_id = ?", [group_id])
    ]
    for lesson in lessons:
        time_slot = lesson.get("time_slot")
        statements.append(
            libsql_client.Statement(
                """
                INSERT INTO schedule
                    (group_id, weekday, time_slot, subject, room, teacher,
                     lesson_type, week_parity, sort_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    group_id,
                    lesson.get("weekday"),
                    time_slot,
                    lesson.get("subject"),
                    lesson.get("room"),
                    lesson.get("teacher"),
                    lesson.get("lesson_type"),
                    lesson.get("week_parity", "all"),
                    _time_to_sort_key(time_slot),
                ],
            )
        )
    await client.batch(statements)


async def get_schedule_for_day(group_id: int, weekday: int) -> list[dict]:
    client = _get_client()
    rs = await client.execute(
        "SELECT * FROM schedule WHERE group_id = ? AND weekday = ? ORDER BY sort_key",
        [group_id, weekday],
    )
    return _rows_to_dicts(rs)


async def get_schedule_for_week(group_id: int) -> dict[int, list[dict]]:
    client = _get_client()
    rs = await client.execute(
        "SELECT * FROM schedule WHERE group_id = ? ORDER BY weekday, sort_key",
        [group_id],
    )
    week: dict[int, list[dict]] = {i: [] for i in range(7)}
    for row in rs.rows:
        d = _row_to_dict(rs.columns, row)
        week[d["weekday"]].append(d)
    return week


# ---------- Настройки (кука PHPSESSID и т.п.) ----------

async def set_setting(key: str, value: str) -> None:
    client = _get_client()
    await client.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        [key, value],
    )


async def get_setting(key: str) -> str | None:
    client = _get_client()
    rs = await client.execute("SELECT value FROM settings WHERE key = ?", [key])
    return rs.rows[0][0] if rs.rows else None
