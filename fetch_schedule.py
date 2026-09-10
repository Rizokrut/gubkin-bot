"""
Робот для GitHub Actions.
Раз в 30 минут ходит в API Губкина и сохраняет расписание всех групп
в файл schedule_cache.json.
"""

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import httpx

import groups_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("fetch_schedule")

API_URL = "https://lk.gubkin.ru/schedule/api/api.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = httpx.Timeout(connect=6.0, read=8.0, write=6.0, pool=6.0)

DAYS_AHEAD = 14
CONCURRENCY = 4
OUTPUT = Path("schedule_cache.json")


def format_time_slot(time_chunks, indexes):
    if not indexes or not time_chunks:
        return ""
    try:
        first = time_chunks[indexes[0]]
        last = time_chunks[indexes[-1]]
        start = first.split("-")[0].strip()
        end = last.split("-")[-1].strip()
        return f"{start}-{end}"
    except Exception:
        return ""


def format_teachers(teachers):
    result = []
    for teacher in teachers or []:
        last = teacher.get("lastName") or ""
        first = teacher.get("firstName") or ""
        patronymic = teacher.get("patronymic") or ""
        initials = ""
        if first:
            initials += first[0] + "."
        if patronymic:
            initials += patronymic[0] + "."
        name = f"{last} {initials}".strip()
        if name:
            result.append(name)
    return ", ".join(result)


def parse_lessons_from_response(data: dict) -> list[dict]:
    rows = data.get("rows") or {}
    organizations = rows.get("organizations") or []

    lessons = []
    for organization in organizations:
        time_chunks = organization.get("lessonsTimeChunks") or []
        organization_lessons = organization.get("lessons") or []

        for lesson in organization_lessons:
            rooms = lesson.get("rooms") or []
            room = rooms[0].get("number", "") if rooms else ""
            course = lesson.get("course") or {}
            teachers = lesson.get("teachers") or []

            lessons.append(
                {
                    "weekday": lesson.get("weekDayNumber"),
                    "time_slot": format_time_slot(
                        time_chunks, lesson.get("timeChunks") or []
                    ),
                    "subject": course.get("name", ""),
                    "room": room,
                    "teacher": format_teachers(teachers),
                    "lesson_type": lesson.get("type", ""),
                    "week_parity": "all",
                }
            )
    return lessons


async def fetch_one(
    client: httpx.AsyncClient,
    group_id: int,
    date_str: str,
    sem: asyncio.Semaphore,
) -> list[dict] | None:
    params = {"act": "schedule", "date": date_str, "groupId": group_id}
    async with sem:
        try:
            response = await client.get(API_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.warning("group=%s date=%s error=%r", group_id, date_str, e)
            return None

        if not data.get("state"):
            logger.warning("group=%s date=%s state=false", group_id, date_str)
            return None

        return parse_lessons_from_response(data)


async def main() -> None:
    groups = list(groups_data.GROUPS)
    logger.info("Всего групп: %s", len(groups))

    today = date.today()
    dates = [
        (today + timedelta(days=i)).strftime("%d-%m-%Y")
        for i in range(DAYS_AHEAD)
    ]
    logger.info("Дат: %s, первая=%s, последняя=%s", len(dates), dates[0], dates[-1])

    sem = asyncio.Semaphore(CONCURRENCY)
    cache: dict = {
        "generated_at": today.isoformat(),
        "days_ahead": DAYS_AHEAD,
        "groups": {},
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": "https://lk.gubkin.ru/",
        },
    ) as client:
        for course, group_name, group_id in groups:
            key = f"{course}|{group_name}|{group_id}"
            logger.info("Группа %s (%s) — старт", group_name, group_id)

            tasks = [fetch_one(client, group_id, d, sem) for d in dates]
            results = await asyncio.gather(*tasks)

            days_data: dict = {}
            ok_days = 0
            for d, lessons in zip(dates, results):
                if lessons is not None:
                    days_data[d] = lessons
                    ok_days += 1

            cache["groups"][key] = {
                "course": course,
                "name": group_name,
                "group_id": group_id,
                "days": days_data,
            }
            logger.info(
                "Группа %s (%s) — готово, дней с данными: %s/%s",
                group_name, group_id, ok_days, len(dates),
            )

    OUTPUT.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Сохранено в %s", OUTPUT)


if __name__ == "__main__":
    asyncio.run(main())
