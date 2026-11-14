"""Cookie-сессия (itsdangerous). Ролей нет (ТЗ v2): все пользователи — редакторы.

Использование в роутерах::

    user: User = Depends(current_user)                         # любой залогиненный
    user: User | None = Depends(current_user_optional)         # страницы без обязательного входа

Неавторизованный запрос → HTTP 401. В app/main.py есть обработчик: для путей не под /api
401 превращается в редирект на /login.
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import User

COOKIE_NAME = "nf_session"
_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="newsflow-session")


def verify_password(password: str) -> bool:
    """Все seed-пользователи входят с паролем settings.DEMO_PASSWORD («demo»)."""
    return hmac.compare_digest(password or "", settings.DEMO_PASSWORD)


def make_session_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_token(token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=settings.SESSION_MAX_AGE)
    except BadSignature:
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None


def set_login_cookie(response: Response, user: User | int) -> None:
    uid = user if isinstance(user, int) else user.id
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(uid),
        max_age=settings.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


async def current_user_optional(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    uid = read_session_token(request.cookies.get(COOKIE_NAME))
    if uid is None:
        return None
    user = await session.get(User, uid)
    request.state.user = user
    return user


async def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
    return user
