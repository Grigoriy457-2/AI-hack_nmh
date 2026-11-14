"""API v2: статусная модель сюжета, «Передать дальше», «Не подтвердилось», «Копать глубже»
(CONTRACTS_v2 §2, §4.3). Недопустимые переходы — 409, ошибки ввода — 422."""
from __future__ import annotations

import pytest

from app.models import InvestigationStatus as S
from nf_helpers import CHERTANOVO_LEAD, check, data, detail_text, make_story

pytestmark = pytest.mark.api


async def _status(c, inv: int) -> str:
    return data(await c.get(f"/api/stories/{inv}"))["status"]


async def _history(c, inv: int) -> list[tuple]:
    return [(h["from_status"], h["to_status"], h["user_id"]) for h in data(await c.get(f"/api/stories/{inv}/history"))]


# ================================================================ допустимые переходы
async def test_new_to_rejected_and_back_to_review(editor, users):
    inv = await make_story(users, S.new)
    r = data(await editor.post(f"/api/stories/{inv}/reject", json={"comment": "фейк"}))
    assert r["status"] == "rejected" and r["status_label"] == "Не подтвердилось"
    r = data(await editor.post(f"/api/stories/{inv}/reopen", json={"comment": "есть новости"}))
    assert r["status"] == "review"
    hist = data(await editor.get(f"/api/stories/{inv}/history"))
    assert [(h["from_status"], h["to_status"]) for h in hist][-2:] == [("new", "rejected"), ("rejected", "review")]
    assert hist[-2]["comment"] == "фейк" and hist[-1]["comment"] == "есть новости"
    assert hist[-1]["user_id"] == users["editor"]


async def test_reject_without_body(editor, users):
    inv = await make_story(users, S.review)
    assert data(await editor.post(f"/api/stories/{inv}/reject"))["status"] == "rejected"
    assert data(await editor.post(f"/api/stories/{inv}/reopen"))["status"] == "review"


async def test_stop_researching(editor, users):
    inv = await make_story(users, S.researching)
    r = data(await editor.post(f"/api/stories/{inv}/stop"))
    assert r["status"] == "review" and r["review_at"], "первый выход в review фиксирует review_at"
    assert (await _history(editor, inv))[-1] == ("researching", "review", users["editor"])


async def test_generic_transition_endpoint(editor, users):
    inv = await make_story(users, S.review)
    r = data(await editor.post(f"/api/stories/{inv}/transition", json={"to_status": "rejected", "comment": "x"}))
    assert r["status"] == "rejected"
    r = data(await editor.post(f"/api/stories/{inv}/transition", json={"to_status": "review"}))
    assert r["status"] == "review"


async def test_run_from_new_goes_through_pipeline_to_review(editor, users):
    inv = await make_story(users, S.new, lead_text=CHERTANOVO_LEAD, title="Хлопок в Чертанове")
    r = data(await editor.post(f"/api/stories/{inv}/run"))
    assert r["status"] == "review", "PIPELINE_INLINE: /run ждёт пайплайн"
    hist = await _history(editor, inv)
    assert hist[-2:] == [("new", "researching", users["editor"]), ("researching", "review", None)]


# ================================================================ недопустимые переходы -> 409
BAD = [
    (S.new, "reopen"), (S.new, "stop"), (S.new, "deeper"),
    (S.researching, "run"), (S.researching, "reject"), (S.researching, "reopen"), (S.researching, "deeper"),
    (S.review, "reopen"), (S.review, "stop"),
    (S.handed_off, "run"), (S.handed_off, "reject"), (S.handed_off, "reopen"), (S.handed_off, "deeper"),
    (S.handed_off, "stop"),
    (S.rejected, "run"), (S.rejected, "reject"), (S.rejected, "deeper"), (S.rejected, "stop"),
]


@pytest.mark.parametrize("status,action", BAD, ids=[f"{s.value}-{a}" for s, a in BAD])
async def test_action_not_allowed_409(editor, users, status, action):
    inv = await make_story(users, status)
    r = await editor.post(f"/api/stories/{inv}/{action}", json={})
    check(r, 409)
    assert detail_text(r), "409 должен содержать понятный detail"
    assert await _status(editor, inv) == status.value
    assert len(await _history(editor, inv)) == 1, "неуспешный переход не пишет историю"


