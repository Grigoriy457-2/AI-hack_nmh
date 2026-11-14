# NEWSFLOW AI: приёмка MVP по ТЗ

Дата: 2026-11-14. Принимал тимлид. Сверка шла по `docs/TZ.md` (п.0–11 и DoD) с кодом. Дополнительно:
`pytest` (281 passed) и ручной прогон живого `uvicorn` на чистой SQLite. Детали проверки — в конце файла.

Легенда: ✅ выполнено · ⚠️ выполнено с оговоркой или не проверено в реальном окружении · ❌ не выполнено.

## п.0. Принципы

| Пункт | Статус | Где в коде |
|---|---|---|
| NEWSFLOW не публикует сам, решения принимают люди | ✅ | Публикация — только переход `ready→published` ролью SMM: `app/services/workflow.py:ALLOWED_TRANSITIONS`. Пайплайн доводит статус только до `report_ready` |
| У каждого факта справки и черновика есть `source_ids` | ✅ | `pipeline.build_report`, `pipeline.stage_report_and_draft` (claims), `app/ai/drafting.py`. Метрика Traceability на демо ≈0.96 |
| CHAIN SEARCH: AI порождает гипотезы, не больше 3 итераций | ✅ | `pipeline.stage_chain_search` (LLM-задача `chain_hypotheses`, лимит `CHAIN_MAX_ITERATIONS=3`). На демо 3 итерации |
| Демо работает без интернета и ключей, у всех провайдеров есть mock | ✅ | `MockLLM` (`app/ai/llm.py`), `MockSearchProvider` (`app/integrations/search.py`), `MockMaxClient` (`app/integrations/max/mock.py`). Данные сценария «Чертаново, хлопок ~14:30» — `app/ai/mock_data.py` |

## п.1. Роли

| Пункт | Статус | Где в коде |
|---|---|---|
| Вход выбором пользователя, seed-пользователи, пароль `demo` | ✅ | `/login`: `pages.login_page`, `templates/login.html`; `app/auth.py`; `app/seed.py:SEED_USERS` |
| Продюсер: создаёт зацепку, запускает исследование, проверяет справку | ✅ | `pages.investigation_create`, `api.create_investigation` / `run_investigation`, вкладка «Справка» |
| Продюсер: отмечает факт как подтверждён / не подтверждён / ложь | ✅ | `POST /api/investigations/{id}/facts`, `pages.investigation_mark_fact`, `_tab_report.html` |
| Продюсер: контакты авторов уникальных сообщений и кнопка `tel:` | ✅ | `api.list_contacts`, `pages._contacts`, `templates/investigation/_tab_contacts.html` |
| Продюсер: передаёт на редактуру, ставит «норм / стрём» с комментарием | ✅ | Переход `report_ready→editing`; `POST /api/investigations/{id}/feedback`; `/feedback` и блок на странице расследования |
| Редактор правит готовый AI-черновик, а не пишет с нуля: diff, подсветка неподтверждённого, переход к источнику | ✅ | `templates/editor.html` (AI-версия только для чтения против правки, живой diff, change-meter, карточки claims со сносками). JS — `static/app.js`; серверный diff — `GET /api/.../drafts/diff` |
| Редактор: «Вернуть продюсеру» / «Готово для SMM» | ✅ | Переходы `editing→report_ready`, `editing→ready` (`TRANSITION_BUTTONS` в `pages.py`) |
| SMM видит готовые материалы и отмечает «Опубликовано» | ✅ | Дашборд SMM (`dashboard.html`, `role_view=smm`), переход `ready→published` |
| AI-оператор: доска по статусам, зависшие задачи, ответственный | ✅ | `/operator` (`operator.html#board`), `api.operator_board` (`BoardOut`: `responsible`, `is_stale` по `STALE_AFTER_MINUTES`) |
| AI-оператор: метрики эффективности | ✅ | `services/metrics.compute_metrics`, `operator.html#metrics` |
| AI-оператор: обратная связь «норм / стрём» | ✅ | `GET /api/feedback`, `operator.html#feedback` |
| AI-оператор: источники (чаты MAX, каналы, Channel DNA) | ✅ | `operator.html#chats` / `#channels`. API: `PATCH /api/max/chats/{id}` (`enabled`, `district`), `POST /api/max/sync`, `POST /api/channels/{id}/dna` |
| AI-оператор может всё | ✅ | `auth.require_role` / `has_role` всегда пропускают operator; `workflow.can_transition` разрешает ему любой переход из таблицы |

## п.2. Статусная модель

