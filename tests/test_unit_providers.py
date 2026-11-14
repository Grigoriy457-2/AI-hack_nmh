"""Юнит: mock-провайдеры (MAX, LLM, search), Channel DNA, poller (п.3, 6, 7, 5.5 ТЗ)."""
from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.ai import llm as L
from app.ai.channel_dna import analyze_posts, ensure_channel_dna
from app.integrations.max.client import MaxChat, MaxMessage, MaxUserProfile, get_max_client
from app.integrations.max.mock import MockMaxClient
from app.integrations.search import OFFICIAL_DOMAINS, MockSearchProvider, SearchResult
from app.models import AuthorProfile, Channel, ChatMessage, MaxChat as MaxChatRow
from app.seed import SEED_CHANNELS
from app.services.uniqueness import jaccard, shingles
from nf_helpers import CHERTANOVO_LEAD

pytestmark = pytest.mark.unit


# ================================================================ MockMaxClient
async def _all_messages(c: MockMaxClient) -> list[MaxMessage]:
    out = []
    for ch in await c.list_chats():
        out.extend(await c.fetch_messages(ch.external_id))
    return out


def test_factory_returns_mock_without_token():
    from app.integrations.max.client import set_max_client

    set_max_client(None)
    try:
        assert isinstance(get_max_client(), MockMaxClient)
    finally:
        set_max_client(MockMaxClient())


async def test_mock_max_chats():
    chats = await MockMaxClient().list_chats()
    assert 4 <= len(chats) <= 5
    assert all(isinstance(c, MaxChat) for c in chats)
    ids = [c.external_id for c in chats]
    assert len(set(ids)) == len(ids) and all(isinstance(i, str) and i for i in ids)
    assert any("чертан" in f"{c.title} {c.district}".lower() for c in chats)


async def test_mock_max_messages_shape_and_order():
    c = MockMaxClient()
    total = 0
    for ch in await c.list_chats():
        msgs = await c.fetch_messages(ch.external_id)
        assert all(isinstance(m, MaxMessage) for m in msgs)
        assert all(m.chat_external_id == ch.external_id for m in msgs)
        times = [m.sent_at for m in msgs]
        assert times == sorted(times), "fetch_messages должен возвращать по возрастанию sent_at"
        assert all(isinstance(t, datetime) and t.tzinfo is None for t in times), "naive UTC"
        assert len({m.external_id for m in msgs}) == len(msgs)
        total += len(msgs)
    assert 30 <= total <= 60, f"ожидали ~40 сообщений, получили {total}"


async def test_mock_max_since_and_limit():
    c = MockMaxClient()
    ch = (await c.list_chats())[0]
    msgs = await c.fetch_messages(ch.external_id)
    assert len(msgs) >= 3
    pivot = msgs[1].sent_at
    newer = await c.fetch_messages(ch.external_id, since=pivot)
    assert all(m.sent_at > pivot for m in newer), "since — строго новее"
    assert len(newer) == len([m for m in msgs if m.sent_at > pivot])
    assert len(await c.fetch_messages(ch.external_id, limit=2)) <= 2
    assert await c.fetch_messages(ch.external_id, since=msgs[-1].sent_at) == []


async def test_mock_max_content_relevant_junk_duplicates():
    msgs = await _all_messages(MockMaxClient())
    texts = [m.text.lower() for m in msgs]
    relevant = [t for t in texts if any(k in t for k in ("хлоп", "взрыв", "бахну", "грохот"))]
    assert len(relevant) >= 5, "мало сообщений по сценарию «хлопок в Чертанове»"
    assert len(relevant) < len(texts), "должен быть и мусор"
    assert any(m.has_media for m in msgs), "нужны сообщения с фото/видео"
    sh = [shingles(m.text) for m in msgs]
    dup_pairs = sum(1 for a, b in itertools.combinations(sh, 2) if jaccard(a, b) >= 0.6)
    assert any(m.is_forwarded for m in msgs) or dup_pairs > 0, "нужны пересылки/дубли"
    assert dup_pairs > 0, "нужен хотя бы один копипаст (Jaccard ≥ 0.6) для демонстрации дублей"


