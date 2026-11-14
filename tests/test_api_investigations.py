"""API: расследования, источники, контакты, справка, черновики/diff, факты, фидбэк, оператор, MAX.

Данные готовятся напрямую в БД (фикстура ``researched``) — не зависят от пайплайна.
"""
from __future__ import annotations

import pytest

from app.models import InvestigationStatus as S
from nf_helpers import REPORT_SECTION_KEYS, check, data, make_investigation

pytestmark = pytest.mark.api


# ================================================================ создание / список / детали
async def test_create_without_autorun(producer, users):
    inv = data(await producer.post("/api/investigations",
                                   json={"lead_text": "У дома 5 странный запах газа", "autorun": False}), 201)
    assert inv["status"] == "new"
    assert inv["producer_id"] == users["producer"]
    assert inv["title"], "title по умолчанию — начало lead_text"
    assert inv["title"] in "У дома 5 странный запах газа" or "У дома 5" in inv["title"]
    hist = data(await producer.get(f"/api/investigations/{inv['id']}/history"))
    assert [(h["from_status"], h["to_status"]) for h in hist] == [(None, "new")]
    prog = data(await producer.get(f"/api/investigations/{inv['id']}/progress"))
    assert prog["investigation_id"] == inv["id"] and prog["status"] == "new"
    assert isinstance(prog["progress"], dict)
    check(await producer.get(f"/api/investigations/{inv['id']}/report"), 404)


async def test_create_long_lead_title_truncated(producer):
    lead = "Очень длинная зацепка " * 20
    inv = data(await producer.post("/api/investigations", json={"lead_text": lead, "autorun": False}), 201)
    assert 0 < len(inv["title"]) <= 100


async def test_create_validation(producer):
    check(await producer.post("/api/investigations", json={"lead_text": "ab"}), 422)
    check(await producer.post("/api/investigations", json={}), 422)


async def test_create_with_explicit_title_and_channel(producer):
    chans = data(await producer.get("/api/channels"))
    assert len(chans) >= 3
    inv = data(await producer.post("/api/investigations", json={
        "lead_text": "Хлопок у дома", "title": "Мой заголовок", "channel_id": chans[0]["id"],
        "autorun": False}), 201)
    assert inv["title"] == "Мой заголовок" and inv["channel_id"] == chans[0]["id"]


async def test_get_detail_and_404(producer, researched):
    d = data(await producer.get(f"/api/investigations/{researched['id']}"))
    assert d["status"] == "report_ready"
    assert d["producer"]["login"] == "producer"
    assert d["entities_json"]["keywords"]
    check(await producer.get("/api/investigations/999999"), 404)
    for p in ("sources", "report", "drafts", "history", "media", "facts", "iterations", "progress"):
        check(await producer.get(f"/api/investigations/999999/{p}"), 404)


async def test_list_visibility_by_role(clients, users):
    new_id = await make_investigation(users, S.new, title="new-one")
    ed_id = await make_investigation(users, S.editing, title="editing-one")
    ready_id = await make_investigation(users, S.ready, title="ready-one")
    pub_id = await make_investigation(users, S.published, title="pub-one")
    p2_id = await make_investigation(users, S.new, producer="producer2", title="p2-one")

    ids = lambda lst: {x["id"] for x in lst}  # noqa: E731
    op = ids(data(await clients["operator"].get("/api/investigations")))
    assert {new_id, ed_id, ready_id, pub_id, p2_id} <= op

    smm = ids(data(await clients["smm"].get("/api/investigations")))
    assert {ready_id, pub_id} <= smm
    assert not smm & {new_id, ed_id, p2_id}, "SMM видит только ready/published"

    ed = ids(data(await clients["editor"].get("/api/investigations")))
    assert ed_id in ed and new_id not in ed

    mine = ids(data(await clients["producer2"].get("/api/investigations", params={"mine": "true"})))
    assert p2_id in mine and new_id not in mine

    only_ready = data(await clients["operator"].get("/api/investigations", params={"status": "ready"}))
    assert only_ready and all(x["status"] == "ready" for x in only_ready)


async def test_list_sorted_by_updated_desc(operator, users):
    a = await make_investigation(users, S.new, minutes_ago=50)
    b = await make_investigation(users, S.new, minutes_ago=5)
    lst = [x["id"] for x in data(await operator.get("/api/investigations"))]
    assert lst.index(b) < lst.index(a)


