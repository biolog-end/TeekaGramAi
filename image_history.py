"""Временная связь отправленных картинок с ID сообщений, отдельно для каждого чата."""
import json
import logging
import os
import threading
import time

from instance_paths import private_path
FILE = private_path('image_message_map.json')
_lock = threading.RLock()


def _load():
    try:
        with open(FILE, encoding='utf-8') as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logging.warning('Не удалось прочитать связи отправленных изображений.')
        return {}


def _save(data):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    temp = FILE + '.tmp'
    with open(temp, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
    os.replace(temp, FILE)


def _prune(data, settings_for_chat=None):
    changed = False
    now = time.time()
    for chat_id, entries in list(data.items()):
        settings = settings_for_chat(int(chat_id)) if settings_for_chat else None
        ttl = max(0, float(settings.get('image_history_ttl_hours', 168))) if settings else None
        for msg_id, entry in list(entries.items()):
            hours = ttl if ttl is not None else entry.get('ttl_hours', 168)
            if now >= entry['at'] + hours * 3600:
                del entries[msg_id]; changed = True
        if not entries:
            del data[chat_id]; changed = True
    return changed


def cleanup(settings_for_chat=None):
    """Удаляет просроченные записи; фоновый вызов раз в минуту, без запросов к Telegram."""
    with _lock:
        data = _load()
        if _prune(data, settings_for_chat): _save(data)


def remember(chat_id, sent_messages, images, ttl_hours=168):
    """Порядок возвращённых сообщений соответствует порядку картинок альбома."""
    with _lock:
        data = _load(); _prune(data)
        entries = data.setdefault(str(chat_id), {})
        for message, image in zip(sent_messages, images):
            if not message or ttl_hours <= 0: continue
            photo = getattr(message, 'photo', None)
            entries[str(message.id)] = {'codename': image['codename'],
                'description': image.get('description', ''), 'at': time.time(), 'ttl_hours': ttl_hours,
                'photo_id': str(photo.id) if photo else None}
        if not entries: data.pop(str(chat_id), None)
        _save(data)


def for_chat(chat_id, ttl_hours=168):
    with _lock:
        data = _load()
        if _prune(data, lambda cid: {'image_history_ttl_hours': ttl_hours} if cid == chat_id else {}):
            _save(data)
        return dict(data.get(str(chat_id), {}))
