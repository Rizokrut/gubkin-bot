from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def courses_keyboard(courses: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for course in courses:
        builder.button(text=course, callback_data=f"course:{course}")
    builder.adjust(2)
    return builder.as_markup()


def faculties_keyboard(faculties: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for faculty in faculties:
        builder.button(text=faculty, callback_data=f"faculty:{faculty}")
    builder.button(text="⬅️ Назад", callback_data="back_to_course")
    builder.adjust(1)
    return builder.as_markup()


def groups_keyboard(groups: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for group in groups:
        builder.button(text=group, callback_data=f"group:{group}")
    builder.button(text="⬅️ Назад", callback_data="back_to_faculty")
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
