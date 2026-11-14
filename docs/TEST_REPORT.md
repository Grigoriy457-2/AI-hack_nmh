# NEWSFLOW AI v2 — отчёт QA

Дата: 2026-11-14. Окружение: Windows 11, Python 3.12.6, pytest 9.1.1, pytest-asyncio 1.4.0, SQLite (aiosqlite).
Запуск: `pytest` (конфиг `pytest.ini`, `asyncio_mode = auto`). Полный прогон ~2 мин (из них ~45 с — e2e с настоящим uvicorn).
Контракт: `docs/TZ_v2.md`, `docs/CONTRACTS_v2.md` (вкл. «Изменения» v2.1), `docs/TELEGRAM.md`.

## Итог финального прогона

**380 тестов: 376 прошли, 4 упали (открытые баги №4–№7), 0 skip/xfail.**
Финальный прогон — после того, как файлы фронтенда (`app/templates`, `app/static`, `app/routers/pages.py`)
перестали меняться: последняя правка в 22:35, прогон в 23:03 по местному времени.

| Маркер | Тестов | Что покрывает |
|---|---|---|
| `unit` | 133 | workflow v2, uniqueness, mock-провайдеры, poller, MAX Bot API, Telegram (парсер, клиенты), демо-трикл, stats |
| `api` | 216 | 401, вход, сюжеты, переходы/409, handoff, «Копать глубже», лента, SSE, emerging, источники/Telegram, демо, статистика, граничные случаи |
| `pipeline` | 17 | полный пайплайн на mock, «Копать глубже», TG-контакты |
| `e2e` | 2 | путь редактора (ASGI) + настоящий uvicorn в демо-режиме |
| `smoke` | 29 | `/login`, `/`, `/stories`, `/stories/{id}`, `/sources`, `/stats`, формы, XSS |

Маркеры пересекаются: тесты `pipeline` помечены и как `api`.

Упавшие (баги, не проблемы тестов):
- `test_api_realtime.py::test_feed_junk_not_marked_important` — баг №4
- `test_api_realtime.py::test_sse_hello_server_time_iso` — баг №5
- `test_api_realtime.py::test_demo_restart_without_trickle_keeps_feed` — баг №6
- `test_api_realtime.py::test_demo_restart_keeps_telegram_demo` — баг №7

## Состав набора (v2)

Набор переписан под v2. Удалены тесты ролей, фидбэка, доски оператора и метрик v1:
`test_api_roles.py`, `test_unit_metrics.py`, `test_api_investigations.py`.

