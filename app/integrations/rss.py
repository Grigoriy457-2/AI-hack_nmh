"""RSS: каталог проверенных лент, разбор (xml.etree, без feedparser), Google News RSS-поиск,
поиск по лентам СМИ/ведомств, стартовые live-источники (``default_live_sources``). Зона DEV-REALDATA.
Выгрузку RSS-источников в ленту «Радар» делает DEV-SOURCES (``services.sources.sync_rss``).

Все URL каталога ``DEFAULT_FEEDS`` проверены запросом 2026-11-15 (HTTP 200, валидный RSS, свежие
записи) — см. docs/CONTRACTS_v3.md. Ни одна ссылка не конструируется: в результаты попадает только
то, что пришло в ответе ленты (``<link>``, ``<pubDate>``, ``<enclosure>``).

Google News: ``https://news.google.com/rss/search?q=…&hl=ru&gl=RU&ceid=RU:ru``. Ссылки вида
``news.google.com/rss/articles/<id>`` без JS ведут на consent.google.com, а id нового формата
(``AU_yqL…``) не содержит URL статьи. Раскрываем так: (1) id старого формата — base64 с URL внутри;
(2) совпадение заголовка с записью из лент каталога (тот же материал издания) → прямая ссылка;
иначе остаётся рабочая ссылка Google News (``extra["google_news_url"]``, ``extra["resolved"]=False``).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

from app.integrations.webutil import TTLCache, host_of, http_client, parse_rfc822, strip_html

log = logging.getLogger("newsflow.rss")

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"


@dataclass(frozen=True, slots=True)
class Feed:
    url: str
    title: str
    site: str
    kind: str = "news"  # news | official
    ingest: bool = False  # московская лента → в «Радар» (platform=rss)


# Проверено запросом (HTTP 200 + валидный RSS + свежие записи), 2026-11-15.
# Не работают/не включены: tass.ru/rss/v2.xml (403 для ботов), moslenta.ru/rss (редирект на auth),
# ria.ru/export/rss2/moscow (404), mk.ru/rss/moscow (404), gazeta.ru/export/rss/lenta.xml (404),
# 77.mchs.gov.ru (DNS).
DEFAULT_FEEDS: tuple[Feed, ...] = (
    Feed("https://www.mos.ru/rss", "mos.ru — новости Москвы", "https://www.mos.ru", "official", True),
    Feed("https://www.m24.ru/rss.xml", "Москва 24", "https://www.m24.ru", "news", True),
    Feed("https://www.mskagency.ru/rss/index.rss", "Агентство городских новостей «Москва»",
         "https://www.mskagency.ru", "news", True),
    Feed("https://lenta.ru/rss/news/russia/moscow", "Лента.ру — Москва", "https://lenta.ru", "news", True),
    Feed("https://msk1.ru/rss-feeds/rss.xml", "MSK1.RU", "https://msk1.ru", "news", True),
    Feed("https://vm.ru/rss", "Вечерняя Москва", "https://vm.ru", "news", True),
    Feed("https://mchs.gov.ru/deyatelnost/press-centr/novosti/rss", "МЧС России", "https://mchs.gov.ru", "official"),
    Feed("https://ria.ru/export/rss2/archive/index.xml", "РИА Новости", "https://ria.ru"),
    Feed("https://www.interfax.ru/rss", "Интерфакс", "https://www.interfax.ru"),
    Feed("https://www.kommersant.ru/rss/news.xml", "Коммерсантъ", "https://www.kommersant.ru"),
    Feed("https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "РБК", "https://www.rbc.ru"),
    Feed("https://iz.ru/xml/rss/all.xml", "Известия", "https://iz.ru"),
    Feed("https://www.vedomosti.ru/rss/news", "Ведомости", "https://www.vedomosti.ru"),
    Feed("https://lenta.ru/rss", "Лента.ру", "https://lenta.ru"),
)


def configured_feeds() -> list[Feed]:
    """RSS_FEEDS (через запятую) заменяет каталог; иначе DEFAULT_FEEDS."""
    from app.config import settings

    raw = [u.strip() for u in (settings.RSS_FEEDS or "").replace(";", ",").split(",") if u.strip()]
    if not raw:
        return list(DEFAULT_FEEDS)
    known = {f.url: f for f in DEFAULT_FEEDS}
    out = []
    for u in raw:
        if u in known:
            out.append(known[u])
        else:
            from app.integrations.search import is_official_url

            out.append(Feed(u, host_of(u) or u, f"https://{host_of(u)}",
                            "official" if is_official_url(u) else "news", True))
    return out


@dataclass(slots=True)
class FeedItem:
    title: str
    url: str
    summary: str = ""
    published_at: datetime | None = None
    outlet: str | None = None  # название издания
    outlet_url: str | None = None
    image_url: str | None = None  # enclosure / media:content image
    guid: str | None = None
    feed_url: str | None = None
    kind: str = "news"
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- parse
def _text(el: ET.Element | None, tag: str) -> str:
    if el is None:
        return ""
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def _image_of(it: ET.Element) -> str | None:
    for enc in it.findall("enclosure"):
        t = (enc.get("type") or "").lower()
        u = enc.get("url")
        if u and (t.startswith("image") or u.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))):
            return u
    for tag in (f"{MEDIA}content", f"{MEDIA}thumbnail"):
        for m in it.findall(tag):
            u = m.get("url")
            if u and (m.get("medium") in (None, "image") or (m.get("type") or "").startswith("image")):
                return u
    return None


def parse_feed(content: bytes, feed: Feed | None = None) -> list[FeedItem]:
    """RSS 2.0 / Atom → [FeedItem]. Битый XML → []."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.warning("RSS parse error %s: %s", feed.url if feed else "?", e)
        return []
    out: list[FeedItem] = []
    channel_title = _text(root.find("channel"), "title")
    for it in root.iter("item"):
        link = _text(it, "link")
        title = strip_html(_text(it, "title"))
        if not link or not title:
            continue
        src = it.find("source")
        desc = _text(it, "description") or _text(it, "{http://purl.org/rss/1.0/modules/content/}encoded")
        out.append(FeedItem(
            title=title, url=link, summary=strip_html(desc)[:1500],
            published_at=parse_rfc822(_text(it, "pubDate") or _text(it, "{http://purl.org/dc/elements/1.1/}date")),
            outlet=(src.text.strip() if src is not None and src.text else None) or (feed.title if feed else channel_title or None),
            outlet_url=(src.get("url") if src is not None else None) or (feed.site if feed else None),
            image_url=_image_of(it), guid=_text(it, "guid") or link,
            feed_url=feed.url if feed else None, kind=feed.kind if feed else "news",
        ))
    for e in root.iter(f"{ATOM}entry"):
        link_el = next((lk for lk in e.findall(f"{ATOM}link") if lk.get("rel", "alternate") == "alternate"), None)
        link = link_el.get("href") if link_el is not None else ""
        title = strip_html(_text(e, f"{ATOM}title"))
        if not link or not title:
            continue
        out.append(FeedItem(
            title=title, url=link, summary=strip_html(_text(e, f"{ATOM}summary") or _text(e, f"{ATOM}content"))[:1500],
            published_at=parse_rfc822(_text(e, f"{ATOM}updated") or _text(e, f"{ATOM}published")),
            outlet=feed.title if feed else None, outlet_url=feed.site if feed else None,
            guid=_text(e, f"{ATOM}id") or link, feed_url=feed.url if feed else None,
            kind=feed.kind if feed else "news",
        ))
    return out


