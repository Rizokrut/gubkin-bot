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
import parser as site_parser
import groups_data
from config import BOT_TOKEN, ADMIN_ID

logging.basicConfig(level=logging.INFO)
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
        type_part = f" ({lesson['lesson_type']})" if lesson.get("lesson_type") else ""
        lines.append(
            f"⏰ <b>{lesson['time_slot']}</b>\n"
            f"📘 {lesson['subject']}{type_part}\n"
            f"🚪 {lesson['room']}    👤 {lesson['teacher']}"
        )
    return header + "\n\n".join(lines)


async def send_day_schedule(message: Message, group_name: str, group_id: int, weekday: int) -> None:
    lessons = await db.get_schedule_for_day(group_id, weekday)
    await message.answer(format_day(group_name, weekday, lessons))


# ---------- /start и регистрация ----------

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
            "База расписания пока пуста. Попросите администратора бота выполнить "
            "команду /update, затем нажмите /start ещё раз."
        )
        return

    await state.set_state(Registration.choosing_course)
    await message.answer(
        "Привет! Давай выберем твою группу.\n\nШаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )


@router.callback_query(Registration.choosing_course, F.data.startswith("course:"))
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


@router.callback_query(Registration.choosing_group, F.data == "back_to_course")
async def back_to_course(callback: CallbackQuery, state: FSMContext) -> None:
    courses = await db.get_courses()
    await state.set_state(Registration.choosing_course)
    await callback.message.edit_text(
        "Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses)
    )
    await callback.answer()


@router.callback_query(Registration.choosing_group, F.data.startswith("group:"))
async def choose_group(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    course = data["course"]

    groups = await db.get_groups(course)
    group_name = next((name for name, gid in groups if gid == group_id), str(group_id))

    await db.save_user(callback.from_user.id, course, group_name, group_id)
    await state.clear()

    await callback.message.edit_text(f"Готово! Твоя группа: <b>{group_name}</b>")
    await callback.message.answer(
        "Открываю главное меню 👇", reply_markup=kb.main_menu_keyboard()
    )
    await callback.answer()


# ---------- Главное меню ----------

@router.message(F.text == "⚙️ Сменить группу")
async def change_group(message: Message, state: FSMContext) -> None:
    courses = await db.get_courses()
    if not courses:
        await message.answer("База расписания пока пуста, попробуйте позже.")
        return
    await state.set_state(Registration.choosing_course)
    await message.answer(
        "Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses)
    )


@router.message(F.text == "📅 На сегодня")
async def today_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    weekday = datetime.now().weekday()
    await send_day_schedule(message, user["group_name"], user["group_id"], weekday)


@router.message(F.text == "📆 На завтра")
async def tomorrow_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    weekday = (datetime.now() + timedelta(days=1)).weekday()
    await send_day_schedule(message, user["group_name"], user["group_id"], weekday)


@router.message(F.text == "🗓 На неделю")
async def week_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    group_name = user["group_name"]
    week = await db.get_schedule_for_week(user["group_id"])
    for weekday in range(7):
        await message.answer(format_day(group_name, weekday, week[weekday]))
        await asyncio.sleep(0.1)


@router.message(F.text == "🔗 Ссылка на сайт")
async def site_link(message: Message) -> None:
    from config import BASE_URL

    user = await db.get_user(message.from_user.id)
    group_name = user["group_name"] if user else ""
    await message.answer(f"Сайт расписания: {BASE_URL}\nТвоя группа: <b>{group_name}</b>")


# ---------- Админ-команды ----------

@router.message(Command("setcookie"))
async def set_cookie(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /setcookie <значение_PHPSESSID>")
        return
    
    # Извлечение чистого значения куки, если передали всей строкой "PHPSESSID=..."
    cookie = parts[1].strip()
    if "PHPSESSID=" in cookie:
        cookie = cookie.split("PHPSESSID=")[-1].split(";")[0].strip()

    await db.set_setting("phpsessid", cookie)
    await message.answer("Кука принудительно сохранена ✅!\nТеперь отправьте команду /update")


@router.message(Command("update"))
async def force_update(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer("Запускаю обновление расписания...")
    result = await run_update()
    await message.answer(result)


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    count = await db.count_users()
    await message.answer(f"Всего пользователей бота: <b>{count}</b>")


# ---------- Фоновое обновление расписания ----------

async def run_update() -> str:
    if not groups_data.GROUPS:
        return (
            "Список групп (groups_data.py) пока пуст. Заполните его данными "
            "групп (курс, название, group_id) и передеплойте бота."
        )

    cookie = await db.get_setting("phpsessid")
    if not cookie:
        return "Кука PHPSESSID ещё не задана. Используйте /setcookie <значение>."

    try:
        await db.save_structure(groups_data.GROUPS)

        updated = 0
        errors = 0
        for course, group_name, group_id in groups_data.GROUPS:
            try:
                lessons = await site_parser.fetch_schedule(cookie, group_id)
                if lessons:
                    await db.save_schedule_for_group(group_id, lessons)
                    updated += 1
                else:
                    errors += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("Не удалось обновить группу %s (%s): %s", group_name, group_id, exc)
                errors += 1
            await asyncio.sleep(0.3)

        result = f"Готово! Успешно загружено групп: {updated}."
        if errors:
            result += f" Не удалось загрузить / пустых: {errors}."
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при процессе обновления расписания")
        return f"Ошибка при обновлении: {exc}"


async def scheduled_update(bot: Bot) -> None:
    result = await run_update()
    try:
        await bot.send_message(ADMIN_ID, f"[Автообновление] {result}")
    except Exception:  # noqa: BLE001
        pass


# ---------- Точка входа ----------

async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def run_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_handle := handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health-check сервер запущен на порту %s", port)


async def main() -> None:
    await db.init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
    scheduler.add_job(scheduled_update, "cron", hour=3, minute=0, args=[bot])
    scheduler.start()

    await run_health_server()

    logger.info("Бот запущен")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
