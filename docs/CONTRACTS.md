# NEWSFLOW AI — контракты (шпаргалка)

Главный документ — `docs/TZ.md`. Здесь — то, о чём команда договорилась и что нельзя менять молча.
Изменить контракт можно только через раздел «Запросы на изменение контракта» в конце файла
(решение принимает тимлид).

## 1. Зоны ответственности (кто какие файлы правит)

| Зона | Файлы |
|---|---|
| **TEAM LEAD** (общие, правки только через запрос) | `app/__init__.py`, `app/config.py`, `app/db.py`, `app/models.py`, `app/schemas.py`, `app/auth.py`, `app/main.py`, `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `docs/*`, таблица `ALLOWED_TRANSITIONS` в `workflow.py` |
| **DEV-AI** | `app/ai/*` (`llm.py`, `prompts.py`, `mock_data.py`, `channel_dna.py`), `app/integrations/search.py`, `app/services/pipeline.py`, `app/services/uniqueness.py` |
| **DEV-BACKEND** | `app/integrations/max/*` (`client.py`, `bot_api.py`, `mock.py`, `poller.py`), `app/services/workflow.py` (функции), `app/services/metrics.py`, `app/routers/api.py`, `app/seed.py` |
| **DEV-FRONTEND** | `app/routers/pages.py`, `app/templates/*`, `app/static/*` |
| **QA** | `tests/*`, `pytest.ini` |

Правило: в чужой файл не коммитим. Нужна новая функция/поле — пишем запрос (раздел 9).
Новые файлы внутри своей зоны создавать можно (например `app/templates/investigation/_tab_sources.html`).

## 2. Общие правила кода

- Python 3.12, `from __future__ import annotations`, async везде (SQLAlchemy 2.0 async).
- Время: **naive UTC**, только `app.models.utcnow()`. В UI переводит фронт (МСК = UTC+3).
- Сессии: в роутерах `Depends(get_session)`; в фоне — `async with db.session_scope() as s:`.
  Фабрику брать через модуль: `from app import db; db.SessionLocal()` (тесты зовут `db.configure(url)`).
- Enum-ы (`app.models`): `Role` (producer, editor, smm, operator), `InvestigationStatus`
  (new, researching, report_ready, editing, ready, published, rejected), `SourceKind`
  (web, max_chat, official, media), `MediaKind` (photo, video), `DraftAuthorKind` (ai, editor),
  `FactState` (confirmed, unconfirmed, false), `Verdict` (good, bad). В БД хранится `.value`.
  Русские подписи: `ROLE_LABELS`, `STATUS_LABELS`, свойство `.label`.
- JSON-поля ORM при изменении присваивать **новым объектом** (`inv.progress_json = {...}`), не мутировать.
- Внешние провайдеры только через фабрики: `get_llm()`, `get_search_provider()`, `get_max_client()`;
  в тестах подмена через `set_llm()`, `set_search_provider()`, `set_max_client()`.
- Без ключей всё работает на mock (LLM_PROVIDER/SEARCH_PROVIDER/MAX_PROVIDER по умолчанию auto/mock).
- Незаконченный эндпоинт кидает `NotImplementedError` → main.py отдаёт 501.

## 3. Авторизация (`app/auth.py`, готово)

- Cookie `nf_session` (itsdangerous, подпись `SECRET_KEY`), payload `{"uid": int}`.
- Пароль всех seed-пользователей — `demo` (`verify_password`).
- `current_user_optional` → `User|None`; `current_user` → 401; `require_role(*roles)` → 403,
  **operator проходит всегда**; `has_role(user, *roles)` — для шаблонов/логики.
- `set_login_cookie(response, user)`, `clear_login_cookie(response)`.
- 401 на НЕ-`/api` пути → редирект 303 на `/login` (обработчик в main.py).
- Seed-логины: `producer`, `producer2`, `editor`, `smm`, `operator`.

## 4. Сигнатуры сервисов

```python
# app/ai/llm.py (DEV-AI)
class LLMClient(ABC):
    async def complete_json(system, user, schema_hint=None, *, task=None, temperature=0.2, max_tokens=None) -> dict
    async def complete_text(system, user, *, task=None, temperature=0.5, max_tokens=None) -> str
get_llm() -> LLMClient          # AnthropicLLM (ключ есть) | MockLLM
# task-ключи: extract_entities, chain_hypotheses, relevance, importance, intelligence, media_relation, report, draft

# app/ai/channel_dna.py (DEV-AI)
analyze_posts(posts: list[str]) -> dict                         # dna_json
async ensure_channel_dna(session, channel, *, force=False) -> dict   # без commit

# app/integrations/search.py (DEV-AI)
@dataclass SearchResult(title, url, snippet, published_at=None, source_type="web", media_urls=[], extra={})
class SearchProvider: async search(query, *, limit=10, domains=None, source_type=None) -> list[SearchResult]
get_search_provider() -> SearchProvider; OFFICIAL_DOMAINS

# app/integrations/max/client.py (DEV-BACKEND)
@dataclass MaxChat(external_id, title, district=None, members_count=None)
@dataclass MaxMessage(external_id, chat_external_id, author_external_id, author_name, text, sent_at, has_media=False, media_url=None, is_forwarded=False)
@dataclass MaxUserProfile(external_id, name, phone=None, username=None)
class MaxClient: async list_chats(); async fetch_messages(chat_id, since=None, limit=100); async get_user_profile(user_id) -> MaxUserProfile|None
get_max_client() -> MaxClient   # MaxBotApiClient (токен есть) | MockMaxClient

# app/integrations/max/poller.py (DEV-BACKEND)
async sync_once(session, client=None) -> schemas.SyncResult      # commit внутри
async ensure_author_profile(session, author_external_id, client=None) -> AuthorProfile|None   # без commit
start_poller(interval: int) -> asyncio.Task

# app/services/uniqueness.py (DEV-AI) — чистые функции
normalize_text(text) -> str; shingles(text, n=3) -> set[str]; jaccard(a, b) -> float
cluster_messages(items: Sequence[UniqItem], threshold=0.6) -> list[Cluster]
is_important_heuristic(text, has_media=False) -> bool
score_uniqueness(items, clusters=None, importance=None, threshold=0.6) -> dict[id, UniquenessResult]
select_unique_important(results, min_uniqueness=0.5) -> list[id]

# app/services/pipeline.py (DEV-AI)
STAGES = [("input",…),("search",…),("intelligence",…),("media",…),("dna",…),("report",…)]
start_investigation(investigation_id) -> asyncio.Task    # фон
async run_investigation(investigation_id) -> None        # сам открывает сессии, не бросает исключений

# app/services/workflow.py (DEV-BACKEND; таблица — тимлид)
ALLOWED_TRANSITIONS: dict[from, dict[to, frozenset[Role|None]]]   # None = система
can_transition(role|None, from, to) -> bool; available_transitions(role|None, from) -> list[status]
async transition(session, inv, to, user|None, comment=None) -> StatusHistory   # flush, без commit
# исключения: TransitionNotAllowed (→409), TransitionForbidden (→403)

# app/services/metrics.py (DEV-BACKEND)
async compute_metrics(session) -> dict   # ключи METRIC_KEYS == schemas.MetricsOut
```

### Переходы статусов (`ALLOWED_TRANSITIONS`)

| Из → В | Кто | Кнопка |
|---|---|---|
| new → researching | producer | «Запустить NEWSFLOW» |
| new → rejected | producer | «Отклонить» |
| researching → report_ready | система | (пайплайн завершён) |
| researching → new | система | (фатальная ошибка, опц.) |
| report_ready → editing | producer | «Передать на редактуру» |
| report_ready → rejected | producer | «Стрём / не подтвердилось» |
| report_ready → researching | producer | «Перезапустить исследование» |
| editing → report_ready | editor | «Вернуть продюсеру» |
| editing → ready | editor | «Готово для SMM» |
| ready → published | smm | «Опубликовано» |
| ready → editing | smm | «Вернуть на доработку» |
| rejected → new | producer | «Вернуть в работу» |

Operator может любой переход из таблицы. Каждый переход → строка `status_history`.

## 5. JSON-форматы

### `investigations.entities_json`
```json
{"summary": "Хлопок у жилого дома в Чертанове около 14:30",
 "event_type": "хлопок/взрыв", "object": "жилой дом",
 "location": {"text": "Чертаново, Варшавское ш.", "district": "Чертаново Центральное", "address": null},
 "time": {"text": "около 14:30", "iso": "2026-11-14T11:30:00"},
 "keywords": ["хлопок", "Чертаново", "взрыв газа"],
 "status": "unconfirmed"}
```

### `investigations.progress_json` (отдаётся в `GET /api/investigations/{id}/progress`)
```json
{"state": "running",                 // idle | running | done | error
 "percent": 45, "current_stage": "search",
 "started_at": "2026-11-14T11:31:02", "finished_at": null, "error": null,
 "stages": [{"key": "input", "title": "Разбор зацепки", "status": "done",   // pending|running|done|error
             "started_at": "…", "finished_at": "…", "note": "найдено 5 сущностей"}],
 "log": [{"ts": "2026-11-14T11:31:03", "stage": "search", "level": "info",   // info|warn|error
          "message": "Итерация 1: 4 запроса, 17 результатов"}]}
```
Фронт опрашивает раз в 1.5 с, пока `state` не `done`/`error`, затем перезагружает страницу.

### `search_iterations.queries_json`
```json
[{"agent": "web", "query": "хлопок Чертаново 14:30", "results": 6, "new": 3},
 {"agent": "max_chats", "query": "хлопок OR взрыв Чертаново", "results": 9, "new": 9}]
```
`results` — сколько вернул агент, `new` — сколько новых источников добавлено (дедуп по url /
сообщению между итерациями); `search_iterations.results_count` = сумма `new`.
`rationale` — текст LLM, почему такие гипотезы (для 2-й и 3-й итерации — «что нашли и что ищем дальше»).

### `reports.report_json` (9 разделов исходного плана, порядок фиксирован) — ред. DEV-AI 2026-11-14
```json
{"version": 1, "generated_at": "…", "title": "Возгорание автомобиля у жилого дома в Чертанове",
 "sections": [
   {"key": "summary",        "title": "Краткое описание ситуации",      "text": "…", "items": []},
   {"key": "timeline",       "title": "Хронология",                     "text": "", "items": [ITEM]},
   {"key": "facts",          "title": "Найденная информация",           "text": "", "items": [ITEM]},
   {"key": "sources",        "title": "Источники",                      "text": "Ключевые источники. Всего найдено: 34…", "items": [ITEM]},
   {"key": "confirmed",      "title": "Подтверждённые сведения",        "text": "", "items": [ITEM]},
   {"key": "unconfirmed",    "title": "Неподтверждённая информация",    "text": "", "items": [ITEM]},
   {"key": "contradictions", "title": "Противоречия",                   "text": "", "items": [ITEM]},
   {"key": "media",          "title": "Фото и видео",                   "text": "", "items": [ITEM]},
   {"key": "to_verify",      "title": "Что ещё необходимо проверить",   "text": "", "items": [ITEM]}
 ],
 "contacts": [CONTACT],          // блок «Кому позвонить» (без телефонов!)
 "stats": {"iterations": 3, "sources_total": 34, "primary": 27, "duplicates": 7,
           "by_kind": {"web": 6, "max_chat": 22, "official": 3, "media": 3},
           "media": 4, "possibly_old_media": 1, "contacts": 8},
 "channel": "Москва Срочно"}     // или null
```
`ITEM = {"id": "timeline-1", "text": "14:31 — жители сообщают о хлопке", "source_ids": [12, 15],
"state": "confirmed|unconfirmed|false|null", "time": "14:31"|null}` + необязательные поля:
`claim` (confirmed/unconfirmed — формулировка для поста); в `sources`: `kind`, `url`, `published_at`,
`uniqueness`, `duplicates` (размер кластера − 1), `is_primary`, `note` (первая фраза сниппета);
в `media`: `media_id` (`media_items.id`), `possibly_old` (у «старых» `state="false"`).
`source_ids` — `sources.id` этого расследования. Пустые разделы допустимы (`items: []`).
Раздел `sources` — ключевые первоисточники (официальные, СМИ/ТГ, медиа + 2 важных чата, ≤ 12);
полный список с дублями — `GET /sources`. В `to_verify` в конце — до 4 строк «Позвонить очевидцу: …».

`CONTACT = {"source_id", "author_external_id", "name", "has_phone": bool, "username", "quote",
"chat_title", "sent_at" (UTC ISO), "time_msk": "14:31", "message_url", "uniqueness_score", "messages": 2}` —
один на автора (первое уникальное важное сообщение), сначала с телефоном. **Номер телефона в
report_json НЕ кладётся** (справку видят все роли); телефон отдаёт только `GET /contacts`
(producer/operator) из `author_profiles` — профили к этому моменту уже запрошены пайплайном.

### `drafts.claims_json`
```json
[{"id": "c1", "text": "Около 14:30 жители дома услышали хлопок",
  "source_ids": [12, 15], "status": "unconfirmed", "note": null}]
```
- `text` — точная подстрока `body` (фронт подсвечивает через поиск подстроки; если редактор
  изменил текст и подстроки нет — утверждение показывается в списке как «изменено редактором»).
- `status`: confirmed | unconfirmed | false; итоговый статус в UI = последняя `fact_marks` с тем же
  `claim_id`, иначе `status` из claims. Неподтверждённые/ложные — подсветка.
- Draft v1 — AI (`author_kind=ai`), правки редактора — v2, v3… (`author_kind=editor`).

### `channels.dna_json`
```json
{"posts_analyzed": 12, "avg_length": 340, "avg_paragraphs": 2.5,
 "emoji_per_post": 1.8, "top_emojis": ["⚡️", "❗️"],
 "headline": {"style": "caps", "caps_share": 0.7, "emoji_prefix_share": 0.6, "avg_words": 7.0},
 "uses_hashtags": true, "top_hashtags": ["#москва"],
 "structure": ["headline", "lead", "details", "hashtags"],
 "tone": "срочный",
 "rules": ["Заголовок капсом, начинается с ⚡️", "2–3 коротких абзаца", "В конце 1–2 хэштега"]}
```
`style`: caps | emoji_caps | plain | question. `tone`: срочный | нейтральный | аналитический.

### «Кому позвонить» (`ContactOut`)
Источник `kind=max_chat`, `is_primary=True`, `uniqueness_score>=0.5`, `is_important=True` +
`author_profiles`. `phone=None` → «телефон скрыт в профиле». `message_url` — `sources.url`
(для MAX: `max://chat/{chat_external_id}/{message_external_id}`). Видят только producer и operator.

## 6. JSON API (`app/routers/api.py`, prefix `/api`)

Роли: P=producer, E=editor, S=smm, O=operator (O проходит всегда). «все» = любой вошедший.

| Метод | Путь | Вход | Выход | Роли |
|---|---|---|---|---|
| GET | `/api/health` | — | `{"status":"ok"}` | публичный |
| GET | `/api/users` | — | `list[UserOut]` | публичный |
| POST | `/api/auth/login` | `LoginIn` | `UserOut` + cookie | публичный |
| POST | `/api/auth/logout` | — | 204 | все |
| GET | `/api/me` | — | `UserOut` | все |
| GET | `/api/channels` | — | `list[ChannelOut]` | все |
| POST | `/api/channels/{id}/dna` | — | `ChannelOut` | O |
| GET | `/api/investigations?status=&mine=` | — | `list[InvestigationOut]` | все |
| POST | `/api/investigations` | `InvestigationCreate` | `InvestigationOut` 201 | P |
| GET | `/api/investigations/{id}` | — | `InvestigationDetail` | все |
| POST | `/api/investigations/{id}/run` | — | `InvestigationOut` | P |
| GET | `/api/investigations/{id}/progress` | — | `ProgressOut` | все |
| GET | `/api/investigations/{id}/iterations` | — | `list[SearchIterationOut]` | все |
| GET | `/api/investigations/{id}/sources?kind=&primary_only=` | — | `list[SourceOut]` | все |
| GET | `/api/investigations/{id}/contacts` | — | `list[ContactOut]` | P |
| GET | `/api/investigations/{id}/media` | — | `list[MediaItemOut]` | все |
| GET | `/api/investigations/{id}/report` | — | `ReportOut` (404 если нет) | все |
| GET | `/api/investigations/{id}/drafts` | — | `list[DraftOut]` | все |
| POST | `/api/investigations/{id}/drafts` | `DraftCreate` | `DraftOut` 201 | E (статус editing) |
| GET | `/api/investigations/{id}/drafts/diff?base=&target=` | — | `DraftDiffOut` | все |
| GET | `/api/investigations/{id}/facts` | — | `list[FactMarkOut]` | все |
| POST | `/api/investigations/{id}/facts` | `FactMarkIn` | `FactMarkOut` 201 | P, E |
| POST | `/api/investigations/{id}/transition` | `TransitionIn` | `InvestigationOut` | по workflow (403/409) |
| GET | `/api/investigations/{id}/history` | — | `list[StatusHistoryOut]` | все |
| POST | `/api/investigations/{id}/feedback` | `FeedbackIn` | `FeedbackOut` 201 | P |
| GET | `/api/feedback` | — | `list[FeedbackOut]` | O |
| GET | `/api/operator/board` | — | `BoardOut` | O |
| GET | `/api/metrics` | — | `MetricsOut` | O |
| GET | `/api/max/chats` | — | `list[MaxChatOut]` | O |
| PATCH | `/api/max/chats/{id}` | `MaxChatUpdate` | `MaxChatOut` | O |
| POST | `/api/max/sync` | — | `SyncResult` | O |

Коды: 401 не вошёл, 403 нет прав, 404 нет объекта, 409 недопустимый переход/состояние, 422 валидация, 501 не реализовано.
При `PIPELINE_INLINE=true` `POST /investigations` (autorun) и `/run` ждут окончания пайплайна (для тестов).

### Метрики (`MetricsOut`, `metrics.METRIC_KEYS`)
`time_to_draft_avg_sec`, `source_coverage`, `traceability`, `draft_acceptance_rate`
(ratio ≥ 0.85 = «малый diff»), `factcheck_correction_rate`, `feedback_good_share`,
`investigations_total`, `by_status`, `feedback_total`. Доли 0..1, `None` = нет данных.

## 7. HTML-страницы (`app/routers/pages.py`) и шаблоны

Все шаблоны наследуют `base.html` (тёмная шапка, навигация по роли, светлый контент) и получают
**общий контекст**: `request`, `user` (User|None), `nav_active` (str). В `templates.env.globals`
регистрируются `ROLE_LABELS`, `STATUS_LABELS`, `Role`, `InvestigationStatus`, `has_role`.
Фильтры Jinja: `dt` (UTC → «14:31, 14 ноя» МСК), `pct` (0.73 → «73%»).

| Маршрут | Шаблон | Доп. контекст |
|---|---|---|
| GET `/login`, POST `/login` (form: user_id, password) | `login.html` | `users`, `error` |
| GET `/logout` | — (редирект `/login`) | |
| GET `/` | `dashboard.html` | `investigations`, `role_view` (producer/editor/smm/operator) |
| GET `/investigations/new`, POST (form: lead_text, channel_id, title) | `investigation_new.html` | `channels`, `error` |
| GET `/investigations/{id}?tab=` | `investigation.html` | `inv`, `tab` (progress/report/sources/contacts/media/draft/history), `iterations`, `report` (dict\|None), `sources`, `sources_by_id`, `clusters` ({cluster_id: [Source]}), `contacts`, `can_see_contacts`, `media`, `drafts`, `latest_draft`, `history`, `fact_marks`, `feedback`, `allowed_transitions` |
| GET `/investigations/{id}/edit`, POST (form: title, body) | `editor.html` | `inv`, `ai_draft`, `current_draft`, `diff` (DraftDiffOut), `claims`, `sources_by_id`, `fact_marks`, `allowed_transitions` |
| POST `/investigations/{id}/transition` (form: to_status, comment) | — (303) | |
| POST `/investigations/{id}/facts` (form: fact_text, state, claim_id, comment) | — (303) | |
| GET `/operator` | `operator.html` | `board` (BoardOut), `metrics`, `metric_labels`, `feedback`, `chats`, `channels`, `sync_result` |
| POST `/operator/chats/{id}/toggle`, POST `/operator/sync` | — (303) | |
| GET `/feedback?investigation_id=`, POST (form: investigation_id, verdict, comment) | `feedback.html` | `investigations`, `selected_id`, `my_feedback` |
| ошибки 403/404 | `error.html` | `status_code`, `message` |

Частичные шаблоны — `app/templates/partials/*.html` (`_macros.html`: `status_badge`, `source_badge`,
`uniqueness_bar`), вкладки — `app/templates/investigation/_tab_<tab>.html`.
JS — `app/static/app.js` (опрос прогресса, вкладки, клик по утверждению → источник, diff-подсветка).
Внешние CDN запрещены; CSS — только `app/static/style.css`.

## 8. Тесты (QA)

- `tests/conftest.py`: `DATABASE_URL=sqlite+aiosqlite:///:memory:` (или tmp-файл) ДО импорта app,
  `PIPELINE_INLINE=true`, `MAX_POLLER_ENABLED=false`; `db.configure(url)` + `await db.init_db(drop=True)`;
  клиент `httpx.AsyncClient(transport=ASGITransport(app))` внутри `app.router.lifespan_context(app)`;
  логин — `POST /api/auth/login` или cookie `auth.make_session_token(user_id)`.
- Подмены провайдеров: `set_llm`, `set_search_provider`, `set_max_client` (mock-реализации).

## 9. Запросы на изменение контракта

Формат: `- [ ] YYYY-MM-DD, ЗОНА, файл: что нужно и зачем` → тимлид отмечает `[x]` и вносит правку.

- [x] 2026-11-14, DEV-FRONTEND, app/main.py: обработчик HTTPException для НЕ-`/api` путей с кодами 403/404 рендерить `error.html` (сейчас отдаётся JSON). Пока страницы проверяют роли сами (`has_role` внутри обработчика) и рендерят `error.html` с кодом 403/404, поэтому `require_role` в pages.py не используется. **Сделано тимлидом**: `app/main.py:_html_error`.
- [x] 2026-11-14, DEV-FRONTEND, §7 (к сведению, только зона pages.py): добавлены `GET /investigations/{id}/progress.json` (формат = ProgressOut, резерв для app.js, если `/api/.../progress` отдаёт не 200), необязательное поле `next` в форме `POST /login` (быстрая смена роли из шапки), `GET /login?switch=1` (показать логин уже вошедшему), `GET /?view=producer|editor|smm` (оператор смотрит дашборд другой роли), `POST /investigations/{id}/facts` с `Accept: application/json` отвечает JSON (отметка факта без перезагрузки). Jinja-глобалы сверх контракта: `STATUS_ORDER`, `STATUS_FLOW`, `TRANSITION_BUTTONS`, `asset()`; фильтры: `dur`, `ago`, `minutes`, `status_label`, `role_label`, `kind_label`, `fact_label`, `safe_url`.
- [x] 2026-11-14, DEV-AI, §5 `report_json` (зона формата DEV-AI, правка уже внесена выше): порядок и названия разделов приведены к 9 разделам исходного плана — `summary, timeline, facts, sources, confirmed, unconfirmed, contradictions, media, to_verify` («Краткое описание ситуации», «Хронология», «Найденная информация», «Источники», «Подтверждённые сведения», «Неподтверждённая информация», «Противоречия», «Фото и видео», «Что ещё необходимо проверить»); добавлены верхнеуровневые `contacts` (без телефонов) и `stats`, необязательные поля ITEM. **QA**: обновить `tests/nf_helpers.py:REPORT_SECTION_KEYS` (сейчас из-за порядка падают 3 теста: e2e, test_report_9_sections_and_source_ids, test_generic_lead; с новым порядком проходят). Фронт итерирует `sections` — правок не нужно.
- [x] 2026-11-14, DEV-AI → DEV-BACKEND (к сведению, уже используется в `max/mock.py`): единый источник демо-данных MAX — `app/ai/mock_data.py`: `SCENARIO_MAX_CHATS` (5 чатов), `SCENARIO_MAX_MESSAGES` (40 сообщений `m01…m40`: 22 релевантных сценарию, из них дубли/пересылки m03, m09, m10, m17, m19; 18 мусорных, в т.ч. «ловушки» m27, m38), `SCENARIO_MAX_PROFILES` (у части авторов телефона нет), `scenario_max_messages(anchor)`, `scenario_event_time()`/`set_scenario_anchor()`. MockLLM (релевантность, важность, справка) опирается на эти тексты и external_id — менять их только вместе с DEV-AI. `sources.url` для MAX — `max://chat/{chat_external_id}/{message_external_id}` (`mock_data.max_message_url`).
- [x] 2026-11-14, DEV-AI, §4 (к сведению): `pipeline.progress_update(...)` получил необязательные kw `note=` (note этапа) и `done=True` (state=done); `queries_json[]` получил поле `new`; `SearchProvider` дополнен `SerperSearchProvider` (SEARCH_PROVIDER=serper). Если в БД нет ни одного сообщения MAX, пайплайн один раз вызывает `poller.sync_once` перед поиском. Демо-паузы пайплайна: только при MockLLM и `PIPELINE_INLINE=false` (~13 с), отключаются `pipeline.DEMO_DELAY_SCALE = 0`.
- [x] 2026-11-14, TEAM LEAD, app/schemas.py: `MaxChatUpdate` — оба поля необязательны (`enabled`, новое `district`); PATCH меняет только переданные.
