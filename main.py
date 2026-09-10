import asyncio
import logging
import os
import socket
import ssl
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
import keyboards as kb
import groups_data
import parser as site_parser

from parser import ScheduleAPIError, ScheduleFormatError
from config import BOT_TOKEN, ADMIN_ID, BASE_URL


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================================================
# ROUTER
# =========================================================

router = Router()

WEEKDAY_NAMES_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]

TASHKENT_TZ = ZoneInfo("Asia/Tashkent")

UPDATE_CONCURRENCY = 6
PER_GROUP_TIMEOUT = 25


# =========================================================
# REGISTRATION STATES
# =========================================================

class Registration(StatesGroup):
    choosing_course = State()
    choosing_group = State()


def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID


# =========================================================
# FORMAT SCHEDULE
# =========================================================

def format_day(
    group_name: str,
    weekday: int,
    lessons: list[dict],
) -> str:

    header = (
        f"<b>{WEEKDAY_NAMES_RU[weekday]}</b> "
        f"— группа {group_name}\n\n"
    )

    if not lessons:
        return header + "Пар нет 🎉"

    lines = []

    for lesson in lessons:

        type_part = (
            f" ({lesson['lesson_type']})"
            if lesson.get("lesson_type")
            else ""
        )

        time_slot = lesson.get("time_slot", "")
        subject = lesson.get("subject", "")
        room = lesson.get("room", "")
        teacher = lesson.get("teacher", "")

        lines.append(
            f"⏰ <b>{time_slot}</b>\n"
            f"📘 {subject}{type_part}\n"
            f"🚪 {room}    👤 {teacher}"
        )

    return header + "\n\n".join(lines)


async def send_day_schedule(
    message: Message,
    group_name: str,
    group_id: int,
    weekday: int,
) -> None:

    lessons = await db.get_schedule_for_day(
        group_id,
        weekday,
    )

    await message.answer(
        format_day(
            group_name,
            weekday,
            lessons,
        )
    )


# =========================================================
# /START
# =========================================================

