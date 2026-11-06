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


class Registration(StatesGroup):
    choosing_course = State()
    choosing_group = State()


class AdminBroadcast(StatesGroup):
    waiting_text = State()


class AdminManualSub(StatesGroup):
    waiting_data = State()


class AdminPaySettings(StatesGroup):
    waiting_details = State()


class UserCheckUpload(StatesGroup):
    waiting_photo = State()


class AdminAddAdmin(StatesGroup):
    waiting_id = State()


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


async def has_active_subscription(telegram_id: int) -> bool:
    if is_admin(telegram_id):
        return True
    user = await db.get_user(telegram_id)
    if not user or not user.get("subscription_expires"):
        return False
    try:
        expires = datetime.fromisoformat(user["subscription_expires"])
        return expires > datetime.now()
    except ValueError:
        return False


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
    if any(word in low for word in ("аудитор", "кабинет", "спортзал", "лингфон", "лаборатор")):
        return text
    return f"кабинет {text}"


def _is_cancelled_flag(value) -> bool:
    return value in (1, "1", True, "true", "True")


def _lesson_sort_key(lesson: dict) -> tuple:
    minutes = db._time_slot_to_minutes(site_parser.remap_slot(lesson.get("time_slot") or ""))
    cancelled = 1 if _is_cancelled_flag(lesson.get("is_cancelled")) else 0
    return (minutes, cancelled)


def week_monday(now: datetime | None = None) -> datetime:
    now = now or datetime.now(TZ)
    if now.weekday() == 6:
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def date_for_weekday(weekday: int, now: datetime | None = None) -> datetime:
    return week_monday(now) + timedelta(days=weekday)


def format_day_truncated(group_name: str, weekday: int, lessons: list[dict], day_date: datetime | None = None) -> str:
    day_name = WEEKDAY_NAMES_RU[weekday]
    if day_date is None:
        day_date = date_for_weekday(weekday)
    header = f"📅 <b>{day_name}</b>, {day_date.strftime('%d.%m')}  ·  {group_name}\n"
    if not lessons:
        return header + "\nПар нет — можно выдохнуть 🎉"
    
    n = sum(1 for x in lessons if not _is_cancelled_flag(x.get("is_cancelled")))
    if n % 10 == 1 and n % 100 != 11:
        pair_word = "пара"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        pair_word = "пары"
    else:
        pair_word = "пар"
    header += f"\n<b>{n} {pair_word}</b>\n"
    return header


def format_day(group_name: str, weekday: int, lessons: list[dict], day_date: datetime | None = None) -> str:
    day_name = WEEKDAY_NAMES_RU[weekday]
    if day_date is None:
        day_date = date_for_weekday(weekday)
    header = f"📅 <b>{day_name}</b>, {day_date.strftime('%d.%m')}  ·  {group_name}\n"
    if not lessons:
        return header + "\nПар нет — можно выдохнуть 🎉"

    lessons = sorted(lessons, key=_lesson_sort_key)
    n = sum(1 for x in lessons if not _is_cancelled_flag(x.get("is_cancelled")))
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
            blocks.append(f"<s>{i}. {time_slot}</s>\n❌ <s>{subject}</s>\n<i>Пара отменена</i>")
            continue

        extra = f"\n👤 {teacher}" if teacher else ""
        try:
            sub = int(lesson.get("subgroup") or 0)
        except (TypeError, ValueError):
            sub = 0
        sub_line = f"\n👥 подгруппа {sub}" if sub in (1, 2) else ""
        blocks.append(f"<b>{i}. {time_slot}</b>\n{subject}{sub_line}\n{type_line}  ·  {room}{extra}")

    return header + "\n\n" + "\n\n".join(blocks)


