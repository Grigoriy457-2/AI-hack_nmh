"""Метрики эффективности для AI-оператора (п.9.6 ТЗ). Зона DEV-BACKEND.

compute_metrics возвращает dict с ключами METRIC_KEYS (совпадает со schemas.MetricsOut).
Доли — float 0..1, None если нет данных.
"""
from __future__ import annotations

import difflib
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Draft,
    DraftAuthorKind,
    FactMark,
    FactState,
    Feedback,
    Investigation,
    InvestigationStatus,
    Report,
    Source,
    Verdict,
)

SMALL_DIFF_RATIO = 0.85  # SequenceMatcher.ratio(AI v1, финальная правка) >= -> «принят»

METRIC_KEYS: dict[str, str] = {
    "time_to_draft_avg_sec": "Time to Draft: среднее (report_ready_at − created_at), сек",
    "source_coverage": "Source Coverage: доля расследований (с отчётом), где ≥3 первоисточника "
                       "и ≥2 типа источников (kind)",
    "traceability": "Traceability: доля claims в последних черновиках с непустыми source_ids",
    "draft_acceptance_rate": "Draft Acceptance Rate: доля расследований, где финальная правка "
                             "редактора близка к AI-версии (ratio ≥ SMALL_DIFF_RATIO); без правок = принят",
    "factcheck_correction_rate": "Fact-check correction rate: доля fact_marks со state false/unconfirmed",
    "feedback_good_share": "Доля «норм» (good) во всём фидбэке",
    "investigations_total": "Всего расследований",
    "by_status": "Количество по статусам {status: n} (все статусы, включая 0)",
    "feedback_total": "Всего отзывов",
}


MIN_PRIMARY_SOURCES = 3
MIN_SOURCE_KINDS = 2
_TOKEN_RE = re.compile(r"\s+|[^\s]+")


def tokenize(text: str) -> list[str]:
    """Слова и пробельные промежутки (склейка токенов == исходный текст)."""
    return _TOKEN_RE.findall(text or "")


def draft_similarity(a: str, b: str) -> float:
    """Пословная близость двух версий черновика (difflib ratio, 1.0 = без правок)."""
    wa = [t for t in tokenize(a) if not t.isspace()]
    wb = [t for t in tokenize(b) if not t.isspace()]
    if not wa and not wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def _share(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


async def compute_metrics(session: AsyncSession) -> dict[str, Any]:
    invs = (await session.scalars(select(Investigation))).all()

    # --- по статусам
    by_status = {s.value: 0 for s in InvestigationStatus}
    for inv in invs:
        by_status[InvestigationStatus(inv.status).value] += 1

    # --- Time to Draft
    durations = [
        (inv.report_ready_at - inv.created_at).total_seconds()
        for inv in invs
        if inv.report_ready_at and inv.created_at and inv.report_ready_at >= inv.created_at
    ]
    time_to_draft = round(sum(durations) / len(durations), 1) if durations else None

    # --- Source Coverage (только расследования со справкой)
    report_ids = set((await session.scalars(select(Report.investigation_id))).all())
    primaries: dict[int, int] = defaultdict(int)
    kinds: dict[int, set[str]] = defaultdict(set)
    for inv_id, kind, is_primary in (
        await session.execute(select(Source.investigation_id, Source.kind, Source.is_primary))
    ).all():
        kinds[inv_id].add(str(getattr(kind, "value", kind)))
        if is_primary:
            primaries[inv_id] += 1
    covered = sum(
        1 for i in report_ids
        if primaries[i] >= MIN_PRIMARY_SOURCES and len(kinds[i]) >= MIN_SOURCE_KINDS
    )
    source_coverage = _share(covered, len(report_ids))

    # --- черновики
    drafts_by_inv: dict[int, list[Draft]] = defaultdict(list)
    for d in (await session.scalars(select(Draft).order_by(Draft.investigation_id, Draft.version))).all():
        drafts_by_inv[d.investigation_id].append(d)

    # Traceability: claims последней версии с непустыми source_ids
    claims_total = claims_traced = 0
    for versions in drafts_by_inv.values():
        for c in versions[-1].claims_json or []:
            claims_total += 1
            if c.get("source_ids"):
                claims_traced += 1
    traceability = _share(claims_traced, claims_total)

    # Draft Acceptance Rate: расследования, прошедшие редактуру (ready/published) или с правками
    status_of = {inv.id: InvestigationStatus(inv.status) for inv in invs}
    done = {InvestigationStatus.ready, InvestigationStatus.published}
    accepted = considered = 0
    for inv_id, versions in drafts_by_inv.items():
        ai = next((d for d in versions if DraftAuthorKind(d.author_kind) == DraftAuthorKind.ai), None)
        if ai is None:
            continue
        edits = [d for d in versions if DraftAuthorKind(d.author_kind) == DraftAuthorKind.editor]
        if not edits and status_of.get(inv_id) not in done:
            continue  # ещё не отредактировано — не учитываем
        considered += 1
        final = edits[-1] if edits else ai
        ratio = draft_similarity(ai.title + " " + ai.body, final.title + " " + final.body)
        if ratio >= SMALL_DIFF_RATIO:
            accepted += 1
    draft_acceptance = _share(accepted, considered)

    # Fact-check correction rate
    marks = (await session.execute(select(FactMark.state, func.count()).group_by(FactMark.state))).all()
    marks_total = sum(n for _, n in marks)
    corrected = sum(n for st, n in marks if FactState(st) in (FactState.false, FactState.unconfirmed))
    factcheck = _share(corrected, marks_total)

    # Feedback
    fb = (await session.execute(select(Feedback.verdict, func.count()).group_by(Feedback.verdict))).all()
    fb_total = sum(n for _, n in fb)
    fb_good = sum(n for v, n in fb if Verdict(v) == Verdict.good)

    return {
        "time_to_draft_avg_sec": time_to_draft,
        "source_coverage": source_coverage,
        "traceability": traceability,
        "draft_acceptance_rate": draft_acceptance,
        "factcheck_correction_rate": factcheck,
        "feedback_good_share": _share(fb_good, fb_total),
        "investigations_total": len(invs),
        "by_status": by_status,
        "feedback_total": fb_total,
    }
