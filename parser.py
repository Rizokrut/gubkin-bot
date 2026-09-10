import logging
from datetime import date

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://lk.gubkin.ru/schedule/api/api.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def make_client(cookie: str) -> httpx.AsyncClient:
    """
    Поддерживает:
    1. только значение PHPSESSID
    2. полную строку Cookie из браузера
    """

    cookie = cookie.strip()

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "User-Agent": USER_AGENT,
        "Referer": "https://lk.gubkin.ru/",
        "Origin": "https://lk.gubkin.ru",
    }

    # Если вставлена полная Cookie-строка:
    # PHPSESSID=xxx; cookie2=yyy
    if "=" in cookie:
        headers["Cookie"] = cookie

        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                connect=10.0,
                read=15.0,
                write=10.0,
                pool=10.0,
            ),
            follow_redirects=True,
        )

    # Если передано только значение PHPSESSID
    return httpx.AsyncClient(
        headers=headers,
        cookies={"PHPSESSID": cookie},
        timeout=httpx.Timeout(
            connect=10.0,
            read=15.0,
            write=10.0,
            pool=10.0,
        ),
        follow_redirects=True,
    )


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
        "========================================"
    )

    logger.info(
        "API REQUEST group=%s date=%s",
        group_id,
        date_str,
    )

    logger.info(
        "URL: %s",
        API_URL,
    )

    logger.info(
        "PARAMS: %s",
        params,
    )

    try:

        async with make_client(cookie) as client:

            response = await client.get(
                API_URL,
                params=params,
            )

            logger.info(
                "API RESPONSE group=%s status=%s",
                group_id,
                response.status_code,
            )

            logger.info(
                "FINAL URL: %s",
                response.url,
            )

            logger.info(
                "CONTENT-TYPE: %s",
                response.headers.get("content-type"),
            )

            body = response.text

            logger.info(
                "BODY FIRST 2000 CHARS:\n%s",
                body[:2000],
            )

            response.raise_for_status()

            try:
                data = response.json()

            except Exception:

                logger.error(
                    "group=%s API НЕ ВЕРНУЛ JSON",
                    group_id,
                )

                return []

            logger.info(
                "JSON KEYS: %s",
                list(data.keys()),
            )

            logger.info(
                "STATE: %r",
                data.get("state"),
            )

            if not data.get("state"):

                logger.error(
                    "group=%s API вернул state=false",
                    group_id,
                )

                return []

            rows = data.get("rows") or {}

            organizations = (
                rows.get("organizations")
                or []
            )

            logger.info(
                "group=%s organizations=%s",
                group_id,
                len(organizations),
            )

            lessons = []

            for organization in organizations:

                time_chunks = (
                    organization.get(
                        "lessonsTimeChunks"
                    )
                    or []
                )

                organization_lessons = (
                    organization.get("lessons")
                    or []
                )

                logger.info(
                    "group=%s organization lessons=%s",
                    group_id,
                    len(organization_lessons),
                )

                for lesson in organization_lessons:

                    rooms = (
                        lesson.get("rooms")
                        or []
                    )

                    room = ""

                    if rooms:
                        room = rooms[0].get(
                            "number",
                            ""
                        )

                    course = (
                        lesson.get("course")
                        or {}
                    )

                    teachers = (
                        lesson.get("teachers")
                        or []
                    )

                    lessons.append(
                        {
                            "weekday": lesson.get(
                                "weekDayNumber"
                            ),

                            "time_slot": format_time_slot(
                                time_chunks,
                                lesson.get(
                                    "timeChunks"
                                )
                                or [],
                            ),

                            "subject": course.get(
                                "name",
                                ""
                            ),

                            "room": room,

                            "teacher": format_teachers(
                                teachers
                            ),

                            "lesson_type": lesson.get(
                                "type",
                                ""
                            ),

                            "week_parity": "all",
                        }
                    )

            logger.info(
                "group=%s FINAL LESSONS=%s",
                group_id,
                len(lessons),
            )

            return lessons

    except httpx.TimeoutException:

        logger.error(
            "group=%s TIMEOUT API",
            group_id,
            exc_info=True,
        )

        return []

    except httpx.HTTPStatusError as e:

        logger.error(
            "group=%s HTTP STATUS ERROR: %s",
            group_id,
            e,
            exc_info=True,
        )

        return []

    except httpx.HTTPError as e:

        logger.error(
            "group=%s HTTP ERROR: %s",
            group_id,
            e,
            exc_info=True,
        )

        return []

    except Exception as e:

        logger.error(
            "group=%s UNEXPECTED ERROR: %s",
            group_id,
            e,
            exc_info=True,
        )

        return []


async def check_cookie_valid(
    cookie: str,
    sample_group_id: int = 9336,
) -> bool:

    today = date.today()

    date_str = (
        f"{today.day}-"
        f"{today.month}-"
        f"{today.year}"
    )

    try:

        async with make_client(cookie) as client:

            response = await client.get(
                API_URL,
                params={
                    "act": "schedule",
                    "date": date_str,
                    "groupId": sample_group_id,
                },
            )

            logger.info(
                "COOKIE CHECK status=%s url=%s",
                response.status_code,
                response.url,
            )

            logger.info(
                "COOKIE CHECK BODY:\n%s",
                response.text[:2000],
            )

            if response.status_code != 200:
                return False

            try:
                data = response.json()

            except Exception:
                return False

            return bool(data.get("state"))

    except Exception:

        logger.exception(
            "Ошибка проверки Cookie"
        )

        return False
