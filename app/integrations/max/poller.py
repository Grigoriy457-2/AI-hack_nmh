"""Фоновая синхронизация MAX -> БД (зона DEV-BACKEND).

Контракт:
- ``sync_once(session, client=None) -> SyncResult``: list_chats -> upsert max_chats (новые
  enabled=True); для enabled-чатов fetch_messages(since=последнее sent_at в БД) -> insert
  chat_messages (дубли по (chat_id, external_id) пропускать); обновить last_synced_at; commit.
- ``ensure_author_profile(session, author_external_id, client=None) -> AuthorProfile | None``:
  кэш author_profiles (используется пайплайном для «Кому позвонить»). Без commit.
- ``start_poller(interval) -> asyncio.Task``: бесконечный цикл (своя сессия через
  ``db.session_scope()``) sync_once раз в interval секунд; ошибки логировать, не падать.
  Вызывается из app.main lifespan, отменяется при shutdown.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.integrations.max.client import MaxClient, get_max_client
from app.models import AuthorProfile, ChatMessage, MaxChat, utcnow
from app.schemas import SyncResult

log = logging.getLogger("newsflow.max")

PROFILE_TTL = timedelta(hours=12)  # как часто перезапрашивать профиль автора
FETCH_LIMIT = 500

_lock = asyncio.Lock()  # фоновый поллер и кнопка «Синхронизировать сейчас» не пересекаются


async def sync_once(session: AsyncSession, client: MaxClient | None = None) -> SyncResult:
    """list_chats -> upsert max_chats; для enabled-чатов — новые сообщения в chat_messages; commit."""
    client = client or get_max_client()
    result = SyncResult()
    async with _lock:
        try:
            remote_chats = await client.list_chats()
        except Exception as e:  # noqa: BLE001
            log.warning("MAX list_chats failed: %r", e)
            result.errors.append(f"list_chats: {e}")
            remote_chats = []

        existing = {c.external_id: c for c in (await session.scalars(select(MaxChat))).all()}
        for rc in remote_chats:
            chat = existing.get(rc.external_id)
            if chat is None:
                chat = MaxChat(external_id=rc.external_id, title=rc.title, district=rc.district, enabled=True)
                session.add(chat)
                existing[rc.external_id] = chat
            else:
                chat.title = rc.title or chat.title
                chat.district = chat.district or rc.district  # район, заданный вручную, не затираем
        await session.flush()

        for chat in existing.values():
            if not chat.enabled:
                continue
            result.chats += 1
            since = await session.scalar(
                select(func.max(ChatMessage.sent_at)).where(ChatMessage.chat_id == chat.id)
            )
            try:
                msgs = await client.fetch_messages(chat.external_id, since=since, limit=FETCH_LIMIT)
            except Exception as e:  # noqa: BLE001
                log.warning("MAX fetch_messages(%s) failed: %r", chat.external_id, e)
                result.errors.append(f"{chat.title}: {e}")
                continue
            if msgs:
                ext_ids = [m.external_id for m in msgs]
                known = set(
                    (await session.scalars(
                        select(ChatMessage.external_id).where(
                            ChatMessage.chat_id == chat.id, ChatMessage.external_id.in_(ext_ids)
                        )
                    )).all()
                )
                for m in msgs:
                    if m.external_id in known:
                        continue
                    known.add(m.external_id)
                    session.add(ChatMessage(
                        chat_id=chat.id,
                        external_id=m.external_id,
                        author_external_id=m.author_external_id,
                        author_name=m.author_name,
                        text=m.text,
                        sent_at=m.sent_at,
                        has_media=m.has_media,
                        media_url=m.media_url,
                        is_forwarded=m.is_forwarded,
                    ))
                    result.new_messages += 1
            chat.last_synced_at = utcnow()
        await session.commit()
    result.finished_at = utcnow()
    return result


async def sync_now(client: MaxClient | None = None) -> SyncResult:
    """Синхронизация в собственной сессии (для фоновых вызовов / CLI)."""
    async with db.session_scope() as session:
        return await sync_once(session, client)


async def ensure_author_profile(
    session: AsyncSession, author_external_id: str, client: MaxClient | None = None
) -> AuthorProfile | None:
    """Профиль автора из кэша author_profiles; если нет или устарел — запрос get_user_profile.
    Без commit (flush). Ошибки MAX не пробрасываются — возвращается кэш или None."""
    if not author_external_id:
        return None
    author_external_id = str(author_external_id)
    cached = await session.get(AuthorProfile, author_external_id)
    if cached is not None and cached.fetched_at and utcnow() - cached.fetched_at < PROFILE_TTL:
        return cached
    client = client or get_max_client()
    try:
        prof = await client.get_user_profile(author_external_id)
    except Exception as e:  # noqa: BLE001
        log.warning("MAX get_user_profile(%s) failed: %r", author_external_id, e)
        return cached
    if prof is None:
        return cached
    if cached is None:
        cached = AuthorProfile(external_id=author_external_id)
        session.add(cached)
    cached.name = prof.name or cached.name
    # Телефон, которым человек когда-то поделился, не теряем, если API его сейчас не вернул.
    cached.phone = prof.phone or cached.phone
    cached.username = prof.username or cached.username
    cached.fetched_at = utcnow()
    await session.flush()
    return cached


async def _loop(interval: int) -> None:
    while True:
        try:
            res = await sync_now()
            if res.new_messages or res.errors:
                log.info("MAX sync: chats=%s new=%s errors=%s", res.chats, res.new_messages, res.errors)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("MAX sync failed")
        await asyncio.sleep(max(int(interval), 1))


def start_poller(interval: int) -> asyncio.Task:
    """Фоновая задача: sync раз в interval секунд. Отменяется в lifespan при shutdown."""
    return asyncio.create_task(_loop(interval), name="max-poller")
