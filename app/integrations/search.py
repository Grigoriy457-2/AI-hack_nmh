"""Web search (контракт). Зона DEV-AI / DEV-REALDATA.

``SearchProvider.search(query) -> list[SearchResult]``. Реализации: ``MockSearchProvider``
(новостные порталы, ТГ-каналы, mos.ru/МЧС — офлайн, только DATA_MODE=demo), ``RssSearchProvider``
(live, без ключей: Google News RSS + проверенные RSS-ленты СМИ/ведомств, см. ``app.integrations.rss``),
``TavilySearchProvider`` (TAVILY_API_KEY), ``SerperSearchProvider`` (SERPER_API_KEY),
``CompositeSearchProvider`` (параллельно, asyncio.gather, дедуп по url).
Получать провайдера только через ``get_search_provider()``.

Реальные результаты: ``url`` — из ответа источника (статья; для Google News — прямая ссылка, если
удалось раскрыть, иначе рабочая ссылка news.google.com), ``published_at`` — дата публикации,
``extra["outlet"]`` — издание, ``extra["outlet_url"]`` — сайт издания, ``extra["via"]`` —
google_news|rss|tavily|serper, ``extra["google_news_url"]``, ``extra["resolved"]``.

Медиа в результатах: ``media_urls`` — ссылки на фото/видео; ``extra["media"]`` (mock) —
[{kind, url, thumbnail_url, captured_at (naive UTC|None), note}] для MEDIA FINDER.
"""
from __future__ import annotations

import hashlib
import re
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

log = logging.getLogger("newsflow.search")

SourceType = Literal["news", "telegram", "official", "media", "web"]

OFFICIAL_DOMAINS: tuple[str, ...] = (
    "mchs.gov.ru", "mvd.ru", "xn--b1aew.xn--p1ai",  # мвд.рф
    "mos.ru", "sobyanin.ru", "sledcom.ru", "genproc.gov.ru", "mosgorsud.ru", "kremlin.ru",
)


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: datetime | None = None  # naive UTC
    source_type: SourceType = "web"
    media_urls: list[str] = field(default_factory=list)  # для MEDIA FINDER
    extra: dict[str, Any] = field(default_factory=dict)


class SearchProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains: list[str] | None = None,
        source_type: SourceType | None = None,
    ) -> list[SearchResult]:
        """Поиск. ``domains`` — фильтр (агент official передаёт OFFICIAL_DOMAINS),
        ``source_type`` — подсказка (агент media передаёт "media"). Сетевые ошибки
        не пробрасывать — вернуть [] и залогировать."""

    async def aclose(self) -> None:  # pragma: no cover
        """Закрыть HTTP-клиент."""


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def domain_matches(url: str, domains: list[str] | tuple[str, ...] | None) -> bool:
    if not domains:
        return True
    host = _host(url)
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in domains)


def result_matches(r: SearchResult, domains: list[str] | tuple[str, ...] | None) -> bool:
    """Фильтр по доменам: url статьи или сайт издания (для нераскрытых ссылок Google News)."""
    if not domains:
        return True
    return domain_matches(r.url, domains) or domain_matches(str(r.extra.get("outlet_url") or ""), domains)


def is_official_url(url: str) -> bool:
    return domain_matches(url, OFFICIAL_DOMAINS)


# ---------------------------------------------------------------- mock
def _h(s: str) -> int:
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)


