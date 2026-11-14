"""Фоновая синхронизация чатов-источников -> БД: MAX и Telegram (зона BACKEND).

Контракт:
- ``sync_once(session, client=None, *, platforms=None) -> SyncResult``: для каждой платформы —
  list_chats -> upsert ``source_chats`` (новые enabled=True); для enabled-чатов платформы
  fetch_messages(since=последнее неархивное sent_at) -> insert chat_messages (дубли по
  (chat_id, external_id) пропускать); профили новых авторов — в кэш (``ensure_author_profile``,
  чтобы лента знала «есть телефон»); last_synced_at; commit. ``client`` — клиент MAX (тесты).
- Telegram опрашивается, если пакет ``app.integrations.telegram`` импортируется И (TG_PROVIDER не
  ``mock`` ИЛИ в БД есть включённые TG-источники). Ошибки Telegram не мешают MAX.
- ``ensure_author_profile(session, author_external_id, client=None, platform="max")`` — кэш author_profiles.
- ``start_poller(interval) -> asyncio.Task``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.config import settings
from app.integrations.max.client import MaxClient, get_max_client
from app.models import AuthorProfile, ChatMessage, MaxChat, utcnow
from app.schemas import SyncResult

log = logging.getLogger("newsflow.max")

PROFILE_TTL = timedelta(hours=12)  # как часто перезапрашивать профиль автора
FETCH_LIMIT = 500
MAX_PROFILES_PER_SYNC = 30

_lock = asyncio.Lock()  # фоновый поллер и кнопка «Синхронизировать сейчас» не пересекаются


# ---------------------------------------------------------------- telegram (опционально)
def telegram_module():
    """Модуль клиента Telegram или None (пакет DEV-TELEGRAM может отсутствовать)."""
    if not settings.TG_ENABLED:
        return None
    try:
        from app.integrations.telegram import client as tg  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        log.debug("Telegram package unavailable: %r", e)
        return None
    return tg


_runner_active = False  # Telegram синхронизирует runner (lifespan) — общий поллер TG не трогает


def set_telegram_runner_active(active: bool) -> None:
    global _runner_active
    _runner_active = active


async def _telegram_client(session: AsyncSession, *, force: bool = False) -> Any | None:
    tg = telegram_module()
    if tg is None or (_runner_active and not force):
        return None
    if not force:
        try:
            provider = tg.choose_provider()
        except Exception:  # noqa: BLE001
            provider = "mock"
        if provider == "mock":
            has_sources = await session.scalar(
                select(func.count()).select_from(MaxChat).where(MaxChat.platform == "telegram", MaxChat.enabled.is_(True))
            )
            if not has_sources:
                return None
    try:
        return tg.get_telegram_client()
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram client init failed: %r", e)
        return None


def _known_sources(client: Any) -> set[str]:
    return set(getattr(client, "_sources", {}) or {})


# ---------------------------------------------------------------- sync
async def _sync_platform(session: AsyncSession, platform: str, client: Any, result: SyncResult) -> None:
    try:
        remote_chats = await client.list_chats()
    except Exception as e:  # noqa: BLE001
        log.warning("%s list_chats failed: %r", platform, e)
        result.errors.append(f"{platform} list_chats: {e}")
        remote_chats = []

    existing = {c.external_id: c for c in (await session.scalars(
        select(MaxChat).where(MaxChat.platform == platform)
    )).all()}
    for rc in remote_chats:
        chat = existing.get(rc.external_id)
        if chat is None:
            chat = MaxChat(platform=platform, external_id=rc.external_id, title=rc.title or rc.external_id,
                           district=rc.district, enabled=True,
                           ref=getattr(rc, "url", None) or getattr(rc, "username", None),
                           username=getattr(rc, "username", None), kind=getattr(rc, "kind", None),
                           url=getattr(rc, "url", None))
            session.add(chat)
            existing[rc.external_id] = chat
        else:
            chat.title = rc.title or chat.title
            chat.district = chat.district or rc.district  # район, заданный вручную, не затираем
            chat.username = chat.username or getattr(rc, "username", None)
            chat.kind = chat.kind or getattr(rc, "kind", None)
            chat.url = chat.url or getattr(rc, "url", None)
    await session.flush()

    # Telegram: источники из БД, о которых клиент ещё не знает (после рестарта процесса)
    if platform == "telegram" and hasattr(client, "add_source"):
        known = _known_sources(client)
        for chat in existing.values():
            if chat.enabled and chat.external_id not in known:
                try:
                    await client.add_source(chat.ref or chat.external_id)
                except Exception as e:  # noqa: BLE001
                    if getattr(client, "name", "") != "mock":
                        result.errors.append(f"{chat.title}: {e}")

    from app.integrations.max import demo_stream

    tg_gate = platform == "telegram" and demo_stream.telegram_gate_active(client)
    new_authors: set[str] = set()
    for chat in existing.values():
        if not chat.enabled:
            continue
        result.chats += 1
        since = await session.scalar(
            select(func.max(ChatMessage.sent_at)).where(ChatMessage.chat_id == chat.id, ChatMessage.archived.is_(False))
        )
        try:
            if tg_gate:  # демо: посты mock-Telegram «выходят» по ходу MAX-потока (см. demo_stream)
                msgs = demo_stream.gate_telegram(
                    await client.fetch_messages(chat.external_id, since=None, limit=FETCH_LIMIT))
            else:
                msgs = await client.fetch_messages(chat.external_id, since=since, limit=FETCH_LIMIT)
        except Exception as e:  # noqa: BLE001
            log.warning("%s fetch_messages(%s) failed: %r", platform, chat.external_id, e)
            result.errors.append(f"{chat.title}: {e}")
            continue
        if msgs:
            ext_ids = [m.external_id for m in msgs]
            known_ids = set((await session.scalars(
                select(ChatMessage.external_id).where(
                    ChatMessage.chat_id == chat.id, ChatMessage.external_id.in_(ext_ids)
                )
            )).all())
            for m in msgs:
                if m.external_id in known_ids:
                    continue
                known_ids.add(m.external_id)
                session.add(ChatMessage(
                    chat_id=chat.id,
                    platform=platform,
                    external_id=m.external_id,
                    author_external_id=m.author_external_id or "",
                    author_name=m.author_name,
                    text=m.text or "",
                    sent_at=m.sent_at,
                    has_media=m.has_media,
                    media_url=m.media_url,
                    message_url=getattr(m, "url", None),
                    views=getattr(m, "views", None),
                    is_forwarded=m.is_forwarded,
                ))
                result.new_messages += 1
                result.by_platform[platform] = result.by_platform.get(platform, 0) + 1
                if m.author_external_id:
                    new_authors.add(m.author_external_id)
        chat.last_synced_at = utcnow()
    await session.flush()

    # профили новых авторов (бейдж «есть телефон» в ленте)
    for author in list(new_authors)[:MAX_PROFILES_PER_SYNC]:
        try:
            await ensure_author_profile(session, author, client)
        except Exception as e:  # noqa: BLE001
            log.debug("profile %s failed: %r", author, e)


async def sync_once(session: AsyncSession, client: MaxClient | None = None, *,
                    platforms: tuple[str, ...] | None = None) -> SyncResult:
    """Синхронизировать MAX (+ Telegram, если доступен); commit."""
    result = SyncResult()
    if client is not None and getattr(client, "platform", "max") == "telegram":
        async with _lock:  # явный Telegram-клиент (DEV-TELEGRAM / тесты) — только его платформа
            await _sync_platform(session, "telegram", client, result)
            await session.commit()
        result.finished_at = utcnow()
        return result
    async with _lock:
        if platforms is None or "max" in platforms:
            await _sync_platform(session, "max", client or get_max_client(), result)
        if platforms is None or "telegram" in platforms:
            tg_client = await _telegram_client(session, force=bool(platforms and "telegram" in platforms))
            if tg_client is not None:
                try:
                    await _sync_platform(session, "telegram", tg_client, result)
                except Exception as e:  # noqa: BLE001
                    log.warning("Telegram sync failed: %r", e)
                    result.errors.append(f"telegram: {e}")
        await session.commit()
    result.finished_at = utcnow()
    return result


async def sync_now(client: MaxClient | None = None) -> SyncResult:
    """Синхронизация в собственной сессии (для фоновых вызовов / CLI)."""
    async with db.session_scope() as session:
        return await sync_once(session, client)


async def ensure_author_profile(
    session: AsyncSession, author_external_id: str, client: Any | None = None
) -> AuthorProfile | None:
    """Профиль автора из кэша author_profiles; если нет или устарел — запрос get_user_profile.
    Без commit (flush). Клиент по умолчанию: Telegram для id с префиксом ``tg:``, иначе MAX.
    Ошибки не пробрасываются — возвращается кэш или None."""
    if not author_external_id:
        return None
    author_external_id = str(author_external_id)
    cached = await session.get(AuthorProfile, author_external_id)
    if cached is not None and cached.fetched_at and utcnow() - cached.fetched_at < PROFILE_TTL:
        return cached
    if client is None:
        if author_external_id.startswith("tg:"):
            tg = telegram_module()
            try:
                client = tg.get_telegram_client() if tg else None
            except Exception:  # noqa: BLE001
                client = None
            if client is None:
                return cached
        else:
            client = get_max_client()
    try:
        prof = await client.get_user_profile(author_external_id)
    except Exception as e:  # noqa: BLE001
        log.warning("get_user_profile(%s) failed: %r", author_external_id, e)
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


def effective_interval(interval: int | None = None) -> int:
    """В mock-режиме MAX опрашиваем не реже раза в 5 с (демо-трикл)."""
    from app.integrations.max.mock import MockMaxClient

    iv = int(interval or settings.MAX_POLL_INTERVAL)
    if isinstance(get_max_client(), MockMaxClient):
        iv = min(iv, 5)
    return max(iv, 1)


async def _loop(interval: int) -> None:
    while True:
        try:
            res = await sync_now()
            if res.new_messages or res.errors:
                log.info("sync: chats=%s new=%s errors=%s", res.chats, res.new_messages, res.errors)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("sync failed")
        await asyncio.sleep(effective_interval(interval))


def start_poller(interval: int) -> asyncio.Task:
    """Фоновая задача: sync раз в interval секунд (mock: ≤ 5 с). Отменяется в lifespan."""
    return asyncio.create_task(_loop(interval), name="max-poller")
