"""Утилиты QA v2: строгая проверка ответов, разбор SSE, построение тестовых данных прямо в БД."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import httpx

from app import db
from app.models import (
    AuthorProfile,
    ChatMessage,
    Draft,
    DraftAuthorKind,
    Investigation,
    InvestigationStatus,
    MaxChat,
    Report,
    Source,
    SourceKind,
    StatusHistory,
    utcnow,
)

S = InvestigationStatus

# Порядок разделов справки (CONTRACTS.md §5, без изменений в v2).
REPORT_SECTION_KEYS = [
    "summary", "timeline", "facts", "sources", "confirmed",
    "unconfirmed", "contradictions", "media", "to_verify",
]
HANDOFF_CHANNEL_KEYS = ["site", "telegram", "max", "tv", "print", "social", "other"]

CHERTANOVO_LEAD = (
    "В Чертанове около 14:30 у жилого дома на Варшавском шоссе был слышен хлопок, "
    "жители пишут в домовых чатах про выбитые окна. Не подтверждено."
)
TG_LIVE_TEXT = (
    "⚡️В Чертанове прогремел взрыв у жилого дома. Жители сообщают о громком хлопке около 14:30, "
    "во дворе дым. Предварительно — взрыв газа."
)


# ---------------------------------------------------------------- ответы
def check(resp: httpx.Response, expected: int = 200) -> httpx.Response:
    assert resp.status_code == expected, (
        f"{resp.request.method} {resp.request.url} -> {resp.status_code} "
        f"(ожидали {expected}): {resp.text[:1500]}"
    )
    return resp


def data(resp: httpx.Response, expected: int = 200) -> Any:
    return check(resp, expected).json()


def detail_text(resp: httpx.Response) -> str:
    try:
        d = resp.json().get("detail")
    except Exception:  # noqa: BLE001
        return ""
    return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)


_TRACE_MARKERS = ("Traceback (most recent call last)", "Internal Server Error", "jinja2.exceptions",
                  "UndefinedError", "Not implemented yet")


def assert_clean_html(resp: httpx.Response) -> str:
    check(resp, 200)
    ctype = resp.headers.get("content-type", "")
    assert "text/html" in ctype, f"{resp.request.url}: content-type={ctype}"
    html = resp.text
    for m in _TRACE_MARKERS:
        assert m not in html, f"{resp.request.url}: в HTML найдено {m!r}"
    assert "<html" in html.lower(), f"{resp.request.url}: нет <html>"
    host = resp.request.url.host
    ext = [u for u in re.findall(r"<(?:script|link)[^>]+?(?:src|href)=[\"'](https?://[^\"']+)", html, flags=re.I)
           if httpx.URL(u).host != host]
    assert not ext, f"{resp.request.url}: внешние ресурсы {ext}"
    return html


# ---------------------------------------------------------------- SSE
def parse_sse(text: str) -> list[dict[str, Any]]:
    """text/event-stream -> [{"event", "id", "data"(json|str), "comment"}]."""
    events: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", text):
        if not block.strip():
            continue
        ev: dict[str, Any] = {"event": None, "id": None, "data": None, "comment": None, "retry": None}
        lines = []
        for line in block.splitlines():
            if line.startswith(":"):
                ev["comment"] = line[1:].strip()
            elif line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("id:"):
                ev["id"] = line[3:].strip()
            elif line.startswith("retry:"):
                ev["retry"] = line[6:].strip()
            elif line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))
        if lines:
            raw = "\n".join(lines)
            try:
                ev["data"] = json.loads(raw)
            except ValueError:
                ev["data"] = raw
        events.append(ev)
    return events


# ---------------------------------------------------------------- построение данных
async def make_story(
    users: dict[str, int],
    status: InvestigationStatus = S.new,
    *,
    owner: str = "editor",
    minutes_ago: int = 0,
    title: str = "Тестовая зацепка",
    lead_text: str = "Тестовая зацепка: что-то случилось у дома 5",
) -> int:
    """Сюжет в заданном статусе без артефактов (для проверок переходов)."""
    ts = utcnow() - timedelta(minutes=minutes_ago)
    async with db.SessionLocal() as s:
        inv = Investigation(
            title=title, lead_text=lead_text, status=status, owner_id=users[owner], origin="manual",
            created_at=ts, updated_at=ts, signal_at=ts,
            review_at=ts if status not in (S.new, S.researching) else None,
        )
        s.add(inv)
        await s.flush()
        s.add(StatusHistory(investigation_id=inv.id, from_status=None, to_status=status,
                            user_id=users[owner], created_at=ts))
        await s.commit()
        return inv.id


async def build_review_story(users: dict[str, int], status: InvestigationStatus = S.review) -> dict:
    """Сюжет в review с полным набором артефактов по контрактам.

    Источники:
      s1  max_chat primary, uniq 1.0, important, автор С телефоном      -> контакт
      s2  max_chat дубль s1 (кластер 1), не primary, uniq 0.0           -> нет
      s3  max_chat primary, uniq 0.9, important, автор БЕЗ телефона     -> контакт (phone=None)
      s4  max_chat primary, uniq 0.3, important, телефон есть           -> нет (uniq < 0.5)
      s5  max_chat primary, uniq 1.0, НЕ important, телефон есть        -> нет
      s6  web      primary, uniq 1.0, important                          -> нет (не чат)
    Справка: confirmed — 1 пункт, unconfirmed — 1 пункт (state unconfirmed), contradictions — пусто.
    Черновик v1 (AI): claims c1 (unconfirmed), c2 (confirmed).
    """
    now = utcnow()
    t0 = now - timedelta(hours=1)
    async with db.SessionLocal() as s:
        chat = MaxChat(platform="max", external_id="qa-chat-1", title="QA: ЖК Чертаново Центральное",
                       district="Чертаново Центральное", enabled=True)
        s.add(chat)
        await s.flush()

        s.add_all([
            AuthorProfile(external_id="qa-u-phone", name="Иван Тестов", phone="+79990001122", username="ivan_qa"),
            AuthorProfile(external_id="qa-u-nophone", name="Мария Скрытая", phone=None, username="maria_qa"),
            AuthorProfile(external_id="qa-u-dup", name="Пётр Пересыльщик", phone="+79990003344"),
            AuthorProfile(external_id="qa-u-low", name="Лев Низкий", phone="+79990005566"),
            AuthorProfile(external_id="qa-u-unimp", name="Ольга Болтун", phone="+79990007788"),
        ])
        # время сообщений — в далёком прошлом, чтобы не вмешиваться в ленту/возникающие сюжеты
        old = now - timedelta(days=3)
        msg_specs = [
            ("qa-m1", "qa-u-phone", "Иван Тестов", 0,
             "QA: видел вспышку у дома 5 по Варшавскому шоссе в 14:30, выбило окна на 3 этаже", True),
            ("qa-m2", "qa-u-dup", "Пётр Пересыльщик", 9,
             "QA: видел вспышку у дома 5 по Варшавскому шоссе в 14:30, выбило окна на 3 этаже", False),
            ("qa-m3", "qa-u-nophone", "Мария Скрытая", 4,
             "QA: около 14:30 хлопок во дворе, двое пострадавших, приехали две скорые", False),
            ("qa-m4", "qa-u-low", "Лев Низкий", 6, "QA: говорят у дома 5 что-то бахнуло в 14:30", False),
            ("qa-m5", "qa-u-unimp", "Ольга Болтун", 7, "QA: кто-нибудь знает что за шум был?", False),
        ]
        msgs: dict[str, ChatMessage] = {}
        for ext, author, name, dmin, text, media in msg_specs:
            m = ChatMessage(chat_id=chat.id, platform="max", external_id=ext, author_external_id=author,
                            author_name=name, text=text, sent_at=old + timedelta(minutes=dmin),
                            has_media=media, media_url="https://example.org/qa.jpg" if media else None,
                            is_forwarded=(ext == "qa-m2"), archived=True)
            s.add(m)
            msgs[ext] = m

        inv = Investigation(title="QA: хлопок в Чертанове", lead_text=CHERTANOVO_LEAD, status=status,
                            owner_id=users["editor"], origin="manual",
                            created_at=t0, updated_at=now, signal_at=t0, review_at=now,
                            entities_json={"summary": "Хлопок у жилого дома в Чертанове",
                                           "keywords": ["хлопок", "Чертаново"], "status": "unconfirmed"},
                            progress_json={"state": "done", "percent": 100, "current_stage": None,
                                           "stages": [], "log": []})
        s.add(inv)
        await s.flush()

        def src(ext: str | None, kind: SourceKind, cluster: int, uniq: float, imp: bool,
                primary: bool, title: str, url: str) -> Source:
            m = msgs.get(ext) if ext else None
            return Source(investigation_id=inv.id, kind=kind, title=title, url=url,
                          snippet=m.text if m else "Новостной сайт: хлопок в Чертанове",
                          published_at=m.sent_at if m else t0,
                          chat_message_id=m.id if m else None, iteration_n=1, query="хлопок Чертаново",
                          cluster_id=cluster, uniqueness_score=uniq, is_important=imp, is_primary=primary)

        sources = {
            "s1": src("qa-m1", SourceKind.max_chat, 1, 1.0, True, True, "Иван Тестов", "max://chat/qa-chat-1/qa-m1"),
            "s2": src("qa-m2", SourceKind.max_chat, 1, 0.0, True, False, "Пётр Пересыльщик",
                      "max://chat/qa-chat-1/qa-m2"),
            "s3": src("qa-m3", SourceKind.max_chat, 2, 0.9, True, True, "Мария Скрытая", "max://chat/qa-chat-1/qa-m3"),
            "s4": src("qa-m4", SourceKind.max_chat, 3, 0.3, True, True, "Лев Низкий", "max://chat/qa-chat-1/qa-m4"),
            "s5": src("qa-m5", SourceKind.max_chat, 4, 1.0, False, True, "Ольга Болтун",
                      "max://chat/qa-chat-1/qa-m5"),
            "s6": src(None, SourceKind.web, 5, 1.0, True, True, "Новости района", "https://news.example.org/chertanovo"),
        }
        s.add_all(sources.values())
        await s.flush()
        sid = {k: v.id for k, v in sources.items()}

        def item(i, text, ids, state=None):
            return {"id": i, "text": text, "source_ids": ids, "state": state, "time": None}

        report_json = {
            "version": 1, "generated_at": now.isoformat(), "title": "Хлопок у жилого дома в Чертанове",
            "sections": [
                {"key": "summary", "title": "Коротко", "text": "Около 14:30 хлопок.", "items": []},
                {"key": "timeline", "title": "Хронология", "items": [item("timeline-1", "14:30 — вспышка", [sid["s1"]])]},
                {"key": "facts", "title": "Что известно", "text": "",
                 "items": [item("facts-1", "Хлопок около 14:30 у дома 5", [sid["s1"], sid["s6"]])]},
                {"key": "sources", "title": "Ключевые источники", "items": [item("sources-1", "Чат ЖК", [sid["s1"], sid["s3"]])]},
                {"key": "confirmed", "title": "Подтверждено",
                 "items": [item("confirmed-1", "Хлопок был", [sid["s1"], sid["s6"]], "confirmed")]},
                {"key": "unconfirmed", "title": "Не подтверждено / слухи",
                 "items": [item("unconfirmed-1", "Двое пострадавших", [sid["s3"]], "unconfirmed")]},
                {"key": "contradictions", "title": "Противоречия", "items": []},
                {"key": "media", "title": "Медиа", "items": []},
                {"key": "to_verify", "title": "Что проверить и кому позвонить",
                 "items": [item("to_verify-1", "Позвонить Ивану", [sid["s1"]])]},
            ],
        }
        s.add(Report(investigation_id=inv.id, report_json=report_json, created_at=now))

        body = ("⚡️ХЛОПОК В ЧЕРТАНОВЕ\n\nОколо 14:30 жители дома услышали хлопок. "
                "По словам очевидцев, выбило окна на 3 этаже. Информация о пострадавших уточняется.")
        claims = [
            {"id": "c1", "text": "Около 14:30 жители дома услышали хлопок", "source_ids": [sid["s1"]],
             "status": "unconfirmed", "note": None},
            {"id": "c2", "text": "выбило окна на 3 этаже", "source_ids": [sid["s1"], sid["s3"]],
             "status": "confirmed", "note": None},
        ]
        s.add(Draft(investigation_id=inv.id, version=1, author_kind=DraftAuthorKind.ai,
                    title="ХЛОПОК В ЧЕРТАНОВЕ", body=body, claims_json=claims, created_at=now))
        s.add_all([
            StatusHistory(investigation_id=inv.id, from_status=None, to_status=S.new,
                          user_id=users["editor"], created_at=t0),
            StatusHistory(investigation_id=inv.id, from_status=S.new, to_status=S.researching,
                          user_id=users["editor"], created_at=t0),
            StatusHistory(investigation_id=inv.id, from_status=S.researching, to_status=S.review,
                          user_id=None, created_at=now),
        ])
        await s.commit()
        return {"id": inv.id, "sources": sid, "chat_id": chat.id, "body": body, "claims": claims}
