import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Страница расписания филиала.
BASE_URL = os.getenv(
    "BASE_URL",
    "https://gubkin.uz/ru/item/raspisanie-zaniatii",
)
SCHEDULE_URL = "https://gubkin.uz/ru/item/raspisanie-zaniatii"
ADMIN_URL = "https://t.me/rllzo"

DB_PATH = os.getenv(
    "DB_PATH",
    "bot.db",
)

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

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