class MockSearchProvider(SearchProvider):
    """Офлайн-поиск: корпус сценария «Чертаново» (mock_data.SCENARIO_WEB) или детерминированные
    дженерики по словам запроса."""

    name = "mock"

    async def search(self, query, *, limit=10, domains=None, source_type=None) -> list[SearchResult]:
        from app.ai import mock_data
        from app.ai.text import stems

        q = stems(query)
        if not q:
            return []
        from app.config import settings

        if q & mock_data.SCENARIO_LOCATION_STEMS and settings.is_demo:
            results = self._scenario(q, mock_data)
        else:
            results = self._generic(query, q, mock_data)
        if domains:
            results = [r for r in results if domain_matches(r.url, domains)]
        if source_type == "media":
            results = [r for r in results if r.media_urls]
        return results[:limit]

    def _scenario(self, q: set[str], md) -> list[SearchResult]:
        from app.ai.text import stem, stems

        anchor = md.scenario_event_time()
        scored: list[tuple[int, int, dict]] = []
        for idx, e in enumerate(md.SCENARIO_WEB):
            must = {stem(m) for m in e.get("must", [])}
            if must and not (q & must):
                continue
            overlap = len(q & stems(e["title"] + " " + e["snippet"]))
            if overlap < min(2, len(q)):
                continue
            scored.append((-overlap, idx, e))
        scored.sort(key=lambda t: (t[0], t[1]))
        out = []
        for _, _, e in scored:
            media = []
            for m in e.get("media", []):
                if m.get("captured_at"):
                    cap = datetime.fromisoformat(m["captured_at"]) - timedelta(hours=3)  # МСК -> UTC
                else:
                    cap = anchor + timedelta(minutes=m.get("captured_minutes", e["minutes"]))
                media.append({
                    "kind": m["kind"], "url": m["url"],
                    "thumbnail_url": md.svg_thumb(m["label"], bg=m.get("bg", "#3b4252"), kind=m["kind"]),
                    "captured_at": cap, "note": m.get("note"),
                })
            out.append(SearchResult(
                title=e["title"], url=e["url"], snippet=e["snippet"],
                published_at=anchor + timedelta(minutes=e["minutes"]),
                source_type=e["source_type"],
                media_urls=[m["url"] for m in media],
                extra={"key": e["key"], "media": media, "mock": True},
            ))
        return out

    def _generic(self, query: str, q: set[str], md) -> list[SearchResult]:
        from app.ai.text import words
        from app.models import utcnow

        topic = re.sub(r"\s+", " ", query).strip().rstrip(".!?")[:120] or " ".join(words(query))
        h = _h(query)
        now = utcnow().replace(second=0, microsecond=0)
        slug = f"{h:08x}"
        out: list[SearchResult] = []
        n_news = 2 + h % 2
        for i in range(n_news):
            outlet = md.GENERIC_OUTLETS[(h + i) % len(md.GENERIC_OUTLETS)]
            is_tg = outlet["type"] == "telegram"
            url = (f"https://{outlet['domain']}/{(h >> 4) % 90000 + i}" if is_tg
                   else f"https://{outlet['domain']}/news/{slug}-{i}")
            title = (f"{outlet['name']}: читатели сообщают — «{topic}»" if i == 0
                     else f"«{topic}»: что известно к этому часу")
            snippet = (
                "Читатели присылают сообщения о происшествии. Очевидцы описывают произошедшее по-разному, "
                "официальных комментариев пока нет." if i == 0 else
                f"Жители публикуют фото в соцсетях, редакция «{outlet['name']}» уточняет детали у экстренных "
                f"служб. Тема: «{topic}»."
            )
            out.append(SearchResult(
                title=title, url=url, snippet=snippet,
                published_at=now - timedelta(minutes=15 + (h >> (i + 3)) % 90),
                source_type="telegram" if is_tg else "news", extra={"mock": True},
            ))
        out.append(SearchResult(
            title=f"Оперативная информация по обращениям жителей: «{topic}»",
            url=f"https://www.mos.ru/mock/news/item/{slug}",
            snippet=f"Городские службы проверяют поступившие сообщения. Тема обращений: «{topic}». "
                    f"Информация уточняется.",
            published_at=now - timedelta(minutes=10 + h % 40),
            source_type="official", extra={"mock": True},
        ))
        media_url = f"https://t.me/typical_msk_mock/{h % 100000}?photo=1"
        out.append(SearchResult(
            title=f"Фото и видео очевидцев: «{topic}»",
            url=f"https://t.me/typical_msk_mock/{h % 100000}",
            snippet=f"Подписчики прислали фото с места событий. Тема: «{topic}».",
            published_at=now - timedelta(minutes=20 + h % 50),
            source_type="media", media_urls=[media_url],
            extra={"mock": True, "media": [{
                "kind": "photo", "url": media_url,
                "thumbnail_url": md.svg_thumb("Фото очевидца", bg="#434c5e"),
                "captured_at": now - timedelta(minutes=25 + h % 50), "note": None,
            }]},
        ))
        return out


