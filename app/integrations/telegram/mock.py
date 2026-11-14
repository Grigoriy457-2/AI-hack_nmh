"""Mock Telegram (зона DEV-TELEGRAM): 2 публичных канала + 1 районный чат, офлайн.

Сообщения пересекаются по смыслу со сценарием MAX «Чертаново, хлопок у жилого дома ~14:30»
(``app.ai.mock_data``), чтобы на Радаре были видны кросс-платформенные дубли:
- пост канала «Москва Live» дословно совпадает с пересылками m09/m10 из MAX-чатов
  (первоисточник — Telegram, MAX-сообщения — дубли);
- «Срочно | Юг Москвы» пересказывает очевидца m02 (горящий белый седан у второго подъезда);
- в чате «Чертаново онлайн» — копия m12 (парковку перекрыли) и уникальное свидетельство с
  открытым телефоном автора (для «Кому позвонить»).

Время = якорь сценария (``mock_data.scenario_event_time``) + ``minutes``; сообщения «из будущего»
не отдаются, пока не наступит их время (живой поток на демо).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.integrations.telegram.client import (
    TelegramClient,
    TgChat,
    TgMessage,
    TgUserProfile,
    is_tg_id,
    utcnow,
)

MOCK_TG_CHATS: list[dict[str, Any]] = [
    {"external_id": "tg:moscow_live_demo", "username": "moscow_live_demo", "title": "Москва Live (демо)",
     "kind": "channel", "district": None, "members_count": 412000},
    {"external_id": "tg:srochno_yug_demo", "username": "srochno_yug_demo", "title": "Срочно | Юг Москвы (демо)",
     "kind": "channel", "district": "ЮАО", "members_count": 58300},
    {"external_id": "tg:chertanovo_online_demo", "username": "chertanovo_online_demo",
     "title": "Чертаново онлайн — чат района (демо)", "kind": "group",
     "district": "Чертаново Центральное", "members_count": 3120},
]

# Номера вымышленные (диапазон 555). phone=None — номер скрыт настройками приватности.
MOCK_TG_PROFILES: dict[str, dict[str, Any]] = {
    "tg:moscow_live_demo": {"name": "Москва Live (демо)", "username": "moscow_live_demo", "phone": None},
    "tg:srochno_yug_demo": {"name": "Срочно | Юг Москвы (демо)", "username": "srochno_yug_demo", "phone": None},
    "tg:700101": {"name": "Максим Рябов", "username": "max_ryabov", "phone": "+7 926 555-31-08"},
    "tg:700102": {"name": "Ксения", "username": "ksu_chert", "phone": None},
    "tg:700103": {"name": "Олег Н.", "username": None, "phone": None},
}

# Дословно как _TG_LIVE_TEXT в app.ai.mock_data (m09/m10 — его пересылки в MAX).
_LIVE_TEXT = (
    "⚡️В Чертанове прогремел взрыв у жилого дома. Жители сообщают о громком хлопке около 14:30, "
    "во дворе дым. Предварительно — взрыв газа."
)

MOCK_TG_MESSAGES: list[dict[str, Any]] = [
    # «Москва Live»: ранний непроверенный пост (версия «взрыв газа»), потом уточнение
    {"external_id": "8812", "chat": "tg:moscow_live_demo", "author": "tg:moscow_live_demo", "minutes": 9,
     "text": _LIVE_TEXT, "views": 48200},
    {"external_id": "8815", "chat": "tg:moscow_live_demo", "author": "tg:moscow_live_demo", "minutes": 47,
     "text": "Уточнение по Чертанову: в жилом доме взрыва не было — во дворе на Чертановской сгорел "
             "автомобиль. Мосгаз утечки не нашёл, пострадавших нет.", "views": 31900},
    # «Срочно | Юг Москвы»: пересказ очевидца m02 с фото
    {"external_id": "2291", "chat": "tg:srochno_yug_demo", "author": "tg:srochno_yug_demo", "minutes": 7,
     "text": "Чертаново: во дворе дома на Чертановской у второго подъезда горит белый седан. "
             "Очевидцы слышали хлопок, потом огонь из-под капота. Видео от подписчика 👇",
     "media_url": "https://t.me/mock/media/srochno_2291.mp4", "views": 12700},
    {"external_id": "2297", "chat": "tg:srochno_yug_demo", "author": "tg:srochno_yug_demo", "minutes": 120,
     "text": "В Бутове открыли новую детскую площадку у пруда", "views": 3100},
    # Районный чат: копия из MAX (m12), уникальное свидетельство, мусор
    {"external_id": "51034", "chat": "tg:chertanovo_online_demo", "author": "tg:700102", "minutes": 19,
     "text": "Полиция перекрыла парковку лентой, машины не выпускают. Свою не могу забрать", "is_forwarded": True},
    {"external_id": "51040", "chat": "tg:chertanovo_online_demo", "author": "tg:700101", "minutes": 21,
     "text": "Я из соседнего дома, 48к1. Около 14:20 видел двоих парней у белого Соляриса на парковке, "
             "один заглядывал под машину. Через 10 минут хлопок. Камера на подъезде всё пишет",
     "media_url": "https://t.me/mock/media/chert_51040.jpg"},
    {"external_id": "51047", "chat": "tg:chertanovo_online_demo", "author": "tg:700103", "minutes": 33,
     "text": "Продам детскую коляску, почти новая, самовывоз Чертаново"},
]


class MockTelegramClient(TelegramClient):
    """Детерминированный офлайн-клиент Telegram для демо и тестов."""

    name = "mock"

    def __init__(self, now: datetime | None = None, *, include_future: bool = False,
                 poll_interval: int = 10) -> None:
        super().__init__(poll_interval=poll_interval)
        self._now = now
        self._include_future = include_future
        for c in MOCK_TG_CHATS:
            self._sources[c["external_id"]] = c["username"]

    def anchor(self) -> datetime:
        from app.ai import mock_data

        return mock_data.scenario_event_time(self._now) if self._now else mock_data.scenario_event_time()

    def all_messages(self) -> list[TgMessage]:
        now, anchor = self._now or utcnow(), self.anchor()
        out: list[TgMessage] = []
        for m in MOCK_TG_MESSAGES:
            if m["chat"] not in self._sources:
                continue
            username = m["chat"][3:]
            author = MOCK_TG_PROFILES.get(m["author"], {})
            media = m.get("media_url")
            out.append(TgMessage(
                external_id=m["external_id"],
                chat_external_id=m["chat"],
                author_external_id=m["author"],
                author_name=author.get("name"),
                text=m["text"],
                sent_at=anchor + timedelta(minutes=m["minutes"]),
                has_media=bool(media),
                media_url=media,
                is_forwarded=bool(m.get("is_forwarded")),
                url=f"https://t.me/{username}/{m['external_id']}",
                views=m.get("views"),
            ))
        if not self._include_future:
            out = [m for m in out if m.sent_at <= now]
        out.sort(key=lambda x: (x.sent_at, x.external_id))
        return out

    async def add_source(self, ref: str) -> TgChat:
        from app.integrations.telegram.client import TelegramSourceError, chat_ext_id, parse_tg_ref

        ext = chat_ext_id(parse_tg_ref(ref))
        if not any(c["external_id"] == ext for c in MOCK_TG_CHATS):
            raise TelegramSourceError(f"В mock-режиме доступны только демо-источники, не {ref!r}")
        self._sources[ext] = ref
        return next(c for c in await self.list_chats() if c.external_id == ext)

    async def list_chats(self) -> list[TgChat]:
        return [
            TgChat(external_id=c["external_id"], title=c["title"], district=c["district"],
                   members_count=c["members_count"], username=c["username"], kind=c["kind"],
                   url=f"https://t.me/{c['username']}")
            for c in MOCK_TG_CHATS if c["external_id"] in self._sources
        ]

    async def fetch_messages(self, chat_id: str, since: datetime | None = None,
                             limit: int = 100) -> list[TgMessage]:
        if not is_tg_id(chat_id):
            return []
        msgs = [m for m in self.all_messages() if m.chat_external_id == chat_id]
        if since is not None:
            msgs = [m for m in msgs if m.sent_at > since]
        return msgs[:limit]

    async def get_user_profile(self, user_id: str) -> TgUserProfile | None:
        p = MOCK_TG_PROFILES.get(str(user_id))
        if p is None:
            return None
        return TgUserProfile(external_id=str(user_id), name=p["name"], phone=p["phone"],
                             username=p["username"])
