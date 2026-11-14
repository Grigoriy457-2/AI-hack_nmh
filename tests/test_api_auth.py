"""API: служебные эндпоинты, логин/логаут, 401 без входа (CONTRACTS §3, §6)."""
from __future__ import annotations

import pytest

from app.auth import COOKIE_NAME
from nf_helpers import check, data

pytestmark = pytest.mark.api


async def test_health(anon):
    assert data(await anon.get("/api/health")) == {"status": "ok"}


async def test_users_public(anon, users):
    lst = data(await anon.get("/api/users"))
    logins = {u["login"] for u in lst}
    assert set(users) <= logins
    roles = {u["role"] for u in lst}
    assert roles == {"producer", "editor", "smm", "operator"}


async def test_login_logout_flow(anon, users):
    r = check(await anon.post("/api/auth/login", json={"user_id": users["editor"], "password": "demo"}))
    assert r.json()["login"] == "editor" and r.json()["role"] == "editor"
    assert COOKIE_NAME in r.cookies or COOKIE_NAME in anon.cookies
    me = data(await anon.get("/api/me"))
    assert me["id"] == users["editor"]
    check(await anon.post("/api/auth/logout"), 204)
    anon.cookies.clear()  # браузер удалил бы cookie по Set-Cookie max-age=0
    assert (await anon.get("/api/me")).status_code == 401


async def test_logout_sets_expired_cookie(producer):
    r = check(await producer.post("/api/auth/logout"), 204)
    sc = r.headers.get("set-cookie", "")
    assert COOKIE_NAME in sc, "logout должен снимать cookie"


async def test_login_wrong_password(anon, users):
    r = await anon.post("/api/auth/login", json={"user_id": users["producer"], "password": "nope"})
    check(r, 401)


async def test_login_unknown_user(anon):
    check(await anon.post("/api/auth/login", json={"user_id": 99999, "password": "demo"}), 404)


async def test_login_validation(anon):
    check(await anon.post("/api/auth/login", json={"password": "demo"}), 422)


async def test_bad_cookie_is_401(make_client):
    c = make_client(None)
    c.cookies.set(COOKIE_NAME, "garbage.token.value")
    assert (await c.get("/api/me")).status_code == 401


async def test_me_for_each_role(clients, users):
    for login in ("producer", "editor", "smm", "operator"):
        me = data(await clients[login].get("/api/me"))
        assert me["login"] == login and me["id"] == users[login]


PROTECTED = [
    ("GET", "/api/me"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/channels"),
    ("POST", "/api/channels/1/dna"),
    ("GET", "/api/investigations"),
    ("POST", "/api/investigations"),
    ("GET", "/api/investigations/1"),
    ("POST", "/api/investigations/1/run"),
    ("GET", "/api/investigations/1/progress"),
    ("GET", "/api/investigations/1/iterations"),
    ("GET", "/api/investigations/1/sources"),
    ("GET", "/api/investigations/1/contacts"),
    ("GET", "/api/investigations/1/media"),
    ("GET", "/api/investigations/1/report"),
    ("GET", "/api/investigations/1/drafts"),
    ("POST", "/api/investigations/1/drafts"),
    ("GET", "/api/investigations/1/drafts/diff"),
    ("GET", "/api/investigations/1/facts"),
    ("POST", "/api/investigations/1/facts"),
    ("POST", "/api/investigations/1/transition"),
    ("GET", "/api/investigations/1/history"),
    ("POST", "/api/investigations/1/feedback"),
    ("GET", "/api/feedback"),
    ("GET", "/api/operator/board"),
    ("GET", "/api/metrics"),
    ("GET", "/api/max/chats"),
    ("PATCH", "/api/max/chats/1"),
    ("POST", "/api/max/sync"),
]


@pytest.mark.parametrize("method,path", PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
async def test_401_without_login(anon, method, path):
    body = {
        "/api/investigations": {"lead_text": "что-то случилось"},
        "/api/investigations/1/drafts": {"title": "t", "body": "b"},
        "/api/investigations/1/facts": {"fact_text": "f", "state": "confirmed"},
        "/api/investigations/1/transition": {"to_status": "editing"},
        "/api/investigations/1/feedback": {"verdict": "good"},
        "/api/max/chats/1": {"enabled": False},
    }.get(path)
    r = await anon.request(method, path, json=body)
    assert r.status_code == 401, f"{method} {path} без входа -> {r.status_code}"


PAGES = ["/", "/investigations/new", "/investigations/1", "/investigations/1/edit", "/operator",
         "/feedback"]


@pytest.mark.parametrize("path", PAGES)
async def test_pages_redirect_to_login(anon, path):
    r = await anon.get(path)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")
