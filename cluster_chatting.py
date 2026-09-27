"""Один последовательный авто-режим для группы и личек её участников."""

import asyncio
from collections import deque
import hashlib
import logging
import queue
import re
import threading
import time

import app_state
import auto_activity
import character_utils
import events
import key_pool
import model_fallbacks
import providers
import settings_manager
from cluster_prompts import (CLUSTER_GROUP_COMMANDS_PROMPT, DEFAULT_DM_PROMPT,
                             DEFAULT_GROUP_PROMPT, DEFAULT_CONTEXT_PROMPT)
from gemini_utils import BASE_GEMENI_MODEL, build_generation_config, generate_chat_reply_original
from instance_paths import private_path
from shared_storage import file_lock, read_json, write_json
from sticker_guard import generate_with_sticker_guard
from auto_chat_cycle import (AutoChatDecision, AutoChatState, check_auto_chat,
                             register_chat as register_auto_chat,
                             unregister_chat as unregister_auto_chat)


MEMORY_STATE_FILE = private_path('cluster_memory.json')
COMMAND_RE = re.compile(r'(?<![\w])(?P<name>exit|ignore|ls)\s*\(\s*(?P<arg>[^)]*)\)', re.I)

_registry_lock = threading.Lock()
_registry = None

DM_OVERRIDE_KEYS = {
    'auto_mode_check_interval', 'auto_mode_initial_wait',
    'typing_delay_ms_min', 'typing_delay_ms_max', 'max_typing_duration_s',
    'base_thinking_delay_s_min', 'base_thinking_delay_s_max',
    'substitution_chance', 'transposition_chance', 'skip_chance',
    'duplication_chance', 'lower_chance', 'word_loss_chance', 'max_lost_words',
    'sticker_choosing_delay_min', 'sticker_choosing_delay_max',
    'image_choosing_delay_min', 'image_choosing_delay_max',
    'image_additional_delay_min', 'image_additional_delay_max',
}


def dm_branch_settings(settings):
    """Возвращает настройки активной лички поверх настроек основной группы."""
    result = dict(settings)
    overrides = settings.get('cluster_dm_settings_overrides') or {}
    if isinstance(overrides, dict):
        result.update({key: value for key, value in overrides.items()
                       if key in DM_OVERRIDE_KEYS})
    return result


def extract_commands(text, allowed=None):
    """Убирает служебные команды из любого места ответа, возвращает последнее действие."""
    allowed = set(allowed or ('exit', 'ignore', 'ls'))
    action = None
    def remove(match):
        nonlocal action
        name = match.group('name').casefold()
        arg = match.group('arg').strip()
        if name not in allowed:
            return ''
        if name == 'exit' and not arg:
            action = ('exit', None)
        elif name == 'ignore' and re.fullmatch(r'-?\d+', arg):
            action = ('ignore', int(arg))
        elif name == 'ls' and arg:
            action = ('ls', arg)
        return ''
    cleaned = COMMAND_RE.sub(remove, text or '')
    return cleaned.strip(), action


def _format_template(template, **values):
    # str.format не подходит для редактируемого промпта: пользователь может
    # писать собственные скобки или JSON. Подставляем только наши маркеры.
    return re.sub(r'\{([a-z_]+)\}',
                  lambda match: str(values.get(match.group(1), match.group(0))), template)


def _history_text(history):
    return character_utils.format_memory_transcript(history)


def _dm_context(dm_id, character_id):
    chat = settings_manager.load_chat_settings().get(dm_id, {})
    return (chat.get('character_specifics', {}).get(character_id, {})
            .get('chat_context_prompt', ''))


def _is_called(text, aliases):
    content = (text or '').casefold()
    return any(re.search(r'(?<!\w)@?' + re.escape(name.casefold()) + r'(?!\w)', content)
               for name in aliases if name and len(name) >= 2)


def register(group_id, inbox, aliases, own_id):
    global _registry
    with _registry_lock:
        _registry = {'group_id': group_id, 'inbox': inbox, 'aliases': aliases, 'own_id': own_id,
                     'members': {}, 'last_scan': 0}


