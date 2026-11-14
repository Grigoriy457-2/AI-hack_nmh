"""Пайплайн исследования (п.5 ТЗ). Зона DEV-AI.

Запуск: api/pages переводят статус new -> researching через workflow.transition, затем вызывают
``start_investigation(id)`` (фоновая asyncio-задача; при settings.PIPELINE_INLINE — ``await
run_investigation(id)``). Пайплайн открывает СВОИ сессии через ``db.session_scope()``.

Этапы (ключи STAGES -> progress_json.stages[].key):
1. input        — LLM извлекает сущности -> investigations.entities_json.
2. search       — CHAIN SEARCH: до CHAIN_MAX_ITERATIONS итераций; в каждой asyncio.gather агентов
                  web / max_chats / official / media; запись search_iterations + sources;
                  LLM предлагает гипотезы следующей итерации (для демо — минимум 2 итерации).
3. intelligence — services.uniqueness: кластеры, uniqueness_score, is_primary, is_important;
                  для уникальных/важных авторов MAX — poller.ensure_author_profile (телефоны);
                  хронология, подтверждённое/неподтверждённое, противоречия, «что проверить».
4. media        — MEDIA FINDER -> media_items.
5. dna          — ai.channel_dna.ensure_channel_dna для канала расследования.
6. report       — reports.report_json (9 разделов) + drafts v1 (author_kind=ai, claims_json).
Финал: workflow.transition(researching -> review, user=None), review_at=now (первый раз).
Ошибка: progress.error, stage.status="error", статус остаётся researching (оператор видит «зависшую»).
Каждая запись прогресса — отдельный commit, чтобы GET /progress видел живой лог.

«Копать глубже» / перезапуск (review -> researching): вопросы редактора из
``investigations.deeper_questions`` дают +1 итерацию CHAIN SEARCH и доп. запросы агентам.
Повторный запуск удаляет прошлые итерации/источники/медиа/справку;
черновики сохраняются, новый AI-черновик получает следующий номер версии.

Демо-паузы: в mock-режиме LLM и без PIPELINE_INLINE между шагами есть ``asyncio.sleep`` (всего
~12–15 с), чтобы на странице был виден живой прогресс. ``DEMO_DELAY_SCALE = 0`` — отключить.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.ai import mock_data as md
from app.ai import prompts
from app.ai.llm import MockLLM, get_llm
from app.ai.text import first_sentence, hhmm_msk, stems
from app.config import settings
from app.integrations.search import OFFICIAL_DOMAINS, SearchResult, get_search_provider, is_official_url
from app.models import (
    AuthorProfile,
    Channel,
    ChatMessage,
    Draft,
    DraftAuthorKind,
    Investigation,
    InvestigationStatus,
    MaxChat,
    MediaItem,
    MediaKind,
    Report,
    SearchIteration,
    Source,
    SourceKind,
    StatusHistory,
    utcnow,
)
from app.services import uniqueness as uq

log = logging.getLogger("newsflow.pipeline")

STAGES: list[tuple[str, str]] = [
    ("input", "Разбор зацепки"),
    ("search", "Цепочка поиска"),
    ("intelligence", "Анализ и уникальность"),
    ("media", "Поиск медиа"),
    ("dna", "Channel DNA"),
    ("report", "Справка и черновик"),
]
STAGE_TITLES = dict(STAGES)
STAGE_PERCENT: dict[str, tuple[int, int]] = {
    "input": (0, 8), "search": (8, 55), "intelligence": (55, 72),
    "media": (72, 82), "dna": (82, 88), "report": (88, 100),
}

AGENTS: tuple[str, ...] = ("web", "max_chats", "official", "media")
AGENT_LABELS = {"web": "СМИ и Telegram", "max_chats": "Домовые чаты MAX", "official": "Официальные источники", "media": "Фото и видео"}
KIND_LABELS = {"web": "СМИ/ТГ", "max_chat": "Чат MAX", "official": "Официально", "media": "Медиа"}

REPORT_SECTIONS: list[tuple[str, str]] = [
    ("summary", "Краткое описание ситуации"),
    ("timeline", "Хронология"),
    ("facts", "Найденная информация"),
    ("sources", "Источники"),
    ("confirmed", "Подтверждённые сведения"),
    ("unconfirmed", "Неподтверждённая информация"),
    ("contradictions", "Противоречия"),
    ("media", "Фото и видео"),
    ("to_verify", "Что ещё необходимо проверить"),
]

DEMO_DELAY_SCALE: float = 1.4
MAX_MEDIA_ITEMS = 4
MAX_QUERIES_PER_AGENT = 2
RELEVANCE_THRESHOLD = 0.5

_running: set[asyncio.Task] = set()  # держим ссылки на фоновые задачи


@dataclass
class PipelineContext:
    investigation_id: int
    entities: dict[str, Any] = field(default_factory=dict)
    iterations: list[dict[str, Any]] = field(default_factory=list)  # [{n, queries, rationale}]
    source_ids: list[int] = field(default_factory=list)
    channel_dna: dict[str, Any] | None = None
    intelligence: dict[str, Any] = field(default_factory=dict)  # timeline/confirmed/...
    # --- служебное
    inv: Investigation | None = None
    lead_text: str = ""
    scenario: bool = False
    channel_name: str | None = None
    sources: dict[int, Source] = field(default_factory=dict)
    src_meta: dict[int, dict[str, Any]] = field(default_factory=dict)
    seen_urls: set[str] = field(default_factory=set)
    seen_messages: set[int] = field(default_factory=set)
    media_candidates: dict[int, list[dict[str, Any]]] = field(default_factory=dict)  # source_id -> media
    chat_pool: list[dict[str, Any]] | None = None
    uniq: dict[int, uq.UniquenessResult] = field(default_factory=dict)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    pending: list[tuple[Source, dict[str, Any]]] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)  # «Копать глубже»: вопросы редактора
    prev_iterations: int = 0  # сколько итераций было в прошлом прогоне (для «Копать глубже»: +1)


# ---------------------------------------------------------------- утилиты
def _delays_enabled() -> bool:
    return DEMO_DELAY_SCALE > 0 and not settings.PIPELINE_INLINE and isinstance(get_llm(), MockLLM)


async def _pause(seconds: float) -> None:
    if _delays_enabled():
        await asyncio.sleep(seconds * DEMO_DELAY_SCALE)


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() if dt else None


async def _llm_json(task: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Вызов LLM по задаче; любая ошибка реального LLM -> ответ MockLLM (демо не падает)."""
    llm = get_llm()
    system, user, schema = prompts.build(task, json.dumps(payload, ensure_ascii=False, default=str))
    try:
        res = await llm.complete_json(system, user, schema, task=task)
        if isinstance(res, dict):
            return res
        raise ValueError("LLM returned non-dict")
    except Exception as e:  # noqa: BLE001
        if isinstance(llm, MockLLM):
            raise
        log.warning("LLM task %s failed: %r — using MockLLM", task, e)
        return await MockLLM().complete_json(system, user, schema, task=task)


