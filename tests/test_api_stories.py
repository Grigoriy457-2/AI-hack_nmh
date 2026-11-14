"""API v2: сюжеты (/api/stories + алиас /api/investigations) — создание, список, детали, источники,
«Кому позвонить» (видит редактор), справка, черновики/diff, отметки фактов (CONTRACTS_v2 §4.3).

Данные — ``review_story`` (собраны прямо в БД), поэтому тесты не зависят от пайплайна.
"""
from __future__ import annotations

import pytest

from app.models import InvestigationStatus as S
from nf_helpers import REPORT_SECTION_KEYS, check, data, make_story

pytestmark = pytest.mark.api

STORY_OUT_KEYS = {
    "id", "title", "lead_text", "status", "status_label", "origin", "emerging_key", "channel_id", "owner_id",
    "created_at", "updated_at", "signal_at", "review_at", "handoff_to", "handoff_channel",
    "handoff_channel_label", "handoff_channel_id", "handoff_comment", "handed_off_at", "handed_off_by_id",
}
DETAIL_EXTRA = {"entities_json", "progress_json", "channel", "owner", "handed_off_by", "deeper_questions",
                "allowed_transitions", "unconfirmed_count"}


# ================================================================ создание / список / детали
async def test_create_without_autorun(editor, users):
    inv = data(await editor.post("/api/stories",
                                 json={"lead_text": "У дома 5 странный запах газа", "autorun": False}), 201)
    assert STORY_OUT_KEYS <= set(inv), f"StoryOut: нет {STORY_OUT_KEYS - set(inv)}"
    assert inv["status"] == "new" and inv["status_label"] == "Новый"
    assert inv["owner_id"] == users["editor"] and inv["origin"] == "manual" and inv["emerging_key"] is None
    assert inv["signal_at"], "signal_at = created_at для своей зацепки"
    assert inv["review_at"] is None and inv["handoff_to"] is None
    assert "У дома 5" in inv["title"]
    hist = data(await editor.get(f"/api/stories/{inv['id']}/history"))
    assert [(h["from_status"], h["to_status"]) for h in hist] == [(None, "new")]
    prog = data(await editor.get(f"/api/stories/{inv['id']}/progress"))
    assert prog["investigation_id"] == inv["id"] and prog["status"] == "new"
    check(await editor.get(f"/api/stories/{inv['id']}/report"), 404)


async def test_create_long_lead_title_truncated(editor):
    lead = "Очень длинная зацепка " * 20
    inv = data(await editor.post("/api/stories", json={"lead_text": lead, "autorun": False}), 201)
    assert 0 < len(inv["title"]) <= 100


async def test_create_validation(editor):
    check(await editor.post("/api/stories", json={"lead_text": "ab"}), 422)
    check(await editor.post("/api/stories", json={}), 422)
    check(await editor.post("/api/stories", json={"lead_text": "   ", "autorun": False}), 422)
    check(await editor.post("/api/stories", json={"lead_text": "Хлопок у дома", "channel_id": 999999}), 422)


async def test_create_with_explicit_title_and_channel(editor):
    chans = data(await editor.get("/api/channels"))
    assert len(chans) >= 3
    inv = data(await editor.post("/api/stories", json={
        "lead_text": "Хлопок у дома", "title": "Мой заголовок", "channel_id": chans[0]["id"],
        "autorun": False}), 201)
    assert inv["title"] == "Мой заголовок" and inv["channel_id"] == chans[0]["id"]


async def test_detail_and_404(editor, review_story, users):
    d = data(await editor.get(f"/api/stories/{review_story['id']}"))
    assert (STORY_OUT_KEYS | DETAIL_EXTRA) <= set(d), f"StoryDetail: нет {(STORY_OUT_KEYS | DETAIL_EXTRA) - set(d)}"
    assert d["status"] == "review" and d["status_label"] == "На проверке"
    assert d["owner"]["login"] == "editor"
    assert d["entities_json"]["keywords"]
    assert set(d["allowed_transitions"]) == {"researching", "handed_off", "rejected"}
    # неподтверждённые: claim c1 (draft) + unconfirmed-1 (report)
    assert d["unconfirmed_count"] == 2
    check(await editor.get("/api/stories/999999"), 404)
    for p in ("sources", "report", "drafts", "history", "media", "facts", "iterations", "progress", "contacts",
              "handoff/check"):
        check(await editor.get(f"/api/stories/999999/{p}"), 404)


