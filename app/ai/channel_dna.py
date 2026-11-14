"""CHANNEL DNA (зона DEV-AI): профиль стиля канала по истории постов. Формат — CONTRACTS.md: dna_json."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, utcnow

# Эмодзи: основные блоки + символы, которые часто идут с вариационным селектором (⚡️ ❗️ ✅ ❓)
_EMOJI_RE = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2}"
    "|[\U0001F300-\U0001FAFF☀-➿⬀-⯿‼⁉™ℹ↔-↪"
    "⌚-⏿Ⓜ▪-◾〰〽㊗㊙])️?"
)
_HASHTAG_RE = re.compile(r"#[\w]+", re.UNICODE)
_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
_CAPS_WORD_RE = re.compile(r"\b[А-ЯЁA-Z]{4,}\b")

URGENT_MARKERS = ("срочно", "только что", "прямо сейчас", "молния", "⚡", "❗", "🔥", "🚨")
ANALYTIC_MARKERS = ("известно", "неизвестно", "источник", "проверк", "слух", "не подтвердил", "факт", "✅", "❓")


def _emojis(text: str) -> list[str]:
    return _EMOJI_RE.findall(text or "")


def _headline(post: str) -> str:
    for line in (post or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _paragraphs(post: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", (post or "").strip()) if p.strip()]


def _is_caps_headline(h: str) -> bool:
    letters = _LETTER_RE.findall(h)
    if not letters:
        return False
    upper_share = sum(1 for ch in letters if ch.isupper()) / len(letters)
    if upper_share >= 0.6:
        return True
    # «⚡️СРОЧНО: …» — заголовок открывается словом капсом
    stripped = _EMOJI_RE.sub("", h).strip()
    first = stripped.split(" ", 1)[0].strip(":!.,") if stripped else ""
    return bool(_CAPS_WORD_RE.fullmatch(first or "-"))


def _starts_with_emoji(h: str) -> bool:
    m = _EMOJI_RE.match(h.strip())
    return bool(m)


def analyze_posts(posts: list[str]) -> dict[str, Any]:
    """Чистая функция без LLM: длина, эмодзи, структура заголовков, хэштеги → dna_json."""
    posts = [p for p in (posts or []) if p and p.strip()]
    n = len(posts)
    if n == 0:
        return {
            "posts_analyzed": 0, "avg_length": 0, "avg_paragraphs": 0.0, "emoji_per_post": 0.0,
            "top_emojis": [], "headline": {"style": "plain", "caps_share": 0.0, "emoji_prefix_share": 0.0, "avg_words": 0.0},
            "uses_hashtags": False, "top_hashtags": [], "structure": ["headline", "lead", "details"],
            "tone": "нейтральный", "rules": ["Нейтральный новостной стиль: заголовок, лид, детали"],
        }

    emoji_counter: Counter[str] = Counter()
    tag_counter: Counter[str] = Counter()
    total_len = total_par = total_emoji = 0
    caps = emoji_prefix = questions = posts_with_tags = 0
    head_words = 0
    urgent_hits = analytic_hits = 0
    known_unknown = 0

    for p in posts:
        total_len += len(p)
        total_par += max(1, len(_paragraphs(p)) - (1 if _paragraphs(p) and _paragraphs(p)[0].strip() == _headline(p) else 0))
        em = _emojis(p)
        total_emoji += len(em)
        emoji_counter.update(e if e.endswith("️") or len(e) > 1 else e for e in em)
        tags = [t.lower() for t in _HASHTAG_RE.findall(p)]
        if tags:
            posts_with_tags += 1
            tag_counter.update(tags)
        h = _headline(p)
        head_words += len(_EMOJI_RE.sub("", h).split())
        caps += _is_caps_headline(h)
        emoji_prefix += _starts_with_emoji(h)
        questions += h.rstrip().endswith("?")
        low = p.lower()
        urgent_hits += any(m in low for m in URGENT_MARKERS)
        analytic_hits += any(m in low for m in ANALYTIC_MARKERS)
        known_unknown += ("✅" in p and "❓" in p) or ("известно" in low and "неизвестно" in low)

    caps_share = round(caps / n, 2)
    emoji_prefix_share = round(emoji_prefix / n, 2)
    if caps_share >= 0.5 and emoji_prefix_share >= 0.5:
        style = "emoji_caps"
    elif caps_share >= 0.5:
        style = "caps"
    elif questions / n >= 0.5:
        style = "question"
    else:
        style = "plain"

    uses_hashtags = posts_with_tags / n >= 0.3
    if analytic_hits / n >= 0.5:
        tone = "аналитический"
    elif urgent_hits / n >= 0.5 or style in ("caps", "emoji_caps"):
        tone = "срочный"
    else:
        tone = "нейтральный"

    if known_unknown / n >= 0.3:
        structure = ["headline", "known", "unknown", "source"]
    else:
        structure = ["headline", "lead", "details"]
    if uses_hashtags:
        structure.append("hashtags")

    avg_len = round(total_len / n)
    avg_par = round(total_par / n, 1)
    emoji_per_post = round(total_emoji / n, 1)
    top_emojis = [e for e, _ in emoji_counter.most_common(5)]
    top_hashtags = [t for t, _ in tag_counter.most_common(5)]

    rules: list[str] = []
    if style == "emoji_caps":
        rules.append(f"Заголовок капсом (или с капс-словом), начинается с эмодзи {top_emojis[0] if top_emojis else '⚡️'}")
    elif style == "caps":
        rules.append("Заголовок капсом")
    elif style == "question":
        rules.append("Заголовок — вопрос")
    else:
        rules.append("Заголовок обычным регистром, без эмодзи")
    if "known" in structure:
        rules.append("Структура «✅ Известно / ❓ Не подтверждено», в конце — источник")
    else:
        rules.append(f"{max(1, round(avg_par))}–{max(2, round(avg_par) + 1)} коротких абзаца")
    rules.append(f"Объём около {avg_len} знаков")
    if uses_hashtags:
        rules.append(f"В конце 1–2 хэштега ({' '.join(top_hashtags[:2])})")
    if emoji_per_post >= 1 and style != "emoji_caps":
        rules.append("Умеренно эмодзи-маркеры")
    if tone == "нейтральный":
        rules.append("Спокойный тон, можно цитаты жителей")
    elif tone == "аналитический":
        rules.append("Разделять факты и слухи, указывать источник")
    else:
        rules.append("Срочный тон, коротко, главное — в первой фразе")

    return {
        "posts_analyzed": n,
        "avg_length": avg_len,
        "avg_paragraphs": avg_par,
        "emoji_per_post": emoji_per_post,
        "top_emojis": top_emojis,
        "headline": {
            "style": style,
            "caps_share": caps_share,
            "emoji_prefix_share": emoji_prefix_share,
            "avg_words": round(head_words / n, 1),
        },
        "uses_hashtags": uses_hashtags,
        "top_hashtags": top_hashtags,
        "structure": structure,
        "tone": tone,
        "rules": rules,
    }


async def ensure_channel_dna(session: AsyncSession, channel: Channel, *, force: bool = False) -> dict[str, Any]:
    """Посчитать и сохранить channel.dna_json (+dna_updated_at), если пусто или force. Без commit."""
    if channel.dna_json and not force:
        return channel.dna_json
    dna = analyze_posts(list(channel.sample_posts or []))
    channel.dna_json = dict(dna)  # новый объект
    channel.dna_updated_at = utcnow()
    await session.flush()
    return dna
