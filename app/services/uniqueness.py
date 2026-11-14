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

Уточнения реализации:
- Пересылка (``is_forwarded``) не становится первоисточником, если в кластере есть не-пересылка;
  тогда первоисточник — самая ранняя НЕ пересылка.
- Для первоисточника кластера сходство считается только с более ранними сообщениями ДРУГИХ
  кластеров (они по определению < threshold), чтобы первоисточник не «обесценивался» более
  ранней пересылкой самого себя.
"""
from __future__ import annotations

import re
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


# ---------------------------------------------------------------- нормализация
_URL_RE = re.compile(r"(?:https?://|www\.|max://|t\.me/)\S+", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """lowercase, ё->е, без ссылок/эмодзи/пунктуации, одиночные пробелы."""
    if not text:
        return ""
    t = text.lower().replace("ё", "е")
    t = _URL_RE.sub(" ", t)
    t = _NON_WORD_RE.sub(" ", t)  # эмодзи и пунктуация не входят в \w
    t = t.replace("_", " ")
    return _SPACES_RE.sub(" ", t).strip()


def shingles(text: str, n: int = 3) -> set[str]:
    """Принимает СЫРОЙ текст (нормализует внутри)."""
    words = normalize_text(text).split()
    if not words:
        return set()
    if len(words) < n:
        return {" ".join(words)}
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# ---------------------------------------------------------------- кластеры
def _order(items: Sequence[UniqItem]) -> list[int]:
    """Индексы items по возрастанию времени (None — в конец, tie — порядок во входе)."""
    return sorted(
        range(len(items)),
        key=lambda i: (items[i].sent_at is None, items[i].sent_at or datetime.min, i),
    )


class _DSU:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _cluster_indices(
    items: Sequence[UniqItem], sh: list[set[str]], threshold: float
) -> list[tuple[list[int], int]]:
    """[(индексы кластера по времени, индекс первоисточника)] в порядке времени первоисточника."""
    n = len(items)
    dsu = _DSU(n)
    for i in range(n):
        if not sh[i]:
            continue
        for j in range(i + 1, n):
            if sh[j] and jaccard(sh[i], sh[j]) >= threshold:
                dsu.union(i, j)
    rank = {idx: pos for pos, idx in enumerate(_order(items))}
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(dsu.find(i), []).append(i)
    out: list[tuple[list[int], int]] = []
    for members in groups.values():
        members.sort(key=lambda i: rank[i])
        originals = [i for i in members if not items[i].is_forwarded]
        primary = originals[0] if originals else members[0]
        out.append((members, primary))
    out.sort(key=lambda g: rank[g[1]])
    return out


def cluster_messages(
    items: Sequence[UniqItem], threshold: float = DUPLICATE_THRESHOLD
) -> list[Cluster]:
    sh = [shingles(it.text) for it in items]
    return [
        Cluster(cluster_id=cid, items=[items[i] for i in members], primary=items[primary])
        for cid, (members, primary) in enumerate(_cluster_indices(items, sh, threshold), start=1)
    ]


# ---------------------------------------------------------------- важность
_TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b|\bв\s+\d{1,2}\s*(?:час|ч\b)")
_PLACE_RE = re.compile(
    r"\b(?:ул\.|улиц\w*|шоссе|ш\.|проспект\w*|пр-т|переул\w*|проезд\w*|бульвар\w*|площад\w*|"
    r"д\.\s*\d|дом\w*\s*\d|корп\w*|к\.\s*\d|\d+\s*к\s*\d|подъезд\w*|двор\w*|парковк\w*|стоянк\w*|"
    r"этаж\w*|квартир\w*|станци\w*|метро|у\s+дома|возле\s+дома|перекрест\w*)",
    re.IGNORECASE,
)
_CASUALTY_RE = re.compile(
    r"(?:пострадавш\w*|пострадал\w*|ранен\w*|погиб\w*|госпитализ\w*|эвакуир\w*|"
    r"скор\w*\s+помощ\w*|увезл\w*\s+на\s+скорой|жертв\w*)",
    re.IGNORECASE,
)
_WITNESS_RE = re.compile(
    r"\b(?:видел\w*|видела|вижу|слышал\w*|слышу|своими\s+глазами|из\s+окна|с\s+балкона|"
    r"сам\w*\s+видел\w*|снял\w*|сфотографировал\w*|я\s+там|я\s+рядом|у\s+нас\s+во\s+дворе|"
    r"очевид\w*|смотрел\w*)",
    re.IGNORECASE,
)
_MEDIA_RE = re.compile(r"\b(?:фото\w*|видео\w*|снимок|снимк\w*|запис\w*\s+с\s+камер)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+\b")
_SERVICES_RE = re.compile(
    r"(?:полици\w*|пожарн\w*|мчс|скор\w*|эвакуатор\w*|оцеп\w*|перекрыл\w*|мосгаз\w*|газовщик\w*|"
    r"росгварди\w*|сирен\w*|спасател\w*)",
    re.IGNORECASE,
)


def importance_signals(text: str, has_media: bool = False) -> dict[str, bool]:
    """Набор признаков «новых фактов» (для UI/отладки и эвристики)."""
    t = (text or "").lower().replace("ё", "е")
    return {
        "time": bool(_TIME_RE.search(t)),
        "place": bool(_PLACE_RE.search(t)),
        "casualties": bool(_CASUALTY_RE.search(t)),
        "witness": bool(_WITNESS_RE.search(t)),
        "media": bool(has_media or _MEDIA_RE.search(t)),
        "numbers": bool(_NUMBER_RE.search(t)),
        "services": bool(_SERVICES_RE.search(t)),
    }


def is_important_heuristic(text: str, has_media: bool = False) -> bool:
    """Новые факты: время (14:30), место/адрес, числа пострадавших, фото/видео, «я видел/слышал».

    Важно, если есть медиа / свидетельство очевидца / сведения о пострадавших, либо минимум два
    «фактических» признака из {время, место, числа, экстренные службы}.
    """
    s = importance_signals(text, has_media)
    if s["media"] or s["witness"] or s["casualties"]:
        return True
    return sum((s["time"], s["place"], s["numbers"], s["services"])) >= 2


# ---------------------------------------------------------------- оценка
def score_uniqueness(
    items: Sequence[UniqItem],
    clusters: Sequence[Cluster] | None = None,
    importance: Mapping[Hashable, bool] | None = None,
    threshold: float = DUPLICATE_THRESHOLD,
) -> dict[Hashable, UniquenessResult]:
    """Результат по каждому item.id. ``clusters`` — если уже посчитаны; ``importance`` —
    оценки LLM (иначе эвристика)."""
    if not items:
        return {}
    sh = [shingles(it.text) for it in items]
    if clusters is None:
        clusters = cluster_messages(items, threshold)

    # item.id -> (cluster_id, primary_id)
    cl_of: dict[Hashable, tuple[int, Hashable]] = {}
    for c in clusters:
        for it in c.items:
            cl_of[it.id] = (c.cluster_id, c.primary.id)

    order = _order(items)
    results: dict[Hashable, UniquenessResult] = {}
    for pos, i in enumerate(order):
        it = items[i]
        cid, primary_id = cl_of.get(it.id, (0, it.id))
        is_primary = primary_id == it.id
        max_sim = 0.0
        for j in order[:pos]:
            if is_primary and cl_of.get(items[j].id, (None,))[0] == cid:
                continue
            if sh[i] and sh[j]:
                max_sim = max(max_sim, jaccard(sh[i], sh[j]))
        if importance is not None and it.id in importance:
            important = bool(importance[it.id])
        else:
            important = is_important_heuristic(it.text, bool(it.meta.get("has_media")))
        results[it.id] = UniquenessResult(
            item_id=it.id,
            cluster_id=cid,
            is_primary=is_primary,
            uniqueness_score=round(1.0 - max_sim, 4),
            max_similarity=round(max_sim, 4),
            duplicate_of=None if is_primary else primary_id,
            is_important=important,
        )
    return results


def select_unique_important(
    results: Mapping[Hashable, UniquenessResult], min_uniqueness: float = UNIQUE_THRESHOLD
) -> list[Hashable]:
    """id первоисточников с uniqueness >= min_uniqueness и is_important."""
    return [
        r.item_id
        for r in results.values()
        if r.is_primary and r.uniqueness_score >= min_uniqueness and r.is_important
    ]
