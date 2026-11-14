# NEWSFLOW AI — контракты v2 (один редактор, реальное время)

Главный документ — `docs/TZ_v2.md` (+ неотменённые части `docs/TZ.md`). Этот файл **замещает**
`docs/CONTRACTS.md` в части ролей, статусов, API и сервисов. Форматы `report_json`, `progress_json`,
`claims_json`, `dna_json`, `entities_json`, `queries_json` — без изменений (см. CONTRACTS.md §5).
Изменения после первой версии — в разделе «Изменения» в конце.

Зоны: BACKEND — всё в `app/` кроме `app/routers/pages.py`, `app/templates/*`, `app/static/*` (FRONTEND);
`app/integrations/telegram/*` — DEV-TELEGRAM; `tests/*` — QA.

## 1. Пользователь и вход

- Ролей нет. Модель `User(id, name, login, title)` — все пользователи — редакторы. `Role`, `ROLE_LABELS`,
  `require_role`, `has_role` **удалены**.
- Seed: `editor` (Анна Редакторова), `editor2`; пароль `demo`.
- `app.auth`: `current_user` (401), `current_user_optional`, `set_login_cookie`, `clear_login_cookie`,
  `verify_password`, `make_session_token`, `read_session_token` — как раньше.
- 401 на не-`/api` путях → редирект `/login` (main.py).

## 2. Статусы сюжета (`InvestigationStatus`, значение в БД = `.value`)

| value | `.label` / `STATUS_LABELS` | смысл |
|---|---|---|
| `new` | Новый | создан, поиск не запускали |
| `researching` | AI ищет | идёт пайплайн / «Копать глубже» |
| `review` | На проверке | справка готова; редактор проверяет, звонит, правит |
| `handed_off` | Передано дальше | редактор передал (кому/куда, канал, комментарий, время) |
| `rejected` | Не подтвердилось | отклонено редактором |

`STATUS_ORDER = [new, researching, review, handed_off, rejected]`, `ACTIVE_STATUSES = {new, researching, review}`.

Переходы (`workflow.ALLOWED_TRANSITIONS`, `"user"` = любой вошедший редактор, `"system"` = пайплайн):

| Из → В | Кто | Кнопка / событие |
|---|---|---|
| new → researching | user | «Исследовать» / «Запустить» |
| new → rejected | user | «Не подтвердилось» |
| researching → review | system | пайплайн завершён |
| researching → review | user | «Остановить» (если пайплайн упал/завис) |
| review → researching | user | «Копать глубже» / «Перезапустить поиск» |
| review → handed_off | user | «Передать дальше» |
| review → rejected | user | «Не подтвердилось» |
| rejected → review | user | «Вернуть в работу» |

Каждый переход → строка `status_history` (user_id = None для системы). Передача дальше НЕ блокируется
неподтверждёнными фактами — API возвращает `warnings`.

Каналы передачи (`HANDOFF_CHANNELS`, `GET /api/handoff-channels`):
`site` Сайт · `telegram` Telegram-канал · `max` Канал MAX · `tv` ТВ/радио · `print` Печать/газета · `social` Соцсети · `other` Другое.

## 3. Модель данных (изменения)

- `users`: без `role`.
- `investigations`: `owner_id` (вместо producer_id; кто создал/взял), **нет** `editor_id`; новые поля:
  `origin` (`manual` | `emerging`), `emerging_key` (str|None), `signal_at` (время первого сигнала:
  первое сообщение кластера или created_at), `review_at` (вместо report_ready_at — первый выход
  справки), `deeper_questions` (JSON list[{question, asked_at, user_id}]),
  `handoff_to` (str, кому/куда), `handoff_channel` (key из HANDOFF_CHANNELS), `handoff_channel_id`
  (FK channels, опц.), `handoff_comment`, `handed_off_at`, `handed_off_by_id`.
- `source_chats` (класс `MaxChat`, алиас `SourceChat`): + `platform` (`max`|`telegram`), `ref`
  (исходная ссылка/@username), `added_by_id`; уникальность `(platform, external_id)`.
