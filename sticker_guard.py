"""Карантин стикеров по отказу модели.

В vision-режиме стикеры уходят модели картинками. Если провайдер вернул отказ на
уровне API (safety-блок Gemini, `refusal` / `content_filter` у OpenAI), значит фильтр
конкретно на этой картинке — стикеры-картинки из промпта отправляются в карантин,
дальше модель получает их только строкой sticker(id=…), запрос повторяется без картинок.

Текстовые «извини, я не могу…» ловить нельзя: это часто нормальная реплика персонажа,
слишком легко получить ложные срабатывания.
"""

import logging

import events
from auto_activity import emit
import gemini_utils
import sticker_store
import telegram_utils


def looks_like_refusal(text, error=None):
    """Ответ модели — отказ на уровне провайдера, а не сеть/квота."""
    return gemini_utils.is_safety_error(error) if error else False


def image_sticker_ids(history):
    """id стикеров, которые в этой истории ушли модели картинками."""
    ids = set()
    for msg in history or []:
        for part in msg.get('parts') or []:
            if 'image_base64' in part and part.get('sticker_id') is not None:
                ids.add(int(part['sticker_id']))
    return ids


def strip_sticker_images(history, sticker_ids):
    """Копия истории без картинок указанных стикеров; строка sticker(id=…) остаётся."""
    stripped = []
    for msg in history or []:
        parts = [p for p in (msg.get('parts') or [])
                 if not ('image_base64' in p and p.get('sticker_id') in sticker_ids)]
        stripped.append({**msg, 'parts': parts})
    return stripped


def generate_with_sticker_guard(model_name, system_prompt, chat_history, config=None, chat_id=None, progress=None,
                                **rotation_options):
    """generate_chat_reply_original + карантин стикеров при safety-отказе и один повтор без картинок."""
    if rotation_options and progress:
        rotation_options['progress'] = progress
    text, error = gemini_utils.generate_chat_reply_original(model_name, system_prompt, chat_history, config, **rotation_options)
    ids = image_sticker_ids(chat_history)
    if not ids or not looks_like_refusal(text, error):
        return text, error

    reason = str(error or '').strip().replace('\n', ' ')[:200]
    changed = sticker_store.quarantine(ids)
    telegram_utils.load_sticker_db()
    logging.warning(f"Safety-отказ провайдера при стикерах-картинках: {reason!r}. "
                    f"В карантин: {sorted(ids)} (новых {changed}). Повтор без картинок.")
    events.publish('stickers', {'quarantined': sorted(ids), 'reason': reason}, chat_id=chat_id)
    emit(progress, 'request', 'Модель отказалась обработать изображения стикеров. Повторяю запрос без них.', model=model_name)

    if rotation_options.get('stop_event') is not None and rotation_options['stop_event'].is_set():
        return None, 'Генерация остановлена.'

    return gemini_utils.generate_chat_reply_original(
        model_name, system_prompt, strip_sticker_images(chat_history, ids), config, **rotation_options)
