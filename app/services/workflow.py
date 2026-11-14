"""Статусная модель сюжета (ТЗ v2): один пользователь — редактор.

new → researching → review → handed_off | rejected; rejected → review; review → researching
(«Копать глубже» / перезапуск). ``"system"`` — переходы пайплайна, ``"user"`` — редактора.
"""
from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Investigation, StatusHistory, User, utcnow
from app.models import InvestigationStatus as S

Actor = Literal["user", "system"]
USER: Actor = "user"
SYSTEM: Actor = "system"

ALLOWED_TRANSITIONS: dict[S, dict[S, frozenset[str]]] = {
    S.new: {
        S.researching: frozenset({USER}),  # «Исследовать»
        S.rejected: frozenset({USER}),  # «Не подтвердилось»
    },
    S.researching: {
        S.review: frozenset({SYSTEM, USER}),  # пайплайн завершён / «Остановить»
    },
    S.review: {
        S.researching: frozenset({USER}),  # «Копать глубже» / перезапуск
        S.handed_off: frozenset({USER}),  # «Передать дальше»
        S.rejected: frozenset({USER}),  # «Не подтвердилось»
    },
    S.handed_off: {},
    S.rejected: {
        S.review: frozenset({USER}),  # «Вернуть в работу»
    },
}

TRANSITION_BUTTONS: dict[tuple[S, S], str] = {
    (S.new, S.researching): "Исследовать",
    (S.new, S.rejected): "Не подтвердилось",
    (S.researching, S.review): "Остановить поиск",
    (S.review, S.researching): "Копать глубже",
    (S.review, S.handed_off): "Передать дальше",
    (S.review, S.rejected): "Не подтвердилось",
    (S.rejected, S.review): "Вернуть в работу",
}


class TransitionError(Exception):
    """Базовая ошибка перехода."""


class TransitionNotAllowed(TransitionError):
    """Перехода нет в таблице (api -> HTTP 409)."""


# совместимость v1: «запрещено для роли» больше не бывает
TransitionForbidden = TransitionNotAllowed


def _actor(user: User | None) -> Actor:
    return SYSTEM if user is None else USER


def can_transition(actor: str | User | None, from_status: S, to_status: S) -> bool:
    if not isinstance(actor, str):
        actor = _actor(actor)
    allowed = ALLOWED_TRANSITIONS.get(S(from_status), {}).get(S(to_status))
    return bool(allowed) and actor in allowed


def available_transitions(from_status: S, actor: str = USER) -> list[S]:
    """Куда редактор может перевести сюжет (для кнопок UI)."""
    from_status = S(from_status)
    return [to for to, who in ALLOWED_TRANSITIONS.get(from_status, {}).items() if actor in who]


def transition_exists(from_status: S, to_status: S) -> bool:
    return S(to_status) in ALLOWED_TRANSITIONS.get(S(from_status), {})


async def transition(
    session: AsyncSession,
    inv: Investigation,
    to: S,
    user: User | None,
    comment: str | None = None,
) -> StatusHistory:
    """Сменить статус (иначе TransitionNotAllowed), обновить updated_at; при первом
    researching→review — review_at; добавить StatusHistory. flush, без commit."""
    to = S(to)
    from_status = S(inv.status)
    if not can_transition(_actor(user), from_status, to):
        raise TransitionNotAllowed(
            f"Переход «{from_status.label}» → «{to.label}» недопустим"
        )
    now = utcnow()
    inv.status = to
    inv.updated_at = now
    if to == S.review and inv.review_at is None:
        inv.review_at = now
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
