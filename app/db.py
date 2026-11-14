"""Async-движок SQLAlchemy, фабрика сессий, FastAPI-зависимость и init_db.

ВАЖНО: обращайтесь к фабрике через модуль (``from app import db; db.SessionLocal()``)
или ``db.session_scope()`` — тесты могут пересоздать движок через ``db.configure(url)``,
и импортированная напрямую ссылка ``from app.db import SessionLocal`` устареет.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.config import settings


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей."""


engine: AsyncEngine
SessionLocal: async_sessionmaker[AsyncSession]


def _make_engine(url: str) -> AsyncEngine:
    kwargs: dict = {"echo": settings.DB_ECHO, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url.rstrip("/").endswith("sqlite+aiosqlite:"):
            kwargs["poolclass"] = StaticPool  # одна БД на все соединения
    else:
        kwargs.update(pool_pre_ping=True, pool_recycle=3600)
    return create_async_engine(url, **kwargs)


def configure(url: str | None = None) -> AsyncEngine:
    """(Пере)создать движок и фабрику сессий. Используется при импорте и в тестах."""
    global engine, SessionLocal
    engine = _make_engine(url or settings.DATABASE_URL)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine


configure()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: ``session: AsyncSession = Depends(get_session)``."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия для фоновых задач (пайплайн, poller): ``async with db.session_scope() as s:``.

    Коммит — явно внутри блока; при исключении — rollback.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db(drop: bool = False) -> None:
    """Создать все таблицы (``Base.metadata.create_all``). ``drop=True`` — сначала удалить."""
    import app.models  # noqa: F401  регистрирует модели в Base.metadata

    async with engine.begin() as conn:
        if drop:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    await engine.dispose()
