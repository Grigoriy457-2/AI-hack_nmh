"""Юнит: демо-трикл (app/integrations/max/demo_stream.py) на управляемом времени — без sleep.

``released(now)`` / ``status(now)`` принимают «текущее» время, поэтому расписание проверяется
детерминированно при настоящих интервалах 10–30 с."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.ai import mock_data
from app.config import settings
from app.integrations.max import demo_stream as ds
from app.integrations.max.client import set_max_client
from app.integrations.max.mock import MockMaxClient
from app.models import utcnow

pytestmark = pytest.mark.unit


@pytest.fixture
def demo(monkeypatch, db_ready):
    monkeypatch.setattr(settings, "DEMO_STREAM_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_STREAM_INITIAL", 10)
    monkeypatch.setattr(settings, "DEMO_STREAM_MIN_SEC", 10.0)
    monkeypatch.setattr(settings, "DEMO_STREAM_MAX_SEC", 30.0)
    monkeypatch.setattr(settings, "DEMO_STREAM_LOOP", False)
    ds.reset_state(epoch=0)
    set_max_client(MockMaxClient())
    t0 = utcnow().replace(microsecond=0)
    ds.released(t0)  # старт потока в t0
    return t0


def test_initial_messages_are_noise_in_past(demo):
    t0 = demo
    out = ds.released(t0)
    assert len(out) == 10
    relevant = {m["external_id"] for m in mock_data.SCENARIO_MAX_MESSAGES if m.get("relevant")}
    assert not relevant & {m["external_id"] for m in out}, "стартовые — фоновый шум, событие идёт потоком"
    times = [m["sent_at"] for m in out]
    assert times == sorted(times) and all(t < t0 for t in times)
    assert times[-1] == t0 - ds.INITIAL_SPACING


def test_trickle_schedule_10_30_sec(demo):
    t0 = demo
    assert len(ds.released(t0 + timedelta(seconds=9.9))) == 10, "раньше 10 с новых нет"
    n31 = len(ds.released(t0 + timedelta(seconds=30.01)))
    assert 11 <= n31 <= 13, "к 30 с — 1..3 новых (интервал 10–30 с)"
    full = ds.released(t0 + timedelta(hours=1))
    assert len(full) == 40
    tail = [m["sent_at"] for m in full[10:]]
    gaps = [(b - a).total_seconds() for a, b in zip([t0] + tail[:-1], tail)]
    assert all(10.0 <= g <= 30.0 for g in gaps), gaps
    assert len({m["external_id"] for m in full}) == 40
    first_event = next(i for i, m in enumerate(full) if m["external_id"] == "m01")
    assert first_event >= 10


def test_status_progress_and_finish(demo):
    t0 = demo
    st = ds.status(t0)
    assert st["enabled"] and st["active"] and st["released"] == 10 and st["total"] == 40
    assert st["next_at"] and t0 + timedelta(seconds=10) <= st["next_at"] <= t0 + timedelta(seconds=30)
    assert not st["finished"] and st["interval_sec"] == [10.0, 30.0]
    end = ds.status(t0 + timedelta(hours=1))
    assert end["finished"] and end["released"] == 40 and end["next_at"] is None


def test_status_inactive_for_non_streaming_client(demo):
    set_max_client(MockMaxClient(stream=False))
    st = ds.status(demo)
    assert st["enabled"] and st["active"] is False and st["next_at"] is None


def test_loop_mode(demo, monkeypatch):
    t0 = demo
    monkeypatch.setattr(settings, "DEMO_STREAM_LOOP", True)
    out = ds.released(t0 + timedelta(hours=3))
    assert len(out) > 40
    ids = [m["external_id"] for m in out]
    assert len(set(ids)) == len(ids), "новый круг — новые external_id (~c1)"
    assert any(i.endswith("~c1") for i in ids)
    assert ds.status(t0 + timedelta(hours=3))["finished"] is False


def test_reset_state_new_epoch_ids(demo):
    ds.reset_state()
    t1 = utcnow()
    out = ds.released(t1)
    assert out and all(m["external_id"].endswith("~e1") for m in out)
    assert ds.status(t1)["epoch"] == 1


def test_anchor_aligned_to_stream(demo):
    """Якорь сценария = старт потока − 125 мин (все 7 постов mock-Telegram уже «вышли»)."""
    assert mock_data.scenario_event_time() == demo - ds.TG_LEAD


async def test_mock_client_streams_released_only(demo):
    c = MockMaxClient()
    assert c.streaming
    msgs = [m for ch in await c.list_chats() for m in await c.fetch_messages(ch.external_id)]
    assert 10 <= len(msgs) < 40
    assert MockMaxClient(stream=False).streaming is False
    assert MockMaxClient(now=demo).streaming is False, "явный now — детерминированный режим v1"
