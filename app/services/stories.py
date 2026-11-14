"""Сюжеты (investigations) — бизнес-логика для api.py и pages.py. См. CONTRACTS_v2 §4.3, §5.

Сервисы бросают ``StoryError(status_code, detail)``; api.py превращает её в HTTPException.
Функции, меняющие данные, делают commit сами.
"""
from __future__ import annotations

import difflib
import logging
from collections import Counter
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    HANDOFF_CHANNELS,
    AuthorProfile,
    Channel,
    ChatMessage,
    Draft,
    DraftAuthorKind,
    FactMark,
    FactState,
    Investigation,
    InvestigationStatus,
    MaxChat,
    Report,
    Source,
    SourceKind,
    StatusHistory,
    User,
    utcnow,
)
from app.schemas import (
    ContactOut,
    DiffOp,
    DraftDiffOut,
    HandoffCheckOut,
    HandoffResultOut,
    InvestigationDetail,
    InvestigationOut,
    SourceOut,
    UnconfirmedItem,
)
from app.services import workflow
from app.services.metrics import draft_similarity, tokenize

log = logging.getLogger("newsflow.stories")
S = InvestigationStatus


class StoryError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------- чтение
async def get_story(session: AsyncSession, inv_id: int) -> Investigation:
    inv = await session.get(Investigation, inv_id)
    if inv is None:
        raise StoryError(404, "Сюжет не найден")
    return inv


async def list_stories(session: AsyncSession, *, status: InvestigationStatus | str | None = None,
                       owner_id: int | None = None, q: str | None = None) -> list[Investigation]:
    stmt = select(Investigation)
    if status:
        stmt = stmt.where(Investigation.status == S(status))
    if owner_id is not None:
        stmt = stmt.where(Investigation.owner_id == owner_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Investigation.title.ilike(like), Investigation.lead_text.ilike(like)))
    stmt = stmt.order_by(Investigation.updated_at.desc(), Investigation.id.desc())
    return list((await session.scalars(stmt)).all())


async def story_detail(session: AsyncSession, inv: Investigation) -> InvestigationDetail:
    out = InvestigationDetail.model_validate(inv)
    out.allowed_transitions = workflow.available_transitions(inv.status)
    check = await handoff_check(session, inv)
    out.unconfirmed_count = len(check.unconfirmed)
    return out


# ---------------------------------------------------------------- создание / запуск
def _title_from(lead: str) -> str:
    first_line = lead.splitlines()[0] if lead else lead
    return first_line if len(first_line) <= 80 else first_line[:79].rstrip() + "…"


async def _transition(session: AsyncSession, inv: Investigation, to: S, user: User | None,
                      comment: str | None = None) -> None:
    try:
        await workflow.transition(session, inv, to, user, comment)
    except workflow.TransitionNotAllowed as e:
        raise StoryError(409, str(e)) from e


async def launch_pipeline(session: AsyncSession, inv: Investigation) -> None:
    """Запуск пайплайна после commit перехода в researching (inline при PIPELINE_INLINE)."""
    from app.services import pipeline

    if settings.PIPELINE_INLINE:
        await pipeline.run_investigation(inv.id)
        await session.refresh(inv)
    else:
        pipeline.start_investigation(inv.id)


async def create_story(session: AsyncSession, user: User | None, lead_text: str, *, title: str | None = None,
                       channel_id: int | None = None, autorun: bool = True, origin: str = "manual",
                       emerging_key: str | None = None, signal_at=None) -> Investigation:
    lead = (lead_text or "").strip()
    if len(lead) < 3:
        raise StoryError(422, "Опишите зацепку (минимум 3 символа)")
    if channel_id is not None and await session.get(Channel, channel_id) is None:
        raise StoryError(422, "Канал не найден")
    now = utcnow()
    inv = Investigation(
        title=((title or "").strip() or _title_from(lead))[:255], lead_text=lead, channel_id=channel_id,
        status=S.new, owner_id=user.id if user else None, origin=origin, emerging_key=emerging_key,
        signal_at=signal_at or now, created_at=now, updated_at=now,
    )
    session.add(inv)
    await session.flush()
    session.add(StatusHistory(investigation_id=inv.id, from_status=None, to_status=S.new,
                              user_id=user.id if user else None,
                              comment="Из возникающего сюжета" if origin == "emerging" else "Своя зацепка",
                              created_at=now))
    if autorun:
        await _transition(session, inv, S.researching, user, "Запуск NEWSFLOW")
    await session.commit()
    if autorun:
        await launch_pipeline(session, inv)
    return inv