async def send_day_schedule(message: Message, group_name: str, group_id: int, weekday: int) -> None:
    user_id = message.from_user.id
    lessons = await db.get_schedule_for_day(group_id, weekday)
    
    if await has_active_subscription(user_id):
        await send_long(message, format_day(group_name, weekday, lessons, date_for_weekday(weekday)))
    else:
        text = format_day_truncated(group_name, weekday, lessons, date_for_weekday(weekday))
        text += (
            "\n\n🔒 <b>Полное расписание (время, кабинеты, преподаватели) и напоминания доступны по подписке!</b>\n"
            "Приобретая подписку, вы получаете удобного бота без ограничений всего за символическую сумму.\n\n"
            "Нажмите кнопку ниже, чтобы узнать подробности и подключить доступ 👇"
        )
        kb_pay = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💳 Купить подписку", callback_data="menu:sub")]]
        )
        await message.answer(text, reply_markup=kb_pay)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        maybe_ref = int(args[1])
        if maybe_ref != user_id:
            referrer_id = maybe_ref

    user = await db.get_user(user_id)
    if not user and referrer_id:
        # Обработка реферала
        ref_user = await db.get_user(referrer_id)
        if ref_user:
            invited = await db.increment_invited(referrer_id)
            if invited >= 5 and (ref_user.get("ref_bonus_claimed", 0) == 0):
                await db.add_subscription_days(referrer_id, 30)
                # Пометим бонус как выданный или обнулим счетчик/добавим флаг
                async with aiosqlite.connect(db.DB_PATH) as db_conn:
                    await db_conn.execute("UPDATE users SET ref_bonus_claimed = 1 WHERE telegram_id = ?", (referrer_id,))
                    await db_conn.commit()
                try:
                    await message.bot.send_message(
                        referrer_id, 
                        "🎉 Поздравляем! По вашей реферальной ссылке зарегистрировалось 5 человек, вам начислен 1 месяц подписки в подарок!"
                    )
                except Exception:
                    pass

    if user and user.get("group_id"):
        await save_user_safe(user_id, user.get("course"), user.get("group_name"), user.get("group_id"), message.from_user.username, message.from_user.first_name, referrer_id)
        await message.answer(
            f"С возвращением 👋\n"
            f"Группа: <b>{user['group_name']}</b>\n\n"
            f"Жми кнопки внизу — расписание на сегодня, завтра или всю неделю.",
            reply_markup=kb.main_menu_keyboard(),
        )
        return

    courses = await db.get_courses()
    if not courses:
        await message.answer("База расписания пока пуста.\n\nПопросите администратора выполнить /update.")
        return

    await state.set_state(Registration.choosing_course)
    await message.answer(
        "Привет! Это расписание филиала Губкина в Ташкенте.\n\n"
        "Шаг 1 из 2 — выбери курс:",
        reply_markup=kb.courses_keyboard(courses),
    )


async def save_user_safe(uid, course, group_name, group_id, username, first_name, ref_id=None):
    await db.save_user(uid, course, group_name, group_id, username, first_name, referrer_id=ref_id)


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
    
    # Получим существующего пользователя на предмет реферала
    existing = await db.get_user(u.id)
    ref_id = existing.get("referrer_id") if existing else None

    await db.save_user(u.id, course, group_name, group_id, username=u.username, first_name=u.first_name, referrer_id=ref_id)
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
    await message.answer("Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses))


@router.message(F.text.in_({"💳 Подписка"}))
@router.callback_query(F.data == "menu:sub")
async def subscription_menu(event: Message | CallbackQuery) -> None:
    user_id = event.from_user.id
    user = await db.get_user(user_id)
    active = await has_active_subscription(user_id)
    
    status_text = "❌ <b>Неактивна</b>"
    if active and user and user.get("subscription_expires"):
        try:
            exp = datetime.fromisoformat(user["subscription_expires"])
            status_text = f"✅ <b>Активна до {exp.strftime('%d.%m.%Y %H:%M')}</b>"
        except Exception:
            status_text = "✅ <b>Активна</b>"

    text = (
        f"💳 <b>Управление подпиской</b>\n\n"
        f"Статус: {status_text}\n\n"
        f"✨ Что дает подписка:\n"
        f"• Полное расписание с временем, аудиториями и преподавателями\n"
        f"• Уведомления-напоминания о парах в личку\n"
        f"• Никакой рекламы и мгновенный доступ к изменениям\n\n"
        f"Выберите подходящий тариф для продления или покупки:"
    )
    
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb.subscription_tariffs_keyboard())
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb.subscription_tariffs_keyboard())


