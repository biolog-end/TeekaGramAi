"""Проверки журнала и настоящих задержек с подставным клиентом, без Telegram/API."""
import asyncio
import json
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from telethon.tl.types import InputPeerUser

import app_state
import auto_activity
import auto_chat_cycle
import bot_logic
import events
import image_store
import main
import sticker_store
import telegram_utils
import character_lexicon
from reply_protocol import extract_reply


class CharacterLexiconTests(unittest.TestCase):
    def test_order_literal_matching_and_no_recursive_replacement(self):
        apply = character_lexicon.apply_character_lexicon
        self.assertEqual(apply('привет привет', [
            {'find': 'привет', 'replace': 'привета'},
            {'find': 'привета', 'replace': 'привет'}]), 'привет привет')
        self.assertEqual(apply('aaa', [{'find': 'a', 'replace': 'aa'}]), 'aaaaaa')
        self.assertEqual(apply('a.a\na+a — Привет привет', [
            {'find': 'a.a\na+a', 'replace': r'\1[.]'},
            {'find': '—', 'replace': ':'},
            {'find': 'Привет', 'replace': ''}]), '\\1[.] :  привет')

    def test_chance_is_independent_for_each_occurrence(self):
        draw = MagicMock(side_effect=[.1, .9, .49, .5])
        result = character_lexicon.apply_character_lexicon('Привет Привет Привет Привет', [
            {'find': 'Привет', 'replace': 'Привета', 'chance': .5}], random_draw=draw)
        self.assertEqual(result, 'Привета Привет Привета Привет')
        self.assertEqual(draw.call_count, 4)
        draw.reset_mock()
        self.assertEqual(character_lexicon.apply_character_lexicon('a a', [
            {'find': 'a', 'replace': 'b', 'chance': 0},
            {'find': 'a', 'replace': 'c', 'chance': 1}], random_draw=draw), 'c c')
        draw.assert_not_called()

    def test_invalid_rules_and_independent_parsed_data(self):
        source = [{'find': ' x ', 'replace': '', 'chance': '0.5'}]
        parsed = character_lexicon.parse_lexicon_rules(json.dumps(source))
        self.assertEqual(parsed, [{'find': ' x ', 'replace': '', 'chance': .5}])
        parsed[0]['find'] = 'changed'
        self.assertEqual(source[0]['find'], ' x ')
        for bad in ({}, None, '[', [{'find': ''}], [{'find': 'x', 'replace': 1}],
                    *[[{'find': 'x', 'chance': value}] for value in (-1, 1.1, 'NaN', True)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                character_lexicon.parse_lexicon_rules(bad)

    def test_lexicon_precedes_typos_and_survives_humanizer_failure(self):
        settings = {'lexicon_rules': [{'find': 'привет ', 'replace': 'привета '}]}
        with patch.object(telegram_utils, 'final_fine_tune_sms',
                          return_value=('привта -', 'привта - приветствие')) as tune:
            self.assertEqual(telegram_utils.humanize_outgoing_text('привет — приветствие', settings),
                             ('привта -', 'привта - приветствие'))
            self.assertEqual(tune.call_args.args[0], 'привета - приветствие')
        with patch.object(telegram_utils, 'final_fine_tune_sms', side_effect=RuntimeError('broken')), \
             self.assertLogs(level='ERROR'):
            self.assertEqual(telegram_utils.humanize_outgoing_text('привет —', settings),
                             ('привета -', 'привета -'))


class ActivityTests(unittest.TestCase):
    def test_new_message_wakes_interval_wait_without_starting_generation(self):
        state = auto_chat_cycle.AutoChatState(next_check_at=999)
        auto_chat_cycle.register_chat(-900, state)
        try:
            self.assertTrue(auto_chat_cycle.notify_chat(-900))
            self.assertEqual(state.next_check_at, 0)
            self.assertTrue(state.wake_event.is_set())
        finally:
            auto_chat_cycle.unregister_chat(-900, state)
        self.assertFalse(auto_chat_cycle.notify_chat(-900))

    def test_incoming_during_turn_survives_own_send_and_forces_followup(self):
        state = auto_chat_cycle.AutoChatState(next_check_at=999)
        auto_chat_cycle.register_chat(-900, state)
        try:
            state.begin_turn()
            self.assertTrue(auto_chat_cycle.notify_chat(
                -900, incoming=True, message_id=77))
            state.finish_turn()
            state.mark_sent({'auto_mode_check_interval': 60}, now_fn=lambda: 10)
            self.assertEqual(state.next_check_at, 0)
            self.assertEqual(state.pending_incoming(), (1, 77))
        finally:
            auto_chat_cycle.unregister_chat(-900, state)

    def test_pending_incoming_debounces_even_when_own_message_is_latest(self):
        state = auto_chat_cycle.AutoChatState()
        state.begin_turn()
        state.note_incoming(77)
        state.finish_turn()
        waits = []
        history = [
            {'role': 'user', 'message_id': 77, 'parts': [{'text': 'важное'}]},
            {'role': 'model', 'message_id': 79, 'parts': [{'text': 'последняя часть'}]},
        ]

        class Stop:
            def is_set(self): return False
            def wait(self, seconds):
                waits.append(seconds)
                return False

        async def wait_for_chat_send_slot(chat_id, stop_event, progress, force_refresh):
            return None

        async def get_formatted_history(chat_id, limit, settings, acknowledge=True):
            return None

        def bridge(coro, timeout=60):
            name = coro.__name__
            coro.close()
            if name == 'wait_for_chat_send_slot':
                return {'ready': True, 'waited': False}, None
            return history, None

        decision, error = auto_chat_cycle.check_auto_chat(
            -900, {'auto_mode_check_interval': 60, 'auto_mode_initial_wait': 7},
            state, Stop(), lambda *args, **kwargs: None, bridge,
            get_formatted_history, wait_for_chat_send_slot,
            block_until_due=False, now_fn=lambda: 10)

        self.assertIsNone(error)
        self.assertEqual(decision, auto_chat_cycle.AutoChatDecision(
            'pending_incoming', 77, pending_serial=1))
        self.assertEqual(waits, [7])

    def test_followup_request_moves_missed_user_turn_after_own_split_messages(self):
        history = [
            {'role': 'user', 'message_id': 77, 'parts': [{'text': 'важное сообщение'}]},
            {'role': 'model', 'message_id': 78, 'parts': [{'text': 'часть 1'}]},
            {'role': 'model', 'message_id': 79, 'parts': [{'text': 'часть 2'}]},
        ]
        captured = []
        async def get_info(chat_id): return None
        async def get_history(chat_id, limit, settings, acknowledge=True): return None
        def bridge(coro, timeout=60):
            name = coro.__name__
            coro.close()
            return ({'name': 'Тест'}, None) if name == 'get_info' else (history, None)
        with patch.object(bot_logic.character_utils, 'get_full_prompt_for_character', return_value='BASE'), \
             patch.object(bot_logic.model_fallbacks, 'generate',
                          side_effect=lambda settings, model, prompt, request_history, *args, **kwargs:
                          (captured.extend(request_history) or 'ответ', None, model)), \
             patch.object(bot_logic.key_pool, 'get_status', return_value={}):
            outcome, _ = bot_logic.generate_auto_turn(
                -900, {'model_name': 'gemini-test'}, 'c', {},
                auto_chat_cycle.AutoChatState(),
                auto_chat_cycle.AutoChatDecision('pending_incoming', 77, pending_serial=1),
                threading.Event(), lambda *args, **kwargs: None,
                bridge=bridge, get_history_fn=get_history, get_info_fn=get_info,
                send_fn=lambda *args, **kwargs: (True, None))
        self.assertEqual(outcome, 'sent')
        self.assertEqual([item['message_id'] for item in captured], [78, 79, 77])
        self.assertEqual(captured[-1]['role'], 'user')

    def test_start_dm_allows_empty_history_and_adds_explicit_user_trigger(self):
        captured = []
        async def get_info(chat_id): return None
        async def get_history(chat_id, limit, settings, acknowledge=True): return None
        def bridge(coro, timeout=60):
            name = coro.__name__
            coro.close()
            return ({'name': 'Иван'}, None) if name == 'get_info' else ([], None)
        with patch.object(bot_logic.character_utils, 'get_full_prompt_for_character', return_value='BASE'), \
             patch.object(bot_logic.model_fallbacks, 'generate',
                          side_effect=lambda settings, model, prompt, request_history, *args, **kwargs:
                          (captured.extend(request_history) or 'привет', None, model)), \
             patch.object(bot_logic.key_pool, 'get_status', return_value={}):
            outcome, _ = bot_logic.generate_auto_turn(
                11, {'model_name': 'gemini-test'}, 'c', {},
                auto_chat_cycle.AutoChatState(), auto_chat_cycle.AutoChatDecision('start_dm'),
                threading.Event(), lambda *args, **kwargs: None,
                bridge=bridge, get_history_fn=get_history, get_info_fn=get_info,
                send_fn=lambda *args, **kwargs: (True, None), enable_standard_memory=False)
        self.assertEqual(outcome, 'sent')
        self.assertEqual(captured[-1]['role'], 'user')
        self.assertIn('начни личный разговор', captured[-1]['parts'][0]['text'])

    def setUp(self):
        auto_activity.reset(-900)

    def test_snapshot_bounds_response_and_restart(self):
        with patch.object(events, 'publish'):
            auto_activity.record(-900, 'response', 'Ответ', text='<script>ответ</script>', model='gpt-5-mini')
            for n in range(100):
                auto_activity.record(-900, 'idle', str(n))
        state = auto_activity.snapshot(-900)
        self.assertEqual(len(state['entries']), 80)
        self.assertEqual(state['response']['text'], '<script>ответ</script>')
        self.assertEqual(state['current']['message'], '99')
        auto_activity.reset(-900)
        fresh = auto_activity.snapshot(-900)
        self.assertGreater(fresh['run_id'], state['run_id'])
        self.assertEqual(fresh['entries'], [])
        self.assertIsNone(fresh['response'])

    def test_deadline_and_part_survive_thread_bridge(self):
        with patch.object(events, 'publish'), patch.object(auto_activity.time, 'time', return_value=100):
            auto_activity.record(-900, 'part', 'Часть', part=2, total=3, kind='text', text='привет')
            thread = threading.Thread(target=lambda: auto_activity.record(-900, 'typing', 'Печатаю', duration_s=7.5))
            thread.start(); thread.join()
        state = auto_activity.snapshot(-900)
        self.assertEqual(state['current']['ends_at'], 107.5)
        self.assertEqual(state['current']['part'], 2)
        self.assertEqual(state['current']['total'], 3)

    def test_sse_order_and_chat_isolation(self):
        stream = events.stream(-900)
        next(stream)
        try:
            auto_activity.record(900, 'idle', 'Другой чат')
            auto_activity.record(-900, 'idle', 'Этот чат')
            payload = json.loads(next(stream)[6:])
            self.assertEqual(payload['type'], 'auto_activity')
            self.assertEqual(payload['data']['entry']['message'], 'Этот чат')
        finally:
            stream.close()

    def test_read_endpoint_never_calls_telegram(self):
        with patch.object(events, 'publish'):
            auto_activity.record(-900, 'typing', 'Печатаю', duration_s=5)
        with patch.object(telegram_utils, 'run_in_telegram_loop', side_effect=AssertionError('Telegram запрещён')):
            result = main.app.test_client().get('/api/auto_activity/-900')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['current']['duration_s'], 5)

    def test_parts_pauses_and_failed_send(self):
        seen = []
        async def text_send(_chat, text, settings=None, progress=None):
            return (text != 'вторая', 'сбой' if text == 'вторая' else None)
        async def sticker_send(_chat, name, settings=None, progress=None):
            return True, None
        def bridge(coro, timeout=60):
            return asyncio.run(coro)
        with patch.object(bot_logic, 'replace_standalone_sticker_names', side_effect=lambda text: text), \
             patch.object(bot_logic, 'send_telegram_message', side_effect=text_send), \
             patch.object(bot_logic, 'send_sticker_by_codename', side_effect=sticker_send), \
             patch.object(bot_logic, 'run_in_telegram_loop', side_effect=bridge), \
             patch.object(bot_logic.time, 'sleep') as sleep:
            result = bot_logic.send_generated_reply(-900, 'первая sticker(cat) {split}вторая{split}не отправлять',
                settings={'base_thinking_delay_s_min': 2, 'base_thinking_delay_s_max': 2},
                progress=lambda phase, message, **kw: seen.append({'phase': phase, **kw}))
        self.assertEqual(result, (False, 'сбой'))
        self.assertEqual([item['part'] for item in seen if item['phase'] == 'part'], [1, 2, 3])
        self.assertEqual([item['text'] for item in seen if item['phase'] == 'part'], ['первая', 'cat', 'вторая'])
        self.assertEqual([item['duration_s'] for item in seen if item['phase'] == 'pause'], [2, 2])
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(seen[-1]['phase'], 'error')

    def test_sticker_history_marker_and_id_commands(self):
        self.assertEqual(
            telegram_utils._sticker_history_text(5287768146319515241),
            'sticker(id=5287768146319515241) - не удалось загрузить.')
        self.assertEqual(
            telegram_utils._sticker_history_text(10, 'cat_wave', visible=True),
            'sticker(cat_wave)')
        self.assertEqual(
            telegram_utils._sticker_history_text(10, 'cat_wave', own=True),
            'sticker(cat_wave)')

        sent_texts = []
        sent_ids = []

        async def text_send(_chat, text, settings=None, progress=None):
            sent_texts.append(text)
            return True, None

        async def sticker_id_send(_chat, sticker_id, settings=None, progress=None):
            sent_ids.append(sticker_id)
            return True, None

        def bridge(coro, timeout=60):
            return asyncio.run(coro)

        with patch.object(bot_logic, 'replace_standalone_sticker_names', side_effect=lambda text: text), \
             patch.object(bot_logic, 'send_telegram_message', side_effect=text_send), \
             patch.object(bot_logic, 'send_sticker_by_id', side_effect=sticker_id_send), \
             patch.object(bot_logic, 'run_in_telegram_loop', side_effect=bridge), \
             patch.object(bot_logic.time, 'sleep'):
            result = bot_logic.send_generated_reply(
                -900,
                'до sticker(id=123) после sticker(id=неверный айди) - не удалось загрузить. конец',
                settings={'base_thinking_delay_s_min': 0, 'base_thinking_delay_s_max': 0})

        self.assertEqual(result, (True, None))
        self.assertEqual(sent_ids, [123])
        self.assertEqual(sent_texts, ['до', 'после', 'конец'])

    def test_explicit_known_sticker_id_is_sent_even_when_raw_or_quarantined(self):
        sticker = {'id': 123, 'access_hash': 456, 'quarantined': True}
        fake_client = SimpleNamespace(is_connected=lambda: True)
        with patch.object(telegram_utils, 'client', fake_client), \
             patch.object(sticker_store, 'find_by_id', return_value=('raw_123', sticker)), \
             patch.object(telegram_utils, '_send_sticker_document', new_callable=AsyncMock,
                          return_value=(True, None)) as send:
            result = asyncio.run(telegram_utils.send_sticker_by_id(-900, 123))
        self.assertEqual(result, (True, None))
        send.assert_awaited_once_with(-900, sticker, 'raw_123', None, None)

        with patch.object(telegram_utils, 'client', fake_client), \
             patch.object(sticker_store, 'find_by_id', return_value=(None, None)):
            result = asyncio.run(telegram_utils.send_sticker_by_id(-900, 999))
        self.assertTrue(result[0])
        self.assertIn('not found', result[1])

    def test_worker_receives_response_and_stops_before_send(self):
        stop = threading.Event()
        history = [{'role': 'user', 'parts': [{'text': '(ID: 1)\n[2026-09-16 12:00:00]\nпривет'}]}]
        def bridge(coro, timeout=60):
            name = coro.__name__
            coro.close()
            if name == 'wait_for_chat_send_slot':
                return {'ready': True, 'waited': False}, None
            return ({'name': 'Тест'} if name == 'get_chat_info' else history), None
        def generate(**kw):
            stop.set()
            return 'полный ответ{split}ещё ответ', None
        with ExitStack() as stack:
            stack.enter_context(patch.object(app_state, 'auto_mode_workers', {-900: {'status': 'active'}}))
            stack.enter_context(patch.object(bot_logic, 'get_chat_settings', return_value={
                'active_character_id': 'test', 'model_name': 'gpt-5-mini', 'auto_mode_initial_wait': 0,
                'enable_auto_memory': False}))
            stack.enter_context(patch.object(bot_logic.character_utils, 'get_character', return_value={'name': 'Тест'}))
            stack.enter_context(patch.object(bot_logic.character_utils, 'get_full_prompt_for_character', return_value='роль'))
            stack.enter_context(patch.object(bot_logic, 'run_in_telegram_loop', side_effect=bridge))
            stack.enter_context(patch.object(bot_logic, 'generate_with_sticker_guard', side_effect=generate))
            stack.enter_context(patch.object(bot_logic.key_pool, 'get_status', return_value={}))
            stack.enter_context(patch.object(events, 'publish'))
            send = stack.enter_context(patch.object(bot_logic, 'send_generated_reply'))
            bot_logic.auto_mode_worker(-900, stop)
        send.assert_not_called()
        state = auto_activity.snapshot(-900)
        self.assertEqual(state['response']['text'], 'полный ответ{split}ещё ответ')
        self.assertIn('request', [entry['phase'] for entry in state['entries']])
        self.assertEqual(state['current']['phase'], 'stopped')

    def test_worker_check_interval_and_slow_mode_overlap(self):
        for interval, cooldown, startup_cooldown, should_reply in [
                (60, 5, 0, True), (30, 60, 0, True), (60, 0, 20, True), (60, 0, 0, False)]:
            with self.subTest(interval=interval, cooldown=cooldown, startup=startup_cooldown, reply=should_reply):
                clock = SimpleNamespace(now=0.0, blocked_until=startup_cooldown)
                checks = []
                sent_at = []
                history = [{'role': 'user' if should_reply else 'model',
                    'parts': [{'text': '[2026-09-16 12:00:00]\nпривет'}]}]

                class Stop:
                    stopped = False
                    waits = 0
                    def is_set(self): return self.stopped
                    def set(self): self.stopped = True
                    def wait(self, seconds):
                        self.waits += 1
                        if self.waits > 20:
                            self.set()
                        if not self.stopped:
                            clock.now += seconds
                        return self.stopped

                stop = Stop()

                def bridge(coro, timeout=60):
                    name = coro.__name__
                    limit = coro.cr_frame.f_locals.get('limit')
                    coro.close()
                    if name == 'wait_for_chat_send_slot':
                        remaining = max(0, clock.blocked_until - clock.now)
                        stop.wait(remaining)
                        return {'ready': True, 'waited': remaining > 0}, None
                    if name == 'get_chat_info':
                        return {'name': 'Тест'}, None
                    if limit == 2:
                        checks.append(clock.now)
                        if len(checks) == (3 if should_reply else 2):
                            stop.set()
                            return [{'role': 'model', 'parts': [{'text': 'ответ'}]}], None
                    return history, None

                def generate(**kw):
                    clock.now += 3
                    return 'ответ', None

                def send(*args, **kw):
                    clock.now += 2
                    sent_at.append(clock.now)
                    clock.blocked_until = clock.now + cooldown
                    return True, None

                with ExitStack() as stack:
                    stack.enter_context(patch.object(app_state, 'auto_mode_workers', {-900: {'status': 'active'}}))
                    stack.enter_context(patch.object(bot_logic, 'time', SimpleNamespace(monotonic=lambda: clock.now)))
                    stack.enter_context(patch.object(bot_logic, 'get_chat_settings', return_value={
                        'active_character_id': 'test', 'model_name': 'gpt-5-mini',
                        'auto_mode_check_interval': interval, 'auto_mode_initial_wait': 4,
                        'auto_mode_no_reply_timeout': 9999, 'enable_auto_memory': False}))
                    stack.enter_context(patch.object(bot_logic.character_utils, 'get_character', return_value={'name': 'Тест'}))
                    stack.enter_context(patch.object(bot_logic.character_utils, 'get_full_prompt_for_character', return_value='роль'))
                    stack.enter_context(patch.object(bot_logic, 'run_in_telegram_loop', side_effect=bridge))
                    stack.enter_context(patch.object(bot_logic, 'generate_with_sticker_guard', side_effect=generate))
                    stack.enter_context(patch.object(bot_logic, 'send_generated_reply', side_effect=send))
                    stack.enter_context(patch.object(bot_logic.key_pool, 'get_status', return_value={}))
                    stack.enter_context(patch.object(events, 'publish'))
                    bot_logic.auto_mode_worker(-900, stop)

                if should_reply:
                    self.assertEqual(sent_at, [startup_cooldown + 9])
                    self.assertEqual(checks, [startup_cooldown, startup_cooldown + 4,
                        sent_at[0] + max(interval, cooldown)])
                else:
                    self.assertEqual(sent_at, [])
                    self.assertEqual(checks, [0, interval])


class FakeAction:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


class ActualDelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.seen = []
        self.progress = lambda phase, message, **kw: self.seen.append({'phase': phase, **kw})
        self.client = SimpleNamespace(is_connected=lambda: True, is_user_authorized=AsyncMock(return_value=True),
            get_input_entity=AsyncMock(return_value=InputPeerUser(1, 2)),
            get_messages=AsyncMock(return_value=SimpleNamespace(id=9, chat_id=-900)),
            action=lambda *args: FakeAction(), send_file=AsyncMock(), edit_message=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(id=1, peer_id=-900, text='привт')))

    async def test_double_keypress_and_disabled_chance_reach_send(self):
        settings = {'substitution_chance': 0, 'transposition_chance': 0,
            'skip_chance': 0, 'lower_chance': 0, 'word_loss_chance': 0, 'duplication_chance': 1}
        with patch.object(telegram_utils, 'client', self.client), \
             patch.object(telegram_utils.random, 'random', return_value=0), \
             patch.object(telegram_utils, 'calculate_telegram_send_delay', return_value=0), \
             patch.object(telegram_utils.asyncio, 'sleep', new_callable=AsyncMock):
            self.assertEqual(await telegram_utils.send_telegram_message(-900, 'Я a1! 🙂', settings=settings), (True, None))
            self.assertEqual(self.client.send_message.await_args.args[1], 'ЯЯ aa11!! 🙂')
            settings['duplication_chance'] = 0
            self.assertEqual(await telegram_utils.send_telegram_message(-900, 'Я a1! 🙂', settings=settings), (True, None))
            self.assertEqual(self.client.send_message.await_args.args[1], 'Я a1! 🙂')

    async def test_service_tokens_are_removed_even_when_glued_to_text(self):
        text, target = extract_reply(
            'Ахпхanswer[я променял скин]\\<fff>(17204606) бляяя '
            '(ID: 9)[2026-09-20 20:33:40]<ник:Анна>')
        self.assertEqual(target['id'], 17204606)
        self.assertNotIn('answer', text)
        self.assertNotIn('ID:', text)
        self.assertNotIn('<ник:', text)
        orphan, _ = extract_reply('- не удалось загрузить.]<Кирилл>(17204393)\nи тут текст')
        self.assertEqual(orphan, 'и тут текст')

        with patch.object(telegram_utils, 'client', self.client), \
             patch.object(telegram_utils, 'humanize_outgoing_text',
                          return_value=('привет(ID: 2)<ник:Бот>!',
                                        'привет(ID: 2)<ник:Бот>!')), \
             patch.object(telegram_utils, 'calculate_telegram_send_delay', return_value=0), \
             patch.object(telegram_utils.asyncio, 'sleep', new_callable=AsyncMock):
            self.assertEqual(await telegram_utils.send_telegram_message(-900, 'привет', settings={}),
                             (True, None))
        sent = self.client.send_message.await_args.args[1]
        self.assertNotIn('ID:', sent)
        self.assertNotIn('<ник:', sent)

    async def test_humanization_stays_independent_and_applies_to_album_caption(self):
        first = {'substitution_chance': 0, 'transposition_chance': 0, 'skip_chance': 0,
                 'lower_chance': 0, 'word_loss_chance': 0, 'duplication_chance': 1,
                 'lexicon_rules': [{'find': 'A', 'replace': 'C'}]}
        second = dict(first, duplication_chance=0, lexicon_rules=[])
        original = first.copy()
        with patch.object(telegram_utils, 'client', self.client), \
             patch.object(telegram_utils.random, 'random', return_value=0), \
             patch.object(telegram_utils, 'calculate_telegram_send_delay', return_value=0), \
             patch.object(telegram_utils, '_choose_image_delay', return_value=0), \
             patch.object(image_store, 'find_file', return_value='fake.jpg'), \
             patch.object(image_store, 'list_images', return_value=[]), \
             patch.object(telegram_utils.image_history, 'remember'), \
             patch.object(telegram_utils.asyncio, 'sleep', new_callable=AsyncMock):
            for settings, expected in ((first, 'CC -- ББ'), (second, 'A - Б'), (first, 'CC -- ББ')):
                self.assertEqual(await telegram_utils.send_telegram_message(-900, 'A — Б', settings=settings), (True, None))
                self.assertEqual(self.client.send_message.await_args.args[1], expected)
                self.assertEqual(await telegram_utils.send_images_with_caption(-900, ['a', 'b'], 'A — Б', settings), (True, None))
                self.assertEqual(self.client.send_file.await_args.kwargs['caption'], [expected, ''])
        self.assertEqual(first, original)

    async def test_dash_normalization_survives_humanizer_failure_and_caption_correction(self):
        with patch.object(telegram_utils, 'client', self.client), \
             patch.object(telegram_utils, 'calculate_telegram_send_delay', return_value=0), \
             patch.object(telegram_utils.asyncio, 'sleep', new_callable=AsyncMock), \
             patch.object(telegram_utils, 'final_fine_tune_sms', side_effect=RuntimeError('broken')), self.assertLogs(level='ERROR'):
            self.assertEqual(await telegram_utils.send_telegram_message(-900, 'A — Б', settings={}), (True, None))
        self.assertEqual(self.client.send_message.await_args.args[1], 'A - Б')
        with patch.object(telegram_utils, 'client', self.client), \
             patch.object(telegram_utils, 'final_fine_tune_sms', return_value=('A —', 'A — Б')), \
             patch.object(telegram_utils, '_choose_image_delay', return_value=0), \
             patch.object(telegram_utils, 'calculate_telegram_send_delay', return_value=0), \
             patch.object(telegram_utils, 'edit_message_with_correction_simulation', new_callable=AsyncMock) as edit, \
             patch.object(image_store, 'find_file', return_value='fake.jpg'), \
             patch.object(image_store, 'list_images', return_value=[]), \
             patch.object(telegram_utils.image_history, 'remember'), \
             patch.object(telegram_utils.asyncio, 'sleep', new_callable=AsyncMock):
            self.client.send_file.return_value = SimpleNamespace(id=1)
            self.assertEqual(await telegram_utils.send_images_with_caption(-900, ['a'], 'A — Б', {}), (True, None))
            self.assertEqual(self.client.send_file.await_args.kwargs['caption'], 'A -')
            self.assertEqual(edit.await_args.args[1], 'A - Б')
        self.assertEqual(telegram_utils.simulate_word_loss('A — Б', 1, 0), ('A — Б', 'A — Б'))
        with patch.object(telegram_utils, 'final_fine_tune_sms', return_value=('', 'слово')):
            self.assertEqual(telegram_utils.humanize_outgoing_text('слово'), ('слово', 'слово'))

    async def test_typing_and_correction_match_sleep_and_actual_text(self):
        with patch.object(telegram_utils, 'client', self.client), \
             patch.object(telegram_utils, 'final_fine_tune_sms', return_value=('привт', 'привет')), \
             patch.object(telegram_utils, 'calculate_telegram_send_delay', return_value=4.25) as draw, \
             patch.object(telegram_utils.random, 'uniform', side_effect=[2, 50]), \
             patch.object(telegram_utils.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            result = await telegram_utils.send_telegram_message(-900, 'answer(9)привет', settings={}, progress=self.progress)
        self.assertEqual(result, (True, None))
        draw.assert_called_once()
        self.assertEqual([item['duration_s'] for item in self.seen if 'duration_s' in item], [4.25, 1.8, .5])
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [4.25, 1.8, .5])
        self.assertEqual(self.seen[0]['text'], 'привт')
        self.client.send_message.assert_awaited_once_with(-900, 'привт', reply_to=9)
        self.client.edit_message.assert_awaited_once_with(-900, 1, text='привет')

    async def test_sticker_and_image_selection_match_actual_sleep(self):
        with patch.object(telegram_utils, 'client', self.client), \
             patch.object(telegram_utils, '_choose_sticker_delay', return_value=3.75), \
             patch.object(telegram_utils, '_choose_image_delay', return_value=2.25), \
             patch.object(sticker_store, 'record_use'), \
             patch.object(image_store, 'find_file', return_value='fake.jpg'), \
             patch.object(telegram_utils.image_history, 'remember'), \
             patch.object(telegram_utils.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            self.assertEqual(await telegram_utils._send_sticker_document(-900, {'id': 123, 'access_hash': 456},
                'cat', progress=self.progress), (True, None))
            self.assertEqual(await telegram_utils.send_image_by_codename(-900, 'pic', progress=self.progress), (True, None))
        self.assertEqual([item['duration_s'] for item in self.seen if 'duration_s' in item], [3.75, 2.25])
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [3.75, 2.25])
        self.assertEqual(self.client.send_file.await_count, 2)

    async def test_ui_callback_failure_does_not_abort_send(self):
        def broken(*args, **kw): raise RuntimeError('Нет интерфейса')
        with patch.object(telegram_utils, 'client', self.client), \
             patch.object(telegram_utils, 'final_fine_tune_sms', return_value=('привет', 'привет')), \
             patch.object(telegram_utils, 'calculate_telegram_send_delay', return_value=0), \
             patch.object(telegram_utils.asyncio, 'sleep', new_callable=AsyncMock), self.assertLogs(level='ERROR'):
            self.assertEqual(await telegram_utils.send_telegram_message(-900, 'привет', settings={}, progress=broken), (True, None))
        self.client.send_message.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
