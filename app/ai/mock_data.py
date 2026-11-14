"""Детерминированные данные для MockLLM и mock-поиска: сценарий «Чертаново, хлопок у жилого
дома ~14:30» + дженерики для произвольной зацепки. Зона DEV-AI.

ВАЖНО для DEV-BACKEND (MockMaxClient): чаты, сообщения и профили авторов сценария берите отсюда —
``SCENARIO_MAX_CHATS``, ``scenario_max_messages()`` (или сырой ``SCENARIO_MAX_MESSAGES``) и
``SCENARIO_MAX_PROFILES``. MockLLM (релевантность, справка, «Кому позвонить») опирается на эти
тексты и external_id сообщений (``m01``…), поэтому их нельзя менять независимо.

Время: «событие» = 14:30 МСК = 11:30 UTC (naive UTC в БД). ``scenario_event_time()`` даёт якорь:
сегодня 11:30 UTC, если сейчас уже позже 12:30 UTC, иначе вчера. Сообщения/публикации задаются
смещением в минутах от якоря.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Any

from app.models import utcnow

# ================================================================ время сценария
SCENARIO_EVENT_UTC = (11, 30)  # 14:30 МСК
_anchor: datetime | None = None


def scenario_event_time(now: datetime | None = None) -> datetime:
    """Якорь сценария (naive UTC). Если задан через set_scenario_anchor — он."""
    if _anchor is not None and now is None:
        return _anchor
    now = now or utcnow()
    ev = now.replace(hour=SCENARIO_EVENT_UTC[0], minute=SCENARIO_EVENT_UTC[1], second=0, microsecond=0)
    if now < ev + timedelta(hours=1):
        ev -= timedelta(days=1)
    return ev


def set_scenario_anchor(dt: datetime | None) -> None:
    """Пайплайн выставляет якорь по реальным сообщениям в БД (чтобы веб-поиск и чаты совпали по дате)."""
    global _anchor
    _anchor = dt


from app.ai.text import stem as _stem  # noqa: E402

SCENARIO_LOCATION_STEMS: frozenset[str] = frozenset({_stem("чертаново")})  # «Чертаново», «Чертановская»…
SCENARIO_EVENT_STEMS: frozenset[str] = frozenset(_stem(w) for w in (
    "хлопок", "взрыв", "бахнуло", "горит", "горел", "пожар", "возгорание", "дым", "машина", "автомобиль",
    "сгорел", "огонь", "газ", "мосгаз"))


def is_scenario(text: str) -> bool:
    """Зацепка/запрос относится к демо-сценарию «Чертаново, хлопок»."""
    from app.ai.text import stems

    s = stems(text)
    return bool(s & SCENARIO_LOCATION_STEMS) and bool(s & SCENARIO_EVENT_STEMS)


# ================================================================ превью (офлайн, data: URI)
def svg_thumb(label: str, bg: str = "#3b4252", accent: str = "#ff7a45", kind: str = "photo") -> str:
    """Маленькое SVG-превью 320x180 в data URI (работает без интернета, < 1 КБ)."""
    icon = "▶" if kind == "video" else "◉"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">'
        f'<rect width="320" height="180" fill="{bg}"/>'
        f'<circle cx="250" cy="60" r="38" fill="{accent}" opacity=".55"/>'
        f'<text x="20" y="40" font-size="28" fill="#fff">{icon}</text>'
        f'<text x="20" y="150" font-family="sans-serif" font-size="15" fill="#fff">{label}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# ================================================================ MAX: чаты, профили, сообщения
SCENARIO_MAX_CHATS: list[dict[str, Any]] = [
    {"external_id": "chat_chert_48k2", "title": "Чертановская 48к2 — жильцы", "district": "Чертаново Центральное", "members_count": 214},
    {"external_id": "chat_chert_center", "title": "Соседи Чертаново Центральное", "district": "Чертаново Центральное", "members_count": 1830},
    {"external_id": "chat_chert_north", "title": "ЖК «Северное Чертаново» — соседи", "district": "Чертаново Северное", "members_count": 960},
    {"external_id": "chat_chert_south", "title": "Чертаново Южное | Объявления", "district": "Чертаново Южное", "members_count": 2410},
    {"external_id": "chat_biryulevo", "title": "Бирюлёво Восточное — двор", "district": "Бирюлёво Восточное", "members_count": 540},
]

# user_id -> профиль. phone=None — «телефон скрыт в профиле». Номера вымышленные.
SCENARIO_MAX_PROFILES: dict[str, dict[str, Any]] = {
    "u1001": {"name": "Ирина Ковалёва", "phone": "+7 916 555-01-23", "username": "irina_kov"},
    "u1002": {"name": "Сергей Лебедев", "phone": "+7 925 555-17-40", "username": "s_lebedev"},
    "u1003": {"name": "Пётр Никитин", "phone": None, "username": "petr_n"},
    "u1004": {"name": "Татьяна Орлова", "phone": "+7 903 555-22-81", "username": None},
    "u1005": {"name": "Алексей Гордеев", "phone": None, "username": "gordeev_a"},
    "u1006": {"name": "Наталья Белова", "phone": "+7 977 555-63-09", "username": None},
    "u1007": {"name": "Марина Олейник", "phone": "+7 926 555-48-12", "username": "marina_ol"},
    "u1008": {"name": "Виктор Сомов", "phone": None, "username": None},
    "u1009": {"name": "Людмила Петровна", "phone": None, "username": None},
    "u1010": {"name": "Кирилл Ершов", "phone": "+7 999 555-70-14", "username": "kirill_e"},
    "u1011": {"name": "Дмитрий Захаров", "phone": "+7 915 555-36-27", "username": "dzakharov"},
    "u1012": {"name": "Ольга Миронова", "phone": None, "username": "olga_m"},
    "u1013": {"name": "Светлана К.", "phone": None, "username": None},
    "u1014": {"name": "Андрей Фомин", "phone": "+7 905 555-90-33", "username": None},
    "u1015": {"name": "Гульнара Сафина", "phone": "+7 968 555-11-58", "username": "gulnara_s"},
    "u1016": {"name": "Роман Тихонов", "phone": None, "username": "roman_t"},
    "u1017": {"name": "Евгения Лисина", "phone": "+7 919 555-84-06", "username": "evg_lis"},
    "u1018": {"name": "Николай Ушаков", "phone": None, "username": None},
    "u1019": {"name": "Анна Б.", "phone": None, "username": None},
    "u1020": {"name": "Магазин «Окна Про»", "phone": "+7 495 555-00-00", "username": "okna_pro"},
    "u1021": {"name": "Валентина", "phone": None, "username": None},
    "u1022": {"name": "Игорь С.", "phone": None, "username": None},
    "u1023": {"name": "Елена Р.", "phone": "+7 916 555-77-19", "username": None},
}

_TG_LIVE_TEXT = (
    "⚡️В Чертанове прогремел взрыв у жилого дома. Жители сообщают о громком хлопке около 14:30, "
    "во дворе дым. Предварительно — взрыв газа."
)

# minutes — смещение от события (14:30 МСК); relevant / important — разметка для MockLLM
# (релевантно сценарию / содержит новые факты; у мусора important не задан = False)
SCENARIO_MAX_MESSAGES: list[dict[str, Any]] = [
    # ---------- релевантные (часть — дубли/пересылки)
    {"external_id": "m01", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1001", "author_name": "Ирина Ковалёва", "minutes": -1,
     "text": "Что это так бахнуло?? У нас окна задрожали, 14:29, Чертановская 48к2", "important": True, "relevant": True},
    {"external_id": "m02", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1002", "author_name": "Сергей Лебедев", "minutes": 1,
     "text": "Я видел из окна — во дворе у второго подъезда горит машина, белый седан. Сначала был хлопок, потом сразу огонь из-под капота",
     "has_media": True, "media_url": "https://max.ru/mock/media/m02.jpg", "important": True, "relevant": True},
    {"external_id": "m03", "chat_external_id": "chat_chert_center", "author_external_id": "u1003", "author_name": "Пётр Никитин", "minutes": 6,
     "text": "Я видел из окна — во дворе у второго подъезда горит машина, белый седан. Сначала был хлопок, потом сразу огонь из-под капота",
     "is_forwarded": True, "important": False, "relevant": True},
    {"external_id": "m04", "chat_external_id": "chat_chert_north", "author_external_id": "u1004", "author_name": "Татьяна Орлова", "minutes": 3,
     "text": "Девочки, слышали взрыв в районе Чертановской? Говорят, газ взорвался в доме!", "important": False, "relevant": True},
    {"external_id": "m05", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1005", "author_name": "Алексей Гордеев", "minutes": 4,
     "text": "Пожарная машина уже едет, слышу сирены. Дым чёрный валит со стороны парковки", "important": True, "relevant": True},
    {"external_id": "m06", "chat_external_id": "chat_chert_center", "author_external_id": "u1006", "author_name": "Наталья Белова", "minutes": 5,
     "text": "Говорят, в Чертанове взорвался газ в жилом доме, это правда? Кто рядом живёт?", "important": False, "relevant": True},
    {"external_id": "m07", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1002", "author_name": "Сергей Лебедев", "minutes": 10,
     "text": "Приехали две пожарные машины, тушат. Огонь перекинулся на соседнюю машину, но вроде быстро сбили",
     "has_media": True, "media_url": "https://max.ru/mock/media/m07.mp4", "important": True, "relevant": True},
    {"external_id": "m08", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1007", "author_name": "Марина Олейник", "minutes": 12,
     "text": "Газом не пахнет, я в первом подъезде, у нас всё нормально. Горит только машина на парковке", "important": True, "relevant": True},
    {"external_id": "m09", "chat_external_id": "chat_chert_north", "author_external_id": "u1008", "author_name": "Виктор Сомов", "minutes": 13,
     "text": _TG_LIVE_TEXT, "is_forwarded": True, "important": False, "relevant": True},
    {"external_id": "m10", "chat_external_id": "chat_chert_center", "author_external_id": "u1009", "author_name": "Людмила Петровна", "minutes": 16,
     "text": _TG_LIVE_TEXT, "is_forwarded": True, "important": False, "relevant": True},
    {"external_id": "m11", "chat_external_id": "chat_chert_south", "author_external_id": "u1010", "author_name": "Кирилл Ершов", "minutes": 15,
     "text": "У нас в Южном тоже слышали хлопок около половины третьего, думали салют", "important": False, "relevant": True},
    {"external_id": "m12", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1011", "author_name": "Дмитрий Захаров", "minutes": 18,
     "text": "Полиция перекрыла парковку лентой, машины не выпускают. Свою не могу забрать 😡", "important": True, "relevant": True},
    {"external_id": "m13", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1005", "author_name": "Алексей Гордеев", "minutes": 22,
     "text": "Пожар потушили в 14:52, пожарные сказали, что пострадавших нет", "important": True, "relevant": True},
    {"external_id": "m14", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1012", "author_name": "Ольга Миронова", "minutes": 25,
     "text": "А чья машина? Вроде соседа с 5 этажа, белый Солярис. Он говорит, вечером кто-то крутился у машины", "important": True, "relevant": True},
    {"external_id": "m15", "chat_external_id": "chat_chert_center", "author_external_id": "u1013", "author_name": "Светлана К.", "minutes": 28,
     "text": "Слышала, что двое пострадавших, увезли на скорой. Кто-нибудь знает подробности?", "important": False, "relevant": True},
    {"external_id": "m16", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1001", "author_name": "Ирина Ковалёва", "minutes": 30,
     "text": "Скорая не приезжала, я всё время смотрела с балкона. Только пожарные и полиция", "important": True, "relevant": True},
    {"external_id": "m17", "chat_external_id": "chat_chert_north", "author_external_id": "u1014", "author_name": "Андрей Фомин", "minutes": 35,
     "text": "Полиция перекрыла парковку лентой, машины не выпускают. Свою не могу забрать", "is_forwarded": True, "important": False, "relevant": True},
    {"external_id": "m18", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1015", "author_name": "Гульнара Сафина", "minutes": 40,
     "text": "Мосгаз приходил, ходят по квартирам, проверяют. Сказали, утечки нет", "important": True, "relevant": True},
    {"external_id": "m19", "chat_external_id": "chat_chert_center", "author_external_id": "u1016", "author_name": "Роман Тихонов", "minutes": 42,
     "text": "Мосгаз приходил, ходят по квартирам проверяют. Сказали утечки нет!", "important": False, "relevant": True},
    {"external_id": "m20", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1011", "author_name": "Дмитрий Захаров", "minutes": 60,
     "text": "Эвакуатор увёз сгоревшую машину, парковку открыли", "important": True, "relevant": True},
    {"external_id": "m21", "chat_external_id": "chat_chert_north", "author_external_id": "u1017", "author_name": "Евгения Лисина", "minutes": 8,
     "text": "Видео с Чертановской, горит машина во дворе, сняла с 9 этажа", "has_media": True,
     "media_url": "https://max.ru/mock/media/m21.mp4", "important": True, "relevant": True},
    {"external_id": "m22", "chat_external_id": "chat_chert_south", "author_external_id": "u1018", "author_name": "Николай Ушаков", "minutes": 50,
     "text": "Это уже третья сгоревшая машина за год в нашем районе, кто-то поджигает", "important": False, "relevant": True},
    # ---------- мусор / нерелевантное (в т.ч. с «ловушками»-словами)
    {"external_id": "m23", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1019", "author_name": "Анна Б.", "minutes": -20,
     "text": "Кто-нибудь нашёл ключи с брелоком-мишкой у 3 подъезда?", "relevant": False},
    {"external_id": "m24", "chat_external_id": "chat_chert_north", "author_external_id": "u1021", "author_name": "Валентина", "minutes": -240,
     "text": "Продам детскую коляску, почти новая, 5000 р.", "relevant": False},
    {"external_id": "m25", "chat_external_id": "chat_chert_north", "author_external_id": "u1004", "author_name": "Татьяна Орлова", "minutes": -180,
     "text": "Когда включат горячую воду? Уже неделю нет", "relevant": False},
    {"external_id": "m26", "chat_external_id": "chat_chert_center", "author_external_id": "u1022", "author_name": "Игорь С.", "minutes": -150,
     "text": "Кто знает хорошего сантехника в районе?", "relevant": False},
    {"external_id": "m27", "chat_external_id": "chat_chert_center", "author_external_id": "u1016", "author_name": "Роман Тихонов", "minutes": -300,
     "text": "Чья машина перекрыла выезд у 3 подъезда?? Серебристая Киа, уберите пожалуйста", "relevant": False},
    {"external_id": "m28", "chat_external_id": "chat_chert_south", "author_external_id": "u1023", "author_name": "Елена Р.", "minutes": -120,
     "text": "Собрание жильцов в четверг в 19:00 в холле первого этажа", "relevant": False},
    {"external_id": "m29", "chat_external_id": "chat_chert_south", "author_external_id": "u1021", "author_name": "Валентина", "minutes": -90,
     "text": "Отдам котят в добрые руки 🐱", "relevant": False},
    {"external_id": "m30", "chat_external_id": "chat_biryulevo", "author_external_id": "u1022", "author_name": "Игорь С.", "minutes": -400,
     "text": "Опять шумят на стройке всю ночь, сил нет", "relevant": False},
    {"external_id": "m31", "chat_external_id": "chat_biryulevo", "author_external_id": "u1023", "author_name": "Елена Р.", "minutes": -60,
     "text": "Кто-нибудь знает, работает ли сегодня МФЦ?", "relevant": False},
    {"external_id": "m32", "chat_external_id": "chat_biryulevo", "author_external_id": "u1019", "author_name": "Анна Б.", "minutes": -30,
     "text": "Продаю гараж, недорого, пишите в личку", "relevant": False},
    {"external_id": "m33", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1007", "author_name": "Марина Олейник", "minutes": -360,
     "text": "Доброе утро, соседи! ☀️", "relevant": False},
    {"external_id": "m34", "chat_external_id": "chat_chert_48k2", "author_external_id": "u1015", "author_name": "Гульнара Сафина", "minutes": -200,
     "text": "Лифт во втором подъезде опять не работает", "relevant": False},
    {"external_id": "m35", "chat_external_id": "chat_chert_center", "author_external_id": "u1006", "author_name": "Наталья Белова", "minutes": -500,
     "text": "Субботник в субботу в 11:00, приходите!", "relevant": False},
    {"external_id": "m36", "chat_external_id": "chat_chert_north", "author_external_id": "u1020", "author_name": "Магазин «Окна Про»", "minutes": -100,
     "text": "🔥Акция! Скидки на пластиковые окна до 50%! Звоните прямо сейчас", "is_forwarded": True, "relevant": False},
    {"external_id": "m37", "chat_external_id": "chat_chert_south", "author_external_id": "u1018", "author_name": "Николай Ушаков", "minutes": -70,
     "text": "Кто потерял собаку? Рыжая, бегает у пруда без ошейника", "relevant": False},
    {"external_id": "m38", "chat_external_id": "chat_biryulevo", "author_external_id": "u1021", "author_name": "Валентина", "minutes": 20,
     "text": "Хлопок был? У нас вроде тихо всё", "relevant": False},
    {"external_id": "m39", "chat_external_id": "chat_chert_center", "author_external_id": "u1013", "author_name": "Светлана К.", "minutes": -45,
     "text": "Вечером будет салют в парке? Кто знает во сколько", "relevant": False},
    {"external_id": "m40", "chat_external_id": "chat_chert_north", "author_external_id": "u1014", "author_name": "Андрей Фомин", "minutes": 90,
     "text": "Спасибо всем, кто помог найти кота! Нашёлся 🙏", "relevant": False},
]

SCENARIO_MESSAGE_BY_TEXT: dict[str, dict[str, Any]] = {}
for _m in SCENARIO_MAX_MESSAGES:
    SCENARIO_MESSAGE_BY_TEXT.setdefault(_m["text"].strip(), _m)


def scenario_max_messages(anchor: datetime | None = None) -> list[dict[str, Any]]:
    """Сообщения сценария с вычисленным ``sent_at`` (naive UTC) и полями MaxMessage.

    Возвращает dict-ы с ключами: external_id, chat_external_id, author_external_id, author_name,
    text, sent_at, has_media, media_url, is_forwarded (лишние ключи: minutes, relevant).
    """
    base = anchor or scenario_event_time()
    out = []
    for m in SCENARIO_MAX_MESSAGES:
        d = {"has_media": False, "media_url": None, "is_forwarded": False, **m}
        d["sent_at"] = base + timedelta(minutes=m["minutes"])
        out.append(d)
    return out


def max_message_url(chat_external_id: str, message_external_id: str) -> str:
    return f"max://chat/{chat_external_id}/{message_external_id}"


# ================================================================ web-корпус сценария
# must — хотя бы один стем из списка должен быть в запросе (так «раскрывается» цепочка гипотез);
# minutes — время публикации от события; media — превью для MEDIA FINDER.
SCENARIO_WEB: list[dict[str, Any]] = [
    # --- итерация 1: «хлопок / взрыв»
    {"key": "tg_live", "source_type": "telegram", "minutes": 11,
     "title": "Москва Live: взрыв у жилого дома в Чертанове",
     "url": "https://t.me/moscow_live_mock/48211",
     "snippet": _TG_LIVE_TEXT, "must": ["хлопо", "взрыв"]},
    {"key": "reprint1", "source_type": "news", "minutes": 19,
     "title": "В Чертанове прогремел взрыв у жилого дома",
     "url": "https://gorod24-news.mock/moscow/chertanovo-vzryv",
     "snippet": "В Чертанове прогремел взрыв у жилого дома. Жители сообщают о громком хлопке около 14:30, во дворе дым. "
                "Предварительно — взрыв газа. Подробности уточняются.", "must": ["хлопо", "взрыв"]},
    {"key": "reprint2", "source_type": "news", "minutes": 32,
     "title": "Взрыв в Чертанове: что известно",
     "url": "https://novosti-msk.mock/incidents/48211",
     "snippet": "Жители сообщают о громком хлопке около 14:30, во дворе дым. Предварительно — взрыв газа. "
                "В Чертанове прогремел взрыв у жилого дома.", "must": ["хлопо", "взрыв"]},
    {"key": "tg_video", "source_type": "media", "minutes": 15,
     "title": "Чертаново онлайн: видео столба дыма над домом после хлопка",
     "url": "https://t.me/chertanovo_online_mock/9932",
     "snippet": "Подписчики присылают видео: над двором на Чертановской поднимается чёрный дым, слышны сирены. Снято в 14:36.",
     "media": [{"kind": "video", "url": "https://t.me/chertanovo_online_mock/9932?video=1", "label": "Видео: дым над двором",
                "captured_minutes": 6, "bg": "#2e3440"}],
     "must": ["хлопо", "взрыв", "дым", "видео"]},
    {"key": "old_photo", "source_type": "media", "minutes": 40,
     "title": "Слухи Москвы: фото с места взрыва в Чертанове",
     "url": "https://t.me/sluhi_moskvy_mock/7710",
     "snippet": "Фото с места взрыва в Чертанове — горит машина, рядом пожарный расчёт.",
     "media": [{"kind": "photo", "url": "https://t.me/sluhi_moskvy_mock/7710?photo=1", "label": "Фото «с места взрыва»",
                "captured_at": "2023-08-12T14:05:00", "bg": "#5e4b3c",
                "note": "Обратный поиск: снимок публиковался 12.08.2023 (пожар автомобиля в Бирюлёве)"}],
     "must": ["взрыв", "фото"]},
    # --- итерация 2: «пожарная машина / горит автомобиль»
    {"key": "mchs_fire", "source_type": "official", "minutes": 35,
     "title": "ГУ МЧС по г. Москве: ликвидировано возгорание автомобиля в ЮАО",
     "url": "https://mchs.gov.ru/mock/moscow/news/2026-chertanovskaya-avto",
     "snippet": "В 14:34 поступило сообщение о возгорании легкового автомобиля на открытой парковке у жилого дома по "
                "ул. Чертановская. На место выехали 3 единицы техники и 12 человек личного состава. Пожар на площади "
                "4 кв. м ликвидирован в 14:52. Пострадавших нет.",
     "must": ["пожар", "возго", "автом", "машин", "горит"]},
    {"key": "district_news", "source_type": "news", "minutes": 50,
     "title": "Во дворе на Чертановской сгорел автомобиль: жильцы приняли хлопок за взрыв газа",
     "url": "https://rayon-yuao.mock/news/sgorel-avtomobil-chertanovskaya",
     "snippet": "По словам жительницы дома, сначала раздался громкий хлопок, затем из-под капота припаркованного седана "
                "показался огонь. Пожарные приехали через несколько минут.",
     "must": ["автом", "машин", "горит", "сгоре", "пожар"]},
    {"key": "tg_victims", "source_type": "telegram", "minutes": 45,
     "title": "Срочные новости ЮАО: двое пострадавших при взрыве автомобиля",
     "url": "https://t.me/srochno_yuao_mock/3305",
     "snippet": "Двое пострадавших при взрыве автомобиля в Чертанове госпитализированы, сообщают очевидцы.",
     "must": ["автом", "машин", "постр"]},
    {"key": "tg_photo_fire", "source_type": "media", "minutes": 9,
     "title": "Чертаново онлайн: фото горящего автомобиля во дворе на Чертановской",
     "url": "https://t.me/chertanovo_online_mock/9928",
     "snippet": "Фото от подписчика: во дворе на Чертановской горит легковой автомобиль, 14:33.",
     "media": [{"kind": "photo", "url": "https://t.me/chertanovo_online_mock/9928?photo=1", "label": "Фото: горит автомобиль",
                "captured_minutes": 3, "bg": "#4c566a"}],
     "must": ["автом", "машин", "горит", "горящ", "фото"]},
    # --- итерация 3: «перекрытая парковка / полиция / Мосгаз»
    {"key": "parking_closed", "source_type": "news", "minutes": 60,
     "title": "Парковку у дома на Чертановской временно перекрыли",
     "url": "https://rayon-yuao.mock/news/parkovka-perekryta",
     "snippet": "Полиция огородила часть парковки лентой; около 15:30 эвакуатор увёз сгоревший автомобиль, проезд открыт.",
     "must": ["парко", "перек", "эваку"]},
    {"key": "mosgaz", "source_type": "official", "minutes": 70,
     "title": "mos.ru: Мосгаз проверил дом на Чертановской улице",
     "url": "https://www.mos.ru/mock/news/item/mosgaz-chertanovskaya",
     "snippet": "Специалисты АО «Мосгаз» проверили газовое оборудование в подъездах дома на Чертановской улице. "
                "Утечки газа не выявлено, газоснабжение дома не прерывалось.",
     "must": ["газ", "газом", "мосга", "утечк"]},
    {"key": "mvd_check", "source_type": "official", "minutes": 100,
     "title": "ГУ МВД по г. Москве: проводится проверка по факту возгорания автомобиля",
     "url": "https://mvd.ru/mock/moscow/news/proverka-chertanovskaya",
     "snippet": "Полиция проводит проверку по факту возгорания автомобиля на ул. Чертановская. Рассматривается в том "
                "числе версия поджога, изучаются записи камер видеонаблюдения.",
     "must": ["полиц", "поджо", "мвд"]},
]
SCENARIO_WEB_BY_KEY = {e["key"]: e for e in SCENARIO_WEB}

# ================================================================ LLM: сущности и цепочка
SCENARIO_ENTITIES: dict[str, Any] = {
    "summary": "Громкий хлопок у жилого дома в Чертанове около 14:30, во дворе горит автомобиль",
    "event_type": "хлопок/возгорание",
    "object": "жилой дом, автомобиль во дворе",
    "location": {"text": "Чертаново, ул. Чертановская", "district": "Чертаново Центральное", "address": None},
    "time": {"text": "около 14:30", "iso": None},  # iso заполняется из якоря
    "keywords": ["хлопок", "Чертаново", "горит автомобиль", "взрыв", "пожар"],
    "status": "unconfirmed",
}

SCENARIO_CHAIN: list[dict[str, Any]] = [
    {"rationale": "Стартовые гипотезы по зацепке: «хлопок/взрыв» у жилого дома в Чертанове около 14:30. "
                  "Ищем первые сообщения в СМИ и ТГ, в домовых чатах района, официальные сводки и медиа.",
     "queries": [
         {"agent": "web", "query": "хлопок Чертаново жилой дом 14:30"},
         {"agent": "web", "query": "взрыв Чертаново жилой дом"},
         {"agent": "max_chats", "query": "хлопок взрыв бахнуло Чертановская"},
         {"agent": "official", "query": "МЧС Москвы взрыв Чертаново"},
         {"agent": "media", "query": "Чертаново хлопок дым видео фото"},
     ]},
    {"rationale": "В домовых чатах очевидцы пишут про пожарную машину и горящий автомобиль во дворе у второго "
                  "подъезда; в ТГ-каналах — версия «взрыв газа в доме». Новая гипотеза: не взрыв в доме, а "
                  "возгорание автомобиля на парковке. Проверяем её в МЧС, СМИ и чатах.",
     "queries": [
         {"agent": "web", "query": "горит автомобиль двор Чертановская"},
         {"agent": "max_chats", "query": "пожарная машина горит машина Чертановская"},
         {"agent": "official", "query": "МЧС возгорание автомобиля Чертановская"},
         {"agent": "media", "query": "горящий автомобиль Чертановская фото"},
     ]},
    {"rationale": "МЧС подтвердило возгорание автомобиля (пострадавших нет), но в чатах пишут, что полиция "
                  "перекрыла парковку, соседи говорят о возможном поджоге, а часть жильцов боится утечки газа. "
                  "Новые гипотезы: перекрытая парковка, версия поджога, проверка Мосгаза.",
     "queries": [
         {"agent": "web", "query": "перекрыта парковка Чертановская полиция эвакуатор"},
         {"agent": "max_chats", "query": "полиция перекрыла парковку Мосгаз утечка поджог"},
         {"agent": "max_chats", "query": "пострадавшие скорая Чертановская"},
         {"agent": "official", "query": "полиция поджог автомобиля Чертановская МВД"},
         {"agent": "official", "query": "Мосгаз утечка газа Чертановская"},
     ]},
]

# ================================================================ LLM: INTELLIGENCE сценария
# src — ключи источников: "msg:<external_id>" (MAX) или "web:<key>" (SCENARIO_WEB).
# claim — формулировка для черновика поста (только confirmed/unconfirmed, без false).
SCENARIO_INTEL: dict[str, Any] = {
    "title": "Возгорание автомобиля у жилого дома в Чертанове",
    "headline": "Хлопок в Чертанове: во дворе жилого дома сгорел автомобиль",
    "topic": "хлопке в Чертанове",
    "summary": (
        "Около 14:30 жители дома на улице Чертановской (Чертаново Центральное, ЮАО) услышали громкий хлопок, "
        "после чего во дворе загорелся припаркованный легковой автомобиль. По данным МЧС, пожар на площади 4 кв. м "
        "ликвидирован в 14:52, пострадавших нет. Версия о взрыве газа в жилом доме, которую разгоняли ТГ-каналы, "
        "не подтверждается: Мосгаз утечек не выявил. Причина возгорания устанавливается, полиция проверяет в том "
        "числе версию поджога."
    ),
    "timeline": [
        {"time": "14:29", "text": "Жители дома на Чертановской сообщают о громком хлопке — «окна задрожали»", "src": ["msg:m01", "msg:m04"], "state": "confirmed"},
        {"time": "14:31", "text": "Очевидец видит из окна горящий автомобиль во дворе у второго подъезда", "src": ["msg:m02", "web:tg_photo_fire"], "state": "confirmed"},
        {"time": "14:34", "text": "В МЧС поступает сообщение о возгорании легкового автомобиля на открытой парковке", "src": ["web:mchs_fire"], "state": "confirmed"},
        {"time": "14:40", "text": "На месте работают пожарные расчёты", "src": ["msg:m07", "msg:m05", "web:mchs_fire"], "state": "confirmed"},
        {"time": "14:41", "text": "ТГ-каналы пишут о «взрыве газа» в жилом доме, текст расходится по чатам", "src": ["web:tg_live", "web:reprint1", "web:reprint2", "msg:m09"], "state": "false"},
        {"time": "14:48", "text": "Полиция перекрывает часть парковки лентой", "src": ["msg:m12", "web:parking_closed"], "state": "confirmed"},
        {"time": "14:52", "text": "Пожар ликвидирован, пострадавших нет (данные МЧС)", "src": ["web:mchs_fire", "msg:m13"], "state": "confirmed"},
        {"time": "15:10", "text": "Мосгаз обходит квартиры — утечки газа не выявлено", "src": ["msg:m18", "web:mosgaz"], "state": "confirmed"},
        {"time": "15:30", "text": "Эвакуатор увозит сгоревший автомобиль, парковку открывают", "src": ["msg:m20", "web:parking_closed"], "state": "unconfirmed"},
        {"time": "16:10", "text": "Полиция проводит проверку, рассматривается версия поджога", "src": ["web:mvd_check"], "state": "confirmed"},
    ],
    "facts": [
        {"text": "Что: возгорание легкового автомобиля (белый седан, по словам соседей — Hyundai Solaris) после громкого хлопка", "src": ["web:mchs_fire", "msg:m02", "msg:m14"], "state": "confirmed"},
        {"text": "Где: открытая парковка во дворе жилого дома, ул. Чертановская, 48к2 (Чертаново Центральное, ЮАО)", "src": ["msg:m01", "msg:m02", "web:mchs_fire"], "state": "confirmed"},
        {"text": "Когда: хлопок около 14:29–14:30, вызов в МЧС — 14:34, пожар ликвидирован в 14:52", "src": ["msg:m01", "web:mchs_fire"], "state": "confirmed"},
        {"text": "Силы: 3 единицы техники и 12 человек личного состава МЧС", "src": ["web:mchs_fire"], "state": "confirmed"},
        {"text": "Пострадавших нет (МЧС); скорая, по словам жительницы, не приезжала", "src": ["web:mchs_fire", "msg:m16"], "state": "confirmed"},
        {"text": "Огонь мог частично перекинуться на соседний автомобиль", "src": ["msg:m07"], "state": "unconfirmed"},
    ],
    "confirmed": [
        {"text": "Около 14:30 во дворе жилого дома на Чертановской улице загорелся легковой автомобиль",
         "claim": "Около 14:30 во дворе жилого дома на Чертановской улице загорелся легковой автомобиль",
         "src": ["web:mchs_fire", "msg:m02", "msg:m01"]},
        {"text": "Перед возгоранием жители слышали громкий хлопок",
         "claim": "Перед этим жители слышали громкий хлопок — некоторые приняли его за взрыв газа",
         "src": ["msg:m01", "msg:m04", "web:district_news"]},
        {"text": "Пожар на площади 4 кв. м ликвидирован в 14:52, пострадавших нет",
         "claim": "Пожар на площади 4 кв. м потушили к 14:52, пострадавших нет",
         "src": ["web:mchs_fire", "msg:m13"]},
        {"text": "Мосгаз проверил дом — утечки газа не выявлено",
         "claim": "Специалисты Мосгаза проверили дом — утечки газа не выявлено",
         "src": ["web:mosgaz", "msg:m18"]},
        {"text": "Полиция проводит проверку по факту возгорания",
         "claim": "Полиция проводит проверку по факту возгорания",
         "src": ["web:mvd_check", "msg:m12"]},
    ],
    "unconfirmed": [
        {"text": "Версия поджога: соседи рассказывают, что накануне у машины «кто-то крутился»",
         "claim": "Соседи рассказывают, что вечером у машины кто-то крутился; полиция рассматривает версию поджога",
         "src": ["msg:m14", "msg:m22", "web:mvd_check"], "state": "unconfirmed"},
        {"text": "Огонь перекинулся на соседний автомобиль",
         "claim": "По словам очевидца, огонь успел перекинуться на соседнюю машину",
         "src": ["msg:m07"], "state": "unconfirmed"},
        {"text": "«Двое пострадавших госпитализированы» — опровергается МЧС и очевидцами",
         "src": ["web:tg_victims", "msg:m15"], "state": "false"},
        {"text": "«Взрыв газа в жилом доме» — не подтверждается (МЧС, Мосгаз, жильцы)",
         "src": ["web:tg_live", "msg:m04", "msg:m06"], "state": "false"},
    ],
    "contradictions": [
        {"text": "Взрыв газа в жилом доме (ТГ-каналы, часть чатов) vs возгорание автомобиля на парковке (МЧС, очевидцы во дворе, «газом не пахнет»)",
         "src": ["web:tg_live", "msg:m04", "web:mchs_fire", "msg:m02", "msg:m08"]},
        {"text": "«Двое пострадавших» (ТГ «Срочные новости ЮАО», чат) vs «пострадавших нет» (МЧС) и «скорая не приезжала» (жительница)",
         "src": ["web:tg_victims", "msg:m15", "web:mchs_fire", "msg:m16"]},
        {"text": "Фото «с места взрыва» из ТГ «Слухи Москвы» по обратному поиску публиковалось ещё в 2023 году",
         "src": ["web:old_photo"]},
    ],
    "to_verify": [
        {"text": "Запросить ГУ МВД по Москве: возбуждено ли дело, подтверждается ли версия поджога", "src": ["web:mvd_check"]},
        {"text": "Уточнить марку автомобиля и владельца (соседи называют белый Hyundai Solaris, сосед с 5 этажа)", "src": ["msg:m14"]},
        {"text": "Проверить, пострадал ли соседний автомобиль", "src": ["msg:m07"]},
        {"text": "Связаться с очевидцами из блока «Кому позвонить», получить разрешение на использование фото и видео", "src": ["msg:m02", "msg:m21"]},
        {"text": "Не использовать фото из ТГ «Слухи Москвы» — вероятно, архивный снимок 2023 года", "src": ["web:old_photo"]},
        {"text": "Запросить записи камер «Безопасного города» или управляющей компании", "src": []},
    ],
}

# Связь медиа с событием (MEDIA FINDER) — по url медиа.
SCENARIO_MEDIA_RELATION: dict[str, str] = {
    "https://max.ru/mock/media/m02.jpg": "Снято очевидцем из окна дома через ~1 минуту после хлопка: горящий автомобиль у второго подъезда",
    "https://max.ru/mock/media/m07.mp4": "Видео очевидца: работа пожарных расчётов, ~14:40",
    "https://max.ru/mock/media/m21.mp4": "Видео жительницы с 9 этажа соседнего дома: горит машина во дворе",
    "https://t.me/chertanovo_online_mock/9932?video=1": "Видео подписчика ТГ-канала: дым над двором, слышны сирены (14:36)",
    "https://t.me/chertanovo_online_mock/9928?photo=1": "Фото подписчика: горящий автомобиль во дворе на Чертановской (14:33)",
    "https://t.me/sluhi_moskvy_mock/7710?photo=1": "Выдаётся за фото «с места взрыва», но обратный поиск находит снимок от 12.08.2023 (Бирюлёво) — вероятно, старое фото",
}

# ================================================================ дженерики
EVENT_TYPES: list[tuple[tuple[str, ...], str]] = [
    (("взрыв", "хлопо", "бахну", "грохо"), "хлопок/взрыв"),
    (("пожар", "горит", "горел", "возго", "дым", "задым"), "пожар/возгорание"),
    (("дтп", "авари", "столк", "сбил"), "ДТП"),
    (("прорв", "затоп", "трубы", "кипят", "залил"), "коммунальная авария"),
    (("отклю", "свет", "электр", "воды", "отопл"), "отключение коммунальных услуг"),
    (("драка", "напад", "стрел", "нож", "грабе", "краж"), "происшествие (криминал)"),
    (("обрушь", "обвал", "рухну", "упало", "упал", "дерев"), "обрушение/падение"),
    (("митин", "акция", "проте", "пикет", "собра"), "публичная акция"),
]

GENERIC_OUTLETS: list[dict[str, str]] = [
    {"name": "Городской вестник", "domain": "gorvestnik.mock", "type": "news"},
    {"name": "Москва Live", "domain": "t.me/moscow_live_mock", "type": "telegram"},
    {"name": "Районные новости", "domain": "rayon-news.mock", "type": "news"},
    {"name": "Типичная Москва", "domain": "t.me/typical_msk_mock", "type": "telegram"},
]

# Гипотезы второй итерации для дженерика (MockLLM не «понимает» находки, поэтому — по типу события)
GENERIC_FOLLOWUPS: dict[str, list[str]] = {
    "хлопок/взрыв": ["пожарные расчёты", "эвакуация жильцов", "пострадавшие"],
    "пожар/возгорание": ["пожарные расчёты", "эвакуация жильцов", "задымление"],
    "ДТП": ["пробка", "пострадавшие", "Госавтоинспекция"],
    "коммунальная авария": ["аварийные службы", "отключение воды", "затопленные подвалы"],
    "отключение коммунальных услуг": ["аварийные службы", "сроки восстановления", "жалобы жителей"],
    "происшествие (криминал)": ["полиция", "задержание", "камеры видеонаблюдения"],
    "обрушение/падение": ["пострадавшие", "аварийные службы", "оцепление"],
    "публичная акция": ["количество участников", "полиция", "задержания"],
    "происшествие": ["очевидцы", "экстренные службы", "последствия"],
}

GENERIC_TO_VERIFY: list[str] = [
    "Получить официальный комментарий профильного ведомства (МЧС / ГУ МВД / управа района)",
    "Найти и опросить очевидцев, уточнить точное время и место",
    "Проверить, не являются ли фото и видео архивными (обратный поиск изображений)",
    "Уточнить, есть ли пострадавшие и нужна ли помощь жителям",
]
