"""Единый цикл проверки чата для обычного и кластерного авто-режима."""

from dataclasses import dataclass, field
import threading
import time as _time

from settings_manager import DEFAULT_CHAT_SETTINGS
from text_utils import parse_time_from_message


_registry_lock = threading.Lock()
_registered_states = {}


def register_chat(chat_id, state):
    with _registry_lock:
        _registered_states[chat_id] = state


def unregister_chat(chat_id, state):
    with _registry_lock:
        if _registered_states.get(chat_id) is state:
            _registered_states.pop(chat_id, None)


def notify_chat(chat_id, *, incoming=False, message_id=None):
    """Будит проверку и отмечает входящее, пришедшее во время текущего хода."""
    with _registry_lock:
        state = _registered_states.get(chat_id)
    if state is not None:
        if incoming:
            state.note_incoming(message_id)
        else:
            state.force_check()
        return True
    return False


@dataclass
class AutoChatState:
    """Состояние одного чата, которым в данный момент управляет авто-режим."""

    last_processed_marker: object = None
    last_own_sent_at: float = field(default_factory=_time.monotonic)
    next_check_at: float = 0.0
    cooldown_checked: bool = False
    latest_role: str | None = None
    memory_anchor: object = None
    memory_snapshot: list | None = None
    turn_in_progress: bool = False
    incoming_serial: int = 0
    pending_incoming_serial: int = 0
    pending_incoming_marker: object = None
    wake_event: threading.Event = field(default_factory=threading.Event, repr=False)
    state_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def force_check(self):
        self.next_check_at = 0.0
        self.wake_event.set()

    def begin_turn(self):
        with self.state_lock:
            self.turn_in_progress = True

    def finish_turn(self):
        with self.state_lock:
            self.turn_in_progress = False
            pending = bool(self.pending_incoming_serial)
        if pending:
            self.force_check()

    def note_incoming(self, message_id=None):
        """Запоминает входящее, если модель уже генерирует или отправляет ход."""
        with self.state_lock:
            self.incoming_serial += 1
            if self.turn_in_progress or self.pending_incoming_serial:
                self.pending_incoming_serial = self.incoming_serial
                if message_id is not None:
                    self.pending_incoming_marker = message_id
        self.force_check()

    def pending_incoming(self):
        with self.state_lock:
            if not self.pending_incoming_serial:
                return None
            return self.pending_incoming_serial, self.pending_incoming_marker

    def resolve_pending(self, serial):
        """Снимает только уже обработанный флаг, не стирая более новое входящее."""
        with self.state_lock:
            if self.pending_incoming_serial and self.pending_incoming_serial <= serial:
                self.pending_incoming_serial = 0
                self.pending_incoming_marker = None

    def mark_sent(self, settings, now_fn=_time.monotonic):
        now = now_fn()
        self.last_own_sent_at = now
        interval = settings.get(
            'auto_mode_check_interval', DEFAULT_CHAT_SETTINGS['auto_mode_check_interval'])
        self.next_check_at = (0.0 if self.pending_incoming()
                              else now + max(0.5, float(interval)))
        self.latest_role = 'model'


@dataclass(frozen=True)
class AutoChatDecision:
    trigger: str  # incoming | pending_incoming | timeout | start_dm
    marker: object = None
    pending_serial: int = 0


def _latest_regular(history):
    return next((item for item in reversed(history) if not item.get('outside_context')),
                history[-1] if history else None)


def _marker(message):
    if not message:
        return None
    return message.get('message_id') or parse_time_from_message(message)


