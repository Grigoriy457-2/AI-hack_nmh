"""E2E на настоящем процессе uvicorn (DoD п.11: «uvicorn app.main:app с SQLite стартует без ключей»).

Режим как в демо: PIPELINE_INLINE=false (фоновая задача + демо-паузы), MAX poller включён,
SQLite-файл. Проверяет живой опрос /progress и отсутствие «database is locked» при параллельной
работе poller + пайплайн + запросы UI.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from nf_helpers import CHERTANOVO_LEAD, require_pipeline

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
           if not k.startswith(("ANTHROPIC", "MAX_", "TAVILY", "SERPER", "LLM_", "SEARCH_"))}
    env.update({
        "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp_path / 'server.db').as_posix()}",
        "PIPELINE_INLINE": "false",
        "MAX_POLLER_ENABLED": "true",
        "MAX_POLL_INTERVAL": "2",
        "SEED_ON_STARTUP": "true",
        "PYTHONIOENCODING": "utf-8",
    })
    log = open(tmp_path / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(tmp_path), env={**env, "PYTHONPATH": str(ROOT)}, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
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


def test_real_server_demo_flow(server):
    require_pipeline()
    base, logfile = server
    with httpx.Client(base_url=base, timeout=30, follow_redirects=False) as c:
        r = c.get("/login")
        assert r.status_code == 200 and "Traceback" not in r.text
        users = {u["login"]: u["id"] for u in c.get("/api/users").json()}
        r = c.post("/login", data={"user_id": str(users["producer"]), "password": "demo"})
        assert r.status_code == 303
        assert c.get("/").status_code == 200
        assert c.get("/static/style.css").status_code == 200

        r = c.post("/api/investigations", json={"lead_text": CHERTANOVO_LEAD})
        assert r.status_code == 201, r.text
        inv = r.json()
        assert inv["status"] == "researching"

        states = []
        deadline = time.time() + 120
        while time.time() < deadline:
            p = c.get(f"/api/investigations/{inv['id']}/progress")
            assert p.status_code == 200, p.text
            st = p.json()["progress"].get("state")
            states.append((st, p.json()["progress"].get("percent")))
            # параллельно — страница расследования (как фронт)
            assert c.get(f"/investigations/{inv['id']}").status_code == 200
            if st in ("done", "error"):
                break
            time.sleep(1.5)
        final = p.json()
        assert final["progress"]["state"] == "done", f"{final['progress'].get('error')} / {states[-5:]}"
        assert final["status"] == "report_ready"
        assert any(s == "running" for s, _ in states), "живой прогресс не наблюдался"
        percents = [pc for _, pc in states if pc is not None]
        assert percents == sorted(percents), f"percent должен расти монотонно: {percents}"
        html = c.get(f"/investigations/{inv['id']}", params={"tab": "contacts"}).text
        assert "tel:" in html

    text = logfile.read_text("utf-8", "replace")
    assert "database is locked" not in text, "SQLite lock при параллельной работе poller/пайплайна"
    assert "Traceback" not in text, "в логе сервера есть трейсбэк:\n" + text[-3000:]
