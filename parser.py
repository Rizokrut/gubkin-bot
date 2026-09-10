"""
Парсер читает готовый schedule_cache.json, который собрали с iPhone
через закладку. Напрямую в Gubkin не ходит — Render туда не пускает.
"""

import logging
from datetime import date

import httpx

logger = logging.getLogger(__name__)

CACHE_URL = "https://raw.githubusercontent.com/Rizokrut/gubkin-bot/main/schedule_cache.json"

CACHE_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


class ScheduleAuthError(Exception):
    """Оставлено для совместимости с main.py."""


class ScheduleFormatError(Exception):
    """Оставлено для совместимости с main.py."""


async def _load_cache() -> dict:
    async with httpx.AsyncClient(timeout=CACHE_TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get(CACHE_URL)
            response.raise_for_status()
        except httpx.RequestError as e:
            logger.error("Ошибка соединения с кэшем: %r", e)
            raise ScheduleAuthError(f"Нет доступа к кэшу: {type(e).__name__}") from e

    try:
        return response.json()
    except Exception as e:
        logger.error("Кэш не JSON: %r", e)
        raise ScheduleFormatError("Кэш повреждён") from e


async def fetch_schedule(
    cookie: str | None = None,
    group_id: int = 0,
    date_str: str | None = None,
) -> list[dict]:
    """
    Возвращает список уроков для группы из кэша.
    Параметр cookie оставлен для совместимости с main.py, не используется.
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

    if date_str:
        wanted = None
        try:
            parts = date_str.split("-")
            if len(parts) == 3:
                d, m, y = (int(p) for p in parts)
                wanted = f"{d:02d}-{m:02d}-{y:04d}"
        except Exception:
            wanted = None

        if wanted and wanted in days:
            return days[wanted]
        if wanted:
            return []

    lessons: list[dict] = []
    for day_lessons in days.values():
        lessons.extend(day_lessons)
    return lessons


async def check_cookie_valid(cookie: str, sample_group_id: int = 9336) -> bool:
    """Заглушка — кука больше не нужна."""
    return True