async def run_story(session: AsyncSession, inv: Investigation, user: User) -> Investigation:
    if inv.status == S.researching:
        raise StoryError(409, "Поиск уже идёт")
    if inv.status not in (S.new, S.review):
        raise StoryError(409, f"Нельзя запустить поиск из статуса «{S(inv.status).label}»")
    await _transition(session, inv, S.researching, user, "Перезапуск поиска" if inv.status == S.review else "Запуск NEWSFLOW")
    await session.commit()
    await launch_pipeline(session, inv)
    return inv


async def dig_deeper(session: AsyncSession, inv: Investigation, user: User, question: str | None) -> Investigation:
    """«Копать глубже»: вопрос редактора -> ещё итерация CHAIN SEARCH, пересборка справки."""
    if inv.status != S.review:
        raise StoryError(409, "«Копать глубже» доступно, когда справка готова (статус «На проверке»)")
    q = " ".join((question or "").split())[:1000]
    if q:
        inv.deeper_questions = list(inv.deeper_questions or []) + [
            {"question": q, "asked_at": utcnow().isoformat(timespec="seconds"), "user_id": user.id}
        ]
    await _transition(session, inv, S.researching, user, f"Копать глубже: {q}" if q else "Копать глубже")
    await session.commit()
    await launch_pipeline(session, inv)
    return inv


async def stop_story(session: AsyncSession, inv: Investigation, user: User) -> Investigation:
    if inv.status != S.researching:
        raise StoryError(409, "Остановить можно только идущий поиск (статус «AI ищет»)")
    await _transition(session, inv, S.review, user, "Поиск остановлен редактором")
    await session.commit()
    return inv


# ---------------------------------------------------------------- проверка фактов / передача
def _effective_state(item_id: str | None, own: str | None, marks: dict[str, str]) -> str | None:
    if item_id and item_id in marks:
        return marks[item_id]
    return own


async def _latest_marks(session: AsyncSession, inv_id: int) -> dict[str, str]:
    rows = (await session.scalars(
        select(FactMark).where(FactMark.investigation_id == inv_id).order_by(FactMark.created_at, FactMark.id)
    )).all()
    out: dict[str, str] = {}
    for m in rows:
        if m.claim_id:
            out[m.claim_id] = FactState(m.state).value
    return out


async def handoff_check(session: AsyncSession, inv: Investigation) -> HandoffCheckOut:
    marks = await _latest_marks(session, inv.id)
    unconfirmed: list[UnconfirmedItem] = []
    drafts = await list_drafts(session, inv.id)
    if drafts:
        for c in drafts[-1].claims_json or []:
            st = _effective_state(c.get("id"), c.get("status"), marks)
            if st in ("unconfirmed", "false"):
                unconfirmed.append(UnconfirmedItem(id=c.get("id"), text=str(c.get("text") or ""), state=st,
                                                   source_ids=list(c.get("source_ids") or []), origin="draft"))
    rep = await session.get(Report, inv.id)
    if rep is not None:
        for sec in (rep.report_json or {}).get("sections") or []:
            if sec.get("key") not in ("confirmed", "unconfirmed", "contradictions"):
                continue
            for it in sec.get("items") or []:
                own = it.get("state") or ("unconfirmed" if sec.get("key") != "confirmed" else "confirmed")
                st = _effective_state(it.get("id"), own, marks)
                if st in ("unconfirmed", "false"):
                    unconfirmed.append(UnconfirmedItem(id=it.get("id"), text=str(it.get("text") or ""), state=st,
                                                       source_ids=list(it.get("source_ids") or []), origin="report"))
    warnings: list[str] = []
    n_draft = sum(1 for u in unconfirmed if u.origin == "draft")
    n_false = sum(1 for u in unconfirmed if u.state == "false")
    if n_draft:
        warnings.append(f"В черновике {n_draft} неподтверждённых утверждений")
    n_rep = len(unconfirmed) - n_draft
    if n_rep:
        warnings.append(f"В справке {n_rep} неподтверждённых сведений")
    if n_false:
        warnings.append(f"{n_false} утверждений отмечены как ложные")
    if rep is None:
        warnings.append("Справка ещё не готова")
    if not drafts:
        warnings.append("Нет черновика")
    return HandoffCheckOut(ok=not warnings, warnings=warnings, unconfirmed=unconfirmed,
                           has_report=rep is not None, has_draft=bool(drafts))


