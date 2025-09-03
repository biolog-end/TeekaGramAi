#!/usr/bin/env python3
"""
TeekaGramAi - Демонстрационный сервер
=====================================
Упрощенная версия для показа веб-интерфейса без Telegram подключения
"""

import os
import json
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.routing import BaseConverter

# Создаем mock данные для демонстрации
class MockChat:
    def __init__(self, id, name):
        self.id = id
        self.name = name

class MockChatInfo:
    def __init__(self, name):
        self.name = name

class SignedIntConverter(BaseConverter):
    regex = r'-?\d+'
    def to_python(self, value):
        return int(value)
    def to_url(self, value):
        return str(value)

app = Flask(__name__)
app.url_map.converters['sint'] = SignedIntConverter
app.secret_key = 'demo_secret_key'

# Mock данные
DEMO_CHATS = [
    MockChat(-1001234567890, "🤖 AI Development Group"),
    MockChat(-1001234567891, "💬 General Chat"),
    MockChat(1234567890, "👨‍💻 John Developer"),
    MockChat(1234567891, "👩‍🎨 Jane Designer"),
    MockChat(1234567892, "🧑‍🔬 Alex Scientist")
]

DEMO_CHAT_SETTINGS = {
    "num_messages_to_fetch": 65,
    "can_see_photos": True,
    "can_see_videos": True,
    "can_see_audio": True,
    "can_see_files_pdf": True,
    "auto_mode_check_interval": 3.5,
    "auto_mode_initial_wait": 6.0,
    "auto_mode_no_reply_timeout": 4.0,
    "auto_mode_no_reply_suffix": "\n\n(Тебе давно не отвечали. Вежливо поинтересуйся, все ли в порядке или почему молчат.)",
    "sticker_choosing_delay_min": 2.0,
    "sticker_choosing_delay_max": 5.5,
    "typing_delay_ms_min": 40.0,
    "typing_delay_ms_max": 90.0,
    "base_thinking_delay_s_min": 1.2,
    "base_thinking_delay_s_max": 2.8,
    "max_typing_duration_s": 25.0,
}

DEMO_HISTORY = [
    {
        'role': 'user',
        'parts': [{'text': 'Привет! Как дела? 👋'}]
    },
    {
        'role': 'model',
        'parts': [{'text': 'Привет! Дела отлично! 😊 Работаю над новыми функциями для TeekaGramAi. А как у тебя дела?'}]
    },
    {
        'role': 'user',
        'parts': [{'text': 'Круто! А что за новые функции?'}]
    },
    {
        'role': 'model',
        'parts': [{'text': 'Сейчас добавляю поддержку голосовых сообщений и улучшаю систему персонажей. Скоро будет еще круче! 🎉'}]
    }
]

DEMO_STICKER_PACKS = [
    {'codename': 'AnimatedEmojies', 'enabled': True},
    {'codename': 'PepeCollection', 'enabled': False},
    {'codename': 'CatStickers', 'enabled': True},
    {'codename': 'TechMemes', 'enabled': False}
]

DEMO_CHARACTERS = {
    'char_001': {
        'name': 'Анна Консультант',
        'personality_prompt': 'Ты дружелюбный и профессиональный консультант по технологиям. Любишь помогать людям.',
        'memory_prompt': '- Пользователь интересуется Python\n- Предпочитает краткие объяснения',
        'system_commands_prompt': 'Используй эмодзи для выделения важных моментов',
        'memory_update_prompt': 'Анализируй диалог и обновляй память персонажа',
        'enabled_sticker_packs': ['AnimatedEmojies', 'CatStickers']
    }
}

@app.route('/')
def index():
    return render_template('index.html', chats=DEMO_CHATS)

@app.route('/select_chat', methods=['POST'])
def select_chat():
    chat_id = request.form.get('chat_id')
    if chat_id:
        return redirect(url_for('chat_page', chat_id=int(chat_id)))
    flash('Выберите чат', 'error')
    return redirect(url_for('index'))

@app.route('/chat/<sint:chat_id>')
def chat_page(chat_id):
    # Найдем чат по ID
    chat_info = None
    for chat in DEMO_CHATS:
        if chat.id == chat_id:
            chat_info = MockChatInfo(chat.name)
            break
    
    if not chat_info:
        chat_info = MockChatInfo(f"Demo Chat {chat_id}")
    
    current_limit = request.args.get('limit', 50, type=int)
    
    return render_template('chat.html',
        chat_id=chat_id,
        chat_info=chat_info,
        history=DEMO_HISTORY,
        current_limit=current_limit,
        generation_mode='character',
        loaded_system_prompt='Ты умный AI-ассистент для Telegram.',
        default_model_name='gemini-pro',
        all_characters=DEMO_CHARACTERS,
        active_character_id='char_001',
        active_character_data=DEMO_CHARACTERS['char_001'],
        sticker_packs=DEMO_STICKER_PACKS,
        sticker_prompt_text_for_js='Используй стикеры AnimatedEmojies и CatStickers',
        chat_settings=DEMO_CHAT_SETTINGS,
        auto_mode_status='inactive'
    )

