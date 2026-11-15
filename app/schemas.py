"""Pydantic-схемы JSON API v2 (контракт между api.py, pages.py/JS и тестами).

Все *Out-схемы читаются из ORM (``from_attributes=True``): ``XOut.model_validate(orm_obj)``.
Форматы вложенных JSON (report/progress/claims/dna/entities) — docs/CONTRACTS.md §5,
API v2 — docs/CONTRACTS_v2.md.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    DraftAuthorKind,
    FactState,
    InvestigationStatus,
    MediaKind,
    SourceKind,
)


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth / users
class LoginIn(BaseModel):
    user_id: int | None = None
    login: str | None = None
    password: str = "demo"


class UserOut(ORM):
    id: int
    name: str
    login: str
    title: str | None = None


# ---------------------------------------------------------------- channels / источники
class ChannelOut(ORM):
    id: int
    name: str
    handle: str | None = None
    description: str | None = None
    dna_json: dict[str, Any] | None = None
    dna_updated_at: datetime | None = None


class SourceChatOut(ORM):
    id: int
    platform: str = "max"
    platform_label: str = "MAX"
    external_id: str
    ref: str | None = None
    title: str
    district: str | None = None
    enabled: bool
    last_synced_at: datetime | None = None
    messages_count: int = 0
    # v3 (CONTRACTS_v3 §2): реальные метаданные источника из resolve
    username: str | None = None
    kind: str | None = None  # channel | group | chat | user | feed
    url: str | None = None  # t.me/max.ru-ссылка; для rss — URL ленты
    site_url: str | None = None
    avatar_url: str | None = None
    subscribers: int | None = None
    description: str | None = None


MaxChatOut = SourceChatOut  # совместимость v1


class SourceChatUpdate(BaseModel):
    """PATCH: передаются только изменяемые поля."""

    enabled: bool | None = None
    district: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=255)


MaxChatUpdate = SourceChatUpdate


class SourceAddIn(BaseModel):
    """``platform="auto"`` — определить по ссылке (как ``/api/sources/resolve``)."""

    platform: Literal["max", "telegram", "rss", "auto"] = "telegram"
    ref: str = Field(default="", max_length=1024,
                     description="@username, t.me/max.ru-ссылка, id чата MAX или URL сайта/RSS (пустое — 422)")
    title: str | None = Field(default=None, max_length=255)
    district: str | None = Field(default=None, max_length=128)


class SourceResolveIn(BaseModel):
    ref: str = Field(default="", max_length=1024)
    platform: Literal["max", "telegram", "rss", "auto"] | None = None  # подсказка; None/auto — по ссылке


class SourcePreviewOut(BaseModel):
    """Превью источника до добавления (POST /api/sources/resolve). Ошибка — в ``error``, не 4xx."""

    platform: Literal["max", "telegram", "rss"]
    platform_label: str = ""
    ref: str
    external_id: str | None = None  # с каким external_id источник будет сохранён
    title: str
    username: str | None = None
    kind: str | None = None
    avatar_url: str | None = None
    subscribers: int | None = None
    description: str | None = None
    url: str | None = None
    site_url: str | None = None
    already_added: bool = False
    chat_id: int | None = None  # id уже подключённого источника
    error: str | None = None
    cached: bool = False


class SourceDeleteOut(BaseModel):
    id: int
    title: str
    platform: str
    deleted: bool = True
    archived_messages: int = 0  # сколько сообщений ушло из ленты/emerging/поиска
    story_references: int = 0  # сколько строк источников в сюжетах ссылаются на его сообщения (остаются)


class ModeOut(BaseModel):
    data_mode: str  # live | demo
    llm: str  # anthropic | extractive | mock
    telegram_provider: str | None = None
    max_provider: str | None = None
    search_providers: list[str] = Field(default_factory=list)
    label: str | None = None  # «Реальные данные» | «Демо (синтетика)»
    rss_ingest: bool | None = None
    notes: list[str] = Field(default_factory=list)


class SyncResult(BaseModel):
    chats: int = 0  # сколько чатов опрошено
    new_messages: int = 0
    errors: list[str] = Field(default_factory=list)
    finished_at: datetime | None = None
    by_platform: dict[str, int] = Field(default_factory=dict)  # новых сообщений по платформам


class DemoStreamOut(BaseModel):
    enabled: bool
    active: bool  # трикл реально работает (mock MAX)
    epoch: int = 0
    started_at: datetime | None = None
    released: int = 0
    total: int = 0
    next_at: datetime | None = None
    finished: bool = False
    interval_sec: list[float] = Field(default_factory=list)


# ---------------------------------------------------------------- лента / возникающие сюжеты
class FeedBadges(BaseModel):
    unique: bool = False
    important: bool = False
    duplicates: int = 0  # у первоисточника: сколько копий
    is_duplicate: bool = False
    duplicate_of: int | None = None
    has_phone: bool = False


class FeedMessage(BaseModel):
    id: int
    platform: str = "max"
    platform_label: str = "MAX"
    chat_id: int
    chat_title: str | None = None
    district: str | None = None
    author_name: str | None = None
    author_external_id: str | None = None
    text: str
    sent_at: datetime
    has_media: bool = False
    media_url: str | None = None
    is_forwarded: bool = False
    message_url: str | None = None
    badges: FeedBadges = Field(default_factory=FeedBadges)
    uniqueness_score: float | None = None
    emerging_key: str | None = None


class FeedOut(BaseModel):
    items: list[FeedMessage]
    cursor: int = 0
    server_time: datetime


class EmergingStoryOut(BaseModel):
    key: str
    title: str
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    place: str | None = None
    messages_count: int
    chats_count: int
    chats: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    first_at: datetime
    last_at: datetime
    trend: Literal["rising", "steady", "fading"] = "steady"
    trend_label: str = "стабильно"
    recent_count: int = 0
    important_count: int = 0
    unique_count: int = 0
    has_phone_count: int = 0
    score: float = 0.0
    message_ids: list[int] = Field(default_factory=list)
    sample: list[FeedMessage] = Field(default_factory=list)
    investigation_id: int | None = None
    investigation_status: InvestigationStatus | None = None


class EmergingInvestigateIn(BaseModel):
    channel_id: int | None = None
    title: str | None = None
    note: str | None = Field(default=None, description="Дополнение редактора к зацепке")


# ---------------------------------------------------------------- сюжеты
class InvestigationCreate(BaseModel):
    lead_text: str = Field(min_length=3, description="«Своя зацепка»")
    title: str | None = Field(default=None, description="Если пусто — первые ~80 символов lead_text")
    channel_id: int | None = None
    autorun: bool = True


StoryCreate = InvestigationCreate


class InvestigationOut(ORM):
    id: int
    title: str
    lead_text: str
    status: InvestigationStatus
    status_label: str = ""
    origin: str = "manual"
    emerging_key: str | None = None
    channel_id: int | None = None
    owner_id: int | None = None
    created_at: datetime
    updated_at: datetime
    signal_at: datetime | None = None
    review_at: datetime | None = None
    handoff_to: str | None = None
    handoff_channel: str | None = None
    handoff_channel_label: str | None = None
    handoff_channel_id: int | None = None
    handoff_comment: str | None = None
    handed_off_at: datetime | None = None
    handed_off_by_id: int | None = None


StoryOut = InvestigationOut


class InvestigationDetail(InvestigationOut):
    entities_json: dict[str, Any] | None = None
    progress_json: dict[str, Any] | None = None
    deeper_questions: list[dict[str, Any]] | None = None
    channel: ChannelOut | None = None
    owner: UserOut | None = None
    handed_off_by: UserOut | None = None
    allowed_transitions: list[InvestigationStatus] = Field(default_factory=list)
    unconfirmed_count: int = 0


StoryDetail = InvestigationDetail


class DeeperIn(BaseModel):
    question: str = Field(default="", max_length=1000, description="Доп. вопрос редактора")


class CommentIn(BaseModel):
    comment: str | None = None


class HandoffIn(BaseModel):
    to: str = Field(min_length=1, max_length=255, description="Кому / куда")
    channel: str = Field(description="Ключ из HANDOFF_CHANNELS")
    channel_id: int | None = None
    comment: str | None = None


class UnconfirmedItem(BaseModel):
    id: str | None = None
    text: str
    state: str
    source_ids: list[int] = Field(default_factory=list)
    origin: Literal["draft", "report"] = "draft"


class HandoffCheckOut(BaseModel):
    ok: bool
    warnings: list[str] = Field(default_factory=list)
    unconfirmed: list[UnconfirmedItem] = Field(default_factory=list)
    has_report: bool = False
    has_draft: bool = False


class HandoffResultOut(BaseModel):
    story: InvestigationOut
    warnings: list[str] = Field(default_factory=list)
    unconfirmed: list[UnconfirmedItem] = Field(default_factory=list)


class ProgressOut(BaseModel):
    """Ответ GET /api/stories/{id}/progress."""

    investigation_id: int
    status: InvestigationStatus
    progress: dict[str, Any]
    iterations: list[SearchIterationOut] = Field(default_factory=list)


class SearchIterationOut(ORM):
    id: int
    n: int
    queries_json: list[dict[str, Any]]
    rationale: str | None = None
    results_count: int = 0
    created_at: datetime


class SourceOut(ORM):
    id: int
    kind: SourceKind
    title: str | None = None
    url: str | None = None
    snippet: str | None = None
    published_at: datetime | None = None
    chat_message_id: int | None = None
    iteration_n: int | None = None
    query: str | None = None
    cluster_id: int | None = None
    uniqueness_score: float | None = None
    is_important: bool = False
    is_primary: bool = False
    duplicates_count: int = 0
    platform: str | None = None  # для kind=max_chat: max | telegram


class ContactOut(BaseModel):
    """Блок «Кому позвонить»."""

    source_id: int
    author_external_id: str
    name: str | None = None
    phone: str | None = None  # None → «телефон скрыт в профиле»
    username: str | None = None
    quote: str
    chat_title: str | None = None
    platform: str = "max"
    sent_at: datetime | None = None
    message_url: str | None = None
    uniqueness_score: float | None = None


class MediaItemOut(ORM):
    id: int
    source_id: int | None = None
    kind: MediaKind
    url: str
    thumbnail_url: str | None = None
    origin: str | None = None
    captured_at: datetime | None = None
    relation: str | None = None
    rights_status: str = "not_verified"
    possibly_old: bool = False


class ReportOut(ORM):
    investigation_id: int
    report_json: dict[str, Any]
    created_at: datetime


class DraftOut(ORM):
    id: int
    investigation_id: int
    version: int
    author_kind: DraftAuthorKind
    author_user_id: int | None = None
    title: str
    body: str
    claims_json: list[dict[str, Any]]
    created_at: datetime


class DraftCreate(BaseModel):
    title: str
    body: str
    claims_json: list[dict[str, Any]] | None = None  # None → перенести claims из пред. версии


class DiffOp(BaseModel):
    op: Literal["equal", "insert", "delete", "replace"]
    a: str = ""
    b: str = ""


class DraftDiffOut(BaseModel):
    base_version: int
    target_version: int
    ratio: float
    ops: list[DiffOp]


class FactMarkIn(BaseModel):
    fact_text: str
    state: FactState
    claim_id: str | None = None
    comment: str | None = None


class FactMarkOut(ORM):
    id: int
    claim_id: str | None = None
    fact_text: str
    state: FactState
    user_id: int | None = None
    comment: str | None = None
    created_at: datetime


class TransitionIn(BaseModel):
    to_status: InvestigationStatus
    comment: str | None = None


class StatusHistoryOut(ORM):
    id: int
    from_status: InvestigationStatus | None = None
    to_status: InvestigationStatus
    user_id: int | None = None
    user: UserOut | None = None
    comment: str | None = None
    created_at: datetime


# ---------------------------------------------------------------- статистика
class RecentHandoff(BaseModel):
    id: int
    title: str
    handoff_to: str | None = None
    handoff_channel: str | None = None
    handoff_channel_label: str | None = None
    handed_off_at: datetime | None = None
    signal_to_handoff_sec: float | None = None


class StatsOut(BaseModel):
    stories_total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    handed_off: int = 0
    rejected: int = 0
    in_work: int = 0
    avg_signal_to_handoff_sec: float | None = None
    avg_signal_to_review_sec: float | None = None
    traceability: float | None = None
    source_coverage: float | None = None
    factcheck_correction_rate: float | None = None
    draft_acceptance_rate: float | None = None
    handoffs_by_channel: dict[str, int] = Field(default_factory=dict)
    recent_handoffs: list[RecentHandoff] = Field(default_factory=list)


ProgressOut.model_rebuild()
