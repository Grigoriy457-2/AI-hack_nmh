"""Живая лента входящих сообщений (MAX + Telegram) с бейджами. См. CONTRACTS_v2 §4.2."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatMessage, MaxChat, utcnow
from app.schemas import FeedMessage, FeedOut
from app.services import emerging


def parse_since(since: str | int | None) -> tuple[int | None, datetime | None]:
    """``since`` = id-курсор (int / "123") или ISO-время."""
    if since is None or since == "":
        return None, None
    if isinstance(since, int):
        return since, None
    s = str(since).strip()
    if s.isdigit():
        return int(s), None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if dt.tzinfo is not None:
        from datetime import UTC

        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return None, dt


async def latest_cursor(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.max(ChatMessage.id)).where(ChatMessage.archived.is_(False))) or 0)


async def get_feed(session: AsyncSession, *, since: str | int | None = None, limit: int = 50,
                   chat_id: int | None = None, platform: str | None = None) -> FeedOut:
    limit = max(1, min(int(limit or 50), 500))
    since_id, since_dt = parse_since(since)
    q = (select(ChatMessage, MaxChat).join(MaxChat, ChatMessage.chat_id == MaxChat.id)
         .where(ChatMessage.archived.is_(False), MaxChat.enabled.is_(True)))
    if chat_id is not None:
        q = q.where(ChatMessage.chat_id == chat_id)
    if platform:
        q = q.where(MaxChat.platform == platform)
    if since_id is not None:
        q = q.where(ChatMessage.id > since_id).order_by(ChatMessage.id.asc()).limit(limit)
        rows = list((await session.execute(q)).all())
    elif since_dt is not None:
        q = q.where(ChatMessage.fetched_at > since_dt).order_by(ChatMessage.id.asc()).limit(limit)
        rows = list((await session.execute(q)).all())
    else:
        q = q.order_by(ChatMessage.id.desc()).limit(limit)
        rows = list(reversed((await session.execute(q)).all()))
    items = await to_feed_messages(session, rows)
    cursor = max([since_id or 0] + [m.id for m in items]) if items or since_id else await latest_cursor(session)
    return FeedOut(items=items, cursor=cursor, server_time=utcnow())


async def to_feed_messages(session: AsyncSession, rows) -> list[FeedMessage]:
    if not rows:
        return []
    an = await emerging.analyze(session)
    cards = await emerging.compute_emerging(session)
    card_keys = {c.key for c in cards}
    out = []
    for m, c in rows:
        info = an.infos.get(m.id)
        out.append(emerging.feed_message(info, card_keys=card_keys) if info else emerging.plain_feed_message(m, c))
    return out
