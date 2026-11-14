"""Мелкие текстовые утилиты DEV-AI (общие для MockLLM, mock-поиска и пайплайна).

Грубый «стемминг» для русского: первые 4 буквы слова (слова от 3 букв, без стоп-слов).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from app.services.uniqueness import normalize_text

MSK_OFFSET = timedelta(hours=3)

STOPWORDS: frozenset[str] = frozenset(
    """а и в во на не но что как это все всё там уже они был была были где кто нас вас вот тут еще ещё
    очень сейчас было будет есть или для при про под над без так же ли бы то от до из за по со об
    его ее её их мне меня нам вам вы мы ты он она оно это эти этот эта тот та те кто-то кто-нибудь
    чем чего когда тоже только просто вроде говорят пишут знает знаете кто-нибудь всем сегодня
    около после потом сразу уже вообще ну да нет ага ой может можно нужно надо тогда если чтобы
    какой какая какие который которая которые свой свою своя наш наша наши ваш ваша этого этом
    этой этих тех том той тем весь вся всех всем раз два три один одна or and the""".split()
)


def words(text: str) -> list[str]:
    return [w for w in normalize_text(text).split() if len(w) >= 3 and w not in STOPWORDS and not w.isdigit()]


STEM_LEN = 4


def stem(word: str) -> str:
    return word[:STEM_LEN]


def stems(text: str) -> set[str]:
    return {stem(w) for w in words(text)}


def to_msk(dt: datetime | None) -> datetime | None:
    return dt + MSK_OFFSET if dt else None


def hhmm_msk(dt: datetime | None) -> str | None:
    return (dt + MSK_OFFSET).strftime("%H:%M") if dt else None


_ABBR = frozenset({"ул", "д", "г", "кв", "м", "им", "пр", "ш", "корп", "стр", "просп", "пер", "т", "н", "э", "т.е", "т.д", "р", "руб", "мин", "ч"})
_SENT_END_RE = re.compile(r"[.!?…](?=\s+[А-ЯЁA-Z«\"(—–-]|\s*$)")


def first_sentence(text: str, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    s = t
    for m in _SENT_END_RE.finditer(t):
        if m.start() < 20:
            continue
        prev = re.search(r"([\w.]+)$", t[: m.start()])
        if t[m.start()] == "." and prev and prev.group(1).lower() in _ABBR:
            continue
        s = t[: m.start() + 1]
        break
    if len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0] + "…"
    return s


def extract_json(text: str) -> Any:
    """Устойчиво достать JSON из ответа LLM: ```json блок / весь текст / первый {...} или [...]."""
    if text is None:
        raise ValueError("empty")
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.DOTALL | re.IGNORECASE)
    candidates = [m.group(1).strip()] if m else []
    candidates.append(t)
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, TypeError):
            pass
    # первый сбалансированный объект/массив
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = t.find(open_ch)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(t)):
                ch = t[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[start : i + 1])
                        except json.JSONDecodeError:
                            break
            start = t.find(open_ch, start + 1)
    raise ValueError("no JSON found in LLM response")
