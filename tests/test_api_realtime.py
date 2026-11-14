"""API v2: лента /api/feed, SSE /api/stream, «Возникающие сюжеты» /api/emerging, демо-поток
(CONTRACTS_v2 §4.2, §4.4). Данные: seed (40 сообщений mock-MAX, DEMO_STREAM_ENABLED=false) и
mock-Telegram (фикстура ``with_telegram``)."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.ai import mock_data
from app.config import settings
from nf_helpers import TG_LIVE_TEXT, check, data, make_story, parse_sse

pytestmark = pytest.mark.api

FEED_KEYS = {"id", "platform", "platform_label", "chat_id", "chat_title", "district", "author_name",
             "author_external_id", "text", "sent_at", "has_media", "media_url", "is_forwarded", "message_url",
             "badges", "uniqueness_score", "emerging_key"}
BADGE_KEYS = {"unique", "important", "duplicates", "is_duplicate", "duplicate_of", "has_phone"}
CARD_KEYS = {"key", "title", "summary", "keywords", "place", "messages_count", "chats_count", "chats", "platforms",
             "first_at", "last_at", "trend", "trend_label", "recent_count", "important_count", "unique_count",
             "has_phone_count", "score", "message_ids", "sample", "investigation_id", "investigation_status"}
DEMO_KEYS = {"enabled", "active", "epoch", "started_at", "released", "total", "next_at", "finished", "interval_sec"}

TEXT = {m["external_id"]: m["text"] for m in mock_data.SCENARIO_MAX_MESSAGES}


async def _all_feed(c, **params) -> list[dict]:
    return data(await c.get("/api/feed", params={"limit": 500, **params}))["items"]


def _by_text(items: list[dict], text: str) -> list[dict]:
    return [m for m in items if m["text"] == text]


# ================================================================ лента
async def test_feed_shape_order_cursor(editor):
    f = data(await editor.get("/api/feed"))
    assert set(f) == {"items", "cursor", "server_time"}
    items = f["items"]
    assert len(items) == 40, "без since — последние limit=50, после seed их 40"
    ids = [m["id"] for m in items]
    assert ids == sorted(ids), "items по возрастанию id"
    assert f["cursor"] == max(ids)
    datetime.fromisoformat(f["server_time"])
    for m in items:
        assert FEED_KEYS <= set(m), FEED_KEYS - set(m)
        assert set(m["badges"]) == BADGE_KEYS
        assert m["platform"] == "max" and m["platform_label"] == "MAX"
        assert (m["message_url"] or "").startswith("max://chat/")
    assert all(m["chat_title"] for m in items)


async def test_feed_limit_since_cursor(editor):
    f = data(await editor.get("/api/feed", params={"limit": 5}))
    ids = [m["id"] for m in f["items"]]
    assert len(ids) == 5 and ids == sorted(ids) and f["cursor"] == ids[-1] == 40, "последние 5, по возрастанию"
    cur = f["cursor"]
    empty = data(await editor.get("/api/feed", params={"since": cur}))
    assert empty["items"] == [] and empty["cursor"] == cur
    tail = data(await editor.get("/api/feed", params={"since": cur - 3}))
    assert [m["id"] for m in tail["items"]] == [cur - 2, cur - 1, cur] and tail["cursor"] == cur
    page = data(await editor.get("/api/feed", params={"since": 0, "limit": 7}))
    assert [m["id"] for m in page["items"]] == list(range(1, 8)) and page["cursor"] == 7, \
        "с since — первые limit после курсора (догоняем по страницам)"
    check(await editor.get("/api/feed", params={"limit": 0}), 422)
    check(await editor.get("/api/feed", params={"limit": 501}), 422)


async def test_feed_since_iso_time(editor):
    """since = ISO-время: только сообщения, полученные позже (новый Telegram-источник)."""
    t = data(await editor.get("/api/feed"))["server_time"]
    await asyncio.sleep(0.01)
    data(await editor.post("/api/sources", json={"platform": "telegram", "ref": "@moscow_live_demo"}), 201)
    items = data(await editor.get("/api/feed", params={"since": t}))["items"]
    assert items and {m["platform"] for m in items} == {"telegram"}, [m["platform"] for m in items]
    assert len(items) == 7
    items_z = data(await editor.get("/api/feed", params={"since": t + "Z"}))["items"]
    assert len(items_z) == 7, "ISO с таймзоной (Z) тоже понимается"


async def test_feed_filters_chat_platform(editor, with_telegram):
    chats = data(await editor.get("/api/sources/chats"))
    max_chat = next(c for c in chats if c["platform"] == "max")
    only = await _all_feed(editor, chat_id=max_chat["id"])
    assert only and {m["chat_id"] for m in only} == {max_chat["id"]}
    assert len(only) == max_chat["messages_count"]
    tg = await _all_feed(editor, platform="telegram")
    assert len(tg) == 7 and {m["platform_label"] for m in tg} == {"Telegram"}
    assert all(m["message_url"].startswith("https://t.me/") for m in tg)
    assert len(await _all_feed(editor, platform="max")) == 40


async def test_feed_badges_scenario(editor):
    items = await _all_feed(editor)
    m02 = _by_text(items, TEXT["m02"])
    assert len(m02) == 2, "m02 и его пересылка m03 (дословно)"
    orig, copy = sorted(m02, key=lambda m: m["sent_at"])
    assert orig["badges"]["unique"] and orig["badges"]["important"] and orig["badges"]["duplicates"] >= 1
    assert not orig["badges"]["is_duplicate"]
    assert copy["badges"]["is_duplicate"] and copy["badges"]["duplicate_of"] == orig["id"]
    assert not copy["badges"]["unique"] and copy["badges"]["duplicates"] == 0
    assert copy["uniqueness_score"] < 0.5 <= orig["uniqueness_score"]
    # «есть телефон»: автор m01 u1001 — по профилю mock-MAX
    phone = mock_data.SCENARIO_MAX_PROFILES["u1001"]["phone"]
    m01 = _by_text(items, TEXT["m01"])[0]
    assert m01["badges"]["has_phone"] is bool(phone)
    m11 = _by_text(items, TEXT["m11"])[0]  # u1010 — без телефона?
    assert m11["badges"]["has_phone"] is bool(mock_data.SCENARIO_MAX_PROFILES["u1010"]["phone"])
    assert any(m["has_media"] and m["media_url"] for m in items)


async def test_feed_junk_not_marked_important(editor):
    """Бейдж «важное» — только для сообщений с новыми фактами. В mock_data у мусора important не
    задан (= False, см. комментарий к SCENARIO_MAX_MESSAGES)."""
    items = await _all_feed(editor)
    junk = {m["text"] for m in mock_data.SCENARIO_MAX_MESSAGES if not m.get("relevant")}
    wrong = [m["text"][:50] for m in items if m["text"] in junk and m["badges"]["important"]]
    assert not wrong, f"мусор помечен «важное»: {wrong}"


async def test_feed_cross_platform_duplicates(editor, with_telegram):
    """TG-пост «Москва Live» — первоисточник, MAX m09/m10 (пересылки) — его дубли."""
    items = await _all_feed(editor)
    live = _by_text(items, TG_LIVE_TEXT)
    tg = [m for m in live if m["platform"] == "telegram"]
    mx = [m for m in live if m["platform"] == "max"]
    assert len(tg) == 1 and len(mx) == 2, [(m["platform"], m["id"]) for m in live]
    tg = tg[0]
    assert tg["chat_title"].startswith("Москва Live") and tg["message_url"] == "https://t.me/moscow_live_demo/8812"
    assert not tg["badges"]["is_duplicate"] and tg["badges"]["duplicates"] == 2 and tg["badges"]["unique"]
    for m in mx:
        assert m["badges"]["is_duplicate"] and m["badges"]["duplicate_of"] == tg["id"], m["badges"]
        assert m["is_forwarded"]
    # копия MAX m12 в TG-чате «Чертаново онлайн» — дубль MAX-первоисточника
    m12 = [m for m in items if m["text"].startswith("Полиция перекрыла парковку")]
    first = min(m12, key=lambda m: m["sent_at"])
    assert first["platform"] == "max" and first["badges"]["duplicates"] == len(m12) - 1 >= 2
    tg_copy = [m for m in m12 if m["platform"] == "telegram"]
    assert tg_copy and tg_copy[0]["badges"]["duplicate_of"] == first["id"]
    # уникальное свидетельство в TG с открытым телефоном
    witness = [m for m in items if m["text"].startswith("Я из соседнего дома, 48к1")]
    assert witness and witness[0]["badges"]["unique"] and witness[0]["badges"]["has_phone"]


async def test_feed_hides_disabled_chat(editor):
    chats = data(await editor.get("/api/sources/chats"))
    target = max((c for c in chats if c["platform"] == "max"), key=lambda c: c["messages_count"])
    data(await editor.patch(f"/api/sources/chats/{target['id']}", json={"enabled": False}))
    items = await _all_feed(editor)
    assert target["id"] not in {m["chat_id"] for m in items}
    assert len(items) == 40 - target["messages_count"]
    for card in data(await editor.get("/api/emerging")):
        assert target["title"] not in card["chats"]


# ================================================================ возникающие сюжеты
async def test_emerging_cards_shape(editor, with_telegram):
    cards = data(await editor.get("/api/emerging"))
    assert cards, "по сценарию «Чертаново» должна быть хотя бы одна карточка"
    feed = {m["id"]: m for m in await _all_feed(editor)}
    for c in cards:
        assert CARD_KEYS <= set(c), CARD_KEYS - set(c)
        assert c["title"].startswith("Возможный инфоповод: ")
        assert c["messages_count"] == len(c["message_ids"]) >= settings.EMERGING_MIN_MESSAGES
        assert c["chats_count"] == len(c["chats"]) and set(c["platforms"]) <= {"max", "telegram"}
        assert c["trend"] in ("rising", "steady", "fading")
        assert c["trend_label"] == {"rising": "растёт", "steady": "стабильно", "fading": "затихает"}[c["trend"]]
        assert c["first_at"] <= c["last_at"]
        assert set(c["message_ids"]) <= set(feed)
        first = min(c["message_ids"], key=lambda i: (feed[i]["sent_at"], i))
        assert c["key"] == f"e{first}", "key = e<id самого раннего сообщения кластера>"
        assert c["first_at"] == feed[first]["sent_at"]
        assert 1 <= len(c["sample"]) <= 3 and all(FEED_KEYS <= set(s) for s in c["sample"])
        assert c["investigation_id"] is None and c["investigation_status"] is None
        for mid in c["message_ids"]:
            assert feed[mid]["emerging_key"] == c["key"], "FeedMessage.emerging_key = ключ карточки"
    scores = [c["score"] for c in cards]
    assert scores == sorted(scores, reverse=True)
    main = cards[0]
    assert set(main["platforms"]) == {"max", "telegram"}, "кросс-платформенный кластер MAX + Telegram"
    assert main["chats_count"] >= 3 and main["important_count"] >= 1 and main["has_phone_count"] >= 1


async def test_emerging_params(editor):
    assert data(await editor.get("/api/emerging", params={"min_messages": 100})) == []
    assert data(await editor.get("/api/emerging", params={"window_min": 1})) == []
    base = data(await editor.get("/api/emerging"))
    two = data(await editor.get("/api/emerging", params={"min_messages": 2}))
    assert len(two) >= len(base)
    check(await editor.get("/api/emerging", params={"min_messages": 1}), 422)
    check(await editor.get("/api/emerging", params={"window_min": 0}), 422)


async def test_emerging_investigate_idempotent(editor, editor2, users):
    card = data(await editor.get("/api/emerging"))[0]
    r = await editor.post(f"/api/emerging/{card['key']}/investigate", json={"note": "QA: проверить парковку"})
    inv = data(r, 201)
    assert inv["origin"] == "emerging" and inv["emerging_key"] == card["key"]
    assert inv["owner_id"] == users["editor"]
    assert inv["status"] == "review", "PIPELINE_INLINE: пайплайн уже отработал"
    assert inv["signal_at"] == card["first_at"], "signal_at = первое сообщение кластера"
    assert "QA: проверить парковку" in inv["lead_text"]
    assert inv["title"] and "Возможный инфоповод" not in inv["title"]
    # повторно (другой редактор) — 200 и тот же сюжет
    again = data(await editor2.post(f"/api/emerging/{card['key']}/investigate"), 200)
    assert again["id"] == inv["id"]
    # без тела — тоже 200
    assert data(await editor.post(f"/api/emerging/{card['key']}/investigate"), 200)["id"] == inv["id"]
    upd = next(c for c in data(await editor.get("/api/emerging")) if c["key"] == card["key"])
    assert upd["investigation_id"] == inv["id"] and upd["investigation_status"] == "review"
    stories = data(await editor.get("/api/stories", params={"q": inv["title"][:20]}))
    assert [s["id"] for s in stories if s["emerging_key"] == card["key"]] == [inv["id"]]
    hist = data(await editor.get(f"/api/stories/{inv['id']}/history"))
    assert [h["to_status"] for h in hist] == ["new", "researching", "review"]


async def test_emerging_investigate_by_member_message_key(editor):
    """Ключ «устарел» (кластер слился) — investigate по e<id любого сообщения кластера> находит его."""
    card = data(await editor.get("/api/emerging"))[0]
    other = next(i for i in card["message_ids"] if f"e{i}" != card["key"])
    inv = data(await editor.post(f"/api/emerging/e{other}/investigate", json={"title": "Мой заголовок"}), 201)
    assert inv["emerging_key"] == card["key"] and inv["title"] == "Мой заголовок"


async def test_emerging_investigate_404(editor):
    check(await editor.post("/api/emerging/e999999/investigate"), 404)
    check(await editor.post("/api/emerging/bogus/investigate"), 404)


async def test_emerging_investigate_bad_channel(editor):
    card = data(await editor.get("/api/emerging"))[0]
    r = await editor.post(f"/api/emerging/{card['key']}/investigate", json={"channel_id": 999999})
    check(r, 422)
    assert data(await editor.get("/api/emerging"))[0]["investigation_id"] is None


async def test_emerging_dismiss(editor):
    cards = data(await editor.get("/api/emerging"))
    key = cards[0]["key"]
    check(await editor.post(f"/api/emerging/{key}/dismiss"), 204)
    check(await editor.post(f"/api/emerging/{key}/dismiss"), 204)  # повтор — идемпотентно
    assert key not in {c["key"] for c in data(await editor.get("/api/emerging"))}
    assert key in {c["key"] for c in data(await editor.get("/api/emerging", params={"include_dismissed": "true"}))}


# ================================================================ SSE
async def test_sse_hello_and_snapshot(editor):
    cur = data(await editor.get("/api/feed"))["cursor"]
    r = await editor.get("/api/stream", params={"timeout": 0.3})
    check(r)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers.get("cache-control") == "no-cache"
    assert r.text.startswith("retry: 3000")
    evs = [e for e in parse_sse(r.text) if e["event"]]
    assert evs[0]["event"] == "hello"
    hello = evs[0]["data"]
    assert hello["cursor"] == cur and DEMO_KEYS <= set(hello["demo"])
    datetime.fromisoformat(hello["server_time"])
    em = [e for e in evs if e["event"] == "emerging_update"]
    assert em and isinstance(em[0]["data"]["items"], list) and em[0]["data"]["items"]
    assert CARD_KEYS <= set(em[0]["data"]["items"][0])
    assert not [e for e in evs if e["event"] == "new_message"], "без since — только новые с момента подключения"


async def test_sse_hello_server_time_iso(editor):
    """server_time в hello — ISO-время, как в FeedOut (а не str(datetime) с пробелом)."""
    r = await editor.get("/api/stream", params={"timeout": 0})
    hello = next(e for e in parse_sse(r.text) if e["event"] == "hello")["data"]
    assert "T" in hello["server_time"], hello["server_time"]


async def test_sse_since_replays_new_messages(editor):
    cur = data(await editor.get("/api/feed"))["cursor"]
    r = await editor.get("/api/stream", params={"timeout": 0.5, "since": cur - 3})
    msgs = [e for e in parse_sse(r.text) if e["event"] == "new_message"]
    assert [int(e["id"]) for e in msgs] == [cur - 2, cur - 1, cur]
    assert [e["data"]["id"] for e in msgs] == [cur - 2, cur - 1, cur]
    assert FEED_KEYS <= set(msgs[0]["data"])


async def test_sse_live_new_messages_and_story_update(editor, editor2, users, monkeypatch):
    """Пока поток открыт, появляются сообщения (подключён Telegram) и меняется сюжет."""
    monkeypatch.setattr(settings, "SSE_POLL_SEC", 0.2)
    inv = await make_story(users, "review")
    cur = data(await editor.get("/api/feed"))["cursor"]

    async def act():
        await asyncio.sleep(0.5)
        data(await editor2.post("/api/sources", json={"platform": "telegram", "ref": "@moscow_live_demo"}), 201)
        check(await editor2.post(f"/api/stories/{inv}/reject", json={"comment": "QA"}))

    r, _ = await asyncio.gather(editor.get("/api/stream", params={"timeout": 2.5}), act())
    evs = parse_sse(r.text)
    names = [e["event"] for e in evs if e["event"]]
    assert names[0] == "hello"
    new = [e["data"] for e in evs if e["event"] == "new_message"]
    assert len(new) == 7 and {m["platform"] for m in new} == {"telegram"}
    assert all(m["id"] > cur for m in new) and [m["id"] for m in new] == sorted(m["id"] for m in new)
    em = [e for e in evs if e["event"] == "emerging_update"]
    assert len(em) >= 2, "после новых сообщений — свежий снимок возникающих сюжетов"
    assert names.index("new_message") < len(names) - 1 - names[::-1].index("emerging_update")
    upd = [e["data"] for e in evs if e["event"] == "story_update" and e["data"]["id"] == inv]
    assert upd and upd[-1]["status"] == "rejected" and upd[-1]["status_label"] == "Не подтвердилось"
    assert {"id", "title", "status", "status_label", "progress", "updated_at"} <= set(upd[-1])
    assert set(upd[-1]["progress"]) == {"state", "percent", "current_stage"}


async def test_sse_keepalive(editor, monkeypatch):
    monkeypatch.setattr(settings, "SSE_POLL_SEC", 0.2)
    monkeypatch.setattr(settings, "SSE_KEEPALIVE_SEC", 1.0)
    r = await editor.get("/api/stream", params={"timeout": 1.6})
    assert any(e["comment"] == "keepalive" for e in parse_sse(r.text))


async def test_sse_bad_params(editor):
    check(await editor.get("/api/stream", params={"timeout": -1}), 422)
    check(await editor.get("/api/stream", params={"since": "abc", "timeout": 0}), 422)


# ================================================================ демо-поток
async def test_demo_stream_status_disabled(editor):
    st = data(await editor.get("/api/demo/stream"))
    assert set(st) == DEMO_KEYS
    assert st["enabled"] is False and st["active"] is False and st["next_at"] is None
    assert st["total"] == 40


@pytest.fixture
def fast_demo(monkeypatch):
    """Демо-трикл «мгновенно»: 0.05–0.1 с между сообщениями (CONTRACTS_v2 «Изменения» v2.1)."""
    from app.integrations.max import demo_stream
    from app.integrations.max.client import set_max_client
    from app.integrations.max.mock import MockMaxClient

    monkeypatch.setattr(settings, "DEMO_STREAM_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_STREAM_MIN_SEC", 0.05)
    monkeypatch.setattr(settings, "DEMO_STREAM_MAX_SEC", 0.1)
    demo_stream.reset_state(epoch=0)
    client = MockMaxClient()
    assert client.streaming
    set_max_client(client)
    return demo_stream


async def test_demo_restart_trickle(editor, fast_demo):
    before = data(await editor.get("/api/feed"))["cursor"]
    st = data(await editor.post("/api/demo/restart"))
    assert st["enabled"] and st["active"] and st["epoch"] == 1 and st["total"] == 40
    assert st["released"] >= settings.DEMO_STREAM_INITIAL
    f1 = data(await editor.get("/api/feed", params={"limit": 500}))
    assert f1["items"], "после перезапуска в ленте есть стартовые сообщения"
    assert all(m["id"] > before for m in f1["items"]), "прежние сообщения архивированы и скрыты из ленты"
    n1 = len(f1["items"])
    assert n1 >= settings.DEMO_STREAM_INITIAL
    # управляемое время: ждём, пока «выйдет» ещё несколько сообщений (интервал 0.05–0.1 с)
    await asyncio.sleep(0.6)
    res = data(await editor.post("/api/sources/sync"))
    assert res["new_messages"] > 0, "трикл: новые сообщения появляются со временем"
    f2 = data(await editor.get("/api/feed", params={"since": f1["cursor"]}))
    assert len(f2["items"]) == res["new_messages"]
    st2 = data(await editor.get("/api/demo/stream"))
    assert st2["released"] > st["released"] or st2["finished"]
    await asyncio.sleep(max(0.0, 40 * 0.1 - 0.6) + 0.2)
    data(await editor.post("/api/sources/sync"))
    st3 = data(await editor.get("/api/demo/stream"))
    assert st3["finished"] and st3["released"] == 40 and st3["next_at"] is None
    assert len(await _all_feed(editor, platform="max")) == 40
    # стартовые сообщения — фоновый шум, событие сценария идёт потоком
    first_texts = [m["text"] for m in f1["items"][: settings.DEMO_STREAM_INITIAL]]
    assert TEXT["m01"] not in first_texts


async def test_demo_restart_keeps_story_sources(editor, fast_demo, review_story):
    data(await editor.post("/api/demo/restart"))
    src = data(await editor.get(f"/api/stories/{review_story['id']}/sources"))
    assert len(src) == 6 and data(await editor.get(f"/api/stories/{review_story['id']}/contacts"))


async def test_demo_restart_keeps_telegram_demo(editor, fast_demo, with_telegram):
    """После «Перезапустить демо-поток» 7 постов mock-Telegram снова в ленте (якорь = старт − 125 мин),
    иначе на Радаре пропадает кросс-платформенный дубль «Москва Live» ↔ MAX m09/m10."""
    data(await editor.post("/api/demo/restart"))
    data(await editor.post("/api/sources/sync"))
    tg = await _all_feed(editor, platform="telegram")
    assert len(tg) == 7, f"после перезапуска в ленте {len(tg)} сообщений Telegram из 7"


async def test_demo_restart_without_trickle_keeps_feed(editor):
    """DEMO_STREAM_ENABLED=false (детерминированный режим): «Перезапустить демо-поток» не должен
    оставлять ленту пустой — сообщения сценария должны вернуться."""
    before = len(await _all_feed(editor))
    st = data(await editor.post("/api/demo/restart"))
    assert st["epoch"] == 1
    after = await _all_feed(editor)
    assert len(after) == before, f"после перезапуска лента: {len(after)} сообщений (было {before})"