| Файл | Маркер | Что покрывает |
|---|---|---|
| `test_unit_workflow.py` | unit | Таблица переходов v2 (user/system) сверяется с эталоном CONTRACTS_v2 §2. Полная матрица, `available_transitions`, подписи кнопок. `review_at` фиксирует первый выход справки. История, flush без commit. Роли/фидбэк удалены из моделей |
| `test_unit_uniqueness.py` | unit | Нормализация, шинглы, кластеры дублей, первоисточник, важность (без изменений с v1) |
| `test_unit_providers.py` | unit | MockMaxClient, MockLLM, MockSearch, Channel DNA, poller, профили авторов |
| `test_unit_max_bot_and_poller.py` | unit | MaxBotApiClient на `httpx.MockTransport`, сетевые ошибки, жизненный цикл поллера, lifespan с поллером |
| `test_unit_telegram.py` | unit | `parse_tg_ref`: варианты ссылок, приватные инвайты, битые имена. Id `tg:`, фабрика (mock по умолчанию). MockTelegramClient: 3 источника, 7 постов, дословный кросс-дубль с m09/m10, «из будущего», профили. **Парсер веб-превью на локальном HTML-фрагменте**: `<br>`, сущности, эмодзи, цитата-ответ, пересылка, фото из `background-image`, просмотры 12.7K / «3 100», подпись автора, сервисное сообщение, шапка и счётчик. `TelegramWebPreviewClient` на MockTransport (без сети): since, кэш, нет превью, 404, приватная ссылка. `sync_once` с TG-клиентом |
| `test_unit_demo_stream.py` | unit | Демо-трикл на **управляемом времени** (`released(now)`/`status(now)`, без sleep): 10 стартовых — фоновый шум, интервалы 10–30 с, finished/next_at, LOOP (новые id `~c1`), новая эпоха (`~e1`), якорь = старт − 125 мин |
| `test_api_auth.py` | api | health, `/api/users` (только editor/editor2, без роли), логин по `user_id`/`login`, demo-login, logout. **401 без входа на 48 защищённых эндпоинтах**. Удалённые эндпоинты v1 → 404/405. Страницы без входа → `/login` |
| `test_api_stories.py` | api | Создание/валидация, StoryOut/StoryDetail (все поля контракта). Алиас `/api/investigations` (скрыт из OpenAPI). Список: фильтры status/mine/q, сортировка. Источники/дубли, `SourceOut.platform`. **«Кому позвонить» видят оба редактора**. Справка, черновики (review/rejected — можно, остальное 409), diff, отметки фактов (upsert по claim_id), `unconfirmed_count` |
| `test_api_workflow_v2.py` | api, pipeline | Допустимые переходы через reject/reopen/stop/run/transition. **Недопустимые → 409** (18 комбинаций «статус × действие» + 7 через `/transition`), без записи в историю. handed_off только через `/handoff`. **Handoff**: check (неподтверждённые из черновика и справки), 422 на пустое/пробельное «кому», неизвестный канал или `channel_id`; warnings при неподтверждённых; после проверки фактов — без warnings; **повторная → 409**; из new/researching/rejected → 409. **«Копать глубже»**: вопрос в `deeper_questions`, запросы по вопросу к web и max_chats, +1 итерация, черновик v+1 (правка редактора сохранена), история; пустой вопрос, накопление вопросов, 1001 символ → 422 |
| `test_api_realtime.py` | api | **Лента** `/api/feed`: форма, порядок, курсор, `since` (id и ISO-время, в т.ч. `Z`), limit, фильтры chat_id/platform. Бейджи сценария. **Кросс-платформенный дубль**: TG «Москва Live» — первоисточник ×2, MAX m09/m10 — `duplicate_of`; TG-копия m12 — дубль MAX. Выключенный чат скрыт. **Emerging**: форма карточки, ключ = `e<id самого раннего>`, связь с `FeedMessage.emerging_key`, MAX+Telegram в одном кластере, параметры, **investigate идемпотентен** (201 → 200 тот же сюжет, в т.ч. от другого редактора), поиск по ключу члена кластера, 404/422, **dismiss** (идемпотентно, `include_dismissed`). **SSE** `/api/stream?timeout=N`: retry, hello, emerging_update, replay по `since`, живые new_message + emerging_update + story_update при параллельных действиях, keepalive, 422. **Демо**: статус, `/api/demo/restart` с быстрым триклом (0.05–0.1 с): архив старых, рост ленты, finished; источники сюжетов сохраняются |
| `test_api_sources.py` | api | Источники после seed, PATCH вкл/выкл/район/название (+алиас), выключенный не синкается, синк идемпотентен. `POST /api/sources` telegram (5 форм ссылки, повтор → 200). **Приватная TG-ссылка → 422 с причиной**, битые имена → 422, неизвестный канал на mock. MAX-источник. DELETE без ссылок и с ссылками из сюжета (отключается, сюжет цел). `/api/telegram/status`, `/api/telegram/sources` (add/delete/ошибки) |
| `test_stats.py` | unit, api | `compute_stats` на пустой БД (None-доли) и на подготовленных данных (все 13 ключей, средние, доли, каналы, recent). `/api/stats` после handoff, алиас `/api/metrics` |
| `test_pipeline.py` | api, pipeline | Полный пайплайн: review, review_at, прогресс 6 этапов, 2–3 итерации CHAIN SEARCH, кластеры и первоисточники, 9 разделов с валидными `source_ids`, контакты с телефонами. **TG-свидетель (tg:700101) в «Кому позвонить» с platform=telegram**. Черновик v1 с claims, медиа, история (system → review), DNA, произвольная зацепка, перезапуск (черновик v2), run из new |
| `test_edge_cases.py` | api | Seed (≥3 статусов, есть handed_off), идемпотентность seed, пересоздание БД со схемой v1. Фоновый пайплайн (`PIPELINE_INLINE=false`): живой прогресс, монотонный percent; во время researching deeper/run/handoff → 409. Ошибка LLM → state=error, статус researching, «Остановить» → review. Все чаты выключены. URL чатовых источников. Кривой JSON, юникод, длинные поля |
| `test_smoke_pages.py` | smoke | `/login`: кнопка «Войти как редактор», ролей v1 нет. Форма логина (с `user_id` и без), неверный пароль, `next` без open redirect, `/logout`. `/` Радар: «Своя зацепка» (форма без JS), «Возможные сюжеты», данные `#radar-data` (лента, курсор, карточки), Telegram, мои сюжеты, нет элементов v1. Формы `/emerging/{key}/investigate` (повтор → тот же сюжет) и `/stories/new`. `/stories`: статусы, фильтр, поиск. `/stories/{id}`: «Копать глубже», «Передать дальше», «Не подтвердилось», «Кому позвонить» с `tel:`, черновик, справка, источники; все 5 статусов; handed_off показывает кому/канал; после настоящего пайплайна; 404; редирект `/investigations/{id}`. `/sources`: источники MAX+TG, юр. оговорка, форма добавления (приватная ссылка не добавляется). `/stats`. **XSS**: lead/title и текст сообщения ленты (в т.ч. `</script>` внутри JSON) экранированы. Статика, нет внешних CDN |
| `test_e2e.py` | e2e | **Полный путь редактора одним тестом**: вход кнопкой → подключение TG через форму → Радар (лента MAX+TG, кросс-дубль) → карточка → «Исследовать» → review (справка, дубли, телефоны, страница) → «Копать глубже» с вопросом → правка (diff) → отметка факта → handoff с параллельным SSE (story_update) → повтор 409 → история, страница, статистика |
| `test_e2e_server.py` | e2e | **Настоящий `uvicorn`** в демо-режиме: поллер, Telegram-runner (mock), трикл 0.4–0.8 с, фоновый пайплайн. Вход формой, лента растёт сама, **SSE читается потоково** (hello < 2 с, new_message по ходу). TG-посты в ленте, карточка → «Исследовать» → живой прогресс → review → «Копать глубже» → review → handoff → перезапуск демо-потока. В логе нет «database is locked» и трейсбэков |

