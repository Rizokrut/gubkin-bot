"""
Модуль работы с базой данных SQLite.

Почему SQLite, а не Supabase:
Supabase — это отдельный внешний сервис (облачный PostgreSQL), для которого
нужно создавать аккаунт, проект, получать ключи и настраивать сетевые запросы
из бота. Для бота с такой нагрузкой (личный расписание студентов одного филиала)
это лишняя сложность и лишняя точка отказа. SQLite — это просто один файл
(bot.db) прямо рядом с ботом, ничего дополнительно настраивать не нужно.
Если позже вырастете из SQLite — база легко переносится в PostgreSQL/Supabase,
структура таблиц ниже такая же простая.

ВАЖНО про бесплатные тарифы Render/Railway: на бесплатном тарифе диск может
быть "эфемерным" — то есть при новом деплое (не при обычном перезапуске)
файл bot.db может обнулиться. Если это критично, в разделе инструкции
по деплою я показываю, как подключить постоянный диск (persistent disk).
"""

import aiosqlite
from config import DB_PATH


async def init_db() -> None:
    """Создаёт все нужные таблицы, если их ещё нет. Вызывается один раз при старте бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                course TEXT,
                faculty TEXT,
                group_name TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS structure (
                course TEXT NOT NULL,
                faculty TEXT NOT NULL,
                group_name TEXT NOT NULL,
                PRIMARY KEY (course, faculty, group_name)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule (
                group_name TEXT NOT NULL,
                weekday INTEGER NOT NULL,      -- 0=понедельник ... 6=воскресенье
                time_slot TEXT,
                subject TEXT,
                room TEXT,
                teacher TEXT,
                lesson_type TEXT,               -- лекция/семинар/лаб. и т.п., если есть
                week_parity TEXT                -- 'all' / 'odd' / 'even', если расписание чередуется по неделям
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
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def save_user(telegram_id: int, course: str, faculty: str, group_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, course, faculty, group_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                course = excluded.course,
                faculty = excluded.faculty,
                group_name = excluded.group_name
            """,
            (telegram_id, course, faculty, group_name),
        )
        await db.commit()


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0


# ---------- Структура курс -> факультет -> группа ----------

async def get_courses() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT DISTINCT course FROM structure ORDER BY course")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_faculties(course: str) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT faculty FROM structure WHERE course = ? ORDER BY faculty",
            (course,),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_groups(course: str, faculty: str) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT group_name FROM structure WHERE course = ? AND faculty = ? ORDER BY group_name",
            (course, faculty),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def save_structure(rows: list[tuple[str, str, str]]) -> None:
    """rows: список (course, faculty, group_name). Полностью перезаписывает таблицу структуры."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM structure")
        await db.executemany(
            "INSERT OR IGNORE INTO structure (course, faculty, group_name) VALUES (?, ?, ?)",
            rows,
        )
        await db.commit()


# ---------- Расписание ----------

async def save_schedule_for_group(group_name: str, lessons: list[dict]) -> None:
    """lessons: список словарей с ключами weekday, time_slot, subject, room, teacher, lesson_type, week_parity."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM schedule WHERE group_name = ?", (group_name,))
        await db.executemany(
            """
            INSERT INTO schedule (group_name, weekday, time_slot, subject, room, teacher, lesson_type, week_parity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    group_name,
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


async def get_schedule_for_day(group_name: str, weekday: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM schedule
            WHERE group_name = ? AND weekday = ?
            ORDER BY time_slot
            """,
            (group_name, weekday),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_schedule_for_week(group_name: str) -> dict[int, list[dict]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM schedule
            WHERE group_name = ?
            ORDER BY weekday, time_slot
            """,
            (group_name,),
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
