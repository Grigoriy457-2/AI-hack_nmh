"""Получение названия/типа/аватара/подписчиков Telegram-источника по ссылке (зона DEV-REALDATA).

``resolve_telegram(ref) -> dict`` — без авторизации, публичная страница ``https://t.me/<name>``
(og:-теги + ``.tgme_page_extra`` «N subscribers / N members») и проверка веб-превью
``https://t.me/s/<name>`` (есть ли лента постов без логина).

Результат (общий формат с ``app.integrations.max.public.resolve_max``)::

    {"platform": "telegram", "external_id": "tg:moscowmap", "username": "moscowmap",
     "title": "Новости Москвы", "kind": "channel" | "group" | "bot" | "user",
     "avatar_url": "https://cdn4.telesco.pe/file/…" | None, "subscribers": 2617260 | None,
     "description": "…" | None, "url": "https://t.me/moscowmap", "preview": True,
     "preview_url": "https://t.me/s/moscowmap" | None, "posts_available": True,
     "last_post_at": datetime | None, "note": "…"}

Ошибки — ``TelegramSourceError`` с человекочитаемой причиной (приватная ссылка, не найден, …).
"""
from __future__ import annotations

import asyncio
import html
import re
from typing import Any

from app.integrations.telegram.client import TelegramError, TelegramSourceError, chat_ext_id, parse_tg_ref
from app.integrations.webutil import TTLCache, http_client, parse_meta

_EXTRA_RE = re.compile(r'class="tgme_page_extra">\s*([^<]*?)\s*<', re.S)
_ACTION_RE = re.compile(r'class="tgme_action_button_new[^"]*"[^>]*>\s*([^<]*?)\s*<', re.S)
_PHOTO_RE = re.compile(r'class="tgme_page_photo_image[^"]*"\s+src="([^"]+)"')
_NUM_RE = re.compile(r"([\d\s .,]+)\s*([KMkм]?)\s*(subscribers?|members?|подписчик\w*|участник\w*|monthly users)", re.I)
_DEFAULT_IMAGES = ("telegram.org/img/t_logo", "telegram.org/img/")

_cache = TTLCache(ttl=600, maxsize=256)


def _count(extra: str) -> tuple[int | None, str | None]:
    m = _NUM_RE.search(extra or "")
    if not m:
        return None, None
    raw, suf, what = m.group(1), m.group(2).upper(), m.group(3).lower()
    digits = re.sub(r"[\s ]", "", raw)
    try:
        val = float(digits.replace(",", ".")) if suf else float(digits.replace(",", "").replace(".", ""))
    except ValueError:
        return None, what
    return int(val * {"": 1, "K": 1e3, "M": 1e6, "М": 1e6}.get(suf, 1)), what


async def resolve_telegram(ref: str, *, check_preview: bool = True, client: Any = None) -> dict[str, Any]:
    """См. docstring модуля. ``check_preview=False`` — без запроса t.me/s (быстрее)."""
    username = parse_tg_ref(ref)  # TelegramSourceError для приватных/битых ссылок
    cached = _cache.get((username, check_preview))
    if cached is not None:
        return dict(cached)
    own = client is None
    client = client or http_client()
    try:
        r = None
        for attempt in range(2):  # t.me иногда рвёт соединение — один повтор
            try:
                r = await client.get(f"https://t.me/{username}")
                break
            except Exception as e:  # noqa: BLE001
                if attempt:
                    raise TelegramError(f"t.me недоступен: {e!r}") from e
                await asyncio.sleep(0.5)
        assert r is not None
        if r.status_code == 404:
            raise TelegramSourceError(f"@{username} не найден")
        if r.status_code >= 400:
            raise TelegramError(f"t.me/{username}: HTTP {r.status_code}")
        page = r.text
        meta = parse_meta(page)
        extras = [html.unescape(x).strip() for x in _EXTRA_RE.findall(page)]
        actions = [html.unescape(x).strip().lower() for x in _ACTION_RE.findall(page)]
        title = (meta.get("og:title") or "").strip()
        if not title or title.startswith("Telegram: Contact @") or (not extras and "send message" in actions
                                                                     and title.startswith("Telegram")):
            raise TelegramSourceError(f"@{username} не найден или это закрытый аккаунт")
        subs, what = None, None
        for x in extras:
            subs, what = _count(x)
            if subs is not None:
                break
        if what and what.startswith(("subscriber", "подписчик")):
            kind = "channel"
        elif what and what.startswith(("member", "участник")):
            kind = "group"
        elif "start bot" in actions or (what and "monthly" in what) or username.endswith("bot"):
            kind = "bot"
        else:
            kind = "user"
        img = meta.get("og:image")
        m = _PHOTO_RE.search(page)
        avatar = (m.group(1) if m else img) or None
        if avatar and any(d in avatar for d in _DEFAULT_IMAGES):
            avatar = None
        desc = (meta.get("og:description") or "").strip() or None
        out: dict[str, Any] = {
            "platform": "telegram", "external_id": chat_ext_id(username), "username": username,
            "title": title, "kind": kind, "avatar_url": avatar,
            "subscribers": subs if kind in ("channel", "group") else None,
            "description": desc, "url": f"https://t.me/{username}",
            "preview": False, "preview_url": None, "posts_available": False, "last_post_at": None, "note": None,
        }
        if check_preview and kind in ("channel", "group"):
            try:
                p = await client.get(f"https://t.me/s/{username}")
                if p.status_code < 400 and "/s/" in str(p.url):
                    from app.integrations.telegram.web_preview import parse_preview_html

                    pp = parse_preview_html(p.text)
                    out["preview"] = True
                    out["preview_url"] = f"https://t.me/s/{username}"
                    out["posts_available"] = bool(pp.posts)
                    out["last_post_at"] = pp.posts[-1].sent_at if pp.posts else None
                    if pp.counter and not out["subscribers"]:
                        out["subscribers"] = pp.counter
            except Exception:  # noqa: BLE001
                pass
        if kind == "channel" and not out["preview"]:
            out["note"] = "Веб-превью t.me/s выключено — посты доступны только через Telethon-сессию или бота"
        elif kind == "group":
            out["note"] = "Группа: сообщения — через Telethon-сессию или бота-администратора (web-превью не отдаёт группы)"
        elif kind in ("bot", "user"):
            out["note"] = "Это не канал и не группа — как источник новостей не подключается"
        _cache.set((username, check_preview), out)
        return dict(out)
    finally:
        if own:
            await client.aclose()
