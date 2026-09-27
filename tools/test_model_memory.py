"""Локальные проверки выбора моделей и памяти; Telegram и внешние API не вызываются."""

import asyncio
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import character_utils
import main
import openai_models
import providers
import instance_paths
import settings_manager
import web_routes
import gemini_models
import gemini_quota
import gemini_budget
import gemini_budget.core
import model_fallbacks
import events
import gemini_utils
import key_pool
import sticker_ai
import sticker_store
from google.genai import errors as genai_errors, types as genai_types


class ModelAndMemoryTests(unittest.TestCase):
    def test_sticker_describer_prompt_catalog_and_variant_storage(self):
        fixture = {
            'menhera_chan': {'enabled': True, 'description': 'героиня Menhera', 'stickers': []},
            'menhera_chan_happy': {'enabled': True, 'description': 'радуется', 'stickers': [
                {'id': 1, 'access_hash': 11, 'set_id': 101},
                {'id': 2, 'access_hash': 22, 'set_id': 101}]},
        }
        for index in range(12):
            fixture[f'other_{index}'] = {'enabled': True, 'description': f'пример {index}', 'stickers': [
                {'id': 100 + index, 'access_hash': 200 + index, 'set_id': 300 + index}]}

        catalog = sticker_ai._catalog_text(fixture)
        self.assertIn('Пак программы **menhera_chan** — героиня Menhera', catalog)
        self.assertIn('Пак программы **остальные**', catalog)
        self.assertIn('sticker_ids=1,2', catalog)
        self.assertIn('telegram_set_ids=101', catalog)
        with patch.object(sticker_ai.random, 'sample', side_effect=lambda items, count: items[:count]):
            references = sticker_ai._reference_entries(fixture)
        self.assertEqual([item['id'] for item in references[:2]], [1, 2])
        self.assertEqual(len(references), 11)
        selected = sticker_ai._reference_entries(fixture, selected_ids=['105', '110'])
        self.assertEqual([item['id'] for item in selected], [1, 2, 105, 110])
        preview = sticker_ai.reference_preview(fixture)
        self.assertEqual(len(preview), 11)
        self.assertEqual(set(preview[0]), {'id', 'codename', 'description', 'pack_name', 'set_id',
                                           'set_title', 'set_short_name', 'emoji'})

        set_ref = SimpleNamespace(id=777, access_hash=888)
        document = SimpleNamespace(attributes=[SimpleNamespace(stickerset=set_ref, alt='🙂')])
        self.assertEqual(sticker_store.telegram_set_meta(document), (777, 888))
        self.assertEqual(sticker_store.telegram_sticker_meta(document)['emoji'], '🙂')

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(sticker_store, 'STICKER_JSON_FILE', str(Path(temp_dir) / 'stickers.json')):
            sticker_store.save({
                'raw_501': {'enabled': True, 'description': '', 'stickers': [{'id': 501, 'access_hash': 1}]},
                'raw_502': {'enabled': True, 'description': '', 'stickers': [{'id': 502, 'access_hash': 2}]},
            })
            applied = sticker_ai._apply_descriptions({
                501: {'codename': 'wave', 'description': 'машет', 'pack_name': 'anime',
                      'pack_description': 'персонажи аниме'},
                502: {'codename': 'wave', 'description': 'машет', 'pack_name': 'anime',
                      'pack_description': 'персонажи аниме'},
            }, {501: {'set_id': 99, 'set_title': 'Anime Telegram',
                      'set_short_name': 'anime_pack', 'emoji': '👋'}})
            saved = sticker_store.load()
        self.assertEqual(applied['applied'], 2)
        self.assertTrue(applied['packs'][0]['created'])
        self.assertEqual(applied['stickers'][0]['codename'], 'anime_wave')
        self.assertEqual(applied['stickers'][0]['telegram_pack_title'], 'Anime Telegram')
        self.assertEqual(applied['stickers'][0]['emoji'], '👋')
        self.assertEqual({item['id'] for item in saved['anime_wave']['stickers']}, {501, 502})
        self.assertEqual(saved['anime']['description'], 'персонажи аниме')

    def test_sticker_describer_prompt_route_saves_and_resets_shared_text(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(sticker_ai, 'STICKER_AI_SETTINGS_FILE', str(Path(temp_dir) / 'settings.json')), \
             main.app.test_client() as client:
            response = client.post('/api/stickers/describer_prompt', json={'system_prompt': 'Контекст аниме'})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(sticker_ai.get_system_prompt(), 'Контекст аниме')
            response = client.post('/api/stickers/describer_prompt', json={'reset': True})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['system_prompt'], sticker_ai.DEFAULT_DESCRIBER_SYSTEM_PROMPT)

    def test_temperature_reaches_provider_configs_without_breaking_reasoning(self):
        for value in (0, 2):
            settings = {'temperature': value}
            for model in ('gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.5-flash-lite', 'gemma-4-31b-it'):
                self.assertEqual(gemini_utils.build_generation_config(settings, model).temperature, value)
            for model in ('gpt-4.1', 'gpt-4o-mini', 'gpt-5.1', 'gpt-5.2', 'gpt-5.4', 'gpt-5.4-2026-03-05'):
                config = gemini_utils.build_generation_config(settings, model)
                self.assertEqual(config['temperature'], value)
                if openai_models.supports_reasoning(model):
                    self.assertEqual(config['reasoning'], {'effort': 'none'})
            reasoning = gemini_utils.build_generation_config(dict(settings, enable_thinking=True), 'gpt-5.4')
            self.assertEqual(reasoning['reasoning'], {'effort': 'high'})
            self.assertNotIn('temperature', reasoning)
            for model in ('gpt-5', 'gpt-5-mini', 'o3', 'gpt-5.4-mini', 'gpt-5.6-sol'):
                self.assertNotIn('temperature', gemini_utils.build_generation_config(settings, model))
        self.assertIsNone(gemini_utils.build_generation_config({}, 'gemini-3.8-flash').temperature)
        self.assertNotIn('temperature', gemini_utils.build_generation_config({}, 'gpt-5.4'))
        for value in (None, '', '  '):
            self.assertIsNone(gemini_utils.parse_temperature(value))
        for value in (-.1, 2.1, 'NaN', 'inf', True, 'text'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gemini_utils.parse_temperature(value)
    def test_each_sticker_is_media_then_real_ids_in_model_history(self):
        reference = {'role': 'user', 'parts': [
            {'mime_type': 'image/jpeg', 'image_base64': 'cmVm'},
            {'text': 'ЭТАЛОН: sticker_id=8; telegram_sticker_set_id=80'}]}
        batch = [
            {'id': 501, 'access_hash': 1, 'set_id': 9001, 'set_title': 'Anime Reactions',
             'set_short_name': 'anime_reacts', 'emoji': '👋', 'index': 1},
            {'id': 502, 'access_hash': 2, 'set_id': 9001, 'set_title': 'Anime Reactions',
             'set_short_name': 'anime_reacts', 'emoji': '😴', 'index': 2},
        ]
        captured = {}

        async def media(entry, _module):
            return {'mime_type': 'image/jpeg', 'image_base64': str(entry['id']),
                    'sticker_id': entry['id']}

        def generate(**kwargs):
            captured.update(kwargs)
            return ('{"items":[{"index":1,"codename":"wave","description":"машет",'
                    '"pack_name":"anime","pack_description":"герои"},'
                    '{"index":2,"codename":"wave","description":"машет",'
                    '"pack_name":"anime","pack_description":"герои"}]}', None)

        with patch.object(sticker_ai, '_media_part', side_effect=media), \
             patch.object(sticker_ai.gemini_utils, 'generate_chat_reply_original', side_effect=generate):
            described, refused, meta = asyncio.run(sticker_ai._describe_batch(
                batch, 'gemini-3.8-flash', object(), 'особый контекст', 'КАТАЛОГ', [reference]))

        self.assertFalse(refused)
        self.assertEqual(meta, {'submitted': 2, 'requested': True, 'requests': 1, 'attempts': 1})
        self.assertEqual(set(described), {501, 502})
        self.assertEqual(captured['system_prompt'], 'особый контекст\n\nКАТАЛОГ')
        history = captured['chat_history']
        self.assertIs(history[0], reference)
        self.assertIn('sticker_id=501', history[1]['parts'][1]['text'])
        self.assertIn('telegram_sticker_set_id=9001', history[1]['parts'][1]['text'])
        self.assertIn('telegram_pack_title=Anime Reactions', history[1]['parts'][1]['text'])
        self.assertIn('telegram_pack_short_name=anime_reacts', history[1]['parts'][1]['text'])
        self.assertIn('emoji=👋', history[1]['parts'][1]['text'])
        self.assertEqual(history[1]['parts'][0]['sticker_id'], 501)

        preview = AsyncMock(return_value=None)
        with tempfile.TemporaryDirectory() as temp_dir:
            gif_path = Path(temp_dir) / '700.gif'
            gif_path.write_bytes(b'GIF89a-animation')
            model_media = AsyncMock(return_value=(str(gif_path), 'image/gif'))
            module = SimpleNamespace(get_sticker_preview=preview,
                                     get_sticker_model_media=model_media)
            message = SimpleNamespace(document=SimpleNamespace(mime_type='video/webm'))
            media_part = asyncio.run(sticker_ai._media_part(
                {'id': 700, 'access_hash': 7, 'message': message}, module))
        self.assertEqual(media_part['mime_type'], 'image/gif')
        self.assertIn('image_base64', media_part)
        self.assertNotIn('video_base64', media_part)
        preview.assert_awaited_once_with(700, 7, message=message)
        model_media.assert_awaited_once_with(700, 7, message=message)

    def test_sticker_describer_sends_animated_gif_in_one_request(self):
        calls = []

        async def media(entry, _module):
            return {'mime_type': 'image/gif', 'image_base64': 'R0lGODlh',
                    'sticker_id': entry['id']}

        def generate(**kwargs):
            calls.append(kwargs['chat_history'])
            return ('{"items":[{"index":1,"codename":"wave","description":"машет",'
                    '"pack_name":"","pack_description":""}]}', None)

        with patch.object(sticker_ai, '_media_part', side_effect=media), \
             patch.object(sticker_ai.gemini_utils, 'generate_chat_reply_original', side_effect=generate):
            described, refused, meta = asyncio.run(sticker_ai._describe_batch(
                [{'id': 501, 'access_hash': 1, 'index': 1}], 'gemini-3.5-flash',
                object(), 'prompt', 'catalog', []))

        self.assertFalse(refused)
        self.assertIn(501, described)
        self.assertEqual(meta['requests'], 1)
        self.assertEqual(meta['attempts'], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0]['parts'][0]['mime_type'], 'image/gif')
        self.assertIn('image_base64', calls[0][0]['parts'][0])
        self.assertNotIn('video_base64', calls[0][0]['parts'][0])

    def test_sticker_finder_preloads_media_without_calling_model(self):
        entry = {'id': 9007199254740993, 'access_hash': 1, 'set_id': 77,
                 'set_title': 'Котики', 'set_short_name': 'cats', 'emoji': '🐈'}
        with patch.object(sticker_ai, '_collect_unknown_from_chat',
                          new=AsyncMock(return_value=([entry], {}, {}, None))), \
             patch.object(sticker_ai, '_media_part',
                          new=AsyncMock(return_value={'mime_type': 'image/jpeg'})), \
             patch.object(sticker_ai.gemini_utils, 'generate_chat_reply_original') as generate:
            result, error = asyncio.run(sticker_ai.find_stickers_in_chat(10, limit=50))
        self.assertIsNone(error)
        self.assertEqual(result['media_ready'], 1)
        self.assertEqual(result['candidates'][0]['id'], '9007199254740993')
        self.assertEqual(result['candidates'][0]['set_title'], 'Котики')
        generate.assert_not_called()
        sticker_ai._discovery_cache.pop(10, None)

    def test_sticker_description_uses_discovery_snapshot_without_losing_candidates(self):
        entry = {'id': 501, 'access_hash': 1, 'message': object(), 'set_id': 77,
                 'set_title': 'Котики', 'set_short_name': 'cats', 'emoji': '🐈'}
        sticker_ai._discovery_cache[44] = {
            'created_at': sticker_ai.time.monotonic(), 'limit': 100, 'entries': {501: entry}}
        applied = {'applied': 1, 'stickers': [{'sticker_id': '501'}], 'packs': []}
        with patch.object(sticker_ai, '_collect_unknown_from_chat',
                          new=AsyncMock(side_effect=AssertionError('повторный поиск не нужен'))), \
             patch.object(sticker_ai, '_refresh_reference_metadata',
                          new=AsyncMock(return_value=({}, [], {}))), \
             patch.object(sticker_ai, '_reference_history', new=AsyncMock(return_value=[])), \
             patch.object(sticker_ai, '_describe_batch', new=AsyncMock(return_value=(
                 {501: {'codename': 'cat', 'description': 'кот', 'pack_name': '',
                        'pack_description': ''}}, False,
                 {'submitted': 1, 'requested': True}))), \
             patch.object(sticker_ai, '_apply_descriptions', return_value=applied), \
             patch('telegram_utils.load_sticker_db'):
            result, error = asyncio.run(sticker_ai.describe_from_chat(
                44, 'gemini-3.8-flash', candidate_ids=['501']))
        sticker_ai._discovery_cache.pop(44, None)
        self.assertIsNone(error)
        self.assertEqual(result['found'], 1)
        self.assertEqual(result['described'], 1)
        self.assertEqual(result['model'], 'gemini-3.8-flash')

    def test_sticker_pack_lookup_fills_title_short_name_and_emoji(self):
        reference = SimpleNamespace(id=77, access_hash=88)
        entries = [{'id': 501, 'set_id': 77, 'set_access_hash': 88,
                    'set_reference': reference, 'emoji': ''}]

        class FakeClient:
            async def __call__(self, request):
                self.request = request
                document = SimpleNamespace(id=501, mime_type='video/webm', attributes=[])
                self.document = document
                return SimpleNamespace(
                    set=SimpleNamespace(id=77, access_hash=88, title='Коты на каждый день',
                                        short_name='daily_cats'),
                    packs=[SimpleNamespace(emoticon='😼', documents=[501])],
                    documents=[document])

        client = FakeClient()
        asyncio.run(sticker_ai._enrich_pack_metadata(
            entries, SimpleNamespace(client=client)))
        self.assertEqual(entries[0]['set_title'], 'Коты на каждый день')
        self.assertEqual(entries[0]['set_short_name'], 'daily_cats')
        self.assertEqual(entries[0]['emoji'], '😼')
        self.assertIs(entries[0]['media_source'], client.document)

    def test_sticker_preview_uses_live_message_file_reference(self):
        import io
        import telegram_utils
        from PIL import Image

        raw = io.BytesIO()
        Image.new('RGB', (8, 8), (255, 100, 20)).save(raw, 'PNG')
        message = object()
        client = SimpleNamespace(
            is_connected=lambda: True,
            download_media=AsyncMock(return_value=raw.getvalue()))
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_utils, 'STICKERS_CACHE_DIR', temp_dir), \
             patch.object(telegram_utils, 'client', client):
            path = asyncio.run(telegram_utils.get_sticker_preview(
                501, access_hash=77, message=message))
            self.assertTrue(Path(path).is_file())
        client.download_media.assert_awaited_once_with(message, file=bytes, thumb=-1)

    def test_sticker_gif_uses_live_message_converts_once_and_reuses_cache(self):
        import telegram_utils

        message = object()
        client = SimpleNamespace(
            is_connected=lambda: True,
            download_media=AsyncMock(return_value=b'webm-animation'))
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_utils, 'STICKERS_CACHE_DIR', temp_dir), \
             patch.object(telegram_utils, 'client', client), \
             patch.object(telegram_utils.sticker_media, 'webm_to_gif',
                          return_value=b'GIF89a-animation') as convert:
            first = asyncio.run(telegram_utils.get_sticker_gif(
                501, access_hash=77, message=message))
            second = asyncio.run(telegram_utils.get_sticker_gif(
                501, access_hash=77, message=message))
            self.assertEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), b'GIF89a-animation')
        client.download_media.assert_awaited_once_with(message, file=bytes)
        convert.assert_called_once_with(b'webm-animation')

    def test_webm_sticker_does_not_block_history_or_use_old_static_preview(self):
        import telegram_utils

        started = asyncio.Event()
        release = asyncio.Event()
        message = SimpleNamespace(document=SimpleNamespace(mime_type='video/webm'))

        async def make_gif(_id, _hash, message=None):
            started.set()
            await release.wait()
            return gif_path

        async def exercise():
            result = await telegram_utils.get_sticker_model_media(
                123, 456, message=message, wait_for_download=False)
            self.assertEqual(result, (None, None))
            await asyncio.wait_for(started.wait(), 1)
            release.set()
            await asyncio.gather(*telegram_utils._sticker_media_tasks.values())
            Path(gif_path).write_bytes(b'GIF89a')
            return await telegram_utils.get_sticker_model_media(
                123, 456, message=message, wait_for_download=False)

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_utils, 'STICKERS_CACHE_DIR', temp_dir), \
             patch.object(telegram_utils, 'get_sticker_gif', side_effect=make_gif):
            gif_path = str(Path(temp_dir) / '123.gif')
            (Path(temp_dir) / '123.jpg').write_bytes(b'static-preview')
            telegram_utils._sticker_media_tasks.clear()
            telegram_utils._sticker_gif_fallback.discard(123)
            result = asyncio.run(exercise())
        telegram_utils._sticker_media_tasks.clear()
        self.assertEqual(result, (gif_path, 'image/gif'))

    def test_media_downloads_are_deduplicated_and_saved_in_visible_cache(self):
        import telegram_utils
        from telethon.tl.types import MessageMediaPhoto

        download = AsyncMock(return_value=b'jpeg-data')
        message = SimpleNamespace(
            id=701, media=MessageMediaPhoto(photo=None), sticker=None,
            download_media=download)
        fake_client = SimpleNamespace(is_connected=lambda: True)

        async def load_twice():
            return await asyncio.gather(
                telegram_utils.get_media_for_message(55, 701, message=message),
                telegram_utils.get_media_for_message(55, 701, message=message))

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_utils, 'MEDIA_CACHE_DIR', temp_dir), \
             patch.object(telegram_utils, 'LEGACY_MEDIA_CACHE_DIR', None), \
             patch.object(telegram_utils, 'client', fake_client):
            telegram_utils._media_download_tasks.clear()
            results = asyncio.run(load_twice())
            cached = Path(temp_dir) / '55_701.jpg'
            self.assertEqual(cached.read_bytes(), b'jpeg-data')
        telegram_utils._media_download_tasks.clear()

        self.assertEqual(download.await_count, 1)
        self.assertEqual(results[0], results[1])
        self.assertIsNone(results[0][1])

    def test_slow_media_continues_in_background_without_blocking_generation(self):
        import telegram_utils
        from telethon.tl.types import MessageMediaPhoto

        release = asyncio.Event()
        started = asyncio.Event()
        calls = []

        async def delayed_download(**_kwargs):
            calls.append(1)
            started.set()
            await release.wait()
            return b'eventually-loaded'

        message = SimpleNamespace(
            id=702, media=MessageMediaPhoto(photo=None), sticker=None,
            download_media=delayed_download)
        fake_client = SimpleNamespace(is_connected=lambda: True)

        async def load_and_continue():
            first, second = await asyncio.gather(*[
                telegram_utils.get_media_for_message(
                    55, 702, message=message, wait_for_download=False) for _ in range(2)])
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(first, (None, telegram_utils.MEDIA_LOADING_MESSAGE))
            self.assertEqual(second, first)
            self.assertEqual(len(calls), 1)
            release.set()
            await asyncio.gather(*telegram_utils._media_download_tasks.values())
            return await telegram_utils.get_media_for_message(
                55, 702, message=message, wait_for_download=False)

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_utils, 'MEDIA_CACHE_DIR', temp_dir), \
             patch.object(telegram_utils, 'LEGACY_MEDIA_CACHE_DIR', None), \
             patch.object(telegram_utils, 'client', fake_client):
            telegram_utils._media_download_tasks.clear()
            parts, error = asyncio.run(load_and_continue())
            self.assertEqual((Path(temp_dir) / '55_702.jpg').read_bytes(), b'eventually-loaded')
        telegram_utils._media_download_tasks.clear()

        self.assertIsNone(error)
        self.assertEqual(parts[0]['image_base64'], 'ZXZlbnR1YWxseS1sb2FkZWQ=')

    def test_history_uses_available_media_and_placeholder_for_pending_file(self):
        import telegram_utils
        from datetime import datetime
        from telethon.tl.types import MessageMediaPhoto

        release = asyncio.Event()
        started = asyncio.Event()

        async def delayed_download(**_kwargs):
            started.set()
            await release.wait()
            return b'photo-bytes'

        message = SimpleNamespace(
            id=705, date=datetime(2026, 9, 20, 12), sender_id=2,
            sender=SimpleNamespace(first_name='Анна', last_name=''),
            message='', text='', reply_to_msg_id=None, reactions=None,
            media=MessageMediaPhoto(photo=None), sticker=None, grouped_id=None,
            download_media=delayed_download)
        fake_client = SimpleNamespace(
            is_connected=lambda: True, is_user_authorized=AsyncMock(return_value=True),
            get_messages=AsyncMock(return_value=[message]),
            send_read_acknowledge=AsyncMock())

        async def read_before_and_after():
            pending, error = await telegram_utils.get_formatted_history(55, limit=1,
                settings={'can_see_photos': True}, download_media=True)
            self.assertIsNone(error)
            self.assertIn('[Изображение] - не удалось загрузить.', pending[0]['parts'][0]['text'])
            route_parts, route_error = await telegram_utils.get_media_for_message(
                55, 705, wait_for_download=False)
            self.assertIsNone(route_parts)
            self.assertEqual(route_error, telegram_utils.MEDIA_LOADING_MESSAGE)
            self.assertEqual(fake_client.get_messages.await_count, 1)
            await asyncio.wait_for(started.wait(), 1)
            release.set()
            await asyncio.gather(*telegram_utils._media_download_tasks.values())
            ready, error = await telegram_utils.get_formatted_history(55, limit=1,
                settings={'can_see_photos': True}, download_media=True)
            self.assertIsNone(error)
            return ready

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_utils, 'MEDIA_CACHE_DIR', temp_dir), \
             patch.object(telegram_utils, 'LEGACY_MEDIA_CACHE_DIR', None), \
             patch.object(telegram_utils, 'client', fake_client), \
             patch.object(telegram_utils, 'my_id', 1), \
             patch.object(telegram_utils, 'refresh_shared_stickers'), \
             patch.object(telegram_utils.image_history, 'for_chat', return_value={}):
            telegram_utils._media_download_tasks.clear()
            ready = asyncio.run(read_before_and_after())
            self.assertEqual((Path(temp_dir) / '55_705.jpg').read_bytes(), b'photo-bytes')
        telegram_utils._media_download_tasks.clear()
        self.assertEqual(ready[0]['parts'][1]['image_base64'], 'cGhvdG8tYnl0ZXM=')

    def test_media_route_reports_background_loading_instead_of_waiting(self):
        import telegram_utils

        def bridge(coro, timeout=60):
            coro.close()
            return None, telegram_utils.MEDIA_LOADING_MESSAGE

        with patch.object(web_routes, 'run_in_telegram_loop', side_effect=bridge), \
             main.app.test_client() as client:
            response = client.get('/media/55/705')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['loading'])

    def test_sticker_find_route_is_separate_and_description_requires_candidates(self):
        def bridge(coro, timeout=60):
            coro.close()
            return ({'found': 1, 'media_ready': 1, 'candidates': [{
                'id': '9007199254740993', 'set_id': '77', 'set_title': 'Коты',
                'set_short_name': 'cats', 'emoji': '🐈', 'media_available': True}]}, None)

        with patch.object(web_routes, 'run_in_telegram_loop', side_effect=bridge), \
             main.app.test_client() as client:
            response = client.post('/api/stickers/find_in_chat/1000', json={'limit': 50})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['candidates'][0]['id'], '9007199254740993')
            response = client.post('/api/stickers/describe_from_chat/1000', json={'limit': 50})
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertIn('Сначала найдите', response.get_json()['message'])

    def test_temperature_is_sent_to_responses_api_including_zero(self):
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text='Привет', output=[], usage=None)
        fake = SimpleNamespace(responses=SimpleNamespace(create=create))
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(gemini_utils, 'GENERATION_LOG_FILE', str(Path(temp_dir) / 'generation.txt')), \
             patch.object(gemini_utils, '_check_openai_budget', return_value=None), \
             patch.object(gemini_utils, '_log_response'), \
             patch.object(providers, 'budget', return_value=None), \
             patch.object(key_pool, 'call_with_rotation', side_effect=lambda fn, **kw: (fn(fake), None)):
            for model, settings, expected in (
                ('gpt-4.1', {'temperature': 0}, 0),
                ('gpt-5.4', {'temperature': 2}, 2),
                ('gpt-5.4', {'temperature': 2, 'enable_thinking': True}, None),
                ('gpt-5-mini', {'temperature': 2}, None)):
                config = gemini_utils.build_generation_config(settings, model)
                self.assertEqual(gemini_utils._generate_openai(model, 'test',
                    [{'role': 'user', 'parts': [{'text': 'Привет'}]}], config), ('Привет', None))
                self.assertEqual(calls[-1].get('temperature'), expected)
                if model == 'gpt-5.4' and expected is not None:
                    self.assertEqual(calls[-1]['reasoning'], {'effort': 'none'})

    def test_lexicon_round_trip_defaults_presets_and_invalid_save(self):
        default_rules = [{'find': 'Привет', 'replace': 'Привета', 'chance': 1}]
        chat_rules = [{'find': '[x]\n', 'replace': '\\1', 'chance': .5},
                      {'find': '\\1', 'replace': 'готово', 'chance': 1}]
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(character_utils, 'CHARACTERS_FILE', str(Path(temp_dir) / 'characters.json')), \
             patch.object(settings_manager, 'CHAT_SETTINGS_FILE', str(Path(temp_dir) / 'chat_settings.json')), \
             patch.object(settings_manager, 'LEGACY_CHAT_SETTINGS_FILE', str(Path(temp_dir) / 'legacy.json')), \
             patch.object(instance_paths, 'IS_MANAGED', False), main.app.test_client() as client:
            character_utils.save_characters({'anna': {'name': 'Анна', 'advanced_settings': {
                'lexicon_rules': default_rules}}})
            settings_manager.save_chat_settings({-1000: {'active_character_id': 'anna'},
                                                 -1001: {'active_character_id': 'anna'}})
            current = settings_manager.get_chat_settings(-1000)
            self.assertEqual(current['lexicon_rules'], default_rules)
            current['lexicon_rules'][0]['find'] = 'changed'
            self.assertEqual(settings_manager.get_chat_settings(-1000)['lexicon_rules'], default_rules)
            self.assertEqual(settings_manager.DEFAULT_CHAT_SETTINGS['lexicon_rules'], [])
            form = {key: ('true' if value else 'false') if isinstance(value, bool) else ('' if value is None else str(value))
                    for key, value in settings_manager.DEFAULT_CHAT_SETTINGS.items()
                    if not isinstance(value, list)}
            form.update(save_action='save_for_chat', lexicon_rules=json.dumps(chat_rules),
                        temperature='2',
                        auto_fallback_models='\n'.join(model_fallbacks.DEFAULT_CHAIN))
            response = client.post('/save_chat_settings/-1000', data=form)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(settings_manager.get_chat_settings(-1000)['lexicon_rules'], chat_rules)
            self.assertEqual(settings_manager.get_chat_settings(-1000)['temperature'], 2)
            self.assertIsNone(settings_manager.get_chat_settings(-1001)['temperature'])
            self.assertEqual(settings_manager.get_chat_settings(-1001)['lexicon_rules'], default_rules)
            dm_form = dict(form, cluster_dm_overrides_present='1',
                           cluster_dm_auto_mode_check_interval='1.25',
                           cluster_dm_auto_mode_initial_wait='12',
                           cluster_dm_typing_delay_ms_min='80',
                           cluster_dm_substitution_chance='0')
            response = client.post('/save_chat_settings/-1000', data=dm_form)
            self.assertEqual(response.status_code, 200, response.get_json())
            overrides = settings_manager.get_chat_settings(-1000)['cluster_dm_settings_overrides']
            self.assertEqual(overrides, {'auto_mode_check_interval': 1.25,
                                         'auto_mode_initial_wait': 12.0,
                                         'typing_delay_ms_min': 80.0,
                                         'substitution_chance': 0.0})
            invalid_dm = dict(dm_form, cluster_dm_auto_mode_check_interval='0.1')
            self.assertEqual(client.post('/save_chat_settings/-1000', data=invalid_dm).status_code, 400)
            self.assertEqual(settings_manager.get_chat_settings(-1000)['cluster_dm_settings_overrides'],
                             overrides)
            with open(settings_manager.CHAT_SETTINGS_FILE, encoding='utf-8') as stream:
                saved = json.load(stream)
            self.assertEqual(saved['-1000']['character_specifics']['anna']['advanced_settings']['lexicon_rules'], chat_rules)
            invalid = dict(form, lexicon_rules='[{"find":"x","chance":2}]')
            response = client.post('/save_chat_settings/-1000', data=invalid)
            self.assertEqual(response.status_code, 400)
            invalid = dict(form, temperature='NaN')
            self.assertEqual(client.post('/save_chat_settings/-1000', data=invalid).status_code, 400)
            self.assertEqual(settings_manager.get_chat_settings(-1000)['temperature'], 2)
            self.assertEqual(settings_manager.get_chat_settings(-1000)['lexicon_rules'], chat_rules)
            old_form = dict(form)
            old_form.pop('lexicon_rules')
            old_form.pop('temperature')
            self.assertEqual(client.post('/save_chat_settings/-1000', data=old_form).status_code, 200)
            self.assertEqual(settings_manager.get_chat_settings(-1000)['temperature'], 2)
            self.assertEqual(settings_manager.get_chat_settings(-1000)['lexicon_rules'], chat_rules)
            response = client.post('/apply_preset/-1000', data={'preset_id': 'human'})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(settings_manager.get_chat_settings(-1000)['lexicon_rules'], chat_rules)
            self.assertEqual(settings_manager.get_chat_settings(-1000)['temperature'], 2)
            form['save_action'] = 'save_for_chat_and_default'
            self.assertEqual(client.post('/save_chat_settings/-1000', data=form).status_code, 200)
            self.assertEqual(settings_manager.get_chat_settings(-1001)['lexicon_rules'], chat_rules)
            self.assertEqual(settings_manager.get_chat_settings(-1001)['temperature'], 2)

    def test_new_chat_models_exclude_audio_and_preserve_configurable_timeout(self):
        options = {row['id']: row for row in web_routes._model_options(include_live=False)}
        for model in ('gemini-3.6-flash', 'gemma-4-26b-a4b-it', 'gemma-4-31b-it'):
            self.assertTrue(options[model]['free_tier'])
        self.assertEqual(options['gemma-4-31b-it']['tier'], 'light')
        self.assertEqual(options['gemini-2.5-pro']['tier'], 'paid')
        for model in ('gemini-3.1-flash-tts-preview', 'gemini-3.5-transcribe',
                      'gemini-robotics-er-2-preview', 'gemini-3.8-live'):
            self.assertFalse(gemini_models.is_text_model(model))
            self.assertNotIn(model, options)
        for enabled, level in ((True, 'HIGH'), (False, 'MINIMAL')):
            config = gemini_utils.build_generation_config({'enable_thinking': enabled}, 'gemma-4-31b-it')
            self.assertEqual(config.thinking_config.thinking_level, level)
            self.assertIsNone(config.thinking_config.thinking_budget)
        self.assertIsNone(gemini_utils.build_generation_config({'enable_google_search': True}, 'gemma-4-31b-it').tools)
        config = gemini_utils.build_generation_config({'enable_thinking': True}, 'gemini-3.8-flash')
        self.assertEqual(config.thinking_config.thinking_level, 'HIGH')
        self.assertIsNone(config.thinking_config.thinking_budget)
        self.assertEqual(config.http_options.timeout, 300000)
        old = {'auto_fallback_request_timeout_s': 120}
        self.assertEqual(settings_manager._with_current_timeout(old)['auto_fallback_request_timeout_s'], 300)
        self.assertEqual(old['auto_fallback_request_timeout_s'], 120)
        explicit = dict(old, auto_fallback_timeout_version=2)
        self.assertEqual(settings_manager._with_current_timeout(explicit)['auto_fallback_request_timeout_s'], 120)

    def test_instances_keep_character_and_chat_settings_separate(self):
        anna_rules = [{'find': 'привет', 'replace': 'привета', 'chance': 1}]
        oleg_rules = [{'find': 'привет', 'replace': 'здравствуйте', 'chance': .5}]
        characters = {'anna': {'name': 'Анна', 'advanced_settings': {'lexicon_rules': anna_rules}},
                      'oleg': {'name': 'Олег', 'advanced_settings': {'lexicon_rules': oleg_rules}}}
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(instance_paths, 'ROOT', Path(temp_dir)), \
             patch.object(instance_paths, 'IS_MANAGED', True), \
             patch.object(character_utils, 'get_character', side_effect=characters.get):
            with patch.object(instance_paths, 'INSTANCE_ID', 'a' * 32):
                anna_file = instance_paths.private_path('chat_settings.json')
            with patch.object(instance_paths, 'INSTANCE_ID', 'b' * 32):
                oleg_file = instance_paths.private_path('chat_settings.json')
            self.assertNotEqual(anna_file, oleg_file)
            root_file = str(Path(temp_dir) / 'data' / 'chat_settings.json')

            with patch.object(settings_manager, 'LEGACY_CHAT_SETTINGS_FILE', root_file):
                with patch.object(settings_manager, 'CHAT_SETTINGS_FILE', root_file):
                    settings_manager.save_chat_settings({-1000: {'active_character_id': 'anna'}})
                with patch.object(settings_manager, 'CHAT_SETTINGS_FILE', anna_file):
                    self.assertEqual(settings_manager.get_chat_settings(-1000)['active_character_id'], 'anna')
                    web_routes._store_advanced_settings(-1000, 'anna', {'duplication_chance': .01}, False)
                with patch.object(settings_manager, 'CHAT_SETTINGS_FILE', oleg_file):
                    with patch.object(instance_paths, 'IS_MANAGED', False), main.app.test_client() as client:
                        response = client.post('/chat/-1000/set_active_character', data={'character_id': 'oleg'})
                        self.assertEqual(response.status_code, 200)
                        response = client.post('/chat/-1000/set_model', data={'model_name': 'gpt-4.1-nano'})
                        self.assertEqual(response.status_code, 200)
                    settings = settings_manager.get_chat_settings(-1000)
                    self.assertEqual(settings['active_character_id'], 'oleg')
                    self.assertEqual(settings['model_name'], 'gpt-4.1-nano')
                    self.assertEqual(settings['duplication_chance'], .002)
                    self.assertEqual(settings['lexicon_rules'], oleg_rules)
                with patch.object(settings_manager, 'CHAT_SETTINGS_FILE', root_file):
                    self.assertEqual(settings_manager.get_chat_settings(-1000)['active_character_id'], 'anna')
                    settings_manager.save_chat_settings({-1000: {'active_character_id': 'oleg'}})
                with patch.object(settings_manager, 'CHAT_SETTINGS_FILE', anna_file):
                    settings = settings_manager.get_chat_settings(-1000)
                    self.assertEqual(settings['active_character_id'], 'anna')
                    self.assertEqual(settings['duplication_chance'], .01)
                    self.assertEqual(settings['model_name'], '')
                    self.assertEqual(settings['lexicon_rules'], anna_rules)
                with open(oleg_file, encoding='utf-8') as stream:
                    self.assertEqual(json.load(stream)['-1000']['active_character_id'], 'oleg')

    def test_selector_groups_and_paid_models_last(self):
        options = web_routes._model_options(include_live=False)
        for provider in ('gemini', 'openai'):
            tiers = [item['tier'] for item in options if item['provider'] == provider]
            self.assertEqual(tiers, sorted(tiers, key={'strong': 0, 'light': 1, 'unknown': 2, 'paid': 3}.get))
        by_id = {item['id']: item for item in options}
        self.assertEqual({item['id'] for item in options if item['provider'] == 'gemini' and item['free_tier']},
                         {row['id'] for row in gemini_budget.free_models()})
        for model in ('gpt-5.5', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'o1-mini'):
            self.assertEqual(by_id[model]['tier'], 'paid')
            self.assertFalse(by_id[model]['free_tier'])
        self.assertEqual(by_id['o3-mini']['tier'], 'light')
        self.assertEqual(by_id['gemini-3.8-flash']['tier'], 'strong')
        self.assertEqual(by_id['gemini-3.5-flash-lite']['tier'], 'light')
        self.assertEqual(by_id['gemini-3-flash-preview']['tier'], 'strong')
        self.assertTrue(by_id['gemini-3-flash-preview']['free_tier'])
        self.assertEqual(gemini_models.get_model_info('gemini-3-flash-preview')['input_price'], .50)
        self.assertEqual(gemini_models.get_model_info('gemini-3-flash-preview')['output_price'], 3.00)
        shared_budget = providers.budget()
        if shared_budget:
            self.assertEqual(shared_budget.group_for('o3-mini'), 'small')
            limits = shared_budget.status()['groups']
            self.assertEqual(limits['large']['limit'], 250_000)
            self.assertEqual(limits['small']['limit'], 2_500_000)
            for option in options:
                if option['provider'] == 'openai':
                    self.assertEqual(shared_budget.group_for(option['id']), openai_models.free_tier_group(option['id']))

    def test_free_models_match_account_offer_exactly(self):
        expected = {
            'large': {'gpt-5.4', 'gpt-5.2', 'gpt-5.1', 'gpt-5', 'gpt-4.1', 'gpt-4o', 'o1', 'o3'},
            'small': {'gpt-5.4-mini', 'gpt-5.4-nano', 'gpt-5-mini', 'gpt-5-nano', 'gpt-4.1-mini',
                      'gpt-4.1-nano', 'gpt-4o-mini', 'o3-mini', 'o4-mini'},
        }
        for group, model_ids in expected.items():
            self.assertEqual(openai_models.FREE_TIER_GROUPS[group]['models'], model_ids)
        self.assertEqual(openai_models.free_tier_group('o3-mini-2025-01-31'), 'small')
        self.assertIsNone(openai_models.free_tier_group('gpt-5.5-2026-04-23'))
        live = openai_models.build_selector_options(['gpt-new-paid-model'])[-1]
        self.assertEqual(live['tier'], 'paid')
        self.assertFalse(live['free_tier'])
        self.assertFalse(openai_models.is_text_model('gpt-new-paid-model-2026-09-01'))

    def test_catalog_prices_are_present(self):
        for option in openai_models.build_selector_options():
            self.assertNotIn('цена неизвестна', option['price'], option['id'])
            self.assertIn('$', option['price'])
        info = openai_models.get_model_info('gpt-5.4-mini')
        self.assertEqual((info['input_price'], info['output_price']), (.75, 4.50))
        info = openai_models.get_model_info('gpt-5.4-nano')
        self.assertEqual((info['input_price'], info['output_price']), (.20, 1.25))

    def test_memory_model_override_and_legacy_fallback(self):
        generated_with = []

        def fake_generate(**kwargs):
            generated_with.append(kwargs['model_name'])
            return 'важное событие', None

        characters = {'one': {'memory_prompt': '', 'personality_prompt': 'роль',
                              'memory_model_name': 'gpt-5.6-terra'}}
        with patch.object(character_utils, 'get_character', side_effect=lambda _id: characters['one']), \
             patch.object(character_utils, 'load_characters', return_value=characters), \
             patch.object(character_utils, 'save_characters', return_value=True), \
             patch.object(character_utils, 'generate_chat_reply_original', side_effect=fake_generate):
            text, error = character_utils.update_character_memory('one', 'Чат', False,
                                                                   [{'role': 'user', 'parts': [{'text': 'Привет'}]}],
                                                                   model_name='gemini-2.5-flash')
            self.assertIsNone(error)
            self.assertIn('важное событие', text)
            characters['one'].pop('memory_model_name')
            character_utils.update_character_memory('one', 'Чат', False,
                                                    [{'role': 'user', 'parts': [{'text': 'Ещё'}]}],
                                                    model_name='gpt-5.4-mini')
        self.assertEqual(generated_with, ['gpt-5.6-terra', 'gpt-5-mini'])

    def test_memory_receives_one_text_transcript_and_retries_old_lite_model(self):
        calls = []
        characters = {'one': {'memory_prompt': '', 'personality_prompt': 'роль',
                              'memory_model_name': 'gemini-2.5-flash-lite'}}

        def generate(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return None, 'This model is no longer available to new users'
            return 'Я запомнил важное событие.', None

        history = [{'role': 'user', 'parts': [{'text': '(ID: 1) привет'}]},
                   {'role': 'model', 'parts': [{'text': 'ответ'},
                                             {'mime_type': 'image/jpeg', 'image_base64': 'secret'}]}]
        with patch.object(character_utils, 'get_character', return_value=characters['one']), \
             patch.object(character_utils, 'load_characters', return_value=characters), \
             patch.object(character_utils, 'save_characters', return_value=True), \
             patch.object(character_utils, 'generate_chat_reply_original', side_effect=generate):
            memory, error = character_utils.update_character_memory('one', 'Чат', False, history,
                                                                     model_name='gemini-3.8-flash')
        self.assertIsNone(error)
        self.assertIn('важное событие', memory)
        self.assertEqual([call['model_name'] for call in calls],
                         ['gemini-2.5-flash-lite', 'gemini-3.5-flash-lite'])
        self.assertEqual(calls[0]['chat_history'][0]['role'], 'user')
        self.assertEqual(len(calls[0]['chat_history']), 1)
        transcript = calls[0]['chat_history'][0]['parts'][0]['text']
        self.assertIn('Сообщение 1 (собеседник)', transcript)
        self.assertIn('Сообщение 2 (персонаж)', transcript)
        self.assertIn('[Изображение]', transcript)
        self.assertNotIn('secret', transcript)

    def test_memory_rejects_chat_line_instead_of_saving_it(self):
        characters = {'one': {'memory_prompt': '', 'personality_prompt': 'роль'}}
        generated = ['(ID: 15)\n[2026-09-20 12:00:00]\nпривет',
                     'Я узнал, что собеседник любит игры.']
        with patch.object(character_utils, 'get_character', return_value=characters['one']), \
             patch.object(character_utils, 'load_characters', return_value=characters), \
             patch.object(character_utils, 'save_characters', return_value=True), \
             patch.object(character_utils, 'generate_chat_reply_original',
                          side_effect=lambda **_kwargs: (generated.pop(0), None)) as generate:
            memory, error = character_utils.update_character_memory('one', 'Чат', False,
                [{'role': 'user', 'parts': [{'text': 'люблю игры'}]}],
                model_name='gemini-3.8-flash')
        self.assertIsNone(error)
        self.assertEqual(generate.call_count, 2)
        self.assertIn('Я узнал', memory)
        self.assertNotIn('(ID: 15)', memory)

    def test_reply_context_fetches_at_most_three_ancestors(self):
        import telegram_utils
        from datetime import datetime, timedelta

        base = datetime(2026, 9, 20, 12)
        messages = {}
        for index, message_id in enumerate((10, 20, 30, 40, 50)):
            messages[message_id] = SimpleNamespace(
                id=message_id, date=base + timedelta(minutes=index),
                sender_id=2, sender=SimpleNamespace(first_name='Анна', last_name=''),
                message=f'текст {message_id}', text=f'текст {message_id}',
                reply_to_msg_id=message_id - 10 if message_id > 10 else None,
                reactions=None, media=None, sticker=None, grouped_id=None)
        batches = []

        async def get_messages(_chat_id, *, limit=None, ids=None):
            if ids is None:
                return [messages[50]]
            batches.append(ids)
            return [messages[item] for item in ids]

        fake_client = SimpleNamespace(
            is_connected=lambda: True, is_user_authorized=AsyncMock(return_value=True),
            get_messages=get_messages, send_read_acknowledge=AsyncMock())
        with patch.object(telegram_utils, 'client', fake_client), \
             patch.object(telegram_utils, 'my_id', 1), \
             patch.object(telegram_utils, 'refresh_shared_stickers'), \
             patch.object(telegram_utils.image_history, 'for_chat', return_value={}):
            history, error = asyncio.run(telegram_utils.get_formatted_history(
                55, limit=1, settings={'reply_load_outside_context': True,
                                       'reply_context_depth': 3}, download_media=False))
        self.assertIsNone(error)
        self.assertEqual(batches, [[40], [30], [20]])
        self.assertEqual(len(history), 4)
        self.assertEqual([entry['outside_context'] for entry in history],
                         [True, True, True, False])

    def test_save_character_persists_memory_model(self):
        characters = {'one': {'name': 'Персонаж', 'memory_prompt': ''}}
        with patch.object(web_routes.character_utils, 'load_characters', return_value=characters), \
             patch.object(web_routes.character_utils, 'save_characters', return_value=True), \
             patch.object(web_routes, 'load_chat_settings', return_value={}), \
             patch.object(web_routes, 'save_chat_settings', return_value=True):
            with main.app.test_client() as client:
                response = client.post('/character/save/one/1000', data={'memory_model_name': 'gpt-5-mini'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(characters['one']['memory_model_name'], 'gpt-5-mini')
                invalid = client.post('/character/save/one/1000', data={'memory_model_name': 'gpt-5-audio'})
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(characters['one']['memory_model_name'], 'gpt-5-mini')

    def test_budget_endpoint_reports_shared_balance(self):
        fake = SimpleNamespace(status=lambda: {'groups': {'large': {'left': 120000},
                                                           'small': {'left': 2400000}}, 'source': 'local'})
        with patch.object(web_routes.providers, 'budget', return_value=fake):
            with main.app.test_client() as client:
                data = client.get('/api/openai/budget').get_json()
        self.assertTrue(data['available'])
        self.assertEqual(data['groups']['large']['left'], 120000)

    def test_cached_input_is_counted_once_in_free_token_budget(self):
        import openai_budget

        response = SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, output_tokens=30,
            input_tokens_details=SimpleNamespace(cached_tokens=80)))
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.dict(os.environ, {'OPENAI_BUDGET_DIR': temp_dir}):
            openai_budget.record_response('gpt-5-mini', response)
            status = openai_budget.status()
        self.assertEqual(status['groups']['small']['used'], 130)
        self.assertEqual(status['models'][0]['cached_tokens'], 80)

    def test_launcher_selects_remembered_account(self):
        accounts = {'Первый': 'accounts/first', 'Второй': 'accounts/second'}
        with patch.object(main, 'load_accounts', return_value=accounts), \
             patch.object(main, 'read_last_account', return_value='accounts/second'):
            self.assertEqual(main.choose_account(use_last=True), 'accounts/second')


class GeminiQuotaTests(unittest.TestCase):
    @staticmethod
    def api_error(code=429, details=None, message='Check your plan and billing details.'):
        return genai_errors.APIError(code, {'error': {
            'code': code, 'message': message, 'status': 'RESOURCE_EXHAUSTED',
            'details': details or []}})

    @staticmethod
    def violation(quota_id, **extra):
        return {'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{
            'quotaId': quota_id, 'quotaDimensions': {'model': 'gemini-backend-alias'}, **extra}]}

    def test_quota_report_and_wait(self):
        minute = self.violation('GenerateContentInputTokensPerModelPerMinute-FreeTier')
        day = self.violation('GenerateRequestsPerDayPerProjectPerModel-FreeTier')
        retry = {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '12.5s'}
        cases = [
            ([minute, retry], 'TPM', 1012.5),
            ([day, retry], 'RPD', 9000),
            ([minute, day, retry], 'RPD', 9000),
            ([self.violation('InputTokensPerDay')], 'TPD', 9000),
            ([self.violation('RequestsPerMinute')], 'RPM', 1060),
            ([self.violation('RequestsPerMinute', quotaValue='0'), retry], 'равна 0', 2800),
            ([{'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'reason': 'RATE_LIMIT_EXCEEDED',
               'metadata': {'quota_limit_value': '0', 'quota_metric': 'input_tokens',
                            'quota_unit': '1/min/{project}/{model}', 'model': 'gemini-backend-alias'}}], 'равна 0', 2800),
            ([], 'без типа квоты', 0),
        ]
        with patch.object(key_pool.time, 'time', return_value=1000), \
             patch.object(key_pool, '_next_pacific_midnight_ts', return_value=9000):
            for details, label, deadline in cases:
                with self.subTest(label=label, details=details):
                    error = self.api_error(details=details)
                    failure = key_pool._classify_gemini(error, 'gemini-requested')
                    self.assertEqual(failure['model'], 'gemini-requested')
                    self.assertEqual(failure['until'], deadline)
                    self.assertIn(label, failure['reason'])
                    self.assertEqual(failure['report']['zero_limit'], label == 'равна 0')
                    if label == 'без типа квоты':
                        self.assertNotIn('RPM', failure['reason'])
            legacy = self.api_error(message='Quota exceeded, limit: 0.5, model: gemini-requested')
            self.assertFalse(key_pool._quota_details(legacy)['zero_limit'])
            legacy.message = 'Quota exceeded, limit: 0, model: gemini-requested. Please retry in 5s.'
            self.assertTrue(key_pool._quota_details(legacy)['zero_limit'])
            actual_zero = self.api_error(details=[minute, day, {'retryDelay': '28s'}], message=(
                'Quota exceeded for metric: generate_content_free_tier_input_token_count, '
                'limit: 0, model: gemini-2.5-pro\nPlease retry in 28.45304763s.'))
            failure = key_pool._classify_gemini(actual_zero, 'gemini-2.5-pro')
            self.assertIn('равна 0', failure['reason'])
            self.assertEqual(failure['until'], 2800)
            raw = self.api_error()
            raw.details = [minute, {'retryDelay': {'seconds': '7', 'nanos': 500000000}}]
            self.assertEqual(key_pool._quota_details(raw)['retry_s'], 7.5)

    def test_independent_projects_and_requested_alias(self):
        keys = [{'id': name, 'label': name, 'key': 'unused', 'provider': 'gemini', 'enabled': True}
                for name in ('project-a', 'project-b')]
        state = {entry['id']: key_pool._blank_state() for entry in keys}
        calls = []
        def request(client):
            calls.append(client)
            if len(calls) == 1:
                raise self.api_error(details=[self.violation('RequestsPerMinute', quotaValue='5')])
            return 'ОК'
        with patch.object(key_pool, '_keys', keys), patch.object(key_pool, '_state', state), \
             patch.object(key_pool, '_cursor', 0), patch.object(key_pool, '_get_client', side_effect=lambda k: k['id']):
            self.assertEqual(key_pool.call_with_rotation(request, model='gemini-requested'), ('ОК', None))
            self.assertIn('gemini-requested', state['project-a']['model_cooldowns'])
            self.assertNotIn('gemini-backend-alias', state['project-a']['model_cooldowns'])
            self.assertFalse(state['project-b']['model_cooldowns'])
            self.assertEqual(state['project-a']['cooldown_until'], 0)
            self.assertEqual(key_pool.call_with_rotation(request, model='gemini-other'), ('ОК', None))
            self.assertEqual(key_pool.call_with_rotation(request, model='gemini-requested'), ('ОК', None))
        self.assertEqual(calls, ['project-a', 'project-b', 'project-a', 'project-b'])

    def test_model_access_does_not_disable_key_or_hide_file_errors(self):
        cases = [(403, 'Model gemini-test: access denied', 2),
                 (404, 'models/gemini-test is not found for API version v1beta', 2),
                 (404, 'This model models/gemini-2.5-pro is no longer available to new users. Please update your code to use a newer model.', 2),
                 (404, 'File files/missing was not found', 1),
                 (403, 'Permission denied on file files/private', 1)]
        for code, message, expected_calls in cases:
            with self.subTest(code=code, message=message):
                keys = [{'id': name, 'label': name, 'key': 'unused', 'provider': 'gemini', 'enabled': True}
                        for name in ('a', 'b')]
                state = {entry['id']: key_pool._blank_state() for entry in keys}
                calls = []
                def request(client):
                    calls.append(client)
                    if client == 'a':
                        raise self.api_error(code, message=message)
                    return 'ОК'
                with patch.object(key_pool, '_keys', keys), patch.object(key_pool, '_state', state), \
                     patch.object(key_pool, '_cursor', 0), patch.object(key_pool, '_get_client', side_effect=lambda k: k['id']):
                    result, error = key_pool.call_with_rotation(request, model='gemini-test')
                self.assertEqual(len(calls), expected_calls)
                self.assertTrue(keys[0]['enabled'])
                self.assertEqual(bool(state['a']['model_cooldowns']), expected_calls == 2)
                self.assertEqual(result, 'ОК' if expected_calls == 2 else None)
                self.assertEqual(error is None, expected_calls == 2)
        invalid = self.api_error(400, details=[{'reason': 'API_KEY_INVALID'}])
        self.assertEqual(key_pool._classify_gemini(invalid, 'gemini-test')['kind'], 'bad_key')

    def test_quota_error_keeps_api_report_in_generation_log_with_media(self):
        error = self.api_error(details=[self.violation('RequestsPerMinute', quotaValue='0', subject='full-report-subject')])
        failure_text = key_pool._describe_failure(key_pool._classify_gemini(error, 'gemini-3.5-flash-lite'), 'gemini')
        history = [{'role': 'user', 'parts': [{'text': 'Что на картинке?'},
                                            {'mime_type': 'image/jpeg', 'image_base64': 'aW1hZ2U='}]}]
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(gemini_utils, 'GENERATION_LOG_FILE', str(Path(temp_dir) / 'generation.log')), \
             patch.object(key_pool, 'is_configured', return_value=True), \
             patch.object(key_pool, 'call_with_rotation', return_value=(None, failure_text)):
            result, returned_error = gemini_utils.generate_chat_reply_original('gemini-3.5-flash-lite', 'Персонаж', history)
            log = Path(gemini_utils.GENERATION_LOG_FILE).read_text(encoding='utf-8')
        self.assertIsNone(result)
        self.assertEqual(returned_error, failure_text)
        for expected in ('CONTENTS', 'Что на картинке?', '[MEDIA DATA PRESENT]', 'RESPONSE', 'quotaValue', 'равна 0', 'full-report-subject', 'status=RESOURCE_EXHAUSTED'):
            self.assertIn(expected, log)
        self.assertTrue(gemini_models.is_retired('models/gemini-3-pro-preview'))
        self.assertFalse(gemini_models.is_retired('gemini-3.1-flash-lite-preview'))
        self.assertFalse(gemini_models.is_retired('gemini-3-flash-preview'))
        self.assertTrue(gemini_models.get_model_info('gemini-3.5-flash-lite')['free_tier'])
        self.assertFalse(gemini_models.get_model_info('gemini-3.1-pro-preview')['free_tier'])

    def test_search_429_explains_free_tier_and_preserves_original_error(self):
        original = key_pool._describe_failure(key_pool._classify_gemini(self.api_error(details=[{
            '@type': 'type.googleapis.com/google.rpc.Help', 'links': [{'url': 'https://ai.google.dev/gemini-api/docs/rate-limits'}]}]),
            'gemini-3.5-flash-lite'), 'gemini')
        history = [{'role': 'user', 'parts': [{'text': 'Привет'}]}]
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(gemini_utils, 'GENERATION_LOG_FILE', str(Path(temp_dir) / 'generation.log')), \
             patch.object(key_pool, 'is_configured', return_value=True), \
             patch.object(key_pool, 'call_with_rotation', return_value=(None, original)):
            config = gemini_utils.build_generation_config({'enable_google_search': True}, 'gemini-3.5-flash-lite')
            result, error = gemini_utils.generate_chat_reply_original('gemini-3.5-flash-lite', 'Персонаж', history, config)
            log = Path(gemini_utils.GENERATION_LOG_FILE).read_text(encoding='utf-8')
        self.assertIsNone(result)
        self.assertIn(original, error)
        self.assertIn('выключите', error)
        self.assertIn('Google Search', error)
        self.assertNotIn('RPM', error)
        self.assertIn(error, log)


