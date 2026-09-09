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
                week_parity TEXT
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
        await db.commit()


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
            INSERT INTO schedule (group_id, weekday, time_slot, subject, room, teacher, lesson_type, week_parity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                )
                for lesson in lessons
            ],
        )
        await db.commit()


async def get_schedule_for_day(group_id: int, weekday: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM schedule WHERE group_id = ? AND weekday = ? ORDER BY time_slot",
            (group_id, weekday),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_schedule_for_week(group_id: int) -> dict[int, list[dict]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM schedule WHERE group_id = ? ORDER BY weekday, time_slot",
            (group_id,),
        )
        rows = await cursor.fetchall()
    week: dict[int, list[dict]] = {i: [] for i in range(7)}
    for r in rows:
        week[r["weekday"]].append(dict(r))
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


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None