async def test_mock_max_profiles_with_and_without_phone():
    c = MockMaxClient()
    msgs = await _all_messages(c)
    authors = {m.author_external_id for m in msgs}
    profiles = [await c.get_user_profile(a) for a in sorted(authors)]
    assert all(isinstance(p, MaxUserProfile) for p in profiles), "у каждого автора есть профиль"
    assert all(p.external_id == a for p, a in zip(profiles, sorted(authors)))
    with_phone = [p for p in profiles if p.phone]
    without = [p for p in profiles if not p.phone]
    assert with_phone and without, "нужны авторы и с телефоном, и без"
    assert all(p.phone.lstrip("+").replace(" ", "").replace("-", "").isdigit() for p in with_phone)
    assert await c.get_user_profile("no-such-user-qa") is None


async def test_mock_max_deterministic():
    a = await _all_messages(MockMaxClient())
    b = await _all_messages(MockMaxClient())
    assert [(m.external_id, m.text, m.sent_at) for m in a] == [(m.external_id, m.text, m.sent_at) for m in b]


async def test_mock_max_important_authors_have_phones():
    """Сценарий «Кому позвонить» в демо: среди авторов релевантных сообщений есть телефоны."""
    c = MockMaxClient()
    msgs = await _all_messages(c)
    rel_authors = {m.author_external_id for m in msgs if "хлоп" in m.text.lower()}
    phones = [(await c.get_user_profile(a)) for a in rel_authors]
    assert any(p and p.phone for p in phones)


# ================================================================ MockLLM
TASKS = ["extract_entities", "chain_hypotheses", "relevance", "importance", "intelligence",
         "media_relation", "report", "draft"]


def test_llm_factory_mock_without_key():
    L.set_llm(None)
    try:
        assert isinstance(L.get_llm(), L.MockLLM)
    finally:
        L.set_llm(L.MockLLM())


@pytest.mark.parametrize("task", TASKS)
async def test_mock_llm_complete_json_every_task(task):
    llm = L.MockLLM()
    res = await llm.complete_json("system", CHERTANOVO_LEAD, None, task=task)
    assert isinstance(res, dict) and res, f"task={task}: пустой/не dict ответ {res!r}"
    again = await llm.complete_json("system", CHERTANOVO_LEAD, None, task=task)
    assert res == again, "MockLLM должен быть детерминированным"


async def test_mock_llm_extract_entities_scenario():
    res = await L.MockLLM().complete_json("s", CHERTANOVO_LEAD, task="extract_entities")
    for k in ("summary", "event_type", "location", "time", "keywords", "status"):
        assert k in res, f"нет ключа {k} (формат entities_json, CONTRACTS §5)"
    assert isinstance(res["keywords"], list) and res["keywords"]
    assert "чертан" in str(res).lower()
    assert res["status"] == "unconfirmed"


async def test_mock_llm_generic_lead():
    lead = "На складе в Люберцах загорелись паллеты, видно дым с МКАД"
    res = await L.MockLLM().complete_json("s", lead, task="extract_entities")
    assert isinstance(res, dict) and res.get("keywords")
    assert "чертан" not in str(res).lower(), "для произвольной зацепки — дженерик, не Чертаново"


async def test_mock_llm_unknown_task_and_text():
    llm = L.MockLLM()
    assert isinstance(await llm.complete_json("s", "что-то", task=None), dict)
    txt = await llm.complete_text("s", CHERTANOVO_LEAD, task="draft")
    assert isinstance(txt, str) and txt.strip()


# ================================================================ MockSearchProvider
async def test_mock_search_basic():
    res = await MockSearchProvider().search("хлопок Чертаново")
    assert res and all(isinstance(r, SearchResult) for r in res)
    assert all(r.title and r.url and r.url.startswith("http") for r in res)
    assert all(r.published_at is None or r.published_at.tzinfo is None for r in res)
    assert len(await MockSearchProvider().search("хлопок Чертаново", limit=2)) <= 2


async def test_mock_search_official_domains():
    """Официальные источники «раскрываются» гипотезами цепочки (пожар / газ / полиция)."""
    p = MockSearchProvider()
    res = []
    for q in ("хлопок Чертаново", "пожар автомобиля Чертаново", "утечка газа Чертаново",
              "полиция Чертаново поджог"):
        res += await p.search(q, domains=list(OFFICIAL_DOMAINS))
    assert res, "mock должен отдавать официальные источники (МЧС/МВД/mos.ru)"
    assert all(any(d in r.url for d in OFFICIAL_DOMAINS) for r in res)


