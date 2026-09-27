"""Оффлайн-проверки очереди, команд и перезаписи памяти кластера."""

import asyncio
import queue
import tempfile
import time
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import character_utils
import cluster_chatting as cluster
import settings_manager


class ClusterProtocolTests(unittest.TestCase):
    def test_commands_are_removed_even_when_glued_to_message(self):
        self.assertEqual(cluster.extract_commands('Пока!exit()'), ('Пока!', ('exit', None)))
        self.assertEqual(cluster.extract_commands('Ухожу ignore(-1) сейчас'),
                         ('Ухожу  сейчас', ('ignore', -1)))
        self.assertEqual(cluster.extract_commands('ignore(15)'), ('', ('ignore', 15)))
        self.assertEqual(cluster.extract_commands('Пока ignore(навсегда)'), ('Пока', None))
        self.assertEqual(cluster.extract_commands('щас ls(@ivan) напишу', ('ls',)),
                         ('щас  напишу', ('ls', '@ivan')))
        self.assertEqual(cluster.extract_commands('ls(Иван Петров)', ('exit', 'ignore')),
                         ('', None))

    def test_resolve_group_member_prefers_exact_username_or_name(self):
        users = [
            SimpleNamespace(id=11, username='ivan', first_name='Иван', last_name='Петров', deleted=False),
            SimpleNamespace(id=22, username='ivanov', first_name='Иван', last_name='Иванов', deleted=False),
        ]
        import telegram_utils
        client = SimpleNamespace(get_participants=AsyncMock(return_value=users))
        with patch.object(telegram_utils, 'client', client):
            member, error = asyncio.run(cluster.resolve_group_member(-100, '@ivan', own_id=999))
        self.assertIsNone(error)
        self.assertEqual(member, {'id': 11, 'name': 'Иван Петров'})
        client.get_participants.assert_awaited_once_with(-100, search='ivan', limit=100)

    def test_queue_priority_and_ignore_lifetime(self):
        state = cluster.SessionQueue()
        now = time.monotonic()
        state.add(11, now)
        state.add(22, now + 1)
        state.add(33, now + 2)
        self.assertEqual(state.next(), 11)
        self.assertEqual(list(state.pending), [22, 33])
        state.leave(-1)
        self.assertFalse(state.add(11, now + 3))
        self.assertEqual(state.next(), 22)
        state.leave(0)
        self.assertTrue(state.add(22, now + 4))
        self.assertEqual(state.next(), 33)

    def test_dm_branch_settings_inherit_and_override_only_selected_values(self):
        base = {'auto_mode_check_interval': 5, 'auto_mode_initial_wait': 8,
                'typing_delay_ms_min': 40, 'substitution_chance': .01,
                'cluster_dm_settings_overrides': {
                    'auto_mode_check_interval': 1.5,
                    'typing_delay_ms_min': 90,
                    'unknown_setting': 'ignored'}}
        result = cluster.dm_branch_settings(base)
        self.assertEqual(result['auto_mode_check_interval'], 1.5)
        self.assertEqual(result['typing_delay_ms_min'], 90)
        self.assertEqual(result['auto_mode_initial_wait'], 8)
        self.assertEqual(result['substitution_chance'], .01)
        self.assertNotIn('unknown_setting', result)
        self.assertEqual(base['auto_mode_check_interval'], 5)

    def test_template_does_not_consume_user_braces(self):
        self.assertEqual(cluster._format_template('{group_name}: {"a": 1}', group_name='Общий'),
                         'Общий: {"a": 1}')

    def test_classification_checks_members_and_reply_without_reading(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def get_permissions(self, group_id, sender_id):
                self.calls.append(('permissions', group_id, sender_id))
                return object()

            async def get_messages(self, group_id, ids):
                self.calls.append(('reply', group_id, ids))
                return SimpleNamespace(sender_id=777)

        client = Client()
        inbox = queue.Queue()
        registry = {'group_id': -100, 'inbox': inbox, 'aliases': ['Лёха'],
                    'own_id': 777, 'members': {}}
        dm = SimpleNamespace(chat_id=11, is_private=True,
                             message=SimpleNamespace(id=1, sender_id=11, message='Привет'))
        group = SimpleNamespace(chat_id=-100, is_private=False,
                                message=SimpleNamespace(id=2, sender_id=12, message='ответ', reply_to_msg_id=9))
        import telegram_utils
        with patch.object(telegram_utils, 'client', client), patch.object(telegram_utils, 'my_id', 777):
            asyncio.run(cluster._classify(dm, registry))
            asyncio.run(cluster._classify(group, registry))
        self.assertEqual(inbox.get()[0:2], ('dm', 11))
        self.assertEqual(inbox.get()[0:4], ('group', -100, 2, True))
        self.assertEqual(client.calls, [('permissions', -100, 11), ('reply', -100, 9)])

    def test_dm_reply_gets_group_context_without_marking_group_read(self):
        calls = []
        async def get_chat_info(chat_id):
            return None
        async def get_formatted_history(chat_id, limit, settings, acknowledge=True):
            return None
        async def wait_for_chat_send_slot(chat_id, stop_event, progress, force_refresh):
            return None
        def bridge(coro, timeout=60):
            name = coro.__name__
            args = dict(coro.cr_frame.f_locals)
            coro.close()
            calls.append((name, args))
            if name == 'wait_for_chat_send_slot':
                return {'ready': True}, None
            if name == 'get_chat_info':
                return {'name': 'Друг' if args['chat_id'] == 11 else 'Группа'}, None
            if name == 'get_formatted_history':
                return [{'role': 'user', 'message_id': 1,
                         'parts': [{'text': 'Привет'}]}], None
            self.fail(name)
        sent = []
        def send(chat_id, text, **kwargs):
            sent.append((chat_id, text))
            return True, None
        with patch.object(character_utils, 'get_full_prompt_for_character', return_value='BASE'), \
             patch.object(cluster, '_ensure_dm_context', return_value='Знакомый из группы'), \
             patch.object(cluster, '_dm_context', return_value=''), \
             patch.object(cluster.model_fallbacks, 'generate',
                          side_effect=lambda settings, model, prompt, history, *args, **kwargs:
                          (sent.append(('prompt', prompt)) or 'Привет!exit()', None, model)), \
             patch.object(cluster.key_pool, 'get_status', return_value={}):
            success, action = cluster._reply(11, -100, 'Группа', 'c', {}, threading.Event(),
                lambda *args, **kwargs: None, send, get_formatted_history, get_chat_info,
                wait_for_chat_send_slot, bridge)
        self.assertEqual(success, 'sent')
        self.assertEqual(action, ('exit', None))
        self.assertIn('Знакомый из группы', sent[0][1])
        self.assertEqual(sent[0][1].count('Контекст группы:'), 1)
        self.assertEqual(sent[1], (11, 'Привет!'))
        history_calls = [args for name, args in calls if name == 'get_formatted_history']
        self.assertEqual(history_calls[1]['chat_id'], -100)
        self.assertFalse(history_calls[1]['acknowledge'])

    def test_group_reply_hides_ls_command_and_returns_start_action(self):
        async def get_chat_info(chat_id): return None
        async def get_formatted_history(chat_id, limit, settings, acknowledge=True): return None
        async def wait_for_chat_send_slot(chat_id, stop_event, progress, force_refresh): return None
        def bridge(coro, timeout=60):
            name = coro.__name__
            coro.close()
            if name == 'wait_for_chat_send_slot': return {'ready': True}, None
            if name == 'get_chat_info': return {'name': 'Группа'}, None
            if name == 'get_formatted_history':
                return [{'role': 'user', 'message_id': 1,
                         'parts': [{'text': 'Привет'}]}], None
            self.fail(name)
        prompts, sent = [], []
        with patch.object(character_utils, 'get_full_prompt_for_character', return_value='BASE'), \
             patch.object(cluster.model_fallbacks, 'generate',
                          side_effect=lambda settings, model, prompt, history, *args, **kwargs:
                          (prompts.append(prompt) or 'напишу ему ls(@ivan)', None, model)), \
             patch.object(cluster.key_pool, 'get_status', return_value={}):
            outcome, action = cluster._reply(
                -100, -100, 'Группа', 'c', {}, threading.Event(),
                lambda *args, **kwargs: None,
                lambda chat_id, text, **kwargs: (sent.append((chat_id, text)) or True, None),
                get_formatted_history, get_chat_info, wait_for_chat_send_slot, bridge,
                decision=cluster.AutoChatDecision('incoming', 1))
        self.assertEqual((outcome, action), ('sent', ('ls', '@ivan')))
        self.assertEqual(sent, [(-100, 'напишу ему')])
        self.assertIn('ls(точное имя человека или @username)', prompts[0])

    def test_own_latest_message_never_generates_without_silence_timeout(self):
        async def get_chat_info(chat_id):
            return None
        async def get_formatted_history(chat_id, limit, settings, acknowledge=True):
            return None
        async def wait_for_chat_send_slot(chat_id, stop_event, progress, force_refresh):
            return None
        def bridge(coro, timeout=60):
            name = coro.__name__
            coro.close()
            if name == 'wait_for_chat_send_slot':
                return {'ready': True}, None
            if name == 'get_chat_info':
                return {'name': 'Группа'}, None
            if name == 'get_formatted_history':
                return [{'role': 'model', 'message_id': 50,
                         'parts': [{'text': 'Последнее сообщение персонажа'}]}], None
            self.fail(name)
        with patch.object(cluster.model_fallbacks, 'generate',
                          side_effect=AssertionError('Запрос к модели не должен выполняться')):
            outcome, action = cluster._reply(
                -100, -100, 'Группа', 'c', {}, threading.Event(),
                lambda *args, **kwargs: None,
                lambda *_args, **_kwargs: self.fail('Отправка не должна выполняться'),
                get_formatted_history, get_chat_info, wait_for_chat_send_slot, bridge)
        self.assertEqual((outcome, action), ('obsolete', None))

    def test_silence_timeout_is_only_own_latest_exception_and_adds_suffix(self):
        async def get_chat_info(chat_id):
            return None
        async def get_formatted_history(chat_id, limit, settings, acknowledge=True):
            return None
        async def wait_for_chat_send_slot(chat_id, stop_event, progress, force_refresh):
            return None
        def bridge(coro, timeout=60):
            name = coro.__name__
            coro.close()
            if name == 'wait_for_chat_send_slot':
                return {'ready': True}, None
            if name == 'get_chat_info':
                return {'name': 'Группа'}, None
            if name == 'get_formatted_history':
                return [{'role': 'model', 'message_id': 50,
                         'parts': [{'text': 'Последнее сообщение персонажа'}]}], None
            self.fail(name)
        prompts = []
        settings = {'auto_mode_no_reply_suffix': 'ПРОВЕРКА МОЛЧАНИЯ'}
        with patch.object(character_utils, 'get_full_prompt_for_character', return_value='BASE'), \
             patch.object(cluster.model_fallbacks, 'generate',
                          side_effect=lambda settings, model, prompt, *args, **kwargs:
                          (prompts.append(prompt) or 'Куда все ушли?', None, model)), \
             patch.object(cluster.key_pool, 'get_status', return_value={}):
            outcome, action = cluster._reply(
                -100, -100, 'Группа', 'c', settings, threading.Event(),
                lambda *args, **kwargs: None,
                lambda *_args, **_kwargs: (True, None),
                get_formatted_history, get_chat_info, wait_for_chat_send_slot, bridge,
                timeout_trigger=True)
        self.assertEqual((outcome, action), ('sent', None))
        self.assertIn('ПРОВЕРКА МОЛЧАНИЯ', prompts[0])

    def test_cluster_group_restarts_full_pause_when_another_message_arrives(self):
        import bot_logic
        import telegram_utils
        stop = threading.Event()
        check_ids = iter([1, 2, 2, 2])
        observed_checks = []
        generated = []
        sent = []

        def history(message_id):
            return [{'role': 'user', 'message_id': message_id,
                     'parts': [{'text': f'(ID: {message_id})\n[2026-09-23 12:00:0{message_id}]\nтекст'}]}]

        def bridge(coro, timeout=60):
            name = coro.__name__
            args = dict(coro.cr_frame.f_locals)
            coro.close()
            if name == 'get_chat_info':
                return {'name': 'Группа'}, None
            if name == '_get_me':
                return SimpleNamespace(id=777, username='test', first_name='Тест'), None
            if name == 'scan_unread':
                return [], None
            if name == 'wait_for_chat_send_slot':
                return {'ready': True, 'waited': False}, None
            if name == 'get_formatted_history':
                if args['limit'] == 2:
                    message_id = next(check_ids)
                    observed_checks.append(message_id)
                    return history(message_id), None
                return history(2), None
            self.fail(f'Неожиданная корутина {name}')

        settings = dict(settings_manager.DEFAULT_CHAT_SETTINGS)
        settings.update({'active_character_id': 'c', 'auto_mode_check_interval': 0.5,
                         'auto_mode_initial_wait': 0, 'auto_mode_no_reply_timeout': 100,
                         'num_messages_to_fetch': 65, 'model_name': 'gemini-test'})

        def send(chat_id, text, **kwargs):
            sent.append((chat_id, text))
            stop.set()
            return True, None

        with patch.object(telegram_utils, 'run_in_telegram_loop', side_effect=bridge), \
             patch.object(bot_logic, 'send_generated_reply', side_effect=send), \
             patch.object(settings_manager, 'get_chat_settings', return_value=settings), \
             patch.object(character_utils, 'get_character', return_value={'name': 'Тест'}), \
             patch.object(character_utils, 'get_full_prompt_for_character', return_value='BASE'), \
             patch.object(cluster.model_fallbacks, 'generate',
                          side_effect=lambda *args, **kwargs:
                          (generated.append(True) or 'ответ', None, 'gemini-test')), \
             patch.object(cluster.key_pool, 'get_status', return_value={}):
            cluster.cluster_mode_worker(-100, stop)

        # 1 → 2 во время первой паузы: запроса ещё нет. Только после новой
        # полной паузы 2 → 2 выполняется одна генерация.
        self.assertEqual(observed_checks, [1, 2, 2, 2])
        self.assertEqual(len(generated), 1)
        self.assertEqual(sent, [(-100, 'ответ')])

    def test_cluster_dm_uses_same_polling_and_full_pause_restart(self):
        import bot_logic
        import telegram_utils
        stop = threading.Event()
        dm_check_ids = iter([1, 2, 2, 2])
        observed_checks = []
        generated = []
        sent = []
        active_ui_chats = []

        def history(message_id, role='user'):
            return [{'role': role, 'message_id': message_id,
                     'parts': [{'text': f'(ID: {message_id})\n'
                                        f'[2026-09-23 12:00:0{message_id}]\nтекст'}]}]

        scans = 0
        def bridge(coro, timeout=60):
            nonlocal scans
            name = coro.__name__
            args = dict(coro.cr_frame.f_locals)
            coro.close()
            if name == 'get_chat_info':
                return {'name': 'Друг' if args['chat_id'] == 11 else 'Группа'}, None
            if name == '_get_me':
                return SimpleNamespace(id=777, username='test', first_name='Тест'), None
            if name == 'scan_unread':
                scans += 1
                return ([('dm', 11, 1, False, time.monotonic())] if scans == 1 else []), None
            if name == 'wait_for_chat_send_slot':
                return {'ready': True, 'waited': False}, None
            if name == 'get_formatted_history':
                if args['chat_id'] == 11 and args['limit'] == 2:
                    message_id = next(dm_check_ids)
                    observed_checks.append(message_id)
                    return history(message_id), None
                if args['chat_id'] == 11:
                    return history(2), None
                return history(90), None
            self.fail(f'Неожиданная корутина {name}')

        settings = dict(settings_manager.DEFAULT_CHAT_SETTINGS)
        settings.update({'active_character_id': 'c', 'auto_mode_check_interval': 0.5,
                         'auto_mode_initial_wait': 0, 'auto_mode_no_reply_timeout': 100,
                         'cluster_dm_idle_minutes': 10, 'cluster_no_commitment': True,
                         'num_messages_to_fetch': 65, 'model_name': 'gemini-test'})

        def send(chat_id, text, **kwargs):
            sent.append((chat_id, text))
            stop.set()
            return True, None

        with patch.object(telegram_utils, 'run_in_telegram_loop', side_effect=bridge), \
             patch.object(bot_logic, 'send_generated_reply', side_effect=send), \
             patch.object(settings_manager, 'get_chat_settings', return_value=settings), \
             patch.object(character_utils, 'get_character', return_value={'name': 'Тест'}), \
             patch.object(character_utils, 'get_full_prompt_for_character', return_value='BASE'), \
             patch.object(cluster, '_dm_context', return_value='Уже знакомы'), \
             patch.object(cluster.model_fallbacks, 'generate',
                          side_effect=lambda *args, **kwargs:
                          (generated.append(True) or 'ответ в личку', None, 'gemini-test')), \
             patch.object(cluster.key_pool, 'get_status', return_value={}), \
             patch.object(cluster.events, 'publish',
                          side_effect=lambda event_type, data=None, **kwargs:
                          active_ui_chats.append(data['active_chat_id'])
                          if event_type == 'cluster_active_chat' else None):
            cluster.cluster_mode_worker(-100, stop)

        self.assertEqual(observed_checks, [1, 2, 2, 2])
        self.assertEqual(len(generated), 1)
        self.assertEqual(sent, [(11, 'ответ в личку')])
        self.assertEqual(active_ui_chats, [-100, 11, -100])

    def test_passive_group_history_does_not_acknowledge(self):
        import telegram_utils
        from datetime import datetime
        message = SimpleNamespace(id=4, date=datetime(2026, 9, 21, 12), sender_id=2,
            sender=SimpleNamespace(first_name='Друг', last_name=''),
            message='привет', text='привет', reply_to_msg_id=None, reactions=None,
            media=None, sticker=None, grouped_id=None)
        client = SimpleNamespace(is_connected=lambda: True,
            is_user_authorized=AsyncMock(return_value=True),
            get_messages=AsyncMock(return_value=[message]),
            send_read_acknowledge=AsyncMock())
        with patch.object(telegram_utils, 'client', client), \
             patch.object(telegram_utils, 'my_id', 1), \
             patch.object(telegram_utils, 'refresh_shared_stickers'), \
             patch.object(telegram_utils.image_history, 'for_chat', return_value={}):
            result, error = asyncio.run(telegram_utils.get_formatted_history(
                -100, limit=1, settings={}, download_media=False, acknowledge=False))
        self.assertIsNone(error)
        self.assertTrue(result)
        client.send_read_acknowledge.assert_not_awaited()

    def test_grouped_history_marker_is_the_last_real_message(self):
        import telegram_utils
        from datetime import datetime, timedelta
        sender = SimpleNamespace(id=2, first_name='Друг', last_name='', username='friend')
        older = SimpleNamespace(id=10, date=datetime(2026, 9, 21, 12), sender_id=2,
            sender=sender, message='первая часть', text='первая часть', reply_to_msg_id=None,
            reactions=None, media=None, sticker=None, grouped_id=None)
        newer = SimpleNamespace(id=11, date=older.date + timedelta(seconds=20), sender_id=2,
            sender=sender, message='вторая часть', text='вторая часть', reply_to_msg_id=None,
            reactions=None, media=None, sticker=None, grouped_id=None)
        client = SimpleNamespace(is_connected=lambda: True,
            is_user_authorized=AsyncMock(return_value=True),
            get_messages=AsyncMock(return_value=[newer, older]),
            send_read_acknowledge=AsyncMock())
        with patch.object(telegram_utils, 'client', client), \
             patch.object(telegram_utils, 'my_id', 1), \
             patch.object(telegram_utils, 'refresh_shared_stickers'), \
             patch.object(telegram_utils.image_history, 'for_chat', return_value={}):
            result, error = asyncio.run(telegram_utils.get_formatted_history(
                -100, limit=2, settings={}, download_media=False, acknowledge=False))
        self.assertIsNone(error)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['message_id'], 11)

    def test_cluster_activation_is_only_for_explicit_group(self):
        chars = {'advanced_settings': {'cluster_chatting_enabled': True}}
        rows = {
            -100: {'active_character_id': 'c', 'character_specifics': {
                'c': {'advanced_settings': {'cluster_chatting_enabled': True}}}},
            -200: {'active_character_id': 'c'},
            11: {'active_character_id': 'c', 'character_specifics': {
                'c': {'advanced_settings': {'cluster_chatting_enabled': True}}}},
        }
        with patch.object(settings_manager, 'load_chat_settings', return_value=rows), \
             patch.object(settings_manager.character_utils, 'get_character', return_value=chars):
            self.assertTrue(settings_manager.get_chat_settings(-100)['cluster_chatting_enabled'])
            self.assertFalse(settings_manager.get_chat_settings(-200)['cluster_chatting_enabled'])
            self.assertFalse(settings_manager.get_chat_settings(11)['cluster_chatting_enabled'])

    def test_cluster_dm_event_stream_also_receives_root_activity(self):
        import events
        stream = events.stream(chat_id=11, extra_chat_ids=[-100])
        self.assertIn('connected', next(stream))
        events.publish('auto_activity', {'message': 'root'}, chat_id=-100)
        self.assertIn('root', next(stream))
        stream.close()

    def test_open_group_page_stays_passive_while_cluster_runs(self):
        import web_routes
        with patch.object(web_routes, '_current_auto_mode_status', return_value='active'):
            self.assertFalse(web_routes._acknowledge_history(
                -100, {'cluster_chatting_enabled': True}))
            self.assertTrue(web_routes._acknowledge_history(
                -200, {'cluster_chatting_enabled': False}))


