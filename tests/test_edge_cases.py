"""Граничные случаи и негативные сценарии: фоновый режим пайплайна, ошибки LLM, XSS, кривые формы,
приватность контактов на всех вкладках, seed для демо."""
from __future__ import annotations

import asyncio

import pytest

from app.ai.llm import LLMError, MockLLM, set_llm
from app.config import settings
from app.models import InvestigationStatus as S
from nf_helpers import (
    CHERTANOVO_LEAD,
    REPORT_SECTION_KEYS,
    assert_clean_html,
    check,
    data,
    make_investigation,
    require_pipeline,
)

pytestmark = pytest.mark.api

TABS = ["progress", "report", "sources", "contacts", "media", "draft", "history"]


# ================================================================ seed для демо
async def test_seed_has_demo_board(operator):
    """TEAM_PLAN DEV-BACKEND п.6: 3–4 демо-расследования в разных статусах + немного фидбэка."""
    board = data(await operator.get("/api/operator/board"))
    cards = [c for cs in board["columns"].values() for c in cs]
    statuses = {s for s, cs in board["columns"].items() if cs}
    assert len(cards) >= 3 and len(statuses) >= 3, f"доска после seed: {len(cards)} карточек, {statuses}"
    assert data(await operator.get("/api/feedback")), "seed должен добавить немного фидбэка"
    m = data(await operator.get("/api/metrics"))
    assert m["investigations_total"] >= 3


async def test_seed_idempotent(app_started):
    from app import db
    from app.seed import seed_if_empty

    async with db.session_scope() as s:
        assert await seed_if_empty(s) is False


# ================================================================ фоновый режим (как в демо)
async def test_background_pipeline_with_progress_polling(producer, monkeypatch):
    """PIPELINE_INLINE=false: POST сразу отвечает researching, /progress показывает живой лог, затем done."""
    require_pipeline()
    from app.services import pipeline

    monkeypatch.setattr(settings, "PIPELINE_INLINE", False)
    monkeypatch.setattr(pipeline, "DEMO_DELAY_SCALE", 0.05, raising=False)
    inv = data(await producer.post("/api/investigations", json={"lead_text": CHERTANOVO_LEAD}), 201)
    assert inv["status"] == "researching", "в фоновом режиме ответ не ждёт пайплайн"
    seen_running = False
    for _ in range(600):
        p = data(await producer.get(f"/api/investigations/{inv['id']}/progress"))
        state = p["progress"].get("state")
        seen_running |= state == "running"
        if state in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    assert p["progress"]["state"] == "done", p["progress"]
    assert p["status"] == "report_ready"
    assert len(p["iterations"]) >= 2
    if pipeline._running:
        await asyncio.gather(*list(pipeline._running), return_exceptions=True)


class _FailingLLM(MockLLM):
    async def complete_json(self, *a, task=None, **kw):
        if task in ("intelligence", "report"):
            raise LLMError("boom (QA)")
        return await super().complete_json(*a, task=task, **kw)


async def test_pipeline_error_is_reported_not_raised(producer, operator):
    require_pipeline()
    set_llm(_FailingLLM())
    try:
        r = await producer.post("/api/investigations", json={"lead_text": CHERTANOVO_LEAD})
        inv = data(r, 201)
    finally:
        set_llm(MockLLM())
    d = data(await producer.get(f"/api/investigations/{inv['id']}"))
    prog = d["progress_json"]
    if prog["state"] == "done":  # пайплайн обошёл ошибку фолбэком — допустимо, но справка должна быть
        check(await producer.get(f"/api/investigations/{inv['id']}/report"))
        return
    assert prog["state"] == "error" and prog.get("error"), prog
    assert any(s["status"] == "error" for s in prog["stages"])
    assert d["status"] in ("researching", "new"), "при ошибке статус не должен становиться report_ready"
    board = data(await operator.get("/api/operator/board"))
    assert inv["id"] in {c["id"] for cs in board["columns"].values() for c in cs}
    # повторный запуск после ошибки должен быть возможен (оператор/продюсер)
    if d["status"] == "researching":
        r2 = await producer.post(f"/api/investigations/{inv['id']}/run")
        assert r2.status_code != 500


