"""Ссылки «открыть» для сообщений чатов/каналов и источников сюжета (v4).

Правило: ссылка на сам пост, если она известна (Telegram — ``https://t.me/<channel>/<id>``, MAX — публичная
ссылка на пост из Bot API, RSS — URL статьи); иначе — ссылка на чат/канал (``https://t.me/<name>``,
``https://max.ru/<name>``); иначе ничего. ``link_kind``: ``post`` | ``chat`` | ``none``.

Ссылка вычисляется при отдаче из ``chat_messages.message_url`` + данных чата — поэтому и старые записи
``sources`` (с пустым или «внутренним» url) получают правильную ссылку.
Внутренние ``max://chat/...`` (сценарий DATA_MODE=demo) считаются ссылкой на пост только в demo.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from app.config import settings

LinkKind = Literal["post", "chat", "none"]

_TG_USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_EPOCH_SFX_RE = re.compile(r"~[ec]\d+.*$")  # суффиксы эпохи/круга демо-потока


def _is_http(url: str | None) -> bool:
    return bool(url) and str(url).startswith(("http://", "https://"))


def _tg_username(chat: Any) -> str | None:
    name = (getattr(chat, "username", None) or "").lstrip("@")
    if not name:
        ext = str(getattr(chat, "external_id", "") or "")
        if ext.startswith("tg:"):
            name = ext[3:]
    return name if name and _TG_USER_RE.match(name) else None


def chat_url(chat: Any) -> str | None:
    """Публичная ссылка на чат/канал (или None)."""
    if chat is None:
        return None
    platform = getattr(chat, "platform", None) or "max"
    url = getattr(chat, "url", None)
    if platform == "rss":
        site = getattr(chat, "site_url", None)
        return site if _is_http(site) else None
    if platform == "telegram":
        name = _tg_username(chat)
        if name:
            return f"https://t.me/{name}"
    return url if _is_http(url) else None


def post_url(msg: Any, chat: Any) -> str | None:
    """Ссылка на сам пост (или None)."""
    if msg is None:
        return None
    url = getattr(msg, "message_url", None)
    if _is_http(url):
        return url
    if url and str(url).startswith("max://") and settings.is_demo:
        return url
    if chat is not None and (getattr(chat, "platform", None) or "max") == "telegram":
        name = _tg_username(chat)
        mid = _EPOCH_SFX_RE.sub("", str(getattr(msg, "external_id", "") or ""))
        if name and mid.isdigit() and (getattr(chat, "kind", None) or "channel") != "user":
            return f"https://t.me/{name}/{mid}"
    return None


def resolve(msg: Any, chat: Any) -> tuple[str | None, LinkKind]:
    """(url, link_kind) для сообщения чата: пост → чат → none."""
    p = post_url(msg, chat)
    if p:
        return p, "post"
    c = chat_url(chat)
    if c:
        return c, "chat"
    return None, "none"


def for_source(source: Any, chat: Any = None) -> tuple[str | None, LinkKind]:
    """(url, link_kind) для строки ``sources``. Источник из чата — по сообщению (+ чат), иначе — url как есть."""
    msg = getattr(source, "chat_message", None)
    if msg is not None:
        url, kind = resolve(msg, chat)
        if kind != "none":
            return url, kind
    url = getattr(source, "url", None)
    if _is_http(url) or (url and settings.is_demo and str(url).startswith("max://")):
        if msg is not None or getattr(getattr(source, "kind", None), "value", getattr(source, "kind", None)) == "max_chat":
            # сохранённый url чатового источника: пост, если похож на ссылку поста
            return url, ("post" if _looks_like_post(url) else "chat")
        return url, "post"
    return None, "none"


def _looks_like_post(url: str) -> bool:
    u = str(url)
    if u.startswith("max://"):
        return True
    if re.match(r"^https?://(?:www\.)?t\.me/(?:s/)?[A-Za-z0-9_]+/\d+", u):
        return True
    return bool(re.match(r"^https?://(?:www\.)?max\.ru/.+/.+", u))


async def chats_by_id(session, chat_ids) -> dict[int, Any]:
    """Загрузить чаты пачкой (ChatMessage.chat — ленивая связь, в async её не трогаем)."""
    from sqlalchemy import select

    from app.models import MaxChat

    ids = {i for i in chat_ids if i is not None}
    if not ids:
        return {}
    return {c.id: c for c in (await session.scalars(select(MaxChat).where(MaxChat.id.in_(ids)))).all()}
