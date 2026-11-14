"""Фоновая работа Telegram-источника в lifespan (зона DEV-TELEGRAM).

``start_telegram_sync(interval)`` → две asyncio-задачи:
1. ``telegram-listen`` — ``client.listen()`` (Telethon events / Bot API long polling / опрос
   web-превью); на каждое новое сообщение «пинает» синхронизацию;
2. ``telegram-sync`` — запись в БД через общий поллер MAX: ``poller.sync_now(client)`` — сразу
   после пинка (с дебаунсом ~1 с) или раз в ``interval`` секунд как страховка.
Так сообщения Telegram попадают в те же ``max_chats``/``chat_messages`` и дальше — в Радар,
кластеризацию, уникальность и пайплайн, ровно как MAX. Ошибки логируются, задачи не падают.

Отмена: ``stop_telegram_sync(tasks)`` в shutdown lifespan.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.integrations.telegram.client import (
    TelegramClient,
    TelegramConfigError,
    TgMessage,
    get_telegram_client,
)

log = logging.getLogger("newsflow.telegram")

SyncFn = Callable[[TelegramClient], Awaitable[Any]]


async def _default_sync(client: TelegramClient) -> Any:
    from app.integrations.max.poller import sync_now

    return await sync_now(client)


def start_telegram_sync(interval: int | None = None, *, client: TelegramClient | None = None,
                        sync: SyncFn | None = None,
                        on_message: Callable[[TgMessage], Awaitable[None]] | None = None,
                        ) -> list[asyncio.Task]:
    """Запустить listener + sync-цикл. ``sync`` — функция записи в БД (по умолчанию
    ``app.integrations.max.poller.sync_now``); ``on_message`` — доп. хук (напр. SSE-пуш)."""
    from app.integrations.telegram.settings import get_tg_settings

    client = client or get_telegram_client()
    interval = max(int(interval or get_tg_settings().TG_POLL_INTERVAL), 3)
    sync = sync or _default_sync
    kick = asyncio.Event()

    async def _on_message(m: TgMessage) -> None:
        kick.set()
        if on_message is not None:
            await on_message(m)

    async def _listen() -> None:
        backoff = interval
        while True:
            try:
                await client.listen(_on_message)
            except asyncio.CancelledError:
                raise
            except TelegramConfigError as e:
                log.error("Telegram listener (%s) остановлен: %s", client.name, e)
                client.last_error = str(e)
                return  # без сессии/токена реального времени нет; sync-цикл покажет ошибку
            except Exception:  # noqa: BLE001
                log.exception("Telegram listener (%s) упал, перезапуск через %ss", client.name, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def _sync_loop() -> None:
        while True:
            try:
                await asyncio.wait_for(kick.wait(), timeout=interval)
                await asyncio.sleep(1.0)  # дебаунс пачки сообщений
            except TimeoutError:
                pass
            kick.clear()
            try:
                res = await sync(client)
                new = getattr(res, "new_messages", 0)
                errors = getattr(res, "errors", None)
                if new or errors:
                    log.info("Telegram sync (%s): new=%s errors=%s", client.name, new, errors)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Telegram sync failed")

    log.info("Telegram source started: provider=%s, interval=%ss", client.name, interval)
    return [
        asyncio.create_task(_listen(), name="telegram-listen"),
        asyncio.create_task(_sync_loop(), name="telegram-sync"),
    ]


async def stop_telegram_sync(tasks: list[asyncio.Task], client: TelegramClient | None = None) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    try:
        await (client or get_telegram_client()).aclose()
    except Exception:  # noqa: BLE001
        log.debug("Telegram client close failed", exc_info=True)
