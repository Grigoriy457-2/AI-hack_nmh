"""MAX (домовые чаты) — контракт клиента. Зона DEV-BACKEND.

Реализации: ``bot_api.MaxBotApiClient`` (Bot API https://platform-api2.max.ru, MAX_BOT_TOKEN,
httpx async), ``public.MaxPublicClient`` (live без токена: метаданные публичных каналов max.ru,
постов без входа нет — docs/MAX.md) и ``mock.MockMaxClient`` (DATA_MODE=demo: 4–5 чатов района,
~40 сообщений). ``public.resolve_max(ref)`` — название/тип/аватар/подписчики по ссылке. Синхронизация в БД —
``poller.py``. Получать клиента только через ``get_max_client()``.

Идентификаторы MAX — строки (external_id). Время — naive UTC.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class MaxChat:
    external_id: str
    title: str
    district: str | None = None
    members_count: int | None = None


@dataclass(slots=True)
class MaxMessage:
    external_id: str
    chat_external_id: str
    author_external_id: str
    author_name: str | None
    text: str
    sent_at: datetime
    has_media: bool = False
    media_url: str | None = None
    is_forwarded: bool = False


@dataclass(slots=True)
class MaxUserProfile:
    external_id: str
    name: str | None
    phone: str | None = None  # None — телефон скрыт в профиле
    username: str | None = None


class MaxClient(ABC):
    name: str = "abstract"

    @abstractmethod
    async def list_chats(self) -> list[MaxChat]:
        """Чаты, куда добавлен бот."""

    @abstractmethod
    async def fetch_messages(
        self, chat_id: str, since: datetime | None = None, limit: int = 100
    ) -> list[MaxMessage]:
        """Сообщения чата (external chat_id) строго новее ``since``, по возрастанию sent_at."""

    @abstractmethod
    async def get_user_profile(self, user_id: str) -> MaxUserProfile | None:
        """Профиль автора (external user_id) или None, если недоступен."""

    async def aclose(self) -> None:  # pragma: no cover
        """Закрыть HTTP-клиент."""


_client: MaxClient | None = None


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").replace(";", ",").split(",") if x.strip()]


def build_max_client() -> MaxClient:
    """MAX_PROVIDER: bot (или auto + MAX_BOT_TOKEN) → MaxBotApiClient; web (или auto в live без
    токена) → MaxPublicClient (только метаданные публичных каналов, постов нет); mock (или auto в
    demo) → MockMaxClient."""
    from app.config import settings

    prov = settings.MAX_PROVIDER
    if prov in ("bot", "auto") and settings.MAX_BOT_TOKEN:
        from app.integrations.max.bot_api import MaxBotApiClient

        verify: object = True
        if settings.MAX_CA_BUNDLE:  # certifi + сертификат Минцифры (Russian Trusted Root CA)
            import ssl

            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
            ctx.load_verify_locations(cafile=settings.MAX_CA_BUNDLE)
            verify = ctx
        return MaxBotApiClient(settings.MAX_BOT_TOKEN, settings.MAX_API_BASE,
                               fallback_base_url=settings.MAX_API_FALLBACK_BASE, verify=verify,
                               chat_ids=_split(settings.MAX_CHAT_IDS))
    if prov == "web" or (prov in ("auto", "bot") and settings.is_live):
        from app.integrations.max.public import MaxPublicClient

        return MaxPublicClient(_split(settings.MAX_SOURCES))
    from app.integrations.max.mock import MockMaxClient

    return MockMaxClient()


def get_max_client() -> MaxClient:
    """Синглтон (см. ``build_max_client``)."""
    global _client
    if _client is None:
        _client = build_max_client()
    return _client


def set_max_client(client: MaxClient | None) -> None:
    """Подмена в тестах."""
    global _client
    _client = client