@router.callback_query(F.data.startswith("sub:buy:"))
async def process_buy_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    days = int(callback.data.split(":")[-1])
    tariff_names = {30: "1 месяц (30 дней)", 90: "3 месяца (90 дней)", 365: "1 год (365 дней)"}
    
    # Дефолтные цены и карта, если не заданы в админке
    card_info = await db.get_setting("pay_card") or "8600 0000 0000 0000 (Имя Фамилия)"
    prices = {
        30: await db.get_setting("price_30") or "10 000 сум",
        90: await db.get_setting("price_90") or "25 000 сум",
        365: await db.get_setting("price_365") or "70 000 сум",
    }
    price = prices.get(days, "уточняйте")

    await state.update_data(tariff_days=days)
    await state.set_state(UserCheckUpload.waiting_photo)

    text = (
        f"📦 Вы выбрали тариф: <b>{tariff_names.get(days, f'{days} дней')}</b>\n"
        f"💵 К оплате: <b>{price}</b>\n\n"
        f"💳 Реквизиты для перевода:\n<code>{card_info}</code>\n\n"
        f"📸 Пожалуйста, совершите перевод и отправьте в ответный чат <b>скриншот или фото чека</b>.\n"
        f"После проверки администратором подписка активируется автоматически!"
    )
    await callback.message.edit_text(text, reply_markup=kb.admin_back_keyboard("menu:sub"))
    await callback.answer()


