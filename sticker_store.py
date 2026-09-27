"""Единая точка работы с data/stickers.json.

Схема v2: у каждого стикера есть quarantined, reviewed, use_count и last_used_ts.
Читатель дописывает эти поля дефолтами на лету, так что старый файл не ломается.
Наборы (записи с пустым списком stickers) остались как были — это описания паков.

Карантин — это «модель получает стикер только текстом sticker(id=…), без картинки».
Туда стикер попадает только после отказа модели (см. sticker_guard), а не при первой
встрече. Незнакомые стикеры просто запоминаются как `raw_<id>` — без имени, но с
картинкой в vision-режиме.

Мутирующие операции берут блокировку модуля и сохраняют файл сразу, чтобы состояние
на диске и в памяти жило не рассинхронизированным.
"""

import json
import logging
import os
import threading
import time
from shared_storage import locked, write_json

STICKER_JSON_FILE = 'data/stickers.json'
QUARANTINE_PREFIX = 'raw_'   # кодовое имя для стикера, пришедшего из чата, но ещё не названного

_lock = threading.RLock()
_last_set_use = {}   # {set_name: ts} — только для «человечной» задержки, на диск не идёт


def _default_sticker(record):
    """Дописывает недостающие поля стикера, чтобы старый файл был совместим."""
    return {
        'id': record.get('id'),
        'access_hash': record.get('access_hash'),
        'set_id': record.get('set_id'),
        'set_access_hash': record.get('set_access_hash'),
        'set_title': record.get('set_title', ''),
        'set_short_name': record.get('set_short_name', ''),
        'emoji': record.get('emoji', ''),
        'quarantined': bool(record.get('quarantined', False)),
        # Пользователь уже принял решение по карантинному стикеру — в списке ревью не показывать.
        'reviewed': bool(record.get('reviewed', False)),
        'use_count': int(record.get('use_count', 0)),
        'last_used_ts': float(record.get('last_used_ts', 0)),
    }


