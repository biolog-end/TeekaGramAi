"""Поднимает веб-интерфейс на фейковых данных, не трогая Telegram.

Запуск:  python tools/preview.py [--port 5099]

Подменяет run_in_telegram_loop везде, где он импортирован по имени, и отдаёт
правдоподобные фикстуры по имени корутины. Нужен для скриншотов и проверки
вёрстки: юзербот — живой аккаунт, тестовый трафик к Telegram недопустим.
"""
import os
import sys
import argparse
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault('TELAGRAMM_API_ID', '1')
os.environ.setdefault('TELAGRAMM_API_HASH', 'preview')
os.environ.setdefault('INSTANCE_NUMBER', '1')

import telegram_utils  # noqa: E402
import web_routes  # noqa: E402
import bot_logic  # noqa: E402
import key_pool  # noqa: E402

# Ключи читаем настоящие (наружу уходят только маски), чтобы модалка и список
# моделей выглядели как в приложении. Telegram при этом не трогается.
key_pool.load_keys()

# Скриншоты должны быть воспроизводимы и не ждать внешние каталоги моделей.
_real_model_options = web_routes._model_options
web_routes._model_options = lambda: _real_model_options(include_live=False)

NAMES = ['Соня', 'Махир', 'Кадзу', 'Рабочий чат', 'Аня Петрова', 'Дядя Витя', 'Книжный клуб',
         'Лера', 'Семейный', 'Игорь', 'Дизайн-команда', 'Настя', 'Марк', 'Общага 4 этаж',
         'Тимур', 'Оля', 'Саша К.', 'Проект X', 'Мама', 'Даня', 'Курс по японскому',
         'Женя', 'Влад', 'Кино на выходных', 'Лиза', 'Артём', 'Костя', 'Заказчик', 'Юля', 'Никита']

PREVIEWS = ['ок, давай завтра', '[Стикер]', 'ахахах', 'скинь ссылку плз', '[Фото]',
            'Ну это как посмотреть, если честно, я бы не стал так делать, слишком рискованно']


def fake_chats():
    now = datetime.now()
    out = []
    for i, name in enumerate(NAMES):
        is_group = i % 4 == 3
        cid = -(100000 + i) if is_group else 1000 + i
        dt = (now - timedelta(hours=i * 9)).astimezone()
        out.append({
            'id': cid, 'name': name, 'is_group': is_group,
            'photo_id': 5 if i in (0, 3) else None,
            'preview': PREVIEWS[i % 6],
            'date_label': telegram_utils._dialog_date_label(dt),
            'unread': [0, 3, 0, 12, 0, 1][i % 6],
        })
    return out


def fake_history(chat_id):
    group = chat_id < 0
    base = datetime.now() - timedelta(hours=2)

    def ts(i):
        return (base + timedelta(minutes=i * 3)).strftime('%Y-%m-%d %H:%M:%S')

    def nick(n):
        return f'<ник:{n}> ' if group else ''

    # Формат в точности как у get_formatted_history: реакции — перед префиксом,
    # id и время — на отдельных строках.
    def user(i, text, n='Соня', lead=''):
        return {'role': 'user', 'parts': [{'text': f'{lead}(ID: {100 + i}) \n[{ts(i)}]\n{nick(n)}{text}'}]}

    def model(i, text):
        return {'role': 'model', 'parts': [{'text': f'[{ts(i)}]\n{text}'}]}

    return [
        user(0, 'привет) ты тут?'),
        model(1, 'тут\n{split}\nчто случилось'),
        user(2, 'да ничего, скучно просто'),
        user(3, 'смотри что нашла', 'Махир', lead='react(101)[🔥]\n'),
        {'role': 'user', 'parts': [{'text': f'(ID: 104) \n[{ts(4)}]\n{nick("Махир")}'},
                                   {'type': 'media_placeholder', 'chat_id': chat_id, 'message_id': 104}]},
        model(5, 'answer(104)\nо, прикольно'),
        # Vision-режим: превью рядом с текстом — и карантинный стикер, который модель видит только текстом.
        {'role': 'user', 'parts': [{'text': f'(ID: 106) \n[{ts(6)}]\n{nick("Соня")}sticker(id=5228964907256384900, codename=menhera_chan_fun)'},
                                   {'type': 'sticker_preview', 'sticker_id': 5228964907256384900, 'quarantined': False}]},
        model(7, 'sticker(fun_dance_sticker)'),
        {'role': 'user', 'parts': [{'text': f'(ID: 107) \n[{ts(7)}]\n{nick("Махир")}sticker(id=123456789)'},
                                   {'type': 'sticker_preview', 'sticker_id': 123456789, 'quarantined': True}]},
        user(8, 'кстати, ' + 'длинное сообщение про то, как прошёл день, ' * 6 + 'вот.'),
        model(9, 'звучит как нормальный день\n{split}\nя бы тоже так провёл'),
        user(10, '[Голосовое сообщение] - не удалось загрузить.'),
        model(11, 'не слышно, напиши текстом'),
        user(12, 'лан, потом'),
    ]


