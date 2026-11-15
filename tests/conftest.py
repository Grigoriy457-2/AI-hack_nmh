"""Общие фикстуры QA для v2 (один редактор, реальное время). Контракт — docs/CONTRACTS_v2.md.

* Окружение выставляется ДО импорта ``app`` (settings читаются при импорте): SQLite во временном
  файле, ``PIPELINE_INLINE=true``, поллер выключен, все провайдеры mock, ``TG_PROVIDER=mock``,
  ``DEMO_STREAM_ENABLED=false`` (все 40 сообщений сценария сразу — детерминированно).
* Якорь сценария (``mock_data.set_scenario_anchor``) фиксируется на всю сессию: «сейчас − 125 мин»
  (как демо-поток), чтобы сообщения MAX/Telegram были «свежими» и попадали в окно возникающих
  сюжетов (240 мин) независимо от времени суток запуска тестов.
* Каждый тест получает свою БД — копию файла-шаблона (пустая схема или схема + seed).
* Глобальное состояние процесса (якорь, демо-поток, кэш анализа ленты, клиенты MAX/Telegram,
  LLM, поиск) сбрасывается перед каждым тестом.
* Клиенты — ``httpx.AsyncClient(ASGITransport)`` с cookie редактора (вход — отдельные тесты).

Никаких xfail/skip-подмен: любой неверный код ответа или исключение — FAIL.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import timedelta
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
        "TG_PROVIDER": "mock",
        "TG_ENABLED": "true",
        "TG_SOURCES": "",
        "TG_API_ID": "",
        "TG_API_HASH": "",
        "TG_BOT_TOKEN": "",
        "DATA_MODE": "demo",
        "DEMO_STREAM_ENABLED": "false",
        "DEMO_STREAM_LOOP": "false",
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
from app.models import utcnow  # noqa: E402

LOGINS = ("editor", "editor2")

# Якорь сценария «Чертаново, хлопок 14:30» на всю сессию (см. docstring).
TEST_ANCHOR = utcnow().replace(microsecond=0) - timedelta(minutes=125)


# ---------------------------------------------------------------- глобальное состояние
def reset_globals() -> None:
    from app.ai import mock_data
    from app.ai.llm import MockLLM, set_llm
    from app.integrations.max import demo_stream, poller
    from app.integrations.max.client import set_max_client
    from app.integrations.max.mock import MockMaxClient
    from app.integrations.search import MockSearchProvider, set_search_provider
    from app.integrations.telegram.client import set_telegram_client
    from app.integrations.telegram.mock import MockTelegramClient
    from app.services import emerging

    mock_data.set_scenario_anchor(TEST_ANCHOR)
    demo_stream.reset_state(epoch=0)
    emerging.reset_cache()
    poller.set_telegram_runner_active(False)
    set_llm(MockLLM())
    set_search_provider(MockSearchProvider())
    set_max_client(MockMaxClient(stream=False))
    set_telegram_client(MockTelegramClient())
    _reset_v3_caches()


def _reset_v3_caches() -> None:
    """Кэши v3 (resolve, ленты RSS, Google News, og:image) — между тестами не переносятся."""
    from app.integrations import rss, search
    from app.integrations.max import public as max_public
    from app.integrations.telegram import resolve as tg_resolve
    from app.services import sources

    sources.clear_resolve_cache()
    tg_resolve._cache.clear()
    max_public._cache.clear()
    rss.reset_cache()
    rss._feed_locks.clear()
    search._og_cache.clear()


# ---------------------------------------------------------------- сеть
class FakeNet:
    """Подмена внешней сети для httpx.AsyncClient без явного transport (все клиенты приложения).

    ``fake_net.route(url_or_prefix, response | callable(request) -> response)``; совпадение — точный URL
    или префикс (самый длинный). Всё остальное — ConnectError «сеть в тестах отключена».
    ``fake_net.calls`` — запрошенные URL."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []

    def route(self, url: str, resp) -> None:
        self.routes[url] = resp

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        key = url if url in self.routes else max((k for k in self.routes if url.startswith(k)), key=len, default=None)
        if key is None:
            raise httpx.ConnectError(f"сеть в тестах отключена: {url}", request=request)
        resp = self.routes[key]
        resp = resp(request) if callable(resp) else resp
        if isinstance(resp, httpx.Response):
            return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content, request=request)
        return resp


