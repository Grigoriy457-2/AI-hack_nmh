"""Статусная модель расследования (п.2 ТЗ). Зона DEV-BACKEND.

``ALLOWED_TRANSITIONS`` — единственный источник правды о переходах (таблица заполнена
тимлидом, менять — через «Запросы на изменение контракта»). Роль ``None`` = система
(пайплайн). ``Role.operator`` может выполнить ЛЮБОЙ переход из таблицы (включая системные).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InvestigationStatus as S
from app.models import Investigation, Role, StatusHistory, User

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
    raise NotImplementedError("DEV-BACKEND: can_transition")


def available_transitions(role: Role | None, from_status: S) -> list[S]:
    """Список статусов, куда роль может перевести (для кнопок UI)."""
    raise NotImplementedError("DEV-BACKEND: available_transitions")


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
    raise NotImplementedError("DEV-BACKEND: transition")