| Пункт | Статус | Где в коде |
|---|---|---|
| Цепочка `new→researching→report_ready→editing→ready→published` и `rejected` | ✅ | `workflow.ALLOWED_TRANSITIONS`, `models.InvestigationStatus` |
| Каждый переход пишется в `status_history` (кто, когда, комментарий) | ✅ | `workflow.transition`; вкладка «История» (`_tab_history.html`) |
| Переходы разрешены только своей роли | ✅ | `workflow.can_transition`, `TransitionForbidden` → 403, `TransitionNotAllowed` → 409. Полная матрица в `tests/test_unit_workflow.py` |

## п.3. Интеграция MAX

| Пункт | Статус | Где в коде |
|---|---|---|
| Интерфейс `MaxClient`: `list_chats`, `fetch_messages(chat_id, since)`, `get_user_profile` | ✅ | `app/integrations/max/client.py` |
| `MaxBotApiClient` на httpx async, `platform-api.max.ru`, `MAX_BOT_TOKEN` | ⚠️ | `app/integrations/max/bot_api.py`. Проверен только на фейковом HTTP (`httpx.MockTransport`). С реальным Bot API не проверялся, формат ответов помечен `TODO(MAX)` |
| `MockMaxClient`: 5 чатов, 40 сообщений (релевантные, мусор, дубли, пересылки), телефон есть у части авторов | ✅ | `max/mock.py` → `mock_data.SCENARIO_MAX_*` |
| Фоновый poller в lifespan раз в N секунд | ✅ | `max/poller.start_poller`, запуск — `app/main.py:_start_poller`, интервал `MAX_POLL_INTERVAL` |
| Кнопка «Синхронизировать сейчас» | ✅ | `POST /operator/sync`, `POST /api/max/sync` → `poller.sync_once` |
| Юридическая оговорка о согласии участников в UI | ✅ | `_tab_contacts.html`, `operator.html#chats` |
| Агент `max_chats` в RADAR ищет по сохранённым сообщениям: ключевые слова + LLM-релевантность | ✅ | `pipeline.agent_max_chats` (только включённые чаты, `RELEVANCE_THRESHOLD`) |

## п.4. Уникальность сообщений

| Пункт | Статус | Где в коде |
|---|---|---|
| Нормализация: lowercase, без пунктуации, эмодзи и ссылок | ✅ | `uniqueness.normalize_text` |
| Шинглы по 3 слова, Jaccard ≥ 0.6 → один кластер | ✅ | `uniqueness.shingles`, `jaccard`, `cluster_messages` (DSU, транзитивно) |
| Пересланное и скопированное не считается независимым подтверждением | ✅ | Пересылка не становится первоисточником (`UniqItem.is_forwarded`); дубли свёрнуты во вкладке «Источники» |
| `uniqueness_score` = 1 − max сходства с более ранними; первоисточник — самое раннее | ✅ | `uniqueness.score_uniqueness` |
| `is_important` через LLM или эвристику | ✅ | Задача LLM `importance` в `pipeline.stage_intelligence`, фолбэк — `uniqueness.is_important_heuristic` |
| «Кому позвонить»: первоисточник, uniqueness ≥ 0.5, important → профиль MAX, имя, телефон, цитата, чат, время, `tel:`; «телефон скрыт в профиле» | ✅ | `uniqueness.select_unique_important`, `poller.ensure_author_profile`, `api.list_contacts`, `_tab_contacts.html`. На демо 8 контактов, из них 6 с телефоном |
| Контакты видны только Продюсеру и AI-оператору | ✅ | API: `require_role(P)` (operator проходит всегда); страница: `can_see_contacts = has_role(user, P)`. Редактор получает 403 — проверено вживую и тестами. ⚠️ Мелочь: раздел справки «Что ещё проверить» называет очевидца по имени без телефона, и этот раздел видят все роли. С ТЗ не расходится |

## п.5. Пайплайн

| Пункт | Статус | Где в коде |
|---|---|---|
| Фоновая asyncio-задача после «Запустить» | ✅ | `pipeline.start_investigation` (ссылки хранятся в `_running`), режим `PIPELINE_INLINE` для тестов |
| 1. INPUT: сущности | ✅ | `stage_input` → `entities_json` |
| 2. ORCHESTRATOR: агенты web / max_chats / official / media через `asyncio.gather`, до 3 итераций, итерации сохраняются | ✅ | `stage_chain_search`, `search_iterations`, вкладка «Прогресс / цепочка поиска» |
| 3. INTELLIGENCE: дедуп, уникальность, хронология, подтверждённое и нет, противоречия, «что проверить» | ✅ | `stage_intelligence` |
| 4. MEDIA FINDER: превью, источник, время, связь, права «не проверены», «возможно старое фото» | ✅ | `stage_media`, `media_items`, `_tab_media.html` |
| 5. CHANNEL DNA | ✅ | `app/ai/channel_dna.py` (`analyze_posts`, `ensure_channel_dna`), 3 канала в seed |
| 6. REPORT & DRAFT: справка из 9 разделов, черновик по DNA, `source_ids` у утверждений | ✅ | `build_report`, `stage_report_and_draft`, `app/ai/drafting.py` |
| Прогресс в БД, опрос `GET /progress` раз в 1–2 с, живой лог | ✅ | `progress_update` (commit на каждом шаге), `static/app.js` |

