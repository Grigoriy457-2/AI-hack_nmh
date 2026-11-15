"""JSON API v2 (prefix /api). Зона BACKEND. Контракт — docs/CONTRACTS_v2.md.

Один тип пользователя — редактор: везде достаточно входа (401). Бизнес-логика — в services/*
(stories, feed, emerging, sources, stats); здесь только HTTP-обвязка. Сюжеты доступны под
``/api/stories/...`` и под алиасом ``/api/investigations/...`` (скрыт из OpenAPI).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth, db
from app.auth import current_user
from app.config import settings
from app.db import get_session
from app.models import (
    HANDOFF_CHANNELS,
    Channel,
    Investigation,
    InvestigationStatus,
    MediaItem,
    Report,
    SearchIteration,
    SourceKind,
    User,
    utcnow,
)
from app.schemas import (
    ChannelOut,
    CommentIn,
    ContactOut,
    DeeperIn,
    DemoStreamOut,
    DraftCreate,
    DraftDiffOut,
    DraftOut,
    EmergingInvestigateIn,
    EmergingStoryOut,
    FactMarkIn,
    FactMarkOut,
    FeedOut,
    HandoffCheckOut,
    HandoffIn,
    HandoffResultOut,
    InvestigationCreate,
    InvestigationDetail,
    InvestigationOut,
    LoginIn,
    MediaItemOut,
    ModeOut,
    ProgressOut,
    ReportOut,
    SearchIterationOut,
    SourceAddIn,
    SourceChatOut,
    SourceChatUpdate,
    SourceDeleteOut,
    SourceOut,
    SourcePreviewOut,
    SourceResolveIn,
    StatsOut,
    StatusHistoryOut,
    SyncResult,
    TransitionIn,
    UserOut,
)
from app.services import emerging, feed, sources, stats, stories
from app.services.stories import StoryError

log = logging.getLogger("newsflow.api")

router = APIRouter(prefix="/api", tags=["api"])
S = InvestigationStatus


class _Err:
    """Контекст: StoryError -> HTTPException."""

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if isinstance(ev, StoryError):
            raise HTTPException(ev.status_code, ev.detail) from ev
        return False


def _err() -> _Err:
    return _Err()


async def _story(session: AsyncSession, inv_id: int) -> Investigation:
    with _err():
        return await stories.get_story(session, inv_id)


# ================================================================ служебное / auth
@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/users", response_model=list[UserOut])
async def list_users(session: AsyncSession = Depends(get_session)):
    """Публичный: карточки редакторов для страницы входа."""
    return (await session.scalars(select(User).order_by(User.id))).all()


@router.post("/auth/login", response_model=UserOut)
async def login(data: LoginIn, response: Response, session: AsyncSession = Depends(get_session)):
    user = None
    if data.user_id is not None:
        user = await session.get(User, data.user_id)
    elif data.login:
        user = await session.scalar(select(User).where(User.login == data.login.strip()))
    if user is None:
        raise HTTPException(404, "Пользователь не найден")
    if not auth.verify_password(data.password):
        raise HTTPException(401, "Неверный пароль")
    auth.set_login_cookie(response, user)
    return user


@router.post("/auth/demo-login", response_model=UserOut)
async def demo_login(response: Response, session: AsyncSession = Depends(get_session)):
    """«Войти как редактор» одной кнопкой (первый пользователь)."""
    user = await session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        raise HTTPException(404, "Нет ни одного редактора (запустите seed)")
    auth.set_login_cookie(response, user)
    return user


@router.post("/auth/logout", status_code=204)
async def logout(user: User = Depends(current_user)):
    resp = Response(status_code=204)
    auth.clear_login_cookie(resp)
    return resp


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user


# ================================================================ лента / реальное время
@router.get("/feed", response_model=FeedOut)
async def get_feed(since: str | None = None, limit: int = Query(50, ge=1, le=500), chat_id: int | None = None,
                   platform: str | None = None, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)):
    """Лента сообщений. ``since`` — курсор (id) или ISO-время; без него — последние ``limit``."""
    return await feed.get_feed(session, since=since, limit=limit, chat_id=chat_id, platform=platform)


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _sse(event: str | None, data: Any, *, id_: int | None = None) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=_json_default)
    head = (f"id: {id_}\n" if id_ is not None else "") + (f"event: {event}\n" if event else "")
    return head + "".join(f"data: {line}\n" for line in payload.splitlines() or [""]) + "\n"


def _story_event(inv: Investigation) -> dict[str, Any]:
    pj = inv.progress_json or {}
    return {
        "id": inv.id, "title": inv.title, "status": S(inv.status).value, "status_label": S(inv.status).label,
        "progress": {"state": pj.get("state"), "percent": pj.get("percent"), "current_stage": pj.get("current_stage")},
        "updated_at": inv.updated_at.isoformat() if inv.updated_at else None,
    }


def _emerging_sig(cards: list[EmergingStoryOut]) -> tuple:
    return tuple((c.key, c.messages_count, c.trend, c.investigation_id, c.investigation_status) for c in cards)


async def _stream(request: Request, since: int | None, timeout: float | None) -> AsyncIterator[str]:
    from app.integrations.max import demo_stream

    loop = asyncio.get_running_loop()
    started = loop.time()
    poll = max(0.2, float(settings.SSE_POLL_SEC))
    keepalive = max(1.0, float(settings.SSE_KEEPALIVE_SEC))
    yield "retry: 3000\n\n"
    async with db.SessionLocal() as s:
        cursor = since if since is not None else await feed.latest_cursor(s)
        cards = await emerging.compute_emerging(s)
        story_seen = {i: u for i, u in (await s.execute(select(Investigation.id, Investigation.updated_at))).all()}
    yield _sse("hello", {"server_time": utcnow().isoformat(), "cursor": cursor,
                         "demo": DemoStreamOut(**demo_stream.status()).model_dump(mode="json")})
    yield _sse("emerging_update", {"items": [c.model_dump(mode="json") for c in cards]})
    last_sig = _emerging_sig(cards)
    last_event = last_emerging = loop.time()
    while True:
        if timeout is not None and loop.time() - started >= timeout:
            break
        try:
            if await request.is_disconnected():
                break
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(poll)
        try:
            async with db.SessionLocal() as s:
                page = await feed.get_feed(s, since=cursor, limit=100)
                for m in page.items:
                    yield _sse("new_message", m.model_dump(mode="json"), id_=m.id)
                    last_event = loop.time()
                cursor = max(cursor, page.cursor)
                if page.items or loop.time() - last_emerging >= 30:
                    cards = await emerging.compute_emerging(s)
                    sig = _emerging_sig(cards)
                    if sig != last_sig or loop.time() - last_emerging >= 30:
                        yield _sse("emerging_update", {"items": [c.model_dump(mode="json") for c in cards]})
                        last_sig, last_event = sig, loop.time()
                    last_emerging = loop.time()
                rows = (await s.scalars(
                    select(Investigation).order_by(Investigation.updated_at.desc()).limit(200)
                )).all()
                for inv in rows:
                    if story_seen.get(inv.id) != inv.updated_at:
                        story_seen[inv.id] = inv.updated_at
                        yield _sse("story_update", _story_event(inv))
                        last_event = loop.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  поток не должен падать
            log.warning("SSE tick failed: %r", e)
        if loop.time() - last_event >= keepalive:
            yield ": keepalive\n\n"
            last_event = loop.time()


@router.get("/stream")
async def stream(request: Request, since: int | None = None,
                 timeout: float | None = Query(None, ge=0, description="закрыть поток через N секунд"),
                 user: User = Depends(current_user)):
    """SSE: hello, new_message, emerging_update, story_update, keepalive-комментарии."""
    return StreamingResponse(
        _stream(request, since, timeout), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/emerging", response_model=list[EmergingStoryOut])
async def list_emerging(window_min: int | None = Query(None, ge=1), min_messages: int | None = Query(None, ge=2),
                        include_dismissed: bool = False, user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    return await emerging.compute_emerging(session, window_min=window_min, min_messages=min_messages,
                                           include_dismissed=include_dismissed)


@router.post("/emerging/{key}/investigate", response_model=InvestigationOut, status_code=201)
async def investigate_emerging(key: str, response: Response, data: EmergingInvestigateIn | None = None,
                               user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """«Исследовать»: сюжет из кластера + запуск пайплайна. Уже есть сюжет — 200 и он же."""
    data = data or EmergingInvestigateIn()
    with _err():
        inv, created = await emerging.investigate_emerging(session, key, user, channel_id=data.channel_id,
                                                           title=data.title, note=data.note)
    if not created:
        response.status_code = 200
    return inv


@router.post("/emerging/{key}/dismiss", status_code=204)
async def dismiss_emerging(key: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await emerging.dismiss_emerging(session, key)
    return Response(status_code=204)


# ================================================================ сюжеты
stories_router = APIRouter()


@stories_router.get("", response_model=list[InvestigationOut])
async def list_stories(status: InvestigationStatus | None = None, mine: bool = False, q: str | None = None,
                       user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await stories.list_stories(session, status=status, owner_id=user.id if mine else None, q=q)


@stories_router.post("", response_model=InvestigationOut, status_code=201)
async def create_story(data: InvestigationCreate, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    """«Своя зацепка»."""
    with _err():
        return await stories.create_story(session, user, data.lead_text, title=data.title,
                                          channel_id=data.channel_id, autorun=data.autorun)


@stories_router.get("/{inv_id}", response_model=InvestigationDetail)
async def get_story(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    inv = await _story(session, inv_id)
    return await stories.story_detail(session, inv)


@stories_router.post("/{inv_id}/run", response_model=InvestigationOut)
async def run_story(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    inv = await _story(session, inv_id)
    with _err():
        return await stories.run_story(session, inv, user)


@stories_router.post("/{inv_id}/deeper", response_model=InvestigationOut)
async def dig_deeper(inv_id: int, data: DeeperIn | None = None, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    """«Копать глубже»: ещё итерация CHAIN SEARCH с вопросом редактора."""
    inv = await _story(session, inv_id)
    with _err():
        return await stories.dig_deeper(session, inv, user, (data or DeeperIn()).question)


@stories_router.post("/{inv_id}/stop", response_model=InvestigationOut)
async def stop_story(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    inv = await _story(session, inv_id)
    with _err():
        return await stories.stop_story(session, inv, user)


@stories_router.get("/{inv_id}/handoff/check", response_model=HandoffCheckOut)
async def handoff_check(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    inv = await _story(session, inv_id)
    return await stories.handoff_check(session, inv)


@stories_router.post("/{inv_id}/handoff", response_model=HandoffResultOut)
async def hand_off(inv_id: int, data: HandoffIn, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)):
    """«Передать дальше». Не блокируется неподтверждёнными фактами — они в warnings."""
    inv = await _story(session, inv_id)
    with _err():
        return await stories.hand_off(session, inv, user, to=data.to, channel=data.channel,
                                      channel_id=data.channel_id, comment=data.comment)


@stories_router.post("/{inv_id}/reject", response_model=InvestigationOut)
async def reject_story(inv_id: int, data: CommentIn | None = None, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    """«Не подтвердилось»."""
    inv = await _story(session, inv_id)
    with _err():
        return await stories.reject(session, inv, user, (data or CommentIn()).comment)


@stories_router.post("/{inv_id}/reopen", response_model=InvestigationOut)
async def reopen_story(inv_id: int, data: CommentIn | None = None, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    inv = await _story(session, inv_id)
    with _err():
        return await stories.reopen(session, inv, user, (data or CommentIn()).comment)


@stories_router.post("/{inv_id}/transition", response_model=InvestigationOut)
async def transition_story(inv_id: int, data: TransitionIn, user: User = Depends(current_user),
                           session: AsyncSession = Depends(get_session)):
    inv = await _story(session, inv_id)
    with _err():
        return await stories.transition(session, inv, data.to_status, user, data.comment)


@stories_router.get("/{inv_id}/progress", response_model=ProgressOut)
async def get_progress(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    inv = await _story(session, inv_id)
    iters = (await session.scalars(
        select(SearchIteration).where(SearchIteration.investigation_id == inv_id).order_by(SearchIteration.n)
    )).all()
    return ProgressOut(investigation_id=inv.id, status=inv.status, progress=inv.progress_json or {},
                       iterations=[SearchIterationOut.model_validate(i) for i in iters])


@stories_router.get("/{inv_id}/iterations", response_model=list[SearchIterationOut])
async def list_iterations(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await _story(session, inv_id)
    return (await session.scalars(
        select(SearchIteration).where(SearchIteration.investigation_id == inv_id).order_by(SearchIteration.n)
    )).all()


@stories_router.get("/{inv_id}/sources", response_model=list[SourceOut])
async def list_story_sources(inv_id: int, kind: SourceKind | None = None, primary_only: bool = False,
                             user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await _story(session, inv_id)
    return await stories.list_sources(session, inv_id, kind=kind, primary_only=primary_only)


@stories_router.get("/{inv_id}/contacts", response_model=list[ContactOut])
async def list_contacts(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """«Кому позвонить» (с телефонами) — видит редактор."""
    await _story(session, inv_id)
    return await stories.list_contacts(session, inv_id)


@stories_router.get("/{inv_id}/media", response_model=list[MediaItemOut])
async def list_media(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await _story(session, inv_id)
    return (await session.scalars(
        select(MediaItem).where(MediaItem.investigation_id == inv_id).order_by(MediaItem.possibly_old, MediaItem.id)
    )).all()


@stories_router.get("/{inv_id}/report", response_model=ReportOut)
async def get_report(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await _story(session, inv_id)
    rep = await session.get(Report, inv_id)
    if rep is None:
        raise HTTPException(404, "Справка ещё не готова")
    return rep


@stories_router.get("/{inv_id}/drafts", response_model=list[DraftOut])
async def list_drafts(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await _story(session, inv_id)
    return await stories.list_drafts(session, inv_id)


@stories_router.post("/{inv_id}/drafts", response_model=DraftOut, status_code=201)
async def create_draft(inv_id: int, data: DraftCreate, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    """Правка редактора — новая версия (author_kind=editor)."""
    inv = await _story(session, inv_id)
    with _err():
        return await stories.save_draft(session, inv, user, title=data.title, body=data.body,
                                        claims_json=data.claims_json)


@stories_router.get("/{inv_id}/drafts/diff", response_model=DraftDiffOut)
async def diff_drafts(inv_id: int, base: int | None = None, target: int | None = None,
                      user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Пословный diff; base по умолчанию — последняя AI-версия, target — последняя версия."""
    await _story(session, inv_id)
    with _err():
        return await stories.draft_diff(session, inv_id, base, target)


