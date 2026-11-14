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
        if not drop and await conn.run_sync(_is_outdated_schema):
            import logging

            logging.getLogger("newsflow").warning(
                "БД со схемой v1 (роли/старые статусы) — пересоздаю таблицы под v2 (данные демо будут засеяны заново)"
            )
            drop = True
        if drop:
            await conn.run_sync(_drop_everything)
        await conn.run_sync(Base.metadata.create_all)


def _is_outdated_schema(sync_conn) -> bool:
    """True, если в БД схема v1 (нет колонки investigations.owner_id / есть users.role)."""
    from sqlalchemy import inspect

    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())
    if "investigations" in tables:
        cols = {c["name"] for c in insp.get_columns("investigations")}
        if "owner_id" not in cols or "handoff_to" not in cols:
            return True
    if "users" in tables and "role" in {c["name"] for c in insp.get_columns("users")}:
        return True
    if "max_chats" in tables or "feedback" in tables:
        return True
    if "chat_messages" in tables:
        cols = {c["name"] for c in insp.get_columns("chat_messages")}
        if not {"platform", "message_url", "views", "archived"} <= cols:
            return True
    if "source_chats" in tables:
        if not {"username", "kind", "url", "ref"} <= {c["name"] for c in insp.get_columns("source_chats")}:
            return True
    return False


def _drop_everything(sync_conn) -> None:
    """Удалить и текущие, и устаревшие таблицы (v1: max_chats, feedback)."""
    from sqlalchemy import MetaData

    from sqlalchemy import inspect, text

    try:
        meta = MetaData()
        meta.reflect(bind=sync_conn)  # сначала отражаем всё, что есть (включая таблицы v1)
        meta.drop_all(sync_conn)
        Base.metadata.drop_all(sync_conn, checkfirst=True)
        return
    except Exception as e:  # noqa: BLE001  битые FK (ссылки на отсутствующие таблицы) и т.п.
        import logging

        logging.getLogger("newsflow").warning("drop_all via reflect failed (%r) — DROP TABLE по списку", e)
    dialect = sync_conn.dialect.name
    if dialect == "mysql":
        sync_conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
    elif dialect == "sqlite":
        sync_conn.execute(text("PRAGMA foreign_keys=OFF"))
    q = sync_conn.dialect.identifier_preparer.quote
    for name in inspect(sync_conn).get_table_names():
        sync_conn.execute(text(f"DROP TABLE IF EXISTS {q(name)}"))
    if dialect == "mysql":
        sync_conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


async def dispose() -> None:
    await engine.dispose()
