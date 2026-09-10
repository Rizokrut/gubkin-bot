import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

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
from parser import ScheduleAuthError, ScheduleFormatError
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

UPDATE_CONCURRENCY = 6
PER_GROUP_TIMEOUT = 25

# Лимит Telegram — 4096 символов. Оставим запас.
MAX_MESSAGE_LEN = 3500

# GitHub
GITHUB_USER = "Rizokrut"
GITHUB_REPO = "gubkin-bot"
GITHUB_BRANCH = "main"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"


class Registration(StatesGroup):
    choosing_course = State()
    choosing_group = State()


def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID


# =========================================================
# РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
# =========================================================
def split_long_message(text: str, max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    """
    Режет длинный текст на части так, чтобы каждая влезала в лимит Telegram.
    Режет по переводам строк (по парам), а не по буквам.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            # Если одна строка сама по себе больше лимита — режем её грубо
            if len(line) > max_len:
                for i in range(0, len(line), max_len):
                    chunks.append(line[i:i + max_len])
                current = ""
            else:
                current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_long(message: Message, text: str) -> None:
    for chunk in split_long_message(text):
        try:
            await message.answer(chunk)
        except Exception:
            logger.exception("Не удалось отправить часть сообщения")
        await asyncio.sleep(0.05)


# =========================================================
# GITHUB HELPERS
# =========================================================
def _gh_headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "gubkin-bot",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


async def _gh_download_json(filename: str) -> dict | None:
    url = (
        f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/"
        f"{GITHUB_BRANCH}/{filename}"
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url)
            if r.status_code != 200:
                logger.warning("Файл %s не скачался: HTTP %s", filename, r.status_code)
                return None
            return r.json()
    except Exception as e:
        logger.error("Ошибка скачивания %s: %r", filename, e)
        return None


async def _gh_upload_json(filename: str, data: dict) -> bool:
    if not GITHUB_TOKEN:
        logger.error("GITHUB_TOKEN не установлен — не могу загрузить %s", filename)
        return False

    url = f"{GITHUB_API}/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"

    async with httpx.AsyncClient(timeout=60) as client:
        sha = None
        try:
            r = await client.get(url, headers=_gh_headers())
            if r.status_code == 200:
                sha = r.json().get("sha")
        except Exception:
            pass

        import base64
        content = base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii")

        body = {
            "message": f"Merge {filename} from /merge",
            "content": content,
            "branch": GITHUB_BRANCH,
        }
        if sha:
            body["sha"] = sha

        r = await client.put(url, headers=_gh_headers(), json=body)
        if r.status_code in (200, 201):
            return True
        logger.error("Не удалось загрузить %s: HTTP %s %s", filename, r.status_code, r.text[:200])
        return False


# =========================================================
# /MERGE
# =========================================================
@router.message(Command("merge"))
async def cmd_merge(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    status = await message.answer("🔧 <b>Склеиваю части...</b>\n\nЧитаю файлы из GitHub.")

    if not GITHUB_TOKEN:
        await status.edit_text(
            "❌ <b>Нужен GITHUB_TOKEN.</b>\n\n"
            "Добавь переменную окружения GITHUB_TOKEN на Render."
        )
        return

    base = await _gh_download_json("schedule_cache.json")
    if base is None:
        await status.edit_text("❌ Не смог прочитать schedule_cache.json из GitHub.")
        return

    if "groups" not in base:
        base = {"groups": {}}

    merged_groups = dict(base.get("groups", {}))
    logger.info("После Части 1: %s групп", len(merged_groups))

    parts = [2, 3, 4, 5, 6, 7, 8]
    for p in parts:
        fname = f"part{p}.json"
        await status.edit_text(
            f"🔧 <b>Склеиваю части...</b>\n\nЧитаю {fname}..."
        )
        part = await _gh_download_json(fname)
        if part is None or "groups" not in part:
            logger.warning("%s пропущен", fname)
            continue

        added = 0
        for key, val in part["groups"].items():
            if val.get("days"):
                merged_groups[key] = val
                added += 1
        logger.info("Из %s добавлено групп с данными: %s", fname, added)

    result = {
        "generated_at": datetime.now().date().isoformat(),
        "days_ahead": base.get("days_ahead", 14),
        "groups": merged_groups,
    }

    total = len(result["groups"])
    non_empty = sum(1 for g in result["groups"].values() if g.get("days"))

    await status.edit_text(
        f"🔧 <b>Склеиваю части...</b>\n\n"
        f"Собрано групп: {total}\n"
        f"Из них с расписанием: {non_empty}\n\n"
        f"Загружаю в GitHub..."
    )
    ok = await _gh_upload_json("schedule_cache.json", result)

    if ok:
        await status.edit_text(
            f"✅ <b>Готово!</b>\n\n"
            f"Всего групп в кэше: {total}\n"
            f"С расписанием: {non_empty}\n\n"
            f"Теперь запусти /update, чтобы подтянуть расписание в базу бота."
        )
    else:
        await status.edit_text(
            "❌ Не удалось загрузить новый schedule_cache.json в GitHub.\n"
            "Проверь GITHUB_TOKEN и логи Render."
        )


# =========================================================
# ФОРМАТ ДНЯ
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
    text = format_day(group_name, weekday, lessons)
    await send_long(message, text)


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
            "База расписания пока пуста.\n\n"
            "Попросите администратора выполнить /update."
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


@router.message(F.text == "⚙️ Сменить группу")
async def change_group(message: Message, state: FSMContext) -> None:
    courses = await db.get_courses()
    if not courses:
        await message.answer("База расписания пока пуста.")
        return
    await state.set_state(Registration.choosing_course)
    await message.answer("Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses))


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
        text = format_day(group_name, weekday, week[weekday])
        await send_long(message, text)
        await asyncio.sleep(0.15)


@router.message(F.text == "🔗 Ссылка на сайт")
async def site_link(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    group_name = user["group_name"] if user else ""
    await message.answer(f"Сайт расписания: {BASE_URL}\nТвоя группа: <b>{group_name}</b>")


@router.message(Command("setcookie"))
async def set_cookie(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer("Кука больше не нужна. Данные берутся из schedule_cache.json.")


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
        try:
            lessons = await asyncio.wait_for(
                site_parser.fetch_schedule(group_id=group_id),
                timeout=PER_GROUP_TIMEOUT,
            )
            if lessons:
                await db.save_schedule_for_group(group_id, lessons)
                counters["updated"] += 1
            else:
                counters["empty"] += 1
        except ScheduleAuthError:
            counters["auth_failed"] += 1
        except ScheduleFormatError:
            counters["format_failed"] += 1
        except asyncio.TimeoutError:
            counters["errors"] += 1
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
                    f"📄 Плохой ответ: {counters['format_failed']}\n"
                    f"❌ Ошибок: {counters['errors']}"
                )
            except Exception:
                pass


async def run_update(status_message: Message | None = None) -> str:
    if not groups_data.GROUPS:
        return "❌ Список групп пуст."

    total = len(groups_data.GROUPS)

    try:
        await db.save_structure(groups_data.GROUPS)
    except Exception:
        return "❌ Не удалось сохранить список групп."

    counters = {"updated": 0, "empty": 0, "errors": 0, "done": 0, "auth_failed": 0, "format_failed": 0}
    semaphore = asyncio.Semaphore(UPDATE_CONCURRENCY)

    tasks = [
        _update_one_group(course, group_name, group_id, semaphore, counters, total, status_message)
        for course, group_name, group_id in groups_data.GROUPS
    ]
    await asyncio.gather(*tasks)

    return (
        "✅ <b>Обновление завершено!</b>\n\n"
        f"Всего групп: {total}\n"
        f"✅ Успешно: {counters['updated']}\n"
        f"⚠️ Пустых: {counters['empty']}\n"
        f"📄 Плохой ответ: {counters['format_failed']}\n"
        f"❌ Ошибок: {counters['errors']}"
    )


async def scheduled_update(bot: Bot) -> None:
    result = await run_update()
    try:
        await bot.send_message(ADMIN_ID, f"[Автообновление]\n\n{result}")
    except Exception:
        pass


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


# =========================================================
# MAIN
# =========================================================
async def main() -> None:
    logger.info("Starting Gubkin Bot")

    await db.init_db()

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

    await run_health_server()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