# ================================================================ источники / дубли
async def test_sources_list_and_duplicates(producer, researched):
    sid = researched["sources"]
    lst = data(await producer.get(f"/api/investigations/{researched['id']}/sources"))
    assert {s["id"] for s in lst} == set(sid.values())
    by = {s["id"]: s for s in lst}
    assert by[sid["s1"]]["duplicates_count"] == 1, "кластер s1+s2 -> duplicates_count = 1"
    assert by[sid["s3"]]["duplicates_count"] == 0
    assert by[sid["s1"]]["is_primary"] and not by[sid["s2"]]["is_primary"]
    # сортировка: первоисточники раньше, внутри — по uniqueness desc
    prim_flags = [s["is_primary"] for s in lst]
    assert prim_flags == sorted(prim_flags, reverse=True), "primary должны идти первыми"
    prim_uniq = [s["uniqueness_score"] for s in lst if s["is_primary"]]
    assert prim_uniq == sorted(prim_uniq, reverse=True)


async def test_sources_filters(editor, researched):
    sid = researched["sources"]
    prim = data(await editor.get(f"/api/investigations/{researched['id']}/sources",
                                 params={"primary_only": "true"}))
    assert sid["s2"] not in {s["id"] for s in prim} and all(s["is_primary"] for s in prim)
    web = data(await editor.get(f"/api/investigations/{researched['id']}/sources", params={"kind": "web"}))
    assert [s["id"] for s in web] == [sid["s6"]]


# ================================================================ «Кому позвонить»
async def test_contacts_selection(producer, researched):
    sid = researched["sources"]
    lst = data(await producer.get(f"/api/investigations/{researched['id']}/contacts"))
    got = {c["source_id"]: c for c in lst}
    assert set(got) == {sid["s1"], sid["s3"]}, (
        "контакты = max_chat & primary & uniq>=0.5 & important; "
        f"получили source_ids={sorted(got)}, ожидали {[sid['s1'], sid['s3']]}")
    c1 = got[sid["s1"]]
    assert c1["phone"] == "+79990001122" and c1["name"] == "Иван Тестов"
    assert c1["author_external_id"] == "qa-u-phone"
    assert "вспышку" in c1["quote"]
    assert c1["chat_title"] == "QA: ЖК Чертаново Центральное"
    assert c1["message_url"] == "max://chat/qa-chat-1/qa-m1"
    assert c1["sent_at"]
    c3 = got[sid["s3"]]
    assert c3["phone"] is None, "телефон скрыт в профиле -> phone=None"
    assert c3["name"] == "Мария Скрытая"


# ================================================================ справка / медиа / итерации
async def test_report(smm, researched):
    r = data(await smm.get(f"/api/investigations/{researched['id']}/report"))
    assert r["investigation_id"] == researched["id"]
    assert [s["key"] for s in r["report_json"]["sections"]] == REPORT_SECTION_KEYS


async def test_media_and_iterations_empty_ok(editor, researched):
    assert data(await editor.get(f"/api/investigations/{researched['id']}/media")) == []
    assert data(await editor.get(f"/api/investigations/{researched['id']}/iterations")) == []


# ================================================================ черновики / diff
async def _to_editing(producer, inv_id):
    check(await producer.post(f"/api/investigations/{inv_id}/transition", json={"to_status": "editing"}))


async def test_drafts_list(editor, researched):
    lst = data(await editor.get(f"/api/investigations/{researched['id']}/drafts"))
    assert len(lst) == 1 and lst[0]["version"] == 1 and lst[0]["author_kind"] == "ai"


async def test_editor_draft_requires_editing_status(editor, researched):
    r = await editor.post(f"/api/investigations/{researched['id']}/drafts", json={"title": "t", "body": "b"})
    check(r, 409)


