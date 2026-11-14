"""Юнит: MaxBotApiClient на фейковом HTTP (httpx.MockTransport), устойчивость sync_once к ошибкам
сети, жизненный цикл poller (start/cancel) и lifespan с включённым poller."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.integrations.max.bot_api import MaxBotApiClient
from app.integrations.max.client import MaxClient
from app.models import ChatMessage, MaxChat as MaxChatRow

pytestmark = pytest.mark.unit


def ms(h: int, m: int) -> int:
    return int(datetime(2026, 11, 14, h, m, tzinfo=UTC).timestamp() * 1000)


MESSAGES = [
    {"sender": {"user_id": 11, "first_name": "Иван", "last_name": "Петров"}, "timestamp": ms(11, 31),
     "body": {"mid": "m1", "text": "Хлопок у дома 5 в 14:30",
              "attachments": [{"type": "image", "payload": {"url": "https://cdn.max/1.jpg"}}]}},
    {"sender": {"user_id": 12, "first_name": "Ольга"}, "timestamp": ms(11, 35),
     "link": {"type": "forward", "message": {"text": "Пересланное: хлопок у дома 5"}},
     "body": {"mid": "m2", "text": ""}},
    {"sender": {"user_id": 13, "first_name": "Пётр"}, "timestamp": ms(11, 40),
     "body": {"mid": "m3", "text": "Мой номер, звоните",
              "attachments": [{"type": "contact", "payload": {
                  "vcf_info": "BEGIN:VCARD\nTEL;TYPE=cell:+79161234567\nEND:VCARD",
                  "max_info": {"user_id": 13}}}]}},
    {"sender": {"user_id": 14}, "timestamp": ms(11, 41), "body": {"mid": "m4", "text": ""}},  # пустое
]


def handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("Authorization") == "tkn"
    p = request.url.path
    if p == "/chats":
        return httpx.Response(200, json={"chats": [
            {"chat_id": 100, "type": "chat", "status": "active", "title": "ЖК Чертаново"},
            {"chat_id": 200, "type": "dialog", "status": "active", "title": "личка"},
        ], "marker": None})
    if p == "/messages":
        return httpx.Response(200, json={"messages": MESSAGES})
    if p == "/chats/100/members":
        uid = request.url.params.get("user_ids")
        return httpx.Response(200, json={"members": [
            {"user_id": int(uid), "first_name": "Иван", "last_name": "Петров", "username": "ivan"}]
            if uid == "11" else []})
    return httpx.Response(404, json={"error": "not found"})


def bot() -> MaxBotApiClient:
    return MaxBotApiClient("tkn", "https://max.test", transport=httpx.MockTransport(handler))


async def test_bot_list_chats_filters_non_group():
    chats = await bot().list_chats()
    assert [c.external_id for c in chats] == ["100"]
    assert chats[0].title == "ЖК Чертаново"


async def test_bot_fetch_messages_parsing():
    c = bot()
    msgs = await c.fetch_messages("100")
    assert [m.external_id for m in msgs] == ["m1", "m2", "m3"], "пустое сообщение без медиа пропускается"
    m1, m2, m3 = msgs
    assert m1.author_name == "Иван Петров" and m1.has_media and m1.media_url == "https://cdn.max/1.jpg"
    assert m1.sent_at == datetime(2026, 11, 14, 11, 31) and m1.sent_at.tzinfo is None
    assert m2.is_forwarded and "Пересланное" in m2.text
    since = await c.fetch_messages("100", since=datetime(2026, 11, 14, 11, 35))
    assert [m.external_id for m in since] == ["m3"]


async def test_bot_profiles_phone_only_if_shared():
    c = bot()
    await c.fetch_messages("100")
    p11 = await c.get_user_profile("11")
    assert p11 and p11.name == "Иван Петров" and p11.phone is None
    p13 = await c.get_user_profile("13")
    assert p13 and p13.phone == "+79161234567"
    assert await c.get_user_profile("999") is None


class _BrokenClient(MaxClient):
    async def list_chats(self):
        raise httpx.ConnectError("no network")

    async def fetch_messages(self, chat_id, since=None, limit=100):
        raise httpx.ConnectError("no network")

    async def get_user_profile(self, user_id):
        raise httpx.ConnectError("no network")


async def test_sync_once_network_error_does_not_raise(db_ready, session):
    from app.integrations.max.poller import sync_once

    res = await sync_once(session, _BrokenClient())
    assert res.errors and res.new_messages == 0


async def test_bot_api_unreachable_does_not_raise(db_ready, session):
    from app.integrations.max.poller import sync_once

    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    c = MaxBotApiClient("tkn", "https://max.test", transport=httpx.MockTransport(boom))
    res = await sync_once(session, c)
    assert res.errors


async def test_sync_with_bot_client(db_ready, session):
    from app.integrations.max.poller import sync_once

    res = await sync_once(session, bot())
    assert res.chats == 1 and res.new_messages == 3
    again = await sync_once(session, bot())
    assert again.new_messages == 0
    assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 3


async def test_poller_runs_and_cancels(db_ready, monkeypatch):
    from app import db
    from app.integrations.max import poller

    task = poller.start_poller(1)
    assert isinstance(task, asyncio.Task)
    for _ in range(100):
        async with db.SessionLocal() as s:
            n = await s.scalar(select(func.count()).select_from(MaxChatRow))
        if n:
            break
        await asyncio.sleep(0.05)
    assert n >= 4, "poller не синхронизировал mock-MAX"
    task.cancel()
    try:
        await asyncio.wait_for(task, 5)
    except asyncio.CancelledError:
        pass
    assert task.done()


async def test_lifespan_with_poller_enabled(db_ready, monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "MAX_POLLER_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_POLL_INTERVAL", 1)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/health")).status_code == 200
        await asyncio.sleep(0.2)
    # выход из lifespan не зависает и не бросает
