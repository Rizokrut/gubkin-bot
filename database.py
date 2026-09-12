"""
База бота.

Turso: если заданы TURSO_DATABASE_URL и TURSO_AUTH_TOKEN.
Иначе — локальный bot.db, как сейчас.
"""

from __future__ import annotations

import logging

import aiosqlite

from config import DB_PATH, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL

logger = logging.getLogger(__name__)

_turso = None


def _use_turso() -> bool:
    return bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)


def _client():
    global _turso
    if _turso is None:
        from libsql_client import create_client

        _turso = create_client(
            url=TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN,
        )
    return _turso


class _RS:
    def __init__(self, columns, rows):
        self.columns = columns
        self.rows = rows


def _as_dicts(result) -> list[dict]:
    if result is None:
        return []
    cols = list(getattr(result, "columns", None) or [])
    raw_rows = list(getattr(result, "rows", None) or [])
    out = []
    for row in raw_rows:
        if cols:
            out.append({cols[i]: row[i] for i in range(min(len(cols), len(row)))})
        else:
            out.append(row)
    return out


async def _exec(sql: str, args: tuple | list = ()):
    if _use_turso():
        return await _client().execute(sql, list(args) if args else None)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        if sql.lstrip().upper().startswith("SELECT"):
            fetched = await cur.fetchall()
            cols = list(fetched[0].keys()) if fetched else []
            rows = [tuple(r) for r in fetched]
            await db.commit()
            return _RS(cols, rows)
        await db.commit()
        return _RS([], [])


async def _exec_many(sql: str, seq: list[tuple]) -> None:
    if not seq:
        return
    if _use_turso():
        client = _client()
        for args in seq:
            await client.execute(sql, list(args))
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(sql, seq)
        await db.commit()


async def init_db() -> None:
    for sql in (
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            course TEXT,
            group_name TEXT,
            group_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            reminders_on INTEGER DEFAULT 1,
            remind_minutes INTEGER DEFAULT 5,
            username TEXT,
            first_name TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS structure (
            course TEXT NOT NULL,
            group_name TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            PRIMARY KEY (course, group_id)
        )
        """,
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
            is_cancelled INTEGER DEFAULT 0,
            subgroup INTEGER DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reminders (
            telegram_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            time_slot TEXT NOT NULL,
            subject TEXT NOT NULL,
            PRIMARY KEY (telegram_id, day, time_slot, subject)
        )
        """,
    ):
        await _exec(sql)
    for stmt in (
        "ALTER TABLE users ADD COLUMN reminders_on INTEGER DEFAULT 1",
        "ALTER TABLE users ADD COLUMN remind_minutes INTEGER DEFAULT 5",
        "ALTER TABLE users ADD COLUMN username TEXT",
        "ALTER TABLE users ADD COLUMN first_name TEXT",
        "ALTER TABLE schedule ADD COLUMN is_cancelled INTEGER DEFAULT 0",
        "ALTER TABLE schedule ADD COLUMN subgroup INTEGER DEFAULT 0",
    ):
        try:
            await _exec(stmt)
        except Exception:
            pass
    logger.info("DB ready turso=%s", _use_turso())


def _time_slot_to_minutes(time_slot: str) -> int:
    if not time_slot:
        return 99999
    try:
        start = time_slot.split("-")[0].strip()
        h, m = start.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 99999