async def test_editor_draft_and_diff(producer, editor, researched, users):
    inv = researched["id"]
    await _to_editing(producer, inv)
    new_body = researched["body"].replace("выбило окна на 3 этаже", "выбило стёкла в нескольких квартирах")
    d2 = data(await editor.post(f"/api/investigations/{inv}/drafts",
                                json={"title": "ХЛОПОК В ЧЕРТАНОВЕ", "body": new_body}), 201)
    assert d2["version"] == 2 and d2["author_kind"] == "editor"
    assert d2["author_user_id"] == users["editor"]
    assert d2["claims_json"] == researched["claims"], "claims_json=None -> перенос из пред. версии"

    d3 = data(await editor.post(f"/api/investigations/{inv}/drafts",
                                json={"title": "t3", "body": new_body + " Upd.", "claims_json": []}), 201)
    assert d3["version"] == 3 and d3["claims_json"] == []

    lst = data(await editor.get(f"/api/investigations/{inv}/drafts"))
    assert [x["version"] for x in lst] == [1, 2, 3]

    diff = data(await editor.get(f"/api/investigations/{inv}/drafts/diff", params={"base": 1, "target": 2}))
    assert diff["base_version"] == 1 and diff["target_version"] == 2
    assert 0 < diff["ratio"] < 1
    ops = diff["ops"]
    assert ops and {o["op"] for o in ops} <= {"equal", "insert", "delete", "replace"}
    assert any(o["op"] != "equal" for o in ops)
    changed_b = " ".join(o["b"] for o in ops if o["op"] in ("insert", "replace"))
    changed_a = " ".join(o["a"] for o in ops if o["op"] in ("delete", "replace"))
    assert "стёкла" in changed_b and "окна" in changed_a

    default = data(await editor.get(f"/api/investigations/{inv}/drafts/diff"))
    assert default["base_version"] == 1 and default["target_version"] == 3

    same = data(await editor.get(f"/api/investigations/{inv}/drafts/diff", params={"base": 1, "target": 1}))
    assert same["ratio"] == pytest.approx(1.0)
    assert all(o["op"] == "equal" for o in same["ops"])

    check(await editor.get(f"/api/investigations/{inv}/drafts/diff", params={"base": 1, "target": 42}), 404)


async def test_producer_cannot_save_draft(producer, researched):
    await _to_editing(producer, researched["id"])
    r = await producer.post(f"/api/investigations/{researched['id']}/drafts", json={"title": "t", "body": "b"})
    assert r.status_code == 403


# ================================================================ факты
async def test_fact_marks(producer, editor, smm, researched, users):
    inv = researched["id"]
    f = data(await producer.post(f"/api/investigations/{inv}/facts", json={
        "fact_text": "Около 14:30 жители дома услышали хлопок", "state": "confirmed",
        "claim_id": "c1", "comment": "дозвонились"}), 201)
    assert f["state"] == "confirmed" and f["claim_id"] == "c1" and f["user_id"] == users["producer"]

    # повторная отметка того же claim_id тем же пользователем — обновление
    f2 = data(await producer.post(f"/api/investigations/{inv}/facts", json={
        "fact_text": "Около 14:30 жители дома услышали хлопок", "state": "false", "claim_id": "c1"}), 201)
    assert f2["state"] == "false"
    data(await editor.post(f"/api/investigations/{inv}/facts", json={
        "fact_text": "выбило окна на 3 этаже", "state": "unconfirmed", "claim_id": "c2"}), 201)

    lst = data(await smm.get(f"/api/investigations/{inv}/facts"))
    c1 = [x for x in lst if x["claim_id"] == "c1"]
    assert len(c1) == 1, f"повторная отметка должна обновлять, а не дублировать: {c1}"
    assert c1[0]["state"] == "false"
    assert len(lst) == 2

    check(await smm.post(f"/api/investigations/{inv}/facts",
                         json={"fact_text": "x", "state": "confirmed"}), 403)
    check(await producer.post(f"/api/investigations/{inv}/facts",
                              json={"fact_text": "x", "state": "maybe"}), 422)


