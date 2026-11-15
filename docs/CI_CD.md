# CI/CD (GitHub Actions)

## CI — `.github/workflows/ci.yml`
Запускается на push в `master`, на каждый pull request и вручную.

| Job | Что делает |
|---|---|
| **Тесты (pytest, SQLite)** | Python 3.12, `pip install -r requirements.txt`, компиляция `app/`, `pytest` (сетевые тесты с маркером `live` исключены). Отчёт JUnit — артефакт `pytest-report`. |
| **Смоук на MySQL 8** | Поднимает MySQL 8 как сервис, `python -m app.seed --reset`, запускает uvicorn и проверяет `/api/health`, вход, `/api/mode`, `/api/feed`, `/api/stories` и страницы `/`, `/stories`, `/sources`, `/stats`; падает, если в логе сервера есть Traceback. |
| **Сборка Docker-образа** | Проверяет, что `Dockerfile` собирается (без публикации). |

Ключи и сеть CI не нужны: всё работает на mock-провайдерах.

## CD — `.github/workflows/cd.yml`
- После **успешного CI на `master`** собирает образ и публикует в GitHub Container Registry: `ghcr.io/<владелец>/ai-hack_nmh:latest` и `:<короткий sha>`. При пуше тега `v*` — образ с тегом версии.
- **Деплой по SSH** выполняется, только если в репозитории заданы секреты (Settings → Secrets and variables → Actions):

| Секрет | Значение |
|---|---|
| `DEPLOY_HOST` | адрес сервера |
| `DEPLOY_USER` | пользователь SSH |
| `DEPLOY_SSH_KEY` | приватный ключ (публичный — в `~/.ssh/authorized_keys` на сервере) |
| `DEPLOY_PATH` | необязательно, папка на сервере (по умолчанию `~/newsflow`) |

  Деплой копирует `docker-compose.prod.yml` на сервер и делает `docker compose pull && up -d`. На сервере нужны Docker с compose-плагином и файл `.env` рядом с compose-файлом (ключи `XAI_API_KEY`, `TG_*`, `MAX_BOT_TOKEN`, `SECRET_KEY` и т.д.). Без секретов шаг деплоя просто пропускается.

## Ручной запуск продакшен-сборки
```bash
NEWSFLOW_IMAGE=ghcr.io/<владелец>/ai-hack_nmh:latest docker compose -f docker-compose.prod.yml up -d
```
Сессия Telegram хранится в томе `telegram_session` (`/srv/data/telegram`). Создать её можно один раз:
```bash
docker compose -f docker-compose.prod.yml run --rm app python -m app.integrations.telegram.login --qr
```
