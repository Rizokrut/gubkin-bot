import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import database
import parser as site_parser
import keyboards


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================
# BOT
# =========================

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


# =========================
# HEALTH CHECK
# =========================

async def handle_health(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        int(config.PORT),
    )

    await site.start()

    logger.info("Health server started on port %s", config.PORT)


# =========================
# START
# =========================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id

    await database.save_user(
        user_id=user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    groups = await database.get_groups()

    if not groups:
        await message.answer(
            "⚠️ Список групп пока не загружен."
        )
        return

    await message.answer(
        "Привет! 👋\n\n"
        "Выбери свою группу:",
        reply_markup=keyboards.groups_keyboard(groups),
    )


# =========================
# GROUP SELECTION
# =========================

@dp.callback_query(F.data.startswith("group:"))
async def select_group(callback):
    try:
        group_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("Ошибка группы", show_alert=True)
        return

    user_id = callback.from_user.id

    await database.save_user(
        user_id=user_id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        group_id=group_id,
    )

    groups = await database.get_groups()

    group_name = None

    for group in groups:
        if isinstance(group, dict):
            if int(group.get("id", -1)) == group_id:
                group_name = group.get("name")
                break
        else:
            try:
                if int(group[0]) == group_id:
                    group_name = group[1]
                    break
            except Exception:
                pass

    if not group_name:
        group_name = str(group_id)

    await callback.message.edit_text(
        f"✅ Группа выбрана: <b>{group_name}</b>\n\n"
        "Что хочешь посмотреть?",
        reply_markup=keyboards.schedule_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================
# TODAY
# =========================

@dp.callback_query(F.data == "today")
async def show_today(callback):
    user = await database.get_user(callback.from_user.id)

    if not user:
        await callback.answer(
            "Сначала выбери группу через /start",
            show_alert=True,
        )
        return

    group_id = user["group_id"]

    if not group_id:
        await callback.answer(
            "Сначала выбери группу через /start",
            show_alert=True,
        )
        return

    today = datetime.now().date()

    schedule = await database.get_schedule_for_day(
        group_id,
        today,
    )

    text = format_schedule(
        schedule,
        today,
        "Сегодня",
    )

    await callback.message.edit_text(
        text,
        reply_markup=keyboards.schedule_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================
# TOMORROW
# =========================

@dp.callback_query(F.data == "tomorrow")
async def show_tomorrow(callback):
    user = await database.get_user(callback.from_user.id)

    if not user:
        await callback.answer(
            "Сначала выбери группу через /start",
            show_alert=True,
        )
        return

    group_id = user["group_id"]

    if not group_id:
        await callback.answer(
            "Сначала выбери группу через /start",
            show_alert=True,
        )
        return

    tomorrow = datetime.now().date() + timedelta(days=1)

    schedule = await database.get_schedule_for_day(
        group_id,
        tomorrow,
    )

    text = format_schedule(
        schedule,
        tomorrow,
        "Завтра",
    )

    await callback.message.edit_text(
        text,
        reply_markup=keyboards.schedule_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================
# WEEK
# =========================

@dp.callback_query(F.data == "week")
async def show_week(callback):
    user = await database.get_user(callback.from_user.id)

    if not user:
        await callback.answer(
            "Сначала выбери группу через /start",
            show_alert=True,
        )
        return

    group_id = user["group_id"]

    if not group_id:
        await callback.answer(
            "Сначала выбери группу через /start",
            show_alert=True,
        )
        return

    today = datetime.now().date()

    schedule = await database.get_schedule_for_week(
        group_id,
        today,
    )

    text = format_week_schedule(schedule)

    await callback.message.edit_text(
        text,
        reply_markup=keyboards.schedule_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================
# BACK TO GROUPS
# =========================

@dp.callback_query(F.data == "change_group")
async def change_group(callback):
    groups = await database.get_groups()

    await callback.message.edit_text(
        "Выбери группу:",
        reply_markup=keyboards.groups_keyboard(groups),
    )

    await callback.answer()


# =========================
# SITE
# =========================

@dp.callback_query(F.data == "site")
async def open_site(callback):
    await callback.answer()

    await callback.message.answer(
        "🌐 Личный кабинет университета:\n"
        "https://lk.gubkin.ru/"
    )


# =========================
# FORMAT SCHEDULE
# =========================

def format_schedule(schedule, date, title):
    if not schedule:
        return (
            f"📅 <b>{title}</b>\n"
            f"{date.strftime('%d.%m.%Y')}\n\n"
            "🎉 Пар нет!"
        )

    lines = [
        f"📅 <b>{title}</b>",
        date.strftime("%d.%m.%Y"),
        "",
    ]

    for lesson in schedule:
        if isinstance(lesson, dict):
            time_start = lesson.get("time_start", "")
            time_end = lesson.get("time_end", "")
            subject = lesson.get("subject", "Без названия")
            teacher = lesson.get("teacher", "")
            room = lesson.get("room", "")
            lesson_type = lesson.get("type", "")

        else:
            # На случай старого формата БД
            try:
                time_start = lesson[0]
                time_end = lesson[1]
                subject = lesson[2]
                teacher = lesson[3]
                room = lesson[4]
                lesson_type = lesson[5]
            except Exception:
                subject = str(lesson)
                time_start = ""
                time_end = ""
                teacher = ""
                room = ""
                lesson_type = ""

        time_text = ""

        if time_start and time_end:
            time_text = f"🕐 {time_start}–{time_end}"
        elif time_start:
            time_text = f"🕐 {time_start}"

        lines.append(
            f"<b>{subject}</b>"
        )

        if time_text:
            lines.append(time_text)

        if teacher:
            lines.append(f"👨‍🏫 {teacher}")

        if room:
            lines.append(f"🚪 {room}")

        if lesson_type:
            lines.append(f"📚 {lesson_type}")

        lines.append("")

    return "\n".join(lines)


# =========================
# FORMAT WEEK
# =========================

def format_week_schedule(schedule):
    if not schedule:
        return "📅 <b>Расписание на неделю</b>\n\nПар нет."

    lines = [
        "📅 <b>Расписание на неделю</b>",
        "",
    ]

    current_date = None

    for item in schedule:
        if isinstance(item, dict):
            date = item.get("date")
            time_start = item.get("time_start", "")
            time_end = item.get("time_end", "")
            subject = item.get("subject", "Без названия")
            teacher = item.get("teacher", "")
            room = item.get("room", "")
            lesson_type = item.get("type", "")

        else:
            try:
                date = item[0]
                time_start = item[1]
                time_end = item[2]
                subject = item[3]
                teacher = item[4]
                room = item[5]
                lesson_type = item[6]
            except Exception:
                continue

        if date != current_date:
            current_date = date

            lines.append(
                f"\n📌 <b>{date}</b>"
            )

        time_text = ""

        if time_start and time_end:
            time_text = f"{time_start}–{time_end}"
        elif time_start:
            time_text = time_start

        lines.append(
            f"• <b>{time_text}</b> — {subject}"
        )

        if teacher:
            lines.append(
                f"  👨‍🏫 {teacher}"
            )

        if room:
            lines.append(
                f"  🚪 {room}"
            )

        if lesson_type:
            lines.append(
                f"  📚 {lesson_type}"
            )

    return "\n".join(lines)


# =========================
# SET COOKIE
# =========================

@dp.message(Command("setcookie"))
async def set_cookie(message: Message):
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "Использование:\n\n"
            "<code>/setcookie PHPSESSID=твой_cookie</code>\n\n"
            "или просто:\n"
            "<code>/setcookie твой_cookie</code>",
            parse_mode="HTML",
        )
        return

    cookie = parts[1].strip()

    if not cookie:
        await message.answer("❌ Cookie пустой.")
        return

    await database.set_setting(
        "phpsessid",
        cookie,
    )

    await message.answer(
        "✅ Cookie сохранён.\n\n"
        "Теперь можно запускать:\n"
        "<code>/update</code>",
        parse_mode="HTML",
    )

    logger.info(
        "PHPSESSID cookie updated by user %s",
        message.from_user.id,
    )


# =========================
# UPDATE
# =========================

@dp.message(Command("update"))
async def run_update(message: Message):
    logger.info(
        "========== UPDATE STARTED =========="
    )

    status_message = await message.answer(
        "⏳ Обновляю расписание всех групп...\n"
        "Это может занять некоторое время."
    )

    cookie = await database.get_setting(
        "phpsessid"
    )

    if not cookie:
        logger.error(
            "UPDATE STOPPED: PHPSESSID is not set"
        )

        await status_message.edit_text(
            "❌ Cookie не установлен.\n\n"
            "Сначала используй:\n"
            "<code>/setcookie PHPSESSID=...</code>",
            parse_mode="HTML",
        )

        return

    logger.info(
        "Cookie found. Starting update."
    )

    try:
        groups = await database.get_groups()
    except Exception:
        logger.exception(
            "Failed to load groups from database"
        )

        await status_message.edit_text(
            "❌ Не удалось получить список групп из базы."
        )

        return

    if not groups:
        logger.error(
            "No groups found in database"
        )

        await status_message.edit_text(
            "❌ В базе данных нет групп."
        )

        return

    logger.info(
        "Groups loaded: %s",
        len(groups),
    )

    success = 0
    empty = 0
    failed = 0

    total = len(groups)

    # =========================
    # UPDATE EACH GROUP
    # =========================

    for index, group in enumerate(groups, start=1):

        # Поддерживаем несколько форматов результата get_groups()
        if isinstance(group, dict):
            group_id = group.get("id")
            group_name = group.get("name", str(group_id))
        else:
            try:
                group_id = group[0]
                group_name = group[1]
            except Exception:
                logger.error(
                    "Invalid group object: %r",
                    group,
                )
                failed += 1
                continue

        try:
            group_id = int(group_id)
        except Exception:
            logger.error(
                "Invalid group ID: %r",
                group_id,
            )
            failed += 1
            continue

        logger.info(
            "[%s/%s] Starting group: %s | ID=%s",
            index,
            total,
            group_name,
            group_id,
        )

        try:
            # Максимум 25 секунд на одну группу
            lessons = await asyncio.wait_for(
                site_parser.fetch_schedule(
                    cookie=cookie,
                    group_id=group_id,
                ),
                timeout=25,
            )

            logger.info(
                "[%s/%s] Parser returned %s lessons for %s",
                index,
                total,
                len(lessons) if lessons else 0,
                group_name,
            )

            if lessons:
                await database.save_schedule_for_group(
                    group_id,
                    lessons,
                )

                success += 1

                logger.info(
                    "[%s/%s] SUCCESS: %s",
                    index,
                    total,
                    group_name,
                )

            else:
                empty += 1

                logger.warning(
                    "[%s/%s] EMPTY: %s",
                    index,
                    total,
                    group_name,
                )

        except asyncio.TimeoutError:
            failed += 1

            logger.error(
                "[%s/%s] TIMEOUT after 25 seconds: %s | ID=%s",
                index,
                total,
                group_name,
                group_id,
            )

        except Exception as e:
            failed += 1

            logger.exception(
                "[%s/%s] FAILED: %s | ID=%s | error=%s",
                index,
                total,
                group_name,
                group_id,
                e,
            )

        # Обновляем сообщение примерно каждые 5 групп
        # или на последней группе
        if index == 1 or index % 5 == 0 or index == total:
            try:
                await status_message.edit_text(
                    "⏳ <b>Обновляю расписание...</b>\n\n"
                    f"Обработано: {index}/{total}\n"
                    f"✅ Успешно: {success}\n"
                    f"⚠️ Пусто: {empty}\n"
                    f"❌ Ошибок: {failed}",
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception(
                    "Failed to edit update status message"
                )

    # =========================
    # FINISHED
    # =========================

    logger.info(
        "========== UPDATE FINISHED =========="
    )

    logger.info(
        "Result: success=%s empty=%s failed=%s total=%s",
        success,
        empty,
        failed,
        total,
    )

    await status_message.edit_text(
        "✅ <b>Обновление завершено!</b>\n\n"
        f"Всего групп: {total}\n"
        f"✅ Успешно: {success}\n"
        f"⚠️ Пустых: {empty}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="HTML",
    )


# =========================
# STATS
# =========================

@dp.message(Command("stats"))
async def stats(message: Message):
    try:
        users_count = await database.count_users()
    except Exception:
        logger.exception(
            "Failed to get users count"
        )
        users_count = 0

    groups = await database.get_groups()

    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: {users_count}\n"
        f"👥 Групп: {len(groups)}",
        parse_mode="HTML",
    )


# =========================
# AUTOMATIC UPDATE
# =========================

async def scheduled_update():
    logger.info(
        "========== SCHEDULED UPDATE STARTED =========="
    )

    cookie = await database.get_setting(
        "phpsessid"
    )

    if not cookie:
        logger.warning(
            "Scheduled update skipped: no PHPSESSID"
        )
        return

    groups = await database.get_groups()

    if not groups:
        logger.warning(
            "Scheduled update skipped: no groups"
        )
        return

    success = 0
    empty = 0
    failed = 0

    total = len(groups)

    for index, group in enumerate(groups, start=1):

        if isinstance(group, dict):
            group_id = group.get("id")
            group_name = group.get("name", str(group_id))
        else:
            try:
                group_id = group[0]
                group_name = group[1]
            except Exception:
                failed += 1
                continue

        try:
            group_id = int(group_id)

            logger.info(
                "[AUTO %s/%s] Updating %s | ID=%s",
                index,
                total,
                group_name,
                group_id,
            )

            lessons = await asyncio.wait_for(
                site_parser.fetch_schedule(
                    cookie=cookie,
                    group_id=group_id,
                ),
                timeout=25,
            )

            if lessons:
                await database.save_schedule_for_group(
                    group_id,
                    lessons,
                )
                success += 1
            else:
                empty += 1

        except asyncio.TimeoutError:
            failed += 1
            logger.error(
                "[AUTO %s/%s] Timeout: %s",
                index,
                total,
                group_name,
            )

        except Exception:
            failed += 1
            logger.exception(
                "[AUTO %s/%s] Failed: %s",
                index,
                total,
                group_name,
            )

    logger.info(
        "========== SCHEDULED UPDATE FINISHED =========="
    )

    logger.info(
        "AUTO RESULT: success=%s empty=%s failed=%s total=%s",
        success,
        empty,
        failed,
        total,
    )


# =========================
# MAIN
# =========================

async def main():
    logger.info(
        "Starting Gubkin schedule bot..."
    )

    # Database
    await database.init_db()

    logger.info(
        "Database initialized"
    )

    # =========================
    # SCHEDULER
    # =========================

    scheduler = AsyncIOScheduler(
        timezone="Asia/Tashkent"
    )

    scheduler.add_job(
        scheduled_update,
        CronTrigger(
            hour=3,
            minute=0,
            timezone="Asia/Tashkent",
        ),
        id="daily_schedule_update",
        replace_existing=True,
    )

    scheduler.start()

    logger.info(
        "Scheduler started. Daily update: 03:00 Asia/Tashkent"
    )

    # =========================
    # WEB SERVER
    # =========================

    await start_web_server()

    # =========================
    # BOT
    # =========================

    logger.info(
        "Bot polling started"
    )

    try:
        await dp.start_polling(bot)

    finally:
        scheduler.shutdown()

        await bot.session.close()

        logger.info(
            "Bot stopped"
        )


# =========================
# ENTRY POINT
# =========================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info(
            "Bot stopped manually"
        )