def unregister(group_id):
    global _registry
    with _registry_lock:
        if _registry and _registry['group_id'] == group_id:
            _registry = None


def notify(event):
    """Telethon callback: classification is async; no read acknowledgement."""
    with _registry_lock:
        registry = _registry
    if registry:
        asyncio.create_task(_classify(event, registry))


async def _member(client, registry, sender_id):
    if not sender_id:
        return False
    cached = registry['members'].get(sender_id)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    try:
        await client.get_permissions(registry['group_id'], sender_id)
        result = True
    except Exception as exc:
        # Ошибка доступа не означает, что пользователь не состоит в группе.
        # Повторим проверку на следующем сообщении.
        logging.info('Кластер: не удалось проверить участника %s: %s', sender_id, exc)
        return False
    registry['members'][sender_id] = (result, time.monotonic() + 600)
    return result


async def _classify(event, registry):
    import telegram_utils
    try:
        client = telegram_utils.client
        if not client:
            return
        chat_id = event.chat_id
        message = event.message
        if chat_id == registry['group_id']:
            called = _is_called(message.message, registry['aliases'])
            reply_id = getattr(message, 'reply_to_msg_id', None)
            if reply_id:
                replied = await client.get_messages(chat_id, ids=reply_id)
                called = called or bool(replied and replied.sender_id == registry.get('own_id'))
            registry['inbox'].put(('group', chat_id, message.id, called, time.monotonic()))
        elif getattr(event, 'is_private', False) and await _member(client, registry, getattr(message, 'sender_id', None)):
            registry['inbox'].put(('dm', chat_id, message.id, False, time.monotonic()))
    except Exception:
        logging.exception('Кластер: ошибка классификации нового сообщения')


async def scan_unread(group_id, member_cache):
    """Стартовая и резервная проверка всех личных диалогов без отметки прочтения."""
    import telegram_utils
    client = telegram_utils.client
    found = []
    for dialog in await client.get_dialogs(limit=None):
        if not dialog.unread_count or not dialog.message or dialog.message.out:
            continue
        chat_id = dialog.id
        if chat_id == group_id:
            found.append(('group', chat_id, dialog.message.id, False, time.monotonic()))
        elif getattr(dialog, 'is_user', False) and await _member(client, member_cache, chat_id):
            found.append(('dm', chat_id, dialog.message.id, False, time.monotonic()))
    return found, None


class SessionQueue:
    """Порядок личек и временные игноры живут только пока работает режим."""
    def __init__(self):
        self.active = None
        self.pending = deque()
        self.ignored = {}
        self.last_incoming = {}
        self.last_sent = {}
        self.exit_action = {}

    def add(self, chat_id, when):
        expiry = self.ignored.get(chat_id)
        if expiry == -1 or (expiry and expiry > time.monotonic()):
            return False
        if expiry:
            self.ignored.pop(chat_id, None)
        self.last_incoming[chat_id] = max(when, self.last_incoming.get(chat_id, 0))
        if chat_id != self.active and chat_id not in self.pending:
            self.pending.append(chat_id)
        return True

    def next(self):
        if self.active is None and self.pending:
            self.active = self.pending.popleft()
        return self.active

    def add_priority(self, chat_id, when):
        """Ставит явную `ls()`-инициацию первой, не прерывая уже активную личку."""
        self.ignored.pop(chat_id, None)
        if not self.add(chat_id, when):
            return False
        if chat_id != self.active and chat_id in self.pending:
            self.pending.remove(chat_id)
            self.pending.appendleft(chat_id)
        return True

    def leave(self, ignore_minutes=None):
        old = self.active
        if old is not None:
            if ignore_minutes is not None:
                self.ignored[old] = -1 if ignore_minutes < 0 else time.monotonic() + 60 * ignore_minutes
            self.active = None
        return old


def _memory_state_key(character_id, group_id, dm_id):
    return f'{character_id}:{group_id}:{dm_id}'


