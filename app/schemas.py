"""Pydantic-схемы JSON API (контракт между api.py, pages.py/JS и тестами).

Все *Out-схемы читаются из ORM (``from_attributes=True``): ``XOut.model_validate(orm_obj)``.
Форматы вложенных JSON (report/progress/claims/dna/entities) — docs/CONTRACTS.md.
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
    Role,
    SourceKind,
    Verdict,
)


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth / users
class LoginIn(BaseModel):
    user_id: int
    password: str = "demo"


class UserOut(ORM):
    id: int
    name: str
    role: Role
    login: str
    title: str | None = None


# ---------------------------------------------------------------- channels / MAX
class ChannelOut(ORM):
    id: int
    name: str
    handle: str | None = None
    description: str | None = None
    dna_json: dict[str, Any] | None = None
    dna_updated_at: datetime | None = None


class MaxChatOut(ORM):
    id: int
    external_id: str
    title: str
    district: str | None = None
    enabled: bool
    last_synced_at: datetime | None = None
    messages_count: int = 0  # заполняется вручную в api.py


class MaxChatUpdate(BaseModel):
    """PATCH: передаются только изменяемые поля."""

    enabled: bool | None = None
    district: str | None = Field(default=None, max_length=128)


class SyncResult(BaseModel):
    chats: int = 0  # сколько чатов опрошено
    new_messages: int = 0
    errors: list[str] = Field(default_factory=list)
    finished_at: datetime | None = None


# ---------------------------------------------------------------- investigations
class InvestigationCreate(BaseModel):
    lead_text: str = Field(min_length=3, description="«Что произошло?»")
    title: str | None = Field(default=None, description="Если пусто — первые ~80 символов lead_text")
    channel_id: int | None = None
    autorun: bool = True  # сразу запустить пайплайн (кнопка «Запустить NEWSFLOW»)


class InvestigationOut(ORM):
    id: int
    title: str
    lead_text: str
    status: InvestigationStatus
    channel_id: int | None = None
    producer_id: int | None = None
    editor_id: int | None = None
    created_at: datetime
    updated_at: datetime
    report_ready_at: datetime | None = None


class InvestigationDetail(InvestigationOut):
    entities_json: dict[str, Any] | None = None
    progress_json: dict[str, Any] | None = None
    channel: ChannelOut | None = None
    producer: UserOut | None = None
    editor: UserOut | None = None
    allowed_transitions: list[InvestigationStatus] = Field(
        default_factory=list, description="Куда текущий пользователь может перевести статус"
    )


class ProgressOut(BaseModel):
    """Ответ GET /api/investigations/{id}/progress (опрос раз в 1–2 с)."""

    investigation_id: int
    status: InvestigationStatus
    progress: dict[str, Any]  # progress_json (CONTRACTS.md), {} если ещё не запускали
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
    duplicates_count: int = 0  # размер кластера − 1, заполняется в api.py


class ContactOut(BaseModel):
    """Блок «Кому позвонить» — только producer/operator."""

    source_id: int
    author_external_id: str
    name: str | None = None
    phone: str | None = None  # None → «телефон скрыт в профиле»
    username: str | None = None
    quote: str
    chat_title: str | None = None
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
    """Сохранение правки редактора (новая версия, author_kind=editor)."""

    title: str
    body: str
    claims_json: list[dict[str, Any]] | None = None  # None → перенести claims из пред. версии


class DiffOp(BaseModel):
    op: Literal["equal", "insert", "delete", "replace"]
    a: str = ""  # фрагмент базовой версии
    b: str = ""  # фрагмент целевой версии


class DraftDiffOut(BaseModel):
    base_version: int
    target_version: int
    ratio: float  # difflib.SequenceMatcher.ratio(), 1.0 = без правок
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
    editor_id: int | None = None  # при переводе в editing можно назначить редактора


class StatusHistoryOut(ORM):
    id: int
    from_status: InvestigationStatus | None = None
    to_status: InvestigationStatus
    user_id: int | None = None
    user: UserOut | None = None
    comment: str | None = None
    created_at: datetime


class FeedbackIn(BaseModel):
    verdict: Verdict
    comment: str | None = None


class FeedbackOut(ORM):
    id: int
    investigation_id: int
    user_id: int | None = None
    user: UserOut | None = None
    verdict: Verdict
    comment: str | None = None
    created_at: datetime


# ---------------------------------------------------------------- operator
class BoardCard(BaseModel):
    id: int
    title: str
    status: InvestigationStatus
    responsible: UserOut | None = None  # по статусу: producer / editor / (smm — None)
    updated_at: datetime
    age_minutes: int
    is_stale: bool  # дольше STALE_AFTER_MINUTES в одном статусе (кроме published/rejected)


class BoardOut(BaseModel):
    columns: dict[InvestigationStatus, list[BoardCard]]  # все 7 статусов, даже пустые
    stale: list[BoardCard]


class MetricsOut(BaseModel):
    """Ключи — services/metrics.py:METRIC_KEYS. Значение None = недостаточно данных."""

    time_to_draft_avg_sec: float | None = None
    source_coverage: float | None = None
    traceability: float | None = None
    draft_acceptance_rate: float | None = None
    factcheck_correction_rate: float | None = None
    feedback_good_share: float | None = None
    investigations_total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    feedback_total: int = 0


ProgressOut.model_rebuild()
