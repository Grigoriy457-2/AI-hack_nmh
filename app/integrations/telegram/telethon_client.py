"""Telegram через MTProto (Telethon, пользовательская сессия) — зона DEV-TELEGRAM.

Основной провайдер для защиты. Читает публичные каналы и публичные группы по @username или
t.me-ссылке БЕЗ вступления (``get_entity`` + ``iter_messages``). Реальное время:
- ``events.NewMessage`` — Telegram присылает апдейты только по чатам, где аккаунт состоит
  (подписан/вступил); для них сообщения приходят мгновенно;
- для публичных источников без вступления апдейтов нет — параллельно идёт лёгкий опрос
  ``iter_messages(min_id=…)`` раз в ``poll_interval`` секунд. Дубли отсекаются по id.

Сессия создаётся ОДИН раз самим пользователем: ``python -m app.integrations.telegram.login``
(телефон и код вводятся в консоли). Без сессии клиент не пытается логиниться, а бросает
``TelegramConfigError`` с подсказкой. Файл ``*.session`` — секрет (полный доступ к аккаунту),
хранится вне git.

Лимиты: ``flood_sleep_threshold`` Telethon сам спит на коротких FloodWait; более длинные
``FloodWaitError`` ждём до ``flood_max_wait`` секунд, дольше — ошибка (не долбим API).
Телефон автора берём из ``User.phone`` — Telegram отдаёт его только если пользователь открыл
номер (или он у нас в контактах); иначе ``None``. Скрытые номера не ищем и не собираем.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    to_naive_utc,
)

log = logging.getLogger("newsflow.telegram")

LOGIN_HINT = (
    "Telegram-сессия не найдена или не авторизована. Один раз выполните в консоли: "
    "python -m app.integrations.telegram.login  (нужны TG_API_ID и TG_API_HASH с my.telegram.org)"
)


def _import_telethon() -> Any:
    try:
        import telethon
    except ImportError as e:  # pragma: no cover
        raise TelegramConfigError("Пакет telethon не установлен: pip install telethon") from e
    return telethon


def _display_name(entity: Any) -> str | None:
    if entity is None:
        return None
    title = getattr(entity, "title", None)
    if title:
        return title
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(p for p in parts if p)
    return name or getattr(entity, "username", None)


class TelethonClient(TelegramClient):
    name = "telethon"

    def __init__(self, *, api_id: int | None, api_hash: str | None, session_path: str,
                 sources: list[str] | None = None, poll_interval: int = 20, backfill: int = 30,
                 flood_max_wait: int = 120) -> None:
        super().__init__(sources, poll_interval=poll_interval, backfill=backfill)
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.flood_max_wait = flood_max_wait
        self._tg: Any = None  # telethon.TelegramClient
        self._connect_lock = asyncio.Lock()
        self._entities: dict[str, Any] = {}  # chat ext id -> entity
        self._chats: dict[str, TgChat] = {}
        self._senders: dict[str, Any] = {}  # author ext id -> User/Channel (кэш для профилей)

    # ------------------------------------------------------------ session
    @property
    def session_file(self) -> Path:
        p = Path(self.session_path)
        return p if p.suffix == ".session" else p.with_name(p.name + ".session")

    async def _client(self) -> Any:
        """Подключиться по существующей сессии. Никогда не запускает интерактивный логин."""
        if self._tg is not None and self._tg.is_connected():
            return self._tg
        async with self._connect_lock:
            if self._tg is not None and self._tg.is_connected():
                return self._tg
            if not (self.api_id and self.api_hash):
                raise TelegramConfigError("Не заданы TG_API_ID / TG_API_HASH (my.telegram.org)")
            if not self.session_file.exists():
                raise TelegramConfigError(f"{LOGIN_HINT}. Ожидался файл {self.session_file}")
            telethon = _import_telethon()
            tg = telethon.TelegramClient(
                str(self.session_file.with_suffix("")), int(self.api_id), self.api_hash,
                flood_sleep_threshold=60, request_retries=3, connection_retries=3,
            )
            await tg.connect()
            if not await tg.is_user_authorized():
                await tg.disconnect()
                raise TelegramConfigError(LOGIN_HINT)
            self._tg = tg
            return tg

    async def _call(self, coro_factory: Any) -> Any:
        """Выполнить запрос с обработкой FloodWait (ждём до flood_max_wait, один повтор)."""
        from telethon import errors

        for attempt in range(2):
            try:
                return await coro_factory()
            except errors.FloodWaitError as e:
                wait = int(getattr(e, "seconds", 0) or 0)
                if attempt or wait > self.flood_max_wait:
                    raise TelegramError(f"Telegram FloodWait {wait} c — пауза, попробуйте позже") from e
                log.warning("Telegram FloodWait %ss — ждём", wait)
                await asyncio.sleep(wait + 1)
        return None  # pragma: no cover

    async def aclose(self) -> None:
        if self._tg is not None:
            await self._tg.disconnect()
            self._tg = None

    # ------------------------------------------------------------ sources
    def _chat_from_entity(self, entity: Any, ref: str | None = None) -> TgChat:
        from telethon import utils
        from telethon.tl.types import Channel, Chat

        username = getattr(entity, "username", None)
        ext = chat_ext_id(username, utils.get_peer_id(entity))
        if isinstance(entity, Channel):
            kind = "group" if getattr(entity, "megagroup", False) else "channel"
        elif isinstance(entity, Chat):
            kind = "group"
        else:
            kind = "chat"
        return TgChat(
            external_id=ext, title=_display_name(entity) or ref or ext, district=None,
            members_count=getattr(entity, "participants_count", None), username=username.lower() if username else None,
            kind=kind, url=f"https://t.me/{username}" if username else None,
        )

    async def add_source(self, ref: str) -> TgChat:
        from telethon import errors
        from telethon.tl.types import User

        raw = strip_prefix(ref.strip())
        # числовой peer id (чат без username, уже известный сессии) или публичный @username
        username: Any = int(raw) if raw.lstrip("-").isdigit() else parse_tg_ref(raw)
        tg = await self._client()
        try:
            entity = await self._call(lambda: tg.get_entity(username))
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError) as e:
            raise TelegramSourceError(f"@{username} не найден") from e
        except errors.RPCError as e:
            raise TelegramSourceError(f"@{username}: {e}") from e
        if isinstance(entity, User):
            raise TelegramSourceError(f"@{username} — это пользователь, а не канал или группа")
        chat = self._chat_from_entity(entity, ref)
        # Проверка доступа без вступления: читаем одно сообщение.
        try:
            await self._call(lambda: tg.get_messages(entity, limit=1))
        except errors.ChannelPrivateError as e:
            raise TelegramSourceError(f"@{username}: приватный, нужен инвайт (не подключаем)") from e
        except errors.RPCError as e:
            raise TelegramSourceError(f"@{username}: история недоступна без вступления ({e})") from e
        self._entities[chat.external_id] = entity
        self._chats[chat.external_id] = chat
        self._sources[chat.external_id] = ref
        return chat

    async def remove_source(self, chat_id: str) -> None:
        await super().remove_source(chat_id)
        self._chats.pop(str(chat_id), None)
        self._entities.pop(str(chat_id), None)

    async def list_chats(self) -> list[TgChat]:
        if self._pending:
            await self._client()  # понятная ошибка «запустите login», если сессии нет
            await self._resolve_pending()
        return list(self._chats.values())

    # ------------------------------------------------------------ messages
    def _author(self, msg: Any, chat: TgChat) -> tuple[str, str | None]:
        from telethon import utils

        sender = getattr(msg, "sender", None)
        if sender is not None:
            ext = f"tg:{utils.get_peer_id(sender)}"
            self._senders[ext] = sender
            name = _display_name(sender)
            if getattr(msg, "post_author", None):  # подпись автора в канале
                name = f"{msg.post_author} ({name})"
            return ext, name
        return chat.external_id, chat.title

    def _to_message(self, msg: Any, chat: TgChat) -> TgMessage | None:
        text = msg.message or ""
        has_media = msg.media is not None and not hasattr(msg.media, "webpage")
        if not text and not has_media:
            return None  # сервисные сообщения
        author_id, author_name = self._author(msg, chat)
        url = f"https://t.me/{chat.username}/{msg.id}" if chat.username else None
        return TgMessage(
            external_id=str(msg.id),
            chat_external_id=chat.external_id,
            author_external_id=author_id,
            author_name=author_name,
            text=text,
            sent_at=to_naive_utc(msg.date),  # type: ignore[arg-type]
            has_media=has_media,
            # Медиа не скачиваем: ссылка на пост (превью откроется в Telegram).
            media_url=(url or f"tg://media/{chat.external_id}/{msg.id}") if has_media else None,
            is_forwarded=msg.fwd_from is not None,
            url=url,
            views=getattr(msg, "views", None),
        )

    async def _entity_for(self, chat_id: str) -> tuple[Any, TgChat]:
        if chat_id not in self._entities:
            await self.add_source(strip_prefix(chat_id))
        return self._entities[chat_id], self._chats[chat_id]

    async def fetch_messages(self, chat_id: str, since: datetime | None = None,
                             limit: int = 100) -> list[TgMessage]:
        if not is_tg_id(chat_id):
            return []
        tg = await self._client()
        entity, chat = await self._entity_for(chat_id)

        async def _collect() -> list[Any]:
            if since is None:  # первый показ — последние backfill сообщений
                return [m async for m in tg.iter_messages(entity, limit=min(limit, self.backfill))]
            # строго новее since, от старых к новым
            off = since.replace(tzinfo=UTC)
            return [m async for m in tg.iter_messages(entity, limit=limit, offset_date=off, reverse=True)]

        raw = await self._call(_collect)
        out = [m for m in (self._to_message(r, chat) for r in raw) if m is not None]
        if since is not None:
            out = [m for m in out if m.sent_at > since]
        out.sort(key=lambda m: (m.sent_at, int(m.external_id)))
        return out

    # ------------------------------------------------------------ profiles
    async def get_user_profile(self, user_id: str) -> TgUserProfile | None:
        user_id = str(user_id)
        if not is_tg_id(user_id):
            return None
        entity = self._senders.get(user_id) or self._entities.get(user_id)
        if entity is None:
            raw = strip_prefix(user_id)
            try:
                tg = await self._client()
                entity = await self._call(lambda: tg.get_entity(int(raw) if raw.lstrip("-").isdigit() else raw))
            except Exception as e:  # noqa: BLE001
                log.info("Telegram profile %s unavailable: %r", user_id, e)
                return None
        phone = getattr(entity, "phone", None)  # только если Telegram отдал (номер открыт)
        if phone and not phone.startswith("+"):
            phone = "+" + phone
        username = getattr(entity, "username", None)
        return TgUserProfile(external_id=user_id, name=_display_name(entity), phone=phone or None,
                             username=username)

    # ------------------------------------------------------------ realtime
    async def listen(self, on_message: OnMessage) -> None:
        """events.NewMessage для чатов, где аккаунт состоит + опрос публичных источников
        без вступления (min_id) раз в poll_interval секунд. Дубли — по (chat, id)."""
        from telethon import events, utils

        tg = await self._client()
        await self.list_chats()
        seen: set[tuple[str, str]] = set()
        last_id: dict[str, int] = {}

        async def emit(m: TgMessage) -> None:
            key = (m.chat_external_id, m.external_id)
            if key in seen:
                return
            seen.add(key)
            last_id[m.chat_external_id] = max(last_id.get(m.chat_external_id, 0), int(m.external_id))
            await _safe_call(on_message, m)

        async def handler(event: Any) -> None:
            peer = utils.get_peer_id(event.message.peer_id)
            chat = next((c for cid, c in self._chats.items()
                         if utils.get_peer_id(self._entities[cid]) == peer), None)
            if chat is None:
                return
            m = self._to_message(event.message, chat)
            if m is not None:
                await emit(m)

        tg.add_event_handler(handler, events.NewMessage(incoming=True))
        try:
            # стартовая точка: последний id каждого источника (историю не «проигрываем»)
            for cid, ent in list(self._entities.items()):
                msgs = await self._call(lambda ent=ent: tg.get_messages(ent, limit=1))
                last_id[cid] = msgs[0].id if msgs else 0
            while True:
                await asyncio.sleep(self.poll_interval)
                for cid, ent in list(self._entities.items()):
                    chat = self._chats.get(cid)
                    if chat is None:
                        continue
                    try:
                        raw = await self._call(lambda ent=ent, cid=cid: _min_id_batch(
                            tg, ent, last_id.get(cid, 0), self.backfill))
                    except TelegramError as e:
                        self.last_error = str(e)
                        log.warning("Telegram poll %s: %s", cid, e)
                        continue
                    for r in sorted(raw, key=lambda x: x.id):
                        m = self._to_message(r, chat)
                        if m is not None:
                            await emit(m)
                        last_id[cid] = max(last_id.get(cid, 0), r.id)
        finally:
            tg.remove_event_handler(handler)

    async def status(self) -> dict[str, Any]:
        st = await super().status()
        st["session_file"] = str(self.session_file)
        st["session_exists"] = self.session_file.exists()
        st["connected"] = bool(self._tg is not None and self._tg.is_connected())
        return st


async def _min_id_batch(tg: Any, entity: Any, min_id: int, limit: int) -> list[Any]:
    return [m async for m in tg.iter_messages(entity, min_id=min_id, limit=limit)]
