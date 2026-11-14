# NEWSFLOW AI

NEWSFLOW AI — редакционный AI-ассистент (MVP для хакатона NMH). На входе информационная зацепка
(«хлопок у жилого дома в Чертанове около 14:30»). На выходе:
- справка с источниками;
- список очевидцев, которым стоит позвонить;
- черновик поста в стиле выбранного канала.

Работает с ролями редакции и домовыми чатами мессенджера **MAX**. Публикует всегда человек.

Стек: Python 3.12, FastAPI, SQLAlchemy 2.0 (async), MySQL (aiomysql) или SQLite (aiosqlite),
Jinja2 + vanilla JS, asyncio.

> Демо работает **без интернета и без ключей**: LLM, веб-поиск и MAX имеют mock-реализации
> с правдоподобными данными. Реальные провайдеры включаются переменными окружения.

## Быстрый старт (SQLite)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.seed --reset                        # чистая БД: пользователи, каналы, чаты MAX, демо-расследования
uvicorn app.main:app --reload                     # http://127.0.0.1:8000
```

Без `python -m app.seed` тоже работает: при старте таблицы создаются, а пустая БД засевается.
Тесты: `pytest` (~1 минута, SQLite, все провайдеры в mock-режиме).

## Docker (MySQL 8)

```bash
docker compose up --build                         # http://127.0.0.1:8000
```

`docker-compose.yml` поднимает `mysql:8` (utf8mb4) и приложение с
`DATABASE_URL=mysql+aiomysql://newsflow:newsflow@db:3306/newsflow`. Переменные из `.env`
подхватываются, если файл есть.

## Переменные окружения

Полный список — в `.env.example` (скопируйте в `.env`).

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./newsflow.db` | БД |
| `ANTHROPIC_API_KEY` | — | Включает настоящий Claude (`LLM_MODEL=claude-sonnet-5-5`). Без ключа — `MockLLM` |
| `SEARCH_PROVIDER` + `TAVILY_API_KEY` / `SERPER_API_KEY` | `mock` | Реальный веб-поиск (`tavily` или `serper`) |
| `MAX_BOT_TOKEN` | — | Включает MAX Bot API (`platform-api.max.ru`). Без токена — mock-чаты |
| `MAX_POLL_INTERVAL` | `60` | Период фоновой синхронизации чатов MAX, секунды |
| `SECRET_KEY` | dev-значение | Подпись cookie-сессии (в проде поменять) |
| `DEMO_PASSWORD` | `demo` | Пароль всех демо-пользователей |

## Демо-сценарий для жюри (3–5 минут)

Пароль у всех — `demo`. Быстро сменить роль можно из шапки.

| Логин | Роль |
|---|---|
| `producer` (Анна Смирнова) | Продюсер |
| `editor` (Елена Воронина) | Редактор |
| `smm` (Мария Ким) | SMM |
| `operator` (Игорь Белов) | AI-оператор |

1. **Продюсер.** Войдите как `producer`, нажмите «Новая зацепка» и напишите:
   *«Чертаново, хлопок у жилого дома около 14:30»*. Выберите канал и нажмите «Запустить NEWSFLOW».
2. Вкладка **«Прогресс»**: живой лог этапов и **цепочка поиска** из 2–3 итераций. Агенты СМИ/ТГ,
   домовые чаты MAX, официальные источники и медиа работают параллельно. Около 15 секунд.
3. **«Справка»**: 9 разделов, у каждого факта есть ссылки на источники. Факты можно отметить
   «подтверждён / не подтверждён».
   **«Источники»**: дубли и пересылки свёрнуты в кластеры, есть метки «первоисточник» и уникальность.
4. **«Кому позвонить»**: авторы уникальных важных сообщений из чатов MAX. Есть имя, телефон
   (кнопка `tel:`), цитата, чат и время. Если телефон скрыт в профиле, это показано. Блок видят
   только продюсер и AI-оператор.
   Также посмотрите **«Медиа»** (права «не проверены», флаг «возможно старое») и **«Черновик»**.
5. Нажмите «Передать на редактуру».
6. **Редактор.** Войдите как `editor` и откройте материал из очереди. Слева AI-черновик, справа ваша
   правка, внизу живой diff. Неподтверждённые утверждения подсвечены; клик по утверждению ведёт к
   источнику. Сохраните правку и нажмите «Готово для SMM».
7. **SMM.** Войдите как `smm` и нажмите «Опубликовано».
8. **Продюсер.** Поставьте оценку «норм / стрём» с комментарием: на странице расследования или в `/feedback`.
9. **AI-оператор.** Войдите как `operator`, откройте `/operator`. Там:
   - канбан всех расследований и зависшие задачи;
   - метрики: Time to Draft, Source Coverage, Traceability, Draft Acceptance Rate, Fact-check correction rate, доля «норм»;
   - лента обратной связи;
   - чаты MAX (вкл/выкл, «Синхронизировать сейчас») и Channel DNA каналов.

## Архитектура

```
app/
  main.py            FastAPI, lifespan: create_all → seed-if-empty → MAX poller
  config.py db.py models.py schemas.py auth.py (cookie-сессия, require_role)
  services/          pipeline.py (исследование), uniqueness.py (дубли/уникальность),
                     workflow.py (статусы и права), metrics.py
  ai/                llm.py (AnthropicLLM / MockLLM), prompts.py, mock_data.py, channel_dna.py, drafting.py
  integrations/      search.py (mock / Tavily / Serper), max/ (client, bot_api, mock, poller)
  routers/           pages.py (HTML), api.py (JSON /api)
  templates/ static/ Jinja2 + style.css + app.js
tests/               pytest: unit, API, pipeline, e2e, smoke
docs/                TZ.md, CONTRACTS.md, TEAM_PLAN.md, ACCEPTANCE.md, TEST_REPORT.md
```

**Пайплайн** — фоновая asyncio-задача. Этапы:
1. INPUT: извлечение сущностей.
2. CHAIN SEARCH: до 3 итераций, агенты запускаются через `asyncio.gather`; LLM предлагает новые гипотезы.
3. INTELLIGENCE: шинглы, Jaccard ≥ 0.6, первоисточник, uniqueness, важность, профили MAX.
4. MEDIA FINDER.
5. CHANNEL DNA.
6. REPORT & DRAFT: справка из 9 разделов и черновик, где у каждого утверждения есть `source_ids`.

Прогресс пишется в БД, страница опрашивает `GET /api/investigations/{id}/progress`.

**Статусы:** `new → researching → report_ready → editing → ready → published` и `rejected`.
Каждый переход проверяется по роли и пишется в `status_history`.

Контракты модулей и форматы JSON описаны в `docs/CONTRACTS.md`.
