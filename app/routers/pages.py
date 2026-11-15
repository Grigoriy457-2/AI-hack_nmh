"""HTML-страницы (Jinja2). Зона FRONTEND. Контракт — docs/CONTRACTS_v2.md §5.

Один пользователь — редактор. Экраны: Радар ``/``, Сюжет ``/stories/{id}``, Сюжеты ``/stories``,
Источники ``/sources``, Статистика ``/stats``, ``/login``. Живые данные (лента, возникающие сюжеты,
прогресс) рендерятся в шаблон как JSON и дальше обновляются из ``/api/*`` (app.js). Действия на
страницах — через JSON API; для форм без JS есть POST-эндпоинты с PRG-редиректом.
Сервисы бэкенда импортируются лениво, чтобы страницы не падали при импорте, пока бэкенд меняется.
"""
from __future__ import annotations

import logging

import jinja2
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app import models as M
from app.auth import current_user, current_user_optional
from app.config import settings
from app.db import get_session

log = logging.getLogger("newsflow.pages")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = TEMPLATES_DIR.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["pages"], include_in_schema=False)

S = M.InvestigationStatus
STATUS_LABELS: dict = getattr(M, "STATUS_LABELS", {})
STATUS_ORDER: list = list(getattr(M, "STATUS_ORDER", list(S)))
ACTIVE_STATUSES: set = set(getattr(M, "ACTIVE_STATUSES", {s for s in S if s.value in ("new", "researching", "review")}))
HANDOFF_CHANNELS: dict[str, str] = dict(getattr(M, "HANDOFF_CHANNELS", {
    "site": "Сайт", "telegram": "Telegram-канал", "max": "Канал MAX", "tv": "ТВ/радио",
    "print": "Печать/газета", "social": "Соцсети", "other": "Другое"}))
PLATFORM_LABELS: dict[str, str] = {"max": "MAX", "telegram": "Telegram", "rss": "СМИ (RSS)",
                                   **dict(getattr(M, "PLATFORM_LABELS", {}))}
PLATFORM_SHORT = {"max": "MAX", "telegram": "TG", "rss": "СМИ"}
KIND_LABELS = {"channel": "канал", "group": "группа", "chat": "чат", "user": "пользователь", "feed": "лента"}
SourceChat = getattr(M, "SourceChat", None) or M.MaxChat

SOURCE_KIND_LABELS = {"web": "Сайт / СМИ", "max_chat": "Чат", "official": "Официальный", "media": "Медиа"}
AGENT_LABELS = {"web": "веб", "max_chats": "чаты", "max_chat": "чаты", "official": "официальные",
                "media": "медиа", "telegram": "Telegram"}
FACT_STATE_LABELS = {"confirmed": "подтверждён", "unconfirmed": "не подтверждён", "false": "ложь"}
REPORT_FACT_SECTIONS = {"timeline", "facts", "confirmed", "unconfirmed", "contradictions", "to_verify"}

DEMO_LEAD = (
    "В чатах Чертанова пишут, что около 14:30 у жилого дома на Варшавском шоссе "
    "был громкий хлопок. Жильцы говорят про запах гари и выбитые стёкла на 3-м этаже, "
    "кто-то видел скорую и пожарных. Официальной информации пока нет."
)

# ================================================================ Jinja: фильтры
MSK = timedelta(hours=3)
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _to_dt(value: Any) -> datetime | None:
    if value in (None, ""):
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
    """UTC -> МСК. short: «14:31, 14 ноя»; time: «14:31»; date: «14 ноя»."""
    d = _to_dt(value)
    if d is None or d.year in (1, 9999):
        return "—"
    try:
        m = d + MSK
    except OverflowError:
        return "—"
    if mode == "time":
        return m.strftime("%H:%M")
    if mode == "date":
        return f"{m.day} {MONTHS[m.month - 1]}"
    return f"{m.strftime('%H:%M')}, {m.day} {MONTHS[m.month - 1]}"


