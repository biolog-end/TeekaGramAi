# File: character_utils.py
import os
import re
import json
import logging
import uuid
from datetime import datetime

from gemini_utils import generate_chat_reply_original # Импортируем функцию генерации

# --- НАЧАЛО: НОВЫЙ ФАЙЛ И НОВЫЕ ФУНКЦИИ ---

CHARACTERS_FILE = 'data/characters.json'
CHAT_CHARACTER_MAP_FILE = 'data/chat_character_map.json'

# Дефолтные значения для нового персонажа
DEFAULT_MEMORY_UPDATE_PROMPT = """
Твоя задача - обновить память персонажа.
Проанализируй предоставленную историю переписки. Не упоминай старую память.
Основываясь на личности персонажа и новых сообщениях, очень кратко, в 1-2 предложениях, опиши самое важное, что персонаж запомнил бы из этого диалога.
Пиши только о новых событиях. Формулируй от первого лица, как будто это воспоминание персонажа.

- Личность персонажа: {character_personality}
- Его прошлая память: {character_past_memory}
- История переписки из чата "{chat_name}" ({chat_type}):
"""

DEFAULT_SYSTEM_COMMANDS_PROMPT = """
Чтобы написать сразу несколько коротких сообщений, разделяй их используя ключевое слово {split}
Чтобы твоё сообщение было ответом на сообщение собеседника пиши в начале сообщения ключевое слово answer(ID реального сообщения собеседника на которое хочешь дать свой ответ)
Чтобы отправить стикер, используй команду sticker(кодовое_имя_из_списка_ниже).

Доступные стикеры:
(сюда будут подставляться доступные стикеры персонажа)
"""

def load_characters():
    """Загружает всех персонажей из JSON файла."""
    try:
        if os.path.exists(CHARACTERS_FILE):
            with open(CHARACTERS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Ошибка чтения файла персонажей '{CHARACTERS_FILE}': {e}")
        return {}

def save_characters(characters_data):
    """Сохраняет всех персонажей в JSON файл."""
    try:
        with open(CHARACTERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(characters_data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        logging.error(f"Ошибка сохранения файла персонажей '{CHARACTERS_FILE}': {e}")
        return False

def get_character(character_id):
    """Возвращает данные одного персонажа по его ID."""
    characters = load_characters()
    return characters.get(character_id)

def create_new_character(name="Новый персонаж"):
    """Создает нового персонажа с дефолтными настройками и возвращает его ID."""
    characters = load_characters()
    new_id = str(uuid.uuid4())
    
    characters[new_id] = {
        "name": name,
        "personality_prompt": "Это личность нового персонажа. Опиши его характер, манеру речи, знания.",
        "memory_prompt": "# Начало памяти персонажа\n",
        "system_commands_prompt": DEFAULT_SYSTEM_COMMANDS_PROMPT,
        "memory_update_prompt": DEFAULT_MEMORY_UPDATE_PROMPT,
        # ДОБАВЛЕНО: Поля для персональных настроек
        "enabled_sticker_packs": [], 
        "advanced_settings": {} # Пустой словарь означает "использовать глобальные дефолты"
    }
    
    if save_characters(characters):
        logging.info(f"Создан новый персонаж '{name}' с ID: {new_id}")
        return new_id
    else:
        logging.error("Не удалось сохранить нового персонажа.")
        return None
    
def update_character_memory(character_id: str, chat_name: str, is_group: bool, chat_history: list):
    """
    Основная функция для обновления памяти.
    Она генерирует новое воспоминание и добавляет его к `memory_prompt` персонажа.
    """
    logging.info(f"Запуск обновления памяти для персонажа {character_id} из чата '{chat_name}'")
    
    character_data = get_character(character_id)
    if not character_data:
        return None, "Персонаж не найден."

    # Собираем промпт для summarizer-модели
    summarizer_system_prompt = character_data.get('memory_update_prompt', DEFAULT_MEMORY_UPDATE_PROMPT)
    
    # Подставляем нужные значения в шаблон промпта
    final_summarizer_prompt = summarizer_system_prompt.format(
        character_personality=character_data.get('personality_prompt', ''),
        character_past_memory=character_data.get('memory_prompt', ''),
        chat_name=chat_name,
        chat_type="группа" if is_group else "личный чат"
    )

    # Вызываем Gemini для генерации краткого воспоминания
    # Используем специальную быструю модель, как ты и просил
    new_memory_entry, error = generate_chat_reply_original(
        model_name="gemini-1.5-flash-latest", # Используем быструю и дешевую модель для этой задачи
        system_prompt=final_summarizer_prompt,
        chat_history=chat_history
    )

    if error:
        logging.error(f"Ошибка при генерации воспоминания: {error}")
        return None, f"Ошибка модели-суммаризатора: {error}"

    if not new_memory_entry or not new_memory_entry.strip():
        logging.warning("Модель-суммаризатор вернула пустой ответ. Память не обновлена.")
        return None, "Модель-суммаризатор не сгенерировала текст."

    # Форматируем и добавляем новое воспоминание
    characters = load_characters()
    
    timestamp = datetime.now().strftime("%Y-%m-%d")
    chat_type_str = "в группе" if is_group else "с"
    
    # Новая строка для файла памяти
    formatted_entry = f"\n- {timestamp}, переписка {chat_type_str} {chat_name}: {new_memory_entry.strip()}"
    
    # Добавляем в конец промпта памяти
    characters[character_id]['memory_prompt'] += formatted_entry
    
    if save_characters(characters):
        logging.info(f"Память персонажа {character_id} успешно обновлена.")
        return characters[character_id]['memory_prompt'], None
    else:
        logging.error("Не удалось сохранить обновленную память персонажа.")
        return None, "Ошибка сохранения файла персонажа."

def get_full_prompt_for_character(character_id: str, sticker_prompt_text: str = ""):
    """Собирает итоговый системный промпт для персонажа из всех его частей."""
    character_data = get_character(character_id)
    if not character_data:
        return ""
        
    # Обновляем текст со стикерами в системном промпте команд
    commands_prompt = character_data.get('system_commands_prompt', DEFAULT_SYSTEM_COMMANDS_PROMPT)
    if sticker_prompt_text:
        # Это заменит плейсхолдер или добавит/обновит блок со стикерами
        if "Доступные стикеры:" in commands_prompt:
             commands_prompt = re.sub(r'Доступные стикеры:.*', f"Доступные стикеры:\n{sticker_prompt_text}", commands_prompt, flags=re.DOTALL)
        else:
             commands_prompt += f"\n\nДоступные стикеры:\n{sticker_prompt_text}"

    # Собираем все части в один большой промпт
    full_prompt = (
        f"{character_data.get('personality_prompt', '')}\n\n"
        f"### Память персонажа (давние и недавние события):\n"
        f"{character_data.get('memory_prompt', '')}\n\n"
        f"### Системные инструкции и команды:\n"
        f"{commands_prompt}"
    )
    
    return full_prompt.strip()

# --- КОНЕЦ: НОВЫЙ ФАЙЛ И НОВЫЕ ФУНКЦИИ ---