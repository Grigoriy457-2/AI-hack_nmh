"""Фикстуры v3 «реальные данные» без сети: сохранённые фрагменты t.me / max.ru / RSS / Google News
(tests/fixtures) и маршруты для ``fake_net`` (conftest)."""
from __future__ import annotations

import base64
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx

from app.models import utcnow

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CITY_FEED = "https://qa-news.example/export/rss.xml"
CITY_SITE = "https://qa-news.example"
OFFICIAL_FEED = "https://www.mos.ru/rss"
GN_PREFIX = "https://news.google.com/rss/search"
OLD_GN_TARGET = "https://vesti-qa.example/article/bus-fire"
# Все URL, которые реально «присылает сеть» в фикстурах: только они допустимы как ссылки источников.
FIXTURE_ARTICLE_URLS = {
    "https://qa-news.example/news/bus-fire", "https://qa-news.example/news/nsk-bus",
    "https://qa-news.example/news/school", "https://www.mos.ru/news/item/qa-bus-fire/", OLD_GN_TARGET,
}
BUS_LEAD = "В Москве на Варшавском шоссе загорелся автобус, пассажиров эвакуировали"


def old_gn_id(url: str = OLD_GN_TARGET) -> str:
    """id Google News старого формата: base64(protobuf с URL статьи)."""
    raw = b"\x08\x13\x22" + bytes([len(url)]) + url.encode() + b"\xd2\x01\x00"
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _rfc822(minutes_ago: int) -> str:
    from datetime import UTC

    return format_datetime((utcnow() - timedelta(minutes=minutes_ago)).replace(tzinfo=UTC))


def load(name: str) -> str:
    text = (FIXTURES / name).read_text("utf-8")
    return (text.replace("__PUB1__", _rfc822(60)).replace("__PUB2__", _rfc822(120))
            .replace("__PUB3__", _rfc822(60 * 30)).replace("__OLDFORMAT__", old_gn_id()))


def html(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=load(name), headers={"content-type": "text/html; charset=utf-8"})


def xml(name: str) -> httpx.Response:
    return httpx.Response(200, content=load(name).encode("utf-8"),
                          headers={"content-type": "application/rss+xml; charset=utf-8"})


def empty_rss() -> httpx.Response:
    return httpx.Response(200, content=b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
                                       b"</channel></rss>", headers={"content-type": "application/rss+xml"})


def route_telegram(net, username: str = "telegram") -> None:
    net.route(f"https://t.me/{username}", html("tme_page_channel.html"))
    net.route(f"https://t.me/s/{username}", html("tme_s_channel.html"))


def route_city_site(net) -> None:
    net.route(CITY_SITE, html("site_with_rss.html"))
    net.route(CITY_SITE + "/", html("site_with_rss.html"))
    net.route(CITY_FEED, xml("feed_city.xml"))


def route_news_world(net) -> None:
    """Ленты + Google News для поиска пайплайна в live. Статьи (og:image) — 404."""
    route_city_site(net)
    net.route(OFFICIAL_FEED, xml("feed_official.xml"))
    net.route(GN_PREFIX, xml("gn_search.xml"))
    for u in FIXTURE_ARTICLE_URLS:
        net.route(u, httpx.Response(404))