BAD_TRANSITIONS = [(S.new, "review"), (S.handed_off, "review"), (S.handed_off, "rejected"),
                   (S.rejected, "new"), (S.review, "new"), (S.researching, "rejected"), (S.review, "review")]


@pytest.mark.parametrize("status,to", BAD_TRANSITIONS, ids=[f"{s.value}->{t}" for s, t in BAD_TRANSITIONS])
async def test_generic_transition_409(editor, users, status, to):
    inv = await make_story(users, status)
    check(await editor.post(f"/api/stories/{inv}/transition", json={"to_status": to}), 409)
    assert await _status(editor, inv) == status.value


@pytest.mark.parametrize("status", [S.new, S.review, S.rejected])
async def test_transition_to_handed_off_requires_handoff(editor, users, status):
    """«Передано дальше» — только через POST /handoff (нужно «кому»)."""
    inv = await make_story(users, status)
    r = await editor.post(f"/api/stories/{inv}/transition", json={"to_status": "handed_off"})
    assert r.status_code in (409, 422), r.text
    assert await _status(editor, inv) == status.value


async def test_transition_validation(editor, users):
    inv = await make_story(users, S.review)
    check(await editor.post(f"/api/stories/{inv}/transition", json={"to_status": "editing"}), 422)
    check(await editor.post(f"/api/stories/{inv}/transition", json={}), 422)
    check(await editor.post("/api/stories/999999/transition", json={"to_status": "rejected"}), 404)
    check(await editor.post("/api/stories/999999/reject"), 404)


# ================================================================ «Передать дальше»
async def test_handoff_check_lists_unconfirmed(editor, review_story):
    chk = data(await editor.get(f"/api/stories/{review_story['id']}/handoff/check"))
    assert chk["ok"] is False and chk["has_report"] and chk["has_draft"]
    got = {(u["id"], u["origin"], u["state"]) for u in chk["unconfirmed"]}
    assert got == {("c1", "draft", "unconfirmed"), ("unconfirmed-1", "report", "unconfirmed")}
    c1 = next(u for u in chk["unconfirmed"] if u["id"] == "c1")
    assert c1["source_ids"] == [review_story["sources"]["s1"]] and c1["text"]
    assert chk["warnings"] and all(isinstance(w, str) and w for w in chk["warnings"])


async def test_handoff_check_empty_story(editor, users):
    inv = await make_story(users, S.review)
    chk = data(await editor.get(f"/api/stories/{inv}/handoff/check"))
    assert chk["has_report"] is False and chk["has_draft"] is False and chk["ok"] is False
    assert chk["unconfirmed"] == [] and len(chk["warnings"]) >= 2


@pytest.mark.parametrize("to", ["", "   "])
async def test_handoff_empty_to_422(editor, review_story, to):
    inv = review_story["id"]
    r = await editor.post(f"/api/stories/{inv}/handoff", json={"to": to, "channel": "site"})
    check(r, 422)
    assert await _status(editor, inv) == "review"


async def test_handoff_bad_channel_422(editor, review_story):
    inv = review_story["id"]
    check(await editor.post(f"/api/stories/{inv}/handoff", json={"to": "Сайт", "channel": "fax"}), 422)
    check(await editor.post(f"/api/stories/{inv}/handoff", json={"to": "Сайт"}), 422)
    check(await editor.post(f"/api/stories/{inv}/handoff",
                            json={"to": "Сайт", "channel": "site", "channel_id": 999999}), 422)
    assert await _status(editor, inv) == "review"


