import os
import json
import logging
from copy import deepcopy
import character_utils
import sticker_store
from shared_storage import file_lock, read_json, write_json
from instance_paths import ROOT, private_path
from model_fallbacks import DEFAULT_CHAIN
from cluster_prompts import DEFAULT_DM_PROMPT, DEFAULT_GROUP_PROMPT, DEFAULT_CONTEXT_PROMPT

GLOBAL_SETTINGS_FILE = 'data/global_settings.json'
ACCOUNTS_JSON_FILE = 'data/accounts.json'
LEGACY_CHAT_SETTINGS_FILE = str(ROOT / 'data' / 'chat_settings.json')
CHAT_SETTINGS_FILE = private_path('chat_settings.json')
STICKER_JSON_FILE = 'data/stickers.json'

DEFAULT_GLOBAL_SETTINGS = {
    "media_cleanup_enabled": True,
    "media_cleanup_days": 7,
}

DEFAULT_CHAT_SETTINGS = {
    # Общие
    "num_messages_to_fetch": 65,
    "add_chat_name_prefix": True,
    "reply_include_quote": False,
    "reply_include_name": False,
    "reply_load_outside_context": False,
    "reply_context_depth": 3,
    # Настройки для Gemini
    "model_name": "", 
    "enable_google_search": False,
    "enable_thinking": False,
    # None оставляет температуру по умолчанию у модели; явное значение — от 0 до 2.
    "temperature": None,
    # Глубина рассуждений reasoning-моделей OpenAI при включённом thinking: low | medium | high.
    "reasoning_effort": "high",
    # Слать запросы OpenAI и после исчерпания бесплатного дневного лимита (платно).
    # По умолчанию генерация останавливается у лимита (openai_budget).
    "allow_paid_overage": False,
    # Настройки памяти
    "enable_auto_memory": True,
    # Для медиа
    "can_see_photos": True,
    "can_see_videos": True,
    "can_see_audio": True,
    "can_see_files_pdf": True,
    "ignore_all_media": False, 
    # Для Auto-Mode
    "auto_fallback_enabled": True,
    "auto_fallback_models": DEFAULT_CHAIN.copy(),
    "auto_fallback_request_timeout_s": 300,
    "auto_fallback_timeout_version": 2,
    "auto_mode_check_interval": 3.5,
    "auto_mode_initial_wait": 6.0,
    "auto_mode_no_reply_timeout": 4.0,
    "auto_mode_no_reply_suffix": "\n\n(Тебе давно не отвечали. Вежливо поинтересуйся, все ли в порядке или почему молчат.)",
    "cluster_chatting_enabled": False,
    "cluster_no_commitment": False,
    "cluster_dm_idle_minutes": 4.0,
    "cluster_dm_prompt": DEFAULT_DM_PROMPT,
    "cluster_group_prompt": DEFAULT_GROUP_PROMPT,
    "cluster_context_prompt": DEFAULT_CONTEXT_PROMPT,
    # Пустой словарь означает полное наследование настроек основной группы.
    "cluster_dm_settings_overrides": {},
    # Для telegram_utils (симуляция)
    "sticker_choosing_delay_min": 2.0,
    "sticker_choosing_delay_max": 5.5,
    # Показывать модели стикеры картинкой + `sticker(id=NNN)`, а не текстовым codename.
    # Дороже (каждый стикер = картинка в промпте), но модель понимает контекст точно.
    "stickers_as_images": False,
    # Задержка «выбора картинки» — та же схема, что у стикеров. По умолчанию совпадает.
    "image_choosing_delay_min": 2.0,
    "image_choosing_delay_max": 5.5,
    "image_additional_delay_min": 0.2,
    "image_additional_delay_max": 0.8,
    "image_history_ttl_hours": 168.0,
    "typing_delay_ms_min": 40.0,
    "typing_delay_ms_max": 90.0,
    "base_thinking_delay_s_min": 1.2,
    "base_thinking_delay_s_max": 2.8,
    "max_typing_duration_s": 25.0,
    # Настройки для опечаток
    "lexicon_rules": [],
    "substitution_chance": 0.005,
    "transposition_chance": 0.005,
    "skip_chance": 0.002,
    "duplication_chance": 0.002,
    "lower_chance": 0.05,
    "word_loss_chance": 0.0,
    "max_lost_words": 1,
}

