"""Интеграция: полный пайплайн через API (PIPELINE_INLINE=true, mock LLM/search/MAX) — п.4, 5 ТЗ."""
from __future__ import annotations

from collections import defaultdict

import pytest

from nf_helpers import CHERTANOVO_LEAD, REPORT_SECTION_KEYS, check, data, require_pipeline

pytestmark = [pytest.mark.api, pytest.mark.pipeline]

STAGE_KEYS = ["input", "search", "intelligence", "media", "dna", "report"]


async def _create(producer, lead=CHERTANOVO_LEAD, channel_id=None):
    require_pipeline()
    if channel_id is None:
        channel_id = data(await producer.get("/api/channels"))[0]["id"]
    return data(await producer.post("/api/investigations", json={
        "lead_text": lead, "channel_id": channel_id, "autorun": True}), 201)


@pytest.fixture
async def chertanovo(producer, operator):
    # гарантируем наличие сообщений MAX в БД (если seed их ещё не подтянул)
    check(await operator.post("/api/max/sync"))
    inv = await _create(producer)
    return inv


async def test_pipeline_reaches_report_ready(producer, chertanovo):
    inv = chertanovo
    assert inv["status"] == "report_ready", "при PIPELINE_INLINE POST /investigations ждёт пайплайн"
    d = data(await producer.get(f"/api/investigations/{inv['id']}"))
    assert d["status"] == "report_ready"
    assert d["report_ready_at"]
    ent = d["entities_json"]
    assert ent and ent.get("keywords") and "summary" in ent


async def test_progress_done(producer, chertanovo):
    p = data(await producer.get(f"/api/investigations/{chertanovo['id']}/progress"))
    prog = p["progress"]
    assert prog["state"] == "done", f"progress: {prog}"
    assert prog["percent"] == 100
    assert prog.get("error") in (None, "")
    assert [s["key"] for s in prog["stages"]] == STAGE_KEYS
    assert all(s["status"] == "done" for s in prog["stages"])
    assert prog["log"] and all({"ts", "stage", "level", "message"} <= set(x) for x in prog["log"])
    assert len(p["iterations"]) >= 2


async def test_chain_search_iterations(producer, chertanovo):
    its = data(await producer.get(f"/api/investigations/{chertanovo['id']}/iterations"))
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


async def test_sources_kinds_clusters_and_primary(producer, chertanovo):
    src = data(await producer.get(f"/api/investigations/{chertanovo['id']}/sources"))
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
    dups = [s for s in src if not s["is_primary"]]
    assert all(s["uniqueness_score"] < 0.5 for s in dups), "дубль не может быть уникальным"


async def test_report_9_sections_and_source_ids(producer, chertanovo):
    inv_id = chertanovo["id"]
    rep = data(await producer.get(f"/api/investigations/{inv_id}/report"))["report_json"]
    assert [s["key"] for s in rep["sections"]] == REPORT_SECTION_KEYS
    assert all(s.get("title") for s in rep["sections"])
    src_ids = {s["id"] for s in data(await producer.get(f"/api/investigations/{inv_id}/sources"))}
    total = with_src = 0
    bad = []
    for sec in rep["sections"]:
        assert isinstance(sec.get("items", []), list)
        for it in sec.get("items", []):
            total += 1
            assert {"id", "text", "source_ids"} <= set(it), it
            ids = set(it["source_ids"] or [])
            with_src += bool(ids)
            if not ids <= src_ids:
                bad.append((sec["key"], it["id"], sorted(ids - src_ids)))
    assert not bad, f"source_ids ссылаются на несуществующие источники: {bad}"
    assert total >= 5 and with_src >= 3
    non_empty = {s["key"] for s in rep["sections"] if s.get("items") or s.get("text")}
    assert {"summary", "facts", "timeline", "sources"} <= non_empty