@stories_router.get("/{inv_id}/facts", response_model=list[FactMarkOut])
async def list_facts(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await _story(session, inv_id)
    return await stories.list_facts(session, inv_id)


@stories_router.post("/{inv_id}/facts", response_model=FactMarkOut, status_code=201)
async def mark_fact(inv_id: int, data: FactMarkIn, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    await _story(session, inv_id)
    return await stories.mark_fact(session, inv_id, user, fact_text=data.fact_text, state=data.state,
                                   claim_id=data.claim_id, comment=data.comment)


@stories_router.get("/{inv_id}/history", response_model=list[StatusHistoryOut])
async def list_history(inv_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await _story(session, inv_id)
    return await stories.list_history(session, inv_id)


router.include_router(stories_router, prefix="/stories")
router.include_router(stories_router, prefix="/investigations", include_in_schema=False)  # алиас v1


@router.get("/handoff-channels")
async def handoff_channels(user: User = Depends(current_user)) -> list[dict[str, str]]:
    return [{"key": k, "label": v} for k, v in HANDOFF_CHANNELS.items()]


# ================================================================ каналы (Channel DNA)
@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(Channel).order_by(Channel.id))).all()


@router.post("/channels/{channel_id}/dna", response_model=ChannelOut)
async def rebuild_channel_dna(channel_id: int, user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)):
    from app.ai.channel_dna import ensure_channel_dna

    ch = await session.get(Channel, channel_id)
    if ch is None:
        raise HTTPException(404, "Канал не найден")
    await ensure_channel_dna(session, ch, force=True)
    await session.commit()
    await session.refresh(ch)
    return ch