# ---------------------------------------------------------------- запуск
def start_investigation(investigation_id: int) -> asyncio.Task:
    """Создать фоновую задачу run_investigation и сохранить ссылку в _running."""
    task = asyncio.get_running_loop().create_task(
        run_investigation(investigation_id), name=f"newsflow-pipeline-{investigation_id}"
    )
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


async def run_investigation(investigation_id: int) -> None:
    """Полный прогон этапов STAGES. Никогда не бросает исключение наружу."""
    current = "input"
    try:
        async with db.session_scope() as session:
            inv = await session.get(Investigation, investigation_id)
            if inv is None:
                log.warning("pipeline: investigation %s not found", investigation_id)
                return
            ctx = PipelineContext(investigation_id=investigation_id, inv=inv, lead_text=inv.lead_text or "")
            ctx.questions = [str(q.get("question") or "").strip() for q in (inv.deeper_questions or [])
                             if isinstance(q, dict) and str(q.get("question") or "").strip()]
            ctx.prev_iterations = int(await session.scalar(
                select(func.count()).select_from(SearchIteration).where(SearchIteration.investigation_id == investigation_id)
            ) or 0)
            await progress_init(session, inv)
            await _reset_previous_run(session, investigation_id)
            funcs = {
                "input": stage_input, "search": stage_chain_search, "intelligence": stage_intelligence,
                "media": stage_media, "dna": stage_channel_dna, "report": stage_report_and_draft,
            }
            for key, title in STAGES:
                current = key
                await progress_update(session, inv, stage=key, stage_status="running",
                                      message=f"{title}…", percent=STAGE_PERCENT[key][0])
                await funcs[key](session, ctx)
                await progress_update(session, inv, stage=key, stage_status="done",
                                      note=ctx.notes.get(key), percent=STAGE_PERCENT[key][1])
            await _finish(session, ctx)
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline failed for investigation %s at stage %s", investigation_id, current)
        try:
            async with db.session_scope() as session:
                inv = await session.get(Investigation, investigation_id)
                if inv is not None:
                    await progress_update(
                        session, inv, stage=current, stage_status="error", level="error",
                        message=f"Ошибка на этапе «{STAGE_TITLES.get(current, current)}»: {e}",
                        error=f"{type(e).__name__}: {e}",
                    )
        except Exception:  # noqa: BLE001
            log.exception("pipeline: failed to record error for %s", investigation_id)


async def _reset_previous_run(session: AsyncSession, inv_id: int) -> None:
    for model in (MediaItem, Source, SearchIteration, Report):
        await session.execute(delete(model).where(model.investigation_id == inv_id))
    await session.commit()


async def _finish(session: AsyncSession, ctx: PipelineContext) -> None:
    inv = ctx.inv
    assert inv is not None
    await session.refresh(inv)
    if inv.status == InvestigationStatus.researching:
        try:
            from app.services import workflow

            await workflow.transition(session, inv, InvestigationStatus.review, None,
                                      "NEWSFLOW: справка и черновик готовы")
        except NotImplementedError:
            prev = inv.status
            inv.status = InvestigationStatus.review
            inv.updated_at = utcnow()
            session.add(StatusHistory(investigation_id=inv.id, from_status=prev,
                                      to_status=InvestigationStatus.review, user_id=None,
                                      comment="NEWSFLOW: справка и черновик готовы"))
        if inv.review_at is None:
            inv.review_at = utcnow()
    else:
        log.warning("pipeline: investigation %s is %s, not researching — status unchanged", inv.id, inv.status)
    await progress_update(session, inv, message="Готово: справка и черновик сформированы", percent=100, done=True)


# ---------------------------------------------------------------- прогресс
async def progress_init(session: AsyncSession, inv: Investigation) -> None:
    """progress_json = {stages: все pending, log: [], percent: 0, ...} + commit."""
    now = _iso(utcnow())
    inv.progress_json = {
        "state": "running", "percent": 0, "current_stage": None,
        "started_at": now, "finished_at": None, "error": None,
        "stages": [{"key": k, "title": t, "status": "pending", "started_at": None, "finished_at": None, "note": None}
                   for k, t in STAGES],
        "log": [{"ts": now, "stage": None, "level": "info", "message": "NEWSFLOW запущен"}],
    }
    await session.commit()


async def progress_update(
    session: AsyncSession,
    inv: Investigation,
    *,
    stage: str | None = None,
    stage_status: str | None = None,
    message: str | None = None,
    level: str = "info",
    percent: int | None = None,
    error: str | None = None,
    note: str | None = None,
    done: bool = False,
) -> None:
    """Обновить progress_json (новый dict, а не мутация — иначе SQLAlchemy не увидит) + commit."""
    now = _iso(utcnow())
    old = inv.progress_json or {}
    pj: dict[str, Any] = {
        "state": old.get("state", "running"), "percent": old.get("percent", 0),
        "current_stage": old.get("current_stage"), "started_at": old.get("started_at") or now,
        "finished_at": old.get("finished_at"), "error": old.get("error"),
        "stages": [dict(s) for s in old.get("stages") or
                   [{"key": k, "title": t, "status": "pending", "started_at": None, "finished_at": None, "note": None}
                    for k, t in STAGES]],
        "log": list(old.get("log") or []),
    }
    if stage:
        pj["current_stage"] = stage
        for s in pj["stages"]:
            if s["key"] == stage:
                if stage_status:
                    s["status"] = stage_status
                    if stage_status == "running" and not s.get("started_at"):
                        s["started_at"] = now
                    if stage_status in ("done", "error"):
                        s["finished_at"] = now
                if note is not None:
                    s["note"] = note
    if percent is not None:
        pj["percent"] = max(0, min(100, int(percent)))
    if message:
        pj["log"].append({"ts": now, "stage": stage or pj.get("current_stage"), "level": level, "message": message})
        pj["log"] = pj["log"][-200:]
    if error:
        pj["state"] = "error"
        pj["error"] = error
        pj["finished_at"] = now
    if done:
        pj["state"] = "done"
        pj["percent"] = 100
        pj["current_stage"] = None
        pj["finished_at"] = now
    inv.progress_json = pj
    await session.commit()


async def _log(session: AsyncSession, ctx: PipelineContext, stage: str, message: str, *,
               level: str = "info", percent: int | None = None) -> None:
    assert ctx.inv is not None
    await progress_update(session, ctx.inv, stage=stage, message=message, level=level, percent=percent)


