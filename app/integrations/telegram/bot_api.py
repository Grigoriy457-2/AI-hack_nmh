"""Telegram Bot API (httpx, getUpdates long polling) — зона DEV-TELEGRAM.

Для групп/каналов, куда бот добавлен АДМИНИСТРАТОРОМ (с согласия участников):
- в группах у бота должен быть отключён privacy mode (@BotFather → /setprivacy → Disable),
  иначе бот видит только команды и упоминания; после смены режима бота нужно перевыдать в группу;
- в каналах бот должен быть администратором — тогда приходят апдейты ``channel_post``.
Ограничения Bot API: истории нет (видны только сообщения ПОСЛЕ добавления бота), список чатов
бот получить не может — чаты узнаём из апдейтов и из TG_SOURCES (@username/числовой id → getChat).
Один токен = один потребитель getUpdates (иначе HTTP 409); вебхук у бота должен быть выключен.

Телефон: Bot API не отдаёт телефоны участников. Номер известен, только если человек сам
поделился СВОИМ контактом (``message.contact.user_id == from.id``) — иначе ``None``.
Лимиты: HTTP 429 → ``parameters.retry_after`` (ждём, не долбим).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

import httpx

from app.integrations.telegram.client import (
    OnMessage,
    TelegramClient,
    TelegramConfigError,
    TelegramError,
    TelegramSourceError,
    TgChat,
    TgMessage,
    TgUserProfile,
    _safe_call,
    chat_ext_id,
    is_tg_id,
    parse_tg_ref,
    strip_prefix,
)

log = logging.getLogger("newsflow.telegram")

BUFFER_PER_CHAT = 1000
LONG_POLL = 25  # секунд, timeout getUpdates в listen()
_MEDIA_KEYS = ("photo", "video", "animation", "document", "audio", "voice", "video_note", "sticker")


def _name(u: dict[str, Any] | None) -> str | None:
    if not u:
        return None
    if u.get("title"):
        return u["title"]
    name = " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p)
    return name or u.get("username")


class TelegramBotApiClient(TelegramClient):
    name = "bot"

    def __init__(self, token: str, base_url: str = "https://api.telegram.org", *,
                 sources: list[str] | None = None, poll_interval: int = 20, backfill: int = 30,
                 timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not token:
            raise TelegramConfigError("TG_BOT_TOKEN не задан")
        super().__init__(sources, poll_interval=poll_interval, backfill=backfill)
        self._base = f"{base_url.rstrip('/')}/bot{token}/"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=LONG_POLL + timeout),
                                       transport=transport)
        self._offset: int | None = None
        self._pump_lock = asyncio.Lock()
        self._listening = False
        self._me: dict[str, Any] | None = None
        self._chats: dict[str, TgChat] = {}
        self._id2ext: dict[int, str] = {}
        self._buf: dict[str, deque[TgMessage]] = {}
        self._users: dict[str, dict[str, Any]] = {}
        self._shared_phones: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ http
    async def _api(self, method: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(2):
            try:
                r = await self._http.post(self._base + method, json=params or {})
            except httpx.HTTPError as e:
                raise TelegramError(f"Bot API {method}: {e!r}") from e
            try:
                data = r.json()
            except ValueError as e:
                raise TelegramError(f"Bot API {method}: HTTP {r.status_code}, не JSON") from e
            if data.get("ok"):
                return data.get("result")
            code, desc = data.get("error_code", r.status_code), data.get("description", "")
            if code == 429 and attempt == 0:
                wait = int((data.get("parameters") or {}).get("retry_after", 5))
                if wait <= 60:
                    log.warning("Bot API 429 (%s), ждём %ss", method, wait)
                    await asyncio.sleep(wait + 1)
                    continue
            if code == 401:
                raise TelegramConfigError("Неверный TG_BOT_TOKEN (401 Unauthorized)")
            if code == 409:
                raise TelegramError("Bot API 409: у бота включён вебхук или getUpdates читает другой "
                                    "процесс (один токен — один поллер; deleteWebhook)")
            raise TelegramError(f"Bot API {method}: {code} {desc}")
        raise TelegramError(f"Bot API {method}: 429, лимит запросов")  # pragma: no cover

    # ------------------------------------------------------------ chats
    def _register_chat(self, c: dict[str, Any]) -> TgChat:
        ext = self._id2ext.get(c["id"]) or chat_ext_id(c.get("username"), c["id"])
        kind = {"channel": "channel", "group": "group", "supergroup": "group"}.get(c.get("type"), "chat")
        username = (c.get("username") or "").lower() or None
        chat = self._chats.get(ext)
        if chat is None:
            chat = TgChat(external_id=ext, title=_name(c) or ext, kind=kind, username=username,
                          url=f"https://t.me/{username}" if username else None)
            self._chats[ext] = chat
            self._sources.setdefault(ext, str(c["id"]))
        else:
            chat.title = _name(c) or chat.title
        self._id2ext[c["id"]] = ext
        return chat

    async def add_source(self, ref: str) -> TgChat:
        raw = strip_prefix(ref.strip())
        chat_ref: Any = int(raw) if raw.lstrip("-").isdigit() else "@" + parse_tg_ref(raw)
        try:
            c = await self._api("getChat", {"chat_id": chat_ref})
        except TelegramConfigError:
            raise
        except TelegramError as e:
            raise TelegramSourceError(f"{ref}: бот не видит чат ({e})") from e
        if self._me is None:
            self._me = await self._api("getMe")
        try:
            member = await self._api("getChatMember", {"chat_id": c["id"], "user_id": self._me["id"]})
            status = member.get("status")
        except TelegramError:
            status = None
        if status not in ("administrator", "creator", "member"):
            raise TelegramSourceError(
                f"{ref}: бот @{self._me.get('username')} не добавлен в чат. Попросите админа добавить "
                "бота (в группах — с отключённым privacy mode, в каналах — администратором)."
            )
        try:
            cnt = await self._api("getChatMemberCount", {"chat_id": c["id"]})
        except TelegramError:
            cnt = None
        chat = self._register_chat(c)
        chat.members_count = cnt
        self._sources[chat.external_id] = ref
        return chat

    async def list_chats(self) -> list[TgChat]:
        if self._pending:
            await self._resolve_pending()
        if not self._listening:
            await self._pump(0)
        return list(self._chats.values())

    # ------------------------------------------------------------ updates
    def _parse(self, m: dict[str, Any]) -> TgMessage | None:
        chat = self._register_chat(m["chat"])
        sender = m.get("from") or m.get("sender_chat") or m["chat"]
        author_id = f"tg:{sender['id']}"
        self._users[author_id] = {**self._users.get(author_id, {}), **sender}
        contact = m.get("contact")
        if contact and m.get("from") and contact.get("user_id") == m["from"]["id"] and contact.get("phone_number"):
            phone = contact["phone_number"]
            self._shared_phones[author_id] = phone if phone.startswith("+") else "+" + phone
        text = m.get("text") or m.get("caption") or ""
        media_key = next((k for k in _MEDIA_KEYS if k in m), None)
        if not text and media_key is None:
            return None
        url = f"https://t.me/{chat.username}/{m['message_id']}" if chat.username else None
        return TgMessage(
            external_id=str(m["message_id"]),
            chat_external_id=chat.external_id,
            author_external_id=author_id,
            author_name=m.get("author_signature") or _name(sender),
            text=text,
            sent_at=datetime.fromtimestamp(m["date"], UTC).replace(tzinfo=None),
            has_media=media_key is not None,
            media_url=(url or f"tg://media/{chat.external_id}/{m['message_id']}") if media_key else None,
            is_forwarded=bool(m.get("forward_origin") or m.get("forward_date")),
            url=url,
        )

    async def _pump(self, timeout: int) -> list[TgMessage]:
        """Один вызов getUpdates; новые сообщения кладём в буфер и возвращаем."""
        async with self._pump_lock:
            params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "channel_post"]}
            if self._offset is not None:
                params["offset"] = self._offset
            updates = await self._api("getUpdates", params) or []
            out: list[TgMessage] = []
            for u in updates:
                self._offset = u["update_id"] + 1
                raw = u.get("message") or u.get("channel_post")
                if not raw:
                    continue
                msg = self._parse(raw)
                if msg is None:
                    continue
                self._buf.setdefault(msg.chat_external_id, deque(maxlen=BUFFER_PER_CHAT)).append(msg)
                out.append(msg)
            return out

    async def fetch_messages(self, chat_id: str, since: datetime | None = None,
                             limit: int = 100) -> list[TgMessage]:
        if not is_tg_id(chat_id):
            return []
        if not self._listening:
            await self._pump(0)
        msgs = list(self._buf.get(chat_id, ()))
        if since is not None:
            msgs = [m for m in msgs if m.sent_at > since]
        msgs.sort(key=lambda m: (m.sent_at, int(m.external_id)))
        return msgs[-limit:]

    async def get_user_profile(self, user_id: str) -> TgUserProfile | None:
        user_id = str(user_id)
        u = self._users.get(user_id)
        phone = self._shared_phones.get(user_id)
        if u is None and phone is None:
            return None
        return TgUserProfile(external_id=user_id, name=_name(u), phone=phone,
                             username=(u or {}).get("username"))

    async def listen(self, on_message: OnMessage) -> None:
        """Long polling getUpdates (timeout=25 с) — сообщения приходят почти мгновенно."""
        if self._pending:
            await self._resolve_pending()
        self._listening = True
        try:
            while True:
                try:
                    for m in await self._pump(LONG_POLL):
                        await _safe_call(on_message, m)
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except TelegramConfigError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
                    log.warning("Bot API getUpdates failed: %s", e)
                    await asyncio.sleep(self.poll_interval)
        finally:
            self._listening = False
