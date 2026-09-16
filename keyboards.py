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
    builder.button(text="🔗 Сайт")
    builder.button(text="💬 Админ")
    builder.button(text="⚙️ Настройки")
    builder.adjust(2, 2, 2)
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


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================

def admin_panel_keyboard(is_owner: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="adm:stats")
    builder.button(text="🔄 Обновить расписание", callback_data="adm:update")
    builder.button(text="📢 Рассылка", callback_data="adm:broadcast")
    builder.button(text="👥 Админы", callback_data="adm:admins")
    builder.button(text="📤 Экспорт пользователей", callback_data="adm:export")
    builder.button(text="📥 Импорт пользователей", callback_data="adm:import")
    if is_owner:
        builder.button(text="➕ Добавить админа", callback_data="adm:addadmin")
        builder.button(text="➖ Убрать админа", callback_data="adm:deladmin")
    builder.button(text="❌ Закрыть", callback_data="adm:close")
    # 1 в ряд для основных, 2 для экспорта/импорта и add/del, 1 для закрыть
    if is_owner:
        builder.adjust(1, 1, 1, 1, 2, 2, 1)
    else:
        builder.adjust(1, 1, 1, 1, 2, 1)
    return builder.as_markup()


def admin_stats_root_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📚 По курсам", callback_data="adm:stats:courses")
    builder.button(text="👥 По группам", callback_data="adm:stats:groups")
    builder.button(text="📋 Все пользователи", callback_data="adm:stats:all")
    builder.button(text="⬅️ Назад", callback_data="adm:panel")
    builder.adjust(1)
    return builder.as_markup()


def admin_stats_courses_keyboard(courses: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    """courses: (course_name, count)"""
    builder = InlineKeyboardBuilder()
    for course, count in courses:
        label = f"{course} · {count}"
        # callback короткий: course name может быть длинным, кодируем безопасно
        safe = course.replace(":", "_")[:40]
        builder.button(text=label, callback_data=f"adm:stats:c:{safe}")
    builder.button(text="⬅️ Назад", callback_data="adm:stats")
    builder.adjust(1)
    return builder.as_markup()


def admin_stats_groups_keyboard(
    groups: list[tuple[str, str, int]],
    course_filter: str | None = None,
) -> InlineKeyboardMarkup:
    """groups: (course, group_name, count). Если course_filter — только этот курс.
    В callback кладём course|group_name (укороченно при необходимости)."""
    builder = InlineKeyboardBuilder()
    for course, group_name, count in groups:
        if course_filter and course != course_filter:
            continue
        label = f"{group_name} · {count}"
        key = f"{course}|{group_name}"
        if len(key.encode("utf-8")) > 40:
            # хеш-подобный короткий ключ: первые символы + длина
            key = f"{course[:12]}|{group_name[:18]}"
        builder.button(text=label, callback_data=f"adm:stats:g:{key}")
    back = "adm:stats:courses" if course_filter else "adm:stats"
    builder.button(text="⬅️ Назад", callback_data=back)
    builder.adjust(1)
    return builder.as_markup()


def admin_back_keyboard(callback: str = "adm:panel") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=callback)
    return builder.as_markup()