# ---------------------------------------------------------------- 1. INPUT
async def stage_input(session: AsyncSession, ctx: PipelineContext) -> None:
    inv = ctx.inv
    assert inv is not None
    ctx.scenario = md.is_scenario(ctx.lead_text)
    if ctx.scenario:
        await _detect_scenario_anchor(session)
    await _pause(0.6)
    ent = await _llm_json("extract_entities", {"lead_text": ctx.lead_text, "now": _iso(utcnow())})
    ent = _normalize_entities(ent, ctx.lead_text)
    ctx.entities = ent
    inv.entities_json = dict(ent)
    loc = (ent.get("location") or {}).get("text") or "—"
    tm = (ent.get("time") or {}).get("text") or "—"
    kws = ", ".join(ent.get("keywords") or [])
    await _log(session, ctx, "input",
               f"Событие: {ent.get('event_type') or '—'}; место: {loc}; время: {tm}; ключевые слова: {kws}")
    filled = sum(1 for k in ("event_type", "object", "location", "time", "keywords")
                 if ent.get(k) and (not isinstance(ent[k], dict) or any(ent[k].values())))
    ctx.notes["input"] = f"извлечено сущностей: {filled}, статус — не подтверждено"
    await _pause(0.4)


def _normalize_entities(ent: dict[str, Any], lead: str) -> dict[str, Any]:
    loc = ent.get("location") if isinstance(ent.get("location"), dict) else {"text": ent.get("location")}
    tm = ent.get("time") if isinstance(ent.get("time"), dict) else {"text": ent.get("time")}
    kws = ent.get("keywords") if isinstance(ent.get("keywords"), list) else []
    return {
        "summary": str(ent.get("summary") or first_sentence(lead, 200)),
        "event_type": ent.get("event_type"),
        "object": ent.get("object"),
        "location": {"text": loc.get("text"), "district": loc.get("district"), "address": loc.get("address")},
        "time": {"text": tm.get("text"), "iso": tm.get("iso")},
        "keywords": [str(k) for k in kws if k][:8],
        "status": "unconfirmed",
    }


async def _detect_scenario_anchor(session: AsyncSession) -> None:
    """Якорь сценария = время сообщения m01 в БД − его смещение (чтобы веб/чаты совпали по дате)."""
    ref = md.SCENARIO_MAX_MESSAGES[0]
    # по тексту (после «перезапуска демо-потока» external_id получают суффикс эпохи), без архивных
    sent = await session.scalar(
        select(ChatMessage.sent_at).where(ChatMessage.text == ref["text"], ChatMessage.archived.is_(False))
        .order_by(ChatMessage.sent_at.desc()).limit(1)
    )
    if sent is not None:
        from datetime import timedelta

        md.set_scenario_anchor(sent - timedelta(minutes=ref["minutes"]))
    else:
        md.set_scenario_anchor(None)