async def test_alias_investigations(editor, review_story):
    a = data(await editor.get(f"/api/stories/{review_story['id']}"))
    b = data(await editor.get(f"/api/investigations/{review_story['id']}"))
    assert a == b
    lst = data(await editor.get("/api/investigations"))
    assert review_story["id"] in {x["id"] for x in lst}
    paths = data(await editor.get("/openapi.json"))["paths"]
    assert any(p.startswith("/api/stories") for p in paths)
    assert not any(p.startswith("/api/investigations") for p in paths), "алиас скрыт из OpenAPI"


async def test_list_filters_and_order(editor, editor2, users):
    a = await make_story(users, S.new, minutes_ago=30, title="Старая тема про трамвай")
    b = await make_story(users, S.review, minutes_ago=1, title="Свежая тема про qaмонорельс")
    c = await make_story(users, S.handed_off, owner="editor2", minutes_ago=5, title="Чужая тема про qaмонорельс")
    lst = data(await editor.get("/api/stories"))
    ids = [x["id"] for x in lst]
    assert {a, b, c} <= set(ids), "редактор видит все сюжеты (ролей нет)"
    upd = [x["updated_at"] for x in lst]
    assert upd == sorted(upd, reverse=True), "сортировка updated_at desc"
    assert {x["id"] for x in data(await editor.get("/api/stories", params={"status": "review"}))} >= {b}
    assert all(x["status"] == "review" for x in data(await editor.get("/api/stories", params={"status": "review"})))
    mine = data(await editor2.get("/api/stories", params={"mine": "true"}))
    assert c in {x["id"] for x in mine} and {a, b}.isdisjoint({x["id"] for x in mine})
    assert all(x["owner_id"] == users["editor2"] for x in mine)
    q = {x["id"] for x in data(await editor.get("/api/stories", params={"q": "qaмонорельс"}))}
    assert q == {b, c}
    check(await editor.get("/api/stories", params={"status": "editing"}), 422)


async def test_handoff_channels(editor):
    lst = data(await editor.get("/api/handoff-channels"))
    assert [x["key"] for x in lst] == ["site", "telegram", "max", "tv", "print", "social", "other"]
    assert dict((x["key"], x["label"]) for x in lst)["site"] == "Сайт"


# ================================================================ источники / дубли
async def test_sources_list_and_duplicates(editor, review_story):
    sid = review_story["sources"]
    lst = data(await editor.get(f"/api/stories/{review_story['id']}/sources"))
    assert {s["id"] for s in lst} == set(sid.values())
    by = {s["id"]: s for s in lst}
    assert by[sid["s1"]]["duplicates_count"] == 1, "кластер s1+s2 -> duplicates_count = 1"
    assert by[sid["s3"]]["duplicates_count"] == 0
    assert by[sid["s1"]]["is_primary"] and not by[sid["s2"]]["is_primary"]
    assert by[sid["s1"]]["platform"] == "max", "SourceOut.platform для чатовых источников"
    assert by[sid["s6"]]["platform"] is None
    prim_flags = [s["is_primary"] for s in lst]
    assert prim_flags == sorted(prim_flags, reverse=True), "первоисточники идут первыми"
    prim_uniq = [s["uniqueness_score"] for s in lst if s["is_primary"]]
    assert prim_uniq == sorted(prim_uniq, reverse=True)


async def test_sources_filters(editor, review_story):
    sid = review_story["sources"]
    prim = data(await editor.get(f"/api/stories/{review_story['id']}/sources", params={"primary_only": "true"}))
    assert sid["s2"] not in {s["id"] for s in prim} and all(s["is_primary"] for s in prim)
    web = data(await editor.get(f"/api/stories/{review_story['id']}/sources", params={"kind": "web"}))
    assert [s["id"] for s in web] == [sid["s6"]]


