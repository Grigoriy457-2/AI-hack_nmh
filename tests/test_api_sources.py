"""API v2: источники (MAX + Telegram) — список, вкл/выкл, добавление по ссылке, удаление, синк,
блок «Telegram» (CONTRACTS_v2 §4.4, «Изменения» v2.1; docs/TELEGRAM.md)."""
from __future__ import annotations

import pytest

from nf_helpers import check, data, detail_text

pytestmark = pytest.mark.api

CHAT_KEYS = {"id", "platform", "platform_label", "external_id", "ref", "title", "district", "enabled",
             "last_synced_at", "messages_count"}


async def test_source_chats_after_seed(editor):
    chats = data(await editor.get("/api/sources/chats"))
    assert len(chats) == 5, "seed синхронизирует 5 домовых чатов mock-MAX"
    assert all(CHAT_KEYS <= set(c) for c in chats)
    assert {(c["platform"], c["platform_label"]) for c in chats} == {("max", "MAX")}
    assert sum(c["messages_count"] for c in chats) == 40
    assert all(c["enabled"] and c["last_synced_at"] for c in chats)
    assert data(await editor.get("/api/max/chats")) == chats, "алиас /api/max/chats"
    assert data(await editor.get("/api/sources/chats", params={"platform": "telegram"})) == []


async def test_patch_source(editor):
    cid = data(await editor.get("/api/sources/chats"))[0]["id"]
    upd = data(await editor.patch(f"/api/sources/chats/{cid}", json={"enabled": False}))
    assert upd["enabled"] is False and upd["id"] == cid and upd["messages_count"] > 0
    upd = data(await editor.patch(f"/api/sources/chats/{cid}", json={"district": "Тестовый район", "title": "QA чат"}))
    assert upd["district"] == "Тестовый район" and upd["title"] == "QA чат" and upd["enabled"] is False
    upd = data(await editor.patch(f"/api/max/chats/{cid}", json={"enabled": True}))
    assert upd["enabled"] is True
    again = {c["id"]: c for c in data(await editor.get("/api/sources/chats"))}
    assert again[cid]["title"] == "QA чат" and again[cid]["enabled"]
    check(await editor.patch("/api/sources/chats/999999", json={"enabled": True}), 404)
    check(await editor.patch(f"/api/sources/chats/{cid}", json={"title": "x" * 300}), 422)


async def test_disabled_chat_not_synced(editor):
    cid = data(await editor.get("/api/sources/chats"))[0]["id"]
    data(await editor.patch(f"/api/sources/chats/{cid}", json={"enabled": False}))
    res = data(await editor.post("/api/sources/sync"))
    assert res["chats"] == 4 and res["new_messages"] == 0 and not res["errors"]


async def test_sync_idempotent(editor):
    r1 = data(await editor.post("/api/sources/sync"))
    assert set(r1) >= {"chats", "new_messages", "errors", "finished_at", "by_platform"}
    assert r1["chats"] == 5 and r1["new_messages"] == 0 and not r1["errors"]
    r2 = data(await editor.post("/api/max/sync"))
    assert r2["new_messages"] == 0


# ================================================================ Telegram через POST /api/sources
@pytest.mark.parametrize("ref", ["@moscow_live_demo", "moscow_live_demo", "https://t.me/moscow_live_demo",
                                 "t.me/s/moscow_live_demo", "https://t.me/moscow_live_demo/8812"])
async def test_add_telegram_source_refs(editor, ref):
    r = data(await editor.post("/api/sources", json={"platform": "telegram", "ref": ref}), 201)
    assert CHAT_KEYS <= set(r)
    assert r["platform"] == "telegram" and r["platform_label"] == "Telegram"
    assert r["external_id"] == "tg:moscow_live_demo" and r["title"] == "Москва Live (демо)"
    assert r["enabled"] and r["messages_count"] == 2, "сразу после подключения — первая выгрузка"
    # повторное подключение того же канала — 200 и тот же источник
    again = data(await editor.post("/api/sources", json={"platform": "telegram", "ref": "@MOSCOW_LIVE_DEMO"}), 200)
    assert again["id"] == r["id"]
    tg = data(await editor.get("/api/sources/chats", params={"platform": "telegram"}))
    assert len([c for c in tg if c["external_id"] == "tg:moscow_live_demo"]) == 1


async def test_add_telegram_syncs_all_mock_sources(editor, with_telegram):
    assert {c["external_id"] for c in with_telegram} == {
        "tg:moscow_live_demo", "tg:srochno_yug_demo", "tg:chertanovo_online_demo"}
    assert sum(c["messages_count"] for c in with_telegram) == 7
    res = data(await editor.post("/api/sources/sync"))
    assert res["new_messages"] == 0 and res["chats"] == 8 and not res["errors"]


@pytest.mark.parametrize("ref", ["https://t.me/+AbCdEfGhIjK", "t.me/joinchat/AbCdEf", "+AbCdEfGh"])
async def test_add_private_telegram_link_422(editor, ref):
    r = await editor.post("/api/sources", json={"platform": "telegram", "ref": ref})
    check(r, 422)
    assert "приват" in detail_text(r).lower(), f"понятная причина: {r.text}"
    assert data(await editor.get("/api/sources/chats", params={"platform": "telegram"})) == []


