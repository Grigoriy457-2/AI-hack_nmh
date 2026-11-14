"""Юнит: пакет app/integrations/telegram (DEV-TELEGRAM) — без сети.

parse_tg_ref, идентификаторы, фабрика, MockTelegramClient, парсер веб-превью t.me/s на локальном
HTML-фрагменте, TelegramWebPreviewClient на httpx.MockTransport, poller.sync_once с TG-клиентом.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.ai import mock_data
from app.integrations.telegram import client as tgc
from app.integrations.telegram.client import (
    TelegramSourceError,
    chat_ext_id,
    is_tg_id,
    parse_tg_ref,
    strip_prefix,
)
from app.integrations.telegram.mock import MOCK_TG_MESSAGES, MockTelegramClient
from app.integrations.telegram.web_preview import (
    TelegramWebPreviewClient,
    parse_count,
    parse_preview_html,
)
from app.models import AuthorProfile, ChatMessage, MaxChat

pytestmark = pytest.mark.unit


# ================================================================ ссылки / id
@pytest.mark.parametrize("ref,expected", [
    ("@Moscow_Live", "moscow_live"), ("moscow_live", "moscow_live"), ("https://t.me/moscow_live", "moscow_live"),
    ("t.me/s/moscow_live", "moscow_live"), ("https://t.me/moscow_live/123", "moscow_live"),
    ("http://telegram.me/moscow_live", "moscow_live"), ("tg:moscow_live", "moscow_live"),
    ("  https://www.t.me/moscow_live?x=1  ", "moscow_live"),
])
def test_parse_tg_ref_ok(ref, expected):
    assert parse_tg_ref(ref) == expected


@pytest.mark.parametrize("ref", ["https://t.me/+AbCdEf123", "t.me/joinchat/AbCd", "+AbCdEf"])
def test_parse_tg_ref_private(ref):
    with pytest.raises(TelegramSourceError, match="(?i)приват"):
        parse_tg_ref(ref)


@pytest.mark.parametrize("ref", ["", "   ", "@ab", "1channel", "имя_канала", "https://t.me/", "a" * 40])
def test_parse_tg_ref_bad(ref):
    with pytest.raises(TelegramSourceError):
        parse_tg_ref(ref)


def test_ids():
    assert chat_ext_id("Moscow_Live") == "tg:moscow_live"
    assert chat_ext_id(None, -1001234) == "tg:-1001234"
    assert is_tg_id("tg:x") and not is_tg_id("chat_chert_48k2") and not is_tg_id(None)
    assert strip_prefix("tg:abc") == "abc" and strip_prefix("abc") == "abc"


def test_factory_mock_by_default():
    assert tgc.choose_provider() == "mock"
    assert tgc.choose_provider("web") == "web"
    tgc.set_telegram_client(None)
    try:
        c = tgc.get_telegram_client()
        assert isinstance(c, MockTelegramClient) and c.platform == "telegram"
        assert tgc.get_telegram_client() is c, "синглтон"
    finally:
        tgc.set_telegram_client(MockTelegramClient())


# ================================================================ MockTelegramClient
async def test_mock_tg_chats_and_messages():
    c = MockTelegramClient(include_future=True)
    chats = await c.list_chats()
    assert {ch.external_id for ch in chats} == {"tg:moscow_live_demo", "tg:srochno_yug_demo",
                                                "tg:chertanovo_online_demo"}
    assert {ch.kind for ch in chats} == {"channel", "group"}
    assert all(ch.url == f"https://t.me/{ch.username}" for ch in chats)
    total = []
    for ch in chats:
        msgs = await c.fetch_messages(ch.external_id)
        assert [m.sent_at for m in msgs] == sorted(m.sent_at for m in msgs)
        assert all(m.chat_external_id == ch.external_id and m.url.startswith("https://t.me/") for m in msgs)
        assert all(m.sent_at.tzinfo is None for m in msgs)
        total += msgs
    assert len(total) == 7 == len(MOCK_TG_MESSAGES)
    assert await c.fetch_messages("chat_chert_48k2") == [], "не-tg chat_id -> []"


async def test_mock_tg_cross_duplicate_text_and_timing():
    """Пост «Москва Live» дословно = пересылкам MAX m09/m10 и раньше их по времени."""
    c = MockTelegramClient(include_future=True)
    live = [m for m in await c.fetch_messages("tg:moscow_live_demo")]
    m09 = next(m for m in mock_data.SCENARIO_MAX_MESSAGES if m["external_id"] == "m09")
    m10 = next(m for m in mock_data.SCENARIO_MAX_MESSAGES if m["external_id"] == "m10")
    first = live[0]
    assert first.text == m09["text"] == m10["text"]
    anchor = mock_data.scenario_event_time()
    assert first.sent_at < anchor + timedelta(minutes=min(m09["minutes"], m10["minutes"]))


async def test_mock_tg_future_and_since():
    anchor = mock_data.scenario_event_time()
    c = MockTelegramClient(now=anchor + timedelta(minutes=20))
    msgs = [m for ch in await c.list_chats() for m in await c.fetch_messages(ch.external_id)]
    assert msgs and all(m.sent_at <= anchor + timedelta(minutes=20) for m in msgs), "из будущего не отдаются"
    c2 = MockTelegramClient(include_future=True)
    all_live = await c2.fetch_messages("tg:moscow_live_demo")
    newer = await c2.fetch_messages("tg:moscow_live_demo", since=all_live[0].sent_at)
    assert [m.external_id for m in newer] == [m.external_id for m in all_live[1:]]


async def test_mock_tg_profiles_and_sources():
    c = MockTelegramClient()
    p = await c.get_user_profile("tg:700101")
    assert p and p.phone and p.name == "Максим Рябов"
    assert (await c.get_user_profile("tg:700102")).phone is None
    assert await c.get_user_profile("tg:nobody") is None
    chat = await c.add_source("https://t.me/srochno_yug_demo")
    assert chat.external_id == "tg:srochno_yug_demo"
    with pytest.raises(TelegramSourceError):
        await c.add_source("@unknown_channel_qa")
    await c.remove_source("tg:srochno_yug_demo")
    assert "tg:srochno_yug_demo" not in {ch.external_id for ch in await c.list_chats()}


# ================================================================ парсер веб-превью
PREVIEW_HTML = """
<html><body>
<div class="tgme_channel_info">
  <div class="tgme_channel_info_header_title"><span>Тест  Канал &amp; Ко</span></div>
  <div class="tgme_channel_info_description">Описание канала</div>
  <div class="tgme_channel_info_counter"><span class="counter_value">12.5K</span>
     <span class="counter_type">subscribers</span></div>
  <div class="tgme_channel_info_counter"><span class="counter_value">300</span>
     <span class="counter_type">photos</span></div>
