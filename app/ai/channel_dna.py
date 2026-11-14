"""CHANNEL DNA (зона DEV-AI): профиль стиля канала по истории постов. Формат — CONTRACTS.md: dna_json."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel


def analyze_posts(posts: list[str]) -> dict[str, Any]:
    """Чистая функция без LLM: длина, эмодзи, структура заголовков, хэштеги → dna_json."""
    raise NotImplementedError("DEV-AI: analyze_posts")


async def ensure_channel_dna(session: AsyncSession, channel: Channel, *, force: bool = False) -> dict[str, Any]:
    """Посчитать и сохранить channel.dna_json (+dna_updated_at), если пусто или force. Без commit."""
    raise NotImplementedError("DEV-AI: ensure_channel_dna")
