"""Анализ свежих сообщений чатов-источников: бейджи ленты и «Возникающие сюжеты».

1. Окно — последние ``ANALYSIS_LIMIT`` неархивных сообщений (MAX + Telegram вместе).
2. Дубли — ``uniqueness.cluster_messages`` / ``score_uniqueness`` (кросс-платформенно).
3. Темы — объединение (DSU) кластеров дублей по общим «стемам» (≥ 2 общих, без слишком частых
   слов окна) и близости во времени (≤ ``TOPIC_GAP``).
4. Карточка — тема с ≥ EMERGING_MIN_MESSAGES сообщений в окне EMERGING_WINDOW_MIN минут.

Результат анализа кэшируется по (max id, число сообщений, число профилей с телефоном).

DATA_MODE=live (реальные TG-каналы и RSS-ленты СМИ — длинные тексты): темы строятся по «стемам»
заголовка/первых ``LIVE_TOPIC_CHARS`` символов, всегда нужно ≥ 3 общих стема, частые слова окна —
от ``LIVE_COMMON_DF_SHARE``, слияние «по чату/району» (для домовых чатов) не применяется.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import mock_data as md
from app.ai.text import first_sentence, stem
from app.ai.text import words as text_words
from app.config import settings
from app.models import (
    PLATFORM_LABELS,
    AuthorProfile,
    ChatMessage,
    DismissedEmerging,
    Investigation,
    InvestigationStatus,
    MaxChat,
    utcnow,
)
from app.schemas import EmergingStoryOut, FeedBadges, FeedMessage
from app.services import links
from app.services import uniqueness as uq

ANALYSIS_LIMIT = 400
STRONG_SHARED_STEMS = 3
MERGE_BEFORE = timedelta(minutes=5)  # слияние мелких тем с крупной: окно вокруг её сообщений
MERGE_AFTER = timedelta(minutes=15)
TOPIC_GAP = timedelta(minutes=120)
MIN_SHARED_STEMS = 2
COMMON_DF_SHARE = 0.25  # стем встречается в >25% сообщений окна — не тематический
RECENT = timedelta(minutes=10)
FADING_AFTER = timedelta(minutes=30)
TREND_LABELS = {"rising": "растёт", "steady": "стабильно", "fading": "затихает"}
LIVE_TOPIC_CHARS = 220
LIVE_COMMON_DF_SHARE = 0.08


@dataclass
class MsgInfo:
    msg: ChatMessage
    chat: MaxChat
    stems: set[str]
    uniq: uq.UniquenessResult | None = None
    dup_count: int = 0  # для первоисточника: копий в кластере
    has_phone: bool = False
    topic: int = 0
    emerging_key: str | None = None


@dataclass
class Analysis:
    infos: dict[int, MsgInfo] = field(default_factory=dict)
    topics: dict[int, list[int]] = field(default_factory=dict)  # topic -> [msg ids]
    common: set[str] = field(default_factory=set)
    computed_at: datetime | None = None


_cache: tuple[tuple, Analysis] | None = None


def _important(text: str, has_media: bool) -> bool:
    """«Важное» = новые факты о событии. Известные сообщения сценария: ключ ``important`` (нет ключа —
    не важно). Прочие: эвристика + обязательный признак события (очевидец, медиа, пострадавшие,
    экстренные службы) — бытовые объявления «в 19:00 у 3 подъезда» важными не считаются."""
    known = md.SCENARIO_MESSAGE_BY_TEXT.get((text or "").strip())
    if known is not None:
        return bool(known.get("important", False))
    sig = uq.importance_signals(text, has_media)
    if not (sig["witness"] or sig["media"] or sig["casualties"] or sig["services"]):
        return False
    return uq.is_important_heuristic(text, has_media)


def message_url(m: ChatMessage, chat: MaxChat) -> str | None:
    """Ссылка «открыть» (см. services.links): пост → чат/канал → None."""
    return links.resolve(m, chat)[0]


def link_kind(m: ChatMessage, chat: MaxChat) -> str:
    return links.resolve(m, chat)[1]


class _DSU:
    def __init__(self) -> None:
        self.p: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


async def analyze(session: AsyncSession, *, force: bool = False) -> Analysis:
    global _cache
    sig_row = (await session.execute(
        select(func.max(ChatMessage.id), func.count()).where(ChatMessage.archived.is_(False))
    )).one()
    phones = await session.scalar(select(func.count()).select_from(AuthorProfile).where(AuthorProfile.phone.is_not(None)))
    enabled = tuple((await session.scalars(select(MaxChat.id).where(MaxChat.enabled.is_(True)).order_by(MaxChat.id))).all())
    sig = (sig_row[0], sig_row[1], phones, enabled)
    if not force and _cache is not None and _cache[0] == sig:
        return _cache[1]

    rows = (await session.execute(
        select(ChatMessage, MaxChat).join(MaxChat, ChatMessage.chat_id == MaxChat.id)
        .where(ChatMessage.archived.is_(False), MaxChat.enabled.is_(True))
        .order_by(ChatMessage.id.desc()).limit(ANALYSIS_LIMIT)
    )).all()
    rows = list(reversed(rows))
    an = Analysis(computed_at=utcnow())
    if not rows:
        _cache = (sig, an)
        return an

    authors = {m.author_external_id for m, _ in rows if m.author_external_id}
    with_phone = set((await session.scalars(
        select(AuthorProfile.external_id).where(AuthorProfile.external_id.in_(authors), AuthorProfile.phone.is_not(None))
    )).all()) if authors else set()

    items = []
    importance: dict[int, bool] = {}
    for m, c in rows:
        an.infos[m.id] = MsgInfo(msg=m, chat=c, stems={stem(w) for w in text_words(m.text or "")},
                                 has_phone=m.author_external_id in with_phone)
        items.append(uq.UniqItem(id=m.id, text=m.text or "", sent_at=m.sent_at, is_forwarded=bool(m.is_forwarded),
                                 meta={"has_media": bool(m.has_media)}))
        importance[m.id] = _important(m.text or "", bool(m.has_media))
    clusters = uq.cluster_messages(items)
    results = uq.score_uniqueness(items, clusters, importance)
    for c in clusters:
        if c.primary.id in an.infos:
            an.infos[c.primary.id].dup_count = len(c.items) - 1
    for mid, r in results.items():
        an.infos[mid].uniq = r

    # --- темы: DSU по кластерам дублей + общие стемы
    dsu = _DSU()
    for c in clusters:
        for it in c.items:
            dsu.union(c.items[0].id, it.id)
    n = len(rows)
    live = not settings.is_demo
    if live:  # длинные посты/статьи: тема — по заголовку (началу текста)
        head_stems = {m.id: {stem(w) for w in text_words((m.text or "")[:LIVE_TOPIC_CHARS])} for m, _ in rows}
    else:
        head_stems = {mid: info.stems for mid, info in an.infos.items()}
    df = Counter(s for st in head_stems.values() for s in st)
    share = LIVE_COMMON_DF_SHARE if live else COMMON_DF_SHARE
    common = {s for s, k in df.items() if n >= 8 and k / n > share}
    ids = [m.id for m, _ in rows]
    topic_stems = {mid: head_stems[mid] - common for mid in ids}
    an.common = common
    for i, a in enumerate(ids):
        sa, ta = topic_stems[a], an.infos[a].msg.sent_at
        if len(sa) < MIN_SHARED_STEMS:
            continue
        imp_a = bool(an.infos[a].uniq and an.infos[a].uniq.is_important)
        for b in ids[i + 1:]:
            shared = len(sa & topic_stems[b])
            if shared < MIN_SHARED_STEMS or abs(an.infos[b].msg.sent_at - ta) > TOPIC_GAP:
                continue
            imp_b = bool(an.infos[b].uniq and an.infos[b].uniq.is_important)
            # 2 общих «стема» достаточно только для двух важных сообщений; иначе нужно ≥ 3
            if shared >= STRONG_SHARED_STEMS or (imp_a and imp_b and not live):
                dsu.union(a, b)
    topics: dict[int, list[int]] = {}
    for mid in ids:
        topics.setdefault(dsu.find(mid), []).append(mid)
    if not live:
        topics = _merge_nearby(an, topics)
    for root, members in topics.items():
        for mid in members:
            an.infos[mid].topic = root
    an.topics = topics
    for root, members in an.topics.items():
        if len(members) >= 2:
            first = min(members, key=lambda x: (an.infos[x].msg.sent_at, x))
            key = f"e{first}"
            for x in members:
                an.infos[x].emerging_key = key
    _cache = (sig, an)
    return an


def _top_stems(an: Analysis, members: list[int], limit: int = 12, *, with_common: bool = False) -> set[str]:
    cnt: Counter[str] = Counter()
    for x in members:
        cnt.update(an.infos[x].stems if with_common else an.infos[x].stems - an.common)
    return {st for st, k in cnt.most_common(limit) if k >= 2}


def _merge_nearby(an: Analysis, topics: dict[int, list[int]]) -> dict[int, list[int]]:
    """Слить мелкие темы с крупной по месту и времени: те же чаты/район, сообщения в окне
    [первое − 5 мин, последнее + 15 мин] крупной темы и хотя бы один общий частый «стем».
    Целиком — если вся мелкая тема в окне; иначе по одному важному сообщению."""
    min_n = max(2, int(settings.EMERGING_MIN_MESSAGES))
    order = sorted(topics, key=lambda r: -len(topics[r]))
    for target in order:
        if target not in topics or len(topics[target]) < min_n:
            continue
        tm = topics[target]
        first = min(an.infos[x].msg.sent_at for x in tm) - MERGE_BEFORE
        last = max(an.infos[x].msg.sent_at for x in tm) + MERGE_AFTER
        chats = {an.infos[x].chat.id for x in tm}
        districts = {an.infos[x].chat.district for x in tm if an.infos[x].chat.district}
        top = _top_stems(an, tm, 15, with_common=True)

        def near(x: int) -> bool:
            i = an.infos[x]
            return (first <= i.msg.sent_at <= last and bool(i.stems & top)
                    and (i.chat.id in chats or (i.chat.district in districts)))

        for other in list(topics):
            if other == target or other not in topics or len(topics[other]) >= len(tm):
                continue
            om = topics[other]
            if all(near(x) for x in om):
                tm.extend(om)
                del topics[other]
                continue
            moved = [x for x in om if near(x) and an.infos[x].uniq and an.infos[x].uniq.is_important]
            if moved:
                tm.extend(moved)
                rest = [x for x in om if x not in moved]
                del topics[other]
                if rest:
                    topics[min(rest)] = rest
        tm.sort()
    return topics


def _centrality(an: Analysis, info: MsgInfo, top: set[str]) -> tuple:
    r = info.uniq
    return (len((info.stems - an.common) & top) + (2 if r and r.is_important else 0)
            + (1 if r and r.is_primary else 0) + (1 if info.has_phone else 0), -info.msg.id)


def feed_message(info: MsgInfo, *, card_keys: set[str] | None = None) -> FeedMessage:
    m, c, r = info.msg, info.chat, info.uniq
    platform = c.platform or "max"
    is_dup = bool(r and not r.is_primary)
    return FeedMessage(
        id=m.id, platform=platform, platform_label=PLATFORM_LABELS.get(platform, platform),
        chat_id=c.id, chat_title=c.title, district=c.district, author_name=m.author_name,
        author_external_id=m.author_external_id, text=m.text or "", sent_at=m.sent_at,
        has_media=bool(m.has_media), media_url=m.media_url, is_forwarded=bool(m.is_forwarded),
        message_url=message_url(m, c), link_kind=link_kind(m, c),
        badges=FeedBadges(
            unique=bool(r and r.is_primary and r.uniqueness_score >= uq.UNIQUE_THRESHOLD),
            important=bool(r and r.is_important),
            duplicates=info.dup_count if not is_dup else 0,
            is_duplicate=is_dup, duplicate_of=r.duplicate_of if is_dup else None,
            has_phone=info.has_phone,
        ),
        uniqueness_score=r.uniqueness_score if r else None,
        emerging_key=info.emerging_key if (card_keys is None or info.emerging_key in card_keys) else None,
    )


def plain_feed_message(m: ChatMessage, c: MaxChat) -> FeedMessage:
    """Сообщение вне окна анализа (без бейджей)."""
    platform = c.platform or "max"
    return FeedMessage(
        id=m.id, platform=platform, platform_label=PLATFORM_LABELS.get(platform, platform), chat_id=c.id,
        chat_title=c.title, district=c.district, author_name=m.author_name,
        author_external_id=m.author_external_id, text=m.text or "", sent_at=m.sent_at,
        has_media=bool(m.has_media), media_url=m.media_url, is_forwarded=bool(m.is_forwarded),
        message_url=message_url(m, c), link_kind=link_kind(m, c),
    )


def _keywords(infos: list[MsgInfo], limit: int = 5) -> list[str]:
    stem_count: Counter[str] = Counter()
    word_for: dict[str, Counter[str]] = {}
    for info in infos:
        seen: set[str] = set()
        for w in text_words(info.msg.text or ""):
            if len(w) < 4:
                continue
            s = stem(w)
            word_for.setdefault(s, Counter())[w] += 1
            if s not in seen:
                stem_count[s] += 1
                seen.add(s)
    out = []
    for s, k in stem_count.most_common():
        if k < 2 and out:
            break
        out.append(word_for[s].most_common(1)[0][0])
        if len(out) >= limit:
            break
    return out


async def compute_emerging(session: AsyncSession, *, window_min: int | None = None,
                           min_messages: int | None = None, include_dismissed: bool = False,
                           now: datetime | None = None) -> list[EmergingStoryOut]:
    an = await analyze(session)
    now = now or utcnow()
    window = timedelta(minutes=window_min or settings.EMERGING_WINDOW_MIN)
    min_n = max(2, int(min_messages or settings.EMERGING_MIN_MESSAGES))
    dismissed = set() if include_dismissed else set((await session.scalars(select(DismissedEmerging.key))).all())
    linked = {k: (i, s) for k, i, s in (await session.execute(
        select(Investigation.emerging_key, Investigation.id, Investigation.status)
        .where(Investigation.emerging_key.is_not(None)).order_by(Investigation.id)
    )).all()}
    cards: list[EmergingStoryOut] = []
    for members in an.topics.values():
        infos = [an.infos[x] for x in members if now - an.infos[x].msg.sent_at <= window]
        if len(infos) < min_n:
            continue
        infos.sort(key=lambda i: (i.msg.sent_at, i.msg.id))
        key = an.infos[members[0]].emerging_key or f"e{infos[0].msg.id}"
        if key in dismissed:
            continue
        important = [i for i in infos if i.uniq and i.uniq.is_important]
        unique = [i for i in infos if i.uniq and i.uniq.is_primary and i.uniq.uniqueness_score >= uq.UNIQUE_THRESHOLD]
        if not important and len({i.chat.id for i in infos}) < 2:
            continue  # болтовня в одном чате без фактов
        chats = list(dict.fromkeys(i.chat.title for i in infos))
        platforms = list(dict.fromkeys(i.chat.platform or "max" for i in infos))
        first_at, last_at = infos[0].msg.sent_at, infos[-1].msg.sent_at
        recent = sum(1 for i in infos if now - i.msg.sent_at <= RECENT)
        prev = sum(1 for i in infos if RECENT < now - i.msg.sent_at <= 2 * RECENT)
        if now - last_at > FADING_AFTER:
            trend = "fading"
        elif recent > 0 and recent >= prev:
            trend = "rising"
        else:
            trend = "steady"
        top = _top_stems(an, [i.msg.id for i in infos])
        ranked = sorted(infos, key=lambda i: _centrality(an, i, top), reverse=True)
        lead = ranked[0]
        keywords = _keywords(infos)
        districts = Counter(i.chat.district for i in infos if i.chat.district)
        place = districts.most_common(1)[0][0] if districts else None
        head = first_sentence(lead.msg.text or "", 90)
        title = "Возможный инфоповод: " + head
        # summary — из самых «центральных» первоисточников (разные кластеры дублей, не заголовок)
        seen_clusters = {lead.uniq.cluster_id if lead.uniq else -1}
        parts: list[str] = []
        for i in ranked[1:]:
            cid = i.uniq.cluster_id if i.uniq else -i.msg.id
            if cid in seen_clusters or (i.uniq and not i.uniq.is_primary):
                continue
            seen_clusters.add(cid)
            t = " ".join((i.msg.text or "").split())
            parts.append(t if len(t) <= 140 else t[:139].rsplit(" ", 1)[0] + "…")
            if len(parts) >= 2:
                break
        summary = " ".join(p if p.endswith((".", "!", "?", "…")) else p + "." for p in parts) or head
        link = linked.get(key)
        if link is None:  # ключ мог смениться при слиянии кластеров
            for i in infos:
                if f"e{i.msg.id}" in linked:
                    link = linked[f"e{i.msg.id}"]
                    break
        score = (len(infos) + 2 * len(chats) + len(important) + 2 * recent
                 + (3 if trend == "rising" else 0) - (5 if trend == "fading" else 0))
        cards.append(EmergingStoryOut(
            key=key, title=title, summary=summary[:300], keywords=keywords,
            place=place, messages_count=len(infos), chats_count=len(chats), chats=chats, platforms=platforms,
            first_at=first_at, last_at=last_at, trend=trend, trend_label=TREND_LABELS[trend],
            recent_count=recent, important_count=len(important), unique_count=len(unique),
            has_phone_count=len({i.msg.author_external_id for i in infos if i.has_phone}),
            score=float(score), message_ids=[i.msg.id for i in infos],
            sample=[feed_message(i) for i in ranked[:3]],
            investigation_id=link[0] if link else None,
            investigation_status=InvestigationStatus(link[1]) if link else None,
        ))
    cards.sort(key=lambda c: (-c.score, c.first_at))
    return cards


async def find_card(session: AsyncSession, key: str) -> EmergingStoryOut | None:
    """Карточка по ключу; если ключ сменился — карточка, содержащая сообщение ``e<id>``."""
    cards = await compute_emerging(session, include_dismissed=True)
    for c in cards:
        if c.key == key:
            return c
    if key.startswith("e") and key[1:].isdigit():
        mid = int(key[1:])
        for c in cards:
            if mid in c.message_ids:
                return c
    return None


def lead_from_card(card: EmergingStoryOut, note: str | None = None) -> str:
    """Зацепка из карточки: сводка сигнала + цитаты (заголовок сюжета не повторяется)."""
    plats = ", ".join({"max": "MAX", "telegram": "Telegram"}.get(p, p) for p in card.platforms)
    lines = [f"Сигнал из чатов ({plats}): {card.messages_count} сообщений из {card.chats_count} чатов"
             + (f", {card.place}" if card.place else "") + "."]
    if card.summary:
        lines.append(card.summary)
    for s in card.sample[:3]:
        lines.append(f"— {s.chat_title}: «{first_sentence(s.text, 220)}»")
    if card.keywords:
        lines.append("Ключевые слова: " + ", ".join(card.keywords))
    if note:
        lines.append("Комментарий редактора: " + note.strip())
    return "\n".join(lines)


async def investigate_emerging(session: AsyncSession, key: str, user, *, channel_id: int | None = None,
                               title: str | None = None, note: str | None = None) -> tuple[Investigation, bool]:
    """Создать сюжет из карточки и запустить пайплайн. (inv, created); уже есть — (inv, False)."""
    from app.services import stories
    from app.services.stories import StoryError

    card = await find_card(session, key)
    if card is None:
        raise StoryError(404, "Возникающий сюжет не найден (возможно, сообщения вышли из окна)")
    if card.investigation_id is not None:
        inv = await session.get(Investigation, card.investigation_id)
        if inv is not None:
            return inv, False
    lead = lead_from_card(card, note)
    t = (title or "").strip() or first_sentence(card.title.replace("Возможный инфоповод: ", "", 1), 80)
    inv = await stories.create_story(session, user, lead, title=t, channel_id=channel_id, autorun=True,
                                     origin="emerging", emerging_key=card.key, signal_at=card.first_at)
    return inv, True


async def dismiss_emerging(session: AsyncSession, key: str) -> None:
    if await session.get(DismissedEmerging, key) is None:
        session.add(DismissedEmerging(key=key, created_at=utcnow()))
        await session.commit()


def reset_cache() -> None:
    global _cache
    _cache = None


__all__ = ["analyze", "compute_emerging", "investigate_emerging", "dismiss_emerging", "find_card",
           "feed_message", "plain_feed_message", "message_url", "reset_cache"]