# ---------------------------------------------------------------- реальные провайдеры
def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        from datetime import UTC

        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _guess_type(url: str, default: SourceType = "web") -> SourceType:
    host = _host(url)
    if is_official_url(url):
        return "official"
    if host in ("t.me", "telegram.me"):
        return "telegram"
    return default


class TavilySearchProvider(SearchProvider):
    name = "tavily"
    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._client = None

    def _http(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def search(self, query, *, limit=10, domains=None, source_type=None) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "query": query, "max_results": max(1, min(limit, 20)), "search_depth": "basic",
            "topic": "news", "include_images": source_type == "media",
        }
        if domains:
            payload["include_domains"] = list(domains)
        try:
            resp = await self._http().post(
                self.ENDPOINT, json=payload, headers={"Authorization": f"Bearer {self.api_key}"}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            log.warning("Tavily search failed (%s): %s", query, e)
            return []
        images = [i if isinstance(i, str) else i.get("url") for i in data.get("images") or []]
        out = []
        for i, r in enumerate(data.get("results") or []):
            url = r.get("url") or ""
            media = [images[i]] if source_type == "media" and i < len(images) and images[i] else []
            out.append(SearchResult(
                title=r.get("title") or url, url=url, snippet=(r.get("content") or "")[:1000],
                published_at=_parse_dt(r.get("published_date")),
                source_type="media" if media else _guess_type(url, "news"),
                media_urls=media,
                extra={"score": r.get("score"), "via": "tavily", "outlet": _host(url) or None,
                       "outlet_url": f"https://{_host(url)}" if _host(url) else None, "media": [{"kind": "photo", "url": m, "thumbnail_url": m,
                                                           "captured_at": None, "note": None} for m in media]},
            ))
        if domains:
            out = [r for r in out if domain_matches(r.url, domains)]
        return out[:limit]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class SerperSearchProvider(SearchProvider):
    name = "serper"

    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._client = None

    def _http(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def search(self, query, *, limit=10, domains=None, source_type=None) -> list[SearchResult]:
        q = query
        if domains:
            q += " (" + " OR ".join(f"site:{d}" for d in domains) + ")"
        kind = "images" if source_type == "media" else "news"
        try:
            resp = await self._http().post(
                f"https://google.serper.dev/{kind}",
                json={"q": q, "gl": "ru", "hl": "ru", "num": max(1, min(limit, 20))},
                headers={"X-API-KEY": self.api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            log.warning("Serper search failed (%s): %s", query, e)
            return []
        out = []
        for r in data.get(kind) or data.get("organic") or []:
            url = r.get("link") or ""
            img = r.get("imageUrl") if kind == "images" else None
            out.append(SearchResult(
                title=r.get("title") or url, url=url, snippet=r.get("snippet") or r.get("title") or "",
                published_at=None,
                source_type="media" if img else _guess_type(url, "news"),
                media_urls=[img] if img else [],
                extra={"date_text": r.get("date"), "source": r.get("source"), "via": "serper",
                       "outlet": r.get("source") or _host(url) or None,
                       "outlet_url": f"https://{_host(url)}" if _host(url) else None,
                       "media": [{"kind": "photo", "url": img, "thumbnail_url": r.get("thumbnailUrl") or img,
                                  "captured_at": None, "note": None}] if img else []},
            ))
        if domains:
            out = [r for r in out if domain_matches(r.url, domains)]
        return out[:limit]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------- live: Google News RSS + ленты
class RssSearchProvider(SearchProvider):
    """Без ключей. Параллельно: Google News RSS (по запросу) + записи проверенных RSS-лент
    (``rss.configured_feeds()``, кэш SEARCH_CACHE_TTL). Ссылки Google News раскрываются через
    совпадение заголовка с записями лент; медиа — enclosure ленты или og:image статьи."""

    name = "rss"

    def __init__(self, *, google_news: bool | None = None, when: str | None = None) -> None:
        from app.config import settings

        self.google_news = settings.GOOGLE_NEWS_ENABLED if google_news is None else google_news
        self.when = settings.GOOGLE_NEWS_WHEN if when is None else when

    async def search(self, query, *, limit=10, domains=None, source_type=None) -> list[SearchResult]:
        import asyncio

        from app.integrations import rss

        gn_query = query
        if domains:
            gn_query += " (" + " OR ".join(f"site:{d}" for d in domains if not d.startswith("xn--")) + ")"
        feeds = rss.configured_feeds()
        if domains:
            feeds = [f for f in feeds if f.kind == "official" or domain_matches(f.url, domains)]
        tasks = [rss.fetch_all(feeds)]
        if self.google_news:
            tasks.append(rss.google_news_search(gn_query, limit=max(limit * 2, 15), when=self.when or None))
        res = await asyncio.gather(*tasks, return_exceptions=True)
        feed_items = res[0] if isinstance(res[0], list) else []
        gn_items = res[1] if len(res) > 1 and isinstance(res[1], list) else []
        for e in res:
            if isinstance(e, BaseException):
                log.warning("RSS search part failed (%s): %r", query, e)
        rss_out: list[SearchResult] = []
        seen: set[str] = set()
        for it in rss.match_items(feed_items, query, limit=limit * 2):
            if it.url not in seen:
                seen.add(it.url)
                rss_out.append(self._from_item(it, "rss"))
        all_items = feed_items
        if gn_items and len(feed_items) < 50:  # для раскрытия ссылок нужен весь каталог лент
            all_items = feed_items + await rss.fetch_all()
        resolver = rss.GoogleNewsResolver(all_items) if gn_items else None
        gn_out: list[SearchResult] = []
        for it in gn_items:
            direct = rss.decode_google_news_id(it.url)
            match = resolver.resolve(it.title, it.outlet_url) if resolver and not direct else None
            url = direct or (match.url if match else it.url)
            if url in seen:
                continue
            seen.add(url)
            r = self._from_item(it, "google_news")
            r.url = url
            r.extra["resolved"] = url != it.url
            if match is not None:
                r.snippet = match.summary or r.snippet
                if match.image_url and not r.media_urls:
                    r.media_urls = [match.image_url]
                    r.extra["media"] = [_media(match.image_url, "enclosure RSS")]
            gn_out.append(r)
        out: list[SearchResult] = []
        for i in range(max(len(rss_out), len(gn_out))):  # чередуем: ленты и Google News
            out += [lst[i] for lst in (rss_out, gn_out) if i < len(lst)]
        if domains:
            out = [r for r in out if result_matches(r, domains)]
        if source_type == "media":
            out = [r for r in out if r.media_urls] + [r for r in out if not r.media_urls]
            await _enrich_og_images(out[: limit + 3])
            out = [r for r in out if r.media_urls]
        return out[:limit]

    @staticmethod
    def _from_item(it: Any, via: str) -> SearchResult:
        media = [it.image_url] if it.image_url else []
        official = it.kind == "official" or is_official_url(it.url) or is_official_url(it.outlet_url or "")
        return SearchResult(
            title=it.title, url=it.url, snippet=it.summary or it.title, published_at=it.published_at,
            source_type="official" if official else _guess_type(it.url, "news"),
            media_urls=media,
            extra={"outlet": it.outlet, "outlet_url": it.outlet_url, "via": via, "feed": it.feed_url,
                   "google_news_url": it.extra.get("google_news_url"), "resolved": via == "rss",
                   "media": [_media(m, "enclosure RSS") for m in media]},
        )


def _media(url: str, note: str | None) -> dict[str, Any]:
    # captured_at неизвестна: дата публикации НЕ равна дате съёмки
    kind = "video" if url.lower().split("?")[0].endswith((".mp4", ".mov", ".webm")) else "photo"
    return {"kind": kind, "url": url, "thumbnail_url": url, "captured_at": None, "note": note}


_og_cache: dict[str, str | None] = {}


async def fetch_og_image(url: str, client: Any = None) -> str | None:
    """og:image статьи (кэш на процесс). Только прямые ссылки (не news.google.com)."""
    if not url or "news.google." in url:
        return None
    if url in _og_cache:
        return _og_cache[url]
    from app.integrations.webutil import http_client, parse_meta

    own = client is None
    client = client or http_client(timeout=8)
    img = None
    try:
        r = await client.get(url)
        if r.status_code < 400 and "html" in r.headers.get("content-type", ""):
            meta = parse_meta(r.text)
            img = meta.get("og:image") or meta.get("twitter:image")
            if img and img.startswith("//"):
                img = "https:" + img
            if img and (not img.startswith("http") or "logo" in img.lower()):
                img = None  # логотип издания — не фото события
    except Exception as e:  # noqa: BLE001
        log.debug("og:image %s failed: %r", url, e)
    finally:
        if own:
            await client.aclose()
    _og_cache[url] = img
    return img


async def _enrich_og_images(results: list[SearchResult]) -> None:
    import asyncio

    from app.integrations.webutil import http_client

    todo = [r for r in results if not r.media_urls and "news.google." not in r.url]
    if not todo:
        return
    async with http_client(timeout=8) as client:
        imgs = await asyncio.gather(*(fetch_og_image(r.url, client) for r in todo), return_exceptions=True)
    for r, img in zip(todo, imgs):
        if isinstance(img, str) and img:
            r.media_urls = [img]
            r.extra["media"] = [_media(img, "og:image статьи")]


class CompositeSearchProvider(SearchProvider):
    """Несколько провайдеров параллельно (asyncio.gather), дедуп по url, кэш по запросу."""

    def __init__(self, providers: list[SearchProvider]) -> None:
        from app.config import settings
        from app.integrations.webutil import TTLCache

        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self._cache = TTLCache(ttl=max(30, int(settings.SEARCH_CACHE_TTL)), maxsize=256)

    async def search(self, query, *, limit=10, domains=None, source_type=None) -> list[SearchResult]:
        import asyncio

        key = (query, limit, tuple(domains or ()), source_type)
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)
        res = await asyncio.gather(*(p.search(query, limit=limit, domains=domains, source_type=source_type)
                                     for p in self.providers), return_exceptions=True)
        for e in res:
            if isinstance(e, BaseException):
                log.warning("search provider failed (%s): %r", query, e)
        lists = [r for r in res if isinstance(r, list)]
        out: list[SearchResult] = []
        seen: set[str] = set()
        for i in range(max((len(x) for x in lists), default=0)):  # чередуем провайдеров
            for lst in lists:
                if i < len(lst) and lst[i].url and lst[i].url not in seen:
                    seen.add(lst[i].url)
                    out.append(lst[i])
        out = out[:limit]
        self._cache.set(key, out)
        return list(out)

    async def aclose(self) -> None:
        for p in self.providers:
            await p.aclose()


_provider: SearchProvider | None = None


def build_search_provider() -> SearchProvider:
    """SEARCH_PROVIDER: mock → Mock; auto → demo: Mock, live: [Tavily|Serper по ключу] + RSS;
    rss → только RSS; tavily|serper → ключевой + RSS (без ключа — только RSS)."""
    from app.config import settings

    mode = settings.SEARCH_PROVIDER
    if mode == "mock" or (mode == "auto" and settings.is_demo):
        return MockSearchProvider()
    keyed: list[SearchProvider] = []
    if mode in ("auto", "tavily") and settings.TAVILY_API_KEY:
        keyed.append(TavilySearchProvider(settings.TAVILY_API_KEY))
    elif mode in ("auto", "serper") and settings.SERPER_API_KEY:
        keyed.append(SerperSearchProvider(settings.SERPER_API_KEY))
    rss_p = RssSearchProvider()
    return CompositeSearchProvider([*keyed, rss_p]) if keyed else rss_p


def get_search_provider() -> SearchProvider:
    """Синглтон (см. ``build_search_provider``)."""
    global _provider
    if _provider is None:
        _provider = build_search_provider()
    return _provider


def set_search_provider(provider: SearchProvider | None) -> None:
    """Подмена в тестах."""
    global _provider
    _provider = provider
