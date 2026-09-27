"""Обработчики веб-интерфейса.

Регистрируются в main.py через app.add_url_rule (не Blueprint).

Соглашение: страницы (index, chat_page) отдают HTML, всё остальное — JSON вида
{"status": "success"|"error", "message": "..."}. Раньше почти каждый маршрут делал
redirect на chat_page, из-за чего браузер перезагружал страницу целиком и заново
тянул историю из Telegram. Теперь фронтенд сам обновляет нужный кусок.
"""

from flask import render_template, request, jsonify, Response, stream_with_context, send_file
import json
import logging
import math
import threading
import uuid
from datetime import datetime, timedelta

import app_state
import events
import presets as presets_module
from settings_manager import (
    load_global_settings,
    get_chat_settings,
    generate_sticker_prompt,
    structure_sticker_data,
    save_chat_settings,
    load_chat_settings,
    save_global_settings,
    DEFAULT_CHAT_SETTINGS
    )
import character_utils
import settings_manager
import model_fallbacks
from character_lexicon import parse_lexicon_rules
from shared_storage import locked
from bot_logic import send_generated_reply, auto_mode_worker
from cluster_chatting import cluster_mode_worker

from telegram_utils import (
    get_chats,
    get_chat_info,
    get_formatted_history,
    run_in_telegram_loop,
    get_media_for_message,
    get_avatar,
    get_sticker_preview,
    load_sticker_db,
    cleanup_old_cache_files,
    export_chat_range,
    MEDIA_CACHE_DIR,
    MEDIA_LOADING_MESSAGE,
)
import sticker_store
import sticker_ai
import image_store
from sticker_guard import generate_with_sticker_guard
from gemini_utils import (
    build_generation_config,
    parse_temperature,
    BASE_GEMENI_MODEL,
)
import gemini_models
import gemini_quota
import openai_models
import providers
import key_pool

CHAT_LIMIT = 10000


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def ok(message=None, **extra):
    """Успешный JSON-ответ."""
    payload = {'status': 'success'}
    if message:
        payload['message'] = message
    payload.update(extra)
    return jsonify(payload)


def fail(message, code=400, **extra):
    """Ответ с ошибкой. Текст показывается пользователю как есть."""
    payload = {'status': 'error', 'message': message}
    payload.update(extra)
    return jsonify(payload), code


