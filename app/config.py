"""Конфигурация приложения (pydantic-settings, переменные окружения / .env).

Режим данных (TZ_v3): ``DATA_MODE=live`` (по умолчанию) — реальные источники: Google News RSS +
проверенные RSS-ленты СМИ/ведомств, Telegram web-превью t.me/s (стартовый набор каналов), MAX Bot API
(по токену) / публичные страницы max.ru (только метаданные); без ANTHROPIC_API_KEY — экстрактивный
анализ без выдуманных фактов. ``DATA_MODE=demo`` — офлайн-синтетика (сценарий «Чертаново»): mock MAX,
mock Telegram, mock-поиск, MockLLM — как в v2. Явное ``*_PROVIDER=mock`` работает в любом режиме.
Использование: ``from app.config import settings``.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Общие ---
    APP_NAME: str = "NEWSFLOW AI"
    # live — реальные данные (по умолчанию); demo — офлайн-синтетика (сценарий «Чертаново»)
    DATA_MODE: Literal["live", "demo"] = "live"
    # Вежливый User-Agent для всех внешних запросов (без персональных данных)
    HTTP_USER_AGENT: str = "NewsflowAI/1.0 (newsroom research tool; RSS reader)"
    HTTP_TIMEOUT: float = 12.0
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-newsflow-dev-secret"
    DEMO_PASSWORD: str = "demo"
    SESSION_MAX_AGE: int = 60 * 60 * 24 * 7  # секунды

    # --- БД ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./newsflow.db"
    DB_ECHO: bool = False
    SEED_ON_STARTUP: bool = True  # seed, если таблица users пуста

    # --- LLM ---
    # auto: anthropic если есть ключ; иначе live → extractive (без выдумок), demo → mock
    LLM_PROVIDER: Literal["auto", "anthropic", "extractive", "mock"] = "auto"
    ANTHROPIC_API_KEY: str | None = None
    LLM_MODEL: str = "claude-sonnet-5-5"
    LLM_TIMEOUT: float = 60.0
    LLM_MAX_TOKENS: int = 4000

    # --- Web search ---
    # auto: live → Google News RSS + RSS-ленты (+ Tavily/Serper, если есть ключ); demo → mock.
    # rss — только Google News RSS + ленты; tavily|serper — ключевой провайдер + RSS; mock — офлайн.
    SEARCH_PROVIDER: Literal["auto", "rss", "mock", "tavily", "serper"] = "auto"
    TAVILY_API_KEY: str | None = None
    SERPER_API_KEY: str | None = None
    SEARCH_CACHE_TTL: int = 300  # кэш ответов поиска/лент, секунды
    GOOGLE_NEWS_ENABLED: bool = True
    GOOGLE_NEWS_WHEN: str = "7d"  # окно свежести Google News (оператор when:), пусто — без ограничения
    # RSS-ленты: пусто — встроенный проверенный каталог (app/integrations/rss.py, DEFAULT_FEEDS);
    # иначе URL через запятую (заменяют каталог).
    RSS_FEEDS: str = ""
    RSS_INGEST_ENABLED: bool = True  # live: московские ленты попадают в ленту «Радар» (platform=rss)
    RSS_POLL_INTERVAL: int = 300  # сек между выгрузками RSS-источников (services.sources.sync_rss, ≥60)
    RSS_INGEST_MAX_AGE_HOURS: int = 24  # в ленту берём записи не старше N часов
    RSS_INGEST_PER_FEED: int = 30

    # --- MAX ---
    # auto: bot если есть токен; иначе live → web (публичные страницы max.ru: только метаданные,
    # постов без логина нет — см. docs/MAX.md), demo → mock
    MAX_PROVIDER: Literal["auto", "bot", "web", "mock"] = "auto"
    MAX_BOT_TOKEN: str | None = None
    MAX_API_BASE: str = "https://platform-api2.max.ru"  # с 19.07.2026 (сертификат Минцифры)
    MAX_API_FALLBACK_BASE: str = "https://platform-api.max.ru"  # если TLS к api2 не проходит
    MAX_CA_BUNDLE: str | None = None  # PEM с «Russian Trusted Root CA» для platform-api2
    MAX_CHAT_IDS: str = ""  # chat_id чатов/каналов, где бот — админ (GET /chats отключён с 06.2026)
    MAX_SOURCES: str = ""  # публичные каналы max.ru/<name> через запятую (метаданные, web-режим)
    MAX_POLL_INTERVAL: int = 5  # секунды между синхронизациями (в mock-режиме ограничивается ≤ 5)
    MAX_POLLER_ENABLED: bool = True

    # --- Демо-поток (трикл сообщений сценария у MockMaxClient) ---
    DEMO_STREAM_ENABLED: bool = True  # False — все сообщения сценария доступны сразу (как в v1)
    DEMO_STREAM_INITIAL: int = 10  # сколько сообщений «уже есть» при старте
    DEMO_STREAM_MIN_SEC: float = 10.0  # интервал между новыми сообщениями, от
    DEMO_STREAM_MAX_SEC: float = 30.0  # ... до
    DEMO_STREAM_LOOP: bool = False  # после исчерпания начать новый круг

    # --- Telegram (пакет app/integrations/telegram, DEV-TELEGRAM) ---
    TG_ENABLED: bool = True
    # Значения по умолчанию совпадают с app/integrations/telegram/settings.py (пакет читает env сам).
    # auto: telethon (ключи+сессия) → bot (токен) → live: web (t.me/s) | demo: mock
    TG_PROVIDER: Literal["auto", "telethon", "bot", "web", "mock"] = "auto"
    TG_SOURCES: str = ""  # @username / t.me-ссылки через запятую; пусто в live — стартовый набор
    TG_API_ID: int | None = None
    TG_API_HASH: str | None = None
    TG_SESSION_PATH: str = "data/telegram/newsflow"  # Telethon допишет .session
    TG_BOT_TOKEN: str | None = None
    TG_POLL_INTERVAL: int = 20
    TG_WEB_POLL_INTERVAL: int = 30

    @field_validator("TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN", "MAX_BOT_TOKEN", "MAX_CA_BUNDLE",
                     "ANTHROPIC_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    # --- Реальное время ---
    SSE_POLL_SEC: float = 1.0  # как часто SSE-поток проверяет БД
    SSE_KEEPALIVE_SEC: float = 15.0
    EMERGING_WINDOW_MIN: int = 240  # окно кластеризации «возникающих сюжетов», минуты
    EMERGING_MIN_MESSAGES: int = 3

    # --- Пайплайн ---
    CHAIN_MAX_ITERATIONS: int = 3
    PIPELINE_INLINE: bool = False  # True: /run ждёт окончания пайплайна (удобно в тестах)
    STALE_AFTER_MINUTES: int = 30  # «завис» в одном статусе (подсветка в списке сюжетов)


    @property
    def is_live(self) -> bool:
        return self.DATA_MODE == "live"

    @property
    def is_demo(self) -> bool:
        return self.DATA_MODE == "demo"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


DATA_MODE_LABELS = {"live": "Реальные данные", "demo": "Демо (синтетика)"}


def data_mode_status() -> dict:
    """Для UI/API (бейдж режима): какой режим и какие провайдеры реально выбраны.

    ``{"mode": "live", "label": "Реальные данные", "llm": "anthropic|extractive|mock",
    "search": "rss+google_news|tavily+rss|mock…", "max": "bot|web|mock", "telegram": "web|telethon|bot|mock",
    "rss_ingest": bool, "notes": [..]}``. Ошибки провайдеров не пробрасываются."""
    out: dict = {"mode": settings.DATA_MODE, "label": DATA_MODE_LABELS.get(settings.DATA_MODE, settings.DATA_MODE),
                 "notes": []}
    try:
        from app.ai.llm import get_llm

        out["llm"] = get_llm().name
    except Exception as e:  # noqa: BLE001
        out["llm"] = f"error: {e}"
    try:
        from app.integrations.search import get_search_provider

        out["search"] = get_search_provider().name
    except Exception as e:  # noqa: BLE001
        out["search"] = f"error: {e}"
    try:
        from app.integrations.max.client import get_max_client

        out["max"] = get_max_client().name
    except Exception as e:  # noqa: BLE001
        out["max"] = f"error: {e}"
    try:
        from app.integrations.telegram.client import choose_provider

        out["telegram"] = choose_provider() if settings.TG_ENABLED else "off"
    except Exception as e:  # noqa: BLE001
        out["telegram"] = f"error: {e}"
    out["rss_ingest"] = bool(settings.is_live and settings.RSS_INGEST_ENABLED)
    if out.get("llm") == "extractive":
        out["notes"].append("Без ANTHROPIC_API_KEY: справка собирается из цитат найденных источников, "
                            "противоречия — только явные")
    if out.get("max") == "web":
        out["notes"].append("MAX без токена бота: доступны только названия/аватары публичных каналов, "
                            "посты — через Bot API (MAX_BOT_TOKEN)")
    return out