@pytest.mark.parametrize("ref", ["@ab", "https://t.me/", "не ссылка вовсе", "@1abc"])
async def test_add_bad_telegram_ref_422(editor, ref):
    r = await editor.post("/api/sources", json={"platform": "telegram", "ref": ref})
    check(r, 422)
    assert detail_text(r), "422 должен содержать detail"


async def test_add_unknown_telegram_channel_mock_422_with_detail(editor):
    """Запрос фронтенда: на mock-Telegram неизвестный канал не должен давать 422 с пустым телом.
    Решение бэкенда: mock сохраняет публичный источник без проверки (201, сообщений пока нет);
    допустим и 422, но только с понятным {"detail": "..."}."""
    r = await editor.post("/api/sources", json={"platform": "telegram", "ref": "https://t.me/test_public_channel"})
    assert r.status_code in (201, 422), r.text
    if r.status_code == 422:
        assert r.json().get("detail") and isinstance(r.json()["detail"], str), r.text
    else:
        body = r.json()
        assert body["external_id"] == "tg:test_public_channel" and body["platform"] == "telegram"
        assert body["title"] == "@test_public_channel" and body["messages_count"] == 0


async def test_add_source_validation(editor):
    check(await editor.post("/api/sources", json={"platform": "vk", "ref": "@moscow_live_demo"}), 422)
    check(await editor.post("/api/sources", json={"platform": "telegram", "ref": ""}), 422)
    check(await editor.post("/api/sources", json={"platform": "telegram"}), 422)


async def test_add_max_source(editor, users):
    r = data(await editor.post("/api/sources", json={"platform": "max", "ref": "qa_chat_777",
                                                     "title": "QA: новый чат", "district": "Тест"}), 201)
    assert r["platform"] == "max" and r["external_id"] == "qa_chat_777" and r["title"] == "QA: новый чат"
    assert r["district"] == "Тест" and r["ref"] == "qa_chat_777"
    again = data(await editor.post("/api/sources", json={"platform": "max", "ref": "qa_chat_777"}), 200)
    assert again["id"] == r["id"]


async def test_delete_source_without_references(editor, with_telegram):
    tg = next(c for c in with_telegram if c["external_id"] == "tg:srochno_yug_demo")
    check(await editor.delete(f"/api/sources/chats/{tg['id']}"), 204)
    assert tg["id"] not in {c["id"] for c in data(await editor.get("/api/sources/chats"))}
    feed = data(await editor.get("/api/feed", params={"limit": 500}))["items"]
    assert tg["id"] not in {m["chat_id"] for m in feed}
    check(await editor.delete(f"/api/sources/chats/{tg['id']}"), 404)


async def test_delete_source_with_story_references(editor, review_story):
    """Источник, на сообщения которого ссылается сюжет, отключается, а не удаляется; сюжет цел."""
    cid = review_story["chat_id"]
    check(await editor.delete(f"/api/sources/chats/{cid}"), 204)
    chats = {c["id"]: c for c in data(await editor.get("/api/sources/chats"))}
    assert cid in chats and chats[cid]["enabled"] is False
    src = data(await editor.get(f"/api/stories/{review_story['id']}/sources"))
    assert len(src) == 6 and all(s["snippet"] for s in src)
    assert len(data(await editor.get(f"/api/stories/{review_story['id']}/contacts"))) == 2


# ================================================================ блок «Telegram»
async def test_telegram_status(editor):
    st = data(await editor.get("/api/telegram/status"))
    assert st["available"] is True and st["platform"] == "telegram" and st["provider"] == "mock"
    assert {"sources", "pending", "last_error"} <= set(st)


async def test_telegram_sources_alias_add_delete(editor):
    r = data(await editor.post("/api/telegram/sources", json={"ref": "https://t.me/chertanovo_online_demo"}), 201)
    assert r["platform"] == "telegram" and r["external_id"] == "tg:chertanovo_online_demo"
    assert data(await editor.post("/api/telegram/sources", json={"ref": "@chertanovo_online_demo"}), 200)["id"] == r["id"]
    check(await editor.delete("/api/telegram/sources/chertanovo_online_demo"), 204)
    ids = {c["external_id"] for c in data(await editor.get("/api/sources/chats", params={"platform": "telegram"}))}
    assert "tg:chertanovo_online_demo" not in ids
    check(await editor.delete("/api/telegram/sources/tg:chertanovo_online_demo"), 404)


async def test_telegram_sources_alias_errors(editor):
    check(await editor.post("/api/telegram/sources", json={}), 422)
    r = await editor.post("/api/telegram/sources", json={"ref": "https://t.me/+secret"})
    check(r, 422)
    assert "приват" in detail_text(r).lower()
    check(await editor.delete("/api/telegram/sources/no_such_channel"), 404)
