"""
Парсер расписания через официальный API lk.gubkin.ru.

Важно:
- CAPTCHA не используется.
- PHPSESSID не используется.
- Запрос идёт напрямую к API.
- Из ответа берётся только организация "Ташкент".
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx


logger = logging.getLogger(__name__)

# ВАЖНО:
# Не используем BASE_URL из config.py, потому что там по умолчанию
# стоит lk.gubkin.uz, а API расписания находится на lk.gubkin.ru.
API_URL = "https://lk.gubkin.ru/schedule/api/api.php"

TASHKENT_ORGANIZATION = "Ташкент"

REQUEST_TIMEOUT = 20.0


class ScheduleAPIError(Exception):
    """Ошибка ответа API расписания."""


class ScheduleFormatError(ScheduleAPIError):
    """API вернул ответ в неожиданном формате."""


# Оставляем для совместимости со старым main.py.
# Новый main.py больше не будет использовать эту ошибку.
class ScheduleAuthError(ScheduleAPIError):
    """Старая ошибка авторизации. PHPSESSID больше не используется."""


def _safe_text(value) -> str:
    """Безопасно превращает значение в строку."""
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    return str(value).strip()


def _format_time_slot(
    time_chunks: list,
    indices: list,
) -> str:
    """
    Превращает:

    ["8:30-9:15", "9:15-10:00", "10:10-10:55"]
    [0, 1]

    в:

    8:30-10:00
    """

    if not indices:
        return ""

    valid_chunks = []

    for index in indices:
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue

        if 0 <= index < len(time_chunks):
            chunk = time_chunks[index]

            if isinstance(chunk, str) and "-" in chunk:
                valid_chunks.append(chunk.strip())

            elif isinstance(chunk, dict):
                # На случай, если API когда-нибудь начнёт
                # возвращать интервалы объектами.
                start = (
                    chunk.get("start")
                    or chunk.get("from")
                    or chunk.get("begin")
                    or ""
                )
                end = (
                    chunk.get("end")
                    or chunk.get("to")
                    or chunk.get("finish")
                    or ""
                )

                if start and end:
                    valid_chunks.append(f"{start}-{end}")

    if not valid_chunks:
        return ""

    first = valid_chunks[0]
    last = valid_chunks[-1]

    try:
        start_time = first.split("-", 1)[0].strip()
        end_time = last.split("-", 1)[1].strip()
        return f"{start_time}-{end_time}"
    except (IndexError, ValueError):
        return first


def _format_person(person) -> str:
    """Извлекает имя преподавателя из разных вариантов структуры API."""

    if not isinstance(person, dict):
        return _safe_text(person)

    # Возможные готовые поля.
    for key in (
        "fullName",
        "fullname",
        "name",
        "fio",
        "title",
    ):
        value = person.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    last_name = _safe_text(
        person.get("lastName")
        or person.get("surname")
    )

    first_name = _safe_text(
        person.get("firstName")
        or person.get("name")
    )

    patronymic = _safe_text(
        person.get("patronymic")
        or person.get("middleName")
    )

    parts = [
        part
        for part in (
            last_name,
            first_name,
            patronymic,
        )
        if part
    ]

    return " ".join(parts)


def _format_teachers(teachers) -> str:
    """Форматирует список преподавателей."""

    if not teachers:
        return ""

    if isinstance(teachers, dict):
        teachers = [teachers]

    names = []

    for teacher in teachers:
        name = _format_person(teacher)

        if name:
            names.append(name)

    return ", ".join(dict.fromkeys(names))


def _format_rooms(rooms) -> str:
    """Форматирует список аудиторий."""

    if not rooms:
        return ""

    if isinstance(rooms, dict):
        rooms = [rooms]

    result = []

    for room in rooms:
        if isinstance(room, dict):
            value = (
                room.get("number")
                or room.get("name")
                or room.get("title")
                or room.get("room")
                or ""
            )
        else:
            value = room

        value = _safe_text(value)

        if value:
            result.append(value)

    return ", ".join(dict.fromkeys(result))


def _extract_teacher_changes(changes) -> str:
    """
    Если у занятия есть замена преподавателя,
    пытаемся взять преподавателя из changes.
    """

    if not isinstance(changes, dict):
        return ""

    changed_teachers = (
        changes.get("teachers")
        or changes.get("teacher")
        or changes.get("lecturers")
    )

    if not changed_teachers:
        return ""

    return _format_teachers(changed_teachers)


def _group_ids_from_lesson(lesson: dict) -> set[int]:
    """
    Извлекает ID групп, которым принадлежит занятие.

    API может отдавать groups как:
    - [10118, 10119]
    - [{"id": 10118}, {"id": 10119}]
    """

    groups = lesson.get("groups")

    if not groups:
        return set()

    if isinstance(groups, dict):
        groups = [groups]

    result = set()

    for group in groups:
        if isinstance(group, dict):
            value = (
                group.get("id")
                or group.get("groupId")
                or group.get("group_id")
            )
        else:
            value = group

        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue

    return result


def _lesson_belongs_to_group(lesson: dict, group_id: int) -> bool:
    """
    Проверяет, относится ли занятие к запрошенной группе.

    Если API вообще не передал groups, не отбрасываем занятие.
    Это важно для специальных/общих мероприятий.
    """

    groups = _group_ids_from_lesson(lesson)

    if not groups:
        return True

    return group_id in groups


def _get_tashkent_organization(data: dict) -> dict | None:
    """Находит организацию 'Ташкент' в JSON API."""

    rows = data.get("rows")

    if not isinstance(rows, dict):
        return None

    organizations = rows.get("organizations")

    if not isinstance(organizations, list):
        return None

    for organization in organizations:
        if not isinstance(organization, dict):
            continue

        name = _safe_text(organization.get("name"))

        if name == TASHKENT_ORGANIZATION:
            return organization

    return None


def _get_week_type(data: dict) -> str:
    """
    Получает тип недели именно для Ташкента.

    Например:
    lower
    upper
    """

    rows = data.get("rows")

    if not isinstance(rows, dict):
        return "all"

    week_data = rows.get("week")

    if not isinstance(week_data, dict):
        return "all"

    tashkent_week = week_data.get("weekTashkent")

    if not isinstance(tashkent_week, dict):
        return "all"

    week_type = _safe_text(tashkent_week.get("type"))

    return week_type or "all"


def _parse_lesson(
    lesson: dict,
    time_chunks: list,
    week_type: str,
    group_id: int,
) -> dict | None:
    """Превращает один объект API lesson в формат нашей БД."""

    if not isinstance(lesson, dict):
        return None

    if not _lesson_belongs_to_group(lesson, group_id):
        return None

    weekday = lesson.get("weekDayNumber")

    try:
        weekday = int(weekday)
    except (TypeError, ValueError):
        return None

    if weekday < 0 or weekday > 6:
        return None

    time_indices = lesson.get("timeChunks") or []

    if not isinstance(time_indices, list):
        time_indices = [time_indices]

    time_slot = _format_time_slot(
        time_chunks,
        time_indices,
    )

    course = lesson.get("course")

    if isinstance(course, dict):
        subject = (
            course.get("name")
            or course.get("title")
            or ""
        )
    else:
        subject = course or ""

    subject = _safe_text(subject)

    lesson_type = _safe_text(
        lesson.get("type")
        or lesson.get("lessonType")
        or ""
    )

    room = _format_rooms(
        lesson.get("rooms") or []
    )

    teacher = _format_teachers(
        lesson.get("teachers") or []
    )

    # Если обычный teachers пустой, но есть замена,
    # показываем заменённого преподавателя.
    changed_teacher = _extract_teacher_changes(
        lesson.get("changes")
    )

    if changed_teacher:
        teacher = changed_teacher

    # Отменённые пары не удаляем.
    # Иначе студент вообще не поймёт, что занятие отменили.
    if lesson.get("isCanceled") is True:
        if subject:
            subject = f"❌ ОТМЕНЕНО: {subject}"
        else:
            subject = "❌ ОТМЕНЕНО"

    # Если API почему-то прислал полностью пустую запись,
    # не сохраняем мусор в БД.
    if not subject and not time_slot and not room and not teacher:
        return None

    return {
        "weekday": weekday,
        "time_slot": time_slot,
        "subject": subject,
        "room": room,
        "teacher": teacher,
        "lesson_type": lesson_type,
        "week_parity": week_type,
    }


async def fetch_schedule(
    group_id: int,
    date_str: str | None = None,
) -> list[dict]:
    """
    Получает расписание группы напрямую через API.

    PHPSESSID НЕ нужен.

    Один запрос API возвращает данные всей недели.
    """

    if date_str is None:
        tashkent_now = datetime.now(
            ZoneInfo("Asia/Tashkent")
        )

        today = tashkent_now.date()

        date_str = (
            f"{today.day}-{today.month}-{today.year}"
        )

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

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
    }

    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        ) as client:

            response = await client.get(
                API_URL,
                params=params,
            )

            response.raise_for_status()

            logger.info(
                "[Group %s] API status=%s content-type=%s bytes=%s",
                group_id,
                response.status_code,
                response.headers.get("content-type"),
                len(response.content),
            )

            try:
                data = response.json()
            except ValueError as exc:
                logger.error(
                    "[Group %s] API вернул не JSON. Первые 500 символов: %s",
                    group_id,
                    response.text[:500],
                )
                raise ScheduleFormatError(
                    "API вернул не JSON"
                ) from exc

    except httpx.HTTPStatusError as exc:
        logger.error(
            "[Group %s] HTTP ошибка: %s",
            group_id,
            exc,
        )
        raise ScheduleAPIError(
            f"HTTP {exc.response.status_code}"
        ) from exc

    except httpx.RequestError as exc:
        logger.error(
            "[Group %s] Ошибка соединения с API: %s",
            group_id,
            exc,
        )
        raise ScheduleAPIError(
            f"Ошибка соединения: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ScheduleFormatError(
            "Корень JSON не является объектом"
        )

    # ВАЖНО:
    # state=true означает, что API успешно отдал данные.
    # Это НЕ связано с PHPSESSID.
    if data.get("state") is not True:
        logger.warning(
            "[Group %s] API вернул state=%r",
            group_id,
            data.get("state"),
        )

        # Иногда API может вернуть ошибку с дополнительным
        # сообщением. Сохраняем его в лог.
        logger.warning(
            "[Group %s] API response keys=%s",
            group_id,
            list(data.keys()),
        )

        raise ScheduleAPIError(
            f"API state={data.get('state')}"
        )

    organization = _get_tashkent_organization(data)

    if organization is None:
        logger.error(
            "[Group %s] Организация 'Ташкент' не найдена в API.",
            group_id,
        )

        organizations = (
            data.get("rows", {})
            .get("organizations", [])
        )

        names = []

        if isinstance(organizations, list):
            for org in organizations:
                if isinstance(org, dict):
                    names.append(
                        _safe_text(org.get("name"))
                    )

        logger.error(
            "[Group %s] Доступные организации: %s",
            group_id,
            names,
        )

        raise ScheduleFormatError(
            "Организация 'Ташкент' не найдена"
        )

    time_chunks = organization.get(
        "lessonsTimeChunks",
        [],
    )

    if not isinstance(time_chunks, list):
        time_chunks = []

    lessons_from_api = organization.get(
        "lessons",
        [],
    )

    if not isinstance(lessons_from_api, list):
        raise ScheduleFormatError(
            "Поле Ташкент.lessons не является списком"
        )

    week_type = _get_week_type(data)

    parsed_lessons = []

    for lesson in lessons_from_api:
        parsed = _parse_lesson(
            lesson=lesson,
            time_chunks=time_chunks,
            week_type=week_type,
            group_id=group_id,
        )

        if parsed is not None:
            parsed_lessons.append(parsed)

    # Сортируем расписание по дню и времени.
    parsed_lessons.sort(
        key=lambda item: (
            item.get("weekday", 99),
            item.get("time_slot", ""),
            item.get("subject", ""),
        )
    )

    logger.info(
        "[Group %s] Ташкент: API lessons=%s, "
        "parsed lessons=%s, week_type=%s",
        group_id,
        len(lessons_from_api),
        len(parsed_lessons),
        week_type,
    )

    return parsed_lessons


async def check_api(
    sample_group_id: int = 10118,
) -> bool:
    """
    Простая проверка API.

    Можно использовать позже для диагностики.
    """

    try:
        lessons = await fetch_schedule(
            group_id=sample_group_id
        )

        logger.info(
            "API check: group=%s lessons=%s",
            sample_group_id,
            len(lessons),
        )

        return True

    except Exception:
        logger.exception(
            "API check failed for group=%s",
            sample_group_id,
        )
        return False
