import threading
import asyncio
import time
import logging
import random
import os
import re

# Импорты наших модулей
import app_state
import events
import auto_activity
import key_pool
import providers
import model_fallbacks
from settings_manager import DEFAULT_CHAT_SETTINGS, get_chat_settings
from text_utils import (
    parse_time_from_message,
    replace_standalone_sticker_names,
    split_message_by_limit,
    VALID_REACTIONS,
)
from reply_protocol import ANSWER_RE, extract_reply, strip_system_lines, split_outside_reply
import character_utils
from telegram_utils import (
    telegram_main_loop,
    run_in_telegram_loop,
    get_formatted_history,
    get_chat_info,
    wait_for_chat_send_slot,
    send_telegram_message,
    send_sticker_by_codename,
    send_sticker_by_id,
    send_image_by_codename,
    send_images_with_caption,
    send_telegram_reaction,
    max_send_duration,
)
from gemini_utils import build_generation_config, BASE_GEMENI_MODEL
from sticker_guard import generate_with_sticker_guard
from auto_chat_cycle import AutoChatState, check_auto_chat, register_chat, unregister_chat, notify_chat
TELAGRAMM_API_ID = os.getenv('TELAGRAMM_API_ID')
TELAGRAMM_API_HASH = os.getenv('TELAGRAMM_API_HASH')

# Глобальная константа (или можно брать из settings_manager)
TELEGRAM_MAX_MESSAGE_LENGTH = 4006

def start_telegram_thread(session_name_to_use: str):
    """Запускает поток для Telethon с УКАЗАННЫМ именем сессии."""
    if app_state.telegram_thread and app_state.telegram_thread.is_alive():
        logging.warning("Поток Telethon уже запущен.")
        return

    logging.info(f"Запуск потока для Telethon с сессией '{session_name_to_use}'...")
    thread = threading.Thread(
        target=asyncio.run, 
        args=(telegram_main_loop( 
            TELAGRAMM_API_ID,
            TELAGRAMM_API_HASH,
            session_name_to_use,  
            app_state.telegram_ready_event 
        ),),
        name=f"TelegramThread-{session_name_to_use}", 
        daemon=True 
    )
    thread.start()
    app_state.telegram_thread = thread
    logging.info("Поток Telethon запущен. Ожидание сигнала готовности...")

def stop_telegram_thread():
    """Останавливает цикл событий Telethon и ждет завершения потока."""
    logging.info("Остановка всех активных потоков авто-режима...")
    import instance_auth
    instance_auth.cancel()
    with app_state.auto_mode_lock:
        for chat_id, worker_info in list(app_state.auto_mode_workers.items()):
            if worker_info["thread"] and worker_info["thread"].is_alive():
                logging.info(f"Отправка сигнала остановки потоку для чата {chat_id}")
                worker_info["stop_event"].set()
                notify_chat(chat_id)
                worker_info["status"] = "stopping" 
        
        active_threads = [wi["thread"] for wi in app_state.auto_mode_workers.values() if wi["thread"] and wi["thread"].is_alive()]
    if active_threads:
        logging.info(f"Ожидание завершения {len(active_threads)} потоков авто-режима (макс 5 секунд)...")
        for thread in active_threads:
            thread.join(timeout=5.0 / len(active_threads) if len(active_threads) > 0 else 5.0)
            if thread.is_alive():
                logging.warning(f"Поток {thread.name} не завершился вовремя.")
    logging.info("Все потоки авто-режима остановлены или им дан сигнал.")

    logging.info("Получен сигнал завершения. Остановка потока Telethon...")
    from telegram_utils import telegram_loop, client as telethon_client, disconnect_telegram 

    if telegram_loop and telegram_loop.is_running():
        if telethon_client and telethon_client.is_connected():
            logging.info("Отправка команды disconnect в цикл Telethon...")
            future = asyncio.run_coroutine_threadsafe(disconnect_telegram(), telegram_loop)
            try:
                future.result(timeout=10)
                logging.info("Команда disconnect выполнена.")
            except asyncio.TimeoutError:
                logging.warning("Отключение Telethon заняло слишком много времени.")
            except Exception as e:
                 logging.error(f"Ошибка при выполнении disconnect_telegram: {e}")
        else:
            logging.info("Клиент не подключен, остановка цикла Telethon...")
            telegram_loop.call_soon_threadsafe(telegram_loop.stop)

    if app_state.telegram_thread and app_state.telegram_thread.is_alive():
        logging.info("Ожидание завершения потока Telethon (до 15 секунд)...")
        app_state.telegram_thread.join(timeout=15)
        if app_state.telegram_thread.is_alive():
            logging.warning("Поток Telethon не завершился вовремя.")
        else:
            logging.info("Поток Telethon успешно завершен.")
    else:
        logging.info("Поток Telethon не был активен.")

