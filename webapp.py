"""Веб-приложение (PWA): вход по коду из бота, API расписания, раздача файлов."""
from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path

import aiosqlite
from aiohttp import web

import database as db
from config import DB_PATH
from icons import ICON_180, ICON_512

BASE = Path(__file__).parent
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
DEVICE_RE = re.compile(r"[A-Za-z0-9-]{16,64}")
JUNK = (" - филиал в г.Ташкент", " — филиал в г.Ташкент", " филиал в г.Ташкент")

# Файлы, которые разрешено отдавать наружу (больше ничего из репозитория не видно)
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json; charset=utf-8"),
}


async def init_web_tables() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_tokens (
                token TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                device TEXT
            )
            """
        )
        cur = await conn.execute("PRAGMA table_info(web_tokens)")
        if "device" not in {row[1] for row in await cur.fetchall()}:
            await conn.execute("ALTER TABLE web_tokens ADD COLUMN device TEXT")
        await conn.commit()


async def create_token(telegram_id: int) -> str:
    """Новый код входа. Старые коды этого пользователя перестают работать."""
    code = "".join(secrets.choice(ALPHABET) for _ in range(10))
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM web_tokens WHERE telegram_id = ?", (telegram_id,))
        await conn.execute(
            "INSERT INTO web_tokens (token, telegram_id) VALUES (?, ?)", (code, telegram_id)
        )
        await conn.commit()
    return code


async def _check_token(token: str, device: str) -> tuple[int | None, str]:
    """Возвращает (telegram_id, статус). Статусы: ok, bad_token, bad_device.
    Первое устройство, которое использовало код, закрепляется за ним навсегда."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT telegram_id, device FROM web_tokens WHERE token = ?", (token,)
        )
        row = await cur.fetchone()
        if not row:
            return None, "bad_token"
        uid, bound = int(row[0]), row[1]
        if not bound:
            # атомарно: закрепляем, только если никто не успел раньше
            await conn.execute(
                "UPDATE web_tokens SET device = ? WHERE token = ? AND device IS NULL",
                (device, token),
            )
            await conn.commit()
            cur = await conn.execute("SELECT device FROM web_tokens WHERE token = ?", (token,))
            bound = (await cur.fetchone())[0]
        if bound != device:
            return None, "bad_device"
        return uid, "ok"


def _room(room: str) -> str:
    text = room or ""
    for junk in JUNK:
        text = text.replace(junk, "")
    return text.strip(" -—")


async def handle_schedule(request: web.Request) -> web.Response:
    headers = {"Cache-Control": "no-store"}
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip().upper() if auth.startswith("Bearer ") else ""
    device = request.headers.get("X-Device", "").strip()
    if not token:
        return web.json_response({"error": "bad_token"}, status=401, headers=headers)
    if not DEVICE_RE.fullmatch(device):
        return web.json_response({"error": "bad_device"}, status=403, headers=headers)
    uid, status = await _check_token(token, device)
    if status == "bad_token":
        return web.json_response({"error": "bad_token"}, status=401, headers=headers)
    if status == "bad_device":
        return web.json_response({"error": "bad_device"}, status=403, headers=headers)

    user = await db.get_user(uid)
    if not user or not user.get("group_id"):
        return web.json_response({"error": "no_group"}, status=404, headers=headers)

    if not await request.app["has_sub"](uid):
        return web.json_response({"error": "no_subscription"}, status=402, headers=headers)

    week = await db.get_schedule_for_week(user["group_id"])
    out = {}
    for wd, lessons in week.items():
        out[str(wd)] = [
            {
                "time": l.get("time_slot") or "",
                "subject": l.get("subject") or "",
                "type": l.get("lesson_type") or "",
                "room": _room(l.get("room") or ""),
                "teacher": l.get("teacher") or "",
                "cancelled": bool(l.get("is_cancelled")),
                "subgroup": int(l.get("subgroup") or 0),
            }
            for l in lessons
        ]
    return web.json_response(
        {
            "group": user.get("group_name") or "",
            "expires": user.get("subscription_expires") or "",
            "week": out,
        },
        headers=headers,
    )


def _file_handler(name: str, ctype: str):
    async def handler(request: web.Request) -> web.Response:
        path = BASE / name
        if not path.exists():
            return web.Response(status=404, text="not found")
        return web.Response(
            body=path.read_bytes(),
            content_type=ctype.split(";")[0],
            charset="utf-8" if "charset" in ctype else None,
            headers={"Cache-Control": "no-cache"},
        )

    return handler


def _icon_handler(b64: str):
    data = base64.b64decode(b64)

    async def handler(request: web.Request) -> web.Response:
        return web.Response(
            body=data, content_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
        )

    return handler


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def register(app: web.Application) -> None:
    app.router.add_get("/api/schedule", handle_schedule)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/icon-180.png", _icon_handler(ICON_180))
    app.router.add_get("/icon-512.png", _icon_handler(ICON_512))
    for url, (name, ctype) in FILES.items():
        app.router.add_get(url, _file_handler(name, ctype))