# ================================================================ «Кому позвонить» — видит редактор
@pytest.mark.parametrize("who", ["editor", "editor2"])
async def test_contacts_visible_to_editor(make_client, review_story, who):
    sid = review_story["sources"]
    lst = data(await make_client(who).get(f"/api/stories/{review_story['id']}/contacts"))
    got = {c["source_id"]: c for c in lst}
    assert set(got) == {sid["s1"], sid["s3"]}, (
        "контакты = чат & primary & uniq>=0.5 & important; "
        f"получили source_ids={sorted(got)}, ожидали {[sid['s1'], sid['s3']]}")
    c1 = got[sid["s1"]]
    assert c1["phone"] == "+79990001122" and c1["name"] == "Иван Тестов"
    assert c1["author_external_id"] == "qa-u-phone" and c1["platform"] == "max"
    assert "вспышку" in c1["quote"]
    assert c1["chat_title"] == "QA: ЖК Чертаново Центральное"
    assert c1["message_url"] == "max://chat/qa-chat-1/qa-m1" and c1["sent_at"]
    c3 = got[sid["s3"]]
    assert c3["phone"] is None, "телефон скрыт в профиле -> phone=None"
    assert [c["source_id"] for c in lst] == [sid["s1"], sid["s3"]], "с телефоном — первыми"


# ================================================================ справка / медиа / итерации
async def test_report(editor, review_story):
    r = data(await editor.get(f"/api/stories/{review_story['id']}/report"))
    assert r["investigation_id"] == review_story["id"]
    assert [s["key"] for s in r["report_json"]["sections"]] == REPORT_SECTION_KEYS


async def test_media_and_iterations_empty_ok(editor, review_story):
    assert data(await editor.get(f"/api/stories/{review_story['id']}/media")) == []
    assert data(await editor.get(f"/api/stories/{review_story['id']}/iterations")) == []


# ================================================================ черновики / diff
async def test_drafts_list(editor, review_story):
    lst = data(await editor.get(f"/api/stories/{review_story['id']}/drafts"))
    assert len(lst) == 1 and lst[0]["version"] == 1 and lst[0]["author_kind"] == "ai"


async def test_draft_and_diff_in_review(editor, review_story, users):
    inv = review_story["id"]
    new_body = review_story["body"].replace("выбило окна на 3 этаже", "выбило стёкла в нескольких квартирах")
    d2 = data(await editor.post(f"/api/stories/{inv}/drafts",
                                json={"title": "ХЛОПОК В ЧЕРТАНОВЕ", "body": new_body}), 201)
    assert d2["version"] == 2 and d2["author_kind"] == "editor" and d2["author_user_id"] == users["editor"]
    assert d2["claims_json"] == review_story["claims"], "claims_json=None -> перенос из пред. версии"

    d3 = data(await editor.post(f"/api/stories/{inv}/drafts",
                                json={"title": "t3", "body": new_body + " Upd.", "claims_json": []}), 201)
    assert d3["version"] == 3 and d3["claims_json"] == []
    assert [x["version"] for x in data(await editor.get(f"/api/stories/{inv}/drafts"))] == [1, 2, 3]

    diff = data(await editor.get(f"/api/stories/{inv}/drafts/diff", params={"base": 1, "target": 2}))
    assert diff["base_version"] == 1 and diff["target_version"] == 2 and 0 < diff["ratio"] < 1
    ops = diff["ops"]
    assert ops and {o["op"] for o in ops} <= {"equal", "insert", "delete", "replace"}
    changed_b = " ".join(o["b"] for o in ops if o["op"] in ("insert", "replace"))
    changed_a = " ".join(o["a"] for o in ops if o["op"] in ("delete", "replace"))
    assert "стёкла" in changed_b and "окна" in changed_a
    # склейка ops восстанавливает обе версии
    assert "".join(o["a"] for o in ops) == review_story["body"]
    assert "".join(o["b"] for o in ops) == new_body

    default = data(await editor.get(f"/api/stories/{inv}/drafts/diff"))
    assert default["base_version"] == 1 and default["target_version"] == 3, "base по умолчанию — последняя AI"
    same = data(await editor.get(f"/api/stories/{inv}/drafts/diff", params={"base": 1, "target": 1}))
    assert same["ratio"] == pytest.approx(1.0) and all(o["op"] == "equal" for o in same["ops"])
    check(await editor.get(f"/api/stories/{inv}/drafts/diff", params={"base": 1, "target": 42}), 404)


