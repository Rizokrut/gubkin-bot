"""
Парсер читает schedule_cache.json, собранный с iPhone через закладку.
Берём уроки ТОЛЬКО за текущую неделю (сегодня + 6 дней) и убираем дубликаты.
"""

import logging
from datetime import date, timedelta

import httpx

logger = logging.getLogger(__name__)

CACHE_URL = "https://raw.githubusercontent.com/Rizokrut/gubkin-bot/main/schedule_cache.json"

CACHE_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


class ScheduleAuthError(Exception):
    """Оставлено для совместимости с main.py."""


class ScheduleFormatError(Exception):
    """Оставлено для совместимости с main.py."""


def _date_key(d: date) -> str:
    """Ключ в формате, который использовался при сборе: 'дд-мм-гггг'."""
    return f"{d.day:02d}-{d.month:02d}-{d.year:04d}"


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
    Возвращает плоский список уроков для группы.
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

    # Определяем, какие дни нам нужны
    wanted_keys = []
    if date_str:
        # Явно переданная дата (формат дд-мм-гггг или д-м-гггг)
        try:
            parts = date_str.split("-")
            if len(parts) == 3:
                d, m, y = (int(p) for p in parts)
                wanted_keys = [f"{d:02d}-{m:02d}-{y:04d}"]
        except Exception:
            wanted_keys = []
    else:
        # По умолчанию: сегодня + 6 дней (одна неделя)
        today = date.today()
        wanted_keys = [_date_key(today + timedelta(days=i)) for i in range(7)]

    # Собираем уроки и убираем дубликаты
    seen = set()
    lessons: list[dict] = []

    for key in wanted_keys:
        day_lessons = days.get(key)
        if not day_lessons:
            continue

        for lesson in day_lessons:
            # Уникальный ключ: день недели + время + предмет + тип + аудитория
            sig = (
                lesson.get("weekday"),
                lesson.get("time_slot") or "",
                lesson.get("subject") or "",
                lesson.get("lesson_type") or "",
                lesson.get("room") or "",
                lesson.get("teacher") or "",
            )
            if sig in seen:
                continue
            seen.add(sig)
            lessons.append(lesson)

    logger.info(
        "group=%s: собрано %s уроков (дни: %s)",
        group_id, len(lessons), wanted_keys,
    )
    return lessons


async def check_cookie_valid(cookie: str, sample_group_id: int = 9336) -> bool:
    """Заглушка — кука больше не нужна."""
    return True
