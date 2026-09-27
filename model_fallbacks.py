"""Редактируемая цепочка авто-режима: новый обход с начала для каждого ответа."""

import logging
import re

import events
import gemini_utils
import openai_models
import providers
import sticker_guard
import sticker_store
from auto_activity import emit

DEFAULT_CHAIN = ['gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.5-flash-lite',
                 'gpt-5.4', 'gpt-5.4-mini']


def parse_chain(value):
    """Список или имена по строкам/через запятую; порядок и повторы сохраняются."""
    if isinstance(value, str):
        value = re.split(r'[\s,;]+', value.strip()) if value.strip() else []
    if not isinstance(value, list):
        raise ValueError('Цепочка моделей должна быть списком.')
    result = []
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r'[a-zA-Z0-9._:/-]+', item.strip()):
            raise ValueError('Некорректное имя модели в цепочке.')
        result.append(item.strip())
    return result


def generate(settings, selected_model, system_prompt, history, chat_id, progress, stop_event,
             generate_fn, config_fn):
    """Один проход по произвольному числу моделей; API-пул обходит все ключи каждой."""
    enabled = settings.get('auto_fallback_enabled', False)
    chain = parse_chain(settings.get('auto_fallback_models', DEFAULT_CHAIN)) if enabled else [selected_model]
    if not chain:
        return None, 'Цепочка моделей пуста. Добавьте модель в настройках авто-режима.', selected_model
    errors = []
    for position, model in enumerate(chain, 1):
        if stop_event.is_set():
            return None, 'Генерация остановлена.', model
        try:
            provider = providers.provider_for_model(model)
            step_settings = dict(settings)
            free_openai = enabled and provider == 'openai' and openai_models.free_tier_group(model)
            if free_openai:
                step_settings['allow_paid_overage'] = False
                error = gemini_utils._check_openai_budget(
                    model, system_prompt, gemini_utils._history_to_openai_input(history), strict=True)
                if error:
                    errors.append(f'{model}: {error}')
                    emit(progress, 'fallback', f'{model}: {error}. Перехожу дальше.', model=model)
                    continue
            config = config_fn(step_settings, model, cache_key=f'teeka-{chat_id}')
            if free_openai and isinstance(config, dict):
                config['strict_free_budget'] = True
            emit(progress, 'request', f'Запрос к API {providers.LABELS[provider]}: {model}'
                 + (f' ({position}/{len(chain)})' if enabled else '') + '. Ожидаю ответ.', model=model)
            events.publish('generation', {'state': 'started', 'model': model, 'source': 'auto'}, chat_id=chat_id)
            # Предыдущая ступень могла отправить картинки стикеров в карантин.
            request_history = history
            if enabled and position > 1:
                ids = sticker_guard.image_sticker_ids(history)
                if ids:
                    quarantined = sticker_store.quarantined_ids()
                    request_history = sticker_guard.strip_sticker_images(history, ids & quarantined)
            options = {'try_all_keys_on_error': True, 'stop_event': stop_event,
                       'request_timeout_s': max(5, int(settings.get('auto_fallback_request_timeout_s', 300)))} if enabled else {}
            text, error = generate_fn(model_name=model, system_prompt=system_prompt,
                                      chat_history=request_history, config=config, chat_id=chat_id,
                                      progress=progress, **options)
            if not error and text and text.strip():
                return text, None, model
            error = error or 'Модель вернула пустой ответ.'
        except Exception as exc:
            logging.exception('Ошибка ступени авто-режима %s', model)
            error = f'Ошибка генерации: {exc}'
        if stop_event.is_set():
            return None, 'Генерация остановлена.', model
        errors.append(f'{model}: {error}')
        if enabled:
            emit(progress, 'fallback', f'{model}: {error}. '
                     + ('Пробую следующую модель.' if position < len(chain) else 'Цепочка закончилась.'),
                     model=model)
    error = 'Все модели цепочки не сработали. ' + '; '.join(errors) if enabled else errors[-1]
    return None, error, chain[-1]
