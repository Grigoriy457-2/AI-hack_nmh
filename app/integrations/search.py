"""Web search (контракт). Зона DEV-AI.

``SearchProvider.search(query) -> list[SearchResult]``. Реализации: ``MockSearchProvider``
(новостные порталы, ТГ-каналы, mos.ru/МЧС — офлайн), ``TavilySearchProvider`` (TAVILY_API_KEY).
Получать провайдера только через ``get_search_provider()``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

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


class MockSearchProvider(SearchProvider):
    name = "mock"

    async def search(self, query, *, limit=10, domains=None, source_type=None) -> list[SearchResult]:
        raise NotImplementedError("DEV-AI: MockSearchProvider.search")


class TavilySearchProvider(SearchProvider):
    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def search(self, query, *, limit=10, domains=None, source_type=None) -> list[SearchResult]:
        raise NotImplementedError("DEV-AI: TavilySearchProvider.search")


_provider: SearchProvider | None = None


def get_search_provider() -> SearchProvider:
    """SEARCH_PROVIDER=tavily и есть ключ → Tavily, иначе Mock. Синглтон."""
    global _provider
    if _provider is None:
        from app.config import settings

        if settings.SEARCH_PROVIDER == "tavily" and settings.TAVILY_API_KEY:
            _provider = TavilySearchProvider(settings.TAVILY_API_KEY)
        else:
            _provider = MockSearchProvider()
    return _provider


def set_search_provider(provider: SearchProvider | None) -> None:
    """Подмена в тестах."""
    global _provider
    _provider = provider
