# NEWSFLOW AI — CONTRACTS v3 (реальные данные, источники)

Дополняет `CONTRACTS_v2.md`. Источник требований — `TZ_v3.md` (п.1, 6). Зона DEV-SOURCES:
`app/models.py`, `app/services/sources.py`, `app/routers/api.py`, `app/seed.py`, `app/schemas.py`, `app/main.py`.

## 1. Режим данных — `GET /api/mode` (без входа)

```json
{"data_mode": "live" | "demo", "llm": "anthropic" | "extractive" | "mock",
 "telegram_provider": "web" | "telethon" | "bot" | "mock" | null,
 "max_provider": "bot" | "web" | "mock" | null,
 "search_providers": ["rss", "google_news"],
 "label": "Реальные данные", "rss_ingest": true, "notes": ["…"]}
```
`data_mode` — `settings.DATA_MODE` (DEV-REALDATA, `app/config.py`). `label/rss_ingest/notes` — из
`app.config.data_mode_status()`, если есть. Ошибки провайдеров не пробрасываются.

## 2. Модель источника (`source_chats`, класс `MaxChat` = `SourceChat`)

| поле | v3 | смысл |
|---|---|---|
| `platform` | + `rss` | `max` \| `telegram` \| `rss` (лента СМИ / ведомства) |
| `external_id` | | telegram: `tg:<username>`; max: id чата бота или slug `max.ru/<slug>`; rss: `rss:<URL ленты>` (если > 128 симв. — `rss:sha1:<hex>`) |
| `url` | | t.me / max.ru-ссылка; **для rss — URL самой ленты** |
| `site_url` | new | rss: сайт СМИ |
| `avatar_url`, `subscribers`, `description` | new | из resolve |
| `kind` | | `channel` \| `group` \| `chat` \| `user` \| `feed` |
| `deleted_at` | new | мягкое удаление (см. §4) |

`SourceChatOut` дополнен: `username, kind, url, site_url, avatar_url, subscribers, description`.
`PLATFORM_LABELS["rss"] = "СМИ (RSS)"`. Новые колонки в существующей БД добавляются при старте
(`models.add_missing_columns`, вызывается из lifespan и `python -m app.seed`) — пересоздавать БД не нужно.

Сообщения RSS (`chat_messages`, `platform="rss"`): `external_id` = guid/link записи (≤128, иначе sha1),
`author_external_id = "rss:<host>"`, `author_name` = название ленты, `text` = заголовок + анонс,
`message_url` = ссылка на статью, `sent_at` = pubDate (naive UTC).

## 3. Превью по ссылке — `POST /api/sources/resolve`

Body `{"ref": "https://t.me/telegram" | "@name" | "max.ru/…" | "lenta.ru" | "https://…/rss", "platform"?: "auto"|…}`.
Ничего не сохраняет. Кэш 10 мин (`cached: true`). Пустой `ref` → 422; остальные проблемы → 200 с `error`.

Определение платформы (`sources.detect_platform`): `t.me/…`, `telegram.me/…`, `telegram.dog/…`, `@name`,
`tg:name`, голый `username` → telegram; `max.ru/…`, `web.max.ru/…`, числовой id → max; прочее → rss
(сайт: ищем `<link rel=alternate type=application/rss+xml>`, затем `/rss`, `/rss.xml`, `/feed`).

Ответ `SourcePreviewOut`:
```json
{"platform": "telegram", "platform_label": "Telegram", "ref": "https://t.me/telegram",
 "external_id": "tg:telegram", "title": "Telegram News", "username": "telegram", "kind": "channel",
 "avatar_url": "https://cdn…jpg", "subscribers": 9117111, "description": "…", "url": "https://t.me/telegram",
 "site_url": null, "already_added": false, "chat_id": null, "error": null, "cached": false}
```

## 4. Добавление / удаление

- `POST /api/sources {platform: "telegram"|"max"|"rss"|"auto", ref, title?, district?}` → `SourceChatOut`
  201 (200 — уже был). Название — реальное из resolve (если редактор не задал `title`); при mock-Telegram
  сеть не трогаем (как в v2). rss: 422 с `detail`, если ленту найти не удалось. Сразу первая выгрузка
  (rss — `sources.sync_rss`). Повторное добавление удалённого источника восстанавливает ту же строку (201).
- `DELETE /api/sources/chats/{id}` (алиасы `/api/sources/{id}`, `/api/max/chats/{id}`,
  `/api/telegram/sources/{external_id}`) → **200** `SourceDeleteOut`
  `{"id", "title", "platform", "deleted": true, "archived_messages": N, "story_references": M}`; 404 — нет/уже удалён.
  Семантика: источник скрыт из списков (`deleted_at`), `enabled=false`, все его сообщения `archived=true`
  → уходят из ленты, emerging, поиска пайплайна. Строки `sources` сюжетов и сами сообщения **не удаляются**
  (цитаты, ссылки, «Кому позвонить» в созданных сюжетах целы). Telegram: `client.remove_source(external_id)`.
  Строка остаётся, поэтому поллер не пересоздаёт источник из `list_chats()` клиента.
  *Изменение относительно v2:* был 204 и источник со ссылками оставался в списке выключенным.
