# Telegram-источник (публичные каналы и чаты) — NEWSFLOW AI

Зона DEV-TELEGRAM: пакет `app/integrations/telegram/*` и этот документ. Чужие файлы не менялись —
всё, что нужно встроить бэкенду и фронтенду, описано в разделе **«Как подключить»**.

Цель: на защите подключить реальный публичный канал/чат Telegram и показать, как его сообщения в
реальном времени попадают в ленту «Радар», кластеризуются вместе с MAX (кросс-платформенные дубли),
оцениваются на уникальность и запускают исследование. Telegram работает **ровно как MAX**: те же
сущности «чат» (`max_chats`) и «сообщение» (`chat_messages`), плюс поле платформы.

## 1. Что в пакете

| Файл | Что делает |
|---|---|
| `client.py` | Контракт `TelegramClient` (наследник `MaxClient`: `list_chats` / `fetch_messages(chat_id, since, limit)` / `get_user_profile`) + `add_source`, `remove_source`, `listen` (реальное время), `status`. Dataclass-ы `TgChat`/`TgMessage`/`TgUserProfile` — наследники `MaxChat`/`MaxMessage`/`MaxUserProfile` с доп. полями `platform="telegram"`, `username`, `kind`, `url`, `views`. Фабрика `get_telegram_client()` / `set_telegram_client()`. Хелпер `parse_tg_ref()`. |
| `telethon_client.py` | `TelethonClient` — MTProto, пользовательская сессия. **Основной для защиты.** |
| `bot_api.py` | `TelegramBotApiClient` — Bot API, getUpdates long polling. |
| `web_preview.py` | `TelegramWebPreviewClient` — без авторизации, парсинг `https://t.me/s/<channel>` (`html.parser`). Запасной вариант. |
| `mock.py` | `MockTelegramClient` — офлайн: 2 канала + 1 районный чат, 7 сообщений, пересекающихся со сценарием «Чертаново». **По умолчанию.** |
| `settings.py` | `TelegramSettings` (pydantic-settings, читает `.env`) — ключи `TG_*`. |
| `runner.py` | `start_telegram_sync()` / `stop_telegram_sync()` — фоновые задачи для lifespan: listener + запись в БД через общий поллер MAX. |
| `login.py` | `python -m app.integrations.telegram.login` — однократный интерактивный вход (создаёт сессию). |
| `__main__.py` | `python -m app.integrations.telegram [--provider web] @channel … [--listen]` — проверка из консоли без БД. |

### Провайдеры

| `TG_PROVIDER` | Что читает | Реальное время | Нужно |
|---|---|---|---|
| `mock` (по умолч.) | 3 демо-источника | сообщения «появляются» по времени сценария | ничего |
| `telethon` | публичные **каналы и группы** по `@username`/t.me-ссылке **без вступления** (`get_entity` + `iter_messages`); история есть | `events.NewMessage` для чатов, где аккаунт состоит; для остальных — опрос `iter_messages(min_id)` раз в `TG_POLL_INTERVAL` с (Telegram не шлёт апдейты по чатам, где вы не участник) | `TG_API_ID`, `TG_API_HASH`, сессия (`login`) |
| `bot` | группы/каналы, куда бот добавлен **админом**; истории нет — только сообщения после добавления | long polling getUpdates (~мгновенно) | `TG_BOT_TOKEN`; в группах privacy mode **выключен** (@BotFather → /setprivacy → Disable, затем перевыдать бота в группу) |
| `web` | только публичные **каналы** с включённым веб-превью (`t.me/s/…`); группы — нет | опрос раз в `TG_POLL_INTERVAL` с (кэш 5 с на канал) | ничего (обычный публичный GET) |
| `auto` | telethon (если ключи + файл сессии + пакет) → bot (если токен) → web (если заданы `TG_SOURCES`) → mock | | |

### Идентификаторы (чтобы не пересекаться с MAX в общих таблицах)

- чат: `tg:<username>` (lowercase), без username — `tg:<peer_id>` (напр. `tg:-1001234567890`);
- сообщение: `<message_id>` (уникально в пределах чата — как в MAX, `uq_chat_message_ext` подходит);
- автор: `tg:<user_id|channel_id>`; анонимный пост канала — автор = канал (`tg:<username>`);
  подпись автора в канале (web) — `tg:<username>#<подпись>`.
- `fetch_messages()` для **не-`tg:`** chat_id возвращает `[]` (поллер может пройти по всем чатам БД).
- Время — naive UTC, как везде.

