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


init(autoreset=True)
load_dotenv()

import config as app_config

from telegram_utils import (
    get_chats,
    get_chat_info,
    get_formatted_history,
    send_telegram_message,
    disconnect_telegram,
    telegram_main_loop, 
    run_in_telegram_loop,
    STICKER_DB,
    send_sticker_by_codename
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
app.url_map.converters['sint'] = SignedIntConverter
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24)) 

DEFAULT_SYSTEM_PROMPT = """
Чтобы написать сразу несколько коротких сообщений, разделяй их используя ключевое слово {split}
Чтобы твоё сообщение было ответом на сообщение собеседника пиши в начале сообщения ключевое слово answer(ID реального сообщения собеседника на которое хочешь дать свой ответ)
"""
ACCOUNTS_JSON_FILE = 'data/accounts.json'
DEFAULT_SESSION_NAME = 'kadzu'
PROMPT_STORAGE_FILE = 'data/system_prompts.json'
CHAT_SETTINGS_FILE = 'data/chat_settings.json'
STICKER_JSON_FILE = 'data/stickers.json'
CHARTS_LIMIT = 120
CHAT_LIMIT = 10000

gemini_client_global = None
telegram_thread = None 
telegram_ready_event = threading.Event() 

DEFAULT_CHAT_SETTINGS = {
    # Общие
    "num_messages_to_fetch": 65,
    "add_chat_name_prefix": True,
    # Для медиа
    "can_see_photos": True,
    "can_see_videos": True,
    "can_see_audio": True,
    "can_see_files_pdf": True,
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
}

auto_mode_workers = {} 
auto_mode_lock = threading.Lock() 

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