@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
) -> None:

    user = await db.get_user(
        message.from_user.id
    )

    if user and user.get("group_id"):

        await message.answer(
            f"С возвращением! "
            f"Ваша группа: <b>{user['group_name']}</b>",
            reply_markup=kb.main_menu_keyboard(),
        )

        return

    courses = await db.get_courses()

    if not courses:

        await message.answer(
            "База расписания пока пуста.\n\n"
            "Попросите администратора выполнить команду /update."
        )

        return

    await state.set_state(
        Registration.choosing_course
    )

    await message.answer(
        "Привет! Давай выберем твою группу.\n\n"
        "Шаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )


# =========================================================
# CHOOSE COURSE
# =========================================================

@router.callback_query(
    Registration.choosing_course,
    F.data.startswith("course:"),
)
async def choose_course(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:

    course = callback.data.split(
        ":",
        1,
    )[1]

    await state.update_data(
        course=course
    )

    groups = await db.get_groups(course)

    await state.set_state(
        Registration.choosing_group
    )

    await callback.message.edit_text(
        f"Курс: {course}\n\n"
        "Шаг 2 из 2 — выбери группу:",
        reply_markup=kb.groups_keyboard(groups),
    )

    await callback.answer()


# =========================================================
# BACK TO COURSE
# =========================================================

@router.callback_query(
    Registration.choosing_group,
    F.data == "back_to_course",
)
async def back_to_course(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:

    courses = await db.get_courses()

    await state.set_state(
        Registration.choosing_course
    )

    await callback.message.edit_text(
        "Шаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )

    await callback.answer()


# =========================================================
# CHOOSE GROUP
# =========================================================

@router.callback_query(
    Registration.choosing_group,
    F.data.startswith("group:"),
)
async def choose_group(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:

    group_id = int(
        callback.data.split(
            ":",
            1,
        )[1]
    )

    data = await state.get_data()

    course = data.get("course")

    if not course:

        await callback.answer(
            "Сначала выбери курс",
            show_alert=True,
        )

        return

    groups = await db.get_groups(course)

    group_name = next(
        (
            name
            for name, gid in groups
            if gid == group_id
        ),
        str(group_id),
    )

    await db.save_user(
        callback.from_user.id,
        course,
        group_name,
        group_id,
    )

    await state.clear()

    await callback.message.edit_text(
        f"Готово! Твоя группа: "
        f"<b>{group_name}</b>"
    )

    await callback.message.answer(
        "Открываю главное меню 👇",
        reply_markup=kb.main_menu_keyboard(),
    )

    await callback.answer()


# =========================================================
# CHANGE GROUP
# =========================================================

@router.message(
    F.text == "⚙️ Сменить группу"
)
async def change_group(
    message: Message,
    state: FSMContext,
) -> None:

    courses = await db.get_courses()

    if not courses:

        await message.answer(
            "База расписания пока пуста."
        )

        return

    await state.set_state(
        Registration.choosing_course
    )

    await message.answer(
        "Шаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )


# =========================================================
# TODAY
# =========================================================

@router.message(
    F.text == "📅 На сегодня"
)
async def today_schedule(
    message: Message,
) -> None:

    user = await db.get_user(
        message.from_user.id
    )

    if not user or not user.get("group_id"):

        await message.answer(
            "Сначала выбери группу командой /start"
        )

        return

    now = datetime.now(TASHKENT_TZ)

    weekday = now.weekday()

    await send_day_schedule(
        message,
        user["group_name"],
        user["group_id"],
        weekday,
    )


# =========================================================
# TOMORROW
# =========================================================

@router.message(
    F.text == "📆 На завтра"
)
async def tomorrow_schedule(
    message: Message,
) -> None:

    user = await db.get_user(
        message.from_user.id
    )

    if not user or not user.get("group_id"):

        await message.answer(
            "Сначала выбери группу командой /start"
        )

        return

    now = datetime.now(TASHKENT_TZ)

    weekday = (
        now + timedelta(days=1)
    ).weekday()

    await send_day_schedule(
        message,
        user["group_name"],
        user["group_id"],
        weekday,
    )


# =========================================================
# WEEK
# =========================================================

@router.message(
    F.text == "🗓 На неделю"
)
async def week_schedule(
    message: Message,
) -> None:

    user = await db.get_user(
        message.from_user.id
    )

    if not user or not user.get("group_id"):

        await message.answer(
            "Сначала выбери группу командой /start"
        )

        return

    group_name = user["group_name"]

    week = await db.get_schedule_for_week(
        user["group_id"]
    )

    for weekday in range(7):

        await message.answer(
            format_day(
                group_name,
                weekday,
                week[weekday],
            )
        )

        await asyncio.sleep(0.1)


# =========================================================
# SITE LINK
# =========================================================

@router.message(
    F.text == "🔗 Ссылка на сайт"
)
async def site_link(
    message: Message,
) -> None:

    user = await db.get_user(
        message.from_user.id
    )

    group_name = (
        user["group_name"]
        if user
        else ""
    )

    await message.answer(
        f"Сайт расписания: {BASE_URL}\n"
        f"Твоя группа: <b>{group_name}</b>"
    )


# =========================================================
# /TESTAPI
# =========================================================

@router.message(
    Command("testapi")
)
async def test_api(
    message: Message,
) -> None:

    if not is_admin(
        message.from_user.id
    ):
        return

    status = await message.answer(
        "🔎 <b>Проверяю соединение Render → Gubkin...</b>"
    )

    host = "lk.gubkin.ru"
    port = 443

    lines = [
        "<b>🔎 Диагностика Gubkin API</b>",
        "",
        f"Хост: <code>{host}</code>",
        f"Порт: <code>{port}</code>",
    ]

    # -----------------------------------------------------
    # DNS
    # -----------------------------------------------------

    try:

        addresses = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            ),
        )

        unique_addresses = []

        for item in addresses:

            family = item[0]
            sockaddr = item[4]

            ip = sockaddr[0]

            family_name = (
                "IPv4"
                if family == socket.AF_INET
                else "IPv6"
                if family == socket.AF_INET6
                else str(family)
            )

            entry = (
                family_name,
                ip,
            )

            if entry not in unique_addresses:
                unique_addresses.append(entry)

        if unique_addresses:

            lines.append("")
            lines.append("<b>DNS:</b> ✅")

            for family_name, ip in unique_addresses:

                lines.append(
                    f"• {family_name}: <code>{ip}</code>"
                )

        else:

            lines.append("")
            lines.append(
                "<b>DNS:</b> ⚠️ адреса не найдены"
            )

    except Exception as exc:

        logger.exception(
            "DNS TEST FAILED"
        )

        lines.append("")
        lines.append(
            "<b>DNS:</b> ❌"
        )
        lines.append(
            f"<code>{type(exc).__name__}: {exc!r}</code>"
        )

        await status.edit_text(
            "\n".join(lines)
        )

        return

    # -----------------------------------------------------
    # TCP TEST
    # -----------------------------------------------------

    lines.append("")
    lines.append("<b>TCP CONNECT:</b>")

    tested = set()

    for family_name, ip in unique_addresses:

        key = (
            family_name,
            ip,
        )

        if key in tested:
            continue

        tested.add(key)

        family = (
            socket.AF_INET
            if family_name == "IPv4"
            else socket.AF_INET6
        )

        start_time = asyncio.get_running_loop().time()

        try:

            if family == socket.AF_INET:

                connection = await asyncio.wait_for(
                    asyncio.open_connection(
                        host=ip,
                        port=port,
                        family=socket.AF_INET,
                    ),
                    timeout=8,
                )

            else:

                connection = await asyncio.wait_for(
                    asyncio.open_connection(
                        host=ip,
                        port=port,
                        family=socket.AF_INET6,
                    ),
                    timeout=8,
                )

            reader, writer = connection

            elapsed = (
                asyncio.get_running_loop().time()
                - start_time
            )

            writer.close()

            try:
                await writer.wait_closed()
            except Exception:
                pass

            lines.append(
                f"• {family_name} <code>{ip}</code>: "
                f"✅ подключение "
                f"({elapsed:.2f}s)"
            )

        except asyncio.TimeoutError:

            elapsed = (
                asyncio.get_running_loop().time()
                - start_time
            )

            lines.append(
                f"• {family_name} <code>{ip}</code>: "
                f"❌ timeout "
                f"({elapsed:.2f}s)"
            )

        except Exception as exc:

            elapsed = (
                asyncio.get_running_loop().time()
                - start_time
            )

            logger.exception(
                "TCP TEST FAILED %s %s",
                family_name,
                ip,
            )

            lines.append(
                f"• {family_name} <code>{ip}</code>: "
                f"❌ {type(exc).__name__}: "
                f"{exc!r} "
                f"({elapsed:.2f}s)"
            )

    # -----------------------------------------------------
    # HTTPS TEST
    # -----------------------------------------------------

    lines.append("")
    lines.append("<b>HTTPS:</b>")

    try:

        start_time = asyncio.get_running_loop().time()

        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
        ) as client:

            response = await client.get(
                "https://lk.gubkin.ru/"
            )

        elapsed = (
            asyncio.get_running_loop().time()
            - start_time
        )

        lines.append(
            f"• HTTPS: ✅ "
            f"HTTP {response.status_code} "
            f"({elapsed:.2f}s)"
        )

        lines.append(
            f"• Финальный URL: "
            f"<code>{response.url}</code>"
        )

    except Exception as exc:

        elapsed = (
            asyncio.get_running_loop().time()
            - start_time
        )

        logger.exception(
            "HTTPS TEST FAILED"
        )

        lines.append(
            f"• HTTPS: ❌ "
            f"{type(exc).__name__}: "
            f"{exc!r} "
            f"({elapsed:.2f}s)"
        )

    lines.append("")
    lines.append(
        "Теперь отправь мне <b>весь результат</b> этого сообщения."
    )

    try:

        await status.edit_text(
            "\n".join(lines)
        )

    except Exception:

        logger.exception(
            "Failed to send API test result"
        )


# =========================================================
# /UPDATE
# =========================================================

@router.message(
    Command("update")
)
async def force_update(
    message: Message,
) -> None:

    if not is_admin(
        message.from_user.id
    ):
        return

    status = await message.answer(
        "⏳ <b>Обновляю расписание...</b>\n\n"
        "Подготовка..."
    )

    result = await run_update(
        status
    )

    try:

        await status.edit_text(
            result
        )

    except Exception:

        logger.exception(
            "Не удалось изменить итоговое сообщение"
        )


# =========================================================
# UPDATE ONE GROUP
# =========================================================

async def _update_one_group(
    course: str,
    group_name: str,
    group_id: int,
    semaphore: asyncio.Semaphore,
    counters: dict,
    total: int,
    status_message: Message | None,
) -> None:

    async with semaphore:

        logger.info(
            "START group=%s id=%s",
            group_name,
            group_id,
        )

        try:

            lessons = await asyncio.wait_for(
                site_parser.fetch_schedule(
                    group_id=group_id
                ),
                timeout=PER_GROUP_TIMEOUT,
            )

            await db.save_schedule_for_group(
                group_id,
                lessons,
            )

            if lessons:

                counters["updated"] += 1

                logger.info(
                    "SUCCESS %s (%s lessons)",
                    group_name,
                    len(lessons),
                )

            else:

                counters["empty"] += 1

                logger.warning(
                    "EMPTY response for %s (%s)",
                    group_name,
                    group_id,
                )

        except ScheduleFormatError as exc:

            counters["format_failed"] += 1

            logger.error(
                "FORMAT ERROR for %s (%s): %s",
                group_name,
                group_id,
                exc,
            )

        except ScheduleAPIError as exc:

            counters["api_failed"] += 1

            logger.error(
                "API ERROR for %s (%s): %s",
                group_name,
                group_id,
                exc,
            )

        except asyncio.TimeoutError:

            counters["errors"] += 1

            logger.error(
                "TIMEOUT %ss for %s (%s)",
                PER_GROUP_TIMEOUT,
                group_name,
                group_id,
            )

        except Exception as exc:

            counters["errors"] += 1

            logger.exception(
                "ERROR for %s (%s): %s",
                group_name,
                group_id,
                exc,
            )

        counters["done"] += 1

        done = counters["done"]

        if status_message and (
            done == 1
            or done % 5 == 0
            or done == total
        ):

            try:

                await status_message.edit_text(
                    "⏳ <b>Обновляю расписание...</b>\n\n"
                    f"Обработано: {done}/{total}\n"
                    f"✅ С парами: {counters['updated']}\n"
                    f"⚠️ Пусто: {counters['empty']}\n"
                    f"📄 Формат: {counters['format_failed']}\n"
                    f"🌐 API ошибок: {counters['api_failed']}\n"
                    f"❌ Прочих ошибок: {counters['errors']}"
                )

            except Exception:

                logger.exception(
                    "Failed to update Telegram progress message"
                )


# =========================================================
# DUPLICATE GROUP IDS
# =========================================================

def find_duplicate_group_ids() -> list[tuple[int, list[str]]]:

    by_id: dict[int, list[str]] = {}

    for course, group_name, group_id in groups_data.GROUPS:

        label = f"{course} / {group_name}"

        by_id.setdefault(
            group_id,
            [],
        ).append(label)

    duplicates = []

    for group_id, names in by_id.items():

        if len(names) > 1:

            duplicates.append(
                (
                    group_id,
                    names,
                )
            )

    return duplicates


# =========================================================
# RUN UPDATE
# =========================================================

async def run_update(
    status_message: Message | None = None,
) -> str:

    logger.info(
        "========================================"
    )

    logger.info(
        "UPDATE STARTED"
    )

    logger.info(
        "========================================"
    )

    if not groups_data.GROUPS:

        logger.error(
            "groups_data.GROUPS is empty"
        )

        return "❌ Список групп пуст."

    total = len(
        groups_data.GROUPS
    )

    logger.info(
        "Groups loaded: %s",
        total,
    )

    duplicates = find_duplicate_group_ids()

    if duplicates:

        logger.warning(
            "FOUND DUPLICATE GROUP IDS:"
        )

        for group_id, names in duplicates:

            logger.warning(
                "ID %s -> %s",
                group_id,
                ", ".join(names),
            )

    try:

        await db.save_structure(
            groups_data.GROUPS
        )

        logger.info(
            "Group structure saved"
        )

    except Exception:

        logger.exception(
            "Failed to save group structure"
        )

        return (
            "❌ Не удалось сохранить "
            "список групп."
        )

    counters = {
        "updated": 0,
        "empty": 0,
        "errors": 0,
        "done": 0,
        "api_failed": 0,
        "format_failed": 0,
    }

    semaphore = asyncio.Semaphore(
        UPDATE_CONCURRENCY
    )

    tasks = [
        _update_one_group(
            course=course,
            group_name=group_name,
            group_id=group_id,
            semaphore=semaphore,
            counters=counters,
            total=total,
            status_message=status_message,
        )
        for course, group_name, group_id
        in groups_data.GROUPS
    ]

    await asyncio.gather(
        *tasks
    )

    logger.info(
        "========================================"
    )

    logger.info(
        "UPDATE FINISHED"
    )

    logger.info(
        "TOTAL=%s SUCCESS=%s EMPTY=%s "
        "API_FAILED=%s FORMAT_FAILED=%s ERRORS=%s",
        total,
        counters["updated"],
        counters["empty"],
        counters["api_failed"],
        counters["format_failed"],
        counters["errors"],
    )

    logger.info(
        "========================================"
    )

    result = (
        "✅ <b>Обновление завершено!</b>\n\n"
        f"Всего групп: {total}\n"
        f"✅ С расписанием: {counters['updated']}\n"
        f"⚠️ Пустых: {counters['empty']}\n"
        f"🌐 Ошибок API: {counters['api_failed']}\n"
        f"📄 Ошибок формата: {counters['format_failed']}\n"
        f"❌ Прочих ошибок: {counters['errors']}"
    )

    if duplicates:

        result += (
            "\n\n⚠️ <b>Найдены дубликаты group_id:</b>\n"
        )

        for group_id, names in duplicates:

            result += (
                f"\nID <code>{group_id}</code>:\n"
                + "\n".join(
                    f"• {name}"
                    for name in names
                )
                + "\n"
            )

        result += (
            "\nНужно проверить ID этих групп "
            "в groups_data.py."
        )

    return result


# =========================================================
# SCHEDULED UPDATE
# =========================================================

async def scheduled_update(
    bot: Bot,
) -> None:

    logger.info(
        "Автоматическое обновление запущено"
    )

    result = await run_update()

    try:

        await bot.send_message(
            ADMIN_ID,
            f"[Автообновление]\n\n{result}",
        )

    except Exception:

        logger.exception(
            "Не удалось отправить результат "
            "автообновления"
        )


# =========================================================
# STATS
# =========================================================

@router.message(
    Command("stats")
)
async def stats(
    message: Message,
) -> None:

    if not is_admin(
        message.from_user.id
    ):
        return

    count = await db.count_users()

    await message.answer(
        f"Всего пользователей бота: "
        f"<b>{count}</b>"
    )


# =========================================================
# HEALTH CHECK
# =========================================================

async def handle_health(
    request: web.Request,
) -> web.Response:

    return web.Response(
        text="ok"
    )


async def run_health_server() -> None:

    app = web.Application()

    app.router.add_get(
        "/",
        handle_health,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    logger.info(
        "Health-check server started on port %s",
        port,
    )


# =========================================================
# MAIN
# =========================================================

async def main() -> None:

    logger.info(
        "========================================"
    )

    logger.info(
        "Starting Gubkin Bot"
    )

    logger.info(
        "========================================"
    )

    await db.init_db()

    logger.info(
        "Database initialized"
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode="HTML"
        ),
    )

    dp = Dispatcher(
        storage=MemoryStorage()
    )

    dp.include_router(
        router
    )

    scheduler = AsyncIOScheduler(
        timezone="Asia/Tashkent"
    )

    scheduler.add_job(
        scheduled_update,
        "cron",
        hour=3,
        minute=0,
        args=[bot],
        id="daily_schedule_update",
        replace_existing=True,
    )

    scheduler.start()

    logger.info(
        "Scheduler started: "
        "daily at 03:00 Asia/Tashkent"
    )

    await run_health_server()

    logger.info(
        "Бот запущен"
    )

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    asyncio.run(main())
