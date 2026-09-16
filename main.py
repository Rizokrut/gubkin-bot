import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
import keyboards as kb
import groups_data
import parser as site_parser
from parser import ScheduleAuthError, ScheduleFormatError
from config import BOT_TOKEN, ADMIN_ID, SCHEDULE_URL, ADMIN_URL

try:
    from config import GOSSIP_CHANNEL_ID, GOSSIP_CHANNEL_URL
except ImportError:
    GOSSIP_CHANNEL_ID = "-1002667144030"
    GOSSIP_CHANNEL_URL = "https://t.me/+X2EP9WhNon85YTAy"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

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

UPDATE_CONCURRENCY = 6
PER_GROUP_TIMEOUT = 25
MAX_MESSAGE_LEN = 3500


TZ = ZoneInfo("Asia/Tashkent")
REMIND_MINUTES = 5


class Registration(StatesGroup):
    choosing_course = State()
    choosing_group = State()


class AdminBroadcast(StatesGroup):
    waiting_text = State()


class AdminImport(StatesGroup):
    waiting_json = State()


_extra_admins: set[int] = set()


def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID or telegram_id in _extra_admins


def is_owner(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID


def _parse_admin_ids(raw: str | None) -> set[int]:
    result: set[int] = set()
    if not raw:
        return result
    for part in raw.replace(" ", ",").split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


async def load_extra_admins() -> None:
    raw = await db.get_setting("extra_admins")
    _extra_admins.clear()
    _extra_admins.update(_parse_admin_ids(raw))


async def save_extra_admins() -> None:
    value = ",".join(str(i) for i in sorted(_extra_admins))
    await db.set_setting("extra_admins", value)


def split_long_message(text: str, max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            if len(line) > max_len:
                for i in range(0, len(line), max_len):
                    chunks.append(line[i : i + max_len])
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


TYPE_ICON = {
    "лекция": "📖",
    "семинар": "💬",
    "лабораторная работа": "🔬",
    "лабораторная": "🔬",
    "мероприятие": "📌",
    "практика": "🛠",
}


def _short_room(room: str) -> str:
    text = (room or "").strip()
    for junk in (" - филиал в г.Ташкент", " — филиал в г.Ташкент", " филиал в г.Ташкент"):
        text = text.replace(junk, "")
    return text.strip(" -—") or "—"


def _pretty_room(room: str) -> str:
    text = _short_room(room)
    pairs = (
        ("Ауд. каф.", "Аудитория кафедры"),
        ("ауд. каф.", "аудитория кафедры"),
        ("Ауд.", "Аудитория "),
        ("ауд.", "аудитория "),
        ("Каб.", "Кабинет "),
        ("каб.", "кабинет "),
        ("каф.", "кафедры "),
    )
    for src, dst in pairs:
        text = text.replace(src, dst)
    text = " ".join(text.split())
    low = text.lower()
    if any(
        word in low
        for word in ("аудитор", "кабинет", "спортзал", "лингфон", "лаборатор")
    ):
        return text
    return f"кабинет {text}"


def _is_cancelled_flag(value) -> bool:
    return value in (1, "1", True, "true", "True")


def _lesson_sort_key(lesson: dict) -> tuple:
    minutes = db._time_slot_to_minutes(site_parser.remap_slot(lesson.get("time_slot") or ""))
    cancelled = 1 if _is_cancelled_flag(lesson.get("is_cancelled")) else 0
    return (minutes, cancelled)


def week_monday(now: datetime | None = None) -> datetime:
    """Понедельник той недели, которую показываем.
    В воскресенье это уже завтрашний понедельник, не прошедший."""
    now = now or datetime.now(TZ)
    if now.weekday() == 6:
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def date_for_weekday(weekday: int, now: datetime | None = None) -> datetime:
    return week_monday(now) + timedelta(days=weekday)


def format_day(
    group_name: str,
    weekday: int,
    lessons: list[dict],
    day_date: datetime | None = None,
) -> str:
    day_name = WEEKDAY_NAMES_RU[weekday]
    if day_date is None:
        day_date = date_for_weekday(weekday)
    header = (
        f"📅 <b>{day_name}</b>, {day_date.strftime('%d.%m')}  ·  {group_name}\n"
    )
    if not lessons:
        return header + "\nПар нет — можно выдохнуть 🎉"

    lessons = sorted(lessons, key=_lesson_sort_key)
    n = sum(
        1 for x in lessons if not _is_cancelled_flag(x.get("is_cancelled"))
    )
    if n % 10 == 1 and n % 100 != 11:
        pair_word = "пара"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        pair_word = "пары"
    else:
        pair_word = "пар"
    header += f"\n<b>{n} {pair_word}</b>\n"

    blocks = []
    for i, lesson in enumerate(lessons, start=1):
        subject = (lesson.get("subject") or "Занятие").strip()
        ltype = (lesson.get("lesson_type") or "").strip()
        icon = TYPE_ICON.get(ltype.lower(), "📘")
        time_slot = site_parser.remap_slot(lesson.get("time_slot") or "") or "—"
        room = _pretty_room(lesson.get("room") or "")
        teacher = (lesson.get("teacher") or "").strip()
        type_line = f"{icon} {ltype}" if ltype else icon

        if _is_cancelled_flag(lesson.get("is_cancelled")):
            blocks.append(
                f"<s>{i}. {time_slot}</s>\n"
                f"❌ <s>{subject}</s>\n"
                f"<i>Пара отменена</i>"
            )
            continue

        extra = f"\n👤 {teacher}" if teacher else ""
        try:
            sub = int(lesson.get("subgroup") or 0)
        except (TypeError, ValueError):
            sub = 0
        sub_line = f"\n👥 подгруппа {sub}" if sub in (1, 2) else ""
        blocks.append(
            f"<b>{i}. {time_slot}</b>\n"
            f"{subject}{sub_line}\n"
            f"{type_line}  ·  {room}{extra}"
        )

    return header + "\n\n" + "\n\n".join(blocks)


async def send_day_schedule(
    message: Message, group_name: str, group_id: int, weekday: int
) -> None:
    lessons = await db.get_schedule_for_day(group_id, weekday)
    await send_long(
        message,
        format_day(group_name, weekday, lessons, date_for_weekday(weekday)),
    )



def _gossip_chat_id():
    raw = str(GOSSIP_CHANNEL_ID or "").strip()
    if raw.startswith("-") and raw[1:].isdigit():
        return int(raw)
    if raw.isdigit():
        return int(raw)
    return raw


async def require_gossip_sub(event) -> bool:
    user = event.from_user
    if not user:
        return False
    if user.id == ADMIN_ID:
        return True
    try:
        member = await event.bot.get_chat_member(_gossip_chat_id(), user.id)
        if member.status in ("member", "administrator", "creator", "restricted"):
            return True
    except Exception:
        logger.exception("gossip sub check")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подписаться на канал", url=GOSSIP_CHANNEL_URL)],
            [InlineKeyboardButton(text="Проверить подписку", callback_data="gossip:chk")],
        ]
    )
    text = "Чтобы пользоваться расписанием, подпишись на канал."
    if hasattr(event, "answer") and getattr(event, "message", None) is None:
        await event.answer(text, reply_markup=kb)
    elif hasattr(event, "message") and event.message:
        await event.message.answer(text, reply_markup=kb)
        if hasattr(event, "answer"):
            await event.answer()
    return False


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not await require_gossip_sub(message):
        return
    user = await db.get_user(message.from_user.id)
    if user and user.get("group_id"):
        await message.answer(
            f"С возвращением 👋\n"
            f"Группа: <b>{user['group_name']}</b>\n\n"
            f"Жми кнопки внизу — расписание на сегодня, завтра или всю неделю.",
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
        "Привет! Это расписание филиала Губкина в Ташкенте.\n\n"
        "Шаг 1 из 2 — выбери курс:",
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
    u = callback.from_user
    await db.save_user(
        u.id,
        course,
        group_name,
        group_id,
        username=u.username,
        first_name=u.first_name,
    )
    await state.clear()
    await callback.message.edit_text(f"Готово. Твоя группа: <b>{group_name}</b>")
    await callback.message.answer(
        "Меню внизу экрана 👇\nСегодня · Завтра · Неделя",
        reply_markup=kb.main_menu_keyboard(),
    )
    await callback.answer()


@router.message(F.text.in_({"👤 Группа", "⚙️ Сменить группу"}))
async def change_group(message: Message, state: FSMContext) -> None:
    courses = await db.get_courses()
    if not courses:
        await message.answer("База расписания пока пуста.")
        return
    await state.set_state(Registration.choosing_course)
    await message.answer(
        "Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses)
    )



@router.callback_query(F.data == "gossip:chk")
async def gossip_chk(callback: CallbackQuery) -> None:
    if await require_gossip_sub(callback):
        await callback.message.answer("Подписка есть. Жми /start", reply_markup=kb.main_menu_keyboard())
        await callback.answer("Ок")
        return
    await callback.answer("Ещё не подписан", show_alert=True)


@router.message(F.text.in_({"📅 Сегодня", "📅 На сегодня"}))
async def today_schedule(message: Message) -> None:
    if not await require_gossip_sub(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    await send_day_schedule(
        message, user["group_name"], user["group_id"], datetime.now(TZ).weekday()
    )


@router.message(F.text.in_({"🌅 Завтра", "📆 На завтра"}))
async def tomorrow_schedule(message: Message) -> None:
    if not await require_gossip_sub(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    weekday = (datetime.now(TZ) + timedelta(days=1)).weekday()
    await send_day_schedule(message, user["group_name"], user["group_id"], weekday)


@router.message(F.text.in_({"🗓 Неделя", "🗓 На неделю"}))
async def week_schedule(message: Message) -> None:
    if not await require_gossip_sub(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    week = await db.get_schedule_for_week(user["group_id"])
    monday = week_monday()
    for weekday in range(7):
        day_date = monday + timedelta(days=weekday)
        await send_long(
            message,
            format_day(user["group_name"], weekday, week[weekday], day_date),
        )
        await asyncio.sleep(0.15)


@router.message(F.text.in_({"🔗 Сайт", "🔗 Ссылка на сайт"}))
async def site_link(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    group_name = user["group_name"] if user else "не выбрана"
    await message.answer(
        f"Официальное расписание:\n{SCHEDULE_URL}\n\n"
        f"Твоя группа в боте: <b>{group_name}</b>"
    )


@router.message(F.text.in_({"💬 Админ", "Связь с админом"}))
async def contact_admin(message: Message) -> None:
    if is_admin(message.from_user.id):
        await message.answer(
            "<b>Админ-панель</b>\n\nВыбери действие:",
            reply_markup=kb.admin_panel_keyboard(is_owner=is_owner(message.from_user.id)),
        )
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Написать админу", url=ADMIN_URL)]
        ]
    )
    await message.answer("Связь с админом:", reply_markup=markup)


@router.message(F.text.in_({"ℹ️ Помощь", "/help"}))
async def help_text(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться</b>\n\n"
        "📅 Сегодня — пары на этот день\n"
        "🌅 Завтра — пары на следующий день\n"
        "🗓 Неделя — пн–вс текущей недели\n"
        "Группу менять в ⚙️ Настройки\n\n"
        "❌ Зачёркнутая пара = отменена на сайте.\n"
        "За 5 минут до пары бот пришлёт напоминание "
        "(время Ташкента).\n"
        "Расписание обновляет админ после нового сбора.\n"
        "Напоминания включаются в ⚙️ Настройки."
    )


def _settings_text(prefs: dict, group_name: str | None) -> str:
    on = bool(prefs.get("reminders_on", 1))
    minutes = int(prefs.get("remind_minutes") or 5)
    status = "включены" if on else "выключены"
    lines = [
        "<b>Настройки</b>",
        "",
        f"Группа: <b>{group_name or 'не выбрана'}</b>",
        f"Напоминания: <b>{status}</b>",
    ]
    if on:
        lines.append(f"Писать за <b>{minutes} мин</b> до пары")
    lines.append("")
    lines.append("Интервал виден, только если напоминания включены.")
    return "\n".join(lines)


@router.message(F.text == "⚙️ Настройки")
async def open_settings(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала выбери группу командой /start")
        return
    prefs = await db.get_user_prefs(message.from_user.id)
    await message.answer(
        _settings_text(prefs, user.get("group_name")),
        reply_markup=kb.settings_keyboard(
            bool(prefs["reminders_on"]), int(prefs["remind_minutes"])
        ),
    )


@router.callback_query(F.data == "set:close")
async def settings_close(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("Настройки закрыты.")
    await callback.answer()


@router.callback_query(F.data == "set:group")
async def settings_change_group(callback: CallbackQuery, state: FSMContext) -> None:
    courses = await db.get_courses()
    if not courses:
        await callback.answer("База пуста", show_alert=True)
        return
    await state.set_state(Registration.choosing_course)
    await callback.message.edit_text(
        "Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses)
    )
    await callback.answer()


@router.callback_query(F.data == "set:rem:off")
async def settings_rem_off(callback: CallbackQuery) -> None:
    await db.set_reminders_on(callback.from_user.id, False)
    await _refresh_settings(callback)


@router.callback_query(F.data == "set:rem:on")
async def settings_rem_on(callback: CallbackQuery) -> None:
    await db.set_reminders_on(callback.from_user.id, True)
    await _refresh_settings(callback)


@router.callback_query(F.data.startswith("set:min:"))
async def settings_minutes(callback: CallbackQuery) -> None:
    minutes = int(callback.data.split(":")[-1])
    await db.set_remind_minutes(callback.from_user.id, minutes)
    await _refresh_settings(callback)


async def _refresh_settings(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    prefs = await db.get_user_prefs(callback.from_user.id)
    await callback.message.edit_text(
        _settings_text(prefs, (user or {}).get("group_name")),
        reply_markup=kb.settings_keyboard(
            bool(prefs["reminders_on"]), int(prefs["remind_minutes"])
        ),
    )
    await callback.answer()


@router.message(Command("setcookie"))
async def set_cookie(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer("Кука больше не нужна. Данные берутся из schedule_cache.json.")


@router.message(Command("merge"))
async def cmd_merge(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "Команда /merge отключена.\n"
        "Пришли part1.json … part8.json — соберём один schedule_cache.json."
    )


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

    site_parser.invalidate_cache()
    total = len(groups_data.GROUPS)

    try:
        await db.save_structure(groups_data.GROUPS)
    except Exception:
        return "❌ Не удалось сохранить список групп."

    counters = {
        "updated": 0,
        "empty": 0,
        "errors": 0,
        "done": 0,
        "auth_failed": 0,
        "format_failed": 0,
    }
    semaphore = asyncio.Semaphore(UPDATE_CONCURRENCY)
    tasks = [
        _update_one_group(
            course, group_name, group_id, semaphore, counters, total, status_message
        )
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


# ============================================================
# АДМИН-ПАНЕЛЬ (кнопки + совместимость со старыми /командами)
# ============================================================

def _format_user_line(p: dict) -> str:
    name = (p.get("first_name") or "").strip() or "без имени"
    username = (p.get("username") or "").strip()
    uid = p.get("telegram_id")
    link = f'<a href="tg://user?id={uid}">{name}</a>'
    nick = f" @{username}" if username else ""
    return f"· {link}{nick} <code>{uid}</code>"


def _format_users_grouped(people: list[dict]) -> str:
    """Красивый список: курс → группа → люди (новые внизу)."""
    if not people:
        return "Пользователей нет."

    lines: list[str] = []
    current_course = object()
    current_group = object()

    for p in people:
        course = p.get("course") or "Без курса"
        group = p.get("group_name") or "Без группы"
        if course != current_course:
            current_course = course
            current_group = object()
            lines.append("")
            lines.append(f"<b>📚 {course}</b>")
        if group != current_group:
            current_group = group
            lines.append(f"\n<b>  {group}</b>")
        lines.append("  " + _format_user_line(p))

    return "\n".join(lines).strip()


async def _stats_summary_text() -> str:
    count = await db.count_users()
    by_course = await db.users_by_course()
    lines = [
        "<b>📊 Статистика</b>",
        "",
        f"Всего пользователей: <b>{count}</b>",
        "",
    ]
    if by_course:
        lines.append("<b>По курсам</b>")
        for course, n in by_course:
            lines.append(f"· {course}: <b>{n}</b>")
    else:
        lines.append("Пока никого нет.")
    lines.append("")
    lines.append("Выбери, что посмотреть подробнее:")
    return "\n".join(lines)


@router.callback_query(F.data == "adm:close")
async def adm_close(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("Панель закрыта.")
    await callback.answer()


@router.callback_query(F.data == "adm:panel")
async def adm_panel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "<b>Админ-панель</b>\n\nВыбери действие:",
        reply_markup=kb.admin_panel_keyboard(is_owner=is_owner(callback.from_user.id)),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:stats")
async def adm_stats_root(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    text = await _stats_summary_text()
    await callback.message.edit_text(text, reply_markup=kb.admin_stats_root_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:stats:courses")
async def adm_stats_courses(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    by_course = await db.users_by_course()
    if not by_course:
        await callback.message.edit_text(
            "Курсов пока нет.",
            reply_markup=kb.admin_back_keyboard("adm:stats"),
        )
        await callback.answer()
        return
    lines = ["<b>📚 По курсам</b>", ""]
    for course, n in by_course:
        lines.append(f"· {course}: <b>{n}</b>")
    lines.append("")
    lines.append("Нажми на курс, чтобы увидеть группы и людей:")
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb.admin_stats_courses_keyboard(by_course),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:stats:c:"))
async def adm_stats_course_detail(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    raw = callback.data.split(":", 3)[-1]
    # Восстанавливаем имя курса (мы заменяли : на _)
    by_course = await db.users_by_course()
    course = None
    for c, _ in by_course:
        if c.replace(":", "_")[:40] == raw:
            course = c
            break
    if course is None:
        await callback.answer("Курс не найден", show_alert=True)
        return

    by_group = await db.users_by_group()
    groups_here = [(c, g, n) for c, g, n in by_group if c == course]
    people = await db.list_users_by_course(course)

    lines = [f"<b>📚 {course}</b>", f"Всего: <b>{len(people)}</b>", ""]
    if groups_here:
        lines.append("<b>Группы</b>")
        for _, g, n in groups_here:
            lines.append(f"· {g}: {n}")
        lines.append("")
    if people:
        lines.append("<b>Люди</b> (новые внизу)")
        current_group = object()
        for p in people:
            g = p.get("group_name") or "—"
            if g != current_group:
                current_group = g
                lines.append(f"\n<i>{g}</i>")
            lines.append(_format_user_line(p))

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb.admin_stats_groups_keyboard(groups_here, course_filter=course),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:stats:groups")
async def adm_stats_groups(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    by_group = await db.users_by_group()
    if not by_group:
        await callback.message.edit_text(
            "Групп пока нет.",
            reply_markup=kb.admin_back_keyboard("adm:stats"),
        )
        await callback.answer()
        return
    lines = ["<b>👥 По группам</b>", ""]
    current_course = object()
    for course, group_name, n in by_group:
        if course != current_course:
            current_course = course
            lines.append(f"\n<b>{course}</b>")
        lines.append(f"· {group_name}: <b>{n}</b>")
    lines.append("")
    lines.append("Нажми на группу, чтобы увидеть список:")
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb.admin_stats_groups_keyboard(by_group),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:stats:g:"))
async def adm_stats_group_detail(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    key = callback.data.split(":", 3)[-1]
    if "|" not in key:
        await callback.answer("Ошибка данных", show_alert=True)
        return
    course_part, group_part = key.split("|", 1)

    by_group = await db.users_by_group()
    matched = None
    for c, g, n in by_group:
        c_key = c[:15] if len(f"{c}|{g}".encode()) > 40 else c
        g_key = g[:20] if len(f"{c}|{g}".encode()) > 40 else g
        if (c == course_part and g == group_part) or (
            c_key == course_part and g_key == group_part
        ):
            matched = (c, g, n)
            break
        # fallback: startswith
        if c.startswith(course_part) and g.startswith(group_part):
            matched = (c, g, n)
            break

    if not matched:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    course, group_name, count = matched
    people = await db.list_users_by_group(course, group_name)
    lines = [
        f"<b>{group_name}</b>",
        f"Курс: {course}",
        f"Людей: <b>{count}</b>",
        "",
        "<b>Список</b> (новые внизу)",
        "",
    ]
    for p in people:
        lines.append(_format_user_line(p))
    if not people:
        lines.append("Пусто.")

    text = "\n".join(lines)
    # Если слишком длинно — отправим новым сообщением
    if len(text) > 3500:
        await callback.message.edit_text(
            f"<b>{group_name}</b> · {count} чел.\nСписок ниже 👇",
            reply_markup=kb.admin_back_keyboard("adm:stats:groups"),
        )
        await send_long(callback.message, text)
    else:
        await callback.message.edit_text(
            text,
            reply_markup=kb.admin_back_keyboard("adm:stats:groups"),
        )
    await callback.answer()


@router.callback_query(F.data == "adm:stats:all")
async def adm_stats_all(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    people = await db.list_users_detailed()
    count = len(people)
    await callback.message.edit_text(
        f"<b>📋 Все пользователи</b>\nВсего: <b>{count}</b>\n\nСписок ниже 👇",
        reply_markup=kb.admin_back_keyboard("adm:stats"),
    )
    await callback.answer()
    text = _format_users_grouped(people)
    await send_long(callback.message, text)


@router.callback_query(F.data == "adm:update")
async def adm_update(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    status = await callback.message.answer("⏳ <b>Обновляю расписание...</b>\n\nПодготовка...")
    result = await run_update(status)
    try:
        await status.edit_text(result)
    except Exception:
        logger.exception("Не удалось изменить итоговое сообщение")


@router.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminBroadcast.waiting_text)
    await callback.message.edit_text(
        "Напиши текст рассылки одним сообщением.\n"
        "Можно HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>\n\n"
        "Отмена: /cancel",
        reply_markup=kb.admin_back_keyboard("adm:panel"),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:admins")
async def adm_admins_list(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await load_extra_admins()
    lines = ["<b>👥 Админы</b>", "", f"Владелец: <code>{ADMIN_ID}</code>"]
    if _extra_admins:
        lines.append("")
        lines.append("Дополнительно:")
        for aid in sorted(_extra_admins):
            lines.append(f"· <code>{aid}</code>")
    else:
        lines.append("")
        lines.append("Дополнительных админов нет.")
    if is_owner(callback.from_user.id):
        lines.append("")
        lines.append("Добавить/убрать — кнопки в панели или /addadmin /deladmin")
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb.admin_back_keyboard("adm:panel"),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:export")
async def adm_export(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer("Готовлю файл…")
    rows = await db.export_users()
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    data = payload.encode("utf-8")
    await callback.message.answer_document(
        BufferedInputFile(data, filename="users_export.json"),
        caption=f"Людей в базе: {len(rows)}\nСохрани файл. После деплоя — импорт.",
    )


@router.callback_query(F.data == "adm:import")
async def adm_import(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminImport.waiting_json)
    await callback.message.edit_text(
        "Пришли файл <code>users_export.json</code> или вставь JSON текстом.\n"
        "Отмена: /cancel",
        reply_markup=kb.admin_back_keyboard("adm:panel"),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:addadmin")
async def adm_addadmin_prompt(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    await callback.message.edit_text(
        "Напиши команду:\n<code>/addadmin 123456789</code>\n\n"
        "ID можно взять у @userinfobot.",
        reply_markup=kb.admin_back_keyboard("adm:panel"),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:deladmin")
async def adm_deladmin_prompt(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    await callback.message.edit_text(
        "Напиши команду:\n<code>/deladmin 123456789</code>",
        reply_markup=kb.admin_back_keyboard("adm:panel"),
    )
    await callback.answer()


# --- Старые /команды (совместимость) ---

@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    text = await _stats_summary_text()
    await message.answer(text, reply_markup=kb.admin_stats_root_keyboard())


@router.message(Command("admin"))
async def admin_help(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Админ-панель</b>\n\nВыбери действие:",
        reply_markup=kb.admin_panel_keyboard(is_owner=is_owner(message.from_user.id)),
    )


@router.message(Command("admins"))
async def list_admins(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await load_extra_admins()
    lines = [f"Владелец: <code>{ADMIN_ID}</code>"]
    if _extra_admins:
        lines.append("Дополнительно:")
        for aid in sorted(_extra_admins):
            lines.append(f"· <code>{aid}</code>")
    else:
        lines.append("Дополнительных админов нет.")
    await message.answer("\n".join(lines))


@router.message(Command("addadmin"))
async def add_admin_cmd(message: Message) -> None:
    if not is_owner(message.from_user.id):
        if is_admin(message.from_user.id):
            await message.answer("Добавлять админов может только владелец.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "Напиши так:\n<code>/addadmin 123456789</code>\n\n"
            "ID человек берёт у @userinfobot."
        )
        return
    new_id = int(parts[1])
    if new_id == ADMIN_ID:
        await message.answer("Это и так владелец.")
        return
    await load_extra_admins()
    _extra_admins.add(new_id)
    await save_extra_admins()
    await message.answer(f"Админка выдана: <code>{new_id}</code>")


@router.message(Command("deladmin"))
async def del_admin_cmd(message: Message) -> None:
    if not is_owner(message.from_user.id):
        if is_admin(message.from_user.id):
            await message.answer("Убирать админов может только владелец.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Напиши так:\n<code>/deladmin 123456789</code>")
        return
    old_id = int(parts[1])
    await load_extra_admins()
    _extra_admins.discard(old_id)
    await save_extra_admins()
    await message.answer(f"Админка снята: <code>{old_id}</code>")


@router.message(Command("exportusers"))
async def export_users_cmd(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = await db.export_users()
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    data = payload.encode("utf-8")
    await message.answer_document(
        BufferedInputFile(data, filename="users_export.json"),
        caption=f"Людей в базе: {len(rows)}\nСохрани файл. После деплоя — /importusers",
    )


@router.message(Command("importusers"))
async def import_users_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminImport.waiting_json)
    await message.answer(
        "Пришли файл <code>users_export.json</code> или вставь JSON текстом.\n"
        "Отмена: /cancel"
    )


@router.message(AdminImport.waiting_json, Command("cancel"))
async def import_users_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Импорт отменён.")


@router.message(AdminImport.waiting_json, F.document)
async def import_users_file(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    bot = message.bot
    file = await bot.download(message.document)
    raw = file.read().decode("utf-8")
    await _apply_import(message, state, raw)


@router.message(AdminImport.waiting_json)
async def import_users_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await _apply_import(message, state, message.text or "")


async def _apply_import(message: Message, state: FSMContext, raw: str) -> None:
    raw = (raw or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await message.answer("Это не JSON. Пришли файл от /exportusers.")
        return
    if not isinstance(data, list):
        await message.answer("В корне должен быть список [...].")
        return
    ok = 0
    skip = 0
    for item in data:
        if not isinstance(item, dict):
            skip += 1
            continue
        try:
            tid = int(item.get("telegram_id"))
            gid = int(item.get("group_id"))
        except (TypeError, ValueError):
            skip += 1
            continue
        course = str(item.get("course") or "")
        name = str(item.get("group_name") or "")
        if not course or not name:
            skip += 1
            continue
        await db.save_user(
            tid,
            course,
            name,
            gid,
            username=item.get("username"),
            first_name=item.get("first_name"),
        )
        ok += 1
    await state.clear()
    await message.answer(f"Готово. Вернул: {ok}. Пропустил: {skip}.")


@router.message(Command("broadcast"))
async def broadcast_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminBroadcast.waiting_text)
    await message.answer(
        "Напиши текст рассылки одним сообщением.\n"
        "Можно HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>\n\n"
        "Отмена: /cancel"
    )


@router.message(Command("cancel"))
async def broadcast_cancel(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Отменил.")


@router.message(AdminBroadcast.waiting_text)
async def broadcast_send(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    text = (message.html_text or message.text or "").strip()
    if not text:
        await message.answer("Пусто. Напиши текст или /cancel")
        return
    await state.clear()
    users = await db.get_all_users()
    status = await message.answer(f"Рассылаю {len(users)} чел...")
    ok = 0
    fail = 0
    for user in users:
        try:
            await message.bot.send_message(user["telegram_id"], text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"Готово.\nДоставлено: {ok}\nНе дошло: {fail}")


def _lesson_start_minutes(time_slot: str) -> int | None:
    minutes = db._time_slot_to_minutes(time_slot)
    if minutes >= 99999:
        return None
    return minutes


def _short_room_admin(room: str) -> str:
    text = (room or "").strip()
    return text.replace(" - филиал в г.Ташкент", "").strip(" -—") or "—"


async def send_pair_reminders(bot: Bot) -> None:
    now = datetime.now(TZ)
    weekday = now.weekday()
    now_min = now.hour * 60 + now.minute
    day_key = now.date().isoformat()
    users = await db.get_all_users()
    if not users:
        return

    grouped: dict[int, list[dict]] = {}
    for user in users:
        grouped.setdefault(user["group_id"], []).append(user)

    for group_id, group_users in grouped.items():
        lessons = await db.get_schedule_for_day(group_id, weekday)
        for lesson in lessons:
            if lesson.get("is_cancelled") in (1, "1", True):
                continue
            slot = site_parser.remap_slot(lesson.get("time_slot") or "")
            start = _lesson_start_minutes(slot)
            if start is None:
                continue
            subject = (lesson.get("subject") or "Пара").strip()
            room = _pretty_room(lesson.get("room") or "")
            teacher = (lesson.get("teacher") or "").strip()
            ltype = (lesson.get("lesson_type") or "").strip()

            for user in group_users:
                if not int(user.get("reminders_on") or 0):
                    continue
                lead = int(user.get("remind_minutes") or 5)
                delta = start - now_min
                if delta < lead - 1 or delta > lead:
                    continue
                already = await db.reminder_was_sent(
                    user["telegram_id"], day_key, slot, subject
                )
                if already:
                    continue
                text = (
                    f"Через {lead} мин пара\n\n"
                    f"<b>{slot}</b>\n"
                    f"{subject}"
                )
                if ltype:
                    text += f" ({ltype})"
                text += f"\n{room}"
                if teacher:
                    text += f"\n{teacher}"
                try:
                    await bot.send_message(user["telegram_id"], text)
                    await db.mark_reminder_sent(
                        user["telegram_id"], group_id, day_key, slot, subject
                    )
                except Exception:
                    logger.exception(
                        "Не отправилось напоминание %s", user["telegram_id"]
                    )
                await asyncio.sleep(0.03)


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


async def main() -> None:
    logger.info("Starting Gubkin Bot")
    await db.init_db()
    await load_extra_admins()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
    scheduler.add_job(
        send_pair_reminders,
        "interval",
        minutes=1,
        args=[bot],
        id="pair_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    await run_health_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