def _mark_inactive(group_id, progress):
    with app_state.auto_mode_lock:
        worker = app_state.auto_mode_workers.get(group_id)
        if worker:
            worker['status'] = 'inactive'
    events.publish('auto_mode', {'status': 'inactive'}, chat_id=group_id)
    progress('stopped', 'Кластерный режим выключен.')


def _save_dm_memory(character_id, group_id, dm_id, dm_name, history, model_name, settings, progress):
    ids = [m.get('message_id') for m in history if m.get('message_id') and not m.get('outside_context')]
    if not ids:
        return False
    key = _memory_state_key(character_id, group_id, dm_id)
    with file_lock(MEMORY_STATE_FILE):
        state = read_json(MEMORY_STATE_FILE, {})
        previous = state.get(key, {})
    signature = hashlib.sha256(_history_text(history).encode('utf-8')).hexdigest()
    if previous.get('last_history') == signature:
        return True
    replace = previous.get('entry') if previous.get('anchor_id') in ids else None
    saved = []
    auto_activity.record(group_id, 'memory', f'Сохраняю личный разговор с {dm_name}.')
    _, error = character_utils.update_character_memory(
        character_id, dm_name, False, history, model_name,
        bool(settings.get('allow_paid_overage')), replace_entry=replace, saved_entry=saved)
    if error:
        progress('error', f'Память личного чата: {error}', level='error')
        return False
    with file_lock(MEMORY_STATE_FILE):
        state = read_json(MEMORY_STATE_FILE, {})
        state[key] = {'entry': saved[0], 'anchor_id': ids[-1], 'last_history': signature}
        write_json(MEMORY_STATE_FILE, state)
    progress('memory_done', f'Память личного чата с {dm_name} обновлена.')
    return True


def _ensure_dm_context(character_id, group_id, dm_id, group_name, dm_name, group_history, dm_history,
                       settings, stop_event, progress):
    current = _dm_context(dm_id, character_id)
    if current.strip() or stop_event.is_set():
        return current
    character = character_utils.get_character(character_id) or {}
    model = character.get('memory_model_name') or providers.memory_model_for(
        settings.get('model_name') or BASE_GEMENI_MODEL)
    prompt = _format_template(settings.get('cluster_context_prompt') or DEFAULT_CONTEXT_PROMPT,
                              group_name=group_name, dm_name=dm_name,
                              group_context=_history_text(group_history),
                              dm_context=_history_text(dm_history))
    prompt += '\n\nЛичность персонажа:\n' + character.get('personality_prompt', '')
    prompt += '\n\nПамять персонажа:\n' + character.get('memory_prompt', '')
    config = {'provider': 'openai', 'allow_paid_overage': bool(settings.get('allow_paid_overage'))} \
        if providers.provider_for_model(model) == 'openai' else None
    progress('context', f'Создаю контекст личного чата: {model}', model=model)
    text, error = generate_chat_reply_original(model_name=model, system_prompt=prompt,
        chat_history=[{'role': 'user', 'parts': [{'text': 'Составь контекст для этой личной переписки.'}]}],
        config=config)
    if error or not text or stop_event.is_set():
        progress('error', f'Не удалось создать контекст лички: {error or "пустой ответ"}', level='error')
        return ''
    with file_lock(settings_manager.CHAT_SETTINGS_FILE):
        all_settings = settings_manager.load_chat_settings()
        specific = all_settings.setdefault(dm_id, {}).setdefault('character_specifics', {}).setdefault(character_id, {})
        if not specific.get('chat_context_prompt', '').strip():
            specific['chat_context_prompt'] = text.strip()
            settings_manager.save_chat_settings(all_settings)
        return specific['chat_context_prompt']