async def test_handoff_with_warnings_then_repeat_409(editor, editor2, review_story, users):
    inv = review_story["id"]
    chans = data(await editor.get("/api/channels"))
    r = data(await editor.post(f"/api/stories/{inv}/handoff", json={
        "to": "Ночная смена сайта", "channel": "site", "channel_id": chans[0]["id"], "comment": "  срочно  "}))
    st = r["story"]
    assert st["status"] == "handed_off" and st["status_label"] == "Передано дальше"
    assert st["handoff_to"] == "Ночная смена сайта" and st["handoff_channel"] == "site"
    assert st["handoff_channel_label"] == "Сайт" and st["handoff_channel_id"] == chans[0]["id"]
    assert st["handoff_comment"] == "срочно" and st["handed_off_at"] and st["handed_off_by_id"] == users["editor"]
    assert r["warnings"], "передача не блокируется, но предупреждает о неподтверждённых"
    assert {u["id"] for u in r["unconfirmed"]} == {"c1", "unconfirmed-1"}
    hist = data(await editor.get(f"/api/stories/{inv}/history"))
    assert (hist[-1]["from_status"], hist[-1]["to_status"], hist[-1]["user_id"]) == \
        ("review", "handed_off", users["editor"])
    assert "Ночная смена сайта" in (hist[-1]["comment"] or "")
    d = data(await editor.get(f"/api/stories/{inv}"))
    assert d["allowed_transitions"] == [] and d["handed_off_by"]["login"] == "editor"
    # повторная передача (в т.ч. другим редактором) — 409, данные первой не перезаписаны
    r2 = await editor2.post(f"/api/stories/{inv}/handoff", json={"to": "ТВ", "channel": "tv"})
    check(r2, 409)
    d = data(await editor.get(f"/api/stories/{inv}"))
    assert d["handoff_to"] == "Ночная смена сайта" and d["handed_off_by_id"] == users["editor"]


async def test_handoff_without_warnings_after_factcheck(editor, review_story):
    inv = review_story["id"]
    for cid, text in (("c1", "Около 14:30 жители дома услышали хлопок"), ("unconfirmed-1", "Двое пострадавших")):
        data(await editor.post(f"/api/stories/{inv}/facts",
                               json={"fact_text": text, "state": "confirmed", "claim_id": cid}), 201)
    chk = data(await editor.get(f"/api/stories/{inv}/handoff/check"))
    assert chk["ok"] is True and chk["warnings"] == [] and chk["unconfirmed"] == []
    r = data(await editor.post(f"/api/stories/{inv}/handoff", json={"to": "Telegram-канал", "channel": "telegram"}))
    assert r["warnings"] == [] and r["unconfirmed"] == [] and r["story"]["handoff_comment"] is None


@pytest.mark.parametrize("status", [S.new, S.researching, S.rejected])
async def test_handoff_wrong_status_409(editor, users, status):
    inv = await make_story(users, status)
    check(await editor.post(f"/api/stories/{inv}/handoff", json={"to": "Сайт", "channel": "site"}), 409)
    assert await _status(editor, inv) == status.value


async def test_handoff_404(editor):
    check(await editor.post("/api/stories/999999/handoff", json={"to": "Сайт", "channel": "site"}), 404)


async def test_handoff_via_alias(editor, review_story):
    r = data(await editor.post(f"/api/investigations/{review_story['id']}/handoff",
                               json={"to": "Печать", "channel": "print"}))
    assert r["story"]["status"] == "handed_off"


# ================================================================ «Копать глубже» (пайплайн inline)
@pytest.fixture
async def researched_story(editor) -> dict:
    channel_id = data(await editor.get("/api/channels"))[0]["id"]
    inv = data(await editor.post("/api/stories", json={
        "lead_text": CHERTANOVO_LEAD, "channel_id": channel_id, "autorun": True}), 201)
    assert inv["status"] == "review", "PIPELINE_INLINE: POST /stories ждёт окончания пайплайна"
    return inv