Профиль автора: `name`, `username`, `phone` — **только если Telegram его отдаёт** (Telethon:
`User.phone` виден, если человек открыл номер в настройках приватности / у вас в контактах; Bot API:
только если человек сам поделился своим контактом в чате). Иначе `phone=None` → «телефон скрыт».

## 2. Настройки (`.env`)

```dotenv
TG_PROVIDER=mock            # telethon | bot | web | mock | auto
TG_SOURCES=@moscowach,https://t.me/s/some_channel   # публичные источники через запятую
TG_POLL_INTERVAL=20         # сек между опросами / страховочная синхронизация
TG_BACKFILL=30              # сколько последних сообщений взять при первом подключении
# Telethon
TG_API_ID=1234567           # my.telegram.org → API development tools
TG_API_HASH=0123456789abcdef0123456789abcdef
TG_SESSION_PATH=data/telegram/newsflow   # → data/telegram/newsflow.session
TG_FLOOD_MAX_WAIT=120       # FloodWait до N сек ждём сами, дольше — ошибка
# Bot API
TG_BOT_TOKEN=
```

## 3. Подготовка к защите (что нужно от пользователя)

1. Получить `api_id` и `api_hash`: https://my.telegram.org → войти по номеру → *API development tools*
   → создать приложение (название любое). Лучше отдельный аккаунт редакции, не личный.
2. Прописать в `.env`: `TG_API_ID`, `TG_API_HASH`, `TG_PROVIDER=telethon`, `TG_SOURCES=@канал1,@группа2`.
3. **Один раз самому** выполнить в консоли: `python -m app.integrations.telegram.login` — ввести
   телефон, код из Telegram и (если есть) облачный пароль. Появится `data/telegram/newsflow.session`.
   Этот файл = полный доступ к аккаунту: не коммитить, не пересылать; отозвать — Telegram →
   Настройки → Устройства.
4. Проверить: `python -m app.integrations.telegram --provider telethon @канал --listen`
   (напишите сообщение в тестовую публичную группу — оно появится в консоли).
5. Если сессии нет/не работает — `TG_PROVIDER=web` и публичный канал с превью
   (`python -m app.integrations.telegram --provider web @канал`). Ещё офлайн-вариант — `mock`.

Сценарий показа: открыть «Источники» → «Подключить Telegram» → вставить `t.me/…` → на «Радаре»
появляются сообщения с бейджем Telegram; новое сообщение в группе → в ленте через 1–3 с (Telethon
участник / бот) или ≤ `TG_POLL_INTERVAL` с (публичный канал без вступления / web). В mock-режиме
пост «Москва Live» дословно совпадает с пересылками MAX m09/m10 и раньше их по времени →
кластер «дубль ×3» с первоисточником в Telegram; свидетельство в чате «Чертаново онлайн» (48к1,
двое у машины) — уникальное/важное, у автора открыт телефон → «Кому позвонить».

## 4. Как подключить (для бэкенда и фронтенда)

### 4.1 requirements.txt и .gitignore
```text
# requirements.txt
telethon>=1.36          # проверено с 1.45.0
```
```gitignore
# Telegram: сессия = доступ к аккаунту
*.session
*.session-journal
data/telegram/
```

### 4.2 app/config.py (необязательно, для единообразия)
Пакет читает `TG_*` сам (`app.integrations.telegram.settings.get_tg_settings()`), но для видимости
можно продублировать в `Settings`:
```python
# --- Telegram ---
TG_PROVIDER: Literal["auto", "telethon", "bot", "web", "mock"] = "mock"
TG_SOURCES: str = ""
TG_API_ID: int | None = None
TG_API_HASH: str | None = None
TG_SESSION_PATH: str = "data/telegram/newsflow"
TG_BOT_TOKEN: str | None = None
TG_POLL_INTERVAL: int = 20
TG_BACKFILL: int = 30
TG_FLOOD_MAX_WAIT: int = 120
TG_ENABLED: bool = True        # запускать ли Telegram-задачи в lifespan
```

