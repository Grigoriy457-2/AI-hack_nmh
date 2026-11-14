"""JSON API (prefix /api). Зона DEV-BACKEND.

Список эндпоинтов, схемы и роли — контракт (docs/CONTRACTS.md). Права проверяются
зависимостями ``require_role`` (operator проходит всегда); переходы статусов — только через
services.workflow.transition. Коды ошибок: 401 не вошёл, 403 нет прав, 404 нет объекта,
409 недопустимый переход, 422 валидация.
"""
from __future__ import annotations

import difflib
import logging
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status as http
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.auth import current_user, require_role
from app.config import settings
from app.db import get_session
from app.models import (
    AuthorProfile,
    Channel,
    ChatMessage,
    Draft,
    DraftAuthorKind,
    FactMark,
    Feedback,
    Investigation,
    InvestigationStatus,
    MaxChat,
    MediaItem,
    Report,
    Role,
    SearchIteration,
    Source,
    SourceKind,
    StatusHistory,
    User,
    utcnow,
)
from app.schemas import (
    BoardCard,
    BoardOut,
    ChannelOut,
    ContactOut,
    DiffOp,
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
from app.services import workflow
from app.services.metrics import draft_similarity, tokenize

log = logging.getLogger("newsflow.api")

router = APIRouter(prefix="/api", tags=["api"])

P, E, SMM, OP = Role.producer, Role.editor, Role.smm, Role.operator
ANY = (P, E, SMM, OP)
S = InvestigationStatus

# Кто «отвечает» за карточку в каждом статусе (доска оператора)
_RESPONSIBLE = {
    S.new: "producer", S.researching: "producer", S.report_ready: "producer",
    S.rejected: "producer", S.editing: "editor", S.ready: None, S.published: None,
}
_FINAL = {S.published, S.rejected}


# ================================================================ helpers
async def _get_inv(session: AsyncSession, inv_id: int) -> Investigation:
    inv = await session.get(Investigation, inv_id)
    if inv is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Расследование не найдено")
    return inv


def _role(user: User | None) -> Role | None:
    return Role(user.role) if user is not None else None


async def _do_transition(session: AsyncSession, inv: Investigation, to: S, user: User | None,
                         comment: str | None = None) -> None:
    try:
        await workflow.transition(session, inv, to, user, comment)
    except workflow.TransitionForbidden as e:
        raise HTTPException(http.HTTP_403_FORBIDDEN, str(e)) from e
    except workflow.TransitionNotAllowed as e:
        raise HTTPException(http.HTTP_409_CONFLICT, str(e)) from e


async def _launch_pipeline(session: AsyncSession, inv: Investigation) -> None:
    """Запуск пайплайна DEV-AI после commit перехода в researching."""
    from app.services import pipeline

    try:
        if settings.PIPELINE_INLINE:
            await pipeline.run_investigation(inv.id)
            await session.refresh(inv)
        else:
            pipeline.start_investigation(inv.id)
    except NotImplementedError:
        log.warning("pipeline not implemented yet — investigation %s stays researching", inv.id)
        inv.progress_json = {
            "state": "error", "percent": 0, "current_stage": None, "started_at": None,
            "finished_at": None, "error": "Пайплайн ещё не реализован", "stages": [], "log": [],
        }
        await session.commit()


def _diff(a: str, b: str) -> list[DiffOp]:
    ta, tb = tokenize(a), tokenize(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return [
        DiffOp(op=tag, a="".join(ta[i1:i2]), b="".join(tb[j1:j2]))
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
    ]


async def _history_last_change(session: AsyncSession) -> dict[int, object]:
    rows = await session.execute(
        select(StatusHistory.investigation_id, func.max(StatusHistory.created_at))
        .group_by(StatusHistory.investigation_id)
    )
    return dict(rows.all())


# ================================================================ служебное / auth
@router.get("/health")
async def health() -> dict:
    """Публичный. Проверка живости."""
    return {"status": "ok"}


@router.get("/users", response_model=list[UserOut])
async def list_users(session: AsyncSession = Depends(get_session)):
    """Публичный: карточки пользователей для страницы логина."""
    return (await session.scalars(select(User).order_by(User.id))).all()


@router.post("/auth/login", response_model=UserOut)
async def login(data: LoginIn, response: Response, session: AsyncSession = Depends(get_session)):
    """Публичный. Проверяет пароль (auth.verify_password), ставит cookie (auth.set_login_cookie).
    401 при неверном пароле, 404 нет пользователя."""
    user = await session.get(User, data.user_id)
    if user is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Пользователь не найден")
    if not auth.verify_password(data.password):
        raise HTTPException(http.HTTP_401_UNAUTHORIZED, "Неверный пароль")
    auth.set_login_cookie(response, user)
    return user


@router.post("/auth/logout", status_code=204)
async def logout(user: User = Depends(current_user)):
    """Снимает cookie."""
    resp = Response(status_code=204)
    auth.clear_login_cookie(resp)
    return resp


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user


# ================================================================ каналы
@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Все роли."""
    return (await session.scalars(select(Channel).order_by(Channel.id))).all()


@router.post("/channels/{channel_id}/dna", response_model=ChannelOut)
async def rebuild_channel_dna(channel_id: int, user: User = Depends(require_role(OP)),
                              session: AsyncSession = Depends(get_session)):
    """Operator: пересчитать Channel DNA (ai.channel_dna.ensure_channel_dna force=True)."""
    from app.ai.channel_dna import ensure_channel_dna

    ch = await session.get(Channel, channel_id)
    if ch is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Канал не найден")
    await ensure_channel_dna(session, ch, force=True)
    await session.commit()
    await session.refresh(ch)
    return ch


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
    q = select(Investigation)
    role = _role(user)
    if role == Role.producer:
        if mine:
            q = q.where(Investigation.producer_id == user.id)
    elif role == Role.editor:
        q = q.where(Investigation.editor_id == user.id) if mine else q.where(
            or_(Investigation.status == S.editing, Investigation.editor_id == user.id)
        )
    elif role == Role.smm:
        q = q.where(Investigation.status.in_([S.ready, S.published]))
    if status is not None:
        q = q.where(Investigation.status == status)
    q = q.order_by(Investigation.updated_at.desc(), Investigation.id.desc())
    return (await session.scalars(q)).all()


@router.post("/investigations", response_model=InvestigationOut, status_code=201)
async def create_investigation(data: InvestigationCreate, user: User = Depends(require_role(P)),
                               session: AsyncSession = Depends(get_session)):
    """Producer. status=new, producer_id=user, StatusHistory(None->new). autorun=True →
    transition(new->researching) + pipeline.start_investigation (или await при PIPELINE_INLINE)."""
    lead = data.lead_text.strip()
    if data.channel_id is not None and await session.get(Channel, data.channel_id) is None:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "Канал не найден")
    title = (data.title or "").strip()
    if not title:
        first_line = lead.splitlines()[0] if lead else lead
        title = first_line if len(first_line) <= 80 else first_line[:79].rstrip() + "…"
    now = utcnow()
    inv = Investigation(title=title, lead_text=lead, channel_id=data.channel_id, status=S.new,
                        producer_id=user.id, created_at=now, updated_at=now)
    session.add(inv)
    await session.flush()
    session.add(StatusHistory(investigation_id=inv.id, from_status=None, to_status=S.new,
                              user_id=user.id, comment="Зацепка создана", created_at=now))
    if data.autorun:
        await _do_transition(session, inv, S.researching, user, "Запуск NEWSFLOW")
    await session.commit()
    if data.autorun:
        await _launch_pipeline(session, inv)
    return inv


@router.get("/investigations/{inv_id}", response_model=InvestigationDetail)
async def get_investigation(inv_id: int, user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)):
    """Все роли. allowed_transitions = workflow.available_transitions(user.role, status)."""
    inv = await _get_inv(session, inv_id)
    out = InvestigationDetail.model_validate(inv)
    out.allowed_transitions = workflow.available_transitions(_role(user), inv.status)
    return out


@router.post("/investigations/{inv_id}/run", response_model=InvestigationOut)
async def run_investigation(inv_id: int, user: User = Depends(require_role(P)),
                            session: AsyncSession = Depends(get_session)):
    """Producer. new|report_ready -> researching, запуск пайплайна. 409 если уже идёт."""
    inv = await _get_inv(session, inv_id)
    if inv.status == S.researching:
        raise HTTPException(http.HTTP_409_CONFLICT, "Исследование уже идёт")
    if inv.status not in (S.new, S.report_ready):
        raise HTTPException(http.HTTP_409_CONFLICT, f"Нельзя запустить из статуса «{S(inv.status).label}»")
    await _do_transition(session, inv, S.researching, user, "Запуск NEWSFLOW")
    await session.commit()
    await _launch_pipeline(session, inv)
    return inv


@router.get("/investigations/{inv_id}/progress", response_model=ProgressOut)
async def get_progress(inv_id: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    """Все роли. Опрос раз в 1–2 с; progress_json + итерации цепочки поиска."""
    inv = await _get_inv(session, inv_id)
    iters = (await session.scalars(
        select(SearchIteration).where(SearchIteration.investigation_id == inv_id).order_by(SearchIteration.n)
    )).all()
    return ProgressOut(
        investigation_id=inv.id,
        status=inv.status,
        progress=inv.progress_json or {},
        iterations=[SearchIterationOut.model_validate(i) for i in iters],
    )


@router.get("/investigations/{inv_id}/iterations", response_model=list[SearchIterationOut])
async def list_iterations(inv_id: int, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    await _get_inv(session, inv_id)
    return (await session.scalars(
        select(SearchIteration).where(SearchIteration.investigation_id == inv_id).order_by(SearchIteration.n)
    )).all()


@router.get("/investigations/{inv_id}/sources", response_model=list[SourceOut])
async def list_sources(
    inv_id: int,
    kind: SourceKind | None = None,
    primary_only: bool = False,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Все роли. duplicates_count = размер кластера − 1. Сортировка: primary, uniqueness desc."""
    await _get_inv(session, inv_id)
    all_sources = (await session.scalars(select(Source).where(Source.investigation_id == inv_id))).all()
    cluster_sizes = Counter(s.cluster_id for s in all_sources if s.cluster_id is not None)
    items = [
        s for s in all_sources
        if (kind is None or s.kind == kind) and (not primary_only or s.is_primary)
    ]
    items.sort(key=lambda s: (not s.is_primary, -(s.uniqueness_score if s.uniqueness_score is not None else -1), s.id))
    out = []
    for s in items:
        o = SourceOut.model_validate(s)
        o.duplicates_count = max(cluster_sizes.get(s.cluster_id, 1) - 1, 0) if s.cluster_id is not None else 0
        out.append(o)
    return out


@router.get("/investigations/{inv_id}/contacts", response_model=list[ContactOut])
async def list_contacts(inv_id: int, user: User = Depends(require_role(P)),
                        session: AsyncSession = Depends(get_session)):
    """Producer/Operator ONLY. «Кому позвонить»: sources kind=max_chat, is_primary,
    uniqueness>=0.5, is_important + author_profiles (phone может быть None)."""
    from app.integrations.max.poller import ensure_author_profile

    await _get_inv(session, inv_id)
    sources = (await session.scalars(
        select(Source).where(
            Source.investigation_id == inv_id,
            Source.kind == SourceKind.max_chat,
            Source.is_primary.is_(True),
            Source.is_important.is_(True),
            Source.uniqueness_score >= 0.5,
            Source.chat_message_id.is_not(None),
        )
    )).all()
    best: dict[str, tuple[Source, ChatMessage]] = {}
    for s in sources:
        msg = s.chat_message
        if msg is None:
            continue
        cur = best.get(msg.author_external_id)
        if cur is None or (s.uniqueness_score or 0) > (cur[0].uniqueness_score or 0):
            best[msg.author_external_id] = (s, msg)
    chat_titles = dict((await session.execute(select(MaxChat.id, MaxChat.title))).all())
    out: list[ContactOut] = []
    touched = False
    for author_id, (s, msg) in best.items():
        prof = await session.get(AuthorProfile, author_id)
        if prof is None:
            prof = await ensure_author_profile(session, author_id)
            touched = touched or prof is not None
        out.append(ContactOut(
            source_id=s.id,
            author_external_id=author_id,
            name=(prof.name if prof else None) or msg.author_name,
            phone=prof.phone if prof else None,
            username=prof.username if prof else None,
            quote=msg.text or s.snippet or "",
            chat_title=chat_titles.get(msg.chat_id),
            sent_at=msg.sent_at,
            message_url=s.url,
            uniqueness_score=s.uniqueness_score,
        ))
    if touched:
        await session.commit()
    # Сначала те, кому можно позвонить, затем по времени сообщения
    out.sort(key=lambda c: (c.phone is None, c.sent_at or utcnow()))
    return out


@router.get("/investigations/{inv_id}/media", response_model=list[MediaItemOut])
async def list_media(inv_id: int, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    await _get_inv(session, inv_id)
    return (await session.scalars(
        select(MediaItem).where(MediaItem.investigation_id == inv_id).order_by(MediaItem.possibly_old, MediaItem.id)
    )).all()


@router.get("/investigations/{inv_id}/report", response_model=ReportOut)
async def get_report(inv_id: int, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    """404 если справки ещё нет."""
    await _get_inv(session, inv_id)
    rep = await session.get(Report, inv_id)
    if rep is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Справка ещё не готова")
    return rep


async def _drafts(session: AsyncSession, inv_id: int) -> list[Draft]:
    return list((await session.scalars(
        select(Draft).where(Draft.investigation_id == inv_id).order_by(Draft.version)
    )).all())


@router.get("/investigations/{inv_id}/drafts", response_model=list[DraftOut])
async def list_drafts(inv_id: int, user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    """Все версии по возрастанию version (v1 — AI)."""
    await _get_inv(session, inv_id)
    return await _drafts(session, inv_id)


@router.post("/investigations/{inv_id}/drafts", response_model=DraftOut, status_code=201)
async def create_draft(inv_id: int, data: DraftCreate, user: User = Depends(require_role(E)),
                       session: AsyncSession = Depends(get_session)):
    """Editor (только в статусе editing, иначе 409). Новая версия author_kind=editor."""
    inv = await _get_inv(session, inv_id)
    if inv.status != S.editing:
        raise HTTPException(http.HTTP_409_CONFLICT, "Править черновик можно только в статусе «На редактуре»")
    versions = await _drafts(session, inv_id)
    prev = versions[-1] if versions else None
    claims = data.claims_json if data.claims_json is not None else list(prev.claims_json or []) if prev else []
    draft = Draft(
        investigation_id=inv_id,
        version=(prev.version + 1) if prev else 1,
        author_kind=DraftAuthorKind.editor,
        author_user_id=user.id,
        title=data.title,
        body=data.body,
        claims_json=[dict(c) for c in claims],
        created_at=utcnow(),
    )
    session.add(draft)
    if inv.editor_id is None and _role(user) == Role.editor:
        inv.editor_id = user.id
    inv.updated_at = utcnow()
    await session.commit()
    await session.refresh(draft)
    return draft


@router.get("/investigations/{inv_id}/drafts/diff", response_model=DraftDiffOut)
async def diff_drafts(
    inv_id: int,
    base: int | None = Query(None, description="версия, по умолчанию 1 (AI)"),
    target: int | None = Query(None, description="версия, по умолчанию последняя"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Пословный diff (difflib) body base vs target."""
    await _get_inv(session, inv_id)
    versions = {d.version: d for d in await _drafts(session, inv_id)}
    if not versions:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Черновиков нет")
    base_v = base if base is not None else min(versions)
    target_v = target if target is not None else max(versions)
    if base_v not in versions or target_v not in versions:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Версия черновика не найдена")
    a, b = versions[base_v].body, versions[target_v].body
    return DraftDiffOut(base_version=base_v, target_version=target_v,
                        ratio=round(draft_similarity(a, b), 4), ops=_diff(a, b))


@router.get("/investigations/{inv_id}/facts", response_model=list[FactMarkOut])
async def list_facts(inv_id: int, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    await _get_inv(session, inv_id)
    return (await session.scalars(
        select(FactMark).where(FactMark.investigation_id == inv_id).order_by(FactMark.created_at, FactMark.id)
    )).all()


@router.post("/investigations/{inv_id}/facts", response_model=FactMarkOut, status_code=201)
async def mark_fact(inv_id: int, data: FactMarkIn, user: User = Depends(require_role(P, E)),
                    session: AsyncSession = Depends(get_session)):
    """Producer/Editor: подтверждён / не подтверждён / ложь. Повторная отметка того же
    claim_id тем же пользователем — обновление."""
    await _get_inv(session, inv_id)
    mark = None
    if data.claim_id:
        mark = await session.scalar(select(FactMark).where(
            FactMark.investigation_id == inv_id, FactMark.claim_id == data.claim_id,
            FactMark.user_id == user.id,
        ).order_by(FactMark.id.desc()).limit(1))
    if mark is None:
        mark = FactMark(investigation_id=inv_id, claim_id=data.claim_id, user_id=user.id)
        session.add(mark)
    mark.fact_text = data.fact_text
    mark.state = data.state
    mark.comment = data.comment
    mark.created_at = utcnow()  # «последняя отметка» определяет итоговый статус в UI
    await session.commit()
    await session.refresh(mark)
    return mark


@router.post("/investigations/{inv_id}/transition", response_model=InvestigationOut)
async def transition_investigation(inv_id: int, data: TransitionIn, user: User = Depends(current_user),
                                   session: AsyncSession = Depends(get_session)):
    """Любая роль; права — workflow.can_transition (403 / 409)."""
    inv = await _get_inv(session, inv_id)
    if data.to_status == S.editing and data.editor_id is not None:
        editor = await session.get(User, data.editor_id)
        if editor is None or Role(editor.role) != Role.editor:
            raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "editor_id: нет такого редактора")
    await _do_transition(session, inv, data.to_status, user, data.comment)
    if data.to_status == S.editing and data.editor_id is not None:
        inv.editor_id = data.editor_id
    await session.commit()
    if data.to_status == S.researching:  # перезапуск исследования
        await _launch_pipeline(session, inv)
    return inv


@router.get("/investigations/{inv_id}/history", response_model=list[StatusHistoryOut])
async def list_history(inv_id: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    await _get_inv(session, inv_id)
    return (await session.scalars(
        select(StatusHistory).where(StatusHistory.investigation_id == inv_id)
        .order_by(StatusHistory.created_at, StatusHistory.id)
    )).all()


@router.post("/investigations/{inv_id}/feedback", response_model=FeedbackOut, status_code=201)
async def add_feedback(inv_id: int, data: FeedbackIn, user: User = Depends(require_role(P)),
                       session: AsyncSession = Depends(get_session)):
    """Producer: «норм / стрём» + комментарий."""
    await _get_inv(session, inv_id)
    fb = Feedback(investigation_id=inv_id, user_id=user.id, verdict=data.verdict,
                  comment=(data.comment or "").strip() or None, created_at=utcnow())
    session.add(fb)
    await session.commit()
    await session.refresh(fb, attribute_names=["user"])
    return fb


# ================================================================ AI-оператор
@router.get("/feedback", response_model=list[FeedbackOut])
async def list_feedback(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    """Operator: лента обратной связи (created_at desc)."""
    return (await session.scalars(select(Feedback).order_by(Feedback.created_at.desc(), Feedback.id.desc()))).all()


@router.get("/operator/board", response_model=BoardOut)
async def operator_board(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    """Канбан всех статусов + зависшие (STALE_AFTER_MINUTES)."""
    invs = (await session.scalars(select(Investigation).order_by(Investigation.updated_at.desc()))).all()
    last_change = await _history_last_change(session)
    now = utcnow()
    columns: dict[S, list[BoardCard]] = {s: [] for s in S}
    stale: list[BoardCard] = []
    for inv in invs:
        st = S(inv.status)
        since = last_change.get(inv.id) or inv.updated_at or inv.created_at
        age = max(int((now - since).total_seconds() // 60), 0)
        who = _RESPONSIBLE.get(st)
        responsible = inv.producer if who == "producer" else inv.editor if who == "editor" else None
        card = BoardCard(
            id=inv.id, title=inv.title, status=st,
            responsible=UserOut.model_validate(responsible) if responsible else None,
            updated_at=inv.updated_at, age_minutes=age,
            is_stale=st not in _FINAL and age > settings.STALE_AFTER_MINUTES,
        )
        columns[st].append(card)
        if card.is_stale:
            stale.append(card)
    stale.sort(key=lambda c: -c.age_minutes)
    return BoardOut(columns=columns, stale=stale)


@router.get("/metrics", response_model=MetricsOut)
async def get_metrics(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    """services.metrics.compute_metrics."""
    from app.services.metrics import compute_metrics

    return MetricsOut(**await compute_metrics(session))


async def _chat_out(session: AsyncSession, chats: list[MaxChat]) -> list[MaxChatOut]:
    counts = dict((await session.execute(
        select(ChatMessage.chat_id, func.count()).group_by(ChatMessage.chat_id)
    )).all())
    out = []
    for c in chats:
        o = MaxChatOut.model_validate(c)
        o.messages_count = counts.get(c.id, 0)
        out.append(o)
    return out


@router.get("/max/chats", response_model=list[MaxChatOut])
async def list_max_chats(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    chats = (await session.scalars(select(MaxChat).order_by(MaxChat.id))).all()
    return await _chat_out(session, list(chats))


@router.patch("/max/chats/{chat_id}", response_model=MaxChatOut)
async def update_max_chat(chat_id: int, data: MaxChatUpdate, user: User = Depends(require_role(OP)),
                          session: AsyncSession = Depends(get_session)):
    """Вкл/выкл чат и/или смена района (district)."""
    chat = await session.get(MaxChat, chat_id)
    if chat is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Чат не найден")
    if data.enabled is not None:
        chat.enabled = data.enabled
    if data.district is not None:
        chat.district = data.district.strip() or None
    await session.commit()
    return (await _chat_out(session, [chat]))[0]


@router.post("/max/sync", response_model=SyncResult)
async def sync_max(user: User = Depends(require_role(OP)), session: AsyncSession = Depends(get_session)):
    """«Синхронизировать сейчас»: poller.sync_once."""
    from app.integrations.max.poller import sync_once

    return await sync_once(session)