@pytest.mark.pipeline
async def test_deeper_new_iteration_question_and_draft(editor, researched_story, users):
    inv = researched_story["id"]
    its_before = data(await editor.get(f"/api/stories/{inv}/iterations"))
    drafts = data(await editor.get(f"/api/stories/{inv}/drafts"))
    assert [(d["version"], d["author_kind"]) for d in drafts] == [(1, "ai")]
    # правка редактора — должна сохраниться как прежняя версия
    edit = data(await editor.post(f"/api/stories/{inv}/drafts", json={
        "title": drafts[0]["title"], "body": drafts[0]["body"] + "\n\nПравка редактора."}), 201)
    assert edit["version"] == 2

    question = "Кто владелец сгоревшего Соляриса"
    r = data(await editor.post(f"/api/stories/{inv}/deeper", json={"question": question}))
    assert r["status"] == "review", "после «Копать глубже» пайплайн снова выводит в review"

    d = data(await editor.get(f"/api/stories/{inv}"))
    dq = d["deeper_questions"]
    assert len(dq) == 1 and dq[0]["question"] == question and dq[0]["user_id"] == users["editor"]
    assert dq[0]["asked_at"]

    its = data(await editor.get(f"/api/stories/{inv}/iterations"))
    assert [i["n"] for i in its] == list(range(1, len(its) + 1)), "итерации не дублируются"
    assert len(its) >= len(its_before)
    queries = [q["query"] for it in its for q in it["queries_json"]]
    assert any(question.lower() in q.lower() for q in queries), f"вопрос редактора не попал в запросы: {queries}"
    agents_with_q = {q["agent"] for it in its for q in it["queries_json"] if question.lower() in q["query"].lower()}
    assert {"web", "max_chats"} <= agents_with_q, f"вопрос — ко всем агентам: {agents_with_q}"

    drafts = data(await editor.get(f"/api/stories/{inv}/drafts"))
    assert [(x["version"], x["author_kind"]) for x in drafts] == [(1, "ai"), (2, "editor"), (3, "ai")]
    assert drafts[1]["body"].endswith("Правка редактора.")
    diff = data(await editor.get(f"/api/stories/{inv}/drafts/diff"))
    assert diff["base_version"] == 3 and diff["target_version"] == 3, "base по умолчанию — последняя AI"

    hist = data(await editor.get(f"/api/stories/{inv}/history"))
    tail = [(h["from_status"], h["to_status"], h["user_id"]) for h in hist[-2:]]
    assert tail == [("review", "researching", users["editor"]), ("researching", "review", None)]
    assert question in (hist[-2]["comment"] or "")
    assert data(await editor.get(f"/api/stories/{inv}/report"))["report_json"]["sections"]


@pytest.mark.pipeline
async def test_deeper_adds_one_chain_iteration(editor, researched_story):
    """CONTRACTS_v2 §4.3: «CHAIN SEARCH получает +1 итерацию с запросами по вопросу ко всем агентам»."""
    inv = researched_story["id"]
    before = data(await editor.get(f"/api/stories/{inv}/iterations"))
    data(await editor.post(f"/api/stories/{inv}/deeper", json={"question": "Кто владелец сгоревшего Соляриса"}))
    after = data(await editor.get(f"/api/stories/{inv}/iterations"))
    assert len(after) == len(before) + 1, \
        f"«Копать глубже» = +1 итерация CHAIN SEARCH: было {len(before)}, стало {len(after)}"


@pytest.mark.pipeline
async def test_deeper_empty_question_and_accumulation(editor, researched_story):
    inv = researched_story["id"]
    r = data(await editor.post(f"/api/stories/{inv}/deeper", json={"question": "   "}))
    assert r["status"] == "review"
    assert not data(await editor.get(f"/api/stories/{inv}"))["deeper_questions"]
    data(await editor.post(f"/api/stories/{inv}/deeper"))  # без тела
    data(await editor.post(f"/api/stories/{inv}/deeper", json={"question": "Были ли пострадавшие"}))
    data(await editor.post(f"/api/stories/{inv}/deeper", json={"question": "Что говорит МЧС"}))
    dq = data(await editor.get(f"/api/stories/{inv}"))["deeper_questions"]
    assert [q["question"] for q in dq] == ["Были ли пострадавшие", "Что говорит МЧС"]
    versions = [d["version"] for d in data(await editor.get(f"/api/stories/{inv}/drafts"))]
    assert versions == list(range(1, len(versions) + 1)) and len(versions) == 5


async def test_deeper_question_too_long_422(editor, users):
    inv = await make_story(users, S.review)
    check(await editor.post(f"/api/stories/{inv}/deeper", json={"question": "x" * 1001}), 422)
    assert await _status(editor, inv) == "review"
