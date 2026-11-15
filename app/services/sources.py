"""Источники (MAX, Telegram, RSS-ленты СМИ): список, превью по ссылке, добавление, вкл/выкл, удаление.

CONTRACTS_v2 §4.4 + CONTRACTS_v3 §2. Зона DEV-SOURCES.

- ``resolve(session, ref, platform=None)`` — определить платформу по ссылке/@username и получить
  реальные метаданные (название, тип, аватар, подписчики) без сохранения. Кэш 10 минут.
  Telegram/MAX — функции DEV-REALDATA ``resolve_telegram(ref)`` / ``resolve_max(ref)`` (ищутся
  защитным импортом, см. ``_RESOLVER_MODULES``); пока их нет — встроенный разбор публичной
  страницы ``t.me/<name>`` / ``max.ru/<name>`` (og:title/og:image).
- ``add_source`` сохраняет реальное название из resolve; ``platform="rss"`` — лента СМИ.
- ``delete_source`` — мягкое удаление: источник скрыт, его сообщения ``archived`` (уходят из ленты,
  emerging, поиска), строки ``sources`` сюжетов и сами сообщения остаются.
- ``sync_rss`` — выгрузка RSS-источников в ``chat_messages`` (вызывают /api/sources/sync, фоновая
  задача в main.py и add_source).
"""
from __future__ import annotations

import dataclasses
import hashlib
import html as html_lib
import importlib
import inspect
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PLATFORM_LABELS, ChatMessage, MaxChat, Source, User, utcnow
from app.schemas import SourceChatOut, SyncResult
from app.services.stories import StoryError

log = logging.getLogger("newsflow.sources")

PLATFORMS = ("max", "telegram", "rss")
RESOLVE_TTL_SEC = 600
RESOLVE_ERROR_TTL_SEC = 60  # сетевые сбои/«не найден» кэшируем коротко
HTTP_TIMEOUT = 10.0
USER_AGENT = getattr(settings, "HTTP_USER_AGENT", None) or "NewsflowAI/1.0 (newsroom research tool; RSS reader)"
RSS_ITEMS_LIMIT = int(getattr(settings, "RSS_INGEST_PER_FEED", 0) or 50)

_TG_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?([^/?#]+)", re.I)
_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")
_MAX_ID_RE = re.compile(r"^-?\d{3,}$")

# где искать функции DEV-REALDATA (первая найденная побеждает)
_RESOLVER_MODULES: dict[str, tuple[str, ...]] = {
    "resolve_telegram": (
        "app.integrations.resolve", "app.integrations.resolver", "app.integrations.telegram.resolve",
        "app.integrations.telegram.web_preview", "app.integrations.telegram", "app.integrations.telegram.client",
    ),
    "resolve_max": (
        "app.integrations.resolve", "app.integrations.resolver", "app.integrations.max.resolve",
        "app.integrations.max.web", "app.integrations.max.web_preview", "app.integrations.max.public",
        "app.integrations.max", "app.integrations.max.client", "app.integrations.max.bot_api",
    ),
}

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


# ================================================================ режим данных
def data_mode() -> str:
    """``live`` | ``demo``. Источник истины — ``settings.DATA_MODE`` (DEV-REALDATA); пока поля нет — env."""
    v = getattr(settings, "DATA_MODE", None) or os.environ.get("DATA_MODE") or "demo"
    return str(getattr(v, "value", v)).strip().lower()


# ================================================================ список
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
    q = select(MaxChat).where(MaxChat.deleted_at.is_(None)).order_by(MaxChat.platform, MaxChat.id)
    if platform:
        q = q.where(MaxChat.platform == platform)
    chats = (await session.scalars(q)).all()
    counts = await _counts(session)
    return [chat_out(c, counts.get(c.id, 0)) for c in chats]


async def get_source_out(session: AsyncSession, chat_id: int) -> SourceChatOut:
    chat = await get_chat(session, chat_id)
    count = await session.scalar(select(func.count()).select_from(ChatMessage).where(
        ChatMessage.chat_id == chat.id, ChatMessage.archived.is_(False)))
    return chat_out(chat, int(count or 0))


# ================================================================ разбор ссылок
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


def _host(url: str) -> str:
    try:
        h = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def detect_platform(ref: str) -> str:
    """t.me/…, telegram.me/…, @name, tg:name, голый username → telegram; max.ru/… или числовой id → max;
    остальное (домен/URL) → rss."""
    raw = (ref or "").strip()
    low = raw.lower()
    if low.startswith(("@", "tg:")) or _host(raw) in _TG_HOSTS:
        return "telegram"
    if low.startswith("+"):
        return "telegram"  # приватный инвайт — parse_tg_ref даст понятную ошибку
    host = _host(raw)
    if host == "max.ru" or host.endswith(".max.ru"):
        return "max"
    if _MAX_ID_RE.match(raw):
        return "max"
    if _USERNAME_RE.match(raw) and "." not in raw:
        return "telegram"
    return "rss"


def _max_slug(ref: str) -> str:
    raw = ref.strip()
    if "max.ru" in raw.lower():
        path = urlparse(raw if "://" in raw else f"https://{raw}").path
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else ""
    return raw.lstrip("@")


def _norm_url(ref: str) -> str:
    raw = ref.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    return raw


def rss_external_id(feed_url: str) -> str:
    ext = f"rss:{feed_url.strip()}"
    if len(ext) > 128:
        ext = "rss:sha1:" + hashlib.sha1(feed_url.strip().encode()).hexdigest()
    return ext


# ================================================================ HTTP / HTML helpers
async def _http_get(url: str) -> tuple[str, bytes, str]:
    """(final_url, body, content_type). Ошибки — исключения httpx."""
    import httpx

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}) as c:
        for attempt in (1, 2):  # один повтор на сетевой сбой (ConnectError/таймаут)
            try:
                r = await c.get(url)
                break
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt == 2:
                    raise
        r.raise_for_status()
        return str(r.url), r.content[:3_000_000], r.headers.get("content-type", "")


