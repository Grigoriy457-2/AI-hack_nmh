# MAX: что реально доступно (исследование DEV-REALDATA, 2026-11-15)

Итог одной строкой: **без входа MAX отдаёт только метаданные публичного канала/бота (название,
описание, аватар, число подписчиков) — постов нет.** Сообщения каналов и чатов можно получить
только через **Bot API**: бот организации (токен платформы «MAX для партнёров»), добавленный
**администратором** в канал/чат.

## 1. Публичные страницы `https://max.ru/<name>`

Проверено запросами (`httpx`, User-Agent `NewsflowAI/1.0 …`) на реальных каналах: `max.ru/mosru`
(«Город Москва», официальный канал mos.ru), `max.ru/m24` («Москва 24»), `max.ru/ria`, `max.ru/vmoskva`,
`max.ru/citizenmoscow`, бот `max.ru/ag_mos_ru_bot`. Ссылки взяты со страниц mos.ru, m24.ru, ria.ru, vm.ru.

| Запрос | Ответ |
|---|---|
| `GET https://max.ru/m24` | 200, `text/html`, ~14 КБ, SvelteKit SSR |
| `GET https://max.ru/<несуществующее>` | 404 |
| `GET https://max.ru/join/<hash>` (неверный) | 404 |
| `GET https://max.ru/s/m24` (аналог t.me/s) | 404 — веб-превью ленты **нет** |
| `GET https://max.ru/m24/1` (попытка ссылки на пост) | 200, но пустой `linkInfo: {}` и общие og-теги MAX |
| `GET https://web.max.ru/` | 200, SPA «Веб-версия MAX» — вход по номеру телефона, без API без логина |

Что есть в HTML страницы канала:

```html
<meta property="og:title" content="Город Москва"/>
<meta property="og:description" content="Официальный канал городского портала mos.ru."/>
<meta property="og:image" content="https://i.oneme.ru/i?r=…"/>
<link rel="canonical" href="https://max.ru/mosru"/>
<script type="application/ld+json">{"@type": "Organization", "name": "Город Москва",
  "url": "https://max.ru/mosru", "interactionStatistic": {"@type": "InteractionCounter",
  "interactionType": "https://schema.org/FollowAction", "userInteractionCount": 130878}, …}</script>
```

и встроенные данные SvelteKit (ключи без кавычек):

```js
data: [null, {type: "data", data: {linkInfo: {channel: {title: "Москва 24",
  description: "Рассказываем всё, чем живёт столица…", icon: "https://i.oneme.ru/i?r=…",
  participantsCount: 267002, channelId: 68062315746578}}, isBot: true,
  canonical: "https://max.ru/m24"}}, null]
```

Для бота: `linkInfo: {bot: {id: 17599946, name: "Чат-бот «Активного гражданина»", avatar: "…", description: "…"}}`.
Видимый текст страницы: название, `@username`, описание и призыв «Перейдите в канал… откройте в приложении MAX».
**Постов, дат, ссылок на посты на странице нет.** Аватар (`i.oneme.ru`) отдаётся по прямой ссылке (200, image/jpeg).

Реализация: `app/integrations/max/public.py`
- `resolve_max(ref)` → `{"platform": "max", "external_id": "m24", "channel_id": "68062315746578", "username": "m24",
  "title": "Москва 24", "kind": "channel", "avatar_url": "https://i.oneme.ru/…", "subscribers": 267008,
  "description": "…", "url": "https://max.ru/m24", "posts_available": false, "note": "…"}`;
  принимает `https://max.ru/<name>`, `max.ru/join/<hash>`, `@name`, `max:<name>`; 404 → `MaxResolveError`
  («не найден»); числовой chat_id → `MaxResolveError` («только через Bot API»). Кэш 10 мин.
- `MaxPublicClient` (`MAX_PROVIDER=web`, по умолчанию в `DATA_MODE=live` без токена): источники из
  `MAX_SOURCES`/добавленные по ссылке видны с реальным названием и подписчиками, `fetch_messages() → []`,
  `status()` объясняет, что нужен бот.