def cluster_mode_worker(group_id, stop_event):
    """Меняет активный чат, а каждый чат ведёт единый цикл авто-режима."""
    from bot_logic import send_generated_reply
    from telegram_utils import (get_chat_info, get_formatted_history, run_in_telegram_loop,
                                wait_for_chat_send_slot)
    inbox = queue.Queue()
    sessions = SessionQueue()
    seen = {}
    states = {group_id: AutoChatState(last_own_sent_at=time.monotonic())}
    start_requests = {}
    group_requested = False
    progress = lambda phase, message, **details: auto_activity.record(
        group_id, phase, message, **details)
    group_settings = settings_manager.get_chat_settings(group_id)
    character_id = group_settings.get('active_character_id')
    character = character_utils.get_character(character_id) if character_id else None
    if not character:
        progress('error', 'Для кластерного режима выберите персонажа группы.', level='error')
        _mark_inactive(group_id, progress)
        return
    info, error = run_in_telegram_loop(get_chat_info(group_id))
    if error or not info:
        progress('error', f'Не удалось открыть группу: {error}', level='error')
        _mark_inactive(group_id, progress)
        return
    group_name = info['name']
    me, me_error = run_in_telegram_loop(_get_me())
    if me_error or not me:
        progress('error', f'Не удалось определить Telegram-аккаунт: {me_error}', level='error')
        _mark_inactive(group_id, progress)
        return
    aliases = [character.get('name', ''), getattr(me, 'username', ''), getattr(me, 'first_name', '')]
    register(group_id, inbox, aliases, getattr(me, 'id', None))
    register_auto_chat(group_id, states[group_id])

    def state_for(chat_id):
        state = states.get(chat_id)
        if state is None:
            state = AutoChatState(last_own_sent_at=time.monotonic())
            states[chat_id] = state
            register_auto_chat(chat_id, state)
        return state

    visible_chat = None

    def show_active_chat(chat_id):
        nonlocal visible_chat
        if chat_id == visible_chat:
            return
        visible_chat = chat_id
        events.publish('cluster_active_chat', {
            'root_chat_id': group_id,
            'active_chat_id': chat_id,
        })

    show_active_chat(group_id)
    next_scan = 0.0
    try:
        while not stop_event.is_set():
            settings = settings_manager.get_chat_settings(group_id)
            if settings.get('active_character_id') != character_id:
                progress('error', 'Персонаж группы изменился. Перезапустите кластерный режим.',
                         level='error')
                break

            if time.monotonic() >= next_scan:
                with _registry_lock:
                    registry = _registry
                if registry:
                    found, scan_error = run_in_telegram_loop(
                        scan_unread(group_id, registry), timeout=120)
                    if scan_error:
                        progress('error', f'Не удалось проверить непрочитанные чаты: {scan_error}',
                                 level='error')
                    else:
                        for item in found or []:
                            inbox.put(item)
                next_scan = time.monotonic() + 30

            pending_events = []
            try:
                pending_events.append(inbox.get(timeout=0.5))
                while True:
                    pending_events.append(inbox.get_nowait())
            except queue.Empty:
                pass
            for kind, chat_id, message_id, called, when in pending_events:
                if message_id <= seen.get(chat_id, 0):
                    continue
                seen[chat_id] = message_id
                if kind == 'dm':
                    # Если адресат `ls()` успел написать сам, отвечаем на его
                    # реальное сообщение вместо искусственного начала диалога.
                    start_requests.pop(chat_id, None)
                    if sessions.add(chat_id, when):
                        state_for(chat_id).force_check()
                        chosen = sessions.next()
                        if chosen == chat_id:
                            progress('cluster_queue', f'Личка {chat_id}: активная саб-сессия.')
                        else:
                            place = list(sessions.pending).index(chat_id) + 1
                            progress('cluster_queue', f'Личка {chat_id}: pending, место {place}.')
                elif kind == 'group' and sessions.active is not None and called:
                    # Событие выбирает группу для следующего хода, но не запускает
                    # генерацию. Решение принимает обычная проверка истории ниже.
                    group_requested = True
                    states[group_id].force_check()

            active = sessions.next()
            if active is not None and active in sessions.exit_action:
                action = sessions.exit_action[active]
                if _leave_dm(active, sessions, character_id, group_id, settings, progress,
                             get_formatted_history, get_chat_info, run_in_telegram_loop,
                             ignore_minutes=action[1] if action[0] == 'ignore' else None):
                    sessions.exit_action.pop(active, None)
                    following = sessions.next()
                    if following is not None:
                        state_for(following).force_check()
                        progress('cluster_queue',
                                 f'Личка {following}: следующая активная саб-сессия.')
                else:
                    stop_event.wait(30)
                continue

            if active is not None and group_requested:
                selected_chat = group_id
                selected_state = states[group_id]
                selected_settings = settings
                allow_timeout = False
                active_dm_context = active
            elif active is not None:
                selected_chat = active
                selected_state = state_for(active)
                selected_settings = dm_branch_settings(settings)
                allow_timeout = False
                active_dm_context = None
            else:
                selected_chat = group_id
                selected_state = states[group_id]
                selected_settings = settings
                allow_timeout = True
                active_dm_context = None

            show_active_chat(selected_chat)

            forced_trigger = ('start_dm' if selected_chat != group_id
                              and selected_chat in start_requests else None)
            decision, check_error = check_auto_chat(
                selected_chat, selected_settings, selected_state, stop_event, progress,
                run_in_telegram_loop, get_formatted_history, wait_for_chat_send_slot,
                allow_timeout=allow_timeout, acknowledge=True, block_until_due=False,
                forced_trigger=forced_trigger,
                now_fn=lambda: time.monotonic())
            if check_error == 'stopped':
                break
            if check_error:
                progress('error', f'Не удалось проверить чат {selected_chat}: {check_error}',
                         level='error')
                selected_state.next_check_at = time.monotonic() + 30
                continue

            if decision:
                selected_state.begin_turn()
                try:
                    outcome, action = _reply(
                        selected_chat, group_id, group_name, character_id, selected_settings,
                        stop_event, progress, send_generated_reply, get_formatted_history,
                        get_chat_info, wait_for_chat_send_slot, run_in_telegram_loop,
                        active_dm=active_dm_context, state=selected_state, decision=decision,
                        character=character)
                    if outcome == 'sent':
                        if decision.trigger == 'pending_incoming':
                            selected_state.resolve_pending(decision.pending_serial)
                        elif decision.trigger == 'start_dm':
                            start_requests.pop(selected_chat, None)
                        selected_state.mark_sent(selected_settings, now_fn=lambda: time.monotonic())
                        if selected_chat != group_id:
                            sessions.last_sent[selected_chat] = time.monotonic()
                            if action:
                                sessions.exit_action[selected_chat] = action
                        else:
                            if action and action[0] == 'ls':
                                member, member_error = run_in_telegram_loop(
                                    resolve_group_member(group_id, action[1], getattr(me, 'id', None)))
                                if member_error or not member:
                                    progress('error', member_error or 'Не удалось определить участника для ls().',
                                             level='error')
                                else:
                                    dm_id = member['id']
                                    sessions.add_priority(dm_id, time.monotonic())
                                    start_requests[dm_id] = member
                                    state_for(dm_id).force_check()
                                    progress('cluster_queue',
                                             f'ls(): начинаю личный разговор с {member["name"]}.')
                            if group_requested and not selected_state.pending_incoming():
                                group_requested = False
                    elif decision.trigger == 'start_dm':
                        # Ошибка генерации не должна добавлять к 20-секундной паузе
                        # ещё и обычный интервал проверки перед инициативной личкой.
                        selected_state.force_check()
                    elif outcome == 'obsolete' and selected_chat == group_id and group_requested:
                        group_requested = False
                finally:
                    selected_state.finish_turn()
                if outcome == 'stopped':
                    break
                continue

            # В активной личке таймаут означает завершение саб-сессии, а не
            # инициативное сообщение. Перед выходом общий цикл уже проверил
            # реальную историю, поэтому новое входящее здесь не потеряется.
            if active is not None and selected_chat == active:
                idle_minutes = max(0.1, float(settings.get('cluster_dm_idle_minutes', 4)))
                last_sent = sessions.last_sent.get(active)
                if selected_state.latest_role == 'model' and last_sent is not None \
                        and time.monotonic() - last_sent >= idle_minutes * 60:
                    if _leave_dm(active, sessions, character_id, group_id, settings, progress,
                                 get_formatted_history, get_chat_info, run_in_telegram_loop):
                        following = sessions.next()
                        if following is not None:
                            state_for(following).force_check()
                            progress('cluster_queue',
                                     f'Личка {following}: следующая активная саб-сессия.')
                    else:
                        sessions.last_sent[active] = time.monotonic() - idle_minutes * 60 + 30
    except Exception:
        logging.exception('Ошибка кластерного режима группы %s', group_id)
        progress('error', 'Кластерный режим прерван из-за ошибки.', level='error')
    finally:
        unregister(group_id)
        for tracked_chat_id, tracked_state in list(states.items()):
            unregister_auto_chat(tracked_chat_id, tracked_state)
        if sessions.active is not None:
            try:
                _leave_dm(sessions.active, sessions, character_id, group_id, group_settings,
                          progress, get_formatted_history, get_chat_info, run_in_telegram_loop)
            except Exception:
                logging.exception('Не удалось записать личку при остановке кластера')
        show_active_chat(group_id)
        _mark_inactive(group_id, progress)


