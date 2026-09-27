"""Шина событий для живого обновления интерфейса через Server-Sent Events.

Зачем: раньше почти каждое действие в вебе делало redirect и перезагружало
страницу целиком, заново вытягивая историю из Telegram. Теперь страница грузится
один раз, а изменения прилетают сюда.

Как устроено: события публикуются из любого потока (поток Telethon, воркер
авто-режима, обработчик Flask), а каждая открытая вкладка держит свою очередь и
читает из неё. Очереди ограничены по длине — если вкладку закрыли и никто не
читает, события просто отбрасываются, память не растёт.
"""

import json
import queue
import logging
import threading

# Размер очереди на одну вкладку. Больше и не нужно: если столько накопилось,
# значит вкладка мертва.
MAX_QUEUE_SIZE = 100

# Раз в столько секунд шлём комментарий-пустышку, чтобы соединение не закрыли
# прокси или сам браузер.
HEARTBEAT_INTERVAL_S = 20

_lock = threading.Lock()
_subscribers = []  # список словарей {queue, chat_id}


class _Subscriber:
    __slots__ = ('queue', 'chat_id', 'chat_ids')

    def __init__(self, chat_id, extra_chat_ids=None):
        self.queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self.chat_id = chat_id
        self.chat_ids = {chat_id} if chat_id is not None else set()
        self.chat_ids.update(value for value in (extra_chat_ids or []) if value is not None)


def publish(event_type, data=None, chat_id=None):
    """Отправляет событие всем подходящим подписчикам.

    Args:
        event_type (str): тип события, например 'new_message' или 'auto_mode'.
        data (dict | None): полезная нагрузка.
        chat_id (int | None): если указан, событие получат только вкладки этого
            чата. None — событие общее (например, состояние пула ключей).
    """
    payload = json.dumps(
        {'type': event_type, 'data': data or {}},
        ensure_ascii=False,
    )

    with _lock:
        targets = [
            s for s in _subscribers
            if chat_id is None or s.chat_id is None or chat_id in s.chat_ids
        ]

    dropped = 0
    for sub in targets:
        try:
            sub.queue.put_nowait(payload)
        except queue.Full:
            dropped += 1

    if dropped:
        logging.debug(f"Событие '{event_type}': очередь переполнена у {dropped} подписчиков.")


def subscriber_count():
    with _lock:
        return len(_subscribers)


def stream(chat_id=None, extra_chat_ids=None):
    """Генератор строк в формате SSE. Отдаётся во Flask как тело ответа."""
    sub = _Subscriber(chat_id, extra_chat_ids=extra_chat_ids)
    with _lock:
        _subscribers.append(sub)
    logging.info(f"SSE: новая вкладка (чат {chat_id}). Всего подписчиков: {subscriber_count()}.")

    try:
        # Сразу подтверждаем, что канал живой, — фронтенд по этому снимает
        # плашку «нет соединения».
        yield _format('connected', {'chat_id': chat_id})

        while True:
            try:
                payload = sub.queue.get(timeout=HEARTBEAT_INTERVAL_S)
            except queue.Empty:
                # Комментарий SSE: браузер его игнорирует, но соединение живёт.
                yield ": heartbeat\n\n"
                continue

            yield f"data: {payload}\n\n"
    except GeneratorExit:
        raise
    finally:
        with _lock:
            if sub in _subscribers:
                _subscribers.remove(sub)
        logging.info(f"SSE: вкладка отключилась (чат {chat_id}). Осталось: {subscriber_count()}.")


def _format(event_type, data):
    payload = json.dumps({'type': event_type, 'data': data or {}}, ensure_ascii=False)
    return f"data: {payload}\n\n"
