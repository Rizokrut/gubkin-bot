"""
Модуль работы с реальным API сайта lk.gubkin.ru.
"""

import logging
import httpx

from config import BASE_URL

API_URL = f"{BASE_URL}/schedule/api/api.php"

# Настройка логирования для отслеживания ошибок в консоли Render
logger = logging.getLogger(__name__)


def _client(cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        cookies={"PHPSESSID": cookie},
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        },
        timeout=30.0,
        follow_redirects=True,
    )


def _format_time_slot(time_chunks: list[str], indices: list[int]) -> str:
    if not indices:
        return ""
    start = time_chunks[indices[0]].split("-")[0]
    end = time_chunks[indices[-1]].split("-")[-1]
    return f"{start}-{end}"


def _format_teachers(teachers: list[dict]) -> str:
    names = []
    for t in teachers:
        last = t.get("lastName") or ""
        first = t.get("firstName") or ""
        patronymic = t.get("patronymic") or ""
        initials = "".join(f"{n[0]}." for n in (first, patronymic) if n)
        names.append(f"{last} {initials}".strip())
    return ", ".join(n for n in names if n)


async def fetch_schedule(cookie: str, group_id: int, date_str: str | None = None) -> list[dict]:
    if date_str is None:
        from datetime import date

        today = date.today()
        date_str = f"{today.day}-{today.month}-{today.year}"

    try:
        async with _client(cookie) as client:
            response = await client.get(
                API_URL, params={"act": "schedule", "date": date_str, "groupId": group_id}
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("state"):
                logger.warning(f"[Group ID {group_id}] API вернул state=false (возможно кука сброшена)")
                return []

            lessons: list[dict] = []
            organizations = data.get("rows", {}).get("organizations", [])
            for org in organizations:
                time_chunks = org.get("lessonsTimeChunks", [])
                for lesson in org.get("lessons", []):
                    rooms = lesson.get("rooms") or []
                    room = rooms[0]["number"] if rooms else ""
                    lessons.append(
                        {
                            "weekday": lesson.get("weekDayNumber"),
                            "time_slot": _format_time_slot(time_chunks, lesson.get("timeChunks", [])),
                            "subject": (lesson.get("course") or {}).get("name", ""),
                            "room": room,
                            "teacher": _format_teachers(lesson.get("teachers") or []),
                            "lesson_type": lesson.get("type", ""),
                            "week_parity": "all",
                        }
                    )
            return lessons
    except Exception as e:
        logger.error(f"[Group ID {group_id}] Ошибка при запросе расписания: {e}")
        return []


async def check_cookie_valid(cookie: str, sample_group_id: int = 10494) -> bool:
    """Проверяет валидность куки на реальной узбекской группе УРИ-26-01 (ID: 10494)."""
    try:
        async with _client(cookie) as client:
            from datetime import date
            today = date.today()
            date_str = f"{today.day}-{today.month}-{today.year}"

            response = await client.get(
                API_URL, params={"act": "schedule", "date": date_str, "groupId": sample_group_id}
            )
            if response.status_code >= 400:
                logger.warning(f"Проверка куки: сайт вернул статус {response.status_code}")
                return False
            data = response.json()
            is_valid = bool(data.get("state"))
            if not is_valid:
                logger.warning(f"Проверка куки: API ответил JSON без state=True ({data})")
            return is_valid
    except Exception as e:
        logger.error(f"Ошибка проверки куки: {e}")
        return False