async def test_draft_editable_in_rejected(editor, review_story):
    inv = review_story["id"]
    check(await editor.post(f"/api/stories/{inv}/reject", json={"comment": "нет"}))
    d = data(await editor.post(f"/api/stories/{inv}/drafts", json={"title": "t", "body": "b"}), 201)
    assert d["version"] == 2


@pytest.mark.parametrize("status", [S.new, S.researching, S.handed_off])
async def test_draft_not_editable(editor, users, status):
    inv = await make_story(users, status)
    check(await editor.post(f"/api/stories/{inv}/drafts", json={"title": "t", "body": "b"}), 409)


async def test_diff_without_drafts_404(editor, users):
    inv = await make_story(users, S.review)
    check(await editor.get(f"/api/stories/{inv}/drafts/diff"), 404)


# ================================================================ отметки фактов
async def test_fact_marks(editor, editor2, review_story, users):
    inv = review_story["id"]
    f = data(await editor.post(f"/api/stories/{inv}/facts", json={
        "fact_text": "Около 14:30 жители дома услышали хлопок", "state": "confirmed",
        "claim_id": "c1", "comment": "дозвонились"}), 201)
    assert f["state"] == "confirmed" and f["claim_id"] == "c1" and f["user_id"] == users["editor"]
    f2 = data(await editor2.post(f"/api/stories/{inv}/facts", json={
        "fact_text": "Около 14:30 жители дома услышали хлопок", "state": "false", "claim_id": "c1"}), 201)
    assert f2["state"] == "false"
    data(await editor.post(f"/api/stories/{inv}/facts", json={
        "fact_text": "Двое пострадавших", "state": "unconfirmed", "claim_id": "unconfirmed-1"}), 201)
    data(await editor.post(f"/api/stories/{inv}/facts", json={"fact_text": "без claim", "state": "confirmed"}), 201)
    lst = data(await editor.get(f"/api/stories/{inv}/facts"))
    c1 = [x for x in lst if x["claim_id"] == "c1"]
    assert len(c1) == 1 and c1[0]["state"] == "false", f"повторная отметка обновляет, а не дублирует: {c1}"
    assert len(lst) == 3
    check(await editor.post(f"/api/stories/{inv}/facts", json={"fact_text": "x", "state": "maybe"}), 422)
    check(await editor.post("/api/stories/999999/facts", json={"fact_text": "x", "state": "confirmed"}), 404)


async def test_fact_marks_change_unconfirmed_count(editor, review_story):
    inv = review_story["id"]
    assert data(await editor.get(f"/api/stories/{inv}"))["unconfirmed_count"] == 2
    data(await editor.post(f"/api/stories/{inv}/facts", json={
        "fact_text": "Около 14:30 жители дома услышали хлопок", "state": "confirmed", "claim_id": "c1"}), 201)
    data(await editor.post(f"/api/stories/{inv}/facts", json={
        "fact_text": "Двое пострадавших", "state": "confirmed", "claim_id": "unconfirmed-1"}), 201)
    assert data(await editor.get(f"/api/stories/{inv}"))["unconfirmed_count"] == 0
    # отметка «ложь» на подтверждённом claim c2 делает его неподтверждённым
    data(await editor.post(f"/api/stories/{inv}/facts", json={
        "fact_text": "выбило окна на 3 этаже", "state": "false", "claim_id": "c2"}), 201)
    chk = data(await editor.get(f"/api/stories/{inv}/handoff/check"))
    assert [(u["id"], u["state"], u["origin"]) for u in chk["unconfirmed"]] == [("c2", "false", "draft")]


# ================================================================ каналы
async def test_channels_and_dna(editor):
    chans = data(await editor.get("/api/channels"))
    assert len(chans) >= 3
    ch = data(await editor.post(f"/api/channels/{chans[0]['id']}/dna"))
    assert ch["dna_json"] and ch["dna_updated_at"]
    check(await editor.post("/api/channels/999999/dna"), 404)
