"""Реальный клиент MAX Bot API (зона DEV-BACKEND). httpx async.

Документация: https://dev.max.ru/docs-api (база ``https://platform-api.max.ru``; API унаследовано
от TamTam Bot API). Для демо реального токена нет — клиент изолирован, ни один модуль не
импортирует его без ``MAX_BOT_TOKEN`` (см. ``client.get_max_client``).

Что используем:
- ``GET /chats?count=&marker=`` → ``{"chats": [Chat], "marker": int|null}`` — чаты, где состоит бот.
- ``GET /messages?chat_id=&count=&to=`` → ``{"messages": [Message]}`` — сообщения чата.
  ``Message = {sender: User, recipient: {chat_id}, timestamp: ms, link: {type: forward|reply, ...},
  body: {mid, text, attachments: [{type: image|video|file|contact|..., payload: {...}}]}}``.
- ``GET /chats/{chatId}/members?user_ids=...`` → ``{"members": [ChatMember]}`` — профиль участника.

Авторизация: токен в заголовке ``Authorization: <token>`` (текущая рекомендация MAX; старый
способ — query-параметр ``access_token``, включается ``auth_mode="query"``).

Телефон: Bot API НЕ отдаёт телефон участника. Он известен боту только если пользователь сам
поделился контактом (вложение ``type=contact`` с собственным ``max_info.user_id``) — такие
телефоны мы запоминаем при чтении сообщений; либо если в ``ChatMember`` появится поле phone
(на будущее, берём, если есть). Иначе ``phone=None`` → в UI «телефон скрыт в профиле».

TODO(MAX): сверить с актуальной документацией на реальном токене:
  * единицы ``from``/``to`` в /messages (считаем миллисекунды, как ``timestamp``) и порядок
    выдачи (сортируем сами, фильтр ``since`` — на нашей стороне);
  * формат ``user_ids`` (передаём через запятую);
  * поля вложений image/video (берём ``payload.url``).
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from app.integrations.max.client import MaxChat, MaxClient, MaxMessage, MaxUserProfile

log = logging.getLogger("newsflow.max")

PAGE = 100  # максимум count в MAX Bot API
_TEL_RE = re.compile(r"^TEL[^:]*:(.+)$", re.MULTILINE | re.IGNORECASE)


class MaxApiError(RuntimeError):
    pass


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

    def __init__(
        self,
        token: str,
        base_url: str = "https://platform-api.max.ru",
        *,
        timeout: float = 15.0,
        auth_mode: Literal["header", "query"] = "header",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.auth_mode = auth_mode
        headers = {"Authorization": token} if auth_mode == "header" else {}
        self._http = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )
        # user_id -> chat_id, где видели автора (нужно для /chats/{id}/members)
        self._user_chat: dict[str, str] = {}
        # user_id -> телефон, которым пользователь сам поделился (вложение contact)
        self._shared_phones: dict[str, str] = {}

    # ------------------------------------------------------------ http
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.auth_mode == "query":
            params["access_token"] = self.token
        try:
            r = await self._http.get(path, params=params)
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
    async def list_chats(self) -> list[MaxChat]:
        chats: list[MaxChat] = []
        marker: Any = None
        for _ in range(50):  # защита от бесконечной пагинации
            data = await self._get("/chats", {"count": PAGE, "marker": marker})
            for c in data.get("chats") or []:
                if c.get("type") not in (None, "chat"):  # диалоги и каналы не интересны
                    continue
                if c.get("status") not in (None, "active"):
                    continue
                chats.append(
                    MaxChat(
                        external_id=str(c["chat_id"]),
                        title=c.get("title") or f"Чат {c['chat_id']}",
                        district=None,  # в MAX нет — оператор правит вручную (TODO)
                        members_count=c.get("participants_count"),
                    )
                )
            marker = data.get("marker")
            if not marker:
                break
        return chats

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
        for a in attachments:
            atype = a.get("type")
            payload = a.get("payload") or {}
            if atype in ("image", "video", "file") and media_url is None:
                media_url = payload.get("url")  # TODO(MAX): для video url может отсутствовать
                media_url = media_url or f"max://attachment/{atype}/{payload.get('token') or mid}"
            elif atype == "contact":
                info = payload.get("max_info") or {}
                # Телефон берём, только если человек поделился СВОИМ контактом.
                if author_id and str(info.get("user_id", "")) == author_id:
                    phone = _phone_from_vcf(payload.get("vcf_info"))
                    if phone:
                        self._shared_phones[author_id] = phone
        if author_id:
            self._user_chat[author_id] = chat_id
        if not text and media_url is None:
            return None
        return MaxMessage(
            external_id=str(mid),
            chat_external_id=chat_id,
            author_external_id=author_id,
            author_name=_user_name(sender),
            text=text,
            sent_at=sent_at,
            has_media=media_url is not None,
            media_url=media_url,
            is_forwarded=is_forwarded,
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
