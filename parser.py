"""
Модуль работы с реальным API сайта lk.gubkin.ru.

Найденный рабочий запрос (спасибо перехвату через Eruda):

  GET https://lk.gubkin.ru/schedule/api/api.php?act=schedule&date=D-M-YYYY&groupId=<id>

Отдаёт JSON с расписанием НА ВСЮ НЕДЕЛЮ (не на один день), сгруппированным
по "organizations" (это города/кампусы — Москва, Оренбург, Ташкент, Атырау,
у каждого свои номера временных слотов). Для нужной группы данные лежат внутри
lessons того "organization", к которому реально относится группа — поэтому
мы просто проверяем все organizations и берём то, где lessons не пустой.

weekDayNumber в ответе: 0=понедельник ... 6=воскресенье — то есть совпадает
1-в-1 с тем, что использует наш database.py, конвертировать не нужно.
"""

import httpx

from config import BASE_URL

API_URL = f"{BASE_URL}/schedule/api/api.php"


def _client(cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        cookies={"PHPSESSID": cookie},
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
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
    """
    date_str в формате "D-M-YYYY" (например "10-9-2026"), без ведущих нулей.
    Если не передать — берём сегодняшнюю дату. Один такой запрос отдаёт
    расписание сразу на всю текущую неделю (по весам числитель/знаменатель
    сайт сам разруливает — нам это видно не нужно учитывать отдельно).
    """
    if date_str is None:
        from datetime import date

        today = date.today()
        date_str = f"{today.day}-{today.month}-{today.year}"

    async with _client(cookie) as client:
        response = await client.get(
            API_URL, params={"act": "schedule", "date": date_str, "groupId": group_id}
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("state"):
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


async def check_cookie_valid(cookie: str, sample_group_id: int = 9336) -> bool:
    """Простая проверка: сайт отвечает валидным JSON, а не ошибкой/редиректом на логин."""
    try:
        async with _client(cookie) as client:
            response = await client.get(
                API_URL, params={"act": "schedule", "date": "1-9-2026", "groupId": sample_group_id}
            )
            if response.status_code >= 400:
                return False
            data = response.json()
            return bool(data.get("state"))
    except Exception:  # noqa: BLE001
        return False
