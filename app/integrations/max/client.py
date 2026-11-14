"""MAX (домовые чаты) — контракт клиента. Зона DEV-BACKEND.

Реализации: ``bot_api.MaxBotApiClient`` (Bot API https://platform-api.max.ru, MAX_BOT_TOKEN,
httpx async) и ``mock.MockMaxClient`` (4–5 чатов района, ~40 сообщений). Синхронизация в БД —
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


def get_max_client() -> MaxClient:
    """MAX_PROVIDER=auto → MaxBotApiClient при наличии MAX_BOT_TOKEN, иначе MockMaxClient."""
    global _client
    if _client is None:
        from app.config import settings

        use_bot = settings.MAX_PROVIDER == "bot" or (
            settings.MAX_PROVIDER == "auto" and bool(settings.MAX_BOT_TOKEN)
        )
        if use_bot and settings.MAX_BOT_TOKEN:
            from app.integrations.max.bot_api import MaxBotApiClient

            _client = MaxBotApiClient(settings.MAX_BOT_TOKEN, settings.MAX_API_BASE)
        else:
            from app.integrations.max.mock import MockMaxClient

            _client = MockMaxClient()
    return _client


def set_max_client(client: MaxClient | None) -> None:
    """Подмена в тестах."""
    global _client
    _client = client