def fake_run_in_telegram_loop(coro, timeout=60):
    name = getattr(coro, '__name__', '')
    frame = coro.cr_frame
    args = dict(frame.f_locals) if frame else {}
    coro.close()
    if name == 'wait_for_chat_send_slot':
        return {'ready': True, 'waited': False}, None
    if name == 'get_chats':
        return fake_chats(), None
    if name == 'get_chat_info':
        cid = args.get('chat_id', 0)
        match = next((c for c in fake_chats() if c['id'] == cid), None)
        return ({'id': cid, 'name': match['name'] if match else f'Чат {cid}',
                 'photo_id': match['photo_id'] if match else None}, None)
    if name == 'get_formatted_history':
        return fake_history(args.get('chat_id', 1)), None
    if name == 'get_avatar':
        return ('static/favicons/favicon1.ico', None) if args.get('photo_id') == 5 else (None, None)
    if name == 'get_media_for_message':
        return None, 'preview: нет медиа'
    if name == 'get_sticker_preview':
        return _fake_sticker_jpg()
    if name == 'find_stickers_in_chat':
        candidates = [
            {'id': '9007199254740993', 'set_id': '701', 'set_title': 'Menhera-chan reactions',
             'set_short_name': 'menhera_chan_pack', 'emoji': '🥺', 'media_available': True},
            {'id': '9007199254740995', 'set_id': '702', 'set_title': 'Коты на каждый день',
             'set_short_name': 'daily_cats', 'emoji': '😼', 'media_available': True},
        ]
        return {'found': 2, 'media_ready': 2, 'candidates': candidates}, None
    if name == 'describe_from_chat':
        return {
            'found': 2, 'described': 2, 'quarantined': 0, 'model': 'gemini-3.8-flash',
            'requests': 1, 'media_sent': 2, 'reference_examples': 12, 'errors': [],
            'added_packs': [{'name': 'daily_cats', 'description': 'Коты и их реакции',
                             'created': True}],
            'added_stickers': [
                {'sticker_id': '9007199254740993', 'codename': 'menhera_chan_pleading',
                 'description': 'умоляюще смотрит', 'program_pack': 'menhera_chan',
                 'telegram_set_id': '701', 'telegram_pack_title': 'Menhera-chan reactions',
                 'telegram_pack_short_name': 'menhera_chan_pack', 'emoji': '🥺'},
                {'sticker_id': '9007199254740995', 'codename': 'daily_cats_smug',
                 'description': 'самодовольно улыбается', 'program_pack': 'daily_cats',
                 'telegram_set_id': '702', 'telegram_pack_title': 'Коты на каждый день',
                 'telegram_pack_short_name': 'daily_cats', 'emoji': '😼'},
            ],
        }, None
    return None, f'preview stub: {name} не поддержан'


