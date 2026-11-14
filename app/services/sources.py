"""Чаты-источники (MAX + Telegram): список, добавление по ссылке/@username, вкл/выкл. CONTRACTS_v2 §4.4."""
from __future__ import annotations

import logging
import re

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PLATFORM_LABELS, ChatMessage, MaxChat, Source, User, utcnow
from app.schemas import SourceChatOut
from app.services.stories import StoryError

log = logging.getLogger("newsflow.sources")

_TG_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([^/?#]+)", re.I)


async def _counts(session: AsyncSession) -> dict[int, int]:
    return dict((await session.execute(
        select(ChatMessage.chat_id, func.count()).where(ChatMessage.archived.is_(False)).group_by(ChatMessage.chat_id)
    )).all())


def chat_out(chat: MaxChat, count: int = 0) -> SourceChatOut:
    o = SourceChatOut.model_validate(chat)
    o.platform = chat.platform or "max"
    o.platform_label = PLATFORM_LABELS.get(o.platform, o.platform)
    o.messages_count = count
    return o


async def list_source_chats(session: AsyncSession, platform: str | None = None) -> list[SourceChatOut]:
    q = select(MaxChat).order_by(MaxChat.platform, MaxChat.id)
    if platform:
        q = q.where(MaxChat.platform == platform)
    chats = (await session.scalars(q)).all()
    counts = await _counts(session)
    return [chat_out(c, counts.get(c.id, 0)) for c in chats]


def _tg_username(ref: str) -> str:
    try:
        from app.integrations.telegram.client import parse_tg_ref

        return parse_tg_ref(ref)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001  TelegramSourceError и др.
        raise StoryError(422, str(e)) from e
    raw = ref.strip()
    m = _TG_RE.match(raw)
    name = (m.group(1) if m else raw).lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name):
        raise StoryError(422, f"Не похоже на публичный @username Telegram: {ref!r}")
    return name.lower()


async def add_source(session: AsyncSession, user: User | None, platform: str, ref: str, *,
                     title: str | None = None, district: str | None = None) -> tuple[MaxChat, bool]:
    """Подключить источник. Telegram: @username / t.me-ссылка (клиент проверяет доступность, если умеет).
    MAX: id чата, куда добавлен бот. Возвращает (chat, created). Сразу пытается синхронизировать."""
    ref = (ref or "").strip()
    if not ref:
        raise StoryError(422, "Укажите @username или ссылку t.me на публичный канал/чат")
    if platform == "telegram":
        username = _tg_username(ref)
        external_id = f"tg:{username}"
        resolved_title = None
        client = None
        try:
            from app.integrations.max.poller import telegram_module

            tg = telegram_module()
            if tg is not None:
                client = tg.get_telegram_client()
                chat_info = await client.add_source(ref)
                external_id = chat_info.external_id or external_id
                resolved_title = chat_info.title
                district = district or chat_info.district
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            is_mock = getattr(client, "name", "") == "mock"
            if is_mock:
                # mock-провайдер знает только демо-каналы; публичный источник сохраняем — сообщения
                # пойдут после переключения TG_PROVIDER на telethon/bot/web.
                log.info("Telegram mock: источник %s сохранён без проверки (%s)", ref, e)
            elif "Source" in name or "Config" in name:
                raise StoryError(422, f"Telegram: {e}") from e
            else:
                log.warning("Telegram add_source(%s) failed: %r — источник сохранён, проверим при синхронизации",
                            ref, e)
        chat_title = (title or resolved_title or f"@{username}")[:255]
    elif platform == "max":
        external_id = ref
        chat_title = (title or ref)[:255]
    else:
        raise StoryError(422, "platform: max | telegram")

    chat = await session.scalar(select(MaxChat).where(MaxChat.platform == platform, MaxChat.external_id == external_id))
    created = chat is None
    if chat is None:
        chat = MaxChat(platform=platform, external_id=external_id, ref=ref[:255], title=chat_title,
                       district=(district or None), enabled=True, added_by_id=user.id if user else None,
                       created_at=utcnow())
        session.add(chat)
    else:
        chat.enabled = True
        if title:
            chat.title = title[:255]
        if district:
            chat.district = district
    await session.commit()
    await session.refresh(chat)

    try:  # первая выгрузка сообщений нового источника
        from app.integrations.max.poller import sync_once

        await sync_once(session, platforms=(platform,))
    except Exception as e:  # noqa: BLE001
        log.warning("sync after add_source failed: %r", e)
    await session.refresh(chat)
    return chat, created


async def get_chat(session: AsyncSession, chat_id: int) -> MaxChat:
    chat = await session.get(MaxChat, chat_id)
    if chat is None:
        raise StoryError(404, "Источник не найден")
    return chat


async def update_source(session: AsyncSession, chat_id: int, *, enabled: bool | None = None,
                        district: str | None = None, title: str | None = None) -> SourceChatOut:
    chat = await get_chat(session, chat_id)
    if enabled is not None:
        chat.enabled = enabled
    if district is not None:
        chat.district = district.strip() or None
    if title is not None and title.strip():
        chat.title = title.strip()[:255]
    await session.commit()
    counts = await _counts(session)
    return chat_out(chat, counts.get(chat.id, 0))


async def delete_source(session: AsyncSession, chat_id: int) -> None:
    """Удалить источник: сообщения, на которые ссылаются сюжеты, остаются (archived), прочие удаляются."""
    chat = await get_chat(session, chat_id)
    referenced = set((await session.scalars(
        select(Source.chat_message_id).join(ChatMessage, Source.chat_message_id == ChatMessage.id)
        .where(ChatMessage.chat_id == chat.id)
    )).all())
    if referenced:
        chat.enabled = False
        await session.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat.id, ChatMessage.id.not_in(referenced)))
        await session.execute(ChatMessage.__table__.update().where(ChatMessage.chat_id == chat.id).values(archived=True))
    else:
        await session.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat.id))
        await session.delete(chat)
    if chat.platform == "telegram":
        try:
            from app.integrations.max.poller import telegram_module

            tg = telegram_module()
            if tg is not None:
                await tg.get_telegram_client().remove_source(chat.external_id)
        except Exception:  # noqa: BLE001
            pass
    await session.commit()