## п.6–7. LLM и web search

| Пункт | Статус | Где в коде |
|---|---|---|
| `LLMClient.complete_json`, `AnthropicLLM` (SDK, `claude-sonnet-5-5`), `MockLLM` | ⚠️ | `app/ai/llm.py`. MockLLM покрыт тестами. AnthropicLLM без ключа не проверялся; при ошибке срабатывает фолбэк на MockLLM |
| Все промпты в `prompts.py` | ✅ | `app/ai/prompts.py` |
| `SearchProvider.search`, mock-провайдер и реальный провайдер | ⚠️ | `MockSearchProvider` ✅. `TavilySearchProvider` и `SerperSearchProvider` без ключей не проверялись |

## п.8. Данные

| Пункт | Статус | Где в коде |
|---|---|---|
| Все таблицы п.8 | ✅ | `app/models.py` (14 таблиц) |
| `create_all` при старте и seed (`python -m app.seed`) | ✅ | `db.init_db`, `main.lifespan`, `app/seed.py` (`--reset`) |
| Совместимость с MySQL | ⚠️ | DDL компилируется под диалект MySQL; `docker compose up` в этой приёмке не запускался |

## п.9. Экраны

| Экран | Статус | Где в коде |
|---|---|---|
| `/login`: карточки | ✅ | `login.html` |
| `/`: дашборд по роли | ✅ | `dashboard.html` (`role_view`) |
| `/investigations/new` | ✅ | `investigation_new.html` |
| `/investigations/{id}`: 7 вкладок | ✅ | `investigation.html` + `investigation/_tab_*.html` |
| `/investigations/{id}/edit` | ✅ | `editor.html` |
| `/operator`: канбан, зависшие, 6 метрик, фидбэк, чаты MAX | ✅ | `operator.html` |
| `/feedback` | ✅ | `feedback.html` |
| Тёмная шапка, русские подписи, без внешних CDN, один CSS | ✅ | `base.html`, `static/style.css`. Smoke-тесты проверяют отсутствие внешних ресурсов |
| 403/404 на HTML-страницах → `error.html` | ✅ | Исправлено при приёмке: `app/main.py:_html_error` |

## п.10–11. Структура и DoD

| Пункт | Статус | Комментарий |
|---|---|---|
| Структура репозитория | ✅ | Совпадает с п.10. README добавлен при приёмке |
| `pip install -r requirements.txt && uvicorn app.main:app` с SQLite без ключей | ✅ | Проверено вживую на порту 8077 |
| `docker compose up` (MySQL + app) | ⚠️ | Файлы готовы, healthcheck MySQL и utf8mb4 настроены, но в этой приёмке не запускалось |
| Демо-сценарий кликами < 5 минут | ✅ | Пайплайн ≈15 с в демо-режиме, 3 итерации. Затем справка, дубли, контакты, editing, diff, ready, published, «норм», доска и метрики оператора — всё проверено вживую |
| `pytest` зелёный: уникальность, workflow и права, API на SQLite, e2e | ✅ | 281 passed (~61 с), включая e2e через ASGI и через настоящий uvicorn (`tests/test_e2e_server.py`) |

## Исправления при приёмке

1. `app/main.py`: 403/404 на HTML-страницах (не `/api`, не `/static`) теперь рендерят `error.html` с пользователем из cookie. Это запрос DEV-FRONTEND из CONTRACTS §9.
2. `MaxChatUpdate` получил необязательное поле `district`, и `enabled` тоже стал необязательным. `PATCH /api/max/chats/{id}` меняет только переданные поля.
3. Добавлен `README.md`. Запросы из CONTRACTS §9 закрыты.

## Ручная проверка (живой сервер)

`python -m app.seed --reset` на чистой SQLite, затем `uvicorn app.main:app --port 8077`. Результаты:
- `/` → 303 на `/login`.
- Вход продюсером, зацепка «Чертаново, хлопок у жилого дома около 14:30»: за ~15 с дошла до `report_ready`, 3 итерации.
- Все 7 вкладок отвечают 200. Контактов 8, из них 6 с телефоном.
- Редактор: `/api/.../contacts` → 403, `/operator` → 403 (HTML-страница ошибки), несуществующий путь → 404 HTML.
- Полный сценарий: editing → правка (diff ratio 0.98) → ready → published → «норм».
- Оператор: метрики считаются, доска показывает published.
- В логе сервера нет трейсбэков и «database is locked».
