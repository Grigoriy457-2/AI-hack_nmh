"""Юнит: services/workflow.py v2 — таблица переходов (user/system), история, flush без commit."""
from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select

from app import db
from app.models import Investigation, InvestigationStatus as S, StatusHistory, User, utcnow
from app.services import workflow as W

pytestmark = pytest.mark.unit

# Эталон из CONTRACTS_v2 §2 (независимо от ALLOWED_TRANSITIONS — ловим молчаливые правки).
SPEC: dict[tuple[S, S], set[str]] = {
    (S.new, S.researching): {"user"},
    (S.new, S.rejected): {"user"},
    (S.researching, S.review): {"user", "system"},
    (S.review, S.researching): {"user"},
    (S.review, S.handed_off): {"user"},
    (S.review, S.rejected): {"user"},
    (S.rejected, S.review): {"user"},
}


def test_statuses_and_labels():
    from app.models import ACTIVE_STATUSES, STATUS_LABELS, STATUS_ORDER

    assert [s.value for s in STATUS_ORDER] == ["new", "researching", "review", "handed_off", "rejected"]
    assert {s.value: STATUS_LABELS[s] for s in S} == {
        "new": "Новый", "researching": "AI ищет", "review": "На проверке",
        "handed_off": "Передано дальше", "rejected": "Не подтвердилось"}
    assert ACTIVE_STATUSES == {S.new, S.researching, S.review}
    assert S.review.label == "На проверке"


def test_handoff_channels():
    from app.models import HANDOFF_CHANNELS

    assert list(HANDOFF_CHANNELS) == ["site", "telegram", "max", "tv", "print", "social", "other"]


def test_no_roles_left():
    import app.auth as auth
    import app.models as models

    for name in ("Role", "ROLE_LABELS", "Feedback", "Verdict"):
        assert not hasattr(models, name), f"app.models.{name} должен быть удалён (v2)"
    for name in ("require_role", "has_role"):
        assert not hasattr(auth, name), f"app.auth.{name} должен быть удалён (v2)"
    assert "role" not in {c.name for c in User.__table__.columns}


def test_table_matches_spec():
    table = {(a, b): set(r) for a, d in W.ALLOWED_TRANSITIONS.items() for b, r in d.items()}
    assert table == SPEC
    assert set(W.ALLOWED_TRANSITIONS) == set(S)


@pytest.mark.parametrize("actor", ["user", "system"])
def test_can_transition_full_matrix(actor):
    wrong = [
        (a.value, b.value, W.can_transition(actor, a, b))
        for a, b in itertools.product(S, S)
        if W.can_transition(actor, a, b) is not (actor in SPEC.get((a, b), set()))
    ]
    assert not wrong, f"{actor}: неверно (from, to, got) {wrong}"


def test_can_transition_accepts_user_object_and_none():
    u = User(id=1, name="x", login="x")
    assert W.can_transition(u, S.review, S.handed_off)
    assert not W.can_transition(None, S.review, S.handed_off), "система не передаёт дальше"
    assert W.can_transition(None, S.researching, S.review)


def test_available_transitions_for_user():
    got = {a: set(W.available_transitions(a)) for a in S}
    assert got == {
        S.new: {S.researching, S.rejected},
        S.researching: {S.review},
        S.review: {S.researching, S.handed_off, S.rejected},
        S.handed_off: set(),
        S.rejected: {S.review},
    }


def test_transition_buttons_cover_table():
    for (a, b) in SPEC:
        assert W.TRANSITION_BUTTONS.get((a, b)), f"нет подписи кнопки для {a.value}->{b.value}"
    assert W.TRANSITION_BUTTONS[(S.review, S.handed_off)] == "Передать дальше"
    assert W.TRANSITION_BUTTONS[(S.review, S.researching)] == "Копать глубже"


async def _mk(session, status: S) -> tuple[Investigation, User]:
    u = User(name="Ред", login=f"ed{status.value}")
    session.add(u)
    await session.flush()
    inv = Investigation(title="t", lead_text="l", status=status, owner_id=u.id)
    session.add(inv)
    await session.flush()
    return inv, u


async def test_transition_writes_history_and_review_at(session):
    inv, u = await _mk(session, S.researching)
    assert inv.review_at is None
    before = utcnow()
    h = await W.transition(session, inv, S.review, None, "готово")
    assert inv.status == S.review and inv.review_at is not None and inv.review_at >= before
    assert h.from_status == S.researching and h.to_status == S.review and h.user_id is None
    first_review = inv.review_at
    await W.transition(session, inv, S.researching, u, "копать глубже")
    await W.transition(session, inv, S.review, None)
    assert inv.review_at == first_review, "review_at — момент ПЕРВОГО выхода справки"
    rows = (await session.scalars(select(StatusHistory).where(StatusHistory.investigation_id == inv.id)
                                  .order_by(StatusHistory.id))).all()
    assert [(r.from_status, r.to_status, r.user_id) for r in rows] == [
        (S.researching, S.review, None), (S.review, S.researching, u.id), (S.researching, S.review, None)]


@pytest.mark.parametrize("a,b", [(S.new, S.review), (S.new, S.handed_off), (S.researching, S.handed_off),
                                 (S.handed_off, S.review), (S.handed_off, S.rejected), (S.rejected, S.new),
                                 (S.rejected, S.handed_off), (S.review, S.new), (S.review, S.review)])
async def test_transition_not_allowed(session, a, b):
    inv, u = await _mk(session, a)
    with pytest.raises(W.TransitionNotAllowed):
        await W.transition(session, inv, b, u)
    assert inv.status == a


async def test_system_cannot_do_user_transitions(session):
    inv, _ = await _mk(session, S.review)
    with pytest.raises(W.TransitionNotAllowed):
        await W.transition(session, inv, S.handed_off, None)


async def test_transition_flush_without_commit(db_ready):
    async with db.SessionLocal() as s:
        inv, u = await _mk(s, S.new)
        await s.commit()
        inv_id = inv.id
        await W.transition(s, inv, S.researching, u)
        await s.rollback()
    async with db.SessionLocal() as s2:
        fresh = await s2.get(Investigation, inv_id)
        assert fresh.status == S.new, "transition не должен коммитить"
        n = (await s2.scalars(select(StatusHistory).where(StatusHistory.investigation_id == inv_id))).all()
        assert n == []
