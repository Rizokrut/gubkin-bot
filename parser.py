"""
Работа с реальным API расписания Губкинского университета.

Важно:
- API находится на lk.gubkin.ru;
- можно передавать как чистый PHPSESSID,
  так и полную строку Cookie из браузера;
- подробные логи нужны, чтобы видеть реальный ответ API на Render.
"""

import logging
from datetime import date

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://lk.gubkin.ru/schedule/api/api.php"


def _build_client(cookie: str) -> httpx.AsyncClient:
    cookie = cookie.strip()

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://lk.gubkin.ru/",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
    }

    # Если пользователь вставил полную Cookie-строку из браузера,
    # передаём её как есть. Если передан только PHPSESSID,
    # httpx сам сформирует Cookie-заголовок.
    if "=" in cookie:
        headers["Cookie"] = cookie
        return httpx.AsyncClient(
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
        )

    return httpx.AsyncClient(
        cookies={"PHPSESSID": cookie},
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
    )


def _format_time_slot(
    time_chunks: list[str],
    indices: list[int],
) -> str:
    if not indices or not time_chunks:
        return ""

    try:
        start = time_chunks[indices[0]].split("-")[0]
        end = time_chunks[indices[-1]].split("-")[-1]
        return f"{start}-{end}"
    except (IndexError, TypeError):
        return ""


def _format_teachers(teachers: list[dict]) -> str:
    names = []

    for teacher in teachers:
        last = teacher.get("lastName") or ""
        first = teacher.get("firstName") or ""
        patronymic = teacher.get("patronymic") or ""

        initials = "".join(
            f"{name[0]}."
            for name in (first, patronymic)
            if name
        )

        full_name = f"{last} {initials}".strip()

        if full_name:
            names.append(full_name)

    return ", ".join(names)


async def fetch_schedule(
    cookie: str,
    group_id: int,
    date_str: str | None = None,
) -> list[dict]:
    if date_str is None:
        today = date.today()
        date_str = f"{today.day}-{today.month}-{today.year}"

    params = {
        "act": "schedule",
        "date": date_str,
        "groupId": group_id,
    }

    logger.info(
        "[Group ID %s] API request: %s params=%s",
        group_id,
        API_URL,
        params,
    )

    try:
        async with _build_client(cookie) as client:
            response = await client.get(
                API_URL,
                params=params,
            )

            logger.info(
                "[Group ID %s] API status=%s url=%s content-type=%s",
                group_id,
                response.status_code,
                response.url,
                response.headers.get("content-type"),
            )

            body_preview = response.text[:2000]

            logger.info(
                "[Group ID %s] API response: %s",
                group_id,
                body_preview,
            )

            response.raise_for_status()

            try:
                data = response.json()
            except Exception:
                logger.exception(
                    "[Group ID %s] API вернул не JSON",
                    group_id,
                )
                return []

            if not data.get("state"):
                logger.error(
                    "[Group ID %s] API state=false. Полный ответ: %s",
                    group_id,
                    body_preview,
                )
                return []

            rows = data.get("rows") or {}
            organizations = rows.get("organizations") or []

            logger.info(
                "[Group ID %s] organizations=%s",
                group_id,
                len(organizations),
            )

            lessons: list[dict] = []

            for organization in organizations:
                time_chunks = (
                    organization.get("lessonsTimeChunks") or []
                )

                organization_lessons = (
                    organization.get("lessons") or []
                )

                for lesson in organization_lessons:
                    rooms = lesson.get("rooms") or []

                    room = ""
                    if rooms:
                        room = rooms[0].get("number", "")

                    lessons.append(
                        {
                            "weekday": lesson.get(
                                "weekDayNumber"
                            ),
                            "time_slot": _format_time_slot(
                                time_chunks,
                                lesson.get("timeChunks") or [],
                            ),
                            "subject": (
                                lesson.get("course") or {}
                            ).get("name", ""),
                            "room": room,
                            "teacher": _format_teachers(
                                lesson.get("teachers") or []
                            ),
                            "lesson_type": lesson.get(
                                "type",
                                "",
                            ),
                            "week_parity": "all",
                        }
                    )

            logger.info(
                "[Group ID %s] FINAL lessons=%s",
                group_id,
                len(lessons),
            )

            return lessons

    except httpx.HTTPError as exc:
        logger.error(
            "[Group ID %s] HTTP error: %s",
            group_id,
            exc,
            exc_info=True,
        )
        return []

    except Exception as exc:
        logger.error(
            "[Group ID %s] Unexpected parser error: %s",
            group_id,
            exc,
            exc_info=True,
        )
        return []


async def check_cookie_valid(
    cookie: str,
    sample_group_id: int = 10494,
) -> bool:
    today = date.today()
    date_str = f"{today.day}-{today.month}-{today.year}"

    try:
        async with _build_client(cookie) as client:
            response = await client.get(
                API_URL,
                params={
                    "act": "schedule",
                    "date": date_str,
                    "groupId": sample_group_id,
                },
            )

            logger.info(
                "Cookie check: status=%s url=%s body=%s",
                response.status_code,
                response.url,
                response.text[:1000],
            )

            if response.status_code >= 400:
                return False

            data = response.json()
            return bool(data.get("state"))

    except Exception:
        logger.exception("Ошибка проверки Cookie")
        return False