async def test_contacts_have_phones(producer, chertanovo):
    inv_id = chertanovo["id"]
    contacts = data(await producer.get(f"/api/investigations/{inv_id}/contacts"))
    assert contacts, "блок «Кому позвонить» пуст"
    assert any(c["phone"] for c in contacts), "хотя бы у одного контакта должен быть телефон"
    src = {s["id"]: s for s in data(await producer.get(f"/api/investigations/{inv_id}/sources"))}
    for c in contacts:
        s = src[c["source_id"]]
        assert s["kind"] == "max_chat" and s["is_primary"] and s["is_important"]
        assert s["uniqueness_score"] >= 0.5
        assert c["quote"] and c["author_external_id"]
        assert (c["message_url"] or "").startswith("max://chat/")


async def test_draft_v1_claims(producer, chertanovo):
    inv_id = chertanovo["id"]
    drafts = data(await producer.get(f"/api/investigations/{inv_id}/drafts"))
    assert len(drafts) == 1
    d = drafts[0]
    assert d["version"] == 1 and d["author_kind"] == "ai" and d["body"].strip() and d["title"].strip()
    src_ids = {s["id"] for s in data(await producer.get(f"/api/investigations/{inv_id}/sources"))}
    assert d["claims_json"], "черновик без утверждений"
    for c in d["claims_json"]:
        assert {"id", "text", "source_ids", "status"} <= set(c)
        assert c["text"] in d["body"], f"claim {c['id']} не подстрока body: {c['text']!r}"
        assert c["source_ids"] and set(c["source_ids"]) <= src_ids, f"claim {c['id']}: {c['source_ids']}"
        assert c["status"] in ("confirmed", "unconfirmed", "false")


async def test_media_cards(producer, chertanovo):
    media = data(await producer.get(f"/api/investigations/{chertanovo['id']}/media"))
    assert media, "MEDIA FINDER не нашёл ни одной карточки"
    for m in media:
        assert m["url"] and m["kind"] in ("photo", "video")
        assert m["rights_status"] == "not_verified"
    assert any(m["possibly_old"] for m in media) or len(media) >= 1


async def test_history_system_transition(producer, chertanovo, users):
    hist = data(await producer.get(f"/api/investigations/{chertanovo['id']}/history"))
    chain = [(h["from_status"], h["to_status"], h["user_id"]) for h in hist]
    assert chain == [(None, "new", users["producer"]), ("new", "researching", users["producer"]),
                     ("researching", "report_ready", None)]


async def test_channel_dna_computed(producer, chertanovo):
    ch = [c for c in data(await producer.get("/api/channels")) if c["id"] == chertanovo["channel_id"]][0]
    assert ch["dna_json"] and ch["dna_json"].get("headline")


async def test_generic_lead(producer, operator):
    check(await operator.post("/api/max/sync"))
    inv = await _create(producer, lead="На складе в Люберцах загорелись паллеты, дым виден с МКАД")
    assert inv["status"] == "report_ready"
    rep = data(await producer.get(f"/api/investigations/{inv['id']}/report"))["report_json"]
    assert [s["key"] for s in rep["sections"]] == REPORT_SECTION_KEYS


async def test_rerun_from_report_ready(producer, chertanovo):
    inv_id = chertanovo["id"]
    out = data(await producer.post(f"/api/investigations/{inv_id}/run"))
    assert out["status"] == "report_ready"
    its = data(await producer.get(f"/api/investigations/{inv_id}/iterations"))
    assert [i["n"] for i in its] == list(range(1, len(its) + 1)), \
        f"после перезапуска итерации не должны дублироваться: {[i['n'] for i in its]}"
    drafts = data(await producer.get(f"/api/investigations/{inv_id}/drafts"))
    assert len({d["version"] for d in drafts}) == len(drafts)


async def test_pipeline_run_from_new(producer):
    require_pipeline()
    inv = data(await producer.post("/api/investigations", json={
        "lead_text": CHERTANOVO_LEAD, "autorun": False}), 201)
    out = data(await producer.post(f"/api/investigations/{inv['id']}/run"))
    assert out["status"] == "report_ready"
