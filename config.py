import os
from dotenv import load_dotenv

# Загружает переменные из файла .env (при локальном запуске).
# На Render/Railway переменные окружения задаются в панели управления,
# и load_dotenv() просто ничего не найдёт — это нормально, ошибки не будет.
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BASE_URL = os.getenv("BASE_URL", "https://lk.gubkin.uz")
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Создайте файл .env рядом с main.py и добавьте туда "
        "строку BOT_TOKEN=ваш_токен_от_BotFather (или задайте переменную окружения "
        "BOT_TOKEN в панели Render/Railway)."
    )

if ADMIN_ID == 0:
    raise RuntimeError(
        "Не задан ADMIN_ID. Узнайте свой Telegram ID у бота @userinfobot и добавьте "
        "строку ADMIN_ID=ваш_id в файл .env (или в переменные окружения)."
    )
