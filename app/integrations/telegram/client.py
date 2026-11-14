"""Telegram-источник — контракт клиента (зона DEV-TELEGRAM).

Интерфейс совместим с ``app.integrations.max.client.MaxClient``: ``list_chats()``,
``fetch_messages(chat_id, since, limit)``, ``get_user_profile(user_id)``; возвращаются те же
dataclass-ы (``TgChat``/``TgMessage`` — наследники ``MaxChat``/``MaxMessage`` c доп. полями),
поэтому ``app.integrations.max.poller.sync_once(session, client)`` работает без изменений логики.

Реализации:
- ``telethon_client.TelethonClient`` — MTProto, пользовательская сессия: публичные каналы и
  публичные группы по @username / t.me-ссылке без вступления; реальное время.
- ``bot_api.TelegramBotApiClient`` — Bot API (getUpdates long polling): группы/каналы, куда
  бот добавлен администратором (в группах — с отключённым privacy mode).
- ``web_preview.TelegramWebPreviewClient`` — без авторизации, парсинг https://t.me/s/<channel>.
- ``mock.MockTelegramClient`` — офлайн (2 канала + 1 чат, пересечения со сценарием «Чертаново»).
Получать клиента только через ``get_telegram_client()`` (TG_PROVIDER).

Идентификаторы — строки с префиксом ``tg:``, чтобы не пересекаться с MAX в общих таблицах
(``max_chats.external_id`` unique, ``author_profiles.external_id`` PK):
- чат: ``tg:<username>`` (lowercase; если username нет — ``tg:<peer_id>``, напр. ``tg:-1001234``);
- сообщение: ``<message_id>`` (уникален в пределах чата, как в MAX);
- автор: ``tg:<user_or_channel_id>``; для анонимных постов канала (web) — ``tg:<username>``.
Время — naive UTC.

Ограничения и этика: только публичные источники или чаты, где бот добавлен администратором с
согласия участников. Лимиты Telegram соблюдаются (FloodWait/429 → ждём ``retry_after``, не
долбим). Телефон автора берём только если Telegram его отдаёт (человек сам открыл номер или
поделился контактом), иначе ``None``. ToS Telegram ограничивает использование контента для AI —
демо на защите является пилотом, использование в продакшене — на согласование юристам NMH.
"""
from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.integrations.max.client import MaxChat, MaxClient, MaxMessage, MaxUserProfile

log = logging.getLogger("newsflow.telegram")

PLATFORM = "telegram"
ID_PREFIX = "tg:"


class TelegramError(RuntimeError):
    """Базовая ошибка Telegram-источника (сообщение — человекочитаемое, для UI)."""


class TelegramConfigError(TelegramError):
    """Нет ключей/сессии/токена для выбранного провайдера."""


class TelegramSourceError(TelegramError):
    """Источник нельзя подключить (приватный, не найден, превью выключено…)."""


# ---------------------------------------------------------------- dataclasses
@dataclass(slots=True)
class TgChat(MaxChat):
    platform: str = PLATFORM
    username: str | None = None  # без @
    kind: str = "channel"  # channel | group | chat
    url: str | None = None  # https://t.me/<username>


@dataclass(slots=True)
class TgMessage(MaxMessage):
    platform: str = PLATFORM
    url: str | None = None  # https://t.me/<username>/<id> — ссылка «открыть в Telegram»
    views: int | None = None


@dataclass(slots=True)
class TgUserProfile(MaxUserProfile):
    platform: str = PLATFORM


OnMessage = Callable[[TgMessage], Awaitable[None]]


