"""HTML-страницы (Jinja2). Зона DEV-FRONTEND.

Все страницы, кроме /login, требуют входа (``current_user`` -> 401 -> main.py редиректит на
/login). Каждый шаблон наследует ``base.html`` и получает минимум ``request``, ``user``,
``nav_active``. Фильтры/глобалы Jinja регистрируйте здесь на объекте ``templates``.
Данные страница может получать напрямую из БД (session) и сервисов, либо через JS-fetch к /api.
Формы — POST + редирект 303 (PRG). Имена шаблонов и контекст — docs/CONTRACTS.md.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user, current_user_optional, require_role
from app.db import get_session
from app.models import Role, User

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["pages"], include_in_schema=False)

P, E, SMM, OP = Role.producer, Role.editor, Role.smm, Role.operator


# ---------------------------------------------------------------- вход
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: User | None = Depends(current_user_optional),
                     session: AsyncSession = Depends(get_session)):
    """login.html: {users: list[User] (сгруппировать по role), error: str|None}.
    Уже вошёл -> редирект на /."""
    raise NotImplementedError


@router.post("/login")
async def login_submit(request: Request, user_id: int = Form(...), password: str = Form("demo"),
                       session: AsyncSession = Depends(get_session)):
    """Проверка пароля -> auth.set_login_cookie на RedirectResponse('/', 303); ошибка -> login.html."""
    raise NotImplementedError


@router.get("/logout")
async def logout() -> RedirectResponse:
    """Снять cookie, редирект на /login."""
    raise NotImplementedError


# ---------------------------------------------------------------- дашборд
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    """dashboard.html: {investigations, role_view: "producer"|"editor"|"smm"|"operator"}.
    producer — мои зацепки + «Новая зацепка»; editor — очередь editing; smm — ready/published;
    operator — редирект на /operator (или краткая доска)."""
    raise NotImplementedError


# ---------------------------------------------------------------- расследования
@router.get("/investigations/new", response_class=HTMLResponse)
async def investigation_new(request: Request, user: User = Depends(require_role(P)),
                            session: AsyncSession = Depends(get_session)):
    """investigation_new.html: {channels: list[Channel], error: str|None}."""
    raise NotImplementedError


@router.post("/investigations/new")
async def investigation_create(request: Request, lead_text: str = Form(...),
                               channel_id: int | None = Form(None), title: str | None = Form(None),
                               user: User = Depends(require_role(P)),
                               session: AsyncSession = Depends(get_session)):
    """Создать + new->researching + pipeline.start_investigation; 303 на /investigations/{id}."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}", response_class=HTMLResponse)
async def investigation_detail(request: Request, inv_id: int, tab: str = "progress",
                               user: User = Depends(current_user),
                               session: AsyncSession = Depends(get_session)):
    """investigation.html. tab: progress|report|sources|contacts|media|draft|history.
    Контекст: inv, tab, iterations, report (dict|None), sources, clusters
    ({cluster_id: [Source]}), contacts (list[ContactOut], [] если роль не producer/operator),
    can_see_contacts, media, drafts, latest_draft, history, fact_marks, feedback,
    allowed_transitions (list[InvestigationStatus]), sources_by_id."""
    raise NotImplementedError


@router.get("/investigations/{inv_id}/edit", response_class=HTMLResponse)
async def investigation_edit(request: Request, inv_id: int, user: User = Depends(require_role(E)),
                             session: AsyncSession = Depends(get_session)):
    """editor.html: inv, ai_draft (v1), current_draft (последняя), diff (DraftDiffOut),
    claims (из current_draft.claims_json), sources_by_id, fact_marks, allowed_transitions."""
    raise NotImplementedError


@router.post("/investigations/{inv_id}/edit")
async def investigation_edit_save(request: Request, inv_id: int, title: str = Form(...),
                                  body: str = Form(...), user: User = Depends(require_role(E)),
                                  session: AsyncSession = Depends(get_session)):
    """Сохранить новую версию (author_kind=editor); 303 на /investigations/{id}/edit."""
    raise NotImplementedError


@router.post("/investigations/{inv_id}/transition")
async def investigation_transition(request: Request, inv_id: int, to_status: str = Form(...),
                                   comment: str | None = Form(None),
                                   user: User = Depends(current_user),
                                   session: AsyncSession = Depends(get_session)):
    """workflow.transition; 303 назад (Referer) или на /investigations/{id}."""
    raise NotImplementedError


@router.post("/investigations/{inv_id}/facts")
async def investigation_mark_fact(request: Request, inv_id: int, fact_text: str = Form(...),
                                  state: str = Form(...), claim_id: str | None = Form(None),
                                  comment: str | None = Form(None),
                                  user: User = Depends(require_role(P, E)),
                                  session: AsyncSession = Depends(get_session)):
    raise NotImplementedError


# ---------------------------------------------------------------- AI-оператор
@router.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request, user: User = Depends(require_role(OP)),
                        session: AsyncSession = Depends(get_session)):
    """operator.html: board (BoardOut), metrics (dict, METRIC_KEYS), metric_labels,
    feedback (list[Feedback]), chats (list[MaxChatOut]), channels (list[Channel]), sync_result."""
    raise NotImplementedError


@router.post("/operator/chats/{chat_id}/toggle")
async def operator_toggle_chat(chat_id: int, user: User = Depends(require_role(OP)),
                               session: AsyncSession = Depends(get_session)):
    raise NotImplementedError


@router.post("/operator/sync")
async def operator_sync(user: User = Depends(require_role(OP)),
                        session: AsyncSession = Depends(get_session)):
    """poller.sync_once; 303 на /operator?synced=N."""
    raise NotImplementedError


# ---------------------------------------------------------------- фидбэк
@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request, investigation_id: int | None = None,
                        user: User = Depends(require_role(P)),
                        session: AsyncSession = Depends(get_session)):
    """feedback.html: investigations (мои, без отзыва — первыми), selected_id, my_feedback."""
    raise NotImplementedError


@router.post("/feedback")
async def feedback_submit(request: Request, investigation_id: int = Form(...),
                          verdict: str = Form(...), comment: str | None = Form(None),
                          user: User = Depends(require_role(P)),
                          session: AsyncSession = Depends(get_session)):
    """Сохранить Feedback; 303 на Referer или /feedback."""
    raise NotImplementedError
