"""
Парсер читает готовый schedule_cache.json с GitHub.
Защита от дубликатов: API Губкина на каждый день возвращает расписание
всей недели целиком, поэтому в кэше один и тот же урок лежит много раз.
Здесь мы дедуплицируем уроки по ключу (weekday, time_slot, subject, room,
teacher, lesson_type).
"""

import logging
import time
from datetime import date

import httpx

logger = logging.getLogger(__name__)

CACHE_URL = "https://raw.githubusercontent.com/Rizokrut/gubkin-bot/main/schedule_cache.json"

CACHE_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)

_CACHE_TTL = 60
_cache: dict | None = None
_cache_time: float = 0.0


class ScheduleAuthError(Exception):
    """Оставлено для совместимости с main.py."""


class ScheduleFormatError(Exception):
    """Оставлено для совместимости с main.py."""


async def _load_cache() -> dict:
    global _cache, _cache_time

    now = time.monotonic()
    if _cache is not None and (now - _cache_time) < _CACHE_TTL:
        return _cache

    logger.info("Скачиваю schedule_cache.json с GitHub")
    async with httpx.AsyncClient(timeout=CACHE_TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get(CACHE_URL)
            response.raise_for_status()
        except httpx.RequestError as e:
            logger.error("Ошибка соединения с кэшем: %r", e)
            if _cache is not None:
                logger.warning("Отдаю устаревший кэш из памяти")
                return _cache
            raise ScheduleAuthError(f"Нет доступа к кэшу: {type(e).__name__}") from e

    try:
        data = response.json()
    except Exception as e:
        logger.error("Кэш не JSON: %r", e)
        raise ScheduleFormatError("Кэш повреждён") from e

    _cache = data
    _cache_time = now
    logger.info("Кэш обновлён, групп: %s", len(data.get("groups", {})))
    return data


def _lesson_signature(lesson: dict) -> tuple:
    """
    Ключ для дедупликации. Если у двух уроков все поля совпадают —
    считаем их одним уроком.
    """
    return (
        lesson.get("weekday"),
        (lesson.get("time_slot") or "").strip(),
        (lesson.get("subject") or "").strip(),
        (lesson.get("room") or "").strip(),
        (lesson.get("teacher") or "").strip(),
        (lesson.get("lesson_type") or "").strip(),
    )


async def fetch_schedule(
    cookie: str | None = None,
    group_id: int = 0,
    date_str: str | None = None,
) -> list[dict]:
    """
    Возвращает список уроков для группы, БЕЗ дубликатов.
    Параметр cookie оставлен для совместимости с main.py.
    """
    cache = await _load_cache()
    groups = cache.get("groups") or {}

    found = None
    for key, info in groups.items():
        if int(info.get("group_id", -1)) == int(group_id):
            found = info
            break

    if not found:
        logger.warning("В кэше нет группы group_id=%s", group_id)
        return []

    days: dict = found.get("days") or {}
    if not days:
        return []

    seen: set = set()
    lessons: list[dict] = []

    for day_lessons in days.values():
        for lesson in day_lessons:
            sig = _lesson_signature(lesson)
            if sig in seen:
                continue
            seen.add(sig)
            lessons.append(lesson)

    logger.info(
        "group=%s: собрано %s уроков после дедупликации",
        group_id,
        len(lessons),
    )
    return lessons


async def check_cookie_valid(cookie: str, sample_group_id: int = 9336) -> bool:
    return True
