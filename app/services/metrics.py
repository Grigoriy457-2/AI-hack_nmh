"""Помощники метрик: близость версий черновика (draft_similarity) и токенизация для diff.
Статистика v2 — services/stats.py (compute_stats).
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

SMALL_DIFF_RATIO = 0.85  # SequenceMatcher.ratio(AI v1, финальная правка) >= -> «принят»

METRIC_KEYS: dict[str, str] = {
    "avg_signal_to_handoff_sec": "Среднее время от первого сигнала до «Передано дальше», сек",
    "avg_signal_to_review_sec": "Среднее время от сигнала до готовой справки, сек",
    "traceability": "Traceability: доля утверждений последних черновиков с непустыми source_ids",
    "source_coverage": "Source Coverage: доля сюжетов со справкой, где ≥3 первоисточника и ≥2 типа источников",
    "factcheck_correction_rate": "Доля отметок фактов «не подтверждён / ложь»",
    "draft_acceptance_rate": "Доля переданных сюжетов, где правка близка к AI-версии (ratio ≥ 0.85)",
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
    """Совместимость v1: то же, что services.stats.compute_stats."""
    from app.services.stats import compute_stats

    return await compute_stats(session)
