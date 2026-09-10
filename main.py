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
from parser import ScheduleAuthError, ScheduleFormatError
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
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]

# Сколько групп обновляем ОДНОВРЕМЕННО во время /update. Раньше группы шли
# строго одна за другой (по одной, до 25 сек. ожидания на каждую) — если
# кука была нерабочей, 41 группа могла растянуться на ~17 минут и выглядеть
# как зависание. Теперь несколько групп опрашиваются параллельно.
UPDATE_CONCURRENCY = 6

# Таймаут на одну группу (несколько выше внутреннего таймаута parser.py,
# чтобы он успел сработать первым и корректно залогировать причину).
PER_GROUP_TIMEOUT = 12

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
def format_day(group_name: str, weekday: int, lessons: list[dict]) -> str:
    header = f"<b>{WEEKDAY_NAMES_RU[weekday]}</b> — группа {group_name}\n\n"
    if not lessons:
        return header + "Пар нет 🎉"
    lines = []
    for lesson in lessons:
        type_part = f" ({lesson['lesson_type']})" if lesson.get("lesson_type") else ""
        lines.append(
            f"⏰ <b>{lesson.get('time_slot', '')}</b>\n"
            f"📘 {lesson.get('subject', '')}{type_part}\n"
            f"🚪 {lesson.get('room', '')}    👤 {lesson.get('teacher', '')}"
        )
    return header + "\n\n".join(lines)


async def send_day_schedule(message: Message, group_name: str, group_id: int, weekday: int) -> None:
    lessons = await db.get_schedule_for_day(group_id, weekday)
    await message.answer(format_day(group_name, weekday, lessons))