def load():
    """Читает файл со стикерами, приводя записи к схеме v2."""
    with _lock:
        try:
            if not os.path.exists(STICKER_JSON_FILE):
                return {}
            with open(STICKER_JSON_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except (json.JSONDecodeError, IOError, ValueError) as e:
            logging.warning(f"Не удалось прочитать {STICKER_JSON_FILE}: {e}")
            return {}

        result = {}
        for codename, data in (raw or {}).items():
            if not isinstance(data, dict):
                continue
            result[codename] = {
                'enabled': bool(data.get('enabled', True)),
                'description': data.get('description', ''),
                'stickers': [_default_sticker(s) for s in (data.get('stickers') or [])
                             if isinstance(s, dict) and s.get('id') is not None],
            }
        return result


@locked(lambda: STICKER_JSON_FILE)
def save(data):
    """Пишет обновлённые данные на диск."""
    with _lock:
        try:
            os.makedirs(os.path.dirname(STICKER_JSON_FILE), exist_ok=True)
            write_json(STICKER_JSON_FILE, data)
            return True
        except IOError as e:
            logging.error(f"Не удалось сохранить {STICKER_JSON_FILE}: {e}")
            return False


# ---------------------------------------------------------------------------
# Поиск и итерация
# ---------------------------------------------------------------------------

def all_stickers(data=None):
    """Плоский список (codename, sticker) — по всему файлу."""
    data = load() if data is None else data
    for codename, record in data.items():
        for sticker in record.get('stickers') or []:
            yield codename, sticker


def telegram_sticker_meta(document):
    """Читает доступные без запроса метаданные Telegram-стикера.

    ``set_title`` обычно отсутствует у самого документа: его позже дополняет
    AI-поиск через ``messages.GetStickerSet``. ``set_reference`` нужен только
    для этого запроса и в JSON не сохраняется.
    """
    reference = (getattr(document, 'set', None)
                 or getattr(document, 'stickerset', None))
    emoji = ''
    for attribute in getattr(document, 'attributes', None) or []:
        candidate = getattr(attribute, 'stickerset', None)
        if candidate is not None:
            reference = candidate
        alt = getattr(attribute, 'alt', None)
        if isinstance(alt, str) and alt:
            emoji = alt
    return {
        'set_id': getattr(reference, 'id', None) if reference else None,
        'set_access_hash': getattr(reference, 'access_hash', None) if reference else None,
        'set_title': getattr(reference, 'title', '') if reference else '',
        'set_short_name': getattr(reference, 'short_name', '') if reference else '',
        'emoji': emoji,
        'set_reference': reference,
    }


def telegram_set_meta(document):
    """Возвращает ``(set_id, access_hash)`` из Telegram-документа стикера.

    В Telethon ссылка на набор обычно лежит не на самом ``Document``, а в
    ``DocumentAttributeSticker.stickerset``. Прямые поля оставлены как запасной
    путь для тестовых объектов и совместимости с другими версиями библиотеки.
    """
    meta = telegram_sticker_meta(document)
    return meta['set_id'], meta['set_access_hash']


def find_by_id(sticker_id, data=None):
    """Возвращает (codename, sticker) или (None, None), если такого id нет."""
    try:
        sticker_id = int(sticker_id)
    except (TypeError, ValueError):
        return None, None
    data = load() if data is None else data
    for codename, sticker in all_stickers(data):
        if sticker.get('id') == sticker_id:
            return codename, sticker
    return None, None


def id_to_codename(data=None):
    """{sticker_id: codename} для названных стикеров. raw_* пропускаем — это не имя."""
    mapping = {}
    for codename, sticker in all_stickers(data):
        if codename.startswith(QUARANTINE_PREFIX):
            continue
        sid = sticker.get('id')
        if sid is not None:
            mapping[int(sid)] = codename
    return mapping


def known_ids(data=None):
    """Все id из базы, включая безымянные raw_*."""
    return {int(s['id']) for _, s in all_stickers(data) if s.get('id') is not None}


def quarantined_ids(data=None):
    """id стикеров, которые модель должна получать только текстом."""
    return {int(s['id']) for _, s in all_stickers(data) if s.get('quarantined') and s.get('id') is not None}


# ---------------------------------------------------------------------------
# Автоматическое пополнение из чата
# ---------------------------------------------------------------------------

@locked(lambda: STICKER_JSON_FILE)
def ingest_unknown(sticker_id, access_hash, set_id=None, set_access_hash=None,
                   set_title='', set_short_name='', emoji=''):
    """Запоминает незнакомый стикер как `raw_<id>` — чтобы знать access_hash и пак.

    Идемпотентно: если стикер уже где-то есть — просто дописывает set_id, если пропал.
    Возвращает True, если это была новая запись.
    """
    try:
        sticker_id = int(sticker_id)
    except (TypeError, ValueError):
        return False

    with _lock:
        data = load()
        existing_codename, existing = find_by_id(sticker_id, data)
        if existing:
            changed = False
            for field, value in (
                    ('set_id', set_id), ('set_access_hash', set_access_hash),
                    ('set_title', set_title), ('set_short_name', set_short_name),
                    ('emoji', emoji)):
                if value and not existing.get(field):
                    existing[field] = value
                    changed = True
            if changed:
                save(data)
            return False

        codename = f"{QUARANTINE_PREFIX}{sticker_id}"
        record = data.setdefault(codename, {'enabled': True, 'description': '', 'stickers': []})
        record['stickers'].append({
            'id': sticker_id, 'access_hash': access_hash,
            'set_id': set_id, 'set_access_hash': set_access_hash,
            'set_title': set_title or '', 'set_short_name': set_short_name or '',
            'emoji': emoji or '',
            'quarantined': False, 'reviewed': False, 'use_count': 0, 'last_used_ts': 0,
        })
        save(data)
        return True


# ---------------------------------------------------------------------------
# Карантин и ревью
# ---------------------------------------------------------------------------

@locked(lambda: STICKER_JSON_FILE)
def quarantine(sticker_ids):
    """Отправляет стикеры в карантин после отказа модели.

    Возвращает число стикеров, у которых флаг действительно изменился.
    """
    wanted = set()
    for sid in sticker_ids or []:
        try:
            wanted.add(int(sid))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return 0

    with _lock:
        data = load()
        changed = 0
        for _, sticker in all_stickers(data):
            if sticker.get('id') in wanted and not sticker.get('quarantined'):
                sticker['quarantined'] = True
                sticker['reviewed'] = False
                changed += 1
        if changed:
            save(data)
        return changed


def list_quarantined():
    """Карантинные стикеры, по которым пользователь ещё не принял решение."""
    result = []
    data = load()
    for codename, sticker in all_stickers(data):
        if sticker.get('quarantined') and not sticker.get('reviewed'):
            result.append({
                'sticker_id': sticker['id'],
                'codename': codename,
                'set_id': sticker.get('set_id'),
                'description': data.get(codename, {}).get('description', ''),
            })
    return result


@locked(lambda: STICKER_JSON_FILE)
def release(sticker_id):
    """Снимает карантин: модель снова получит стикер картинкой (имя не меняется)."""
    with _lock:
        data = load()
        codename, sticker = find_by_id(sticker_id, data)
        if not sticker:
            return False, 'Стикер не найден.'
        sticker['quarantined'] = False
        sticker['reviewed'] = False
        save(data)
        return True, codename


@locked(lambda: STICKER_JSON_FILE)
def ignore(sticker_id):
    """Оставляет стикер в карантине (текстом), но убирает из списка ревью."""
    with _lock:
        data = load()
        codename, sticker = find_by_id(sticker_id, data)
        if not sticker:
            return False, 'Стикер не найден.'
        sticker['reviewed'] = True
        save(data)
        return True, codename


@locked(lambda: STICKER_JSON_FILE)
def describe(sticker_id, new_codename, description=''):
    """Переносит стикер под новое кодовое имя и пишет описание.

    Карантин не трогает: карантинный стикер так и остаётся «текстовым», но теперь
    с именем — модель понимает, что это, и может его отправить.
    Если кодовое имя занято другим набором — стикер добавляется туда.
    """
    new_codename = (new_codename or '').strip().lower()
    if not new_codename:
        return False, 'Пустое кодовое имя.'
    if not all(ch.isalnum() or ch in '_-' for ch in new_codename):
        return False, 'В имени только латиница, цифры, _ и -.'

    with _lock:
        data = load()
        old_codename, sticker = find_by_id(sticker_id, data)
        if not sticker:
            return False, 'Стикер не найден.'

        sticker['reviewed'] = True
        if old_codename != new_codename:
            data.setdefault(new_codename, {'enabled': True, 'description': '', 'stickers': []})
            data[new_codename]['stickers'].append(sticker)
            if description:
                data[new_codename]['description'] = description
            data[old_codename]['stickers'] = [s for s in data[old_codename]['stickers']
                                              if s.get('id') != sticker['id']]
            # Опустевшая raw_ запись больше ничего не значит.
            if not data[old_codename]['stickers'] and old_codename.startswith(QUARANTINE_PREFIX):
                del data[old_codename]
        elif description:
            data[new_codename]['description'] = description

        save(data)
        return True, new_codename


@locked(lambda: STICKER_JSON_FILE)
def describe_set(set_name, description):
    """Пишет описание пака (набора)."""
    set_name = (set_name or '').strip().lower()
    if not set_name:
        return False
    with _lock:
        data = load()
        record = data.setdefault(set_name, {'enabled': True, 'description': '', 'stickers': []})
        record['description'] = description
        save(data)
        return True


# ---------------------------------------------------------------------------
# Учёт использования — для «человечной» задержки перед отправкой
# ---------------------------------------------------------------------------

@locked(lambda: STICKER_JSON_FILE)
def record_use(sticker_id):
    """Отмечает, что стикер только что был отправлен нами."""
    with _lock:
        data = load()
        codename, sticker = find_by_id(sticker_id, data)
        if not sticker:
            return
        now = time.time()
        sticker['use_count'] = int(sticker.get('use_count', 0)) + 1
        sticker['last_used_ts'] = now
        save(data)
        # Ключ «набор» для эвристики задержки — это префикс кодового имени до последнего _,
        # так работал старый structure_sticker_data. Без диска, только в памяти.
        set_key = codename.rsplit('_', 1)[0] if '_' in codename else codename
        _last_set_use[set_key] = now


def last_set_use_ago(codename):
    """Секунд назад мы отправляли стикер из этого набора; None — ни разу."""
    set_key = codename.rsplit('_', 1)[0] if '_' in codename else codename
    ts = _last_set_use.get(set_key)
    if ts is None:
        return None
    return time.time() - ts
