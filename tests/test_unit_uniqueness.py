"""Юнит: services/uniqueness.py (п.4 ТЗ)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from app.services import uniqueness as U
from app.services.uniqueness import UniqItem

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 14, 11, 0, 0)

WORDS = ("утром жители дома услышали громкий хлопок затем увидели дым над крышей соседнего "
         "здания приехали пожарные машины полиция скорая перекрыли двор").split()


def words(a: int, b: int) -> str:
    return " ".join(WORDS[a:b])


# ---------------------------------------------------------------- normalize_text
def test_normalize_lowercase_punct_emoji_links():
    raw = "ХЛОПОК!!! В Чертанове 😱🔥 подробности: https://t.me/msk_news/123 ... ЁЛКА, «да»"
    n = U.normalize_text(raw)
    assert n == n.lower()
    for bad in ("!", ":", ",", "«", "»", "😱", "🔥", "http", "t.me", "..."):
        assert bad not in n, f"{bad!r} остался в {n!r}"
    assert "  " not in n and n == n.strip()
    assert "хлопок" in n.split() and "чертанове" in n.split()
    assert "елка" in n.split(), "ё должна заменяться на е"


def test_normalize_equivalent_texts_equal():
    a = "Хлопок у дома 5!!! 😱 https://example.org"
    b = "хлопок   у дома 5"
    assert U.normalize_text(a) == U.normalize_text(b)


def test_normalize_idempotent_and_empty():
    t = "Очевидец: «Я видел вспышку в 14:30» 🔥"
    once = U.normalize_text(t)
    assert U.normalize_text(once) == once
    assert U.normalize_text("") == ""
    assert U.normalize_text("😱🔥!!! https://x.ru") == ""


# ---------------------------------------------------------------- shingles / jaccard
def test_shingles_word_trigrams():
    sh = U.shingles("раз два три четыре")
    assert isinstance(sh, set)
    assert len(sh) == 2  # «раз два три», «два три четыре»


def test_shingles_normalizes_inside():
    assert U.shingles("Хлопок у ДОМА пять!!! 😱") == U.shingles("хлопок у дома пять")


def test_shingles_short_text_not_empty():
    assert U.shingles("хлопок") != set()
    assert U.shingles("хлопок дом") != set()


def test_shingles_custom_n():
    assert len(U.shingles("a b c d e", n=2)) == 4


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ({"x", "y"}, {"x", "y"}, 1.0),
        ({"x"}, {"y"}, 0.0),
        (set(), set(), 0.0),
        ({"a", "b"}, {"b", "c"}, 1 / 3),
        ({"a", "b", "c", "d"}, {"a", "b", "c"}, 0.75),
    ],
)
def test_jaccard(a, b, expected):
    assert U.jaccard(a, b) == pytest.approx(expected)


# ---------------------------------------------------------------- кластеризация
def _demo_items() -> list[UniqItem]:
    original = "Я видел вспышку у дома 5 по Варшавскому шоссе в 14:30, выбило окна"
    return [
        UniqItem(id="unrelated", text="Продаю детскую коляску недорого, самовывоз из Бирюлёво",
                 sent_at=T0 - timedelta(hours=1)),
        UniqItem(id="orig", text=original, sent_at=T0),
        UniqItem(id="copy", text="‼️ " + original.upper() + " 😱", sent_at=T0 + timedelta(minutes=5)),
        UniqItem(id="fwd", text=original + " https://t.me/x", sent_at=T0 + timedelta(minutes=10),
                 is_forwarded=True),
        UniqItem(id="other", text="Двое пострадавших, приехали две скорые и пожарные",
                 sent_at=T0 + timedelta(minutes=3)),
    ]


def _by_member(clusters):
    return {it.id: c for c in clusters for it in c.items}


def test_cluster_duplicates_and_forwards_together():
    items = _demo_items()
    random.Random(1).shuffle(items)
    clusters = U.cluster_messages(items)
    assert len(clusters) == 3
    m = _by_member(clusters)
    assert m["orig"] is m["copy"] is m["fwd"]
    assert m["other"] is not m["orig"] and m["unrelated"] is not m["orig"]
    assert sum(len(c.items) for c in clusters) == len(items)


def test_cluster_primary_is_earliest_and_items_sorted():
    items = _demo_items()
    items.reverse()
    clusters = U.cluster_messages(items)
    dup = _by_member(clusters)["orig"]
    assert dup.primary.id == "orig"
    times = [i.sent_at for i in dup.items]
    assert times == sorted(times)


def test_cluster_ids_ordered_by_primary_time():
    clusters = U.cluster_messages(_demo_items())
    ids = sorted(c.cluster_id for c in clusters)
    assert ids == list(range(1, len(clusters) + 1))
    by_id = sorted(clusters, key=lambda c: c.cluster_id)
    prim_times = [c.primary.sent_at for c in by_id]
    assert prim_times == sorted(prim_times)
    assert _by_member(clusters)["unrelated"].cluster_id == 1


def test_cluster_threshold_boundary():
    a = UniqItem(id="a", text=words(0, 10), sent_at=T0)            # 8 шинглов
    b = UniqItem(id="b", text=words(0, 12), sent_at=T0 + timedelta(minutes=1))  # 10, J=0.8
    assert len(U.cluster_messages([a, b], threshold=0.6)) == 1
    assert len(U.cluster_messages([a, b], threshold=0.9)) == 2


def test_cluster_transitive_union_find():
    a = UniqItem(id="a", text=words(0, 10), sent_at=T0)                           # 8 шинглов
    b = UniqItem(id="b", text=words(0, 12), sent_at=T0 + timedelta(minutes=1))    # J(a,b)=0.8
    c = UniqItem(id="c", text=words(0, 17), sent_at=T0 + timedelta(minutes=2))    # J(b,c)=0.67, J(a,c)=0.53
    assert U.jaccard(U.shingles(a.text), U.shingles(c.text)) < 0.6
    clusters = U.cluster_messages([c, a, b])
    assert len(clusters) == 1
    assert clusters[0].primary.id == "a"


def test_cluster_none_time_goes_last_and_tie_by_input_order():
    text = "Хлопок у дома пять на Варшавском шоссе около половины третьего"
    x = UniqItem(id="x", text=text, sent_at=None)
    y = UniqItem(id="y", text=text, sent_at=T0)
    z = UniqItem(id="z", text=text, sent_at=T0)
    clusters = U.cluster_messages([x, y, z])
    assert len(clusters) == 1
    assert clusters[0].primary.id == "y"
    assert clusters[0].items[-1].id == "x"


def test_forwarded_is_not_primary_when_original_present():
    text = "Хлопок у дома пять на Варшавском шоссе около половины третьего"
    fwd = UniqItem(id="fwd", text=text, sent_at=T0, is_forwarded=True)
    orig = UniqItem(id="orig", text=text, sent_at=T0)
    clusters = U.cluster_messages([fwd, orig])
    assert len(clusters) == 1
    assert clusters[0].primary.id == "orig"


def test_cluster_empty_input():
    assert U.cluster_messages([]) == []


# ---------------------------------------------------------------- importance
@pytest.mark.parametrize(
    "text,media,expected",
    [
        ("Я видел вспышку у дома 5, было около 14:30", False, True),
        ("В 14:35 приехали две скорые", False, True),
        ("Двое пострадавших, их увезли", False, True),
        ("вот фото", True, True),
        ("Всем добрый вечер! Кто продаёт коляску?", False, False),
        ("Поздравляем с днём рождения соседку 🎉", False, False),
    ],
)
def test_is_important_heuristic(text, media, expected):
    assert U.is_important_heuristic(text, has_media=media) is expected


# ---------------------------------------------------------------- score_uniqueness
def test_score_uniqueness_basic():
    items = _demo_items()
    res = U.score_uniqueness(items)
    assert set(res) == {i.id for i in items}
    for r in res.values():
        assert 0.0 <= r.uniqueness_score <= 1.0
        assert r.uniqueness_score == pytest.approx(1.0 - r.max_similarity)
    # самый ранний во всей выборке
    assert res["unrelated"].uniqueness_score == pytest.approx(1.0)
    assert res["orig"].is_primary and res["orig"].duplicate_of is None
    assert res["orig"].uniqueness_score == pytest.approx(1.0)
    for d in ("copy", "fwd"):
        assert not res[d].is_primary
        assert res[d].duplicate_of == "orig"
        assert res[d].cluster_id == res["orig"].cluster_id
        assert res[d].uniqueness_score < 0.5
    assert res["copy"].uniqueness_score == pytest.approx(0.0)
    assert res["other"].is_primary and res["other"].uniqueness_score > 0.5


def test_score_uniqueness_partial_similarity():
    a = UniqItem(id="a", text=words(0, 10), sent_at=T0)
    b = UniqItem(id="b", text=words(0, 12), sent_at=T0 + timedelta(minutes=1))
    res = U.score_uniqueness([b, a])
    assert res["a"].uniqueness_score == pytest.approx(1.0)
    assert res["b"].max_similarity == pytest.approx(0.8)
    assert res["b"].uniqueness_score == pytest.approx(0.2)


def test_score_uniqueness_compares_only_with_earlier():
    """Позднее похожее сообщение не снижает уникальность раннего."""
    a = UniqItem(id="a", text=words(0, 12), sent_at=T0)
    b = UniqItem(id="b", text=words(0, 12), sent_at=T0 + timedelta(minutes=1))
    res = U.score_uniqueness([b, a])
    assert res["a"].uniqueness_score == pytest.approx(1.0)
    assert res["b"].uniqueness_score == pytest.approx(0.0)


def test_score_uniqueness_importance_heuristic_and_override():
    items = [
        UniqItem(id=1, text="Я видел вспышку у дома 5 в 14:30", sent_at=T0),
        UniqItem(id=2, text="Кто продаёт коляску?", sent_at=T0 + timedelta(minutes=1)),
    ]
    res = U.score_uniqueness(items)
    assert res[1].is_important is True
    assert res[2].is_important is False
    res2 = U.score_uniqueness(items, importance={1: False, 2: True})
    assert res2[1].is_important is False
    assert res2[2].is_important is True


def test_score_uniqueness_accepts_precomputed_clusters():
    items = _demo_items()
    clusters = U.cluster_messages(items)
    res = U.score_uniqueness(items, clusters=clusters)
    m = _by_member(clusters)
    for iid, r in res.items():
        assert r.cluster_id == m[iid].cluster_id


# ---------------------------------------------------------------- select_unique_important
def test_select_unique_important():
    R = U.UniquenessResult
    results = {
        "ok": R("ok", 1, True, 1.0, 0.0, None, True),
        "boundary": R("boundary", 2, True, 0.5, 0.5, None, True),
        "low": R("low", 3, True, 0.49, 0.51, None, True),
        "dup": R("dup", 1, False, 0.9, 0.1, "ok", True),
        "unimportant": R("unimportant", 4, True, 1.0, 0.0, None, False),
    }
    sel = U.select_unique_important(results)
    assert set(sel) == {"ok", "boundary"}
    assert set(U.select_unique_important(results, min_uniqueness=0.95)) == {"ok"}


def test_end_to_end_unique_selection():
    items = _demo_items()
    res = U.score_uniqueness(items)
    sel = set(U.select_unique_important(res))
    assert "orig" in sel and "other" in sel
    assert not sel & {"copy", "fwd", "unrelated"}
