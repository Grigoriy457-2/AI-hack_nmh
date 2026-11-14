"""Интеграция: полный пайплайн через API v2 (PIPELINE_INLINE=true, mock LLM/search/MAX/Telegram)."""
from __future__ import annotations

from collections import defaultdict

import pytest

from nf_helpers import CHERTANOVO_LEAD, REPORT_SECTION_KEYS, data

pytestmark = [pytest.mark.api, pytest.mark.pipeline]

STAGE_KEYS = ["input", "search", "intelligence", "media", "dna", "report"]


async def _create(c, lead=CHERTANOVO_LEAD, channel_id=None):
    if channel_id is None:
        channel_id = data(await c.get("/api/channels"))[0]["id"]
    return data(await c.post("/api/stories", json={"lead_text": lead, "channel_id": channel_id, "autorun": True}), 201)


@pytest.fixture
async def chertanovo(editor):
    return await _create(editor)


async def test_pipeline_reaches_review(editor, chertanovo):
    inv = chertanovo
    assert inv["status"] == "review", "при PIPELINE_INLINE POST /stories ждёт пайплайн"
    d = data(await editor.get(f"/api/stories/{inv['id']}"))
    assert d["status"] == "review" and d["review_at"] and d["review_at"] >= d["signal_at"]
    ent = d["entities_json"]
    assert ent and ent.get("keywords") and "summary" in ent
    assert set(d["allowed_transitions"]) == {"researching", "handed_off", "rejected"}


async def test_progress_done(editor, chertanovo):
    p = data(await editor.get(f"/api/stories/{chertanovo['id']}/progress"))
    prog = p["progress"]
    assert prog["state"] == "done", f"progress: {prog}"
    assert prog["percent"] == 100 and prog.get("error") in (None, "")
    assert [s["key"] for s in prog["stages"]] == STAGE_KEYS
    assert all(s["status"] == "done" for s in prog["stages"])
    assert prog["log"] and all({"ts", "stage", "level", "message"} <= set(x) for x in prog["log"])
    assert len(p["iterations"]) >= 2


async def test_chain_search_iterations(editor, chertanovo):
    its = data(await editor.get(f"/api/stories/{chertanovo['id']}/iterations"))
    assert 2 <= len(its) <= 3, f"CHAIN SEARCH: ≥2 итерации в демо, ≤3 — получили {len(its)}"
    assert [i["n"] for i in its] == list(range(1, len(its) + 1))
    agents = set()
    for it in its:
        assert it["queries_json"], f"итерация {it['n']} без запросов"
        for q in it["queries_json"]:
            assert {"agent", "query", "results"} <= set(q)
            agents.add(q["agent"])
    assert {"web", "max_chats"} <= agents
    assert all(i["rationale"] for i in its[1:]), "для 2-й и 3-й итерации нужен rationale"


async def test_sources_kinds_clusters_and_primary(editor, chertanovo):
    src = data(await editor.get(f"/api/stories/{chertanovo['id']}/sources"))
    assert len(src) >= 5
    kinds = {s["kind"] for s in src}
    assert "max_chat" in kinds and len(kinds) >= 2
    clusters = defaultdict(list)
    for s in src:
        assert s["cluster_id"] is not None, f"источник {s['id']} без cluster_id"
        assert s["uniqueness_score"] is not None and 0 <= s["uniqueness_score"] <= 1
        clusters[s["cluster_id"]].append(s)
    for cid, members in clusters.items():
        prim = [m for m in members if m["is_primary"]]
        assert len(prim) == 1, f"кластер {cid}: первоисточников {len(prim)}"
        assert prim[0]["duplicates_count"] == len(members) - 1
        dated = [m for m in members if m["published_at"]]
        if prim[0]["published_at"] and dated:
            assert prim[0]["published_at"] == min(m["published_at"] for m in dated), \
                f"кластер {cid}: первоисточник не самый ранний"
    assert any(len(m) > 1 for m in clusters.values()), "дубли должны свернуться хотя бы в один кластер"
    assert all(s["uniqueness_score"] < 0.5 for s in src if not s["is_primary"]), "дубль не может быть уникальным"
    for s in src:
        if s["kind"] == "max_chat":
            assert s["platform"] in ("max", "telegram") and s["chat_message_id"]


async def test_report_9_sections_and_source_ids(editor, chertanovo):
    inv_id = chertanovo["id"]
    rep = data(await editor.get(f"/api/stories/{inv_id}/report"))["report_json"]
    assert [s["key"] for s in rep["sections"]] == REPORT_SECTION_KEYS
    assert all(s.get("title") for s in rep["sections"])
    src_ids = {s["id"] for s in data(await editor.get(f"/api/stories/{inv_id}/sources"))}
    total = with_src = 0
    bad = []
    for sec in rep["sections"]:
        for it in sec.get("items", []):
            total += 1
            assert {"id", "text", "source_ids"} <= set(it), it
            assert it.get("state") in (None, "confirmed", "unconfirmed", "false"), it
            ids = set(it["source_ids"] or [])
            with_src += bool(ids)
            if not ids <= src_ids:
                bad.append((sec["key"], it["id"], sorted(ids - src_ids)))
    assert not bad, f"source_ids ссылаются на несуществующие источники: {bad}"
    assert total >= 5 and with_src >= 3
    item_ids = [it["id"] for s in rep["sections"] for it in s.get("items", [])]
    assert len(item_ids) == len(set(item_ids)), "id элементов справки уникальны"
    non_empty = {s["key"] for s in rep["sections"] if s.get("items") or s.get("text")}
    assert {"summary", "facts", "timeline", "sources"} <= non_empty


