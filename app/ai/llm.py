"""LLM-клиент (контракт). Зона DEV-AI.

Интерфейс ``LLMClient`` + реализации ``AnthropicLLM`` (реальный Claude) и ``MockLLM``
(детерминированные ответы для демо «Чертаново, хлопок ~14:30» и разумные дженерики).
Все промпты — в ``app/ai/prompts.py``. Остальной код получает клиента ТОЛЬКО через ``get_llm()``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMError(RuntimeError):
    """Ошибка провайдера / невалидный JSON после ретраев."""


class LLMClient(ABC):
    """Абстрактный LLM-клиент. Все методы async и не бросают сетевые исключения наружу
    (кроме LLMError)."""

    name: str = "abstract"

    @abstractmethod
    async def complete_json(
        self,
        system: str,
        user: str,
        schema_hint: dict[str, Any] | str | None = None,
        *,
        task: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Вернуть JSON-объект (dict).

        :param system: системный промпт (из prompts.py).
        :param user: пользовательское сообщение (данные задачи).
        :param schema_hint: пример/описание ожидаемой структуры — добавляется в промпт.
        :param task: машинный ключ задачи ("extract_entities", "chain_hypotheses",
            "relevance", "importance", "intelligence", "report", "draft", ...).
            MockLLM выбирает ответ по нему; AnthropicLLM может игнорировать.
        Реализация обязана распарсить ответ (в т.ч. из ```json блока), при неудаче —
        1 ретрай, затем LLMError.
        """

    @abstractmethod
    async def complete_text(
        self,
        system: str,
        user: str,
        *,
        task: str | None = None,
        temperature: float = 0.5,
        max_tokens: int | None = None,
    ) -> str:
        """Вернуть свободный текст."""

    async def aclose(self) -> None:  # pragma: no cover - опционально
        """Закрыть HTTP-ресурсы."""


class AnthropicLLM(LLMClient):
    """Claude через SDK ``anthropic`` (AsyncAnthropic). Модель — settings.LLM_MODEL."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def complete_json(self, system, user, schema_hint=None, *, task=None,
                            temperature=0.2, max_tokens=None) -> dict[str, Any]:
        raise NotImplementedError("DEV-AI: AnthropicLLM.complete_json")

    async def complete_text(self, system, user, *, task=None, temperature=0.5,
                            max_tokens=None) -> str:
        raise NotImplementedError("DEV-AI: AnthropicLLM.complete_text")


class MockLLM(LLMClient):
    """Детерминированный офлайн-LLM. Ответы берёт из ``app/ai/mock_data.py`` по ``task``."""

    name = "mock"

    async def complete_json(self, system, user, schema_hint=None, *, task=None,
                            temperature=0.2, max_tokens=None) -> dict[str, Any]:
        raise NotImplementedError("DEV-AI: MockLLM.complete_json")

    async def complete_text(self, system, user, *, task=None, temperature=0.5,
                            max_tokens=None) -> str:
        raise NotImplementedError("DEV-AI: MockLLM.complete_text")


_llm: LLMClient | None = None


def get_llm() -> LLMClient:
    """Синглтон-фабрика. LLM_PROVIDER=auto → AnthropicLLM при наличии ANTHROPIC_API_KEY,
    иначе MockLLM. Тесты могут подменить через ``set_llm(MockLLM())``."""
    global _llm
    if _llm is None:
        from app.config import settings

        use_real = settings.LLM_PROVIDER == "anthropic" or (
            settings.LLM_PROVIDER == "auto" and bool(settings.ANTHROPIC_API_KEY)
        )
        if use_real and settings.ANTHROPIC_API_KEY:
            _llm = AnthropicLLM(settings.ANTHROPIC_API_KEY, settings.LLM_MODEL, settings.LLM_TIMEOUT)
        else:
            _llm = MockLLM()
    return _llm


def set_llm(client: LLMClient | None) -> None:
    """Подменить/сбросить синглтон (для тестов)."""
    global _llm
    _llm = client