</div>
<div class="tgme_widget_message_wrap">
 <div class="tgme_widget_message" data-post="QaChan/101">
  <div class="tgme_widget_message_bubble">
   <div class="tgme_widget_message_reply"><div class="tgme_widget_message_text">ЦИТАТА ОТВЕТА</div>
      <time datetime="2020-01-01T00:00:00+00:00">old</time></div>
   <div class="tgme_widget_message_text">Хлопок в Чертанове &mdash; первая строка<br/>вторая   строка 🔥</div>
   <span class="tgme_widget_message_views">12.7K</span>
   <span class="tgme_widget_message_from_author">Иван Автор</span>
   <a class="tgme_widget_message_date" href="https://t.me/QaChan/101">
      <time datetime="2026-11-14T11:40:00+00:00">14:40</time></a>
  </div>
 </div>
</div>
<div class="tgme_widget_message_wrap">
 <div class="tgme_widget_message" data-post="QaChan/102">
  <div class="tgme_widget_message_forwarded_from">Forwarded from
     <a class="tgme_widget_message_forwarded_from_name">Другой канал</a></div>
  <a class="tgme_widget_message_photo_wrap" style="width:100px;background-image:url('https://cdn.example/p.jpg')"></a>
  <div class="tgme_widget_message_text">Фото с места</div>
  <span class="tgme_widget_message_views">3 100</span>
  <a class="tgme_widget_message_date" href="https://t.me/QaChan/102"><time datetime="2026-11-14T14:45:00+03:00">14:45</time></a>
 </div>
</div>
<div class="tgme_widget_message service_message" data-post="QaChan/103">
  <div class="tgme_widget_message_text">Channel created</div>