async def _get_me():
    import telegram_utils
    return await telegram_utils.client.get_me(), None


def _member_labels(user):
    username = (getattr(user, 'username', '') or '').strip()
    first = (getattr(user, 'first_name', '') or '').strip()
    last = (getattr(user, 'last_name', '') or '').strip()
    full = ' '.join(part for part in (first, last) if part)
    display = full or (('@' + username) if username else str(getattr(user, 'id', '')))
    aliases = {value.casefold() for value in (username, '@' + username if username else '',
                                               first, last, full) if value}
    return display, aliases


async def resolve_group_member(group_id, query, own_id=None):
    """Ищет участника группы для `ls(name)` и возвращает его личный chat id."""
    import telegram_utils
    wanted = ' '.join(str(query or '').strip().split())
    if not wanted:
        return None, 'В команде ls() не указано имя пользователя.'
    normalized = wanted.casefold()
    try:
        participants = await telegram_utils.client.get_participants(
            group_id, search=wanted.lstrip('@'), limit=100)
    except Exception as exc:
        return None, f'Не удалось найти участника «{wanted}» в группе: {exc}'

    candidates = [user for user in participants
                  if getattr(user, 'id', None) is not None
                  and getattr(user, 'id', None) != own_id
                  and not getattr(user, 'deleted', False)]
    exact = []
    for user in candidates:
        display, aliases = _member_labels(user)
        if normalized in aliases:
            exact.append((user, display))
    matches = exact or ([(candidates[0], _member_labels(candidates[0])[0])]
                        if len(candidates) == 1 else [])
    if not matches:
        if candidates:
            names = ', '.join(_member_labels(user)[0] for user in candidates[:5])
            return None, (f'Имя «{wanted}» неоднозначно. Уточните полное имя или @username. '
                          f'Найдены: {names}.')
        return None, f'Участник «{wanted}» в этой группе не найден.'
    if len(matches) > 1:
        names = ', '.join(display for _, display in matches[:5])
        return None, (f'Имя «{wanted}» неоднозначно. Уточните @username. '
                      f'Совпадения: {names}.')
    user, display = matches[0]
    return {'id': int(user.id), 'name': display}, None