def _bundle_image_tasks(tasks):
    """В непрерывном блоке текста/картинок подпись и до 9 фото — одна задача."""
    result, pending = [], []
    def flush():
        if not pending: return
        images = [item['content'] for item in pending if item['type'] == 'image']
        if not images:
            result.extend(pending)
        else:
            if len(images) > 9: raise ValueError('В одном альбоме максимум 9 картинок. Сократите число картинок.')
            caption = '\n\n'.join(item['content'] for item in pending if item['type'] == 'text')
            if len(extract_reply(caption)[0].encode('utf-16-le')) // 2 > 1024:
                raise ValueError('Подпись к картинкам длиннее 1024 символов. Сократите её.')
            result.append({'type': 'images', 'content': {'codenames': images, 'caption': caption}})
        pending.clear()
    for task in tasks:
        if task['type'] in ('text', 'image'): pending.append(task)
        else: flush(); result.append(task)
    flush()
    return result


def send_generated_reply(chat_id: int, message_text: str, settings: dict = None, progress=None, stop_event=None):
    """
    Централизованная функция для отправки сгенерированного ответа.
    Обрабатывает команды react(), разделитель {split}, команды sticker() и смешанный контент.
    """

    if not message_text or not message_text.strip():
        logging.warning(f"В send_generated_reply передано пустое сообщение для чата {chat_id}.")
        return True, "Empty message provided."

    if settings is None:
        logging.debug(f"send_generated_reply: настройки не переданы, загружаются для чата {chat_id}")
        settings_to_use = get_chat_settings(chat_id)
    else:
        logging.debug(f"send_generated_reply: используются переданные настройки для чата {chat_id}")
        settings_to_use = dict(settings)

    if stop_event is not None:
        settings_to_use = {**settings_to_use, '_send_stop_event': stop_event}

    try:
        message_text = replace_standalone_sticker_names(message_text)
    except Exception as e:
        logging.error(f"Ошибка при исправлении имен стикеров: {e}", exc_info=True)


    message_text = strip_system_lines(message_text)
    reaction_tasks = []
    if 'react' in message_text: 
        react_pattern_with_id = re.compile(r"react\s*\(\s*(\d+)\s*\)\s*(?:\[([^\]\n]+?)\]|([^\s\w\d,.<>{|}]+))", re.IGNORECASE)
        
        matches = list(react_pattern_with_id.finditer(message_text))
        reply_ranges = [(m.start(), m.end()) for m in ANSWER_RE.finditer(message_text)]
        for match in matches:
            if any(start <= match.start() < end for start, end in reply_ranges): continue
            msg_id_str = match.group(1)
            emoji_str = match.group(2) or match.group(3)

            if not emoji_str: continue
            
            try:
                msg_id = int(msg_id_str)
            except ValueError:
                logging.warning(f"Невалидный ID сообщения '{msg_id_str}' в команде реакции. Пропуск.")
                continue

            if emoji_str not in VALID_REACTIONS:
                new_emoji = random.choice(VALID_REACTIONS)
                logging.warning(f"Невалидный эмодзи для реакции '{emoji_str}'. Заменен на случайный: '{new_emoji}'.")
                emoji_str = new_emoji
            
            reaction_tasks.append({"type": "reaction", "message_id": msg_id, "emoji": emoji_str})

        message_text = re.sub(r'react\s*\(\s*\d+\s*\)\s*(?:\[[^\]\n]+?\]|[^\s\w\d,.<>{|}]+)\s*',
            lambda match: match.group(0) if any(start <= match.start() < end for start, end in reply_ranges) else '',
            message_text, flags=re.IGNORECASE).strip()
        message_text = re.sub(r'react\s*\[[^\]\n]+?\]', '', message_text, flags=re.IGNORECASE).strip()

    if not message_text.strip() and not reaction_tasks:
        logging.warning(f"В send_generated_reply для чата {chat_id} не осталось ни текста, ни задач на реакцию. Отправка отменена.")
        return True, "Empty message and no reaction tasks."


    # Принимаем и старое `sticker(codename)`, и новое `sticker(id=NNN[, codename=…])`
    # — с двумя формами через опциональный префикс id=. Плюс image(codename) для картинок
    # из data/images/. Обе команды парсятся одной регуляркой — иначе порядок «текст-стикер-
    # картинка» пришлось бы восстанавливать двумя проходами.
    sticker_pattern = (r"(?:(sticker)|(image))\s*\(\s*"
                       r"(?:id\s*=\s*([^)]*)|([\w\d_-]+))\s*\)"
                       r"(?:\s*-\s*не удалось загрузить\.?)?")
    split_separator = "{split}"

    tasks_to_send = []
    tasks_to_send.extend(reaction_tasks)
    
    if message_text.strip():
        initial_parts = [p.strip() for p in split_outside_reply(message_text, split_separator) if p.strip()]
        
        for part in initial_parts:
            found_stickers = list(re.finditer(sticker_pattern, part, re.IGNORECASE))
            reply_ranges = [(m.start(), m.end()) for m in ANSWER_RE.finditer(part)]
            found_stickers = [m for m in found_stickers if not any(start <= m.start() < end for start, end in reply_ranges)]
            
            if not found_stickers:
                if len(part) > TELEGRAM_MAX_MESSAGE_LENGTH:
                    text_chunks = split_message_by_limit(part, TELEGRAM_MAX_MESSAGE_LENGTH)
                    for chunk in text_chunks:
                        tasks_to_send.append({"type": "text", "content": chunk})
                else:
                    tasks_to_send.append({"type": "text", "content": part})
                continue

            last_index = 0
            for match in found_stickers:
                start, end = match.span()
                if start > last_index:
                    text_before = part[last_index:start].strip()
                    if text_before:
                        if len(text_before) > TELEGRAM_MAX_MESSAGE_LENGTH:
                            text_chunks = split_message_by_limit(text_before, TELEGRAM_MAX_MESSAGE_LENGTH)
                            for chunk in text_chunks:
                                tasks_to_send.append({"type": "text", "content": chunk})
                        else:
                            tasks_to_send.append({"type": "text", "content": text_before})

                is_sticker = bool(match.group(1))
                sticker_id_raw = match.group(3)
                codename = match.group(4)
                if is_sticker and sticker_id_raw is not None:
                    # Любую sticker(id=...) вырезаем из текста. Задачу создаём только
                    # для целого положительного Telegram id; мусорная команда исчезает.
                    sticker_id_text = sticker_id_raw.split(',', 1)[0].strip()
                    if sticker_id_text.isdigit() and int(sticker_id_text) > 0:
                        tasks_to_send.append({"type": "sticker_id", "content": int(sticker_id_text)})
                elif is_sticker:
                    tasks_to_send.append({"type": "sticker", "content": codename})
                else:
                    # image(id=…) значения не имеет — только codename.
                    image_name = codename or ''
                    if image_name:
                        tasks_to_send.append({"type": "image", "content": image_name})

                last_index = end
            
            if last_index < len(part):
                text_after = part[last_index:].strip()
                if text_after:
                     if len(text_after) > TELEGRAM_MAX_MESSAGE_LENGTH:
                        text_chunks = split_message_by_limit(text_after, TELEGRAM_MAX_MESSAGE_LENGTH)
                        for chunk in text_chunks:
                            tasks_to_send.append({"type": "text", "content": chunk})
                     else:
                        tasks_to_send.append({"type": "text", "content": text_after})

    try:
        tasks_to_send = _bundle_image_tasks(tasks_to_send)
    except ValueError as err:
        auto_activity.emit(progress, 'error', str(err), level='error')
        return False, str(err)
    logging.info(f"Будет выполнено {len(tasks_to_send)} задач на отправку в чат {chat_id}.")
    
    all_success = True
    first_error_message = None

    for i, task in enumerate(tasks_to_send):
        if stop_event is not None and stop_event.is_set():
            return False, 'Отправка отменена: авто-режим остановлен.'
        kind = task['type']
        preview = (f"react({task['message_id']})[{task['emoji']}]" if kind == 'reaction'
                   else str(task.get('content', '')))
        if kind == 'images':
            preview = task['content']['caption'] + '\n' + ' '.join(f'image({name})' for name in task['content']['codenames'])
        kind_label = {'text': 'текст', 'sticker': 'стикер', 'sticker_id': 'стикер по ID',
                      'image': 'картинка', 'images': 'картинки с подписью', 'reaction': 'реакция'}[kind]
        auto_activity.emit(progress, 'part', f'Часть {i + 1} из {len(tasks_to_send)}: {kind_label}',
                           text=preview, part=i + 1, total=len(tasks_to_send), kind=kind)
        success = False
        error_message = None

        timeout = 60 

        if task["type"] == "text":
            logging.info(f"Отправка текста в чат {chat_id}: \"{task['content'][:50]}...\"")
            
            timeout = max_send_duration(settings_to_use) + 20
            logging.info(f"Таймаут отправки (худший случай печати и правки): {timeout:.0f}s.")
            
            success, error_message = run_in_telegram_loop(
                send_telegram_message(chat_id, task["content"], settings=settings_to_use, progress=progress),
                timeout=timeout
            )

        elif task["type"] == "sticker":
            logging.info(f"Отправка стикера '{task['content']}' в чат {chat_id}.")
            success, error_message = run_in_telegram_loop(send_sticker_by_codename(chat_id, task["content"], settings=settings_to_use, progress=progress))

            if success and error_message:
                logging.warning(f"Задача отправки стикера '{task['content']}' пропущена: {error_message}")

        elif task["type"] == "sticker_id":
            logging.info(f"Отправка стикера по id={task['content']} в чат {chat_id}.")
            success, error_message = run_in_telegram_loop(send_sticker_by_id(chat_id, task["content"], settings=settings_to_use, progress=progress))
            if success and error_message:
                logging.warning(f"Задача отправки стикера id={task['content']} пропущена: {error_message}")

        elif task["type"] == "images":
            content = task['content']
            image_timeout = (max_send_duration(settings_to_use)
                + max(0, float(settings_to_use.get('image_choosing_delay_min', 2)), float(settings_to_use.get('image_choosing_delay_max', 5.5)))
                + (len(content['codenames']) - 1) * max(0, float(settings_to_use.get('image_additional_delay_min', .2)), float(settings_to_use.get('image_additional_delay_max', .8)))
                + 180)
            success, error_message = run_in_telegram_loop(
                send_images_with_caption(chat_id, content['codenames'], content['caption'], settings_to_use, progress),
                timeout=image_timeout)
            if success and error_message:
                logging.warning(f"Задача отправки картинки '{task['content']}' пропущена: {error_message}")
        
        elif task["type"] == "reaction":
            logging.info(f"Отправка реакции '{task['emoji']}' на сообщение {task['message_id']} в чат {chat_id}.")
            success, error_message = run_in_telegram_loop(
                send_telegram_reaction(chat_id, task["message_id"], task["emoji"])
            )
            if success and error_message:
                logging.warning(f"Задача отправки реакции '{task['emoji']}' пропущена: {error_message}")


        auto_activity.emit(progress, 'part_done' if success else 'error',
                           f'Часть {i + 1}: ' + ('отправлена' if success and not error_message else str(error_message or 'ошибка')),
                           part=i + 1, total=len(tasks_to_send), level='error' if not success else 'warning' if error_message else 'info')
        if not success:
            all_success = False
            logging.error(f"Ошибка отправки задачи {i+1} ({task['type']}) в чат {chat_id}: {error_message}")
            if first_error_message is None:
                first_error_message = error_message
            break 

        if i < len(tasks_to_send) - 1:
            delay = 0.0
            current_type = task["type"]
            next_type = tasks_to_send[i+1]["type"]

            if current_type == "reaction" and next_type == "reaction":
                delay = random.uniform(0.3, 0.8)
                logging.info(f"Короткая пауза между реакциями: {delay:.2f} сек.")
            else:
                min_pause = settings_to_use.get('base_thinking_delay_s_min', 1.0)
                max_pause = settings_to_use.get('base_thinking_delay_s_max', 2.0)
                if max_pause < min_pause: max_pause = min_pause
                delay = random.uniform(min_pause, max_pause)
                logging.info(f"Пауза перед следующей частью: {delay:.2f} сек.")
            
            if delay > 0.05:
                auto_activity.emit(progress, 'pause', 'Пауза перед следующей частью', duration_s=delay)
                if stop_event is not None:
                    if stop_event.wait(delay):
                        return False, 'Отправка отменена: авто-режим остановлен.'
                else:
                    time.sleep(delay)

    return all_success, first_error_message