def fmt_ago(value: Any) -> str:
    d = _to_dt(value)
    if d is None:
        return "—"
    minutes = int((M.utcnow() - d).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 60 * 24:
        return f"{minutes // 60} ч назад"
    return f"{minutes // 1440} дн назад"


def fmt_dur(seconds: Any) -> str:
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


def fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def ev(x: Any) -> Any:
    # Отсутствующее поле в шаблоне (jinja2 Undefined) — это «нет значения», а не ошибка.
    if isinstance(x, jinja2.Undefined):
        return None
    return getattr(x, "value", x)


def status_label(s: Any) -> str:
    try:
        return STATUS_LABELS[S(ev(s))]
    except (ValueError, KeyError):
        return str(ev(s) or "—")


def initials(name: str | None) -> str:
    parts = [p for p in re.split(r"[^\w]+", name or "") if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def safe_url(url: Any) -> str | None:
    if not url:
        return None
    u = str(url).strip()
    return u if u.lower().startswith(("http://", "https://")) else None


def host_of(url: Any) -> str:
    try:
        return urlsplit(str(url)).netloc.removeprefix("www.") or str(url)
    except ValueError:
        return str(url)


_TG_POST = re.compile(r"^/(?:s/)?[\w\d_]+/\d+/?$")


def link_label(url: Any, platform: Any = None, link_kind: Any = None) -> str | None:
    """Подпись ссылки на источник. Telegram/MAX: пост → «Открыть пост в …», канал → «Открыть канал».
    Сайты/RSS — домен издания. Нет http(s)-ссылки → None (ссылку не рисуем)."""
    u = safe_url(url)
    if not u:
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    host = parts.netloc.lower().removeprefix("www.")
    p = str(ev(platform) or "").lower()
    if not p or p in ("web", "official", "media", "max_chat"):
        p = "telegram" if host in ("t.me", "telegram.me") else "max" if host.endswith("max.ru") else p
    kind = str(ev(link_kind) or "").lower()
    if p in ("telegram", "max"):
        if not kind:
            path = parts.path
            if p == "telegram":
                kind = "post" if _TG_POST.match(path) else "chat"
            else:
                kind = "post" if len([s for s in path.split("/") if s]) >= 2 else "chat"
        if kind == "post":
            return "Открыть пост в Telegram" if p == "telegram" else "Открыть пост в MAX"
        return "Открыть канал"
    return host or None


def tel_href(phone: Any) -> str:
    return "tel:" + re.sub(r"[^\d+]", "", str(phone or ""))


def plural(n: Any, one: str, few: str, many: str) -> str:
    try:
        n = abs(int(n or 0))
    except (TypeError, ValueError):
        n = 0
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def asset(path: str) -> str:
    try:
        v = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        v = 0
    return f"/static/{path}?v={v}"


def to_json_data(obj: Any) -> Any:
    return jsonable_encoder(obj)


env = templates.env
env.filters.update(dt=fmt_dt, ago=fmt_ago, dur=fmt_dur, pct=fmt_pct, ev=ev, status_label=status_label,
                   initials=initials, safe_url=safe_url, host=host_of, tel=tel_href, jsonable=to_json_data)
env.globals.update(STATUS_LABELS=STATUS_LABELS, STATUS_ORDER=STATUS_ORDER, HANDOFF_CHANNELS=HANDOFF_CHANNELS,
                   PLATFORM_LABELS=PLATFORM_LABELS, PLATFORM_SHORT=PLATFORM_SHORT, KIND_LABELS=KIND_LABELS,
                   SOURCE_KIND_LABELS=SOURCE_KIND_LABELS, AGENT_LABELS=AGENT_LABELS,
                   FACT_STATE_LABELS=FACT_STATE_LABELS, plural=plural, asset=asset, APP_NAME=settings.APP_NAME,
                   link_label=link_label)


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


def _redirect(url: str, msg: str | None = None, level: str = "ok") -> RedirectResponse:
    return RedirectResponse(_with_params(url, msg=msg, level=level) if msg else url, status_code=303)


async def _live_sources_count(session: AsyncSession) -> int:
    try:
        return int(await session.scalar(select(func.count()).select_from(SourceChat)
                                        .where(SourceChat.enabled.is_(True))) or 0)
    except Exception:  # noqa: BLE001
        return 0


_MODE_CACHE: dict[str, Any] = {"at": None, "data": None}


async def _data_mode() -> dict[str, Any] | None:
    """Режим данных для шапки (= GET /api/mode), кэш 30 с. Ошибки не роняют страницу."""
    now = M.utcnow()
    at = _MODE_CACHE["at"]
    if at is not None and (now - at).total_seconds() < 30:
        return _MODE_CACHE["data"]
    data = None
    try:
        from app.routers.api import get_mode

        data = jsonable_encoder(await get_mode())
    except Exception:  # noqa: BLE001
        log.debug("data mode unavailable", exc_info=True)
    if data is not None:
        is_demo = data.get("data_mode") == "demo"
        llm = data.get("llm")
        data["ui_label"] = "Демо-данные" if is_demo else "Реальные данные"
        llm_s = str(llm or "").lower()
        if llm_s == "extractive":
            data["ui_ai"], data["llm_kind"] = "Без AI: только цитаты", "extractive"
        elif llm_s == "mock":
            data["ui_ai"], data["llm_kind"] = "AI-заглушка (демо)", "mock"
        elif llm_s:
            # v4: LLM — Grok (xAI); любые другие провайдеры показываем как «AI-анализ»
            data["ui_ai"] = "AI-анализ (Grok)" if ("grok" in llm_s or "xai" in llm_s) else "AI-анализ"
            data["llm_kind"] = "llm"
        else:
            data["ui_ai"], data["llm_kind"] = None, None
        data["is_demo"] = is_demo
    _MODE_CACHE.update(at=now, data=data)
    return data


async def _render(request: Request, name: str, ctx: dict[str, Any], *, user: Any, session: AsyncSession,
                  nav_active: str = "", status_code: int = 200) -> HTMLResponse:
    base = {"request": request, "user": user, "nav_active": nav_active, "flash": _flash(request),
            "live_sources_count": await _live_sources_count(session) if user else 0,
            "data_mode": await _data_mode()}
    base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


async def _error(request: Request, user: Any, session: AsyncSession, status_code: int,
                 message: str) -> HTMLResponse:
    """Используется и в app/main.py для 403/404 на HTML-страницах."""
    return await _render(request, "error.html", {"status_code": status_code, "message": message},
                         user=user, session=session, status_code=status_code)


def _story_error_text(e: Exception) -> str:
    return str(getattr(e, "detail", None) or e)


async def _first_editor(session: AsyncSession) -> Any:
    return await session.scalar(select(M.User).order_by(M.User.id).limit(1))


async def _channels(session: AsyncSession) -> list[Any]:
    return list(await session.scalars(select(M.Channel).order_by(M.Channel.id)))


# ================================================================ вход
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: Any = Depends(current_user_optional),
                     session: AsyncSession = Depends(get_session)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return await _render(request, "login.html", {"editor": await _first_editor(session), "error": None},
                         user=None, session=session, nav_active="login")


@router.post("/login")
async def login_submit(request: Request, user_id: int | None = Form(None), password: str = Form("demo"),
                       next: str | None = Form(None), session: AsyncSession = Depends(get_session)):
    target = await session.get(M.User, user_id) if user_id else await _first_editor(session)
    if target is None or not auth.verify_password(password):
        return await _render(request, "login.html",
                             {"editor": await _first_editor(session),
                              "error": "Не удалось войти: нет пользователя-редактора или пароль не «demo». "
                                       "Пересоздайте демо-данные: python -m app.seed --reset"},
                             user=None, session=session, nav_active="login", status_code=401)
    dest = next if next and next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(dest, status_code=303)
    auth.set_login_cookie(resp, target)
    return resp


@router.get("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    auth.clear_login_cookie(resp)
    return resp


# ================================================================ Радар
async def _demo_status() -> dict[str, Any] | None:
    try:
        from app.integrations.max import demo_stream

        return jsonable_encoder(demo_stream.status())
    except Exception:  # noqa: BLE001
        return None


@router.get("/", response_class=HTMLResponse)
async def radar(request: Request, user: Any = Depends(current_user), session: AsyncSession = Depends(get_session)):
    from app.services import emerging as emerging_svc
    from app.services import feed as feed_svc
    from app.services import stories as stories_svc

    feed = await feed_svc.get_feed(session, limit=60)
    emerging = await emerging_svc.compute_emerging(session)
    all_stories = await stories_svc.list_stories(session)
    in_work = [s for s in all_stories if s.status in ACTIVE_STATUSES]
    # архив (rejected) на радаре не показываем
    recent_done = [s for s in all_stories if ev(s.status) == "handed_off"][:4]
    ctx = {
        "feed": jsonable_encoder(feed), "emerging": jsonable_encoder(emerging),
        "in_work": in_work, "recent_done": recent_done, "channels": await _channels(session),
        "demo_lead": DEMO_LEAD, "demo": await _demo_status(),
    }
    return await _render(request, "radar.html", ctx, user=user, session=session, nav_active="radar")


@router.post("/stories/new")
async def story_create_form(request: Request, lead_text: str = Form(""), title: str | None = Form(None),
                            channel_id: str | None = Form(None), user: Any = Depends(current_user),
                            session: AsyncSession = Depends(get_session)):
    """«Своя зацепка» без JS."""
    from app.services import stories as stories_svc

    lead = (lead_text or "").strip()
    if len(lead) < 3:
        return _redirect("/", "Опишите зацепку хотя бы парой слов", "err")
    ch = int(channel_id) if channel_id and channel_id.isdigit() else None
    try:
        inv = await stories_svc.create_story(session, user, lead, title=(title or "").strip() or None, channel_id=ch)
    except Exception as e:  # noqa: BLE001
        log.exception("create_story failed")
        return _redirect("/", f"Не удалось создать сюжет: {_story_error_text(e)}", "err")
    return _redirect(f"/stories/{inv.id}")


@router.post("/emerging/{key}/investigate")
async def emerging_investigate_form(key: str, user: Any = Depends(current_user),
                                    session: AsyncSession = Depends(get_session)):
    from app.services import emerging as emerging_svc

    try:
        inv, _created = await emerging_svc.investigate_emerging(session, key, user)
    except Exception as e:  # noqa: BLE001
        return _redirect("/", f"Не удалось начать проверку: {_story_error_text(e)}", "err")
    return _redirect(f"/stories/{inv.id}")


# ================================================================ Сюжеты (список)
@router.get("/stories", response_class=HTMLResponse)
async def stories_list(request: Request, status: str | None = None, q: str | None = None,
                       archived: str | None = None,
                       user: Any = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """«В работе» (по умолчанию: всё, кроме rejected) и «Архив» (?archived=1: rejected)."""
    from app.services import stories as stories_svc

    all_stories = list(await stories_svc.list_stories(session, q=(q or None)))
    try:  # v4: сервис может прятать архив по умолчанию — добираем его явно
        seen = {s.id for s in all_stories}
        all_stories += [s for s in await stories_svc.list_stories(session, q=(q or None), archived=True)
                        if s.id not in seen]
    except TypeError:
        pass
    all_stories.sort(key=lambda s: s.updated_at or datetime.min, reverse=True)
    is_archive = archived in ("1", "true", "yes")
    active_list = [s for s in all_stories if ev(s.status) != "rejected"]
    archive_list = [s for s in all_stories if ev(s.status) == "rejected"]
    pool = archive_list if is_archive else active_list
    counts = defaultdict(int)
    for s in active_list:
        counts[ev(s.status)] += 1
    st = status if not is_archive and status in {ev(x) for x in STATUS_ORDER if ev(x) != "rejected"} else None
    items = [s for s in pool if st is None or ev(s.status) == st]
    ctx = {"stories": items, "counts": dict(counts), "total": len(active_list), "archive_total": len(archive_list),
           "is_archive": is_archive, "status": st, "q": q or ""}
    return await _render(request, "stories.html", ctx, user=user, session=session, nav_active="stories")


@router.get("/investigations/{inv_id}")
async def legacy_investigation(inv_id: int) -> RedirectResponse:
    return RedirectResponse(f"/stories/{inv_id}", status_code=301)


@router.get("/investigations/{inv_id}/edit")
async def legacy_investigation_edit(inv_id: int) -> RedirectResponse:
    return RedirectResponse(f"/stories/{inv_id}#draft", status_code=301)


# ================================================================ Сюжет
async def _sources_ctx(session: AsyncSession, inv_id: int) -> dict[str, Any]:
    """Источники сюжета, сгруппированные по кластерам дублей (кросс-платформенные кластеры)."""
    sources = list(await session.scalars(select(M.Source).where(M.Source.investigation_id == inv_id)
                                         .order_by(M.Source.id)))
    chat_ids = {s.chat_message.chat_id for s in sources if s.chat_message}
    chats: dict[int, Any] = {}
    if chat_ids:
        for c in await session.scalars(select(SourceChat).where(SourceChat.id.in_(chat_ids))):
            chats[c.id] = c

    def platform_of(s: Any) -> str | None:
        msg = s.chat_message
        if msg is None:
            return None
        p = ev(getattr(msg, "platform", None)) or ev(getattr(chats.get(msg.chat_id), "platform", None))
        return p or "max"

    def when(s: Any) -> datetime:
        return s.published_at or (s.chat_message.sent_at if s.chat_message else None) or s.created_at or datetime.min

    clusters: dict[int, list[Any]] = defaultdict(list)
    for s in sources:
        clusters[s.cluster_id if s.cluster_id is not None else -s.id].append(s)
    groups = []
    for cid, items in clusters.items():
        items.sort(key=lambda s: (not s.is_primary, when(s)))
        plats = sorted({p for p in (platform_of(s) for s in items) if p})
        groups.append({"primary": items[0], "duplicates": items[1:], "platforms": plats,
                       "cross": len(plats) > 1})
    groups.sort(key=lambda g: (ev(g["primary"].kind) != "official", not g["primary"].is_important,
                               -(g["primary"].uniqueness_score or 0)))
    numbering: dict[int, int] = {}
    for g in groups:
        for s in [g["primary"], *g["duplicates"]]:
            numbering[s.id] = len(numbering) + 1

    src_json = {}
    for s in sources:
        msg = s.chat_message
        chat = chats.get(msg.chat_id) if msg else None
        src_json[s.id] = {
            "id": s.id, "n": numbering[s.id], "kind": ev(s.kind), "platform": platform_of(s),
            "title": s.title or (f"Сообщение: {msg.author_name}" if msg else "Источник"),
            "url": safe_url(s.url), "chat": chat.title if chat else None,
        }
    return {"sources": sources, "source_groups": groups, "source_num": numbering, "sources_json": src_json,
            "chats_by_id": chats, "platform_of": {s.id: platform_of(s) for s in sources}}


def _effective_states(report: dict | None, claims: list[dict], marks: list[Any]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if report:
        for sec in report.get("sections") or []:
            for it in sec.get("items") or []:
                if it.get("id"):
                    out[str(it["id"])] = {"state": it.get("state"), "by_human": False}
    for c in claims or []:
        if c.get("id"):
            out[str(c["id"])] = {"state": c.get("status"), "by_human": False}
    for m in sorted(marks, key=lambda m: (m.created_at or datetime.min, m.id)):
        if m.claim_id:
            out[str(m.claim_id)] = {"state": ev(m.state), "by_human": True}
    return out


@router.get("/stories/{inv_id}", response_class=HTMLResponse)
async def story_page(request: Request, inv_id: int, user: Any = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    from app.services import stories as stories_svc

    inv = await session.get(M.Investigation, inv_id)
    if inv is None:
        return await _error(request, user, session, 404, f"Сюжета №{inv_id} нет. Возможно, демо-данные пересоздали.")
    detail = await stories_svc.story_detail(session, inv)
    iterations = list(await session.scalars(select(M.SearchIteration)
                                            .where(M.SearchIteration.investigation_id == inv_id)
                                            .order_by(M.SearchIteration.n, M.SearchIteration.id)))
    rep = await session.get(M.Report, inv_id)
    report = rep.report_json if rep else None
    sctx = await _sources_ctx(session, inv_id)
    try:
        # v4: «Кому позвонить» — только авторы с открытым телефоном
        contacts = [c for c in await stories_svc.list_contacts(session, inv_id) if getattr(c, "phone", None)]
    except Exception:  # noqa: BLE001
        log.exception("list_contacts failed")
        contacts = []
    media = list(await session.scalars(select(M.MediaItem).where(M.MediaItem.investigation_id == inv_id)
                                       .order_by(M.MediaItem.possibly_old, M.MediaItem.id)))
    drafts = list(await stories_svc.list_drafts(session, inv_id))
    drafts.sort(key=lambda d: d.version)
    ai_drafts = [d for d in drafts if ev(d.author_kind) == "ai"]
    ai_draft = ai_drafts[-1] if ai_drafts else (drafts[0] if drafts else None)
    current = drafts[-1] if drafts else None
    marks = list(await stories_svc.list_facts(session, inv_id))
    history = list(await stories_svc.list_history(session, inv_id))
    claims = (current.claims_json if current and current.claims_json else
              (ai_draft.claims_json if ai_draft else None)) or []
    states = _effective_states(report, claims, marks)

    # подсчёт отметок по справке для строки «проверено»
    tally = {"confirmed": 0, "open": 0, "false": 0}
    if report:
        for sec in report.get("sections") or []:
            if sec.get("key") not in REPORT_FACT_SECTIONS:
                continue
            for it in sec.get("items") or []:
                st = states.get(str(it.get("id")), {}).get("state")
                tally["confirmed" if st == "confirmed" else "false" if st == "false" else "open"] += 1

    status = ev(inv.status)
    page_data = {
        "id": inv.id, "status": status, "title": inv.title,
        "progress": inv.progress_json or {}, "sources": sctx["sources_json"],
        "claims": [{**c, "state": states.get(str(c.get("id")), {}).get("state", c.get("status"))} for c in claims],
        "ai": {"version": ai_draft.version, "title": ai_draft.title, "body": ai_draft.body} if ai_draft else None,
        "current": {"version": current.version, "title": current.title, "body": current.body,
                    "author_kind": ev(current.author_kind)} if current else None,
        "can_edit": status in ("review", "rejected"),
        "handoff_channels": [{"key": k, "label": v} for k, v in HANDOFF_CHANNELS.items()],
    }
    ctx = {
        "inv": inv, "detail": detail, "status": status, "iterations": iterations, "report": report, **sctx,
        "contacts": contacts, "media": media, "drafts": drafts, "ai_draft": ai_draft, "current_draft": current,
        "states": states, "claims": page_data["claims"], "history": history, "tally": tally,
        "progress": inv.progress_json or {}, "entities": inv.entities_json or {},
        "deeper_questions": getattr(inv, "deeper_questions", None) or [],
        "page_data": page_data, "fact_sections": REPORT_FACT_SECTIONS,
    }
    return await _render(request, "story.html", ctx, user=user, session=session, nav_active="story")


# ================================================================ Источники
@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request, user: Any = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    from app.services import sources as sources_svc

    chats = await sources_svc.list_source_chats(session)
    # время последнего сообщения по каждому источнику
    last_msg = dict((await session.execute(select(M.ChatMessage.chat_id, func.max(M.ChatMessage.sent_at))
                                           .group_by(M.ChatMessage.chat_id))).all())
    ctx = {"chats": chats, "last_msg": last_msg, "channels": await _channels(session),
           "demo": await _demo_status()}
    return await _render(request, "sources.html", ctx, user=user, session=session, nav_active="sources")


@router.post("/sources/add")
async def sources_add_form(platform: str = Form("auto"), ref: str = Form(""),
                           district: str | None = Form(None), user: Any = Depends(current_user),
                           session: AsyncSession = Depends(get_session)):
    from app.services import sources as sources_svc

    if not ref.strip():
        return _redirect("/sources", "Укажите @username или ссылку t.me", "err")
    try:
        chat, created = await sources_svc.add_source(session, user, platform, ref.strip(),
                                                     district=(district or "").strip() or None)
    except Exception as e:  # noqa: BLE001
        return _redirect("/sources", f"Источник не добавлен: {_story_error_text(e)}", "err")
    return _redirect("/sources", f"Источник «{chat.title}» {'добавлен' if created else 'уже был подключён'}")


# ================================================================ Статистика
@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, user: Any = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    from app.services import stats as stats_svc

    stats = jsonable_encoder(await stats_svc.compute_stats(session))
    return await _render(request, "stats.html", {"stats": stats}, user=user, session=session, nav_active="stats")
