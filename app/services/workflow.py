"""Статусная модель расследования (п.2 ТЗ). Зона DEV-BACKEND.

``ALLOWED_TRANSITIONS`` — единственный источник правды о переходах (таблица заполнена
тимлидом, менять — через «Запросы на изменение контракта»). Роль ``None`` = система
(пайплайн). ``Role.operator`` может выполнить ЛЮБОЙ переход из таблицы (включая системные).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InvestigationStatus as S
from app.models import Investigation, Role, StatusHistory, User, utcnow

SYSTEM = None  # «роль» пайплайна

ALLOWED_TRANSITIONS: dict[S, dict[S, frozenset[Role | None]]] = {
    S.new: {
        S.researching: frozenset({Role.producer}),  # «Запустить NEWSFLOW»
        S.rejected: frozenset({Role.producer}),
    },
    S.researching: {
        S.report_ready: frozenset({SYSTEM}),  # пайплайн завершён
        S.new: frozenset({SYSTEM}),  # откат при фатальной ошибке (опционально)
    },
    S.report_ready: {
        S.editing: frozenset({Role.producer}),  # «Передать на редактуру»
        S.rejected: frozenset({Role.producer}),  # «стрём» / не подтвердилось
        S.researching: frozenset({Role.producer}),  # перезапуск исследования
    },
    S.editing: {
        S.report_ready: frozenset({Role.editor}),  # «Вернуть продюсеру»
        S.ready: frozenset({Role.editor}),  # «Готово для SMM»
    },
    S.ready: {
        S.published: frozenset({Role.smm}),  # «Опубликовано»
        S.editing: frozenset({Role.smm}),  # вернуть на доработку
    },
    S.published: {},
    S.rejected: {
        S.new: frozenset({Role.producer}),  # вернуть зацепку в работу
    },
}


class TransitionError(Exception):
    """Базовая ошибка перехода."""


class TransitionNotAllowed(TransitionError):
    """Перехода нет в таблице (api -> HTTP 409)."""


class TransitionForbidden(TransitionError):
    """Переход есть, но не для этой роли (api -> HTTP 403)."""


def can_transition(role: Role | None, from_status: S, to_status: S) -> bool:
    """True, если ``role`` (None = система) может перевести from -> to.
    Operator: True для любого перехода, присутствующего в таблице."""
    from_status, to_status = S(from_status), S(to_status)
    allowed = ALLOWED_TRANSITIONS.get(from_status, {}).get(to_status)
    if allowed is None:
        return False
    if role is not None and Role(role) == Role.operator:
        return True
    return role in allowed


def available_transitions(role: Role | None, from_status: S) -> list[S]:
    """Список статусов, куда роль может перевести (для кнопок UI)."""
    from_status = S(from_status)
    return [to for to in ALLOWED_TRANSITIONS.get(from_status, {}) if can_transition(role, from_status, to)]


def transition_exists(from_status: S, to_status: S) -> bool:
    return S(to_status) in ALLOWED_TRANSITIONS.get(S(from_status), {})


async def transition(
    session: AsyncSession,
    inv: Investigation,
    to: S,
    user: User | None,
    comment: str | None = None,
) -> StatusHistory:
    """Проверить can_transition (иначе TransitionNotAllowed / TransitionForbidden), сменить
    статус, обновить updated_at, при to=report_ready — report_ready_at, при to=editing и
    user-редакторе — editor_id; добавить StatusHistory(user_id=None для системы).
    Делает flush, НЕ commit (commit — вызывающий)."""
    to = S(to)
    from_status = S(inv.status)
    if not transition_exists(from_status, to):
        raise TransitionNotAllowed(f"Переход {from_status.value} -> {to.value} недопустим")
    role = user.role if user is not None else SYSTEM
    if not can_transition(role, from_status, to):
        who = Role(role).label if role is not None else "система"
        raise TransitionForbidden(f"Роль «{who}» не может выполнить переход {from_status.value} -> {to.value}")

    now = utcnow()
    inv.status = to
    inv.updated_at = now
    # Time to Draft считаем по завершению исследования (researching -> report_ready);
    # возврат от редактора (editing -> report_ready) метку не сбрасывает.
    if to == S.report_ready and (from_status == S.researching or inv.report_ready_at is None):
        inv.report_ready_at = now
    if to == S.editing and user is not None and user.role == Role.editor:
        inv.editor_id = user.id

    entry = StatusHistory(
        investigation_id=inv.id,
        from_status=from_status,
        to_status=to,
        user_id=user.id if user is not None else None,
        comment=comment,
        created_at=now,
    )
    session.add(entry)
    await session.flush()
    return entry
