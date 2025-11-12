import os
import logging
import threading 
import asyncio 
import time 
import random
import atexit 
import re
from datetime import timedelta, datetime
import json 
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify 
from dotenv import load_dotenv
from colorama import Fore, Style, init
from werkzeug.routing import BaseConverter
import character_utils 
from google.genai import types
import argparse 

init(autoreset=True)
load_dotenv()

INSTANCE_NUMBER = int(os.getenv('INSTANCE_NUMBER', 1))
TELAGRAMM_API_ID = os.getenv('TELAGRAMM_API_ID')
TELAGRAMM_API_HASH = os.getenv('TELAGRAMM_API_HASH')

if not TELAGRAMM_API_ID or not TELAGRAMM_API_HASH:
    raise ValueError("TELAGRAMM_API_ID и TELAGRAMM_API_HASH должны быть установлены в .env файле")

from telegram_utils import (
    get_chats,
    get_chat_info,
    get_formatted_history,
    send_telegram_message,
    disconnect_telegram,
    telegram_main_loop, 
    run_in_telegram_loop,
    STICKER_DB,
    send_sticker_by_codename,
    send_telegram_reaction,
    get_media_for_message,
    cleanup_old_cache_files  
)
from gemini_utils import (
    init_gemini_client,
    generate_chat_reply_original,
    BASE_GEMENI_MODEL,
    
)

class SignedIntConverter(BaseConverter):
    """Кастомный конвертер для URL, который обрабатывает положительные и отрицательные целые числа."""
    regex = r'-?\d+'

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(value)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s')

app = Flask(__name__)
app.config['SESSION_COOKIE_NAME'] = f'telegram_bot_session_{INSTANCE_NUMBER}'
app.url_map.converters['sint'] = SignedIntConverter
app.secret_key = os.urandom(24)

ACCOUNTS_JSON_FILE = 'data/accounts.json'
DEFAULT_SESSION_NAME = 'kadzu'
CHAT_SETTINGS_FILE = 'data/chat_settings.json'
GLOBAL_SETTINGS_FILE = 'data/global_settings.json'
STICKER_JSON_FILE = 'data/stickers.json'
CHARTS_LIMIT = 120
CHAT_LIMIT = 10000
TELEGRAM_MAX_MESSAGE_LENGTH = 4006


gemini_client_global = None
telegram_thread = None 
telegram_ready_event = threading.Event() 

DEFAULT_GLOBAL_SETTINGS = {
    "media_cleanup_enabled": True,
    "media_cleanup_days": 7,
}

DEFAULT_CHAT_SETTINGS = {
    # Общие
    "num_messages_to_fetch": 65,
    "add_chat_name_prefix": True,
    # Настройки для Gemini
    "model_name": "", 
    "enable_google_search": False,
    "enable_thinking": False,
    # Настройки памяти
    "enable_auto_memory": True,
    # Для медиа
    "can_see_photos": True,
    "can_see_videos": True,
    "can_see_audio": True,
    "can_see_files_pdf": True,
    "ignore_all_media": False, 
    # Для Auto-Mode
    "auto_mode_check_interval": 3.5,
    "auto_mode_initial_wait": 6.0,
    "auto_mode_no_reply_timeout": 4.0,
    "auto_mode_no_reply_suffix": "\n\n(Тебе давно не отвечали. Вежливо поинтересуйся, все ли в порядке или почему молчат.)",
    # Для telegram_utils (симуляция)
    "sticker_choosing_delay_min": 2.0,
    "sticker_choosing_delay_max": 5.5,
    "typing_delay_ms_min": 40.0,
    "typing_delay_ms_max": 90.0,
    "base_thinking_delay_s_min": 1.2,
    "base_thinking_delay_s_max": 2.8,
    "max_typing_duration_s": 25.0,
    # Настройки для опечаток
    "substitution_chance": 0.005,
    "transposition_chance": 0.005,
    "skip_chance": 0.002,
    "lower_chance": 0.05,
}


auto_mode_workers = {} 
auto_mode_lock = threading.Lock() 

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
        with open(GLOBAL_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_dict, f, ensure_ascii=False, indent=4)
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

def choose_account_from_console(account_choice_arg=None):
    """
    Отображает в консоли список аккаунтов и просит пользователя сделать выбор.
    Возвращает имя выбранного файла сессии.
    Может принимать выбор как аргумент.
    """
    accounts = load_accounts()
    if not accounts:
        print(Fore.YELLOW + f"Файл '{ACCOUNTS_JSON_FILE}' не найден или пуст. Используется сессия по умолчанию: '{DEFAULT_SESSION_NAME}'")
        return DEFAULT_SESSION_NAME

    account_list = list(accounts.items())

    if account_choice_arg is not None:
        try:
            choice_index = int(account_choice_arg) - 1
            if 0 <= choice_index < len(account_list):
                selected_session_file = account_list[choice_index][1]
                selected_account_name = account_list[choice_index][0]
                print(Fore.GREEN + f"Аккаунт выбран автоматически: '{selected_account_name}' (№{account_choice_arg})...")
                return selected_session_file
            else:
                print(Fore.RED + f"Ошибка: номер аккаунта '{account_choice_arg}' из аргумента вне диапазона. Переход к ручному выбору.")
        except ValueError:
            print(Fore.RED + f"Ошибка: переданный аргумент '{account_choice_arg}' не является числом. Переход к ручному выбору.")
    
    print(Fore.CYAN + "Пожалуйста, выберите аккаунт для запуска:")
    for i, (name, _) in enumerate(account_list):
        print(f"  {Fore.GREEN}{i + 1}{Style.RESET_ALL}: {name}")
    
    while True:
        try:
            choice_str = input(f"Введите номер (1-{len(account_list)}): ")
            choice_index = int(choice_str) - 1
            if 0 <= choice_index < len(account_list):
                selected_session_file = account_list[choice_index][1]
                selected_account_name = account_list[choice_index][0]
                print(Fore.GREEN + f"Выбран аккаунт: '{selected_account_name}'. Запуск с сессией '{selected_session_file}'...")
                return selected_session_file
            else:
                print(Fore.RED + "Неверный номер. Пожалуйста, попробуйте снова.")
        except ValueError:
            print(Fore.RED + "Авто-выбор, ты вильзи")
            selected_session_file = account_list[2][1]
            selected_account_name = account_list[2][0]
            print(Fore.GREEN + f"Выбран аккаунт: '{selected_account_name}'. Запуск с сессией '{selected_session_file}'...")
            return selected_session_file
        except (KeyboardInterrupt, EOFError):
            print(Fore.YELLOW + "\nВыбор отменен. Завершение работы.")
            exit()