## Устройство тестов

- `tests/conftest.py`: окружение задаётся до импорта `app`:
  - SQLite во временном файле, `PIPELINE_INLINE=true`, поллер выключен;
  - все провайдеры mock, `TG_PROVIDER=mock`;
  - **`DEMO_STREAM_ENABLED=false`**: все 40 сообщений сразу, детерминированно; клиент `MockMaxClient(stream=False)`.
- Якорь сценария фиксируется на сессию: «сейчас − 125 мин», как у демо-потока. Так сообщения MAX/TG «свежие» и попадают в окно возникающих сюжетов в любое время суток.
- Перед каждым тестом сбрасывается глобальное состояние процесса:
  - якорь, демо-поток (`reset_state(epoch=0)`), кэш анализа ленты (`emerging.reset_cache`);
  - флаг TG-runner, клиенты LLM / поиска / MAX / Telegram.
- Каждый тест получает свою БД — копию файла-шаблона (пустая схема или seed). lifespan отрабатывает.
- Клиенты: `editor`, `editor2`, `anon` — `httpx.AsyncClient(ASGITransport)` с cookie. Сам логин проверяется отдельно.
- `tests/nf_helpers.py`:
  - `check`/`data` — строгая проверка кода ответа;
  - `parse_sse` — разбор text/event-stream;
  - `assert_clean_html` — нет трейсбэков и внешних ресурсов;
  - `make_story` (сюжет в нужном статусе) и `build_review_story` (review со справкой, 6 источниками, контактами, черновиком) — данные прямо в БД.
- Хуки xfail/«not implemented» из v1 удалены: никаких skip/xfail, любая ошибка — FAIL.
- Демо-трикл проверяется на управляемом времени (`released(now)`). В API — с интервалом 0.05–0.1 с, без долгих sleep.

## Найденные баги