</div>
</body></html>
"""


def test_parse_count():
    assert parse_count("12.7K") == 12700 and parse_count("1.5M") == 1500000
    assert parse_count("3 100") == 3100 and parse_count("48") == 48
    assert parse_count(None) is None and parse_count("abc") is None


def test_parse_preview_html():
    page = parse_preview_html(PREVIEW_HTML)
    assert page.title == "Тест Канал & Ко"
    assert page.counter == 12500 and page.counter_type == "subscribers"
    assert page.description == "Описание канала"
    assert [p.message_id for p in page.posts] == [101, 102], "сервисное сообщение без времени отброшено"
    p1, p2 = page.posts
    assert p1.username == "qachan"
    assert p1.text == "Хлопок в Чертанове — первая строка\nвторая строка 🔥", repr(p1.text)
    assert "ЦИТАТА" not in p1.text, "цитата-ответ не попадает в текст"
    assert p1.sent_at == datetime(2026, 11, 14, 11, 40) and p1.sent_at.tzinfo is None
    assert p1.views == 12700 and p1.author_signature == "Иван Автор"
    assert p1.url == "https://t.me/QaChan/101" and not p1.is_forwarded and not p1.has_media
    assert p2.is_forwarded and p2.forwarded_from == "Другой канал"
    assert p2.has_media and p2.media_url == "https://cdn.example/p.jpg"
    assert p2.views == 3100
    assert p2.sent_at == datetime(2026, 11, 14, 11, 45), "время приводится к naive UTC"


def _transport(pages: dict[str, str], calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        path = request.url.path
        if path in pages:
            return httpx.Response(200, text=pages[path])
        if path == "/s/nopreview_qa":
            return httpx.Response(302, headers={"location": "https://t.me/nopreview_qa"})
        if path == "/nopreview_qa":
            return httpx.Response(200, text="<html>no preview</html>")
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_web_preview_client_offline():
    calls: list[str] = []
    c = TelegramWebPreviewClient(base_url="https://t.me", min_refetch=60,
                                 transport=_transport({"/s/qachan": PREVIEW_HTML}, calls))
    try:
        chat = await c.add_source("https://t.me/QaChan")
        assert chat.external_id == "tg:qachan" and chat.title == "Тест Канал & Ко" and chat.members_count == 12500
        assert [ch.external_id for ch in await c.list_chats()] == ["tg:qachan"]
        msgs = await c.fetch_messages("tg:qachan")
        assert [m.external_id for m in msgs] == ["101", "102"]
        m1 = msgs[0]
        assert m1.author_external_id == "tg:qachan#Иван Автор" and m1.author_name == "Иван Автор"
        assert m1.url == "https://t.me/QaChan/101" and m1.views == 12700
        newer = await c.fetch_messages("tg:qachan", since=datetime(2026, 11, 14, 11, 40))
        assert [m.external_id for m in newer] == ["102"]
        assert await c.fetch_messages("chat_chert_48k2") == []
        prof = await c.get_user_profile("tg:qachan")
        assert prof and prof.phone is None, "в веб-превью телефонов нет"
        assert len(calls) == 1, f"кэш страницы (min_refetch): {calls}"
        with pytest.raises(TelegramSourceError):
            await c.add_source("@nopreview_qa")
        with pytest.raises(TelegramSourceError):
            await c.add_source("@missing_qa")
        with pytest.raises(TelegramSourceError):
            await c.add_source("https://t.me/+private")
    finally:
        await c.aclose()


# ================================================================ poller + Telegram
async def test_sync_once_with_telegram_client(db_ready, session):
    from app.integrations.max.poller import sync_once

    c = MockTelegramClient()
    res = await sync_once(session, c)
    assert res.chats == 3 and res.new_messages == 7 and res.by_platform == {"telegram": 7} and not res.errors
    rows = (await session.scalars(select(MaxChat))).all()
    assert {r.platform for r in rows} == {"telegram"}, "явный TG-клиент синхронизирует только Telegram"
    msg = await session.scalar(select(ChatMessage).where(ChatMessage.external_id == "8812"))
    assert msg.platform == "telegram" and msg.message_url == "https://t.me/moscow_live_demo/8812"
    assert msg.views == 48200
    prof = await session.get(AuthorProfile, "tg:700101")
    assert prof and prof.phone, "профили TG-авторов с открытым телефоном кэшируются"
    again = await sync_once(session, c)
    assert again.new_messages == 0
    assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 7


async def test_sync_once_default_skips_mock_telegram_without_sources(db_ready, session):
    """TG_PROVIDER=mock и нет TG-источников в БД -> общий синк трогает только MAX."""
    from app.integrations.max.poller import sync_once

    res = await sync_once(session)
    assert res.by_platform == {"max": 40}
    assert not (await session.scalars(select(MaxChat).where(MaxChat.platform == "telegram"))).all()