def choose_account_from_console():
    """
    Отображает в консоли список аккаунтов и просит пользователя сделать выбор.
    Возвращает имя выбранного файла сессии.
    """
    accounts = load_accounts()
    if not accounts:
        print(Fore.YELLOW + f"Файл '{ACCOUNTS_JSON_FILE}' не найден или пуст. Используется сессия по умолчанию: '{DEFAULT_SESSION_NAME}'")
        return DEFAULT_SESSION_NAME

    account_list = list(accounts.items())

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
        # --- НАЧАЛО ИЗМЕНЕНИЙ ---
        if os.path.exists(CHAT_SETTINGS_FILE):
            with open(CHAT_SETTINGS_FILE, 'r', encoding='utf-8') as f:
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---
                # Конвертируем ключи обратно в int
                return {int(k): v for k, v in json.load(f).items()}
        return {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logging.warning(f"Не удалось загрузить файл настроек ({CHAT_SETTINGS_FILE}): {e}. Будет использован пустой словарь.")
        return {}

def save_chat_settings(settings_dict):
    """Сохраняет словарь настроек чатов в JSON файл."""
    try:
        # --- НАЧАЛО ИЗМЕНЕНИЙ ---
        os.makedirs(os.path.dirname(CHAT_SETTINGS_FILE), exist_ok=True)
        with open(CHAT_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_dict, f, ensure_ascii=False, indent=4)
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---
    except IOError as e:
        logging.error(f"Ошибка сохранения файла настроек ({CHAT_SETTINGS_FILE}): {e}")
        flash("Не удалось сохранить настройки в файл.", "error")

def get_chat_settings(chat_id):
    """
    Получает настройки для конкретного чата.
    Если для чата нет сохраненных настроек, возвращает настройки по умолчанию.
    Если есть, объединяет их с настройками по умолчанию (чтобы новые ключи настроек подхватывались).
    """
    all_settings = load_chat_settings()
    chat_specific_settings = all_settings.get(chat_id)

    # Создаем копию настроек по умолчанию, чтобы не изменять оригинал
    final_settings = DEFAULT_CHAT_SETTINGS.copy()
    
    # --- НАЧАЛО ИЗМЕНЕНИЙ ---
    # Добавляем новые настройки для режимов
    final_settings['generation_mode'] = 'prompt' # 'prompt' или 'character'
    final_settings['active_character_id'] = None
    # --- КОНЕЦ ИЗМЕНЕНИЙ ---

    if chat_specific_settings:
        # Обновляем значения по умолчанию сохраненными, если они есть
        final_settings.update(chat_specific_settings)

    return final_settings

def load_prompts():
    """Загружает сохраненные системные промпты из JSON файла."""
    try:
        if os.path.exists(PROMPT_STORAGE_FILE):
            with open(PROMPT_STORAGE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
        return {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logging.warning(f"Не удалось загрузить файл промптов ({PROMPT_STORAGE_FILE}): {e}. Будет использован пустой словарь.")
        return {}

def save_prompts(prompts_dict):
    """Сохраняет словарь системных промптов в JSON файл."""
    try:
        with open(PROMPT_STORAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(prompts_dict, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logging.error(f"Ошибка сохранения файла промптов ({PROMPT_STORAGE_FILE}): {e}")
        flash("Не удалось сохранить системный промпт в файл.", "error")

def generate_sticker_prompt():
    """
    Создает строку-инструкцию и список доступных стикеров для системного промпта.
    """
    BASE_STICKER_PROMPT = "Чтобы отправить стикер, используй команду sticker(кодовое_имя_из_списка_ниже)."
    if not STICKER_DB:
        return "" 

    available_stickers_lines = []
    for codename, data in sorted(STICKER_DB.items()):
        if data.get("enabled"):
            line = f"- {codename}"
            if data.get("description"):
                line += f": {data['description']}"
            available_stickers_lines.append(line)
    
    if not available_stickers_lines:
        return "" 

    full_prompt = BASE_STICKER_PROMPT + "\n\nДоступные стикеры:\n" + "\n".join(available_stickers_lines)
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
            app_config.TelegramConfig.API_ID,
            app_config.TelegramConfig.API_HASH,
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

def send_generated_reply(chat_id: int, message_text: str, settings: dict = None):
    """
    Централизованная функция для отправки сгенерированного ответа.
    Обрабатывает разделитель {split}, команды sticker() и смешанный контент.
    ИЗМЕНЕНО: Принимает необязательный словарь настроек.
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

    sticker_pattern = r"sticker\s*\(([\w\d_-]+)\)"
    split_separator = "{split}"

    initial_parts = [p.strip() for p in message_text.split(split_separator) if p.strip()]
    
    tasks_to_send = []
    for part in initial_parts:
        found_stickers = list(re.finditer(sticker_pattern, part, re.IGNORECASE))
        
        if not found_stickers:
            tasks_to_send.append({"type": "text", "content": part})
            continue

        last_index = 0
        for match in found_stickers:
            start, end = match.span()
            if start > last_index:
                text_before = part[last_index:start].strip()
                if text_before:
                    tasks_to_send.append({"type": "text", "content": text_before})
            
            codename = match.group(1)
            tasks_to_send.append({"type": "sticker", "content": codename})
            
            last_index = end
        
        if last_index < len(part):
            text_after = part[last_index:].strip()
            if text_after:
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
        
        if not success:
            all_success = False
            logging.error(f"Ошибка отправки задачи {i+1} ({task['type']}) в чат {chat_id}: {error_message}")
            if first_error_message is None:
                first_error_message = error_message
            break 

        if i < len(tasks_to_send) - 1:
            min_pause = settings_to_use.get('base_thinking_delay_s_min', 1.0)
            max_pause = settings_to_use.get('base_thinking_delay_s_max', 2.0)

            
            if max_pause < min_pause:
                max_pause = min_pause

            delay = random.uniform(min_pause, max_pause)
            
            if delay > 0.05: 
                logging.info(f"Пауза перед следующей частью: {delay:.2f} сек.")
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
    global auto_mode_workers, auto_mode_lock, load_prompts, DEFAULT_SYSTEM_PROMPT
    global BASE_GEMENI_MODEL
    global run_in_telegram_loop, get_formatted_history, generate_chat_reply_original, character_utils

    worker_name = f"AutoMode-{chat_id}"
    logging.info(f"[{worker_name}] Поток запущен с логикой ожидания новых сообщений.")

    last_processed_user_msg_time = None
    last_own_message_sent_time = datetime.now()

    while not stop_event.is_set():
        
        base_chat_settings = get_chat_settings(chat_id)
        settings_for_generation = base_chat_settings.copy() 

        generation_mode = base_chat_settings.get('generation_mode')
        
        if generation_mode == 'character':
            character_id = base_chat_settings.get('active_character_id')
            if character_id:
                character_data = character_utils.get_character(character_id)
                if character_data and character_data.get('advanced_settings'):
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
                final_system_prompt = ""
                model_name_to_use = BASE_GEMENI_MODEL 

                if generation_mode == 'character':
                    character_id = base_chat_settings.get('active_character_id')
                    if character_id:
                        character_data = character_utils.get_character(character_id)
                        if character_data:
                            logging.info(f"[{worker_name}] Работа в режиме 'Персонаж': {character_data.get('name')}")
                            sticker_prompt = generate_sticker_prompt() 
                            final_system_prompt = character_utils.get_full_prompt_for_character(character_id, sticker_prompt)
                        else:
                            logging.error(f"[{worker_name}] Режим 'Персонаж', но данные для ID {character_id} не найдены. Откат к режиму 'Промпт'.")
                            generation_mode = 'prompt'
                    else:
                        logging.warning(f"[{worker_name}] Режим 'Персонаж', но ID не выбран. Откат к режиму 'Промпт'.")
                        generation_mode = 'prompt'

                if generation_mode == 'prompt':
                    logging.info(f"[{worker_name}] Работа в режиме 'Промпт'.")
                    all_prompts = load_prompts()
                    prompt_parts = [all_prompts.get(chat_id, DEFAULT_SYSTEM_PROMPT)]
                    if chat_id not in all_prompts:
                        sticker_prompt = generate_sticker_prompt()
                        if sticker_prompt: prompt_parts.append(sticker_prompt)
                    final_system_prompt = "\n\n".join(p.strip() for p in prompt_parts if p.strip())

                if settings_for_generation.get('add_chat_name_prefix', True):
                    chat_info, _ = run_in_telegram_loop(get_chat_info(chat_id))
                    if chat_info:
                        chat_name = chat_info.get('name', str(chat_id))
                        is_group = chat_id < 0
                        prefix = f"Это чат в группе '{chat_name}'.\n\n" if is_group else f"Это чат с '{chat_name}'.\n\n"
                        final_system_prompt = prefix + final_system_prompt

                if is_timeout_trigger:
                    no_reply_suffix = settings_for_generation.get('auto_mode_no_reply_suffix', DEFAULT_CHAT_SETTINGS['auto_mode_no_reply_suffix'])
                    final_system_prompt += f"\n\n{no_reply_suffix}"

                num_messages = settings_for_generation.get('num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
                full_history, history_error = run_in_telegram_loop(get_formatted_history(chat_id, limit=num_messages, settings=settings_for_generation))

                if history_error or not full_history:
                    logging.error(f"[{worker_name}] Ошибка получения истории для генерации: {history_error}. Пропуск.")
                    stop_event.wait(15)
                    continue

                if generation_mode == 'character' and base_chat_settings.get('active_character_id'):
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
                            character_id = base_chat_settings.get('active_character_id')
                            chat_info, _ = run_in_telegram_loop(get_chat_info(chat_id))
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

                logging.info(f"[{worker_name}] Вызов Gemini для генерации (лимит истории: {num_messages})...")
                generated_text, gen_error = generate_chat_reply_original(
                    model_name=model_name_to_use, system_prompt=final_system_prompt.strip(), chat_history=full_history
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

    return render_template('index.html', chats=chats_data if chats_data else [], error=error)

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
        session['current_chat_id'] = chat_id
        logging.info(f"Выбран чат с ID: {chat_id}")
        session.pop('generated_reply', None)
        session.pop('last_generation_error', None)
        session.pop('chat_info', None) 
        session.pop(f'auto_mode_status_{chat_id}', None)
        return redirect(url_for('chat_page', chat_id=chat_id))
    except ValueError:
        flash("Некорректный ID чата.", "error")
        return redirect(url_for('index'))


@app.route('/generate/<sint:chat_id>', methods=['POST'])
def generate_reply(chat_id):
    """
    Обрабатывает ручную генерацию ответа.
    ИСПРАВЛЕННАЯ ВЕРСИЯ: использует персональные настройки чата для определения
    количества загружаемых сообщений (лимита истории).
    """
    logging.info(f"Запрос POST /generate/{chat_id}")
    if session.get('current_chat_id') != chat_id:
        flash("Ошибка сессии или ID чата. Пожалуйста, выберите чат заново.", "error")
        return redirect(url_for('index'))

    chat_settings = get_chat_settings(chat_id)
    generation_mode = chat_settings.get('generation_mode', 'prompt')

    # Получаем информацию о чате для префикса
    chat_info_data = session.get('chat_info')
    if not chat_info_data:
        chat_info_data, _ = run_in_telegram_loop(get_chat_info(chat_id))
        if chat_info_data: session['chat_info'] = chat_info_data

    final_system_prompt = ""
    settings_for_generation = chat_settings # По умолчанию используем настройки чата

    if generation_mode == 'character':
        character_id = chat_settings.get('active_character_id')
        if not character_id:
            flash("Режим 'Персонаж' активен, но персонаж не выбран!", "error")
            return redirect(url_for('chat_page', chat_id=chat_id))
        
        character_data = character_utils.get_character(character_id)
        if not character_data:
            flash(f"Не удалось загрузить данные для персонажа ID {character_id}", "error")
            return redirect(url_for('chat_page', chat_id=chat_id))

        # Собираем полный промпт из частей персонажа
        final_system_prompt = character_utils.get_full_prompt_for_character(character_id, generate_sticker_prompt())
        
        # Если у персонажа есть свои продвинутые настройки, используем их
        if character_data.get('advanced_settings'):
            # Важно! Мы объединяем их с дефолтными, чтобы не потерять ключи
            char_adv_settings = DEFAULT_CHAT_SETTINGS.copy()
            char_adv_settings.update(character_data['advanced_settings'])
            settings_for_generation = char_adv_settings

    else: # Режим 'prompt'
        # Логика как раньше, но теперь она в блоке else
        all_prompts = load_prompts()
        base_prompt = all_prompts.get(chat_id, "")
        sticker_prompt = generate_sticker_prompt()
        if not base_prompt: # Если для чата нет кастомного промпта, создаем дефолтный
             final_system_prompt = f"{character_utils.DEFAULT_SYSTEM_COMMANDS_PROMPT}\n\n{sticker_prompt}".strip()
        else:
             final_system_prompt = f"{base_prompt}\n\n{sticker_prompt}".strip()

    if settings_for_generation.get('add_chat_name_prefix', True): # По умолчанию включено
        if chat_info_data:
            chat_name = chat_info_data.get('name', str(chat_id))
            is_group = chat_id < 0
            prefix = f"Это чат в группе '{chat_name}'.\n\n" if is_group else f"Это чат с '{chat_name}'.\n\n"
            final_system_prompt = prefix + final_system_prompt
            logging.info(f"Добавлен префикс в системный промпт: '{prefix.strip()}'")

    limit_from_settings = settings_for_generation.get('num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])

    limit_str = request.form.get('history_limit', str(limit_from_settings))
    try:
        current_limit = int(limit_str)
        if not (0 < current_limit <= CHAT_LIMIT):
            logging.warning(f"Недопустимый лимит {current_limit} из формы, используется {limit_from_settings}")
            current_limit = limit_from_settings
    except ValueError:
        logging.warning(f"Некорректный лимит '{limit_str}' из формы, используется {limit_from_settings}")
        current_limit = limit_from_settings

    with auto_mode_lock:
        worker_info = auto_mode_workers.get(chat_id)
        auto_mode_status = worker_info["status"] if worker_info and worker_info["thread"] and worker_info["thread"].is_alive() else "inactive"
    if not gemini_client_global:
        flash("Клиент Gemini не инициализирован. Генерация невозможна.", "error")
        chat_info_data = session.get('chat_info')
        info_error = None
        if not chat_info_data:
            chat_info_data, info_error = run_in_telegram_loop(get_chat_info(chat_id))
            if info_error: logging.warning(f"Ошибка получения инфо о чате {chat_id} при ошибке Gemini: {info_error}")
            elif chat_info_data: session['chat_info'] = chat_info_data
        history_data, history_error = run_in_telegram_loop(get_formatted_history(chat_id, limit=current_limit, settings=settings_for_generation))
        if history_error: logging.error(f"Ошибка истории при ошибке Gemini: {history_error}")
        return render_template(
            'chat.html',
            chat_id=chat_id,
            chat_info=chat_info_data,
            history=history_data if history_data else [],
            history_error=history_error,
            generated_reply=None,
            generation_error="Клиент Gemini не инициализирован.",
            default_system_prompt=request.form.get('system_prompt', DEFAULT_SYSTEM_PROMPT),
            default_model_name=request.form.get('model_name', BASE_GEMENI_MODEL),
            current_limit=current_limit,
            auto_mode_status=auto_mode_status
        )
    system_prompt_from_form = request.form.get('system_prompt', DEFAULT_SYSTEM_PROMPT)
    model_name_input = request.form.get('model_name', '').strip()
    model_name_to_use = model_name_input if model_name_input else BASE_GEMENI_MODEL
    logging.info(f"Получение истории для генерации (чат {chat_id}, лимит: {current_limit})")
    history_data, history_error_for_render = run_in_telegram_loop(get_formatted_history(chat_id, limit=current_limit, settings=settings_for_generation))
    chat_info_data = session.get('chat_info')
    if not chat_info_data:
        chat_info_data, _ = run_in_telegram_loop(get_chat_info(chat_id))
        if chat_info_data: session['chat_info'] = chat_info_data
    if history_error_for_render:
        flash(f"Ошибка получения истории для генерации: {history_error_for_render}", "error")
        logging.error(f"Ошибка истории перед генерацией: {history_error_for_render}")
        return render_template(
            'chat.html',
            chat_id=chat_id,
            chat_info=chat_info_data,
            history=history_data if history_data else [],
            history_error=history_error_for_render,
            generated_reply=None,
            generation_error=f"Ошибка получения истории: {history_error_for_render}",
            loaded_system_prompt=system_prompt_from_form,
            default_model_name=model_name_to_use,
            current_limit=current_limit,
            auto_mode_status=auto_mode_status,
            generation_mode=generation_mode,
            all_characters=character_utils.load_characters(),
            active_character_id=chat_settings.get('active_character_id'),
            active_character_data=character_utils.get_character(chat_settings.get('active_character_id')) if generation_mode == 'character' else None,
            chat_settings=chat_settings
        )
    if not history_data:
        flash("История чата пуста, генерация невозможна.", "warning")
        logging.warning(f"Попытка генерации для чата {chat_id} с пустой историей.")
        return render_template(
            'chat.html',
            chat_id=chat_id,
            chat_info=chat_info_data,
            history=[],
            history_error=None,
            generated_reply=None,
            generation_error="История чата пуста.",
            loaded_system_prompt=system_prompt_from_form,
            default_model_name=model_name_to_use,
            current_limit=current_limit,
            auto_mode_status=auto_mode_status,
            generation_mode=generation_mode,
            all_characters=character_utils.load_characters(),
            active_character_id=chat_settings.get('active_character_id'),
            active_character_data=character_utils.get_character(chat_settings.get('active_character_id')) if generation_mode == 'character' else None,
            chat_settings=chat_settings
        )
    logging.info(f"Вызов Gemini для генерации (чат {chat_id}, модель: {model_name_to_use})")
    generated_text, generation_error_message = generate_chat_reply_original(
        model_name=model_name_to_use,
        system_prompt=final_system_prompt,
        chat_history=history_data 
    )
    if generation_error_message:
        flash(f"Ошибка генерации ответа: {generation_error_message}", "error")
        logging.error(f"Ошибка Gemini: {generation_error_message}")
        return render_template(
            'chat.html',
            chat_id=chat_id,
            chat_info=chat_info_data,
            history=history_data,
            history_error=history_error_for_render,
            generated_reply=None,
            generation_error=generation_error_message,
            loaded_system_prompt=system_prompt_from_form,
            default_model_name=model_name_to_use,
            current_limit=current_limit,
            auto_mode_status=auto_mode_status,
            generation_mode=generation_mode,
            all_characters=character_utils.load_characters(),
            active_character_id=chat_settings.get('active_character_id'),
            active_character_data=character_utils.get_character(chat_settings.get('active_character_id')) if generation_mode == 'character' else None,
            chat_settings=chat_settings
        )
    else:
        flash("Ответ успешно сгенерирован!", "success")
        logging.info(f"Gemini успешно сгенерировал ответ для чата {chat_id}")
        logging.info(f"ОТЛАДКА: Тип generated_text: {type(generated_text)}")
        logging.info(f"ОТЛАДКА: Значение generated_text перед strip: '{generated_text}'")
        reply_to_render = None
        if isinstance(generated_text, str):
            stripped_text = generated_text.strip()
            if stripped_text:
                reply_to_render = stripped_text
                logging.info(f"ОТЛАДКА: generated_text является непустой строкой. Будет передан в шаблон: '{reply_to_render}'")
            else:
                logging.warning(f"ОТЛАДКА: Сгенерированный текст '{generated_text}' пуст или состоит только из пробелов. В шаблон будет передан None.")
                flash("Сгенерированный ответ оказался пустым.", "warning")
        else:
            logging.warning(f"ОТЛАДКА: Сгенерированный текст не является строкой (тип: {type(generated_text)}). В шаблон будет передан None.")
            flash("Сгенерированный ответ имеет неверный формат.", "warning")
        return render_template(
            'chat.html',
            chat_id=chat_id,
            chat_info=chat_info_data,
            history=history_data,
            history_error=history_error_for_render,
            generated_reply=reply_to_render,
            generation_error=None,
            loaded_system_prompt=system_prompt_from_form,
            default_model_name=model_name_to_use,
            current_limit=current_limit,
            auto_mode_status=auto_mode_status,
            generation_mode=generation_mode,
            all_characters=character_utils.load_characters(),
            active_character_id=chat_settings.get('active_character_id'),
            active_character_data=character_utils.get_character(chat_settings.get('active_character_id')) if generation_mode == 'character' else None,
            chat_settings=chat_settings
        )

@app.route('/chat/<sint:chat_id>')
def chat_page(chat_id):
    logging.info(f"Запрос GET /chat/{chat_id}")
    if session.get('current_chat_id') != chat_id:
         flash("ID чата в URL не совпадает с выбранным. Пожалуйста, выберите чат заново.", "warning")
         session['current_chat_id'] = chat_id
         session.pop('chat_info', None) 

    chat_settings = get_chat_settings(chat_id)
    generation_mode = chat_settings.get('generation_mode', 'prompt')
    active_character_id = chat_settings.get('active_character_id')
    
    settings_to_use = chat_settings
    active_character_data = None
    
    if generation_mode == 'character' and active_character_id:
        active_character_data = character_utils.get_character(active_character_id)
        if active_character_data and active_character_data.get('advanced_settings'):
            # Объединяем дефолтные настройки с персональными настройками персонажа
            char_adv_settings = DEFAULT_CHAT_SETTINGS.copy()
            char_adv_settings.update(active_character_data['advanced_settings'])
            settings_to_use = char_adv_settings

    current_limit_from_settings = settings_to_use.get('num_messages_to_fetch', DEFAULT_CHAT_SETTINGS['num_messages_to_fetch'])
    
    limit_str = request.args.get('limit', str(current_limit_from_settings))
    try:
        current_limit = int(limit_str)
        if not (0 < current_limit <= CHAT_LIMIT):
            logging.warning(f"Недопустимый лимит {current_limit} из URL, используется {current_limit}")
            current_limit = current_limit_from_settings
    except ValueError:
        logging.warning(f"Некорректный лимит '{limit_str}' из URL, используется {current_limit}")
        current_limit = current_limit_from_settings

    chat_info_data = session.get('chat_info')
    info_error = None
    if not chat_info_data:
        logging.info(f"Запрос истории для чата {chat_id} с лимитом {current_limit}")
        chat_info_data, info_error = run_in_telegram_loop(get_chat_info(chat_id))
        if info_error:
            flash(f"Не удалось получить информацию о чате: {info_error}", "warning")
            logging.warning(f"Ошибка получения инфо о чате {chat_id}: {info_error}")
        elif chat_info_data:
             session['chat_info'] = chat_info_data
    else:
        logging.debug(f"Используем кешированную информацию для чата {chat_id}")

    with auto_mode_lock:
        worker_info = auto_mode_workers.get(chat_id)
        if worker_info and worker_info["thread"] and worker_info["thread"].is_alive():
             auto_mode_status = worker_info["status"] 
        else:
             auto_mode_status = "inactive"
             if chat_id in auto_mode_workers:
                 del auto_mode_workers[chat_id]

    session[f'auto_mode_status_{chat_id}'] = auto_mode_status

    logging.info(f"Запрос истории для чата {chat_id}")
    history_data, history_error = run_in_telegram_loop(get_formatted_history(chat_id, limit=current_limit, settings=settings_to_use))
    if history_error:
        logging.error(f"Ошибка получения истории для чата {chat_id}: {history_error}")

    all_prompts = load_prompts()
    sticker_prompt_text = generate_sticker_prompt()
    loaded_system_prompt = all_prompts.get(chat_id, character_utils.DEFAULT_SYSTEM_COMMANDS_PROMPT)

    # Загружаем всех персонажей для селектора
    all_characters = character_utils.load_characters()
    sticker_prompt_text = generate_sticker_prompt()

    if chat_id in all_prompts:
        loaded_system_prompt = all_prompts[chat_id]
        logging.info(f"Загружен сохраненный системный промпт для чата {chat_id}.")
    else:
        if sticker_prompt_text:
            loaded_system_prompt = f"{DEFAULT_SYSTEM_PROMPT}\n\n{sticker_prompt_text}".strip()
        else:
            loaded_system_prompt = DEFAULT_SYSTEM_PROMPT
        logging.info(f"Используется дефолтный промпт + промпт для стикеров для чата {chat_id}.")

    sticker_db = load_sticker_data()
    sticker_packs_for_template = sorted(
        [{"codename": name, **data} for name, data in sticker_db.items()],
        key=lambda x: x['codename']
    )

    return render_template(
        'chat.html',
        chat_id=chat_id,
        chat_info=chat_info_data,
        history=history_data if history_data else [],
        history_error=history_error,
        generated_reply=None, 
        generation_error=None,
        loaded_system_prompt=loaded_system_prompt, 
        sticker_prompt_text_for_js=sticker_prompt_text,
        sticker_packs=sticker_packs_for_template,
        default_model_name=BASE_GEMENI_MODEL,
        current_limit=current_limit,
        auto_mode_status=auto_mode_status,
        chat_settings=settings_to_use, 
        generation_mode=generation_mode,
        all_characters=all_characters,
        active_character_id=active_character_id,
        active_character_data=active_character_data
    )

@app.route('/update_sticker_status/<sint:chat_id>', methods=['POST'])
def update_sticker_status(chat_id):
    """
    Обновляет статусы стикеров для чата ИЛИ для активного персонажа.
    """
    logging.info(f"Запрос POST /update_sticker_status/{chat_id}")
    if session.get('current_chat_id') != chat_id:
        flash("Ошибка сессии при обновлении статусов стикеров.", "error")
        return redirect(url_for('index'))

    enabled_codenames = request.form.getlist('sticker_enabled')
    
    chat_settings = get_chat_settings(chat_id)
    generation_mode = chat_settings.get('generation_mode', 'prompt')

    if generation_mode == 'character':
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

    else: # Режим 'prompt'
        # Старая логика: обновляем глобальную базу стикеров
        sticker_data = load_sticker_data()
        updated = False
        for codename, data in sticker_data.items():
            was_enabled = data.get('enabled', False)
            is_now_enabled = codename in enabled_codenames
            
            if was_enabled != is_now_enabled:
                sticker_data[codename]['enabled'] = is_now_enabled
                updated = True
        
        if updated:
            if save_sticker_data(sticker_data):
                flash("Глобальные статусы наборов стикеров успешно обновлены.", "success")
                from telegram_utils import load_sticker_db
                load_sticker_db() # Перезагружаем в память
            else:
                flash("Ошибка при сохранении статусов стикеров.", "error")
        else:
            flash("Изменений в статусах стикеров не было.", "info")

    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/start_auto_mode/<sint:chat_id>', methods=['POST'])
def start_auto_mode(chat_id):
    logging.info(f"Запрос POST /start_auto_mode/{chat_id}")
    if session.get('current_chat_id') != chat_id:
        flash("Ошибка сессии или ID чата при запуске авто-режима.", "error")
        return redirect(url_for('index'))

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
    if session.get('current_chat_id') != chat_id:
        flash("Ошибка сессии или ID чата при остановке авто-режима.", "error")
        return redirect(url_for('index'))

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

@app.route('/save_prompt/<sint:chat_id>', methods=['POST'])
def save_prompt(chat_id):
    logging.info(f"Запрос POST /save_prompt/{chat_id}")
    if session.get('current_chat_id') != chat_id:
        flash("Ошибка сессии или ID чата при сохранении промпта.", "error")
        return redirect(url_for('index'))

    prompt_to_save = request.form.get('prompt_to_save')

    if prompt_to_save is None: 
         flash("Ошибка: не получен текст промпта для сохранения.", "error")
         return redirect(url_for('chat_page', chat_id=chat_id))

    logging.info(f"Сохранение системного промпта для чата {chat_id}")
    all_prompts = load_prompts()
    all_prompts[chat_id] = prompt_to_save 
    save_prompts(all_prompts) 

    flash(f"Системный промпт для чата {chat_id} успешно сохранен.", "success")
    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/save_chat_settings/<sint:chat_id>', methods=['POST'])
def save_chat_settings_route(chat_id):
    """
    Сохраняет продвинутые настройки для чата ИЛИ для активного персонажа в чате.
    """
    logging.info(f"Запрос POST /save_chat_settings/{chat_id}")
    if session.get('current_chat_id') != chat_id:
        flash("Ошибка сессии при сохранении настроек.", "error")
        return redirect(url_for('index'))

    chat_settings = get_chat_settings(chat_id)
    generation_mode = chat_settings.get('generation_mode', 'prompt')
    
    settings_data = {}
    try:
        # Сначала собираем все данные из формы в словарь
        settings_data['can_see_photos'] = 'can_see_photos' in request.form
        settings_data['can_see_videos'] = 'can_see_videos' in request.form
        settings_data['can_see_audio'] = 'can_see_audio' in request.form
        settings_data['can_see_files_pdf'] = 'can_see_files_pdf' in request.form
        settings_data['add_chat_name_prefix'] = 'add_chat_name_prefix' in request.form
        settings_data['num_messages_to_fetch'] = int(request.form.get('num_messages_to_fetch'))
        settings_data['auto_mode_check_interval'] = float(request.form.get('auto_mode_check_interval'))
        settings_data['auto_mode_initial_wait'] = float(request.form.get('auto_mode_initial_wait'))
        settings_data['auto_mode_no_reply_timeout'] = float(request.form.get('auto_mode_no_reply_timeout'))
        settings_data['auto_mode_no_reply_suffix'] = request.form.get('auto_mode_no_reply_suffix', '')
        settings_data['sticker_choosing_delay_min'] = float(request.form.get('sticker_choosing_delay_min'))
        settings_data['sticker_choosing_delay_max'] = float(request.form.get('sticker_choosing_delay_max'))
        settings_data['typing_delay_ms_min'] = float(request.form.get('typing_delay_ms_min'))
        settings_data['typing_delay_ms_max'] = float(request.form.get('typing_delay_ms_max'))
        settings_data['base_thinking_delay_s_min'] = float(request.form.get('base_thinking_delay_s_min'))
        settings_data['base_thinking_delay_s_max'] = float(request.form.get('base_thinking_delay_s_max'))
        settings_data['max_typing_duration_s'] = float(request.form.get('max_typing_duration_s'))

    except (ValueError, TypeError) as e:
        logging.error(f"Ошибка приведения типов при сохранении настроек: {e}")
        flash("Ошибка: введено неверное значение в одном из полей.", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    if generation_mode == 'character':
        character_id = chat_settings.get('active_character_id')
        if not character_id:
            flash("Не выбран персонаж для сохранения настроек.", "error")
            return redirect(url_for('chat_page', chat_id=chat_id))
        
        all_characters = character_utils.load_characters()
        if character_id in all_characters:
            all_characters[character_id]['advanced_settings'] = settings_data
            if character_utils.save_characters(all_characters):
                flash("Продвинутые настройки для персонажа успешно сохранены.", "success")
            else:
                flash("Ошибка сохранения настроек персонажа.", "error")
        else:
            flash("Персонаж для сохранения настроек не найден.", "error")

    else: # Режим 'prompt'
        all_chat_settings = load_chat_settings()
        # Сохраняем только продвинутые настройки, не трогая режим и ID персонажа
        if chat_id not in all_chat_settings:
            all_chat_settings[chat_id] = {}
        all_chat_settings[chat_id].update(settings_data)
        save_chat_settings(all_chat_settings)
        flash("Продвинутые настройки для чата успешно сохранены.", "success")

    return redirect(url_for('chat_page', chat_id=chat_id))


@app.route('/reset_chat_settings/<sint:chat_id>', methods=['POST'])
def reset_chat_settings_route(chat_id):
    """Сбрасывает настройки чата до значений по умолчанию."""
    logging.info(f"Запрос POST /reset_chat_settings/{chat_id}")
    if session.get('current_chat_id') != chat_id:
        flash("Ошибка сессии при сбросе настроек.", "error")
        return redirect(url_for('index'))

    all_settings = load_chat_settings()
    if chat_id in all_settings:
        del all_settings[chat_id]
        save_chat_settings(all_settings)
        flash("Настройки для этого чата сброшены к значениям по умолчанию.", "success")
    else:
        flash("Для этого чата и так используются настройки по умолчанию.", "info")

    return redirect(url_for('chat_page', chat_id=chat_id))

@app.route('/chat/<sint:chat_id>/set_generation_mode', methods=['POST'])
def set_generation_mode(chat_id):
    """Переключает режим генерации для чата."""
    logging.info(f"Запрос POST /chat/{chat_id}/set_generation_mode")
    if session.get('current_chat_id') != chat_id:
        return jsonify({"success": False, "error": "Session error"}), 403

    mode = request.form.get('mode')
    if mode not in ['prompt', 'character']:
        return jsonify({"success": False, "error": "Invalid mode"}), 400

    all_settings = load_chat_settings()
    if chat_id not in all_settings:
        all_settings[chat_id] = {}
    
    all_settings[chat_id]['generation_mode'] = mode
    
    # Если переключаемся на персонажа, но ни один не выбран, выберем первого в списке
    if mode == 'character' and not all_settings[chat_id].get('active_character_id'):
        characters = character_utils.load_characters()
        if characters:
            first_char_id = next(iter(characters))
            all_settings[chat_id]['active_character_id'] = first_char_id

    save_chat_settings(all_settings)
    flash(f"Режим для чата переключен на '{mode}'.", "success")
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
    all_settings[chat_id]['generation_mode'] = 'character' # Принудительно ставим режим
    save_chat_settings(all_settings)

    character_name = character_utils.get_character(character_id).get('name', 'Неизвестный')
    flash(f"Для этого чата выбран персонаж: '{character_name}'.", "success")
    return redirect(url_for('chat_page', chat_id=chat_id))


@app.route('/character/create', methods=['POST'])
def create_character():
    """Создает нового пустого персонажа."""
    logging.info("Запрос POST /character/create")
    character_name = request.form.get('new_character_name', 'Новый персонаж')
    new_id = character_utils.create_new_character(character_name)
    if new_id:
        flash(f"Персонаж '{character_name}' успешно создан!", "success")
    else:
        flash("Не удалось создать персонажа.", "error")
    
    # Возвращаемся на страницу чата, с которого пришли
    chat_id = session.get('current_chat_id')
    return redirect(url_for('chat_page', chat_id=chat_id) if chat_id else url_for('index'))


@app.route('/character/save/<character_id>', methods=['POST'])
def save_character(character_id):
    """Сохраняет все данные персонажа из формы."""
    logging.info(f"Запрос POST /character/save/{character_id}")
    
    characters = character_utils.load_characters()
    if character_id not in characters:
        flash("Персонаж для сохранения не найден.", "error")
        return redirect(url_for('chat_page', chat_id=session.get('current_chat_id')))

    # Собираем данные из формы
    characters[character_id]['name'] = request.form.get('character_name')
    characters[character_id]['personality_prompt'] = request.form.get('personality_prompt')
    characters[character_id]['memory_prompt'] = request.form.get('memory_prompt')
    characters[character_id]['system_commands_prompt'] = request.form.get('system_commands_prompt')
    characters[character_id]['memory_update_prompt'] = request.form.get('memory_update_prompt')

    if character_utils.save_characters(characters):
        flash(f"Данные персонажа '{characters[character_id]['name']}' успешно сохранены.", "success")
    else:
        flash("Ошибка при сохранении данных персонажа.", "error")

    return redirect(url_for('chat_page', chat_id=session.get('current_chat_id')))

@app.route('/chat/<sint:chat_id>/update_memory', methods=['POST'])
def update_memory_route(chat_id):
    """Маршрут для запуска обновления памяти персонажа."""
    logging.info(f"Запрос POST /chat/{chat_id}/update_memory")
    
    chat_settings = get_chat_settings(chat_id)
    character_id = chat_settings.get('active_character_id')
    
    if not character_id:
        flash("Не выбран персонаж для обновления памяти.", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    active_character_data = character_utils.get_character(character_id)
    if not active_character_data:
        flash(f"Не найдены данные для персонажа ID {character_id}.", "error")
        return redirect(url_for('chat_page', chat_id=chat_id))

    settings_to_use = chat_settings 
    if active_character_data.get('advanced_settings'):

        char_adv_settings = DEFAULT_CHAT_SETTINGS.copy()
        char_adv_settings.update(active_character_data['advanced_settings'])
        settings_to_use = char_adv_settings
    
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
    if session.get('current_chat_id') != chat_id:
        flash("Ошибка сессии или ID чата. Пожалуйста, выберите чат заново.", "error")
        return redirect(url_for('index'))

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
    
    initialize_gemini()
    
    selected_session = choose_account_from_console()
    
    start_telegram_thread(selected_session)
    
    atexit.register(stop_telegram_thread)
    
    logging.info("Ожидание инициализации Telegram (до 60 секунд)...")
    if telegram_ready_event.wait(timeout=60):
        logging.info(Fore.GREEN + "Сигнал готовности Telegram получен. Сервер Flask запускается.")
    else:
        logging.warning(Fore.YELLOW + "Telegram не подал сигнал готовности за 60 секунд. Возможны проблемы с подключением.")
    
    app.run(debug=True, host='0.0.0.0', port=5001, use_reloader=False)