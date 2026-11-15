"""Точка входа FastAPI: ``uvicorn app.main:app``.

lifespan: init_db (create_all) -> seed_if_empty (если SEED_ON_STARTUP) -> MAX poller
(если MAX_POLLER_ENABLED и модуль реализован). Ошибки seed/poller не роняют приложение.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.config import settings

log = logging.getLogger("newsflow")
logging.basicConfig(level=logging.DEBUG if settings.DEBUG else logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def upgrade_schema() -> None:
    """Добавить недостающие колонки в существующие таблицы (v3: source_chats.avatar_url и т.п.)."""
    try:
        from app.models import add_missing_columns

        async with db.engine.begin() as conn:
            added = await conn.run_sync(add_missing_columns)
        if added:
            log.info("Schema upgrade: added columns %s", ", ".join(added))
    except Exception:  # noqa: BLE001
        log.exception("Schema upgrade failed")


def _start_rss() -> asyncio.Task | None:
    """Фоновая выгрузка RSS-источников (platform='rss'); вместе с поллером (в тестах выключен)."""
    if not settings.MAX_POLLER_ENABLED or not getattr(settings, "RSS_INGEST_ENABLED", True):
        return None
    interval = max(60, int(getattr(settings, "RSS_POLL_INTERVAL", 0) or 300))

    async def _loop() -> None:
        from app.services.sources import sync_rss

        await asyncio.sleep(5)
        while True:
            try:
                async with db.session_scope() as session:
                    res = await sync_rss(session)
                if res.new_messages or res.errors:
                    log.info("RSS sync: chats=%s new=%s errors=%s", res.chats, res.new_messages, res.errors[:3])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("RSS sync failed")
            await asyncio.sleep(interval)

    return asyncio.create_task(_loop(), name="newsflow-rss")


async def _seed() -> None:
    if not settings.SEED_ON_STARTUP:
        return
    try:
        from app.seed import seed_if_empty

        async with db.session_scope() as session:
            if await seed_if_empty(session):
                log.info("Seed: demo data created")
    except Exception:  # noqa: BLE001
        log.exception("Seed failed")


def _start_poller() -> asyncio.Task | None:
    if not settings.MAX_POLLER_ENABLED:
        return None
    try:
        from app.integrations.max.poller import effective_interval, start_poller

        task = start_poller(settings.MAX_POLL_INTERVAL)
        log.info("Sources poller started (every %ss)", effective_interval())
        return task
    except NotImplementedError:
        log.warning("MAX poller not implemented yet — skipped")
    except Exception:  # noqa: BLE001
        log.exception("MAX poller failed to start")
    return None


def _start_telegram() -> list[asyncio.Task]:
    """Telegram: listener + sync (пакет DEV-TELEGRAM). Только вместе с поллером (в тестах выключен)."""
    if not (settings.TG_ENABLED and settings.MAX_POLLER_ENABLED):
        return []
    try:
        from app.integrations.max.poller import set_telegram_runner_active
        from app.integrations.telegram.runner import start_telegram_sync

        tasks = start_telegram_sync()
        set_telegram_runner_active(True)
        return tasks
    except ImportError:
        log.info("Telegram package not available — skipped")
    except Exception:  # noqa: BLE001
        log.exception("Telegram source failed to start")
    return []


async def _stop_telegram(tasks: list[asyncio.Task]) -> None:
    if not tasks:
        return
    try:
        from app.integrations.max.poller import set_telegram_runner_active
        from app.integrations.telegram.runner import stop_telegram_sync

        set_telegram_runner_active(False)
        await stop_telegram_sync(tasks)
    except Exception:  # noqa: BLE001
        log.exception("Telegram stop failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await upgrade_schema()
    await _seed()
    poller = _start_poller()
    tg_tasks = _start_telegram()
    rss_task = _start_rss()
    try:
        yield
    finally:
        await _stop_telegram(tg_tasks)
        for task in (poller, rss_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await db.dispose()


_DEFAULT_ERROR_MESSAGES = {
    403: "Нет доступа к этой странице.",
    404: "Страница или объект не найдены.",
}


async def _html_error(request: Request, exc: StarletteHTTPException):
    """403/404 на HTML-страницах -> error.html (запрос DEV-FRONTEND, CONTRACTS §9)."""
    from app.auth import COOKIE_NAME, read_session_token
    from app.models import User
    from app.routers.pages import _error

    detail = exc.detail if isinstance(exc.detail, str) else ""
    if not detail or detail in ("Not Found", "Forbidden"):
        detail = _DEFAULT_ERROR_MESSAGES[exc.status_code]
    async with db.SessionLocal() as session:
        uid = read_session_token(request.cookies.get(COOKIE_NAME))
        user = await session.get(User, uid) if uid is not None else None
        return await _error(request, user, session, exc.status_code, detail)


def create_app() -> FastAPI:
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from app.routers import api, pages

    app.include_router(api.router)
    app.include_router(pages.router)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        # Страницы: неавторизованный -> /login. API — обычный JSON.
        path = request.url.path
        is_page = not path.startswith(("/api", "/static"))
        if exc.status_code == 401 and is_page:
            return RedirectResponse("/login", status_code=303)
        if exc.status_code in (403, 404) and is_page:
            try:
                return await _html_error(request, exc)
            except Exception:  # noqa: BLE001  страница ошибки не должна ронять ответ
                log.exception("error.html render failed")
        return await http_exception_handler(request, exc)

    @app.exception_handler(NotImplementedError)
    async def _not_impl(request: Request, exc: NotImplementedError):
        return JSONResponse({"detail": f"Not implemented yet: {exc}"}, status_code=501)

    return app


app = create_app()
