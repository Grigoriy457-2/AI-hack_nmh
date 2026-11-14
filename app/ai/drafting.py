"""Сборка черновика поста по Channel DNA (зона DEV-AI). Чистые функции.

Используется MockLLM (task="draft") и как fallback, если ответ реального LLM не прошёл валидацию.
Гарантия: ``claim["text"]`` — точная подстрока ``body``.
"""
from __future__ import annotations

from typing import Any

DEFAULT_DNA: dict[str, Any] = {
    "avg_length": 400,
    "top_emojis": [],
    "headline": {"style": "plain"},
    "uses_hashtags": False,
    "top_hashtags": [],
    "structure": ["headline", "lead", "details"],
    "tone": "нейтральный",
}


def _sentence(text: str) -> str:
    t = text.strip()
    return t if t.endswith((".", "!", "?", "…")) else t + "."


def _style(dna: dict[str, Any]) -> str:
    structure = dna.get("structure") or []
    if "known" in structure or dna.get("tone") == "аналитический":
        return "analytic"
    hs = (dna.get("headline") or {}).get("style")
    if hs in ("caps", "emoji_caps") or dna.get("tone") == "срочный":
        return "urgent"
    return "neutral"


def compose_draft(
    headline: str,
    facts: list[dict[str, Any]],
    dna: dict[str, Any] | None = None,
    *,
    topic: str | None = None,
    hashtags: list[str] | None = None,
) -> dict[str, Any]:
    """facts: [{text (формулировка для поста), source_ids, status: confirmed|unconfirmed}] — по важности.

    Возвращает {"title", "body", "claims": [{id, text, source_ids, status, note}]}.
    """
    dna = {**DEFAULT_DNA, **(dna or {})}
    style = _style(dna)
    confirmed = [f for f in facts if f.get("status") == "confirmed" and f.get("text")]
    unconfirmed = [f for f in facts if f.get("status") == "unconfirmed" and f.get("text")]
    short = (dna.get("avg_length") or 400) < 300
    confirmed = confirmed[: 4 if short else 6]
    unconfirmed = unconfirmed[: 1 if short else 2]

    parts: list[str] = []  # фрагменты body
    claims: list[dict[str, Any]] = []

    def add_claim(f: dict[str, Any]) -> str:
        text = f["text"].strip().rstrip(".")
        claims.append({
            "id": f"c{len(claims) + 1}",
            "text": text,
            "source_ids": list(f.get("source_ids") or []),
            "status": f.get("status") or "unconfirmed",
            "note": f.get("note"),
        })
        return text

    if style == "analytic":
        title = f"Что известно о {topic}" if topic else headline
        lines = ["✅ Известно:"] + [f"— {add_claim(f)}." for f in confirmed]
        body = "\n".join(lines)
        if unconfirmed:
            body += "\n\n❓ Не подтверждено:\n" + "\n".join(f"— {add_claim(f)}." for f in unconfirmed)
        body += "\n\nИсточники: официальные сообщения ведомств, жители домовых чатов. Информация уточняется."
    elif style == "urgent":
        emoji = (dna.get("top_emojis") or ["⚡️"])[0]
        title = f"{emoji}{headline.upper()}"
        first = [f"{add_claim(f)}." for f in confirmed[:2]]
        rest = [f"{add_claim(f)}." for f in confirmed[2:]]
        parts.append(" ".join(first))
        if rest:
            parts.append(" ".join(rest))
        if unconfirmed:
            parts.append("Не подтверждено: " + " ".join(f"{add_claim(f)}." for f in unconfirmed))
        parts.append("Подробности уточняются.")
        body = "\n\n".join(p for p in parts if p)
        tags = hashtags or dna.get("top_hashtags") or []
        if dna.get("uses_hashtags") and tags:
            body += "\n\n" + " ".join(tags[:2])
    else:
        title = headline
        parts.append(" ".join(f"{add_claim(f)}." for f in confirmed[:2]))
        if confirmed[2:]:
            parts.append(" ".join(f"{add_claim(f)}." for f in confirmed[2:]))
        if unconfirmed:
            parts.append("Пока не подтверждено: " + " ".join(f"{add_claim(f)}." for f in unconfirmed))
        parts.append("Редакция продолжает выяснять подробности.")
        body = "\n\n".join(p for p in parts if p)

    return {"title": title, "body": body, "claims": claims}


def validate_claims(body: str, claims: list[dict[str, Any]], valid_source_ids: set[int]) -> list[dict[str, Any]]:
    """Оставить только claims, чей text — подстрока body; source_ids — только существующие."""
    out: list[dict[str, Any]] = []
    for i, c in enumerate(claims or []):
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        if text not in body:
            stripped = text.rstrip(".!?…").strip()
            if stripped and stripped in body:
                text = stripped
            else:
                continue
        status = c.get("status") if c.get("status") in ("confirmed", "unconfirmed", "false") else "unconfirmed"
        ids = []
        for s in c.get("source_ids") or []:
            try:
                sid = int(s)
            except (TypeError, ValueError):
                continue
            if sid in valid_source_ids and sid not in ids:
                ids.append(sid)
        out.append({"id": str(c.get("id") or f"c{i + 1}"), "text": text, "source_ids": ids,
                    "status": status, "note": c.get("note")})
    # уникальные id
    seen: set[str] = set()
    for i, c in enumerate(out):
        if c["id"] in seen:
            c["id"] = f"c{i + 1}"
        seen.add(c["id"])
    return out
