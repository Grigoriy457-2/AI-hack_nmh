"""Проверка Telegram-источника из консоли (без БД), для подготовки к демо.

    python -m app.integrations.telegram                     # провайдер из TG_PROVIDER, TG_SOURCES
    python -m app.integrations.telegram --provider web @telegram @durov
    python -m app.integrations.telegram --provider telethon @some_public_chat --listen

Печатает подключённые источники и последние сообщения; ``--listen`` — новые сообщения в
реальном времени (Ctrl+C — выход).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.integrations.telegram.client import TelegramError, TgMessage, build_telegram_client


def _line(m: TgMessage) -> str:
    text = m.text.replace("\n", " / ")
    return f"[{m.sent_at:%Y-%m-%d %H:%M:%S}Z] {m.chat_external_id} #{m.external_id} {m.author_name}: {text[:140]}"


async def _main(args: argparse.Namespace) -> int:
    client = build_telegram_client(args.provider)
    print(f"provider: {client.name}")
    try:
        for ref in args.sources:
            try:
                chat = await client.add_source(ref)
                print(f"+ {chat.external_id}: {chat.title} ({chat.kind}, {chat.members_count})")
            except TelegramError as e:
                print(f"! {ref}: {e}")
        for chat in await client.list_chats():
            msgs = await client.fetch_messages(chat.external_id, limit=args.limit)
            print(f"\n== {chat.title} [{chat.external_id}] — {len(msgs)} сообщ.")
            for m in msgs[-args.limit:]:
                print("  " + _line(m))
        if client.last_error:
            print(f"\nlast_error: {client.last_error}")
        if args.listen:
            print("\nЖдём новые сообщения (Ctrl+C — выход)...")

            async def show(m: TgMessage) -> None:
                print("NEW " + _line(m), flush=True)

            await client.listen(show)
    except TelegramError as e:
        print(f"Ошибка: {e}")
        return 1
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    p = argparse.ArgumentParser(prog="python -m app.integrations.telegram")
    p.add_argument("sources", nargs="*", help="@username или t.me-ссылки")
    p.add_argument("--provider", choices=["auto", "telethon", "bot", "web", "mock"], default=None)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--listen", action="store_true")
    try:
        sys.exit(asyncio.run(_main(p.parse_args())))
    except KeyboardInterrupt:
        pass
