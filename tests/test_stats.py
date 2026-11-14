"""Статистика v2: services/stats.compute_stats (юнит на подготовленных данных) и GET /api/stats."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import (
    Draft,
    DraftAuthorKind,
    FactMark,
    FactState,
    Investigation,
    InvestigationStatus as S,
    Report,
    Source,
    SourceKind,
    utcnow,
)
from app.schemas import StatsOut
from app.services.stats import compute_stats
from nf_helpers import data

STATS_KEYS = {"stories_total", "by_status", "handed_off", "rejected", "in_work", "avg_signal_to_handoff_sec",
              "avg_signal_to_review_sec", "traceability", "source_coverage", "factcheck_correction_rate",
              "draft_acceptance_rate", "handoffs_by_channel", "recent_handoffs"}


@pytest.mark.unit
async def test_stats_empty_db(session):
    st = await compute_stats(session)
    assert set(st) == STATS_KEYS == set(StatsOut.model_fields)
    assert st["stories_total"] == 0 and st["by_status"] == {s.value: 0 for s in S}
    assert st["handed_off"] == st["rejected"] == st["in_work"] == 0
    for k in ("avg_signal_to_handoff_sec", "avg_signal_to_review_sec", "traceability", "source_coverage",
              "factcheck_correction_rate", "draft_acceptance_rate"):
        assert st[k] is None, f"{k}: нет данных -> None"
    assert st["handoffs_by_channel"] == {} and st["recent_handoffs"] == []


@pytest.mark.unit
async def test_stats_on_prepared_data(session):
    now = utcnow()

    def inv(status, signal_min_ago, review_min_ago=None, handoff_min_ago=None, channel=None):
        return Investigation(
            title=f"t-{status.value}", lead_text="l", status=status, created_at=now - timedelta(minutes=signal_min_ago),
            updated_at=now, signal_at=now - timedelta(minutes=signal_min_ago),
            review_at=now - timedelta(minutes=review_min_ago) if review_min_ago is not None else None,
            handed_off_at=now - timedelta(minutes=handoff_min_ago) if handoff_min_ago is not None else None,
            handoff_channel=channel, handoff_to="кому" if channel else None)

    a = inv(S.handed_off, 60, review_min_ago=58, handoff_min_ago=30, channel="site")      # 30 мин, 2 мин
    b = inv(S.handed_off, 100, review_min_ago=96, handoff_min_ago=10, channel="telegram")  # 90 мин, 4 мин
    c = inv(S.rejected, 50, review_min_ago=49)
    d = inv(S.review, 20, review_min_ago=19)
    e = inv(S.new, 5)
    session.add_all([a, b, c, d, e])
    await session.flush()
    # traceability: последний черновик a — 2 claims с источниками, b — 1 из 2
    claims_ok = [{"id": "c1", "text": "x", "source_ids": [1]}, {"id": "c2", "text": "y", "source_ids": [2]}]
    session.add_all([
        Draft(investigation_id=a.id, version=1, author_kind=DraftAuthorKind.ai, title="T", body="один два три",
              claims_json=[]),
        Draft(investigation_id=a.id, version=2, author_kind=DraftAuthorKind.editor, title="T", body="один два три",
              claims_json=claims_ok),
        Draft(investigation_id=b.id, version=1, author_kind=DraftAuthorKind.ai, title="T",
              body="совсем другой текст черновика", claims_json=[{"id": "c1", "text": "x", "source_ids": [3]}]),
        Draft(investigation_id=b.id, version=2, author_kind=DraftAuthorKind.editor, title="Z",
              body="переписано полностью редактором иначе", claims_json=[
                  {"id": "c1", "text": "x", "source_ids": [3]}, {"id": "c2", "text": "y", "source_ids": []}]),
    ])
    # source coverage: у a — 3 первоисточника 2 типов (покрыт), у d — 1 (нет)
    session.add_all([Report(investigation_id=a.id, report_json={}), Report(investigation_id=d.id, report_json={})])
    for kind in (SourceKind.max_chat, SourceKind.web, SourceKind.web):
        session.add(Source(investigation_id=a.id, kind=kind, is_primary=True))
    session.add(Source(investigation_id=d.id, kind=SourceKind.web, is_primary=True))
    session.add_all([FactMark(investigation_id=a.id, fact_text="f", state=FactState.confirmed),
                     FactMark(investigation_id=a.id, fact_text="f", state=FactState.false),
                     FactMark(investigation_id=b.id, fact_text="f", state=FactState.unconfirmed),
                     FactMark(investigation_id=b.id, fact_text="f", state=FactState.confirmed)])
    await session.commit()

    st = await compute_stats(session)
    assert st["stories_total"] == 5
    assert st["by_status"] == {"new": 1, "researching": 0, "review": 1, "handed_off": 2, "rejected": 1}
    assert st["handed_off"] == 2 and st["rejected"] == 1 and st["in_work"] == 2
    assert st["avg_signal_to_handoff_sec"] == pytest.approx((30 * 60 + 90 * 60) / 2, abs=1)
    assert st["avg_signal_to_review_sec"] == pytest.approx((2 + 4 + 1 + 1) * 60 / 4, abs=1)
    assert st["traceability"] == pytest.approx(3 / 4, abs=1e-4)
    assert st["source_coverage"] == pytest.approx(0.5)
    assert st["factcheck_correction_rate"] == pytest.approx(0.5)
    assert st["draft_acceptance_rate"] == pytest.approx(0.5), "a принят (без правок), b переписан"
    assert st["handoffs_by_channel"] == {"site": 1, "telegram": 1}
    rec = st["recent_handoffs"]
    assert [r["id"] for r in rec] == [b.id, a.id], "свежие передачи первыми"
    assert rec[0]["handoff_channel_label"] == "Telegram-канал"
    assert rec[1]["signal_to_handoff_sec"] == pytest.approx(30 * 60, abs=1)
    StatsOut(**st)


@pytest.mark.api
async def test_stats_api_after_handoff(editor, review_story):
    before = data(await editor.get("/api/stats"))
    assert set(before) == STATS_KEYS
    assert sum(before["by_status"].values()) == before["stories_total"]
    assert before["in_work"] == sum(before["by_status"][k] for k in ("new", "researching", "review"))
    for k in ("traceability", "source_coverage", "factcheck_correction_rate", "draft_acceptance_rate"):
        assert before[k] is None or 0 <= before[k] <= 1, (k, before[k])
    data(await editor.post(f"/api/stories/{review_story['id']}/handoff", json={"to": "Сайт", "channel": "max"}))
    after = data(await editor.get("/api/stats"))
    assert after["handed_off"] == before["handed_off"] + 1
    assert after["by_status"]["review"] == before["by_status"]["review"] - 1
    assert after["handoffs_by_channel"].get("max", 0) == before["handoffs_by_channel"].get("max", 0) + 1
    top = after["recent_handoffs"][0]
    assert top["id"] == review_story["id"] and top["handoff_channel_label"] == "Канал MAX"
    assert top["signal_to_handoff_sec"] == pytest.approx(3600, abs=60), "signal_at = час назад"
    assert data(await editor.get("/api/metrics")) == after, "алиас /api/metrics"
