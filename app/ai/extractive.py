"""Экстрактивный «LLM» для DATA_MODE=live без ANTHROPIC_API_KEY (зона DEV-REALDATA).

Тот же интерфейс, что у ``LLMClient`` (``complete_json(..., task=...)``), но НИЧЕГО не сочиняет:
каждый пункт справки и черновика — цитата/пересказ заголовка реального найденного источника с его
``source_id``, временем публикации и названием издания. Синтетики сценария «Чертаново» нет.

Задачи:
- ``extract_entities`` — эвристики по тексту зацепки (тип события, время, место, ключевые слова);
- ``chain_hypotheses`` — запросы: 1) сама зацепка + ключевые слова; 2) уточнение словами, которые
  чаще всего встречаются в НАЙДЕННЫХ материалах (не в зацепке);
- ``relevance`` / ``importance`` — пересечение «стемов» / эвристика ``uniqueness``;
- ``intelligence`` — хронология по датам публикаций, цитаты первоисточников; «подтверждено» —
  только официальные источники (kind=official); противоречия — только явные (расходятся числа
  погибших/пострадавших, «взрыв» vs «пожар», «есть пострадавшие» vs «пострадавших нет»), иначе пусто
  с пояснением «требуется LLM» (``notes``);
- ``media_relation`` — подпись из контекста, «возможно старое» — только по датам;
- ``draft`` — черновик из цитат с атрибуцией (claims = подстроки body, со source_ids).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.ai.llm import LLMClient, _parse_iso, _payload
from app.ai.text import first_sentence, stem, stems, words

NO_LLM_NOTE = "Анализ экстрактивный (без LLM): только цитаты найденных источников, без обобщений."
CONTRADICTIONS_NOTE = ("Явных расхождений не найдено (проверялись числа погибших/пострадавших и версии "
                       "«взрыв»/«пожар»). Смысловой поиск противоречий требует LLM (ANTHROPIC_API_KEY).")

_GENERIC_WORDS = frozenset("""взял взяли сообщил сообщила сообщили заявил заявила заявили рассказал рассказала
рассказали стало стал стала стали стали появились появилось произошел произошло произошла назвал назвали
новости новость москве москва москвы столице столица столицы днем вечером утром ночью году года городе
время также более менее несколько данным данные информации информация поэтому пока""".split())

_EVENT_TYPES: list[tuple[tuple[str, ...], str]] = [
    (("взрыв", "хлопо", "бахну", "грохо"), "хлопок/взрыв"),
    (("пожар", "горит", "горел", "возго", "задым", "подж"), "пожар/возгорание"),
    (("дтп", "авари", "столк", "сбил", "наезд", "наеха"), "ДТП/авария"),
    (("прорв", "затоп", "кипят", "залил", "подтоп"), "коммунальная авария"),
    (("отклю", "электр", "отопл"), "отключение коммунальных услуг"),
    (("драка", "напад", "стрел", "нож", "грабе", "краж", "задерж", "убий"), "происшествие (криминал)"),
    (("бпла", "беспи", "дрон", "атак"), "атака БПЛА"),
    (("обруш", "рухну"), "обрушение"),
]
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_CAP_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([А-ЯЁ][а-яё]{3,}(?:[- ][А-ЯЁ][а-яё]+)?)")
_LOC_RE = re.compile(r"\b(?:в|во|на|у|возле|около|рядом с|улиц[еаы]|районе)\s+((?:[А-ЯЁ][\w-]+)(?:\s+(?:[А-ЯЁ][\w-]+|улиц\w+|шоссе|проспект\w*|переулк\w*))?)")
_NUM_CAT_RE = re.compile(
    r"(?P<n1>\d{1,4})\s+(?:человек\w*\s+)?(?P<c1>погиб|пострада|госпитализ|ранен|эвакуир)\w*"
    r"|(?P<c2>погиб|пострада|госпитализ|ранен|эвакуир)\w*\s+(?:не менее\s+|более\s+|уже\s+)?(?P<n2>\d{1,4})",
    re.I,
)
_CAT_LABEL = {"погиб": "погибших", "пострада": "пострадавших", "госпитализ": "госпитализированных",
              "ранен": "раненых", "эвакуир": "эвакуированных"}
MSK = timedelta(hours=3)


def _when(dt: datetime | None) -> str | None:
    # datetime.max/min — заглушки сортировки для источников без даты: времени нет.
    if not dt or dt.year in (1, 9999):
        return None
    try:
        return (dt + MSK).strftime("%d.%m %H:%M")
    except OverflowError:
        return None


def _quote(text: str, limit: int = 170) -> str:
    s = first_sentence(re.sub(r"\s+", " ", text or "").strip(), limit).strip().rstrip(".")
    return s.replace("«", "\"").replace("»", "\"")


def _outlet(s: dict[str, Any]) -> str:
    return str(s.get("outlet") or s.get("chat_title") or s.get("platform_label") or "источник")


def _keywords(lead: str, limit: int = 6) -> list[str]:
    """Ключевые слова зацепки по весу: имена собственные/топонимы, слова события, длинные слова."""
    ws = words(lead)
    caps = {m.group(1).lower() for m in _CAP_RE.finditer(lead)}
    ev = {k for keys, _ in _EVENT_TYPES for k in keys}
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for i, w in enumerate(ws):
        st = stem(w)
        if w in _GENERIC_WORDS or st in seen:
            continue
        seen.add(st)
        score = (3 if w in caps else 0) + (2 if any(w.startswith(e) for e in ev) else 0) + (1 if len(w) >= 6 else 0)
        scored.append((-score, i, w))
    scored.sort()
    top = sorted(scored[:limit], key=lambda t: t[1])
    return [w for _, _, w in top]


def lead_vocab(lead: str, keywords: list[str] | None = None, extra: list[str] | None = None) -> set[str]:
    """Значимые «стемы» зацепки (без служебных/общих слов) + ключевые слова + вопросы редактора."""
    out = {stem(w) for w in words(lead) if w not in _GENERIC_WORDS}
    for k in (keywords or []) + (extra or []):
        out |= {stem(w) for w in words(str(k)) if w not in _GENERIC_WORDS}
    return out


MOSCOW_ALIASES = frozenset({"моск", "столиц", "мкад", "цао", "сао", "свао", "вао", "ювао", "юао", "юзао", "зао",
                            "сзао", "зелао", "тинао", "нао", "мцд", "метро", "подмосков"})


def location_vocab(lead: str) -> set[str]:
    """«Стемы» топонимов зацепки: слова с заглавной буквы не в начале предложения (Москвы, Чертанове,
    Полбина). Если среди них Москва — подходят и её синонимы/округа (ЮВАО, МКАД, столица…)."""
    out = {stem(m.group(1).lower()) for m in _CAP_RE.finditer(lead)} - {stem(w) for w in _GENERIC_WORDS if len(w) > 3}
    out |= {stem(m.group(1).lower()) for m in _CAP_RE.finditer(lead) if m.group(1).lower().startswith("моск")}
    if "моск" in out:
        out |= MOSCOW_ALIASES
    return out


def mentions_location(text: str, loc: set[str]) -> bool:
    if not loc:
        return True
    toks = set(re.findall(r"[a-zа-я0-9]+", (text or "").lower().replace("ё", "е")))
    if stems(text) & loc or toks & loc:
        return True
    return any(t.startswith(a) for t in toks for a in loc if len(a) >= 5)


def relevance_need(vocab: set[str]) -> int:
    """Сколько общих «стемов» с зацепкой нужно, чтобы материал считался относящимся к ней:
    40% словаря зацепки, не меньше 2 и не больше 4."""
    return max(2, min(4, round(len(vocab) * 0.4)))


def is_relevant(text: str, vocab: set[str], need: int | None = None) -> bool:
    if not vocab:
        return True
    need = need or relevance_need(vocab)
    return len(stems(text) & vocab) >= min(need, len(vocab))


class ExtractiveLLM(LLMClient):
    name = "extractive"

    async def complete_json(self, system, user, schema_hint=None, *, task=None,
                            temperature=0.2, max_tokens=None) -> dict[str, Any]:
        p = _payload(user)
        handler = getattr(self, f"_t_{task}", None) if task else None
        if handler is None:
            return {"ok": True, "task": task, "summary": first_sentence(str(p.get("lead_text") or ""))}
        return handler(p)

    async def complete_text(self, system, user, *, task=None, temperature=0.5, max_tokens=None) -> str:
        p = _payload(user)
        return first_sentence(str(p.get("summary") or p.get("lead_text") or ""), 400)

    # ------------------------------------------------------------ INPUT
    def _t_extract_entities(self, p: dict[str, Any]) -> dict[str, Any]:
        lead = str(p.get("lead_text") or "")
        ls = stems(lead)
        event_type = None
        for keys, label in _EVENT_TYPES:
            if any(any(s.startswith(k[:4]) for s in ls) for k in keys):
                event_type = label
                break
        time_text, iso = None, None
        m = _TIME_RE.search(lead)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            time_text = f"около {hh:02d}:{mm:02d}"
            now = _parse_iso(p.get("now")) or datetime.utcnow()
            msk_now = now + MSK
            ev = msk_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if ev > msk_now + timedelta(minutes=30):
                ev -= timedelta(days=1)
            iso = (ev - MSK).isoformat()
        lm = _LOC_RE.search(lead)
        return {
            "summary": first_sentence(lead, 200),
            "event_type": event_type,
            "object": None,
            "location": {"text": lm.group(1) if lm else None, "district": None, "address": None},
            "time": {"text": time_text, "iso": iso},
            "keywords": _keywords(lead),
            "status": "unconfirmed",
        }

    # ------------------------------------------------------------ CHAIN
    def _t_chain_hypotheses(self, p: dict[str, Any]) -> dict[str, Any]:
        lead = str(p.get("lead_text") or "")
        n = int(p.get("n") or 1)
        max_it = int(p.get("max_iterations") or 3)
        ent = p.get("entities") or {}
        kws = [str(k) for k in (ent.get("keywords") or _keywords(lead))]
        base = " ".join(kws[:4]) or lead[:80]
        if n > max_it:
            return {"continue": False, "rationale": "Достигнут лимит итераций.", "queries": []}
        if n == 1:
            headline = first_sentence(lead, 120).rstrip(".!?…")
            short = " ".join(kws[:3]) or base
            queries = [{"agent": "web", "query": headline}]
            if base.lower() != headline.lower():
                queries.append({"agent": "web", "query": base})
            queries += [
                {"agent": "max_chats", "query": short},
                {"agent": "official", "query": short},
                {"agent": "media", "query": base},
            ]
            return {"continue": True, "queries": queries,
                    "rationale": "Ищем публикации по формулировке зацепки и её ключевым словам: СМИ (Google News, "
                                 "RSS-ленты), Telegram/MAX/RSS-источники в ленте, официальные сайты, медиа."}
        findings = [str(f) for f in (p.get("findings") or []) if f]
        if n == 2:
            have = {stem(k) for k in kws} | stems(lead)
            cnt: Counter[str] = Counter()
            forms: dict[str, str] = {}
            for f in findings[:40]:
                for w in set(words(f)):
                    st = stem(w)
                    if st in have or w in _GENERIC_WORDS or len(w) < 5:
                        continue
                    cnt[st] += 1
                    forms.setdefault(st, w)
            new_terms = [forms[st] for st, c in cnt.most_common(4) if c >= 2][:2]
            if not findings:
                core = " ".join(kws[:2]) or base
                return {"continue": True, "queries": [{"agent": "web", "query": core},
                                                      {"agent": "max_chats", "query": core}],
                        "rationale": "По полной формулировке ничего не найдено — расширяем запрос до главных слов."}
            if not new_terms:
                return {"continue": False, "queries": [],
                        "rationale": "Новых повторяющихся деталей в найденных материалах нет — цепочка остановлена."}
            core = " ".join(kws[:2])
            q = f"{core} {' '.join(new_terms)}".strip()
            return {"continue": True, "queries": [
                {"agent": "web", "query": q}, {"agent": "official", "query": q},
                {"agent": "max_chats", "query": f"{kws[0] if kws else ''} {new_terms[0]}".strip()},
            ], "rationale": f"В найденных материалах повторяются детали: {', '.join(new_terms)} — проверяем их."}
        return {"continue": False, "queries": [],
                "rationale": "Две итерации выполнены; для гипотез глубже нужен LLM (ANTHROPIC_API_KEY)."}

    # ------------------------------------------------------------ RELEVANCE / IMPORTANCE
    def _t_relevance(self, p: dict[str, Any]) -> dict[str, Any]:
        """Относится ли сообщение к ЗАЦЕПКЕ (не к промежуточному запросу): ≥ relevance_need общих стемов."""
        vocab = lead_vocab(str(p.get("lead_text") or ""), [str(k) for k in p.get("keywords") or []])
        need = relevance_need(vocab)
        scores = {}
        for m in p.get("messages") or []:
            hit = len(stems(str(m.get("text") or "")) & vocab)
            scores[str(m.get("id"))] = 0.9 if hit >= min(need, len(vocab) or 1) else round(0.4 * hit / need, 2)
        return {"scores": scores}

    def _t_importance(self, p: dict[str, Any]) -> dict[str, Any]:
        from app.services.uniqueness import is_important_heuristic

        return {"important": {str(i.get("id")): is_important_heuristic(str(i.get("text") or ""), bool(i.get("has_media")))
                              for i in p.get("items") or p.get("messages") or []}}

    # ------------------------------------------------------------ INTELLIGENCE
    def _t_intelligence(self, p: dict[str, Any]) -> dict[str, Any]:
        lead = str(p.get("lead_text") or "")
        ent = p.get("entities") or {}
        sources = [s for s in p.get("sources") or [] if s.get("id") is not None]
        lead_st = stems(lead) | {stem(str(k)) for k in ent.get("keywords") or []}

        def t(s: dict[str, Any]) -> datetime:
            return _parse_iso(s.get("published_at")) or datetime.max

        def rel(s: dict[str, Any]) -> int:
            return len(stems(f"{s.get('title') or ''} {s.get('text') or ''}") & lead_st)

        prim = [s for s in sources if s.get("is_primary", True)]
        prim.sort(key=t)
        official = [s for s in prim if s.get("kind") == "official"]
        others = [s for s in prim if s.get("kind") != "official"]
        best = sorted(prim, key=lambda s: (-rel(s), t(s)))
        cluster_outlets: dict[Any, set[str]] = {}
        for s in sources:
            cluster_outlets.setdefault(s.get("cluster_id"), set()).add(_outlet(s))

        def say(s: dict[str, Any]) -> str:
            return _quote(s.get("title") if s.get("kind") in ("web", "official", "media") and s.get("title")
                          else s.get("text") or s.get("title") or "")

        head_src = best[0] if best else None
        title = say(head_src) if head_src else first_sentence(lead, 120).rstrip(".")
        n_all, n_prim = len(sources), len(prim)
        parts = [f"Найдено источников: {n_all}, первоисточников: {n_prim} (перепечатки и пересылки свёрнуты)."]
        if prim:
            first = prim[0]
            parts.append(f"Самая ранняя публикация — {_outlet(first)}, {_when(t(first)) if t(first) != datetime.max else 'время неизвестно'}: «{say(first)}».")
        if official:
            o = official[0]
            parts.append(f"Официальный источник — {_outlet(o)}: «{say(o)}».")
        else:
            parts.append("Официальных публикаций по теме не найдено.")
        if len(prim) > 1:
            last = prim[-1]
            if last is not prim[0]:
                parts.append(f"Последнее по времени — {_outlet(last)}, {_when(t(last)) if t(last) != datetime.max else ''}: «{say(last)}».")
        if not prim:
            parts = ["По зацепке не найдено ни одного источника — справка пустая, сведения не подтверждены."]
        parts.append(NO_LLM_NOTE)
        summary = " ".join(parts)

        timeline = [{"time": _when(t(s)) if t(s) != datetime.max else None,
                     "text": f"{_outlet(s)}: «{say(s)}»", "source_ids": [s["id"]],
                     "state": "confirmed" if s.get("kind") == "official" else "unconfirmed"}
                    for s in prim[:10]]
        facts = []
        for s in best[:8]:
            n_out = len(cluster_outlets.get(s.get("cluster_id"), set()))
            extra = f" (то же сообщают ещё {n_out - 1} изд./канал.)" if n_out > 1 else ""
            facts.append({"text": f"{_outlet(s)}, {_when(t(s)) or 'время неизвестно'}: «{say(s)}»{extra}",
                          "source_ids": [s["id"]], "state": "confirmed" if s.get("kind") == "official" else None})
        confirmed = [{"text": f"«{say(s)}» — {_outlet(s)}", "claim": f"{_outlet(s)} сообщает: «{say(s)}»",
                      "source_ids": [s["id"]], "state": "confirmed"} for s in official[:4]]
        unconfirmed = []
        for s in sorted(others, key=lambda s: (-rel(s), t(s)))[:5]:
            kind = "Сообщение в канале/чате" if s.get("kind") == "max_chat" else "Публикация"
            unconfirmed.append({"text": f"{kind}: «{say(s)}» — {_outlet(s)}",
                                "claim": f"По данным {_outlet(s)}: «{say(s)}»",
                                "source_ids": [s["id"]], "state": "unconfirmed"})
        contradictions = self._contradictions(prim)
        to_verify: list[dict[str, Any]] = []
        if not official:
            to_verify.append({"text": "Официального подтверждения не найдено — запросить комментарий профильного ведомства",
                              "source_ids": []})
        if len(prim) == 1:
            to_verify.append({"text": "Сведения из одного первоисточника — найти независимое подтверждение",
                              "source_ids": [prim[0]["id"]]})
        if prim:
            to_verify.append({"text": f"Проверить первоисточник: самая ранняя публикация — {_outlet(prim[0])}",
                              "source_ids": [prim[0]["id"]]})
        if any(s.get("has_media") for s in prim):
            to_verify.append({"text": "Проверить фото/видео: известна только дата публикации, не дата съёмки",
                              "source_ids": [s["id"] for s in prim if s.get("has_media")][:4]})
        notes = {"summary": NO_LLM_NOTE}
        if not contradictions:
            notes["contradictions"] = CONTRADICTIONS_NOTE
        return {"title": title, "headline": title, "topic": None, "summary": summary,
                "timeline": timeline, "facts": facts, "confirmed": confirmed, "unconfirmed": unconfirmed,
                "contradictions": contradictions, "to_verify": to_verify, "notes": notes}

    @staticmethod
    def _contradictions(prim: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        nums: dict[str, dict[int, list[int]]] = {}
        for s in prim:
            text = f"{s.get('title') or ''}. {s.get('text') or ''}"
            for m in _NUM_CAT_RE.finditer(text):
                cat = (m.group("c1") or m.group("c2") or "").lower()
                num = int(m.group("n1") or m.group("n2"))
                nums.setdefault(cat, {}).setdefault(num, [])
                if s["id"] not in nums[cat][num]:
                    nums[cat][num].append(s["id"])
        for cat, by_num in nums.items():
            if len(by_num) > 1:
                variants = "; ".join(f"{n} (ист. {', '.join(map(str, ids[:3]))})" for n, ids in sorted(by_num.items()))
                out.append({"text": f"Расходятся данные о числе {_CAT_LABEL.get(cat, cat)}: {variants} — "
                                    f"возможно, данные обновлялись; сверить по времени публикации",
                            "source_ids": sorted({i for ids in by_num.values() for i in ids})[:6]})
        groups = [("взрыв", ("взрыв", "взорв")), ("пожар", ("пожар", "возгоран")),
                  ("есть пострадавшие", ("пострадал", "госпитализ", "погиб")),
                  ("пострадавших нет", ("пострадавших нет", "без пострадавших", "никто не пострадал", "обошлось без"))]
        hits = {g: [s["id"] for s in prim if any(k in f"{s.get('title') or ''} {s.get('text') or ''}".lower() for k in keys)]
                for g, keys in groups}
        hits["есть пострадавшие"] = [i for i in hits["есть пострадавшие"] if i not in hits["пострадавших нет"]]
        only_blast = set(hits["взрыв"]) - set(hits["пожар"])
        only_fire = set(hits["пожар"]) - set(hits["взрыв"])
        if only_blast and only_fire:
            out.append({"text": "Версии расходятся: одни источники пишут о взрыве, другие — только о пожаре",
                        "source_ids": sorted(only_blast | only_fire)[:6]})
        if hits["есть пострадавшие"] and hits["пострадавших нет"]:
            out.append({"text": "Есть ли пострадавшие: источники противоречат друг другу",
                        "source_ids": sorted(set(hits["есть пострадавшие"] + hits["пострадавших нет"]))[:6]})
        return out

    # ------------------------------------------------------------ MEDIA / REPORT / DRAFT
    def _t_media_relation(self, p: dict[str, Any]) -> dict[str, Any]:
        ev = _parse_iso(p.get("event_time"))
        items = []
        for m in p.get("media") or []:
            pub = _parse_iso(m.get("captured_at")) or _parse_iso(m.get("published_at"))
            old = bool(ev and pub and pub < ev - timedelta(days=1))
            ctx = _quote(str(m.get("context") or ""), 110)
            items.append({"key": m.get("key"),
                          "relation": f"иллюстрация к публикации «{ctx}»" if ctx else "иллюстрация к публикации",
                          "possibly_old": old,
                          "reason": "Опубликовано более чем за сутки до события" if old else
                                    "Дата съёмки неизвестна — известна только дата публикации"})
        return {"items": items}

    def _t_report(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"summary": str(p.get("summary") or first_sentence(str(p.get("lead_text") or ""), 300))}

    def _t_draft(self, p: dict[str, Any]) -> dict[str, Any]:
        return compose_extractive_draft(str(p.get("headline") or p.get("title") or "Новость"), p.get("facts") or [])


def compose_extractive_draft(headline: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Черновик только из цитат с атрибуцией. ``claims[].text`` — точная подстрока ``body``."""
    claims: list[dict[str, Any]] = []

    def add(f: dict[str, Any]) -> str:
        text = str(f.get("claim") or f.get("text") or "").strip().rstrip(".")
        claims.append({"id": f"c{len(claims) + 1}", "text": text, "source_ids": list(f.get("source_ids") or []),
                       "status": f.get("status") or f.get("state") or "unconfirmed", "note": None})
        return text

    conf = [f for f in facts if (f.get("status") or f.get("state")) == "confirmed" and f.get("source_ids")][:3]
    unconf = [f for f in facts if (f.get("status") or f.get("state")) != "confirmed" and f.get("source_ids")][:4]
    parts: list[str] = []
    if conf:
        parts.append(" ".join(f"{add(f)}." for f in conf))
    if unconf:
        parts.append(("Неподтверждённые сведения: " if conf else "Пока не подтверждено официально. ")
                     + " ".join(f"{add(f)}." for f in unconf))
    if not parts:
        parts.append("Подтверждённых сведений и публикаций по теме не найдено.")
    parts.append("Черновик собран автоматически из цитат источников (без LLM) — требуется редактура и проверка.")
    return {"title": headline, "body": "\n\n".join(parts), "claims": claims}
