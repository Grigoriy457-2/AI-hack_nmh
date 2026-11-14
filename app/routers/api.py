"""JSON API (prefix /api). Зона DEV-BACKEND.

Список эндпоинтов, схемы и роли — контракт (docs/CONTRACTS.md). Права проверяются
зависимостями ``require_role`` (operator проходит всегда); переходы статусов — только через
services.workflow.transition. Коды ошибок: 401 не вошёл, 403 нет прав, 404 нет объекта,
409 недопустимый переход, 422 валидация.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user, require_role
from app.db import get_session
from app.models import InvestigationStatus, Role, SourceKind, User
from app.schemas import (
    BoardOut,
    ChannelOut,
    ContactOut,
    DraftCreate,
    DraftDiffOut,
    DraftOut,
    FactMarkIn,
    FactMarkOut,
    FeedbackIn,
    FeedbackOut,
    InvestigationCreate,
    InvestigationDetail,
    InvestigationOut,
    LoginIn,
    MaxChatOut,
    MaxChatUpdate,
    MediaItemOut,
    MetricsOut,
    ProgressOut,
    ReportOut,
    SearchIterationOut,
    SourceOut,
    StatusHistoryOut,
    SyncResult,
    TransitionIn,
    UserOut,
)

router = APIRouter(prefix="/api", tags=["api"])

P, E, SMM, OP = Role.producer, Role.editor, Role.smm, Role.operator
ANY = (P, E, SMM, OP)


# ================================================================ служебное / auth
@router.get("/health")
async def health() -> dict:
    """Публичный. Проверка живости."""
    return {"status": "ok"}


@router.get("/users", response_model=list[UserOut])
async def list_users(session: AsyncSession = Depends(get_session)):
    """Публичный: карточки пользователей для страницы логина."""
    raise NotImplementedError


@router.post("/auth/login", response_model=UserOut)
async def login(data: LoginIn, response: Response, session: AsyncSession = Depends(get_session)):
    """Публичный. Проверяет пароль (auth.verify_password), ставит cookie (auth.set_login_cookie).
    401 при неверном пароле, 404 нет пользователя."""
    raise NotImplementedError


@router.post("/auth/logout", status_code=204)
async def logout(response: Response):
    """Снимает cookie."""
    raise NotImplementedError


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    raise NotImplementedError


# ================================================================ каналы
@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Все роли."""
    raise NotImplementedError


@router.post("/channels/{channel_id}/dna", response_model=ChannelOut)
async def rebuild_channel_dna(channel_id: int, user: User = Depends(require_role(OP)),
                              session: AsyncSession = Depends(get_session)):
    """Operator: пересчитать Channel DNA (ai.channel_dna.ensure_channel_dna force=True)."""
    raise NotImplementedError


# ================================================================ расследования
@router.get("/investigations", response_model=list[InvestigationOut])
async def list_investigations(
    status: InvestigationStatus | None = None,
    mine: bool = False,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Все роли. По умолчанию видимость по роли: producer — свои (mine) или все при mine=False;
    editor — editing (+свои); smm — ready/published; operator — все. Сортировка updated_at desc."""
    raise NotImplementedError


@router.post("/investigations", response_model=InvestigationOut, status_code=201)
async def create_investigation(data: InvestigationCreate, user: User = Depends(require_role(P)),
                               session: AsyncSession = Depends(get_session)):
    """Producer. status=new, producer_id=user, StatusHistory(None->new). autorun=True →
    transition(new->researching) + pipeline.start_investigation (или await при PIPELINE_INLINE)."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}", response_model=InvestigationDetail)
async def get_investigation(inv_id: int, user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)):
    """Все роли. allowed_transitions = workflow.available_transitions(user.role, status)."""
    raise NotImplementedError


