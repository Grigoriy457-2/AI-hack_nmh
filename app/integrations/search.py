"""Web search (контракт). Зона DEV-AI.

``SearchProvider.search(query) -> list[SearchResult]``. Реализации: ``MockSearchProvider``
(новостные порталы, ТГ-каналы, mos.ru/МЧС — офлайн), ``TavilySearchProvider`` (TAVILY_API_KEY),
``SerperSearchProvider`` (SERPER_API_KEY).
Получать провайдера только через ``get_search_provider()``.

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

OFFICIAL_DOMAINS: tuple[str, ...] = ("mchs.gov.ru", "mvd.ru", "mos.ru")


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
    return any(host == d or host.endswith("." + d) for d in domains)


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
        if q & mock_data.SCENARIO_LOCATION_STEMS:
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
                extra={"score": r.get("score"), "media": [{"kind": "photo", "url": m, "thumbnail_url": m,
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
                extra={"date_text": r.get("date"), "source": r.get("source"),
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


_provider: SearchProvider | None = None


def get_search_provider() -> SearchProvider:
    """SEARCH_PROVIDER=tavily|serper и есть ключ → реальный провайдер, иначе Mock. Синглтон."""
    global _provider
    if _provider is None:
        from app.config import settings

        if settings.SEARCH_PROVIDER == "tavily" and settings.TAVILY_API_KEY:
            _provider = TavilySearchProvider(settings.TAVILY_API_KEY)
        elif settings.SEARCH_PROVIDER == "serper" and settings.SERPER_API_KEY:
            _provider = SerperSearchProvider(settings.SERPER_API_KEY)
        else:
            _provider = MockSearchProvider()
    return _provider


def set_search_provider(provider: SearchProvider | None) -> None:
    """Подмена в тестах."""
    global _provider
    _provider = provider
