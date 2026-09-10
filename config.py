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

DB_PATH = os.getenv(
    "DB_PATH",
    "bot.db",
)

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
