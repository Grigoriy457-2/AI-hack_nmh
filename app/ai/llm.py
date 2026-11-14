"""LLM-клиент (контракт). Зона DEV-AI.

Интерфейс ``LLMClient`` + реализации ``AnthropicLLM`` (реальный Claude) и ``MockLLM``
(детерминированные ответы для демо «Чертаново, хлопок ~14:30» и разумные дженерики).
Все промпты — в ``app/ai/prompts.py``. Остальной код получает клиента ТОЛЬКО через ``get_llm()``.

Пользовательское сообщение задач — JSON (см. prompts.py); MockLLM разбирает его и отвечает
по ``task``. AnthropicLLM при ошибке сети/API/парсинга после 1 ретрая логирует и отвечает
через MockLLM (``fallback_to_mock=True``), чтобы демо не падало.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any

from app.ai import mock_data as md
from app.ai.text import extract_json, first_sentence, hhmm_msk, stem, stems, words

log = logging.getLogger("newsflow.llm")


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


# ================================================================ Anthropic
_HEAVY_TASKS = {"intelligence", "draft", "chain_hypotheses"}


class AnthropicLLM(LLMClient):
    """Claude через SDK ``anthropic`` (AsyncAnthropic). Модель — settings.LLM_MODEL.

    Без ``temperature`` (новые модели принимают только значение по умолчанию); глубина
    рассуждений — ``output_config.effort`` (low для мелких задач, medium для аналитики/черновика).
    """

    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 60.0, *,
                 max_tokens: int | None = None, fallback_to_mock: bool = True) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.fallback_to_mock = fallback_to_mock
        self._client = None
        self._mock = MockLLM()
        self._disabled_reason: str | None = None  # 401/403 — дальше сразу MockLLM, без сети

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self.api_key, timeout=self.timeout, max_retries=1)
        return self._client

    def _default_max_tokens(self) -> int:
        if self.max_tokens:
            return self.max_tokens
        from app.config import settings

        return settings.LLM_MAX_TOKENS

    async def _raw(self, system: str, messages: list[dict[str, Any]], *, task: str | None,
                   max_tokens: int | None) -> str:
        import anthropic

        if self._disabled_reason:
            raise LLMError(self._disabled_reason)
        effort = "medium" if task in _HEAVY_TASKS else "low"
        try:
            resp = await self._get_client().messages.create(
                model=self.model,
                max_tokens=max_tokens or self._default_max_tokens(),
                system=system,
                messages=messages,
                extra_body={"output_config": {"effort": effort}},
            )
        except anthropic.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            if e.status_code in (401, 403):
                self._disabled_reason = f"Anthropic API отклонил ключ ({e.status_code}) — используется MockLLM"
            raise LLMError(f"API status {e.status_code}: {e}") from e
        except anthropic.AnthropicError as e:
            raise LLMError(str(e)) from e
        if getattr(resp, "stop_reason", None) == "refusal":
            raise LLMError("model refused the request")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise LLMError(f"empty response (stop_reason={getattr(resp, 'stop_reason', None)})")
        return text

    async def complete_json(self, system, user, schema_hint=None, *, task=None,
                            temperature=0.2, max_tokens=None) -> dict[str, Any]:
        sys_prompt = system
        if schema_hint is not None:
            hint = schema_hint if isinstance(schema_hint, str) else json.dumps(schema_hint, ensure_ascii=False, indent=1)
            sys_prompt += "\n\nФормат ответа — JSON такой структуры:\n" + hint
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        last_err: Exception | None = None
        try:
            for attempt in range(2):
                text = await self._raw(sys_prompt, messages, task=task, max_tokens=max_tokens)
                try:
                    data = extract_json(text)
                except ValueError as e:
                    last_err = e
                    messages = messages + [
                        {"role": "assistant", "content": text[:4000]},
                        {"role": "user", "content": "Ответ не разобрался как JSON. Верни ТОЛЬКО один валидный JSON-объект."},
                    ]
                    continue
                if isinstance(data, dict):
                    return data
                if isinstance(data, list):
                    return {"items": data}
                last_err = ValueError("JSON is not an object")
            raise LLMError(f"invalid JSON after retry: {last_err}")
        except LLMError as e:
            if not self.fallback_to_mock:
                raise
            log.warning("AnthropicLLM task=%s failed (%s) — fallback to MockLLM", task, e)
            return await self._mock.complete_json(system, user, schema_hint, task=task)

    async def complete_text(self, system, user, *, task=None, temperature=0.5,
                            max_tokens=None) -> str:
        try:
            return await self._raw(system, [{"role": "user", "content": user}], task=task, max_tokens=max_tokens)
        except LLMError as e:
            if not self.fallback_to_mock:
                raise
            log.warning("AnthropicLLM text task=%s failed (%s) — fallback to MockLLM", task, e)
            return await self._mock.complete_text(system, user, task=task)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


# ================================================================ Mock
def _payload(user: str) -> dict[str, Any]:
    try:
        data = extract_json(user)
        return data if isinstance(data, dict) else {"items": data}
    except ValueError:
        return {"lead_text": user}


def _parse_iso(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", ""))
        except ValueError:
            return None
    return None


_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_LOC_RE = re.compile(r"\b(?:в|во|на|у|возле|около|рядом с)\s+((?:[А-ЯЁ][\w-]+)(?:\s+(?:[А-ЯЁ][\w-]+|улиц\w+|шоссе|проспект\w*|переулк\w*))?)")


class MockLLM(LLMClient):
    """Детерминированный офлайн-LLM. Ответы берёт из ``app/ai/mock_data.py`` по ``task``."""

    name = "mock"

    async def complete_json(self, system, user, schema_hint=None, *, task=None,
                            temperature=0.2, max_tokens=None) -> dict[str, Any]:
        p = _payload(user)
        handler = getattr(self, f"_t_{task}", None) if task else None
        if handler is None:
            return {"ok": True, "task": task, "summary": first_sentence(str(p.get("lead_text") or user))}
        return copy.deepcopy(handler(p))

    async def complete_text(self, system, user, *, task=None, temperature=0.5,
                            max_tokens=None) -> str:
        p = _payload(user)
        if task == "report":
            return self._t_report(p)["summary"]
        return first_sentence(str(p.get("summary") or p.get("lead_text") or user), 400)

    # ------------------------------------------------------------ INPUT
    def _t_extract_entities(self, p: dict[str, Any]) -> dict[str, Any]:
        lead = str(p.get("lead_text") or "")
        if md.is_scenario(lead):
            ent = copy.deepcopy(md.SCENARIO_ENTITIES)
            ent["time"]["iso"] = md.scenario_event_time().isoformat()
            return ent
        lead_stems = stems(lead)
        event_type = "происшествие"
        for keys, label in md.EVENT_TYPES:
            if lead_stems & {stem(k) for k in keys}:
                event_type = label
                break
        time_text, iso = None, None
        m = _TIME_RE.search(lead)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            time_text = f"около {hh:02d}:{mm:02d}"
            now = _parse_iso(p.get("now")) or md.utcnow()
            msk_now = now + timedelta(hours=3)
            ev = msk_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if ev > msk_now + timedelta(minutes=30):
                ev -= timedelta(days=1)
            iso = (ev - timedelta(hours=3)).isoformat()
        lm = _LOC_RE.search(lead)
        loc_text = lm.group(1) if lm else None
        obj = None
        for k, v in (("дом", "жилой дом"), ("школ", "школа"), ("метро", "метро"), ("машин", "автомобиль"),
                     ("автомоб", "автомобиль"), ("магазин", "магазин"), ("парк", "парк"), ("труб", "инженерные сети")):
            if k in lead.lower():
                obj = v
                break
        loc_stems = stems(loc_text or "")
        ev_words: list[str] = []
        for w in words(lead):
            if w not in ev_words and stem(w) not in loc_stems and w not in ("около", "сегодня", "сейчас"):
                ev_words.append(w)
        kws = ev_words[:3] + ([loc_text] if loc_text else []) + ev_words[3:]
        return {
            "summary": first_sentence(lead, 200),
            "event_type": event_type,
            "object": obj,
            "location": {"text": loc_text, "district": None, "address": None},
            "time": {"text": time_text, "iso": iso},
            "keywords": kws[:7],
            "status": "unconfirmed",
        }

    # ------------------------------------------------------------ CHAIN
    def _t_chain_hypotheses(self, p: dict[str, Any]) -> dict[str, Any]:
        lead = str(p.get("lead_text") or "")
        n = int(p.get("n") or 1)
        max_it = int(p.get("max_iterations") or 3)
        if n > max_it:
            return {"continue": False, "rationale": "Достигнут лимит итераций.", "queries": []}
        if md.is_scenario(lead):
            if n <= len(md.SCENARIO_CHAIN):
                return {"continue": True, **md.SCENARIO_CHAIN[n - 1]}
            return {"continue": False, "rationale": "Ключевые гипотезы проверены, новых деталей нет.", "queries": []}

        ent = p.get("entities") or {}
        kws = [str(k) for k in (ent.get("keywords") or words(lead))][:5]
        loc = ((ent.get("location") or {}).get("text") or "").strip()
        base = " ".join(kws[:4]) or lead[:60]
        if n == 1:
            return {
                "continue": True,
                "rationale": f"Стартовые гипотезы по зацепке ({ent.get('event_type') or 'событие'}"
                             f"{', ' + loc if loc else ''}): ищем первые публикации, сообщения в домовых чатах, "
                             f"официальные сводки и медиа.",
                "queries": [
                    {"agent": "web", "query": base},
                    {"agent": "max_chats", "query": " ".join(kws[:3]) or base},
                    {"agent": "official", "query": f"{ent.get('event_type') or ''} {loc or base}".strip()},
                    {"agent": "media", "query": f"{base} фото видео"},
                ],
            }
        if n == 2:
            terms = list(md.GENERIC_FOLLOWUPS.get(str(ent.get("event_type") or ""), md.GENERIC_FOLLOWUPS["происшествие"]))
            subject = " ".join(kws[:2]) or base
            anchor = loc or subject
            n_found = len(p.get("findings") or [])
            return {
                "continue": True,
                "rationale": f"Первая итерация дала {n_found} новых материалов, но без официального подтверждения и "
                             f"деталей. Новые гипотезы для проверки: {', '.join(terms)}.",
                "queries": [
                    {"agent": "web", "query": f"{subject} {terms[0]} {loc}".strip()},
                    {"agent": "max_chats", "query": f"{terms[0]} {terms[1]}"},
                    {"agent": "official", "query": f"{terms[0]} {anchor}".strip()},
                    {"agent": "media", "query": f"{subject} {terms[2]} фото".strip()},
                ],
            }
        return {"continue": False,
                "rationale": "Новых существенных деталей не появилось — цепочка поиска остановлена.",
                "queries": []}

    # ------------------------------------------------------------ RELEVANCE / IMPORTANCE
    def _t_relevance(self, p: dict[str, Any]) -> dict[str, Any]:
        lead = str(p.get("lead_text") or "")
        scenario = md.is_scenario(lead)
        vocab = stems(lead) | stems(str(p.get("query") or ""))
        for k in (p.get("keywords") or []):
            vocab |= stems(str(k))
        scores: dict[str, float] = {}
        for m in p.get("messages") or []:
            mid, text = str(m.get("id")), str(m.get("text") or "")
            known = md.SCENARIO_MESSAGE_BY_TEXT.get(text.strip()) if scenario else None
            if known is not None:
                scores[mid] = 0.92 if known["relevant"] else 0.05
                continue
            overlap = len(stems(text) & vocab)
            scores[mid] = round(min(1.0, 0.3 * overlap), 2)
        return {"scores": scores}

    def _t_importance(self, p: dict[str, Any]) -> dict[str, Any]:
        from app.services.uniqueness import is_important_heuristic

        items = p.get("items") or p.get("messages") or []
        out: dict[str, bool] = {}
        for i in items:
            text = str(i.get("text") or "")
            known = md.SCENARIO_MESSAGE_BY_TEXT.get(text.strip())
            out[str(i.get("id"))] = (bool(known.get("important")) if known is not None
                                     else is_important_heuristic(text, bool(i.get("has_media"))))
        return {"important": out}

    # ------------------------------------------------------------ INTELLIGENCE
    @staticmethod
    def _resolver(sources: list[dict[str, Any]]):
        by_url: dict[str, list[int]] = {}
        by_msg: dict[str, list[int]] = {}
        by_text: dict[str, list[int]] = {}
        for s in sources:
            sid = s.get("id")
            url = str(s.get("url") or "")
            by_url.setdefault(url, []).append(sid)
            if url.startswith("max://"):
                by_msg.setdefault(url.rstrip("/").rsplit("/", 1)[-1], []).append(sid)
            by_text.setdefault(str(s.get("text") or "").strip(), []).append(sid)

        def resolve(keys: list[str]) -> list[int]:
            out: list[int] = []
            for k in keys:
                kind, _, ref = k.partition(":")
                ids: list[int] = []
                if kind == "msg":
                    ids = by_msg.get(ref, [])
                    if not ids:
                        msg = next((m for m in md.SCENARIO_MAX_MESSAGES if m["external_id"] == ref), None)
                        if msg:
                            ids = by_text.get(msg["text"].strip(), [])[:1]
                elif kind == "web":
                    e = md.SCENARIO_WEB_BY_KEY.get(ref)
                    ids = by_url.get(e["url"], []) if e else []
                for i in ids:
                    if i not in out:
                        out.append(i)
            return out

        return resolve

    def _t_intelligence(self, p: dict[str, Any]) -> dict[str, Any]:
        lead = str(p.get("lead_text") or "")
        sources = list(p.get("sources") or [])
        if md.is_scenario(lead):
            resolve = self._resolver(sources)
            spec = md.SCENARIO_INTEL
            out: dict[str, Any] = {k: spec[k] for k in ("title", "headline", "topic", "summary")}
            for section in ("timeline", "facts", "confirmed", "unconfirmed", "contradictions", "to_verify"):
                items = []
                for it in spec[section]:
                    ids = resolve(it.get("src", []))
                    if it.get("src") and not ids and section != "to_verify":
                        continue
                    item = {k: v for k, v in it.items() if k != "src"}
                    item["source_ids"] = ids
                    if section == "confirmed":
                        item["state"] = "confirmed"
                    items.append(item)
                out[section] = items
            return out
        return self._generic_intelligence(lead, p.get("entities") or {}, sources)

    @staticmethod
    def _generic_intelligence(lead: str, ent: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
        def t(s):
            return _parse_iso(s.get("published_at")) or datetime.max

        prim = [s for s in sources if s.get("is_primary", True)]
        prim.sort(key=t)
        kind_label = {"web": "СМИ/ТГ", "max_chat": "Домовой чат", "official": "Официально", "media": "Медиа"}
        official = [s for s in prim if s.get("kind") == "official"]
        others = [s for s in prim if s.get("kind") != "official"]
        summary_src = ent.get("summary") or first_sentence(lead, 200)
        title = summary_src.rstrip(".")
        n_all, n_prim = len(sources), len(prim)
        chats = sum(1 for s in prim if s.get("kind") == "max_chat")
        summary = (
            f"{summary_src.rstrip('.')}. Найдено {n_all} источников, из них {n_prim} первоисточников "
            f"(остальное — перепечатки и пересылки); {chats} сообщений из домовых чатов. "
            + ("Есть сообщение официальных служб, но детали требуют проверки."
               if official else "Официального подтверждения пока нет — все сведения предварительные.")
        )
        timeline = [{"time": hhmm_msk(_parse_iso(s.get("published_at"))),
                     "text": f"{kind_label.get(s.get('kind'), 'Источник')}: {first_sentence(str(s.get('text') or s.get('title') or ''), 120)}",
                     "source_ids": [s["id"]],
                     "state": "confirmed" if s.get("kind") == "official" else "unconfirmed"}
                    for s in prim[:7]]
        what = ent.get("event_type") or "происшествие"
        where = (ent.get("location") or {}).get("text")
        when = (ent.get("time") or {}).get("text")
        first_ids = [s["id"] for s in prim[:3]]
        facts = [{"text": f"Что: {what}" + (f" ({ent.get('object')})" if ent.get("object") else ""), "source_ids": first_ids, "state": "unconfirmed"}]
        if where:
            facts.append({"text": f"Где: {where}", "source_ids": first_ids, "state": "unconfirmed"})
        if when:
            facts.append({"text": f"Когда: {when}", "source_ids": first_ids, "state": "unconfirmed"})
        facts.append({"text": f"Источников: {n_all} (первоисточников {n_prim}, чатов MAX {chats})", "source_ids": [], "state": None})
        confirmed: list[dict[str, Any]] = []
        for s in official[:4]:
            sent = first_sentence(str(s.get("text") or ""), 200)
            same = next((c for c in confirmed if c["text"] == sent), None)
            if same:
                same["source_ids"].append(s["id"])
            else:
                confirmed.append({"text": sent, "claim": sent.rstrip("."), "source_ids": [s["id"]], "state": "confirmed"})
        important = [s for s in others if s.get("is_important")] or others
        unconfirmed: list[dict[str, Any]] = []
        if important:
            lead_sent = summary_src.rstrip(".")
            unconfirmed.append({"text": f"Сообщения очевидцев: {lead_sent}",
                                "claim": f"По сообщениям очевидцев, {lead_sent[:1].lower() + lead_sent[1:]}",
                                "source_ids": [s["id"] for s in important[:3]], "state": "unconfirmed"})
        for s in [x for x in important if x.get("kind") == "max_chat"][:2]:
            sent = first_sentence(str(s.get("text") or ""), 160).rstrip(".")
            unconfirmed.append({"text": f"Житель в домовом чате: «{sent}»", "claim": f"Жители пишут в домовых чатах: «{sent}»",
                                "source_ids": [s["id"]], "state": "unconfirmed"})
        contradictions = []
        groups = [("взрыв", ("взрыв", "взорв")), ("пожар/возгорание", ("пожар", "горит", "возгор", "сгорел")),
                  ("есть пострадавшие", ("пострадавш", "госпитализ", "ранен")),
                  ("пострадавших нет", ("пострадавших нет", "без пострадавших", "никто не пострадал"))]
        hits = {g: [s["id"] for s in prim if any(k in str(s.get("text") or "").lower() for k in keys)] for g, keys in groups}
        hits["есть пострадавшие"] = [i for i in hits["есть пострадавшие"] if i not in hits["пострадавших нет"]]
        if hits["взрыв"] and hits["пожар/возгорание"] and set(hits["взрыв"]) != set(hits["пожар/возгорание"]):
            contradictions.append({"text": "Версии расходятся: «взрыв» vs «пожар/возгорание»",
                                   "source_ids": sorted(set(hits["взрыв"] + hits["пожар/возгорание"]))[:6]})
        if hits["есть пострадавшие"] and hits["пострадавших нет"]:
            contradictions.append({"text": "Есть ли пострадавшие: источники противоречат друг другу",
                                   "source_ids": sorted(set(hits["есть пострадавшие"] + hits["пострадавших нет"]))[:6]})
        to_verify = [{"text": t, "source_ids": []} for t in md.GENERIC_TO_VERIFY]
        return {"title": title, "headline": title, "topic": None, "summary": summary,
                "timeline": timeline, "facts": facts, "confirmed": confirmed, "unconfirmed": unconfirmed,
                "contradictions": contradictions, "to_verify": to_verify}

    # ------------------------------------------------------------ MEDIA / REPORT / DRAFT
    def _t_media_relation(self, p: dict[str, Any]) -> dict[str, Any]:
        ev = _parse_iso(p.get("event_time"))
        items = []
        for m in p.get("media") or []:
            url = str(m.get("url") or "")
            cap = _parse_iso(m.get("captured_at"))
            note = str(m.get("note") or "")
            old = bool(ev and cap and cap < ev - timedelta(days=1)) or "обратный поиск" in note.lower()
            relation = md.SCENARIO_MEDIA_RELATION.get(url) or (
                f"{m.get('origin') or 'Источник'}: {first_sentence(str(m.get('context') or ''), 110)}"
            )
            reason = note or ("Дата съёмки заметно раньше события" if old else "Время и место совпадают с событием по данным источника")
            items.append({"key": m.get("key"), "relation": relation, "possibly_old": old, "reason": reason})
        return {"items": items}

    def _t_report(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"summary": str(p.get("summary") or first_sentence(str(p.get("lead_text") or ""), 300))}

    def _t_draft(self, p: dict[str, Any]) -> dict[str, Any]:
        from app.ai.drafting import compose_draft

        facts = [{"text": f.get("claim") or f.get("text"), "source_ids": f.get("source_ids") or [],
                  "status": f.get("status") or f.get("state") or "unconfirmed"} for f in p.get("facts") or []]
        hashtags = p.get("hashtags") or None
        return compose_draft(str(p.get("headline") or p.get("title") or "Новость"), facts, p.get("dna") or None,
                             topic=p.get("topic"), hashtags=hashtags)


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
