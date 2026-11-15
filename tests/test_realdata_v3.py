"""v3 «реальные данные» — без сети (fake_net = httpx.MockTransport, сохранённые фрагменты в tests/fixtures).

Покрывает: определение платформы, resolve (t.me → title/kind/subscribers/avatar/превью; max.ru og-теги и
linkInfo; сайт → поиск RSS; already_added; ошибки), POST /api/sources с platform=auto/rss, sync_rss,
GET /api/mode, парсеры RSS/Atom и Google News, RssSearchProvider (раскрытие ссылок Google News,
official-фильтр), пайплайн в live: экстрактивный анализ (каждый пункт со source_ids, «подтверждено» —
только официальные), фильтр релевантности (другой регион отсекается), ни одного выдуманного URL.
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx
import pytest

from app.config import settings
from nf_helpers import check, data
from realdata_helpers import (
    BUS_LEAD,
    CITY_FEED,
    CITY_SITE,
    FIXTURE_ARTICLE_URLS,
    GN_PREFIX,
    OFFICIAL_FEED,
    OLD_GN_TARGET,
    html,
    load,
    old_gn_id,
    route_city_site,
    route_news_world,
    route_telegram,
    xml,
)

pytestmark = pytest.mark.realdata


# ================================================================ определение платформы
@pytest.mark.unit
@pytest.mark.parametrize("ref,platform", [
    ("https://t.me/telegram", "telegram"), ("t.me/s/moscowmap", "telegram"), ("@moscowmap", "telegram"),
    ("tg:moscowmap", "telegram"), ("moscowmap", "telegram"), ("telegram.me/x_chan", "telegram"),
    ("https://max.ru/m24", "max"), ("web.max.ru/m24", "max"), ("-100123456", "max"),
    ("lenta.ru", "rss"), ("https://www.m24.ru/rss.xml", "rss"), ("https://qa-news.example/", "rss"),
])
def test_detect_platform(ref, platform):
    from app.services.sources import detect_platform

    assert detect_platform(ref) == platform


# ================================================================ resolve: Telegram
@pytest.mark.unit
async def test_resolve_telegram_channel(fake_net):
    from app.integrations.telegram.resolve import resolve_telegram

    route_telegram(fake_net)
    r = await resolve_telegram("https://t.me/telegram")
    assert r["platform"] == "telegram" and r["external_id"] == "tg:telegram" and r["username"] == "telegram"
    assert r["title"] == "Telegram News" and r["kind"] == "channel" and r["subscribers"] == 9117111
    assert r["avatar_url"] == "https://cdn4.telesco.pe/file/qa_avatar_telegram.jpg"
    assert r["url"] == "https://t.me/telegram" and r["preview"] is True and r["posts_available"] is True
    assert r["last_post_at"] == datetime(2026, 11, 14, 10, 0)
    n = len(fake_net.calls)
    await resolve_telegram("@telegram")
    assert len(fake_net.calls) == n, "кэш 10 мин: повторный resolve без сети"


@pytest.mark.unit
async def test_resolve_telegram_group_and_errors(fake_net):
    from app.integrations.telegram.client import TelegramError, TelegramSourceError
    from app.integrations.telegram.resolve import resolve_telegram

    fake_net.route("https://t.me/qa_group", html("tme_page_group.html"))
    g = await resolve_telegram("@qa_group")
    assert g["kind"] == "group" and g["subscribers"] == 3120 and g["avatar_url"] is None, "дефолтный логотип — не аватар"
    assert g["note"]
    fake_net.route("https://t.me/qa_nobody_here", html("tme_page_notfound.html"))
    with pytest.raises(TelegramSourceError):
        await resolve_telegram("@qa_nobody_here")
    fake_net.route("https://t.me/qa_gone_404", httpx.Response(404))
    with pytest.raises(TelegramSourceError):
        await resolve_telegram("@qa_gone_404")
    with pytest.raises(TelegramSourceError, match="(?i)приват"):
        await resolve_telegram("https://t.me/+AbCdEf123")
    with pytest.raises(TelegramError):
        await resolve_telegram("@qa_no_network")  # маршрута нет -> ConnectError -> TelegramError


# ================================================================ resolve: MAX
@pytest.mark.unit
async def test_resolve_max_public_page(fake_net):
    from app.integrations.max.public import MaxResolveError, parse_max_page, resolve_max

    fake_net.route("https://max.ru/m24", html("max_m24.html"))
    r = await resolve_max("https://max.ru/m24")
    assert r["platform"] == "max" and r["external_id"] == "m24" and r["username"] == "m24"
    assert r["title"] == "Москва 24" and r["kind"] == "channel" and r["subscribers"] == 267002
    assert r["avatar_url"] == "https://i.oneme.ru/i?r=QA_M24_AVATAR" and r["channel_id"] == "68062315746578"
    assert r["description"].startswith("Рассказываем") and r["url"] == "https://max.ru/m24"
    assert r["posts_available"] is False and r["note"], "постов без входа нет — честно"
    # og-теги и JSON-LD без встроенного linkInfo
    page = re.sub(r"<script>.*?</script>", "", load("max_m24.html"), flags=re.S)
    p = parse_max_page(page, "m24")
    assert p["title"] == "Москва 24" and p["subscribers"] == 267002 and p["avatar_url"].startswith("https://i.oneme.ru/")
    fake_net.route("https://max.ru/qa_missing", httpx.Response(404))
    with pytest.raises(MaxResolveError, match="не найден"):
        await resolve_max("max.ru/qa_missing")
    with pytest.raises(MaxResolveError, match="Bot API"):
        await resolve_max("-100123456")
    with pytest.raises(MaxResolveError):
        await resolve_max("https://max.ru/")


# ================================================================ парсеры RSS / Atom / Google News
@pytest.mark.unit
def test_parse_rss_feed():
    from app.integrations.rss import Feed, parse_feed

    items = parse_feed(load("feed_city.xml").encode(), Feed(CITY_FEED, "Городские новости QA", CITY_SITE))
    assert [i.url for i in items] == ["https://qa-news.example/news/bus-fire", "https://qa-news.example/news/nsk-bus",
                                      "https://qa-news.example/news/school"]
    bus = items[0]
    assert bus.title == "В Москве на Варшавском шоссе загорелся автобус"
    assert "<p>" not in bus.summary and "эвакуированы" in bus.summary
    assert bus.published_at and bus.published_at.tzinfo is None
    assert bus.image_url == "https://qa-news.example/img/bus.jpg" and bus.outlet == "Городские новости QA"
    assert bus.feed_url == CITY_FEED
    atom = parse_feed(load("feed_atom.xml").encode())
    assert [(a.title, a.url) for a in atom] == [("Атом: перекрыта улица Полбина", "https://atom.example/a1")]
    assert atom[0].published_at == datetime(2026, 11, 15, 6, 0)
    assert parse_feed(b"<rss><channel><item>broken") == []


@pytest.mark.unit
def test_sources_parse_feed_and_detection():
    from app.services.sources import looks_like_feed, parse_feed

    meta, items = parse_feed(load("feed_city.xml").encode())
    assert meta["title"] == "Городские новости QA" and meta["site_url"] == "https://qa-news.example/"
    assert meta["avatar_url"] == "https://qa-news.example/logo.png"
    assert len(items) == 3 and items[0]["media_url"] == "https://qa-news.example/img/bus.jpg"
    assert all(i["published_at"].tzinfo is None for i in items)
    meta_a, items_a = parse_feed(load("feed_atom.xml").encode())
    assert meta_a["title"] == "QA Atom" and items_a[0]["url"] == "https://atom.example/a1"
    assert looks_like_feed(load("feed_city.xml").encode()) and looks_like_feed(load("feed_atom.xml").encode())
    assert not looks_like_feed(load("site_with_rss.html").encode(), "text/html")


@pytest.mark.unit
def test_google_news_helpers():
    from app.integrations import rss

    u = rss.google_news_url("пожар Москва", "7d")
    assert u.startswith("https://news.google.com/rss/search?q=") and u.endswith("&hl=ru&gl=RU&ceid=RU:ru")
    assert "when%3A7d" in u
    assert rss.decode_google_news_id(f"https://news.google.com/rss/articles/{old_gn_id()}?oc=5") == OLD_GN_TARGET
    assert rss.decode_google_news_id("https://news.google.com/rss/articles/CBMiQUFVX3lxTFFBX05FV19GT1JNQVRfSUQ") is None
    assert rss.split_gn_title("Заголовок - Издание", "Издание") == "Заголовок"
    assert rss.feed_ext_id(CITY_FEED) == f"rss:{CITY_FEED}"
    assert rss.feed_ext_id("https://x.example/" + "a" * 200).startswith("rss:sha1:")


@pytest.mark.unit
async def test_google_news_search_parse(fake_net):
    from app.integrations import rss

    fake_net.route(GN_PREFIX, xml("gn_search.xml"))
    items = await rss.google_news_search("автобус Варшавское шоссе", when="7d")
    assert [i.outlet for i in items] == ["Городские новости QA", "Вести QA", "Другое издание QA"]
    assert items[0].title == "В Москве на Варшавском шоссе загорелся автобус", "« - Издание» отрезано"
    assert all(i.url.startswith("https://news.google.com/rss/articles/") for i in items)
    assert all(i.extra["google_news_url"] == i.url for i in items)
    assert items[0].outlet_url == CITY_SITE
    assert fake_net.calls[-1].startswith(GN_PREFIX + "?q=")
    fake_net.route(GN_PREFIX, httpx.Response(503))
    rss.reset_cache()
    assert await rss.google_news_search("другой запрос") == [], "ошибка Google News -> [] без исключения"


@pytest.mark.unit
async def test_rss_search_provider_resolves_links(fake_net, monkeypatch):
    from app.integrations.search import RssSearchProvider

    route_news_world(fake_net)
    monkeypatch.setattr(settings, "RSS_FEEDS", f"{CITY_FEED},{OFFICIAL_FEED}")
    res = await RssSearchProvider(google_news=True, when="7d").search("автобус Варшавское шоссе Москва", limit=10)
    urls = [r.url for r in res]
    assert "https://qa-news.example/news/bus-fire" in urls
    assert OLD_GN_TARGET in urls, "старый формат id Google News раскрыт без сети"
    assert len(urls) == len(set(urls)), "дедуп: тот же материал из ленты и Google News — один раз"
    unresolved = [r for r in res if r.url.startswith("https://news.google.com/")]
    assert all(r.extra["resolved"] is False and r.extra["google_news_url"] == r.url for r in unresolved)
    allowed = FIXTURE_ARTICLE_URLS | {r.extra.get("google_news_url") for r in res}
    assert set(urls) <= allowed, f"выдуманные ссылки: {set(urls) - allowed}"
    off = next(r for r in res if r.url.startswith("https://www.mos.ru/"))
    assert off.source_type == "official" and off.extra["via"] == "rss"
    official = await RssSearchProvider(google_news=True).search(
        "автобус Варшавское шоссе Москва", domains=["mos.ru"], limit=10)
    assert official and all("mos.ru" in r.url or "mos.ru" in (r.extra.get("outlet_url") or "") for r in official)


# ================================================================ API: режим
@pytest.mark.api
async def test_api_mode_demo(anon):
    m = data(await anon.get("/api/mode"))
    assert m["data_mode"] == "demo" and m["llm"] == "mock"
    assert m["telegram_provider"] == "mock" and m["max_provider"] == "mock"
    assert isinstance(m["search_providers"], list) and isinstance(m["notes"], list)
    assert m["label"]


@pytest.mark.api
async def test_api_mode_live(anon, monkeypatch):
    from app.ai.llm import set_llm
    from app.integrations.search import set_search_provider

    monkeypatch.setattr(settings, "DATA_MODE", "live")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    set_llm(None)
    set_search_provider(None)
    m = data(await anon.get("/api/mode"))
    assert m["data_mode"] == "live" and m["llm"] == "extractive" and m["label"] == "Реальные данные"
    assert set(m["search_providers"]) & {"rss", "google_news"}, m["search_providers"]
    assert m["rss_ingest"] is True and m["notes"]


# ================================================================ API: resolve
PREVIEW_KEYS = {"platform", "platform_label", "ref", "external_id", "title", "username", "kind", "avatar_url",
                "subscribers", "description", "url", "site_url", "already_added", "chat_id", "error", "cached"}


@pytest.mark.api
async def test_api_resolve_telegram(editor, fake_net):
    route_telegram(fake_net)
    r = data(await editor.post("/api/sources/resolve", json={"ref": "https://t.me/telegram"}))
    assert set(r) == PREVIEW_KEYS
    assert r["platform"] == "telegram" and r["platform_label"] == "Telegram" and r["external_id"] == "tg:telegram"
    assert r["title"] == "Telegram News" and r["kind"] == "channel" and r["subscribers"] == 9117111
    assert r["avatar_url"].startswith("https://cdn4.telesco.pe/") and r["error"] is None
    assert r["already_added"] is False and r["chat_id"] is None and r["cached"] is False
    again = data(await editor.post("/api/sources/resolve", json={"ref": "@telegram", "platform": "auto"}))
    assert again["cached"] is True and again["title"] == "Telegram News"
    assert data(await editor.get("/api/sources/chats", params={"platform": "telegram"})) == [], "resolve не сохраняет"


@pytest.mark.api
async def test_api_resolve_already_added_mock_tg(editor, with_telegram, fake_net):
    r = data(await editor.post("/api/sources/resolve", json={"ref": "@moscow_live_demo"}))
    assert r["title"] == "Москва Live (демо)" and r["already_added"] is True
    assert r["chat_id"] == next(c["id"] for c in with_telegram if c["external_id"] == "tg:moscow_live_demo")
    assert not fake_net.calls, "демо-каналы mock-Telegram — без сети"


@pytest.mark.api
async def test_api_resolve_max(editor, fake_net):
    fake_net.route("https://max.ru/m24", html("max_m24.html"))
    r = data(await editor.post("/api/sources/resolve", json={"ref": "https://max.ru/m24"}))
    assert r["platform"] == "max" and r["title"] == "Москва 24" and r["subscribers"] == 267002
    assert r["avatar_url"] == "https://i.oneme.ru/i?r=QA_M24_AVATAR" and r["kind"] == "channel"
    assert r["url"] == "https://max.ru/m24" and r["external_id"] == "m24"


@pytest.mark.api
async def test_api_resolve_site_finds_rss(editor, fake_net):
    route_city_site(fake_net)
    r = data(await editor.post("/api/sources/resolve", json={"ref": "qa-news.example"}))
    assert r["platform"] == "rss" and r["platform_label"] == "СМИ (RSS)" and r["error"] is None
    assert r["url"] == CITY_FEED and r["external_id"] == f"rss:{CITY_FEED}"
    assert r["title"] == "Городские новости QA" and r["kind"] == "feed"
    assert (r["site_url"] or "").startswith(CITY_SITE)
    direct = data(await editor.post("/api/sources/resolve", json={"ref": CITY_FEED}))
    assert direct["url"] == CITY_FEED and direct["error"] is None


@pytest.mark.api
@pytest.mark.parametrize("ref,routes", [
    ("https://no-feed.example/", {"https://no-feed.example": "site_without_rss"}),
    ("https://down.example/", {}),
    ("https://t.me/+AbCdEfGh", {}),
    ("https://t.me/qa_nobody_here", {"https://t.me/qa_nobody_here": "tme_page_notfound.html"}),
    ("https://max.ru/qa_missing", {"https://max.ru/qa_missing": 404}),
])
async def test_api_resolve_errors_are_200_with_error(editor, fake_net, ref, routes):
    for url, what in routes.items():
        if what == 404:
            fake_net.route(url, httpx.Response(404))
        elif what == "site_without_rss":
            fake_net.route(url, httpx.Response(200, text="<html><head><title>Без ленты</title></head></html>",
                                               headers={"content-type": "text/html"}))
        else:
            fake_net.route(url, html(what))
    r = data(await editor.post("/api/sources/resolve", json={"ref": ref}))
    assert r["error"], f"{ref}: ожидали понятную ошибку в error, получили {r}"


@pytest.mark.api
async def test_api_resolve_validation(editor):
    check(await editor.post("/api/sources/resolve", json={"ref": ""}), 422)
    check(await editor.post("/api/sources/resolve", json={"ref": "   "}), 422)
    check(await editor.post("/api/sources/resolve", json={"ref": "x", "platform": "vk"}), 422)


# ================================================================ API: добавление RSS, auto, sync_rss
@pytest.mark.api
@pytest.mark.parametrize("platform,ref", [("rss", CITY_SITE), ("auto", CITY_SITE + "/"), ("rss", CITY_FEED),
                                          ("auto", CITY_FEED)])
async def test_api_add_rss_source(editor, fake_net, platform, ref):
    route_city_site(fake_net)
    r = data(await editor.post("/api/sources", json={"platform": platform, "ref": ref, "district": "ЮАО"}), 201)
    assert r["platform"] == "rss" and r["platform_label"] == "СМИ (RSS)" and r["kind"] == "feed"
    assert r["external_id"] == f"rss:{CITY_FEED}" and r["url"] == CITY_FEED
    assert r["title"] == "Городские новости QA" and r["district"] == "ЮАО"
    assert r["messages_count"] == 2, "первая выгрузка: 2 записи моложе RSS_INGEST_MAX_AGE_HOURS (24 ч)"
    items = data(await editor.get("/api/feed", params={"platform": "rss", "limit": 500}))["items"]
    assert {m["message_url"] for m in items} == {"https://qa-news.example/news/bus-fire",
                                                  "https://qa-news.example/news/nsk-bus"}
    bus = next(m for m in items if m["message_url"].endswith("bus-fire"))
    assert bus["text"].startswith("В Москве на Варшавском шоссе загорелся автобус") and bus["platform_label"] == "СМИ (RSS)"
    assert bus["author_name"] == "Городские новости QA" and bus["has_media"]
    again = data(await editor.post("/api/sources", json={"platform": "rss", "ref": CITY_FEED}), 200)
    assert again["id"] == r["id"]


@pytest.mark.api
async def test_api_add_rss_without_feed_422(editor, fake_net):
    fake_net.route("https://no-feed.example", httpx.Response(200, text="<html><title>x</title></html>",
                                                            headers={"content-type": "text/html"}))
    r = await editor.post("/api/sources", json={"platform": "rss", "ref": "https://no-feed.example/"})
    check(r, 422)
    assert r.json().get("detail")
    r = await editor.post("/api/sources", json={"platform": "auto", "ref": "https://down.example/"})
    check(r, 422)


@pytest.mark.api
async def test_api_add_auto_telegram_and_max(editor, fake_net):
    r = data(await editor.post("/api/sources", json={"platform": "auto", "ref": "@moscow_live_demo"}), 201)
    assert r["platform"] == "telegram" and r["title"] == "Москва Live (демо)"
    fake_net.route("https://max.ru/m24", html("max_m24.html"))
    m = data(await editor.post("/api/sources", json={"platform": "auto", "ref": "https://max.ru/m24"}), 201)
    assert m["platform"] == "max" and m["title"] == "Москва 24" and m["external_id"] == "m24"
    assert m["subscribers"] == 267002 and m["avatar_url"] and m["url"] == "https://max.ru/m24"


@pytest.mark.api
async def test_sync_rss_incremental_and_deleted(editor, fake_net):
    from app import db
    from app.services.sources import sync_rss

    route_city_site(fake_net)
    src = data(await editor.post("/api/sources", json={"platform": "rss", "ref": CITY_FEED}), 201)
    res = data(await editor.post("/api/sources/sync"))
    assert res["by_platform"].get("rss", 0) == 0 and not [e for e in res["errors"] if "Городские" in e]
    # в ленте появилась новая запись
    newer = load("feed_city.xml").replace(
        "<item>", "<item><title>В Москве открыли движение по Варшавскому шоссе</title>"
        "<link>https://qa-news.example/news/reopened</link><pubDate>"
        + load("feed_official.xml").split("<pubDate>")[1].split("</pubDate>")[0] + "</pubDate></item><item>", 1)
    fake_net.route(CITY_FEED, httpx.Response(200, content=newer.encode(), headers={"content-type": "application/rss+xml"}))
    async with db.SessionLocal() as s:
        r = await sync_rss(s)
    assert r.new_messages == 1 and r.by_platform == {"rss": 1}
    data(await editor.delete(f"/api/sources/chats/{src['id']}"))
    async with db.SessionLocal() as s:
        r2 = await sync_rss(s)
    assert r2.chats == 0, "удалённый RSS-источник не выгружается"
    assert data(await editor.get("/api/feed", params={"platform": "rss"}))["items"] == []


@pytest.mark.api
async def test_sync_rss_feed_errors(editor, fake_net):
    from app import db
    from app.services.sources import sync_rss

    route_city_site(fake_net)
    data(await editor.post("/api/sources", json={"platform": "rss", "ref": CITY_FEED}), 201)
    fake_net.route(CITY_FEED, httpx.Response(500))
    async with db.SessionLocal() as s:
        r = await sync_rss(s)
    assert r.errors and r.new_messages == 0


# ================================================================ пайплайн в live (экстрактивно, без сети)
@pytest.fixture
async def live_world(editor, fake_net, monkeypatch):
    """DATA_MODE=live + ExtractiveLLM + RssSearchProvider на сохранённых лентах / Google News.
    Чаты демо-БД выключены: источники сюжета — только из «сети»."""
    from app.ai.extractive import ExtractiveLLM
    from app.ai.llm import set_llm
    from app.integrations.search import RssSearchProvider, set_search_provider

    for ch in data(await editor.get("/api/sources/chats")):
        data(await editor.patch(f"/api/sources/chats/{ch['id']}", json={"enabled": False}))
    route_news_world(fake_net)
    monkeypatch.setattr(settings, "DATA_MODE", "live")
    monkeypatch.setattr(settings, "RSS_FEEDS", f"{CITY_FEED},{OFFICIAL_FEED}")
    set_llm(ExtractiveLLM())
    set_search_provider(RssSearchProvider(google_news=True, when="7d"))
    inv = data(await editor.post("/api/stories", json={"lead_text": BUS_LEAD, "autorun": True}), 201)
    assert inv["status"] == "review", inv
    sources = data(await editor.get(f"/api/stories/{inv['id']}/sources"))
    rep = data(await editor.get(f"/api/stories/{inv['id']}/report"))["report_json"]
    drafts = data(await editor.get(f"/api/stories/{inv['id']}/drafts"))
    media = data(await editor.get(f"/api/stories/{inv['id']}/media"))
    return {"inv": inv, "sources": sources, "report": rep, "drafts": drafts, "media": media,
            "gn_urls": {u for u in re.findall(r"<link>(https://news\.google\.com/[^<]+)</link>", load("gn_search.xml"))}}


@pytest.mark.pipeline
@pytest.mark.api
async def test_live_pipeline_sources_are_real(live_world):
    src = live_world["sources"]
    assert src, "в live пайплайн нашёл источники в лентах/Google News"
    allowed = FIXTURE_ARTICLE_URLS | live_world["gn_urls"]
    urls = [s["url"] for s in src if s["url"]]
    bad = [u for u in urls if u not in allowed]
    assert not bad, f"выдуманные ссылки (нет в ответах «сети»): {bad}"
    assert not [u for u in urls if u.startswith(("max://", "telegram://"))]
    assert not [u for u in urls if re.search(r"\.mock\b|example\.org|mock", u)], urls
    assert "https://qa-news.example/news/bus-fire" in urls
    assert any(s["kind"] == "official" and s["url"].startswith("https://www.mos.ru/") for s in src)


@pytest.mark.pipeline
@pytest.mark.api
async def test_live_pipeline_relevance_filter(live_world):
    urls = {s["url"] for s in live_world["sources"]}
    assert "https://qa-news.example/news/nsk-bus" not in urls, "материал другого региона (Новосибирск) отсечён"
    assert "https://qa-news.example/news/school" not in urls, "нерелевантный материал отсечён"


@pytest.mark.pipeline
@pytest.mark.api
async def test_live_report_every_item_has_sources(live_world):
    rep = live_world["report"]
    src = {s["id"]: s for s in live_world["sources"]}
    keys = [s["key"] for s in rep["sections"]]
    assert keys == ["summary", "timeline", "facts", "sources", "confirmed", "unconfirmed", "contradictions",
                    "media", "to_verify"]
    for sec in rep["sections"]:
        for it in sec.get("items") or []:
            if sec["key"] in ("timeline", "facts", "confirmed", "unconfirmed", "contradictions"):
                assert it["source_ids"], f"{sec['key']}/{it['id']}: пункт без источника в live"
            assert set(it.get("source_ids") or []) <= set(src), f"{sec['key']}/{it['id']}: чужие source_ids"
    confirmed = next(s for s in rep["sections"] if s["key"] == "confirmed")
    for it in confirmed["items"]:
        kinds = {src[i]["kind"] for i in it["source_ids"]}
        assert kinds == {"official"}, f"«подтверждено» не по официальному источнику: {it} {kinds}"
    contra = next(s for s in rep["sections"] if s["key"] == "contradictions")
    assert contra["items"] or contra.get("text"), "пустые противоречия — с пояснением режима"
    text = str(rep) + str(live_world["drafts"])
    assert "Чертанов" not in text, "синтетика сценария «Чертаново» в live"


@pytest.mark.pipeline
@pytest.mark.api
async def test_live_draft_claims_sourced(live_world):
    d = live_world["drafts"][-1]
    src = {s["id"] for s in live_world["sources"]}
    assert d["author_kind"] == "ai" and d["body"].strip() and d["claims_json"]
    for c in d["claims_json"]:
        assert c["source_ids"] and set(c["source_ids"]) <= src, c
        assert c["text"] in d["body"], f"claim не подстрока черновика: {c['text']!r}"


@pytest.mark.pipeline
@pytest.mark.api
async def test_live_media_only_real_urls(live_world):
    for m in live_world["media"]:
        assert m["url"].startswith("https://"), m
        assert not m["url"].startswith("data:image/svg"), "SVG-заглушки только в demo"
        assert m["url"] in {"https://qa-news.example/img/bus.jpg"}, f"медиа не из ответа ленты: {m['url']}"


@pytest.mark.api
async def test_live_feed_has_no_invented_message_urls(editor, monkeypatch):
    """CONTRACTS_v3 §7.7: в live message_url — реальная ссылка или публичная страница источника, иначе
    null; никаких max:// / telegram://."""
    from app.services import emerging

    monkeypatch.setattr(settings, "DATA_MODE", "live")
    emerging.reset_cache()
    items = data(await editor.get("/api/feed", params={"limit": 500}))["items"]
    assert items
    bad = [m["message_url"] for m in items if m["message_url"] and not m["message_url"].startswith("https://")]
    assert not bad, f"выдуманные ссылки в ленте live: {bad[:5]}"
    for c in data(await editor.get("/api/emerging")):
        for s in c["sample"]:
            assert not (s["message_url"] or "").startswith(("max://", "telegram://"))