- `chat_messages`: + `platform`, `archived` (bool; старые сообщения после «перезапуска демо-потока»).
  Уникальность `(chat_id, external_id)`.
- `feedback` и `Verdict` удалены. `dismissed_emerging(key, created_at)` — скрытые карточки.
- Старая SQLite-БД со схемой v1 при старте пересоздаётся автоматически (лог WARNING);
  `python -m app.seed --reset` — пересоздать вручную.

## 4. JSON API (`/api`), все кроме помеченных «публичный» требуют вход (401)

Основной префикс сюжетов — `/api/stories`. **Алиас**: всё под `/api/investigations/...` работает так же
(скрыт из OpenAPI), для обратной совместимости.

### 4.1 Служебное / вход
| Метод | Путь | Вход | Выход |
|---|---|---|---|
| GET | `/api/health` | — | `{"status":"ok"}` (публичный) |
| GET | `/api/users` | — | `list[UserOut]` (публичный) |
| POST | `/api/auth/login` | `{"user_id": int}` или `{"login": "editor"}`, `"password": "demo"` | `UserOut` + cookie |
| POST | `/api/auth/demo-login` | — | `UserOut` + cookie (первый редактор; кнопка «Войти как редактор») |
| POST | `/api/auth/logout` | — | 204 |
| GET | `/api/me` | — | `UserOut {id,name,login,title}` |

### 4.2 Лента и реальное время
| Метод | Путь | Вход | Выход |
|---|---|---|---|
| GET | `/api/feed?since=&limit=50&chat_id=&platform=` | `since` = id-курсор (int) **или** ISO-время | `FeedOut` |
| GET | `/api/stream?since=&timeout=` | SSE | `text/event-stream` |
| GET | `/api/emerging?window_min=&min_messages=&include_dismissed=` | — | `list[EmergingStoryOut]` |
| POST | `/api/emerging/{key}/investigate` | `{"channel_id"?, "title"?, "note"?}` | `StoryOut` 201 (если по кластеру уже есть сюжет — 200 и он же) |
| POST | `/api/emerging/{key}/dismiss` | — | 204 (карточка скрыта) |

`FeedOut = {"items": [FeedMessage], "cursor": int /*max id, передавать в since*/, "server_time": iso}`;
`items` — по возрастанию id (при `since`), без `since` — последние `limit` (тоже по возрастанию).

`FeedMessage`:
```json
{"id": 123, "platform": "max", "platform_label": "MAX", "chat_id": 2, "chat_title": "ЖК «Чертаново»",
 "district": "Чертаново Центральное", "author_name": "Ирина К.", "author_external_id": "u1001",
 "text": "…", "sent_at": "2026-11-14T11:29:00", "has_media": true, "media_url": "data:…",
 "is_forwarded": false, "message_url": "max://chat/chat_x/m01",
 "badges": {"unique": true, "important": true, "duplicates": 2, "is_duplicate": false,
            "duplicate_of": null, "has_phone": true},
 "uniqueness_score": 1.0, "emerging_key": "e123"}
```
Бейджи: «уникальное» = `unique` (первоисточник кластера дублей, uniqueness ≥ 0.5), «важное» = `important`,
«дубль ×N» = `duplicates` (у первоисточника — сколько копий) / `is_duplicate` (это копия `duplicate_of`),
«есть телефон» = `has_phone`. Кластеризация дублей — кросс-платформенная (копия TG ↔ MAX = один кластер).

SSE `/api/stream` (`StreamingResponse`, `text/event-stream`, `retry: 3000`):
- `event: hello` — `{"server_time", "cursor", "demo": DemoStreamOut}` (сразу при подключении);
- `event: new_message` — `FeedMessage` (по одному событию на сообщение, `id:` = id сообщения);
- `event: emerging_update` — `{"items": [EmergingStoryOut]}` — полный снимок, после новых сообщений
  (и раз в ~30 с для пересчёта тренда);
