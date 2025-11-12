import os
import json
import logging
import uuid
from datetime import datetime

from gemini_utils import generate_chat_reply_original 


CHARACTERS_FILE = 'data/characters.json'
CHAT_CHARACTER_MAP_FILE = 'data/chat_character_map.json'

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
Чтобы поставить реакцию на сообщение собеседника, используй команду react(ID реального сообщения собеседника на которое хочешь дать свою реакцию)[эмодзи из этого списка '👍', '❤️', '🔥', '🎉', '🤩', '😱', '😁', '😢', '🤔', '👎', '💩', '👌', '😈', '😨', '🕊', '🤬', '🤡', '😐', '🤝', '💯', '🥰', '🤮', '🦄', '😎', '💘', '👾']
Ты можешь поставить реакции сразу на несколько сообщений написав несколько команд подряд
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
        "enabled_sticker_packs": [], 
        "advanced_settings": {} 
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

    summarizer_system_prompt = character_data.get('memory_update_prompt', DEFAULT_MEMORY_UPDATE_PROMPT)
    
    final_summarizer_prompt = summarizer_system_prompt.format(
        character_personality=character_data.get('personality_prompt', ''),
        character_past_memory=character_data.get('memory_prompt', ''),
        chat_name=chat_name,
        chat_type="группа" if is_group else "личный чат"
    )

    new_memory_entry, error = generate_chat_reply_original(
        model_name="gemini-2.5-flash-lite", 
        system_prompt=final_summarizer_prompt,
        chat_history=chat_history
    )

    if error:
        logging.error(f"Ошибка при генерации воспоминания: {error}")
        return None, f"Ошибка модели-суммаризатора: {error}"

    if not new_memory_entry or not new_memory_entry.strip():
        logging.warning("Модель-суммаризатор вернула пустой ответ. Память не обновлена.")
        return None, "Модель-суммаризатор не сгенерировала текст."

    characters = load_characters()
    
    timestamp = datetime.now().strftime("%Y-%m-%d")
    chat_type_str = "в группе" if is_group else "с"
    
    formatted_entry = f"\n- {timestamp}, переписка {chat_type_str} {chat_name}: {new_memory_entry.strip()}"
    
    characters[character_id]['memory_prompt'] += formatted_entry
    
    if save_characters(characters):
        logging.info(f"Память персонажа {character_id} успешно обновлена.")
        return characters[character_id]['memory_prompt'], None
    else:
        logging.error("Не удалось сохранить обновленную память персонажа.")
        return None, "Ошибка сохранения файла персонажа."

def get_full_prompt_for_character(character_id: str, chat_name: str = None, is_group: bool = False, chat_context_prompt: str = None):
    """
    Собирает итоговый системный промпт для персонажа из всех его частей,
    добавляя контекстную информацию о текущем чате, времени и специфичный контекст чата.
    """
    character_data = get_character(character_id)
    if not character_data:
        return ""
    
    context_lines = []
    now = datetime.now()
    context_lines.append(f"Текущая дата и время: {now.strftime('%Y-%m-%d %H:%M')}.")
    
    if chat_name:
        if is_group:
            context_lines.append(f"Ты сейчас находишься в групповом чате '{chat_name}'.")
        else:
            context_lines.append(f"Ты сейчас общаешься с пользователем по имени '{chat_name}'.")
    
    context_prefix = "\n".join(context_lines)

    chat_context_section = ""
    if chat_context_prompt and chat_context_prompt.strip():
        chat_context_section = (
            f"\n### Важный контекст конкретно этого чата:\n"
            f"{chat_context_prompt.strip()}\n"
        )

    full_prompt = (
        f"{context_prefix}\n\n"
        f"### Твоя личность и роль:\n"
        f"{character_data.get('personality_prompt', '')}\n"
        f"{chat_context_section}\n" 
        f"### Твоя память (давние и недавние события):\n"
        f"{character_data.get('memory_prompt', '')}\n\n"
        f"### Системные инструкции и команды, которым ты должен следовать:\n"
        f"{character_data.get('system_commands_prompt', '')}"
    )
    
    return full_prompt.strip()
