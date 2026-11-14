"""API: права ролей (403) и переходы статусов через /transition (п.1, 2 ТЗ; CONTRACTS §6)."""
from __future__ import annotations

import pytest

from app.models import InvestigationStatus as S
from nf_helpers import check, data, make_investigation

pytestmark = pytest.mark.api

# (method, path, json, роли с доступом)  — operator проходит всегда
ROLE_MATRIX = [
    ("POST", "/api/investigations", {"lead_text": "что-то случилось у дома", "autorun": False}, {"producer"}),
    ("POST", "/api/investigations/{id}/run", None, {"producer"}),
    ("GET", "/api/investigations/{id}/contacts", None, {"producer"}),
    ("POST", "/api/investigations/{id}/drafts", {"title": "t", "body": "b"}, {"editor"}),
    ("POST", "/api/investigations/{id}/facts", {"fact_text": "f", "state": "confirmed"}, {"producer", "editor"}),
    ("POST", "/api/investigations/{id}/feedback", {"verdict": "good"}, {"producer"}),
    ("GET", "/api/feedback", None, set()),
    ("GET", "/api/operator/board", None, set()),
    ("GET", "/api/metrics", None, set()),
    ("GET", "/api/max/chats", None, set()),
    ("PATCH", "/api/max/chats/1", {"enabled": True}, set()),
    ("POST", "/api/max/sync", None, set()),
    ("POST", "/api/channels/1/dna", None, set()),
]


@pytest.mark.parametrize("role", ["producer", "editor", "smm"])
async def test_role_matrix_403(clients, researched, role):
    """Роли без доступа получают 403 (до логики эндпоинта)."""
    wrong = []
    for method, path, body, allowed in ROLE_MATRIX:
        if role in allowed:
            continue
        r = await clients[role].request(method, path.format(id=researched["id"]), json=body)
        if r.status_code != 403:
            wrong.append((method, path, r.status_code))
    assert not wrong, f"{role}: ожидали 403, получили {wrong}"


async def test_operator_passes_role_checks(operator, researched):
    wrong = []
    for method, path, body, _ in ROLE_MATRIX:
        if path.endswith("/run") or path == "/api/investigations":
            continue  # запускают пайплайн — проверяется отдельно
        r = await operator.request(method, path.format(id=researched["id"]), json=body)
        if r.status_code in (401, 403):
            wrong.append((method, path, r.status_code))
    assert not wrong, f"operator не должен получать 401/403: {wrong}"


async def test_contacts_hidden_from_editor_and_smm(editor, smm, researched):
    for c in (editor, smm):
        r = await c.get(f"/api/investigations/{researched['id']}/contacts")
        assert r.status_code == 403
        assert "+7999" not in r.text


async def test_contacts_visible_to_producer_and_operator(producer, operator, researched):
    for c in (producer, operator):
        check(await c.get(f"/api/investigations/{researched['id']}/contacts"))


# ---------------------------------------------------------------- переходы через API
async def _tr(client, inv_id, to, expected, **extra):
    return data(await client.post(f"/api/investigations/{inv_id}/transition",
                                  json={"to_status": to, **extra}), expected)


async def test_smm_cannot_move_to_editing(smm, users):
    inv = await make_investigation(users, S.report_ready)
    await _tr(smm, inv, "editing", 403)


async def test_editor_cannot_take_from_report_ready(editor, users):
    inv = await make_investigation(users, S.report_ready)
    await _tr(editor, inv, "editing", 403)


async def test_producer_cannot_publish(producer, users):
    inv = await make_investigation(users, S.ready)
    await _tr(producer, inv, "published", 403)


async def test_editor_cannot_publish(editor, users):
    inv = await make_investigation(users, S.ready)
    await _tr(editor, inv, "published", 403)


async def test_not_in_table_is_409(operator, producer, users):
    inv = await make_investigation(users, S.new)
    await _tr(operator, inv, "published", 409)
    await _tr(producer, inv, "editing", 409)


async def test_transition_404(producer):
    await _tr(producer, 999999, "editing", 404)


async def test_transition_invalid_status_422(producer, users):
    inv = await make_investigation(users, S.report_ready)
    await _tr(producer, inv, "flying", 422)


async def test_full_chain_via_api_and_history(producer, editor, smm, users):
    inv = await make_investigation(users, S.report_ready)
    out = await _tr(producer, inv, "editing", 200, comment="на редактуру")
    assert out["status"] == "editing"
    out = await _tr(editor, inv, "report_ready", 200, comment="вернуть")
    assert out["status"] == "report_ready"
    await _tr(producer, inv, "editing", 200)
    await _tr(editor, inv, "ready", 200)
    await _tr(smm, inv, "editing", 200, comment="вернуть на доработку")
    await _tr(editor, inv, "ready", 200)
    out = await _tr(smm, inv, "published", 200)
    assert out["status"] == "published"

    hist = data(await producer.get(f"/api/investigations/{inv}/history"))
    chain = [(h["from_status"], h["to_status"]) for h in hist]
    assert chain[-7:] == [
        ("report_ready", "editing"), ("editing", "report_ready"), ("report_ready", "editing"),
        ("editing", "ready"), ("ready", "editing"), ("editing", "ready"), ("ready", "published"),
    ]
    by_to: dict = {}
    for h in hist:  # первое вхождение каждой пары
        by_to.setdefault((h["from_status"], h["to_status"]), h)
    assert by_to[("report_ready", "editing")]["comment"] == "на редактуру"
    assert by_to[("ready", "published")]["user_id"] == users["smm"]
    assert by_to[("ready", "published")]["user"]["login"] == "smm"
    # после публикации — никаких переходов
    await _tr(smm, inv, "editing", 409)


async def test_editor_assigned_on_editing(producer, users):
    inv = await make_investigation(users, S.report_ready)
    out = await _tr(producer, inv, "editing", 200, editor_id=users["editor"])
    assert out["editor_id"] == users["editor"]


async def test_rejected_and_back(producer, users):
    inv = await make_investigation(users, S.report_ready)
    await _tr(producer, inv, "rejected", 200, comment="стрём")
    await _tr(producer, inv, "new", 200)


async def test_operator_can_do_any_table_transition(operator, users):
    inv = await make_investigation(users, S.report_ready)
    for st in ("editing", "ready", "published"):
        assert (await _tr(operator, inv, st, 200))["status"] == st


async def test_allowed_transitions_in_detail(clients, users):
    inv = await make_investigation(users, S.report_ready)
    exp = {
        "producer": {"editing", "rejected", "researching"},
        "editor": set(),
        "smm": set(),
        "operator": {"editing", "rejected", "researching"},
    }
    for role, e in exp.items():
        d = data(await clients[role].get(f"/api/investigations/{inv}"))
        assert set(d["allowed_transitions"]) == e, role