- `event: story_update` — `{"id","title","status","status_label","progress":{"state","percent","current_stage"},"updated_at"}`
  — при любом изменении сюжета (прогресс пайплайна, переход статуса);
- `: keepalive` — комментарий раз в `SSE_KEEPALIVE_SEC` (15 с).
`since` — курсор (иначе только новые с момента подключения). `timeout` (сек) — закрыть поток через N с
(для тестов/отладки). Источник — опрос БД раз в `SSE_POLL_SEC` (1 с), работает между процессами.
Fallback без SSE: опрос `GET /api/feed?since=<cursor>` + `GET /api/emerging` раз в 3–5 с.

`EmergingStoryOut`:
```json
{"key": "e123", "title": "Возможный инфоповод: хлопок у жилого дома, Чертаново",
 "summary": "первое сообщение (обрезано)", "keywords": ["хлопок","чертаново","дым"],
 "place": "Чертаново Центральное", "messages_count": 9, "chats_count": 3,
 "chats": ["ЖК …", "…"], "platforms": ["max"], "first_at": "…", "last_at": "…",
 "trend": "rising", "trend_label": "растёт",          // rising|steady|fading: растёт|стабильно|затихает
 "recent_count": 4, "important_count": 5, "unique_count": 6, "has_phone_count": 3,
 "score": 7.5, "message_ids": [123, 125], "sample": [FeedMessage, …≤3],
 "investigation_id": null, "investigation_status": null}
```
`key` = `e<id самого раннего сообщения кластера>`; если кластер слился, `investigate` по старому ключу
найдёт кластер, содержащий это сообщение. Карточка показывается при ≥ `EMERGING_MIN_MESSAGES` (3) сообщениях.

### 4.3 Сюжеты
| Метод | Путь | Вход | Выход |
|---|---|---|---|
| GET | `/api/stories?status=&mine=&q=` | — | `list[StoryOut]` (updated_at desc) |
| POST | `/api/stories` | `{"lead_text", "title"?, "channel_id"?, "autorun"=true}` («Своя зацепка») | `StoryOut` 201 |
| GET | `/api/stories/{id}` | — | `StoryDetail` |
| POST | `/api/stories/{id}/run` | — | `StoryOut` (new\|review → researching; 409 если уже идёт) |
| POST | `/api/stories/{id}/deeper` | `{"question": "…"}` (может быть пустым) | `StoryOut` (review → researching, 409 из других) |
| POST | `/api/stories/{id}/stop` | — | `StoryOut` (researching → review) |
| GET | `/api/stories/{id}/handoff/check` | — | `HandoffCheckOut` (для модалки до отправки) |
| POST | `/api/stories/{id}/handoff` | `{"to": "Ночная смена сайта", "channel": "site", "channel_id"?: int, "comment"?: "…"}` | `HandoffResultOut` |
| POST | `/api/stories/{id}/reject` | `{"comment"?: "…"}` | `StoryOut` |
| POST | `/api/stories/{id}/reopen` | `{"comment"?: "…"}` | `StoryOut` (rejected → review) |
| POST | `/api/stories/{id}/transition` | `{"to_status", "comment"?}` | `StoryOut` (общий; 409 недопустимый) |
| GET | `/api/stories/{id}/progress` | — | `ProgressOut {investigation_id, status, progress, iterations}` |
| GET | `/api/stories/{id}/iterations` | — | `list[SearchIterationOut]` |
| GET | `/api/stories/{id}/sources?kind=&primary_only=` | — | `list[SourceOut]` (+`duplicates_count`) |
| GET | `/api/stories/{id}/contacts` | — | `list[ContactOut]` (теперь видит редактор; с телефонами) |
| GET | `/api/stories/{id}/media` | — | `list[MediaItemOut]` |
| GET | `/api/stories/{id}/report` | — | `ReportOut` (404 нет справки) |
| GET | `/api/stories/{id}/drafts` | — | `list[DraftOut]` |
| POST | `/api/stories/{id}/drafts` | `{"title","body","claims_json"?}` | `DraftOut` 201 (статусы review/rejected, иначе 409) |
| GET | `/api/stories/{id}/drafts/diff?base=&target=` | — | `DraftDiffOut {base_version,target_version,ratio,ops[{op,a,b}]}` |
| GET | `/api/stories/{id}/facts` | — | `list[FactMarkOut]` |
| POST | `/api/stories/{id}/facts` | `{"fact_text","state":"confirmed\|unconfirmed\|false","claim_id"?,"comment"?}` | `FactMarkOut` 201 |
| GET | `/api/stories/{id}/history` | — | `list[StatusHistoryOut]` |
| GET | `/api/handoff-channels` | — | `[{"key","label"}]` |

