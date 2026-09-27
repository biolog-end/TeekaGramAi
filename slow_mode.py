"""Очередь отправок и медленный режим Telegram; работает только в цикле Telethon."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import time

from telethon import TelegramClient, errors, functions
from telethon.tl.types import InputPeerChannel

from auto_activity import emit

bridge_wait_state = ContextVar('telegram_send_wait', default=None)
_sending = ContextVar('telegram_slow_mode_send', default=False)
_client = None
_chats = {}
SAFETY_SECONDS = 0.5
REFRESH_SECONDS = 30


class SlowModeClient(TelegramClient):
    """В нашей отправке SDK возвращает ожидание сразу, в остальных запросах — как раньше."""
    @property
    def flood_sleep_threshold(self):
        return 0 if _sending.get() else TelegramClient.flood_sleep_threshold.fget(self)

    @flood_sleep_threshold.setter
    def flood_sleep_threshold(self, value):
        TelegramClient.flood_sleep_threshold.fset(self, value)


class SendCancelled(Exception):
    """Остановка авто-режима отменяет ещё не начавшуюся отправку."""


def _state(client, chat_id):
    global _client
    if client is not _client:
        _client = client
        _chats.clear()
    return _chats.setdefault(chat_id, {'lock': asyncio.Lock(), 'seconds': 0,
        'next_at': 0, 'last_sent_at': 0, 'error_until': 0, 'checked_at': None})


@contextmanager
def _waiting():
    """Легальное ожидание очереди/кулдауна не расходует таймаут Telegram-операции."""
    state = bridge_wait_state.get()
    if state is not None:
        state['waiting_since'] = time.monotonic()
    try:
        yield
    finally:
        if state is not None:
            state['paused_s'] += time.monotonic() - state['waiting_since']
            state['waiting_since'] = None


def paused_seconds(state):
    since = state['waiting_since']
    return state['paused_s'] + (time.monotonic() - since if since is not None else 0)


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise SendCancelled('Отправка отменена: авто-режим остановлен.')


async def _refresh(client, chat_id, state):
    peer = await client.get_input_entity(chat_id)
    if not isinstance(peer, InputPeerChannel):
        state.update(seconds=0, next_at=0)
        state['checked_at'] = time.monotonic()
        return
    result = await client(functions.channels.GetFullChannelRequest(peer))
    full = result.full_chat
    channel = next((chat for chat in result.chats if chat.id == peer.channel_id), None)
    exempt = channel and (getattr(channel, 'creator', False) or getattr(channel, 'admin_rights', None))
    seconds = 0 if exempt else int(getattr(full, 'slowmode_seconds', None) or 0)
    server_next = getattr(full, 'slowmode_next_send_date', None)
    server_at = server_next.timestamp() + SAFETY_SECONDS if server_next and seconds else 0
    local_at = state['last_sent_at'] + seconds + SAFETY_SECONDS if seconds and state['last_sent_at'] else 0
    state.update(seconds=seconds, next_at=max(server_at, local_at))
    if exempt:
        state['error_until'] = 0
    state['checked_at'] = time.monotonic()


async def _acquire(state, stop_event, progress):
    lock = state['lock']
    waited = lock.locked()
    if waited:
        emit(progress, 'send_queue', 'Ожидаю завершения предыдущей отправки в этот чат.')
    with _waiting():
        while True:
            _check_stop(stop_event)
            try:
                await asyncio.wait_for(lock.acquire(), timeout=1)
                return waited
            except asyncio.TimeoutError:
                waited = True


async def _wait_for_cooldown(client, chat_id, state, stop_event, progress, message):
    displayed_deadline = None
    waited = False
    while True:
        _check_stop(stop_event)
        deadline = max(state['next_at'], state['error_until'])
        remaining = deadline - time.time()
        if remaining <= 0:
            return waited
        waited = True
        if displayed_deadline is None or abs(deadline - displayed_deadline) > 1:
            emit(progress, 'slow_mode_wait', message,
                 duration_s=remaining, slowmode_seconds=state['seconds'])
            displayed_deadline = deadline
        refresh_at = time.monotonic() + min(remaining, REFRESH_SECONDS)
        with _waiting():
            while deadline > time.time() and time.monotonic() < refresh_at:
                _check_stop(stop_event)
                await asyncio.sleep(min(1, max(0, deadline - time.time())))
        await _refresh(client, chat_id, state)


async def wait_until_ready(client, chat_id, stop_event=None, progress=None, force_refresh=False):
    """Авто-режим ждёт доступного слота до чтения чата, не расходуя этот слот."""
    _check_stop(stop_event)
    state = _state(client, chat_id)
    queued = await _acquire(state, stop_event, progress)
    try:
        if force_refresh or state['checked_at'] is None or time.monotonic() - state['checked_at'] >= REFRESH_SECONDS:
            await _refresh(client, chat_id, state)
        waited = await _wait_for_cooldown(client, chat_id, state, stop_event, progress,
            'Кулдаун чата: жду окончания перед проверкой сообщений и запросом к модели.')
        return queued or waited
    finally:
        state['lock'].release()


def note_outgoing(client, chat_id, message):
    """Свой исходящий update учитывает также отправку из другого клиента Telegram."""
    state = _state(client, chat_id)
    date = getattr(message, 'date', None)
    at = date.timestamp() if date else time.time()
    state['last_sent_at'] = max(state['last_sent_at'], at)
    if state['seconds']:
        state['next_at'] = max(state['next_at'], at + state['seconds'] + SAFETY_SECONDS)


async def send(client, chat_id, operation, settings=None, progress=None,
               message='Отправляю сообщение в Telegram.'):
    """Одна отправка/альбом: проверка сервера, ожидание, отправка, новый кулдаун."""
    stop_event = (settings or {}).get('_send_stop_event')
    _check_stop(stop_event)
    state = _state(client, chat_id)
    lock = state['lock']
    await _acquire(state, stop_event, progress)
    try:
        await _refresh(client, chat_id, state)
        while True:
            await _wait_for_cooldown(client, chat_id, state, stop_event, progress,
                'Медленный режим чата: жду разрешения отправить сообщение.')
            _check_stop(stop_event)
            emit(progress, 'telegram_send', message)
            token = _sending.set(True)
            try:
                result = await operation()
            except errors.SlowModeWaitError as err:
                state['error_until'] = time.time() + max(1, err.seconds) + SAFETY_SECONDS
                continue
            finally:
                _sending.reset(token)
            state['last_sent_at'] = time.time()
            state['next_at'] = state['last_sent_at'] + state['seconds'] + SAFETY_SECONDS if state['seconds'] else 0
            state['error_until'] = 0
            return result
    finally:
        lock.release()
