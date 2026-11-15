"""MAX без логина: публичные страницы ``https://max.ru/<name>`` (зона DEV-REALDATA).

Что реально отдаёт max.ru (проверено 2026-11-15, подробности и примеры — docs/MAX.md):
- страница канала/бота — SvelteKit SSR: og:title / og:description / og:image, JSON-LD
  ``Organization`` с ``interactionStatistic.userInteractionCount`` (подписчики) и встроенные данные
  ``linkInfo: {channel: {title, description, icon, participantsCount, channelId}}`` или
  ``linkInfo: {bot: {id, name, avatar, description}}``;
- **постов на странице нет** (только «Перейдите в канал … в приложении MAX»), web-превью вида
  t.me/s у MAX нет (``max.ru/s/<name>`` → 404), web.max.ru — SPA с авторизацией по телефону;
- несуществующее имя → HTTP 404.

Поэтому без логина делаем только ``resolve_max(ref)`` (название, тип, аватар, подписчики, ссылка)
и ``MaxPublicClient`` — источник без сообщений (честно: ``posts_available=False``). Посты —
только через Bot API (бот-администратор канала/чата, ``MAX_BOT_TOKEN``), см. ``bot_api.py``.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from app.integrations.max.client import MaxChat, MaxClient, MaxMessage, MaxUserProfile
from app.integrations.webutil import TTLCache, http_client, parse_meta

log = logging.getLogger("newsflow.max")

MAX_HOST_RE = re.compile(r"^(?:https?://)?(?:www\.|web\.)?max\.ru/(.+?)/?(?:[?#].*)?$", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
_LINKINFO_RE = re.compile(r"linkInfo:(\{.*?\}),isBot:", re.S)
_JS_KEY_RE = re.compile(r'([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:')

NO_POSTS_NOTE = ("Публичная страница MAX отдаёт только название, описание, аватар и число подписчиков; "
                 "посты без входа недоступны. Для сообщений добавьте бота NEWSFLOW администратором "
                 "канала/чата и задайте MAX_BOT_TOKEN (docs/MAX.md).")


class MaxResolveError(ValueError):
    """Ссылку MAX нельзя разрешить (сообщение — человекочитаемое, для UI)."""


def parse_max_ref(ref: str) -> str:
    """``https://max.ru/m24`` / ``max.ru/join/<hash>`` / ``@m24`` / ``m24`` / ``max:m24`` → путь
    после max.ru/ (``m24``, ``join/<hash>``, ``u/<hash>``). Ошибка — ``MaxResolveError``."""
    raw = (ref or "").strip()
    if not raw:
        raise MaxResolveError("Пустая ссылка на источник MAX")
    if raw.lower().startswith("max:"):
        raw = raw[4:]
    if raw.lstrip("-").isdigit():
        raise MaxResolveError("Числовой chat_id MAX разрешается только через Bot API (бот должен быть "
                              "добавлен в чат) — задайте MAX_BOT_TOKEN")
    m = MAX_HOST_RE.match(raw)
    path = m.group(1) if m else raw.lstrip("@")
    path = path.strip("/")
    parts = path.split("/")
    if parts[0] in ("join", "u") and len(parts) >= 2 and NAME_RE.match(parts[1]):
        return f"{parts[0]}/{parts[1]}"
    if len(parts) == 1 and NAME_RE.match(parts[0]):
        return parts[0]
    raise MaxResolveError(f"Не похоже на ссылку MAX (max.ru/<имя> или max.ru/join/…): {ref!r}")


def _js_object_to_json(s: str) -> Any:
    """Объект из встроенного JS SvelteKit (ключи без кавычек, строки в JSON-кавычках) → dict."""
    parts: list[str] = []
    i, n, seg = 0, len(s), 0
    while i < n:
        if s[i] == '"':
            parts.append(_JS_KEY_RE.sub(r'\1"\2":', s[seg:i]))
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            parts.append(s[i:j + 1])
            i = seg = j + 1
        else:
            i += 1
    parts.append(_JS_KEY_RE.sub(r'\1"\2":', s[seg:]))
    text = re.sub(r"\bundefined\b|\bvoid 0\b", "null", "".join(parts))
    return json.loads(text)


def _link_info(page: str) -> dict[str, Any]:
    m = _LINKINFO_RE.search(page)
    if not m:
        return {}
    try:
        data = _js_object_to_json(m.group(1))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError) as e:
        log.debug("MAX linkInfo parse failed: %r", e)
        return {}


def parse_max_page(page: str, path: str) -> dict[str, Any]:
    """HTML max.ru/<path> → dict формата ``resolve_max`` (без сети, для тестов)."""
    meta = parse_meta(page)
    info = _link_info(page)
    ld = next((x for x in meta.get("ld") or [] if isinstance(x, dict)), {})
    stat = ld.get("interactionStatistic") if isinstance(ld.get("interactionStatistic"), dict) else {}
    kind, ent = None, {}
    for k in ("channel", "chat", "group", "bot", "user", "contact"):
        if isinstance(info.get(k), dict):
            kind, ent = k, info[k]
            break
    title = ent.get("title") or ent.get("name") or meta.get("og:title") or ld.get("name")
    if not title or title.startswith("MAX —"):
        raise MaxResolveError(f"max.ru/{path}: страница не содержит данных канала/чата (нужен вход или ссылка неверна)")
    subs = ent.get("participantsCount") or stat.get("userInteractionCount")
    norm_kind = {"channel": "channel", "chat": "group", "group": "group", "bot": "bot",
                 "user": "user", "contact": "user"}.get(kind or "", "channel" if subs else "unknown")
    username = path if "/" not in path else None
    channel_id = str(ent.get("channelId") or ent.get("chatId") or ent.get("id") or "") or None
    # external_id источника MAX без бота = slug max.ru/<slug> (CONTRACTS_v3 §2); id канала — отдельно
    ext_id = username or channel_id
    return {
        "platform": "max",
        "external_id": ext_id,  # slug (max.ru/<slug>), для join-ссылок — id из страницы
        "channel_id": channel_id,  # внутренний id канала/бота MAX (не равен chat_id Bot API)
        "username": username,
        "title": str(title).strip(),
        "kind": norm_kind,
        "avatar_url": ent.get("icon") or ent.get("avatar") or meta.get("og:image") or ld.get("image") or None,
        "subscribers": int(subs) if isinstance(subs, (int, float)) else None,
        "description": (ent.get("description") or meta.get("og:description") or None),
        "url": f"https://max.ru/{path}",
        "preview": False,
        "preview_url": None,
        "posts_available": False,
        "last_post_at": None,
        "note": NO_POSTS_NOTE if norm_kind in ("channel", "group") else
                "Это бот/пользователь MAX — как источник новостей не подключается",
    }


_cache = TTLCache(ttl=600, maxsize=256)


async def resolve_max(ref: str, *, client: Any = None) -> dict[str, Any]:
    """Метаданные публичного канала/бота/чата MAX по ссылке (без логина).

    ``{"platform": "max", "external_id", "username", "title", "kind": channel|group|bot|user|unknown,
    "avatar_url", "subscribers", "description", "url", "preview": False, "posts_available": False,
    "note"}``. Ошибки — ``MaxResolveError`` (404, не ссылка MAX, сеть)."""
    path = parse_max_ref(ref)
    cached = _cache.get(path)
    if cached is not None:
        return dict(cached)
    own = client is None
    client = client or http_client()
    try:
        try:
            r = await client.get(f"https://max.ru/{path}")
        except Exception as e:  # noqa: BLE001
            raise MaxResolveError(f"max.ru недоступен: {e!r}") from e
        if r.status_code == 404:
            raise MaxResolveError(f"max.ru/{path} не найден (канал удалён, приватный или опечатка)")
        if r.status_code >= 400:
            raise MaxResolveError(f"max.ru/{path}: HTTP {r.status_code}")
        out = parse_max_page(r.text, path)
    finally:
        if own:
            await client.aclose()
    _cache.set(path, out)
    return dict(out)


class MaxPublicClient(MaxClient):
    """MAX_PROVIDER=web (live без токена): публичные каналы из MAX_SOURCES / добавленные по ссылке.
    ``list_chats`` — реальные названия/подписчики, ``fetch_messages`` — всегда [] (постов без входа
    нет), ``status()`` объясняет, что нужно для сообщений."""

    name = "web"
    platform = "max"

    def __init__(self, sources: list[str] | None = None) -> None:
        self._pending = list(sources or [])
        self._chats: dict[str, MaxChat] = {}
        self.last_error: str | None = None

    async def add_source(self, ref: str) -> dict[str, Any]:
        info = await resolve_max(ref)
        ext = str(info["external_id"] or info["username"])
        self._chats[ext] = MaxPublicChat(external_id=ext, title=info["title"], district=None,
                                         members_count=info["subscribers"], username=info["username"],
                                         kind=info["kind"], url=info["url"])
        return info

    async def list_chats(self) -> list[MaxChat]:
        pending, self._pending = self._pending, []
        for ref in pending:
            try:
                await self.add_source(ref)
            except MaxResolveError as e:
                self.last_error = f"{ref}: {e}"
                log.warning("MAX source %r skipped: %s", ref, e)
        return list(self._chats.values())

    async def fetch_messages(self, chat_id: str, since: datetime | None = None, limit: int = 100) -> list[MaxMessage]:
        return []

    async def get_user_profile(self, user_id: str) -> MaxUserProfile | None:
        return None

    async def status(self) -> dict[str, Any]:
        return {"platform": "max", "provider": self.name, "sources": sorted(self._chats),
                "pending": list(self._pending), "last_error": self.last_error,
                "posts_available": False, "note": NO_POSTS_NOTE}


from dataclasses import dataclass  # noqa: E402


@dataclass(slots=True)
class MaxPublicChat(MaxChat):
    platform: str = "max"
    username: str | None = None
    kind: str = "channel"
    url: str | None = None
