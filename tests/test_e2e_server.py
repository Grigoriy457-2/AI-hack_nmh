"""E2E на настоящем процессе uvicorn в демо-режиме v2.

Режим как на защите: PIPELINE_INLINE=false (фоновая задача + демо-паузы), поллер источников
включён, Telegram-runner (mock), демо-трикл включён (ускорен: 0.4–0.8 с между сообщениями), SQLite-файл.
Проверяет: вход, живую ленту (трикл + SSE по-настоящему потоково), возникающий сюжет → «Исследовать»
→ живой прогресс → review → «Копать глубже» → review → «Передать дальше», перезапуск демо-потока,
отсутствие «database is locked» и трейсбэков в логе.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC", "MAX_", "TAVILY", "SERPER", "LLM_", "SEARCH_", "TG_", "DEMO_",
                                "PIPELINE_", "SSE_", "DATABASE_", "SEED_"))}
    env.update({
        "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp_path / 'server.db').as_posix()}",
        "PIPELINE_INLINE": "false",
        "MAX_POLLER_ENABLED": "true",
        "MAX_POLL_INTERVAL": "1",
        "SEED_ON_STARTUP": "true",
        "DEMO_STREAM_ENABLED": "true",
        "DEMO_STREAM_MIN_SEC": "0.4",
        "DEMO_STREAM_MAX_SEC": "0.8",
        "TG_PROVIDER": "mock",
        "TG_POLL_INTERVAL": "3",
        "SSE_POLL_SEC": "0.3",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(ROOT),
    })
    log = open(tmp_path / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(tmp_path), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            if proc.poll() is not None:
                break
            try:
                if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert proc.poll() is None, "uvicorn упал при старте:\n" + (tmp_path / "server.log").read_text("utf-8", "replace")
        yield base, tmp_path / "server.log"
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _wait(fn, timeout: float, step: float = 0.5, what: str = ""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(step)
    raise AssertionError(f"не дождались: {what} (последнее значение {last!r})")


def _sse_events(c: httpx.Client, seconds: float) -> list[tuple[str, dict, float]]:
    """Читать SSE потоково: (event, data, время прихода от начала)."""
    out: list[tuple[str, dict, float]] = []
    t0 = time.time()
    with c.stream("GET", "/api/stream", params={"timeout": seconds}, timeout=seconds + 10) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        ev, buf = None, []
        for line in r.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                buf.append(line[5:].strip())
            elif line == "":
                if ev and buf:
                    out.append((ev, json.loads("\n".join(buf)), time.time() - t0))
                ev, buf = None, []
    return out


def _wait_status(c: httpx.Client, iid: int, status: str, timeout: float = 120) -> list:
    states = []

    def done():
        p = c.get(f"/api/stories/{iid}/progress").json()
        states.append((p["progress"].get("state"), p["progress"].get("percent"), p["status"]))
        assert c.get(f"/stories/{iid}").status_code == 200  # параллельно — страница сюжета (как фронт)
        return p["status"] == status and p["progress"].get("state") == "done"

    _wait(done, timeout, 1.0, f"сюжет {iid} в {status}: {states[-3:]}")
    return states


def test_real_server_demo_flow(server):
    base, logfile = server
    with httpx.Client(base_url=base, timeout=30, follow_redirects=False) as c:
        # вход
        r = c.get("/login")
        assert r.status_code == 200 and "Traceback" not in r.text
        r = c.post("/login", data={"password": "demo"})
        assert r.status_code == 303
        assert c.get("/").status_code == 200
        assert c.get("/static/style.css").status_code == 200
        assert c.get("/static/app.js").status_code == 200

        # демо-трикл: лента растёт сама
        st = c.get("/api/demo/stream").json()
        assert st["enabled"] and st["active"], st
        first = _wait(lambda: c.get("/api/feed").json()["items"], 20, what="стартовые сообщения ленты")
        cursor = max(m["id"] for m in first)
        newer = _wait(lambda: c.get("/api/feed", params={"since": cursor}).json()["items"], 20,
                      what="новые сообщения трикла")
        assert all(m["id"] > cursor for m in newer)

        # SSE по-настоящему потоково: hello сразу, new_message приходят по ходу
        evs = _sse_events(c, 4)
        assert evs and evs[0][0] == "hello" and evs[0][2] < 2.0
        live = [e for e in evs if e[0] == "new_message"]
        assert live, f"за 4 с не пришло ни одного new_message: {[e[0] for e in evs]}"

        # Telegram (runner, mock) и возникающий сюжет
        _wait(lambda: any(m["platform"] == "telegram" for m in c.get("/api/feed", params={"limit": 500})
                          .json()["items"]), 30, what="сообщения Telegram в ленте")
        card = _wait(lambda: c.get("/api/emerging").json(), 60, 1.0, "карточка возникающего сюжета")[0]

        # «Исследовать» -> фон -> живой прогресс -> review
        r = c.post(f"/api/emerging/{card['key']}/investigate")
        assert r.status_code == 201, r.text
        inv = r.json()
        assert inv["status"] == "researching", "в демо-режиме ответ не ждёт пайплайн"
        states = _wait_status(c, inv["id"], "review")
        assert any(s == "running" for s, _, _ in states), f"живой прогресс не наблюдался: {states[:5]}"
        percents = [pc for _, pc, _ in states if pc is not None]
        assert percents == sorted(percents), f"percent растёт монотонно: {percents}"
        html = c.get(f"/stories/{inv['id']}").text
        assert "tel:" in html and "Traceback" not in html

        # «Копать глубже»
        r = c.post(f"/api/stories/{inv['id']}/deeper", json={"question": "Есть ли записи камер"})
        assert r.status_code == 200 and r.json()["status"] == "researching", r.text
        assert c.post(f"/api/stories/{inv['id']}/deeper", json={"question": "ещё"}).status_code == 409
        _wait_status(c, inv["id"], "review")

        # «Передать дальше»
        r = c.post(f"/api/stories/{inv['id']}/handoff", json={"to": "Ночная смена сайта", "channel": "site"})
        assert r.status_code == 200 and r.json()["story"]["status"] == "handed_off", r.text
        assert c.get("/stats").status_code == 200

        # перезапуск демо-потока
        r = c.post("/api/demo/restart")
        assert r.status_code == 200 and r.json()["epoch"] == 1, r.text
        _wait(lambda: c.get("/api/feed").json()["items"], 10, what="лента после перезапуска демо-потока")
        assert c.get(f"/api/stories/{inv['id']}/sources").json(), "сюжет сохранил источники после перезапуска"

    text = logfile.read_text("utf-8", "replace")
    assert "database is locked" not in text, "SQLite lock при параллельной работе поллера/пайплайна/SSE"
    assert "Traceback" not in text, "в логе сервера есть трейсбэк:\n" + text[-3000:]
