"""
Модуль работы с базой данных SQLite.

Схема упрощена: только Курс -> Группа (без отдельного "факультета" — на
реальном сайте выбор идёт именно так, факультет отдельным шагом не нужен).
Каждая группа хранит свой числовой group_id — именно он нужен для запроса
к API реального сайта (lk.gubkin.ru).
"""

import aiosqlite
from config import DB_PATH


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
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
                group_id INTEGER NOT NULL,
                weekday INTEGER NOT NULL,
                time_slot TEXT,
                subject TEXT,
                room TEXT,
                teacher TEXT,
                lesson_type TEXT,
                week_parity TEXT,
                is_cancelled INTEGER DEFAULT 0
            )
            """
        )
        try:
            await db.execute(
                "ALTER TABLE schedule ADD COLUMN is_cancelled INTEGER DEFAULT 0"
            )
        except Exception:
            pass
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
            CREATE TABLE IF NOT EXISTS reminders (
                telegram_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                subject TEXT NOT NULL,
                PRIMARY KEY (telegram_id, day, time_slot, subject)
            )
            """
        )
        for stmt in (
            "ALTER TABLE users ADD COLUMN reminders_on INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN remind_minutes INTEGER DEFAULT 5",
        ):
            try:
                await db.execute(stmt)
            except Exception:
                pass
        await db.commit()


# ---------- Сортировка по времени ----------

def _time_slot_to_minutes(time_slot: str) -> int:
    """
    '8:30-10:00' → 510 (8*60+30).
    Если что-то не так — вернём 99999, чтобы пара уехала в конец.
    """
    if not time_slot:
        return 99999
    try:
        start = time_slot.split("-")[0].strip()
        h, m = start.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 99999


# ---------- Пользователи ----------

async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def save_user(telegram_id: int, course: str, group_name: str, group_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, course, group_name, group_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                course = excluded.course,
                group_name = excluded.group_name,
                group_id = excluded.group_id
            """,
            (telegram_id, course, group_name, group_id),
        )
        await db.commit()


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT telegram_id, course, group_name, group_id, "
            "COALESCE(reminders_on, 1) AS reminders_on, "
            "COALESCE(remind_minutes, 5) AS remind_minutes "
            "FROM users WHERE group_id IS NOT NULL"
        )
        return [dict(r) for r in await cursor.fetchall()]


async def users_by_course() -> list[tuple[str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT course, COUNT(*) FROM users GROUP BY course ORDER BY course"
        )
        return await cursor.fetchall()


async def users_by_group() -> list[tuple[str, str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT course, group_name, COUNT(*) FROM users "
            "GROUP BY course, group_name ORDER BY course, group_name"
        )
        return await cursor.fetchall()


async def reminder_was_sent(
    telegram_id: int, day: str, time_slot: str, subject: str
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM reminders WHERE telegram_id = ? AND day = ? "
            "AND time_slot = ? AND subject = ?",
            (telegram_id, day, time_slot, subject),
        )
        return await cursor.fetchone() is not None


async def mark_reminder_sent(
    telegram_id: int, group_id: int, day: str, time_slot: str, subject: str
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO reminders
            (telegram_id, group_id, day, time_slot, subject)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, group_id, day, time_slot, subject),
        )
        await db.commit()


# ---------- Структура курс -> группа ----------

async def get_courses() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT DISTINCT course FROM structure ORDER BY course")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_groups(course: str) -> list[tuple[str, int]]:
    """Возвращает список (group_name, group_id) для курса."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT group_name, group_id FROM structure WHERE course = ? ORDER BY group_name",
            (course,),
        )
        return await cursor.fetchall()


async def save_structure(rows: list[tuple[str, str, int]]) -> None:
    """rows: список (course, group_name, group_id). Полностью перезаписывает таблицу."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM structure")
        await db.executemany(
            "INSERT OR IGNORE INTO structure (course, group_name, group_id) VALUES (?, ?, ?)",
            rows,
        )
        await db.commit()


# ---------- Расписание ----------

async def save_schedule_for_group(group_id: int, lessons: list[dict]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM schedule WHERE group_id = ?", (group_id,))
        await db.executemany(
            """
            INSERT INTO schedule (group_id, weekday, time_slot, subject, room, teacher, lesson_type, week_parity, is_cancelled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    group_id,
                    lesson.get("weekday"),
                    lesson.get("time_slot"),
                    lesson.get("subject"),
                    lesson.get("room"),
                    lesson.get("teacher"),
                    lesson.get("lesson_type"),
                    lesson.get("week_parity", "all"),
                    1 if lesson.get("is_cancelled") else 0,
                )
                for lesson in lessons
            ],
        )
        await db.commit()


async def get_schedule_for_day(group_id: int, weekday: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM schedule WHERE group_id = ? AND weekday = ?",
            (group_id, weekday),
        )
        rows = await cursor.fetchall()

    result = [dict(r) for r in rows]
    result.sort(key=lambda r: _time_slot_to_minutes(r.get("time_slot", "")))
    return result


async def get_schedule_for_week(group_id: int) -> dict[int, list[dict]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM schedule WHERE group_id = ?",
            (group_id,),
        )
        rows = await cursor.fetchall()

    week: dict[int, list[dict]] = {i: [] for i in range(7)}
    for r in rows:
        week[r["weekday"]].append(dict(r))

    for wd in range(7):
        week[wd].sort(key=lambda r: _time_slot_to_minutes(r.get("time_slot", "")))

    return week


# ---------- Настройки (кука PHPSESSID и т.п.) ----------

async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def get_user_prefs(telegram_id: int) -> dict:
    user = await get_user(telegram_id)
    if not user:
        return {"reminders_on": 1, "remind_minutes": 5}
    on = user.get("reminders_on")
    minutes = user.get("remind_minutes")
    if on is None:
        on = 1
    if not minutes:
        minutes = 5
    return {"reminders_on": int(on), "remind_minutes": int(minutes)}


async def set_reminders_on(telegram_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET reminders_on = ? WHERE telegram_id = ?",
            (1 if enabled else 0, telegram_id),
        )
        await db.commit()


async def set_remind_minutes(telegram_id: int, minutes: int) -> None:
    minutes = min(30, max(5, int(minutes)))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET remind_minutes = ? WHERE telegram_id = ?",
            (minutes, telegram_id),
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None
