import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
import keyboards as kb
import parser as site_parser
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
    choosing_faculty = State()
    choosing_group = State()


def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID


def format_day(group_name: str, weekday: int, lessons: list[dict]) -> str:
    header = f"<b>{WEEKDAY_NAMES_RU[weekday]}</b> — группа {group_name}\n\n"
    if not lessons:
        return header + "Пар нет 🎉"
    lines = []
    for lesson in lessons:
        lines.append(
            f"⏰ <b>{lesson['time_slot']}</b>\n"
            f"📘 {lesson['subject']}\n"
            f"🚪 {lesson['room']}    👤 {lesson['teacher']}"
        )
    return header + "\n\n".join(lines)


async def send_day_schedule(message: Message, group_name: str, weekday: int) -> None:
    lessons = await db.get_schedule_for_day(group_name, weekday)
    await message.answer(format_day(group_name, weekday, lessons))


# ---------- /start и регистрация ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user = await db.get_user(message.from_user.id)
    if user and user.get("group_name"):
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
        "Привет! Давай выберем твою группу.\n\nШаг 1 из 3 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )


@router.callback_query(Registration.choosing_course, F.data.startswith("course:"))
async def choose_course(callback: CallbackQuery, state: FSMContext) -> None:
    course = callback.data.split(":", 1)[1]
    await state.update_data(course=course)

    faculties = await db.get_faculties(course)
    await state.set_state(Registration.choosing_faculty)
    await callback.message.edit_text(
        f"Курс: {course}\n\nШаг 2 из 3 — выбери факультет / направление:",
        reply_markup=kb.faculties_keyboard(faculties),
    )
    await callback.answer()


@router.callback_query(Registration.choosing_faculty, F.data == "back_to_course")
async def back_to_course(callback: CallbackQuery, state: FSMContext) -> None:
    courses = await db.get_courses()
    await state.set_state(Registration.choosing_course)
    await callback.message.edit_text(
        "Шаг 1 из 3 — выбери курс:", reply_markup=kb.courses_keyboard(courses)
    )
    await callback.answer()


@router.callback_query(Registration.choosing_faculty, F.data.startswith("faculty:"))
async def choose_faculty(callback: CallbackQuery, state: FSMContext) -> None:
    faculty = callback.data.split(":", 1)[1]
    data = await state.get_data()
    course = data["course"]
    await state.update_data(faculty=faculty)

    groups = await db.get_groups(course, faculty)
    await state.set_state(Registration.choosing_group)
    await callback.message.edit_text(
        f"Курс: {course}\nФакультет: {faculty}\n\nШаг 3 из 3 — выбери группу:",
        reply_markup=kb.groups_keyboard(groups),
    )
    await callback.answer()


@router.callback_query(Registration.choosing_group, F.data == "back_to_faculty")
async def back_to_faculty(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    course = data["course"]
    faculties = await db.get_faculties(course)
    await state.set_state(Registration.choosing_faculty)
    await callback.message.edit_text(
        f"Курс: {course}\n\nШаг 2 из 3 — выбери факультет / направление:",
        reply_markup=kb.faculties_keyboard(faculties),
    )
    await callback.answer()


@router.callback_query(Registration.choosing_group, F.data.startswith("group:"))
async def choose_group(callback: CallbackQuery, state: FSMContext) -> None:
    group_name = callback.data.split(":", 1)[1]
    data = await state.get_data()

    await db.save_user(
        callback.from_user.id, data["course"], data["faculty"], group_name
    )
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
        "Шаг 1 из 3 — выбери курс:", reply_markup=kb.courses_keyboard(courses)
    )


@router.message(F.text == "📅 На сегодня")
async def today_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_name"):
        await message.answer("Сначала выбери группу командой /start")
        return
    weekday = datetime.now().weekday()
    await send_day_schedule(message, user["group_name"], weekday)


@router.message(F.text == "📆 На завтра")
async def tomorrow_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_name"):
        await message.answer("Сначала выбери группу командой /start")
        return
    weekday = (datetime.now() + timedelta(days=1)).weekday()
    await send_day_schedule(message, user["group_name"], weekday)


@router.message(F.text == "🗓 На неделю")
async def week_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_name"):
        await message.answer("Сначала выбери группу командой /start")
        return
    group_name = user["group_name"]
    week = await db.get_schedule_for_week(group_name)
    for weekday in range(7):
        await message.answer(format_day(group_name, weekday, week[weekday]))
        await asyncio.sleep(0.1)  # чтобы Telegram не считал это спамом


@router.message(F.text == "🔗 Ссылка на сайт")
async def site_link(message: Message) -> None:
    from config import BASE_URL

    user = await db.get_user(message.from_user.id)
    group_name = user["group_name"] if user else ""
    await message.answer(
        f"Сайт расписания: {BASE_URL}\nТвоя группа: <b>{group_name}</b>"
    )


# ---------- Админ-команды ----------

@router.message(Command("setcookie"))
async def set_cookie(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /setcookie <значение_PHPSESSID>")
        return
    cookie = parts[1].strip()

    valid = await site_parser.check_cookie_valid(cookie)
    await db.set_setting("phpsessid", cookie)
    if valid:
        await message.answer("Кука сохранена и выглядит рабочей ✅")
    else:
        await message.answer(
            "Кука сохранена, но проверка не прошла ⚠️ (сайт вернул капчу/страницу входа). "
            "Возможно, нужно донастроить parser.py под реальный HTML сайта."
        )


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
    cookie = await db.get_setting("phpsessid")
    if not cookie:
        return "Кука PHPSESSID ещё не задана. Используйте /setcookie <значение>."

    try:
        structure_rows = await site_parser.fetch_structure(cookie)
        if not structure_rows:
            return "Не удалось получить список групп — проверьте куку или структуру сайта в parser.py."
        await db.save_structure(structure_rows)

        groups = sorted({row[2] for row in structure_rows})
        updated = 0
        for group_name in groups:
            lessons = await site_parser.fetch_schedule(cookie, group_name)
            await db.save_schedule_for_group(group_name, lessons)
            updated += 1
            await asyncio.sleep(0.3)  # не долбим сайт запросами слишком быстро

        return f"Готово! Обновлено групп: {updated}."
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при обновлении расписания")
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
    """
    Render Web Service ожидает открытый порт, иначе считает сервис нерабочим
    (это отдельно от Telegram-бота, который работает через polling, а не через
    HTTP). Этот сервер ничего не делает, кроме "да, я живой" на любой запрос.
    Порт Render передаёт через переменную окружения PORT — не задавайте её
    вручную, Render делает это сам.
    """
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health-check сервер запущен на порту %s", port)


async def main() -> None:
    await db.init_db()

    bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
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