@app.route('/generate/<sint:chat_id>', methods=['POST'])
def generate_reply(chat_id):
    # Демо генерация ответа
    demo_replies = [
        "Это демонстрационный ответ от TeekaGramAi! 🤖",
        "Привет! Я работаю в демо-режиме, но интерфейс полностью функциональный! ✨",
        "В реальной версии здесь будет ответ от Gemini AI на основе контекста чата 🧠",
        "Попробуй разные настройки в продвинутых параметрах! 🛠️"
    ]
    
    import random
    generated_reply = random.choice(demo_replies)
    flash('Ответ сгенерирован в демо-режиме!', 'success')
    
    return render_template('chat.html',
        chat_id=chat_id,
        chat_info=MockChatInfo(f"Demo Chat {chat_id}"),
        history=DEMO_HISTORY,
        current_limit=50,
        generation_mode=request.form.get('mode', 'character'),
        loaded_system_prompt='Ты умный AI-ассистент для Telegram.',
        default_model_name='gemini-pro',
        all_characters=DEMO_CHARACTERS,
        active_character_id='char_001',
        active_character_data=DEMO_CHARACTERS['char_001'],
        sticker_packs=DEMO_STICKER_PACKS,
        sticker_prompt_text_for_js='Используй стикеры AnimatedEmojies и CatStickers',
        chat_settings=DEMO_CHAT_SETTINGS,
        auto_mode_status='inactive',
        generated_reply=generated_reply
    )

@app.route('/send/<sint:chat_id>', methods=['POST'])
def send_reply(chat_id):
    message = request.form.get('message_to_send', '')
    if message:
        flash('В демо-режиме сообщения не отправляются в Telegram', 'info')
    return redirect(url_for('chat_page', chat_id=chat_id))

# Mock routes для других функций
@app.route('/set_generation_mode/<sint:chat_id>', methods=['POST'])
def set_generation_mode(chat_id):
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/save_settings/<sint:chat_id>', methods=['POST'])
def save_chat_settings_route(chat_id):
    flash('Настройки сохранены в демо-режиме!', 'success')
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/auto_mode/start/<sint:chat_id>', methods=['POST'])
def start_auto_mode(chat_id):
    flash('Авто-режим активирован в демо-версии!', 'success')
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/auto_mode/stop/<sint:chat_id>', methods=['POST'])
def stop_auto_mode(chat_id):
    flash('Авто-режим остановлен в демо-версии!', 'info')
    return redirect(url_for('chat_page', chat_id=chat_id))

# Дополнительные mock routes
@app.route('/save_prompt/<sint:chat_id>', methods=['POST'])
def save_prompt(chat_id):
    flash('Промпт сохранен в демо-режиме!', 'success')
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/set_active_character/<sint:chat_id>', methods=['POST'])
def set_active_character(chat_id):
    flash('Персонаж выбран в демо-режиме!', 'success')
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/create_character', methods=['POST'])
def create_character():
    flash('Персонаж создан в демо-режиме!', 'success')
    return redirect(url_for('index'))

@app.route('/save_character/<character_id>', methods=['POST'])
def save_character(character_id):
    flash('Данные персонажа сохранены в демо-режиме!', 'success')
    return redirect(url_for('index'))

@app.route('/update_memory/<sint:chat_id>', methods=['POST'])
def update_memory_route(chat_id):
    flash('Память персонажа обновлена в демо-режиме!', 'success')
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/update_sticker/<sint:chat_id>', methods=['POST'])
def update_sticker_status(chat_id):
    flash('Статусы стикеров обновлены в демо-режиме!', 'success')
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/reset_settings/<sint:chat_id>', methods=['POST'])
def reset_chat_settings_route(chat_id):
    flash('Настройки сброшены к значениям по умолчанию!', 'info')
    return redirect(url_for('chat_page', chat_id=chat_id))

if __name__ == '__main__':
    print("🚀 Запуск TeekaGramAi в демонстрационном режиме...")
    print("📱 Веб-интерфейс будет доступен по адресу: http://0.0.0.0:5000")
    print("🎭 Все функции работают в демо-режиме без подключения к Telegram")
    app.run(host='0.0.0.0', port=5000, debug=True)