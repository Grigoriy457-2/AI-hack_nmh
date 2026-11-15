"""Регрессия: источник без даты публикации не должен ронять пайплайн (OverflowError в _when)."""
import json
from datetime import datetime

import pytest

from app.ai.extractive import ExtractiveLLM, _when
from app.ai.text import hhmm_msk, to_msk
from app.routers.pages import fmt_dt

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("dt", [datetime.max, datetime.min, None])
def test_time_helpers_handle_sentinels(dt):
    assert _when(dt) is None
    assert to_msk(dt) is None
    assert hhmm_msk(dt) is None
    assert fmt_dt(dt) == "—"


def test_time_helpers_normal_value():
    dt = datetime(2026, 11, 15, 11, 30)
    assert _when(dt) == "15.11 14:30"
    assert hhmm_msk(dt) == "14:30"


async def test_intelligence_with_undated_source():
    llm = ExtractiveLLM()
    payload = {
        "lead_text": "Пожар в квартире на юго-востоке Москвы",
        "entities": {"keywords": ["пожар", "квартира"]},
        "sources": [
            {"id": 1, "kind": "web", "is_primary": True, "outlet": "Москва 24",
             "title": "Пожар в квартире на юго-востоке Москвы", "text": "В квартире произошёл пожар.",
             "url": "https://www.m24.ru/news/1", "published_at": None},
            {"id": 2, "kind": "web", "is_primary": True, "outlet": "MSK1",
             "title": "Пожар в квартире в Москве", "text": "Пожарные потушили возгорание в квартире.",
             "url": "https://msk1.ru/text/1", "published_at": "2026-11-15T08:00:00"},
        ],
    }
    res = await llm.complete_json("", json.dumps(payload, ensure_ascii=False), task="intelligence")
    assert isinstance(res, dict)
    assert any("время неизвестно" in f.get("text", "") for f in res.get("facts", []))