# =========================================================
# /START
# =========================================================
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
            "База расписания пока пуста.\n\nПопросите администратора выполнить команду /update."
        )
        return

    await state.set_state(Registration.choosing_course)
    await message.answer(
        "Привет! Давай выберем твою группу.\n\nШаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )


# =========================================================
# CHOOSE COURSE
# =========================================================
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


# =========================================================
# BACK TO COURSE
# =========================================================
@router.callback_query(Registration.choosing_group, F.data == "back_to_course")
async def back_to_course(callback: CallbackQuery, state: FSMContext) -> None:
    courses = await db.get_courses()
    await state.set_state(Registration.choosing_course)
    await callback.message.edit_text(
        "Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses)
    )
    await callback.answer()


# =========================================================
# CHOOSE GROUP
# =========================================================
@router.callback_query(Registration.choosing_group, F.data.startswith("group:"))
async def choose_group(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    course = data.get("course")
    if not course:
        await callback.answer("Сначала выбери курс", show_alert=True)
        return

    groups = await db.get_groups(course)
    group_name = next((name for name, gid in groups if gid == group_id), str(group_id))

    await db.save_user(callback.from_user.id, course, group_name, group_id)
    await state.clear()

    await callback.message.edit_text(f"Готово! Твоя группа: <b>{group_name}</b>")
    await callback.message.answer("Открываю главное меню 👇", reply_markup=kb.main_menu_keyboard())
    await callback.answer()


# =========================================================
# CHANGE GROUP
# =========================================================
@router.message(F.text == "⚙️ Сменить группу")
async def change_group(message: Message, state: FSMContext) -> None:
    courses = await db.get_courses()
    if not courses:
        await message.answer("База расписания пока пуста.")
        return
    await state.set_state(Registration.choosing_course)
    await message.answer("Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses))


# =========================================================
# TODAY / TOMORROW / WEEK
# =========================================================
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
    user = await db.get_user(message.from_user.id)
    group_name = user["group_name"] if user else ""
    await message.answer(f"Сайт расписания: {BASE_URL}\nТвоя группа: <b>{group_name}</b>")


# =========================================================
# /SETCOOKIE
# =========================================================
@router.message(Command("setcookie"))
async def set_cookie(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование:\n\n/setcookie PHPSESSID=твой_cookie")
        return
    cookie = parts[1].strip()
    if cookie.startswith("PHPSESSID="):
        cookie = cookie.split("=", 1)[1].strip()

    await db.set_setting("phpsessid", cookie)
    logger.info("PHPSESSID saved")
    await message.answer("Кука сохранена ✅\n\nТеперь запускай /update")


# =========================================================
# /UPDATE
# =========================================================
@router.message(Command("update"))
async def force_update(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    status = await message.answer("⏳ <b>Обновляю расписание...</b>\n\nПодготовка...")
    result = await run_update(status)
    try:
        await status.edit_text(result)
    except Exception:
        logger.exception("Не удалось изменить итоговое сообщение")


# =========================================================
# RUN UPDATE (параллельно, с ограничением одновременных запросов)
# =========================================================
async def _update_one_group(
    course: str,
    group_name: str,
    group_id: int,
    cookie: str,
    semaphore: asyncio.Semaphore,
    counters: dict,
    total: int,
    status_message: Message | None,
) -> None:
    async with semaphore:
        logger.info("START group=%s id=%s", group_name, group_id)
        try:
            lessons = await asyncio.wait_for(
                site_parser.fetch_schedule(cookie=cookie, group_id=group_id),
                timeout=PER_GROUP_TIMEOUT,
            )
            if lessons:
                await db.save_schedule_for_group(group_id, lessons)
                counters["updated"] += 1
                logger.info("SUCCESS %s (%s lessons)", group_name, len(lessons))
            else:
                counters["empty"] += 1
                logger.warning("EMPTY response for %s", group_name)
        except ScheduleAuthError:
            counters["auth_failed"] += 1
            logger.error("AUTH FAILED (state=false) for %s (%s)", group_name, group_id)
        except ScheduleFormatError:
            counters["format_failed"] += 1
            logger.error("BAD RESPONSE (not JSON) for %s (%s)", group_name, group_id)
        except asyncio.TimeoutError:
            counters["errors"] += 1
            logger.error("TIMEOUT %ss for %s (%s)", PER_GROUP_TIMEOUT, group_name, group_id)
        except Exception as exc:
            counters["errors"] += 1
            logger.exception("ERROR for %s (%s): %s", group_name, group_id, exc)

        counters["done"] += 1
        done = counters["done"]

        if status_message and (done == 1 or done % 5 == 0 or done == total):
            try:
                await status_message.edit_text(
                    "⏳ <b>Обновляю расписание...</b>\n\n"
                    f"Обработано: {done}/{total}\n"
                    f"✅ Успешно: {counters['updated']}\n"
                    f"⚠️ Пусто: {counters['empty']}\n"
                    f"🔒 Кука не работает: {counters['auth_failed']}\n"
                    f"📄 Плохой ответ: {counters['format_failed']}\n"
                    f"❌ Ошибок: {counters['errors']}"
                )
            except Exception:
                logger.exception("Failed to update Telegram progress message")


async def run_update(status_message: Message | None = None) -> str:
    logger.info("========================================")
    logger.info("UPDATE STARTED")
    logger.info("========================================")

    if not groups_data.GROUPS:
        logger.error("groups_data.GROUPS is empty")
        return "❌ Список групп пуст."

    total = len(groups_data.GROUPS)
    logger.info("Groups loaded: %s", total)

    cookie = await db.get_setting("phpsessid")
    if not cookie:
        logger.error("PHPSESSID is not set")
        return "❌ PHPSESSID не установлен.\n\nИспользуй:\n/setcookie PHPSESSID=твой_cookie"
    logger.info("PHPSESSID found")

    try:
        await db.save_structure(groups_data.GROUPS)
        logger.info("Group structure saved")
    except Exception:
        logger.exception("Failed to save group structure")
        return "❌ Не удалось сохранить список групп."

    counters = {"updated": 0, "empty": 0, "errors": 0, "done": 0, "auth_failed": 0, "format_failed": 0}
    semaphore = asyncio.Semaphore(UPDATE_CONCURRENCY)

    tasks = [
        _update_one_group(course, group_name, group_id, cookie, semaphore, counters, total, status_message)
        for course, group_name, group_id in groups_data.GROUPS
    ]
    await asyncio.gather(*tasks)

    logger.info("========================================")
    logger.info("UPDATE FINISHED")
    logger.info(
        "TOTAL=%s SUCCESS=%s EMPTY=%s AUTH_FAILED=%s FORMAT_FAILED=%s ERRORS=%s",
        total, counters["updated"], counters["empty"],
        counters["auth_failed"], counters["format_failed"], counters["errors"],
    )
    logger.info("========================================")

    result = (
        "✅ <b>Обновление завершено!</b>\n\n"
        f"Всего групп: {total}\n"
        f"✅ Успешно: {counters['updated']}\n"
        f"⚠️ Пустых: {counters['empty']}\n"
        f"🔒 Кука не работает: {counters['auth_failed']}\n"
        f"📄 Плохой ответ: {counters['format_failed']}\n"
        f"❌ Ошибок: {counters['errors']}"
    )
    if counters["auth_failed"] > 0:
        result += (
            "\n\n🔒 Сайт явно сказал, что кука недействительна (state=false). "
            "Получите новую через браузер и снова используйте /setcookie."
        )
    elif counters["format_failed"] > 0:
        result += (
            "\n\n📄 Сайт вернул не JSON, а что-то другое (скорее всего HTML — "
            "страницу входа или капчи). Кука почти наверняка устарела."
        )
    elif counters["updated"] == 0 and counters["empty"] > 0:
        result += (
            "\n\n⚠️ Все группы ответили state=true, но без пар. Странно, если "
            "это происходит для ВСЕХ 40+ групп сразу — стоит проверить формат "
            "куки (нужно значение PHPSESSID, а не что-то другое)."
        )
    return result


# =========================================================
# SCHEDULED UPDATE
# =========================================================
async def scheduled_update(bot: Bot) -> None:
    logger.info("Автоматическое обновление запущено")
    result = await run_update()
    try:
        await bot.send_message(ADMIN_ID, f"[Автообновление]\n\n{result}")
    except Exception:
        logger.exception("Не удалось отправить результат автообновления")


# =========================================================
# STATS
# =========================================================
@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    count = await db.count_users()
    await message.answer(f"Всего пользователей бота: <b>{count}</b>")


# =========================================================
# HEALTH CHECK
# =========================================================
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
    logger.info("Health-check server started on port %s", port)


# =========================================================
# MAIN
# =========================================================
async def main() -> None:
    logger.info("========================================")
    logger.info("Starting Gubkin Bot")
    logger.info("========================================")

    await db.init_db()
    logger.info("Database initialized")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
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
    logger.info("Scheduler started: daily at 03:00 Asia/Tashkent")

    await run_health_server()

    logger.info("Бот запущен")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