- `POST /api/sources/sync` — MAX + Telegram + RSS (`by_platform.rss`).

## 5. Seed

`DATA_MODE=demo` — как раньше. `DATA_MODE=live` — только редакторы, каналы Channel DNA (+DNA) и стартовые
реальные источники `seed.live_start_sources()`: первая найденная функция `default_live_sources()` в
`app.integrations.{catalog,rss,live_sources,sources_catalog,feeds}`, иначе списки config
`LIVE_SOURCES`, `TG_SOURCES`, `MAX_SOURCES`, `RSS_SOURCES`. Никаких синтетических сообщений/сюжетов,
сеть при seed не трогается (названия уточнят поллер/resolve).

## 6. Ожидаемое от DEV-REALDATA (пишем против этих сигнатур, импорт защитный)

```python
async def resolve_telegram(ref: str) -> dict   # или sync; dict | dataclass | pydantic
async def resolve_max(ref: str) -> dict
# поля: {title, username, kind, avatar_url, subscribers, url, platform} (+ external_id?, description?, error?)
```
Ищутся в модулях (первый найденный): `app.integrations.resolve`, `app.integrations.resolver`,
`app.integrations.telegram.resolve`, `…telegram.web_preview`, `…telegram`, `…telegram.client` /
`app.integrations.max.resolve`, `…max.web`, `…max.web_preview`, `…max.public`, `…max`, `…max.client`,
`…max.bot_api`. Рекомендуем `app/integrations/resolve.py`. Пока функций нет — встроенный разбор
публичной страницы `t.me/<name>` / `max.ru/<name>` (og:title, og:image, «N subscribers/members»).
Ошибка «не найден» — `error` в dict или исключение (текст попадёт в `error`). `external_id` для Telegram
всегда `tg:<username>`; для MAX можно вернуть свой (иначе slug).

**RSS-ingest в «Радар».** Чтобы не было дублей: источники `platform="rss"` выгружает
`app.services.sources.sync_rss(session, chat_ids=None) -> SyncResult` (фоновая задача в main.py раз в
`RSS_POLL_INTERVAL` (≥60, по умолч. 300) с, если `MAX_POLLER_ENABLED` и `RSS_INGEST_ENABLED`; учитывает
`RSS_INGEST_MAX_AGE_HOURS`, `RSS_INGEST_PER_FEED`). Стартовые московские ленты — отдайте списком через
`default_live_sources()` (`[{"platform": "rss", "ref": "<URL ленты>", "title": "…", "district"?: "…"}]`,
можно и telegram/max), seed создаст строки. Если делаете свой ingest — используйте те же
`external_id` (§2) и пропускайте источники с `deleted_at`/`enabled=false`; скажите, и я уберу свою задачу.

## Запросы DEV-REALDATA

(сюда — нужные поля моделей; DEV-SOURCES добавит)

- (DEV-REALDATA) Новых полей моделей не нужно — хватает добавленных DEV-SOURCES (`avatar_url`, `subscribers`,
  `description`, `site_url`, `deleted_at`). Поллер (`max/poller.py`) уже пропускает источники с `deleted_at`
  и не трогает `platform="rss"` (их выгружает `sources.sync_rss`, своего RSS-ingest у DEV-REALDATA нет).
- (DEV-REALDATA → QA) `tests/conftest.py`: добавить `"DATA_MODE": "demo"` в `os.environ.update(...)` — тесты
  написаны под синтетику; без этого они идут в live (сеть, без сценария «Чертаново»). С `DATA_MODE=demo`
  проходят все, кроме 3 тестов удаления источника (204 → 200, изменение DEV-SOURCES §4).
- (DEV-REALDATA → FRONTEND) В справке у раздела «Противоречия» в live без LLM `sections[].text` = пояснение
  «…требует LLM» при пустом `items` — показывать текст раздела. `ContactOut`/контакты справки: `platform`
  теперь и `telegram`. `FeedMessage.message_url` в live может быть `null` (у сообщения нет публичной ссылки) —
  не рисовать «открыть».

---

## 7. DEV-REALDATA: режим данных, поиск, MAX/Telegram, анализ (v3.0, 2026-11-15)

Зона: `app/config.py`, `app/integrations/{search,rss,resolve,webutil}.py`, `app/integrations/max/*`,
`app/integrations/telegram/{settings,client,resolve}.py`, `app/ai/*`, `app/services/{pipeline,emerging}.py`.
Исследование MAX — `docs/MAX.md`.