# ================================================================ источники (MAX + Telegram + RSS)
@router.get("/sources/chats", response_model=list[SourceChatOut])
@router.get("/max/chats", response_model=list[SourceChatOut], include_in_schema=False)
async def list_source_chats(platform: str | None = None, user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)):
    return await sources.list_source_chats(session, platform)


@router.post("/sources/resolve", response_model=SourcePreviewOut)
async def resolve_source(data: SourceResolveIn, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    """Превью до добавления: платформа по ссылке/@username + реальные название, тип, аватар, подписчики.
    Ничего не сохраняет; кэш 10 мин. Ошибки получения данных — в поле ``error`` (200)."""
    with _err():
        return SourcePreviewOut(**await sources.resolve(session, data.ref, data.platform))


@router.post("/sources", response_model=SourceChatOut, status_code=201)
async def add_source(data: SourceAddIn, response: Response, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    """Подключить источник: @username / t.me (Telegram), id чата / max.ru (MAX), сайт или RSS (rss);
    ``platform="auto"`` — определить по ссылке. Название — реальное (resolve)."""
    with _err():
        chat, created = await sources.add_source(session, user, data.platform, data.ref,
                                                 title=data.title, district=data.district)
    if not created:
        response.status_code = 200
    with _err():
        return await sources.get_source_out(session, chat.id)


@router.patch("/sources/chats/{chat_id}", response_model=SourceChatOut)
@router.patch("/max/chats/{chat_id}", response_model=SourceChatOut, include_in_schema=False)
async def update_source_chat(chat_id: int, data: SourceChatUpdate, user: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)):
    with _err():
        return await sources.update_source(session, chat_id, enabled=data.enabled, district=data.district,
                                           title=data.title)


@router.delete("/sources/chats/{chat_id}", response_model=SourceDeleteOut)
@router.delete("/sources/{chat_id}", response_model=SourceDeleteOut, include_in_schema=False)
@router.delete("/max/chats/{chat_id}", response_model=SourceDeleteOut, include_in_schema=False)
async def delete_source_chat(chat_id: int, user: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)):
    """Удалить источник: скрыт из списка, его сообщения archived (уходят из ленты/emerging/поиска),
    цитаты и ссылки в уже созданных сюжетах остаются."""
    with _err():
        return SourceDeleteOut(**await sources.delete_source(session, chat_id))