def load_chat_settings():
    """Загружает все сохраненные настройки чатов из JSON файла."""
    try:
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
        with open(CHAT_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_to_save, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logging.error(f"Ошибка сохранения файла настроек ({CHAT_SETTINGS_FILE}): {e}")

def get_chat_settings(chat_id):
    """
    Получает настройки для конкретного чата с учетом иерархии:
    1. Базовые дефолты.
    2. Настройки по умолчанию для активного персонажа.
    3. Специфичные настройки для этого персонажа в этом чате.
    """
    final_settings = DEFAULT_CHAT_SETTINGS.copy()

    all_chat_settings = load_chat_settings()
    chat_specific_settings = all_chat_settings.get(chat_id, {})

    active_character_id = chat_specific_settings.get('active_character_id')
    final_settings['active_character_id'] = active_character_id

    if active_character_id:
        character_data = character_utils.get_character(active_character_id)
        if character_data:
            char_defaults = character_data.get('advanced_settings', {})
            final_settings.update(char_defaults)

            char_in_chat_specifics = chat_specific_settings.get('character_specifics', {}).get(active_character_id, {})
            
            final_settings['chat_context_prompt'] = char_in_chat_specifics.get('chat_context_prompt', '')
            
            char_in_chat_advanced = char_in_chat_specifics.get('advanced_settings', {})
            final_settings.update(char_in_chat_advanced)

    return final_settings

def structure_sticker_data(sticker_db: dict) -> list:
    """
    Структурирует плоский список стикеров в иерархию наборов на основе префиксов.
    """
    sets = {}
    individual_stickers = {}

    for codename, data in sticker_db.items():
        if not data.get("stickers"):
            sets[codename] = {
                "description": data.get("description", ""),
                "stickers": [],
            }
        else:
            individual_stickers[codename] = data

    set_names = sorted(list(sets.keys()), key=len, reverse=True)
    unassigned_stickers = []

    for codename, data in individual_stickers.items():
        matched = False
        for set_name in set_names:
            if codename.startswith(set_name) and codename != set_name:
                sets[set_name]["stickers"].append({
                    "codename": codename,
                    "description": data.get("description", "")
                })
                matched = True
                break
        if not matched:
            unassigned_stickers.append({
                "codename": codename,
                "description": data.get("description", "")
            })

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

def generate_sticker_prompt(enabled_sticker_packs: list) -> str:
    """
    Создает строку-инструкцию для Gemini на основе ВЫБРАННЫХ стикеров.
    """
    if not STICKER_DB or not enabled_sticker_packs:
        return ""

    available_stickers_lines = []
    for codename in sorted(enabled_sticker_packs):
        data = STICKER_DB.get(codename)
        if data:
            line = f"- {codename}"
            if data.get("description"):
                line += f": {data['description']}"
            available_stickers_lines.append(line)
    
    if not available_stickers_lines:
        return "" 

    full_prompt = (
        "Чтобы отправить стикер, используй команду sticker(кодовое_имя_из_списка_ниже).\n\n"
        "Доступные стикеры:\n"
        f"{'\n'.join(available_stickers_lines)}"
    )
    return full_prompt

def load_sticker_data():
    """Безопасно загружает данные о стикерах из JSON-файла."""
    try:
        if os.path.exists(STICKER_JSON_FILE):
            with open(STICKER_JSON_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logging.warning(f"Не удалось загрузить файл стикеров ({STICKER_JSON_FILE}): {e}.")
        return {}

def save_sticker_data(data):
    """Безопасно сохраняет данные о стикерах в JSON-файл."""
    try:
        with open(STICKER_JSON_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return True
    except IOError as e:
        logging.error(f"Ошибка сохранения файла стикеров ({STICKER_JSON_FILE}): {e}")
        return False
    
def initialize_gemini():
    """Инициализирует клиент Gemini."""
    global gemini_client_global
    logging.info("Инициализация клиента Gemini...")
    gemini_client_global = init_gemini_client()
    if not gemini_client_global:
        logging.error(Fore.RED + "Не удалось инициализировать Gemini. Генерация будет недоступна.")
    else:
        logging.info(Fore.GREEN + "Клиент Gemini инициализирован.")

def start_telegram_thread(session_name_to_use: str):
    """Запускает поток для Telethon с УКАЗАННЫМ именем сессии."""
    global telegram_thread
    if telegram_thread and telegram_thread.is_alive():
        logging.warning("Поток Telethon уже запущен.")
        return

    logging.info(f"Запуск потока для Telethon с сессией '{session_name_to_use}'...")
    thread = threading.Thread(
        target=asyncio.run, 
        args=(telegram_main_loop( 
            TELAGRAMM_API_ID,
            TELAGRAMM_API_HASH,
            session_name_to_use,  
            telegram_ready_event 
        ),),
        name=f"TelegramThread-{session_name_to_use}", 
        daemon=True 
    )
    thread.start()
    telegram_thread = thread
    logging.info("Поток Telethon запущен. Ожидание сигнала готовности...")

def stop_telegram_thread():
    """Останавливает цикл событий Telethon и ждет завершения потока."""
    logging.info("Остановка всех активных потоков авто-режима...")
    with auto_mode_lock:
        for chat_id, worker_info in list(auto_mode_workers.items()):
            if worker_info["thread"] and worker_info["thread"].is_alive():
                logging.info(f"Отправка сигнала остановки потоку для чата {chat_id}")
                worker_info["stop_event"].set()
                worker_info["status"] = "stopping" 
        
        active_threads = [wi["thread"] for wi in auto_mode_workers.values() if wi["thread"] and wi["thread"].is_alive()]
    if active_threads:
        logging.info(f"Ожидание завершения {len(active_threads)} потоков авто-режима (макс 5 секунд)...")
        for thread in active_threads:
            thread.join(timeout=5.0 / len(active_threads) if len(active_threads) > 0 else 5.0)
            if thread.is_alive():
                logging.warning(f"Поток {thread.name} не завершился вовремя.")
    logging.info("Все потоки авто-режима остановлены или им дан сигнал.")

    logging.info("Получен сигнал завершения. Остановка потока Telethon...")
    from telegram_utils import telegram_loop, client as telethon_client, disconnect_telegram 

    if telegram_loop and telegram_loop.is_running():
        if telethon_client and telethon_client.is_connected():
            logging.info("Отправка команды disconnect в цикл Telethon...")
            future = asyncio.run_coroutine_threadsafe(disconnect_telegram(), telegram_loop)
            try:
                future.result(timeout=10)
                logging.info("Команда disconnect выполнена.")
            except asyncio.TimeoutError:
                logging.warning("Отключение Telethon заняло слишком много времени.")
            except Exception as e:
                 logging.error(f"Ошибка при выполнении disconnect_telegram: {e}")
        else:
            logging.info("Клиент не подключен, остановка цикла Telethon...")
            telegram_loop.call_soon_threadsafe(telegram_loop.stop)

    if telegram_thread and telegram_thread.is_alive():
        logging.info("Ожидание завершения потока Telethon (до 15 секунд)...")
        telegram_thread.join(timeout=15)
        if telegram_thread.is_alive():
            logging.warning("Поток Telethon не завершился вовремя.")
        else:
            logging.info("Поток Telethon успешно завершен.")
    else:
        logging.info("Поток Telethon не был активен.")

def parse_time_from_message(message_dict):
    """
    Вспомогательная функция для парсинга времени из текста сообщения.
    ИСПРАВЛЕНА: Ищет текст во всех частях сообщения, а не только в первой.
    """
    try:
        if not message_dict or not isinstance(message_dict, dict) or "parts" not in message_dict:
             return None
        
        text_to_parse = None
        for part in message_dict.get("parts", []):
            if "text" in part and isinstance(part["text"], str):
                text_to_parse = part["text"]
                break 
        
        if text_to_parse is None:
            logging.warning("В сообщении не найдена текстовая часть для парсинга времени.")
            return None

        match = re.search(r"\[(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\]", text_to_parse)
        
        
        if match:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        else:
            
            return None
            
    except Exception as e:
        logging.error(f"Ошибка парсинга времени из текста сообщения: {e}")
        return None

def split_message_by_limit(text: str, limit: int) -> list[str]:
    """
    Разделяет длинный текст на части, не превышающие заданный лимит.
    Разделение происходит по переносам строк, а затем по пробелам,
    чтобы не разрывать слова.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    while len(text) > 0:
        if len(text) <= limit:
            chunks.append(text)
            break
        
        split_pos = text.rfind('\n\n', 0, limit)
        if split_pos == -1:
            split_pos = text.rfind('\n', 0, limit)
        if split_pos == -1:
            split_pos = text.rfind(' ', 0, limit)

        if split_pos == -1:
            split_pos = limit

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip() 

    return chunks

def replace_standalone_sticker_names(text: str) -> str:
    """
    Находит "одинокие" кодовые имена стикеров в тексте и оборачивает их в команду sticker().
    Оптимизирована для запуска только если в тексте есть потенциальные английские слова,
    и избегает оборачивания уже корректно отформатированных команд.
    """
    if not text or not re.search(r'[a-zA-Z]{3,}', text):
        return text

    sticker_codenames = sorted(list(STICKER_DB.keys()), key=len, reverse=True)
    if not sticker_codenames:
        return text

    processed_text = text
    for codename in sticker_codenames:
        pattern = r'(?<!sticker\s*\(\s*)' + r'\b' + re.escape(codename) + r'\b'
        replacement = f'sticker({codename})'
        processed_text = re.sub(pattern, replacement, processed_text, flags=re.IGNORECASE)

    return processed_text

def send_generated_reply(chat_id: int, message_text: str, settings: dict = None):
    """
    Централизованная функция для отправки сгенерированного ответа.
    Обрабатывает команды react(), разделитель {split}, команды sticker() и смешанный контент.
    """

    if not message_text or not message_text.strip():
        logging.warning(f"В send_generated_reply передано пустое сообщение для чата {chat_id}.")
        return True, "Empty message provided."

    if settings is None:
        logging.debug(f"send_generated_reply: настройки не переданы, загружаются для чата {chat_id}")
        settings_to_use = get_chat_settings(chat_id)
    else:
        logging.debug(f"send_generated_reply: используются переданные настройки для чата {chat_id}")
        settings_to_use = settings

    try:
        message_text = replace_standalone_sticker_names(message_text)
    except Exception as e:
        logging.error(f"Ошибка при исправлении имен стикеров: {e}", exc_info=True)

    VALID_REACTIONS = ['👍', '❤️', '🔥', '🎉', '🤩', '😱', '😁', '😢', '🤔', '👎', '💩', '🤔']

    reaction_tasks = []
    if 'react' in message_text: 
        react_pattern_with_id = re.compile(r"react\s*\(\s*(\d+)\s*\)\s*(?:\[([^\]\n]+?)\]|([^\s\w\d,.<>{|}]+))", re.IGNORECASE)
        
        matches = list(react_pattern_with_id.finditer(message_text))
        for match in matches:
            msg_id_str = match.group(1)
            emoji_str = match.group(2) or match.group(3)

            if not emoji_str: continue
            
            try:
                msg_id = int(msg_id_str)
            except ValueError:
                logging.warning(f"Невалидный ID сообщения '{msg_id_str}' в команде реакции. Пропуск.")
                continue

            if emoji_str not in VALID_REACTIONS:
                new_emoji = random.choice(VALID_REACTIONS)
                logging.warning(f"Невалидный эмодзи для реакции '{emoji_str}'. Заменен на случайный: '{new_emoji}'.")
                emoji_str = new_emoji
            
            reaction_tasks.append({"type": "reaction", "message_id": msg_id, "emoji": emoji_str})

        message_text = re.sub(r'react\s*\(\s*\d+\s*\)\s*(?:\[[^\]\n]+?\]|[^\s\w\d,.<>{|}]+)\s*', '', message_text, flags=re.IGNORECASE).strip()
        message_text = re.sub(r'react\s*\[[^\]\n]+?\]', '', message_text, flags=re.IGNORECASE).strip()

    if not message_text.strip() and not reaction_tasks:
        logging.warning(f"В send_generated_reply для чата {chat_id} не осталось ни текста, ни задач на реакцию. Отправка отменена.")
        return True, "Empty message and no reaction tasks."


    sticker_pattern = r"sticker\s*\(([\w\d_-]+)\)"
    split_separator = "{split}"

    tasks_to_send = []
    tasks_to_send.extend(reaction_tasks)
    
    if message_text.strip():
        initial_parts = [p.strip() for p in message_text.split(split_separator) if p.strip()]
        
        for part in initial_parts:
            found_stickers = list(re.finditer(sticker_pattern, part, re.IGNORECASE))
            
            if not found_stickers:
                if len(part) > TELEGRAM_MAX_MESSAGE_LENGTH:
                    text_chunks = split_message_by_limit(part, TELEGRAM_MAX_MESSAGE_LENGTH)
                    for chunk in text_chunks:
                        tasks_to_send.append({"type": "text", "content": chunk})
                else:
                    tasks_to_send.append({"type": "text", "content": part})
                continue

            last_index = 0
            for match in found_stickers:
                start, end = match.span()
                if start > last_index:
                    text_before = part[last_index:start].strip()
                    if text_before:
                        if len(text_before) > TELEGRAM_MAX_MESSAGE_LENGTH:
                            text_chunks = split_message_by_limit(text_before, TELEGRAM_MAX_MESSAGE_LENGTH)
                            for chunk in text_chunks:
                                tasks_to_send.append({"type": "text", "content": chunk})
                        else:
                            tasks_to_send.append({"type": "text", "content": text_before})

                codename = match.group(1)
                tasks_to_send.append({"type": "sticker", "content": codename})
                
                last_index = end
            
            if last_index < len(part):
                text_after = part[last_index:].strip()
                if text_after:
                     if len(text_after) > TELEGRAM_MAX_MESSAGE_LENGTH:
                        text_chunks = split_message_by_limit(text_after, TELEGRAM_MAX_MESSAGE_LENGTH)
                        for chunk in text_chunks:
                            tasks_to_send.append({"type": "text", "content": chunk})
                     else:
                        tasks_to_send.append({"type": "text", "content": text_after})

    logging.info(f"Будет выполнено {len(tasks_to_send)} задач на отправку в чат {chat_id}.")
    
    all_success = True
    first_error_message = None

    for i, task in enumerate(tasks_to_send):
        success = False
        error_message = None

        if task["type"] == "text":
            logging.info(f"Отправка текста в чат {chat_id}: \"{task['content'][:50]}...\"")
            success, error_message = run_in_telegram_loop(
                send_telegram_message(chat_id, task["content"], settings=settings_to_use)
            )
        elif task["type"] == "sticker":
            logging.info(f"Отправка стикера '{task['content']}' в чат {chat_id}.")
            success, error_message = run_in_telegram_loop(send_sticker_by_codename(chat_id, task["content"], settings=settings_to_use))

            if success and error_message:
                logging.warning(f"Задача отправки стикера '{task['content']}' пропущена: {error_message}")
        
        elif task["type"] == "reaction":
            logging.info(f"Отправка реакции '{task['emoji']}' на сообщение {task['message_id']} в чат {chat_id}.")
            success, error_message = run_in_telegram_loop(
                send_telegram_reaction(chat_id, task["message_id"], task["emoji"])
            )
            if success and error_message:
                logging.warning(f"Задача отправки реакции '{task['emoji']}' пропущена: {error_message}")


        if not success:
            all_success = False
            logging.error(f"Ошибка отправки задачи {i+1} ({task['type']}) в чат {chat_id}: {error_message}")
            if first_error_message is None:
                first_error_message = error_message
            break 

        if i < len(tasks_to_send) - 1:
            delay = 0.0
            current_type = task["type"]
            next_type = tasks_to_send[i+1]["type"]

            if current_type == "reaction" and next_type == "reaction":
                delay = random.uniform(0.3, 0.8)
                logging.info(f"Короткая пауза между реакциями: {delay:.2f} сек.")
            else:
                min_pause = settings_to_use.get('base_thinking_delay_s_min', 1.0)
                max_pause = settings_to_use.get('base_thinking_delay_s_max', 2.0)
                if max_pause < min_pause: max_pause = min_pause
                delay = random.uniform(min_pause, max_pause)
                logging.info(f"Пауза перед следующей частью: {delay:.2f} сек.")
            
            if delay > 0.05:
                time.sleep(delay)

    return all_success, first_error_message

def auto_mode_worker(chat_id: int, stop_event: threading.Event):
    """
    Worker авто-режима. Ждет нового сообщения от пользователя, затем выжидает
    определенное время. Если за это время приходят еще сообщения, таймер
    сбрасывается. Ответ генерируется только тогда, когда пользователь
    перестает отправлять сообщения.
    Также управляет автоматическим обновлением памяти персонажа.
    """ 
    global auto_mode_workers, auto_mode_lock
    global BASE_GEMENI_MODEL
    global run_in_telegram_loop, get_formatted_history, generate_chat_reply_original, character_utils

    worker_name = f"AutoMode-{chat_id}"
    logging.info(f"[{worker_name}] Поток запущен.")

    last_processed_user_msg_time = None
    last_own_message_sent_time = datetime.now()

    while not stop_event.is_set():
        
        base_chat_settings = get_chat_settings(chat_id)
        settings_for_generation = base_chat_settings.copy() 

        character_id = base_chat_settings.get('active_character_id')
        if not character_id:
            logging.warning(f"[{worker_name}] В чате не выбран активный персонаж. Авто-режим приостановлен. Пауза 60 сек.")
            stop_event.wait(60)
            continue
            
        character_data = character_utils.get_character(character_id)
        if not character_data:
            logging.error(f"[{worker_name}] Не найдены данные для персонажа {character_id}. Авто-режим приостановлен. Пауза 60 сек.")
            stop_event.wait(60)
            continue
            
        if character_data.get('advanced_settings'):
            logging.debug(f"[{worker_name}] Применяются персональные настройки поверх настроек чата.")
            settings_for_generation.update(character_data['advanced_settings'])
        
        check_interval = settings_for_generation.get('auto_mode_check_interval', DEFAULT_CHAT_SETTINGS['auto_mode_check_interval'])

        try:
            
            with auto_mode_lock:
                 current_status = auto_mode_workers.get(chat_id, {}).get("status", "inactive")
            if current_status != "active":
                 logging.info(f"[{worker_name}] Статус изменился на '{current_status}'. Остановка.")
                 break

            should_generate = False
            is_timeout_trigger = False
            
            history_check, error_check = run_in_telegram_loop(get_formatted_history(chat_id, limit=2, settings=settings_for_generation))

            if error_check:
                logging.error(f"[{worker_name}] Ошибка получения истории для проверки: {error_check}. Пауза 30 сек.")
                stop_event.wait(30)
                continue
            if not history_check:
                stop_event.wait(check_interval)
                continue

            latest_message = history_check[-1]
            latest_message_time = parse_time_from_message(latest_message)
            is_latest_from_user = latest_message["role"] == "user"
            
            initial_wait_s = settings_for_generation.get('auto_mode_initial_wait', DEFAULT_CHAT_SETTINGS['auto_mode_initial_wait'])

            if is_latest_from_user and latest_message_time and \
               (last_processed_user_msg_time is None or latest_message_time > last_processed_user_msg_time):
                logging.info(f"[{worker_name}] Обнаружено новое сообщение от пользователя. Ожидание {initial_wait_s} сек...")
                last_processed_user_msg_time = latest_message_time
                
                stop_event.wait(initial_wait_s)
                if stop_event.is_set(): break
                
                history_after_wait, error_after_wait = run_in_telegram_loop(get_formatted_history(chat_id, limit=2, settings=settings_for_generation))
                if error_after_wait or not history_after_wait:
                    logging.warning(f"[{worker_name}] Не удалось перепроверить историю. Пропуск цикла.")
                else:
                    latest_message_after_wait = history_after_wait[-1]
                    time_after_wait = parse_time_from_message(latest_message_after_wait)
                    
                    if time_after_wait == last_processed_user_msg_time:
                        logging.info(f"[{worker_name}] Новых сообщений за время ожидания не было. Пора отвечать.")
                        should_generate = True
                    else:
                        logging.info(f"[{worker_name}] Обнаружено еще более новое сообщение. Сброс таймера.")
            
            if not should_generate:
                 time_since_last_sent = datetime.now() - last_own_message_sent_time
                 no_reply_timeout_min = settings_for_generation.get('auto_mode_no_reply_timeout', DEFAULT_CHAT_SETTINGS['auto_mode_no_reply_timeout'])
                 
                 if not is_latest_from_user and time_since_last_sent > timedelta(minutes=no_reply_timeout_min):
                     logging.info(f"[{worker_name}] Собеседник не отвечает > {no_reply_timeout_min} мин. Генерация напоминания.")
                     should_generate = True
                     is_timeout_trigger = True
                     last_own_message_sent_time = datetime.now()
                     if latest_message_time:
                         last_processed_user_msg_time = latest_message_time

            if should_generate:
                chat_info, _ = run_in_telegram_loop(get_chat_info(chat_id))
                
                model_name_from_settings = settings_for_generation.get('model_name', '')
                model_name_to_use = model_name_from_settings or BASE_GEMENI_MODEL
                
                logging.info(f"[{worker_name}] Работа от лица персонажа: {character_data.get('name')}")
                
                final_system_prompt = character_utils.get_full_prompt_for_character(
                    character_id, 
                    chat_name=chat_info.get('name', str(chat_id)),
                    is_group=(chat_id < 0),
                    chat_context_prompt=settings_for_generation.get('chat_context_prompt')
                )

                if is_timeout_trigger:
                    no_reply_suffix = settings_for_generation.get('auto_mode_no_reply_suffix', DEFAULT_CHAT_SETTINGS['auto_mode_no_reply_suffix'])
                    final_system_prompt += f"\n\n{no_reply_suffix}"

                num_messages = settings_for_generation.get('num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
                full_history, history_error = run_in_telegram_loop(get_formatted_history(chat_id, limit=num_messages, settings=settings_for_generation))

                if history_error or not full_history:
                    logging.error(f"[{worker_name}] Ошибка получения истории для генерации: {history_error}. Пропуск.")
                    stop_event.wait(15)
                    continue

                if settings_for_generation.get('enable_auto_memory', True):
                    with auto_mode_lock:
                        bot_last_message_anchor = auto_mode_workers.get(chat_id, {}).get("bot_last_message_anchor")
                    
                    def find_last_bot_message_text(history):
                        for msg in reversed(history):
                            if msg.get("role") == "model":
                                for part in msg.get("parts", []):
                                    if "text" in part: return part["text"]
                        return None

                    if not bot_last_message_anchor:
                        new_anchor_text = find_last_bot_message_text(full_history)
                        if new_anchor_text:
                            with auto_mode_lock:
                                if chat_id in auto_mode_workers: auto_mode_workers[chat_id]["bot_last_message_anchor"] = new_anchor_text
                            logging.info(f"[{worker_name}] Авто-память: Установлен начальный якорь: '{new_anchor_text[:50]}...'")
                    else:
                        anchor_is_visible = any( part.get("text") == bot_last_message_anchor for msg in full_history if msg.get("role") == "model" for part in msg.get("parts", []) if "text" in part )
                        
                        if not anchor_is_visible:
                            logging.info(f"[{worker_name}] Авто-память: Якорь '{bot_last_message_anchor[:50]}...' больше не виден. Запуск обновления памяти.")
                            _, mem_update_error = character_utils.update_character_memory(
                                character_id=character_id, chat_name=chat_info.get('name', str(chat_id)),
                                is_group=chat_id < 0, chat_history=full_history
                            )
                            if mem_update_error:
                                logging.error(f"[{worker_name}] Авто-память: Ошибка: {mem_update_error}")
                            else:
                                logging.info(f"[{worker_name}] Авто-память: Память персонажа ID {character_id} успешно обновлена.")
                                new_anchor_text = find_last_bot_message_text(full_history)
                                with auto_mode_lock:
                                    if chat_id in auto_mode_workers: auto_mode_workers[chat_id]["bot_last_message_anchor"] = new_anchor_text
                                logging.info(f"[{worker_name}] Авто-память: Установлен новый якорь: '{new_anchor_text[:50] if new_anchor_text else 'None'}'")
                else:
                    logging.info(f"[{worker_name}] Авто-память отключена в настройках персонажа. Пропуск обновления.")

                tools = []
                if settings_for_generation.get('enable_google_search', False):
                    tools.append(types.Tool(googleSearch=types.GoogleSearch()))

                thinking_config = None
                thinking_models = ['gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.5-flash-lite']
                model_name_lower = model_name_to_use.lower()
                is_thinking_model = any(m in model_name_lower for m in thinking_models)

                if settings_for_generation.get('enable_thinking', False) and is_thinking_model:
                    thinking_config = types.ThinkingConfig(thinking_budget=-1)
                elif settings_for_generation.get('enable_thinking', False):
                    logging.warning(f"[{worker_name}] Thinking mode включен, но модель '{model_name_to_use}' его не поддерживает. Игнорируется.")

                final_generation_config_parts = {}
                if tools:
                    final_generation_config_parts['tools'] = tools
                if thinking_config:
                    final_generation_config_parts['thinking_config'] = thinking_config
                
                final_generation_config = types.GenerateContentConfig(**final_generation_config_parts) if final_generation_config_parts else None

                logging.info(f"[{worker_name}] Вызов Gemini для генерации (лимит истории: {num_messages})...")
                generated_text, gen_error = generate_chat_reply_original(
                    model_name=model_name_to_use, 
                    system_prompt=final_system_prompt.strip(), 
                    chat_history=full_history,
                    config=final_generation_config 
                )
                if gen_error:
                    logging.error(f"[{worker_name}] Ошибка генерации Gemini: {gen_error}")
                    stop_event.wait(20)
                elif generated_text and generated_text.strip():
                    logging.info(f"[{worker_name}] Ответ сгенерирован. Отправка...")
                    success, error_msg = send_generated_reply(chat_id, generated_text.strip(), settings=settings_for_generation)
                    if success:
                        logging.info(f"[{worker_name}] Ответ успешно отправлен.")
                        last_own_message_sent_time = datetime.now()
                    else:
                        logging.error(f"[{worker_name}] Ошибка при отправке: {error_msg}")
                else:
                    logging.warning(f"[{worker_name}] Gemini вернул пустой ответ.")
            
            if not should_generate:
                stop_event.wait(check_interval)
        
        except Exception as e:
            logging.exception(f"[{worker_name}] Неперехваченная ошибка в цикле worker: {e}")
            stop_event.wait(60)

    logging.info(f"[{worker_name}] Поток завершает работу.")
    with auto_mode_lock:
        if chat_id in auto_mode_workers:
            if auto_mode_workers[chat_id].get("status") != "stopping":
                 auto_mode_workers[chat_id]["status"] = "inactive"

@app.route('/')
def index():
    """Главная страница - выбор чата."""
    logging.info("Запрос GET /")
    chats_data, error = run_in_telegram_loop(get_chats(limit=CHARTS_LIMIT))

    if error:
        flash(f"Ошибка получения списка чатов: {error}", "error")
        logging.error(f"Ошибка при получении чатов: {error}")
    elif not chats_data:
         flash("Не удалось получить список чатов или он пуст.", "warning")
         logging.warning("Список чатов пуст или не получен.")

    global_settings = load_global_settings()
    return render_template('index.html',
                           chats=chats_data if chats_data else [],
                           error=error,
                           global_settings=global_settings)

@app.route('/select_chat', methods=['POST'])
def select_chat():
    """Обработка выбора чата."""
    logging.info("Запрос POST /select_chat")
    chat_id_str = request.form.get('chat_id')
    if not chat_id_str:
        flash("ID чата не был передан.", "error")
        return redirect(url_for('index'))
    try:
        chat_id = int(chat_id_str)
        logging.info(f"Выбран чат с ID: {chat_id}")
        session.pop('generated_reply', None)
        session.pop('last_generation_error', None)
        session.pop(f'auto_mode_status_{chat_id}', None)
        return redirect(url_for('chat_page', chat_id=chat_id))
    except ValueError:
        flash("Некорректный ID чата.", "error")
        return redirect(url_for('index'))

@app.route('/generate/<sint:chat_id>', methods=['POST'])
def generate_reply(chat_id):
    """
    Обрабатывает ручную генерацию ответа и возвращает результат в формате JSON.
    """
    logging.info(f"Запрос POST /generate/{chat_id} (AJAX)")

    settings_for_generation = get_chat_settings(chat_id)
    character_id = settings_for_generation.get('active_character_id')

    if not character_id:
        return jsonify({'status': 'error', 'message': 'Активный персонаж не выбран!'}), 400
    
    chat_info_data, _ = run_in_telegram_loop(get_chat_info(chat_id))

    final_system_prompt = character_utils.get_full_prompt_for_character(
        character_id, 
        chat_name=chat_info_data.get('name') if chat_info_data else str(chat_id),
        is_group=(chat_id < 0),
        chat_context_prompt=settings_for_generation.get('chat_context_prompt')
    )
    
    limit = settings_for_generation.get('num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
    history_data, history_error = run_in_telegram_loop(get_formatted_history(chat_id, limit=limit, settings=settings_for_generation))

    if history_error or not history_data:
        error = history_error or "История чата пуста."
        return jsonify({'status': 'error', 'message': f'Ошибка получения истории: {error}'}), 500

    model_name_input = request.form.get('model_name', '').strip()
    model_from_settings = settings_for_generation.get('model_name', '')
    model_name_to_use = model_from_settings or model_name_input or BASE_GEMENI_MODEL
    
    logging.info(f"Вызов Gemini для генерации (чат {chat_id}, модель: {model_name_to_use})")
    
    tools = []
    if settings_for_generation.get('enable_google_search', False):
        tools.append(types.Tool(googleSearch=types.GoogleSearch()))

    thinking_config = None
    thinking_models = ['gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.5-flash-lite']
    if settings_for_generation.get('enable_thinking', False) and any(m in model_name_to_use.lower() for m in thinking_models):
        thinking_config = types.ThinkingConfig(thinking_budget=-1)

    final_generation_config_parts = {}
    if tools: final_generation_config_parts['tools'] = tools
    if thinking_config: final_generation_config_parts['thinking_config'] = thinking_config
    final_generation_config = types.GenerateContentConfig(**final_generation_config_parts) if final_generation_config_parts else None

    generated_text, generation_error_message = generate_chat_reply_original(
        model_name=model_name_to_use,
        system_prompt=final_system_prompt,
        chat_history=history_data,
        config=final_generation_config
    )

    if generation_error_message:
        logging.error(f"Ошибка Gemini: {generation_error_message}")
        return jsonify({'status': 'error', 'message': f'Ошибка генерации: {generation_error_message}'}), 500
    
    reply_to_send = generated_text.strip() if isinstance(generated_text, str) and generated_text.strip() else ""
    logging.info(f"Gemini успешно сгенерировал ответ для чата {chat_id}")
    
    return jsonify({'status': 'success', 'reply': reply_to_send})

@app.route('/chat/<sint:chat_id>')
def chat_page(chat_id):
    logging.info(f"Запрос GET /chat/{chat_id}")

    settings_to_use = get_chat_settings(chat_id)
    active_character_id = settings_to_use.get('active_character_id')
    
    active_character_data = None
    sticker_prompt_text = "" 
    
    if active_character_id:
        active_character_data = character_utils.get_character(active_character_id)
        if active_character_data:
            enabled_packs = active_character_data.get('enabled_sticker_packs', [])
            sticker_prompt_text = generate_sticker_prompt(enabled_packs)

    current_limit_from_settings = settings_to_use.get('num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
    limit_str = request.args.get('limit', str(current_limit_from_settings))
    try:
        current_limit = int(limit_str)
        if not (0 < current_limit <= CHAT_LIMIT):
            logging.warning(f"Недопустимый лимит {current_limit} из URL, используется {current_limit_from_settings}")
            current_limit = current_limit_from_settings
    except ValueError:
        logging.warning(f"Некорректный лимит '{limit_str}' из URL, используется {current_limit_from_settings}")
        current_limit = current_limit_from_settings

    logging.info(f"Запрос информации для чата {chat_id}")
    chat_info_data, info_error = run_in_telegram_loop(get_chat_info(chat_id))
    if info_error:
        flash(f"Не удалось получить информацию о чате: {info_error}", "warning")
        logging.warning(f"Ошибка получения инфо о чате {chat_id}: {info_error}")

    with auto_mode_lock:
        worker_info = auto_mode_workers.get(chat_id)
        if worker_info and worker_info["thread"] and worker_info["thread"].is_alive():
             auto_mode_status = worker_info["status"] 
        else:
             auto_mode_status = "inactive"
             if chat_id in auto_mode_workers:
                 del auto_mode_workers[chat_id]
    session[f'auto_mode_status_{chat_id}'] = auto_mode_status

    logging.info(f"Запрос истории для чата {chat_id} с лимитом {current_limit} (быстрый режим)")
    history_data, history_error = run_in_telegram_loop(
        get_formatted_history(chat_id, limit=current_limit, settings=settings_to_use, download_media=False)
    )

    all_characters = character_utils.load_characters()
    
    sticker_db = load_sticker_data()
    structured_stickers = structure_sticker_data(sticker_db)

    return render_template(
        'chat.html',
        chat_id=chat_id,
        chat_info=chat_info_data,
        history=history_data if history_data else [],
        history_error=history_error,
        generated_reply=None,  
        generation_error=None, 
        sticker_prompt_text_for_js=sticker_prompt_text,
        structured_sticker_sets=structured_stickers,
        default_model_name=BASE_GEMENI_MODEL,
        current_limit=current_limit,
        auto_mode_status=auto_mode_status,
        chat_settings=settings_to_use,
        all_characters=all_characters,
        active_character_id=active_character_id,
        active_character_data=active_character_data
    )

@app.route('/media/<sint:chat_id>/<int:message_id>')
def get_media(chat_id, message_id):
    """
    Endpoint для асинхронной загрузки медиа для одного сообщения.
    """
    logging.info(f"AJAX-запрос на получение медиа для сообщения {message_id} в чате {chat_id}")
    media_parts, error = run_in_telegram_loop(get_media_for_message(chat_id, message_id))
    
    if error:
        return jsonify({'status': 'error', 'message': error}), 500
    
    return jsonify({'status': 'success', 'parts': media_parts})

@app.route('/update_sticker_status/<sint:chat_id>', methods=['POST'])
def update_sticker_status(chat_id):
    """
    Обновляет статусы стикеров для активного персонажа в этом чате.
    """
    logging.info(f"Запрос POST /update_sticker_status/{chat_id}")

    enabled_codenames = request.form.getlist('sticker_enabled')
    
    chat_settings = get_chat_settings(chat_id)
    character_id = chat_settings.get('active_character_id')

    if not character_id:
        flash("Не выбран персонаж для обновления стикеров.", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    all_characters = character_utils.load_characters()
    if character_id in all_characters:
        all_characters[character_id]['enabled_sticker_packs'] = enabled_codenames
        if character_utils.save_characters(all_characters):
            flash("Настройки стикеров для персонажа сохранены.", "success")
        else:
            flash("Ошибка сохранения настроек стикеров персонажа.", "error")
    else:
        flash("Персонаж не найден.", "error")

    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/start_auto_mode/<sint:chat_id>', methods=['POST'])
def start_auto_mode(chat_id):
    logging.info(f"Запрос POST /start_auto_mode/{chat_id}")

    with auto_mode_lock:
        if chat_id in auto_mode_workers and auto_mode_workers[chat_id]["thread"] and auto_mode_workers[chat_id]["thread"].is_alive():
             flash(f"Авто-режим для чата {chat_id} уже активен или останавливается.", "warning")
        else:
             logging.info(f"Запуск потока авто-режима для чата {chat_id}...")
             stop_event = threading.Event()
             thread = threading.Thread(
                 target=auto_mode_worker,
                 args=(chat_id, stop_event),
                 name=f"AutoMode-{chat_id}",
                 daemon=True 
             )
             
             auto_mode_workers[chat_id] = {
                 "thread": thread, 
                 "stop_event": stop_event, 
                 "status": "active",
                 "bot_last_message_anchor": None 
             }
             
             thread.start()
             session[f'auto_mode_status_{chat_id}'] = "active" 
             flash(f"Авто-режим для чата {chat_id} запущен.", "success")

    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/stop_auto_mode/<sint:chat_id>', methods=['POST'])
def stop_auto_mode(chat_id):
    logging.info(f"Запрос POST /stop_auto_mode/{chat_id}")

    with auto_mode_lock:
        worker_info = auto_mode_workers.get(chat_id)
        if worker_info and worker_info["thread"] and worker_info["thread"].is_alive() and worker_info["status"] == "active":
             logging.info(f"Отправка сигнала остановки потоку авто-режима для чата {chat_id}...")
             worker_info["stop_event"].set()
             worker_info["status"] = "stopping" 
             session[f'auto_mode_status_{chat_id}'] = "stopping"
             flash(f"Авто-режим для чата {chat_id} останавливается...", "info")
        elif worker_info and worker_info["status"] == "stopping":
             flash(f"Авто-режим для чата {chat_id} уже в процессе остановки.", "info")
        else:
             flash(f"Авто-режим для чата {chat_id} не был активен.", "warning")
             if chat_id in auto_mode_workers:
                 del auto_mode_workers[chat_id]
             session[f'auto_mode_status_{chat_id}'] = "inactive"

    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/save_global_settings', methods=['POST'])
def save_global_settings_route():
    """Сохраняет глобальные настройки."""
    logging.info("Запрос POST /save_global_settings")

    try:
        settings_to_save = {
            'media_cleanup_enabled': 'media_cleanup_enabled' in request.form,
            'media_cleanup_days': int(request.form.get('media_cleanup_days', 7)),
        }

        if save_global_settings(settings_to_save):
            flash("Глобальные настройки успешно сохранены.", "success")
            if settings_to_save['media_cleanup_enabled']:
                days = settings_to_save['media_cleanup_days']
                logging.info(f"Запуск очистки кэша по запросу после сохранения настроек (файлы старше {days} дней).")
                cleanup_old_cache_files(directory="media_cache", max_age_days=days)
        else:
            flash("Ошибка при сохранении глобальных настроек.", "error")

    except (ValueError, TypeError) as e:
        flash(f"Ошибка в переданных данных: {e}", "error")

    return redirect(url_for('index'))

@app.route('/save_chat_settings/<sint:chat_id>', methods=['POST'])
def save_chat_settings_route(chat_id):
    """
    Сохраняет продвинутые настройки.
    Может сохранять их только для текущего чата или еще и в дефолтные настройки персонажа.
    """
    logging.info(f"Запрос POST /save_chat_settings/{chat_id}")

    save_action = request.form.get('save_action')
    if not save_action:
        flash("Действие для сохранения не определено.", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    all_chat_settings = load_chat_settings()
    character_id = all_chat_settings.get(chat_id, {}).get('active_character_id')

    if not character_id:
        flash("Активный персонаж не выбран. Настройки не сохранены.", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    try:
        advanced_settings_data = {
            'can_see_photos': 'can_see_photos' in request.form,
            'can_see_videos': 'can_see_videos' in request.form,
            'can_see_audio': 'can_see_audio' in request.form,
            'can_see_files_pdf': 'can_see_files_pdf' in request.form,
            'ignore_all_media': 'ignore_all_media' in request.form, 
            'enable_auto_memory': 'enable_auto_memory' in request.form,
            'auto_mode_check_interval': float(request.form.get('auto_mode_check_interval')),
            'auto_mode_initial_wait': float(request.form.get('auto_mode_initial_wait')),
            'auto_mode_no_reply_timeout': float(request.form.get('auto_mode_no_reply_timeout')),
            'auto_mode_no_reply_suffix': request.form.get('auto_mode_no_reply_suffix', ''),
            'model_name': request.form.get('model_name_advanced', ''),
            'enable_google_search': 'enable_google_search' in request.form,
            'enable_thinking': 'enable_thinking' in request.form,
            'num_messages_to_fetch': int(request.form.get('num_messages_to_fetch')),
            'sticker_choosing_delay_min': float(request.form.get('sticker_choosing_delay_min')),
            'sticker_choosing_delay_max': float(request.form.get('sticker_choosing_delay_max')),
            'base_thinking_delay_s_min': float(request.form.get('base_thinking_delay_s_min')),
            'base_thinking_delay_s_max': float(request.form.get('base_thinking_delay_s_max')),
            'typing_delay_ms_min': float(request.form.get('typing_delay_ms_min')),
            'typing_delay_ms_max': float(request.form.get('typing_delay_ms_max')),
            'max_typing_duration_s': float(request.form.get('max_typing_duration_s')),
            'substitution_chance': float(request.form.get('substitution_chance')),
            'transposition_chance': float(request.form.get('transposition_chance')),
            'skip_chance': float(request.form.get('skip_chance')),
            'lower_chance': float(request.form.get('lower_chance')),
        }
    except (ValueError, TypeError) as e:
        flash(f"Ошибка в числовых данных: {e}", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    
    if chat_id not in all_chat_settings: all_chat_settings[chat_id] = {}
    if 'character_specifics' not in all_chat_settings[chat_id]: all_chat_settings[chat_id]['character_specifics'] = {}
    if character_id not in all_chat_settings[chat_id]['character_specifics']: all_chat_settings[chat_id]['character_specifics'][character_id] = {}
    
    all_chat_settings[chat_id]['character_specifics'][character_id]['advanced_settings'] = advanced_settings_data
    
    save_chat_settings(all_chat_settings)
    logging.info(f"Сохранены настройки для персонажа {character_id} в чате {chat_id}.")

    if save_action == 'save_for_chat_and_default':
        all_characters = character_utils.load_characters()
        if character_id in all_characters:
            all_characters[character_id]['advanced_settings'] = advanced_settings_data
            if character_utils.save_characters(all_characters):
                flash("Настройки сохранены для этого чата И как настройки по умолчанию для персонажа.", "success")
                logging.info(f"Обновлены настройки по умолчанию для персонажа {character_id}.")
            else:
                flash("Настройки для чата сохранены, но не удалось обновить дефолт персонажа!", "error")
        else:
            flash("Персонаж для обновления дефолтных настроек не найден.", "error")
    else:
        flash("Настройки для этого чата успешно сохранены.", "success")

    return redirect(url_for('chat_page', chat_id=chat_id))


@app.route('/reset_chat_settings/<sint:chat_id>', methods=['POST'])
def reset_chat_settings_route(chat_id):
    """
    Сбрасывает специфичные настройки персонажа для этого чата,
    возвращая их к глобальным настройкам персонажа по умолчанию.
    """
    logging.info(f"Запрос POST /reset_chat_settings/{chat_id}")

    all_settings = load_chat_settings()
    character_id = all_settings.get(chat_id, {}).get('active_character_id')

    if not character_id:
        flash("Не выбран персонаж, настройки которого нужно сбросить.", "warning")
        return redirect(url_for('chat_page', chat_id=chat_id))

    if chat_id in all_settings and 'character_specifics' in all_settings[chat_id] and character_id in all_settings[chat_id]['character_specifics']:
        del all_settings[chat_id]['character_specifics'][character_id]
        if not all_settings[chat_id]['character_specifics']:
            del all_settings[chat_id]['character_specifics']
            
        save_chat_settings(all_settings)
        flash("Локальные настройки для персонажа в этом чате сброшены к его значениям по умолчанию.", "success")
    else:
        flash("Для этого персонажа в этом чате и так используются его настройки по умолчанию.", "info")

    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/chat/<sint:chat_id>/set_active_character', methods=['POST'])
def set_active_character(chat_id):
    """Устанавливает активного персонажа для чата."""
    logging.info(f"Запрос POST /chat/{chat_id}/set_active_character")
    character_id = request.form.get('character_id')

    all_settings = load_chat_settings()
    if chat_id not in all_settings:
        all_settings[chat_id] = {}

    all_settings[chat_id]['active_character_id'] = character_id
    save_chat_settings(all_settings)

    character_name = character_utils.get_character(character_id).get('name', 'Неизвестный')
    flash(f"Для этого чата выбран персонаж: '{character_name}'.", "success")
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/character/create', methods=['POST'])
def create_character():
    """Создает нового пустого персонажа."""
    logging.info("Запрос POST /character/create")
    character_name = request.form.get('new_character_name', 'Новый персонаж')
    
    chat_id_str = request.form.get('chat_id') 
    
    new_id = character_utils.create_new_character(character_name)
    if new_id:
        flash(f"Персонаж '{character_name}' успешно создан!", "success")
    else:
        flash("Не удалось создать персонажа.", "error")
    
    try:
        chat_id = int(chat_id_str) if chat_id_str else None
    except (ValueError, TypeError):
        chat_id = None
        
    return redirect(url_for('chat_page', chat_id=chat_id) if chat_id else url_for('index'))


@app.route('/character/save/<character_id>/<sint:chat_id>', methods=['POST'])
def save_character(character_id, chat_id):
    """
    Сохраняет все данные персонажа из формы, А ТАКЖЕ специфичный для чата контекст.
    """
    logging.info(f"Запрос POST /character/save/{character_id} для чата {chat_id}")
    
    characters = character_utils.load_characters()
    if character_id not in characters:
        flash("Персонаж для сохранения не найден.", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    characters[character_id]['name'] = request.form.get('character_name')
    characters[character_id]['personality_prompt'] = request.form.get('personality_prompt')
    characters[character_id]['memory_prompt'] = request.form.get('memory_prompt')
    characters[character_id]['system_commands_prompt'] = request.form.get('system_commands_prompt')
    characters[character_id]['memory_update_prompt'] = request.form.get('memory_update_prompt')

    save_character_success = character_utils.save_characters(characters)

    chat_context_prompt = request.form.get('chat_context_prompt', '')
    all_chat_settings = load_chat_settings()
    
    if chat_id not in all_chat_settings: all_chat_settings[chat_id] = {}
    if 'character_specifics' not in all_chat_settings[chat_id]: all_chat_settings[chat_id]['character_specifics'] = {}
    if character_id not in all_chat_settings[chat_id]['character_specifics']: all_chat_settings[chat_id]['character_specifics'][character_id] = {}

    all_chat_settings[chat_id]['character_specifics'][character_id]['chat_context_prompt'] = chat_context_prompt
    save_chat_settings(all_chat_settings)
    logging.info(f"Контекст для персонажа {character_id} в чате {chat_id} обновлен.")

    if save_character_success:
        flash(f"Данные персонажа '{characters[character_id]['name']}' и контекст чата успешно сохранены.", "success")
    else:
        flash("Контекст чата сохранен, но произошла ошибка при сохранении основных данных персонажа.", "error")

    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/chat/<sint:chat_id>/update_memory', methods=['POST'])
def update_memory_route(chat_id):
    """Маршрут для запуска обновления памяти персонажа."""
    logging.info(f"Запрос POST /chat/{chat_id}/update_memory")
    
    settings_to_use = get_chat_settings(chat_id)
    character_id = settings_to_use.get('active_character_id')
    
    if not character_id:
        flash("Не выбран персонаж для обновления памяти.", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    limit_for_memory = settings_to_use.get('num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
    logging.info(f"Для анализа памяти будет использовано {limit_for_memory} сообщений (из настроек).")

    chat_info, _ = run_in_telegram_loop(get_chat_info(chat_id))
    history, history_error = run_in_telegram_loop(get_formatted_history(chat_id, limit=limit_for_memory, settings=settings_to_use))    

    if history_error:
        flash(f"Ошибка получения истории для анализа: {history_error}", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))
    
    if not history:
        flash("История сообщений пуста, нечего добавлять в память.", "warning")
        return redirect(url_for('chat_page', chat_id=chat_id))

    _, error = character_utils.update_character_memory(
        character_id=character_id,
        chat_name=chat_info.get('name', str(chat_id)),
        is_group=chat_id < 0,
        chat_history=history
    )

    if error:
        flash(f"Ошибка обновления памяти: {error}", "error")
    else:
        flash("Память персонажа успешно обновлена!", "success")

    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/send/<sint:chat_id>', methods=['POST'])
def send_reply(chat_id):
    logging.info(f"Запрос POST /send/{chat_id}")

    message_to_send = request.form.get('message_to_send')

    if not message_to_send or not message_to_send.strip():
        flash("Нет текста для отправки.", "warning")
        return redirect(url_for('chat_page', chat_id=chat_id))

    success, error_message = send_generated_reply(chat_id, message_to_send)

    if success:
        flash("Сообщение успешно отправлено (или все его части)!", "success")
        logging.info(f"Сообщение для чата {chat_id} успешно отправлено через веб-интерфейс.")
    else:
        flash(f"При отправке сообщения произошла ошибка: {error_message}", "error")
        logging.error(f"Ошибка отправки сообщения в чат {chat_id} через веб-интерфейс: {error_message}")

    return redirect(url_for('chat_page', chat_id=chat_id))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Запуск Telegram AI бота.")
    parser.add_argument('--account', type=int, help='Номер аккаунта для автоматического выбора.')
    args = parser.parse_args()
    
    flask_port = 5000 + INSTANCE_NUMBER 

    initialize_gemini()

    global_settings = load_global_settings()
    if global_settings.get('media_cleanup_enabled', True):
        cleanup_days = global_settings.get('media_cleanup_days', 7)
        logging.info(f"Запуск очистки кэша при старте (файлы старше {cleanup_days} дней).")
        cleanup_old_cache_files(directory="media_cache", max_age_days=cleanup_days)
    else:
        logging.info("Автоматическая очистка кэша при старте отключена в настройках.")
    
    selected_session = choose_account_from_console(args.account)
    
    start_telegram_thread(selected_session)
    
    atexit.register(stop_telegram_thread)
    
    logging.info("Ожидание инициализации Telegram (до 60 секунд)...")
    if telegram_ready_event.wait(timeout=60):
        logging.info(Fore.GREEN + "Сигнал готовности Telegram получен. Сервер Flask запускается.")
    else:
        logging.warning(Fore.YELLOW + "Telegram не подал сигнал готовности за 60 секунд. Возможны проблемы с подключением.")
    
    print(Fore.CYAN + f"=== Запуск инстанса #{INSTANCE_NUMBER} ===")
    print(Fore.CYAN + f"Веб-интерфейс будет доступен по адресу: http://127.0.0.1:{flask_port}")

    app.run(debug=True, host='0.0.0.0', port=flask_port, use_reloader=False)