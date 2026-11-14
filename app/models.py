"""ORM-модели NEWSFLOW (п.8 ТЗ).

Совместимость MySQL + SQLite: строки — String(N), длинный текст — Text, структуры — JSON,
время — naive UTC DateTime (используйте ``utcnow()``). Enum-ы хранятся строками
(``native_enum=False``), значение в БД = ``.value`` (lowercase).
Форматы JSON-полей описаны в docs/CONTRACTS.md.
"""
from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Текущее время, naive UTC (одинаково ведёт себя в MySQL и SQLite)."""
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------- enums
class InvestigationStatus(str, enum.Enum):
    new = "new"
    researching = "researching"
    review = "review"
    handed_off = "handed_off"
    rejected = "rejected"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]


STATUS_LABELS = {
    InvestigationStatus.new: "Новый",
    InvestigationStatus.researching: "AI ищет",
    InvestigationStatus.review: "На проверке",
    InvestigationStatus.handed_off: "Передано дальше",
    InvestigationStatus.rejected: "Не подтвердилось",
}
STATUS_ORDER = list(InvestigationStatus)
ACTIVE_STATUSES = frozenset({InvestigationStatus.new, InvestigationStatus.researching, InvestigationStatus.review})

# Куда редактор передаёт новость (кнопка «Передать дальше»)
HANDOFF_CHANNELS: dict[str, str] = {
    "site": "Сайт",
    "telegram": "Telegram-канал",
    "max": "Канал MAX",
    "tv": "ТВ/радио",
    "print": "Печать/газета",
    "social": "Соцсети",
    "other": "Другое",
}


class Platform(str, enum.Enum):
    max = "max"
    telegram = "telegram"

    @property
    def label(self) -> str:
        return PLATFORM_LABELS[self.value]


PLATFORM_LABELS = {"max": "MAX", "telegram": "Telegram"}


class StoryOrigin(str, enum.Enum):
    manual = "manual"  # «Своя зацепка»
    emerging = "emerging"  # из карточки «Возникающий сюжет»


class SourceKind(str, enum.Enum):
    web = "web"
    max_chat = "max_chat"
    official = "official"
    media = "media"


class MediaKind(str, enum.Enum):
    photo = "photo"
    video = "video"


class DraftAuthorKind(str, enum.Enum):
    ai = "ai"
    editor = "editor"


class FactState(str, enum.Enum):
    confirmed = "confirmed"
    unconfirmed = "unconfirmed"
    false = "false"


def _enum(e: type[enum.Enum]) -> SAEnum:
    return SAEnum(
        e,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda x: [m.value for m in x],
        name=e.__name__.lower(),
    )


# ---------------------------------------------------------------- tables
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    login: Mapped[str] = mapped_column(String(64), unique=True)  # для карточек логина
    title: Mapped[str | None] = mapped_column(String(128))  # подпись на карточке (должность)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    handle: Mapped[str | None] = mapped_column(String(128))  # @channel
    description: Mapped[str | None] = mapped_column(Text)
    dna_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # см. CONTRACTS: dna_json
    sample_posts: Mapped[list[str] | None] = mapped_column(JSON)  # список текстов постов
    dna_updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class MaxChat(Base):
    """Чат-источник (MAX или Telegram). Алиас ``SourceChat``."""

    __tablename__ = "source_chats"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_source_chat_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), default="max", index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    ref: Mapped[str | None] = mapped_column(String(255))  # ссылка / @username, как ввёл редактор
    username: Mapped[str | None] = mapped_column(String(128))  # Telegram: @username без @
    kind: Mapped[str | None] = mapped_column(String(16))  # channel | group | chat
    url: Mapped[str | None] = mapped_column(String(512))  # https://t.me/<username>
    title: Mapped[str] = mapped_column(String(255))
    district: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    messages: Mapped[list[ChatMessage]] = relationship(back_populates="chat")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("chat_id", "external_id", name="uq_chat_message_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("source_chats.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(16), default="max")
    external_id: Mapped[str] = mapped_column(String(128))
    author_external_id: Mapped[str] = mapped_column(String(128), index=True)
    author_name: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    media_url: Mapped[str | None] = mapped_column(String(1024))
    message_url: Mapped[str | None] = mapped_column(String(1024))  # ссылка «открыть» (TG: https://t.me/…)
    is_forwarded: Mapped[bool] = mapped_column(Boolean, default=False)
    views: Mapped[int | None] = mapped_column(Integer)  # просмотры (каналы Telegram)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # после перезапуска демо-потока
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chat: Mapped[MaxChat] = relationship(back_populates="messages")


class AuthorProfile(Base):
    """Кэш профилей авторов MAX (get_user_profile). Телефон может отсутствовать."""

    __tablename__ = "author_profiles"

    external_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    username: Mapped[str | None] = mapped_column(String(128))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Investigation(Base):
    """Сюжет (story). Имя таблицы/класса сохранено с v1."""

    __tablename__ = "investigations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    lead_text: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    status: Mapped[InvestigationStatus] = mapped_column(
        _enum(InvestigationStatus), default=InvestigationStatus.new, index=True
    )
    origin: Mapped[str] = mapped_column(String(16), default="manual")  # manual | emerging
    emerging_key: Mapped[str | None] = mapped_column(String(64), index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    entities_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # см. CONTRACTS
    progress_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # см. CONTRACTS
    deeper_questions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)  # «Копать глубже»
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    signal_at: Mapped[datetime | None] = mapped_column(DateTime)  # первый сигнал (сообщение/создание)
    review_at: Mapped[datetime | None] = mapped_column(DateTime)  # первый выход справки
    # --- «Передать дальше»
    handoff_to: Mapped[str | None] = mapped_column(String(255))
    handoff_channel: Mapped[str | None] = mapped_column(String(32))
    handoff_channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    handoff_comment: Mapped[str | None] = mapped_column(Text)
    handed_off_at: Mapped[datetime | None] = mapped_column(DateTime)
    handed_off_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    channel: Mapped[Channel | None] = relationship(foreign_keys=[channel_id], lazy="selectin")
    owner: Mapped[User | None] = relationship(foreign_keys=[owner_id], lazy="selectin")
    handed_off_by: Mapped[User | None] = relationship(foreign_keys=[handed_off_by_id], lazy="selectin")

    @property
    def status_label(self) -> str:
        return InvestigationStatus(self.status).label

    @property
    def handoff_channel_label(self) -> str | None:
        if not self.handoff_channel:
            return None
        return HANDOFF_CHANNELS.get(self.handoff_channel, self.handoff_channel)


class SearchIteration(Base):
    """Шаг CHAIN SEARCH: n = 1..CHAIN_MAX_ITERATIONS."""

    __tablename__ = "search_iterations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    n: Mapped[int] = mapped_column(Integer)
    queries_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # см. CONTRACTS
    rationale: Mapped[str | None] = mapped_column(Text)
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind))
    title: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(String(1024))
    snippet: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    chat_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    iteration_n: Mapped[int | None] = mapped_column(Integer)  # какая итерация нашла
    query: Mapped[str | None] = mapped_column(String(512))  # каким запросом найдено
    cluster_id: Mapped[int | None] = mapped_column(Integer, index=True)  # кластер дублей
    uniqueness_score: Mapped[float | None] = mapped_column(Float)  # 0..1
    is_important: Mapped[bool] = mapped_column(Boolean, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)  # первоисточник кластера
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chat_message: Mapped[ChatMessage | None] = relationship(lazy="selectin")


class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    kind: Mapped[MediaKind] = mapped_column(_enum(MediaKind), default=MediaKind.photo)
    url: Mapped[str] = mapped_column(String(1024))
    thumbnail_url: Mapped[str | None] = mapped_column(String(1024))
    origin: Mapped[str | None] = mapped_column(String(255))  # «чат ЖК Чертаново», «t.me/...»
    captured_at: Mapped[datetime | None] = mapped_column(DateTime)
    relation: Mapped[str | None] = mapped_column(Text)  # связь с событием
    rights_status: Mapped[str] = mapped_column(String(32), default="not_verified")
    possibly_old: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Report(Base):
    __tablename__ = "reports"

    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), primary_key=True
    )
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON)  # см. CONTRACTS: report_json
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("investigation_id", "version", name="uq_draft_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)  # 1 = AI, далее правки
    author_kind: Mapped[DraftAuthorKind] = mapped_column(_enum(DraftAuthorKind))
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    claims_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # см. CONTRACTS
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FactMark(Base):
    __tablename__ = "fact_marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    claim_id: Mapped[str | None] = mapped_column(String(64))  # id из claims_json / report items
    fact_text: Mapped[str] = mapped_column(Text)
    state: Mapped[FactState] = mapped_column(_enum(FactState))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StatusHistory(Base):
    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[InvestigationStatus | None] = mapped_column(_enum(InvestigationStatus))
    to_status: Mapped[InvestigationStatus] = mapped_column(_enum(InvestigationStatus))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))  # None = система
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User | None] = relationship(lazy="selectin")


class DismissedEmerging(Base):
    """Скрытые редактором карточки «Возникающий сюжет»."""

    __tablename__ = "dismissed_emerging"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


SourceChat = MaxChat


__all__ = [
    "InvestigationStatus", "SourceKind", "MediaKind", "DraftAuthorKind", "FactState", "Platform",
    "StoryOrigin", "STATUS_LABELS", "STATUS_ORDER", "ACTIVE_STATUSES", "HANDOFF_CHANNELS",
    "PLATFORM_LABELS", "utcnow",
    "User", "Channel", "MaxChat", "SourceChat", "ChatMessage", "AuthorProfile", "Investigation",
    "SearchIteration", "Source", "MediaItem", "Report", "Draft", "FactMark", "StatusHistory",
    "DismissedEmerging",
]
