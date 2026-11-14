"""Telegram без авторизации: парсинг веб-превью https://t.me/s/<channel> (зона DEV-TELEGRAM).

Запасной вариант на случай, если на защите нет Telethon-сессии. Работает только для публичных
КАНАЛОВ с включённым веб-превью (группы и каналы с выключенным превью t.me/s не отдаёт —
``TelegramSourceError``). Реальное время — опрос раз в ``poll_interval`` секунд (базовый
``TelegramClient.listen``); повторные запросы к одному каналу чаще, чем раз в
``min_refetch`` секунд, отдаются из кэша. HTTP 429 → ждём ``Retry-After`` (не более 60 с).

Парсер — стандартный ``html.parser`` (без новых зависимостей). Разметка t.me (2024–2026):
``div.tgme_widget_message[data-post="name/123"]`` → ``div.tgme_widget_message_text`` (текст,
``<br>`` → перевод строки), ``a.tgme_widget_message_date > time[datetime]``,
``div.tgme_widget_message_forwarded_from``, ``a.tgme_widget_message_photo_wrap[style*=url()]``,
``.tgme_widget_message_video_player``, ``span.tgme_widget_message_views``,
``span.tgme_widget_message_from_author`` (подпись автора поста), шапка канала
``.tgme_channel_info_header_title`` и ``.tgme_channel_info_counter``.
Авторы — только канал (или подпись автора поста); телефонов в веб-превью нет.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

import httpx

from app.integrations.telegram.client import (
    TelegramClient,
    TelegramError,
    TelegramSourceError,
    TgChat,
    TgMessage,
    TgUserProfile,
    chat_ext_id,
    is_tg_id,
    parse_tg_ref,
    strip_prefix,
    to_naive_utc,
)

log = logging.getLogger("newsflow.telegram")

_VOID = {"br", "img", "meta", "link", "input", "hr", "source", "wbr", "area", "base", "col", "embed", "track"}
_BG_URL_RE = re.compile(r"background-image\s*:\s*url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)
_NUM_RE = re.compile(r"^\s*([\d.,]+)\s*([KMB]?)\s*$", re.I)


def parse_count(s: str | None) -> int | None:
    """``"48.2K"`` → 48200, ``"1.5M"`` → 1500000, ``"12 345"`` → 12345."""
    if not s:
        return None
    s = s.replace(" ", "").replace(" ", "")
    m = _NUM_RE.match(s)
    if not m:
        return None
    num, suf = m.group(1), m.group(2).upper()
    try:
        val = float(num.replace(",", ".")) if suf else float(num.replace(",", "").replace(".", ""))
    except ValueError:
        return None
    return int(round(val * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[suf]))


@dataclass
class ParsedPost:
    username: str
    message_id: int
    text: str = ""
    sent_at: datetime | None = None
    url: str | None = None
    is_forwarded: bool = False
    forwarded_from: str | None = None
    media_url: str | None = None
    has_media: bool = False
    views: int | None = None
    author_signature: str | None = None
    owner_name: str | None = None


@dataclass
class ParsedPage:
    title: str | None = None
    counter: int | None = None
    counter_type: str | None = None  # subscribers / members
    description: str | None = None
    posts: list[ParsedPost] = field(default_factory=list)


class _Node:
    __slots__ = ("tag", "roles")

    def __init__(self, tag: str, roles: frozenset[str]) -> None:
        self.tag = tag
        self.roles = roles


_ROLE_CLASSES = {
    "tgme_widget_message_text": "text",
    "tgme_widget_message_reply": "reply",
    "tgme_widget_message_views": "views",
    "tgme_widget_message_from_author": "author",
    "tgme_widget_message_owner_name": "owner",
    "tgme_widget_message_forwarded_from_name": "fwd_name",
    "tgme_channel_info_header_title": "title",
    "tgme_channel_info_description": "description",
    "counter_value": "counter_value",
    "counter_type": "counter_type",
    "tgme_channel_info_counter": "counter",
}
_MEDIA_CLASSES = {
    "tgme_widget_message_photo_wrap", "tgme_widget_message_video_player", "tgme_widget_message_video",
    "tgme_widget_message_roundvideo", "tgme_widget_message_document", "tgme_widget_message_voice",
    "tgme_widget_message_sticker_wrap", "tgme_widget_message_grouped_wrap",
}


class TgPreviewParser(HTMLParser):
    """Потоковый разбор страницы t.me/s/<channel> в ``ParsedPage``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = ParsedPage()
        self._stack: list[_Node] = []
        self._post: ParsedPost | None = None
        self._buf: dict[str, list[str]] = {}
        self._text_done = False
        self._counter_parts: list[tuple[str, str]] = []
        self._cv: list[str] = []
        self._ct: list[str] = []
        self._title: list[str] = []
        self._desc: list[str] = []

    # -- helpers
    def _active(self, role: str) -> bool:
        return any(role in n.roles for n in self._stack)

    def _append(self, role: str, s: str) -> None:
        self._buf.setdefault(role, []).append(s)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        classes = set(a.get("class", "").split())
        roles: set[str] = {r for c, r in _ROLE_CLASSES.items() if c in classes}

        if "tgme_widget_message" in classes and a.get("data-post"):
            post_ref = a["data-post"]
            user, _, mid = post_ref.rpartition("/")
            if mid.isdigit():
                self._finish_post()
                self._post = ParsedPost(username=user.lower(), message_id=int(mid))
                self._buf, self._text_done = {}, False
                roles.add("msg")

        if self._post is not None:
            if "tgme_widget_message_forwarded_from" in classes:
                self._post.is_forwarded = True
            if classes & _MEDIA_CLASSES and not self._active("reply"):
                self._post.has_media = True
                m = _BG_URL_RE.search(a.get("style", ""))
                if m and not self._post.media_url:
                    self._post.media_url = m.group(1)
                elif not self._post.media_url and a.get("href", "").startswith("http"):
                    self._post.media_url = a["href"]
            if tag == "video" and a.get("src") and not self._post.media_url:
                self._post.media_url = a["src"]
                self._post.has_media = True
            if "tgme_widget_message_date" in classes and a.get("href"):
                self._post.url = a["href"]
            if tag == "time" and a.get("datetime") and self._post.sent_at is None and not self._active("reply"):
                try:
                    self._post.sent_at = to_naive_utc(datetime.fromisoformat(a["datetime"]))
                except ValueError:
                    pass
            # текст поста — только первый блок вне цитаты-ответа
            if "text" in roles and (self._text_done or self._active("reply")):
                roles.discard("text")
        if tag == "br" and self._active("text"):
            self._append("text", "\n")
        if tag in _VOID:
            return
        self._stack.append(_Node(tag, frozenset(roles)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        # закрываем до совпадающего тега (терпимо к кривой разметке)
        idx = next((i for i in range(len(self._stack) - 1, -1, -1) if self._stack[i].tag == tag), None)
        if idx is None:
            return
        while len(self._stack) > idx:
            node = self._stack.pop()
            if "text" in node.roles and self._post is not None:
                self._text_done = True
            if "counter" in node.roles:
                self._counter_parts.append(("".join(self._cv).strip(), "".join(self._ct).strip()))
                self._cv, self._ct = [], []
            if "msg" in node.roles:
                self._finish_post()

    def handle_data(self, data: str) -> None:
        if self._post is not None:
            for role in ("text", "views", "author", "owner", "fwd_name"):
                if self._active(role) and (role == "text" or not self._active("reply")):
                    self._append(role, data)
        else:
            if self._active("title"):
                self._title.append(data)
            if self._active("description"):
                self._desc.append(data)
        if self._active("counter_value"):
            self._cv.append(data)
        elif self._active("counter_type"):
            self._ct.append(data)

    def _finish_post(self) -> None:
        p = self._post
        if p is None:
            return
        self._post = None
        b = {k: "".join(v) for k, v in self._buf.items()}
        p.text = re.sub(r"[ \t ]+", " ", b.get("text", "")).strip()
        p.text = re.sub(r" ?\n ?", "\n", p.text)
        p.views = parse_count(b.get("views"))
        p.author_signature = (b.get("author") or "").strip() or None
        p.owner_name = (b.get("owner") or "").strip() or None
        p.forwarded_from = (b.get("fwd_name") or "").strip() or None
        if p.sent_at is not None:
            self.page.posts.append(p)
        self._buf = {}

    def close(self) -> None:
        super().close()
        self._finish_post()
        self.page.title = " ".join("".join(self._title).split()) or None
        self.page.description = "".join(self._desc).strip() or None
        for value, ctype in self._counter_parts:
            if ctype.lower() in ("subscribers", "members", "подписчиков", "подписчика", "подписчик",
                                 "участников", "участника", "subscriber", "member"):
                self.page.counter = parse_count(value)
                self.page.counter_type = ctype
                break


def parse_preview_html(html: str) -> ParsedPage:
    """Разобрать HTML t.me/s/<channel>. Посты — по возрастанию id."""
    p = TgPreviewParser()
    p.feed(html)
    p.close()
    p.page.posts.sort(key=lambda x: x.message_id)
    return p.page


class TelegramWebPreviewClient(TelegramClient):
    """Публичные каналы через https://t.me/s/<channel>, без авторизации."""

    name = "web"

    def __init__(self, sources: list[str] | None = None, *, base_url: str = "https://t.me",
                 poll_interval: int = 20, backfill: int = 30, min_refetch: float = 5.0,
                 max_pages: int = 5, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(sources, poll_interval=poll_interval, backfill=backfill)
        self.base_url = base_url.rstrip("/")
        self.min_refetch = min_refetch
        self.max_pages = max_pages
        self._http = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NewsflowPreview/1.0)",
                "Accept-Language": "ru,en;q=0.8",
            },
        )
        self._chats: dict[str, TgChat] = {}
        self._cache: dict[str, tuple[float, ParsedPage]] = {}  # url -> (ts, page)
        self._profiles: dict[str, TgUserProfile] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_page(self, username: str, before: int | None = None) -> ParsedPage:
        url = f"{self.base_url}/s/{username}" + (f"?before={before}" if before else "")
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self.min_refetch:
            return cached[1]
        for attempt in range(2):
            try:
                r = await self._http.get(url)
            except httpx.HTTPError as e:
                raise TelegramError(f"t.me недоступен: {e!r}") from e
            if r.status_code == 429 and attempt == 0:
                wait = min(int(r.headers.get("Retry-After", "10") or 10), 60)
                log.warning("t.me 429 for %s, waiting %ss", username, wait)
                await asyncio.sleep(wait)
                continue
            break
        if r.status_code == 404:
            raise TelegramSourceError(f"Канал @{username} не найден")
        if r.status_code >= 400:
            raise TelegramError(f"t.me/s/{username}: HTTP {r.status_code}")
        # Нет превью → t.me редиректит на t.me/<name> (страница без ленты).
        if "/s/" not in str(r.url):
            raise TelegramSourceError(
                f"У @{username} нет веб-превью (группа, приватный канал или превью выключено). "
                "Подключите через Telethon-сессию или бота."
            )
        page = parse_preview_html(r.text)
        self._cache[url] = (time.monotonic(), page)
        return page

    async def add_source(self, ref: str) -> TgChat:
        username = parse_tg_ref(ref)
        page = await self._get_page(username)
        if not page.posts and not page.title:
            raise TelegramSourceError(f"@{username}: веб-превью пустое или недоступно")
        ext = chat_ext_id(username)
        chat = TgChat(external_id=ext, title=page.title or f"@{username}", district=None,
                      members_count=page.counter, username=username, kind="channel",
                      url=f"https://t.me/{username}")
        self._chats[ext] = chat
        self._sources[ext] = ref
        self._profiles[ext] = TgUserProfile(external_id=ext, name=chat.title, username=username)
        return chat

    async def remove_source(self, chat_id: str) -> None:
        await super().remove_source(chat_id)
        self._chats.pop(str(chat_id), None)

    async def list_chats(self) -> list[TgChat]:
        if self._pending:
            await self._resolve_pending()
        return list(self._chats.values())

    def _to_message(self, chat: TgChat, p: ParsedPost) -> TgMessage:
        if p.author_signature:  # подпись автора поста — «автор» внутри канала
            author_id = f"{chat.external_id}#{p.author_signature}"
            self._profiles.setdefault(author_id, TgUserProfile(
                external_id=author_id, name=f"{p.author_signature} ({chat.title})",
                username=chat.username))
        else:
            author_id = chat.external_id
        return TgMessage(
            external_id=str(p.message_id),
            chat_external_id=chat.external_id,
            author_external_id=author_id,
            author_name=p.author_signature or p.owner_name or chat.title,
            text=p.text,
            sent_at=p.sent_at,  # type: ignore[arg-type]
            has_media=p.has_media,
            media_url=p.media_url,
            is_forwarded=p.is_forwarded,
            url=p.url or f"https://t.me/{chat.username}/{p.message_id}",
            views=p.views,
        )

    async def fetch_messages(self, chat_id: str, since: datetime | None = None,
                             limit: int = 100) -> list[TgMessage]:
        if not is_tg_id(chat_id):
            return []
        chat = self._chats.get(chat_id)
        if chat is None:
            chat = await self.add_source(strip_prefix(chat_id))
        username = chat.username or strip_prefix(chat_id)
        if since is None:
            limit = min(limit, self.backfill)
        out: dict[int, ParsedPost] = {}
        before: int | None = None
        for _ in range(self.max_pages):
            page = await self._get_page(username, before)
            if not page.posts:
                break
            for p in page.posts:
                if since is None or (p.sent_at and p.sent_at > since):
                    out[p.message_id] = p
            oldest = page.posts[0]
            if len(out) >= limit:
                break
            if since is not None and oldest.sent_at and oldest.sent_at <= since:
                break
            if since is None:
                break  # первый показ — достаточно последней страницы
            before = oldest.message_id
        posts = sorted(out.values(), key=lambda p: p.message_id)[-limit:]
        msgs = [self._to_message(chat, p) for p in posts if p.text or p.has_media]
        msgs.sort(key=lambda m: (m.sent_at, int(m.external_id)))
        return msgs

    async def get_user_profile(self, user_id: str) -> TgUserProfile | None:
        return self._profiles.get(str(user_id))

    async def status(self) -> dict[str, Any]:
        st = await super().status()
        st["note"] = "web-превью t.me/s: только публичные каналы, опрос раз в %s с" % self.poll_interval
        return st
