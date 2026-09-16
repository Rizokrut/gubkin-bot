from __future__ import annotations

import aiosqlite

from config import DB_PATH


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def _time_slot_to_minutes(time_slot: str | None) -> int:
    """
    Преобразует:
        08:30-10:00 -> 510
        10:10-11:40 -> 610

    Нужно для нормальной сортировки расписания по времени.
    """
    if not time_slot:
        return 99999

    try:
        start = str(time_slot).split("-", 1)[0].strip()

        hours, minutes = start.split(":", 1)

        hours = int(hours)
        minutes = int(minutes)

        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            return 99999

        return hours * 60 + minutes

    except (ValueError, TypeError):
        return 99999


def _row_to_dict(cursor, row) -> dict:
    if row is None:
        return {}

    columns = [description[0] for description in cursor.description]

    return dict(zip(columns, row))


async def _add_column_if_missing(
    db: aiosqlite.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    """
    Небольшая миграция старой SQLite-базы.

    Если Render вдруг сохранил старый bot.db,
    новые колонки будут добавлены автоматически.
    """

    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()

    existing = {row[1] for row in rows}

    if column not in existing:
        await db.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                course TEXT,
                group_name TEXT,
                group_id INTEGER,
                reminders_on INTEGER NOT NULL DEFAULT 1,
                remind_minutes INTEGER NOT NULL DEFAULT 5,
                username TEXT,
                first_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS structure (
                course TEXT NOT NULL,
                group_name TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (course, group_id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                weekday INTEGER NOT NULL,
                time_slot TEXT,
                subject TEXT,
                room TEXT,
                teacher TEXT,
                lesson_type TEXT,
                week_parity TEXT,
                is_cancelled INTEGER NOT NULL DEFAULT 0,
                subgroup INTEGER NOT NULL DEFAULT 0,
                sort_key INTEGER NOT NULL DEFAULT 99999
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders_sent (
                telegram_id INTEGER NOT NULL,
                reminder_key TEXT NOT NULL,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, reminder_key)
            )
            """
        )

        # ----------------------------------------------------
        # Миграция старой users таблицы
        # ----------------------------------------------------

        await _add_column_if_missing(
            db,
            "users",
            "reminders_on",
            "INTEGER NOT NULL DEFAULT 1",
        )

        await _add_column_if_missing(
            db,
            "users",
            "remind_minutes",
            "INTEGER NOT NULL DEFAULT 5",
        )

        await _add_column_if_missing(
            db,
            "users",
            "username",
            "TEXT",
        )

        await _add_column_if_missing(
            db,
            "users",
            "first_name",
            "TEXT",
        )

        await _add_column_if_missing(
            db,
            "users",
            "created_at",
            "TEXT",
        )

        # ----------------------------------------------------
        # Миграция старой schedule таблицы
        # ----------------------------------------------------

        await _add_column_if_missing(
            db,
            "schedule",
            "is_cancelled",
            "INTEGER NOT NULL DEFAULT 0",
        )

        await _add_column_if_missing(
            db,
            "schedule",
            "subgroup",
            "INTEGER NOT NULL DEFAULT 0",
        )

        await _add_column_if_missing(
            db,
            "schedule",
            "sort_key",
            "INTEGER NOT NULL DEFAULT 99999",
        )

        # Старые записи могут иметь NULL sort_key.
        await db.execute(
            """
            UPDATE schedule
            SET sort_key = 99999
            WHERE sort_key IS NULL
            """
        )

        await db.execute(
            """
            UPDATE schedule
            SET sort_key = ?
            WHERE time_slot IS NOT NULL
              AND (sort_key IS NULL OR sort_key = 99999)
            """,
            (99999,),
        )

        # Проставляем нормальный sort_key построчно.
        cursor = await db.execute(
            """
            SELECT rowid, time_slot
            FROM schedule
            """
        )

        rows = await cursor.fetchall()

        for rowid, time_slot in rows:
            await db.execute(
                """
                UPDATE schedule
                SET sort_key = ?
                WHERE rowid = ?
                """,
                (
                    _time_slot_to_minutes(time_slot),
                    rowid,
                ),
            )

        await db.execute(
            """
            UPDATE users
            SET reminders_on = 1
            WHERE reminders_on IS NULL
            """
        )

        await db.execute(
            """
            UPDATE users
            SET remind_minutes = 5
            WHERE remind_minutes IS NULL
               OR remind_minutes <= 0
            """
        )

        await db.commit()


# ============================================================
# ПОЛЬЗОВАТЕЛИ
# ============================================================

async def get_user(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                course,
                group_name,
                group_id,
                reminders_on,
                remind_minutes,
                username,
                first_name,
                created_at
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )

        row = await cursor.fetchone()

        if row is None:
            return None

        return dict(row)


async def save_user(
    telegram_id: int,
    course: str,
    group_name: str,
    group_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (
                telegram_id,
                course,
                group_name,
                group_id,
                reminders_on,
                remind_minutes,
                username,
                first_name
            )
            VALUES (?, ?, ?, ?, 1, 5, ?, ?)

            ON CONFLICT(telegram_id)
            DO UPDATE SET
                course = excluded.course,
                group_name = excluded.group_name,
                group_id = excluded.group_id,
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (
                telegram_id,
                course,
                group_name,
                group_id,
                username,
                first_name,
            ),
        )

        await db.commit()


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users"
        )

        row = await cursor.fetchone()

        return int(row[0]) if row else 0


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                course,
                group_name,
                group_id,
                reminders_on,
                remind_minutes,
                username,
                first_name,
                created_at
            FROM users
            ORDER BY
                COALESCE(course, 'яяя'),
                COALESCE(group_name, 'яяя'),
                created_at ASC,
                telegram_id ASC
            """
        )

        rows = await cursor.fetchall()

        return [dict(row) for row in rows]


async def list_users_detailed() -> list[dict]:
    """Все пользователи, сгруппированно по курсу/группе.
    Новые (по created_at) — внизу своей группы."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                course,
                group_name,
                group_id,
                reminders_on,
                remind_minutes,
                username,
                first_name,
                created_at
            FROM users
            ORDER BY
                COALESCE(course, 'яяя'),
                COALESCE(group_name, 'яяя'),
                created_at ASC,
                telegram_id ASC
            """
        )

        rows = await cursor.fetchall()

        return [dict(row) for row in rows]