class ClusterMemoryTests(unittest.TestCase):
    def test_no_commitment_leaves_without_history_or_memory_attempt(self):
        sessions = cluster.SessionQueue()
        sessions.add(11, time.monotonic())
        self.assertEqual(sessions.next(), 11)
        progress = []

        def forbidden(*_args, **_kwargs):
            self.fail('В режиме «Без обязательств» история и память не должны вызываться.')

        result = cluster._leave_dm(
            11, sessions, 'c', -100, {'cluster_no_commitment': True},
            lambda phase, message, **details: progress.append((phase, message, details)),
            forbidden, forbidden, forbidden, ignore_minutes=-1)

        self.assertTrue(result)
        self.assertIsNone(sessions.active)
        self.assertEqual(sessions.ignored[11], -1)
        self.assertIn('память отключена', progress[-1][1])

    def test_replaces_only_while_previous_anchor_is_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            characters_path = str(Path(folder) / 'characters.json')
            state_path = str(Path(folder) / 'cluster_memory.json')
            with patch.object(character_utils, 'CHARACTERS_FILE', characters_path), \
                 patch.object(cluster, 'MEMORY_STATE_FILE', state_path), \
                 patch.object(character_utils, 'generate_chat_reply_original',
                              side_effect=[('первое', None), ('обновлено', None), ('новый период', None)]):
                character_utils.save_characters({'c': {'memory_prompt': 'Начало',
                    'personality_prompt': 'тест', 'memory_update_prompt': character_utils.DEFAULT_MEMORY_UPDATE_PROMPT,
                    'memory_model_name': 'gemini-test'}})
                def history(ids):
                    return [{'role': 'user', 'message_id': i,
                             'parts': [{'text': f'Сообщение {i}'}]} for i in ids]
                progress = lambda *args, **kwargs: None
                self.assertTrue(cluster._save_dm_memory('c', -100, 11, 'Друг', history([1, 2]),
                                                         'gemini-test', {}, progress))
                self.assertTrue(cluster._save_dm_memory('c', -100, 11, 'Друг', history([1, 2, 3]),
                                                         'gemini-test', {}, progress))
                memory = character_utils.get_character('c')['memory_prompt']
                self.assertNotIn('первое', memory)
                self.assertEqual(memory.count('обновлено'), 1)
                self.assertTrue(cluster._save_dm_memory('c', -100, 11, 'Друг', history([4, 5]),
                                                         'gemini-test', {}, progress))
                memory = character_utils.get_character('c')['memory_prompt']
                self.assertIn('обновлено', memory)
                self.assertIn('новый период', memory)


if __name__ == '__main__':
    unittest.main()
