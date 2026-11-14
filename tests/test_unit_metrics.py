"""Юнит: services/metrics.py на подготовленных данных (п.9.6 ТЗ, CONTRACTS §6 «Метрики»)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import (
    Draft,
    DraftAuthorKind,
    FactMark,
    FactState,
    Feedback,
    Investigation,
    InvestigationStatus as S,
    Report,
    Role,
    Source,
    SourceKind,
    User,
    Verdict,
    utcnow,
)
from app.schemas import MetricsOut
from app.services import metrics as M

pytestmark = pytest.mark.unit

AI_BODY = ("Около 14:30 жители дома в Чертанове услышали хлопок. По словам очевидцев, "
           "выбило окна на третьем этаже. Информация о пострадавших уточняется.")


def test_metric_keys_match_schema():
    assert set(M.METRIC_KEYS) == set(MetricsOut.model_fields)


async def test_metrics_empty_db(session):
    m = await M.compute_metrics(session)
    assert set(m) == set(M.METRIC_KEYS)
    for k in ("time_to_draft_avg_sec", "source_coverage", "traceability", "draft_acceptance_rate",
              "factcheck_correction_rate", "feedback_good_share"):
        assert m[k] is None, f"{k} должен быть None на пустой БД, а не {m[k]!r}"
    assert m["investigations_total"] == 0
    assert m["feedback_total"] == 0
    assert set(m["by_status"]) == {s.value for s in S}
    assert all(v == 0 for v in m["by_status"].values())
    MetricsOut(**m)


async def test_metrics_on_prepared_data(session):
    p = User(name="p", role=Role.producer, login="p")
    e = User(name="e", role=Role.editor, login="e")
    session.add_all([p, e])
    await session.flush()
    now = utcnow()

    inv1 = Investigation(title="1", lead_text="lead1", status=S.published, producer_id=p.id,
                         created_at=now - timedelta(seconds=100), report_ready_at=now, updated_at=now)
    inv2 = Investigation(title="2", lead_text="lead2", status=S.editing, producer_id=p.id,
                         created_at=now - timedelta(seconds=300), report_ready_at=now, updated_at=now)
    session.add_all([inv1, inv2])
    await session.flush()

    def src(inv, kind, primary=True):
        return Source(investigation_id=inv.id, kind=kind, title="t", url="u", is_primary=primary,
                      uniqueness_score=1.0, is_important=True)

    session.add_all([
        # inv1: 3 первоисточника, 2 типа -> покрыто
        src(inv1, SourceKind.web), src(inv1, SourceKind.web), src(inv1, SourceKind.max_chat),
        src(inv1, SourceKind.web, primary=False),
        # inv2: 1 первоисточник -> не покрыто
        src(inv2, SourceKind.web), src(inv2, SourceKind.official, primary=False),
    ])
    session.add_all([Report(investigation_id=i.id, report_json={"sections": []}) for i in (inv1, inv2)])

    claims_ok = [{"id": "c1", "text": "Около 14:30", "source_ids": [1], "status": "unconfirmed"},
                 {"id": "c2", "text": "хлопок", "source_ids": [2, 3], "status": "confirmed"}]
    claims_half = [{"id": "c1", "text": "x", "source_ids": [1], "status": "unconfirmed"},
                   {"id": "c2", "text": "y", "source_ids": [], "status": "unconfirmed"}]
    session.add_all([
        # inv1: правка почти без изменений -> принят; последние claims 2/2 с источниками
        Draft(investigation_id=inv1.id, version=1, author_kind=DraftAuthorKind.ai, title="t",
              body=AI_BODY, claims_json=[{"id": "c1", "text": "x", "source_ids": []}]),
        Draft(investigation_id=inv1.id, version=2, author_kind=DraftAuthorKind.editor, title="t",
              body=AI_BODY.replace("третьем", "3-м"), claims_json=claims_ok, author_user_id=e.id),
        # inv2: переписан полностью -> не принят; последние claims 1/2
        Draft(investigation_id=inv2.id, version=1, author_kind=DraftAuthorKind.ai, title="t",
              body=AI_BODY, claims_json=claims_ok),
        Draft(investigation_id=inv2.id, version=2, author_kind=DraftAuthorKind.editor, title="t",
              body="Совершенно другой текст, редактор всё переписал с нуля.", claims_json=claims_half,
              author_user_id=e.id),
    ])
    session.add_all([
        FactMark(investigation_id=inv1.id, fact_text="a", state=FactState.confirmed, user_id=p.id),
        FactMark(investigation_id=inv1.id, fact_text="b", state=FactState.false, user_id=p.id),
        FactMark(investigation_id=inv2.id, fact_text="c", state=FactState.unconfirmed, user_id=p.id),
        FactMark(investigation_id=inv2.id, fact_text="d", state=FactState.confirmed, user_id=p.id),
    ])
    session.add_all([
        Feedback(investigation_id=inv1.id, user_id=p.id, verdict=Verdict.good),
        Feedback(investigation_id=inv1.id, user_id=p.id, verdict=Verdict.good),
        Feedback(investigation_id=inv2.id, user_id=p.id, verdict=Verdict.bad),
    ])
    await session.commit()

    m = await M.compute_metrics(session)
    MetricsOut(**m)
    assert m["investigations_total"] == 2
    assert m["by_status"]["published"] == 1 and m["by_status"]["editing"] == 1
    assert m["by_status"]["new"] == 0
    assert m["feedback_total"] == 3
    assert m["time_to_draft_avg_sec"] == pytest.approx(200, abs=2)
    assert m["source_coverage"] == pytest.approx(0.5)
    assert m["traceability"] == pytest.approx(0.75)
    assert m["draft_acceptance_rate"] == pytest.approx(0.5)
    assert m["factcheck_correction_rate"] == pytest.approx(0.5)
    assert m["feedback_good_share"] == pytest.approx(2 / 3, abs=1e-3)


async def test_metrics_draft_without_edits_is_accepted(session):
    """Опубликовано без правок редактора = принят; ещё не дошедшее до редактуры — не учитывается."""
    p = User(name="p", role=Role.producer, login="p")
    session.add(p)
    await session.flush()
    pub = Investigation(title="1", lead_text="lead", status=S.published, producer_id=p.id,
                        report_ready_at=utcnow())
    fresh = Investigation(title="2", lead_text="lead", status=S.report_ready, producer_id=p.id,
                          report_ready_at=utcnow())
    session.add_all([pub, fresh])
    await session.flush()
    for inv in (pub, fresh):
        session.add(Draft(investigation_id=inv.id, version=1, author_kind=DraftAuthorKind.ai, title="t",
                          body=AI_BODY, claims_json=[]))
    await session.commit()
    m = await M.compute_metrics(session)
    assert m["draft_acceptance_rate"] == pytest.approx(1.0)
