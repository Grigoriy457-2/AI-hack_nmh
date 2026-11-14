# NEWSFLOW AI — ТЗ на MVP (хакатон NMH)

Цель: веб-сайт, который жюри может «потыкать» за 3–5 минут: от информационной зацепки до справки с источниками и черновика поста, с ролями редакции и интеграцией домовых чатов из мессенджера **MAX**.

Стек: **Python 3.12, FastAPI, SQLAlchemy 2.0 (async), MySQL (aiomysql), asyncio**. UI — серверные шаблоны Jinja2 + немного vanilla JS (без сборки фронта). Для локального запуска без Docker допускается `DATABASE_URL=sqlite+aiosqlite:///./newsflow.db`; для прода/демо — MySQL через `docker-compose.yml`.

## 0. Принципы
1. NEWSFLOW не публикует сам — решения принимают люди.
2. Каждый факт в справке/черновике имеет ссылку на источник (`source_ids`).
3. CHAIN SEARCH: AI сам порождает следующие поисковые гипотезы (макс. 3 итерации).
4. **Демо должно работать без интернета и без ключей**: все внешние провайдеры (LLM, web search, MAX) имеют mock-реализацию с правдоподобными данными (сценарий «Чертаново, хлопок у жилого дома ~14:30»). Реальные провайдеры включаются переменными окружения.

## 1. Роли (вход — выбор пользователя на странице логина, seed-пользователи, пароль `demo`)
| Роль | Что делает в системе |
|---|---|
| **Продюсер** | Создаёт зацепку, запускает исследование, проверяет справку, помечает факты подтверждён/не подтверждён, видит **контакты авторов уникальных сообщений** (телефон из профиля MAX) и звонит им, передаёт на редактуру. Оставляет оценку темы «норм / стрём» + комментарий. |
| **Редактор** | **Редактор, а не копирайтер**: получает готовый AI-черновик и *правит* его. UI — режим правки текста машины: diff между AI-версией и правкой, подсветка неподтверждённых фактов, переход от утверждения к источнику. Кнопки «Вернуть продюсеру» / «Готово для SMM». |
| **SMM** | Видит готовые материалы, отмечает «Опубликовано». |
| **AI-оператор** (один на редакцию) | Следит за выполнением задач (доска всех расследований по статусам, просроченные/зависшие, кто ответственный), анализирует эффективность проекта (дашборд метрик), собирает обратную связь от продюсеров (норм/стрём по темам), управляет источниками (подключённые чаты MAX, каналы, Channel DNA). |

## 2. Статусная модель расследования
`new` (Новая зацепка, продюсер) → `researching` (NEWSFLOW) → `report_ready` (продюсер проверяет) → `editing` (редактор) → `ready` (SMM) → `published` (SMM). Плюс `rejected` (продюсер счёл тему стрёмной/неподтверждённой). Каждый переход пишется в `status_history` (кто, когда, комментарий). Переходы разрешены только соответствующей роли (AI-оператор может всё).

## 3. Интеграция MAX (домовые чаты)
- Модуль `app/integrations/max/`: интерфейс `MaxClient` с методами `list_chats()`, `fetch_messages(chat_id, since)`, `get_user_profile(user_id)` → `{name, phone|None, username}`.
- `MaxBotApiClient` — реальная реализация через Bot API MAX (`https://platform-api.max.ru`, токен `MAX_BOT_TOKEN`, httpx async). Бот добавлен в чат администратором с согласия участников (юр. оговорка в UI).
- `MockMaxClient` — 4–5 домовых чатов района (Чертаново и др.), ~40 сообщений, часть релевантна сценарию, часть мусор/дубли/пересылки; у части авторов в профиле есть телефон, у части нет.
- Фоновый asyncio-таск (`lifespan`) периодически (раз в N сек) подтягивает новые сообщения в БД `chat_messages`. Плюс кнопка «Синхронизировать сейчас» у AI-оператора.
- В RADAR источник `max_chats` ищет по сохранённым сообщениям (ключевые слова + LLM-релевантность).

## 4. Уникальность сообщений
Для каждого найденного сообщения/публикации считается:
- **Дубликаты**: нормализация текста (lowercase, без пунктуации/эмодзи/ссылок) + shingles (3-граммы слов) + Jaccard ≥ 0.6 → один кластер. Пересланные/скопированные сообщения не считаются независимыми подтверждениями.
- **uniqueness_score** (0..1) = 1 − max сходство с более ранними сообщениями кластера/всей выборки; первоисточник кластера — самое раннее сообщение.
- **is_important**: LLM (или эвристика в mock) — сообщение содержит новые факты (время, место, число пострадавших, фото/видео, «я видел»).
- **Уникальная/важная информация** = первоисточник кластера с uniqueness ≥ 0.5 и is_important. Для таких авторов: запрос `get_user_profile` → если есть телефон, он показывается продюсеру в блоке **«Кому позвонить»** (имя, телефон, цитата сообщения, чат, время, кнопка `tel:`). Если телефона нет — «телефон скрыт в профиле», ссылка на сообщение. Контакты видны только ролям Продюсер и AI-оператор.