def _resolve_limit(chat_id, settings_to_use):
    """Сколько сообщений тянуть: из ?limit= или из настроек чата."""
    fallback = settings_to_use.get(
        'num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
    limit_str = request.args.get('limit', str(fallback))
    try:
        limit = int(limit_str)
    except ValueError:
        logging.warning(f"Некорректный лимит '{limit_str}' из URL, используется {fallback}")
        return fallback
    if not (0 < limit <= CHAT_LIMIT):
        logging.warning(f"Недопустимый лимит {limit} из URL, используется {fallback}")
        return fallback
    return limit


def _current_auto_mode_status(chat_id):
    """Статус авто-режима с попутной уборкой мёртвых воркеров."""
    with app_state.auto_mode_lock:
        worker_info = app_state.auto_mode_workers.get(chat_id)
        if worker_info and worker_info["thread"] and worker_info["thread"].is_alive():
            return worker_info["status"]
        if chat_id in app_state.auto_mode_workers:
            del app_state.auto_mode_workers[chat_id]
        return "inactive"


def _acknowledge_history(chat_id, settings):
    # В открытой вкладке история обновляется каждые 5 с. Это тоже не должно
    # отмечать группу прочитанной, пока кластер наблюдает за ней пассивно.
    return not (settings.get('cluster_chatting_enabled') and
                _current_auto_mode_status(chat_id) == 'active')


def _model_options(include_live=True):
    """Список моделей обоих провайдеров: каталог + то, что вернул API (если есть ключ)."""
    options = []

    live_gemini = []
    if include_live and key_pool.is_configured('gemini'):
        live_gemini = gemini_models.fetch_live_model_ids(key_pool.get_any_client('gemini'))
    for option in gemini_models.build_selector_options(live_gemini):
        option['provider'] = 'gemini'
        options.append(option)

    live_openai = []
    if include_live and key_pool.is_configured('openai'):
        live_openai = openai_models.fetch_live_model_ids(key_pool.get_any_client('openai'))
    options.extend(openai_models.build_selector_options(live_openai))
    tier_order = {'strong': 0, 'light': 1, 'unknown': 2, 'paid': 3}
    options.sort(key=lambda item: (item.get('provider', ''), tier_order.get(item.get('tier'), 2),
                                   item.get('rank', 999), item.get('id', '')))
    return options


# ---------------------------------------------------------------------------
# Страницы
# ---------------------------------------------------------------------------

def index():
    """Главная страница — выбор чата."""
    logging.info("Запрос GET /")
    # Все диалоги: у больших аккаунтов это несколько запросов к Telegram, отсюда таймаут.
    chats_data, error = run_in_telegram_loop(get_chats(limit=None), timeout=120)

    if error:
        logging.error(f"Ошибка при получении чатов: {error}")
    elif not chats_data:
        logging.warning("Список чатов пуст или не получен.")

    return render_template('index.html',
                           chats=chats_data if chats_data else [],
                           error=error,
                           global_settings=load_global_settings(),
                           key_status=key_pool.get_status())


def chat_page(chat_id):
    logging.info(f"Запрос GET /chat/{chat_id}")

    auto_control_chat_id = chat_id
    cluster_root = request.args.get('cluster_root', type=int)
    if cluster_root is not None:
        with app_state.auto_mode_lock:
            root_worker = app_state.auto_mode_workers.get(cluster_root, {})
            if root_worker.get('cluster') and root_worker.get('status') in ('active', 'stopping'):
                auto_control_chat_id = cluster_root

    settings_to_use = get_chat_settings(chat_id)
    active_character_id = settings_to_use.get('active_character_id')

    active_character_data = None
    sticker_prompt_text = ""

    if active_character_id:
        active_character_data = character_utils.get_character(active_character_id)
        if active_character_data:
            enabled_packs = active_character_data.get('enabled_sticker_packs', [])
            sticker_prompt_text = generate_sticker_prompt(enabled_packs)

    current_limit = _resolve_limit(chat_id, settings_to_use)

    logging.info(f"Запрос информации для чата {chat_id}")
    chat_info_data, info_error = run_in_telegram_loop(get_chat_info(chat_id))
    if info_error:
        logging.warning(f"Ошибка получения инфо о чате {chat_id}: {info_error}")

    auto_mode_status = _current_auto_mode_status(auto_control_chat_id)

    logging.info(f"Запрос истории для чата {chat_id} с лимитом {current_limit} (быстрый режим)")
    history_data, history_error = run_in_telegram_loop(
        get_formatted_history(chat_id, limit=current_limit,
                              settings=settings_to_use, download_media=False,
                              acknowledge=_acknowledge_history(chat_id, settings_to_use))
    )

    structured_stickers = structure_sticker_data(sticker_store.load())
    sticker_reference_examples = sticker_ai.reference_preview()
    images_data = image_store.list_images()

    return render_template(
        'chat.html',
        chat_id=chat_id,
        auto_control_chat_id=auto_control_chat_id,
        chat_info=chat_info_data,
        info_error=info_error,
        history=history_data if history_data else [],
        history_error=history_error,
        sticker_prompt_text_for_js=sticker_prompt_text,
        structured_sticker_sets=structured_stickers,
        sticker_describer_prompt=sticker_ai.get_system_prompt(),
        sticker_reference_examples=sticker_reference_examples,
        images_list=images_data,
        default_model_name=BASE_GEMENI_MODEL,
        model_options=_model_options(),
        presets=presets_module.list_presets(),
        key_status=key_pool.get_status(),
        current_limit=current_limit,
        auto_mode_status=auto_mode_status,
        chat_settings=settings_to_use,
        all_characters=character_utils.load_characters(),
        active_character_id=active_character_id,
        active_character_data=active_character_data
    )


# ---------------------------------------------------------------------------
# Живые обновления (SSE)
# ---------------------------------------------------------------------------

def events_stream(chat_id):
    """Поток событий для одной вкладки. Держится открытым, пока вкладка жива."""
    also_chat_id = request.args.get('also_chat_id', type=int)
    extra_chat_ids = [also_chat_id] if also_chat_id is not None and also_chat_id != chat_id else []
    response = Response(
        stream_with_context(events.stream(chat_id=chat_id, extra_chat_ids=extra_chat_ids)),
        mimetype='text/event-stream',
    )
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    # Отключаем буферизацию, иначе события копятся и приходят пачкой.
    response.headers['X-Accel-Buffering'] = 'no'
    return response


def history_fragment(chat_id):
    """Свежая история чата, отрендеренная в HTML.

    Фронтенд подставляет её на место списка сообщений — так разметка сообщений
    остаётся в одном месте (шаблон), а не дублируется в JavaScript.
    """
    settings_to_use = get_chat_settings(chat_id)
    limit = _resolve_limit(chat_id, settings_to_use)

    history_data, history_error = run_in_telegram_loop(
        get_formatted_history(chat_id, limit=limit,
                              settings=settings_to_use, download_media=False,
                              acknowledge=_acknowledge_history(chat_id, settings_to_use))
    )
    if history_error:
        return fail(f"Не удалось обновить историю: {history_error}", 500)

    html = render_template('_messages.html',
                           history=history_data or [],
                           chat_id=chat_id)
    return ok(html=html, count=len(history_data or []))


def auto_mode_status(chat_id):
    """Текущее состояние авто-режима — на случай, если событие потерялось."""
    return ok(auto_mode_status=_current_auto_mode_status(chat_id))


# ---------------------------------------------------------------------------
# Генерация и отправка
# ---------------------------------------------------------------------------

def generate_reply(chat_id):
    """Ручная генерация ответа. Возвращает текст, но НЕ отправляет его."""
    logging.info(f"Запрос POST /generate/{chat_id}")

    settings_for_generation = get_chat_settings(chat_id)
    character_id = settings_for_generation.get('active_character_id')

    if not character_id:
        return fail('Активный персонаж не выбран.')

    chat_info_data, _ = run_in_telegram_loop(get_chat_info(chat_id))

    final_system_prompt = character_utils.get_full_prompt_for_character(
        character_id,
        chat_name=chat_info_data.get('name') if chat_info_data else str(chat_id),
        is_group=(chat_id < 0),
        chat_context_prompt=settings_for_generation.get('chat_context_prompt'),
        stickers_as_images=bool(settings_for_generation.get('stickers_as_images')),
        reply_include_quote=bool(settings_for_generation.get('reply_include_quote')),
        reply_include_name=bool(settings_for_generation.get('reply_include_name')),
    )

    limit = settings_for_generation.get(
        'num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
    history_data, history_error = run_in_telegram_loop(
        get_formatted_history(chat_id, limit=limit, settings=settings_for_generation))

    if history_error or not history_data:
        error = history_error or "История чата пуста."
        return fail(f'Ошибка получения истории: {error}', 500)

    # Явно выбранная в форме модель важнее сохранённой в настройках: пользователь
    # только что ткнул в неё пальцем, значит хочет именно её.
    model_name_input = request.form.get('model_name', '').strip()
    model_from_settings = settings_for_generation.get('model_name', '')
    model_name_to_use = model_name_input or model_from_settings or BASE_GEMENI_MODEL

    logging.info(f"Вызов Gemini для генерации (чат {chat_id}, модель: {model_name_to_use})")
    events.publish('generation', {'state': 'started', 'model': model_name_to_use},
                   chat_id=chat_id)

    generated_text, generation_error_message = generate_with_sticker_guard(
        model_name=model_name_to_use,
        system_prompt=final_system_prompt,
        chat_history=history_data,
        config=build_generation_config(settings_for_generation, model_name_to_use,
                                       cache_key=f"teeka-{chat_id}"),
        chat_id=chat_id,
    )

    if generation_error_message:
        logging.error(f"Ошибка Gemini: {generation_error_message}")
        events.publish('generation', {'state': 'error', 'message': generation_error_message},
                       chat_id=chat_id)
        events.publish('keys', key_pool.get_status())
        return fail(generation_error_message, 500)

    reply_to_send = generated_text.strip() if isinstance(generated_text, str) else ""
    logging.info(f"Gemini успешно сгенерировал ответ для чата {chat_id}")
    events.publish('generation', {'state': 'done'}, chat_id=chat_id)
    events.publish('keys', key_pool.get_status())

    return ok(reply=reply_to_send, model=model_name_to_use)


def send_reply(chat_id):
    logging.info(f"Запрос POST /send/{chat_id}")

    message_to_send = request.form.get('message_to_send')

    if not message_to_send or not message_to_send.strip():
        return fail("Нет текста для отправки.")

    success, error_message = send_generated_reply(chat_id, message_to_send)

    if not success:
        logging.error(f"Ошибка отправки сообщения в чат {chat_id}: {error_message}")
        return fail(f"При отправке произошла ошибка: {error_message}", 500)

    logging.info(f"Сообщение для чата {chat_id} успешно отправлено через веб-интерфейс.")
    return ok("Сообщение отправлено.")


# ---------------------------------------------------------------------------
# Авто-режим
# ---------------------------------------------------------------------------

def auto_activity_route(chat_id):
    """Последние события и текущая операция авто-режима, без внешних запросов."""
    import auto_activity
    return ok(**auto_activity.snapshot(chat_id))


def start_auto_mode(chat_id):
    logging.info(f"Запрос POST /start_auto_mode/{chat_id}")
    settings = get_chat_settings(chat_id)
    cluster_enabled = bool(settings.get('cluster_chatting_enabled'))
    if cluster_enabled and chat_id >= 0:
        return fail('Кластерный чатинг можно включить только в группе.')
    if cluster_enabled and not settings.get('active_character_id'):
        return fail('Сначала выберите персонажа для основной группы.')

    with app_state.auto_mode_lock:
        existing = app_state.auto_mode_workers.get(chat_id)
        if existing and existing["thread"] and existing["thread"].is_alive():
            return fail("Авто-режим уже активен или останавливается.")
        if cluster_enabled:
            other = [key for key, info in app_state.auto_mode_workers.items()
                     if key != chat_id and info.get('thread') and info['thread'].is_alive()]
            if other:
                return fail('Кластерный режим управляет аккаунтом целиком. Остановите другие авто-режимы.')
        elif any(info.get('cluster') and info.get('thread') and info['thread'].is_alive()
                 for info in app_state.auto_mode_workers.values()):
            return fail('Кластерный режим уже управляет аккаунтом. Сначала остановите его.')

        logging.info(f"Запуск потока авто-режима для чата {chat_id}...")
        stop_event = threading.Event()
        thread = threading.Thread(
            target=cluster_mode_worker if cluster_enabled else auto_mode_worker,
            args=(chat_id, stop_event),
            name=f"AutoMode-{chat_id}",
            daemon=True
        )

        app_state.auto_mode_workers[chat_id] = {
            "thread": thread,
            "stop_event": stop_event,
            "status": "active",
            "cluster": cluster_enabled,
            "memory_window_anchor": None
        }
        import auto_activity
        auto_activity.reset(chat_id)
        auto_activity.record(chat_id, 'started',
                             'Кластерный режим включён. Слежу за группой и личками участников.'
                             if cluster_enabled else 'Авто-режим включён. Ожидаю сообщения собеседника.')
        thread.start()

    events.publish('auto_mode', {'status': 'active'}, chat_id=chat_id)
    return ok("Авто-режим запущен.", auto_mode_status="active")


def stop_auto_mode(chat_id):
    logging.info(f"Запрос POST /stop_auto_mode/{chat_id}")

    with app_state.auto_mode_lock:
        worker_info = app_state.auto_mode_workers.get(chat_id)

        if worker_info and worker_info["thread"] and worker_info["thread"].is_alive() \
                and worker_info["status"] == "active":
            logging.info(f"Отправка сигнала остановки потоку авто-режима для чата {chat_id}...")
            worker_info["stop_event"].set()
            from auto_chat_cycle import notify_chat
            notify_chat(chat_id)
            worker_info["status"] = "stopping"
            status, message = "stopping", "Авто-режим останавливается..."
        elif worker_info and worker_info["status"] == "stopping":
            status, message = "stopping", "Авто-режим уже останавливается."
        else:
            if chat_id in app_state.auto_mode_workers:
                del app_state.auto_mode_workers[chat_id]
            status, message = "inactive", "Авто-режим не был активен."

    events.publish('auto_mode', {'status': status}, chat_id=chat_id)
    return ok(message, auto_mode_status=status)


# ---------------------------------------------------------------------------
# Медиа и стикеры
# ---------------------------------------------------------------------------

def get_media(chat_id, message_id):
    """Подгрузка медиа для одного сообщения (ленивая, по мере прокрутки)."""
    media_parts, error = run_in_telegram_loop(
        get_media_for_message(chat_id, message_id, wait_for_download=False))

    if error == MEDIA_LOADING_MESSAGE:
        return ok(loading=True)
    if error:
        return fail(error, 500)

    return ok(parts=media_parts)


def get_avatar_route(chat_id):
    """Аватарка чата. URL версионирован photo_id, поэтому кэшируется навсегда."""
    raw = request.args.get('v', '')
    if not raw.isdigit():
        return fail("Нет версии аватарки.", 404)
    path, error = run_in_telegram_loop(get_avatar(chat_id, int(raw)), timeout=30)
    if error or not path:
        return fail(error or "Нет аватарки.", 404)
    response = send_file(path, mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response


def get_sticker_preview_route(sticker_id):
    """Отдаёт превью стикера как jpg. Кэш вечный: id стикера в Telegram не меняется."""
    # get_sticker_preview отдаёт путь или None; при отказе цикла прилетает пара (None, error).
    result = run_in_telegram_loop(get_sticker_preview(sticker_id), timeout=30)
    path = result[0] if isinstance(result, tuple) else result
    if not path:
        return fail("Нет превью.", 404)
    response = send_file(path, mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response


def list_quarantine_route():
    """Карантинные стикеры без решения пользователя — для модалки ревью."""
    return ok(items=sticker_store.list_quarantined())


def quarantine_decision_route():
    """{sticker_id, action: release|describe|ignore, codename?, description?}

    release — снова показывать модели картинкой; describe — дать имя/описание,
    остаётся текстом; ignore — остаётся текстом, из списка ревью уходит.
    """
    payload = request.get_json(silent=True) or {}
    sticker_id = payload.get('sticker_id')
    action = payload.get('action')
    if action == 'ignore':
        success, msg = sticker_store.ignore(sticker_id)
        return ok("Скрыли из списка, стикер остаётся текстом.") if success else fail(msg)
    if action == 'release':
        success, msg = sticker_store.release(sticker_id)
        load_sticker_db()
        return ok("Модель снова видит стикер картинкой.") if success else fail(msg)
    if action == 'describe':
        success, msg = sticker_store.describe(
            sticker_id, payload.get('codename', ''), payload.get('description', ''))
        load_sticker_db()
        return ok(f"Сохранено как «{msg}».") if success else fail(msg)
    return fail("Неизвестное действие.")


def find_stickers_in_chat_route(chat_id):
    """Ищет безымянные стикеры и загружает медиа, не вызывая AI."""
    payload = request.get_json(silent=True) or {}
    try:
        limit = max(1, min(500, int(payload.get('limit') or 100)))
    except (TypeError, ValueError):
        return fail("Количество сообщений должно быть числом от 1 до 500.")
    result, error = run_in_telegram_loop(
        sticker_ai.find_stickers_in_chat(chat_id, limit=limit), timeout=300)
    if error:
        return fail(error, 500)
    if not result['found']:
        message = "В выбранной части чата нет новых безымянных стикеров."
    elif not result['media_ready']:
        message = (f"Найдено {result['found']}, но Telegram не отдал ни одного "
                   "пригодного медиа.")
    else:
        message = (f"Найдено {result['found']}; медиа загружено: "
                   f"{result['media_ready']}. Проверьте карточки и отправьте их нейросети.")
    return ok(message, **result)


def describe_from_chat_route(chat_id):
    """Просит AI описать стикеры, ранее показанные пользователю на первом шаге."""
    payload = request.get_json(silent=True) or {}
    try:
        limit = max(1, min(500, int(payload.get('limit') or 100)))
    except (TypeError, ValueError):
        return fail("Количество сообщений должно быть числом от 1 до 500.")
    settings = get_chat_settings(chat_id)
    model_name = settings.get('model_name') or BASE_GEMENI_MODEL
    system_prompt = payload.get('system_prompt')
    reference_ids = payload.get('reference_ids')
    if not isinstance(reference_ids, list):
        reference_ids = None
    candidate_ids = payload.get('candidate_ids')
    if not isinstance(candidate_ids, list):
        return fail("Сначала найдите стикеры и передайте их список.")
    if not candidate_ids:
        return fail("Не загрузилось ни одного стикера. Нажмите «Найти стикеры» ещё раз.")
    logging.info("AI-описание стикеров: чат %s, модель %s, кандидатов %s",
                 chat_id, model_name, len(candidate_ids))
    if system_prompt is not None:
        try:
            system_prompt = sticker_ai.save_system_prompt(system_prompt)
        except ValueError as exc:
            return fail(str(exc))

    result, error = run_in_telegram_loop(
        sticker_ai.describe_from_chat(chat_id, model_name, limit=limit,
                                      system_prompt=system_prompt,
                                      reference_ids=reference_ids,
                                      candidate_ids=candidate_ids), timeout=300)
    if error:
        return fail(error, 500)
    message = (f"Найдено новых: {result['found']}; модели отправлено: {result.get('media_sent', 0)}; "
               f"эталонов: {result.get('reference_examples', 0)}; "
               f"успешных API-запросов: {result.get('requests', 0)}; описано: {result['described']} "
               f"(модель {result.get('model', '—')}).")
    if result['found'] and not result.get('attempts'):
        message += " Запрос к модели не выполнялся: Telegram не отдал ни одного пригодного превью или файла."
    elif result.get('errors'):
        message += " Ошибка модели: " + result['errors'][0]
    if result.get('quarantined'):
        message += f" Модель отказалась описывать {result['quarantined']} — они в карантине."
    return ok(message, **result)


def sticker_describer_prompt_route():
    """Сохраняет или сбрасывает общий для инстансов системный текст описателя."""
    payload = request.get_json(silent=True) or {}
    try:
        prompt = (sticker_ai.reset_system_prompt() if payload.get('reset')
                  else sticker_ai.save_system_prompt(payload.get('system_prompt')))
    except ValueError as exc:
        return fail(str(exc))
    return ok("Системный текст описателя сохранён.", system_prompt=prompt)


@locked(lambda: character_utils.CHARACTERS_FILE)
def update_sticker_status(chat_id):
    """Обновляет набор включённых стикеров у активного персонажа."""
    logging.info(f"Запрос POST /update_sticker_status/{chat_id}")

    enabled_codenames = request.form.getlist('sticker_enabled')
    character_id = get_chat_settings(chat_id).get('active_character_id')

    if not character_id:
        return fail("Не выбран персонаж для обновления стикеров.")

    all_characters = character_utils.load_characters()
    if character_id not in all_characters:
        return fail("Персонаж не найден.")

    all_characters[character_id]['enabled_sticker_packs'] = enabled_codenames
    if not character_utils.save_characters(all_characters):
        return fail("Ошибка сохранения настроек стикеров.", 500)

    return ok(f"Сохранено стикеров: {len(enabled_codenames)}.")


def sticker_prompt_route(chat_id):
    """Текущий текст промпта стикеров активного персонажа — для кнопки «Скопировать»."""
    character_id = get_chat_settings(chat_id).get('active_character_id')
    if not character_id:
        return ok(text="")
    character = character_utils.get_character(character_id) or {}
    text = generate_sticker_prompt(character.get('enabled_sticker_packs', []))
    return ok(text=text or "")


# ---------------------------------------------------------------------------
# Картинки: как стикеры, но берутся из папки data/images/
# ---------------------------------------------------------------------------

def list_images_route():
    """Список картинок из data/images/ + описания из data/images.json."""
    return ok(items=image_store.list_images())


def get_image_route(codename):
    """Отдаёт файл картинки — <img src="/image/<codename>"> в модалке и чате."""
    path = image_store.find_file(codename)
    if not path:
        return fail("Нет такой картинки.", 404)
    response = send_file(path)
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@locked(lambda: character_utils.CHARACTERS_FILE)
def update_image_status_route(chat_id):
    """Сохраняет набор включённых картинок у активного персонажа."""
    enabled_codenames = request.form.getlist('image_enabled')
    character_id = get_chat_settings(chat_id).get('active_character_id')
    if not character_id:
        return fail("Не выбран персонаж.")

    all_characters = character_utils.load_characters()
    if character_id not in all_characters:
        return fail("Персонаж не найден.")

    all_characters[character_id]['enabled_images'] = enabled_codenames
    if not character_utils.save_characters(all_characters):
        return fail("Ошибка сохранения.", 500)
    return ok(f"Сохранено картинок: {len(enabled_codenames)}.")


def describe_image_route():
    """{codename, description} — правка описания из UI."""
    payload = request.get_json(silent=True) or {}
    if not image_store.set_description(payload.get('codename', ''), payload.get('description', '')):
        return fail("Не удалось сохранить описание.", 500)
    return ok("Описание сохранено.")


def image_prompt_route(chat_id):
    """Текст промпта картинок — для кнопки «Скопировать промпт» на вкладке."""
    character_id = get_chat_settings(chat_id).get('active_character_id')
    if not character_id:
        return ok(text="")
    character = character_utils.get_character(character_id) or {}
    return ok(text=image_store.prompt_text(character.get('enabled_images', [])) or "")


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def save_global_settings_route():
    logging.info("Запрос POST /save_global_settings")

    try:
        settings_to_save = {
            'media_cleanup_enabled': request.form.get('media_cleanup_enabled') in ('true', 'on', '1'),
            'media_cleanup_days': int(request.form.get('media_cleanup_days', 7)),
        }
    except (ValueError, TypeError) as e:
        return fail(f"Ошибка в переданных данных: {e}")

    if not save_global_settings(settings_to_save):
        return fail("Ошибка при сохранении глобальных настроек.", 500)

    if settings_to_save['media_cleanup_enabled']:
        days = settings_to_save['media_cleanup_days']
        logging.info(f"Очистка кэша медиа (файлы старше {days} дней).")
        cleanup_old_cache_files(directory=MEDIA_CACHE_DIR, max_age_days=days)

    return ok("Глобальные настройки сохранены.")


def _read_advanced_settings_form():
    """Разбирает форму продвинутых настроек. Кидает ValueError при кривых числах."""
    form = request.form

    def flag(name):
        return form.get(name) in ('true', 'on', '1')

    chat_id = (request.view_args or {}).get('chat_id')
    previous_settings = get_chat_settings(chat_id) if chat_id is not None else {}

    chain = model_fallbacks.parse_chain(form.get('auto_fallback_models', '\n'.join(model_fallbacks.DEFAULT_CHAIN)))
    if flag('auto_fallback_enabled') and not chain:
        raise ValueError('Включённой цепочке нужна хотя бы одна модель.')

    lexicon = form.get('lexicon_rules')
    if lexicon is None:
        # Старое открытое окно без редактора не должно стирать уже сохранённые правила.
        lexicon = previous_settings.get('lexicon_rules', [])
    lexicon = parse_lexicon_rules(lexicon)
    previous_temperature = previous_settings.get('temperature')
    temperature = parse_temperature(form.get('temperature', previous_temperature))

    if 'cluster_dm_overrides_present' not in form:
        dm_overrides = dict(previous_settings.get('cluster_dm_settings_overrides') or {})
    else:
        dm_overrides = {}
        dm_float_fields = {
            'auto_mode_check_interval': (0.5, None),
            'auto_mode_initial_wait': (0.0, None),
            'typing_delay_ms_min': (0.0, None),
            'typing_delay_ms_max': (0.0, None),
            'max_typing_duration_s': (0.0, None),
            'base_thinking_delay_s_min': (0.0, None),
            'base_thinking_delay_s_max': (0.0, None),
            'substitution_chance': (0.0, 1.0),
            'transposition_chance': (0.0, 1.0),
            'skip_chance': (0.0, 1.0),
            'duplication_chance': (0.0, 1.0),
            'lower_chance': (0.0, 1.0),
            'word_loss_chance': (0.0, 1.0),
            'sticker_choosing_delay_min': (0.0, None),
            'sticker_choosing_delay_max': (0.0, None),
            'image_choosing_delay_min': (0.0, None),
            'image_choosing_delay_max': (0.0, None),
            'image_additional_delay_min': (0.0, None),
            'image_additional_delay_max': (0.0, None),
        }
        for key, (minimum, maximum) in dm_float_fields.items():
            raw = (form.get('cluster_dm_' + key) or '').strip()
            if not raw:
                continue
            value = float(raw)
            if not math.isfinite(value) or value < minimum \
                    or (maximum is not None and value > maximum):
                raise ValueError(f'Некорректное переопределение лички: {key}')
            dm_overrides[key] = value
        raw_lost_words = (form.get('cluster_dm_max_lost_words') or '').strip()
        if raw_lost_words:
            value = int(raw_lost_words)
            if value < 0:
                raise ValueError('Максимум потерянных слов в личке не может быть отрицательным.')
            dm_overrides['max_lost_words'] = value

    return {
        'can_see_photos': flag('can_see_photos'),
        'can_see_videos': flag('can_see_videos'),
        'can_see_audio': flag('can_see_audio'),
        'can_see_files_pdf': flag('can_see_files_pdf'),
        'ignore_all_media': flag('ignore_all_media'),
        'enable_auto_memory': flag('enable_auto_memory'),
        'stickers_as_images': flag('stickers_as_images'),
        'reply_include_quote': flag('reply_include_quote'),
        'reply_include_name': flag('reply_include_name'),
        'reply_load_outside_context': flag('reply_load_outside_context'),
        'reply_context_depth': max(1, min(3, int(form.get(
            'reply_context_depth', get_chat_settings(chat_id).get('reply_context_depth', 3))))),
        'image_choosing_delay_min': float(form.get('image_choosing_delay_min', form.get('sticker_choosing_delay_min', 2.0))),
        'image_choosing_delay_max': float(form.get('image_choosing_delay_max', form.get('sticker_choosing_delay_max', 5.5))),
        'image_additional_delay_min': max(0.0, float(form.get('image_additional_delay_min', 0.2))),
        'image_additional_delay_max': max(0.0, float(form.get('image_additional_delay_max', 0.8))),
        'image_history_ttl_hours': max(0.0, float(form.get('image_history_ttl_hours', 168))),
        'auto_mode_check_interval': float(form.get('auto_mode_check_interval')),
        'auto_mode_initial_wait': float(form.get('auto_mode_initial_wait')),
        'auto_mode_no_reply_timeout': float(form.get('auto_mode_no_reply_timeout')),
        'auto_mode_no_reply_suffix': form.get('auto_mode_no_reply_suffix', ''),
        'cluster_chatting_enabled': flag('cluster_chatting_enabled'),
        'cluster_no_commitment': flag('cluster_no_commitment'),
        'cluster_dm_idle_minutes': max(0.1, float(form.get('cluster_dm_idle_minutes', 4))),
        'cluster_dm_prompt': form.get('cluster_dm_prompt') or DEFAULT_CHAT_SETTINGS['cluster_dm_prompt'],
        'cluster_group_prompt': form.get('cluster_group_prompt') or DEFAULT_CHAT_SETTINGS['cluster_group_prompt'],
        'cluster_context_prompt': form.get('cluster_context_prompt') or DEFAULT_CHAT_SETTINGS['cluster_context_prompt'],
        'cluster_dm_settings_overrides': dm_overrides,
        'auto_fallback_enabled': flag('auto_fallback_enabled'),
        'auto_fallback_models': chain,
        'auto_fallback_request_timeout_s': max(5, int(form.get('auto_fallback_request_timeout_s', 300))),
        'auto_fallback_timeout_version': 2,
        'model_name': form.get('model_name_advanced', '').strip(),
        'enable_google_search': flag('enable_google_search'),
        'enable_thinking': flag('enable_thinking'),
        'reasoning_effort': (form.get('reasoning_effort') or 'high').strip().lower(),
        'temperature': temperature,
        'allow_paid_overage': flag('allow_paid_overage'),
        'num_messages_to_fetch': int(form.get('num_messages_to_fetch')),
        'sticker_choosing_delay_min': float(form.get('sticker_choosing_delay_min')),
        'sticker_choosing_delay_max': float(form.get('sticker_choosing_delay_max')),
        'base_thinking_delay_s_min': float(form.get('base_thinking_delay_s_min')),
        'base_thinking_delay_s_max': float(form.get('base_thinking_delay_s_max')),
        'typing_delay_ms_min': float(form.get('typing_delay_ms_min')),
        'typing_delay_ms_max': float(form.get('typing_delay_ms_max')),
        'max_typing_duration_s': float(form.get('max_typing_duration_s')),
        'substitution_chance': float(form.get('substitution_chance')),
        'transposition_chance': float(form.get('transposition_chance')),
        'skip_chance': float(form.get('skip_chance')),
        'duplication_chance': float(form.get('duplication_chance', DEFAULT_CHAT_SETTINGS['duplication_chance'])),
        'lower_chance': float(form.get('lower_chance')),
        'word_loss_chance': float(form.get('word_loss_chance', 0.0)),
        'max_lost_words': int(form.get('max_lost_words', 1)),
        'lexicon_rules': lexicon,
    }


@locked(lambda: [settings_manager.CHAT_SETTINGS_FILE, character_utils.CHARACTERS_FILE])
def _store_advanced_settings(chat_id, character_id, advanced_settings_data, also_as_default):
    """Кладёт настройки в chat_settings и, если просят, в дефолты персонажа."""
    all_chat_settings = load_chat_settings()
    all_chat_settings.setdefault(chat_id, {})
    all_chat_settings[chat_id].setdefault('character_specifics', {})
    all_chat_settings[chat_id]['character_specifics'].setdefault(character_id, {})
    all_chat_settings[chat_id]['character_specifics'][character_id]['advanced_settings'] = \
        advanced_settings_data

    if not save_chat_settings(all_chat_settings):
        return False, "Не удалось сохранить настройки чата."

    logging.info(f"Сохранены настройки для персонажа {character_id} в чате {chat_id}.")

    if not also_as_default:
        return True, "Настройки сохранены для этого чата."

    all_characters = character_utils.load_characters()
    if character_id not in all_characters:
        return True, "Настройки чата сохранены, но персонаж для дефолтов не найден."

    all_characters[character_id]['advanced_settings'] = advanced_settings_data
    if not character_utils.save_characters(all_characters):
        return True, "Настройки чата сохранены, но дефолты персонажа обновить не удалось."

    logging.info(f"Обновлены настройки по умолчанию для персонажа {character_id}.")
    return True, "Сохранено для чата и как настройки по умолчанию для персонажа."


def save_chat_settings_route(chat_id):
    """Сохраняет продвинутые настройки чата (и опционально дефолты персонажа)."""
    logging.info(f"Запрос POST /save_chat_settings/{chat_id}")

    save_action = request.form.get('save_action')
    if not save_action:
        return fail("Действие для сохранения не определено.")

    character_id = load_chat_settings().get(chat_id, {}).get('active_character_id')
    if not character_id:
        return fail("Активный персонаж не выбран. Настройки не сохранены.")

    try:
        advanced_settings_data = _read_advanced_settings_form()
    except (ValueError, TypeError) as e:
        return fail(f"Ошибка в настройках: {e}")

    saved, message = _store_advanced_settings(
        chat_id, character_id, advanced_settings_data,
        also_as_default=(save_action == 'save_for_chat_and_default'))

    if not saved:
        return fail(message, 500)
    return ok(message)


@locked(lambda: settings_manager.CHAT_SETTINGS_FILE)
def reset_chat_settings_route(chat_id):
    """Сбрасывает настройки персонажа в этом чате к его дефолтам."""
    logging.info(f"Запрос POST /reset_chat_settings/{chat_id}")

    all_settings = load_chat_settings()
    character_id = all_settings.get(chat_id, {}).get('active_character_id')

    if not character_id:
        return fail("Не выбран персонаж, настройки которого нужно сбросить.")

    specifics = all_settings.get(chat_id, {}).get('character_specifics', {})
    if character_id not in specifics:
        return ok("Здесь и так используются настройки персонажа по умолчанию.")

    del specifics[character_id]
    if not specifics:
        del all_settings[chat_id]['character_specifics']

    if not save_chat_settings(all_settings):
        return fail("Не удалось сохранить сброс настроек.", 500)

    return ok("Настройки сброшены к значениям персонажа по умолчанию.")


def apply_preset_route(chat_id):
    """Накладывает пресет на текущие настройки чата."""
    preset_id = request.form.get('preset_id', '')
    logging.info(f"Запрос POST /apply_preset/{chat_id} (пресет '{preset_id}')")

    character_id = load_chat_settings().get(chat_id, {}).get('active_character_id')
    if not character_id:
        return fail("Сначала выберите персонажа для этого чата.")

    current = get_chat_settings(chat_id)
    updated, error = presets_module.apply_preset(preset_id, current)
    if error:
        return fail(error)

    # Оставляем только те ключи, которые реально хранятся в advanced_settings.
    advanced = {k: v for k, v in updated.items() if k in DEFAULT_CHAT_SETTINGS}

    saved, message = _store_advanced_settings(
        chat_id, character_id, advanced, also_as_default=False)
    if not saved:
        return fail(message, 500)

    preset = presets_module.get_preset(preset_id)
    return ok(f"Применён пресет «{preset['name']}».", settings=advanced)


def list_presets_route():
    return ok(presets=presets_module.list_presets())


@locked(lambda: settings_manager.CHAT_SETTINGS_FILE)
def set_chat_model_route(chat_id):
    """Сохраняет модель для этого чата — в настройки активного персонажа в чате.

    Единственная точка выбора модели: её используют и ручная генерация, и авто-режим.
    """
    model_name = request.form.get('model_name', '').strip()
    logging.info(f"Запрос POST /chat/{chat_id}/set_model ('{model_name}')")

    all_settings = load_chat_settings()
    character_id = all_settings.get(chat_id, {}).get('active_character_id')
    if not character_id:
        return fail("Сначала выберите персонажа для этого чата.")

    specifics = (all_settings.setdefault(chat_id, {})
                 .setdefault('character_specifics', {})
                 .setdefault(character_id, {}))
    specifics.setdefault('advanced_settings', {})['model_name'] = model_name
    if not save_chat_settings(all_settings):
        return fail("Не удалось сохранить модель.", 500)

    retired = providers.is_retired(model_name)
    if retired:
        return ok(f"Модель сохранена, но она не работает: {retired}", model_name=model_name)
    return ok(f"Модель для чата: {model_name or 'по умолчанию'}.", model_name=model_name)


# ---------------------------------------------------------------------------
# Модели и ключи
# ---------------------------------------------------------------------------

def list_models_route():
    """Список моделей с ценами для выпадающего списка."""
    return ok(models=_model_options(),
              default=BASE_GEMENI_MODEL,
              defaults={'gemini': BASE_GEMENI_MODEL, 'openai': openai_models.DEFAULT_MODEL},
              providers=key_pool.get_status()['providers'],
              retired=gemini_models.RETIRED_MODELS,
              catalog_updated=gemini_models.CATALOG_UPDATED)


def keys_status_route():
    return ok(openai_admin_key_masked=key_pool.openai_admin_key_masked(), **key_pool.get_status())


def copy_keys_route():
    payload = request.get_json(silent=True) or {}
    ids = payload.get('key_ids') if isinstance(payload, dict) else None
    if not isinstance(ids, list) or not ids or any(not isinstance(identity, str) for identity in ids):
        return fail('Выберите ключи для копирования.')
    try:
        response = ok(values=key_pool.key_values_for_copy(ids))
    except ValueError as exc:
        return fail(str(exc))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    return response


def gemini_usage_route():
    """Общая для проектов таблица Gemini, показанная для ключей текущего инстанса."""
    status = key_pool.get_status()
    if status.get('gemini_quota_error'):
        return fail(status['gemini_quota_error'], 500)
    return ok(keys=[k for k in status['keys'] if k['provider'] == 'gemini'],
              blocked_models=status['blocked_models'],
              source='gemini_budget', profile_date=gemini_quota.PROFILE_DATE)


def correct_gemini_usage_route():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or not isinstance(payload.get('values'), dict):
        return fail('Ожидались ключ, модель и числовые значения строки.')
    try:
        row = key_pool.correct_gemini_usage(payload.get('key_id'), payload.get('model'), payload['values'])
    except (ValueError, TypeError) as exc:
        return fail(str(exc))
    except Exception as exc:
        logging.exception('Ошибка коррекции общего журнала Gemini')
        return fail(f'Не удалось изменить журнал Gemini: {exc}', 500)
    events.publish('keys', key_pool.get_status())
    return ok('Счётчики и лимиты обновлены для всех проектов с этим ключом. Пауза этой модели на ключе сброшена.', row=row)


def openai_usage_route():
    """Реальное потребление организации OpenAI за сегодня (UTC) — нужен Admin-ключ."""
    summary, error = key_pool.fetch_openai_org_usage()
    if error:
        return fail(error, 502 if 'OpenAI' in error else 400)
    return ok(**summary)


def openai_budget_route():
    """Быстрый приблизительный остаток общих бесплатных групп без принудительного запроса к API."""
    budget = providers.budget()
    if not budget:
        return ok(available=False)
    return ok(available=True, **budget.status())


def save_keys_route():
    """Сохраняет пул ключей.

    Ждёт JSON: {"keys": [{"id": "...", "label": "...", "key": "...", "enabled": true,
                          "provider": "gemini"|"openai"}]}
    Ключ со значением-маской (в нём есть '…') считается неизменённым и берётся
    из уже сохранённых — чтобы не гонять секреты в браузер и обратно.
    """
    payload = request.get_json(silent=True) or {}
    incoming = payload.get('keys')
    if not isinstance(incoming, list):
        return fail("Ожидался список ключей.")

    existing = {k['id']: k for k in key_pool.load_keys()}

    cleaned = []
    for i, item in enumerate(incoming):
        if not isinstance(item, dict):
            continue
        raw_key = (item.get('key') or '').strip()
        key_id = str(item.get('id') or '').strip()

        if '…' in raw_key or not raw_key:
            # Пользователь не менял этот ключ — берём сохранённое значение.
            previous = existing.get(key_id)
            if not previous:
                continue
            raw_key = previous['key']

        cleaned.append({
            'id': key_id or f'key{uuid.uuid4().hex[:8]}',
            'label': (item.get('label') or f'Ключ {i + 1}').strip(),
            'key': raw_key,
            'enabled': bool(item.get('enabled', True)),
            'provider': item.get('provider') or (existing.get(key_id) or {}).get('provider') or 'gemini',
        })

    # Admin-ключ: отсутствует в payload — не трогаем; маска — не трогаем; '' — убираем.
    admin_key = payload.get('openai_admin_key')
    if admin_key is not None:
        admin_key = str(admin_key).strip()
        if '…' in admin_key:
            admin_key = None

    if not key_pool.save_keys(cleaned, openai_admin_key=admin_key):
        return fail("Не удалось сохранить файл с ключами.", 500)

    status = key_pool.get_status()
    events.publish('keys', status)
    return ok(f"Сохранено ключей: {len(cleaned)}.",
              openai_admin_key_masked=key_pool.openai_admin_key_masked(), **status)


# ---------------------------------------------------------------------------
# Персонажи
# ---------------------------------------------------------------------------

@locked(lambda: settings_manager.CHAT_SETTINGS_FILE)
def set_active_character(chat_id):
    logging.info(f"Запрос POST /chat/{chat_id}/set_active_character")
    character_id = request.form.get('character_id')

    character = character_utils.get_character(character_id)
    if not character:
        return fail("Персонаж не найден.")

    all_settings = load_chat_settings()
    all_settings.setdefault(chat_id, {})
    all_settings[chat_id]['active_character_id'] = character_id

    if not save_chat_settings(all_settings):
        return fail("Не удалось сохранить выбор персонажа.", 500)

    return ok(f"Выбран персонаж «{character.get('name', 'без имени')}».")


def create_character():
    logging.info("Запрос POST /character/create")
    character_name = request.form.get('new_character_name', '').strip() or 'Новый персонаж'

    new_id = character_utils.create_new_character(character_name)
    if not new_id:
        return fail("Не удалось создать персонажа.", 500)

    return ok(f"Персонаж «{character_name}» создан.", character_id=new_id)


@locked(lambda: [settings_manager.CHAT_SETTINGS_FILE, character_utils.CHARACTERS_FILE])
def save_character(character_id, chat_id):
    """Сохраняет данные персонажа и контекст, специфичный для этого чата."""
    logging.info(f"Запрос POST /character/save/{character_id} для чата {chat_id}")

    characters = character_utils.load_characters()
    if character_id not in characters:
        return fail("Персонаж для сохранения не найден.")

    memory_model = (request.form.get('memory_model_name') or '').strip()
    if memory_model and not (
        openai_models.is_text_model(memory_model) or
        (memory_model.startswith('gemini-') and gemini_models.is_text_model(memory_model)
         and not gemini_models.is_retired(memory_model))
    ):
        return fail("Выберите текстовую модель для памяти из списка.")
    characters[character_id]['memory_model_name'] = memory_model

    for field in ('character_name', 'personality_prompt', 'memory_prompt',
                  'system_commands_prompt', 'memory_update_prompt'):
        if field not in request.form:
            continue
        key = 'name' if field == 'character_name' else field
        characters[character_id][key] = request.form.get(field)

    save_character_success = character_utils.save_characters(characters)

    all_chat_settings = load_chat_settings()
    all_chat_settings.setdefault(chat_id, {})
    all_chat_settings[chat_id].setdefault('character_specifics', {})
    all_chat_settings[chat_id]['character_specifics'].setdefault(character_id, {})
    all_chat_settings[chat_id]['character_specifics'][character_id]['chat_context_prompt'] = \
        request.form.get('chat_context_prompt', '')
    save_chat_settings(all_chat_settings)
    logging.info(f"Контекст для персонажа {character_id} в чате {chat_id} обновлён.")

    if not save_character_success:
        return fail("Контекст чата сохранён, но данные персонажа сохранить не удалось.", 500)

    return ok(f"Персонаж «{characters[character_id].get('name')}» сохранён.")


def export_chat_route(chat_id):
    """Скачивает историю чата за диапазон дат в виде JSON-файла.

    Query-параметры:
        from, to — локальное время в формате datetime-local: 'YYYY-MM-DDTHH:MM'.
    """
    logging.info(f"Запрос GET /export/{chat_id} from={request.args.get('from')} to={request.args.get('to')}")

    def parse(name):
        raw = (request.args.get(name) or '').strip()
        if not raw:
            return None
        # datetime-local: '2026-08-01T14:30' или с секундами
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    dt_from = parse('from')
    dt_to = parse('to')
    if not dt_from or not dt_to:
        return fail("Укажите диапазон 'from' и 'to' в формате YYYY-MM-DDTHH:MM.")
    if dt_from > dt_to:
        return fail("Начало диапазона позже его конца.")
    if dt_to - dt_from > timedelta(days=366):
        return fail("Диапазон больше года — сузьте его, иначе выгрузка будет слишком долгой.")

    # Экспорт может занимать минуты для длинных периодов — расширяем таймаут.
    payload, error = run_in_telegram_loop(
        export_chat_range(chat_id, dt_from, dt_to), timeout=600)
    if error:
        return fail(f"Не удалось выгрузить историю: {error}", 500)

    body = json.dumps(payload, ensure_ascii=False, indent=2)

    stamp_from = dt_from.strftime('%Y%m%d_%H%M')
    stamp_to = dt_to.strftime('%Y%m%d_%H%M')
    # Не даём пользовательскому имени чата попасть в имя файла — только id.
    filename = f'chat_{chat_id}_{stamp_from}-{stamp_to}.json'

    response = Response(body, mimetype='application/json; charset=utf-8')
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.headers['Cache-Control'] = 'no-store'
    return response


def update_memory_route(chat_id):
    """Сжимает недавнюю историю в одну строку памяти персонажа."""
    logging.info(f"Запрос POST /chat/{chat_id}/update_memory")

    settings_to_use = get_chat_settings(chat_id)
    character_id = settings_to_use.get('active_character_id')

    if not character_id:
        return fail("Не выбран персонаж для обновления памяти.")

    limit_for_memory = settings_to_use.get(
        'num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
    logging.info(f"Для анализа памяти будет использовано {limit_for_memory} сообщений.")

    chat_info, _ = run_in_telegram_loop(get_chat_info(chat_id))
    history, history_error = run_in_telegram_loop(
        get_formatted_history(chat_id, limit=limit_for_memory, settings=settings_to_use))

    if history_error:
        return fail(f"Ошибка получения истории для анализа: {history_error}", 500)
    if not history:
        return fail("История сообщений пуста, нечего добавлять в память.")

    memory, error = character_utils.update_character_memory(
        character_id=character_id,
        chat_name=chat_info.get('name', str(chat_id)) if chat_info else str(chat_id),
        is_group=chat_id < 0,
        chat_history=history,
        model_name=settings_to_use.get('model_name') or BASE_GEMENI_MODEL,
        allow_paid_overage=bool(settings_to_use.get('allow_paid_overage')),
    )

    events.publish('keys', key_pool.get_status())

    if error:
        return fail(f"Ошибка обновления памяти: {error}", 500)

    return ok("Память персонажа обновлена.", memory_prompt=memory)