@router.post("/sources/sync", response_model=SyncResult)
@router.post("/max/sync", response_model=SyncResult, include_in_schema=False)
async def sync_sources(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """«Синхронизировать сейчас» (MAX + Telegram + RSS)."""
    from app.integrations.max.poller import sync_once

    res = await sync_once(session)
    try:
        rss = await sources.sync_rss(session)
    except Exception as e:  # noqa: BLE001
        log.warning("RSS sync failed: %r", e)
        res.errors.append(f"rss: {e}")
        return res
    res.chats += rss.chats
    res.new_messages += rss.new_messages
    res.errors += rss.errors
    for k, v in rss.by_platform.items():
        res.by_platform[k] = res.by_platform.get(k, 0) + v
    return res


# ---------------------------------------------------------------- режим данных (TZ_v3 п.1)
def _provider_name(obj: Any) -> str | None:
    if obj is None:
        return None
    return str(getattr(obj, "name", None) or type(obj).__name__)


@router.get("/mode", response_model=ModeOut)
async def get_mode() -> ModeOut:
    """Какие данные сейчас в работе: live/demo, LLM, провайдеры Telegram/MAX/поиска. Без входа."""
    llm = None
    try:
        from app.ai.llm import get_llm

        llm = _provider_name(get_llm())
    except Exception as e:  # noqa: BLE001
        log.debug("mode: llm %r", e)
    if llm and llm not in ("anthropic", "extractive", "mock"):
        llm = "anthropic" if "anthropic" in llm.lower() or "claude" in llm.lower() else (
            "extractive" if "extract" in llm.lower() else llm)
    tg_provider = None
    try:
        from app.integrations.max.poller import telegram_module

        tg = telegram_module()
        if tg is not None:
            tg_provider = _provider_name(tg.get_telegram_client())
    except Exception as e:  # noqa: BLE001
        log.debug("mode: telegram %r", e)
    max_provider = None
    try:
        from app.integrations.max.client import get_max_client

        max_provider = _provider_name(get_max_client())
    except Exception as e:  # noqa: BLE001
        log.debug("mode: max %r", e)
    search: list[str] = []
    try:
        from app.integrations.search import get_search_provider

        p = get_search_provider()
        inner = getattr(p, "providers", None)
        search = [n for n in (_provider_name(x) for x in inner) if n] if inner else [_provider_name(p) or "?"]
    except Exception as e:  # noqa: BLE001
        log.debug("mode: search %r", e)
    extra: dict[str, Any] = {}
    try:  # DEV-REALDATA: app.config.data_mode_status() — подпись режима, заметки, rss_ingest
        from app.config import data_mode_status

        st = data_mode_status()
        extra = {"label": st.get("label"), "rss_ingest": st.get("rss_ingest"), "notes": st.get("notes") or []}
        if isinstance(st.get("search"), str) and not st["search"].startswith("error"):
            parts = [p for p in st["search"].split("+") if p]
            search = parts if len(parts) > 1 or search in ([], ["?"]) else search
    except Exception as e:  # noqa: BLE001
        log.debug("mode: data_mode_status %r", e)
    return ModeOut(data_mode=sources.data_mode(), llm=llm or "mock", telegram_provider=tg_provider,
                   max_provider=max_provider, search_providers=search, **extra)


# ---------------------------------------------------------------- Telegram (docs/TELEGRAM.md §4.6)
@router.get("/telegram/status")
async def telegram_status(user: User = Depends(current_user)) -> dict[str, Any]:
    """Провайдер, подключённые источники, последняя ошибка (напр. «нужен login»)."""
    from app.integrations.max.poller import telegram_module

    tg = telegram_module()
    if tg is None:
        return {"platform": "telegram", "provider": None, "available": False, "sources": [], "pending": [],
                "last_error": "Пакет Telegram недоступен или TG_ENABLED=false"}
    try:
        st = await tg.get_telegram_client().status()
    except Exception as e:  # noqa: BLE001
        return {"platform": "telegram", "provider": None, "available": False, "sources": [], "pending": [],
                "last_error": str(e)}
    return {"available": True, **st}


@router.post("/telegram/sources", response_model=SourceChatOut, status_code=201)
async def telegram_add_source(data: dict, response: Response, user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)):
    """Алиас ``POST /api/sources {platform: "telegram", ref}``; body ``{"ref": "@name" | "https://t.me/…"}``."""
    ref = str((data or {}).get("ref") or "").strip()
    if not ref:
        raise HTTPException(422, "ref: ссылка или @username")
    with _err():
        chat, created = await sources.add_source(session, user, "telegram", ref, title=data.get("title"),
                                                 district=data.get("district"))
    if not created:
        response.status_code = 200
    with _err():
        return await sources.get_source_out(session, chat.id)