### 7.1 Конфиг (`app/config.py`)
| ключ | по умолч. | смысл |
|---|---|---|
| `DATA_MODE` | `live` | `live` — реальные данные; `demo` — синтетика v2 (сценарий «Чертаново», mock MAX/TG/поиск/LLM) |
| `HTTP_USER_AGENT` | `NewsflowAI/1.0 (newsroom research tool; RSS reader)` | вежливый UA без ПДн, все внешние запросы |
| `HTTP_TIMEOUT` | 12 | сек |
| `LLM_PROVIDER` | `auto` | `auto`: ключ → anthropic; иначе live → `extractive`, demo → `mock`; явные `anthropic\|extractive\|mock` |
| `SEARCH_PROVIDER` | `auto` | `auto`: live → [Tavily\|Serper по ключу] + `rss`; demo → `mock`; явные `rss\|tavily\|serper\|mock` |
| `SEARCH_CACHE_TTL` | 300 | кэш лент/Google News/результатов, сек |
| `GOOGLE_NEWS_ENABLED`, `GOOGLE_NEWS_WHEN` | `true`, `7d` | Google News RSS-поиск, оператор `when:` |
| `RSS_FEEDS` | пусто | URL через запятую — заменяют каталог `rss.DEFAULT_FEEDS` |
| `RSS_INGEST_ENABLED`, `RSS_POLL_INTERVAL`, `RSS_INGEST_MAX_AGE_HOURS`, `RSS_INGEST_PER_FEED` | `true`, 300, 24, 30 | для `sources.sync_rss` (DEV-SOURCES) |
| `MAX_PROVIDER` | `auto` | `auto`: токен → `bot`; иначе live → `web` (метаданные max.ru), demo → `mock` |
| `MAX_API_BASE` / `MAX_API_FALLBACK_BASE` | `https://platform-api2.max.ru` / `https://platform-api.max.ru` | TLS api2 — сертификат Минцифры |
| `MAX_CA_BUNDLE` | — | PEM «Russian Trusted Root CA» (добавляется к certifi) |
| `MAX_CHAT_IDS` | пусто | chat_id чатов/каналов бота (GET /chats отключён MAX с 06.2026) |
| `MAX_SOURCES` | пусто | публичные `max.ru/<slug>` для `MaxPublicClient` |
| `TG_PROVIDER` | `auto` | `auto`: telethon (ключи+сессия) → bot (токен) → live: `web` / demo: `mock` (раньше по умолч. `mock`) |
| `TG_SOURCES` | пусто | пусто в live → стартовый набор `telegram.settings.LIVE_DEFAULT_SOURCES` |

`settings.is_live` / `settings.is_demo`. `app.config.data_mode_status() -> {"mode", "label", "llm", "search",
"max", "telegram", "rss_ingest", "notes": [...]}` — для `GET /api/mode` и бейджа режима.

Тесты/старые окружения: явные `*_PROVIDER=mock` работают в любом режиме, **но сценарий «Чертаново»
(`mock_data.is_scenario`, сценарный mock-поиск) включается только при `DATA_MODE=demo`**.

### 7.2 Поиск (`app/integrations/search.py`, `rss.py`)
- `RssSearchProvider` (`name="rss"`): параллельно (`asyncio.gather`) Google News RSS
  (`news.google.com/rss/search?q=…+when:7d&hl=ru&gl=RU&ceid=RU:ru`) + записи проверенных лент
  `rss.DEFAULT_FEEDS` (14 лент, проверены запросом 2026-11-15: mos.ru, МЧС России, Москва 24, АГН «Москва»,
  Лента.ру/Москва, MSK1.RU, Вечерняя Москва, РИА, Интерфакс, Коммерсантъ, РБК, Известия, Ведомости, Лента.ру).
  Не включены (проверено): tass.ru/rss (403 ботам), moslenta.ru/rss (редирект на auth), ria.ru moscow (404),
  mk.ru moscow (404), gazeta.ru (404), 77.mchs.gov.ru (DNS).
- `domains=` (агент official) → `site:`-фильтр в Google News + домен статьи **или сайта издания**
  (`result_matches`). `OFFICIAL_DOMAINS` расширен (mos.ru, mchs.gov.ru, мвд.рф, sledcom.ru, sobyanin.ru…).
- `source_type="media"` → только результаты с картинкой: `enclosure`/`media:content` ленты или `og:image`
  статьи (логотипы отбрасываются). `captured_at=None` — дата съёмки неизвестна.
- Google News: ссылка раскрывается (1) base64-id старого формата; (2) совпадением заголовка с записью ленты
  каталога того же издания. Иначе остаётся `news.google.com/rss/articles/…` (302 → consent → статья в браузере).
  `extra = {"outlet", "outlet_url", "via": "google_news|rss|tavily|serper", "google_news_url", "resolved", "feed"}`.