_ORIG_ASYNC_INIT = httpx.AsyncClient.__init__


@pytest.fixture(autouse=True)
def fake_net(request, monkeypatch) -> FakeNet:
    """По умолчанию тесты не ходят в сеть (кроме маркера ``live``)."""
    net = FakeNet()
    if request.node.get_closest_marker("live"):
        yield net
        return

    def init(self, *a, **kw):
        if kw.get("transport") is None and kw.get("app") is None:
            kw["transport"] = httpx.MockTransport(net.handler)
        _ORIG_ASYNC_INIT(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    yield net


# ---------------------------------------------------------------- БД
_EMPTY_TEMPLATE = _TMP / "empty_template.db"
_SEED_TEMPLATE = _TMP / "seed_template.db"


@pytest.fixture
async def db_ready(tmp_path):
    """Пустая БД (только таблицы). Схема создаётся один раз в файл-шаблон, дальше — копия."""
    reset_globals()
    if not _EMPTY_TEMPLATE.exists():
        db.configure(f"sqlite+aiosqlite:///{_EMPTY_TEMPLATE.as_posix()}")
        await db.init_db(drop=True)
        await db.dispose()
    target = tmp_path / "nf.db"
    shutil.copyfile(_EMPTY_TEMPLATE, target)
    db.configure(f"sqlite+aiosqlite:///{target.as_posix()}")
    yield
    await db.dispose()
    reset_globals()


@pytest.fixture
async def session(db_ready):
    async with db.SessionLocal() as s:
        yield s


async def _make_seed_template() -> None:
    """Один раз за сессию: пустая БД + seed_if_empty (как в lifespan) -> файл-шаблон."""
    from app.seed import seed_if_empty

    db.configure(f"sqlite+aiosqlite:///{_SEED_TEMPLATE.as_posix()}")
    await db.init_db(drop=True)
    async with db.session_scope() as s:
        await seed_if_empty(s)
    await db.dispose()


@pytest.fixture
async def seeded(db_ready, tmp_path):
    """БД после seed (без запуска приложения)."""
    await db.dispose()
    if not _SEED_TEMPLATE.exists():
        await _make_seed_template()
        reset_globals()
    target = tmp_path / "nf.db"
    shutil.copyfile(_SEED_TEMPLATE, target)
    db.configure(f"sqlite+aiosqlite:///{target.as_posix()}")
    yield


@pytest.fixture
async def app_started(seeded):
    """Приложение с отработавшим lifespan (init_db + seed_if_empty видит заполненную БД)."""
    from app.main import app

    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def users(app_started) -> dict[str, int]:
    from app.models import User

    async with db.SessionLocal() as s:
        rows = (await s.execute(select(User))).scalars().all()
    ids = {u.login: u.id for u in rows}
    missing = [x for x in LOGINS if x not in ids]
    assert not missing, f"seed не создал редакторов {missing} (users={ids})"
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
            timeout=120,
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
def editor(make_client):
    return make_client("editor")


@pytest.fixture
def editor2(make_client):
    return make_client("editor2")


# ---------------------------------------------------------------- данные
@pytest.fixture
async def review_story(app_started, users) -> dict:
    """Сюжет в review с источниками/кластерами/профилями/справкой/черновиком v1 — собран вручную
    по контрактам (не зависит от пайплайна)."""
    from nf_helpers import build_review_story

    return await build_review_story(users)


@pytest.fixture
async def with_telegram(editor) -> list[dict]:
    """Подключить mock-Telegram (POST /api/sources) — синхронизируются все 3 демо-источника."""
    from nf_helpers import data

    data(await editor.post("/api/sources", json={"platform": "telegram", "ref": "@moscow_live_demo"}), 201)
    chats = data(await editor.get("/api/sources/chats", params={"platform": "telegram"}))
    return chats
