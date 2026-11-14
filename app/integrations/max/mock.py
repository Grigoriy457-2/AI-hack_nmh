"""Mock MAX (зона DEV-BACKEND): 5 домовых чатов (Чертаново и соседние районы), ~40 сообщений —
релевантные сценарию «хлопок у жилого дома ~14:30», мусор, дубли (копипаст), пересылки;
у части авторов в профиле есть телефон, у части нет.

Единый источник данных — ``app.ai.mock_data`` (DEV-AI): ``SCENARIO_MAX_CHATS``,
``SCENARIO_MAX_MESSAGES``, ``SCENARIO_MAX_PROFILES`` и якорь времени ``scenario_event_time()``.
MockLLM и mock-поиск опираются на те же тексты и external_id, поэтому здесь данные НЕ дублируются.

Время сообщения = якорь события (14:30 МСК, naive UTC) + ``minutes``. Сообщения «из будущего»
относительно now не отдаются (появятся при следующих синхронизациях — живой эффект на демо).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.ai import mock_data
from app.integrations.max.client import MaxChat, MaxClient, MaxMessage, MaxUserProfile
from app.models import utcnow


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return default


def _norm_chat(c: dict) -> MaxChat:
    return MaxChat(
        external_id=str(_first(c, "external_id", "id", "chat_id")),
        title=str(_first(c, "title", "name", default="Чат")),
        district=_first(c, "district"),
        members_count=_first(c, "members_count", "members"),
    )


def _norm_message(m: dict, anchor: datetime, names: dict[str, str | None]) -> MaxMessage:
    sent = _first(m, "sent_at")
    if isinstance(sent, str):
        sent = datetime.fromisoformat(sent).replace(tzinfo=None)
    if not isinstance(sent, datetime):
        sent = anchor + timedelta(minutes=float(_first(m, "minutes", default=0)))
    author = str(_first(m, "author_external_id", "author", "author_id"))
    media = _first(m, "media_url", "media")
    return MaxMessage(
        external_id=str(_first(m, "external_id", "id")),
        chat_external_id=str(_first(m, "chat_external_id", "chat", "chat_id")),
        author_external_id=author,
        author_name=_first(m, "author_name", "name", default=names.get(author)),
        text=str(_first(m, "text", default="")),
        sent_at=sent,
        has_media=bool(_first(m, "has_media", default=bool(media))),
        media_url=media,
        is_forwarded=bool(_first(m, "is_forwarded", "forwarded", default=False)),
    )


class MockMaxClient(MaxClient):
    """Детерминированный офлайн-клиент MAX для демо и тестов."""

    name = "mock"

    def __init__(self, now: datetime | None = None, *, include_future: bool = False) -> None:
        """``now`` — зафиксировать «текущее» время (тесты); ``include_future`` — отдавать и
        сообщения, которые по смещению ещё «не написаны»."""
        self._now = now
        self._include_future = include_future

    def _current(self) -> datetime:
        return self._now or utcnow()

    def anchor(self) -> datetime:
        # Без явного now — общий якорь сценария (его может выставить пайплайн), чтобы чаты,
        # веб-поиск и MockLLM совпадали по дате.
        return mock_data.scenario_event_time(self._now) if self._now else mock_data.scenario_event_time()

    def all_messages(self) -> list[MaxMessage]:
        now, anchor = self._current(), self.anchor()
        names = {k: v.get("name") for k, v in mock_data.SCENARIO_MAX_PROFILES.items()}
        msgs = [_norm_message(m, anchor, names) for m in mock_data.SCENARIO_MAX_MESSAGES]
        if not self._include_future:
            msgs = [m for m in msgs if m.sent_at <= now]
        msgs.sort(key=lambda m: (m.sent_at, m.external_id))
        return msgs

    async def list_chats(self) -> list[MaxChat]:
        return [_norm_chat(c) for c in mock_data.SCENARIO_MAX_CHATS]

    async def fetch_messages(
        self, chat_id: str, since: datetime | None = None, limit: int = 100
    ) -> list[MaxMessage]:
        msgs = [m for m in self.all_messages() if m.chat_external_id == str(chat_id)]
        if since is not None:
            msgs = [m for m in msgs if m.sent_at > since]
        return msgs[:limit]

    async def get_user_profile(self, user_id: str) -> MaxUserProfile | None:
        p = mock_data.SCENARIO_MAX_PROFILES.get(str(user_id))
        if p is None:
            return None
        return MaxUserProfile(
            external_id=str(user_id),
            name=p.get("name"),
            phone=p.get("phone"),
            username=p.get("username"),
        )
