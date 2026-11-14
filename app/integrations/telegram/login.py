"""Однократный интерактивный вход в Telegram для Telethon-провайдера (зона DEV-TELEGRAM).

Запускает САМ пользователь, один раз, в своей консоли:

    python -m app.integrations.telegram.login

Нужны TG_API_ID и TG_API_HASH (https://my.telegram.org → API development tools) в .env или
окружении. Скрипт спросит номер телефона, код из Telegram и (если включён) облачный пароль 2FA,
и сохранит сессию в TG_SESSION_PATH (по умолчанию data/telegram/newsflow.session).

Файл сессии даёт ПОЛНЫЙ доступ к аккаунту: не коммитьте его (``*.session`` в .gitignore),
не пересылайте. Отозвать: Telegram → Настройки → Устройства → завершить сеанс.
Рекомендуется отдельный (не личный) аккаунт редакции.
"""
from __future__ import annotations

import asyncio
import sys

from app.integrations.telegram.settings import get_tg_settings


async def _main() -> int:
    s = get_tg_settings()
    if not s.has_telethon_keys:
        print("Задайте TG_API_ID и TG_API_HASH (my.telegram.org → API development tools) в .env")
        return 2
    try:
        from telethon import TelegramClient
    except ImportError:
        print("Установите telethon: pip install telethon")
        return 2
    session = s.session_file
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session.with_suffix("")), int(s.TG_API_ID), s.TG_API_HASH)  # type: ignore[arg-type]
    # start() сам спросит телефон, код и пароль 2FA в консоли.
    await client.start(phone=lambda: input("Номер телефона (+7...): ").strip())
    me = await client.get_me()
    print(f"Готово: вошли как {me.first_name or ''} (@{me.username or '-'}). Сессия: {session}")
    print("Добавьте в .env: TG_PROVIDER=telethon и TG_SOURCES=@channel1,@channel2")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