async def test_contacts_have_phones(editor, chertanovo):
    inv_id = chertanovo["id"]
    contacts = data(await editor.get(f"/api/stories/{inv_id}/contacts"))
    assert contacts, "блок «Кому позвонить» пуст"
    assert any(c["phone"] for c in contacts), "хотя бы у одного контакта есть телефон"
    src = {s["id"]: s for s in data(await editor.get(f"/api/stories/{inv_id}/sources"))}
    for c in contacts:
        s = src[c["source_id"]]
        assert s["kind"] == "max_chat" and s["is_primary"] and s["is_important"]
        assert s["uniqueness_score"] >= 0.5
        assert c["quote"] and c["author_external_id"] and c["platform"] == "max"
        assert (c["message_url"] or "").startswith("max://chat/")


async def test_contacts_include_telegram_witness(editor, with_telegram):
    """Кросс-платформенный пайплайн: свидетель из TG-чата «Чертаново онлайн» (телефон открыт)
    попадает в «Кому позвонить» с platform=telegram; MAX-копии поста «Москва Live» — не первоисточники."""
    inv = await _create(editor)
    contacts = data(await editor.get(f"/api/stories/{inv['id']}/contacts"))
    tg = [c for c in contacts if c["platform"] == "telegram"]
    assert tg, f"нет Telegram-контактов: {[(c['name'], c['platform']) for c in contacts]}"
    w = next((c for c in tg if c["author_external_id"] == "tg:700101"), None)
    assert w and w["phone"] and w["message_url"].startswith("https://t.me/chertanovo_online_demo/")
    src = data(await editor.get(f"/api/stories/{inv['id']}/sources", params={"kind": "max_chat"}))
    assert {s["platform"] for s in src} == {"max", "telegram"}


async def test_draft_v1_claims(editor, chertanovo):
    inv_id = chertanovo["id"]
    drafts = data(await editor.get(f"/api/stories/{inv_id}/drafts"))
    assert len(drafts) == 1
    d = drafts[0]
    assert d["version"] == 1 and d["author_kind"] == "ai" and d["body"].strip() and d["title"].strip()
    src_ids = {s["id"] for s in data(await editor.get(f"/api/stories/{inv_id}/sources"))}
    assert d["claims_json"], "черновик без утверждений"
    for c in d["claims_json"]:
        assert {"id", "text", "source_ids", "status"} <= set(c)
        assert c["text"] in d["body"], f"claim {c['id']} не подстрока body: {c['text']!r}"
        assert c["source_ids"] and set(c["source_ids"]) <= src_ids, f"claim {c['id']}: {c['source_ids']}"
        assert c["status"] in ("confirmed", "unconfirmed", "false")


async def test_media_cards(editor, chertanovo):
    media = data(await editor.get(f"/api/stories/{chertanovo['id']}/media"))
    assert media, "MEDIA FINDER не нашёл ни одной карточки"
    for m in media:
        assert m["url"] and m["kind"] in ("photo", "video") and m["rights_status"] == "not_verified"


async def test_history_system_transition(editor, chertanovo, users):
    hist = data(await editor.get(f"/api/stories/{chertanovo['id']}/history"))
    chain = [(h["from_status"], h["to_status"], h["user_id"]) for h in hist]
    assert chain == [(None, "new", users["editor"]), ("new", "researching", users["editor"]),
                     ("researching", "review", None)]


async def test_channel_dna_computed(editor, chertanovo):
    ch = [c for c in data(await editor.get("/api/channels")) if c["id"] == chertanovo["channel_id"]][0]
    assert ch["dna_json"] and ch["dna_json"].get("headline")


async def test_generic_lead(editor):
    inv = await _create(editor, lead="На складе в Люберцах загорелись паллеты, дым виден с МКАД")
    assert inv["status"] == "review"
    rep = data(await editor.get(f"/api/stories/{inv['id']}/report"))["report_json"]
    assert [s["key"] for s in rep["sections"]] == REPORT_SECTION_KEYS


async def test_rerun_from_review(editor, chertanovo):
    inv_id = chertanovo["id"]
    out = data(await editor.post(f"/api/stories/{inv_id}/run"))
    assert out["status"] == "review"
    its = data(await editor.get(f"/api/stories/{inv_id}/iterations"))
    assert [i["n"] for i in its] == list(range(1, len(its) + 1)), "после перезапуска итерации не дублируются"
    drafts = data(await editor.get(f"/api/stories/{inv_id}/drafts"))
    assert [d["version"] for d in drafts] == [1, 2], "новый AI-черновик — следующая версия"


async def test_pipeline_run_from_new(editor):
    inv = data(await editor.post("/api/stories", json={"lead_text": CHERTANOVO_LEAD, "autorun": False}), 201)
    assert inv["status"] == "new"
    assert data(await editor.post(f"/api/stories/{inv['id']}/run"))["status"] == "review"