`StoryOut` (= `InvestigationOut`): `id, title, lead_text, status, status_label, origin, emerging_key,
channel_id, owner_id, created_at, updated_at, signal_at, review_at, handoff_to, handoff_channel,
handoff_channel_label, handoff_channel_id, handoff_comment, handed_off_at, handed_off_by_id`.

`StoryDetail` = StoryOut + `entities_json, progress_json, channel, owner, handed_off_by,
deeper_questions, allowed_transitions: list[status], unconfirmed_count: int`.

`HandoffCheckOut = {"ok": bool, "warnings": ["2 утверждения не подтверждены", …],
"unconfirmed": [{"id","text","state","source_ids","origin":"draft|report"}], "has_report": bool, "has_draft": bool}`.
Неподтверждённые = claims последнего черновика + пункты справки (`confirmed/unconfirmed/contradictions`),
итоговое состояние которых (последняя `fact_marks` по `claim_id`, иначе собственный `state/status`)
`unconfirmed` или `false`.

`HandoffResultOut = {"story": StoryOut, "warnings": [...], "unconfirmed": [...]}` — передача выполняется
всегда (не блокирует); 409 если статус не `review`; 422 если пустое `to` или неизвестный `channel`.

«Копать глубже»: статус → researching, пайплайн перезапускается с вопросом редактора: вопрос добавляется к
зацепке, CHAIN SEARCH получает +1 итерацию с запросами по вопросу ко всем агентам, справка/источники/медиа
пересобираются, новый AI-черновик — следующей версией (правки редактора сохраняются как прежние версии).
Вопросы копятся в `deeper_questions`. По окончании — снова `review`.

### 4.4 Источники, каналы, демо
| Метод | Путь | Вход | Выход |
|---|---|---|---|
| GET | `/api/sources/chats?platform=` (алиас `/api/max/chats`) | — | `list[SourceChatOut]` |
| POST | `/api/sources` | `{"platform": "telegram"\|"max", "ref": "@channel" \| "https://t.me/…" \| "chat_id", "title"?, "district"?}` | `SourceChatOut` 201 (200 если уже есть) |
| PATCH | `/api/sources/chats/{id}` (алиас `/api/max/chats/{id}`) | `{"enabled"?, "district"?, "title"?}` | `SourceChatOut` |
| DELETE | `/api/sources/chats/{id}` | — | 204 (отключённый источник удаляется вместе с сообщениями без ссылок) |
| POST | `/api/sources/sync` (алиас `/api/max/sync`) | — | `SyncResult {chats,new_messages,errors,finished_at,by_platform}` |
| GET | `/api/channels` | — | `list[ChannelOut]` (Channel DNA) |
| POST | `/api/channels/{id}/dna` | — | `ChannelOut` (пересчёт DNA) |
| GET | `/api/demo/stream` | — | `DemoStreamOut` |
| POST | `/api/demo/restart` | — | `DemoStreamOut` (перезапуск демо-потока) |

`SourceChatOut = {id, platform, platform_label, external_id, ref, title, district, enabled, last_synced_at, messages_count}`.

