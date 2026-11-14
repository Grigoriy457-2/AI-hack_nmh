"""Демо-поток (трикл) сообщений сценария для MockMaxClient.

При старте «уже написаны» ``DEMO_STREAM_INITIAL`` сообщений (фоновый шум домовых чатов), дальше —
по одному каждые ``DEMO_STREAM_MIN_SEC..MAX_SEC`` секунд: сначала события сценария (хлопок в Чертанове)
вперемешку с оставшимся шумом. ``sent_at`` = реальное время «выхода» сообщения, поэтому лента живая.
После исчерпания — остановка (или новый круг при ``DEMO_STREAM_LOOP``).

``restart(session)`` — «Перезапустить демо-поток»: прежние сообщения mock-чатов помечаются
``archived`` (сюжеты продолжают на них ссылаться), эпоха +1 (external_id получают суффикс ``~eN``),
отсчёт начинается заново, сразу выполняется синхронизация.

Состояние — в памяти процесса (демо одно на инстанс). Время всегда naive UTC.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.ai import mock_data
from app.config import settings
from app.models import utcnow

INITIAL_SPACING = timedelta(minutes=3)  # шаг между «уже написанными» сообщениями


@dataclass
class _State:
    epoch: int = 0
    started_at: datetime | None = None
    order: list[dict[str, Any]] = field(default_factory=list)  # порядок выдачи (сырые dict mock_data)
    offsets: list[float] = field(default_factory=list)  # сек от started_at для трикла (len = len(order) - initial)
    tg_all: bool = False  # после перезапуска посты mock-Telegram уже «опубликованы» — без гейта


_state = _State()


def is_enabled() -> bool:
    return bool(settings.DEMO_STREAM_ENABLED)


def _build_order() -> list[dict[str, Any]]:
    msgs = list(mock_data.SCENARIO_MAX_MESSAGES)
    junk = sorted((m for m in msgs if not m.get("relevant")), key=lambda m: m["minutes"])
    relevant = sorted((m for m in msgs if m.get("relevant")), key=lambda m: m["minutes"])
    initial_n = max(0, min(int(settings.DEMO_STREAM_INITIAL), len(msgs)))
    head = junk[:initial_n]
    if len(head) < initial_n:  # шума меньше, чем нужно стартовых
        head += relevant[: initial_n - len(head)]
        relevant = relevant[initial_n - len(head):]
    rest_junk = [m for m in junk if m not in head]
    tail: list[dict[str, Any]] = []
    for i, m in enumerate(relevant):
        tail.append(m)
        if i % 3 == 2 and rest_junk:  # шум между событиями
            tail.append(rest_junk.pop(0))
    tail += rest_junk
    return head + tail


def _ensure_started(now: datetime | None = None) -> _State:
    st = _state
    if st.started_at is None:
        st.started_at = now or utcnow()
        st.order = _build_order()
        rnd = random.Random(1000 + st.epoch)
        lo = max(0.0, float(settings.DEMO_STREAM_MIN_SEC))
        hi = max(lo, float(settings.DEMO_STREAM_MAX_SEC))
        acc, offs = 0.0, []
        n_tail = len(st.order) - _initial_count(st)
        # бесконечный круг при LOOP — расписание считаем на лету в _offset()
        for _ in range(n_tail):
            acc += rnd.uniform(lo, hi)
            offs.append(acc)
        st.offsets = offs
        if is_enabled():
            _align_scenario_anchor(st)
    return st


TG_LEAD = timedelta(minutes=125)  # все 7 постов mock-Telegram (+7…+120 мин) уже «вышли»


def _align_scenario_anchor(st: _State) -> None:
    """Якорь сценария = старт потока − 125 мин: mock-Telegram (посты +9…+47 мин от якоря) и
    mock-поиск оказываются «свежими» (в окне возникающих сюжетов) и сразу доступны, поэтому
    кросс-платформенные дубли TG ↔ MAX видны на Радаре. При исследовании пайплайн переставит
    якорь по фактическому времени m01 (``pipeline._detect_scenario_anchor``)."""
    if st.started_at is not None:
        mock_data.set_scenario_anchor(st.started_at - TG_LEAD)


def _initial_count(st: _State) -> int:
    return max(0, min(int(settings.DEMO_STREAM_INITIAL), len(st.order)))


def _offset(st: _State, k: int) -> float:
    """Смещение (сек) k-го сообщения трикла (k от 0); для LOOP — продолжение расписания."""
    if k < len(st.offsets):
        return st.offsets[k]
    lo = max(0.0, float(settings.DEMO_STREAM_MIN_SEC))
    hi = max(lo, float(settings.DEMO_STREAM_MAX_SEC))
    base = st.offsets[-1] if st.offsets else 0.0
    rnd = random.Random(5000 + st.epoch * 1000 + k)
    return base + (k - len(st.offsets) + 1) * (lo + hi) / 2 + rnd.uniform(-1, 1)


def _ext_id(raw_id: str, epoch: int, cycle: int) -> str:
    sfx = (f"~e{epoch}" if epoch else "") + (f"~c{cycle}" if cycle else "")
    return raw_id + sfx


def released(now: datetime | None = None) -> list[dict[str, Any]]:
    """Сообщения, «написанные» к моменту now: dict-ы как mock_data (+ sent_at, external_id с эпохой)."""
    now = now or utcnow()
    st = _ensure_started(now)
    initial = _initial_count(st)
    out: list[dict[str, Any]] = []
    total = len(st.order)
    for i, m in enumerate(st.order[:initial]):
        d = {"has_media": False, "media_url": None, "is_forwarded": False, **m}
        d["sent_at"] = st.started_at - INITIAL_SPACING * (initial - i)
        d["external_id"] = _ext_id(m["external_id"], st.epoch, 0)
        out.append(d)
    k = 0
    while True:
        idx = initial + k
        if idx >= total and not settings.DEMO_STREAM_LOOP:
            break
        if total == 0:
            break
        at = st.started_at + timedelta(seconds=_offset(st, k))
        if at > now:
            break
        cycle, pos = divmod(idx, total)
        m = st.order[pos]
        d = {"has_media": False, "media_url": None, "is_forwarded": False, **m}
        d["sent_at"] = at
        d["external_id"] = _ext_id(m["external_id"], st.epoch, cycle)
        out.append(d)
        k += 1
        if k > 10000:  # защита
            break
    return out


def status(now: datetime | None = None) -> dict[str, Any]:
    from app.integrations.max.client import get_max_client
    from app.integrations.max.mock import MockMaxClient

    now = now or utcnow()
    client = get_max_client()
    active = is_enabled() and isinstance(client, MockMaxClient) and client.streaming
    st = _ensure_started(now)
    rel = len(released(now))
    total = len(st.order)
    finished = rel >= total and not settings.DEMO_STREAM_LOOP
    next_at = None
    if not finished:
        k = rel - _initial_count(st)
        next_at = st.started_at + timedelta(seconds=_offset(st, max(k, 0)))
    return {
        "enabled": is_enabled(), "active": bool(active), "epoch": st.epoch, "started_at": st.started_at,
        "released": rel, "total": total, "next_at": next_at if active else None, "finished": finished,
        "interval_sec": [float(settings.DEMO_STREAM_MIN_SEC), float(settings.DEMO_STREAM_MAX_SEC)],
    }


# ---------------------------------------------------------------- mock-Telegram в демо-потоке
def telegram_gate_active(client) -> bool:
    """Демо-гейт для mock-Telegram: включён, если трикл MAX работает и клиент Telegram — mock."""
    if not is_enabled() or getattr(client, "name", "") != "mock":
        return False
    from app.integrations.max.client import get_max_client
    from app.integrations.max.mock import MockMaxClient

    mc = get_max_client()
    return isinstance(mc, MockMaxClient) and mc.streaming


def gate_telegram(msgs: list, now: datetime | None = None) -> list:
    """Посты mock-Telegram (время = якорь + minutes) выпускаются по ходу MAX-потока: пост минуты M
    «выходит» сразу после первого MAX-сообщения сценария с minutes ≥ M (+1 мс; посты позже всего
    сценария — после последнего). sent_at = момент выхода, external_id получает суффикс эпохи —
    хронология ленты правильная, а «Москва Live» (M=9) остаётся раньше пересылок m09/m10."""
    import dataclasses

    now = now or utcnow()
    st = _ensure_started(now)
    anchor = mock_data.scenario_event_time()
    initial = _initial_count(st)
    times: list[tuple[float, datetime]] = []  # (минута сценария, момент выхода) релевантных MAX
    for i, m in enumerate(st.order):
        if not m.get("relevant"):
            continue
        at = (st.started_at - INITIAL_SPACING * (initial - i)) if i < initial \
            else st.started_at + timedelta(seconds=_offset(st, i - initial))
        times.append((float(m["minutes"]), at))
    times.sort(key=lambda x: x[0])
    last_at = max((t for _, t in times), default=st.started_at)
    if st.tg_all:  # после «Перезапустить демо-поток»: каналы уже опубликовали всё — отдаём как есть
        return sorted(msgs, key=lambda x: (x.sent_at, x.external_id))
    out = []
    for m in msgs:
        minute = (m.sent_at - anchor).total_seconds() / 60.0
        rel = next((t for mm, t in times if mm >= minute), None)
        at = (rel or last_at) + timedelta(milliseconds=1)
        if at > now:
            continue
        sfx = f"~e{st.epoch}" if st.epoch else ""
        out.append(dataclasses.replace(m, sent_at=at, external_id=f"{m.external_id}{sfx}"))
    out.sort(key=lambda x: (x.sent_at, x.external_id))
    return out


def reset_state(*, epoch: int | None = None) -> None:
    """Начать поток заново (без работы с БД). Тесты/перезапуск."""
    global _state
    _state = _State(epoch=_state.epoch + 1 if epoch is None else epoch)


async def restart(session) -> dict[str, Any]:
    """«Перезапустить демо-поток»: эпоха +1, синхронизация.

    Трикл включён: сообщения mock-чатов MAX архивируются и поток идёт заново (новые external_id ~eN).
    Трикл выключен: клиент отдаёт те же external_id, поэтому ничего не архивируем — лента остаётся как есть.
    Посты mock-Telegram не архивируются и после перезапуска доступны сразу все (``tg_all``): «Москва Live»
    остаётся раньше новых m09/m10, т.е. их первоисточником."""
    from sqlalchemy import select, update

    from app.integrations.max.poller import sync_once
    from app.models import ChatMessage, MaxChat

    chat_ids = [str(c.get("external_id") or c.get("id")) for c in mock_data.SCENARIO_MAX_CHATS]
    ids = (await session.scalars(
        select(MaxChat.id).where(MaxChat.platform == "max", MaxChat.external_id.in_(chat_ids))
    )).all()
    from app.integrations.max.client import get_max_client
    from app.integrations.max.mock import MockMaxClient

    mc = get_max_client()
    streaming = is_enabled() and isinstance(mc, MockMaxClient) and mc.streaming
    if ids and streaming:
        await session.execute(update(ChatMessage).where(ChatMessage.chat_id.in_(ids)).values(archived=True))
    await session.commit()
    reset_state()
    _state.tg_all = True
    await sync_once(session)
    return status()