def check_auto_chat(chat_id, settings, state, stop_event, progress, bridge,
                    get_formatted_history, wait_for_chat_send_slot, *,
                    allow_timeout=True, acknowledge=True, block_until_due=True,
                    forced_trigger=None,
                    now_fn=_time.monotonic):
    """Проверяет чат по правилам авто-режима и возвращает решение о генерации.

    Функция едина для обычного авто-режима, основной группы кластера и активной
    личной саб-сессии. Telegram-события могут только выбрать чат и ускорить его
    ближайшую проверку; решение всегда принимается по фактической истории.
    """
    interval = max(0.5, float(settings.get(
        'auto_mode_check_interval', DEFAULT_CHAT_SETTINGS['auto_mode_check_interval'])))

    slot, error = bridge(wait_for_chat_send_slot(
        chat_id, stop_event=stop_event, progress=progress,
        force_refresh=not state.cooldown_checked))
    if stop_event.is_set():
        return None, 'stopped'
    if error or not slot or not slot.get('ready'):
        return None, error or 'Не удалось узнать остаток кулдауна чата.'
    state.cooldown_checked = True
    if slot.get('waited'):
        return None, None

    state.wake_event.clear()
    wait_s = state.next_check_at - now_fn()
    if wait_s > 0:
        if block_until_due:
            progress('idle', 'Ожидаю следующую проверку чата по заданному интервалу.',
                     duration_s=wait_s)
            if isinstance(stop_event, threading.Event):
                state.wake_event.wait(wait_s)
            else:
                # Детерминированные офлайн-тесты используют собственные часы.
                stop_event.wait(wait_s)
        return None, None

    # Интервал и Telegram slow mode идут параллельно: срок следующей проверки
    # назначается до чтения истории и не добавляется поверх кулдауна.
    state.next_check_at = now_fn() + interval
    history, error = bridge(get_formatted_history(
        chat_id, limit=2, settings=settings, acknowledge=acknowledge))
    if error:
        return None, error
    if not history and not forced_trigger:
        state.latest_role = None
        return None, None

    latest = _latest_regular(history)
    marker = _marker(latest)
    state.latest_role = latest.get('role') if latest else None

    if forced_trigger:
        return AutoChatDecision(forced_trigger, marker), None

    pending = state.pending_incoming()
    if pending:
        pending_serial, pending_marker = pending
        pause = max(0.0, float(settings.get(
            'auto_mode_initial_wait', DEFAULT_CHAT_SETTINGS['auto_mode_initial_wait'])))
        progress('debounce',
                 'Во время прошлого ответа пришло сообщение. Жду, закончит ли собеседник писать.',
                 duration_s=pause)
        if stop_event.wait(pause):
            return None, 'stopped'

        slot, error = bridge(wait_for_chat_send_slot(
            chat_id, stop_event=stop_event, progress=progress, force_refresh=False))
        if stop_event.is_set():
            return None, 'stopped'
        if error or not slot or not slot.get('ready'):
            return None, error or 'Не удалось узнать остаток кулдауна чата.'
        if slot.get('waited'):
            state.force_check()
            return None, None

        after, error = bridge(get_formatted_history(
            chat_id, limit=2, settings=settings, acknowledge=acknowledge))
        if error:
            return None, error
        current_pending = state.pending_incoming()
        if current_pending and current_pending[0] != pending_serial:
            state.force_check()
            progress('idle', 'Пришло ещё сообщение. Пауза перед ответом начнётся заново.')
            return None, None
        latest_after = _latest_regular(after or [])
        state.latest_role = latest_after.get('role') if latest_after else None
        return AutoChatDecision('pending_incoming', pending_marker,
                                pending_serial=pending_serial), None

    if state.latest_role == 'user' and marker is not None \
            and marker != state.last_processed_marker:
        previous_marker = state.last_processed_marker
        state.last_processed_marker = marker
        pause = max(0.0, float(settings.get(
            'auto_mode_initial_wait', DEFAULT_CHAT_SETTINGS['auto_mode_initial_wait'])))
        progress('debounce', 'Новое сообщение. Жду, закончит ли собеседник писать.',
                 duration_s=pause)
        if stop_event.wait(pause):
            return None, 'stopped'

        slot, error = bridge(wait_for_chat_send_slot(
            chat_id, stop_event=stop_event, progress=progress, force_refresh=False))
        if stop_event.is_set():
            return None, 'stopped'
        if error or not slot or not slot.get('ready'):
            state.last_processed_marker = previous_marker
            return None, error or 'Не удалось узнать остаток кулдауна чата.'
        if slot.get('waited'):
            state.last_processed_marker = previous_marker
            return None, None

        after, error = bridge(get_formatted_history(
            chat_id, limit=2, settings=settings, acknowledge=acknowledge))
        if error or not after:
            state.last_processed_marker = previous_marker
            return None, error or 'История чата пуста при повторной проверке.'
        latest_after = _latest_regular(after)
        marker_after = _marker(latest_after)
        state.latest_role = latest_after.get('role')
        if state.latest_role == 'user' and marker_after == marker:
            return AutoChatDecision('incoming', marker), None

        # Во время паузы пришло новое сообщение или последнее сообщение уже
        # стало исходящим. Новое входящее должно получить полную паузу заново.
        state.last_processed_marker = previous_marker
        state.force_check()
        progress('idle', 'Пришло ещё сообщение. Пауза перед ответом начнётся заново.')
        return None, None

    if allow_timeout and state.latest_role != 'user':
        timeout_min = max(0.0, float(settings.get(
            'auto_mode_no_reply_timeout', DEFAULT_CHAT_SETTINGS['auto_mode_no_reply_timeout'])))
        if now_fn() - state.last_own_sent_at > timeout_min * 60:
            # Срок сдвигается уже при попытке, чтобы ошибка модели не породила
            # бесконечные запросы в каждом обороте worker-а.
            state.last_own_sent_at = now_fn()
            return AutoChatDecision('timeout', marker), None

    return None, None
