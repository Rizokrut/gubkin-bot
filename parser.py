import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx


logger = logging.getLogger(__name__)

API_URL = "https://lk.gubkin.ru/schedule/api/api.php"
TASHKENT_ORGANIZATION = "Ташкент"
REQUEST_TIMEOUT = 20.0


class ScheduleAPIError(Exception):
    pass


class ScheduleFormatError(Exception):
    pass


class ScheduleAuthError(Exception):
    pass


def _format_time(value):
    if not value:
        return ""

    value = str(value).strip()

    if ":" in value:
        parts = value.split(":")
        if len(parts) >= 2:
            return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"

    return value


def _safe_text(value):
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    return str(value).strip()


async def fetch_schedule(
    group_id: int,
    date_str: str | None = None,
) -> list[dict]:
    """
    Получает расписание группы через официальный API Губкинского университета.
    """

    if date_str is None:
        now = datetime.now(ZoneInfo("Asia/Tashkent"))
        date_str = f"{now.day}-{now.month}-{now.year}"

    params = {
        "act": "schedule",
        "date": date_str,
        "groupId": group_id,
    }

    logger.info(
        "[Group %s] Запрашиваю API: %s params=%s",
        group_id,
        API_URL,
        params,
    )

    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        ) as client:

            response = await client.get(
                API_URL,
                params=params,
            )

            logger.info(
                "[Group %s] API response: status=%s url=%s",
                group_id,
                response.status_code,
                response.url,
            )

            response.raise_for_status()

            try:
                payload = response.json()
            except Exception as exc:
                logger.exception(
                    "[Group %s] Не удалось распарсить JSON. "
                    "Тип=%s repr=%r body=%r",
                    group_id,
                    type(exc).__name__,
                    exc,
                    response.text[:1000],
                )

                raise ScheduleFormatError(
                    f"API вернул невалидный JSON: "
                    f"{type(exc).__name__}: {exc!r}"
                ) from exc

    except httpx.HTTPStatusError as exc:
        logger.exception(
            "[Group %s] HTTP ошибка. "
            "status=%s type=%s repr=%r",
            group_id,
            exc.response.status_code,
            type(exc).__name__,
            exc,
        )

        raise ScheduleAPIError(
            f"HTTP {exc.response.status_code}"
        ) from exc

    except httpx.RequestError as exc:
        logger.exception(
            "[Group %s] ОШИБКА СОЕДИНЕНИЯ С API. "
            "Тип=%s repr=%r args=%r",
            group_id,
            type(exc).__name__,
            exc,
            getattr(exc, "args", None),
        )

        raise ScheduleAPIError(
            f"Ошибка соединения: "
            f"{type(exc).__name__}: {exc!r}"
        ) from exc

    except Exception as exc:
        logger.exception(
            "[Group %s] НЕОЖИДАННАЯ ОШИБКА. "
            "Тип=%s repr=%r",
            group_id,
            type(exc).__name__,
            exc,
        )

        raise ScheduleAPIError(
            f"Неожиданная ошибка: "
            f"{type(exc).__name__}: {exc!r}"
        ) from exc

    if not isinstance(payload, dict):
        logger.error(
            "[Group %s] API вернул не dict: %r",
            group_id,
            payload,
        )
        raise ScheduleFormatError(
            "Ответ API имеет неправильный формат"
        )

    logger.info(
        "[Group %s] API state=%r",
        group_id,
        payload.get("state"),
    )

    if payload.get("state") is not True:
        logger.error(
            "[Group %s] API вернул state != True: %r",
            group_id,
            payload,
        )

        raise ScheduleAPIError(
            f"API вернул state={payload.get('state')!r}"
        )

    data = payload.get("data")

    if not isinstance(data, dict):
        raise ScheduleFormatError(
            "В ответе API отсутствует корректное поле data"
        )

    rows = data.get("rows")

    if not isinstance(rows, dict):
        raise ScheduleFormatError(
            "В ответе API отсутствует корректное поле rows"
        )

    organization = rows.get("organization")

    if not isinstance(organization, list):
        raise ScheduleFormatError(
            "В ответе API отсутствует список organization"
        )

    tashkent = None

    for item in organization:
        if not isinstance(item, dict):
            continue

        name = _safe_text(
            item.get("name")
            or item.get("title")
            or item.get("organization")
        )

        if name == TASHKENT_ORGANIZATION:
            tashkent = item
            break

    if tashkent is None:
        logger.warning(
            "[Group %s] Организация %r не найдена",
            group_id,
            TASHKENT_ORGANIZATION,
        )
        return []

    week_type = "all"

    week = rows.get("week")

    if isinstance(week, dict):
        week_tashkent = week.get("weekTashkent")

        if isinstance(week_tashkent, dict):
            week_type = (
                _safe_text(
                    week_tashkent.get("type")
                )
                or "all"
            )

    logger.info(
        "[Group %s] week_type=%s",
        group_id,
        week_type,
    )

    lessons_source = (
        tashkent.get("lessons")
        or tashkent.get("schedule")
        or tashkent.get("rows")
        or []
    )

    if isinstance(lessons_source, dict):
        lessons_source = (
            lessons_source.get("lessons")
            or lessons_source.get("schedule")
            or []
        )

    if not isinstance(lessons_source, list):
        raise ScheduleFormatError(
            "Список занятий имеет неправильный формат"
        )

    result = []

    for lesson in lessons_source:
        if not isinstance(lesson, dict):
            continue

        lesson_group_id = (
            lesson.get("groupId")
            or lesson.get("group_id")
        )

        if lesson_group_id is not None:
            try:
                if int(lesson_group_id) != int(group_id):
                    continue
            except (TypeError, ValueError):
                pass

        day = (
            lesson.get("day")
            or lesson.get("weekday")
            or lesson.get("weekDay")
            or ""
        )

        lesson_date = (
            lesson.get("date")
            or lesson.get("lessonDate")
            or date_str
        )

        start_time = _format_time(
            lesson.get("startTime")
            or lesson.get("timeStart")
            or lesson.get("start")
            or ""
        )

        end_time = _format_time(
            lesson.get("endTime")
            or lesson.get("timeEnd")
            or lesson.get("end")
            or ""
        )

        time_value = _safe_text(
            lesson.get("time")
            or lesson.get("timeRange")
            or ""
        )

        if not time_value and (start_time or end_time):
            if start_time and end_time:
                time_value = f"{start_time}-{end_time}"
            else:
                time_value = start_time or end_time

        subject = _safe_text(
            lesson.get("subject")
            or lesson.get("discipline")
            or lesson.get("name")
            or lesson.get("title")
            or ""
        )

        teacher = _safe_text(
            lesson.get("teacher")
            or lesson.get("teacherName")
            or lesson.get("lecturer")
            or ""
        )

        room = _safe_text(
            lesson.get("room")
            or lesson.get("auditorium")
            or lesson.get("classroom")
            or ""
        )

        lesson_type = _safe_text(
            lesson.get("type")
            or lesson.get("lessonType")
            or ""
        )

        cancellation = _safe_text(
            lesson.get("cancel")
            or lesson.get("cancellation")
            or lesson.get("cancelled")
            or ""
        )

        result.append(
            {
                "group_id": int(group_id),
                "date": _safe_text(lesson_date),
                "day": _safe_text(day),
                "time": time_value,
                "start_time": start_time,
                "end_time": end_time,
                "subject": subject,
                "teacher": teacher,
                "room": room,
                "type": lesson_type,
                "cancellation": cancellation,
                "week_type": week_type,
            }
        )

    logger.info(
        "[Group %s] Получено занятий: %s",
        group_id,
        len(result),
    )

    return result


async def check_api(sample_group_id: int = 10118):
    """
    Простая проверка доступности API.
    """

    try:
        lessons = await fetch_schedule(
            group_id=sample_group_id
        )

        logger.info(
            "API CHECK SUCCESS: group=%s lessons=%s",
            sample_group_id,
            len(lessons),
        )

        return True

    except Exception as exc:
        logger.exception(
            "API CHECK FAILED: type=%s repr=%r",
            type(exc).__name__,
            exc,
        )

        return False
