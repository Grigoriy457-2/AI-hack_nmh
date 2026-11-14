"""Общие фикстуры QA (CONTRACTS.md §8).

* Окружение выставляется ДО импорта ``app`` (settings читаются при импорте):
  SQLite во временном файле, PIPELINE_INLINE=true, poller выключен, все провайдеры mock.
* Каждый тест получает СВОЮ БД (tmp_path) — ``db.configure(url)`` + ``init_db(drop=True)``.
* Провайдеры подменяются через ``set_llm`` / ``set_search_provider`` / ``set_max_client``.
* Клиенты: ``httpx.AsyncClient(transport=ASGITransport(app))`` внутри ``lifespan_context``;
  вход — cookie ``auth.make_session_token(uid)`` (сам эндпоинт логина тестируется отдельно).

Пока часть контрактов — заглушки, тест, упавший на ``NotImplementedError`` (или получивший
HTTP 501 через ``nf_helpers.check``), помечается как xfail «not implemented yet». Это НЕ
маскирует баги: любое другое исключение / неверный код ответа — обычный FAIL.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="nf_tests_"))
os.environ.update(
    {
        "DATABASE_URL": f"sqlite+aiosqlite:///{(_TMP / 'import.db').as_posix()}",
        "PIPELINE_INLINE": "true",
        "MAX_POLLER_ENABLED": "false",
        "SEED_ON_STARTUP": "true",
        "LLM_PROVIDER": "mock",
        "SEARCH_PROVIDER": "mock",
        "MAX_PROVIDER": "mock",
        "ANTHROPIC_API_KEY": "",
        "MAX_BOT_TOKEN": "",
        "TAVILY_API_KEY": "",
        "SERPER_API_KEY": "",
        "DEBUG": "false",
    }
)

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db  # noqa: E402

LOGINS = ("producer", "producer2", "editor", "smm", "operator")


# ---------------------------------------------------------------- NotImplementedError -> xfail
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if (
        rep.failed
        and call.excinfo is not None
        and call.excinfo.errisinstance(NotImplementedError)
    ):
        rep.outcome = "skipped"
        rep.wasxfail = f"not implemented yet: {call.excinfo.value}"


# ---------------------------------------------------------------- провайдеры
def reset_providers() -> None:
    from app.ai.llm import MockLLM, set_llm
    from app.integrations.max.client import set_max_client
    from app.integrations.max.mock import MockMaxClient
    from app.integrations.search import MockSearchProvider, set_search_provider

    set_llm(MockLLM())
    set_search_provider(MockSearchProvider())
    set_max_client(MockMaxClient())


# ---------------------------------------------------------------- БД
_EMPTY_TEMPLATE = _TMP / "empty_template.db"


@pytest.fixture
async def db_ready(tmp_path):
    """Пустая БД (только таблицы) во временном файле. Схема создаётся один раз (init_db(drop=True))
    в файл-шаблон, дальше — копия файла (create_all на Windows ~0.7 с на тест)."""
    import shutil

    if not _EMPTY_TEMPLATE.exists():
        db.configure(f"sqlite+aiosqlite:///{_EMPTY_TEMPLATE.as_posix()}")
        await db.init_db(drop=True)
        await db.dispose()
    target = tmp_path / "nf.db"
    shutil.copyfile(_EMPTY_TEMPLATE, target)
    db.configure(f"sqlite+aiosqlite:///{target.as_posix()}")
    reset_providers()
    yield
    await db.dispose()


@pytest.fixture
async def session(db_ready):
    async with db.SessionLocal() as s:
        yield s


@pytest.fixture
async def seeded(db_ready):
    """БД + seed_if_empty (без запуска приложения)."""
    from app.seed import seed_if_empty

    async with db.session_scope() as s:
        await seed_if_empty(s)
    yield


# ---------------------------------------------------------------- приложение / клиенты
_SEED_TEMPLATE = _TMP / "seed_template.db"


async def _make_seed_template() -> None:
    """Один раз за сессию: пустая БД + seed_if_empty (как в lifespan) -> файл-шаблон.
    Дальше каждый тест копирует шаблон (seed ~1 с -> копия ~мс). lifespan всё равно отрабатывает
    (init_db + seed_if_empty, который видит заполненную БД и ничего не делает)."""
    from app.seed import seed_if_empty

    db.configure(f"sqlite+aiosqlite:///{_SEED_TEMPLATE.as_posix()}")
    await db.init_db(drop=True)
    async with db.session_scope() as s:
        await seed_if_empty(s)
    await db.dispose()


@pytest.fixture
async def app_started(db_ready, tmp_path):
    """Приложение с отработавшим lifespan (init_db + seed_if_empty, poller выключен)."""
    import shutil

    from app.main import app

    if os.environ.get("NF_TEST_NO_TEMPLATE") != "1":
        await db.dispose()
        if not _SEED_TEMPLATE.exists():
            await _make_seed_template()
        target = tmp_path / "nf.db"
        shutil.copyfile(_SEED_TEMPLATE, target)
        db.configure(f"sqlite+aiosqlite:///{target.as_posix()}")
        reset_providers()

    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def users(app_started) -> dict[str, int]:
    from app.models import User

    async with db.SessionLocal() as s:
        rows = (await s.execute(select(User))).scalars().all()
    ids = {u.login: u.id for u in rows}
    missing = [x for x in LOGINS if x not in ids]
    assert not missing, f"seed не создал пользователей {missing} (users={ids})"
    return ids


@pytest.fixture
async def make_client(app_started, users):
    from app.auth import COOKIE_NAME, make_session_token

    created: list[httpx.AsyncClient] = []

    def _make(login: str | None = None) -> httpx.AsyncClient:
        cookies = {COOKIE_NAME: make_session_token(users[login])} if login else None
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_started),
            base_url="http://testserver",
            cookies=cookies,
            follow_redirects=False,
            timeout=60,
        )
        created.append(c)
        return c

    yield _make
    for c in created:
        await c.aclose()


@pytest.fixture
def anon(make_client):
    return make_client(None)


@pytest.fixture
def producer(make_client):
    return make_client("producer")


@pytest.fixture
def producer2(make_client):
    return make_client("producer2")


@pytest.fixture
def editor(make_client):
    return make_client("editor")


@pytest.fixture
def smm(make_client):
    return make_client("smm")


@pytest.fixture
def operator(make_client):
    return make_client("operator")


@pytest.fixture
def clients(producer, producer2, editor, smm, operator, anon) -> dict[str, httpx.AsyncClient]:
    return {
        "producer": producer,
        "producer2": producer2,
        "editor": editor,
        "smm": smm,
        "operator": operator,
        "anon": anon,
    }


# ---------------------------------------------------------------- данные (вставка напрямую в БД)
@pytest.fixture
async def researched(app_started, users) -> dict:
    """Расследование в report_ready с источниками/кластерами/профилями/справкой/черновиком v1,
    собранное вручную по контрактам (не зависит от пайплайна)."""
    from nf_helpers import build_researched_investigation

    return await build_researched_investigation(users)