`DemoStreamOut = {"enabled": bool, "active": bool /* mock MAX и трикл включён */, "epoch": int,
"started_at", "released": int, "total": int, "next_at": iso|null, "finished": bool, "interval_sec": [10,30]}`.
Перезапуск: прежние сообщения помечаются `archived` (сюжеты на них ссылаются), новый поток начинается с
`DEMO_STREAM_INITIAL` (10) сообщений, далее по одному каждые `DEMO_STREAM_MIN_SEC..MAX_SEC` (10–30 с).
После исчерпания — остановка (`finished=true`), если не `DEMO_STREAM_LOOP=true`.

### 4.5 Статистика
| GET | `/api/stats` (алиас `/api/metrics`) | — | `StatsOut` |

```json
{"stories_total": 7, "by_status": {"new":0,"researching":1,"review":3,"handed_off":2,"rejected":1},
 "handed_off": 2, "rejected": 1, "in_work": 4,
 "avg_signal_to_handoff_sec": 1520.0, "avg_signal_to_review_sec": 95.0,
 "traceability": 0.92, "source_coverage": 0.8, "factcheck_correction_rate": 0.3,
 "draft_acceptance_rate": 0.5, "handoffs_by_channel": {"site": 1, "telegram": 1},
 "recent_handoffs": [{"id","title","handoff_to","handoff_channel","handoff_channel_label","handed_off_at","signal_to_handoff_sec"}]}
```
Доли 0..1, `null` — нет данных.

Коды: 401 не вошёл, 404 нет объекта, 409 недопустимый переход/состояние, 422 валидация.
Удалены: `/api/operator/board`, `/api/feedback`, `/api/investigations/{id}/feedback`, роли в правах.

## 5. Сервисы для `pages.py` (рендер напрямую из БД)

Все функции принимают `AsyncSession` (из `Depends(get_session)`), commit делают сами там, где меняют данные.

```python
# app/models.py
InvestigationStatus, STATUS_LABELS, STATUS_ORDER, ACTIVE_STATUSES, HANDOFF_CHANNELS (dict key->label),
PLATFORM_LABELS {"max": "MAX", "telegram": "Telegram"}, Platform enum, MaxChat (= SourceChat), utcnow

# app/services/workflow.py
available_transitions(from_status) -> list[InvestigationStatus]
TRANSITION_BUTTONS: dict[(from, to), str]     # подписи кнопок
async transition(session, inv, to, user|None, comment=None) -> StatusHistory   # flush, без commit

# app/services/feed.py
async get_feed(session, *, since=None, limit=50, chat_id=None, platform=None) -> schemas.FeedOut
async latest_cursor(session) -> int

# app/services/emerging.py
async compute_emerging(session, *, window_min=None, min_messages=None, include_dismissed=False) -> list[schemas.EmergingStoryOut]
async investigate_emerging(session, key, user, *, channel_id=None, title=None, note=None) -> tuple[Investigation, bool /*created*/]
async dismiss_emerging(session, key) -> None

# app/services/stories.py
async list_stories(session, *, status=None, owner_id=None, q=None) -> list[Investigation]
async get_story(session, inv_id) -> Investigation                     # HTTPException 404
async story_detail(session, inv) -> schemas.StoryDetail
async create_story(session, user, lead_text, *, title=None, channel_id=None, autorun=True,
                   origin="manual", emerging_key=None, signal_at=None) -> Investigation
async run_story(session, inv, user) -> Investigation
async dig_deeper(session, inv, user, question) -> Investigation
async stop_story(session, inv, user) -> Investigation
async handoff_check(session, inv) -> schemas.HandoffCheckOut
async hand_off(session, inv, user, *, to, channel, channel_id=None, comment=None) -> schemas.HandoffResultOut
async reject(session, inv, user, comment=None) -> Investigation
async reopen(session, inv, user, comment=None) -> Investigation
async list_sources(session, inv_id, *, kind=None, primary_only=False) -> list[schemas.SourceOut]
async list_contacts(session, inv_id) -> list[schemas.ContactOut]
async list_drafts(session, inv_id) -> list[Draft]
async save_draft(session, inv, user, *, title, body, claims_json=None) -> Draft
async draft_diff(session, inv_id, base=None, target=None) -> schemas.DraftDiffOut
async list_facts(session, inv_id) -> list[FactMark]
async mark_fact(session, inv_id, user, *, fact_text, state, claim_id=None, comment=None) -> FactMark
async list_history(session, inv_id) -> list[StatusHistory]
StoryError(status_code, detail)  # бросается сервисами (404/409/422); api.py -> HTTPException

# app/services/stats.py
async compute_stats(session) -> dict   # == schemas.StatsOut

# app/services/sources.py
async list_source_chats(session, platform=None) -> list[schemas.SourceChatOut]
async add_source(session, user, platform, ref, *, title=None, district=None) -> tuple[MaxChat, bool]
async update_source(session, chat_id, *, enabled=None, district=None, title=None) -> schemas.SourceChatOut

# app/integrations/max/poller.py
async sync_once(session, client=None) -> SyncResult      # MAX (+ Telegram, если доступен)
# app/integrations/max/demo_stream.py
status() -> schemas.DemoStreamOut;  async restart(session) -> schemas.DemoStreamOut
```

