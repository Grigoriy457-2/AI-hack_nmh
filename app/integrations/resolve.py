"""Единая точка «получить название источника по ссылке» (зона DEV-REALDATA, для DEV-SOURCES).

``await resolve_source(ref, platform=None) -> dict`` — определяет платформу по ссылке и тянет
реальные метаданные:
- Telegram (``@name``, ``t.me/…``) → ``telegram.resolve.resolve_telegram``;
- MAX (``max.ru/…``, ``max:…``) → ``max.public.resolve_max``;
- иначе URL ``http(s)://…`` → RSS/Atom-лента (``resolve_rss``): название ленты, сайт, картинка.

Общий формат ответа::

    {"platform": "telegram"|"max"|"rss", "external_id": str|None, "username": str|None,
     "title": str, "kind": "channel"|"group"|"bot"|"user"|"feed"|"unknown",
     "avatar_url": str|None, "subscribers": int|None, "description": str|None,
     "url": str, "site_url": str|None (rss), "preview": bool, "posts_available": bool,
     "last_post_at": datetime|None, "note": str|None}

Ошибки — ``ResolveError`` (ValueError) с человекочитаемым текстом; исходные исключения платформ
(``TelegramSourceError``, ``MaxResolveError``) приводятся к нему.
"""
from __future__ import annotations

import re
from typing import Any

from app.integrations.webutil import host_of, http_client


class ResolveError(ValueError):
    pass


_TG_RE = re.compile(r"^(@|(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/|tg:)", re.I)
_MAX_RE = re.compile(r"^((?:https?://)?(?:www\.|web\.)?max\.ru/|max:)", re.I)


def detect_platform(ref: str) -> str | None:
    r = (ref or "").strip()
    if _TG_RE.match(r):
        return "telegram"
    if _MAX_RE.match(r):
        return "max"
    if re.match(r"^https?://", r, re.I):
        return "rss"
    return None


async def resolve_rss(url: str, *, client: Any = None) -> dict[str, Any]:
    """Проверить, что по URL — RSS/Atom-лента, и вернуть её название/сайт/картинку."""
    import xml.etree.ElementTree as ET

    from app.integrations.rss import ATOM, Feed, configured_feeds, feed_ext_id, parse_feed

    own = client is None
    client = client or http_client()
    try:
        try:
            r = await client.get(url)
        except Exception as e:  # noqa: BLE001
            raise ResolveError(f"{host_of(url) or url} недоступен: {e!r}") from e
        if r.status_code >= 400:
            raise ResolveError(f"{url}: HTTP {r.status_code}")
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            raise ResolveError("По ссылке не RSS/Atom-лента (страница HTML?). Укажите адрес RSS-ленты "
                               "издания, например https://www.m24.ru/rss.xml") from e
        ch = root.find("channel")
        if ch is not None:
            title = (ch.findtext("title") or "").strip()
            site = (ch.findtext("link") or "").strip() or None
            desc = (ch.findtext("description") or "").strip() or None
            img = (ch.findtext("image/url") or "").strip() or None
        elif root.tag == f"{ATOM}feed":
            title = (root.findtext(f"{ATOM}title") or "").strip()
            link = next((lk.get("href") for lk in root.findall(f"{ATOM}link")
                         if lk.get("rel", "alternate") == "alternate"), None)
            site, desc, img = link, (root.findtext(f"{ATOM}subtitle") or None), (root.findtext(f"{ATOM}logo") or None)
        else:
            raise ResolveError("По ссылке XML, но не RSS/Atom-лента")
        final_url = str(r.url)
        known = next((f for f in configured_feeds() if f.url in (url, final_url)), None)
        items = parse_feed(r.content, known or Feed(final_url, title or host_of(final_url), site or ""))
    finally:
        if own:
            await client.aclose()
    dates = [i.published_at for i in items if i.published_at]
    return {
        "platform": "rss", "external_id": feed_ext_id(final_url), "username": None,
        "title": (known.title if known else None) or title or host_of(final_url),
        "kind": "feed", "avatar_url": img, "subscribers": None, "description": desc,
        "url": final_url, "site_url": site or (known.site if known else None),
        "preview": True, "posts_available": bool(items), "last_post_at": max(dates) if dates else None,
        "items_count": len(items), "note": None if items else "Лента пустая",
    }


async def resolve_source(ref: str, platform: str | None = None) -> dict[str, Any]:
    plat = platform or detect_platform(ref)
    if plat is None:
        # голое имя: по умолчанию Telegram (@username) — как было в v2
        plat = "telegram"
    if plat == "telegram":
        from app.integrations.telegram.client import TelegramError
        from app.integrations.telegram.resolve import resolve_telegram

        try:
            return await resolve_telegram(ref)
        except TelegramError as e:
            raise ResolveError(str(e)) from e
    if plat == "max":
        from app.integrations.max.public import MaxResolveError, resolve_max

        try:
            return await resolve_max(ref)
        except MaxResolveError as e:
            raise ResolveError(str(e)) from e
    if plat == "rss":
        return await resolve_rss(ref.strip())
    raise ResolveError(f"Неизвестная платформа: {plat}")