@router.post("/investigations/{inv_id}/run", response_model=InvestigationOut)
async def run_investigation(inv_id: int, user: User = Depends(require_role(P)),
                            session: AsyncSession = Depends(get_session)):
    """Producer. new|report_ready -> researching, запуск пайплайна. 409 если уже идёт."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/progress", response_model=ProgressOut)
async def get_progress(inv_id: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    """Все роли. Опрос раз в 1–2 с; progress_json + итерации цепочки поиска."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/iterations", response_model=list[SearchIterationOut])
async def list_iterations(inv_id: int, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    raise NotImplementedError


@router.get("/investigations/{inv_id}/sources", response_model=list[SourceOut])
async def list_sources(
    inv_id: int,
    kind: SourceKind | None = None,
    primary_only: bool = False,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Все роли. duplicates_count = размер кластера − 1. Сортировка: primary, uniqueness desc."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/contacts", response_model=list[ContactOut])
async def list_contacts(inv_id: int, user: User = Depends(require_role(P)),
                        session: AsyncSession = Depends(get_session)):
    """Producer/Operator ONLY. «Кому позвонить»: sources kind=max_chat, is_primary,
    uniqueness>=0.5, is_important + author_profiles (phone может быть None)."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/media", response_model=list[MediaItemOut])
async def list_media(inv_id: int, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    raise NotImplementedError


@router.get("/investigations/{inv_id}/report", response_model=ReportOut)
async def get_report(inv_id: int, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    """404 если справки ещё нет."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/drafts", response_model=list[DraftOut])
async def list_drafts(inv_id: int, user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    """Все версии по возрастанию version (v1 — AI)."""
    raise NotImplementedError


@router.post("/investigations/{inv_id}/drafts", response_model=DraftOut, status_code=201)
async def create_draft(inv_id: int, data: DraftCreate, user: User = Depends(require_role(E)),
                       session: AsyncSession = Depends(get_session)):
    """Editor (только в статусе editing, иначе 409). Новая версия author_kind=editor."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/drafts/diff", response_model=DraftDiffOut)
async def diff_drafts(
    inv_id: int,
    base: int | None = Query(None, description="версия, по умолчанию 1 (AI)"),
    target: int | None = Query(None, description="версия, по умолчанию последняя"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Пословный diff (difflib) body base vs target."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/facts", response_model=list[FactMarkOut])
async def list_facts(inv_id: int, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    raise NotImplementedError


@router.post("/investigations/{inv_id}/facts", response_model=FactMarkOut, status_code=201)
async def mark_fact(inv_id: int, data: FactMarkIn, user: User = Depends(require_role(P, E)),
                    session: AsyncSession = Depends(get_session)):
    """Producer/Editor: подтверждён / не подтверждён / ложь. Повторная отметка того же
    claim_id тем же пользователем — обновление."""
    raise NotImplementedError


@router.post("/investigations/{inv_id}/transition", response_model=InvestigationOut)
async def transition_investigation(inv_id: int, data: TransitionIn, user: User = Depends(current_user),
                                   session: AsyncSession = Depends(get_session)):
    """Любая роль; права — workflow.can_transition (403 / 409)."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/history", response_model=list[StatusHistoryOut])
async def list_history(inv_id: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    raise NotImplementedError


@router.post("/investigations/{inv_id}/feedback", response_model=FeedbackOut, status_code=201)
async def add_feedback(inv_id: int, data: FeedbackIn, user: User = Depends(require_role(P)),
                       session: AsyncSession = Depends(get_session)):
    """Producer: «норм / стрём» + комментарий."""
    raise NotImplementedError


# ================================================================ AI-оператор
@router.get("/feedback", response_model=list[FeedbackOut])
async def list_feedback(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    """Operator: лента обратной связи (created_at desc)."""
    raise NotImplementedError


@router.get("/operator/board", response_model=BoardOut)
async def operator_board(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    """Канбан всех статусов + зависшие (STALE_AFTER_MINUTES)."""
    raise NotImplementedError


@router.get("/metrics", response_model=MetricsOut)
async def get_metrics(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    """services.metrics.compute_metrics."""
    raise NotImplementedError


@router.get("/max/chats", response_model=list[MaxChatOut])
async def list_max_chats(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    raise NotImplementedError


@router.patch("/max/chats/{chat_id}", response_model=MaxChatOut)
async def update_max_chat(chat_id: int, data: MaxChatUpdate, user: User = Depends(require_role(OP)),
                          session: AsyncSession = Depends(get_session)):
    """Вкл/выкл чат."""
    raise NotImplementedError


@router.post("/max/sync", response_model=SyncResult)
async def sync_max(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    """«Синхронизировать сейчас»: poller.sync_once."""
    raise NotImplementedError