def _history_anchor(history):
    for message in history:
        if message.get('outside_context'):
            continue
        if message.get('message_id'):
            return message['message_id']
        for part in message.get('parts', []):
            if 'text' in part:
                return part['text']
    return None


def _anchor_visible(history, anchor):
    if isinstance(anchor, int):
        return any(message.get('message_id') == anchor
                   for message in history if not message.get('outside_context'))
    return any(part.get('text') == anchor
               for message in history if not message.get('outside_context')
               for part in message.get('parts', []) if 'text' in part)


def _move_pending_user_to_end(history, marker=None):
    """Для follow-up после отправки делает пропущенное входящее последним ходом API."""
    result = list(history or [])
    candidates = [index for index, message in enumerate(result)
                  if message.get('role') == 'user' and not message.get('outside_context')]
    if not candidates:
        return result
    chosen = next((index for index in reversed(candidates)
                   if marker is not None and result[index].get('message_id') == marker),
                  candidates[-1])
    message = result.pop(chosen)
    result.append(message)
    return result


def generate_auto_turn(chat_id, settings, character_id, character_data, state,
                       decision, stop_event, progress, *, prompt_modifier=None,
                       response_parser=None, enable_standard_memory=True,
                       activity_chat_id=None, acknowledge=True,
                       bridge=None, get_history_fn=None, get_info_fn=None,
                       send_fn=None):
    """Общая генерация/отправка одного хода обычного и кластерного авто-режима."""
    bridge = bridge or run_in_telegram_loop
    get_history_fn = get_history_fn or get_formatted_history
    get_info_fn = get_info_fn or get_chat_info
    send_fn = send_fn or send_generated_reply
    activity_chat_id = chat_id if activity_chat_id is None else activity_chat_id
    progress('history', 'Загружаю историю и собираю промпт персонажа.')
    chat_info, _ = bridge(get_info_fn(chat_id))
    chat_info = chat_info or {}
    chat_name = chat_info.get('name', str(chat_id))
    model = settings.get('model_name') or BASE_GEMENI_MODEL
    limit = settings.get('num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
    history, error = bridge(get_history_fn(
        chat_id, limit=limit, settings=settings, acknowledge=acknowledge))
    if error or (not history and decision.trigger != 'start_dm'):
        progress('error', f'Не удалось загрузить историю: {error or "история пуста"}',
                 level='error')
        return 'retry', None

    latest = next((item for item in reversed(history) if not item.get('outside_context')),
                  history[-1] if history else None)
    latest_marker = ((latest.get('message_id') or parse_time_from_message(latest))
                     if latest else None)
    if decision.trigger == 'timeout' and latest and latest.get('role') == 'user':
        progress('idle', f'В {chat_name} появилось входящее сообщение; напоминание отменено.')
        state.force_check()
        return 'obsolete', None
    if decision.trigger == 'incoming' and (not latest or latest.get('role') != 'user'
                                           or (decision.marker is not None
                                               and latest_marker != decision.marker)):
        # Генерация ещё не началась: новое входящее должно выдержать свою полную
        # паузу. После начала запроса новые сообщения ответ уже не отменяют.
        state.force_check()
        progress('idle', f'В {chat_name} изменились сообщения; проверю их заново до генерации.')
        return 'obsolete', None

    prompt = character_utils.get_full_prompt_for_character(
        character_id, chat_name=chat_name, is_group=chat_id < 0,
        chat_context_prompt=settings.get('chat_context_prompt'),
        stickers_as_images=bool(settings.get('stickers_as_images')),
        reply_include_quote=bool(settings.get('reply_include_quote')),
        reply_include_name=bool(settings.get('reply_include_name')))
    if prompt_modifier:
        prompt, modifier_error = prompt_modifier(prompt, history, chat_info)
        if modifier_error:
            progress('error', modifier_error, level='error')
            return 'retry', None
    if decision.trigger == 'timeout':
        prompt += '\n\n' + settings.get(
            'auto_mode_no_reply_suffix', DEFAULT_CHAT_SETTINGS['auto_mode_no_reply_suffix'])

    if enable_standard_memory and settings.get('enable_auto_memory', True):
        if not state.memory_anchor:
            state.memory_anchor = _history_anchor(history)
            state.memory_snapshot = history
        elif not _anchor_visible(history, state.memory_anchor):
            memory_model = character_data.get('memory_model_name') or providers.memory_model_for(model)
            progress('memory', f'Обновляю память персонажа: {memory_model}', model=memory_model)
            _, memory_error = character_utils.update_character_memory(
                character_id=character_id, chat_name=chat_name, is_group=chat_id < 0,
                chat_history=state.memory_snapshot or history, model_name=model,
                allow_paid_overage=bool(settings.get('allow_paid_overage')))
            if memory_error:
                progress('error', f'Ошибка обновления памяти: {memory_error}', level='error')
            else:
                state.memory_anchor = _history_anchor(history)
                state.memory_snapshot = history
                progress('memory_done', 'Память персонажа обновлена.')
        else:
            state.memory_snapshot = history

    request_history = history
    if decision.trigger == 'pending_incoming':
        request_history = _move_pending_user_to_end(history, decision.marker)
    elif decision.trigger == 'start_dm':
        request_history = list(history)
        request_history.append({
            'role': 'user',
            'parts': [{'text': '[Системное событие: начни личный разговор первым прямо сейчас.]'}],
        })

    generated, generation_error, used_model = model_fallbacks.generate(
        settings, model, prompt.strip(), request_history, chat_id, progress, stop_event,
        generate_fn=generate_with_sticker_guard, config_fn=build_generation_config)
    events.publish('keys', key_pool.get_status())
    if generation_error:
        events.publish('generation', {'state': 'error', 'message': generation_error,
                                      'source': 'auto'}, chat_id=activity_chat_id)
        progress('error', f'Ошибка модели: {generation_error}. Пауза перед повтором.',
                 duration_s=20, level='error')
        stop_event.wait(20)
        return 'retry', None
    if not generated or not generated.strip():
        progress('error', 'Модель вернула пустой ответ.', level='error')
        return 'retry', None

    progress('response', 'Ответ модели получен.', text=generated, model=used_model)
    if stop_event.is_set():
        progress('stopped', 'Авто-режим остановлен до отправки полученного ответа.')
        return 'stopped', None

    text, action = response_parser(generated) if response_parser else (generated.strip(), None)
    if not text and action is None:
        progress('error', 'Модель не вернула текст для отправки.', level='error')
        return 'retry', None
    if text:
        events.publish('generation', {'state': 'sending', 'source': 'auto'},
                       chat_id=activity_chat_id)
        success, send_error = send_fn(chat_id, text, settings=settings,
                                      progress=progress, stop_event=stop_event)
        if not success:
            progress('error', f'Отправка прервана: {send_error}', level='error')
            return 'retry', None
    events.publish('generation', {'state': 'done', 'source': 'auto'},
                   chat_id=activity_chat_id)
    progress('idle', 'Обработка ответа завершена. Ожидаю сообщения собеседника.')
    return 'sent', action


def auto_mode_worker(chat_id: int, stop_event: threading.Event):
    """Обычный авто-режим на едином с кластером цикле управления чатом."""
    worker_name = f'AutoMode-{chat_id}'
    logging.info('[%s] Поток запущен.', worker_name)
    progress = lambda phase, message, **details: auto_activity.record(
        chat_id, phase, message, **details)
    state = AutoChatState(last_own_sent_at=time.monotonic())
    register_chat(chat_id, state)
    with app_state.auto_mode_lock:
        state.memory_anchor = app_state.auto_mode_workers.get(chat_id, {}).get('memory_window_anchor')

    while not stop_event.is_set():
        settings = get_chat_settings(chat_id)
        character_id = settings.get('active_character_id')
        character = character_utils.get_character(character_id) if character_id else None
        if not character:
            progress('error', 'Персонаж не выбран или не найден. Повторная проверка через 60 секунд.',
                     duration_s=60, level='error')
            stop_event.wait(60)
            continue
        try:
            with app_state.auto_mode_lock:
                status = app_state.auto_mode_workers.get(chat_id, {}).get('status', 'inactive')
            if status != 'active':
                break
            decision, error = check_auto_chat(
                chat_id, settings, state, stop_event, progress, run_in_telegram_loop,
                get_formatted_history, wait_for_chat_send_slot,
                now_fn=lambda: time.monotonic())
            if error == 'stopped':
                break
            if error:
                progress('error', f'Не удалось проверить сообщения: {error}',
                         duration_s=30, level='error')
                stop_event.wait(30)
                continue
            if not decision:
                continue
            state.begin_turn()
            try:
                outcome, _ = generate_auto_turn(
                    chat_id, settings, character_id, character, state, decision,
                    stop_event, progress)
                if outcome == 'sent':
                    if decision.trigger == 'pending_incoming':
                        state.resolve_pending(decision.pending_serial)
                    state.mark_sent(settings, now_fn=lambda: time.monotonic())
            finally:
                # Держим флаг до mark_sent: входящее в самом конце отправки
                # должно пережить назначение следующего обычного интервала.
                state.finish_turn()
            if outcome == 'stopped':
                break
            with app_state.auto_mode_lock:
                if chat_id in app_state.auto_mode_workers:
                    app_state.auto_mode_workers[chat_id]['memory_window_anchor'] = state.memory_anchor
        except Exception as exc:
            logging.exception('[%s] Неперехваченная ошибка: %s', worker_name, exc)
            progress('error', f'Ошибка авто-режима: {exc}. Повтор через 60 секунд.',
                     duration_s=60, level='error')
            stop_event.wait(60)

    unregister_chat(chat_id, state)
    logging.info('[%s] Поток завершает работу.', worker_name)
    with app_state.auto_mode_lock:
        if chat_id in app_state.auto_mode_workers \
                and app_state.auto_mode_workers[chat_id].get('status') != 'stopping':
            app_state.auto_mode_workers[chat_id]['status'] = 'inactive'
    events.publish('auto_mode', {'status': 'inactive'}, chat_id=chat_id)
    progress('stopped', 'Авто-режим выключен.')
