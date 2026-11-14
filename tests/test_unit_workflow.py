"""Юнит: services/workflow.py — таблица переходов × роли, история, оператор может всё (п.2 ТЗ)."""
from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select

from app import db
from app.models import Investigation, InvestigationStatus as S, Role, StatusHistory, User, utcnow
from app.services import workflow as W

pytestmark = pytest.mark.unit

ROLES: list[Role | None] = [Role.producer, Role.editor, Role.smm, Role.operator, None]

# Эталон из ТЗ п.2 / CONTRACTS §4 (независимо от ALLOWED_TRANSITIONS — ловим молчаливые правки).
SPEC: dict[tuple[S, S], set[Role | None]] = {
    (S.new, S.researching): {Role.producer},
    (S.new, S.rejected): {Role.producer},
    (S.researching, S.report_ready): {None},
    (S.researching, S.new): {None},
    (S.report_ready, S.editing): {Role.producer},
    (S.report_ready, S.rejected): {Role.producer},
    (S.report_ready, S.researching): {Role.producer},
    (S.editing, S.report_ready): {Role.editor},
    (S.editing, S.ready): {Role.editor},
    (S.ready, S.published): {Role.smm},
    (S.ready, S.editing): {Role.smm},
    (S.rejected, S.new): {Role.producer},
}


def expected(role: Role | None, a: S, b: S) -> bool:
    allowed = SPEC.get((a, b))
    if allowed is None:
        return False
    return role == Role.operator or role in allowed


def test_table_matches_spec():
    table = {(a, b): set(r) for a, d in W.ALLOWED_TRANSITIONS.items() for b, r in d.items()}
    assert table == SPEC
    assert set(W.ALLOWED_TRANSITIONS) == set(S)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value if r else "system")
def test_can_transition_full_matrix(role):
    wrong = [
        (a.value, b.value, W.can_transition(role, a, b))
        for a, b in itertools.product(S, S)
        if W.can_transition(role, a, b) is not expected(role, a, b)
    ]
    assert not wrong, f"роль {role}: неверно (from, to, got) {wrong}"


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value if r else "system")
def test_available_transitions(role):
    for a in S:
        got = W.available_transitions(role, a)
        assert isinstance(got, list)
        assert len(got) == len(set(got)), f"дубли в {got}"
        exp = {b for b in S if expected(role, a, b)}
        assert set(got) == exp, f"{role} из {a.value}: {got} != {exp}"


@pytest.mark.parametrize(
    "role,a,b",
    [
        (Role.smm, S.report_ready, S.editing),
        (Role.smm, S.new, S.researching),
        (Role.editor, S.ready, S.published),
        (Role.editor, S.report_ready, S.editing),
        (Role.producer, S.editing, S.ready),
        (Role.producer, S.ready, S.published),
        (Role.producer, S.researching, S.report_ready),
        (None, S.new, S.researching),
    ],
)
def test_specific_forbidden(role, a, b):
    assert W.can_transition(role, a, b) is False


def test_operator_cannot_jump_outside_table():
    assert W.can_transition(Role.operator, S.new, S.published) is False
    assert W.can_transition(Role.operator, S.published, S.new) is False
    assert W.available_transitions(Role.operator, S.published) == []


def test_operator_can_do_system_transition():
    assert W.can_transition(Role.operator, S.researching, S.report_ready) is True


# ---------------------------------------------------------------- transition() с БД
async def _setup(session, status: S = S.new):
    users = {}
    for login, role in [("p", Role.producer), ("e", Role.editor), ("s", Role.smm), ("o", Role.operator)]:
        u = User(name=login, role=role, login=login)
        session.add(u)
        users[login] = u
    old = utcnow().replace(year=2025)
    inv = Investigation(title="t", lead_text="lead text", status=status, producer_id=None,
                        created_at=old, updated_at=old)
    session.add(inv)
    await session.commit()
    return users, inv


async def _history(session, inv_id):
    rows = await session.execute(
        select(StatusHistory).where(StatusHistory.investigation_id == inv_id).order_by(StatusHistory.id))
    return rows.scalars().all()


async def test_transition_happy_path_writes_history(session):
    u, inv = await _setup(session)
    before = inv.updated_at
    h = await W.transition(session, inv, S.researching, u["p"], comment="go")
    assert isinstance(h, StatusHistory)
    assert inv.status == S.researching
    assert inv.updated_at > before
    assert (h.from_status, h.to_status, h.user_id, h.comment) == (S.new, S.researching, u["p"].id, "go")

    await W.transition(session, inv, S.report_ready, None)
    assert inv.report_ready_at is not None
    await W.transition(session, inv, S.editing, u["p"])
    await W.transition(session, inv, S.ready, u["e"])
    await W.transition(session, inv, S.published, u["s"])
    await session.commit()

    hist = await _history(session, inv.id)
    chain = [(x.from_status, x.to_status) for x in hist]
    assert chain == [(S.new, S.researching), (S.researching, S.report_ready),
                     (S.report_ready, S.editing), (S.editing, S.ready), (S.ready, S.published)]
    assert hist[1].user_id is None  # система
    assert [x.user_id for x in hist[2:]] == [u["p"].id, u["e"].id, u["s"].id]


async def test_transition_not_allowed_raises(session):
    u, inv = await _setup(session)
    with pytest.raises(W.TransitionNotAllowed):
        await W.transition(session, inv, S.published, u["o"])
    assert inv.status == S.new
    assert issubclass(W.TransitionNotAllowed, W.TransitionError)


async def test_transition_forbidden_raises(session):
    u, inv = await _setup(session, S.report_ready)
    with pytest.raises(W.TransitionForbidden):
        await W.transition(session, inv, S.editing, u["s"])
    with pytest.raises(W.TransitionForbidden):
        await W.transition(session, inv, S.editing, u["e"])
    assert inv.status == S.report_ready
    await session.commit()
    assert await _history(session, inv.id) == []


async def test_transition_system_forbidden_for_user_action(session):
    _, inv = await _setup(session, S.new)
    with pytest.raises(W.TransitionForbidden):
        await W.transition(session, inv, S.researching, None)


async def test_operator_can_do_everything(session):
    u, inv = await _setup(session)
    path = [S.researching, S.report_ready, S.editing, S.ready, S.editing, S.report_ready,
            S.rejected, S.new, S.rejected]
    for st in path:
        await W.transition(session, inv, st, u["o"])
        assert inv.status == st
    await session.commit()
    hist = await _history(session, inv.id)
    assert len(hist) == len(path)
    assert all(h.user_id == u["o"].id for h in hist)


async def test_transition_flushes_but_does_not_commit(session):
    u, inv = await _setup(session)
    inv_id = inv.id
    await W.transition(session, inv, S.researching, u["p"])
    # flush сделан: запрос в той же сессии видит историю
    assert len(await _history(session, inv_id)) == 1
    await session.rollback()
    async with db.SessionLocal() as other:
        fresh = await other.get(Investigation, inv_id)
        assert fresh.status == S.new
        assert await _history(other, inv_id) == []
