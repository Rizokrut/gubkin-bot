import asyncio
import logging
import os
from datetime import datetime, timedelta

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


# =========================================================
# REGISTRATION STATES
# =========================================================

class Registration(StatesGroup):
    choosing_course = State()
    choosing_group = State()


# =========================================================
# ADMIN
# =========================================================

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

        lines.append(
            f"⏰ <b>{lesson.get('time_slot', '')}</b>\n"
            f"📘 {lesson.get('subject', '')}{type_part}\n"
            f"🚪 {lesson.get('room', '')}    "
            f"👤 {lesson.get('teacher', '')}"
        )

    return header + "\n\n".join(lines)


# =========================================================
# SEND ONE DAY
# =========================================================

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
            "Попросите администратора выполнить "
            "команду /update."
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
    F.data.startswith("course:")
)
async def choose_course(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:

    course = callback.data.split(
        ":",
        1
    )[1]

    await state.update_data(
        course=course
    )

    groups = await db.get_groups(
        course
    )

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
    F.data == "back_to_course"
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
    F.data.startswith("group:")
)
async def choose_group(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:

    group_id = int(
        callback.data.split(
            ":",
            1
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

    groups = await db.get_groups(
        course
    )

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

    weekday = datetime.now().weekday()

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

    weekday = (
        datetime.now()
        + timedelta(days=1)
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
# SITE
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
# /SETCOOKIE
# =========================================================

@router.message(
    Command("setcookie")
)
async def set_cookie(
    message: Message,
) -> None:

    if not is_admin(
        message.from_user.id
    ):
        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        await message.answer(
            "Использование:\n\n"
            "/setcookie PHPSESSID=твой_cookie"
        )

        return

    cookie = parts[1].strip()

    if cookie.startswith(
        "PHPSESSID="
    ):
        cookie = cookie.split(
            "=",
            1
        )[1].strip()

    await db.set_setting(
        "phpsessid",
        cookie,
    )

    logger.info(
        "PHPSESSID saved"
    )

    await message.answer(
        "Кука сохранена ✅\n\n"
        "Теперь запускай /update"
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

    # -----------------------------------------------------
    # GROUPS
    # -----------------------------------------------------

    if not groups_data.GROUPS:

        logger.error(
            "groups_data.GROUPS is empty"
        )

        return (
            "❌ Список групп пуст."
        )

    total = len(
        groups_data.GROUPS
    )

    logger.info(
        "Groups loaded: %s",
        total,
    )

    # -----------------------------------------------------
    # COOKIE
    # -----------------------------------------------------

    cookie = await db.get_setting(
        "phpsessid"
    )

    if not cookie:

        logger.error(
            "PHPSESSID is not set"
        )

        return (
            "❌ PHPSESSID не установлен.\n\n"
            "Используй:\n"
            "/setcookie PHPSESSID=твой_cookie"
        )

    logger.info(
        "PHPSESSID found"
    )

    # -----------------------------------------------------
    # SAVE GROUP STRUCTURE
    # -----------------------------------------------------

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
            "❌ Не удалось сохранить список групп."
        )

    # -----------------------------------------------------
    # COUNTERS
    # -----------------------------------------------------

    updated = 0
    empty = 0
    errors = 0

    # -----------------------------------------------------
    # LOOP
    # -----------------------------------------------------

    for index, (
        course,
        group_name,
        group_id,
    ) in enumerate(
        groups_data.GROUPS,
        start=1,
    ):

        logger.info(
            "[%s/%s] START group=%s id=%s",
            index,
            total,
            group_name,
            group_id,
        )

        try:

            # Максимум 25 секунд на одну группу.
            lessons = await asyncio.wait_for(
                site_parser.fetch_schedule(
                    cookie=cookie,
                    group_id=group_id,
                ),
                timeout=25,
            )

            if lessons:

                logger.info(
                    "[%s/%s] RECEIVED %s lessons for %s",
                    index,
                    total,
                    len(lessons),
                    group_name,
                )

                await db.save_schedule_for_group(
                    group_id,
                    lessons,
                )

                updated += 1

                logger.info(
                    "[%s/%s] SUCCESS %s",
                    index,
                    total,
                    group_name,
                )

            else:

                empty += 1

                logger.warning(
                    "[%s/%s] EMPTY response for %s",
                    index,
                    total,
                    group_name,
                )

        except asyncio.TimeoutError:

            errors += 1

            logger.error(
                "[%s/%s] TIMEOUT 25 sec for %s (%s)",
                index,
                total,
                group_name,
                group_id,
            )

        except Exception as exc:

            errors += 1

            logger.exception(
                "[%s/%s] ERROR for %s (%s): %s",
                index,
                total,
                group_name,
                group_id,
                exc,
            )

        # -------------------------------------------------
        # UPDATE TELEGRAM STATUS
        # -------------------------------------------------

        if (
            status_message
            and (
                index == 1
                or index % 5 == 0
                or index == total
            )
        ):

            try:

                await status_message.edit_text(
                    "⏳ <b>Обновляю расписание...</b>\n\n"
                    f"Обработано: {index}/{total}\n"
                    f"✅ Успешно: {updated}\n"
                    f"⚠️ Пусто: {empty}\n"
                    f"❌ Ошибок: {errors}"
                )

            except Exception:

                logger.exception(
                    "Failed to update Telegram progress message"
                )

        # Маленькая пауза между запросами
        await asyncio.sleep(0.2)

    # -----------------------------------------------------
    # RESULT
    # -----------------------------------------------------

    logger.info(
        "========================================"
    )
    logger.info(
        "UPDATE FINISHED"
    )
    logger.info(
        "TOTAL=%s SUCCESS=%s EMPTY=%s ERRORS=%s",
        total,
        updated,
        empty,
        errors,
    )
    logger.info(
        "========================================"
    )

    result = (
        "✅ <b>Обновление завершено!</b>\n\n"
        f"Всего групп: {total}\n"
        f"✅ Успешно: {updated}\n"
        f"⚠️ Пустых: {empty}\n"
        f"❌ Ошибок: {errors}"
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
            "Не удалось отправить результат автообновления"
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

    # -----------------------------------------------------
    # DATABASE
    # -----------------------------------------------------

    await db.init_db()

    logger.info(
        "Database initialized"
    )

    # -----------------------------------------------------
    # BOT
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # SCHEDULER
    # -----------------------------------------------------

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
        "Scheduler started: daily at 03:00 Asia/Tashkent"
    )

    # -----------------------------------------------------
    # RENDER HEALTH SERVER
    # -----------------------------------------------------

    await run_health_server()

    # -----------------------------------------------------
    # POLLING
    # -----------------------------------------------------

    logger.info(
        "Bot is starting polling..."
    )

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    try:

        await dp.start_polling(
            bot
        )

    finally:

        scheduler.shutdown()

        await bot.session.close()

        logger.info(
            "Bot stopped"
        )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )
