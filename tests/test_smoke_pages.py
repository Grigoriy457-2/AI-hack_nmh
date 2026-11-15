"""Смоук HTML v2 по контракту (TZ_v2 «Экраны», CONTRACTS_v2 §5): /login, / (Радар), /stories,
/stories/{id}, /sources, /stats — 200 без трейсбэков и внешних CDN, ключевые элементы на месте,
формы без JS работают, пользовательский текст экранирован."""
from __future__ import annotations

import html as htmllib
import json
import re

import pytest

from app.ai import mock_data
from app.models import InvestigationStatus as S
from nf_helpers import TG_LIVE_TEXT, assert_clean_html, check, data, make_story

pytestmark = pytest.mark.smoke

XSS = '<script>alert("xss")</script><img src=x onerror=alert(1)>'


def _text(html: str) -> str:
    """Видимый текст + атрибуты, с раскрытыми сущностями (для поиска подписей)."""
    return htmllib.unescape(re.sub(r"\s+", " ", html))


def _norm_phone(s: str) -> str:
    return re.sub(r"[^\d+]", "", s)


def test_import_app_main():
    import app.main

    assert app.main.app.title


@pytest.mark.parametrize("path,ctype", [("/static/style.css", "css"), ("/static/app.js", "javascript")])
async def test_static(anon, path, ctype):
    r = check(await anon.get(path))
    assert ctype in r.headers.get("content-type", "")


# ================================================================ вход
async def test_login_page(anon):
    html = assert_clean_html(await anon.get("/login"))
    t = _text(html)
    assert "Войти как редактор" in t, "TZ_v2: /login с одной кнопкой «Войти как редактор»"
    assert "<form" in html.lower() and 'action="/login"' in html
    for role in ("Продюсер", "SMM", "AI-оператор", "Оператор"):
        assert role not in t, f"на /login остались роли v1: {role}"


async def test_login_page_redirects_when_logged_in(editor):
    r = check(await editor.get("/login"), 303)
    assert r.headers["location"] in ("/", "http://testserver/")


@pytest.mark.parametrize("with_user_id", [True, False])
async def test_login_form_post(anon, users, with_user_id):
    form = {"password": "demo"} | ({"user_id": str(users["editor2"])} if with_user_id else {})
    r = check(await anon.post("/login", data=form), 303)
    assert r.headers["location"] == "/"
    assert "nf_session" in r.headers.get("set-cookie", "")
    assert_clean_html(await anon.get("/"))
    me = data(await anon.get("/api/me"))
    assert me["id"] == (users["editor2"] if with_user_id else min(users.values()))


async def test_login_form_wrong_password(anon, users):
    r = await anon.post("/login", data={"user_id": str(users["editor"]), "password": "bad"})
    assert r.status_code in (200, 401), r.status_code
    assert "nf_session=" not in r.headers.get("set-cookie", "")
    assert "Traceback" not in r.text


async def test_login_form_next_param(anon, users):
    r = check(await anon.post("/login", data={"password": "demo", "next": "/stats"}), 303)
    assert r.headers["location"] == "/stats"
    r = check(await anon.post("/login", data={"password": "demo", "next": "//evil.example/x"}), 303)
    assert r.headers["location"] == "/", "open redirect запрещён"


async def test_logout_page(editor):
    r = check(await editor.get("/logout"), 303)
    assert r.headers["location"].endswith("/login")


# ================================================================ Радар
def _radar_data(html: str) -> dict:
    """Данные Радара для app.js: <script type="application/json" id="radar-data">."""
    m = re.search(r'<script[^>]*id="radar-data"[^>]*>(.*?)</script>', html, flags=re.S)
    assert m, "нет JSON-данных Радара (#radar-data)"
    return json.loads(m.group(1))


async def test_radar(editor, review_story):
    html = assert_clean_html(await editor.get("/"))
    t = _text(html)
    assert "Своя зацепка" in t, "поле «Своя зацепка»"
    assert 'name="lead_text"' in html and 'action="/stories/new"' in html, "форма своей зацепки без JS"
    assert "Возможные сюжеты" in t or "Возникающ" in t, "блок возникающих сюжетов"
    assert "Лента" in t
    rd = _radar_data(html)
    feed, cards = rd.get("feed") or {}, rd.get("emerging")
    m01 = mock_data.SCENARIO_MAX_MESSAGES[0]["text"]
    assert m01 in {m["text"] for m in feed.get("items", [])}, "лента сообщений в данных Радара"
    assert feed.get("cursor"), "курсор для догрузки /api/feed?since="
    assert cards and cards[0]["title"].startswith("Возможный инфоповод"), "карточки возникающих сюжетов"
    assert "QA: хлопок в Чертанове" in t, "мои сюжеты в работе"
    for removed in ("норм", "стрём", "Пульт оператора", "Доска"):
        assert removed not in t, f"на Радаре остался элемент v1: {removed}"


async def test_radar_with_telegram(editor, with_telegram):
    html = assert_clean_html(await editor.get("/"))
    assert "Telegram" in _text(html)
    items = _radar_data(html)["feed"]["items"]
    assert TG_LIVE_TEXT in {m["text"] for m in items if m["platform"] == "telegram"}


async def test_radar_emerging_investigate_form(editor):
    card = data(await editor.get("/api/emerging"))[0]
    html = assert_clean_html(await editor.get("/"))
    assert card["key"] in html, "кнопка «Исследовать» ссылается на ключ карточки"
    r = check(await editor.post(f"/emerging/{card['key']}/investigate"), 303)
    loc = r.headers["location"]
    assert re.fullmatch(r"/stories/\d+", loc), loc
    t = _text(assert_clean_html(await editor.get(loc)))
    assert "На проверке" in t
    r2 = check(await editor.post(f"/emerging/{card['key']}/investigate"), 303)
    assert r2.headers["location"] == loc, "повторное «Исследовать» ведёт на тот же сюжет"
    r3 = check(await editor.post("/emerging/e999999/investigate"), 303)
    assert r3.headers["location"].startswith("/")


async def test_own_lead_form(editor):
    r = check(await editor.post("/stories/new", data={"lead_text": "Хлопок у дома в Чертанове около 14:30"}), 303)
    loc = r.headers["location"]
    assert re.fullmatch(r"/stories/\d+", loc), loc
    assert_clean_html(await editor.get(loc))
    r = check(await editor.post("/stories/new", data={"lead_text": "a"}), 303)
    assert not re.fullmatch(r"/stories/\d+", r.headers["location"]), "короткая зацепка не создаёт сюжет"


# ================================================================ Сюжеты (список)
async def test_stories_list(editor, users, review_story):
    ho = await make_story(users, S.handed_off, title="QA переданный сюжет")
    rj = await make_story(users, S.rejected, title="QA отклонённый сюжет")
    t = _text(assert_clean_html(await editor.get("/stories")))
    for title in ("QA: хлопок в Чертанове", "QA переданный сюжет"):
        assert title in t
    # v4: «Не подтвердилось» уходит в архив
    assert "QA отклонённый сюжет" not in t
    t = t + _text(assert_clean_html(await editor.get("/stories?archived=1")))
    assert "QA отклонённый сюжет" in t
    for label in ("На проверке", "Передано дальше", "Не подтвердилось"):
        assert label in t
    t = _text(assert_clean_html(await editor.get("/stories", params={"status": "handed_off"})))
    assert "QA переданный сюжет" in t and "QA отклонённый сюжет" not in t
    assert f"/stories/{ho}" in assert_clean_html(await editor.get("/stories", params={"status": "handed_off"}))
    assert_clean_html(await editor.get("/stories", params={"status": "bogus"}))
    t = _text(assert_clean_html(await editor.get("/stories", params={"q": "отклонённый", "archived": "1"})))
    assert "QA отклонённый сюжет" in t and "QA переданный сюжет" not in t
    assert rj


# ================================================================ Сюжет
async def test_story_page_review(editor, review_story):
    html = assert_clean_html(await editor.get(f"/stories/{review_story['id']}"))
    t = _text(html)
    assert "QA: хлопок в Чертанове" in t and "На проверке" in t
    for label in ("Копать глубже", "Передать дальше", "Не подтвердилось", "Кому позвонить"):
        assert label in t, f"нет «{label}»"
    assert "+79990001122" in _norm_phone(html), "телефон автора виден редактору"
    assert "tel:" in html
    assert "Мария Скрытая" in t
    assert "Около 14:30 жители дома услышали хлопок" in t, "черновик / утверждения"
    assert "Двое пострадавших" in t, "справка (неподтверждённое)"
    assert "news.example.org" in t, "источники"
    for removed in ("Передать редактору", "Опубликовать", "норм", "стрём"):
        assert removed not in t, f"на странице сюжета остался элемент v1: {removed}"


@pytest.mark.parametrize("status", [S.new, S.researching, S.handed_off, S.rejected])
async def test_story_page_statuses(editor, users, status):
    inv = await make_story(users, status, title=f"QA статус {status.value}")
    t = _text(assert_clean_html(await editor.get(f"/stories/{inv}")))
    assert f"QA статус {status.value}" in t and status.label in t