async def test_mock_search_media_has_urls():
    res = await MockSearchProvider().search("хлопок Чертаново фото", source_type="media")
    assert res and any(r.media_urls for r in res)


async def test_mock_search_has_reprints():
    res = await MockSearchProvider().search("хлопок Чертаново", limit=50)
    sh = [shingles(f"{r.title} {r.snippet}") for r in res]
    assert any(jaccard(a, b) >= 0.6 for a, b in itertools.combinations(sh, 2)), \
        "TEAM_PLAN: часть результатов — перепечатки одного текста"


# ================================================================ Channel DNA
@pytest.mark.parametrize("ch", SEED_CHANNELS, ids=[c["handle"] for c in SEED_CHANNELS])
def test_analyze_posts_contract(ch):
    dna = analyze_posts(ch["sample_posts"])
    for k in ("posts_analyzed", "avg_length", "avg_paragraphs", "emoji_per_post", "top_emojis",
              "headline", "uses_hashtags", "top_hashtags", "structure", "tone", "rules"):
        assert k in dna, f"нет ключа {k}"
    assert dna["posts_analyzed"] == len(ch["sample_posts"])
    assert dna["headline"]["style"] in {"caps", "emoji_caps", "plain", "question"}
    assert dna["tone"] in {"срочный", "нейтральный", "аналитический"}
    assert isinstance(dna["rules"], list) and dna["rules"]


def test_analyze_posts_distinguishes_channels():
    urgent = analyze_posts(SEED_CHANNELS[0]["sample_posts"])
    calm = analyze_posts(SEED_CHANNELS[1]["sample_posts"])
    assert urgent["emoji_per_post"] > calm["emoji_per_post"]
    assert urgent["uses_hashtags"] is True and calm["uses_hashtags"] is False


async def test_ensure_channel_dna(seeded, session):
    ch = (await session.execute(select(Channel).order_by(Channel.id))).scalars().first()
    ch.dna_json = None
    dna = await ensure_channel_dna(session, ch)
    assert dna and ch.dna_json == dna and ch.dna_updated_at is not None


# ================================================================ poller
async def test_sync_once_and_idempotency(db_ready, session):
    from app.integrations.max.poller import sync_once

    r1 = await sync_once(session, MockMaxClient())
    assert r1.chats >= 4 and r1.new_messages >= 30 and not r1.errors
    n_chats = await session.scalar(select(func.count()).select_from(MaxChatRow))
    n_msgs = await session.scalar(select(func.count()).select_from(ChatMessage))
    assert n_chats == r1.chats and n_msgs == r1.new_messages
    assert all(c.last_synced_at for c in (await session.execute(select(MaxChatRow))).scalars())
    r2 = await sync_once(session, MockMaxClient())
    assert r2.new_messages == 0
    assert await session.scalar(select(func.count()).select_from(ChatMessage)) == n_msgs


async def test_sync_skips_disabled_chats(db_ready, session):
    from app.integrations.max.poller import sync_once

    await sync_once(session, MockMaxClient())
    chat = (await session.execute(select(MaxChatRow).order_by(MaxChatRow.id))).scalars().first()
    chat.enabled = False
    await session.execute(ChatMessage.__table__.delete().where(ChatMessage.chat_id == chat.id))
    await session.commit()
    await sync_once(session, MockMaxClient())
    cnt = await session.scalar(select(func.count()).select_from(ChatMessage)
                               .where(ChatMessage.chat_id == chat.id))
    assert cnt == 0, "выключенный чат не должен синхронизироваться"


async def test_ensure_author_profile_caches(db_ready, session):
    from app.integrations.max.poller import ensure_author_profile

    c = MockMaxClient()
    msgs = await _all_messages(c)
    with_phone = None
    for a in sorted({m.author_external_id for m in msgs}):
        p = await c.get_user_profile(a)
        if p and p.phone:
            with_phone = p
            break
    assert with_phone
    prof = await ensure_author_profile(session, with_phone.external_id, c)
    assert isinstance(prof, AuthorProfile) and prof.phone == with_phone.phone
    await session.commit()
    row = await session.get(AuthorProfile, with_phone.external_id)
    assert row is not None and row.phone == with_phone.phone
    assert await ensure_author_profile(session, "no-such-user-qa", c) is None