### 4.3 app/models.py
Минимум — одно поле платформы; остальное — для красивой ленты.
```python
class MaxChat(Base):          # таблица max_chats = «чаты» всех платформ
    ...
    platform: Mapped[str] = mapped_column(String(16), default="max", server_default="max", index=True)  # max | telegram
    username: Mapped[str | None] = mapped_column(String(128))   # @username без @ (Telegram)
    kind: Mapped[str | None] = mapped_column(String(16))        # channel | group | chat
    url: Mapped[str | None] = mapped_column(String(512))        # https://t.me/<username>

class ChatMessage(Base):
    ...
    url: Mapped[str | None] = mapped_column(String(512))        # https://t.me/<username>/<id>
    views: Mapped[int | None] = mapped_column(Integer)
```
- `external_id` чатов Telegram имеют префикс `tg:` → глобальный `unique` на `max_chats.external_id`
  менять не нужно; PK `author_profiles.external_id` тоже не конфликтует (`tg:…`). Опционально
  `AuthorProfile.platform: String(16), default "max"`.
- Платформа сообщения = `message.chat.platform` (денормализация не нужна).
- `SourceKind` можно не трогать: источники из Telegram-чатов — тот же `max_chat` («чат»), платформа
  берётся из `source.chat_message.chat.platform`. Если хочется явно — добавить `tg_chat = "tg_chat"`.
- SQLite: новые колонки появятся только в новой БД (create_all) — удалить `newsflow.db` или
  `ALTER TABLE max_chats ADD COLUMN platform VARCHAR(16) DEFAULT 'max'` и т.п.

### 4.4 app/integrations/max/poller.py — сделать платформонезависимым
`sync_once(session, client)` уже принимает клиента; нужны 3 правки:
```python
platform = getattr(client, "platform", "max")

# 1) при создании чата
chat = MaxChat(external_id=rc.external_id, title=rc.title, district=rc.district, enabled=True,
               platform=platform,
               username=getattr(rc, "username", None), kind=getattr(rc, "kind", None),
               url=getattr(rc, "url", None))

# 2) опрашивать только чаты своей платформы (иначе MAX-клиент пойдёт в tg-чаты и наоборот)
existing = {c.external_id: c for c in (await session.scalars(
    select(MaxChat).where(MaxChat.platform == platform))).all()}

# 3) при вставке сообщения
session.add(ChatMessage(..., url=getattr(m, "url", None), views=getattr(m, "views", None)))
```
`ensure_author_profile` — выбирать клиента по префиксу автора:
```python
if client is None:
    if author_external_id.startswith("tg:"):
        from app.integrations.telegram.client import get_telegram_client
        client = get_telegram_client()
    else:
        client = get_max_client()
```
Пайплайн (`services/pipeline.py`): при сборе сообщений для `max_chats`-агента/кластеризации ничего
менять не нужно — Telegram-сообщения лежат в тех же таблицах. Если где-то вызывается
`sync_once(session)` без клиента «синхронизировать сейчас», добавить второй вызов
`sync_once(session, get_telegram_client())` (при `TG_ENABLED`).

### 4.5 app/main.py — lifespan
```python
from app.integrations.telegram.runner import start_telegram_sync, stop_telegram_sync

tg_tasks = []
if getattr(settings, "TG_ENABLED", True):
    try:
        tg_tasks = start_telegram_sync()          # listener + sync раз в TG_POLL_INTERVAL
    except Exception:
        log.exception("Telegram source failed to start")
yield
await stop_telegram_sync(tg_tasks)              # отмена задач + client.aclose()
```
`start_telegram_sync(interval=None, *, client=None, sync=None, on_message=None)`:
`sync` — функция записи в БД (по умолчанию `poller.sync_now(client)`), `on_message` — доп. хук
(например, мгновенный пуш в SSE `/api/stream`). Без сессии Telethon listener пишет в лог
понятную ошибку («запустите login») и останавливается, приложение не падает.

### 4.6 API (app/routers/api.py)
```python
from app.integrations.telegram.client import TelegramError, get_telegram_client

@router.get("/api/telegram/status")            # провайдер, источники, последняя ошибка
async def tg_status():
    return await get_telegram_client().status()

@router.post("/api/telegram/sources")          # body: {"ref": "https://t.me/xxx" | "@xxx"}
async def tg_add_source(body: dict, session = Depends(get_session)):
    client = get_telegram_client()
    try:
        chat = await client.add_source(body["ref"])
    except TelegramError as e:
        raise HTTPException(400, str(e))       # человекочитаемая причина: приватный, нет превью…
    res = await sync_once(session, client)      # сразу подтянуть последние TG_BACKFILL сообщений
    return {"chat": {"external_id": chat.external_id, "title": chat.title, "kind": chat.kind,
                     "url": chat.url, "members_count": chat.members_count},
            "new_messages": res.new_messages}

@router.delete("/api/telegram/sources/{external_id}")
async def tg_remove_source(external_id: str):
    await get_telegram_client().remove_source(external_id)   # + MaxChat.enabled = False в БД
```
Источники, добавленные через API, живут в памяти клиента; после рестарта они подхватываются
автоматически: поллер вызывает `fetch_messages` для enabled-чатов из БД, а клиент сам
переподключает неизвестный `tg:<username>` (`add_source`). Для надёжности их можно также
перечислить в `TG_SOURCES`.

