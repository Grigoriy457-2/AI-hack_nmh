"""Mock MAX (зона DEV-BACKEND): 4–5 домовых чатов (Чертаново и др.), ~40 сообщений —
релевантные сценарию, мусор, дубли, пересылки; у части авторов есть телефон.
Данные детерминированы, время — относительно «сегодня ~14:30» (naive UTC)."""
from __future__ import annotations

from datetime import datetime

from app.integrations.max.client import MaxChat, MaxClient, MaxMessage, MaxUserProfile


class MockMaxClient(MaxClient):
    name = "mock"

    async def list_chats(self) -> list[MaxChat]:
        raise NotImplementedError("DEV-BACKEND: MockMaxClient.list_chats")

    async def fetch_messages(
        self, chat_id: str, since: datetime | None = None, limit: int = 100
    ) -> list[MaxMessage]:
        raise NotImplementedError("DEV-BACKEND: MockMaxClient.fetch_messages")

    async def get_user_profile(self, user_id: str) -> MaxUserProfile | None:
        raise NotImplementedError("DEV-BACKEND: MockMaxClient.get_user_profile")