class SharedGeminiUsageTests(unittest.TestCase):
    """Проверяем реальное файловое резервирование; все ключи и ответы вымышленные."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'usage.json')
        self.addCleanup(patch.stopall)
        patch.object(gemini_quota, 'USAGE_FILE', self.path).start()
        self.key = 'test-quota-key-a'
        self.model = 'gemini-3.8-flash'

    def values(self, **changes):
        result = dict(rpm=5, tpm=250000, rpd=20, requests_today=0, successful_today=0,
                      requests_last_minute=0, input_tokens_last_minute=0)
        result.update(changes)
        return result

    def test_paid_pro_models_do_not_consume_nonexistent_free_quota(self):
        entry = {'id': 'paid', 'label': 'Платный проект', 'key': self.key,
                 'provider': 'gemini', 'enabled': True}
        with patch.object(key_pool, '_keys', [entry]), \
             patch.object(key_pool, '_state', {'paid': key_pool._blank_state()}), \
             patch.object(key_pool, '_cursor', 0), \
             patch.object(key_pool, '_get_client', return_value='client'), \
             patch.object(gemini_quota, 'reserve', side_effect=AssertionError('free quota used')):
            for model in ('gemini-2.5-pro', 'gemini-3.1-pro-preview'):
                self.assertEqual(key_pool.call_with_rotation(
                    lambda _client: 'OK', model=model), ('OK', None))

    def snapshot(self, key=None, model=None):
        rows = gemini_quota.snapshots([{'id': 'a', 'key': key or self.key}])['a']
        return next(row for row in rows if row['model'] == (model or self.model))

    def test_failed_unknown_quota_and_empty_replies_release_all_counters(self):
        key = {'id': 'a', 'label': 'Тест', 'key': self.key, 'provider': 'gemini', 'enabled': True}
        state = {'a': key_pool._blank_state()}
        def reply(text='', thought=False, finish='STOP'):
            return SimpleNamespace(candidates=[SimpleNamespace(finish_reason=finish, content=SimpleNamespace(
                parts=[SimpleNamespace(text=text, thought=thought)]))],
                usage_metadata=SimpleNamespace(prompt_token_count=123))
        responses = [GeminiQuotaTests.api_error(504), GeminiQuotaTests.api_error(), reply(),
                     reply('мысли', thought=True), reply('нет', finish='SAFETY'), reply('ответ')]
        with patch.object(key_pool, '_keys', [key]), patch.object(key_pool, '_state', state), \
             patch.object(key_pool, '_cursor', 0), patch.object(key_pool, '_get_client', return_value=object()):
            for index, response in enumerate(responses):
                def request(_):
                    if isinstance(response, Exception):
                        raise response
                    return response
                key_pool.call_with_rotation(request, model=self.model, try_all_keys_on_error=True,
                                            success_fn=gemini_utils._has_gemini_reply, input_tokens_estimate=50)
                row = self.snapshot()
                success = int(index == len(responses) - 1)
                self.assertEqual((row['requests_today'], row['requests_last_minute'], row['in_flight']),
                                 (success, success, 0))
                self.assertEqual(row['input_tokens_last_minute'], 123 if success else 0)
                self.assertFalse(row['blocked'])
            self.assertEqual(state['a']['success_count'], 1)

    def test_long_reply_enters_minute_counter_on_completion_and_finish_is_idempotent(self):
        with patch.object(gemini_quota.time, 'time', return_value=1000):
            reservation, _ = gemini_quota.reserve(self.key, self.model, 50)
        with patch.object(gemini_quota.time, 'time', return_value=1100):
            gemini_quota.finish(self.key, self.model, reservation, success=True, input_tokens=123)
            gemini_quota.finish(self.key, self.model, reservation, success=True, input_tokens=123)
            row = self.snapshot()
        self.assertEqual((row['requests_today'], row['requests_last_minute'], row['input_tokens_last_minute']),
                         (1, 1, 123))

    def test_v1_attempts_migrate_to_known_successes_without_rewriting_source(self):
        legacy = Path(self.directory.name) / 'old_attempts.json'
        now = gemini_quota.time.time()
        day = gemini_quota._calendar(now)[0]
        row = {'day': day, 'requests_today': 20, 'successful_today': 3,
               'minute': [{'id': 'unknown-attempt', 'at': now, 'tokens': 200}], 'pending': {}}
        raw = json.dumps({'version': 1, 'keys': {gemini_budget.core.fingerprint(self.key): {'models': {self.model: row}}}})
        legacy.write_text(raw, encoding='utf-8')
        gemini_budget.import_legacy(legacy, state_file=self.path)
        current = self.snapshot()
        self.assertEqual((current['requests_today'], current['successful_today'], current['requests_last_minute']), (3, 3, 0))
        self.assertEqual(legacy.read_text(encoding='utf-8'), raw)

    def test_key_copy_is_explicit_non_cached_and_rejects_other_instance_ids(self):
        key = {'id': 'a', 'label': 'Тест', 'key': self.key, 'provider': 'gemini', 'enabled': True}
        with patch.object(key_pool, '_keys', [key]), patch.object(key_pool, '_state', {'a': key_pool._blank_state()}), \
             patch.object(events, 'publish') as publish, main.app.test_client() as client:
            status = client.get('/api/keys').get_data(as_text=True)
            self.assertNotIn(self.key, status)
            response = client.post('/api/keys/copy', json={'key_ids': ['a']})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['values'], {'a': self.key})
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(client.post('/api/keys/copy', json={'key_ids': ['another-instance']}).status_code, 400)
            self.assertEqual(client.get('/api/keys/copy').status_code, 405)
            self.assertEqual(client.post('/api/keys/copy', headers={'Origin': 'https://other.example'},
                                         json={'key_ids': ['a']}).status_code, 403)
            publish.assert_not_called()

    def test_same_key_shares_day_across_ids_and_other_keys_models_are_independent(self):
        gemini_quota.correct(self.key, self.model, self.values(requests_today=19, successful_today=19))
        reservation, error = gemini_quota.reserve(self.key, self.model)
        self.assertIsNone(error)
        gemini_quota.finish(self.key, self.model, reservation, success=True, input_tokens=42)
        rows = gemini_quota.snapshots([{'id': 'other-instance-id', 'key': self.key}])['other-instance-id']
        row = next(row for row in rows if row['model'] == self.model)
        self.assertEqual((row['requests_today'], row['successful_today'], row['remaining_day']), (20, 20, 0))
        self.assertIn('закончились на сегодня', gemini_quota.reserve(self.key, self.model)[1])
        self.assertIsNone(gemini_quota.reserve('different-test-key', self.model)[1])
        self.assertIsNone(gemini_quota.reserve(self.key, 'gemini-3.7-flash')[1])
        self.assertNotIn(self.key, Path(self.path).read_text(encoding='utf-8'))

    def test_minute_input_usage_correction_and_restart_persistence(self):
        with patch.object(gemini_quota.time, 'time', return_value=1000):
            for _ in range(5):
                reservation, error = gemini_quota.reserve(self.key, self.model, 10)
                self.assertIsNone(error)
                gemini_quota.finish(self.key, self.model, reservation, success=True, input_tokens=50)
            row = self.snapshot()
            self.assertEqual((row['remaining_minute'], row['input_tokens_last_minute']), (0, 250))
            self.assertIsNotNone(gemini_quota.reserve(self.key, self.model)[1])
        with patch.object(gemini_quota.time, 'time', return_value=1061):
            row = self.snapshot()
            self.assertEqual((row['remaining_minute'], row['requests_today'], row['successful_today']), (5, 5, 5))
            gemini_quota.correct(self.key, self.model, self.values(requests_today=4, successful_today=4,
                                                                 requests_last_minute=1, input_tokens_last_minute=249990))
            self.assertIn('TPM', gemini_quota.reserve(self.key, self.model, 20)[1])
            self.assertIsNone(gemini_quota.reserve(self.key, self.model, 5)[1])

    def test_server_day_quota_and_manual_correction_clear_shared_pause(self):
        reservation, error = gemini_quota.reserve(self.key, self.model)
        failure = key_pool._classify_gemini(GeminiQuotaTests.api_error(details=[
            GeminiQuotaTests.violation('RequestsPerDay', quotaValue='20')]), self.model)
        gemini_quota.finish(self.key, self.model, reservation, failure=failure)
        self.assertTrue(self.snapshot()['blocked'])
        self.assertEqual(self.snapshot()['remaining_day'], 0)
        self.assertIsNotNone(gemini_quota.reserve(self.key, self.model)[1])
        gemini_quota.correct(self.key, self.model, self.values(requests_today=2, successful_today=1))
        self.assertFalse(self.snapshot()['blocked'])
        self.assertIsNone(gemini_quota.reserve(self.key, self.model)[1])

    def test_manual_zero_limit_disables_until_corrected_without_fake_minute_timer(self):
        row = gemini_quota.correct(self.key, self.model, self.values(rpm=0))
        self.assertTrue(row['blocked'])
        self.assertEqual(row['left_s'], 0)
        self.assertIn('Нулевая квота RPM', gemini_quota.reserve(self.key, self.model)[1])
        gemini_quota.correct(self.key, self.model, self.values())
        self.assertIsNone(gemini_quota.reserve(self.key, self.model)[1])

    def test_pacific_midnight_resets_day_but_keeps_minute(self):
        from datetime import datetime, timezone
        now = datetime(2026, 9, 18, 6, 59, 59, tzinfo=timezone.utc).timestamp()
        with patch.object(gemini_quota.time, 'time', return_value=now):
            gemini_quota.correct(self.key, self.model, self.values(requests_today=20, successful_today=20))
            self.assertEqual(self.snapshot()['remaining_day'], 0)
        with patch.object(gemini_quota.time, 'time', return_value=now + 2):
            row = self.snapshot()
            self.assertEqual((row['requests_today'], row['successful_today'], row['remaining_day']), (0, 0, 20))

    def test_old_daily_error_received_after_midnight_does_not_block_new_day(self):
        from datetime import datetime, timezone
        now = datetime(2026, 9, 18, 6, 59, 59, tzinfo=timezone.utc).timestamp()
        with patch.object(gemini_quota.time, 'time', return_value=now):
            reservation, error = gemini_quota.reserve(self.key, self.model)
        with patch.object(gemini_quota.time, 'time', return_value=now + 2):
            failure = key_pool._classify_gemini(GeminiQuotaTests.api_error(details=[
                GeminiQuotaTests.violation('RequestsPerDay', quotaValue='20')]), self.model)
            gemini_quota.finish(self.key, self.model, reservation, failure=failure)
            row = self.snapshot()
            self.assertFalse(row['blocked'])
            self.assertEqual(row['remaining_day'], 20)

    def test_pacific_reset_without_windows_tzdata_handles_dst_transition_day(self):
        from datetime import datetime, timezone
        now = datetime(2026, 3, 8, 9, tzinfo=timezone.utc).timestamp()
        with patch.object(gemini_budget.core, 'ZoneInfo', side_effect=RuntimeError('нет tzdata')):
            day, reset = gemini_quota._calendar(now)
        self.assertEqual(day, '2026-03-08')
        self.assertEqual(reset, datetime(2026, 3, 9, 7, tzinfo=timezone.utc).timestamp())

    def test_two_processes_cannot_both_take_last_daily_request(self):
        import subprocess
        from concurrent.futures import ThreadPoolExecutor
        gemini_quota.correct(self.key, self.model, self.values(requests_today=19, successful_today=19))
        code = ('import sys,json,gemini_quota; gemini_quota.USAGE_FILE=sys.argv[1]; '
                'print(json.dumps(gemini_quota.reserve(sys.argv[2], "gemini-3.8-flash")))')
        def reserve_in_process(_):
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, self.path, self.key],
                                    capture_output=True, text=True, encoding='utf-8', check=True, timeout=20)
            return json.loads(result.stdout)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve_in_process, range(2)))
        self.assertEqual(sum(result[0] is not None for result in results), 1)
        row = self.snapshot()
        self.assertEqual((row['requests_today'], row['reserved_day'], row['remaining_day']), (19, 1, 0))

    def test_edit_does_not_erase_inflight_request_or_apply_old_error_pause(self):
        reservation, error = gemini_quota.reserve(self.key, self.model, 50)
        gemini_quota.correct(self.key, self.model, self.values())
        row = self.snapshot()
        self.assertEqual((row['requests_today'], row['reserved_day']), (0, 1))
        gemini_quota.finish(self.key, self.model, reservation, success=True, failure={
            'kind': 'quota', 'until': gemini_quota.time.time() + 10000, 'reason': 'old error'})
        row = self.snapshot()
        self.assertEqual((row['successful_today'], row['requests_today'], row['in_flight']), (1, 1, 0))
        self.assertFalse(row['blocked'])

    def test_generation_pool_skips_exhausted_key_and_stores_real_prompt_usage(self):
        keys = [{'id': name, 'label': name, 'key': 'fake-' + name, 'provider': 'gemini', 'enabled': True}
                for name in ('a', 'b')]
        gemini_quota.correct(keys[0]['key'], self.model, self.values(requests_today=20, successful_today=20))
        state = {key['id']: key_pool._blank_state() for key in keys}
        calls = []
        def request(client):
            calls.append(client)
            return SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=123, total_token_count=150))
        with patch.object(key_pool, '_keys', keys), patch.object(key_pool, '_state', state), \
             patch.object(key_pool, '_cursor', 0), patch.object(key_pool, '_get_client', side_effect=lambda k: k['id']):
            result, error = key_pool.call_with_rotation(request, model='models/' + self.model, usage_fn=lambda r: 150,
                                                       try_all_keys_on_error=True, input_tokens_estimate=50)
        self.assertIsNone(error)
        self.assertEqual(calls, ['b'])
        row = self.snapshot(key=keys[1]['key'])
        self.assertEqual((row['requests_today'], row['successful_today'], row['input_tokens_last_minute']), (1, 1, 123))

    def test_table_routes_apply_correction_without_telegram_or_secrets(self):
        keys = [{'id': 'a', 'label': 'Тест', 'key': self.key, 'provider': 'gemini', 'enabled': True}]
        with patch.object(key_pool, '_keys', keys), patch.object(key_pool, '_state', {'a': key_pool._blank_state()}), \
             patch.object(events, 'publish'):
            client = main.app.test_client()
            result = client.post('/api/gemini/usage/correct', json={'key_id': 'a', 'model': self.model,
                'values': self.values(requests_today=20, successful_today=20)})
            self.assertEqual(result.status_code, 200)
            result = client.get('/api/gemini/usage')
            self.assertEqual(result.status_code, 200)
            self.assertNotIn(self.key, result.get_data(as_text=True))
            self.assertEqual(len(result.json['keys'][0]['gemini_usage']),
                             sum(map(gemini_quota.has_free_quota, gemini_quota.CHAT_MODEL_IDS)))
            self.assertIn(self.model, result.json['blocked_models'])
            result = client.post('/api/gemini/usage/correct', json={'key_id': 'a', 'model': self.model,
                'values': self.values(requests_today=3, successful_today=2)})
            self.assertEqual(result.status_code, 200)
            self.assertNotIn(self.model, client.get('/api/gemini/usage').json['blocked_models'])
            self.assertEqual(client.post('/api/gemini/usage/correct', json={'key_id': 'missing',
                'model': self.model, 'values': self.values()}).json['status'], 'error')

    def test_independent_projects_share_user_state_without_importing_this_app(self):
        import subprocess
        package_root = str(Path(gemini_budget.__file__).resolve().parent.parent)
        environment = dict(os.environ, GEMINI_BUDGET_DIR=self.directory.name, PYTHONPATH=package_root)
        self.assertEqual(gemini_budget.state_path().name, 'state.json')
        target = str(Path(self.directory.name) / 'state.json')
        gemini_budget.correct(self.key, self.model, self.values(requests_today=19, successful_today=19),
                              state_file=target)
        code = ('import json,sys,gemini_budget as gb; '
                'rid,error=gb.reserve(sys.argv[1],sys.argv[2]); '
                'gb.finish(sys.argv[1],sys.argv[2],rid,success=True,input_tokens=12); '
                'print(json.dumps({"accepted":rid is not None,"row":gb.status(sys.argv[1],sys.argv[2]),'
                '"app_imported":any(m in sys.modules for m in '
                '("flask","telethon","instance_paths","shared_storage","gemini_quota"))}))')
        results = []
        for project in ('project_a', 'project_b'):
            cwd = Path(self.directory.name) / project
            cwd.mkdir()
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, self.key, self.model],
                                    cwd=cwd, env=environment, capture_output=True, text=True,
                                    encoding='utf-8', check=True, timeout=20)
            results.append(json.loads(result.stdout))
        self.assertEqual([r['accepted'] for r in results], [True, False])
        self.assertEqual([r['row']['remaining_day'] for r in results], [0, 0])
        self.assertFalse(any(r['app_imported'] for r in results))

    def test_external_catalog_copies_defaults_and_returns_shared_key_overrides(self):
        models = gemini_budget.free_models()
        self.assertEqual({row['id'] for row in models},
                         {model for model in gemini_quota.CHAT_MODEL_IDS if gemini_quota.has_free_quota(model)})
        self.assertEqual(len(models), 11)
        self.assertEqual(len(gemini_budget.free_models(task='tts')), 2)
        self.assertEqual(len(gemini_budget.free_models(task='transcription')), 2)
        self.assertEqual(len(gemini_budget.models()), 18)
        models[0]['limits']['rpd'] = 9999
        self.assertEqual(gemini_budget.limits(models[0]['id'])['rpd'], 20)
        gemini_budget.correct(self.key, self.model, self.values(rpm=7, rpd=32), state_file=self.path)
        self.assertEqual(gemini_budget.limits(self.model, self.key, state_file=self.path)['rpm'], 7)
        row = next(r for r in gemini_budget.free_models(self.key, state_file=self.path) if r['id'] == self.model)
        self.assertEqual(row['limits']['rpd'], 32)

    def test_legacy_migration_keeps_usage_and_deduplicates_repeat_and_copied_sources(self):
        import shutil
        legacy = Path(self.directory.name) / 'legacy.json'
        copied = Path(self.directory.name) / 'copied.json'
        gemini_budget.correct(self.key, self.model, self.values(requests_today=6, successful_today=5),
                              state_file=legacy)
        rid, _ = gemini_budget.reserve(self.key, self.model, 10, state_file=legacy)
        gemini_budget.finish(self.key, self.model, rid, success=True, input_tokens=20, state_file=legacy)
        shutil.copyfile(legacy, copied)
        self.assertTrue(gemini_budget.import_legacy(legacy, state_file=self.path))
        self.assertFalse(gemini_budget.import_legacy(legacy, state_file=self.path))
        self.assertTrue(gemini_budget.import_legacy(copied, state_file=self.path))
        row = self.snapshot()
        self.assertEqual((row['requests_today'], row['successful_today'], row['requests_last_minute']), (7, 7, 1))
        self.assertEqual(row['input_tokens_last_minute'], 20)
        self.assertTrue(legacy.exists())
        self.assertNotIn(self.key, Path(self.path).read_text(encoding='utf-8'))

    def test_adapter_migrates_old_project_ledger_into_default_shared_state(self):
        directory = Path(self.directory.name) / 'global'
        target = str(directory / 'state.json')
        legacy = Path(self.directory.name) / 'legacy.json'
        gemini_budget.correct(self.key, self.model, self.values(requests_today=11, successful_today=10),
                              state_file=legacy)
        with patch.dict(os.environ, {'GEMINI_BUDGET_DIR': str(directory)}), \
             patch.object(gemini_quota, 'USAGE_FILE', target), \
             patch.object(gemini_quota, 'LEGACY_FILE', str(legacy)):
            row = self.snapshot()
            self.assertEqual((row['requests_today'], row['remaining_day']), (11, 9))
            gemini_quota.correct(self.key, self.model, self.values(requests_today=3, successful_today=2))
            self.assertEqual(self.snapshot()['requests_today'], 3)


class AutoFallbackTests(unittest.TestCase):
    def test_transport_deadline_is_forwarded_to_both_providers(self):
        history = [{'role': 'user', 'parts': [{'text': 'привет'}]}]
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(gemini_utils, 'GENERATION_LOG_FILE', str(Path(temp_dir) / 'generation.log')), \
             patch.object(key_pool, 'is_configured', return_value=True), \
             patch.object(gemini_utils, '_check_openai_budget', return_value=None), \
             patch.object(providers, 'budget', return_value=None):
            client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: None),
                responses=SimpleNamespace(create=lambda **kw: None))
            captured = []
            def rotation(fn, **kw):
                fn(client)
                self.assertNotIn('request_timeout_s', kw)
                return None, 'test-no-api'
            client.models.generate_content = lambda **kw: captured.append(kw['config'].http_options)
            client.responses.create = lambda **kw: captured.append(kw['timeout'])
            with patch.object(key_pool, 'call_with_rotation', side_effect=rotation):
                for model in ('gemini-3.8-flash', 'gpt-5.4-mini'):
                    gemini_utils.generate_chat_reply_original(model, 'роль', history, request_timeout_s=17)
            self.assertEqual(captured[0].timeout, 17000)
            self.assertEqual(captured[0].retry_options.attempts, 1)
            self.assertEqual(captured[1], 17)

    def test_custom_order_restarts_at_first_model_and_falls_back_after_all_keys(self):
        import threading
        calls = []
        keys = [{'id': name, 'label': name, 'key': 'fake-' + name, 'provider': 'gemini', 'enabled': True}
                for name in ('a', 'b')]
        state = {key['id']: key_pool._blank_state() for key in keys}
        current_model = ['']
        history = [{'role': 'user', 'parts': [{'text': 'Привет'}]}]
        def request(client):
            calls.append((current_model[0], client))
            if current_model[0] == 'gemini-first':
                raise GeminiQuotaTests.api_error(503, message='High capacity')
            return 'ответ'
        def generate_fn(**kw):
            current_model[0] = kw['model_name']
            return key_pool.call_with_rotation(request, model=kw['model_name'],
                try_all_keys_on_error=kw['try_all_keys_on_error'], stop_event=kw['stop_event'])
        with patch.object(key_pool, '_keys', keys), patch.object(key_pool, '_state', state), \
             patch.object(key_pool, '_cursor', 0), patch.object(key_pool, '_get_client', side_effect=lambda k: k['id']), \
             patch.object(events, 'publish'):
            for _ in range(2):
                result = model_fallbacks.generate({'auto_fallback_enabled': True,
                    'auto_fallback_models': ['gemini-first', 'gemini-second']}, 'ignored', 'роль', history,
                    100, lambda *a, **kw: None, threading.Event(), generate_fn, gemini_utils.build_generation_config)
                self.assertEqual(result, ('ответ', None, 'gemini-second'))
        self.assertEqual(calls, [('gemini-first', 'a'), ('gemini-first', 'b'), ('gemini-second', 'a'),
                                 ('gemini-first', 'b'), ('gemini-first', 'a'), ('gemini-second', 'b')])

    def test_free_openai_budget_skips_large_and_uses_mini_even_when_paid_overage_on(self):
        import threading
        attempted = []
        budget = SimpleNamespace(estimate_tokens=lambda *a, **kw: 2000,
            can_spend=lambda model, estimate: (model != 'gpt-5.4', {'reason': 'нет запаса бесплатных токенов'}))
        def generate_fn(**kw):
            attempted.append((kw['model_name'], kw['config']))
            return 'готово', None
        with patch.object(providers, 'budget', return_value=budget), patch.object(events, 'publish'):
            result = model_fallbacks.generate({'auto_fallback_enabled': True,
                'auto_fallback_models': ['gpt-5.4', 'gpt-5.4-mini'], 'allow_paid_overage': True}, 'ignored',
                'роль', [{'role': 'user', 'parts': [{'text': 'привет'}]}], 100, lambda *a, **kw: None,
                threading.Event(), generate_fn, gemini_utils.build_generation_config)
        self.assertEqual(result, ('готово', None, 'gpt-5.4-mini'))
        self.assertEqual(len(attempted), 1)
        self.assertFalse(attempted[0][1]['allow_paid_overage'])
        self.assertTrue(attempted[0][1]['strict_free_budget'])

    def test_all_errors_empty_replies_and_stop_never_stall_inside_chain(self):
        import threading
        stop = threading.Event()
        calls = []
        def generate_fn(**kw):
            calls.append(kw['model_name'])
            if len(calls) == 1: raise RuntimeError('ошибка')
            return '', None
        settings = {'auto_fallback_enabled': True, 'auto_fallback_models': ['gemini-a', 'gemini-b']}
        with patch.object(events, 'publish'):
            result = model_fallbacks.generate(settings, 'ignored', 'роль', [], 100,
                lambda *a, **kw: None, stop, generate_fn, gemini_utils.build_generation_config)
            self.assertIn('Все модели цепочки', result[1])
            stop.set()
            model_fallbacks.generate(settings, 'ignored', 'роль', [], 100,
                lambda *a, **kw: None, stop, generate_fn, gemini_utils.build_generation_config)
        self.assertEqual(calls, ['gemini-a', 'gemini-b'])
        self.assertEqual(model_fallbacks.parse_chain('gpt-5.4-mini\ngemini-3.8-flash\ngpt-5.4-mini'),
                         ['gpt-5.4-mini', 'gemini-3.8-flash', 'gpt-5.4-mini'])


if __name__ == '__main__':
    unittest.main()
