from __future__ import annotations

import aiosqlite
from datetime import datetime
from config import DB_PATH


def _time_slot_to_minutes(time_slot: str | None) -> int:
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


async def _add_column_if_missing(
    db: aiosqlite.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                subscription_expires TEXT,
                referrer_id INTEGER,
                invited_count INTEGER DEFAULT 0,
                ref_bonus_claimed INTEGER DEFAULT 0
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

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                tariff_days INTEGER NOT NULL,
                photo_file_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Миграции колонок для старых баз
        await _add_column_if_missing(db, "users", "subscription_expires", "TEXT")
        await _add_column_if_missing(db, "users", "referrer_id", "INTEGER")
        await _add_column_if_missing(db, "users", "invited_count", "INTEGER DEFAULT 0")
        await _add_column_if_missing(db, "users", "ref_bonus_claimed", "INTEGER DEFAULT 0")

        await db.commit()


async def get_user(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT telegram_id, course, group_name, group_id, reminders_on,
                   remind_minutes, username, first_name, created_at,
                   subscription_expires, referrer_id, invited_count, ref_bonus_claimed
            FROM users WHERE telegram_id = ?
            """,
            (telegram_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def save_user(
    telegram_id: int,
    course: str,
    group_name: str,
    group_id: int,
    username: str | None = None,
    first_name: str | None = None,
    referrer_id: int | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверим, существовал ли пользователь раньше
        cursor = await db.execute("SELECT referrer_id FROM users WHERE telegram_id = ?", (telegram_id,))
        existing = await cursor.fetchone()

        ref_to_save = referrer_id
        if existing and existing[0]:
            ref_to_save = existing[0] # Реферер не меняется

        await db.execute(
            """
            INSERT INTO users (
                telegram_id, course, group_name, group_id, reminders_on,
                remind_minutes, username, first_name, referrer_id
            )
            VALUES (?, ?, ?, ?, 1, 5, ?, ?, ?)
            ON CONFLICT(telegram_id)
            DO UPDATE SET
                course = excluded.course,
                group_name = excluded.group_name,
                group_id = excluded.group_id,
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (telegram_id, course, group_name, group_id, username, first_name, ref_to_save),
        )
        await db.commit()


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users ORDER BY created_at ASC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def add_subscription_days(telegram_id: int, days: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT subscription_expires FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        
        now = datetime.now()
        current_expiry = None
        if row and row["subscription_expires"]:
            try:
                current_expiry = datetime.fromisoformat(row["subscription_expires"])
            except ValueError:
                pass

        if current_expiry and current_expiry > now:
            new_expiry = current_expiry.fromordinal(current_expiry.toordinal() + days) # безопасное прибавление дней или через timedelta
            from datetime import timedelta
            new_expiry = current_expiry + timedelta(days=days)
        else:
            from datetime import timedelta
            new_expiry = now + timedelta(days=days)

        new_expiry_str = new_expiry.isoformat()
        await db.execute(
            "UPDATE users SET subscription_expires = ? WHERE telegram_id = ?",
            (new_expiry_str, telegram_id)
        )
        await db.commit()
        return new_expiry.strftime("%d.%m.%Y %H:%M")


async def create_pending_check(telegram_id: int, tariff_days: int, photo_file_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO pending_checks (telegram_id, tariff_days, photo_file_id, status)
            VALUES (?, ?, ?, 'pending')
            """,
            (telegram_id, tariff_days, photo_file_id)
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_check(check_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM pending_checks WHERE id = ?", (check_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_pending_check_status(check_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE pending_checks SET status = ? WHERE id = ?", (status, check_id))
        await db.commit()


async def increment_invited(referrer_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET invited_count = invited_count + 1 WHERE telegram_id = ?", (referrer_id,))
        cursor = await db.execute("SELECT invited_count, ref_bonus_claimed FROM users WHERE telegram_id = ?", (referrer_id,))
        row = await cursor.fetchone()
        await db.commit()
        return row[0] if row else 0


# Структура и расписание
async def get_courses() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT DISTINCT course FROM structure ORDER BY course")
        rows = await cursor.fetchall()
        return [str(row[0]) for row in rows if row[0]]


async def get_groups(course: str) -> list[tuple[str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT group_name, group_id FROM structure WHERE course = ? ORDER BY group_name", (course,))
        rows = await cursor.fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]


async def save_structure(groups: list[tuple[str, str, int]]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM structure")
        for course, group_name, group_id in groups:
            await db.execute(
                "INSERT OR REPLACE INTO structure (course, group_name, group_id) VALUES (?, ?, ?)",
                (course, group_name, int(group_id)),
            )
        await db.commit()


async def save_schedule_for_group(group_id: int, lessons: list[dict]) -> None:
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
        if not (0 <= weekday <= 6):
            continue

        time_slot = str(lesson.get("time_slot") or "").strip()
        subject = str(lesson.get("subject") or "").strip()
        room = str(lesson.get("room") or "").strip()
        teacher = str(lesson.get("teacher") or "").strip()
        lesson_type = str(lesson.get("lesson_type") or "").strip()
        week_parity = str(lesson.get("week_parity") or "all").strip()
        is_cancelled = 1 if lesson.get("is_cancelled") in (True, 1, "1", "true") else 0
        try:
            subgroup = int(lesson.get("subgroup") or 0)
        except (TypeError, ValueError):
            subgroup = 0
        if subgroup not in (1, 2):
            subgroup = 0

        sort_key = _time_slot_to_minutes(time_slot)
        normalized.append((int(group_id), weekday, time_slot, subject, room, teacher, lesson_type, week_parity, is_cancelled, subgroup, sort_key))

    if not normalized:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM schedule WHERE group_id = ?", (int(group_id),))
        await db.executemany(
            """
            INSERT INTO schedule (group_id, weekday, time_slot, subject, room, teacher, lesson_type, week_parity, is_cancelled, subgroup, sort_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized,
        )
        await db.commit()


async def get_schedule_for_day(group_id: int, weekday: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM schedule WHERE group_id = ? AND weekday = ?
            ORDER BY sort_key ASC, is_cancelled ASC, subgroup ASC
            """,
            (int(group_id), int(weekday)),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_schedule_for_week(group_id: int) -> dict[int, list[dict]]:
    result = {w: [] for w in range(7)}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM schedule WHERE group_id = ?
            ORDER BY weekday ASC, sort_key ASC, is_cancelled ASC, subgroup ASC
            """,
            (int(group_id),),
        )
        for row in await cursor.fetchall():
            item = dict(row)
            w = int(item["weekday"])
            if w in result:
                result[w].append(item)
    return result


# Настройки общие (карты, цены)
async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


# Напоминания
def _make_reminder_key(group_id: int, day: str, time_slot: str, subject: str) -> str:
    return f"{group_id}|{day}|{time_slot}|{subject}"


async def reminder_was_sent(telegram_id: int, day: str, time_slot: str, subject: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT group_id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        group_id = int(row[0]) if row and row[0] else 0
        reminder_key = _make_reminder_key(group_id, day, time_slot, subject)
        cursor = await db.execute(
            "SELECT 1 FROM reminders_sent WHERE telegram_id = ? AND reminder_key = ? LIMIT 1",
            (telegram_id, reminder_key)
        )
        return await cursor.fetchone() is not None


async def mark_reminder_sent(telegram_id: int, group_id: int, day: str, time_slot: str, subject: str) -> None:
    reminder_key = _make_reminder_key(int(group_id), day, time_slot, subject)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO reminders_sent (telegram_id, reminder_key) VALUES (?, ?)",
            (telegram_id, reminder_key)
        )
        await db.commit()


async def get_user_prefs(telegram_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT reminders_on, remind_minutes FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        if not row:
            return {"reminders_on": 1, "remind_minutes": 5}
        return {"reminders_on": int(row["reminders_on"] or 1), "remind_minutes": int(row["remind_minutes"] or 5)}


async def set_reminders_on(telegram_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reminders_on = ? WHERE telegram_id = ?", (1 if enabled else 0, telegram_id))
        await db.commit()


async def set_remind_minutes(telegram_id: int, minutes: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET remind_minutes = ? WHERE telegram_id = ?", (minutes, telegram_id))
        await db.commit()
