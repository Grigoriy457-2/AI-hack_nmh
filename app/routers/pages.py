"""HTML-страницы (Jinja2). Зона DEV-FRONTEND.

Все страницы, кроме /login, требуют входа (``current_user`` -> 401 -> main.py редиректит на
/login). Каждый шаблон наследует ``base.html`` и получает минимум ``request``, ``user``,
``nav_active``. Фильтры/глобалы Jinja регистрируются здесь на объекте ``templates``.

Данные страницы берут напрямую из БД (session). Сервисы других зон (workflow, pipeline,
metrics, poller) вызываются «мягко»: пока они не реализованы (NotImplementedError), страницы
не падают — показывают «нет данных» / сообщение, а переходы статусов выполняются локальным
fallback'ом строго по таблице ``workflow.ALLOWED_TRANSITIONS``.
Формы — POST + редирект 303 (PRG). Флэш-сообщения — query-параметры ``msg`` и ``level``.
"""
from __future__ import annotations

import difflib
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.auth import current_user, current_user_optional, has_role
from app.config import settings
from app.db import get_session
from app.models import (
    ROLE_LABELS,
    STATUS_LABELS,
    AuthorProfile,
    Channel,
    ChatMessage,
    Draft,
    DraftAuthorKind,
    FactMark,
    FactState,
    Feedback,
    Investigation,
    InvestigationStatus,
    MaxChat,
    MediaItem,
    Report,
    Role,
    SearchIteration,
    Source,
    SourceKind,
    StatusHistory,
    User,
    Verdict,
    utcnow,
)
from app.schemas import BoardCard, DiffOp, DraftDiffOut, MaxChatOut, UserOut

log = logging.getLogger("newsflow.pages")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["pages"], include_in_schema=False)

P, E, SMM, OP = Role.producer, Role.editor, Role.smm, Role.operator
S = InvestigationStatus

# ================================================================ подписи и справочники
STATUS_ORDER: list[S] = [S.new, S.researching, S.report_ready, S.editing, S.ready, S.published, S.rejected]
STATUS_FLOW: list[S] = [S.new, S.researching, S.report_ready, S.editing, S.ready, S.published]

SOURCE_KIND_LABELS = {
    SourceKind.web: "Web",
    SourceKind.max_chat: "MAX-чат",
    SourceKind.official: "Официальный",
    SourceKind.media: "Медиа",
}
FACT_STATE_LABELS = {
    "confirmed": "подтверждён",
    "unconfirmed": "не подтверждён",
    "false": "ложь",
    None: "не проверен",
}
AGENT_LABELS = {
    "web": "Web-поиск",
    "max_chats": "Чаты MAX",
    "max_chat": "Чаты MAX",
    "official": "Официальные",
    "media": "Медиа",
}
ROLE_DESCRIPTIONS = {
    Role.producer: "Создаёт зацепку, запускает NEWSFLOW, проверяет справку, звонит очевидцам",
    Role.editor: "Правит готовый AI-черновик: diff, проверка утверждений по источникам",
    Role.smm: "Получает готовые материалы и отмечает публикацию",
    Role.operator: "Следит за всеми задачами, метриками, чатами MAX и профилями каналов",
}
# (из, в) -> (подпись кнопки, стиль)
TRANSITION_BUTTONS: dict[tuple[S, S], tuple[str, str]] = {
    (S.new, S.researching): ("Запустить NEWSFLOW", "primary"),
    (S.new, S.rejected): ("Отклонить", "danger-ghost"),
    (S.researching, S.report_ready): ("Завершить исследование (система)", "ghost"),
    (S.researching, S.new): ("Откатить в «Новая» (система)", "ghost"),
    (S.report_ready, S.editing): ("Передать на редактуру", "primary"),
    (S.report_ready, S.rejected): ("Стрём / не подтвердилось", "danger-ghost"),
    (S.report_ready, S.researching): ("Перезапустить исследование", "ghost"),
    (S.editing, S.report_ready): ("Вернуть продюсеру", "ghost"),
    (S.editing, S.ready): ("Готово для SMM", "primary"),
    (S.ready, S.published): ("Опубликовано", "success"),
    (S.ready, S.editing): ("Вернуть на доработку", "ghost"),
    (S.rejected, S.new): ("Вернуть в работу", "ghost"),
}

DEMO_LEAD = (
    "В домовых чатах Чертанова пишут, что около 14:30 у жилого дома на Варшавском шоссе "
    "был громкий хлопок, похожий на взрыв. Жильцы говорят про запах газа и выбитые стёкла "
    "на 3-м этаже, кто-то видел скорую и пожарных. Официальной информации пока нет."
)
DEMO_TITLE = "Хлопок у жилого дома в Чертанове"

UI_METRICS: list[dict[str, str]] = [
    {"key": "time_to_draft_avg_sec", "title": "Time to Draft", "fmt": "dur",
     "hint": "Среднее время от зацепки до готовой справки и черновика. Чем меньше — тем быстрее редакция в эфире."},
    {"key": "source_coverage", "title": "Source Coverage", "fmt": "pct",
     "hint": "Доля расследований, где ≥3 первоисточника и ≥2 типа источников (web, чаты, официальные)."},
    {"key": "traceability", "title": "Traceability", "fmt": "pct",
     "hint": "Доля утверждений черновика, у которых есть ссылка на источник. Цель — 100%."},
    {"key": "draft_acceptance_rate", "title": "Draft Acceptance", "fmt": "pct",
     "hint": "Доля AI-черновиков, принятых редактором с малыми правками (похожесть ≥ 85%)."},
    {"key": "factcheck_correction_rate", "title": "Fact-check corrections", "fmt": "pct",
     "hint": "Доля фактов, которые люди пометили как «не подтверждён» или «ложь». Ниже — лучше."},
    {"key": "feedback_good_share", "title": "Доля «норм»", "fmt": "pct",
     "hint": "Как продюсеры оценивают темы, найденные NEWSFLOW: доля оценок «норм»."},
]


