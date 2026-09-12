import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Реальный сайт расписания.
BASE_URL = os.getenv(
    "BASE_URL",
    "https://lk.gubkin.ru",
)

# Оставлено для обратной совместимости (раньше был путь к локальному
# файлу SQLite). Сейчас база — Turso, но переменная никому не мешает,
# на случай если что-то её ещё использует.
DB_PATH = os.getenv(
    "DB_PATH",
    "bot.db",
)

# Turso (облачная SQLite-совместимая база) — чтобы пользователи и их
# выбранная группа НЕ пропадали после каждого деплоя на Render.
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Задайте BOT_TOKEN "
        "в переменных окружения Render."
    )

if ADMIN_ID == 0:
    raise RuntimeError(
        "Не задан ADMIN_ID. Задайте ADMIN_ID "
        "в переменных окружения Render."
    )

if not TURSO_DATABASE_URL:
    raise RuntimeError(
        "Не задан TURSO_DATABASE_URL. Задайте его в переменных окружения "
        "Render (Environment Group 'Gubkin' или напрямую в сервисе)."
    )

if not TURSO_AUTH_TOKEN:
    raise RuntimeError(
        "Не задан TURSO_AUTH_TOKEN. Задайте его в переменных окружения "
        "Render (Environment Group 'Gubkin' или напрямую в сервисе)."
    )