# ================================================================ фидбэк
async def test_feedback(producer, operator, editor, researched, users):
    inv = researched["id"]
    fb = data(await producer.post(f"/api/investigations/{inv}/feedback",
                                  json={"verdict": "good", "comment": "норм тема"}), 201)
    assert fb["verdict"] == "good" and fb["investigation_id"] == inv and fb["user_id"] == users["producer"]
    data(await producer.post(f"/api/investigations/{inv}/feedback", json={"verdict": "bad"}), 201)
    lst = data(await operator.get("/api/feedback"))
    mine = [x for x in lst if x["investigation_id"] == inv]
    assert len(mine) == 2
    assert mine[0]["created_at"] >= mine[-1]["created_at"], "лента — created_at desc"
    assert mine[-1]["user"]["login"] == "producer"
    check(await producer.post(f"/api/investigations/{inv}/feedback", json={"verdict": "meh"}), 422)
    check(await producer.post("/api/investigations/999999/feedback", json={"verdict": "good"}), 404)
    check(await editor.get("/api/feedback"), 403)


# ================================================================ оператор: доска / метрики
async def test_operator_board(operator, users):
    fresh = await make_investigation(users, S.editing, editor="editor")
    stale = await make_investigation(users, S.report_ready, minutes_ago=180)
    old_pub = await make_investigation(users, S.published, minutes_ago=600)
    board = data(await operator.get("/api/operator/board"))
    assert set(board["columns"]) == {s.value for s in S}, "все 7 статусов, даже пустые"
    col = {s: {c["id"]: c for c in cards} for s, cards in board["columns"].items()}
    assert fresh in col["editing"] and stale in col["report_ready"] and old_pub in col["published"]
    assert col["editing"][fresh]["responsible"]["login"] == "editor"
    assert col["report_ready"][stale]["responsible"]["login"] == "producer"
    assert col["report_ready"][stale]["is_stale"] is True
    assert col["report_ready"][stale]["age_minutes"] >= 170
    assert col["editing"][fresh]["is_stale"] is False
    assert col["published"][old_pub]["is_stale"] is False, "published не бывает зависшим"
    stale_ids = {c["id"] for c in board["stale"]}
    assert stale in stale_ids and fresh not in stale_ids and old_pub not in stale_ids


async def test_metrics_endpoint(operator, researched):
    m = data(await operator.get("/api/metrics"))
    for k in ("time_to_draft_avg_sec", "source_coverage", "traceability", "draft_acceptance_rate",
              "factcheck_correction_rate", "feedback_good_share", "investigations_total",
              "by_status", "feedback_total"):
        assert k in m
    assert m["investigations_total"] >= 1
    assert m["by_status"].get("report_ready", 0) >= 1
    assert m["traceability"] is not None


# ================================================================ MAX / каналы
async def test_max_chats_after_seed(operator):
    """TEAM_PLAN DEV-BACKEND п.6: seed синхронизирует mock-MAX — чаты есть сразу после старта."""
    chats = data(await operator.get("/api/max/chats"))
    assert len(chats) >= 4
    assert sum(c["messages_count"] for c in chats) >= 30


async def test_max_sync_and_toggle(operator):
    r1 = data(await operator.post("/api/max/sync"))
    assert r1["chats"] >= 4 and not r1["errors"]
    r2 = data(await operator.post("/api/max/sync"))
    assert r2["new_messages"] == 0, "повторный синк не дублирует сообщения"
    chats = data(await operator.get("/api/max/chats"))
    assert len(chats) >= 4 and sum(c["messages_count"] for c in chats) >= 30
    cid = chats[0]["id"]
    upd = data(await operator.patch(f"/api/max/chats/{cid}", json={"enabled": False}))
    assert upd["enabled"] is False and upd["id"] == cid
    again = {c["id"]: c for c in data(await operator.get("/api/max/chats"))}
    assert again[cid]["enabled"] is False
    check(await operator.patch("/api/max/chats/999999", json={"enabled": True}), 404)


async def test_channels_and_dna(operator, smm):
    chans = data(await smm.get("/api/channels"))
    assert len(chans) >= 3
    ch = data(await operator.post(f"/api/channels/{chans[0]['id']}/dna"))
    assert ch["dna_json"] and ch["dna_json"]["posts_analyzed"] >= 1
    assert ch["dna_updated_at"]
    check(await operator.post("/api/channels/999999/dna"), 404)


async def test_run_conflict_when_researching(producer, users):
    inv = await make_investigation(users, S.researching)
    check(await producer.post(f"/api/investigations/{inv}/run"), 409)
    inv2 = await make_investigation(users, S.editing)
    check(await producer.post(f"/api/investigations/{inv2}/run"), 409)