async def test_pipeline_with_all_chats_disabled(producer, operator):
    require_pipeline()
    for ch in data(await operator.get("/api/max/chats")):
        data(await operator.patch(f"/api/max/chats/{ch['id']}", json={"enabled": False}))
    inv = data(await producer.post("/api/investigations", json={"lead_text": CHERTANOVO_LEAD}), 201)
    assert inv["status"] == "report_ready"
    src = data(await producer.get(f"/api/investigations/{inv['id']}/sources"))
    assert not [s for s in src if s["kind"] == "max_chat"], "выключенные чаты не должны попадать в RADAR"
    assert data(await producer.get(f"/api/investigations/{inv['id']}/contacts")) == []


async def test_pipeline_max_source_urls(producer, operator):
    require_pipeline()
    inv = data(await producer.post("/api/investigations", json={"lead_text": CHERTANOVO_LEAD}), 201)
    src = data(await producer.get(f"/api/investigations/{inv['id']}/sources", params={"kind": "max_chat"}))
    assert src
    for s in src:
        assert s["chat_message_id"], f"max_chat источник {s['id']} без chat_message_id"
        parts = (s["url"] or "").split("/")
        assert s["url"].startswith("max://chat/") and len(parts) == 5 and all(parts[3:]), s["url"]


async def test_pipeline_pages_hide_contacts_from_editor(producer, editor, smm):
    require_pipeline()
    inv = data(await producer.post("/api/investigations", json={"lead_text": CHERTANOVO_LEAD}), 201)
    phones = [c["phone"] for c in data(await producer.get(f"/api/investigations/{inv['id']}/contacts"))
              if c["phone"]]
    assert phones
    for tab in TABS:
        html_p = assert_clean_html(await producer.get(f"/investigations/{inv['id']}", params={"tab": tab}))
        for c in (editor, smm):
            html = assert_clean_html(await c.get(f"/investigations/{inv['id']}", params={"tab": tab}))
            norm = html.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            for ph in phones:
                digits = "".join(ch for ch in ph if ch.isdigit())
                assert digits[-10:] not in norm, f"телефон {ph} виден не-продюсеру на вкладке {tab}"
        if tab == "contacts":
            assert "tel:" in html_p


# ================================================================ приватность на всех вкладках (ручные данные)
@pytest.mark.parametrize("role", ["editor", "smm"])
async def test_phone_not_leaked_on_any_tab(clients, researched, role):
    for tab in TABS:
        html = assert_clean_html(await clients[role].get(f"/investigations/{researched['id']}", params={"tab": tab}))
        assert "79990001122" not in html.replace("+", "").replace(" ", "").replace("-", ""), \
            f"{role}: телефон на вкладке {tab}"


# ================================================================ XSS / экранирование
async def test_lead_text_is_escaped(producer, operator):
    payload = '<script>alert("xss")</script><img src=x onerror=alert(1)>'
    inv = data(await producer.post("/api/investigations", json={
        "lead_text": "Зацепка " + payload, "title": "XSS " + payload, "autorun": False}), 201)
    pages = [(producer, "/"), (producer, f"/investigations/{inv['id']}"),
             (producer, f"/investigations/{inv['id']}?tab=history"), (operator, "/operator"),
             (producer, "/feedback")]
    for c, path in pages:
        html = assert_clean_html(await c.get(path))
        assert payload not in html, f"{path}: неэкранированный HTML из lead_text/title"
        assert "<script>alert(" not in html


# ================================================================ кривые входные данные
async def test_unknown_tab_does_not_crash(producer, researched):
    r = await producer.get(f"/investigations/{researched['id']}", params={"tab": "bogus"})
    assert r.status_code in (200, 303, 404, 422), r.status_code
    if r.status_code == 200:
        assert_clean_html(r)