# ---------------------------------------------------------------- fetch (cache)
_feed_cache: TTLCache | None = None
_feed_locks: dict[str, asyncio.Lock] = {}


def _cache() -> TTLCache:
    global _feed_cache
    if _feed_cache is None:
        from app.config import settings

        _feed_cache = TTLCache(ttl=max(30, int(settings.SEARCH_CACHE_TTL)), maxsize=256)
    return _feed_cache


def reset_cache() -> None:
    if _feed_cache is not None:
        _feed_cache.clear()


async def fetch_feed(feed: Feed, client=None) -> list[FeedItem]:
    """Лента с кэшем (SEARCH_CACHE_TTL). Ошибки → [] (+ лог)."""
    cached = _cache().get(feed.url)
    if cached is not None:
        return cached
    lock = _feed_locks.setdefault(feed.url, asyncio.Lock())
    async with lock:
        cached = _cache().get(feed.url)
        if cached is not None:
            return cached
        own = client is None
        client = client or http_client()
        try:
            r = await client.get(feed.url)
            if r.status_code >= 400:
                log.warning("RSS %s: HTTP %s", feed.url, r.status_code)
                items: list[FeedItem] = []
            else:
                items = parse_feed(r.content, feed)
        except Exception as e:  # noqa: BLE001
            log.warning("RSS %s failed: %r", feed.url, e)
            items = []
        finally:
            if own:
                await client.aclose()
        _cache().set(feed.url, items)
        return items