# ---------------------------------------------------------------- helpers
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_TME_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?([^/?#]+)(?:/(\d+))?", re.I
)


def parse_tg_ref(ref: str) -> str:
    """``@name`` / ``name`` / ``t.me/name`` / ``https://t.me/s/name`` / ``t.me/name/123`` /
    ``tg:name`` → ``name`` (lowercase). Приватные инвайты (``t.me/+…``, ``joinchat``) — ошибка."""
    raw = (ref or "").strip()
    if not raw:
        raise TelegramSourceError("Пустая ссылка на источник")
    if raw.lower().startswith(ID_PREFIX):
        raw = raw[len(ID_PREFIX):]
    m = _TME_RE.match(raw)
    name = m.group(1) if m else raw
    name = name.lstrip("@")
    if name.startswith("+") or name.lower() == "joinchat":
        raise TelegramSourceError(
            "Это приватная инвайт-ссылка. Подключаются только публичные каналы/группы "
            "(@username) или чаты, куда бот добавлен администратором."
        )
    if not _USERNAME_RE.match(name):
        raise TelegramSourceError(f"Не похоже на публичный @username Telegram: {ref!r}")
    return name.lower()


def chat_ext_id(username: str | None, peer_id: int | str | None = None) -> str:
    if username:
        return f"{ID_PREFIX}{username.lower()}"
    return f"{ID_PREFIX}{peer_id}"


def strip_prefix(external_id: str) -> str:
    s = str(external_id)
    return s[len(ID_PREFIX):] if s.startswith(ID_PREFIX) else s


def is_tg_id(external_id: str | None) -> bool:
    return bool(external_id) and str(external_id).startswith(ID_PREFIX)


def to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------- contract
class TelegramClient(MaxClient, ABC):
    """Общий контракт Telegram-клиентов (MaxClient + управление источниками + реальное время)."""

    name: str = "abstract"
    platform: str = PLATFORM

    def __init__(self, sources: list[str] | None = None, *, poll_interval: int = 20,
                 backfill: int = 30) -> None:
        self._sources: dict[str, str] = {}  # external chat id -> исходная ссылка
        self._pending: list[str] = list(sources or [])  # ещё не разрешённые ссылки
        self.poll_interval = max(int(poll_interval), 3)
        self.backfill = max(int(backfill), 1)
        self.last_error: str | None = None

    # -- источники ----------------------------------------------------------
    async def add_source(self, ref: str) -> TgChat:
        """Подключить публичный канал/группу по @username или t.me-ссылке. Возвращает чат
        (с проверкой, что источник реально доступен). Ошибки — ``TelegramSourceError``."""
        raise TelegramSourceError(f"Провайдер {self.name} не поддерживает подключение по ссылке")

    async def remove_source(self, chat_id: str) -> None:
        self._sources.pop(str(chat_id), None)

    async def _resolve_pending(self) -> None:
        """Разрешить ссылки из TG_SOURCES (лениво, при первом list_chats). Ошибки — в лог."""
        pending, self._pending = self._pending, []
        for ref in pending:
            try:
                await self.add_source(ref)
            except TelegramError as e:
                log.warning("Telegram source %r skipped: %s", ref, e)
                self.last_error = f"{ref}: {e}"

    # -- MaxClient -----------------------------------------------------------
    @abstractmethod
    async def list_chats(self) -> list[TgChat]:  # type: ignore[override]
        """Подключённые источники (каналы/группы/чаты)."""

    @abstractmethod
    async def fetch_messages(  # type: ignore[override]
        self, chat_id: str, since: datetime | None = None, limit: int = 100
    ) -> list[TgMessage]:
        """Сообщения чата строго новее ``since``, по возрастанию sent_at. Чужие (не ``tg:``)
        chat_id → ``[]`` (поллер может пройтись по всем чатам БД)."""

    @abstractmethod
    async def get_user_profile(self, user_id: str) -> TgUserProfile | None:  # type: ignore[override]
        """Профиль автора: имя, username, телефон — только если Telegram его отдаёт, иначе None."""

    async def aclose(self) -> None:
        """Закрыть соединения."""

    # -- реальное время ----------------------------------------------------
    async def listen(self, on_message: OnMessage) -> None:
        """Бесконечно доставлять НОВЫЕ сообщения подключённых источников в ``on_message``
        (до отмены задачи). Базовая реализация — опрос раз в ``poll_interval`` секунд;
        Telethon/Bot API переопределяют push-механизмом. Исключения колбэка логируются."""
        last: dict[str, datetime] = {}
        seen: dict[str, set[str]] = {}
        first = True
        while True:
            try:
                chats = await self.list_chats()
                for c in chats:
                    since = last.get(c.external_id)
                    msgs = await self.fetch_messages(c.external_id, since=since, limit=self.backfill)
                    ids = seen.setdefault(c.external_id, set())
                    for m in msgs:
                        if m.external_id in ids:
                            continue
                        ids.add(m.external_id)
                        if c.external_id in last or not first:
                            await _safe_call(on_message, m)
                        last[c.external_id] = max(last.get(c.external_id, m.sent_at), m.sent_at)
                    last.setdefault(c.external_id, utcnow())
                first = False
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.warning("Telegram %s listen poll failed: %r", self.name, e)
            await asyncio.sleep(self.poll_interval)

    async def status(self) -> dict[str, Any]:
        """Для страницы «Источники»: провайдер, подключённые источники, последняя ошибка."""
        return {
            "platform": self.platform,
            "provider": self.name,
            "sources": sorted(self._sources),
            "pending": list(self._pending),
            "last_error": self.last_error,
        }


async def _safe_call(cb: OnMessage, m: TgMessage) -> None:
    try:
        await cb(m)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Telegram on_message callback failed")


# ---------------------------------------------------------------- factory
_client: TelegramClient | None = None


def _telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


def choose_provider(provider: str | None = None) -> str:
    """Разрешить TG_PROVIDER=auto: telethon (ключи + сессия + пакет) → bot (токен) →
    web (заданы TG_SOURCES) → mock."""
    from app.integrations.telegram.settings import get_tg_settings

    s = get_tg_settings()
    provider = provider or s.TG_PROVIDER
    if provider != "auto":
        return provider
    if s.has_telethon_keys and s.session_file.exists() and _telethon_available():
        return "telethon"
    if s.TG_BOT_TOKEN:
        return "bot"
    if s.sources:
        return "web"
    return "mock"


def build_telegram_client(provider: str | None = None) -> TelegramClient:
    from app.integrations.telegram.settings import get_tg_settings

    s = get_tg_settings()
    provider = choose_provider(provider)
    common = {"sources": s.sources, "poll_interval": s.TG_POLL_INTERVAL, "backfill": s.TG_BACKFILL}
    if provider == "telethon":
        from app.integrations.telegram.telethon_client import TelethonClient

        return TelethonClient(
            api_id=s.TG_API_ID, api_hash=s.TG_API_HASH, session_path=s.TG_SESSION_PATH,
            flood_max_wait=s.TG_FLOOD_MAX_WAIT, **common,
        )
    if provider == "bot":
        from app.integrations.telegram.bot_api import TelegramBotApiClient

        if not s.TG_BOT_TOKEN:
            raise TelegramConfigError("TG_PROVIDER=bot, но TG_BOT_TOKEN не задан")
        return TelegramBotApiClient(s.TG_BOT_TOKEN, s.TG_BOT_API_BASE, **common)
    if provider == "web":
        from app.integrations.telegram.web_preview import TelegramWebPreviewClient

        return TelegramWebPreviewClient(base_url=s.TG_WEB_BASE, **common)
    from app.integrations.telegram.mock import MockTelegramClient

    return MockTelegramClient()


def get_telegram_client() -> TelegramClient:
    """Синглтон по TG_PROVIDER = telethon | bot | web | mock (по умолчанию) | auto."""
    global _client
    if _client is None:
        _client = build_telegram_client()
    return _client


def set_telegram_client(client: TelegramClient | None) -> None:
    """Подмена в тестах / переключение провайдера на лету."""
    global _client
    _client = client
