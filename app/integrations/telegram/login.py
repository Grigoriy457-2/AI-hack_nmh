"""Однократный интерактивный вход в Telegram для Telethon-провайдера.

Запускает САМ пользователь, один раз, в своей консоли, из корня проекта:

    python -m app.integrations.telegram.login          # спросит способ входа
    python -m app.integrations.telegram.login --qr     # вход по QR-коду (код не нужен)
    python -m app.integrations.telegram.login --code   # вход по коду

Нужны TG_API_ID и TG_API_HASH (https://my.telegram.org → API development tools) в .env.
Сессия сохраняется в TG_SESSION_PATH (по умолчанию data/telegram/newsflow.session).

Важно про код: Telegram почти всегда присылает его НЕ по SMS, а сообщением в приложение
Telegram (чат «Telegram») на устройстве, где вы уже вошли. Скрипт печатает, куда ушёл код,
и позволяет запросить его повторно другим способом (SMS/звонок), если Telegram это разрешит.

Файл сессии даёт ПОЛНЫЙ доступ к аккаунту: не коммитьте его (``*.session`` в .gitignore),
не пересылайте. Отозвать: Telegram → Настройки → Устройства → завершить сеанс.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from app.integrations.telegram.settings import get_tg_settings

_SENT_TYPES = {
    "SentCodeTypeApp": "в приложение Telegram — сообщение в чате «Telegram» на телефоне/компьютере, где вы уже вошли",
    "SentCodeTypeSms": "по SMS",
    "SentCodeTypeCall": "голосовым звонком (код продиктуют)",
    "SentCodeTypeFlashCall": "flash-звонком (код — последние цифры номера звонящего)",
    "SentCodeTypeMissedCall": "пропущенным звонком (код — последние цифры номера звонящего)",
    "SentCodeTypeFragmentSms": "через Fragment (номер куплен на fragment.com — код в приложении Fragment)",
    "SentCodeTypeEmailCode": "на почту, привязанную к аккаунту",
    "SentCodeTypeFirebaseSms": "по SMS (через Firebase)",
    "SentCodeTypeSmsWord": "по SMS (слово вместо цифр)",
    "SentCodeTypeSmsPhrase": "по SMS (фраза вместо цифр)",
    "SentCodeTypeSetUpEmailRequired": "Telegram требует сначала привязать почту — войдите через QR (--qr)",
}


def _describe(sent_type) -> str:
    name = type(sent_type).__name__
    return _SENT_TYPES.get(name, name)


def _print_qr(url: str) -> None:
    try:
        import qrcode  # type: ignore
    except ImportError:
        print("(Установите qrcode, чтобы увидеть QR прямо в консоли: pip install qrcode)")
        print(f"Ссылка для QR: {url}")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        # Консоль Windows в cp866/cp1251 не умеет блоки — сохраняем PNG.
        from pathlib import Path
        path = Path("data/telegram/login_qr.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        qr.make_image().save(path)
        print(f"QR сохранён в {path.resolve()} — откройте файл и отсканируйте.")


async def _finish_2fa(client) -> None:
    pwd = getpass.getpass("Включена двухэтапная аутентификация. Облачный пароль: ")
    await client.sign_in(password=pwd)


async def _login_qr(client) -> None:
    from telethon.errors import SessionPasswordNeededError

    print("\nВход по QR. На телефоне: Telegram → Настройки → Устройства → «Подключить устройство»"
          " и наведите камеру на код.\n")
    qr = await client.qr_login()
    while True:
        _print_qr(qr.url)
        print(f"Код действует до {qr.expires:%H:%M:%S} UTC. Жду сканирования...")
        try:
            await qr.wait(timeout=max(10, (qr.expires - qr.expires.now(qr.expires.tzinfo)).total_seconds()))
            return
        except SessionPasswordNeededError:
            await _finish_2fa(client)
            return
        except asyncio.TimeoutError:
            print("QR истёк, создаю новый...\n")
            await qr.recreate()


async def _login_code(client, phone: str | None) -> None:
    from telethon import errors, functions

    phone = phone or input("Номер телефона в международном формате (+79991234567): ").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):  # 8XXXXXXXXXX → +7XXXXXXXXXX
        digits = "7" + digits[1:]
    phone = "+" + digits
    try:
        sent = await client.send_code_request(phone)
    except errors.PhoneNumberInvalidError:
        raise SystemExit("Telegram не принимает номер. Формат: +79991234567.")
    except errors.PhoneNumberBannedError:
        raise SystemExit("Номер заблокирован в Telegram.")
    except errors.FloodWaitError as e:
        raise SystemExit(f"Слишком много попыток. Telegram просит подождать {e.seconds} с. "
                         "Или войдите по QR: --qr")
    except errors.ApiIdInvalidError:
        raise SystemExit("TG_API_ID/TG_API_HASH неверны — проверьте на my.telegram.org.")

    phone_code_hash = sent.phone_code_hash
    print(f"\nКод отправлен {_describe(sent.type)}.")
    if type(sent.type).__name__ == "SentCodeTypeApp":
        print("  Это НЕ SMS: откройте Telegram на телефоне или компьютере — придёт сообщение от «Telegram».")
    if sent.next_type is not None:
        print(f"  Если не пришёл — введите r, чтобы запросить повторно: {_describe(sent.next_type)}.")
    if sent.timeout:
        print(f"  Повторный запрос будет доступен через ~{sent.timeout} с.")
    print("  Код не приходит вообще — прервите (Ctrl+C) и войдите по QR: --qr")

    while True:
        code = input("\nКод (или r — запросить повторно): ").strip()
        if code.lower() in {"r", "к"}:
            try:
                sent = await client(functions.auth.ResendCodeRequest(phone, phone_code_hash))
                phone_code_hash = sent.phone_code_hash
                print(f"Код отправлен повторно {_describe(sent.type)}.")
            except errors.FloodWaitError as e:
                print(f"Рано: подождите {e.seconds} с.")
            except errors.RPCError as e:
                print(f"Повторная отправка недоступна ({e.__class__.__name__}). Используйте --qr.")
            continue
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            return
        except errors.SessionPasswordNeededError:
            await _finish_2fa(client)
            return
        except errors.PhoneCodeInvalidError:
            print("Неверный код, попробуйте ещё раз.")
        except errors.PhoneCodeExpiredError:
            raise SystemExit("Код истёк. Запустите скрипт заново или войдите по QR: --qr")


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Вход в Telegram для NEWSFLOW (Telethon)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--qr", action="store_true", help="вход по QR-коду (код не нужен)")
    g.add_argument("--code", action="store_true", help="вход по коду из Telegram/SMS")
    ap.add_argument("--phone", help="номер телефона для входа по коду")
    args = ap.parse_args(argv)

    s = get_tg_settings()
    if not s.has_telethon_keys:
        print("Не найдены TG_API_ID и TG_API_HASH. Положите их в .env в КОРНЕ проекта и запускайте"
              " команду оттуда (my.telegram.org → API development tools).")
        return 2
    try:
        from telethon import TelegramClient
    except ImportError:
        print("Установите telethon: pip install telethon")
        return 2

    session = s.session_file
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session.with_suffix("")), int(s.TG_API_ID), s.TG_API_HASH)  # type: ignore[arg-type]

    print("Подключаюсь к серверам Telegram...")
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
    except (asyncio.TimeoutError, OSError) as e:
        print(f"Не удалось подключиться к Telegram ({e.__class__.__name__}). Возможно, соединение"
              " с серверами Telegram блокируется сетью — попробуйте другую сеть/VPN.")
        return 1
    print("Подключено.")

    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Уже выполнен вход как {me.first_name or ''} (@{me.username or '-'}). Сессия: {session}")
            return 0

        mode = "qr" if args.qr else "code" if args.code or args.phone else None
        if mode is None:
            ans = input("Способ входа: 1 — QR-код (рекомендуется), 2 — код из Telegram [1]: ").strip()
            mode = "code" if ans == "2" else "qr"
        if mode == "qr":
            await _login_qr(client)
        else:
            await _login_code(client, args.phone)

        me = await client.get_me()
        print(f"\nГотово: вошли как {me.first_name or ''} (@{me.username or '-'}). Сессия: {session}")
        print("В .env должно быть: TG_PROVIDER=telethon и TG_SOURCES=@channel1,@channel2."
              " Перезапустите сервер.")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main(sys.argv[1:])))
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(130)
