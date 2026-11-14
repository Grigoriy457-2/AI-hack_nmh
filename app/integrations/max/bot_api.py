"""Реальный клиент MAX Bot API (зона DEV-BACKEND)."""
from __future__ import annotations

from datetime import datetime

from app.integrations.max.client import MaxChat, MaxClient, MaxMessage, MaxUserProfile


class MaxBotApiClient(MaxClient):
    name = "bot_api"

    def __init__(self, token: str, base_url: str = "https://platform-api.max.ru") -> None:
        self.token = token
        self.base_url = base_url

    async def list_chats(self) -> list[MaxChat]:
        raise NotImplementedError("DEV-BACKEND: MaxBotApiClient.list_chats")

    async def fetch_messages(
        self, chat_id: str, since: datetime | None = None, limit: int = 100
    ) -> list[MaxMessage]:
        raise NotImplementedError("DEV-BACKEND: MaxBotApiClient.fetch_messages")

    async def get_user_profile(self, user_id: str) -> MaxUserProfile | None:
        raise NotImplementedError("DEV-BACKEND: MaxBotApiClient.get_user_profile")