async def test_edit_page_404(editor):
    r = await editor.get("/investigations/999999/edit")
    check(r, 404)
    assert "Traceback" not in r.text


async def test_edit_page_save_outside_editing_rejected(editor, researched):
    """Сохранить правку через форму можно только в статусе editing (как в API: 409)."""
    r = await editor.post(f"/investigations/{researched['id']}/edit", data={"title": "T", "body": "B"})
    assert r.status_code in (303, 409), r.status_code
    drafts = data(await editor.get(f"/api/investigations/{researched['id']}/drafts"))
    assert len(drafts) == 1, "черновик сохранён вне статуса editing"


async def test_page_transition_bad_status(producer, users):
    iid = await make_investigation(users, S.report_ready)
    r = await producer.post(f"/investigations/{iid}/transition", data={"to_status": "flying"})
    assert r.status_code in (303, 400, 409, 422), r.status_code
    assert "Traceback" not in r.text


async def test_page_transition_not_allowed(producer, users):
    iid = await make_investigation(users, S.new)
    r = await producer.post(f"/investigations/{iid}/transition", data={"to_status": "published"})
    assert r.status_code in (303, 409), r.status_code
    d = data(await producer.get(f"/api/investigations/{iid}"))
    assert d["status"] == "new"


async def test_page_fact_bad_state(producer, researched):
    r = await producer.post(f"/investigations/{researched['id']}/facts",
                            data={"fact_text": "x", "state": "maybe"})
    assert r.status_code in (303, 400, 422), r.status_code
    assert data(await producer.get(f"/api/investigations/{researched['id']}/facts")) == []


async def test_page_feedback_bad_verdict(producer, researched):
    r = await producer.post("/feedback", data={"investigation_id": str(researched["id"]), "verdict": "meh"})
    assert r.status_code in (303, 400, 422), r.status_code


async def test_login_form_bad_user_id(anon):
    r = await anon.post("/login", data={"user_id": "abc", "password": "demo"})
    assert r.status_code in (200, 400, 422), r.status_code
    r = await anon.post("/login", data={"user_id": "999999", "password": "demo"})
    assert r.status_code in (200, 400, 401, 404), r.status_code
    assert "Traceback" not in r.text


async def test_list_bad_status_param(operator):
    check(await operator.get("/api/investigations", params={"status": "flying"}), 422)


async def test_diff_without_drafts_404(editor, users):
    iid = await make_investigation(users, S.new)
    check(await editor.get(f"/api/investigations/{iid}/drafts/diff"), 404)


async def test_board_ready_responsible_none(operator, users):
    iid = await make_investigation(users, S.ready, editor="editor")
    board = data(await operator.get("/api/operator/board"))
    card = {c["id"]: c for c in board["columns"]["ready"]}[iid]
    assert card["responsible"] is None, "для ready ответственный — SMM (None по контракту BoardCard)"


async def test_create_investigation_bad_channel(producer):
    r = await producer.post("/api/investigations", json={"lead_text": "что-то случилось",
                                                          "channel_id": 999999, "autorun": False})
    assert r.status_code in (404, 422), f"несуществующий channel_id -> {r.status_code}"


async def test_report_items_states_valid(producer, operator):
    require_pipeline()
    inv = data(await producer.post("/api/investigations", json={"lead_text": CHERTANOVO_LEAD}), 201)
    rep = data(await producer.get(f"/api/investigations/{inv['id']}/report"))["report_json"]
    assert sorted(s["key"] for s in rep["sections"]) == sorted(REPORT_SECTION_KEYS)
    ids = [it["id"] for s in rep["sections"] for it in s.get("items", [])]
    assert len(ids) == len(set(ids)), "id элементов справки должны быть уникальны"
    for s in rep["sections"]:
        for it in s.get("items", []):
            assert it.get("state") in (None, "confirmed", "unconfirmed", "false"), it