@router.message(UserCheckUpload.waiting_photo, F.photo | F.document)
async def receive_payment_check(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    days = data.get("tariff_days")
    if not days:
        await state.clear()
        await message.answer("Ошибка сессии. Нажмите /start")
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    check_id = await db.create_pending_check(message.from_user.id, days, file_id)
    await state.clear()

    await message.answer("✅ Чек успешно отправлен администраторам! Ожидайте подтверждения, обычно это занимает немного времени.")

    # Рассылка админам
    admin_text = (
        f"🔔 <b>Новый чек на проверку! (#ID {check_id})</b>\n\n"
        f"👤 От: <a href='tg://user?id={message.from_user.id}'>{message.from_user.first_name}</a> (@{message.from_user.username or 'нет'})\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"📦 Тариф: <b>{days} дней</b>"
    )
    
    admins = _parse_admin_ids(await db.get_setting("extra_admins"))
    admins.add(ADMIN_ID)
    
    for adm_id in admins:
        try:
            await message.bot.send_photo(
                adm_id, 
                photo=file_id, 
                caption=admin_text, 
                reply_markup=kb.admin_sub_approval_keyboard(check_id)
            )
        except Exception:
            try:
                await message.bot.send_document(
                    adm_id,
                    document=file_id,
                    caption=admin_text,
                    reply_markup=kb.admin_sub_approval_keyboard(check_id)
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("adm:sub:"))
async def admin_handle_sub_check(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[2] # ok или no
    check_id = int(parts[3])

    check_data = await db.get_pending_check(check_id)
    if not check_data or check_data["status"] != "pending":
        await callback.answer("Этот чек уже обработан или не найден!", show_alert=True)
        return

    await db.update_pending_check_status(check_id, "approved" if action == "ok" else "rejected")
    target_user_id = check_data["telegram_id"]
    days = check_data["tariff_days"]

    if action == "ok":
        new_exp_str = await db.add_subscription_days(target_user_id, days)
        
        # Проверим реферальную покупку: если юзера кто-то пригласил и он купил подписку первый раз, дадим рефереру неделю
        user_info = await db.get_user(target_user_id)
        if user_info and user_info.get("referrer_id"):
            ref_id = user_info["referrer_id"]
            # Проверим, давали ли уже за него бонус покупки (можно завести поле или просто начислять при каждой покупке — «если перейдут и купят»)
            await db.add_subscription_days(ref_id, 7)
            try:
                await callback.bot.send_message(ref_id, "🎁 По вашей реферальной ссылке пользователь приобрел подписку! Вам начислен бонус — 1 неделя доступа.")
            except Exception:
                pass

        try:
            await callback.bot.send_message(
                target_user_id,
                f"✅ <b>Ваша оплата подтверждена!</b>\nПодписка успешно продлена/активирована до: <b>{new_exp_str}</b> 🎉"
            )
        except Exception:
            pass

        await callback.message.edit_caption(
            caption=callback.message.caption + f"\n\n✅ <b>Одобрено администратором</b> @{callback.from_user.username or callback.from_user.first_name}",
            reply_markup=None
        )
        await callback.answer("Доступ успешно выдан!")
    else:
        try:
            await callback.bot.send_message(
                target_user_id,
                "❌ <b>К сожалению, ваш чек был отклонен администратором.</b>\nЕсли произошла ошибка, обратитесь в поддержку."
            )
        except Exception:
            pass

        await callback.message.edit_caption(
            caption=callback.message.caption + f"\n\n❌ <b>Отклонено администратором</b> @{callback.from_user.username or callback.from_user.first_name}",
            reply_markup=None
        )
        await callback.answer("Чек отклонен.")


@router.message(F.text.in_({"👥 Рефералы"}))
async def referral_menu(message: Message) -> None:
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    invited = user.get("invited_count", 0) if user else 0
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"

    text = (
        f"👥 <b>Реферальная система</b>\n\n"
        f"Приглашайте друзей по своей ссылке и получайте бесплатную подписку:\n"
        f"• Пригласите <b>5 человек</b> (которые запустят бота) → получите <b>1 месяц</b> подписки!\n"
        f"• Если ваш друг <b>приобретет подписку</b> → вам начисляется бонусная <b>неделя</b>!\n\n"
        f"📊 Ваши приглашенные друзья: <b>{invited} / 5</b>\n\n"
        f"🔗 Ваша реферальная ссылка:\n<code>{ref_link}</code>"
    )
    await message.answer(text)


@router.message(F.text.in_({"📅 Сегодня", "📅 На сегодня"}))
async def today_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    await send_day_schedule(message, user["group_name"], user["group_id"], datetime.now(TZ).weekday())


@router.message(F.text.in_({"🌅 Завтра", "📆 На завтра"}))
async def tomorrow_schedule(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    weekday = (datetime.now(TZ) + timedelta(days=1)).weekday()
    await send_day_schedule(message, user["group_name"], user["group_id"], weekday)


@router.message(F.text.in_({"🗓 Неделя", "🗓 На неделю"}))
async def week_schedule(message: Message) -> None:
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user or not user.get("group_id"):
        await message.answer("Сначала выбери группу командой /start")
        return
    
    if not await has_active_subscription(user_id):
        text = (
            "🔒 <b>Полная недельная сетка расписания доступна только по подписке!</b>\n\n"
            "Приобретите подписку, чтобы видеть все пары, время и кабинеты на всю неделю вперед."
        )
        kb_pay = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💳 Купить подписку", callback_data="menu:sub")]]
        )
        await message.answer(text, reply_markup=kb_pay)
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
    await message.answer(f"Официальное расписание:\n{SCHEDULE_URL}\n\nТвоя группа в боте: <b>{group_name}</b>")


@router.message(F.text.in_({"💬 Админ", "Связь с админом"}))
async def contact_admin(message: Message) -> None:
    if is_admin(message.from_user.id):
        await message.answer(
            "<b>Админ-панель</b>\n\nВыбери действие:",
            reply_markup=kb.admin_panel_keyboard(is_owner=is_owner(message.from_user.id)),
        )
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Написать админу", url=ADMIN_URL)]])
    await message.answer("Связь с админом:", reply_markup=markup)


@router.message(F.text.in_({"ℹ️ Помощь", "/help"}))
async def help_text(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться</b>\n\n"
        "📅 Сегодня — пары на этот день\n"
        "🌅 Завтра — пары на следующий день\n"
        "🗓 Неделя — пн–вс текущей недели\n"
        "💳 Подписка — управление тарифами\n"
        "👥 Рефералы — бонусные дни за друзей\n"
        "Группу менять в ⚙️ Настройки"
    )


def _settings_text(prefs: dict, group_name: str | None) -> str:
    on = bool(prefs.get("reminders_on", 1))
    minutes = int(prefs.get("remind_minutes") or 5)
    status = "включены" if on else "выключены"
    lines = ["<b>Настройки</b>", "", f"Группа: <b>{group_name or 'не выбрана'}</b>", f"Напоминания: <b>{status}</b>"]
    if on:
        lines.append(f"Писать за <b>{minutes} мин</b> до пары")
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
        reply_markup=kb.settings_keyboard(bool(prefs["reminders_on"]), int(prefs["remind_minutes"])),
    )


@router.callback_query(F.data == "set:close")
async def settings_close(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("Закрыто.")
    await callback.answer()


@router.callback_query(F.data == "set:group")
async def settings_change_group(callback: CallbackQuery, state: FSMContext) -> None:
    courses = await db.get_courses()
    if not courses:
        await callback.answer("База пуста", show_alert=True)
        return
    await state.set_state(Registration.choosing_course)
    await callback.message.edit_text("Шаг 1 из 2 — выбери курс:", reply_markup=kb.courses_keyboard(courses))
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
        reply_markup=kb.settings_keyboard(bool(prefs["reminders_on"]), int(prefs["remind_minutes"])),
    )
    await callback.answer()


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


async def _update_one_group(course: str, group_name: str, group_id: int, semaphore: asyncio.Semaphore, counters: dict, total: int, status_message: Message | None) -> None:
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
        except Exception as exc:
            counters["errors"] += 1
            logger.exception("ERROR for %s: %s", group_name, exc)

        counters["done"] += 1
        done = counters["done"]
        if status_message and (done == 1 or done % 5 == 0 or done == total):
            try:
                await status_message.edit_text(
                    f"⏳ <b>Обновляю расписание...</b>\n\nОбработано: {done}/{total}\n✅ Успешно: {counters['updated']}"
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

    counters = {"updated": 0, "empty": 0, "errors": 0, "done": 0, "auth_failed": 0, "format_failed": 0}
    semaphore = asyncio.Semaphore(UPDATE_CONCURRENCY)
    tasks = [
        _update_one_group(c, g, gid, semaphore, counters, total, status_message)
        for c, g, gid in groups_data.GROUPS
    ]
    await asyncio.gather(*tasks)
    return f"✅ <b>Обновление завершено!</b>\nВсего: {total}, Успешно: {counters['updated']}, Ошибок: {counters['errors']}"


# --- Админ-панель и управление подписками ---

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


@router.callback_query(F.data == "adm:sub_manage")
async def adm_sub_manage(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminManualSub.waiting_data)
    await callback.message.edit_text(
        "🔑 <b>Ручное управление подпиской</b>\n\n"
        "Отправьте данные в формате:\n"
        "<code>TELEGRAM_ID КОЛИЧЕСТВО_ДНЕЙ</code>\n"
        "<i>(Чтобы забрать подписку, укажите отрицательное число дней, например: 123456789 -30)</i>\n\n"
        "Отмена: /cancel",
        reply_markup=kb.admin_back_keyboard("adm:panel")
    )
    await callback.answer()


@router.message(AdminManualSub.waiting_data)
async def adm_manual_sub_apply(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
        await message.answer("❌ Ошибка формата. Пример:\n<code>123456789 30</code>\nПопробуйте снова или /cancel")
        return

    uid = int(parts[0])
    days = int(parts[1])
    await state.clear()

    res_date = await db.add_subscription_days(uid, days)
    await message.answer(f"✅ Успешно! Пользователю <code>{uid}</code> обновлена подписка. Новый срок: {res_date}", reply_markup=kb.admin_back_keyboard("adm:panel"))
    
    try:
        await message.bot.send_message(uid, f"✨ Администратор изменил срок вашей подписки. Действует до: <b>{res_date}</b>")
    except Exception:
        pass


@router.callback_query(F.data == "adm:pay_settings")
async def adm_pay_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminPaySettings.waiting_details)
    card = await db.get_setting("pay_card") or "не задана"
    p30 = await db.get_setting("price_30") or "10 000 сум"
    p90 = await db.get_setting("price_90") or "25 000 сум"
    p365 = await db.get_setting("price_365") or "70 000 сум"

    await callback.message.edit_text(
        f"💳 <b>Текущие реквизиты и цены:</b>\n\n"
        f"Карта: <code>{card}</code>\n"
        f"30 дней: {p30}\n"
        f"90 дней: {p90}\n"
        f"365 дней: {p365}\n\n"
        f"Отправьте новые данные в формате:\n"
        f"<code>КАРТА | ЦЕНА_30 | ЦЕНА_90 | ЦЕНА_365</code>\n"
        f"Пример:\n<code>8600123456789012 ФИО | 15000 | 35000 | 80000</code>\n\n"
        f"Отмена: /cancel",
        reply_markup=kb.admin_back_keyboard("adm:panel")
    )
    await callback.answer()


@router.message(AdminPaySettings.waiting_details)
async def adm_pay_settings_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    text = message.text or ""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 4:
        await message.answer("❌ Неверный формат (нужно ровно 4 части через |). Попробуйте снова или /cancel")
        return

    await db.set_setting("pay_card", parts[0])
    await db.set_setting("price_30", parts[1])
    await db.set_setting("price_90", parts[2])
    await db.set_setting("price_365", parts[3])
    await state.clear()
    await message.answer("✅ Реквизиты и цены успешно обновлены!", reply_markup=kb.admin_back_keyboard("adm:panel"))


@router.callback_query(F.data == "adm:stats")
async def adm_stats_root(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    count = await db.count_users()
    await callback.message.edit_text(f"<b>📊 Статистика</b>\n\nВсего пользователей: <b>{count}</b>", reply_markup=kb.admin_stats_root_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:update")
async def adm_update(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    status = await callback.message.answer("⏳ <b>Обновляю расписание...</b>")
    result = await run_update(status)
    try:
        await status.edit_text(result)
    except Exception:
        pass


@router.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminBroadcast.waiting_text)
    await callback.message.edit_text("Напиши текст рассылки одним сообщением.\nОтмена: /cancel", reply_markup=kb.admin_back_keyboard("adm:panel"))
    await callback.answer()


@router.message(AdminBroadcast.waiting_text)
async def broadcast_send(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    text = (message.html_text or message.text or "").strip()
    await state.clear()
    users = await db.get_all_users()
    status = await message.answer(f"Рассылаю {len(users)} чел...")
    ok = 0
    for user in users:
        try:
            await message.bot.send_message(user["telegram_id"], text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await status.edit_text(f"Готово. Доставлено: {ok}")


# --- Управление админами ---

def _admins_text() -> str:
    lines = [f"👑 Владелец: <code>{ADMIN_ID}</code>"]
    if _extra_admins:
        lines.append("")
        lines.append("Админы:")
        for uid in sorted(_extra_admins):
            lines.append(f"• <code>{uid}</code>")
    else:
        lines.append("\nДополнительных админов нет.")
    return "<b>👥 Админы</b>\n\n" + "\n".join(lines)


@router.callback_query(F.data == "adm:admins")
async def adm_admins(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(_admins_text(), reply_markup=kb.admin_back_keyboard("adm:panel"))
    await callback.answer()


@router.callback_query(F.data == "adm:addadmin")
async def adm_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    await state.set_state(AdminAddAdmin.waiting_id)
    await callback.message.edit_text(
        "➕ <b>Добавить админа</b>\n\n"
        "Отправь Telegram ID пользователя (число) "
        "или перешли любое его сообщение.\n\nОтмена: /cancel",
        reply_markup=kb.admin_back_keyboard("adm:panel"),
    )
    await callback.answer()


@router.message(AdminAddAdmin.waiting_id)
async def adm_add_admin_apply(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    new_id = None
    fwd = getattr(message, "forward_from", None)
    if fwd is None and getattr(message, "forward_origin", None) is not None:
        sender = getattr(message.forward_origin, "sender_user", None)
        fwd = sender
    if fwd is not None:
        new_id = fwd.id
    else:
        text = (message.text or "").strip()
        if text.isdigit():
            new_id = int(text)
    if not new_id:
        await message.answer("❌ Нужен числовой Telegram ID. Попробуй снова или /cancel")
        return
    if new_id == ADMIN_ID:
        await message.answer("Это владелец, он уже админ.")
        return
    _extra_admins.add(new_id)
    await save_extra_admins()
    await state.clear()
    await message.answer(
        f"✅ Админ <code>{new_id}</code> добавлен.",
        reply_markup=kb.admin_back_keyboard("adm:panel"),
    )


@router.callback_query(F.data == "adm:deladmin")
async def adm_del_admin(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    if not _extra_admins:
        await callback.answer("Дополнительных админов нет", show_alert=True)
        return
    await callback.message.edit_text(
        "➖ <b>Убрать админа</b>\n\nНажми на ID, которого нужно убрать:",
        reply_markup=kb.admin_remove_keyboard(sorted(_extra_admins)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:deladmin:"))
async def adm_del_admin_apply(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    uid = int(callback.data.split(":")[-1])
    _extra_admins.discard(uid)
    await save_extra_admins()
    await callback.answer(f"Админ {uid} убран")
    if _extra_admins:
        await callback.message.edit_text(
            "➖ <b>Убрать админа</b>\n\nНажми на ID, которого нужно убрать:",
            reply_markup=kb.admin_remove_keyboard(sorted(_extra_admins)),
        )
    else:
        await callback.message.edit_text(
            _admins_text(), reply_markup=kb.admin_back_keyboard("adm:panel")
        )


# --- Статистика: подразделы ---

@router.callback_query(F.data == "adm:stats:courses")
async def adm_stats_courses(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    users = await db.get_all_users()
    counts: dict[str, int] = {}
    for u in users:
        counts[u.get("course") or "—"] = counts.get(u.get("course") or "—", 0) + 1
    rows = sorted(counts.items())
    await callback.message.edit_text(
        "<b>📚 По курсам</b>", reply_markup=kb.admin_stats_courses_keyboard(rows)
    )
    await callback.answer()


async def _group_rows() -> list[tuple[str, str, int]]:
    users = await db.get_all_users()
    counts: dict[tuple[str, str], int] = {}
    for u in users:
        key = (u.get("course") or "—", u.get("group_name") or "—")
        counts[key] = counts.get(key, 0) + 1
    return [(c, g, n) for (c, g), n in sorted(counts.items())]


@router.callback_query(F.data == "adm:stats:groups")
async def adm_stats_groups(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "<b>👥 По группам</b>",
        reply_markup=kb.admin_stats_groups_keyboard(await _group_rows()),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:stats:c:"))
async def adm_stats_course(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    safe = callback.data.split(":", 3)[3]
    rows = await _group_rows()
    course = next((c for c, _, _ in rows if c.replace(":", "_")[:40] == safe), None)
    if course is None:
        await callback.answer("Курс не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"<b>Курс {course}</b>",
        reply_markup=kb.admin_stats_groups_keyboard(rows, course_filter=course),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:stats:g:"))
async def adm_stats_group(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    key = callback.data.split(":", 3)[3]
    course, _, group_name = key.partition("|")
    users = [
        u for u in await db.get_all_users()
        if (u.get("group_name") or "").startswith(group_name)
        and (u.get("course") or "").startswith(course)
    ]
    lines = [f"• {u.get('first_name') or '—'} @{u.get('username') or '—'} (<code>{u['telegram_id']}</code>)" for u in users[:60]]
    text = f"<b>Группа {group_name}</b> — {len(users)} чел.\n\n" + "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=kb.admin_back_keyboard("adm:stats:groups"))
    await callback.answer()


@router.callback_query(F.data == "adm:stats:all")
async def adm_stats_all(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    users = await db.get_all_users()
    lines = [f"• {u.get('first_name') or '—'} @{u.get('username') or '—'} · {u.get('group_name') or '—'} (<code>{u['telegram_id']}</code>)" for u in users[-60:]]
    text = f"<b>📋 Пользователи</b> — всего {len(users)} (последние 60)\n\n" + "\n".join(lines)
    await callback.message.edit_text(text[:4000], reply_markup=kb.admin_back_keyboard("adm:stats"))
    await callback.answer()


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) and not await db.get_user(message.from_user.id):
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=kb.main_menu_keyboard() if not is_admin(message.from_user.id) else kb.admin_panel_keyboard(is_owner(message.from_user.id)))


# Напоминания (только для пользователей с активной подпиской)
async def _lesson_start_minutes(time_slot: str) -> int | None:
    minutes = db._time_slot_to_minutes(time_slot)
    return None if minutes >= 99999 else minutes


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
        if not await has_active_subscription(user["telegram_id"]):
            continue
        grouped.setdefault(user["group_id"], []).append(user)

    for group_id, group_users in grouped.items():
        lessons = await db.get_schedule_for_day(group_id, weekday)
        for lesson in lessons:
            if lesson.get("is_cancelled") in (1, "1", True):
                continue
            slot = site_parser.remap_slot(lesson.get("time_slot") or "")
            start = await _lesson_start_minutes(slot)
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
                already = await db.reminder_was_sent(user["telegram_id"], day_key, slot, subject)
                if already:
                    continue

                text = f"Через {lead} мин пара\n\n<b>{slot}</b>\n{subject}"
                if ltype:
                    text += f" ({ltype})"
                text += f"\n{room}"
                if teacher:
                    text += f"\n{teacher}"
                try:
                    await bot.send_message(user["telegram_id"], text)
                    await db.mark_reminder_sent(user["telegram_id"], group_id, day_key, slot, subject)
                except Exception:
                    pass
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