async def list_users_by_course(course: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                course,
                group_name,
                group_id,
                username,
                first_name,
                created_at
            FROM users
            WHERE course = ?
            ORDER BY
                COALESCE(group_name, 'яяя'),
                created_at ASC,
                telegram_id ASC
            """,
            (course,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def list_users_by_group(course: str, group_name: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                course,
                group_name,
                group_id,
                username,
                first_name,
                created_at
            FROM users
            WHERE course = ? AND group_name = ?
            ORDER BY created_at ASC, telegram_id ASC
            """,
            (course, group_name),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def export_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                course,
                group_name,
                group_id,
                username,
                first_name
            FROM users
            WHERE group_id IS NOT NULL
            ORDER BY
                COALESCE(course, 'яяя'),
                COALESCE(group_name, 'яяя'),
                telegram_id ASC
            """
        )
        return [dict(row) for row in await cursor.fetchall()]


# ============================================================
# СТАТИСТИКА
# ============================================================

async def users_by_course() -> list[tuple[str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                COALESCE(course, 'Без курса') AS course,
                COUNT(*) AS count
            FROM users
            GROUP BY course
            ORDER BY course
            """
        )

        rows = await cursor.fetchall()

        return [
            (str(row[0]), int(row[1]))
            for row in rows
        ]


