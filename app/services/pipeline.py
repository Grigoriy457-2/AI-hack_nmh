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
Финал: workflow.transition(researching -> report_ready, user=None), report_ready_at=now.
Ошибка: progress.error, stage.status="error", статус остаётся researching (оператор видит «зависшую»).
Каждая запись прогресса — отдельный commit, чтобы GET /progress видел живой лог.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.search import SearchResult
from app.models import Investigation

STAGES: list[tuple[str, str]] = [
    ("input", "Разбор зацепки"),
    ("search", "Цепочка поиска"),
    ("intelligence", "Анализ и уникальность"),
    ("media", "Поиск медиа"),
    ("dna", "Channel DNA"),
    ("report", "Справка и черновик"),
]

AGENTS: tuple[str, ...] = ("web", "max_chats", "official", "media")

_running: set[asyncio.Task] = set()  # держим ссылки на фоновые задачи


@dataclass
class PipelineContext:
    investigation_id: int
    entities: dict[str, Any] = field(default_factory=dict)
    iterations: list[dict[str, Any]] = field(default_factory=list)  # [{n, queries, rationale}]
    source_ids: list[int] = field(default_factory=list)
    channel_dna: dict[str, Any] | None = None
    intelligence: dict[str, Any] = field(default_factory=dict)  # timeline/confirmed/...


# ---------------------------------------------------------------- запуск
def start_investigation(investigation_id: int) -> asyncio.Task:
    """Создать фоновую задачу run_investigation и сохранить ссылку в _running."""
    raise NotImplementedError("DEV-AI: start_investigation")


async def run_investigation(investigation_id: int) -> None:
    """Полный прогон этапов STAGES. Никогда не бросает исключение наружу."""
    raise NotImplementedError("DEV-AI: run_investigation")


# ---------------------------------------------------------------- прогресс
async def progress_init(session: AsyncSession, inv: Investigation) -> None:
    """progress_json = {stages: все pending, log: [], percent: 0, ...} + commit."""
    raise NotImplementedError("DEV-AI: progress_init")


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
) -> None:
    """Обновить progress_json (новый dict, а не мутация — иначе SQLAlchemy не увидит) + commit."""
    raise NotImplementedError("DEV-AI: progress_update")


# ---------------------------------------------------------------- этапы
async def stage_input(session: AsyncSession, ctx: PipelineContext) -> None:
    raise NotImplementedError("DEV-AI: stage_input")


async def stage_chain_search(session: AsyncSession, ctx: PipelineContext) -> None:
    raise NotImplementedError("DEV-AI: stage_chain_search")


async def stage_intelligence(session: AsyncSession, ctx: PipelineContext) -> None:
    raise NotImplementedError("DEV-AI: stage_intelligence")


async def stage_media(session: AsyncSession, ctx: PipelineContext) -> None:
    raise NotImplementedError("DEV-AI: stage_media")


async def stage_channel_dna(session: AsyncSession, ctx: PipelineContext) -> None:
    raise NotImplementedError("DEV-AI: stage_channel_dna")


async def stage_report_and_draft(session: AsyncSession, ctx: PipelineContext) -> None:
    raise NotImplementedError("DEV-AI: stage_report_and_draft")


# ---------------------------------------------------------------- агенты поиска
async def agent_web(query: str, ctx: PipelineContext) -> list[SearchResult]:
    raise NotImplementedError("DEV-AI: agent_web")


async def agent_max_chats(session: AsyncSession, query: str, ctx: PipelineContext) -> list[dict[str, Any]]:
    """Поиск по chat_messages (только enabled-чаты): ключевые слова + LLM-релевантность.
    Возвращает [{chat_message_id, text, sent_at, score}]."""
    raise NotImplementedError("DEV-AI: agent_max_chats")


async def agent_official(query: str, ctx: PipelineContext) -> list[SearchResult]:
    raise NotImplementedError("DEV-AI: agent_official")


async def agent_media(query: str, ctx: PipelineContext) -> list[SearchResult]:
    raise NotImplementedError("DEV-AI: agent_media")