async def fetch_all(feeds: list[Feed] | None = None) -> list[FeedItem]:
    feeds = feeds if feeds is not None else configured_feeds()
    async with http_client() as client:
        res = await asyncio.gather(*(fetch_feed(f, client) for f in feeds), return_exceptions=True)
    out: list[FeedItem] = []
    for r in res:
        if isinstance(r, list):
            out.extend(r)
    return out


# ---------------------------------------------------------------- matching
def _norm_title(t: str) -> str:
    t = re.sub(r"[«»\"'„“”‘’—–\-:;,.!?()\[\]]", " ", (t or "").lower()).replace("ё", "е")
    return " ".join(t.split())


def match_stems(text: str) -> set[str]:
    """«Стемы» для поиска по лентам: 5 букв (4-буквенные из app.ai.text дают ложные совпадения:
    «пожар» ~ «пожалуй»)."""
    from app.ai.text import words

    return {w[:5] for w in words(text)}


def match_items(items: list[FeedItem], query: str, *, limit: int = 10, min_overlap: int | None = None) -> list[FeedItem]:
    """Поиск по записям лент: пересечение «стемов» запроса с заголовком+анонсом.
    Порог: все стемы, если в запросе их 1–2, иначе ≥ половины (минимум 2); совпадение в заголовке
    весит вдвое. Сортировка — по совпадению, затем по свежести."""
    q = match_stems(query)
    if not q:
        return []
    need = min_overlap or (len(q) if len(q) <= 2 else max(2, (len(q) + 1) // 2))
    scored = []
    seen: set[str] = set()
    for it in items:
        if it.url in seen:
            continue
        st = match_stems(it.title)
        title_hits = len(q & st)
        hits = len(q & (st | match_stems(it.summary[:600])))
        if hits < need:
            continue
        seen.add(it.url)
        fresh = it.published_at.timestamp() if it.published_at else 0.0
        scored.append((-(hits + title_hits), -fresh, it))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in scored[:limit]]


# ---------------------------------------------------------------- Google News
GN_BASE = "https://news.google.com/rss/search"


def google_news_url(query: str, when: str | None = None) -> str:
    q = query.strip()
    if when and "when:" not in q:
        q += f" when:{when}"
    return f"{GN_BASE}?q={quote_plus(q)}&hl=ru&gl=RU&ceid=RU:ru"


_URL_IN_BYTES = re.compile(rb"https?://[\x21-\x7e]+")


def decode_google_news_id(link: str) -> str | None:
    """Старый формат id (``CBMi…``) содержит URL статьи в base64-protobuf — достаём без сети.
    Новый формат (``AU_yqL…`` внутри) URL не содержит → None."""
    m = re.search(r"/articles/([A-Za-z0-9_-]+)", link or "")
    if not m:
        return None
    s = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError):
        return None
    um = _URL_IN_BYTES.search(raw)
    if not um:
        return None
    url = um.group(0).decode("ascii", "ignore")
    # protobuf: URL заканчивается перед служебным байтом < 0x21 — регулярка это уже учла;
    # отсекаем хвост вида «Ò\x01…» (не ASCII) — тоже. Проверка на правдоподобие:
    return url if "." in host_of(url) and "news.google" not in url else None


