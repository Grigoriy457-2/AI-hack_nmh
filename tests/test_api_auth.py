"""API v2: служебные эндпоинты, вход/выход редактора, 401 без входа (CONTRACTS_v2 §1, §4)."""
from __future__ import annotations

import pytest

from app.auth import COOKIE_NAME
from nf_helpers import check, data

pytestmark = pytest.mark.api


async def test_health(anon):
    assert data(await anon.get("/api/health")) == {"status": "ok"}


async def test_users_public_only_editors(anon, users):
    lst = data(await anon.get("/api/users"))
    assert {u["login"] for u in lst} == {"editor", "editor2"}
    for u in lst:
        assert set(u) == {"id", "name", "login", "title"}, f"UserOut без роли: {u}"


@pytest.mark.parametrize("payload_kind", ["user_id", "login"])
async def test_login_logout_flow(anon, users, payload_kind):
    payload = {"user_id": users["editor"]} if payload_kind == "user_id" else {"login": "editor"}
    r = check(await anon.post("/api/auth/login", json={**payload, "password": "demo"}))
    assert r.json()["login"] == "editor"
    assert COOKIE_NAME in anon.cookies
    me = data(await anon.get("/api/me"))
    assert me["id"] == users["editor"] and set(me) == {"id", "name", "login", "title"}
    r = check(await anon.post("/api/auth/logout"), 204)
    assert COOKIE_NAME in r.headers.get("set-cookie", ""), "logout должен снимать cookie"
    anon.cookies.clear()
    assert (await anon.get("/api/me")).status_code == 401


async def test_demo_login_first_editor(anon, users):
    r = data(await anon.post("/api/auth/demo-login"))
    assert r["id"] == min(users.values())
    assert data(await anon.get("/api/me"))["id"] == r["id"]


async def test_login_wrong_password(anon, users):
    check(await anon.post("/api/auth/login", json={"login": "editor", "password": "nope"}), 401)


async def test_login_unknown_user(anon):
    check(await anon.post("/api/auth/login", json={"user_id": 99999, "password": "demo"}), 404)
    check(await anon.post("/api/auth/login", json={"login": "producer", "password": "demo"}), 404)


async def test_login_without_identity(anon):
    r = await anon.post("/api/auth/login", json={"password": "demo"})
    assert r.status_code in (404, 422), r.text


async def test_bad_cookie_is_401(make_client):
    c = make_client(None)
    c.cookies.set(COOKIE_NAME, "garbage.token.value")
    assert (await c.get("/api/me")).status_code == 401


async def test_me_for_both_editors(editor, editor2, users):
    assert data(await editor.get("/api/me"))["login"] == "editor"
    assert data(await editor2.get("/api/me"))["id"] == users["editor2"]


PROTECTED = [
    ("GET", "/api/me"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/feed"),
    ("GET", "/api/stream?timeout=0"),
    ("GET", "/api/emerging"),
    ("POST", "/api/emerging/e1/investigate"),
    ("POST", "/api/emerging/e1/dismiss"),
    ("GET", "/api/stories"),
    ("POST", "/api/stories"),
    ("GET", "/api/stories/1"),
    ("POST", "/api/stories/1/run"),
    ("POST", "/api/stories/1/deeper"),
    ("POST", "/api/stories/1/stop"),
    ("GET", "/api/stories/1/handoff/check"),
    ("POST", "/api/stories/1/handoff"),
    ("POST", "/api/stories/1/reject"),
    ("POST", "/api/stories/1/reopen"),
    ("POST", "/api/stories/1/transition"),
    ("GET", "/api/stories/1/progress"),
    ("GET", "/api/stories/1/iterations"),
    ("GET", "/api/stories/1/sources"),
    ("GET", "/api/stories/1/contacts"),
    ("GET", "/api/stories/1/media"),
    ("GET", "/api/stories/1/report"),
    ("GET", "/api/stories/1/drafts"),
    ("POST", "/api/stories/1/drafts"),
    ("GET", "/api/stories/1/drafts/diff"),
    ("GET", "/api/stories/1/facts"),
    ("POST", "/api/stories/1/facts"),
    ("GET", "/api/stories/1/history"),
    ("GET", "/api/investigations"),
    ("GET", "/api/investigations/1/contacts"),
    ("GET", "/api/handoff-channels"),
    ("GET", "/api/channels"),
    ("POST", "/api/channels/1/dna"),
    ("GET", "/api/sources/chats"),
    ("GET", "/api/max/chats"),
    ("POST", "/api/sources"),
    ("PATCH", "/api/sources/chats/1"),
    ("DELETE", "/api/sources/chats/1"),
    ("POST", "/api/sources/sync"),
    ("GET", "/api/telegram/status"),
    ("POST", "/api/telegram/sources"),
    ("DELETE", "/api/telegram/sources/tg:x"),
    ("GET", "/api/demo/stream"),
    ("POST", "/api/demo/restart"),
    ("GET", "/api/stats"),
    ("GET", "/api/metrics"),
]

_BODIES = {
    "/api/stories": {"lead_text": "что-то случилось"},
    "/api/stories/1/drafts": {"title": "t", "body": "b"},
    "/api/stories/1/facts": {"fact_text": "f", "state": "confirmed"},
    "/api/stories/1/transition": {"to_status": "review"},
    "/api/stories/1/handoff": {"to": "x", "channel": "site"},
    "/api/sources": {"platform": "telegram", "ref": "@moscow_live_demo"},
    "/api/sources/chats/1": {"enabled": False},
    "/api/telegram/sources": {"ref": "@moscow_live_demo"},
}


@pytest.mark.parametrize("method,path", PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
async def test_401_without_login(anon, method, path):
    body = _BODIES.get(path)
    r = await anon.request(method, path, json=body) if body is not None else await anon.request(method, path)
    assert r.status_code == 401, f"{method} {path} без входа -> {r.status_code}: {r.text[:200]}"


REMOVED = [("GET", "/api/operator/board"), ("GET", "/api/feedback"), ("POST", "/api/investigations/1/feedback")]


@pytest.mark.parametrize("method,path", REMOVED, ids=[f"{m} {p}" for m, p in REMOVED])
async def test_removed_endpoints(editor, method, path):
    r = await editor.request(method, path, json={"verdict": "good"} if method == "POST" else None)
    assert r.status_code in (404, 405), f"{method} {path} должен быть удалён (v2): {r.status_code}"


PAGES = ["/", "/stories", "/stories/1", "/sources", "/stats"]


@pytest.mark.parametrize("path", PAGES)
async def test_pages_redirect_to_login(anon, path):
    r = await anon.get(path)
    assert r.status_code in (302, 303, 307) and r.headers["location"].endswith("/login"), \
        f"{path}: {r.status_code} {r.headers.get('location')}"
