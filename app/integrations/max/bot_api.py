"""Реальный клиент MAX Bot API (зона DEV-REALDATA). httpx async.

Документация: https://dev.max.ru/docs-api (сверено 2026-11-15, см. docs/MAX.md). База —
``https://platform-api2.max.ru`` (с 19.07.2026; TLS-сертификат Минцифры — «Russian Trusted Root CA»,
его нет в certifi: ``MAX_CA_BUNDLE=<pem>``; без него клиент сам переходит на
``https://platform-api.max.ru``, пока тот отвечает). Токен — только заголовком
``Authorization: <token>`` (query-параметр больше не поддерживается). Токен выдаёт платформа
«MAX для партнёров» (business.max.ru) — только верифицированным юрлицам/ИП/самозанятым РФ.

Что используем:
- ``GET /chats`` (устарел с 06.2026, пробуем для совместимости) + ``GET /updates`` (события
  ``bot_added`` / ``message_created`` → chat_id) + ``MAX_CHAT_IDS`` → ``GET /chats/{chatId}``
  (title, type chat|channel, link, participants_count, icon).
- ``GET /messages?chat_id=&count=&to=`` → ``{"messages": [Message]}`` — **только если бот —
  администратор** чата/канала; новые первыми; ``from``/``to`` — Unix ms.
  ``Message = {sender, recipient: {chat_id, chat_type}, timestamp: ms, link: {type: forward|reply},
  body: {mid, text, attachments}, stat: {views} (каналы), url (публичная ссылка на пост канала)}``.
- ``GET /chats/{chatId}/members?user_ids=...`` → ``{"members": [ChatMember]}`` — профиль участника.

Телефон: Bot API НЕ отдаёт телефон участника. Он известен боту только если пользователь сам
поделился контактом (вложение ``type=contact`` с собственным ``max_info.user_id``) — такие
телефоны мы запоминаем при чтении сообщений. Иначе ``phone=None`` → в UI «телефон скрыт».
Вложения без ``payload.url`` (видео) → ``has_media=True``, ``media_url=None`` (ссылки не выдумываем).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from app.integrations.max.client import MaxChat, MaxClient, MaxMessage, MaxUserProfile

log = logging.getLogger("newsflow.max")

PAGE = 100  # максимум count в MAX Bot API
_TEL_RE = re.compile(r"^TEL[^:]*:(.+)$", re.MULTILINE | re.IGNORECASE)


class MaxApiError(RuntimeError):
    pass


@dataclass(slots=True)
class MaxBotChat(MaxChat):
    platform: str = "max"
    username: str | None = None
    kind: str = "group"  # group | channel
    url: str | None = None  # публичная ссылка https://max.ru/<name> (если есть)
    avatar_url: str | None = None
    description: str | None = None


@dataclass(slots=True)
class MaxBotMessage(MaxMessage):
    platform: str = "max"
    url: str | None = None  # Message.url — публичная ссылка на пост канала (у групп нет)
    views: int | None = None  # Message.stat.views (только каналы)


def _ts_to_dt(ts: int | float | None) -> datetime | None:
    if ts is None:
        return None
    ts = float(ts)
    if ts > 1e11:  # миллисекунды
        ts /= 1000.0
    return datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)


def _user_name(u: dict[str, Any] | None) -> str | None:
    if not u:
        return None
    parts = [u.get("first_name"), u.get("last_name")]
    name = " ".join(p for p in parts if p) or u.get("name")
    return name or u.get("username")


def _phone_from_vcf(vcf: str | None) -> str | None:
    if not vcf:
        return None
    m = _TEL_RE.search(vcf)
    return m.group(1).strip() if m else None


class MaxBotApiClient(MaxClient):
    name = "bot_api"
    platform = "max"

    def __init__(
        self,
        token: str,
        base_url: str = "https://platform-api.max.ru",
        *,
        timeout: float = 15.0,
        auth_mode: Literal["header", "query"] = "header",
        transport: httpx.AsyncBaseTransport | None = None,
        fallback_base_url: str | None = None,
        verify: Any = True,
        chat_ids: list[str] | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.fallback_base_url = (fallback_base_url or "").rstrip("/") or None
        self.auth_mode = auth_mode
        self._timeout = timeout
        self._transport = transport
        self._verify = verify
        self._http = self._make_http(self.base_url)
        # user_id -> chat_id, где видели автора (нужно для /chats/{id}/members)
        self._user_chat: dict[str, str] = {}
        # user_id -> телефон, которым пользователь сам поделился (вложение contact)
        self._shared_phones: dict[str, str] = {}
        # chat_id чатов/каналов: MAX_CHAT_IDS + найденные в /updates (bot_added, message_created)
        self._known_chats: set[str] = {str(c).strip() for c in (chat_ids or []) if str(c).strip()}
        self._updates_marker: Any = None
        self.last_error: str | None = None

    def _make_http(self, base: str) -> httpx.AsyncClient:
        headers = {"Authorization": self.token} if self.auth_mode == "header" else {}
        kw: dict[str, Any] = {"base_url": base, "headers": headers, "timeout": self._timeout,
                              "transport": self._transport}
        if self._transport is None:
            kw["verify"] = self._verify
        return httpx.AsyncClient(**kw)

    # ------------------------------------------------------------ http
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.auth_mode == "query":
            params["access_token"] = self.token
        try:
            r = await self._http.get(path, params=params)
        except httpx.ConnectError as e:
            # platform-api2 подписан сертификатом Минцифры (Russian Trusted Root CA), которого нет в
            # certifi. Без MAX_CA_BUNDLE переключаемся на platform-api (пока он работает).
            if self.fallback_base_url and "CERTIFICATE_VERIFY_FAILED" in repr(e) \
                    and self.base_url != self.fallback_base_url:
                log.warning("MAX %s: TLS не прошёл (нужен MAX_CA_BUNDLE с сертификатом Минцифры) — "
                            "переключаюсь на %s", self.base_url, self.fallback_base_url)
                await self._http.aclose()
                self.base_url = self.fallback_base_url
                self._http = self._make_http(self.base_url)
                return await self._get(path, params)
            raise MaxApiError(f"MAX {path}: {e!r}") from e
        except httpx.HTTPError as e:
            raise MaxApiError(f"MAX {path}: {e!r}") from e
        if r.status_code >= 400:
            raise MaxApiError(f"MAX {path}: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise MaxApiError(f"MAX {path}: не JSON") from e

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ chats
    @staticmethod
    def _to_chat(c: dict[str, Any]) -> MaxChat | None:
        ctype = c.get("type")
        if ctype not in (None, "chat", "channel"):  # личные диалоги не интересны
            return None
        if c.get("status") not in (None, "active"):
            return None
        link = c.get("link") or None
        username = None
        if isinstance(link, str) and "max.ru/" in link:
            tail = link.rstrip("/").rsplit("max.ru/", 1)[1]
            username = tail if "/" not in tail else None
        return MaxBotChat(
            external_id=str(c["chat_id"]),
            title=c.get("title") or f"Чат {c['chat_id']}",
            district=None,  # в MAX нет — редактор задаёт вручную
            members_count=c.get("participants_count"),
            kind="channel" if ctype == "channel" else "group",
            url=link if isinstance(link, str) and link.startswith("http") else None,
            username=username,
            avatar_url=((c.get("icon") or {}).get("url") if isinstance(c.get("icon"), dict) else None),
            description=c.get("description"),
        )

    async def poll_updates(self) -> int:
        """Long polling без ожидания (``timeout=0``): узнаём chat_id из событий bot_added /
        message_created (GET /chats с 06.2026 не поддерживается). Возвращает число новых chat_id."""
        before = len(self._known_chats)
        data = await self._get("/updates", {"marker": self._updates_marker, "timeout": 0, "limit": 100,
                                            "types": "bot_added,message_created,bot_removed"})
        for u in data.get("updates") or []:
            utype = u.get("update_type")
            cid = u.get("chat_id") or ((u.get("message") or {}).get("recipient") or {}).get("chat_id")
            if cid is None:
                continue
            if utype == "bot_removed":
                self._known_chats.discard(str(cid))
            else:
                self._known_chats.add(str(cid))
        self._updates_marker = data.get("marker", self._updates_marker)
        return len(self._known_chats) - before

    async def list_chats(self) -> list[MaxChat]:
        chats: dict[str, MaxChat] = {}
        legacy_error: MaxApiError | None = None
        marker: Any = None
        try:  # устаревший GET /chats (до 06.2026) — если ещё отвечает, используем
            for _ in range(50):  # защита от бесконечной пагинации
                data = await self._get("/chats", {"count": PAGE, "marker": marker})
                for c in data.get("chats") or []:
                    chat = self._to_chat(c)
                    if chat is not None:
                        chats[chat.external_id] = chat
                marker = data.get("marker")
                if not marker:
                    break
        except MaxApiError as e:
            legacy_error = e
        try:
            await self.poll_updates()
        except MaxApiError as e:
            log.debug("MAX /updates: %s", e)
        for cid in sorted(self._known_chats - set(chats)):
            try:
                chat = self._to_chat(await self._get(f"/chats/{cid}"))
            except MaxApiError as e:
                self.last_error = f"chat {cid}: {e}"
                log.warning("MAX GET /chats/%s: %s", cid, e)
                continue
            if chat is not None:
                chats[chat.external_id] = chat
        if not chats and legacy_error is not None and not self._known_chats:
            raise legacy_error
        return list(chats.values())

    # ------------------------------------------------------------ messages
    def _parse_message(self, m: dict[str, Any], chat_id: str) -> MaxMessage | None:
        body = m.get("body") or {}
        sender = m.get("sender") or {}
        link = m.get("link") or {}
        is_forwarded = link.get("type") == "forward"
        text = body.get("text") or ""
        attachments = list(body.get("attachments") or [])
        if is_forwarded:  # текст пересланного сообщения лежит в link.message
            fwd = link.get("message") or {}
            text = text or fwd.get("text") or ""
            attachments += list(fwd.get("attachments") or [])
        mid = body.get("mid")
        sent_at = _ts_to_dt(m.get("timestamp"))
        if mid is None or sent_at is None:
            return None
        author_id = str(sender.get("user_id", ""))

        media_url = None
        has_media = False
        for a in attachments:
            atype = a.get("type")
            payload = a.get("payload") or {}
            if atype in ("image", "video", "file") and not has_media:
                media_url = payload.get("url")  # у video url может не быть — тогда только has_media
                has_media = True
            elif atype == "contact":
                info = payload.get("max_info") or {}
                # Телефон берём, только если человек поделился СВОИМ контактом.
                if author_id and str(info.get("user_id", "")) == author_id:
                    phone = _phone_from_vcf(payload.get("vcf_info"))
                    if phone:
                        self._shared_phones[author_id] = phone
        if author_id:
            self._user_chat[author_id] = chat_id
        if not text and not has_media:
            return None
        stat = m.get("stat") or {}
        return MaxBotMessage(
            external_id=str(mid),
            chat_external_id=chat_id,
            author_external_id=author_id,
            author_name=_user_name(sender),
            text=text,
            sent_at=sent_at,
            has_media=has_media,
            media_url=media_url,
            is_forwarded=is_forwarded,
            url=m.get("url") or None,  # публичная ссылка на пост (только каналы)
            views=stat.get("views") if isinstance(stat, dict) else None,
        )

    async def fetch_messages(
        self, chat_id: str, since: datetime | None = None, limit: int = 100
    ) -> list[MaxMessage]:
        chat_id = str(chat_id)
        out: dict[str, MaxMessage] = {}
        to_ms: int | None = None
        while len(out) < limit:
            data = await self._get(
                "/messages", {"chat_id": chat_id, "count": min(PAGE, limit), "to": to_ms}
            )
            raw = data.get("messages") or []
            if not raw:
                break
            oldest: datetime | None = None
            added = 0
            for m in raw:
                msg = self._parse_message(m, chat_id)
                if msg is None:
                    continue
                oldest = msg.sent_at if oldest is None else min(oldest, msg.sent_at)
                if since is not None and msg.sent_at <= since:
                    continue
                if msg.external_id not in out:
                    out[msg.external_id] = msg
                    added += 1
            # Дошли до уже синхронизированного / страница неполная / нет прогресса — стоп.
            if oldest is None or len(raw) < min(PAGE, limit) or added == 0:
                break
            if since is not None and oldest <= since:
                break
            new_to = _dt_to_ms(oldest) - 1
            if to_ms is not None and new_to >= to_ms:
                break
            to_ms = new_to
        msgs = sorted(out.values(), key=lambda x: (x.sent_at, x.external_id))
        return msgs[-limit:] if len(msgs) > limit else msgs

    # ------------------------------------------------------------ profiles
    async def _find_member(self, chat_id: str, user_id: str) -> dict[str, Any] | None:
        data = await self._get(f"/chats/{chat_id}/members", {"user_ids": user_id})
        for mem in data.get("members") or []:
            if str(mem.get("user_id")) == user_id:
                return mem
        return None

    async def get_user_profile(self, user_id: str) -> MaxUserProfile | None:
        user_id = str(user_id)
        member: dict[str, Any] | None = None
        chat_ids: list[str] = []
        if user_id in self._user_chat:
            chat_ids.append(self._user_chat[user_id])
        try:
            if not chat_ids:  # автор ещё не встречался в этом процессе — ищем по чатам
                chat_ids = [c.external_id for c in await self.list_chats()]
            for cid in chat_ids:
                member = await self._find_member(cid, user_id)
                if member:
                    break
        except MaxApiError as e:
            log.warning("MAX profile %s: %s", user_id, e)
        phone = (member or {}).get("phone") or self._shared_phones.get(user_id)
        if member is None and phone is None:
            return None
        return MaxUserProfile(
            external_id=user_id,
            name=_user_name(member),
            phone=phone,
            username=(member or {}).get("username"),
        )