## 6. Конфиг (новые ключи `app/config.py`)

`MAX_POLL_INTERVAL` (по умолчанию 5; в mock-режиме ограничивается ≤ 5 с),
`DEMO_STREAM_ENABLED=true`, `DEMO_STREAM_INITIAL=10`, `DEMO_STREAM_MIN_SEC=10`, `DEMO_STREAM_MAX_SEC=30`,
`DEMO_STREAM_LOOP=false` (трикл только у `MockMaxClient` без явного `now`; `DEMO_STREAM_ENABLED=false` —
все сообщения сценария сразу, как в v1), `SSE_POLL_SEC=1.0`, `SSE_KEEPALIVE_SEC=15`,
`EMERGING_WINDOW_MIN=240`, `EMERGING_MIN_MESSAGES=3`,
Telegram: `TG_PROVIDER` (`auto|telethon|bot|web|mock`, по умолчанию `mock`), `TG_SOURCES`, `TG_POLL_INTERVAL`, `TG_API_ID`, `TG_API_HASH`,
`TG_SESSION_PATH`, `TG_BOT_TOKEN`, `TG_WEB_POLL_INTERVAL`, `TG_ENABLED=true`.

## 7. Telegram (пакет DEV-TELEGRAM)

`app/integrations/telegram`: `get_telegram_client()` → клиент с интерфейсом `MaxClient`
(`list_chats()`, `fetch_messages(chat_id, since)`, `get_user_profile(user_id)`, опц. `resolve_chat(ref)`).
Poller опрашивает Telegram, если пакет импортируется И (`TG_PROVIDER` не `mock` ИЛИ в БД есть включённые
TG-источники). Чаты TG появляются через `POST /api/sources {platform:"telegram", ref}` и/или `list_chats()`.
Сообщения TG хранятся в тех же `chat_messages` (`platform="telegram"`); `message_url` — `https://t.me/<username>/<id>`
(или то, что вернёт клиент). Лента, бейджи, кластеры, «Возникающие сюжеты» и пайплайн (агент `max_chats`
ищет по всем включённым чатам обеих платформ) — общие.

