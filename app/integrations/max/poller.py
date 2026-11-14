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

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.max.client import MaxClient
from app.models import AuthorProfile
from app.schemas import SyncResult


async def sync_once(session: AsyncSession, client: MaxClient | None = None) -> SyncResult:
    raise NotImplementedError("DEV-BACKEND: sync_once")


async def ensure_author_profile(
    session: AsyncSession, author_external_id: str, client: MaxClient | None = None
) -> AuthorProfile | None:
    raise NotImplementedError("DEV-BACKEND: ensure_author_profile")


def start_poller(interval: int) -> asyncio.Task:
    raise NotImplementedError("DEV-BACKEND: start_poller")
