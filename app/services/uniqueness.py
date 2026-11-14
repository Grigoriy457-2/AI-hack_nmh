"""Уникальность сообщений (п.4 ТЗ). Зона DEV-AI. Чистые функции без БД и сети.

Алгоритм:
1. ``normalize_text``: lowercase, ё->е, убрать ссылки, эмодзи, пунктуацию, лишние пробелы.
2. ``shingles``: множества 3-грамм слов (для текстов короче n слов — сами слова/весь текст).
3. ``jaccard``: |A∩B| / |A∪B| (0 для двух пустых).
4. ``cluster_messages``: Jaccard >= threshold (0.6) -> один кластер (union-find, транзитивно).
   Первоисточник кластера (primary) — самое раннее сообщение (sent_at; None — в конец; tie — по
   порядку во входе). cluster_id — 1..N в порядке времени первоисточника.
5. ``score_uniqueness``: uniqueness = 1 − max Jaccard с БОЛЕЕ РАННИМИ сообщениями всей выборки
   (у самого раннего = 1.0). is_important — ``is_important_heuristic`` или значения из ``importance``.
6. ``select_unique_important``: primary & uniqueness >= 0.5 & is_important -> «Кому позвонить».
"""
from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

DUPLICATE_THRESHOLD = 0.6
UNIQUE_THRESHOLD = 0.5


@dataclass(slots=True)
class UniqItem:
    """Входной элемент: сообщение чата или публикация."""

    id: Hashable  # например, Source.id или ChatMessage.id
    text: str
    sent_at: datetime | None = None
    is_forwarded: bool = False  # пересылка не может быть первоисточником при наличии оригинала
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Cluster:
    cluster_id: int
    items: list[UniqItem]  # по возрастанию sent_at
    primary: UniqItem


@dataclass(slots=True)
class UniquenessResult:
    item_id: Hashable
    cluster_id: int
    is_primary: bool
    uniqueness_score: float  # 0..1
    max_similarity: float  # max Jaccard с более ранними
    duplicate_of: Hashable | None  # id первоисточника, если это дубль
    is_important: bool


def normalize_text(text: str) -> str:
    raise NotImplementedError("DEV-AI: normalize_text")


def shingles(text: str, n: int = 3) -> set[str]:
    """Принимает СЫРОЙ текст (нормализует внутри)."""
    raise NotImplementedError("DEV-AI: shingles")


def jaccard(a: set[str], b: set[str]) -> float:
    raise NotImplementedError("DEV-AI: jaccard")


def cluster_messages(
    items: Sequence[UniqItem], threshold: float = DUPLICATE_THRESHOLD
) -> list[Cluster]:
    raise NotImplementedError("DEV-AI: cluster_messages")


def is_important_heuristic(text: str, has_media: bool = False) -> bool:
    """Новые факты: время (14:30), место/адрес, числа пострадавших, фото/видео, «я видел/слышал»."""
    raise NotImplementedError("DEV-AI: is_important_heuristic")


def score_uniqueness(
    items: Sequence[UniqItem],
    clusters: Sequence[Cluster] | None = None,
    importance: Mapping[Hashable, bool] | None = None,
    threshold: float = DUPLICATE_THRESHOLD,
) -> dict[Hashable, UniquenessResult]:
    """Результат по каждому item.id. ``clusters`` — если уже посчитаны; ``importance`` —
    оценки LLM (иначе эвристика)."""
    raise NotImplementedError("DEV-AI: score_uniqueness")


def select_unique_important(
    results: Mapping[Hashable, UniquenessResult], min_uniqueness: float = UNIQUE_THRESHOLD
) -> list[Hashable]:
    """id первоисточников с uniqueness >= min_uniqueness и is_important."""
    raise NotImplementedError("DEV-AI: select_unique_important")