async def hand_off(session: AsyncSession, inv: Investigation, user: User, *, to: str, channel: str,
                   channel_id: int | None = None, comment: str | None = None) -> HandoffResultOut:
    to = (to or "").strip()
    if not to:
        raise StoryError(422, "Укажите, кому/куда передаёте")
    if channel not in HANDOFF_CHANNELS:
        raise StoryError(422, f"Неизвестный канал «{channel}». Допустимо: {', '.join(HANDOFF_CHANNELS)}")
    if channel_id is not None and await session.get(Channel, channel_id) is None:
        raise StoryError(422, "Канал (channel_id) не найден")
    if inv.status != S.review:
        raise StoryError(409, f"Передать дальше можно из статуса «На проверке» (сейчас «{S(inv.status).label}»)")
    check = await handoff_check(session, inv)
    comment = (comment or "").strip() or None
    hist = f"Передано: {to} ({HANDOFF_CHANNELS[channel]})" + (f". {comment}" if comment else "")
    await _transition(session, inv, S.handed_off, user, hist)
    now = utcnow()
    inv.handoff_to = to[:255]
    inv.handoff_channel = channel
    inv.handoff_channel_id = channel_id
    inv.handoff_comment = comment
    inv.handed_off_at = now
    inv.handed_off_by_id = user.id
    await session.commit()
    await session.refresh(inv)
    return HandoffResultOut(story=InvestigationOut.model_validate(inv), warnings=check.warnings,
                            unconfirmed=check.unconfirmed)


async def reject(session: AsyncSession, inv: Investigation, user: User, comment: str | None = None) -> Investigation:
    if inv.status not in (S.new, S.review):
        raise StoryError(409, f"«Не подтвердилось» недоступно из статуса «{S(inv.status).label}»")
    await _transition(session, inv, S.rejected, user, (comment or "").strip() or "Не подтвердилось")
    await session.commit()
    return inv


async def reopen(session: AsyncSession, inv: Investigation, user: User, comment: str | None = None) -> Investigation:
    if inv.status != S.rejected:
        raise StoryError(409, "Вернуть в работу можно только сюжет «Не подтвердилось»")
    await _transition(session, inv, S.review, user, (comment or "").strip() or "Вернули в работу")
    await session.commit()
    return inv


async def transition(session: AsyncSession, inv: Investigation, to: S, user: User,
                     comment: str | None = None) -> Investigation:
    """Общий переход. researching запускает пайплайн, handed_off — только через hand_off."""
    to = S(to)
    if to == S.handed_off:
        raise StoryError(422, "Для передачи используйте «Передать дальше» (POST /handoff): нужно указать кому/куда")
    if to == S.researching:
        return await run_story(session, inv, user)
    await _transition(session, inv, to, user, comment)
    await session.commit()
    return inv


# ---------------------------------------------------------------- источники / контакты
async def list_sources(session: AsyncSession, inv_id: int, *, kind: SourceKind | None = None,
                       primary_only: bool = False) -> list[SourceOut]:
    all_sources = (await session.scalars(select(Source).where(Source.investigation_id == inv_id))).all()
    cluster_sizes = Counter(s.cluster_id for s in all_sources if s.cluster_id is not None)
    items = [s for s in all_sources if (kind is None or s.kind == kind) and (not primary_only or s.is_primary)]
    items.sort(key=lambda s: (not s.is_primary, -(s.uniqueness_score if s.uniqueness_score is not None else -1), s.id))
    out = []
    for s in items:
        o = SourceOut.model_validate(s)
        o.duplicates_count = max(cluster_sizes.get(s.cluster_id, 1) - 1, 0) if s.cluster_id is not None else 0
        if s.chat_message is not None:
            o.platform = s.chat_message.platform or "max"
        out.append(o)
    return out


async def list_contacts(session: AsyncSession, inv_id: int) -> list[ContactOut]:
    """«Кому позвонить»: первоисточники из чатов (uniqueness ≥ 0.5, важные) + профили авторов."""
    from app.integrations.max.poller import ensure_author_profile

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
    chat_rows = {cid: (title, platform) for cid, title, platform in
                 (await session.execute(select(MaxChat.id, MaxChat.title, MaxChat.platform))).all()}
    out: list[ContactOut] = []
    touched = False
    for author_id, (s, msg) in best.items():
        prof = await session.get(AuthorProfile, author_id)
        if prof is None:
            prof = await ensure_author_profile(session, author_id)
            touched = touched or prof is not None
        title, platform = chat_rows.get(msg.chat_id, (None, "max"))
        out.append(ContactOut(
            source_id=s.id, author_external_id=author_id,
            name=(prof.name if prof else None) or msg.author_name,
            phone=prof.phone if prof else None, username=prof.username if prof else None,
            quote=msg.text or s.snippet or "", chat_title=title, platform=platform or "max",
            sent_at=msg.sent_at, message_url=s.url, uniqueness_score=s.uniqueness_score,
        ))
    if touched:
        await session.commit()
    out.sort(key=lambda c: (c.phone is None, c.sent_at or utcnow()))
    return out