async def users_by_group() -> list[tuple[str, str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                COALESCE(course, 'Без курса') AS course,
                COALESCE(group_name, 'Без группы') AS group_name,
                COUNT(*) AS count
            FROM users
            GROUP BY course, group_name
            ORDER BY course, group_name
            """
        )

        rows = await cursor.fetchall()

        return [
            (
                str(row[0]),
                str(row[1]),
                int(row[2]),
            )
            for row in rows
        ]


# ============================================================
# СТРУКТУРА КУРСОВ И ГРУПП
# ============================================================

async def get_courses() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT course
            FROM structure
            ORDER BY course
            """
        )

        rows = await cursor.fetchall()

        return [
            str(row[0])
            for row in rows
            if row[0]
        ]


async def get_groups(course: str) -> list[tuple[str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                group_name,
                group_id
            FROM structure
            WHERE course = ?
            ORDER BY group_name
            """,
            (course,),
        )

        rows = await cursor.fetchall()

        return [
            (
                str(row[0]),
                int(row[1]),
            )
            for row in rows
        ]


async def save_structure(
    groups: list[tuple[str, str, int]]
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM structure")

        for course, group_name, group_id in groups:
            await db.execute(
                """
                INSERT OR REPLACE INTO structure (
                    course,
                    group_name,
                    group_id
                )
                VALUES (?, ?, ?)
                """,
                (
                    course,
                    group_name,
                    int(group_id),
                ),
            )

        await db.commit()


# ============================================================
# РАСПИСАНИЕ
# ============================================================

async def save_schedule_for_group(
    group_id: int,
    lessons: list[dict],
) -> None:
    """
    Сохраняет расписание конкретной группы.

    ВАЖНО:
    если lessons пустой — старое расписание НЕ удаляем.
    """

    if not lessons:
        return

    normalized = []

    for lesson in lessons:
        if not isinstance(lesson, dict):
            continue

        try:
            weekday = int(lesson.get("weekday"))
        except (TypeError, ValueError):
            continue

        if weekday < 0 or weekday > 6:
            continue

        time_slot = str(
            lesson.get("time_slot") or ""
        ).strip()

        subject = str(
            lesson.get("subject") or ""
        ).strip()

        room = str(
            lesson.get("room") or ""
        ).strip()

        teacher = str(
            lesson.get("teacher") or ""
        ).strip()

        lesson_type = str(
            lesson.get("lesson_type") or ""
        ).strip()

        week_parity = str(
            lesson.get("week_parity") or "all"
        ).strip()

        raw_cancelled = lesson.get("is_cancelled", False)

        is_cancelled = (
            1
            if raw_cancelled in (
                True,
                1,
                "1",
                "true",
                "True",
            )
            else 0
        )

        try:
            subgroup = int(
                lesson.get("subgroup") or 0
            )
        except (TypeError, ValueError):
            subgroup = 0

        if subgroup not in (1, 2):
            subgroup = 0

        sort_key = _time_slot_to_minutes(time_slot)

        normalized.append(
            (
                int(group_id),
                weekday,
                time_slot,
                subject,
                room,
                teacher,
                lesson_type,
                week_parity,
                is_cancelled,
                subgroup,
                sort_key,
            )
        )

    if not normalized:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM schedule
            WHERE group_id = ?
            """,
            (int(group_id),),
        )

        await db.executemany(
            """
            INSERT INTO schedule (
                group_id,
                weekday,
                time_slot,
                subject,
                room,
                teacher,
                lesson_type,
                week_parity,
                is_cancelled,
                subgroup,
                sort_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized,
        )

        await db.commit()


async def get_schedule_for_day(
    group_id: int,
    weekday: int,
) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                group_id,
                weekday,
                time_slot,
                subject,
                room,
                teacher,
                lesson_type,
                week_parity,
                is_cancelled,
                subgroup,
                sort_key
            FROM schedule
            WHERE group_id = ?
              AND weekday = ?
            ORDER BY
                sort_key ASC,
                is_cancelled ASC,
                subgroup ASC
            """,
            (
                int(group_id),
                int(weekday),
            ),
        )

        rows = await cursor.fetchall()

        return [dict(row) for row in rows]


async def get_schedule_for_week(
    group_id: int,
) -> dict[int, list[dict]]:
    result = {
        weekday: []
        for weekday in range(7)
    }

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                group_id,
                weekday,
                time_slot,
                subject,
                room,
                teacher,
                lesson_type,
                week_parity,
                is_cancelled,
                subgroup,
                sort_key
            FROM schedule
            WHERE group_id = ?
            ORDER BY
                weekday ASC,
                sort_key ASC,
                is_cancelled ASC,
                subgroup ASC
            """,
            (int(group_id),),
        )

        rows = await cursor.fetchall()

        for row in rows:
            item = dict(row)

            weekday = int(item["weekday"])

            if weekday in result:
                result[weekday].append(item)

    return result


# ============================================================
# НАПОМИНАНИЯ
# ============================================================

def _make_reminder_key(
    group_id: int,
    day: str,
    time_slot: str,
    subject: str,
) -> str:
    return (
        f"{group_id}|"
        f"{day}|"
        f"{time_slot}|"
        f"{subject}"
    )


async def reminder_was_sent(
    telegram_id: int,
    day: str,
    time_slot: str,
    subject: str,
) -> bool:
    """
    ВАЖНО:
    сигнатура специально оставлена такой,
    какую использует main.py.

    group_id берём из текущего профиля пользователя,
    поэтому смена группы не ломает систему напоминаний.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT group_id
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )

        row = await cursor.fetchone()

        group_id = int(row[0]) if row and row[0] else 0

        reminder_key = _make_reminder_key(
            group_id,
            day,
            time_slot,
            subject,
        )

        cursor = await db.execute(
            """
            SELECT 1
            FROM reminders_sent
            WHERE telegram_id = ?
              AND reminder_key = ?
            LIMIT 1
            """,
            (
                telegram_id,
                reminder_key,
            ),
        )

        return await cursor.fetchone() is not None


async def mark_reminder_sent(
    telegram_id: int,
    group_id: int,
    day: str,
    time_slot: str,
    subject: str,
) -> None:
    reminder_key = _make_reminder_key(
        int(group_id),
        day,
        time_slot,
        subject,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO reminders_sent (
                telegram_id,
                reminder_key
            )
            VALUES (?, ?)
            """,
            (
                telegram_id,
                reminder_key,
            ),
        )

        await db.commit()


# ============================================================
# НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ
# ============================================================

async def get_user_prefs(
    telegram_id: int,
) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                reminders_on,
                remind_minutes
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )

        row = await cursor.fetchone()

        if row is None:
            return {
                "reminders_on": 1,
                "remind_minutes": 5,
            }

        return {
            "reminders_on": int(
                row["reminders_on"]
                if row["reminders_on"] is not None
                else 1
            ),
            "remind_minutes": int(
                row["remind_minutes"]
                if row["remind_minutes"] is not None
                else 5
            ),
        }


async def set_reminders_on(
    telegram_id: int,
    enabled: bool,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET reminders_on = ?
            WHERE telegram_id = ?
            """,
            (
                1 if enabled else 0,
                telegram_id,
            ),
        )

        await db.commit()


async def set_remind_minutes(
    telegram_id: int,
    minutes: int,
) -> None:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 5

    if minutes <= 0:
        minutes = 5

    if minutes > 120:
        minutes = 120

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET remind_minutes = ?
            WHERE telegram_id = ?
            """,
            (
                minutes,
                telegram_id,
            ),
        )

        await db.commit()


# ============================================================
# ОБЩИЕ НАСТРОЙКИ БОТА
# ============================================================

async def set_setting(
    key: str,
    value: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (
                key,
                value
            )
            VALUES (?, ?)

            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (
                key,
                value,
            ),
        )

        await db.commit()


async def get_setting(
    key: str,
) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT value
            FROM settings
            WHERE key = ?
            """,
            (key,),
        )

        row = await cursor.fetchone()

        if row is None:
            return None

        return row[0]