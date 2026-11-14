"""Статистика (экран /stats): передано/отклонено, время сигнал→передача, traceability.

``compute_stats(session) -> dict`` — ключи совпадают со ``schemas.StatsOut``. Доли 0..1, None — нет данных.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ACTIVE_STATUSES,
    HANDOFF_CHANNELS,
    Draft,
    DraftAuthorKind,
    FactMark,
    FactState,
    Investigation,
    InvestigationStatus,
    Report,
    Source,
)
from app.services.metrics import MIN_PRIMARY_SOURCES, MIN_SOURCE_KINDS, SMALL_DIFF_RATIO, draft_similarity

S = InvestigationStatus


def _share(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


async def compute_stats(session: AsyncSession) -> dict[str, Any]:
    invs = (await session.scalars(select(Investigation))).all()
    by_status = {s.value: 0 for s in S}
    for inv in invs:
        by_status[S(inv.status).value] += 1

    def _signal(inv: Investigation):
        return inv.signal_at or inv.created_at

    to_handoff = [(inv.handed_off_at - _signal(inv)).total_seconds() for inv in invs
                  if inv.handed_off_at and _signal(inv) and inv.handed_off_at >= _signal(inv)]
    to_review = [(inv.review_at - _signal(inv)).total_seconds() for inv in invs
                 if inv.review_at and _signal(inv) and inv.review_at >= _signal(inv)]

    # Source Coverage
    report_ids = set((await session.scalars(select(Report.investigation_id))).all())
    primaries: dict[int, int] = defaultdict(int)
    kinds: dict[int, set[str]] = defaultdict(set)
    for inv_id, kind, is_primary in (await session.execute(
        select(Source.investigation_id, Source.kind, Source.is_primary)
    )).all():
        kinds[inv_id].add(str(getattr(kind, "value", kind)))
        if is_primary:
            primaries[inv_id] += 1
    covered = sum(1 for i in report_ids if primaries[i] >= MIN_PRIMARY_SOURCES and len(kinds[i]) >= MIN_SOURCE_KINDS)

    # Traceability / acceptance
    drafts_by_inv: dict[int, list[Draft]] = defaultdict(list)
    for d in (await session.scalars(select(Draft).order_by(Draft.investigation_id, Draft.version))).all():
        drafts_by_inv[d.investigation_id].append(d)
    claims_total = claims_traced = 0
    for versions in drafts_by_inv.values():
        for c in versions[-1].claims_json or []:
            claims_total += 1
            if c.get("source_ids"):
                claims_traced += 1
    status_of = {inv.id: S(inv.status) for inv in invs}
    accepted = considered = 0
    for inv_id, versions in drafts_by_inv.items():
        if status_of.get(inv_id) != S.handed_off:
            continue
        ai = [d for d in versions if DraftAuthorKind(d.author_kind) == DraftAuthorKind.ai]
        if not ai:
            continue
        considered += 1
        final = versions[-1]
        base = max((d for d in ai if d.version <= final.version), key=lambda d: d.version)
        if draft_similarity(base.title + " " + base.body, final.title + " " + final.body) >= SMALL_DIFF_RATIO:
            accepted += 1

    marks = (await session.execute(select(FactMark.state, func.count()).group_by(FactMark.state))).all()
    marks_total = sum(n for _, n in marks)
    corrected = sum(n for st, n in marks if FactState(st) in (FactState.false, FactState.unconfirmed))

    handed = sorted((inv for inv in invs if S(inv.status) == S.handed_off and inv.handed_off_at),
                    key=lambda i: i.handed_off_at, reverse=True)
    by_channel: dict[str, int] = defaultdict(int)
    for inv in handed:
        by_channel[inv.handoff_channel or "other"] += 1
    recent = [{
        "id": inv.id, "title": inv.title, "handoff_to": inv.handoff_to, "handoff_channel": inv.handoff_channel,
        "handoff_channel_label": HANDOFF_CHANNELS.get(inv.handoff_channel or "", inv.handoff_channel),
        "handed_off_at": inv.handed_off_at,
        "signal_to_handoff_sec": round((inv.handed_off_at - _signal(inv)).total_seconds(), 1) if _signal(inv) else None,
    } for inv in handed[:10]]

    return {
        "stories_total": len(invs),
        "by_status": by_status,
        "handed_off": by_status[S.handed_off.value],
        "rejected": by_status[S.rejected.value],
        "in_work": sum(by_status[s.value] for s in ACTIVE_STATUSES),
        "avg_signal_to_handoff_sec": _avg(to_handoff),
        "avg_signal_to_review_sec": _avg(to_review),
        "traceability": _share(claims_traced, claims_total),
        "source_coverage": _share(covered, len(report_ids)),
        "factcheck_correction_rate": _share(corrected, marks_total),
        "draft_acceptance_rate": _share(accepted, considered),
        "handoffs_by_channel": dict(by_channel),
        "recent_handoffs": recent,
    }
