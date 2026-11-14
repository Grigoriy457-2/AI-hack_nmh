# NEWSFLOW AI — план команды

Источник требований — `docs/TZ.md`, договорённости — `docs/CONTRACTS.md`.
Фундамент (этап 1) готов: модели, схемы, auth, config, db, main, контракты всех сервисов,
список эндпоинтов и страниц. Любой незаконченный эндпоинт отдаёт 501.

## Правила параллельной работы

1. Каждый правит **только свою зону** (таблица в CONTRACTS.md §1). Новые файлы внутри зоны — можно.
2. Сигнатуры из контрактов не меняем. Нужно изменение общего файла или чужой сигнатуры —
   запись в CONTRACTS.md §9 «Запросы на изменение контракта» и сообщение тимлиду.
3. Пока зависимость не готова — работайте против mock / заглушки у себя (например, фронт может
   временно рендерить фикстуры, пайплайн — вызывать `MockMaxClient` напрямую в тесте).
4. Ветки: `ai/*`, `backend/*`, `frontend/*`, `qa/*` → PR в `main`. Перед PR: `python -c "import app.main"`
   и `pytest` зелёные.
5. Демо обязано работать без интернета и ключей (mock по умолчанию).

## DEV-AI

Файлы: `app/ai/llm.py`, `app/ai/prompts.py`, `app/ai/mock_data.py`, `app/ai/channel_dna.py`,
`app/integrations/search.py`, `app/services/uniqueness.py`, `app/services/pipeline.py`.

1. `uniqueness.py` — все функции (первым делом: от них зависят пайплайн и тесты QA).
2. `MockLLM` + `mock_data.py`: детерминированные ответы для всех `task`-ключей; сценарий
   «Чертаново, хлопок ~14:30» + дженерики для произвольной зацепки (на основе слов из lead_text).
3. `prompts.py` + `AnthropicLLM` (SDK `anthropic`, `AsyncAnthropic`, модель `settings.LLM_MODEL`,
   парсинг JSON из ответа, 1 ретрай, `LLMError`).
4. `MockSearchProvider` (новости, ТГ-каналы, mos.ru/МЧС, медиа с `media_urls`, часть — перепечатки
   одного текста для демонстрации дублей) + `TavilySearchProvider`.
5. `channel_dna.py`: `analyze_posts` (без LLM) + `ensure_channel_dna`.
6. `pipeline.py`: этапы STAGES, CHAIN SEARCH (≥2 итерации в демо, ≤ `CHAIN_MAX_ITERATIONS`),
   `asyncio.gather` агентов, запись `search_iterations`/`sources`/`media_items`/`reports`/`drafts` v1,
   живой `progress_json` (commit после каждого шага), «Кому позвонить» через
   `poller.ensure_author_profile`, финальный `workflow.transition(..., report_ready, None)`.
   Для правдоподобной анимации в mock-режиме — небольшие `asyncio.sleep` (0.3–0.8 с) между шагами,
   отключаемые при `PIPELINE_INLINE`.

Зависимости: `workflow.transition`, `poller.ensure_author_profile` (DEV-BACKEND).

## DEV-BACKEND

Файлы: `app/integrations/max/*`, `app/services/workflow.py`, `app/services/metrics.py`,
`app/routers/api.py`, `app/seed.py`.

1. `workflow.py`: `can_transition`, `available_transitions`, `transition` (таблица уже задана).
2. `max/mock.py`: 4–5 чатов (Чертаново Северное/Центральное/Южное, Бирюлёво…), ~40 сообщений:
   релевантные сценарию (с временем, деталями, фото), пересылки/копипаст (дубли), мусор;
   профили: у части авторов телефон, у части нет.
3. `max/poller.py`: `sync_once`, `ensure_author_profile`, `start_poller`. `max/bot_api.py` — реальный Bot API.
4. `routers/api.py`: все эндпоинты из CONTRACTS.md §6 (права, коды ошибок, diff через difflib,
   board, contacts).
5. `metrics.py`: `compute_metrics` по `METRIC_KEYS`.
6. `seed.py`: расширить — синк mock-MAX при seed, 3–4 демо-расследования в разных статусах
   (для живой доски оператора и метрик), немного фидбэка.

Зависимости: `pipeline.start_investigation` (DEV-AI) — вызывать в `/run` и при `autorun`.

## DEV-FRONTEND

Файлы: `app/routers/pages.py`, `app/templates/*`, `app/static/*` (`style.css`, `app.js`).

1. `base.html` (тёмная шапка, меню по роли, имя/роль, «Выйти»), `partials/_macros.html`, `error.html`.
2. `login.html` — карточки пользователей по ролям, пароль `demo` предзаполнен.
3. `dashboard.html` — 4 представления по ролям.
4. `investigation_new.html` — «Что произошло?», выбор канала, «Запустить NEWSFLOW».
5. `investigation.html` — 7 вкладок; живой прогресс (опрос `/api/.../progress`), цепочка поиска,
   справка с ссылками на источники (`source_ids` → якоря), источники с метками
   дубль/первоисточник/уникальность (дубли свёрнуты), «Кому позвонить» (`tel:`, юр. оговорка о
   согласии участников чатов), медиа-карточки (права «не проверены», «возможно старое»),
   черновик, история; кнопки переходов из `allowed_transitions`; блок «норм/стрём».
6. `editor.html` — AI-версия vs правка, diff-подсветка, подсветка неподтверждённых утверждений,
   клик по утверждению → источник, «Вернуть продюсеру» / «Готово для SMM».
7. `operator.html` — канбан, зависшие, метрики, лента фидбэка, MAX-чаты (вкл/выкл, синк), каналы и DNA.
8. `feedback.html`.

Данные — напрямую через session/сервисы в `pages.py` или fetch к `/api`. До готовности API
можно строить вёрстку на seed-данных и фикстурах.

## QA

Файлы: `tests/*`, `pytest.ini` (`asyncio_mode = auto`, `testpaths = tests`).

1. `conftest.py` (см. CONTRACTS.md §8): in-memory SQLite, `PIPELINE_INLINE=true`, poller выключен,
   фикстуры `client`, `login_as(role)`, `session`.
2. Юнит: `uniqueness` (нормализация, shingles, Jaccard, кластеры ≥0.6, первоисточник = самый ранний,
   score), `workflow` (вся таблица переходов × роли, operator может всё, история пишется).
3. API: 401/403 по ролям для каждого эндпоинта, CRUD расследования, contacts скрыты от editor/smm.
4. e2e: полный сценарий из DoD (producer → исследование ≥2 итерации → справка/источники/контакты →
   editing → правка с diff → ready → published → feedback → operator board/metrics).
5. Смоук: `import app.main`, старт lifespan без ключей.
Тесты против ещё не реализованных частей помечать `@pytest.mark.xfail(raises=NotImplementedError)`
и снимать метку по мере готовности.

## Этапы

| Этап | Результат |
|---|---|
| 1. Фундамент (тимлид) | ✅ этот коммит |
| 2. Параллельная разработка | uniqueness + workflow + mock MAX/LLM/search → api → pipeline → страницы |
| 3. Интеграция | демо-сценарий кликами < 5 мин, pytest зелёный |
| 4. Полировка | дизайн, seed, README, docker compose up |