def _decode(body: bytes, ctype: str = "") -> str:
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "cp1251"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def _meta(page: str, prop: str) -> str | None:
    for pat in (rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']*)["\']',
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(prop)}["\']'):
        m = re.search(pat, page, re.I)
        if m:
            return html_lib.unescape(m.group(1)).strip() or None
    return None


def _html_title(page: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if not m:
        return None
    return re.sub(r"\s+", " ", html_lib.unescape(m.group(1))).strip() or None


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>|</p>", "\n", s, flags=re.I)
    s = html_lib.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _parse_count(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"([\d][\d\s.,  ]*)\s*([KkMmКкМм]?)", s)
    if not m:
        return None
    num, suf = m.group(1).strip(), m.group(2).lower()
    if suf:
        try:
            return int(float(num.replace(",", ".").replace(" ", "")) * (1000 if suf in "kк" else 1_000_000))
        except ValueError:
            return None
    digits = re.sub(r"\D", "", num)
    return int(digits) if digits else None


# ================================================================ resolvers
def _find_resolver(name: str):
    for mod_name in _RESOLVER_MODULES.get(name, ()):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        except Exception as e:  # noqa: BLE001
            log.debug("resolver module %s failed to import: %r", mod_name, e)
            continue
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    return None


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {k: getattr(obj, k) for k in ("title", "username", "kind", "avatar_url", "subscribers", "url",
                                         "platform", "external_id", "description", "error") if hasattr(obj, k)}


async def _call(fn, *args) -> Any:
    res = fn(*args)
    if inspect.isawaitable(res):
        res = await res
    return res


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for k in ("title", "username", "kind", "avatar_url", "subscribers", "url", "external_id", "description",
              "site_url", "error"):
        v = extra.get(k)
        if v not in (None, ""):
            base[k] = v
    if isinstance(base.get("subscribers"), str):
        base["subscribers"] = _parse_count(base["subscribers"])
    if base.get("username"):
        base["username"] = str(base["username"]).lstrip("@")
    return base


async def _mock_tg_chat(username: str) -> dict[str, Any] | None:
    """Демо: mock-Telegram знает свои каналы — превью без сети."""
    try:
        from app.integrations.max.poller import telegram_module

        tg = telegram_module()
        if tg is None:
            return None
        client = tg.get_telegram_client()
        if getattr(client, "name", "") != "mock":
            return None
        for c in await client.list_chats():
            if (getattr(c, "username", "") or "").lower() == username:
                return {"title": c.title, "kind": getattr(c, "kind", None), "url": getattr(c, "url", None)}
    except Exception as e:  # noqa: BLE001
        log.debug("mock tg lookup failed: %r", e)
    return None


def _tg_client_is_mock() -> bool:
    try:
        from app.integrations.max.poller import telegram_module

        tg = telegram_module()
        return tg is None or getattr(tg.get_telegram_client(), "name", "") == "mock"
    except Exception:  # noqa: BLE001
        return True


async def _fetch_public_page(url: str) -> dict[str, Any]:
    """Публичная страница t.me/<name> или max.ru/<name> → og:title/og:image/описание/подписчики."""
    final, body, ctype = await _http_get(url)
    page = _decode(body, ctype)
    out: dict[str, Any] = {
        "title": _meta(page, "og:title") or _html_title(page),
        "avatar_url": _meta(page, "og:image"),
        "description": _meta(page, "og:description") or _meta(page, "description"),
    }
    m = re.search(r'class="tgme_page_extra"[^>]*>(.*?)</div>', page, re.S)
    extra = _strip_html(m.group(1)) if m else ""
    if not extra:  # max.ru и др.: «12 345 подписчиков», «1,2K subscribers»
        m2 = re.search(r"([\d][\d\s.,  ]*[KkMmКкМм]?)\s*(subscribers|members|подписчик\w*|участник\w*)",
                       _strip_html(page), re.I)
        extra = m2.group(0) if m2 else ""
    if extra:
        out["subscribers"] = _parse_count(extra)
        low = extra.lower()
        if "subscriber" in low or "подписч" in low:
            out["kind"] = "channel"
        elif "member" in low or "участник" in low:
            out["kind"] = "group"
    if "tgme_page_extra" not in page and "t.me" in final and "tgme_action_button" in page:
        out.setdefault("kind", "user")
    return out


async def _resolve_telegram(ref: str) -> dict[str, Any]:
    try:
        username = _tg_username(ref)
    except StoryError as e:
        return {"platform": "telegram", "title": ref.strip(), "error": e.detail}
    info: dict[str, Any] = {"platform": "telegram", "username": username, "title": f"@{username}",
                            "external_id": f"tg:{username}", "url": f"https://t.me/{username}"}
    mock = await _mock_tg_chat(username)
    if mock:
        return _merge(info, mock)
    resolver = _find_resolver("resolve_telegram")
    try:
        if resolver is not None:
            res = _as_dict(await _call(resolver, ref))
            if not str(res.get("external_id") or "").startswith("tg:"):
                res.pop("external_id", None)  # формат tg:<username> фиксирован (CONTRACTS_v2)
            return _merge(info, res)
        page = await _fetch_public_page(f"https://t.me/{username}")
        title = page.get("title") or ""
        if not title or title.lower().startswith("telegram: contact") or title.lower() == "telegram messenger":
            info["error"] = "Канал или группа не найдены (или нет публичного @username)"
            page.pop("title", None)
        return _merge(info, page)
    except Exception as e:  # noqa: BLE001
        info["error"] = f"Не удалось получить данные Telegram: {_err_text(e)}"
        return info


async def _resolve_max(ref: str) -> dict[str, Any]:
    raw = ref.strip()
    slug = _max_slug(raw)
    is_link = "max.ru" in raw.lower()
    info: dict[str, Any] = {"platform": "max", "title": raw if not is_link else slug or raw,
                            "external_id": slug or raw, "username": None if _MAX_ID_RE.match(slug) else slug,
                            "url": (f"https://max.ru/{slug}" if slug and not _MAX_ID_RE.match(slug) else None),
                            "kind": "chat" if _MAX_ID_RE.match(slug) else None}
    resolver = _find_resolver("resolve_max")
    try:
        if resolver is not None:
            return _merge(info, _as_dict(await _call(resolver, raw)))
        if not info["url"]:
            info["error"] = "Чат MAX по id подключается через бота (MAX_BOT_TOKEN): название придёт при синхронизации"
            return info
        page = await _fetch_public_page(info["url"])
        if not page.get("title") or page["title"].strip().lower() in ("max", "max — мессенджер"):
            info["error"] = "Публичная страница MAX не найдена или без названия"
            page.pop("title", None)
        return _merge(info, page)
    except Exception as e:  # noqa: BLE001
        info["error"] = f"Не удалось получить данные MAX: {_err_text(e)}"
        return info


def _err_text(e: Exception) -> str:
    s = (str(e).strip().splitlines() or [""])[0]
    return f"{type(e).__name__}: {s}"[:300] if s else type(e).__name__


# ---------------------------------------------------------------- RSS
def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for ch in el:
        if _local(ch.tag) in names:
            return ch
    return None


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def looks_like_feed(body: bytes, ctype: str = "") -> bool:
    head = body[:2000].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<rss", b"<feed", b"<rdf")):
        return True
    has_root = b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head
    if head.startswith(b"<?xml"):
        return has_root
    return "xml" in ctype.lower() and "html" not in ctype.lower() and has_root


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def parse_feed(body: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """RSS 2.0 / Atom / RSS 1.0 → (meta {title, site_url, description, avatar_url}, items[])."""
    root = ET.fromstring(body)
    channel = _child(root, "channel") if _local(root.tag) in ("rss", "rdf") else root
    channel = channel if channel is not None else root
    meta: dict[str, Any] = {"title": _strip_html(_text(_child(channel, "title"))) or None,
                            "description": _strip_html(_text(_child(channel, "description", "subtitle"))) or None}
    for link in channel:
        if _local(link.tag) != "link":
            continue
        href = link.get("href") or _text(link)
        if href and link.get("rel", "alternate") == "alternate":
            meta["site_url"] = href.strip()
            break
    img = _child(channel, "image", "logo", "icon")
    if img is not None:
        meta["avatar_url"] = (_text(_child(img, "url")) or _text(img) or None)
    items: list[dict[str, Any]] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        link = ""
        for ch in el:
            if _local(ch.tag) == "link":
                href = ch.get("href")
                if href and ch.get("rel", "alternate") != "alternate":
                    continue
                link = (href or _text(ch)).strip()
                if link:
                    break
        media = None
        for ch in el.iter():
            if _local(ch.tag) in ("enclosure", "content", "thumbnail") and ch.get("url") and (
                    (ch.get("type") or "image").startswith(("image", "video")) or _local(ch.tag) == "thumbnail"):
                media = ch.get("url")
                break
        title = _strip_html(_text(_child(el, "title")))
        desc = _strip_html(_text(_child(el, "description", "summary", "encoded")) or
                           _text(_child(el, "content")))
        guid = _text(_child(el, "guid", "id")) or link or title
        published = _parse_dt(_text(_child(el, "pubdate", "published", "updated", "date")))
        if not (title or desc):
            continue
        items.append({"guid": guid, "title": title, "text": desc, "url": link or None,
                      "published_at": published, "media_url": media})
    return meta, items


def _feed_links(page: str, base: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"<link\b[^>]*>", page, re.I):
        tag = m.group(0)
        if not re.search(r'type=["\']application/(rss|atom)\+xml["\']', tag, re.I):
            continue
        h = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if h:
            out.append(urljoin(base, html_lib.unescape(h.group(1)).strip()))
    return out


async def _resolve_rss(ref: str) -> dict[str, Any]:
    url = _norm_url(ref)
    info: dict[str, Any] = {"platform": "rss", "title": _host(url) or ref.strip(), "kind": "feed", "url": None,
                            "site_url": None}
    try:
        final, body, ctype = await _http_get(url)
    except Exception as e:  # noqa: BLE001
        info["error"] = f"Сайт недоступен: {_err_text(e)}"
        return info
    candidates: list[str] = []
    page_title = None
    if looks_like_feed(body, ctype):
        feed_url, feed_body = final, body
    else:
        page = _decode(body, ctype)
        page_title = _meta(page, "og:site_name") or _html_title(page)
        info["site_url"] = final
        info["avatar_url"] = _meta(page, "og:image")
        if page_title:
            info["title"] = page_title
        candidates = _feed_links(page, final)
        root = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
        candidates += [urljoin(root, p) for p in ("/rss", "/rss.xml", "/feed", "/feed/", "/rss/")]
        feed_url = feed_body = None
        seen: set[str] = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            try:
                f_final, f_body, f_ctype = await _http_get(cand)
            except Exception:  # noqa: BLE001
                continue
            if looks_like_feed(f_body, f_ctype):
                feed_url, feed_body = f_final, f_body
                break
            if len(seen) >= 6:
                break
        if feed_url is None:
            info["error"] = "RSS-лента на сайте не найдена — укажите прямую ссылку на ленту"
            return info
    try:
        meta, items = parse_feed(feed_body)
    except ET.ParseError as e:
        info["error"] = f"Лента не разобрана: {e}"
        return info
    info["url"] = feed_url
    info["external_id"] = rss_external_id(feed_url)
    info["title"] = meta.get("title") or page_title or info["title"]
    info["description"] = meta.get("description")
    info["site_url"] = info.get("site_url") or meta.get("site_url")
    info["avatar_url"] = meta.get("avatar_url") or info.get("avatar_url")
    info["items_count"] = len(items)
    if not items:
        info["error"] = "Лента пуста"
    return info


# ================================================================ resolve (превью)
def _cache_key(platform: str, ref: str) -> str:
    raw = ref.strip()
    if platform == "telegram":
        try:
            return _tg_username(raw)
        except StoryError:
            return raw.lower()
    return raw.lower().rstrip("/")


def clear_resolve_cache() -> None:
    _cache.clear()


async def _already(session: AsyncSession, platform: str, info: dict[str, Any]) -> MaxChat | None:
    ext = info.get("external_id")
    q = select(MaxChat).where(MaxChat.platform == platform, MaxChat.deleted_at.is_(None))
    if ext:
        return await session.scalar(q.where(MaxChat.external_id == ext))
    if platform == "rss" and info.get("url"):
        return await session.scalar(q.where(MaxChat.url == info["url"]))
    return None


async def resolve(session: AsyncSession, ref: str, platform: str | None = None, *,
                  use_cache: bool = True) -> dict[str, Any]:
    """Превью источника по ссылке/@username без сохранения. Ошибка разбора/сети — в ``error``."""
    ref = (ref or "").strip()
    if not ref:
        raise StoryError(422, "Вставьте ссылку (t.me/…, max.ru/…, сайт или RSS) или @username")
    if platform in (None, "", "auto"):
        platform = detect_platform(ref)
    if platform not in PLATFORMS:
        raise StoryError(422, "platform: max | telegram | rss")
    key = (platform, _cache_key(platform, ref))
    now = time.monotonic()
    hit = _cache.get(key) if use_cache else None
    ttl = RESOLVE_ERROR_TTL_SEC if hit and hit[1].get("error") else RESOLVE_TTL_SEC
    cached = bool(hit and now - hit[0] < ttl)
    if cached:
        info = dict(hit[1])
    else:
        if platform == "telegram":
            info = await _resolve_telegram(ref)
        elif platform == "max":
            info = await _resolve_max(ref)
        else:
            info = await _resolve_rss(ref)
        info["platform"] = platform
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (now, dict(info))
    info.update(ref=ref, platform_label=PLATFORM_LABELS.get(platform, platform), cached=cached)
    info["title"] = str(info.get("title") or ref)[:255]
    chat = await _already(session, platform, info)
    info["already_added"] = chat is not None
    info["chat_id"] = chat.id if chat else None
    return info


# ================================================================ добавление
async def add_source(session: AsyncSession, user: User | None, platform: str, ref: str, *,
                     title: str | None = None, district: str | None = None) -> tuple[MaxChat, bool]:
    """Подключить источник. Telegram: @username / t.me-ссылка; MAX: id чата бота или max.ru-ссылка;
    RSS: URL ленты или сайта (лента ищется). Название — реальное (resolve), если редактор не задал своё.
    Возвращает (chat, created). Сразу пытается синхронизировать."""
    ref = (ref or "").strip()
    if not ref:
        raise StoryError(422, "Укажите @username или ссылку t.me на публичный канал/чат")
    if platform in ("auto", "", None):
        platform = detect_platform(ref)
    info: dict[str, Any] = {}
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
                info = {"kind": getattr(chat_info, "kind", None), "url": getattr(chat_info, "url", None)}
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
        if not _tg_client_is_mock():  # живой режим: реальные название/аватар/подписчики (кэш resolve)
            res = await resolve(session, ref, "telegram")
            if not res.get("error"):
                info = {**info, **{k: v for k, v in res.items() if v not in (None, "")}}
                resolved_title = res.get("title") or resolved_title
        info.setdefault("username", username)
        info.setdefault("url", f"https://t.me/{username}")
        chat_title = (title or resolved_title or f"@{username}")[:255]
    elif platform == "max":
        external_id = ref
        resolved_title = None
        if "max.ru" in ref.lower() or (_find_resolver("resolve_max") is not None and data_mode() == "live"):
            res = await resolve(session, ref, "max")
            external_id = str(res.get("external_id") or _max_slug(ref) or ref)
            info = {k: v for k, v in res.items() if v not in (None, "")}
            if not res.get("error"):
                resolved_title = res.get("title")
        chat_title = (title or resolved_title or ref)[:255]
    elif platform == "rss":
        res = await resolve(session, ref, "rss")
        if not res.get("url"):
            raise StoryError(422, res.get("error") or "RSS-лента не найдена")
        external_id = res.get("external_id") or rss_external_id(res["url"])
        info = {k: v for k, v in res.items() if v not in (None, "")}
        chat_title = (title or res.get("title") or ref)[:255]
    else:
        raise StoryError(422, "platform: max | telegram | rss")

    chat = await session.scalar(select(MaxChat).where(MaxChat.platform == platform, MaxChat.external_id == external_id))
    created = chat is None or chat.deleted_at is not None
    if chat is None:
        chat = MaxChat(platform=platform, external_id=external_id, ref=ref[:255], title=chat_title,
                       district=(district or None), enabled=True, added_by_id=user.id if user else None,
                       created_at=utcnow())
        session.add(chat)
    else:
        if chat.deleted_at is not None:  # повторное добавление удалённого источника
            chat.deleted_at = None
            chat.ref = ref[:255]
            chat.added_by_id = user.id if user else chat.added_by_id
        chat.enabled = True
        if title or (chat_title and chat_title != chat.title and not chat_title.startswith("@")):
            chat.title = chat_title
        if district:
            chat.district = district
    for field, limit in (("username", 128), ("kind", 16), ("url", 512), ("site_url", 512), ("avatar_url", 1024)):
        v = info.get(field)
        if v not in (None, ""):
            setattr(chat, field, str(v)[:limit])
    if info.get("description"):
        chat.description = str(info["description"])[:4000]
    if isinstance(info.get("subscribers"), int):
        chat.subscribers = info["subscribers"]
    await session.commit()
    await session.refresh(chat)

    try:  # первая выгрузка сообщений нового источника
        if platform == "rss":
            await sync_rss(session, chat_ids=[chat.id])
        else:
            from app.integrations.max.poller import sync_once

            await sync_once(session, platforms=(platform,))
    except Exception as e:  # noqa: BLE001
        log.warning("sync after add_source failed: %r", e)
    await session.refresh(chat)
    return chat, created


async def get_chat(session: AsyncSession, chat_id: int) -> MaxChat:
    chat = await session.get(MaxChat, chat_id)
    if chat is None or chat.deleted_at is not None:
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


# ================================================================ удаление
async def delete_source(session: AsyncSession, chat_id: int) -> dict[str, Any]:
    """Удалить источник (мягко): скрыт из списков, выключен, все его сообщения ``archived`` — уходят из
    ленты, emerging и поиска. Сообщения и строки ``sources`` сюжетов НЕ удаляются: цитаты/ссылки в уже
    созданных сюжетах остаются. Telegram — ``remove_source`` у клиента."""
    chat = await get_chat(session, chat_id)
    res = await session.execute(
        update(ChatMessage).where(ChatMessage.chat_id == chat.id, ChatMessage.archived.is_(False))
        .values(archived=True).execution_options(synchronize_session=False)
    )
    archived = int(res.rowcount or 0)
    refs = int(await session.scalar(
        select(func.count()).select_from(Source).join(ChatMessage, Source.chat_message_id == ChatMessage.id)
        .where(ChatMessage.chat_id == chat.id)
    ) or 0)
    chat.enabled = False
    chat.deleted_at = utcnow()
    if chat.platform == "telegram":
        try:
            from app.integrations.max.poller import telegram_module

            tg = telegram_module()
            if tg is not None:
                client = tg.get_telegram_client()
                if hasattr(client, "remove_source"):
                    await _call(client.remove_source, chat.external_id)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram remove_source(%s) failed: %r", chat.external_id, e)
    out = {"id": chat.id, "title": chat.title, "platform": chat.platform, "deleted": True,
           "archived_messages": archived, "story_references": refs}
    await session.commit()
    log.info("Source %s (%s) deleted: %s messages archived, %s story refs kept", chat.id, chat.title, archived, refs)
    return out


# ================================================================ RSS sync
def _msg_ext_id(guid: str) -> str:
    guid = (guid or "").strip()
    return guid if 0 < len(guid) <= 128 else hashlib.sha1(guid.encode()).hexdigest()


async def sync_rss(session: AsyncSession, chat_ids: list[int] | None = None) -> SyncResult:
    """Выгрузить RSS-источники (platform='rss', enabled, не удалённые) в chat_messages. Commit."""
    result = SyncResult()
    q = select(MaxChat).where(MaxChat.platform == "rss", MaxChat.enabled.is_(True), MaxChat.deleted_at.is_(None))
    if chat_ids:
        q = q.where(MaxChat.id.in_(chat_ids))
    chats = (await session.scalars(q)).all()
    now = utcnow()
    for chat in chats:
        result.chats += 1
        feed_url = chat.url or (chat.external_id[4:] if chat.external_id.startswith("rss:http") else None)
        if not feed_url:
            result.errors.append(f"{chat.title}: нет URL ленты")
            continue
        try:
            _, body, _ = await _http_get(feed_url)
            meta, items = parse_feed(body)
        except Exception as e:  # noqa: BLE001
            log.warning("rss %s failed: %r", feed_url, e)
            result.errors.append(f"{chat.title}: {_err_text(e)}")
            continue
        max_age = int(getattr(settings, "RSS_INGEST_MAX_AGE_HOURS", 0) or 0)
        if max_age:
            items = [i for i in items if (i["published_at"] or now) >= now - timedelta(hours=max_age)]
        items = sorted(items, key=lambda i: i["published_at"] or now, reverse=True)[:RSS_ITEMS_LIMIT]
        by_ext = {_msg_ext_id(i["guid"]): i for i in items}
        known = set((await session.scalars(select(ChatMessage.external_id).where(
            ChatMessage.chat_id == chat.id, ChatMessage.external_id.in_(list(by_ext))))).all()) if by_ext else set()
        author = f"rss:{_host(chat.site_url or feed_url)}"
        for ext, it in sorted(by_ext.items(), key=lambda kv: kv[1]["published_at"] or now):
            if ext in known:
                continue
            sent = it["published_at"] or now
            if sent > now + timedelta(minutes=5):
                sent = now
            text = it["title"] if not it["text"] else (
                it["text"] if it["text"].startswith(it["title"]) else f"{it['title']}\n{it['text']}")
            session.add(ChatMessage(
                chat_id=chat.id, platform="rss", external_id=ext, author_external_id=author[:128],
                author_name=(chat.title or meta.get("title") or "")[:128] or None, text=text[:8000],
                sent_at=sent, has_media=bool(it["media_url"]),
                media_url=(it["media_url"] or None) and it["media_url"][:1024],
                message_url=(it["url"] or None) and it["url"][:1024], is_forwarded=False,
            ))
            result.new_messages += 1
            result.by_platform["rss"] = result.by_platform.get("rss", 0) + 1
        chat.last_synced_at = utcnow()
    await session.commit()
    result.finished_at = utcnow()
    return result
