"""Ограниченный журнал авто-режима и текущая операция для SSE и перезагрузки вкладки."""

from collections import OrderedDict, deque
import logging
import threading
import time

import events

MAX_ENTRIES = 80
MAX_CHATS = 128
_lock = threading.RLock()
_chats = OrderedDict()
# Миллисекунды + запас для событий: номера сохраняют порядок и после рестарта сервера.
_sequence = int(time.time() * 1000) * 1000
_instance_id = _sequence


def _state(chat_id):
    if chat_id not in _chats:
        _chats[chat_id] = {'entries': deque(maxlen=MAX_ENTRIES), 'current': None,
                         'response': None, 'run_id': _sequence + 1}
    _chats.move_to_end(chat_id)
    while len(_chats) > MAX_CHATS:
        _chats.popitem(last=False)
    return _chats[chat_id]


def reset(chat_id):
    """Начать новый запуск; номера событий не переиспользуются."""
    with _lock:
        _chats.pop(chat_id, None)
        _state(chat_id)


def record(chat_id, phase, message, duration_s=None, text=None, level='info', **details):
    """Зафиксировать операцию; дедлайн — время настоящей, уже выбранной задержки."""
    global _sequence
    with _lock:
        now = time.time()
        state = _state(chat_id)
        _sequence += 1
        entry = {'id': _sequence, 'at': now, 'phase': phase, 'message': message,
                 'level': level, **details}
        if phase in {'typing', 'choosing_sticker', 'choosing_image', 'send_queue', 'slow_mode_wait', 'telegram_send',
                     'correction_wait', 'correction_typing', 'correction_send', 'pause', 'part_done'}:
            for key in ('part', 'total', 'kind'):
                if key in (state['current'] or {}):
                    entry.setdefault(key, state['current'][key])
        if duration_s is not None:
            entry.update(duration_s=max(0.0, float(duration_s)))
            entry['ends_at'] = now + entry['duration_s']
        if text is not None and phase != 'response':
            entry['text'] = str(text)
        payload = {'entry': entry, 'current': entry, 'server_now': now,
                   'run_id': state['run_id'], 'instance_id': _instance_id}
        if phase == 'response':
            state['response'] = {'text': str(text or ''), 'model': details.get('model'), 'at': now, 'id': entry['id']}
            payload['response'] = state['response']
        state['entries'].append(entry)
        state['current'] = entry
        # Порядок событий совпадает с номерами даже при публикации из двух потоков.
        events.publish('auto_activity', payload, chat_id=chat_id)


def snapshot(chat_id):
    """Снимок без Telegram/API; журнал существует только до перезапуска приложения."""
    with _lock:
        state = _chats.get(chat_id)
        return {'entries': list(state['entries']) if state else [],
                'current': state['current'] if state else None,
                'response': state['response'] if state else None,
                'run_id': state['run_id'] if state else None,
                'instance_id': _instance_id, 'server_now': time.time()}


def emit(progress, phase, message, **details):
    """Сбой интерфейсного уведомления не должен прерывать отправку сообщения."""
    if progress:
        try:
            progress(phase, message, **details)
        except Exception:
            logging.exception('Не удалось обновить журнал авто-режима.')