def load_global_settings():
    """Загружает глобальные настройки из JSON файла."""
    settings = DEFAULT_GLOBAL_SETTINGS.copy()
    try:
        if os.path.exists(GLOBAL_SETTINGS_FILE):
            with open(GLOBAL_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
                settings.update(loaded_settings)
        return settings
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logging.warning(f"Не удалось загрузить файл глобальных настроек ({GLOBAL_SETTINGS_FILE}): {e}. Будут использованы настройки по умолчанию.")
        return settings

def save_global_settings(settings_dict):
    """Сохраняет словарь глобальных настроек в JSON файл."""
    try:
        os.makedirs(os.path.dirname(GLOBAL_SETTINGS_FILE), exist_ok=True)
        write_json(GLOBAL_SETTINGS_FILE, settings_dict)
        return True
    except IOError as e:
        logging.error(f"Ошибка сохранения файла глобальных настроек ({GLOBAL_SETTINGS_FILE}): {e}")
        return False

def load_accounts():
    """Загружает список доступных аккаунтов из JSON файла."""
    try:
        if os.path.exists(ACCOUNTS_JSON_FILE):
            with open(ACCOUNTS_JSON_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Ошибка чтения файла '{ACCOUNTS_JSON_FILE}': {e}")
        return {}

def load_chat_settings():
    """Загружает настройки чатов этого инстанса; старые копирует только один раз."""
    try:
        if CHAT_SETTINGS_FILE != LEGACY_CHAT_SETTINGS_FILE and not os.path.exists(CHAT_SETTINGS_FILE):
            with file_lock(CHAT_SETTINGS_FILE):
                if not os.path.exists(CHAT_SETTINGS_FILE):
                    # Общий старый файл заменяется атомарно; копия дальше независима.
                    write_json(CHAT_SETTINGS_FILE, read_json(LEGACY_CHAT_SETTINGS_FILE, {}))
        if os.path.exists(CHAT_SETTINGS_FILE):
            with open(CHAT_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return {int(k): v for k, v in json.load(f).items()}
        return {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logging.warning(f"Не удалось загрузить файл настроек ({CHAT_SETTINGS_FILE}): {e}. Будет использован пустой словарь.")
        return {}

def save_chat_settings(settings_dict):
    """Сохраняет словарь настроек чатов в JSON файл."""
    try:
        os.makedirs(os.path.dirname(CHAT_SETTINGS_FILE), exist_ok=True)
        
        settings_to_save = {str(k): v for k, v in settings_dict.items()}
        write_json(CHAT_SETTINGS_FILE, settings_to_save)
        return True
    except IOError as e:
        logging.error(f"Ошибка сохранения файла настроек ({CHAT_SETTINGS_FILE}): {e}")
        return False

def get_chat_settings(chat_id):
    """
    Получает настройки для конкретного чата с учетом иерархии:
    1. Базовые дефолты.
    2. Настройки по умолчанию для активного персонажа.
    3. Специфичные настройки для этого персонажа в этом чате.
    """
    final_settings = deepcopy(DEFAULT_CHAT_SETTINGS)

    all_chat_settings = load_chat_settings()
    chat_specific_settings = all_chat_settings.get(chat_id, {})

    active_character_id = chat_specific_settings.get('active_character_id')
    final_settings['active_character_id'] = active_character_id

    if active_character_id:
        character_data = character_utils.get_character(active_character_id)
        if character_data:
            char_defaults = character_data.get('advanced_settings', {})
            final_settings.update(_with_current_timeout(char_defaults))

            char_in_chat_specifics = chat_specific_settings.get('character_specifics', {}).get(active_character_id, {})
            
            final_settings['chat_context_prompt'] = char_in_chat_specifics.get('chat_context_prompt', '')
            
            char_in_chat_advanced = char_in_chat_specifics.get('advanced_settings', {})
            final_settings.update(_with_current_timeout(char_in_chat_advanced))

    # Кластер управляет конкретной основной группой. Копия настроек персонажа
    # по умолчанию не должна незаметно включать его во всех остальных чатах.
    explicit_advanced = chat_specific_settings.get('character_specifics', {}).get(
        active_character_id, {}).get('advanced_settings', {}) if active_character_id else {}
    final_settings['cluster_chatting_enabled'] = bool(
        chat_id < 0 and explicit_advanced.get('cluster_chatting_enabled', False))
    final_settings['lexicon_rules'] = deepcopy(final_settings.get('lexicon_rules', []))
    final_settings['cluster_dm_settings_overrides'] = deepcopy(
        final_settings.get('cluster_dm_settings_overrides', {}))
    return final_settings


def _with_current_timeout(values):
    """Обновляет старый стандарт ожидания; явный новый выбор пользователя сохраняется."""
    result = dict(values)
    if result.get('auto_fallback_timeout_version', 1) < 2 and result.get('auto_fallback_request_timeout_s') == 120:
        result['auto_fallback_request_timeout_s'] = 300
    return result

def load_sticker_data():
    """Данные о стикерах в схеме v2 (см. sticker_store)."""
    return sticker_store.load()

def save_sticker_data(data):
    """Сохраняет данные о стикерах."""
    return sticker_store.save(data)

def structure_sticker_data(sticker_db: dict) -> list:
    """
    Структурирует плоский список стикеров в иерархию наборов на основе префиксов.
    Безымянные raw_* не показывает — модели их по имени не позвать.
    """
    sets = {}
    individual_stickers = {}

    for codename, data in sticker_db.items():
        if codename.startswith(sticker_store.QUARANTINE_PREFIX):
            continue
        if not data.get("stickers"):
            sets[codename] = {
                "description": data.get("description", ""),
                "stickers": [],
            }
        else:
            individual_stickers[codename] = data

    set_names = sorted(list(sets.keys()), key=len, reverse=True)
    unassigned_stickers = []

    def _item(codename, data):
        stickers = data.get("stickers", [])
        return {
            "codename": codename,
            "description": data.get("description", ""),
            # id первого стикера — для превью в интерфейсе
            "sticker_id": stickers[0].get("id") if stickers else None,
            "count": len(stickers),
            # Сколько из них модель получает только текстом — UI помечает такие.
            "quarantined_count": sum(1 for s in stickers if s.get("quarantined")),
        }

    for codename, data in individual_stickers.items():
        matched = False
        for set_name in set_names:
            if codename.startswith(set_name) and codename != set_name:
                sets[set_name]["stickers"].append(_item(codename, data))
                matched = True
                break
        if not matched:
            unassigned_stickers.append(_item(codename, data))

    if unassigned_stickers:
        sets["остальные"] = {
            "description": "Стикеры без определенного набора.",
            "stickers": unassigned_stickers
        }
    
    result_list = []
    for name, data in sets.items():
        if not data["stickers"] and name in individual_stickers:
            continue
        
        data["stickers"].sort(key=lambda x: x["codename"])
        result_list.append({"set_name": name, **data})
    
    result_list.sort(key=lambda x: x["set_name"])

    return result_list

def generate_sticker_prompt(enabled_sticker_packs: list, vision_mode: bool = False) -> str:
    """
    Создает инструкцию для модели на основе ВЫБРАННЫХ стикеров.
    В vision-режиме подсказывает про `sticker(id=NNN)` — иначе только codename.
    """
    sticker_db = sticker_store.load()
    if not sticker_db or not enabled_sticker_packs:
        return ""

    structured_sets = structure_sticker_data(sticker_db)
    enabled_set = set(enabled_sticker_packs)
    
    prompt_lines = []

    for sticker_set in structured_sets:
            
        enabled_stickers_in_set = [
            sticker for sticker in sticker_set.get('stickers', []) 
            if sticker['codename'] in enabled_set
        ]

        if enabled_stickers_in_set:
            if prompt_lines:
                prompt_lines.append("") 

            prompt_lines.append(f"Набор: {sticker_set['set_name']}")
            if sticker_set.get('description'):
                prompt_lines.append(f"Описание: {sticker_set['description']}")
            
            for sticker in enabled_stickers_in_set:
                line = f"- {sticker['codename']}"
                if sticker.get('description'):
                    line += f": {sticker['description']}"
                prompt_lines.append(line)
    
    if not prompt_lines:
        return ""

    intro = ("Чтобы отправить стикер, используй sticker(кодовое_имя_из_списка_ниже). "
             "В истории входящие стикеры уже отмечены командой sticker(id=…) — "
             "если хочешь ответить именно тем же стикером, можно отправить sticker(id=NNN). "
             "Если у sticker(id=…) в истории нет картинки — ты его не видишь; "
             "ориентируйся по codename, если он указан."
             if vision_mode else
             "Чтобы отправить стикер, используй sticker(кодовое_имя_из_списка_ниже).")

    return f"{intro}\n\nДоступные стикеры:\n{chr(10).join(prompt_lines)}"