@router.delete("/telegram/sources/{external_id}", response_model=SourceDeleteOut)
async def telegram_remove_source(external_id: str, user: User = Depends(current_user),
                                 session: AsyncSession = Depends(get_session)):
    from app.models import MaxChat

    ext = external_id if external_id.startswith("tg:") else f"tg:{external_id.lstrip('@').lower()}"
    chat = await session.scalar(select(MaxChat).where(MaxChat.platform == "telegram", MaxChat.external_id == ext,
                                                      MaxChat.deleted_at.is_(None)))
    if chat is None:
        raise HTTPException(404, "Источник не найден")
    with _err():
        return SourceDeleteOut(**await sources.delete_source(session, chat.id))


# ================================================================ демо-поток
@router.get("/demo/stream", response_model=DemoStreamOut)
async def demo_stream_status(user: User = Depends(current_user)):
    from app.integrations.max import demo_stream

    return DemoStreamOut(**demo_stream.status())


@router.post("/demo/restart", response_model=DemoStreamOut)
async def demo_stream_restart(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """«Перезапустить демо-поток»."""
    from app.integrations.max import demo_stream

    return DemoStreamOut(**await demo_stream.restart(session))


# ================================================================ статистика
@router.get("/stats", response_model=StatsOut)
@router.get("/metrics", response_model=StatsOut, include_in_schema=False)
async def get_stats(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return StatsOut(**await stats.compute_stats(session))
