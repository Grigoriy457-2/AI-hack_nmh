"""Метрики эффективности для AI-оператора (п.9.6 ТЗ). Зона DEV-BACKEND.

compute_metrics возвращает dict с ключами METRIC_KEYS (совпадает со schemas.MetricsOut).
Доли — float 0..1, None если нет данных.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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


async def compute_metrics(session: AsyncSession) -> dict[str, Any]:
    raise NotImplementedError("DEV-BACKEND: compute_metrics")