| # | Зона | Файл:строка | Шаги | Ожидание / факт | Статус |
|---|---|---|---|---|---|
| 1 | backend | `app/services/stories.py:267` (`reopen`) | Сюжет в `researching` → `POST /api/stories/{id}/reopen` | Ожидание: 409 («Вернуть в работу» — только `rejected → review»). Факт: 200, сюжет переходил в review (работало как «Остановить») | **Исправлен** бэкендом во время тестирования (проверка статуса, `stories.py:272`) |
| 2 | backend | `app/services/stories.py:170` (`stop_story`) | Сюжет в `rejected` → `POST /api/stories/{id}/stop` | Ожидание: 409 (только `researching → review`). Факт: 200, rejected → review | **Исправлен** (`stories.py:171`) |
| 3 | backend (пайплайн) | `app/services/pipeline.py:413–425` | Сюжет в review (3 итерации) → `POST /deeper {"question": "Кто владелец сгоревшего Соляриса"}` | Ожидание: CHAIN SEARCH +1 итерация (CONTRACTS_v2 §4.3). Факт: снова 3 итерации — запросы по вопросу шли в итерацию 1, цепочка останавливалась раньше `max_it` | **Исправлен** (`test_deeper_adds_one_chain_iteration` проходит) |
| 4 | backend | `app/services/emerging.py:72–76` (`_important`) | Seed → `GET /api/feed` | Ожидание: бейдж «важное» только у сообщений с фактами; у мусора в `mock_data` `important` не задан, по комментарию это False. Факт: `badges.important=true` у мусора: «Субботник в субботу в 11:00», «Собрание жильцов в четверг в 19:00», «Чья машина перекрыла выезд у 3 подъезда??», «Кто-нибудь нашёл ключи…». Причина: при отсутствии ключа `important` срабатывает эвристика (время + место). Итог — шум на Радаре и в счётчике `important_count` карточек. Фикс: для известных сообщений сценария отсутствие ключа = False | Открыт |
| 5 | backend | `app/routers/api.py:160` (`_sse`, `json.dumps(default=str)`) и `:190` (hello) | `GET /api/stream?timeout=0` | Ожидание: `server_time` в hello — ISO, как в `FeedOut` (`2026-11-14T19:45:43`). Факт: `"2026-11-14 19:45:43.338671"` (str(datetime), с пробелом). `Date.parse` в Safari такое не разбирает. То же с `updated_at` в событии `story_update` (`api.py:170`) | Открыт (minor) |
| 6 | backend | `app/integrations/max/demo_stream.py:221–246` (`restart`) | `DEMO_STREAM_ENABLED=false` (или клиент без трикла) → `POST /api/demo/restart` → `GET /api/feed` | Ожидание: лента после перезапуска не пустая. Факт: 0 сообщений (было 40). Все сообщения mock-чатов архивируются, а синк в режиме без трикла отдаёт те же `external_id` (m01…). Поллер пропускает их как уже известные (`poller.py`, проверка `known_ids` не учитывает `archived`) — лента пустеет навсегда | Открыт |
| 7 | backend | `app/integrations/max/demo_stream.py:232–242` (+ `poller._sync_platform`) | Подключить mock-Telegram → включённый трикл → `POST /api/demo/restart` → синк → `GET /api/feed?platform=telegram` | Ожидание: 7 постов mock-Telegram снова в ленте (якорь = старт − 125 мин, «Изменения» v2.1). Факт: 0. Restart архивирует TG-сообщения, а у mock-TG постоянные `external_id` (8812…) — поллер их больше не вставляет. После «Перезапустить демо-поток» с Радара пропадает кросс-платформенный дубль «Москва Live» ↔ m09/m10 и TG-свидетель. Фикс: не архивировать TG либо давать TG-постам суффикс эпохи / разархивировать при повторной выдаче | Открыт |

Багов в зонах **frontend** и **telegram** финальный прогон не выявил:
- смоук страниц, формы, XSS, `#radar-data` — зелёные;
- парсер web-превью, mock-клиент, `parse_tg_ref` — зелёные.

Открытые баги №4–7 — зона backend.

## Замечания (не баги / решения разработчиков, зафиксированные тестами)

- **Неизвестный публичный TG-канал на mock-провайдере.** Источник сохраняется без проверки: 201, `title=@name`, 0 сообщений. Это решение бэкенда по запросу фронтенда (раньше был 422 с пустым телом). Тест допускает 201 или 422 с `detail`.
- **`POST /transition {to_status: handed_off}`** отвечает 422 («используйте /handoff»), а не 409. Статус не меняется. Тест допускает 409/422.
- **`m01` («Что это так бахнуло??»)** — самое первое сообщение события. Оно не попадает в кластер возникающего сюжета: тематические стемы не совпали. Ключ карточки — по самому раннему сообщению кластера. Не баг контракта, но на демо первая реплика очевидца не в карточке.
- **Статус демо-потока при `DEMO_STREAM_ENABLED=false`** показывает `released=10, total=40, finished=false`, хотя в ленте все 40 сообщений. `active=false`, поэтому UI это не показывает.
- `/api/stream?since=abc` → 422: `since` у SSE — только int. В ленте строка/ISO допустимы.

## Что не покрыто

- MySQL — только SQLite.
- Реальные Telethon / Bot API / web-превью с сетью, `AnthropicLLM`, Tavily/Serper — нужны ключи и сеть. Web-превью проверено только на локальном HTML и MockTransport.
- JS-логика страниц (`app.js`: отрисовка ленты из `#radar-data`, SSE на клиенте, модалка «Передать дальше», diff в браузере) — браузерных тестов нет. Проверены разметка и данные для JS.