## Изменения
- 2026-11-14 v2.0 — первая версия.
- 2026-11-14 v2.1 (BACKEND, после интеграции пакета DEV-TELEGRAM, см. docs/TELEGRAM.md §4):
  - Модель `source_chats` (класс `MaxChat`/`SourceChat`): + `username`, `kind` (channel|group|chat), `url`
    (https://t.me/<username>). `chat_messages`: + `views`; ссылка «открыть» хранится в
    **`message_url`** (в TELEGRAM.md она названа `url` — у нас `message_url`, фронт берёт `FeedMessage.message_url`).
    Внешние id Telegram-чатов/авторов — с префиксом `tg:`.
  - Новые эндпоинты (алиасы для блока «Telegram» на странице «Источники»):
    `GET /api/telegram/status` → `{"available", "platform", "provider", "sources": [ext_id], "pending", "last_error"}`;
    `POST /api/telegram/sources {"ref", "title"?, "district"?}` = `POST /api/sources {platform:"telegram"}` → `SourceChatOut`
    (201 / 200 если уже был; 422 + понятный `detail` для приватных/битых ссылок);
    `DELETE /api/telegram/sources/{external_id}` (`tg:name` или `name`) → 204.
  - lifespan: при `MAX_POLLER_ENABLED` и `TG_ENABLED` запускается `telegram.runner.start_telegram_sync()`
    (listener + sync через `poller.sync_now(tg_client)`; в mock-режиме — 3 демо-канала/чата, 7 постов).
    Пока runner работает, общий поллер Telegram не опрашивает. `poller.sync_once(session, tg_client)` с
    Telegram-клиентом синхронизирует только Telegram.
  - Демо: при старте демо-потока якорь сценария = старт − 125 мин, поэтому все 7 постов mock-Telegram сразу
    в ленте и попадают в «Возникающие сюжеты» вместе с MAX (пост «Москва Live» — первоисточник, MAX m09/m10 —
    его дубли, `badges.is_duplicate`/`duplicate_of`). При «Исследовать» пайплайн переставляет якорь по m01.
  - `SourceOut.platform` (max|telegram|null), `ContactOut.platform` — добавлены (запрос фронтенда выполнен).
  - Для тестов (QA): трикл включён по умолчанию у `MockMaxClient()`; для детерминизма выставляйте
    `DEMO_STREAM_ENABLED=false` (все 40 сообщений сразу, как в v1) или `MockMaxClient(stream=False)`;
    для «мгновенного» трикла — `DEMO_STREAM_MIN_SEC=0.05`, `DEMO_STREAM_MAX_SEC=0.1`. SSE в тестах —
    `GET /api/stream?timeout=N` (ASGITransport буферизует ответ целиком).

## Запросы фронтенда
- ContactOut: добавить `platform` (max|telegram) — в блоке «Кому позвонить» показываем платформу автора. Не блокирует: без поля просто не рисуем значок.
- Страницы рендерят источники сюжета из ORM `Source.chat_message` (+ `ChatMessage.platform`); просьба не переименовывать эти поля.
- `POST /api/sources {platform:"telegram", ref:"https://t.me/test_public_channel"}` (и `@another_test`) на mock-Telegram отвечает 422 **с пустым телом** — фронт не может показать причину. Просьба: всегда `{"detail": "…"}` с понятной причиной (например, «канал не найден или закрыт»).

- 2026-11-14 v2.2 (BACKEND, правки по замечаниям фронтенда):
  - `POST /api/sources` при `TG_PROVIDER=mock`: публичный `@name` / `t.me/name` сохраняется (201) даже вне
    демо-каналов (сообщений не будет до реального провайдера). Пустой `ref` → 422 с текстовым `detail`
    (не массивом pydantic); приватные инвайты → 422 с объяснением.
  - Возникающие сюжеты: тема склеивается по ≥3 общим «стемам» (или ≥2, если оба сообщения важные), мелкие
    темы рядом по месту/времени сливаются с крупной. `title` — самое «центральное» сообщение кластера,
    `summary` — 1–2 других первоисточника (не повторяет заголовок), `sample` — по центральности.
    Зацепка сюжета из карточки начинается со сводки сигнала, а не с заголовка.
  - Демо-хронология: посты mock-Telegram выходят триклом по ходу MAX-потока (пост минуты M — сразу после
    MAX-сообщения сценария с minutes ≥ M), `sent_at` = момент выхода; «Москва Live» раньше m09/m10 и остаётся
    первоисточником. «Перезапустить демо-поток» перезапускает и демо-каналы Telegram.
  - Статусные действия строже: `stop` только из `researching`, `reopen` только из `rejected`, `reject` из
    `new|review`; иначе 409.
  - «Копать глубже» гарантирует +1 итерацию к прошлому прогону (ранний останов цепочки отключён).
  - `ContactOut.platform` отдаётся (max|telegram).
