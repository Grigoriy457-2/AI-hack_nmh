"""Seed-данные (зона DEV-BACKEND; базовая версия от тимлида — расширять можно).

Запуск: ``python -m app.seed`` (пересоздаёт таблицы: ``--reset``). При старте приложения
main.py вызывает ``seed_if_empty`` (если users пуста).

Базовая версия создаёт пользователей всех ролей и 3 условных канала с историей постов.
MAX-чаты и сообщения появляются через poller.sync_once (mock-клиент) — DEV-BACKEND может
дополнительно вызывать его здесь, а также добавить демо-расследования для доски оператора.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.models import Channel, Role, User

SEED_USERS: list[dict] = [
    {"login": "producer", "name": "Анна Смирнова", "role": Role.producer, "title": "Продюсер"},
    {"login": "producer2", "name": "Олег Кравцов", "role": Role.producer, "title": "Продюсер"},
    {"login": "editor", "name": "Елена Воронина", "role": Role.editor, "title": "Редактор"},
    {"login": "smm", "name": "Мария Ким", "role": Role.smm, "title": "SMM-менеджер"},
    {"login": "operator", "name": "Игорь Белов", "role": Role.operator, "title": "AI-оператор"},
]

SEED_CHANNELS: list[dict] = [
    {
        "name": "Москва Срочно",
        "handle": "@msk_srochno",
        "description": "Короткие срочные новости, много эмодзи, заголовок капсом.",
        "sample_posts": [
            "⚡️СРОЧНО: на Варшавском шоссе столкнулись три машины\n\nДвижение в сторону центра затруднено. "
            "Пострадавших нет. Подробности уточняются.\n\n#москва #дтп",
            "🔥ПОЖАР В ЖИЛОМ ДОМЕ НА ПРОФСОЮЗНОЙ\n\nИз окон 5 этажа идёт дым, на месте работают пожарные. "
            "Жильцов эвакуируют.\n\n#москва #пожар",
            "❗️В метро на Серпуховско-Тимирязевской линии увеличены интервалы\n\nПричина — "
            "технические работы. Ждём обновлений.",
        ],
    },
    {
        "name": "Южный округ сегодня",
        "handle": "@yuao_today",
        "description": "Районное медиа ЮАО: спокойный тон, подробности, цитаты жителей.",
        "sample_posts": [
            "В Чертанове отремонтируют сквер у пруда\n\nРаботы начнутся в мае. По словам жителей, "
            "главное — новые скамейки и освещение. Управа обещает закончить к осени.",
            "Жители Бирюлёва жалуются на шум со стройки\n\n«Работают даже ночью», — рассказала "
            "нам Ирина с улицы Липецкой. В управе обещали проверить.",
        ],
    },
    {
        "name": "Город без паники",
        "handle": "@gorod_bez_paniki",
        "description": "Аналитический канал: проверка слухов, структура «что известно / что нет».",
        "sample_posts": [
            "Что известно о задымлении в метро\n\n✅ Известно: состав остановили на 10 минут.\n"
            "❓ Неизвестно: причина задымления.\n\nИсточник: пресс-служба метрополитена.",
            "Слух про отключение воды в ЮАО — не подтвердился\n\nМосводоканал: плановых работ нет. "
            "Скриншот, который гуляет по чатам, — прошлогодний.",
        ],
    },
]


async def seed(session: AsyncSession) -> None:
    for u in SEED_USERS:
        session.add(User(**u))
    for c in SEED_CHANNELS:
        session.add(Channel(**c))
    await session.commit()


async def seed_if_empty(session: AsyncSession) -> bool:
    """Засеять, если нет ни одного пользователя. True — если сеяли."""
    count = await session.scalar(select(func.count()).select_from(User))
    if count:
        return False
    await seed(session)
    return True


async def _main(reset: bool) -> None:
    await db.init_db(drop=reset)
    async with db.session_scope() as session:
        done = await seed_if_empty(session)
    await db.dispose()
    print("seeded" if done else "already seeded (use --reset)")


if __name__ == "__main__":
    asyncio.run(_main(reset="--reset" in sys.argv))