async def test_story_page_handed_off_shows_handoff(editor, review_story):
    data(await editor.post(f"/api/stories/{review_story['id']}/handoff",
                           json={"to": "Ночная смена сайта", "channel": "tv", "comment": "QA-комментарий"}))
    t = _text(assert_clean_html(await editor.get(f"/stories/{review_story['id']}")))
    assert "Передано дальше" in t and "Ночная смена сайта" in t and "ТВ/радио" in t


async def test_story_page_after_pipeline(editor):
    inv = data(await editor.post("/api/stories", json={
        "lead_text": "В Чертанове около 14:30 хлопок у жилого дома, горит машина"}), 201)
    html = assert_clean_html(await editor.get(f"/stories/{inv['id']}"))
    t = _text(html)
    assert "Кому позвонить" in t and "tel:" in html
    contacts = data(await editor.get(f"/api/stories/{inv['id']}/contacts"))
    phone = next(c["phone"] for c in contacts if c["phone"])
    assert _norm_phone(phone) in _norm_phone(html)


async def test_story_page_404(editor):
    r = check(await editor.get("/stories/999999"), 404)
    assert "Traceback" not in r.text and "text/html" in r.headers.get("content-type", "")


async def test_legacy_investigation_redirect(editor, review_story):
    r = await editor.get(f"/investigations/{review_story['id']}")
    assert r.status_code in (301, 302, 303, 307, 308)
    assert r.headers["location"].endswith(f"/stories/{review_story['id']}")


# ================================================================ Источники / Статистика
async def test_sources_page(editor, with_telegram):
    html = assert_clean_html(await editor.get("/sources"))
    t = _text(html)
    for c in data(await editor.get("/api/sources/chats")):
        assert c["title"] in t, f"нет источника {c['title']}"
    assert "Telegram" in t and "MAX" in t
    assert 'action="/sources/add"' in html or "/api/sources" in html or "/api/telegram/sources" in html
    assert "публичн" in t.lower(), "юр. оговорка: подключаются только публичные источники"


async def test_sources_add_form(editor):
    r = check(await editor.post("/sources/add", data={"platform": "telegram", "ref": "@srochno_yug_demo"}), 303)
    assert r.headers["location"].startswith("/sources")
    t = _text(assert_clean_html(await editor.get("/sources")))
    assert "Срочно | Юг Москвы" in t
    r = check(await editor.post("/sources/add", data={"platform": "telegram", "ref": "https://t.me/+secret"}), 303)
    assert r.headers["location"].startswith("/sources")
    ids = {c["external_id"] for c in data(await editor.get("/api/sources/chats"))}
    assert not any("secret" in i for i in ids)


async def test_stats_page(editor, review_story):
    data(await editor.post(f"/api/stories/{review_story['id']}/handoff", json={"to": "Сайт", "channel": "site"}))
    t = _text(assert_clean_html(await editor.get("/stats")))
    assert "Передано дальше" in t or "Передано" in t
    assert "QA: хлопок в Чертанове" in t, "последние передачи"


# ================================================================ XSS / экранирование
async def test_story_text_is_escaped(editor):
    inv = data(await editor.post("/api/stories", json={
        "lead_text": "Зацепка " + XSS, "title": "XSS " + XSS, "autorun": False}), 201)
    for path in ("/", "/stories", f"/stories/{inv['id']}"):
        html = assert_clean_html(await editor.get(path))
        assert XSS not in html, f"{path}: неэкранированный HTML из lead_text/title"
        assert '<script>alert("xss")' not in html


async def test_feed_message_text_is_escaped(editor):
    """Текст сообщения из чата (недоверенный ввод) не ломает страницу, в т.ч. JSON в <script>."""
    from app import db
    from app.models import ChatMessage, MaxChat, utcnow

    payload = '</script><script>alert("feed")</script>'
    async with db.SessionLocal() as s:
        chat = (await s.scalars(__import__("sqlalchemy").select(MaxChat).limit(1))).first()
        s.add(ChatMessage(chat_id=chat.id, platform="max", external_id="qa-xss", author_external_id="u-xss",
                          author_name="<b>Хакер</b>", text="Хлопок " + payload, sent_at=utcnow()))
        await s.commit()
    html = assert_clean_html(await editor.get("/"))
    assert payload not in html, "текст сообщения ленты не экранирован"
    assert '<script>alert("feed")' not in html
    assert "<b>Хакер</b>" not in html
