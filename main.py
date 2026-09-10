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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()

WEEKDAY_NAMES_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]


class Registration(StatesGroup):
    choosing_course = State()
    choosing_group = State()


def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID


def format_day(group_name: str, weekday: int, lessons: list[dict]) -> str:
    header = f"<b>{WEEKDAY_NAMES_RU[weekday]}</b> — группа {group_name}\n\n"
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
            f"⏰ <b>{lesson['time_slot']}</b>\n"
            f"📘 {lesson['subject']}{type_part}\n"
            f"🚪 {lesson['room']}    👤 {lesson['teacher']}"
        )
    return header + "\n\n".join(lines)


async def send_day_schedule(
    message: Message,
    group_name: str,
    group_id: int,
    weekday: int,
) -> None:
    lessons = await db.get_schedule_for_day(group_id, weekday)
    await message.answer(format_day(group_name, weekday, lessons))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user = await db.get_user(message.from_user.id)
    if user and user.get("group_id"):
        await message.answer(
            f"С возвращением! Ваша группа: <b>{user['group_name']}</b>",
            reply_markup=kb.main_menu_keyboard(),
        )
        return

    courses = await db.get_courses()
    if not courses:
        await message.answer(
            "База расписания пока пуста. Попросите администратора бота "
            "выполнить команду /update, затем нажмите /start ещё раз."
        )
        return

    await state.set_state(Registration.choosing_course)
    await message.answer(
        "Привет! Давай выберем твою группу.\n\n"
        "Шаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )


@router.callback_query(
    Registration.choosing_course,
    F.data.startswith("course:"),
)
async def choose_course(callback: CallbackQuery, state: FSMContext) -> None:
    course = callback.data.split(":", 1)[1]
    await state.update_data(course=course)
    groups = await db.get_groups(course)

    await state.set_state(Registration.choosing_group)
    await callback.message.edit_text(
        f"Курс: {course}\n\nШаг 2 из 2 — выбери группу:",
        reply_markup=kb.groups_keyboard(groups),
    )
    await callback.answer()


@router.callback_query(
    Registration.choosing_group,
    F.data == "back_to_course",
)
async def back_to_course(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    courses = await db.get_courses()
    await state.set_state(Registration.choosing_course)
    await callback.message.edit_text(
        "Шаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )
    await callback.answer()


@router.callback_query(
    Registration.choosing_group,
    F.data.startswith("group:"),
)
async def choose_group(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    course = data["course"]

    groups = await db.get_groups(course)
    group_name = next(
        (name for name, gid in groups if gid == group_id),
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
        f"Готово! Твоя группа: <b>{group_name}</b>"
    )
    await callback.message.answer(
        "Открываю главное меню 👇",
        reply_markup=kb.main_menu_keyboard(),
    )
    await callback.answer()


@router.message(F.text == "⚙️ Сменить группу")
async def change_group(message: Message, state: FSMContext) -> None:
    courses = await db.get_courses()
    if not courses:
        await message.answer("База расписания пока пуста, попробуйте позже.")
        return

    await state.set_state(Registration.choosing_course)
    await message.answer(
        "Шаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )


@router.message(F.text == "📅 На сегодня")
async def today_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return

    weekday = datetime.now().weekday()
    await send_day_schedule(
        message,
        user["group_name"],
        user["group_id"],
        weekday,
    )


@router.message(F.text == "📆 На завтра")
async def tomorrow_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return

    weekday = (datetime.now() + timedelta(days=1)).weekday()
    await send_day_schedule(
        message,
        user["group_name"],
        user["group_id"],
        weekday,
    )


@router.message(F.text == "🗓 На неделю")
async def week_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return

    group_name = user["group_name"]
    week = await db.get_schedule_for_week(user["group_id"])

    for weekday in range(7):
        await message.answer(
            format_day(group_name, weekday, week[weekday])
        )
        await asyncio.sleep(0.1)


@router.message(F.text == "🔗 Ссылка на сайт")
async def site_link(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    group_name = user["group_name"] if user else ""
    await message.answer(
        f"Сайт расписания: {BASE_URL}\n"
        f"Твоя группа: <b>{group_name}</b>"
    )


@router.message(Command("setcookie"))
async def set_cookie(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "/setcookie <PHPSESSID или полная Cookie-строка>"
        )
        return

    cookie = parts[1].strip()
    await db.set_setting("phpsessid", cookie)

    await message.answer(
        "Cookie сохранена ✅\n"
        "Теперь выполните /update."
    )


@router.message(Command("update"))
async def force_update(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    status_message = await message.answer(
        "⏳ Обновляю расписание всех групп..."
    )

    result = await run_update()

    await status_message.edit_text(result)


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    count = await db.count_users()
    await message.answer(
        f"Всего пользователей бота: <b>{count}</b>"
    )


async def run_update() -> str:
    if not groups_data.GROUPS:
        return "Список групп пуст."

    cookie = await db.get_setting("phpsessid")
    if not cookie:
        return (
            "Cookie ещё не задана.\n"
            "Используйте /setcookie <cookie>"
        )

    try:
        await db.save_structure(groups_data.GROUPS)

        updated = 0
        errors = 0
        total = len(groups_data.GROUPS)

        for index, (course, group_name, group_id) in enumerate(
            groups_data.GROUPS,
            start=1,
        ):
            try:
                logger.info(
                    "[%s/%s] Запрашиваю группу %s (%s), ID=%s",
                    index,
                    total,
                    group_name,
                    course,
                    group_id,
                )

                lessons = await site_parser.fetch_schedule(
                    cookie=cookie,
                    group_id=group_id,
                )

                if not lessons:
                    logger.warning(
                        "[%s/%s] Группа %s: расписание пустое",
                        index,
                        total,
                        group_name,
                    )
                    errors += 1
                else:
                    await db.save_schedule_for_group(
                        group_id,
                        lessons,
                    )
                    updated += 1

                    logger.info(
                        "[%s/%s] Группа %s: сохранено %s пар",
                        index,
                        total,
                        group_name,
                        len(lessons),
                    )

            except Exception:
                logger.exception(
                    "Ошибка обновления группы %s (%s), ID=%s",
                    group_name,
                    course,
                    group_id,
                )
                errors += 1

            await asyncio.sleep(0.3)

        return (
            f"✅ Готово!\n\n"
            f"Загружено групп: {updated} из {total}\n"
            f"Ошибок/пустых: {errors}"
        )

    except Exception as exc:
        logger.exception("Критическая ошибка при обновлении")
        return f"❌ Ошибка при обновлении: {exc}"


async def scheduled_update(bot: Bot) -> None:
    result = await run_update()
    try:
        await bot.send_message(
            ADMIN_ID,
            f"[Автообновление]\n{result}",
        )
    except Exception:
        logger.exception("Не удалось отправить результат автообновления")


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def run_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(
        "Health-check сервер запущен на порту %s",
        port,
    )


async def main() -> None:
    await db.init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
    scheduler.add_job(
        scheduled_update,
        "cron",
        hour=3,
        minute=0,
        args=[bot],
    )
    scheduler.start()

    await run_health_server()

    logger.info("Бот запущен")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
