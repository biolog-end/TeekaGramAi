"""Хранилище картинок: файлы кладёт пользователь в data/images/, мы их видим по имени.

Устройство простое: имя файла (без расширения, в нижнем регистре) — codename, который
модель пишет как image(codename). Описания живут в data/images.json и правятся в UI,
чтобы не терялись при переименовании: ключ там — codename. Файлы новее json — они появятся
автоматически при следующем чтении, лишние записи в json остаются на случай, если файл
временно убрали.
"""

import json
import logging
import os
import re
import threading
from shared_storage import locked, write_json

IMAGES_DIR = 'data/images'
IMAGES_JSON = 'data/images.json'
ALLOWED_EXT = ('.jpg', '.jpeg', '.png', '.webp', '.gif')

_lock = threading.RLock()
_CODENAME_RE = re.compile(r'[^a-z0-9_-]+')


def _normalize(name):
    """Кодовое имя: латиница/цифры/_/- в нижнем регистре. Всё остальное → _."""
    return _CODENAME_RE.sub('_', (name or '').lower()).strip('_')


def _load_meta():
    """Читает json с описаниями; None-safe для отсутствующего файла."""
    if not os.path.exists(IMAGES_JSON):
        return {}
    try:
        with open(IMAGES_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"Не удалось прочитать {IMAGES_JSON}: {e}")
        return {}


def _save_meta(meta):
    try:
        os.makedirs(os.path.dirname(IMAGES_JSON), exist_ok=True)
        write_json(IMAGES_JSON, meta)
        return True
    except IOError as e:
        logging.error(f"Не удалось сохранить {IMAGES_JSON}: {e}")
        return False


def _scan_folder():
    """{codename: filename} из data/images. Пропускает не-картинки и дубли имён."""
    result = {}
    if not os.path.isdir(IMAGES_DIR):
        return result
    for filename in sorted(os.listdir(IMAGES_DIR)):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        base = os.path.splitext(filename)[0]
        codename = _normalize(base)
        if not codename or codename in result:
            continue
        result[codename] = filename
    return result


def list_images():
    """Список картинок для UI и промпта.

    Каждая запись — {codename, filename, description, exists}. Записи без файла на диске
    попадают в список с exists=False, чтобы пользователь мог убрать их из json.
    """
    with _lock:
        meta = _load_meta()
        files = _scan_folder()

        result = []
        seen = set()
        for codename, filename in files.items():
            result.append({
                'codename': codename,
                'filename': filename,
                'description': meta.get(codename, {}).get('description', ''),
                'exists': True,
            })
            seen.add(codename)

        for codename, entry in meta.items():
            if codename in seen:
                continue
            result.append({
                'codename': codename,
                'filename': entry.get('filename', ''),
                'description': entry.get('description', ''),
                'exists': False,
            })
        result.sort(key=lambda x: x['codename'])
        return result


def find_file(codename):
    """Полный путь к файлу по codename. None — если такого нет."""
    codename = _normalize(codename)
    if not codename:
        return None
    filename = _scan_folder().get(codename)
    if not filename:
        return None
    return os.path.join(IMAGES_DIR, filename)


@locked(lambda: IMAGES_JSON)
def set_description(codename, description):
    """Правка описания из UI. Файл на диске трогать не надо."""
    codename = _normalize(codename)
    if not codename:
        return False
    with _lock:
        meta = _load_meta()
        entry = meta.setdefault(codename, {})
        entry['description'] = (description or '').strip()
        entry.setdefault('filename', _scan_folder().get(codename, ''))
        return _save_meta(meta)


def prompt_text(enabled_codenames):
    """Секция «доступные картинки» для системного промпта."""
    enabled = {_normalize(c) for c in (enabled_codenames or []) if c}
    if not enabled:
        return ""
    images = [img for img in list_images() if img['codename'] in enabled and img['exists']]
    if not images:
        return ""

    lines = ["Чтобы отправить картинку, используй команду image(кодовое_имя_из_списка_ниже).",
             "Текст рядом с image(...) — подпись к картинке. Несколько image(...) подряд — один альбом, до 9 картинок.",
             "Не разделяй подпись и картинки через {split}. Подпись альбома — не более 1024 символов.",
             "", "Доступные картинки:"]
    for img in images:
        line = f"- {img['codename']}"
        if img['description']:
            line += f": {img['description']}"
        lines.append(line)
    return "\n".join(lines)