# ---------------------------------------------------------------- 2. CHAIN SEARCH
def _normalize_queries(raw: Any, ctx: PipelineContext, n: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    per_agent: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        agent = str(q.get("agent") or "").strip().lower()
        agent = "max_chats" if agent in ("max", "max_chat", "chats", "chat") else agent
        query = " ".join(str(q.get("query") or "").split())[:300]
        if agent not in AGENTS or not query or (agent, query.lower()) in seen:
            continue
        if per_agent.get(agent, 0) >= MAX_QUERIES_PER_AGENT:
            continue
        per_agent[agent] = per_agent.get(agent, 0) + 1
        seen.add((agent, query.lower()))
        out.append({"agent": agent, "query": query})
    if n == 1:  # на старте каждый агент получает хотя бы один запрос
        base = " ".join((ctx.entities.get("keywords") or [])[:4]) or ctx.lead_text[:80]
        for agent in AGENTS:
            if agent not in per_agent:
                out.append({"agent": agent, "query": base + (" фото видео" if agent == "media" else "")})
    if ctx.questions and n == 1:  # «Копать глубже»: запросы по вопросам редактора
        loc = ((ctx.entities.get("location") or {}).get("text") or "").split(",")[0]
        for qtext in ctx.questions[-3:]:
            for agent in ("web", "max_chats", "official"):
                query = " ".join(f"{loc} {qtext}".split())[:300]
                if (agent, query.lower()) not in seen:
                    seen.add((agent, query.lower()))
                    out.append({"agent": agent, "query": query})
    return out


async def stage_chain_search(session: AsyncSession, ctx: PipelineContext) -> None:
    await _ensure_chat_pool(session, ctx)
    max_it = max(1, int(settings.CHAIN_MAX_ITERATIONS))
    if ctx.questions:  # «Копать глубже» — на одну итерацию больше, чем в прошлом прогоне
        max_it = max(max_it, ctx.prev_iterations) + 1
    if ctx.questions:
        await _log(session, ctx, "search", "Копать глубже, вопрос редактора: " + ctx.questions[-1])
    lo, hi = STAGE_PERCENT["search"]
    findings: list[str] = []
    n = 1
    while n <= max_it:
        hyp = await _llm_json("chain_hypotheses", {
            "lead_text": ctx.lead_text, "entities": ctx.entities, "n": n, "max_iterations": max_it,
            "previous": ctx.iterations, "findings": findings[:30],
            **({"editor_questions": ctx.questions} if ctx.questions else {}),
        })
        if n > 1 and not hyp.get("continue", True) and not ctx.questions:
            await _log(session, ctx, "search", f"Цепочка остановлена после итерации {n - 1}: "
                       f"{hyp.get('rationale') or 'новых гипотез нет'}")
            break
        queries = _normalize_queries(hyp.get("queries"), ctx, n)
        if not queries and ctx.questions:  # «Копать глубже»: гипотез нет — ищем по вопросу редактора
            loc = ((ctx.entities.get("location") or {}).get("text") or "").split(",")[0]
            queries = [{"agent": a, "query": " ".join(f"{loc} {ctx.questions[-1]}".split())[:300]}
                       for a in ("web", "max_chats")]
        if not queries:
            break
        rationale = str(hyp.get("rationale") or "").strip() or None
        await _log(session, ctx, "search", f"Итерация {n}: {rationale or 'проверяем гипотезы'}",
                   percent=lo + (hi - lo) * (n - 1) // max_it)
        await _pause(0.8)

        results = await asyncio.gather(*(_run_agent(session, q, ctx) for q in queries), return_exceptions=True)
        queries_json: list[dict[str, Any]] = []
        new_total = raw_total = 0
        new_texts: list[str] = []
        for q, res in zip(queries, results):
            if isinstance(res, BaseException):
                log.warning("agent %s failed: %r", q["agent"], res)
                await _log(session, ctx, "search", f"{AGENT_LABELS[q['agent']]}: ошибка агента ({res})", level="warn")
                res = []
            added = _save_results(session, ctx, q, res, n)
            raw_total += len(res)
            new_total += len(added)
            new_texts.extend(added)
            queries_json.append({"agent": q["agent"], "query": q["query"], "results": len(res), "new": len(added)})
        await _register_pending(session, ctx)
        session.add(SearchIteration(investigation_id=ctx.investigation_id, n=n, queries_json=queries_json,
                                    rationale=rationale, results_count=new_total))
        ctx.iterations.append({"n": n, "queries": queries_json, "rationale": rationale})
        by_agent: dict[str, int] = {}
        for qj in queries_json:
            by_agent[qj["agent"]] = by_agent.get(qj["agent"], 0) + qj["new"]
        detail = ", ".join(f"{AGENT_LABELS[a]} — {c}" for a, c in by_agent.items())
        await _pause(0.9)
        await _log(session, ctx, "search",
                   f"Итерация {n}: {len(queries)} запросов, {raw_total} результатов, новых источников {new_total} ({detail})",
                   percent=lo + (hi - lo) * n // max_it)
        findings = new_texts
        n += 1
    ctx.source_ids = list(ctx.sources)
    ctx.notes["search"] = f"итераций: {len(ctx.iterations)}, источников: {len(ctx.sources)}"


async def _run_agent(session: AsyncSession, q: dict[str, str], ctx: PipelineContext) -> list[Any]:
    agent, query = q["agent"], q["query"]
    if agent == "web":
        return await agent_web(query, ctx)
    if agent == "max_chats":
        return await agent_max_chats(session, query, ctx)
    if agent == "official":
        return await agent_official(query, ctx)
    return await agent_media(query, ctx)


def _save_results(session: AsyncSession, ctx: PipelineContext, q: dict[str, str], res: list[Any], n: int) -> list[str]:
    """Создать Source для новых результатов (дедуп по url / chat_message_id). Возвращает их тексты."""
    added: list[str] = []
    agent = q["agent"]
    for r in res:
        if agent == "max_chats":
            mid = r["chat_message_id"]
            if mid in ctx.seen_messages:
                continue
            ctx.seen_messages.add(mid)
            url = r.get("message_url") or md.max_message_url(r["chat_external_id"], r["external_id"])
            ctx.seen_urls.add(url)
            src = Source(
                investigation_id=ctx.investigation_id, kind=SourceKind.max_chat,
                title=f"{r['chat_title']} · {r.get('author_name') or 'участник чата'}"[:512],
                url=url, snippet=r["text"], published_at=r["sent_at"], chat_message_id=mid,
                iteration_n=n, query=q["query"][:512],
            )
            meta = {"kind": "max_chat", "text": r["text"], "has_media": r["has_media"], "media_url": r["media_url"],
                    "is_forwarded": r["is_forwarded"], "author_external_id": r["author_external_id"],
                    "author_name": r.get("author_name"), "chat_title": r["chat_title"], "sent_at": r["sent_at"]}
        else:
            if not isinstance(r, SearchResult) or not r.url or r.url in ctx.seen_urls:
                continue
            ctx.seen_urls.add(r.url)
            kind = {"web": SourceKind.web, "official": SourceKind.official, "media": SourceKind.media}[agent]
            src = Source(
                investigation_id=ctx.investigation_id, kind=kind, title=(r.title or r.url)[:512],
                url=r.url[:1024], snippet=r.snippet, published_at=r.published_at,
                iteration_n=n, query=q["query"][:512],
            )
            meta = {"kind": kind.value, "text": r.snippet or r.title, "has_media": bool(r.media_urls),
                    "media_url": r.media_urls[0] if r.media_urls else None, "is_forwarded": False,
                    "sent_at": r.published_at, "title": r.title, "source_type": r.source_type,
                    "media": list(r.extra.get("media") or [
                        {"kind": "photo", "url": u, "thumbnail_url": u, "captured_at": None, "note": None}
                        for u in r.media_urls])}
        session.add(src)
        ctx.pending.append((src, meta))
        added.append(meta["text"] or "")
    return added


async def _register_pending(session: AsyncSession, ctx: PipelineContext) -> None:
    if not ctx.pending:
        return
    await session.flush()
    for src, meta in ctx.pending:
        ctx.sources[src.id] = src
        ctx.src_meta[src.id] = meta
        if meta.get("media"):
            ctx.media_candidates[src.id] = meta["media"]
    ctx.pending = []


# ---------------------------------------------------------------- агенты поиска
async def agent_web(query: str, ctx: PipelineContext) -> list[SearchResult]:
    res = await get_search_provider().search(query, limit=8)
    return [r for r in res if r.source_type not in ("official", "media") and not is_official_url(r.url)]


async def _ensure_chat_pool(session: AsyncSession, ctx: PipelineContext) -> None:
    if ctx.chat_pool is not None:
        return
    rows = (await session.execute(
        select(ChatMessage, MaxChat).join(MaxChat, ChatMessage.chat_id == MaxChat.id)
        .where(MaxChat.enabled.is_(True), ChatMessage.archived.is_(False))
        .order_by(ChatMessage.sent_at.desc()).limit(1000)
    )).all()
    if not rows:
        total = await session.scalar(select(func.count()).select_from(ChatMessage))
        if not total:
            try:  # в БД ещё нет сообщений MAX — одна синхронизация (mock или Bot API)
                from app.integrations.max.poller import sync_once

                res = await sync_once(session)
                await _log(session, ctx, "search", f"Синхронизация MAX: новых сообщений {res.new_messages}")
                rows = (await session.execute(
                    select(ChatMessage, MaxChat).join(MaxChat, ChatMessage.chat_id == MaxChat.id)
                    .where(MaxChat.enabled.is_(True), ChatMessage.archived.is_(False))
                    .order_by(ChatMessage.sent_at.desc()).limit(1000)
                )).all()
            except Exception as e:  # noqa: BLE001
                log.warning("MAX sync before search failed: %r", e)
    ctx.chat_pool = [{
        "chat_message_id": m.id, "external_id": m.external_id, "chat_external_id": c.external_id,
        "chat_title": c.title, "author_external_id": m.author_external_id, "author_name": m.author_name,
        "platform": c.platform or "max", "message_url": m.message_url,
        "text": m.text, "sent_at": m.sent_at, "has_media": bool(m.has_media), "media_url": m.media_url,
        "is_forwarded": bool(m.is_forwarded), "_stems": stems(m.text),
    } for m, c in rows]


async def agent_max_chats(session: AsyncSession, query: str, ctx: PipelineContext) -> list[dict[str, Any]]:
    """Поиск по chat_messages (только enabled-чаты): ключевые слова + LLM-релевантность.
    Возвращает [{chat_message_id, text, sent_at, score, ...}]."""
    if ctx.chat_pool is None:
        await _ensure_chat_pool(session, ctx)
    q = stems(query)
    cands = [m for m in ctx.chat_pool or [] if m["_stems"] & q]
    if not cands:
        return []
    cands.sort(key=lambda m: (-len(m["_stems"] & q), m["sent_at"]))
    cands = cands[:60]
    res = await _llm_json("relevance", {
        "lead_text": ctx.lead_text, "query": query, "keywords": ctx.entities.get("keywords") or [],
        "messages": [{"id": m["chat_message_id"], "text": m["text"]} for m in cands],
    })
    scores = res.get("scores") if isinstance(res.get("scores"), dict) else {}
    out = []
    for m in cands:
        try:
            score = float(scores.get(str(m["chat_message_id"]), scores.get(m["chat_message_id"], 0)) or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score >= RELEVANCE_THRESHOLD:
            out.append({**{k: v for k, v in m.items() if k != "_stems"}, "score": round(score, 2)})
    out.sort(key=lambda m: m["sent_at"])
    return out


async def agent_official(query: str, ctx: PipelineContext) -> list[SearchResult]:
    return await get_search_provider().search(query, limit=5, domains=list(OFFICIAL_DOMAINS))


async def agent_media(query: str, ctx: PipelineContext) -> list[SearchResult]:
    res = await get_search_provider().search(query, limit=5, source_type="media")
    return [r for r in res if r.media_urls]


# ---------------------------------------------------------------- 3. INTELLIGENCE
def _src_payload(ctx: PipelineContext, sid: int) -> dict[str, Any]:
    s, meta, u = ctx.sources[sid], ctx.src_meta.get(sid, {}), ctx.uniq.get(sid)
    return {
        "id": sid, "kind": s.kind.value if hasattr(s.kind, "value") else s.kind, "title": s.title,
        "text": (s.snippet or s.title or "")[:600], "url": s.url,
        "published_at": _iso(s.published_at), "time_msk": hhmm_msk(s.published_at),
        "is_primary": bool(u.is_primary) if u else True, "cluster_id": u.cluster_id if u else None,
        "uniqueness": u.uniqueness_score if u else None, "is_important": bool(u.is_important) if u else False,
        "is_forwarded": bool(meta.get("is_forwarded")), "has_media": bool(meta.get("has_media")),
    }


async def stage_intelligence(session: AsyncSession, ctx: PipelineContext) -> None:
    await _register_pending(session, ctx)
    ids = list(ctx.sources)
    if ids:
        imp = await _llm_json("importance", {"items": [
            {"id": sid, "text": ctx.src_meta[sid].get("text") or "", "has_media": bool(ctx.src_meta[sid].get("has_media"))}
            for sid in ids]})
        imp_map_raw = imp.get("important") if isinstance(imp.get("important"), dict) else {}
        importance = {sid: bool(imp_map_raw[str(sid)]) for sid in ids if str(sid) in imp_map_raw}
        items = [uq.UniqItem(id=sid, text=ctx.src_meta[sid].get("text") or "", sent_at=ctx.sources[sid].published_at,
                             is_forwarded=bool(ctx.src_meta[sid].get("is_forwarded")),
                             meta={"has_media": bool(ctx.src_meta[sid].get("has_media"))}) for sid in ids]
        clusters = uq.cluster_messages(items)
        ctx.uniq = uq.score_uniqueness(items, clusters, importance or None)
        for sid, r in ctx.uniq.items():
            s = ctx.sources[sid]
            s.cluster_id, s.uniqueness_score = r.cluster_id, r.uniqueness_score
            s.is_primary, s.is_important = r.is_primary, r.is_important
        await session.flush()
        dups = sum(1 for r in ctx.uniq.values() if not r.is_primary)
        multi = sum(1 for c in clusters if len(c.items) > 1)
        await _log(session, ctx, "intelligence",
                   f"Дедупликация: {len(ids)} источников → {len(clusters)} кластеров; дублей и пересылок свёрнуто: {dups} "
                   f"(кластеров с дублями: {multi})")
    else:
        await _log(session, ctx, "intelligence", "Источников не найдено — справка будет предварительной", level="warn")
    await _pause(0.6)

    # «Кому позвонить»: первоисточники из MAX с uniqueness >= 0.5 и is_important
    selected = [sid for sid in uq.select_unique_important(ctx.uniq) if ctx.sources[sid].kind == SourceKind.max_chat]
    selected.sort(key=lambda sid: ctx.sources[sid].published_at or datetime.max)
    for sid in selected:
        meta = ctx.src_meta[sid]
        prof = await _author_profile(session, meta.get("author_external_id"))
        s = ctx.sources[sid]
        ctx.contacts.append({
            "source_id": sid, "author_external_id": meta.get("author_external_id"),
            "name": (prof.name if prof and prof.name else meta.get("author_name")),
            "has_phone": bool(prof and prof.phone), "username": prof.username if prof else None,
            "quote": s.snippet, "chat_title": meta.get("chat_title"), "sent_at": _iso(s.published_at),
            "time_msk": hhmm_msk(s.published_at), "message_url": s.url,
            "uniqueness_score": ctx.uniq[sid].uniqueness_score,
        })
    if selected:
        with_phone = sum(1 for c in ctx.contacts if c["has_phone"])
        await _log(session, ctx, "intelligence",
                   f"Уникальные и важные сообщения очевидцев: {len(selected)}; профили MAX запрошены, телефон есть у {with_phone}")

    intel = await _llm_json("intelligence", {
        "lead_text": ctx.lead_text, "entities": ctx.entities,
        "sources": [_src_payload(ctx, sid) for sid in sorted(ids, key=lambda i: ctx.sources[i].published_at or datetime.max)],
    })
    ctx.intelligence = _normalize_intel(intel, set(ids))
    if not ctx.intelligence.get("summary"):
        mock = await MockLLM().complete_json("", json.dumps({"lead_text": ctx.lead_text, "entities": ctx.entities,
                                                              "sources": [_src_payload(ctx, i) for i in ids]},
                                                             ensure_ascii=False, default=str), task="intelligence")
        ctx.intelligence = _normalize_intel(mock, set(ids))
    it = ctx.intelligence
    await _log(session, ctx, "intelligence",
               f"Хронология: {len(it['timeline'])} событий; подтверждено: {len(it['confirmed'])}; "
               f"не подтверждено: {len(it['unconfirmed'])}; противоречий: {len(it['contradictions'])}")
    for c in it["contradictions"][:2]:
        await _log(session, ctx, "intelligence", f"Противоречие: {c['text']}", level="warn")
    ctx.notes["intelligence"] = (f"первоисточников: {sum(1 for r in ctx.uniq.values() if r.is_primary)}, "
                                 f"контактов: {len(ctx.contacts)}, противоречий: {len(it['contradictions'])}")
    await _pause(0.6)


async def _author_profile(session: AsyncSession, author_id: str | None) -> AuthorProfile | None:
    if not author_id:
        return None
    try:
        from app.integrations.max.poller import ensure_author_profile

        return await ensure_author_profile(session, author_id)
    except NotImplementedError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_author_profile(%s) failed: %r", author_id, e)
        return await session.get(AuthorProfile, author_id)
    # fallback: напрямую через клиента MAX
    try:
        from app.integrations.max.client import get_max_client

        prof = await get_max_client().get_user_profile(author_id)
    except Exception as e:  # noqa: BLE001
        log.warning("get_user_profile(%s) failed: %r", author_id, e)
        prof = None
    cached = await session.get(AuthorProfile, author_id)
    if prof is None:
        return cached
    if cached is None:
        cached = AuthorProfile(external_id=author_id)
        session.add(cached)
    cached.name, cached.phone, cached.username, cached.fetched_at = prof.name, prof.phone, prof.username, utcnow()
    await session.flush()
    return cached


_STATES = {"confirmed", "unconfirmed", "false"}


def _norm_items(raw: Any, valid: set[int], *, default_state: str | None, keep_empty_src: bool) -> list[dict[str, Any]]:
    out = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict) or not str(it.get("text") or "").strip():
            continue
        ids: list[int] = []
        for x in it.get("source_ids") or []:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if xi in valid and xi not in ids:
                ids.append(xi)
        if not ids and it.get("source_ids") and not keep_empty_src:
            continue
        state = it.get("state") if it.get("state") in _STATES else default_state
        item = {"text": str(it["text"]).strip(), "source_ids": ids, "state": state,
                "time": it.get("time") or None}
        if it.get("claim"):
            item["claim"] = str(it["claim"]).strip().rstrip(".")
        out.append(item)
    return out


def _normalize_intel(raw: dict[str, Any], valid: set[int]) -> dict[str, Any]:
    return {
        "title": str(raw.get("title") or "").strip(),
        "headline": str(raw.get("headline") or raw.get("title") or "").strip(),
        "topic": raw.get("topic"),
        "summary": str(raw.get("summary") or "").strip(),
        "timeline": _norm_items(raw.get("timeline"), valid, default_state="unconfirmed", keep_empty_src=False),
        "facts": _norm_items(raw.get("facts"), valid, default_state=None, keep_empty_src=True),
        "confirmed": _norm_items(raw.get("confirmed"), valid, default_state="confirmed", keep_empty_src=False),
        "unconfirmed": _norm_items(raw.get("unconfirmed"), valid, default_state="unconfirmed", keep_empty_src=False),
        "contradictions": _norm_items(raw.get("contradictions"), valid, default_state=None, keep_empty_src=False),
        "to_verify": _norm_items(raw.get("to_verify"), valid, default_state=None, keep_empty_src=True),
    }


# ---------------------------------------------------------------- 4. MEDIA FINDER
def _media_kind(url: str, declared: str | None = None) -> MediaKind:
    if declared in ("photo", "video"):
        return MediaKind(declared)
    return MediaKind.video if url.lower().split("?")[0].endswith((".mp4", ".mov", ".webm", ".avi")) else MediaKind.photo


async def stage_media(session: AsyncSession, ctx: PipelineContext) -> None:
    chat_cands: list[dict[str, Any]] = []
    web_cands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in sorted(ctx.sources, key=lambda i: ctx.sources[i].published_at or datetime.max):
        s, meta, u = ctx.sources[sid], ctx.src_meta[sid], ctx.uniq.get(sid)
        if s.kind == SourceKind.max_chat:
            url = meta.get("media_url")
            if not meta.get("has_media") or not url or url in seen or (u and not u.is_primary):
                continue
            seen.add(url)
            kind = _media_kind(url)
            chat_cands.append({
                "key": f"m{len(chat_cands) + 1}", "source_id": sid, "url": url, "kind": kind.value,
                "thumbnail_url": md.svg_thumb("Видео из чата MAX" if kind == MediaKind.video else "Фото из чата MAX",
                                              bg="#3b4a5a", kind=kind.value),
                "origin": f"Чат MAX «{meta.get('chat_title')}», {meta.get('author_name') or 'участник'}",
                "captured_at": s.published_at, "published_at": s.published_at, "context": s.snippet, "note": None,
            })
        else:
            for m in ctx.media_candidates.get(sid, []):
                url = m.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                web_cands.append({
                    "key": f"w{len(web_cands) + 1}", "source_id": sid, "url": url,
                    "kind": _media_kind(url, m.get("kind")).value, "thumbnail_url": m.get("thumbnail_url"),
                    "origin": (s.title or s.url or "")[:255], "captured_at": m.get("captured_at"),
                    "published_at": s.published_at, "context": s.snippet, "note": m.get("note"),
                })
    cands = chat_cands + web_cands
    if not cands:
        await _log(session, ctx, "media", "Фото и видео не найдены")
        ctx.notes["media"] = "медиа не найдено"
        return
    ev_iso = (ctx.entities.get("time") or {}).get("iso")
    res = await _llm_json("media_relation", {
        "lead_text": ctx.lead_text, "event_time": ev_iso,
        "media": [{k: (_iso(v) if isinstance(v, datetime) else v) for k, v in c.items()
                   if k in ("key", "url", "kind", "origin", "captured_at", "published_at", "context", "note")} for c in cands],
    })
    rel = {str(i.get("key")): i for i in (res.get("items") or []) if isinstance(i, dict)}
    for c in cands:
        r = rel.get(c["key"], {})
        c["relation"] = str(r.get("relation") or first_sentence(c.get("context") or "", 140))
        c["possibly_old"] = bool(r.get("possibly_old"))
        low = c["relation"].lower()
        if c["possibly_old"] and r.get("reason") and not any(w in low for w in ("стар", "архив", "обратн")):
            c["relation"] += f" ({r['reason']})"

    # выбор: до MAX_MEDIA_ITEMS, чередуя очевидцев из чатов и публикации; «возможно старое» — обязательно
    old = [c for c in cands if c["possibly_old"]][:1]
    fresh_chat = [c for c in chat_cands if not c["possibly_old"]]
    fresh_web = [c for c in web_cands if not c["possibly_old"]]
    picked: list[dict[str, Any]] = []
    while len(picked) < MAX_MEDIA_ITEMS - len(old) and (fresh_chat or fresh_web):
        for pool in (fresh_chat, fresh_web):
            if pool and len(picked) < MAX_MEDIA_ITEMS - len(old):
                picked.append(pool.pop(0))
    picked.sort(key=lambda c: c.get("captured_at") or datetime.max)
    picked += old
    for c in picked:
        item = MediaItem(
            investigation_id=ctx.investigation_id, source_id=c["source_id"], kind=MediaKind(c["kind"]),
            url=c["url"][:1024], thumbnail_url=(c.get("thumbnail_url") or None),
            origin=(c.get("origin") or "")[:255] or None, captured_at=c.get("captured_at"),
            relation=c["relation"], rights_status="not_verified", possibly_old=c["possibly_old"],
        )
        session.add(item)
        ctx.media.append(item)
    await session.flush()
    n_old = sum(1 for m in ctx.media if m.possibly_old)
    await _pause(0.5)
    await _log(session, ctx, "media",
               f"Медиа: {len(ctx.media)} (фото {sum(1 for m in ctx.media if m.kind == MediaKind.photo)}, "
               f"видео {sum(1 for m in ctx.media if m.kind == MediaKind.video)}); права не проверены"
               + (f"; ⚠ возможно старое фото: {n_old}" if n_old else ""), level="warn" if n_old else "info")
    ctx.notes["media"] = f"найдено {len(ctx.media)}" + (f", возможно старых: {n_old}" if n_old else "")


# ---------------------------------------------------------------- 5. CHANNEL DNA
async def stage_channel_dna(session: AsyncSession, ctx: PipelineContext) -> None:
    from app.ai.channel_dna import ensure_channel_dna

    inv = ctx.inv
    assert inv is not None
    channel = await session.get(Channel, inv.channel_id) if inv.channel_id else None
    await _pause(0.4)
    if channel is None:
        ctx.channel_dna = None
        await _log(session, ctx, "dna", "Канал не выбран — черновик в нейтральном новостном стиле")
        ctx.notes["dna"] = "канал не выбран"
        return
    fresh = not channel.dna_json
    ctx.channel_dna = await ensure_channel_dna(session, channel)
    ctx.channel_name = channel.name
    dna = ctx.channel_dna or {}
    await _log(session, ctx, "dna",
               f"Канал «{channel.name}»{' (профиль построен)' if fresh else ''}: тон — {dna.get('tone')}, "
               f"заголовок — {(dna.get('headline') or {}).get('style')}; правила: " + "; ".join(dna.get("rules") or []))
    ctx.notes["dna"] = f"«{channel.name}»: {dna.get('tone')}"
    await _pause(0.4)


# ---------------------------------------------------------------- 6. REPORT & DRAFT
def _sid_order_key(ctx: PipelineContext, sid: int) -> tuple:
    s, u = ctx.sources[sid], ctx.uniq.get(sid)
    prio = {SourceKind.official: 0, SourceKind.max_chat: 1, SourceKind.web: 2, SourceKind.media: 3}.get(s.kind, 4)
    important = 0 if (u and u.is_important) else 1
    return (prio, important, s.published_at or datetime.max)


def build_report(ctx: PipelineContext) -> dict[str, Any]:
    it = ctx.intelligence
    sections: dict[str, dict[str, Any]] = {k: {"key": k, "title": t, "text": "", "items": []} for k, t in REPORT_SECTIONS}

    def items(key: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for i, r in enumerate(rows, start=1):
            item = {"id": f"{key}-{i}", "text": r["text"], "source_ids": list(r.get("source_ids") or []),
                    "state": r.get("state"), "time": r.get("time")}
            if r.get("claim"):
                item["claim"] = r["claim"]
            for extra in ("kind", "url", "published_at", "uniqueness", "duplicates", "is_primary", "media_id",
                          "possibly_old", "note"):
                if extra in r:
                    item[extra] = r[extra]
            out.append(item)
        return out

    sections["summary"]["text"] = it.get("summary") or ""
    sections["timeline"]["items"] = items("timeline", it.get("timeline", []))
    sections["facts"]["items"] = items("facts", it.get("facts", []))
    sections["confirmed"]["items"] = items("confirmed", it.get("confirmed", []))
    sections["unconfirmed"]["items"] = items("unconfirmed", it.get("unconfirmed", []))
    sections["contradictions"]["items"] = items("contradictions", it.get("contradictions", []))

    # 4. Источники — первоисточники (дубли свёрнуты)
    cluster_size: dict[int, int] = {}
    for r in ctx.uniq.values():
        cluster_size[r.cluster_id] = cluster_size.get(r.cluster_id, 0) + 1
    primaries = [sid for sid in ctx.sources if (ctx.uniq.get(sid) is None or ctx.uniq[sid].is_primary)]
    primaries.sort(key=lambda sid: _sid_order_key(ctx, sid))
    key_sids: list[int] = []
    chats_taken = 0
    for sid in primaries:  # ключевые: официальные, СМИ/ТГ, медиа + 2 самых важных чата; полный список — вкладка «Источники»
        if ctx.sources[sid].kind == SourceKind.max_chat:
            if chats_taken >= 2 or not (ctx.uniq.get(sid) and ctx.uniq[sid].is_important):
                continue
            chats_taken += 1
        key_sids.append(sid)
    src_rows = []
    for sid in key_sids[:12]:
        s, u = ctx.sources[sid], ctx.uniq.get(sid)
        dup = cluster_size.get(u.cluster_id, 1) - 1 if u else 0
        label = KIND_LABELS.get(s.kind.value, "Источник")
        src_rows.append({
            "text": f"{label}: {s.title}" + (f" (+{dup} дубл.)" if dup else ""),
            "source_ids": [sid], "state": "confirmed" if s.kind == SourceKind.official else None,
            "time": hhmm_msk(s.published_at), "kind": s.kind.value, "url": s.url,
            "published_at": _iso(s.published_at), "uniqueness": u.uniqueness_score if u else None,
            "duplicates": dup, "is_primary": True, "note": first_sentence(s.snippet or "", 160),
        })
    sections["sources"]["items"] = items("sources", src_rows)
    n_total, n_prim = len(ctx.sources), len(primaries)
    by_kind: dict[str, int] = {}
    for s in ctx.sources.values():
        by_kind[s.kind.value] = by_kind.get(s.kind.value, 0) + 1
    sections["sources"]["text"] = (
        f"Ключевые источники. Всего найдено: {n_total}, первоисточников: {n_prim}, дублей и пересылок свёрнуто: "
        f"{n_total - n_prim}. "
        + ", ".join(f"{KIND_LABELS.get(k, k)} — {v}" for k, v in by_kind.items())
        + ". Полный список — во вкладке «Источники»."
    )

    # 8. Фото и видео
    media_rows = []
    for m in ctx.media:
        what = "Видео" if m.kind == MediaKind.video else "Фото"
        tm = hhmm_msk(m.captured_at) if not m.possibly_old else (m.captured_at.strftime("%d.%m.%Y") if m.captured_at else None)
        text = f"{what} — {m.origin or 'источник не указан'}" + (f", {tm}" if tm else "") + f": {m.relation}. Права: не проверены"
        if m.possibly_old:
            text += ". ⚠ Возможно старое фото — не использовать без проверки"
        media_rows.append({"text": text, "source_ids": [m.source_id] if m.source_id else [],
                           "state": "false" if m.possibly_old else None,
                           "time": None if m.possibly_old else hhmm_msk(m.captured_at),
                           "media_id": m.id, "possibly_old": m.possibly_old})
    sections["media"]["items"] = items("media", media_rows)
    if not media_rows:
        sections["media"]["text"] = "Фото и видео не найдены."

    # 9. Что проверить (+ кому позвонить)
    verify_rows = list(it.get("to_verify", []))
    contacts = _report_contacts(ctx)
    for c in contacts[:4]:
        phone_note = "телефон в профиле MAX" if c["has_phone"] else "телефон скрыт — написать в MAX"
        verify_rows.append({"text": f"Позвонить очевидцу: {c['name'] or 'автор сообщения'} ({c['chat_title']}, {phone_note}) — "
                                    f"«{first_sentence(c['quote'] or '', 90)}»",
                            "source_ids": [c["source_id"]], "state": None})
    sections["to_verify"]["items"] = items("to_verify", verify_rows)

    return {
        "version": 1,
        "generated_at": _iso(utcnow()),
        "title": it.get("title") or ctx.entities.get("summary") or ctx.lead_text[:120],
        "sections": [sections[k] for k, _ in REPORT_SECTIONS],
        "contacts": contacts,
        "stats": {
            "iterations": len(ctx.iterations), "sources_total": n_total, "primary": n_prim,
            "duplicates": n_total - n_prim, "by_kind": by_kind, "media": len(ctx.media),
            "possibly_old_media": sum(1 for m in ctx.media if m.possibly_old), "contacts": len(contacts),
        },
        "channel": ctx.channel_name,
    }


def _report_contacts(ctx: PipelineContext) -> list[dict[str, Any]]:
    """Один контакт на автора (первое уникальное сообщение), сначала — с телефоном. Без самого номера:
    телефон отдаёт только /api/.../contacts (редактор)."""
    by_author: dict[str, dict[str, Any]] = {}
    for c in ctx.contacts:
        key = c["author_external_id"] or f"src{c['source_id']}"
        if key in by_author:
            by_author[key]["messages"] += 1
            continue
        by_author[key] = {**c, "messages": 1}
    return sorted(by_author.values(), key=lambda c: (not c["has_phone"], c["sent_at"] or ""))


def _draft_hashtags(ctx: PipelineContext) -> list[str]:
    tags = list((ctx.channel_dna or {}).get("top_hashtags") or [])[:1] or ["#москва"]
    loc = (ctx.entities.get("location") or {}).get("district") or (ctx.entities.get("location") or {}).get("text") or ""
    word = loc.split(",")[0].split()[0].lower() if loc.strip() else ""
    word = "".join(ch for ch in word if ch.isalnum())
    if ctx.scenario:
        word = "чертаново"
    if word and f"#{word}" not in tags:
        tags.append(f"#{word}")
    return tags


async def stage_report_and_draft(session: AsyncSession, ctx: PipelineContext) -> None:
    from app.ai.drafting import compose_draft, validate_claims

    report_json = build_report(ctx)
    existing = await session.get(Report, ctx.investigation_id)
    if existing is None:
        session.add(Report(investigation_id=ctx.investigation_id, report_json=report_json))
    else:
        existing.report_json = report_json
        existing.created_at = utcnow()
    await session.flush()
    await _log(session, ctx, "report",
               f"Справка: 9 разделов, источников {report_json['stats']['sources_total']}, "
               f"контактов «Кому позвонить» — {len(report_json['contacts'])}", percent=94)
    await _pause(0.7)

    it = ctx.intelligence
    facts = [{"id": f"f{i}", "text": f["text"], "claim": f.get("claim") or f["text"],
              "source_ids": f["source_ids"], "status": "confirmed"}
             for i, f in enumerate(it.get("confirmed", []), start=1) if f["source_ids"]]
    facts += [{"id": f"u{i}", "text": f["text"], "claim": f.get("claim") or f["text"],
               "source_ids": f["source_ids"], "status": "unconfirmed"}
              for i, f in enumerate(it.get("unconfirmed", []), start=1)
              if f.get("state") != "false" and f.get("claim") and f["source_ids"]]
    headline = it.get("headline") or it.get("title") or ctx.entities.get("summary") or ctx.lead_text[:100]
    payload = {"lead_text": ctx.lead_text, "channel_name": ctx.channel_name, "dna": ctx.channel_dna,
               "headline": headline, "topic": it.get("topic"), "summary": it.get("summary"),
               "facts": facts, "hashtags": _draft_hashtags(ctx)}
    res = await _llm_json("draft", payload)
    title, body = str(res.get("title") or "").strip(), str(res.get("body") or "").strip()
    claims = validate_claims(body, res.get("claims") or [], set(ctx.sources)) if body else []
    if not title or not body or (facts and not claims):
        log.warning("draft from LLM invalid — composing locally")
        local = compose_draft(headline, [{"text": f["claim"], "source_ids": f["source_ids"], "status": f["status"]}
                                         for f in facts], ctx.channel_dna, topic=it.get("topic"),
                              hashtags=payload["hashtags"])
        title, body = local["title"], local["body"]
        claims = validate_claims(body, local["claims"], set(ctx.sources))
    version = (await session.scalar(select(func.max(Draft.version)).where(Draft.investigation_id == ctx.investigation_id)) or 0) + 1
    session.add(Draft(investigation_id=ctx.investigation_id, version=version, author_kind=DraftAuthorKind.ai,
                      author_user_id=None, title=title[:512], body=body, claims_json=claims))
    await session.flush()
    unconf = sum(1 for c in claims if c["status"] != "confirmed")
    await _log(session, ctx, "report",
               f"Черновик v{version}{' для «' + ctx.channel_name + '»' if ctx.channel_name else ''}: "
               f"{len(claims)} утверждений со ссылками на источники (неподтверждённых: {unconf})", percent=99)
    ctx.notes["report"] = f"справка + черновик v{version}"
