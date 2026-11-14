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
        from app.integrations.max.poller import start_poller

        task = start_poller(settings.MAX_POLL_INTERVAL)
        log.info("MAX poller started (every %ss)", settings.MAX_POLL_INTERVAL)
        return task
    except NotImplementedError:
        log.warning("MAX poller not implemented yet — skipped")
    except Exception:  # noqa: BLE001
        log.exception("MAX poller failed to start")
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await _seed()
    poller = _start_poller()
    try:
        yield
    finally:
        if poller is not None:
            poller.cancel()
            try:
                await poller
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await db.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from app.routers import api, pages

    app.include_router(api.router)
    app.include_router(pages.router)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        # Страницы: неавторизованный -> /login. API — обычный JSON.
        if exc.status_code == 401 and not request.url.path.startswith("/api"):
            return RedirectResponse("/login", status_code=303)
        return await http_exception_handler(request, exc)

    @app.exception_handler(NotImplementedError)
    async def _not_impl(request: Request, exc: NotImplementedError):
        return JSONResponse({"detail": f"Not implemented yet: {exc}"}, status_code=501)

    return app


app = create_app()
