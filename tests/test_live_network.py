"""v3: тесты с РЕАЛЬНОЙ сетью (маркер ``live``, по умолчанию исключены в pytest.ini).

Запуск вручную: ``pytest -m live -v``. Проверяют, что внешние источники, на которые опирается live-режим,
действительно отвечают: t.me (resolve), лента из каталога rss.DEFAULT_FEEDS, Google News RSS, и что
все ссылки выдачи поиска открываются (HTTP < 400)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

pytestmark = pytest.mark.live


async def test_live_resolve_telegram():
    from app.integrations.resolve import resolve_source

    r = await resolve_source("https://t.me/telegram")
    assert r["platform"] == "telegram" and r["external_id"] == "tg:telegram"
    assert r["title"] and r["kind"] == "channel"
    assert (r["subscribers"] or 0) > 1_000_000
    assert (r["avatar_url"] or "").startswith("https://")


async def test_live_catalog_feed():
    from app.integrations import rss

    feed = next(f for f in rss.DEFAULT_FEEDS if "m24.ru" in f.url)
    rss.reset_cache()
    items = await rss.fetch_feed(feed)
    assert items, f"{feed.url}: пустая лента"
    assert all(i.url.startswith("http") and i.title for i in items)
    assert any(i.published_at for i in items)


async def test_live_google_news():
    from app.integrations import rss

    rss.reset_cache()
    items = await rss.google_news_search("Москва", limit=10, when="7d")
    assert items, "Google News RSS ничего не вернул"
    assert all(i.url.startswith("https://news.google.com/") and i.title and i.outlet for i in items)


async def test_live_search_urls_open():
    from app.integrations import rss
    from app.integrations.search import RssSearchProvider

    rss.reset_cache()
    res = await RssSearchProvider().search("Москва", limit=8)
    assert res, "поиск live ничего не нашёл по запросу «Москва»"
    from app.integrations.webutil import http_client

    async with http_client(timeout=20) as c:
        async def status(url: str) -> tuple[str, int | str]:
            try:
                r = await c.get(url)
                return url, r.status_code
            except httpx.HTTPError as e:
                return url, repr(e)

        codes = await asyncio.gather(*(status(r.url) for r in res))
    bad = [(u, s) for u, s in codes if not (isinstance(s, int) and s < 400)]
    assert not bad, f"ссылки выдачи не открываются: {bad}"
