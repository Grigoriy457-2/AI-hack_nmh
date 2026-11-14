"""Настройки Telegram-источника (зона DEV-TELEGRAM).

Читаются из переменных окружения / ``.env`` теми же именами, что предлагается добавить в
``app.config.Settings`` (см. docs/TELEGRAM.md, «Как подключить»). Пакет держит собственный
объект настроек, чтобы работать независимо от того, успел ли бэкенд добавить ключи в config.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelegramSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # mock (по умолчанию) | telethon | bot | web | auto (лучший из доступных)
    TG_PROVIDER: Literal["auto", "telethon", "bot", "web", "mock"] = "mock"
    # Источники: @username / t.me-ссылки / username через запятую (для telethon и web).
    TG_SOURCES: str = ""

    # --- Telethon (MTProto, пользовательская сессия) ---
    TG_API_ID: int | None = None  # https://my.telegram.org -> API development tools
    TG_API_HASH: str | None = None
    TG_SESSION_PATH: str = "data/telegram/newsflow"  # Telethon допишет «.session»
    TG_FLOOD_MAX_WAIT: int = 120  # FloodWait до N секунд ждём сами, дольше — ошибка

    # --- Bot API ---
    TG_BOT_TOKEN: str | None = None
    TG_BOT_API_BASE: str = "https://api.telegram.org"

    # --- Web preview (t.me/s/<channel>) ---
    TG_WEB_BASE: str = "https://t.me"

    # --- Общие ---
    TG_POLL_INTERVAL: int = 20  # секунды между опросами (web/telethon-polling/poller)
    TG_BACKFILL: int = 30  # сколько последних сообщений брать при первом подключении источника

    @field_validator("TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def sources(self) -> list[str]:
        return [s.strip() for s in self.TG_SOURCES.replace(";", ",").split(",") if s.strip()]

    @property
    def session_file(self) -> Path:
        p = Path(self.TG_SESSION_PATH)
        return p if p.suffix == ".session" else p.with_name(p.name + ".session")

    @property
    def has_telethon_keys(self) -> bool:
        return bool(self.TG_API_ID and self.TG_API_HASH)


@lru_cache
def get_tg_settings() -> TelegramSettings:
    return TelegramSettings()