### 4.7 Фронтенд (шаблоны)
- Лента «Радар» и «Источники»: бейдж платформы по `chat.platform` (MAX / Telegram), ссылка
  «открыть в Telegram» по `message.url`, просмотры `views` для каналов.
- «Источники»: блок «Telegram» — статус (`GET /api/telegram/status`: провайдер, ошибка «нужен
  login»), поле «Ссылка на публичный канал/чат» + кнопка «Подключить» (`POST /api/telegram/sources`),
  ошибки 400 показывать текстом. Юр. оговорка: «Подключаются только публичные источники или чаты,
  где бот добавлен администратором с согласия участников».
- «Кому позвонить»: у Telegram-авторов без телефона — «телефон скрыт», ссылка на сообщение (`url`).

## 5. Ограничения и этика

- **Только публичные источники** (публичный @username) или чаты, где бот добавлен администратором
  с согласия участников. Приватные инвайты (`t.me/+…`, `joinchat`) клиент отклоняет; автоматически
  ни во что не вступаем.
- **Лимиты Telegram**: Telethon — `flood_sleep_threshold=60`, `FloodWaitError` ждём до
  `TG_FLOOD_MAX_WAIT` с и повторяем один раз, дольше — ошибка без повторов; Bot API — 429 →
  `retry_after`; web — 429 → `Retry-After` (≤ 60 с), кэш страницы 5 с, опрос не чаще
  `TG_POLL_INTERVAL`. Не уменьшайте интервал ниже ~10 с при многих источниках.
- **Телефоны**: не собираем скрытые номера и не ищем их сторонними способами — только то, что
  Telegram отдаёт сам (человек открыл номер или поделился контактом).
- Медиа не скачиваем — храним ссылку на пост (`url`).
- **ToS Telegram** (Terms of Service / API ToS) ограничивает использование контента и данных
  пользователей для обучения/работы AI-систем. Демонстрация на защите — пилот; использование в
  продакшене NMH — **на согласование юристам** (вкл. ПДн авторов, 152-ФЗ).
- Сессия Telethon = полный доступ к аккаунту: хранить вне git, отдельный аккаунт редакции.

## 6. Что проверено

- Юнит-проверки (скрипт в scratchpad, без сети): `parse_tg_ref`; парсер web-превью на
  самодельном HTML-фрагменте (текст с `<br>`/сущностями/эмодзи, цитата-ответ не попадает в
  текст, пересылка, фото из `background-image`, просмотры `12.7K`/`3 100`, подпись автора,
  сервисное сообщение отбрасывается, шапка канала и счётчик подписчиков); web-клиент через
  `httpx.MockTransport` (нет превью → понятная ошибка, фильтр `since`, чужой chat_id → `[]`);
  mock (3 источника, 7 сообщений, 2 дословных кросс-дубля с MAX, телефон открыт/скрыт,
  сообщения «из будущего» не отдаются); Bot API через MockTransport (message/channel_post,
  429 → повтор, телефон только из своего контакта, ошибка getChat); Telethon без ключей/сессии →
  `TelegramConfigError` с подсказкой «python -m app.integrations.telegram.login»; фабрика
  (по умолчанию mock); **совместимость с существующим `poller.sync_once`** на SQLite in-memory
  (MAX 40 + Telegram 7 сообщений, повторный sync — 0 новых); runner (новое сообщение → sync);
  базовый `listen` отдаёт только новые сообщения.
- Живой запрос web-превью: `https://t.me/s/telegram` и `@durov` — сообщения, даты, просмотры,
  медиа и счётчик подписчиков парсятся; пагинация `?before=` (48 сообщений за 200 дней);
  несуществующий канал → «нет веб-превью».
- Telethon с реальной сессией **не проверялся** (нет api_id/сессии) — только импорт (telethon 1.45.0)
  и обработка отсутствующей сессии.
