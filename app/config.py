"""Конфигурация приложения (pydantic-settings, переменные окружения / .env).

Все внешние провайдеры по умолчанию в mock-режиме: демо работает без интернета и ключей.
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
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-newsflow-dev-secret"
    DEMO_PASSWORD: str = "demo"
    SESSION_MAX_AGE: int = 60 * 60 * 24 * 7  # секунды

    # --- БД ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./newsflow.db"
    DB_ECHO: bool = False
    SEED_ON_STARTUP: bool = True  # seed, если таблица users пуста

    # --- LLM ---
    LLM_PROVIDER: Literal["auto", "anthropic", "mock"] = "auto"  # auto: anthropic если есть ключ
    ANTHROPIC_API_KEY: str | None = None
    LLM_MODEL: str = "claude-sonnet-5-5"
    LLM_TIMEOUT: float = 60.0
    LLM_MAX_TOKENS: int = 4000

    # --- Web search ---
    SEARCH_PROVIDER: Literal["mock", "tavily", "serper"] = "mock"
    TAVILY_API_KEY: str | None = None
    SERPER_API_KEY: str | None = None

    # --- MAX ---
    MAX_PROVIDER: Literal["auto", "bot", "mock"] = "auto"  # auto: bot если есть токен
    MAX_BOT_TOKEN: str | None = None
    MAX_API_BASE: str = "https://platform-api.max.ru"
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
    TG_PROVIDER: Literal["auto", "telethon", "bot", "web", "mock"] = "mock"
    TG_SOURCES: str = ""  # @username / t.me-ссылки через запятую
    TG_API_ID: int | None = None
    TG_API_HASH: str | None = None
    TG_SESSION_PATH: str = "data/telegram/newsflow"  # Telethon допишет .session
    TG_BOT_TOKEN: str | None = None
    TG_POLL_INTERVAL: int = 20
    TG_WEB_POLL_INTERVAL: int = 30

    @field_validator("TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN", mode="before")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