# ---------------------------------------------------------------- черновики / факты / история
async def list_drafts(session: AsyncSession, inv_id: int) -> list[Draft]:
    return list((await session.scalars(
        select(Draft).where(Draft.investigation_id == inv_id).order_by(Draft.version)
    )).all())


EDITABLE = {S.review, S.rejected}


async def save_draft(session: AsyncSession, inv: Investigation, user: User, *, title: str, body: str,
                     claims_json: list[dict[str, Any]] | None = None) -> Draft:
    if inv.status not in EDITABLE:
        raise StoryError(409, "Черновик можно править, когда справка готова (статус «На проверке»)")
    versions = await list_drafts(session, inv.id)
    prev = versions[-1] if versions else None
    claims = claims_json if claims_json is not None else (list(prev.claims_json or []) if prev else [])
    draft = Draft(
        investigation_id=inv.id, version=(prev.version + 1) if prev else 1,
        author_kind=DraftAuthorKind.editor, author_user_id=user.id, title=title, body=body,
        claims_json=[dict(c) for c in claims], created_at=utcnow(),
    )
    session.add(draft)
    inv.updated_at = utcnow()
    await session.commit()
    await session.refresh(draft)
    return draft


def _diff(a: str, b: str) -> list[DiffOp]:
    ta, tb = tokenize(a), tokenize(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return [DiffOp(op=tag, a="".join(ta[i1:i2]), b="".join(tb[j1:j2]))
            for tag, i1, i2, j1, j2 in sm.get_opcodes()]


async def draft_diff(session: AsyncSession, inv_id: int, base: int | None = None,
                     target: int | None = None) -> DraftDiffOut:
    """Пословный diff. base по умолчанию — последняя AI-версия до target (иначе v1)."""
    drafts = await list_drafts(session, inv_id)
    versions = {d.version: d for d in drafts}
    if not versions:
        raise StoryError(404, "Черновиков нет")
    target_v = target if target is not None else max(versions)
    if base is None:
        ai = [d.version for d in drafts if DraftAuthorKind(d.author_kind) == DraftAuthorKind.ai and d.version <= target_v]
        base_v = max(ai) if ai else min(versions)
    else:
        base_v = base
    if base_v not in versions or target_v not in versions:
        raise StoryError(404, "Версия черновика не найдена")
    a, b = versions[base_v].body, versions[target_v].body
    return DraftDiffOut(base_version=base_v, target_version=target_v,
                        ratio=round(draft_similarity(a, b), 4), ops=_diff(a, b))


async def list_facts(session: AsyncSession, inv_id: int) -> list[FactMark]:
    return list((await session.scalars(
        select(FactMark).where(FactMark.investigation_id == inv_id).order_by(FactMark.created_at, FactMark.id)
    )).all())


async def mark_fact(session: AsyncSession, inv_id: int, user: User, *, fact_text: str, state: FactState | str,
                    claim_id: str | None = None, comment: str | None = None) -> FactMark:
    mark = None
    if claim_id:
        mark = await session.scalar(select(FactMark).where(
            FactMark.investigation_id == inv_id, FactMark.claim_id == claim_id,
        ).order_by(FactMark.id.desc()).limit(1))
    if mark is None:
        mark = FactMark(investigation_id=inv_id, claim_id=claim_id)
        session.add(mark)
    mark.user_id = user.id
    mark.fact_text = fact_text
    mark.state = FactState(state)
    mark.comment = comment
    mark.created_at = utcnow()
    inv = await session.get(Investigation, inv_id)
    if inv is not None:
        inv.updated_at = utcnow()
    await session.commit()
    await session.refresh(mark)
    return mark


async def list_history(session: AsyncSession, inv_id: int) -> list[StatusHistory]:
    return list((await session.scalars(
        select(StatusHistory).where(StatusHistory.investigation_id == inv_id)
        .order_by(StatusHistory.created_at, StatusHistory.id)
    )).all())
