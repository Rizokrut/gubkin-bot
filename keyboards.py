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
    builder.button(text="📅 На сегодня")
    builder.button(text="📆 На завтра")
    builder.button(text="🗓 На неделю")
    builder.button(text="🔗 Ссылка на сайт")
    builder.button(text="⚙️ Сменить группу")
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True)
