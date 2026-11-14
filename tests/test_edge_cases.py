"""Граничные случаи v2: seed для демо, фоновый пайплайн, ошибки LLM, выключенные источники,
старая схема БД, кривые входные данные."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.ai.llm import LLMError, MockLLM, set_llm
from app.config import settings
from nf_helpers import CHERTANOVO_LEAD, check, data

pytestmark = pytest.mark.api


# ================================================================ seed для демо
async def test_seed_has_demo_stories(editor):
    lst = data(await editor.get("/api/stories"))
    statuses = {s["status"] for s in lst}
    assert len(lst) >= 3 and len(statuses) >= 3, f"seed: {len(lst)} сюжетов, статусы {statuses}"
    assert "handed_off" in statuses, "для статистики нужен хотя бы один переданный сюжет"
    st = data(await editor.get("/api/stats"))
    assert st["stories_total"] == len(lst) and st["handed_off"] >= 1
    assert data(await editor.get("/api/handoff-channels"))


async def test_seed_idempotent(app_started):
    from app import db
    from app.seed import seed_if_empty

    async with db.session_scope() as s:
        assert await seed_if_empty(s) is False


async def test_v1_schema_is_recreated(db_ready):
    """Старая SQLite-БД со схемой v1 (users.role, max_chats, feedback) пересоздаётся при старте."""
    from app import db

    async with db.engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS status_history"))
        await conn.execute(text("CREATE TABLE feedback (id INTEGER PRIMARY KEY, verdict VARCHAR(16))"))
    await db.init_db()
    async with db.engine.begin() as conn:
        tables = {r[0] for r in (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).all()}
    assert "feedback" not in tables and "status_history" in tables


# ================================================================ фоновый режим (как в демо)
async def test_background_pipeline_with_progress_polling(editor, monkeypatch):
    """PIPELINE_INLINE=false: POST сразу отвечает researching, /progress показывает живой лог, затем review."""
    from app.services import pipeline

    monkeypatch.setattr(settings, "PIPELINE_INLINE", False)
    monkeypatch.setattr(pipeline, "DEMO_DELAY_SCALE", 0.02, raising=False)
    inv = data(await editor.post("/api/stories", json={"lead_text": CHERTANOVO_LEAD}), 201)
    assert inv["status"] == "researching", "в фоновом режиме ответ не ждёт пайплайн"
    percents = []
    for _ in range(600):
        p = data(await editor.get(f"/api/stories/{inv['id']}/progress"))
        state = p["progress"].get("state")
        if p["progress"].get("percent") is not None:
            percents.append(p["progress"]["percent"])
        if state in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    assert p["progress"]["state"] == "done", p["progress"]
    for _ in range(100):  # переход в review — после записи done
        if data(await editor.get(f"/api/stories/{inv['id']}"))["status"] == "review":
            break
        await asyncio.sleep(0.05)
    assert data(await editor.get(f"/api/stories/{inv['id']}"))["status"] == "review"
    assert percents == sorted(percents), f"percent растёт монотонно: {percents}"
    running = getattr(pipeline, "_running", None)
    if running:
        await asyncio.gather(*list(running), return_exceptions=True)


async def test_deeper_in_background_is_409_while_researching(editor, monkeypatch):
    from app.services import pipeline

    inv = data(await editor.post("/api/stories", json={"lead_text": CHERTANOVO_LEAD}), 201)
    monkeypatch.setattr(settings, "PIPELINE_INLINE", False)
    monkeypatch.setattr(pipeline, "DEMO_DELAY_SCALE", 0.05, raising=False)
    r = data(await editor.post(f"/api/stories/{inv['id']}/deeper", json={"question": "Есть ли видео"}))
    assert r["status"] == "researching"
    check(await editor.post(f"/api/stories/{inv['id']}/deeper", json={"question": "ещё"}), 409)
    check(await editor.post(f"/api/stories/{inv['id']}/run"), 409)
    check(await editor.post(f"/api/stories/{inv['id']}/handoff", json={"to": "Сайт", "channel": "site"}), 409)
    for _ in range(600):
        if data(await editor.get(f"/api/stories/{inv['id']}"))["status"] == "review":
            break
        await asyncio.sleep(0.05)
    assert data(await editor.get(f"/api/stories/{inv['id']}"))["status"] == "review"
    running = getattr(pipeline, "_running", None)
    if running:
        await asyncio.gather(*list(running), return_exceptions=True)


class _FailingLLM(MockLLM):
    async def complete_json(self, *a, task=None, **kw):
        if task in ("intelligence", "report"):
            raise LLMError("boom (QA)")
        return await super().complete_json(*a, task=task, **kw)


async def test_pipeline_error_is_reported_not_raised(editor):
    set_llm(_FailingLLM())
    try:
        inv = data(await editor.post("/api/stories", json={"lead_text": CHERTANOVO_LEAD}), 201)
    finally:
        set_llm(MockLLM())
    d = data(await editor.get(f"/api/stories/{inv['id']}"))
    prog = d["progress_json"]
    if prog["state"] == "done":  # пайплайн обошёл ошибку фолбэком — допустимо, но справка должна быть
        check(await editor.get(f"/api/stories/{inv['id']}/report"))
        return
    assert prog["state"] == "error" and prog.get("error"), prog
    assert any(s["status"] == "error" for s in prog["stages"])
    assert d["status"] == "researching", "при ошибке статус не становится review"
    # «Остановить» выводит зависший сюжет в review, дальше можно перезапустить
    assert data(await editor.post(f"/api/stories/{inv['id']}/stop"))["status"] == "review"
    assert data(await editor.post(f"/api/stories/{inv['id']}/run"))["status"] == "review"


async def test_pipeline_with_all_chats_disabled(editor):
    for ch in data(await editor.get("/api/sources/chats")):
        data(await editor.patch(f"/api/sources/chats/{ch['id']}", json={"enabled": False}))
    inv = data(await editor.post("/api/stories", json={"lead_text": CHERTANOVO_LEAD}), 201)
    assert inv["status"] == "review"
    src = data(await editor.get(f"/api/stories/{inv['id']}/sources"))
    assert not [s for s in src if s["kind"] == "max_chat"], "выключенные чаты не попадают в поиск"
    assert data(await editor.get(f"/api/stories/{inv['id']}/contacts")) == []
    assert data(await editor.get("/api/feed"))["items"] == []
    assert data(await editor.get("/api/emerging")) == []


async def test_pipeline_max_source_urls(editor):
    inv = data(await editor.post("/api/stories", json={"lead_text": CHERTANOVO_LEAD}), 201)
    src = data(await editor.get(f"/api/stories/{inv['id']}/sources", params={"kind": "max_chat"}))
    assert src
    for s in src:
        assert s["chat_message_id"], f"чатовый источник {s['id']} без chat_message_id"
        parts = (s["url"] or "").split("/")
        assert s["url"].startswith("max://chat/") and len(parts) == 5 and all(parts[3:]), s["url"]


# ================================================================ кривые входные данные
async def test_bad_json_bodies(editor, review_story):
    inv = review_story["id"]
    r = await editor.post(f"/api/stories/{inv}/handoff", content=b"not json",
                          headers={"content-type": "application/json"})
    check(r, 422)
    check(await editor.post("/api/stories", json={"lead_text": 123}), 422)
    check(await editor.post(f"/api/stories/{inv}/facts", json={"state": "confirmed"}), 422)
    check(await editor.get("/api/stories/abc"), 422)
    check(await editor.patch("/api/sources/chats/1", json={"enabled": "maybe"}), 422)


async def test_unicode_and_long_inputs(editor, review_story):
    inv = review_story["id"]
    long_comment = "😀 коммент " * 500
    r = data(await editor.post(f"/api/stories/{inv}/handoff",
                               json={"to": "Ночная смена 🌙 «сайт»", "channel": "other", "comment": long_comment}))
    assert r["story"]["handoff_to"] == "Ночная смена 🌙 «сайт»"
    assert r["story"]["handoff_comment"] == long_comment.strip()
    check(await editor.post(f"/api/stories/{inv}/handoff", json={"to": "x" * 256, "channel": "site"}), 422)
