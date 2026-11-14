"""Конфигурация приложения (pydantic-settings, переменные окружения / .env).

Все внешние провайдеры по умолчанию в mock-режиме: демо работает без интернета и ключей.
Использование: ``from app.config import settings``.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

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
    MAX_POLL_INTERVAL: int = 60  # секунды между синхронизациями
    MAX_POLLER_ENABLED: bool = True

    # --- Пайплайн ---
    CHAIN_MAX_ITERATIONS: int = 3
    PIPELINE_INLINE: bool = False  # True: /run ждёт окончания пайплайна (удобно в тестах)
    STALE_AFTER_MINUTES: int = 30  # «зависшая» задача на доске оператора


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
