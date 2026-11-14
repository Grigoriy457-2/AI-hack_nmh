"""Смоук HTML (п.9 ТЗ, CONTRACTS §7): страницы отдают 200 нужной роли, без трейсбэков и CDN."""
from __future__ import annotations

import pytest

from app.models import InvestigationStatus as S
from nf_helpers import assert_clean_html, check, make_investigation

pytestmark = pytest.mark.smoke

TABS = ["progress", "report", "sources", "contacts", "media", "draft", "history"]


def test_import_app_main():
    import app.main

    assert app.main.app.title


async def test_static_css(anon):
    r = check(await anon.get("/static/style.css"))
    assert "css" in r.headers.get("content-type", "")


async def test_login_page(anon):
    html = assert_clean_html(await anon.get("/login"))
    for name in ("Анна Смирнова", "Елена Воронина", "Мария Ким", "Игорь Белов"):
        assert name in html, f"на /login нет карточки {name}"


async def test_login_page_redirects_when_logged_in(producer):
    r = check(await producer.get("/login"), 303)
    assert r.headers["location"].rstrip("/") in ("", "http://testserver")


async def test_login_form_post(anon, users):
    r = await anon.post("/login", data={"user_id": str(users["producer"]), "password": "demo"})
    check(r, 303)
    assert r.headers["location"].rstrip("/") in ("", "http://testserver")
    assert "nf_session" in r.headers.get("set-cookie", "")
    assert_clean_html(await anon.get("/"))


async def test_login_form_wrong_password(anon, users):
    r = await anon.post("/login", data={"user_id": str(users["producer"]), "password": "bad"})
    assert r.status_code in (200, 401), r.status_code
    assert "nf_session=" not in r.headers.get("set-cookie", "").replace("nf_session=\"\"", "")
    assert "Traceback" not in r.text


async def test_logout_page(producer):
    r = await producer.get("/logout")
    check(r, 303)
    assert r.headers["location"].endswith("/login")


@pytest.mark.parametrize("role", ["producer", "editor", "smm", "operator"])
async def test_dashboard(clients, role, researched):
    c = clients[role]
    r = await c.get("/")
    if role == "operator" and r.status_code in (302, 303, 307):
        assert r.headers["location"].endswith("/operator")
        r = await c.get("/operator")
    assert_clean_html(r)


async def test_new_investigation_page(producer):
    html = assert_clean_html(await producer.get("/investigations/new"))
    assert "Москва Срочно" in html, "в форме нет выбора канала"
    assert "lead_text" in html


async def test_new_investigation_forbidden_for_smm(smm):
    r = await smm.get("/investigations/new")
    assert r.status_code == 403
    assert "Traceback" not in r.text


async def test_new_investigation_form_post(producer, operator):
    check(await operator.post("/api/max/sync"))
    r = await producer.post("/investigations/new", data={"lead_text": "Хлопок у дома в Чертанове около 14:30"})
    check(r, 303)
    assert "/investigations/" in r.headers["location"]
    assert_clean_html(await producer.get(r.headers["location"]))


@pytest.mark.parametrize("role", ["producer", "editor", "smm", "operator"])
@pytest.mark.parametrize("tab", TABS)
async def test_investigation_tabs(clients, researched, role, tab):
    html = assert_clean_html(await clients[role].get(f"/investigations/{researched['id']}", params={"tab": tab}))
    if tab == "contacts":
        if role in ("producer", "operator"):
            assert "+79990001122" in html.replace(" ", "").replace("-", ""), "телефон не показан продюсеру"
            assert "tel:" in html
        else:
            assert "+79990001122" not in html.replace(" ", "").replace("-", ""), \
                f"контакты видны роли {role}!"


async def test_investigation_page_404(producer):
    r = check(await producer.get("/investigations/999999"), 404)
    assert "Traceback" not in r.text


async def test_editor_page(producer, editor, researched):
    check(await producer.post(f"/api/investigations/{researched['id']}/transition", json={"to_status": "editing"}))
    html = assert_clean_html(await editor.get(f"/investigations/{researched['id']}/edit"))
    assert "Около 14:30 жители дома услышали хлопок" in html


async def test_editor_page_save_form(producer, editor, researched):
    iid = researched["id"]
    check(await producer.post(f"/api/investigations/{iid}/transition", json={"to_status": "editing"}))
    r = await editor.post(f"/investigations/{iid}/edit", data={"title": "T", "body": researched["body"] + " Правка."})
    check(r, 303)
    assert_clean_html(await editor.get(f"/investigations/{iid}/edit"))


async def test_page_transition_form(producer, users):
    iid = await make_investigation(users, S.report_ready)
    r = await producer.post(f"/investigations/{iid}/transition", data={"to_status": "editing"})
    check(r, 303)


async def test_page_transition_forbidden(smm, users):
    iid = await make_investigation(users, S.report_ready)
    r = await smm.post(f"/investigations/{iid}/transition", data={"to_status": "editing"})
    assert r.status_code in (403, 303), r.status_code
    assert "Traceback" not in r.text


async def test_page_fact_form(producer, researched):
    r = await producer.post(f"/investigations/{researched['id']}/facts",
                            data={"fact_text": "Хлопок", "state": "confirmed", "claim_id": "c1"})
    check(r, 303)


async def test_editor_page_forbidden_for_smm(smm, researched):
    assert (await smm.get(f"/investigations/{researched['id']}/edit")).status_code == 403


async def test_operator_page(operator, researched):
    html = assert_clean_html(await operator.get("/operator"))
    assert "QA: хлопок в Чертанове" in html


async def test_operator_page_forbidden(producer):
    assert (await producer.get("/operator")).status_code == 403


async def test_operator_sync_and_toggle_forms(operator):
    r = await operator.post("/operator/sync")
    check(r, 303)
    assert "/operator" in r.headers["location"]
    assert_clean_html(await operator.get(r.headers["location"]))


async def test_feedback_page_and_form(producer, researched):
    assert_clean_html(await producer.get("/feedback"))
    assert_clean_html(await producer.get("/feedback", params={"investigation_id": researched["id"]}))
    r = await producer.post("/feedback", data={"investigation_id": str(researched["id"]),
                                               "verdict": "bad", "comment": "стрём"})
    check(r, 303)


async def test_feedback_page_forbidden_for_editor(editor):
    assert (await editor.get("/feedback")).status_code == 403
