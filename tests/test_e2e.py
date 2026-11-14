"""E2E: полный демо-сценарий DoD (п.11 ТЗ) одним тестом — вход через настоящий логин, API + страницы.

producer → исследование (≥2 итерации) → справка с источниками, дубли свёрнуты, «Кому позвонить»
с телефонами → передать редактору → редактор правит (diff) → SMM публикует → продюсер «норм» →
AI-оператор видит всё на доске и в метриках.
"""
from __future__ import annotations

import pytest

from nf_helpers import CHERTANOVO_LEAD, REPORT_SECTION_KEYS, assert_clean_html, check, data, require_pipeline

pytestmark = pytest.mark.e2e


async def _login(make_client, users, login):
    c = make_client(None)
    me = data(await c.post("/api/auth/login", json={"user_id": users[login], "password": "demo"}))
    assert me["login"] == login
    return c


async def test_full_demo_scenario(make_client, users):
    require_pipeline()
    P = await _login(make_client, users, "producer")
    E = await _login(make_client, users, "editor")
    SM = await _login(make_client, users, "smm")
    O = await _login(make_client, users, "operator")

    # 1. продюсер: новая зацепка (страница) + запуск
    assert_clean_html(await P.get("/investigations/new"))
    channel = data(await P.get("/api/channels"))[0]
    inv = data(await P.post("/api/investigations", json={
        "lead_text": CHERTANOVO_LEAD, "channel_id": channel["id"], "autorun": True}), 201)
    iid = inv["id"]
    assert inv["status"] == "report_ready"

    # 2. цепочка поиска ≥2 итераций, прогресс done
    prog = data(await P.get(f"/api/investigations/{iid}/progress"))
    assert prog["progress"]["state"] == "done"
    assert len(prog["iterations"]) >= 2

    # 3. справка с источниками, дубли свёрнуты, контакты с телефонами
    rep = data(await P.get(f"/api/investigations/{iid}/report"))["report_json"]
    # порядок разделов строго проверяется в test_pipeline; здесь — состав, чтобы сценарий шёл дальше
    assert sorted(s["key"] for s in rep["sections"]) == sorted(REPORT_SECTION_KEYS)
    sources = data(await P.get(f"/api/investigations/{iid}/sources"))
    src_ids = {s["id"] for s in sources}
    used = {i for s in rep["sections"] for it in s.get("items", []) for i in it.get("source_ids", [])}
    assert used and used <= src_ids
    assert any(s["duplicates_count"] > 0 for s in sources), "дубли не свёрнуты"
    contacts = data(await P.get(f"/api/investigations/{iid}/contacts"))
    assert any(c["phone"] for c in contacts)
    html = assert_clean_html(await P.get(f"/investigations/{iid}", params={"tab": "contacts"}))
    phone = next(c["phone"] for c in contacts if c["phone"])
    assert "tel:" in html and phone.replace(" ", "")[-4:] in html.replace(" ", "")
    assert_clean_html(await P.get(f"/investigations/{iid}", params={"tab": "report"}))

    # продюсер отмечает факт
    draft_v1 = data(await P.get(f"/api/investigations/{iid}/drafts"))[0]
    claim = draft_v1["claims_json"][0]
    data(await P.post(f"/api/investigations/{iid}/facts", json={
        "fact_text": claim["text"], "state": "confirmed", "claim_id": claim["id"]}), 201)

    # 4. передать на редактуру
    out = data(await P.post(f"/api/investigations/{iid}/transition",
                            json={"to_status": "editing", "editor_id": users["editor"]}))
    assert out["status"] == "editing"

    # 5. редактор правит, видно diff
    queue = {x["id"] for x in data(await E.get("/api/investigations"))}
    assert iid in queue
    assert_clean_html(await E.get(f"/investigations/{iid}/edit"))
    edited = draft_v1["body"] + "\n\nОбновление: на месте работают экстренные службы."
    d2 = data(await E.post(f"/api/investigations/{iid}/drafts",
                           json={"title": draft_v1["title"], "body": edited}), 201)
    assert d2["version"] == 2
    diff = data(await E.get(f"/api/investigations/{iid}/drafts/diff"))
    assert diff["ratio"] < 1 and any(o["op"] != "equal" for o in diff["ops"])
    edit_html = assert_clean_html(await E.get(f"/investigations/{iid}/edit"))
    assert "экстренные службы" in edit_html
    check(await E.post(f"/api/investigations/{iid}/transition", json={"to_status": "ready"}))

    # 6. SMM публикует
    smm_list = {x["id"] for x in data(await SM.get("/api/investigations"))}
    assert iid in smm_list
    assert_clean_html(await SM.get("/"))
    out = data(await SM.post(f"/api/investigations/{iid}/transition", json={"to_status": "published"}))
    assert out["status"] == "published"

    # 7. продюсер: «норм»
    data(await P.post(f"/api/investigations/{iid}/feedback", json={"verdict": "good", "comment": "норм"}), 201)
    assert_clean_html(await P.get("/feedback"))

    # 8. AI-оператор: доска, метрики, фидбэк
    board = data(await O.get("/api/operator/board"))
    assert iid in {c["id"] for c in board["columns"]["published"]}
    m = data(await O.get("/api/metrics"))
    assert m["investigations_total"] >= 1 and m["by_status"]["published"] >= 1
    assert m["feedback_total"] >= 1 and m["feedback_good_share"] is not None
    assert m["time_to_draft_avg_sec"] is not None and m["time_to_draft_avg_sec"] >= 0
    assert m["traceability"] is not None and m["traceability"] > 0
    assert m["draft_acceptance_rate"] is not None
    fb = data(await O.get("/api/feedback"))
    assert any(f["investigation_id"] == iid and f["verdict"] == "good" for f in fb)
    op_html = assert_clean_html(await O.get("/operator"))
    assert inv["title"][:20] in op_html

    hist = data(await O.get(f"/api/investigations/{iid}/history"))
    assert [h["to_status"] for h in hist] == ["new", "researching", "report_ready", "editing", "ready",
                                             "published"]
