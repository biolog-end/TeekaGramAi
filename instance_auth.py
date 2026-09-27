"""Общая авторизация Telegram для консоли и веб-формы дополнительных аккаунтов."""

import asyncio
import secrets
import threading

_lock = threading.RLock()
_state = {'stage': 'connecting', 'message': 'Подключаюсь к Telegram…', 'challenge': None}
_pending = None


def snapshot():
    with _lock: return dict(_state)


def set_state(stage, message):
    with _lock:
        _state.update(stage=stage, message=message, challenge=None)


async def ask(stage, message):
    global _pending
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    with _lock:
        _pending = (loop, future)
        _state.update(stage=stage, message=message, challenge=secrets.token_hex(16))
    try: return await future
    finally:
        with _lock:
            if _pending and _pending[1] is future:
                _pending = None


def submit(challenge, value):
    with _lock:
        if not _pending or challenge != _state.get('challenge'):
            raise ValueError('Форма входа устарела. Дождитесь её обновления.')
        answer = str(value) if _state['stage'] == 'password' else str(value).strip()
        if not answer: raise ValueError('Заполните поле входа.')
        loop, future = _pending
        _state.update(stage='checking', message='Проверяю данные…', challenge=None)
    def deliver():
        if not future.done(): future.set_result(answer)
    loop.call_soon_threadsafe(deliver)


def cancel():
    with _lock:
        pending = _pending
    if pending:
        loop, future = pending
        loop.call_soon_threadsafe(future.cancel)


async def login(client, request_input=None):
    """Одна процедура входа; источник ввода — консоль или ожидающая веб-форма."""
    from telethon import errors
    read = request_input if request_input is not None else ask
    phone_hint = 'Введите номер телефона Telegram-аккаунта в международном формате.'
    while True:
        phone = await read('phone', phone_hint)
        try:
            await client.send_code_request(phone)
        except errors.PhoneNumberInvalidError:
            phone_hint = 'Неверный номер телефона. Введите номер в формате +…'
            continue
        code_hint = 'Введите код входа, присланный Telegram.'
        while True:
            code = await read('code', code_hint)
            try:
                # Как в консоли: SDK хранит hash последнего кода для этого номера.
                await client.sign_in(phone, code)
                return
            except errors.PhoneCodeInvalidError:
                code_hint = 'Неверный код. Введите код из Telegram ещё раз.'
                continue
            except errors.PhoneCodeExpiredError:
                phone_hint = 'Код истёк. Введите номер, чтобы запросить новый код.'
                break
            except errors.SessionPasswordNeededError:
                pass
            hint = 'Введите пароль двухэтапной аутентификации Telegram (2FA).'
            while True:
                password = await read('password', hint)
                try:
                    await client.sign_in(password=password)
                    return
                except errors.PasswordHashInvalidError:
                    hint = 'Неверный пароль 2FA. Попробуйте ещё раз.'
