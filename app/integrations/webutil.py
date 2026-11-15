"""Общие утилиты для внешних HTTP-запросов (зона DEV-REALDATA).

- ``http_client()`` — httpx.AsyncClient с вежливым User-Agent (``settings.HTTP_USER_AGENT``, без
  персональных данных), таймаутом и редиректами.
- ``TTLCache`` — простой in-memory кэш с TTL (ответы поиска, ленты, страницы).
- ``strip_html`` / ``parse_meta`` (og:-теги, <title>, JSON-LD) / ``parse_rfc822`` (даты RSS).
Время везде — naive UTC.
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v ]+")
_META_RE = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z_:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_LDJSON_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.I | re.S)


def user_agent() -> str:
    from app.config import settings

    return settings.HTTP_USER_AGENT


def http_client(timeout: float | None = None, **kw: Any) -> httpx.AsyncClient:
    from app.config import settings

    headers = {"User-Agent": user_agent(), "Accept-Language": "ru,en;q=0.7"}
    headers.update(kw.pop("headers", {}) or {})
    return httpx.AsyncClient(timeout=timeout or settings.HTTP_TIMEOUT, follow_redirects=True,
                             headers=headers, **kw)


class TTLCache:
    def __init__(self, ttl: float, maxsize: int = 512) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._d: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        v = self._d.get(key)
        if v is None:
            return None
        if time.monotonic() - v[0] > self.ttl:
            self._d.pop(key, None)
            return None
        return v[1]

    def set(self, key: Any, value: Any) -> None:
        if len(self._d) >= self.maxsize:
            for k in sorted(self._d, key=lambda k: self._d[k][0])[: self.maxsize // 4]:
                self._d.pop(k, None)
        self._d[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._d.clear()


def strip_html(s: str | None) -> str:
    """HTML-фрагмент → чистый текст (сущности раскрыты, пробелы схлопнуты)."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>|</p>", "\n", s, flags=re.I)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def parse_rfc822(s: str | None) -> datetime | None:
    """Дата RSS (``Sun, 15 Nov 2026 08:40:47 +0300``, в т.ч. с двузначным годом) или ISO (Atom)."""
    if not s:
        return None
    s = s.strip()
    try:
        return to_naive_utc(parsedate_to_datetime(s))
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return to_naive_utc(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_meta(page: str) -> dict[str, Any]:
    """og:/twitter:/name-мета-теги, <title> и JSON-LD страницы.

    ``{"og:title": ..., "og:image": ..., "description": ..., "title": ..., "ld": [obj, ...]}``."""
    out: dict[str, Any] = {}
    head = page[:200_000]
    for tag in _META_RE.findall(head):
        attrs = {m.group(1).lower(): html.unescape(m.group(3) if m.group(3) is not None else m.group(4) or "")
                 for m in _ATTR_RE.finditer(tag)}
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs and key.lower() not in out:
            out[key.lower()] = attrs["content"].strip()
    m = _TITLE_RE.search(head)
    if m:
        out["title"] = html.unescape(m.group(1)).strip()
    ld = []
    for raw in _LDJSON_RE.findall(head):
        try:
            ld.append(json.loads(raw))
        except ValueError:
            continue
    out["ld"] = ld
    return out


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        h = (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h