- Стартовые live-источники (`rss.default_live_sources()`): `max.ru/mosru`, `max.ru/m24` — только метаданные.

## 2. Bot API (`platform-api2.max.ru`)

Документация: https://dev.max.ru/docs-api (сверено 2026-11-15, markdown-версии страниц `…/index.md`).

- **База**: с 19.07.2026 — `https://platform-api2.max.ru`. Её TLS-сертификат выдан **«Russian Trusted
  Root CA» (Минцифры)**, которого нет в certifi → `httpx` даёт `CERTIFICATE_VERIFY_FAILED`. Решение:
  `MAX_CA_BUNDLE=<pem>` (клиент добавит его к certifi); без него клиент автоматически переходит на
  `https://platform-api.max.ru` (пока отвечает). Проверено: `GET platform-api.max.ru/me` без токена →
  `401 {"code":"verify.token","message":"No access token"}`, с неверным → `"Invalid access_token"`.
- **Токен**: только заголовок `Authorization: <token>` (query `access_token` больше не поддерживается).
  Выдаётся на платформе «MAX для партнёров» (business.max.ru → Чат-боты → Настройки) или ботом
  `max.ru/business_bot` командой «Получить токен». **Создать бота может только верифицированный профиль
  организации, ИП или самозанятого — резидента РФ**; бот проходит модерацию.
- **Что видит бот**:
  - `GET /messages?chat_id=…` — сообщения чата/посты канала **только если бот — администратор**
    (новые первыми, `from`/`to` — Unix ms, `count` до 100). В объекте `Message`: `sender`, `recipient`,
    `timestamp`, `body {mid, text, attachments}`, `link` (forward/reply), `stat.views` (только каналы),
    `url` — **публичная ссылка на пост канала** (у групповых чатов её нет).
  - `GET /chats` (список чатов бота) **с июня 2026 не поддерживается**; chat_id узнаём из событий
    `bot_added` / `message_created` (`GET /updates` long polling — «только для разработки», или webhook
    `POST /subscriptions`), либо задаём вручную (`MAX_CHAT_IDS`). `GET /chats/{chatId}` — title, type
    (chat|channel), link, participants_count, icon.
  - `GET /chats/{chatId}/members?user_ids=` — профиль участника; **телефона в ответе нет**. Телефон
    известен, только если человек сам отправил свой контакт (вложение `contact`, `vcf_info`).
- В чужие публичные каналы бот без прав администратора не читает: «подписаться» ботом на канал СМИ
  нельзя. Т.е. для мониторинга чужих каналов MAX официального пути **нет**.

Реализация: `app/integrations/max/bot_api.py` (`MaxBotApiClient`): база api2 + TLS-fallback,
`list_chats()` = устаревший `GET /chats` (если ещё ответит) + `GET /updates` (bot_added/message_created/
bot_removed) + `MAX_CHAT_IDS` → `GET /chats/{id}`; каналы теперь тоже источники (`kind=channel`);
`MaxBotMessage.url` = `Message.url`, `views` = `stat.views`; вложение без `payload.url` → `has_media=True`,
`media_url=None` (никаких выдуманных `max://`-ссылок в live).

## 3. Что нужно, чтобы читать MAX в продукте

1. Юрлицо/ИП/самозанятый редакции верифицируется на business.max.ru, создаёт бота, получает токен.
2. `.env`: `MAX_BOT_TOKEN=…`, при необходимости `MAX_CA_BUNDLE=certs/russian_trusted_root_ca.pem`,
   `MAX_CHAT_IDS=<id1>,<id2>` (если события `bot_added` уже прошли до запуска).
3. Админ домового чата/канала добавляет бота **администратором** (с согласия участников — 152-ФЗ).
4. Сообщения появятся в «Радаре» при следующей синхронизации (`MAX_POLL_INTERVAL`).

Без токена в `DATA_MODE=live` MAX-источники показываются с реальными названиями/аватарами, но без
сообщений; `GET /api/mode` (`app.config.data_mode_status()`) содержит пояснение в `notes`.