# ================================================================ Jinja: фильтры
MSK = timedelta(hours=3)
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _to_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        d = value
    else:
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is not None:
        d = d.astimezone(UTC).replace(tzinfo=None)
    return d


def fmt_dt(value: Any, mode: str = "short") -> str:
    """UTC (naive datetime или ISO-строка) -> МСК. short: «14:31, 14 ноя»; time: «14:31»;
    full: «14:31:05, 14 ноя 2026»; date: «14 ноя»."""
    d = _to_dt(value)
    if d is None:
        return "—" if value in (None, "") else str(value)
    m = d + MSK
    if mode == "time":
        return m.strftime("%H:%M")
    if mode == "seconds":
        return m.strftime("%H:%M:%S")
    if mode == "date":
        return f"{m.day} {MONTHS[m.month - 1]}"
    if mode == "full":
        return f"{m.strftime('%H:%M:%S')}, {m.day} {MONTHS[m.month - 1]} {m.year}"
    return f"{m.strftime('%H:%M')}, {m.day} {MONTHS[m.month - 1]}"


def fmt_pct(value: Any, digits: int = 0) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_dur(seconds: Any) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return f"{s} с"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} мин {s} с" if s else f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин"


def fmt_ago(value: Any) -> str:
    d = _to_dt(value)
    if d is None:
        return "—"
    minutes = int((utcnow() - d).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


def fmt_minutes(value: Any) -> str:
    try:
        m = int(value)
    except (TypeError, ValueError):
        return "—"
    if m < 60:
        return f"{m} мин"
    if m < 60 * 24:
        return f"{m // 60} ч {m % 60} мин"
    return f"{m // 1440} дн"


def _enum_val(x: Any) -> Any:
    return getattr(x, "value", x)


def status_label(s: Any) -> str:
    try:
        return STATUS_LABELS[S(_enum_val(s))]
    except (ValueError, KeyError):
        return str(s)


def role_label(r: Any) -> str:
    try:
        return ROLE_LABELS[Role(_enum_val(r))]
    except (ValueError, KeyError):
        return str(r)


def kind_label(k: Any) -> str:
    try:
        return SOURCE_KIND_LABELS[SourceKind(_enum_val(k))]
    except (ValueError, KeyError):
        return str(k or "—")


def fact_label(state: Any) -> str:
    return FACT_STATE_LABELS.get(_enum_val(state), "не проверен")


def initials(name: str | None) -> str:
    parts = [p for p in (name or "?").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def safe_url(url: Any) -> str | None:
    """Только http(s)-ссылки пригодны для href (max://… и прочее показываем текстом)."""
    if not url:
        return None
    u = str(url).strip()
    return u if u.lower().startswith(("http://", "https://")) else None


def host_of(url: Any) -> str:
    try:
        return urlsplit(str(url)).netloc.removeprefix("www.") or str(url)
    except ValueError:
        return str(url)


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


STATIC_DIR = TEMPLATES_DIR.parent / "static"


def asset(path: str) -> str:
    """URL статики с версией по mtime — браузер не держит старый CSS/JS после правок."""
    try:
        v = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        v = 0
    return f"/static/{path}?v={v}"


env = templates.env
env.filters.update(
    dt=fmt_dt, pct=fmt_pct, dur=fmt_dur, ago=fmt_ago, minutes=fmt_minutes,
    status_label=status_label, role_label=role_label, kind_label=kind_label,
    fact_label=fact_label, initials=initials, safe_url=safe_url, host=host_of, ev=_enum_val,
)
env.globals.update(
    ROLE_LABELS=ROLE_LABELS, STATUS_LABELS=STATUS_LABELS, Role=Role,
    InvestigationStatus=InvestigationStatus, has_role=has_role, STATUS_ORDER=STATUS_ORDER,
    STATUS_FLOW=STATUS_FLOW, SOURCE_KIND_LABELS=SOURCE_KIND_LABELS,
    FACT_STATE_LABELS=FACT_STATE_LABELS, AGENT_LABELS=AGENT_LABELS,
    ROLE_DESCRIPTIONS=ROLE_DESCRIPTIONS, TRANSITION_BUTTONS=TRANSITION_BUTTONS,
    plural=plural, APP_NAME=settings.APP_NAME, asset=asset,
)


# ================================================================ helpers
def _flash(request: Request) -> dict[str, str] | None:
    msg = request.query_params.get("msg")
    if not msg:
        return None
    return {"text": msg[:300], "level": request.query_params.get("level", "ok")}


def _with_params(url: str, **params: Any) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k not in params and k not in ("msg", "level")]
    q += [(k, str(v)) for k, v in params.items() if v is not None]
    return urlunsplit(("", "", parts.path or "/", urlencode(q), parts.fragment))


def _back(request: Request, fallback: str) -> str:
    """Безопасный Referer (только тот же хост, только путь)."""
    ref = request.headers.get("referer")
    if ref:
        p = urlsplit(ref)
        if not p.netloc or p.netloc == request.url.netloc:
            return urlunsplit(("", "", p.path or "/", p.query, ""))
    return fallback


def _redirect(url: str, msg: str | None = None, level: str = "ok", anchor: str | None = None) -> RedirectResponse:
    target = _with_params(url, msg=msg, level=level if msg else None) if msg else url
    if anchor:
        target = target.split("#")[0] + "#" + anchor
    return RedirectResponse(target, status_code=303)


async def _all_users(session: AsyncSession) -> list[User]:
    res = await session.scalars(select(User).order_by(User.id))
    return list(res)


async def _render(request: Request, name: str, ctx: dict[str, Any], *, user: User | None,
                  session: AsyncSession, nav_active: str = "", status_code: int = 200) -> HTMLResponse:
    base = {
        "request": request,
        "user": user,
        "nav_active": nav_active,
        "flash": _flash(request),
        "all_users": await _all_users(session) if user else [],
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


async def _error(request: Request, user: User | None, session: AsyncSession, status_code: int,
                 message: str) -> HTMLResponse:
    return await _render(request, "error.html", {"status_code": status_code, "message": message},
                         user=user, session=session, status_code=status_code)


async def _forbidden(request: Request, user: User, session: AsyncSession, *roles: Role) -> HTMLResponse:
    need = ", ".join(ROLE_LABELS[r] for r in roles) or "—"
    return await _error(request, user, session, 403,
                        f"Этот экран доступен ролям: {need} (и AI-оператору). Вы вошли как «{role_label(user.role)}».")


# ---------------------------------------------------------------- workflow (мягкие вызовы)
def _table_transitions(role: Role | None, from_status: S) -> list[S]:
    from app.services.workflow import ALLOWED_TRANSITIONS

    out = []
    for to, roles in ALLOWED_TRANSITIONS.get(from_status, {}).items():
        if role == Role.operator or role in roles:
            out.append(to)
    return out


def available_transitions(role: Role | None, from_status: S) -> list[S]:
    try:
        from app.services import workflow

        return list(workflow.available_transitions(role, from_status))
    except NotImplementedError:
        return _table_transitions(role, from_status)


class _TransitionFailed(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def do_transition(session: AsyncSession, inv: Investigation, to: S, user: User | None,
                        comment: str | None = None) -> None:
    """workflow.transition, а пока он не реализован — локальный эквивалент по таблице."""
    from app.services import workflow

    try:
        await workflow.transition(session, inv, to, user, comment)
        return
    except NotImplementedError:
        pass
    except workflow.TransitionForbidden:
        raise _TransitionFailed("Этот переход недоступен вашей роли") from None
    except workflow.TransitionNotAllowed:
        raise _TransitionFailed(
            f"Переход «{status_label(inv.status)}» → «{status_label(to)}» невозможен") from None

    role = user.role if user else None
    table = workflow.ALLOWED_TRANSITIONS.get(inv.status, {})
    if to not in table:
        raise _TransitionFailed(f"Переход «{status_label(inv.status)}» → «{status_label(to)}» невозможен")
    if not (role == Role.operator or role in table[to]):
        raise _TransitionFailed("Этот переход недоступен вашей роли")
    now = utcnow()
    session.add(StatusHistory(investigation_id=inv.id, from_status=inv.status, to_status=to,
                              user_id=user.id if user else None, comment=comment or None, created_at=now))
    inv.status = to
    inv.updated_at = now
    if to == S.report_ready:
        inv.report_ready_at = now
    if to == S.editing and user is not None and user.role == Role.editor:
        inv.editor_id = user.id
    await session.flush()


async def _start_pipeline(inv_id: int) -> str | None:
    """Запустить пайплайн; вернуть текст предупреждения, если он ещё не готов."""
    try:
        from app.services import pipeline

        if settings.PIPELINE_INLINE:
            await pipeline.run_investigation(inv_id)
        else:
            pipeline.start_investigation(inv_id)
        return None
    except NotImplementedError:
        return "Пайплайн NEWSFLOW ещё не подключён (бэкенд в разработке) — исследование не запущено"
    except Exception:  # noqa: BLE001
        log.exception("pipeline start failed")
        return "Не удалось запустить пайплайн — см. логи сервера"


# ---------------------------------------------------------------- выборки
def _responsible(inv: Investigation) -> User | None:
    if inv.status == S.editing:
        return inv.editor or None
    if inv.status in (S.ready, S.published):
        return None
    return inv.producer


def _board_card(inv: Investigation, now: datetime) -> BoardCard:
    age = max(0, int((now - (inv.updated_at or inv.created_at or now)).total_seconds() // 60))
    resp = _responsible(inv)
    return BoardCard(
        id=inv.id, title=inv.title, status=inv.status,
        responsible=UserOut.model_validate(resp) if resp else None,
        updated_at=inv.updated_at or inv.created_at, age_minutes=age,
        is_stale=age >= settings.STALE_AFTER_MINUTES and inv.status not in (S.published, S.rejected),
    )


async def _build_board(session: AsyncSession) -> dict[str, Any]:
    invs = list(await session.scalars(select(Investigation).order_by(Investigation.updated_at.desc())))
    now = utcnow()
    columns: dict[S, list[BoardCard]] = {s: [] for s in STATUS_ORDER}
    for inv in invs:
        columns.setdefault(inv.status, []).append(_board_card(inv, now))
    stale = [c for col in columns.values() for c in col if c.is_stale]
    stale.sort(key=lambda c: -c.age_minutes)
    return {"columns": columns, "stale": stale}


def _effective_states(report: dict | None, claims: list[dict], marks: list[FactMark]) -> dict[str, dict]:
    """claim_id -> {state, by_human, comment}. Последняя fact_mark побеждает исходный статус."""
    out: dict[str, dict] = {}
    if report:
        for sec in report.get("sections") or []:
            for it in sec.get("items") or []:
                if it.get("id"):
                    out[str(it["id"])] = {"state": it.get("state"), "by_human": False, "comment": None}
    for c in claims or []:
        if c.get("id"):
            out[str(c["id"])] = {"state": c.get("status"), "by_human": False, "comment": c.get("note")}
    for m in sorted(marks, key=lambda m: (m.created_at or datetime.min, m.id)):
        if m.claim_id:
            out[str(m.claim_id)] = {"state": _enum_val(m.state), "by_human": True, "comment": m.comment}
    return out


_TOKEN_RE = re.compile(r"\s+|[^\s]+")


def word_diff(a: str, b: str, base_version: int = 1, target_version: int = 1) -> DraftDiffOut:
    ta, tb = _TOKEN_RE.findall(a or ""), _TOKEN_RE.findall(b or "")
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    ops = [DiffOp(op=tag, a="".join(ta[i1:i2]), b="".join(tb[j1:j2]))
           for tag, i1, i2, j1, j2 in sm.get_opcodes()]
    return DraftDiffOut(base_version=base_version, target_version=target_version,
                        ratio=round(sm.ratio(), 4), ops=ops)


def _source_dict(s: Source, n: int, dup: int, chat_titles: dict[int, str]) -> dict[str, Any]:
    msg = s.chat_message
    return {
        "id": s.id, "n": n, "kind": _enum_val(s.kind), "kind_label": kind_label(s.kind),
        "title": s.title or (f"Сообщение {msg.author_name}" if msg else "Источник"),
        "url": safe_url(s.url), "raw_url": s.url, "snippet": s.snippet or (msg.text if msg else ""),
        "published": fmt_dt(s.published_at or (msg.sent_at if msg else None)),
        "uniqueness": s.uniqueness_score, "is_primary": bool(s.is_primary),
        "is_important": bool(s.is_important), "duplicates": dup,
        "chat": chat_titles.get(msg.chat_id) if msg else None,
        "author": msg.author_name if msg else None,
        "iteration": s.iteration_n, "query": s.query,
    }


# ================================================================ вход
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: User | None = Depends(current_user_optional),
                     session: AsyncSession = Depends(get_session)):
    """login.html: {users (сгруппированы по role), error}. Уже вошёл -> редирект на /."""
    if user is not None and not request.query_params.get("switch"):
        return RedirectResponse("/", status_code=303)
    return await _login_render(request, session, None)


async def _login_render(request: Request, session: AsyncSession, error: str | None,
                        status_code: int = 200) -> HTMLResponse:
    users = await _all_users(session)
    order = {Role.producer: 0, Role.editor: 1, Role.smm: 2, Role.operator: 3}
    users.sort(key=lambda u: (order.get(u.role, 9), u.id))
    groups: dict[Role, list[User]] = defaultdict(list)
    for u in users:
        groups[u.role].append(u)
    return await _render(request, "login.html",
                         {"users": users, "groups": dict(groups), "error": error},
                         user=None, session=session, nav_active="login", status_code=status_code)


@router.post("/login")
async def login_submit(request: Request, user_id: int = Form(...), password: str = Form("demo"),
                       next: str | None = Form(None),
                       session: AsyncSession = Depends(get_session)):
    """Проверка пароля -> cookie + 303 на / (или next); ошибка -> login.html."""
    target = await session.get(User, user_id)
    if target is None:
        return await _login_render(request, session, "Пользователь не найден", 404)
    if not auth.verify_password(password):
        return await _login_render(request, session, "Неверный пароль. Для демо — «demo».", 401)
    dest = next if next and next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(dest, status_code=303)
    auth.set_login_cookie(resp, target)
    return resp


@router.get("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    auth.clear_login_cookie(resp)
    return resp


# ================================================================ дашборд
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    """dashboard.html: {investigations, role_view}. operator -> /operator."""
    if user.role == Role.operator and not request.query_params.get("view"):
        return RedirectResponse("/operator", status_code=303)
    role_view = request.query_params.get("view") if user.role == Role.operator else user.role.value
    if role_view not in ("producer", "editor", "smm", "operator"):
        role_view = "producer"

    q = select(Investigation).order_by(Investigation.updated_at.desc())
    all_invs = list(await session.scalars(q))
    ctx: dict[str, Any] = {"role_view": role_view}

    if role_view == "producer":
        mine = [i for i in all_invs if i.producer_id == user.id] if user.role == Role.producer else all_invs
        others = [i for i in all_invs if i not in mine]
        fb_ids = set(await session.scalars(select(Feedback.investigation_id).where(Feedback.user_id == user.id)))
        ctx.update(investigations=mine, others=others[:8], feedback_ids=fb_ids,
                   counts={s: sum(1 for i in mine if i.status == s) for s in STATUS_ORDER})
    elif role_view == "editor":
        queue = [i for i in all_invs if i.status == S.editing]
        queue.sort(key=lambda i: (0 if i.editor_id == user.id else 1 if i.editor_id is None else 2,
                                  i.updated_at))
        waiting = [i for i in all_invs if i.status == S.report_ready]
        done = [i for i in all_invs if i.status in (S.ready, S.published)
                and (i.editor_id == user.id or user.role == Role.operator)][:8]
        ctx.update(investigations=queue, waiting=waiting, done=done)
    elif role_view == "smm":
        ready = [i for i in all_invs if i.status == S.ready]
        published = [i for i in all_invs if i.status == S.published][:12]
        ctx.update(investigations=ready, published=published)
    else:
        ctx.update(investigations=all_invs)

    # последние черновики для превью
    ids = [i.id for i in ctx["investigations"]][:30] + [i.id for i in ctx.get("published", [])]
    latest: dict[int, Draft] = {}
    if ids:
        for d in await session.scalars(select(Draft).where(Draft.investigation_id.in_(ids))
                                       .order_by(Draft.version)):
            latest[d.investigation_id] = d
    ctx["latest_drafts"] = latest
    return await _render(request, "dashboard.html", ctx, user=user, session=session, nav_active="dashboard")


# ================================================================ новое расследование
async def _channels(session: AsyncSession) -> list[Channel]:
    return list(await session.scalars(select(Channel).order_by(Channel.id)))


@router.get("/investigations/new", response_class=HTMLResponse)
async def investigation_new(request: Request, user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)):
    """investigation_new.html: {channels, error}."""
    if not has_role(user, P):
        return await _forbidden(request, user, session, P)
    return await _render(request, "investigation_new.html",
                         {"channels": await _channels(session), "error": None, "form": {},
                          "demo_lead": DEMO_LEAD, "demo_title": DEMO_TITLE},
                         user=user, session=session, nav_active="new")


@router.post("/investigations/new")
async def investigation_create(request: Request, lead_text: str = Form(""),
                               channel_id: str | None = Form(None), title: str | None = Form(None),
                               user: User = Depends(current_user),
                               session: AsyncSession = Depends(get_session)):
    """Создать + new->researching + pipeline.start_investigation; 303 на /investigations/{id}."""
    if not has_role(user, P):
        return await _forbidden(request, user, session, P)
    lead = (lead_text or "").strip()
    ch_id = int(channel_id) if channel_id and channel_id.isdigit() else None
    if len(lead) < 3:
        return await _render(request, "investigation_new.html",
                             {"channels": await _channels(session),
                              "error": "Опишите, что произошло (хотя бы пару слов).",
                              "form": {"lead_text": lead, "channel_id": ch_id, "title": title},
                              "demo_lead": DEMO_LEAD, "demo_title": DEMO_TITLE},
                             user=user, session=session, nav_active="new", status_code=422)
    if ch_id is not None and await session.get(Channel, ch_id) is None:
        ch_id = None
    t = (title or "").strip()
    if not t:
        first = lead.split("\n")[0]
        t = first if len(first) <= 80 else first[:77].rsplit(" ", 1)[0] + "…"
    now = utcnow()
    inv = Investigation(title=t[:255], lead_text=lead, channel_id=ch_id, status=S.new,
                        producer_id=user.id, created_at=now, updated_at=now)
    session.add(inv)
    await session.flush()
    session.add(StatusHistory(investigation_id=inv.id, from_status=None, to_status=S.new,
                              user_id=user.id, comment="Зацепка создана", created_at=now))
    warn = None
    try:
        await do_transition(session, inv, S.researching, user, "Запуск NEWSFLOW")
    except _TransitionFailed as e:
        warn = e.message
    await session.commit()
    if warn is None:
        warn = await _start_pipeline(inv.id)
    url = f"/investigations/{inv.id}?tab=progress"
    if warn:
        return _redirect(url, warn, "warn")
    return _redirect(url)


# ================================================================ страница расследования
async def _load_inv(session: AsyncSession, inv_id: int) -> Investigation | None:
    return await session.get(Investigation, inv_id)


async def _sources_ctx(session: AsyncSession, inv_id: int) -> dict[str, Any]:
    sources = list(await session.scalars(select(Source).where(Source.investigation_id == inv_id)
                                         .order_by(Source.id)))
    chat_ids = {s.chat_message.chat_id for s in sources if s.chat_message}
    chat_titles: dict[int, str] = {}
    if chat_ids:
        for c in await session.scalars(select(MaxChat).where(MaxChat.id.in_(chat_ids))):
            chat_titles[c.id] = c.title
    clusters: dict[int, list[Source]] = defaultdict(list)
    for s in sources:
        clusters[s.cluster_id if s.cluster_id is not None else -s.id].append(s)

    def _when(s: Source) -> datetime:
        return s.published_at or (s.chat_message.sent_at if s.chat_message else None) or s.created_at or datetime.min

    groups = []
    for cid, items in clusters.items():
        items.sort(key=lambda s: (not s.is_primary, _when(s)))
        groups.append({"cluster_id": cid, "primary": items[0], "duplicates": items[1:],
                       "kinds": sorted({_enum_val(s.kind) for s in items})})
    groups.sort(key=lambda g: (not g["primary"].is_primary or False,
                               not g["primary"].is_important,
                               -(g["primary"].uniqueness_score or 0)))
    numbering: dict[int, int] = {}
    for g in groups:
        for s in [g["primary"], *g["duplicates"]]:
            numbering[s.id] = len(numbering) + 1
    dup_count = {s.id: len(clusters[s.cluster_id if s.cluster_id is not None else -s.id]) - 1 for s in sources}
    sources_json = {s.id: _source_dict(s, numbering[s.id], dup_count[s.id], chat_titles) for s in sources}
    kind_counts: dict[str, int] = defaultdict(int)
    for s in sources:
        kind_counts[_enum_val(s.kind)] += 1
    return {
        "sources": sources, "sources_by_id": {s.id: s for s in sources}, "clusters": dict(clusters),
        "source_groups": groups, "source_num": numbering, "sources_json": sources_json,
        "chat_titles": chat_titles, "dup_count": dup_count, "kind_counts": dict(kind_counts),
    }


async def _contacts(session: AsyncSession, sources: list[Source], chat_titles: dict[int, str]) -> list[dict]:
    """«Кому позвонить» по контракту: max_chat + primary + uniqueness>=0.5 + important + профиль."""
    picked = [s for s in sources if s.kind == SourceKind.max_chat and s.is_primary and s.is_important
              and (s.uniqueness_score or 0) >= 0.5 and s.chat_message is not None]
    ext_ids = {s.chat_message.author_external_id for s in picked}
    profiles: dict[str, AuthorProfile] = {}
    if ext_ids:
        for p in await session.scalars(select(AuthorProfile).where(AuthorProfile.external_id.in_(ext_ids))):
            profiles[p.external_id] = p
    out = []
    for s in sorted(picked, key=lambda s: -(s.uniqueness_score or 0)):
        m = s.chat_message
        prof = profiles.get(m.author_external_id)
        out.append({
            "source_id": s.id, "author_external_id": m.author_external_id,
            "name": (prof.name if prof and prof.name else None) or m.author_name or "Автор скрыт",
            "phone": prof.phone if prof else None, "username": prof.username if prof else None,
            "quote": m.text, "chat_title": chat_titles.get(m.chat_id), "sent_at": m.sent_at,
            "message_url": s.url, "uniqueness_score": s.uniqueness_score, "has_media": m.has_media,
        })
    return out


@router.get("/investigations/{inv_id}", response_class=HTMLResponse)
async def investigation_detail(request: Request, inv_id: int, tab: str = "progress",
                               user: User = Depends(current_user),
                               session: AsyncSession = Depends(get_session)):
    """investigation.html. tab: progress|report|sources|contacts|media|draft|history."""
    inv = await _load_inv(session, inv_id)
    if inv is None:
        return await _error(request, user, session, 404, f"Расследование #{inv_id} не найдено")
    tabs = ["progress", "report", "sources", "contacts", "media", "draft", "history"]
    if tab not in tabs:
        tab = "progress"

    iterations = list(await session.scalars(select(SearchIteration)
                                            .where(SearchIteration.investigation_id == inv_id)
                                            .order_by(SearchIteration.n, SearchIteration.id)))
    rep = await session.get(Report, inv_id)
    report = rep.report_json if rep else None
    sctx = await _sources_ctx(session, inv_id)
    can_see_contacts = has_role(user, P)
    contacts = await _contacts(session, sctx["sources"], sctx["chat_titles"]) if can_see_contacts else []
    media = list(await session.scalars(select(MediaItem).where(MediaItem.investigation_id == inv_id)
                                       .order_by(MediaItem.possibly_old, MediaItem.id)))
    drafts = list(await session.scalars(select(Draft).where(Draft.investigation_id == inv_id)
                                        .order_by(Draft.version)))
    latest_draft = drafts[-1] if drafts else None
    history = list(await session.scalars(select(StatusHistory)
                                         .where(StatusHistory.investigation_id == inv_id)
                                         .order_by(StatusHistory.created_at, StatusHistory.id)))
    fact_marks = list(await session.scalars(select(FactMark).where(FactMark.investigation_id == inv_id)
                                            .order_by(FactMark.created_at, FactMark.id)))
    feedback = list(await session.scalars(select(Feedback).where(Feedback.investigation_id == inv_id)
                                          .order_by(Feedback.created_at.desc())))
    users_by_id = {u.id: u for u in await _all_users(session)}
    claims = (latest_draft.claims_json if latest_draft else None) or []
    states = _effective_states(report, claims, fact_marks)
    draft_diff = None
    if latest_draft and drafts[0].id != latest_draft.id:
        draft_diff = word_diff(drafts[0].body, latest_draft.body, drafts[0].version, latest_draft.version)
    if tab == "progress" and inv.status not in (S.new, S.researching) and report and \
            "tab" not in request.query_params:
        tab = "report"

    ctx = {
        "inv": inv, "tab": tab, "iterations": iterations, "report": report, **sctx,
        "contacts": contacts, "can_see_contacts": can_see_contacts, "media": media,
        "drafts": drafts, "latest_draft": latest_draft, "history": history,
        "fact_marks": fact_marks, "feedback": feedback, "states": states, "claims": claims,
        "allowed_transitions": available_transitions(user.role, inv.status),
        "draft_diff": draft_diff, "users_by_id": users_by_id,
        "progress": inv.progress_json or {}, "entities": inv.entities_json or {},
        "can_mark": has_role(user, P, E), "can_feedback": has_role(user, P),
        "my_feedback": next((f for f in feedback if f.user_id == user.id), None),
    }
    return await _render(request, "investigation.html", ctx, user=user, session=session,
                         nav_active="investigation")


@router.get("/investigations/{inv_id}/progress.json")
async def investigation_progress_json(inv_id: int, user: User = Depends(current_user),
                                      session: AsyncSession = Depends(get_session)):
    """Резервный источник прогресса для app.js (формат = ProgressOut), если /api ещё отдаёт 501."""
    inv = await _load_inv(session, inv_id)
    if inv is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    iterations = await session.scalars(select(SearchIteration)
                                       .where(SearchIteration.investigation_id == inv_id)
                                       .order_by(SearchIteration.n, SearchIteration.id))
    return JSONResponse({
        "investigation_id": inv.id, "status": inv.status.value, "progress": inv.progress_json or {},
        "iterations": [{"id": it.id, "n": it.n, "queries_json": it.queries_json or [],
                        "rationale": it.rationale, "results_count": it.results_count,
                        "created_at": it.created_at.isoformat() if it.created_at else None}
                       for it in iterations],
    })


# ================================================================ редактор
@router.get("/investigations/{inv_id}/edit", response_class=HTMLResponse)
async def investigation_edit(request: Request, inv_id: int, user: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)):
    """editor.html: inv, ai_draft (v1), current_draft, diff, claims, sources_by_id, fact_marks,
    allowed_transitions."""
    if not has_role(user, E):
        return await _forbidden(request, user, session, E)
    inv = await _load_inv(session, inv_id)
    if inv is None:
        return await _error(request, user, session, 404, f"Расследование #{inv_id} не найдено")
    drafts = list(await session.scalars(select(Draft).where(Draft.investigation_id == inv_id)
                                        .order_by(Draft.version)))
    ai_draft = next((d for d in drafts if d.author_kind == DraftAuthorKind.ai), drafts[0] if drafts else None)
    current = drafts[-1] if drafts else None
    diff = word_diff(ai_draft.body, current.body, ai_draft.version, current.version) if ai_draft and current else None
    claims = (current.claims_json if current and current.claims_json else
              (ai_draft.claims_json if ai_draft else None)) or []
    fact_marks = list(await session.scalars(select(FactMark).where(FactMark.investigation_id == inv_id)
                                            .order_by(FactMark.created_at, FactMark.id)))
    rep = await session.get(Report, inv_id)
    sctx = await _sources_ctx(session, inv_id)
    states = _effective_states(rep.report_json if rep else None, claims, fact_marks)
    claims_view = []
    for c in claims:
        st = states.get(str(c.get("id")), {"state": c.get("status")})
        claims_view.append({**c, "state": st.get("state"), "by_human": st.get("by_human", False),
                            "source_ids": [sid for sid in (c.get("source_ids") or [])]})
    ctx = {
        "inv": inv, "drafts": drafts, "ai_draft": ai_draft, "current_draft": current, "diff": diff,
        "claims": claims_view, "fact_marks": fact_marks,
        "allowed_transitions": available_transitions(user.role, inv.status),
        "can_edit": inv.status == S.editing and has_role(user, E), **sctx,
    }
    return await _render(request, "editor.html", ctx, user=user, session=session, nav_active="edit")


@router.post("/investigations/{inv_id}/edit")
async def investigation_edit_save(request: Request, inv_id: int, title: str = Form(""),
                                  body: str = Form(""), user: User = Depends(current_user),
                                  session: AsyncSession = Depends(get_session)):
    """Сохранить новую версию (author_kind=editor); 303 на /investigations/{id}/edit."""
    if not has_role(user, E):
        return await _forbidden(request, user, session, E)
    inv = await _load_inv(session, inv_id)
    if inv is None:
        return await _error(request, user, session, 404, f"Расследование #{inv_id} не найдено")
    url = f"/investigations/{inv_id}/edit"
    if inv.status != S.editing:
        return _redirect(url, "Сохранять правки можно только в статусе «На редактуре»", "err")
    if not body.strip():
        return _redirect(url, "Текст черновика не может быть пустым", "err")
    prev = await session.scalar(select(Draft).where(Draft.investigation_id == inv_id)
                                .order_by(Draft.version.desc()).limit(1))
    version = (prev.version + 1) if prev else 1
    now = utcnow()
    session.add(Draft(investigation_id=inv_id, version=version, author_kind=DraftAuthorKind.editor,
                      author_user_id=user.id, title=(title or (prev.title if prev else inv.title))[:512],
                      body=body.replace("\r\n", "\n"),
                      claims_json=[dict(c) for c in (prev.claims_json or [])] if prev else [],
                      created_at=now))
    if inv.editor_id is None and user.role == Role.editor:
        inv.editor_id = user.id
    inv.updated_at = now
    await session.commit()
    return _redirect(url, f"Правка сохранена как версия v{version}")


# ================================================================ действия
@router.post("/investigations/{inv_id}/transition")
async def investigation_transition(request: Request, inv_id: int, to_status: str = Form(...),
                                   comment: str | None = Form(None),
                                   user: User = Depends(current_user),
                                   session: AsyncSession = Depends(get_session)):
    """workflow.transition; 303 назад (Referer) или на /investigations/{id}."""
    inv = await _load_inv(session, inv_id)
    if inv is None:
        return await _error(request, user, session, 404, f"Расследование #{inv_id} не найдено")
    back = _back(request, f"/investigations/{inv_id}")
    try:
        to = S(to_status)
    except ValueError:
        return _redirect(back, "Неизвестный статус", "err")
    try:
        await do_transition(session, inv, to, user, (comment or "").strip() or None)
    except _TransitionFailed as e:
        await session.rollback()
        return _redirect(back, e.message, "err")
    await session.commit()
    msg = f"Статус: «{status_label(to)}»"
    level = "ok"
    if to == S.researching:
        warn = await _start_pipeline(inv.id)
        if warn:
            msg, level = warn, "warn"
        back = f"/investigations/{inv_id}?tab=progress"
    elif "/edit" in back and to != S.editing:
        back = f"/investigations/{inv_id}?tab=draft"
    elif back.rstrip("/") == "" or back == "/":
        pass
    return _redirect(back, msg, level)


@router.post("/investigations/{inv_id}/facts")
async def investigation_mark_fact(request: Request, inv_id: int, fact_text: str = Form(...),
                                  state: str = Form(...), claim_id: str | None = Form(None),
                                  comment: str | None = Form(None),
                                  user: User = Depends(current_user),
                                  session: AsyncSession = Depends(get_session)):
    """Отметка факта продюсером/редактором. Повторная отметка того же claim_id тем же
    пользователем — обновление. Для fetch (Accept: application/json) отдаёт JSON."""
    wants_json = "application/json" in request.headers.get("accept", "")
    if not has_role(user, P, E):
        if wants_json:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await _forbidden(request, user, session, P, E)
    inv = await _load_inv(session, inv_id)
    if inv is None:
        return await _error(request, user, session, 404, f"Расследование #{inv_id} не найдено")
    try:
        st = FactState(state)
    except ValueError:
        return _redirect(_back(request, f"/investigations/{inv_id}"), "Неизвестная отметка", "err")
    mark = None
    if claim_id:
        mark = await session.scalar(select(FactMark).where(
            FactMark.investigation_id == inv_id, FactMark.claim_id == claim_id,
            FactMark.user_id == user.id))
    now = utcnow()
    if mark is None:
        mark = FactMark(investigation_id=inv_id, claim_id=claim_id or None, fact_text=fact_text[:4000],
                        state=st, user_id=user.id, comment=(comment or None), created_at=now)
        session.add(mark)
    else:
        mark.state, mark.comment, mark.fact_text, mark.created_at = st, comment or None, fact_text[:4000], now
    await session.commit()
    if wants_json:
        return JSONResponse({"ok": True, "claim_id": claim_id, "state": st.value,
                             "label": fact_label(st.value), "by": user.name})
    return _redirect(_back(request, f"/investigations/{inv_id}?tab=report"),
                     f"Факт отмечен: {fact_label(st.value)}", anchor=f"item-{claim_id}" if claim_id else None)


# ================================================================ AI-оператор
async def _metrics(session: AsyncSession) -> tuple[dict[str, Any], bool]:
    try:
        from app.services.metrics import compute_metrics

        return dict(await compute_metrics(session)), True
    except NotImplementedError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("compute_metrics failed")
    rows = await session.execute(select(Investigation.status, func.count()).group_by(Investigation.status))
    by_status = {s.value: 0 for s in STATUS_ORDER}
    for st, n in rows:
        by_status[_enum_val(st)] = n
    fb_total = await session.scalar(select(func.count()).select_from(Feedback)) or 0
    return {
        "time_to_draft_avg_sec": None, "source_coverage": None, "traceability": None,
        "draft_acceptance_rate": None, "factcheck_correction_rate": None, "feedback_good_share": None,
        "investigations_total": sum(by_status.values()), "by_status": by_status, "feedback_total": fb_total,
    }, False


async def _chats(session: AsyncSession) -> list[MaxChatOut]:
    counts = dict((await session.execute(select(ChatMessage.chat_id, func.count())
                                         .group_by(ChatMessage.chat_id))).all())
    out = []
    for c in await session.scalars(select(MaxChat).order_by(MaxChat.title)):
        o = MaxChatOut.model_validate(c)
        o.messages_count = counts.get(c.id, 0)
        out.append(o)
    return out


@router.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request, user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    """operator.html: board, metrics, metric_labels, feedback, chats, channels, sync_result."""
    if not has_role(user, OP):
        return await _forbidden(request, user, session, OP)
    board = await _build_board(session)
    metrics, metrics_live = await _metrics(session)
    try:
        from app.services.metrics import METRIC_KEYS
        metric_labels = dict(METRIC_KEYS)
    except Exception:  # noqa: BLE001
        metric_labels = {}
    feedback = list(await session.scalars(select(Feedback).order_by(Feedback.created_at.desc()).limit(40)))
    inv_titles = dict((await session.execute(select(Investigation.id, Investigation.title))).all())
    chats = await _chats(session)
    last_sync = max((c.last_synced_at for c in chats if c.last_synced_at), default=None)
    qp = request.query_params
    sync_result = None
    if "synced" in qp:
        sync_result = {"new_messages": qp.get("synced"), "chats": qp.get("chats"), "errors": qp.get("errors")}
    ctx = {
        "board": board, "metrics": metrics, "metrics_live": metrics_live, "metric_labels": metric_labels,
        "ui_metrics": UI_METRICS, "feedback": feedback, "inv_titles": inv_titles, "chats": chats,
        "channels": await _channels(session), "sync_result": sync_result, "last_sync": last_sync,
        "stale_after": settings.STALE_AFTER_MINUTES,
        "good_count": sum(1 for f in feedback if f.verdict == Verdict.good),
        "bad_count": sum(1 for f in feedback if f.verdict == Verdict.bad),
    }
    return await _render(request, "operator.html", ctx, user=user, session=session, nav_active="operator")


@router.post("/operator/chats/{chat_id}/toggle")
async def operator_toggle_chat(request: Request, chat_id: int, user: User = Depends(current_user),
                               session: AsyncSession = Depends(get_session)):
    if not has_role(user, OP):
        return await _forbidden(request, user, session, OP)
    chat = await session.get(MaxChat, chat_id)
    if chat is None:
        return _redirect("/operator", "Чат не найден", "err", anchor="chats")
    chat.enabled = not chat.enabled
    await session.commit()
    return _redirect("/operator", f"Чат «{chat.title}» {'подключён' if chat.enabled else 'отключён'}",
                     anchor="chats")


@router.post("/operator/sync")
async def operator_sync(request: Request, user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    """poller.sync_once; 303 на /operator?synced=N."""
    if not has_role(user, OP):
        return await _forbidden(request, user, session, OP)
    try:
        from app.integrations.max.poller import sync_once

        res = await sync_once(session)
    except NotImplementedError:
        return _redirect("/operator", "Синхронизация MAX ещё не реализована бэкендом", "warn", anchor="chats")
    except Exception as e:  # noqa: BLE001
        log.exception("sync failed")
        return _redirect("/operator", f"Ошибка синхронизации: {e}", "err", anchor="chats")
    url = _with_params("/operator", synced=res.new_messages, chats=res.chats,
                       errors=len(res.errors) or None)
    return _redirect(url, f"Синхронизировано: {res.chats} чатов, новых сообщений — {res.new_messages}",
                     "warn" if res.errors else "ok", anchor="chats")


# ================================================================ фидбэк
@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request, investigation_id: int | None = None,
                        user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    """feedback.html: investigations (мои, без отзыва — первыми), selected_id, my_feedback."""
    if not has_role(user, P):
        return await _forbidden(request, user, session, P)
    q = select(Investigation).order_by(Investigation.updated_at.desc())
    if user.role == Role.producer:
        q = q.where(Investigation.producer_id == user.id)
    invs = list(await session.scalars(q))
    my_feedback = list(await session.scalars(select(Feedback).where(Feedback.user_id == user.id)
                                             .order_by(Feedback.created_at.desc())))
    rated = {f.investigation_id for f in my_feedback}
    invs.sort(key=lambda i: i.id in rated)
    selected = investigation_id if investigation_id and any(i.id == investigation_id for i in invs) else \
        next((i.id for i in invs if i.id not in rated), invs[0].id if invs else None)
    ctx = {"investigations": invs, "selected_id": selected, "my_feedback": my_feedback, "rated": rated,
           "inv_titles": {i.id: i.title for i in invs}}
    return await _render(request, "feedback.html", ctx, user=user, session=session, nav_active="feedback")


@router.post("/feedback")
async def feedback_submit(request: Request, investigation_id: int = Form(...),
                          verdict: str = Form(...), comment: str | None = Form(None),
                          user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    """Сохранить Feedback; 303 на Referer или /feedback."""
    if not has_role(user, P):
        return await _forbidden(request, user, session, P)
    back = _back(request, "/feedback")
    inv = await _load_inv(session, investigation_id)
    if inv is None:
        return _redirect(back, "Расследование не найдено", "err")
    try:
        v = Verdict(verdict)
    except ValueError:
        return _redirect(back, "Выберите «норм» или «стрём»", "err")
    session.add(Feedback(investigation_id=inv.id, user_id=user.id, verdict=v,
                         comment=(comment or "").strip() or None, created_at=utcnow()))
    await session.commit()
    return _redirect(back, "Спасибо! Оценка «%s» сохранена" % ("норм" if v == Verdict.good else "стрём"),
                     anchor="feedback")

