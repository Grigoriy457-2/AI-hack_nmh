"""E2E v2: полный путь редактора одним тестом (ASGI, настоящий логин, API + страницы).

Вход → Радар (лента MAX + Telegram, бейджи, кросс-платформенный дубль) → возникающий сюжет →
«Исследовать» → справка в review (источники, «Кому позвонить» с телефонами) → «Копать глубже» с
вопросом → правка черновика (diff) → отметки фактов → «Передать дальше» (предупреждения, кому/куда)
→ статистика, SSE story_update, повторная передача 409.
"""
from __future__ import annotations

import asyncio

import pytest

from nf_helpers import REPORT_SECTION_KEYS, TG_LIVE_TEXT, assert_clean_html, check, data, parse_sse

pytestmark = pytest.mark.e2e


async def test_full_editor_path(make_client, users):
    E = make_client(None)
    # 0. вход одной кнопкой «Войти как редактор»
    assert "Войти как редактор" in assert_clean_html(await E.get("/login"))
    r = check(await E.post("/login", data={"password": "demo"}), 303)
    assert r.headers["location"] == "/"
    me = data(await E.get("/api/me"))
    assert me["login"] == "editor"

    # 1. подключаем публичный Telegram-канал (страница «Источники»)
    check(await E.post("/sources/add", data={"platform": "telegram", "ref": "https://t.me/moscow_live_demo"}), 303)
    assert "Москва Live" in assert_clean_html(await E.get("/sources"))

    # 2. Радар: лента MAX + Telegram, кросс-платформенный дубль, возникающий сюжет
    assert_clean_html(await E.get("/"))
    feed = data(await E.get("/api/feed", params={"limit": 500}))
    items = feed["items"]
    assert {m["platform"] for m in items} == {"max", "telegram"}
    live = [m for m in items if m["text"] == TG_LIVE_TEXT]
    tg_live = next(m for m in live if m["platform"] == "telegram")
    assert all(m["badges"]["duplicate_of"] == tg_live["id"] for m in live if m["platform"] == "max")
    cards = data(await E.get("/api/emerging"))
    card = cards[0]
    assert set(card["platforms"]) == {"max", "telegram"} and card["messages_count"] >= 3
    assert tg_live["id"] in card["message_ids"]

    # 3. «Исследовать» -> сюжет из кластера, пайплайн (inline) -> review
    inv = data(await E.post(f"/api/emerging/{card['key']}/investigate"), 201)
    iid = inv["id"]
    assert inv["origin"] == "emerging" and inv["status"] == "review" and inv["signal_at"] == card["first_at"]
    html = assert_clean_html(await E.get(f"/stories/{iid}"))
    for label in ("Копать глубже", "Передать дальше", "Кому позвонить"):
        assert label in html
    prog = data(await E.get(f"/api/stories/{iid}/progress"))
    assert prog["progress"]["state"] == "done" and len(prog["iterations"]) >= 2
    rep = data(await E.get(f"/api/stories/{iid}/report"))["report_json"]
    assert [s["key"] for s in rep["sections"]] == REPORT_SECTION_KEYS
    sources = data(await E.get(f"/api/stories/{iid}/sources"))
    assert any(s["duplicates_count"] > 0 for s in sources), "дубли свёрнуты"
    contacts = data(await E.get(f"/api/stories/{iid}/contacts"))
    assert any(c["phone"] for c in contacts) and "tel:" in html
    assert card["key"] in {c["key"] for c in data(await E.get("/api/emerging"))
                           if c["investigation_id"] == iid}

    # 4. «Копать глубже» с вопросом -> снова review, черновик v+1
    v_before = max(d["version"] for d in data(await E.get(f"/api/stories/{iid}/drafts")))
    deeper = data(await E.post(f"/api/stories/{iid}/deeper", json={"question": "Есть ли записи камер у подъезда"}))
    assert deeper["status"] == "review"
    drafts = data(await E.get(f"/api/stories/{iid}/drafts"))
    assert drafts[-1]["version"] == v_before + 1 and drafts[-1]["author_kind"] == "ai"
    queries = [q["query"] for it in data(await E.get(f"/api/stories/{iid}/iterations")) for q in it["queries_json"]]
    assert any("камер" in q for q in queries)

    # 5. правка черновика -> diff с AI-версией
    ai = drafts[-1]
    claim = ai["claims_json"][0]
    edited_body = ai["body"] + "\n\nРедакция уточняет детали."
    ed = data(await E.post(f"/api/stories/{iid}/drafts", json={"title": ai["title"], "body": edited_body}), 201)
    assert ed["author_kind"] == "editor" and ed["claims_json"] == ai["claims_json"]
    diff = data(await E.get(f"/api/stories/{iid}/drafts/diff"))
    assert diff["base_version"] == ai["version"] and diff["target_version"] == ed["version"]
    assert any(o["op"] == "insert" and "уточняет" in o["b"] for o in diff["ops"])

    # 6. проверка фактов и передача дальше (с SSE story_update параллельно)
    chk = data(await E.get(f"/api/stories/{iid}/handoff/check"))
    assert chk["has_report"] and chk["has_draft"]
    data(await E.post(f"/api/stories/{iid}/facts", json={
        "fact_text": claim["text"], "state": "confirmed", "claim_id": claim["id"], "comment": "дозвонились"}), 201)
    chk2 = data(await E.get(f"/api/stories/{iid}/handoff/check"))
    assert claim["id"] not in {u["id"] for u in chk2["unconfirmed"]}

    E2 = make_client("editor2")

    async def do_handoff():
        await asyncio.sleep(0.4)
        return data(await E.post(f"/api/stories/{iid}/handoff", json={
            "to": "Ночная смена сайта", "channel": "site", "comment": "по камерам — завтра"}))

    stream, ho = await asyncio.gather(E2.get("/api/stream", params={"timeout": 2}), do_handoff())
    assert ho["story"]["status"] == "handed_off" and ho["story"]["handoff_to"] == "Ночная смена сайта"
    assert ho["warnings"] == chk2["warnings"]
    upd = [e["data"] for e in parse_sse(stream.text) if e["event"] == "story_update" and e["data"]["id"] == iid]
    assert upd and upd[-1]["status"] == "handed_off"
    check(await E2.post(f"/api/stories/{iid}/handoff", json={"to": "ТВ", "channel": "tv"}), 409)

    # 7. итог: история, страница, статистика
    hist = [(h["from_status"], h["to_status"]) for h in data(await E.get(f"/api/stories/{iid}/history"))]
    assert hist == [(None, "new"), ("new", "researching"), ("researching", "review"),
                    ("review", "researching"), ("researching", "review"), ("review", "handed_off")]
    page = assert_clean_html(await E.get(f"/stories/{iid}"))
    assert "Ночная смена сайта" in page and "Передано дальше" in page
    st = data(await E.get("/api/stats"))
    assert st["recent_handoffs"][0]["id"] == iid and st["handoffs_by_channel"]["site"] >= 1
    assert st["recent_handoffs"][0]["signal_to_handoff_sec"] > 0
    assert_clean_html(await E.get("/stats"))
    assert_clean_html(await E.get("/stories", params={"status": "handed_off"}))