def _fake_sticker_jpg():
    """Одна и та же жёлтая заглушка вместо любого превью — чтобы видеть вёрстку картинок."""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), 'teeka_preview_sticker.jpg')
    if not os.path.exists(path):
        try:
            from PIL import Image, ImageDraw
            img = Image.new('RGB', (256, 256), (242, 183, 5))
            ImageDraw.Draw(img).ellipse((48, 48, 208, 208), fill=(31, 27, 22))
            img.save(path, 'JPEG')
        except Exception:
            return None
    return path


for module in (telegram_utils, web_routes, bot_logic):
    module.run_in_telegram_loop = fake_run_in_telegram_loop

# Без активного персонажа страница чата прячет редактор персонажа — подставляем
# первого из data/characters.json, если в настройках чата никто не выбран.
_real_get_chat_settings = web_routes.get_chat_settings


def _get_chat_settings_with_character(chat_id):
    settings = _real_get_chat_settings(chat_id)
    if not settings.get('active_character_id'):
        characters = web_routes.character_utils.load_characters()
        if characters:
            settings['active_character_id'] = next(iter(characters))
    return settings


web_routes.get_chat_settings = _get_chat_settings_with_character

# Карантин в настоящей базе обычно пуст — подкладываем три карточки, чтобы видеть вёрстку.
web_routes.sticker_store.list_quarantined = lambda: [
    {'sticker_id': 111, 'codename': 'raw_111', 'set_id': 1, 'description': ''},
    {'sticker_id': 222, 'codename': 'cat_sleepy', 'set_id': 1, 'description': 'кот засыпает на клавиатуре'},
    {'sticker_id': 333, 'codename': 'raw_333', 'set_id': 2, 'description': ''},
]
# И один «текстовый» стикер в активных — чтобы видеть пометку карантина в списке.
_real_structure = web_routes.structure_sticker_data


def _structure_with_quarantine_mark(db):
    result = _real_structure(db)
    for group in result:
        for item in group['stickers']:
            if item['codename'].endswith('fun'):
                item['quarantined_count'] = 1
    return result


web_routes.structure_sticker_data = _structure_with_quarantine_mark

import main  # noqa: E402  — регистрирует маршруты; main() не вызывается

from flask import request  # noqa: E402

# PREVIEW_NO_SSE=1: поток событий сразу закрывается. Нужно для скриншотов с
# --virtual-time-budget — иначе Chrome ждёт вечно открытый EventSource и виснет.
if os.environ.get('PREVIEW_NO_SSE'):
    main.app.view_functions['events_stream'] = lambda chat_id: ('', 204)

# ?modal=<id> открывает модалку, ?tab=<data-tab> переключает вкладку, ?click=<element id>
# нажимает кнопку через 300 мс (для скриншотов состояний, которые появляются по клику).
MODAL_OPENER = ('<script>document.addEventListener("DOMContentLoaded",function(){'
                'var q=new URLSearchParams(location.search);App.openModal(q.get("modal"));'
                'var t=q.get("tab");var b=t&&document.querySelector(\'.tab[data-tab="\'+t+\'"]\');'
                'if(b)b.click();'
                'var c=q.get("click");var cb=c&&document.getElementById(c);'
                'if(cb)setTimeout(function(){cb.click();},300);});</script>')


@main.app.after_request
def _open_modal_for_screenshot(response):
    """?modal=<id> раскрывает модалку сразу — чтобы снять её headless-браузером."""
    if request.args.get('modal') and response.mimetype == 'text/html':
        html = response.get_data(as_text=True).replace('</body>', MODAL_OPENER + '</body>')
        response.set_data(html)
    return response


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5099)
    args = parser.parse_args()
    print(f'Preview: http://127.0.0.1:{args.port}/  (Telegram не используется)')
    main.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False, threaded=True)
