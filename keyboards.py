from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def courses_keyboard(courses: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for course in courses:
        builder.button(text=course, callback_data=f"course:{course}")
    builder.adjust(2)
    return builder.as_markup()


def groups_keyboard(groups: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    """groups: список (group_name, group_id). callback_data хранит только id —
    короткий и безопасный для лимита Telegram в 64 байта."""
    builder = InlineKeyboardBuilder()
    for group_name, group_id in groups:
        builder.button(text=group_name, callback_data=f"group:{group_id}")
    builder.button(text="⬅️ Назад", callback_data="back_to_course")
    builder.adjust(2)
    return builder.as_markup()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📅 Сегодня")
    builder.button(text="🌅 Завтра")
    builder.button(text="🗓 Неделя")
    builder.button(text="ℹ️ Помощь")
    builder.button(text="🔗 Сайт")
    builder.button(text="👤 Группа")
    builder.button(text="⚙️ Настройки")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)


def settings_keyboard(reminders_on: bool, minutes: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if reminders_on:
        builder.button(text="🔔 Напоминания: вкл", callback_data="set:rem:off")
        for m in (5, 10, 15, 20):
            mark = "✓ " if minutes == m else ""
            builder.button(text=f"{mark}за {m} мин", callback_data=f"set:min:{m}")
        builder.adjust(1, 2, 2)
    else:
        builder.button(text="🔕 Напоминания: выкл", callback_data="set:rem:on")
        builder.adjust(1)
    builder.button(text="👤 Сменить группу", callback_data="set:group")
    builder.button(text="Закрыть", callback_data="set:close")
    return builder.as_markup()