def _leave_dm(dm_id, sessions, character_id, group_id, settings, progress,
              get_formatted_history, get_chat_info, run_in_telegram_loop, ignore_minutes=None):
    if settings.get('cluster_no_commitment'):
        sessions.leave(ignore_minutes)
        progress('cluster_queue', f'Вернулся в группу после разговора в личке {dm_id}; память отключена.')
        return True
    info, _ = run_in_telegram_loop(get_chat_info(dm_id))
    dm_name = (info or {}).get('name', str(dm_id))
    history, error = run_in_telegram_loop(get_formatted_history(
        dm_id, limit=settings.get('num_messages_to_fetch', 65), settings=settings, acknowledge=False))
    if not error and history:
        saved = _save_dm_memory(character_id, group_id, dm_id, dm_name, history,
                                settings.get('model_name') or BASE_GEMENI_MODEL, settings, progress)
    else:
        progress('error', f'Не удалось сохранить личку {dm_name}: {error or "история пуста"}', level='error')
        saved = False
    if not saved:
        return False
    sessions.leave(ignore_minutes)
    progress('cluster_queue', f'Вернулся в группу после разговора с {dm_name}.')
    return True


def _reply(chat_id, group_id, group_name, character_id, settings, stop_event, progress,
           send_generated_reply, get_formatted_history, get_chat_info,
           wait_for_chat_send_slot, run_in_telegram_loop, active_dm=None,
           timeout_trigger=False, state=None, decision=None, character=None):
    """Добавляет кластерный контекст к общей генерации авто-режима."""
    from bot_logic import generate_auto_turn

    is_dm = chat_id != group_id
    target_settings = dict(settings)
    if is_dm:
        target_settings['chat_context_prompt'] = _dm_context(chat_id, character_id)
    character = character or character_utils.get_character(character_id) or {}
    state = state or AutoChatState(last_own_sent_at=time.monotonic())

    # Совместимость с прямыми вызовами и офлайн-тестами: worker всегда уже
    # приносит решение из check_auto_chat, поэтому второй цикл тут не создаётся.
    if decision is None:
        slot, slot_error = run_in_telegram_loop(wait_for_chat_send_slot(
            chat_id, stop_event=stop_event, progress=progress, force_refresh=True))
        if slot_error or not slot or not slot.get('ready') or stop_event.is_set():
            return 'retry', None
        if slot.get('waited'):
            state.force_check()
            return 'retry', None
        decision = AutoChatDecision('timeout' if timeout_trigger else 'incoming')

    def add_cluster_context(prompt, history, chat_info):
        chat_name = (chat_info or {}).get('name', str(chat_id))
        if is_dm:
            group_history, group_error = run_in_telegram_loop(get_formatted_history(
                group_id, limit=settings.get('num_messages_to_fetch', 65),
                settings=settings, acknowledge=False))
            if group_error:
                return prompt, f'Не удалось загрузить контекст группы: {group_error}'
            context = _ensure_dm_context(
                character_id, group_id, chat_id, group_name, chat_name,
                group_history, history, settings, stop_event, progress)
            if context and context not in prompt:
                prompt += '\n\nКонтекст именно этого личного чата:\n' + context
            prompt += '\n\n' + _format_template(
                settings.get('cluster_dm_prompt') or DEFAULT_DM_PROMPT,
                group_name=group_name, dm_name=chat_name,
                group_context=_history_text(group_history),
                dm_context=_history_text(history))
        else:
            prompt += '\n\n' + CLUSTER_GROUP_COMMANDS_PROMPT
            if active_dm is not None:
                dm_info, _ = run_in_telegram_loop(get_chat_info(active_dm))
                dm_name = (dm_info or {}).get('name', str(active_dm))
                dm_history, dm_error = run_in_telegram_loop(get_formatted_history(
                    active_dm, limit=settings.get('num_messages_to_fetch', 65),
                    settings=settings, acknowledge=False))
                if dm_error:
                    return prompt, f'Не удалось загрузить контекст лички: {dm_error}'
                prompt += '\n\n' + _format_template(
                    settings.get('cluster_group_prompt') or DEFAULT_GROUP_PROMPT,
                    group_name=group_name, dm_name=dm_name,
                    dm_context=_history_text(dm_history or []),
                    group_context=_history_text(history))
        return prompt, None

    response_parser = ((lambda text: extract_commands(text, ('exit', 'ignore')))
                       if is_dm else
                       (lambda text: extract_commands(text, ('ls',))))

    return generate_auto_turn(
        chat_id, target_settings, character_id, character, state, decision,
        stop_event, progress, prompt_modifier=add_cluster_context,
        response_parser=response_parser,
        enable_standard_memory=not is_dm, activity_chat_id=group_id,
        bridge=run_in_telegram_loop, get_history_fn=get_formatted_history,
        get_info_fn=get_chat_info, send_fn=send_generated_reply)