- `CompositeSearchProvider` — ключевой провайдер (Tavily/Serper) + RSS, чередование, дедуп по url, кэш.
- `rss.default_live_sources()` → `[{"platform": "rss"|"telegram"|"max", "ref", "title"?}]` для seed live:
  6 московских лент (`Feed.ingest=True`), 6 TG-каналов, 2 канала MAX (только метаданные).

### 7.3 Получение названия по ссылке (`app/integrations/resolve.py`)
`await resolve_source(ref, platform=None)`; по платформам — `telegram.resolve.resolve_telegram(ref)`,
`max.public.resolve_max(ref)`, `resolve.resolve_rss(url)`. Общий ответ:
```json
{"platform": "telegram", "external_id": "tg:moscowmap", "username": "moscowmap", "title": "Новости Москвы",
 "kind": "channel|group|bot|user|feed|unknown", "avatar_url": "https://cdn4.telesco.pe/…", "subscribers": 2617255,
 "description": "…", "url": "https://t.me/moscowmap", "preview": true, "preview_url": "https://t.me/s/moscowmap",
 "posts_available": true, "last_post_at": "2026-11-15T05:18:40", "note": null}
```
MAX: `external_id` = slug `max.ru/<slug>` (`channel_id` — внутренний id MAX отдельно), `posts_available=false`,
`note` — «посты без входа недоступны, нужен MAX_BOT_TOKEN». RSS: `external_id = "rss:<URL ленты>"`, `site_url`.
Ошибки: `TelegramSourceError` / `MaxResolveError` / `ResolveError` (все — человекочитаемый текст; приватные
инвайты, «не найден», «это бот, не канал» — в `note`). Кэш 10 мин.

### 7.4 Telegram
- Стартовый набор live (`LIVE_DEFAULT_SOURCES`, web-превью t.me/s проверено 2026-11-15): `@moscowmap`,
  `@moscowach`, `@infomoscow24`, `@ostorozhno_moskva`, `@msk1_news`, `@mos_sobyanin`.
- `choose_provider()` в live без ключей → `web`. Остальное — как в TELEGRAM.md.

### 7.5 MAX
- `MaxPublicClient` (`name="web"`) — live без токена: `list_chats` = `MAX_SOURCES` с реальными названиями,
  `fetch_messages → []`, `status()["note"]`.
- `MaxBotApiClient` — api2 + TLS-fallback, chat_id из `/updates` + `MAX_CHAT_IDS`, каналы тоже (`kind=channel`),
  сообщения `MaxBotMessage(url=Message.url, views=stat.views)`; `max://`-ссылки не генерируются.
- Источник MAX, добавленный по ссылке (slug), и тот же канал из Bot API (числовой chat_id) — разные
  `external_id`; для бота подключайте по chat_id.

### 7.6 Пайплайн и анализ
- live без `ANTHROPIC_API_KEY` → `app.ai.extractive.ExtractiveLLM` (`name="extractive"`): справка/черновик —
  только цитаты заголовков/текстов найденных источников с `source_ids`, временем публикации (МСК) и изданием;
  «Подтверждено» — только `kind=official`; противоречия — только явные (разные числа погибших/пострадавших,
  «взрыв» vs «пожар», «есть/нет пострадавших»), иначе `items=[]` и `sections[contradictions].text` =
  пояснение «требует LLM». `report_json` формат прежний; новое: `sections[].text` может содержать пояснение
  режима; в `intelligence` есть `notes`.
- live: в справке (timeline/facts/confirmed/unconfirmed/contradictions) пункты без `source_ids` отбрасываются
  (и для ответа Claude). Результаты поиска фильтруются по релевантности зацепке (≥40% значимых слов, 2–4) и
  месту (зацепка про Москву → материал должен упоминать Москву/округ/МКАД…).
- Агент `max_chats` ищет по сообщениям MAX и Telegram (без `platform="rss"` — их покрывает агент web).
  `Source.url` чата — только реальная ссылка (`message_url`), иначе `None`; заголовок источника —
  «канал · автор». «Кому позвонить» — без постов каналов (автор = сам канал).
- Медиа live: только реальные URL (enclosure, og:image, превью t.me `cdn*.telesco.pe`), `thumbnail_url` =
  сам URL или `None`; SVG-заглушки только в demo.
- Сбой реального LLM → fallback: demo — MockLLM, live — ExtractiveLLM.

### 7.7 Возникающие сюжеты (live)
`emerging.analyze` в live: тема — по «стемам» первых 220 символов (заголовок поста/статьи), всегда ≥3 общих,
частые слова окна от 8%, без слияния «по чату/району». `message_url` в live — реальная ссылка сообщения,
иначе публичная страница источника, иначе `null` (никаких `max://`/`telegram://` в live).

