"""Seed-данные (зона DEV-BACKEND).

Запуск: ``python -m app.seed`` (пересоздаёт таблицы: ``--reset``). При старте приложения
main.py вызывает ``seed_if_empty`` (если users пуста).

Что создаётся:
- редакторы ``editor``, ``editor2`` (пароль ``demo``; ролей в v2 нет);
- 3 условных канала с историей постов (≥10 постов, разный стиль) + Channel DNA, если DEV-AI готов;
- MAX-чаты и сообщения — через ``poller.sync_once`` (по умолчанию MockMaxClient);
- 5 исторических сюжетов (3 переданы дальше, 1 не подтвердился, 1 на проверке) со справкой,
  источниками, черновиками AI + правками редактора и отметками фактов — чтобы список сюжетов и
  статистика не были пустыми на демо. Статусы v1 в спецификациях HISTORY отображаются на v2
  (``_V1_STATUS``), логины v1 — на editor/editor2 (``_V1_USER``).
  Сценарий «Чертаново, хлопок ~14:30» НЕ сеется — его запускают вживую.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.models import (
    Channel,
    Draft,
    DraftAuthorKind,
    FactMark,
    FactState,
    Investigation,
    InvestigationStatus,
    MediaItem,
    MediaKind,
    Report,
    SearchIteration,
    Source,
    SourceKind,
    StatusHistory,
    User,
    utcnow,
)

log = logging.getLogger("newsflow.seed")
S = InvestigationStatus

SEED_USERS: list[dict] = [
    {"login": "editor", "name": "Елена Воронина", "title": "Редактор"},
    {"login": "editor2", "name": "Олег Кравцов", "title": "Редактор"},
]

# Спецификации HISTORY написаны в терминах v1 — отображаем на модель v2.
_V1_USER = {"producer": "editor", "producer2": "editor2", "editor": "editor", "smm": "editor", "operator": "editor"}
_V1_STATUS = {  # None — шаг пропускается (в v2 нет отдельного статуса)
    "researching": S.researching, "report_ready": S.review, "editing": None, "ready": S.handed_off,
    "published": S.handed_off, "rejected": S.rejected,
}


class _V1:  # статусы v1, на которые ссылается HISTORY
    researching, report_ready, editing, ready, published, rejected = (
        "researching", "report_ready", "editing", "ready", "published", "rejected")

SEED_CHANNELS: list[dict] = [
    {
        "name": "Москва Срочно",
        "handle": "@msk_srochno",
        "description": "Короткие срочные новости, много эмодзи, заголовок капсом.",
        "sample_posts": [
            "⚡️СРОЧНО: НА ВАРШАВСКОМ ШОССЕ СТОЛКНУЛИСЬ ТРИ МАШИНЫ\n\nДвижение в сторону центра "
            "затруднено. Пострадавших нет. Подробности уточняются.\n\n#москва #дтп",
            "🔥ПОЖАР В ЖИЛОМ ДОМЕ НА ПРОФСОЮЗНОЙ\n\nИз окон 5 этажа идёт дым, на месте работают "
            "пожарные. Жильцов эвакуируют.\n\n#москва #пожар",
            "❗️ИНТЕРВАЛЫ НА СЕРПУХОВСКО-ТИМИРЯЗЕВСКОЙ ЛИНИИ УВЕЛИЧЕНЫ\n\nПричина — технические "
            "работы. Ждём обновлений.\n\n#метро",
            "⚡️ЗАДЫМЛЕНИЕ НА СТАНЦИИ «ПРАЖСКАЯ»\n\nПассажиров вывели в вестибюль, поезда идут без "
            "остановки. Пострадавших нет.\n\n#метро #москва",
            "🚨ПРОРЫВ ТРУБЫ НА КИРОВОГРАДСКОЙ\n\nУлицу заливает кипятком, движение перекрыто. "
            "Жителям 4 домов отключили отопление.\n\n#юао #жкх",
            "❗️В ЦЕНТРЕ ПЕРЕКРЫЛИ ТВЕРСКУЮ\n\nИдёт подготовка к мероприятию. Объезд — по Бульварному "
            "кольцу.\n\n#москва #пробки",
            "⚡️ГРАД В МОСКВЕ\n\nВ южных районах выпал крупный град, жители публикуют фото помятых "
            "машин. МЧС предупреждает о штормовом ветре.\n\n#погода",
            "🔥ГОРИТ СКЛАД В БИРЮЛЁВЕ\n\nПлощадь пожара — около 500 кв. м. Дым видно из соседних "
            "районов. Присвоен второй номер сложности.\n\n#пожар #юао",
            "🚨ОТКЛЮЧЕНИЕ СВЕТА В ЗЯБЛИКОВЕ\n\nБез электричества остались около 20 домов. "
            "Аварийные бригады на месте.\n\n#юао",
            "❗️НА МКАД ЗАТОР 8 БАЛЛОВ\n\nСкорость в районе Варшавского шоссе — 10 км/ч. "
            "Причина — ДТП с грузовиком.\n\n#пробки",
            "⚡️В МЕТРО ЗАДЕРЖАЛИ ПОДОЗРИТЕЛЬНОГО МУЖЧИНУ\n\nСтанция «Чертановская», вход временно "
            "закрыт. Работает полиция.\n\n#метро",
            "🚨АВАРИЯ НА ВОДОПРОВОДЕ В ЧЕРТАНОВЕ\n\nБез холодной воды 12 домов. Подвоз воды "
            "организован у управы.\n\n#чертаново #жкх",
        ],
    },
    {
        "name": "Южный округ сегодня",
        "handle": "@yuao_today",
        "description": "Районное медиа ЮАО: спокойный тон, подробности, цитаты жителей.",
        "sample_posts": [
            "В Чертанове отремонтируют сквер у пруда\n\nРаботы начнутся в мае. По словам жителей, "
            "главное — новые скамейки и освещение. Управа обещает закончить к осени.",
            "Жители Бирюлёва жалуются на шум со стройки\n\n«Работают даже ночью», — рассказала нам "
            "Ирина с улицы Липецкой. В управе обещали проверить.",
            "На Кировоградской улице прорвало трубу теплосети\n\nКоммунальщики работали всю ночь. "
            "«Тепло дали к утру, но во дворе до сих пор лужи», — говорит житель дома 22.\n\n"
            "В МОЭК обещают восстановить асфальт за неделю.",
            "В Зябликове открылась новая библиотека\n\nВ ней есть коворкинг и детский зал. "
            "Работает без выходных с 10 до 22.",
            "Как в Чертанове Южном прошёл субботник\n\nЖители высадили 30 кустов сирени у пруда. "
            "«Пришли целыми семьями», — рассказала организатор Наталья.",
            "Что изменится на автобусных маршрутах ЮАО\n\nС понедельника маршрут 182 будет ходить "
            "до станции «Пражская». Интервал сократят до 8 минут в час пик.",
            "В Бирюлёве Западном обрушился балкон\n\nПострадавших нет. Жильцы говорят, что жаловались "
            "на трещины ещё весной. Жилинспекция начала проверку.",
            "В поликлинике на Чертановской начали принимать по вечерам\n\nТерапевты и педиатры "
            "работают до 21:00. Запись — через портал mos.ru.",
            "У пруда в Борисовских прудах появились новые лодочные станции\n\nПрокат открыт по "
            "выходным. Цена — 500 рублей в час.",
            "Жители Чертанова собрали подписи против вырубки деревьев\n\n«Это последний зелёный "
            "двор в квартале», — говорит Светлана. Управа обещает провести публичные слушания.",
            "Ярмарка выходного дня вернулась на Варшавское шоссе\n\nФермеры из Тульской и Рязанской "
            "областей привезут мёд, сыр и овощи. Работает с 8 до 20.",
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
            "Закрывают ли поликлинику №170?\n\n✅ Известно: поликлиника работает в обычном режиме.\n"
            "❌ Не подтвердилось: «закрытие с понедельника» — это старое объявление о ремонте "
            "одного кабинета.\n\nИсточник: ДЗМ.",
            "Почему в Чертанове пахнет гарью\n\n✅ Известно: в Бирюлёве горит склад, ветер южный.\n"
            "❓ Неизвестно: превышены ли нормы ПДК — ждём данных Мосэкомониторинга.",
            "Разбираем видео «взрыва» на Варшавке\n\n❌ Это видео 2021 года из другого города. "
            "Проверили по обратному поиску и погоде на кадре.",
            "Что известно об обрушении балкона в Бирюлёве\n\n✅ Пострадавших нет.\n✅ Дом 1972 года, "
            "капремонт был запланирован на 2027.\n❓ Почему не проверили раньше — ответа управы нет.",
            "Град в Москве: правда ли, что «разбиты сотни машин»\n\n✅ Град был в ЮАО и ЮЗАО.\n"
            "❓ Масштаб ущерба — оценок страховщиков пока нет.\n❌ Фото с «градом размером с яйцо» — "
            "из Краснодара.",
            "Отключение света в Зябликове: что известно\n\n✅ Причина — авария на подстанции.\n"
            "✅ Свет вернули через 3 часа.\n❓ Компенсации — МОЭСК не комментирует.",
            "Как отличить старое фото от нового\n\n1. Обратный поиск по картинке.\n2. Погода и "
            "листва на кадре.\n3. Вывески и машины.\nСохраните себе.",
            "Слух о «газовой утечке по всему району» — не подтвердился\n\nМосгаз: была одна "
            "локальная проверка в одном доме. Массовых отключений нет.",
            "Что известно о перекрытии Тверской\n\n✅ Перекрытие до 23:00.\n❓ Причина официально "
            "не названа.\n\nИсточник: ЦОДД.",
        ],
    },
]

# ---------------------------------------------------------------- исторические расследования
STAGE_KEYS = [
    ("input", "Разбор зацепки"), ("search", "Цепочка поиска"), ("intelligence", "Анализ и уникальность"),
    ("media", "Поиск медиа"), ("dna", "Channel DNA"), ("report", "Справка и черновик"),
]

# Источники: (kind, title, url, snippet, minutes_from_created, cluster, uniqueness, important, primary)
HISTORY: list[dict[str, Any]] = [
    {
        "title": "Задымление на станции метро «Пражская»",
        "lead": "В чатах пишут, что на «Пражской» задымление, людей выводят наверх.",
        "channel": "@msk_srochno", "producer": "producer", "editor": "editor",
        "days_ago": 3.2, "ttd_min": 6,
        "flow": [(_V1.researching, "producer", 0, "Запуск NEWSFLOW"), (_V1.report_ready, None, 6, None),
                 (_V1.editing, "producer", 14, "Справка ок, в редактуру"),
                 (_V1.ready, "editor", 31, None), (_V1.published, "smm", 40, None)],
        "entities": {"summary": "Задымление на станции «Пражская»", "event_type": "задымление",
                     "object": "станция метро", "keywords": ["Пражская", "задымление", "метро"],
                     "location": {"text": "ст. м. «Пражская»", "district": "Чертаново Северное", "address": None},
                     "time": {"text": "около 08:40", "iso": None}, "status": "confirmed"},
        "iterations": [
            ([("web", "задымление Пражская метро", 5), ("max_chats", "Пражская дым", 4),
              ("official", "метрополитен Пражская", 1)], "Первичный поиск по зацепке."),
            ([("web", "Пражская задымление причина эскалатор", 3), ("official", "МЧС Пражская", 1)],
             "Нашли сообщения пассажиров; ищем официальную причину и время закрытия станции."),
        ],
        "sources": [
            ("official", "Пресс-служба метрополитена: станция «Пражская» закрыта на вход",
             "https://t.me/metro_official/1", "Из-за задымления в вестибюле станция временно закрыта на вход.", 5, 1, 1.0, True, True),
            ("web", "Москва 24: задымление на «Пражской»", "https://www.m24.ru/news/praga",
             "Пассажиров вывели в вестибюль, пострадавших нет.", 9, 2, 0.82, True, True),
            ("web", "Перепечатка: задымление на «Пражской»", "https://example-news.ru/praga",
             "Пассажиров вывели в вестибюль, пострадавших нет.", 15, 2, 0.1, False, False),
            ("max_chat", "Чат «ЖК «Северное Чертаново»»: «дым у эскалатора»", "max://chat/chat_chert_north/h001",
             "Дым у нижнего эскалатора, всех выводят, 08:41", 2, 3, 0.94, True, True),
            ("official", "МЧС: возгорание мусора под эскалатором", "https://mchs.gov.ru/news/1",
             "Причина — тление мусора под эскалатором, ликвидировано за 15 минут.", 25, 4, 0.9, True, True),
        ],
        "media": [("photo", "/static/demo/hist/praga_smoke.jpg", 3, "чат «ЖК «Северное Чертаново»»", False)],
        "summary": "Утром на станции «Пражская» произошло задымление из-за тления мусора под эскалатором; "
                   "пострадавших нет, станция была закрыта на вход около 30 минут.",
        "facts": [("Задымление в вестибюле станции «Пражская» около 08:40", [3, 0], "confirmed", "08:40"),
                  ("Причина — тление мусора под эскалатором", [4], "confirmed", None)],
        "unconfirmed": [("В соцсетях писали о пострадавших", [], "false", None)],
        "draft_title": "⚡️ЗАДЫМЛЕНИЕ НА СТАНЦИИ «ПРАЖСКАЯ»",
        "draft_body": "Утром пассажиров вывели в вестибюль из-за дыма у нижнего эскалатора. "
                      "Станция была закрыта на вход около 30 минут.\n\nПо данным МЧС, причина — тление "
                      "мусора под эскалатором. Пострадавших нет.\n\n#метро #москва",
        "claims": [("Станция была закрыта на вход около 30 минут", [0], "confirmed"),
                   ("причина — тление мусора под эскалатором", [4], "confirmed"),
                   ("Пострадавших нет", [1], "confirmed")],
        "edit_body": "Утром пассажиров вывели в вестибюль из-за дыма у нижнего эскалатора. "
                     "Станция была закрыта на вход около 30 минут.\n\nПо данным МЧС, причиной стало тление "
                     "мусора под эскалатором. Пострадавших нет.\n\n#метро #москва",
        "marks": [("c3", "Пострадавших нет", FactState.confirmed, "producer"),
                  ("unconfirmed-1", "В соцсетях писали о пострадавших", FactState.false, "producer")],
        "feedback": [("producer", "good", "Быстро и чисто, первоисточник из чата сработал")],
    },
    {
        "title": "Прорыв трубы теплосети на Кировоградской улице",
        "lead": "Жители Кировоградской пишут, что улицу заливает кипятком, идёт пар.",
        "channel": "@yuao_today", "producer": "producer", "editor": "editor",
        "days_ago": 2.1, "ttd_min": 8,
        "flow": [(_V1.researching, "producer", 0, "Запуск NEWSFLOW"), (_V1.report_ready, None, 8, None),
                 (_V1.editing, "producer", 20, None), (_V1.ready, "editor", 62, "Переписала под тон канала"),
                 (_V1.published, "smm", 75, None)],
        "entities": {"summary": "Прорыв трубы теплосети на Кировоградской", "event_type": "авария ЖКХ",
                     "object": "теплосеть", "keywords": ["Кировоградская", "прорыв трубы", "кипяток"],
                     "location": {"text": "Кировоградская ул., 22", "district": "Чертаново Центральное", "address": "Кировоградская ул., 22"},
                     "time": {"text": "ночью", "iso": None}, "status": "confirmed"},
        "iterations": [
            ([("web", "прорыв трубы Кировоградская", 4), ("max_chats", "кипяток Кировоградская", 6)],
             "Первичный поиск по зацепке."),
            ([("official", "МОЭК Кировоградская авария", 1), ("media", "Кировоградская пар фото", 2)],
             "Есть очевидцы из чатов; ищем данные МОЭК о сроках и число отключённых домов."),
            ([("web", "Кировоградская отопление восстановлено", 2)], "Проверяем, дали ли тепло."),
        ],
        "sources": [
            ("max_chat", "Чат ЖК «Чертановский парк»: «улицу заливает кипятком»", "max://chat/chat_chert_center/h101",
             "У дома 22 из-под асфальта бьёт кипяток, пар до 3 этажа", 1, 1, 1.0, True, True),
            ("max_chat", "Пересылка в чат «Чертаново Южное | Объявления»", "max://chat/chat_chert_south/h102",
             "У дома 22 из-под асфальта бьёт кипяток, пар до 3 этажа", 6, 1, 0.05, False, False),
            ("official", "МОЭК: локализован порыв на Кировоградской", "https://moek.ru/press/1",
             "Порыв локализован, отопление 4 домов восстановят к утру.", 40, 2, 1.0, True, True),
            ("web", "Южный округ: последствия аварии", "https://yuao.example.ru/kirov",
             "Во дворе дома 22 остались лужи, асфальт восстановят за неделю.", 300, 3, 0.85, True, True),
            ("media", "Видео пара над улицей", "https://t.me/yuao_video/22", "Видео очевидца", 5, 4, 0.9, True, True),
        ],
        "media": [("video", "/static/demo/hist/kirov_steam.mp4", 5, "t.me/yuao_video", False),
                  ("photo", "/static/demo/hist/kirov_old.jpg", -500, "Яндекс.Картинки", True)],
        "summary": "Ночью на Кировоградской улице прорвало трубу теплосети; МОЭК локализовала порыв, "
                   "отопление 4 домов восстановили к утру.",
        "facts": [("Прорыв трубы у дома 22 по Кировоградской", [0], "confirmed", None),
                  ("Без отопления остались 4 дома", [2], "confirmed", None)],
        "unconfirmed": [("Пострадал прохожий", [], "unconfirmed", None)],
        "draft_title": "🚨ПРОРЫВ ТРУБЫ НА КИРОВОГРАДСКОЙ",
        "draft_body": "Ночью у дома 22 прорвало трубу теплосети, улицу залило кипятком. "
                      "Без отопления остались 4 дома.\n\nМОЭК локализовала порыв, тепло обещают к утру. "
                      "Есть сведения о пострадавшем прохожем.\n\n#юао #жкх",
        "claims": [("у дома 22 прорвало трубу теплосети", [0], "confirmed"),
                   ("Без отопления остались 4 дома", [2], "confirmed"),
                   ("Есть сведения о пострадавшем прохожем", [], "unconfirmed")],
        "edit_title": "На Кировоградской улице прорвало трубу теплосети",
        "edit_body": "Авария случилась ночью у дома 22. «Пар стоял до третьего этажа», — рассказали жители "
                     "в домовом чате.\n\nВ МОЭК сообщили, что порыв локализован, а отопление в четырёх домах "
                     "восстановили к утру. Асфальт во дворе обещают переложить в течение недели.",
        "marks": [("c3", "Есть сведения о пострадавшем прохожем", FactState.unconfirmed, "editor"),
                  ("c2", "Без отопления остались 4 дома", FactState.confirmed, "producer")],
        "feedback": [("producer", "good", "Норм тема для района, черновик пришлось сильно переписать под тон")],
    },
    {
        "title": "Слух о закрытии поликлиники №170",
        "lead": "В чатах Чертанова пишут, что поликлинику №170 закрывают с понедельника.",
        "channel": "@gorod_bez_paniki", "producer": "producer2", "editor": None,
        "days_ago": 1.4, "ttd_min": 5,
        "flow": [(_V1.researching, "producer2", 0, "Запуск NEWSFLOW"), (_V1.report_ready, None, 5, None),
                 (_V1.rejected, "producer2", 18, "Слух, первоисточник — старое объявление")],
        "entities": {"summary": "Слух о закрытии поликлиники №170", "event_type": "слух",
                     "object": "поликлиника", "keywords": ["поликлиника 170", "закрытие"],
                     "location": {"text": "Чертаново", "district": "Чертаново Центральное", "address": None},
                     "time": {"text": "с понедельника", "iso": None}, "status": "unconfirmed"},
        "iterations": [
            ([("max_chats", "поликлиника 170 закрывают", 7), ("web", "поликлиника 170 закрытие", 2)],
             "Первичный поиск по зацепке."),
            ([("official", "ДЗМ поликлиника 170", 1)], "Все сообщения в чатах — копии одного скриншота; ищем официальный ответ."),
        ],
        "sources": [
            ("max_chat", "Скриншот объявления в чате", "max://chat/chat_chert_center/h201",
             "Поликлиника закрывается с понедельника!!! перешлите", 0, 1, 1.0, False, True),
            ("max_chat", "Копия в чате Бирюлёво", "max://chat/chat_biryulevo/h202",
             "Поликлиника закрывается с понедельника!!! перешлите", 3, 1, 0.0, False, False),
            ("max_chat", "Копия в чате Зябликово", "max://chat/chat_yasny_zyablikovo/h203",
             "Поликлиника закрывается с понедельника!!! перешлите всем", 4, 1, 0.08, False, False),
            ("official", "ДЗМ: поликлиника №170 работает в обычном режиме", "https://mosgorzdrav.ru/news/170",
             "Закрыт на ремонт один кабинет, приём ведётся в штатном режиме.", 12, 2, 1.0, True, True),
        ],
        "media": [],
        "summary": "Слух не подтвердился: по данным ДЗМ, поликлиника работает, на ремонт закрыт один кабинет.",
        "facts": [("Поликлиника №170 работает в обычном режиме", [3], "confirmed", None)],
        "unconfirmed": [("Поликлинику закрывают с понедельника", [0], "false", None)],
        "draft_title": "Закрывают ли поликлинику №170?",
        "draft_body": "✅ Известно: поликлиника работает в обычном режиме.\n❌ Не подтвердилось: «закрытие "
                      "с понедельника» — на ремонт закрыт один кабинет.\n\nИсточник: ДЗМ.",
        "claims": [("поликлиника работает в обычном режиме", [3], "confirmed"),
                   ("на ремонт закрыт один кабинет", [3], "confirmed")],
        "marks": [("unconfirmed-1", "Поликлинику закрывают с понедельника", FactState.false, "producer2")],
        "feedback": [("producer2", "bad", "Стрём: чистый вброс, тема не наша")],
    },
    {
        "title": "Обрушение балкона на Медынской улице",
        "lead": "В Бирюлёве Западном на Медынской обрушился балкон, жильцы прислали фото.",
        "channel": "@yuao_today", "producer": "producer2", "editor": "editor",
        "days_ago": 0.12, "ttd_min": 7,  # ~3 часа назад; на редактуре дольше STALE_AFTER_MINUTES
        "flow": [(_V1.researching, "producer2", 0, "Запуск NEWSFLOW"), (_V1.report_ready, None, 7, None),
                 (_V1.editing, "producer2", 15, "Нужен комментарий жилинспекции")],
        "entities": {"summary": "Обрушение балкона на Медынской", "event_type": "обрушение",
                     "object": "балкон жилого дома", "keywords": ["Медынская", "балкон", "обрушение"],
                     "location": {"text": "Медынская ул.", "district": "Бирюлёво Западное", "address": None},
                     "time": {"text": "утром", "iso": None}, "status": "unconfirmed"},
        "iterations": [
            ([("max_chats", "балкон Медынская", 3), ("web", "обрушение балкона Бирюлёво", 2)],
             "Первичный поиск по зацепке."),
            ([("official", "жилинспекция Медынская", 0), ("media", "балкон Медынская фото", 1)],
             "Есть фото из чата; официального комментария пока нет."),
        ],
        "sources": [
            ("max_chat", "Чат «Бирюлёво — Медынская»: фото обрушения", "max://chat/chat_biryulevo/h301",
             "Упал балкон с 4 этажа, внизу никого не было, фото", 0, 1, 1.0, True, True),
            ("web", "Районная группа: обрушился балкон", "https://vk.com/biryulevo/1",
             "Жильцы говорят, что жаловались на трещины весной.", 30, 2, 0.7, True, True),
            ("media", "Фото обломков во дворе", "https://t.me/yuao_video/31", "Фото обломков", 10, 3, 0.95, True, True),
        ],
        "media": [("photo", "/static/demo/hist/balcony.jpg", 2, "чат «Бирюлёво — Медынская»", False)],
        "summary": "Утром на Медынской улице обрушился балкон 4 этажа, пострадавших нет; "
                   "официального комментария жилинспекции пока нет.",
        "facts": [("Обрушился балкон 4 этажа", [0, 2], "confirmed", None)],
        "unconfirmed": [("Жильцы жаловались на трещины весной", [1], "unconfirmed", None)],
        "draft_title": "В Бирюлёве Западном обрушился балкон",
        "draft_body": "Утром на Медынской улице обрушился балкон 4 этажа. Пострадавших нет.\n\n"
                      "Жильцы говорят, что жаловались на трещины ещё весной. Жилинспекция пока не комментирует.",
        "claims": [("обрушился балкон 4 этажа", [0, 2], "confirmed"),
                   ("Пострадавших нет", [0], "unconfirmed"),
                   ("жаловались на трещины ещё весной", [1], "unconfirmed")],
        "marks": [("c2", "Пострадавших нет", FactState.unconfirmed, "editor")],
        "feedback": [],
    },
    {
        "title": "Отключение света в Зябликове",
        "lead": "В ЖК «Ясный» и соседних домах нет света уже час.",
        "channel": "@msk_srochno", "producer": "producer2", "editor": "editor",
        "days_ago": 0.9, "ttd_min": 4,
        "flow": [(_V1.researching, "producer2", 0, "Запуск NEWSFLOW"), (_V1.report_ready, None, 4, None),
                 (_V1.editing, "producer2", 10, None), (_V1.ready, "editor", 22, None)],
        "entities": {"summary": "Отключение света в Зябликове", "event_type": "авария энергосети",
                     "object": "жилые дома", "keywords": ["Зябликово", "нет света"],
                     "location": {"text": "Зябликово", "district": "Зябликово", "address": None},
                     "time": {"text": "вечером", "iso": None}, "status": "confirmed"},
        "iterations": [
            ([("max_chats", "нет света Зябликово", 8), ("web", "отключение электричества Зябликово", 3)],
             "Первичный поиск по зацепке."),
            ([("official", "МОЭСК Зябликово авария", 1)], "Ищем причину и сроки восстановления."),
        ],
        "sources": [
            ("max_chat", "Чат ЖК «Ясный»: «нет света во всём корпусе»", "max://chat/chat_yasny_zyablikovo/h401",
             "Света нет с 19:10 во всех корпусах, лифты стоят", 0, 1, 1.0, True, True),
            ("official", "МОЭСК: авария на подстанции", "https://rossetimr.ru/press/1",
             "Повреждение на подстанции, электроснабжение 20 домов восстановлено к 22:15.", 50, 2, 1.0, True, True),
            ("web", "Новости ЮАО: Зябликово без света", "https://yuao.example.ru/zyab",
             "Около 20 домов остались без электричества.", 25, 3, 0.75, True, True),
        ],
        "media": [],
        "summary": "Вечером из-за аварии на подстанции около 20 домов в Зябликове остались без света; "
                   "электроснабжение восстановили через три часа.",
        "facts": [("Без света около 20 домов", [1, 2], "confirmed", None),
                  ("Причина — авария на подстанции", [1], "confirmed", None)],
        "unconfirmed": [],
        "draft_title": "🚨ОТКЛЮЧЕНИЕ СВЕТА В ЗЯБЛИКОВЕ",
        "draft_body": "Около 20 домов остались без электричества из-за аварии на подстанции. "
                      "Лифты стояли, жители сидели без света с 19:10.\n\nЭлектроснабжение восстановили "
                      "к 22:15.\n\n#юао",
        "claims": [("Около 20 домов остались без электричества", [1, 2], "confirmed"),
                   ("из-за аварии на подстанции", [1], "confirmed"),
                   ("Электроснабжение восстановили к 22:15", [1], "confirmed")],
        "edit_body": "Около 20 домов остались без электричества из-за аварии на подстанции. "
                     "Лифты стояли, жители сидели без света с 19:10.\n\nЭлектроснабжение восстановили "
                     "к 22:15, сообщили в МОЭСК.\n\n#юао #зябликово",
        "marks": [],
        "feedback": [("producer2", "good", "Норм, быстро собрали")],
    },
]


def _progress_done(started: datetime, finished: datetime) -> dict[str, Any]:
    step = (finished - started) / len(STAGE_KEYS)
    stages, log_items = [], []
    for i, (key, title) in enumerate(STAGE_KEYS):
        s0, s1 = started + step * i, started + step * (i + 1)
        stages.append({"key": key, "title": title, "status": "done",
                       "started_at": s0.isoformat(timespec="seconds"),
                       "finished_at": s1.isoformat(timespec="seconds"), "note": None})
        log_items.append({"ts": s1.isoformat(timespec="seconds"), "stage": key, "level": "info",
                          "message": f"{title}: готово"})
    return {"state": "done", "percent": 100, "current_stage": None,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"), "error": None,
            "stages": stages, "log": log_items}


def _thumb(url: str, kind: str, old: bool) -> str | None:
    """Офлайн-превью (SVG data URI от DEV-AI); без mock_data — None."""
    try:
        from app.ai.mock_data import svg_thumb
    except Exception:  # noqa: BLE001
        return None
    label = url.rsplit("/", 1)[-1].split(".")[0].replace("_", " ")
    return svg_thumb(label + (" (архив)" if old else ""), bg="#4c566a" if old else "#3b4252", kind=kind)


def _items(prefix: str, rows: list[tuple], ids: list[int]) -> list[dict[str, Any]]:
    return [
        {"id": f"{prefix}-{n}", "text": text, "source_ids": [ids[i] for i in refs], "state": state, "time": tm}
        for n, (text, refs, state, tm) in enumerate(rows, start=1)
    ]


async def _seed_investigation(session: AsyncSession, spec: dict[str, Any], users: dict[str, User],
                              channels: dict[str, Channel], now: datetime) -> None:
    created = now - timedelta(days=spec["days_ago"])
    at = lambda minutes: created + timedelta(minutes=minutes)  # noqa: E731
    producer = users[_V1_USER[spec["producer"]]]
    editor = users.get(_V1_USER[spec["editor"]]) if spec.get("editor") else None
    flow = [(_V1_STATUS[st], _V1_USER.get(who) if who else None, m, c)
            for st, who, m, c in spec["flow"] if _V1_STATUS[st] is not None]
    # подряд идущие одинаковые статусы (ready -> published) схлопываем, берём последний
    dedup: list[tuple] = []
    for step in flow:
        if dedup and dedup[-1][0] == step[0]:
            dedup[-1] = (step[0], dedup[-1][1], step[2], step[3] or dedup[-1][3])
        else:
            dedup.append(step)
    flow = dedup
    last_status, last_at = flow[-1][0], at(flow[-1][2])
    channel = channels[spec["channel"]]
    handed = last_status == S.handed_off

    inv = Investigation(
        title=spec["title"], lead_text=spec["lead"], channel_id=channel.id,
        status=last_status, owner_id=producer.id, origin="manual",
        entities_json=spec["entities"], progress_json=_progress_done(at(0), at(spec["ttd_min"])),
        created_at=created, updated_at=last_at, signal_at=created - timedelta(minutes=4),
        review_at=at(spec["ttd_min"]),
        handoff_to=f"Выпускающий редактор «{channel.name}»" if handed else None,
        handoff_channel="telegram" if handed else None,
        handoff_channel_id=channel.id if handed else None,
        handoff_comment=(spec.get("feedback") or [(None, None, None)])[0][2] if handed else None,
        handed_off_at=last_at if handed else None,
        handed_off_by_id=(editor or producer).id if handed else None,
    )
    session.add(inv)
    await session.flush()

    # история статусов
    prev = None
    session.add(StatusHistory(investigation_id=inv.id, from_status=None, to_status=S.new,
                              user_id=producer.id, comment="Зацепка создана", created_at=created))
    prev = S.new
    for to, who, minutes, comment in flow:
        session.add(StatusHistory(investigation_id=inv.id, from_status=prev, to_status=to,
                                  user_id=users[who].id if who else None, comment=comment,
                                  created_at=at(minutes)))
        prev = to

    # цепочка поиска
    for n, (queries, rationale) in enumerate(spec["iterations"], start=1):
        qj = [{"agent": a, "query": q, "results": r} for a, q, r in queries]
        session.add(SearchIteration(investigation_id=inv.id, n=n, queries_json=qj, rationale=rationale,
                                    results_count=sum(r for *_, r in queries),
                                    created_at=at(spec["ttd_min"] * n / (len(spec["iterations"]) + 1))))

    # источники
    src_objs: list[Source] = []
    for kind, title, url, snippet, minutes, cluster, uniq, important, primary in spec["sources"]:
        src = Source(investigation_id=inv.id, kind=SourceKind(kind), title=title, url=url, snippet=snippet,
                     published_at=at(minutes), iteration_n=1 if minutes < 10 else 2, cluster_id=cluster,
                     uniqueness_score=uniq, is_important=important, is_primary=primary,
                     created_at=at(spec["ttd_min"] / 2))
        session.add(src)
        src_objs.append(src)
    await session.flush()
    ids = [s.id for s in src_objs]

    for kind, url, minutes, origin, old in spec["media"]:
        thumb = _thumb(url, kind, old)
        session.add(MediaItem(investigation_id=inv.id, source_id=ids[0] if ids else None, kind=MediaKind(kind),
                              url=thumb if url.startswith("/static/demo/") else url, thumbnail_url=thumb, origin=origin,
                              captured_at=at(minutes), relation="Кадр с места события" if not old
                              else "Похоже на старое фото — найдено раньше события",
                              possibly_old=old))

    # справка (9 разделов, CONTRACTS §5)
    facts = _items("facts", spec["facts"], ids)
    unconf = _items("unconfirmed", spec["unconfirmed"], ids)
    msk = lambda m: (at(m) + timedelta(hours=3)).strftime("%H:%M")  # noqa: E731  (подпись в МСК)
    timeline = [{"id": f"timeline-{i}", "text": f"{msk(m)} — {t}", "source_ids": [ids[i - 1]],
                 "state": None, "time": msk(m)}
                for i, (_, t, _, _, m, *_rest) in enumerate(spec["sources"], start=1)]
    timeline.sort(key=lambda x: x["time"])
    sources_items = [{"id": f"sources-{i}", "text": s.title, "source_ids": [s.id], "state": None, "time": None}
                     for i, s in enumerate((s for s in src_objs if s.is_primary), start=1)]
    media_items = [{"id": f"media-{i}", "text": f"{k}: {o}" + (" (возможно старое)" if old else ""),
                    "source_ids": [], "state": None, "time": None}
                   for i, (k, _u, _m, o, old) in enumerate(spec["media"], start=1)]
    report = {
        "version": 1, "generated_at": at(spec["ttd_min"]).isoformat(timespec="seconds"), "title": spec["title"],
        "sections": [
            {"key": "summary", "title": "Коротко", "text": spec["summary"], "items": []},
            {"key": "facts", "title": "Что известно (что/где/когда)", "text": "", "items": facts},
            {"key": "timeline", "title": "Хронология", "items": timeline},
            {"key": "confirmed", "title": "Подтверждено", "items": [f for f in facts if f["state"] == "confirmed"]},
            {"key": "unconfirmed", "title": "Не подтверждено / слухи", "items": unconf},
            {"key": "contradictions", "title": "Противоречия", "items": []},
            {"key": "sources", "title": "Ключевые источники и первоисточники", "items": sources_items},
            {"key": "media", "title": "Медиа", "items": media_items},
            {"key": "to_verify", "title": "Что проверить и кому позвонить",
             "items": [{"id": f"to_verify-{i}", "text": f"Проверить: {u['text']}", "source_ids": u["source_ids"],
                        "state": "unconfirmed", "time": None} for i, u in enumerate(unconf, start=1)]},
        ],
    }
    session.add(Report(investigation_id=inv.id, report_json=report, created_at=at(spec["ttd_min"])))

    # черновики: v1 AI, v2 правка редактора (если была)
    claims = [{"id": f"c{i}", "text": t, "source_ids": [ids[r] for r in refs], "status": st, "note": None}
              for i, (t, refs, st) in enumerate(spec["claims"], start=1)]
    session.add(Draft(investigation_id=inv.id, version=1, author_kind=DraftAuthorKind.ai, title=spec["draft_title"],
                      body=spec["draft_body"], claims_json=claims, created_at=at(spec["ttd_min"])))
    if spec.get("edit_body") and editor is not None:
        edit_at = next((at(m) for st, _w, m, _c in flow if st == S.handed_off), last_at)
        session.add(Draft(investigation_id=inv.id, version=2, author_kind=DraftAuthorKind.editor,
                          author_user_id=editor.id, title=spec.get("edit_title") or spec["draft_title"],
                          body=spec["edit_body"], claims_json=claims, created_at=edit_at - timedelta(minutes=2)))

    for claim_id, text, state, who in spec["marks"]:
        session.add(FactMark(investigation_id=inv.id, claim_id=claim_id, fact_text=text, state=state,
                             user_id=users[_V1_USER[who]].id, created_at=at(spec["ttd_min"] + 5)))


async def _seed_max(session: AsyncSession) -> None:
    from app.integrations.max.poller import sync_once

    try:
        res = await sync_once(session)
        log.info("Seed MAX: chats=%s messages=%s errors=%s", res.chats, res.new_messages, res.errors)
    except Exception:  # noqa: BLE001
        log.exception("Seed MAX sync failed")
        await session.rollback()


async def _seed_dna(session: AsyncSession, channels: list[Channel]) -> None:
    try:
        from app.ai.channel_dna import ensure_channel_dna

        for ch in channels:
            await ensure_channel_dna(session, ch)
        await session.commit()
    except NotImplementedError:
        log.info("Channel DNA ещё не реализован — пропускаем")
        await session.rollback()
    except Exception:  # noqa: BLE001
        log.exception("Seed channel DNA failed")
        await session.rollback()


async def seed(session: AsyncSession, *, with_history: bool = True, with_max: bool = True) -> None:
    users = {u["login"]: User(**u) for u in SEED_USERS}
    session.add_all(users.values())
    channels = {c["handle"]: Channel(**c) for c in SEED_CHANNELS}
    session.add_all(channels.values())
    await session.flush()

    if with_history:
        now = utcnow()
        for spec in HISTORY:
            await _seed_investigation(session, spec, users, channels, now)
    await session.commit()

    await _seed_dna(session, list(channels.values()))
    if with_max:
        await _seed_max(session)


async def seed_if_empty(session: AsyncSession) -> bool:
    """Засеять, если нет ни одного пользователя. True — если сеяли."""
    count = await session.scalar(select(func.count()).select_from(User))
    if count:
        return False
    await seed(session)
    return True


async def _main(reset: bool) -> None:
    logging.basicConfig(level=logging.INFO)
    await db.init_db(drop=reset)  # init_db сам пересоздаёт схему v1
    async with db.session_scope() as session:
        done = await seed_if_empty(session)
    await db.dispose()
    print("seeded" if done else "already seeded (use --reset)")


if __name__ == "__main__":
    asyncio.run(_main(reset="--reset" in sys.argv))