async def get_user(telegram_id: int):
    res = await _exec("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    rows = _as_dicts(res)
    return rows[0] if rows else None


async def save_user(
    telegram_id: int,
    course: str,
    group_name: str,
    group_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> None:
    await _exec(
        """
        INSERT INTO users (
            telegram_id, course, group_name, group_id, username, first_name
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            course = excluded.course,
            group_name = excluded.group_name,
            group_id = excluded.group_id,
            username = COALESCE(excluded.username, users.username),
            first_name = COALESCE(excluded.first_name, users.first_name)
        """,
        (telegram_id, course, group_name, group_id, username, first_name),
    )


async def count_users() -> int:
    res = await _exec("SELECT COUNT(*) AS c FROM users")
    rows = _as_dicts(res)
    if not rows:
        return 0
    row = rows[0]
    if isinstance(row, dict):
        return int(row.get("c") or row.get("COUNT(*)") or 0)
    return int(row[0])


async def get_all_users() -> list[dict]:
    res = await _exec(
        "SELECT telegram_id, course, group_name, group_id, "
        "COALESCE(reminders_on, 1) AS reminders_on, "
        "COALESCE(remind_minutes, 5) AS remind_minutes "
        "FROM users WHERE group_id IS NOT NULL"
    )
    return _as_dicts(res)


async def list_users_detailed() -> list[dict]:
    res = await _exec(
        "SELECT telegram_id, username, first_name, course, group_name "
        "FROM users ORDER BY course, group_name, telegram_id"
    )
    return _as_dicts(res)


async def users_by_course() -> list[tuple[str, int]]:
    res = await _exec(
        "SELECT course, COUNT(*) AS c FROM users GROUP BY course ORDER BY course"
    )
    out = []
    for r in _as_dicts(res):
        if isinstance(r, dict):
            out.append((r.get("course"), int(r.get("c") or r.get("COUNT(*)") or 0)))
        else:
            out.append((r[0], int(r[1])))
    return out


async def users_by_group() -> list[tuple[str, str, int]]:
    res = await _exec(
        "SELECT course, group_name, COUNT(*) AS c FROM users "
        "GROUP BY course, group_name ORDER BY course, group_name"
    )
    out = []
    for r in _as_dicts(res):
        if isinstance(r, dict):
            out.append(
                (
                    r.get("course"),
                    r.get("group_name"),
                    int(r.get("c") or r.get("COUNT(*)") or 0),
                )
            )
        else:
            out.append((r[0], r[1], int(r[2])))
    return out


async def reminder_was_sent(
    telegram_id: int, day: str, time_slot: str, subject: str
) -> bool:
    res = await _exec(
        "SELECT 1 AS x FROM reminders WHERE telegram_id = ? AND day = ? "
        "AND time_slot = ? AND subject = ?",
        (telegram_id, day, time_slot, subject),
    )
    return bool(_as_dicts(res))


async def mark_reminder_sent(
    telegram_id: int, group_id: int, day: str, time_slot: str, subject: str
) -> None:
    await _exec(
        """
        INSERT OR IGNORE INTO reminders
        (telegram_id, group_id, day, time_slot, subject)
        VALUES (?, ?, ?, ?, ?)
        """,
        (telegram_id, group_id, day, time_slot, subject),
    )


async def get_courses() -> list[str]:
    res = await _exec("SELECT DISTINCT course FROM structure ORDER BY course")
    rows = _as_dicts(res)
    return [r["course"] if isinstance(r, dict) else r[0] for r in rows]


async def get_groups(course: str) -> list[tuple[str, int]]:
    res = await _exec(
        "SELECT group_name, group_id FROM structure WHERE course = ? ORDER BY group_name",
        (course,),
    )
    out = []
    for r in _as_dicts(res):
        if isinstance(r, dict):
            out.append((r["group_name"], r["group_id"]))
        else:
            out.append((r[0], r[1]))
    return out


async def save_structure(rows: list[tuple[str, str, int]]) -> None:
    await _exec("DELETE FROM structure")
    await _exec_many(
        "INSERT OR IGNORE INTO structure (course, group_name, group_id) VALUES (?, ?, ?)",
        list(rows),
    )


async def save_schedule_for_group(group_id: int, lessons: list[dict]) -> None:
    if not lessons:
        return
    await _exec("DELETE FROM schedule WHERE group_id = ?", (group_id,))
    await _exec_many(
        """
        INSERT INTO schedule (group_id, weekday, time_slot, subject, room, teacher, lesson_type, week_parity, is_cancelled, subgroup)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(lesson.get("subgroup") or 0),
            )
            for lesson in lessons
        ],
    )


async def get_schedule_for_day(group_id: int, weekday: int) -> list[dict]:
    res = await _exec(
        "SELECT * FROM schedule WHERE group_id = ? AND weekday = ?",
        (group_id, weekday),
    )
    result = _as_dicts(res)
    result.sort(
        key=lambda r: (
            _time_slot_to_minutes(r.get("time_slot") or ""),
            1 if r.get("is_cancelled") in (1, "1", True) else 0,
        )
    )
    return result


async def get_schedule_for_week(group_id: int) -> dict[int, list[dict]]:
    res = await _exec("SELECT * FROM schedule WHERE group_id = ?", (group_id,))
    rows = _as_dicts(res)
    week: dict[int, list[dict]] = {i: [] for i in range(7)}
    for r in rows:
        week[int(r["weekday"])].append(r)
    for wd in range(7):
        week[wd].sort(
            key=lambda r: (
                _time_slot_to_minutes(r.get("time_slot") or ""),
                1 if r.get("is_cancelled") in (1, "1", True) else 0,
            )
        )
    return week


async def set_setting(key: str, value: str) -> None:
    await _exec(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


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
    await _exec(
        "UPDATE users SET reminders_on = ? WHERE telegram_id = ?",
        (1 if enabled else 0, telegram_id),
    )


async def set_remind_minutes(telegram_id: int, minutes: int) -> None:
    minutes = min(30, max(5, int(minutes)))
    await _exec(
        "UPDATE users SET remind_minutes = ? WHERE telegram_id = ?",
        (minutes, telegram_id),
    )


async def get_setting(key: str) -> str | None:
    res = await _exec("SELECT value FROM settings WHERE key = ?", (key,))
    rows = _as_dicts(res)
    if not rows:
        return None
    row = rows[0]
    return row.get("value") if isinstance(row, dict) else row[0]
