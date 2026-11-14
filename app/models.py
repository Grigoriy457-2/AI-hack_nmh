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
class Role(str, enum.Enum):
    producer = "producer"
    editor = "editor"
    smm = "smm"
    operator = "operator"  # AI-оператор, может всё

    @property
    def label(self) -> str:
        return ROLE_LABELS[self]


ROLE_LABELS = {
    Role.producer: "Продюсер",
    Role.editor: "Редактор",
    Role.smm: "SMM",
    Role.operator: "AI-оператор",
}


class InvestigationStatus(str, enum.Enum):
    new = "new"
    researching = "researching"
    report_ready = "report_ready"
    editing = "editing"
    ready = "ready"
    published = "published"
    rejected = "rejected"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]


STATUS_LABELS = {
    InvestigationStatus.new: "Новая зацепка",
    InvestigationStatus.researching: "Исследование",
    InvestigationStatus.report_ready: "Справка готова",
    InvestigationStatus.editing: "На редактуре",
    InvestigationStatus.ready: "Готово для SMM",
    InvestigationStatus.published: "Опубликовано",
    InvestigationStatus.rejected: "Отклонено",
}


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


class Verdict(str, enum.Enum):
    good = "good"  # «норм»
    bad = "bad"  # «стрём»


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
    role: Mapped[Role] = mapped_column(_enum(Role), index=True)
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
    __tablename__ = "max_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    district: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    messages: Mapped[list[ChatMessage]] = relationship(back_populates="chat")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("chat_id", "external_id", name="uq_chat_message_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("max_chats.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(64))
    author_external_id: Mapped[str] = mapped_column(String(64), index=True)
    author_name: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    media_url: Mapped[str | None] = mapped_column(String(1024))
    is_forwarded: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chat: Mapped[MaxChat] = relationship(back_populates="messages")


class AuthorProfile(Base):
    """Кэш профилей авторов MAX (get_user_profile). Телефон может отсутствовать."""

    __tablename__ = "author_profiles"

    external_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    username: Mapped[str | None] = mapped_column(String(128))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    lead_text: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    status: Mapped[InvestigationStatus] = mapped_column(
        _enum(InvestigationStatus), default=InvestigationStatus.new, index=True
    )
    producer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    editor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    entities_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # см. CONTRACTS
    progress_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # см. CONTRACTS
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    report_ready_at: Mapped[datetime | None] = mapped_column(DateTime)  # для Time to Draft

    channel: Mapped[Channel | None] = relationship(lazy="selectin")
    producer: Mapped[User | None] = relationship(foreign_keys=[producer_id], lazy="selectin")
    editor: Mapped[User | None] = relationship(foreign_keys=[editor_id], lazy="selectin")


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


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    verdict: Mapped[Verdict] = mapped_column(_enum(Verdict))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User | None] = relationship(lazy="selectin")


__all__ = [
    "Role", "InvestigationStatus", "SourceKind", "MediaKind", "DraftAuthorKind", "FactState",
    "Verdict", "ROLE_LABELS", "STATUS_LABELS", "utcnow",
    "User", "Channel", "MaxChat", "ChatMessage", "AuthorProfile", "Investigation",
    "SearchIteration", "Source", "MediaItem", "Report", "Draft", "FactMark", "StatusHistory",
    "Feedback",
]