def split_gn_title(title: str, outlet: str | None) -> str:
    """«Заголовок - Издание» → «Заголовок»."""
    if outlet and title.endswith(f" - {outlet}"):
        return title[: -len(outlet) - 3].strip()
    return title.rsplit(" - ", 1)[0].strip() if " - " in title else title


class GoogleNewsResolver:
    """Раскрытие ссылок Google News по заголовку: записи лент каталога (тот же материал издания)."""

    def __init__(self, items: list[FeedItem]) -> None:
        self.by_title: dict[str, FeedItem] = {}
        for it in items:
            self.by_title.setdefault(_norm_title(it.title), it)

    def resolve(self, title: str, outlet_url: str | None) -> FeedItem | None:
        key = _norm_title(title)
        it = self.by_title.get(key)
        if it is None and len(key) > 40:  # заголовок в Google News бывает обрезан
            it = next((v for k, v in self.by_title.items() if k.startswith(key[:60]) or key.startswith(k[:60])), None)
        if it is None:
            return None
        if outlet_url and host_of(outlet_url) and host_of(outlet_url) not in host_of(it.url) \
                and host_of(it.url) not in host_of(outlet_url):
            return None  # одинаковый заголовок у другого издания — не подменяем
        return it


async def google_news_search(query: str, *, limit: int = 15, when: str | None = None,
                             client=None) -> list[FeedItem]:
    """Google News RSS → [FeedItem] (url = ссылка Google News; издание — из ``<source>``)."""
    url = google_news_url(query, when)
    cached = _cache().get(("gn", url))
    if cached is not None:
        return cached[:limit]
    own = client is None
    client = client or http_client()
    try:
        r = await client.get(url)
        items = parse_feed(r.content) if r.status_code < 400 else []
        if r.status_code >= 400:
            log.warning("Google News %s: HTTP %s", query, r.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("Google News %r failed: %r", query, e)
        items = []
    finally:
        if own:
            await client.aclose()
    for it in items:
        it.extra["google_news_url"] = it.url
        it.title = split_gn_title(it.title, it.outlet)
        # description у Google News — html-ссылка на тот же заголовок; анонс без пользы
        if it.summary and _norm_title(it.summary).startswith(_norm_title(it.title)[:30]):
            it.summary = ""
    _cache().set(("gn", url), items)
    return items[:limit]


def feed_ext_id(url: str) -> str:
    """external_id RSS-источника (CONTRACTS_v3 §2): ``rss:<URL ленты>``, длиннее 128 — ``rss:sha1:<hex>``."""
    ext = f"rss:{url}"
    if len(ext) <= 128:
        return ext
    import hashlib

    return "rss:sha1:" + hashlib.sha1(url.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- стартовые источники (live)
# Публичные каналы MAX: на max.ru доступны только метаданные (посты — через Bot API), см. docs/MAX.md.
LIVE_MAX_CHANNELS: tuple[tuple[str, str], ...] = (
    ("https://max.ru/mosru", "Город Москва"),
    ("https://max.ru/m24", "Москва 24"),
)


def default_live_sources() -> list[dict[str, Any]]:
    """Стартовые РЕАЛЬНЫЕ источники для DATA_MODE=live (seed DEV-SOURCES, CONTRACTS_v3 §5):
    московские RSS-ленты каталога (``ingest=True``), Telegram-каналы с web-превью
    (``telegram.settings.LIVE_DEFAULT_SOURCES``) и публичные каналы MAX (только метаданные).
    Все ссылки проверены запросом 2026-11-15."""
    from app.integrations.telegram.settings import LIVE_DEFAULT_SOURCES

    out: list[dict[str, Any]] = [{"platform": "rss", "ref": f.url, "title": f.title}
                                 for f in configured_feeds() if f.ingest]
    out += [{"platform": "telegram", "ref": ref} for ref in LIVE_DEFAULT_SOURCES]
    out += [{"platform": "max", "ref": url, "title": title} for url, title in LIVE_MAX_CHANNELS]
    return out
