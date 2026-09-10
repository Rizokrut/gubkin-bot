"""
Читает один schedule_cache.json с GitHub.

Поддерживает два формата:
  1) Нормализованный: groups[key].lessons = [{weekday, time_slot, subject, ...}]
  2) Старый:         groups[key].days = {date: [lesson, ...]}

Дедупликация по (weekday, time_slot, subject, room, teacher, lesson_type).
Отменённые пары (isCancelled / is_cancelled) пропускаются.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

import httpx

logger = logging.getLogger(__name__)

CACHE_URL = (
    "https://raw.githubusercontent.com/Rizokrut/gubkin-bot/main/schedule_cache.json"
)
CACHE_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)
_CACHE_TTL = 60.0
_cache: dict | None = None
_cache_time: float = 0.0


class ScheduleAuthError(Exception):
    """Совместимость с main.py."""


class ScheduleFormatError(Exception):
    """Совместимость с main.py."""


async def _load_cache() -> dict:
    global _cache, _cache_time

    now = time.monotonic()
    if _cache is not None and (now - _cache_time) < _CACHE_TTL:
        return _cache

    logger.info("Скачиваю schedule_cache.json")
    async with httpx.AsyncClient(timeout=CACHE_TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get(CACHE_URL)
            response.raise_for_status()
        except httpx.RequestError as e:
            logger.error("Ошибка кэша: %r", e)
            if _cache is not None:
                logger.warning("Отдаю устаревший кэш")
                return _cache
            raise ScheduleAuthError(f"Нет доступа к кэшу: {type(e).__name__}") from e

    try:
        data = response.json()
    except Exception as e:
        raise ScheduleFormatError("Кэш не JSON") from e

    if not isinstance(data, dict) or "groups" not in data:
        raise ScheduleFormatError("В кэше нет поля groups")

    _cache = data
    _cache_time = now
    logger.info("Кэш обновлён, групп: %s", len(data.get("groups") or {}))
    return data


def invalidate_cache() -> None:
    global _cache, _cache_time
    _cache = None
    _cache_time = 0.0


def _is_cancelled(lesson: dict) -> bool:
    return bool(
        lesson.get("isCancelled")
        or lesson.get("is_cancelled")
        or lesson.get("cancelled")
    )


def _norm_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _teacher_name(lesson: dict) -> str:
    if lesson.get("teacher"):
        return _norm_text(lesson["teacher"])
    teachers = lesson.get("teachers") or []
    parts = []
    for t in teachers:
        if not isinstance(t, dict):
            continue
        last = _norm_text(t.get("lastName"))
        first = _norm_text(t.get("firstName"))
        patr = _norm_text(t.get("patronymic"))
        initials = ""
        if first:
            initials += first[0] + "."
        if patr:
            initials += patr[0] + "."
        name = f"{last} {initials}".strip()
        if name:
            parts.append(name)
    return ", ".join(parts)


def _room_name(lesson: dict) -> str:
    if lesson.get("room"):
        return _norm_text(lesson["room"])
    rooms = lesson.get("rooms") or []
    numbers = []
    for r in rooms:
        if isinstance(r, dict):
            numbers.append(_norm_text(r.get("number") or r.get("name")))
        else:
            numbers.append(_norm_text(r))
    return ", ".join(x for x in numbers if x)


def _subject_name(lesson: dict) -> str:
    if lesson.get("subject"):
        return _norm_text(lesson["subject"])
    course = lesson.get("course") or {}
    if isinstance(course, dict):
        return _norm_text(course.get("name"))
    return ""


def _time_slot(lesson: dict) -> str:
    slot = _norm_text(lesson.get("time_slot"))
    if slot:
        return slot
    start = _norm_text(lesson.get("start") or lesson.get("timeStart"))
    end = _norm_text(lesson.get("end") or lesson.get("timeEnd"))
    if start and end:
        return f"{start}-{end}"
    return ""


def _weekday(lesson: dict) -> int | None:
    if lesson.get("weekday") is not None:
        try:
            return int(lesson["weekday"])
        except (TypeError, ValueError):
            return None
    # API Губкина: weekDayNumber 1=Пн ... 7=Вс
    raw = lesson.get("weekDayNumber")
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= n <= 7:
        return n - 1
    if 0 <= n <= 6:
        return n
    return None


def _normalize_lesson(lesson: dict) -> dict | None:
    if not isinstance(lesson, dict) or _is_cancelled(lesson):
        return None

    weekday = _weekday(lesson)
    subject = _subject_name(lesson)
    time_slot = _time_slot(lesson)
    if weekday is None or weekday < 0 or weekday > 6 or not subject:
        return None

    return {
        "weekday": weekday,
        "time_slot": time_slot,
        "subject": subject,
        "room": _room_name(lesson),
        "teacher": _teacher_name(lesson),
        "lesson_type": _norm_text(lesson.get("lesson_type") or lesson.get("type")),
        "week_parity": _norm_text(lesson.get("week_parity")) or "all",
    }


def _signature(lesson: dict) -> tuple:
    return (
        lesson.get("weekday"),
        lesson.get("time_slot") or "",
        lesson.get("subject") or "",
        lesson.get("room") or "",
        lesson.get("teacher") or "",
        lesson.get("lesson_type") or "",
    )


def _parse_day_key(key: str):
    raw = str(key).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _iter_raw_lessons(info: dict):
    """
    API Губкина на любой date отдаёт всю неделю.
    Берём один самый свежий снимок из days, иначе две недели смешаются.
    """
    if not isinstance(info, dict):
        return

    lessons = info.get("lessons")
    if isinstance(lessons, list) and lessons:
        for item in lessons:
            yield item
        return

    days = info.get("days") or {}
    if not isinstance(days, dict) or not days:
        return

    dated = []
    undated = []
    for key, day_lessons in days.items():
        if not isinstance(day_lessons, list) or not day_lessons:
            continue
        parsed = _parse_day_key(key)
        if parsed is None:
            undated.append(day_lessons)
        else:
            dated.append((parsed, day_lessons))

    if dated:
        today = date.today()
        # Берём снимок ближайший к сегодня, иначе бот показывает чужую неделю.
        dated.sort(key=lambda x: (abs((x[0] - today).days), -x[0].toordinal()))
        chosen = dated[0][1]
    elif undated:
        chosen = undated[-1]
    else:
        return

    for item in chosen:
        yield item


def extract_group_lessons(info: dict) -> list[dict]:
    seen: set[tuple] = set()
    result: list[dict] = []
    for raw in _iter_raw_lessons(info):
        lesson = _normalize_lesson(raw)
        if not lesson:
            continue
        sig = _signature(lesson)
        if sig in seen:
            continue
        seen.add(sig)
        result.append(lesson)
    result.sort(key=lambda x: (x["weekday"], x["time_slot"], x["subject"]))
    return result


def _find_group(cache: dict, group_id: int) -> dict | None:
    groups = cache.get("groups") or {}
    matches = []
    for info in groups.values():
        if not isinstance(info, dict):
            continue
        try:
            gid = int(info.get("group_id", -1))
        except (TypeError, ValueError):
            continue
        if gid == int(group_id):
            matches.append(info)
    if not matches:
        return None
    # Если два ключа с одним id — берём тот, где больше пар
    matches.sort(key=lambda g: len(extract_group_lessons(g)), reverse=True)
    return matches[0]


async def fetch_schedule(
    cookie: str | None = None,
    group_id: int = 0,
    date_str: str | None = None,
) -> list[dict]:
    cache = await _load_cache()
    found = _find_group(cache, group_id)
    if not found:
        logger.warning("В кэше нет group_id=%s", group_id)
        return []
    lessons = extract_group_lessons(found)
    logger.info("group=%s: %s уникальных пар", group_id, len(lessons))
    return lessons


async def check_cookie_valid(cookie: str, sample_group_id: int = 9336) -> bool:
    return True
