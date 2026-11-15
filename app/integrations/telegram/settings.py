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

    # auto (по умолчанию): telethon → bot → DATA_MODE=live: web | demo: mock; либо явно
    TG_PROVIDER: Literal["auto", "telethon", "bot", "web", "mock"] = "auto"
    # Источники: @username / t.me-ссылки / username через запятую (для telethon и web).
    # Пусто в DATA_MODE=live → стартовый набор LIVE_DEFAULT_SOURCES.
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
        own = [s.strip() for s in self.TG_SOURCES.replace(";", ",").split(",") if s.strip()]
        if own:
            return own
        return list(LIVE_DEFAULT_SOURCES) if _is_live() else []

    @property
    def session_file(self) -> Path:
        p = Path(self.TG_SESSION_PATH)
        return p if p.suffix == ".session" else p.with_name(p.name + ".session")

    @property
    def has_telethon_keys(self) -> bool:
        return bool(self.TG_API_ID and self.TG_API_HASH)


# Стартовый набор для DATA_MODE=live: реальные публичные московские новостные каналы, у которых
# открыто веб-превью t.me/s (проверено запросом 2026-11-15: HTTP 200, лента постов, свежие посты).
LIVE_DEFAULT_SOURCES: tuple[str, ...] = (
    "@moscowmap",          # Новости Москвы (~2,6 млн)
    "@moscowach",          # Москвач • Новости Москвы (~3,9 млн)
    "@infomoscow24",       # Москва 24 (~490 тыс.)
    "@ostorozhno_moskva",  # Осторожно, Москва (~400 тыс.)
    "@msk1_news",          # MSK1.RU | Новости Москвы (~116 тыс.)
    "@mos_sobyanin",       # Мэр Москвы Сергей Собянин (~370 тыс.) — официальный
)


def _is_live() -> bool:
    try:
        from app.config import settings

        return settings.DATA_MODE == "live"
    except Exception:  # noqa: BLE001
        return False


@lru_cache
def get_tg_settings() -> TelegramSettings:
    return TelegramSettings()
