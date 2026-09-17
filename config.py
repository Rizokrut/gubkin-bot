import os

from dotenv import load_dotenv


load_dotenv()


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "bot.db")

SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://gubkin.uz/ru/item/raspisanie-zaniatii",
)

ADMIN_URL = os.getenv(
    "ADMIN_URL",
    "https://t.me/gubkinhelp",
)

GOSSIP_CHANNEL_ID = os.getenv("GOSSIP_CHANNEL_ID", "-1002667144030").strip()
GOSSIP_CHANNEL_URL = os.getenv(
    "GOSSIP_CHANNEL_URL",
    "https://t.me/+XbVUN9inkQ4yZDAy",
)


if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. "
        "Добавьте BOT_TOKEN в Environment Variables Render."
    )


if ADMIN_ID == 0:
    raise RuntimeError(
        "Не задан ADMIN_ID. "
        "Добавьте ADMIN_ID в Environment Variables Render."
    )