## 5. Пайплайн исследования (asyncio, фоновая задача после «Запустить»)
1. **INPUT**: LLM извлекает сущности (тип события, место, время, объект, ключевые слова, статус «не подтверждено»).
2. **ORCHESTRATOR / CHAIN SEARCH**: генерирует запросы → параллельно (`asyncio.gather`) агенты: `web` (search provider), `max_chats`, `official` (МЧС/МВД — web с фильтром доменов или mock), `media`. По результатам LLM предлагает новые гипотезы → до 3 итераций. Каждая итерация и её запросы сохраняются (показываются в UI как «цепочка поиска»).
3. **INTELLIGENCE**: дедуп + уникальность (п.4), хронология, подтверждённое/неподтверждённое, противоречия, «что проверить».
4. **MEDIA FINDER**: карточки фото/видео (превью, источник, время, связь с событием, права «не проверены», флаг «возможно старое фото»).
5. **CHANNEL DNA**: профили каналов (seed 2–3 условных канала с историей постов; анализ: длина, эмодзи, структура заголовков) → JSON-профиль.
6. **REPORT & DRAFT**: справка (9 разделов из плана) + черновик поста по DNA выбранного канала; каждое утверждение черновика → список source_ids.
Прогресс пишется в БД, страница расследования опрашивает `GET /api/investigations/{id}/progress` раз в 1–2 с и показывает живой лог этапов.

## 6. LLM
`app/ai/llm.py`: интерфейс `LLMClient.complete_json(system, user, schema_hint) -> dict`. Реализации: `AnthropicLLM` (если `ANTHROPIC_API_KEY`, модель `claude-sonnet-5-5`, httpx или SDK `anthropic`), `MockLLM` — детерминированные правдоподобные ответы для демо-сценария (и разумные дженерики для произвольной зацепки). Все промпты — в `app/ai/prompts.py`.

## 7. Web search
`app/integrations/search.py`: интерфейс `SearchProvider.search(query) -> list[SearchResult(title,url,snippet,published_at,source_type)]`. Реализации: mock (новостные порталы, ТГ-каналы, mos.ru/МЧС), опционально реальный (например, `TAVILY_API_KEY` или `SERPER_API_KEY`).

## 8. Данные (SQLAlchemy модели, минимум)
`users(id, name, role)`, `channels(id, name, dna_json, sample_posts)`, `max_chats(id, external_id, title, district, enabled)`, `chat_messages(id, chat_id, external_id, author_external_id, author_name, text, sent_at, has_media, media_url)`, `author_profiles(external_id, name, phone, username, fetched_at)`, `investigations(id, title, lead_text, channel_id, status, producer_id, editor_id, entities_json, created_at, updated_at, progress_json)`, `search_iterations(id, investigation_id, n, queries_json, rationale)`, `sources(id, investigation_id, kind[web|max_chat|official|media], title, url, snippet, published_at, chat_message_id, cluster_id, uniqueness_score, is_important, is_primary)`, `media_items(...)`, `reports(investigation_id, report_json)`, `drafts(id, investigation_id, version, author_kind[ai|editor], title, body, claims_json, created_at)`, `fact_marks(id, investigation_id, fact_text, state[confirmed|unconfirmed|false], user_id, comment)`, `status_history(...)`, `feedback(id, investigation_id, user_id, verdict[good|bad], comment, created_at)`.
Создание таблиц — `Base.metadata.create_all` при старте + seed-скрипт (`python -m app.seed`). Alembic не обязателен.

## 9. Экраны
1. `/login` — выбор пользователя/роли (карточки).
2. `/` — дашборд по роли (продюсер: мои зацепки + «Новая зацепка»; редактор: очередь на редактуру; SMM: готово/опубликовано; AI-оператор: доска задач).
3. `/investigations/new` — поле «Что произошло?», выбор канала, кнопка «Запустить NEWSFLOW».
4. `/investigations/{id}` — вкладки: Прогресс/цепочка поиска · Справка · Источники (с метками дубль/первоисточник/уникальность) · **Кому позвонить** · Медиа · Черновик · История.
5. `/investigations/{id}/edit` — редактор: AI-версия vs правка, diff, подсветка неподтверждённого, клик по утверждению → источник.
6. `/operator` — AI-оператор: канбан статусов, зависшие задачи, метрики (Time to Draft, Source Coverage, Traceability, Draft Acceptance Rate = доля черновиков с малым diff, Fact-check correction rate, доля «норм» в фидбэке), лента обратной связи, управление чатами MAX (вкл/выкл, синк).
7. `/feedback` — форма «норм/стрём» для продюсера (также блок на странице расследования).
Дизайн: аккуратный, современный, тёмная шапка, светлый контент, русские подписи. Без внешних CDN не обойтись — допустим только один CSS-файл своего производства (`app/static/style.css`).

## 10. Структура репозитория
```
app/
  main.py            # FastAPI app, lifespan (create_all, seed-if-empty, MAX poller)
  config.py          # pydantic-settings / os.environ
  db.py              # async engine, session dependency
  models.py          # все ORM-модели
  schemas.py         # pydantic-схемы API
  auth.py            # cookie-сессия: current_user, require_role
  services/          # pipeline.py, uniqueness.py, workflow.py, metrics.py
  ai/                # llm.py, prompts.py, mock_data.py, channel_dna.py
  integrations/      # search.py, max/{client.py, bot_api.py, mock.py, poller.py}
  routers/           # pages.py (HTML), api.py (JSON)
  templates/  static/
  seed.py
tests/
docker-compose.yml  Dockerfile  requirements.txt  .env.example  README.md
```

## 11. Критерии готовности (DoD)
- `pip install -r requirements.txt && uvicorn app.main:app` с SQLite стартует без ключей; `docker compose up` поднимает MySQL+app.
- Полный демо-сценарий проходит кликами за < 5 минут: продюсер → исследование (видно цепочку из ≥2 итераций) → справка с источниками, дубли свёрнуты, блок «Кому позвонить» с телефонами → передать редактору → редактор правит (видно diff) → SMM публикует → продюсер ставит «норм/стрём» → AI-оператор видит всё на доске и в метриках.
- `pytest` зелёный: юнит-тесты уникальности, workflow-переходов и прав ролей, интеграционные тесты API через httpx AsyncClient на SQLite, e2e-тест полного сценария.
